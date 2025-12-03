import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import asyncio
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import Optional, List, Set
import math

# 加载环境变量
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
FMP_API_KEY = os.getenv('FMP_API_KEY')

# *** 核心：全局唯一接口地址 (Stable) ***
BASE_URL = "https://financialmodelingprep.com/stable"

# --- 全局状态 ---
PRIVACY_MODE = {}

# --- 白名单 ---
HARD_TECH_TICKERS = ["RKLB", "LUNR", "ASTS", "SPCE", "PLTR", "IONQ", "RGTI", "DNA", "JOBY", "ACHR", "BABA", "NIO", "XPEV", "LI", "TSLA", "NVDA", "AMD", "MSFT", "GOOG", "GOOGL", "AMZN"]

# --- 关键词词典 ---
BLUE_OCEAN_KEYWORDS = ["aerospace", "defense", "space", "satellite", "rocket", "quantum"]
HARD_TECH_KEYWORDS = ["semiconductor", "artificial intelligence", "software", "auto", "biotech", "internet"]

# --- 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("ValuationBot")

# --- 1. 异步数据工具函数 ---

async def get_json_safely(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, timeout=10) as response:
            if response.status != 200:
                logger.warning(f"API Status {response.status} for {url}")
                return None
            try:
                data = await response.json()
            except Exception:
                return None
            if isinstance(data, dict) and "Error Message" in data:
                return None
            return data
    except Exception:
        return None

async def get_treasury_rates(session: aiohttp.ClientSession):
    today = datetime.now()
    start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")
    url = f"{BASE_URL}/treasury-rates?from={start_date}&to={end_date}&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url)
    if data and isinstance(data, list) and len(data) > 0:
        return data[0]
    return None

async def get_company_profile_smart(session: aiohttp.ClientSession, ticker: str):
    url_profile = f"{BASE_URL}/profile?symbol={ticker}&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url_profile)
    if data and isinstance(data, list) and len(data) > 0:
        return data[0]
    
    url_screener = f"{BASE_URL}/stock-screener?symbol={ticker}&apikey={FMP_API_KEY}"
    data_scr = await get_json_safely(session, url_screener)
    if data_scr and isinstance(data_scr, list) and len(data_scr) > 0:
        item = data_scr[0]
        return {
            "symbol": item.get("symbol"),
            "price": item.get("price"),
            "beta": item.get("beta"),
            "mktCap": item.get("marketCap"),
            "companyName": item.get("companyName"),
            "industry": item.get("industry"), 
            "sector": item.get("sector"),      
            "description": "Fetched via Screener",
            "image": "N/A"
        }
    return None

async def get_fmp_data(session: aiohttp.ClientSession, endpoint: str, ticker: str, params: str = ""):
    url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey={FMP_API_KEY}"
    if params: url += f"&{params}"
    return await get_json_safely(session, url)

async def get_estimates_data(session: aiohttp.ClientSession, ticker: str):
    url = f"{BASE_URL}/analyst-estimates?symbol={ticker}&period=annual&limit=10&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url)
    return data if data else []

async def get_earnings_data(session: aiohttp.ClientSession, ticker: str):
    url = f"{BASE_URL}/earnings?symbol={ticker}&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url)
    return data if data else []

def format_percent(num):
    return f"{num * 100:.2f}%" if num is not None and isinstance(num, (int, float)) else "N/A"

def format_num(num):
    return f"{num:.2f}" if num is not None and isinstance(num, (int, float)) else "N/A"

def format_market_cap(num):
    if num is None or num == 0: return "N/A"
    if num >= 1e12: return f"${num/1e12:.2f}T"
    if num >= 1e9: return f"${num/1e9:.2f}B"
    return f"${num/1e6:.2f}M"

SECTOR_EBITDA_MEDIAN = {
    "Technology": 32.0, "Consumer Electronics": 25.0, "Communication Services": 20.0,
    "Healthcare": 18.0, "Financial Services": 12.0, "Energy": 10.0,
    "Utilities": 12.0, "Unknown": 18.0
}

def get_sector_benchmark(sector):
    if not sector: return 18.0
    for key in SECTOR_EBITDA_MEDIAN:
        if key.lower() in str(sector).lower(): return SECTOR_EBITDA_MEDIAN[key]
    return 18.0

class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        self.market_regime = "未知"
        self.risk_var = "N/A"  
        self.logs = []  
        self.flags = []  
        self.strategy = "数据不足 (未命中任何策略模型)" 
        self.fcf_yield_display = "N/A" 
        self.fcf_yield_api = None 
        
        # 信号篮子 (用于策略判断)
        self.signals = set()

    def extract(self, source, key, desc, default=None, required=True):
        val = source.get(key)
        if val is None:
            if default is not None:
                return default
            elif not required:
                return None
            else:
                logger.warning(f"[Missing] {desc} ({key}) is None!")
                return None
        else:
            return val

    async def fetch_data(self, session: aiohttp.ClientSession):
        logger.info(f"--- Analysis Start: {self.ticker} ---")
        task_profile = get_company_profile_smart(session, self.ticker)
        task_treasury = get_treasury_rates(session)
        tasks_generic = {
            "quote": get_fmp_data(session, "quote", self.ticker, ""),
            "metrics": get_fmp_data(session, "key-metrics-ttm", self.ticker, ""),
            "ratios": get_fmp_data(session, "ratios-ttm", self.ticker, ""),
            "growth": get_fmp_data(session, "financial-growth", self.ticker, "period=annual&limit=1"),
            "bs": get_fmp_data(session, "balance-sheet-statement", self.ticker, "limit=1"),
            "cf": get_fmp_data(session, "cash-flow-statement", self.ticker, "period=quarter&limit=4"), 
            "vix": get_fmp_data(session, "quote", "^VIX", ""),
            "earnings": get_earnings_data(session, self.ticker),
            "estimates": get_estimates_data(session, self.ticker)
        }
        profile_data, treasury_data, *generic_results = await asyncio.gather(task_profile, task_treasury, *tasks_generic.values())
        self.data = dict(zip(tasks_generic.keys(), generic_results))
        self.data["profile"] = profile_data 
        self.data["treasury"] = treasury_data 
        
        success_keys = []
        for k in tasks_generic.keys():
            raw = self.data[k]
            list_keys = ["earnings", "estimates", "cf"]
            if k in list_keys:
                if isinstance(raw, list) and len(raw) > 0:
                    self.data[k] = raw
                    success_keys.append(k)
                else:
                    self.data[k] = []
            else:
                if isinstance(raw, list) and len(raw) > 0:
                    self.data[k] = raw[0]
                    success_keys.append(k)
                elif isinstance(raw, list) and len(raw) == 0:
                    self.data[k] = {}
                elif raw is None:
                    self.data[k] = {}
                else:
                    success_keys.append(k)
        
        # Log API Status
        total_endpoints = len(tasks_generic)
        failed_count = total_endpoints - len(success_keys)
        logger.info(f"[API Status] Success: {len(success_keys)} | Failed: {failed_count} endpoints.")
        
        return self.data["profile"] is not None

    def analyze(self):
        try:
            # 数据解包
            p = self.data.get("profile", {}) or {}
            q = self.data.get("quote", {}) or {}
            m = self.data.get("metrics", {}) or {} 
            r = self.data.get("ratios", {}) or {}
            g = self.data.get("growth", {}) or {} 
            t = self.data.get("treasury", {}) or {} 
            vix_data = self.data.get("vix", {}) or {}
            earnings_raw = self.data.get("earnings", []) or []
            cf_list = self.data.get("cf", []) or [] 
            estimates = self.data.get("estimates", []) or []
            bs = self.data.get("bs", {}) or {}
            
            if not p: return None

            # === 1. 基础数据收集 ===
            price = self.extract(q, "price", "Quote Price", default=p.get("price"))
            price_200ma = self.extract(q, "priceAvg200", "200 Day MA", required=False)
            sector = self.extract(p, "sector", "Sector", default="Unknown")
            industry = self.extract(p, "industry", "Industry", default="Unknown")
            beta = self.extract(p, "beta", "Beta", default=1.0)
            m_cap = self.extract(q, "marketCap", "MarketCap", default=p.get("mktCap"))
            
            ev_ebitda = self.extract(r, "enterpriseValueMultipleTTM", "EV/EBITDA", required=False)
            if ev_ebitda is None: ev_ebitda = self.extract(m, "enterpriseValueOverEBITDATTM", "EV/EBITDA", required=False)
            
            fcf_yield_api = self.extract(m, "freeCashFlowYieldTTM", "FCF Yield", required=False)
            self.fcf_yield_api = fcf_yield_api 
            
            roic = self.extract(m, "returnOnInvestedCapitalTTM", "ROIC", required=False)
            net_margin = self.extract(r, "netProfitMarginTTM", "Net Margin", required=False)
            ps_ratio = self.extract(r, "priceToSalesRatioTTM", "P/S", required=False)
            peg_ttm = self.extract(r, "priceToEarningsGrowthRatioTTM", "PEG TTM", required=False)
            pe_ttm = self.extract(r, "priceToEarningsRatioTTM", "PE TTM", required=False)
            ni_growth = self.extract(g, "netIncomeGrowth", "NI Growth", required=False)
            rev_growth = self.extract(g, "revenueGrowth", "Rev Growth", required=False)
            
            # 【提前计算】盈利检查 (用于决定后续估值逻辑)
            eps_ttm = r.get("netIncomePerShareTTM") or m.get("netIncomePerShareTTM")
            is_profitable_strict = (eps_ttm is not None and eps_ttm > 0)

            # Log Earnings
            today_str = datetime.now().strftime("%Y-%m-%d")
            past_earnings_for_log = []
            if isinstance(earnings_raw, list):
                past_earnings_for_log = [e for e in earnings_raw if e.get("date", "9999-99-99") <= today_str]
            if past_earnings_for_log:
                sorted_earnings_for_check = sorted(past_earnings_for_log, key=lambda x: x.get("date", "0000-00-00"), reverse=True)
                latest_q = sorted_earnings_for_check[0]
                val = latest_q.get("epsActual")
                logger.info(f"[Earnings] Latest: {latest_q.get('date')} | EPS: {val}")
            else:
                logger.info("[Earnings] No past earnings data found.")
            
            # Log Snapshots
            logger.info(f"[Data Snapshot] Price: {price} | MCap: {format_market_cap(m_cap)} | Beta: {beta} | Sector: {sector}")
            logger.info(f"[Metric Snapshot] EV/EBITDA: {format_num(ev_ebitda)} | PS: {format_num(ps_ratio)} | ROIC: {format_percent(roic)} | Margin: {format_percent(net_margin)}")

            # === 2. 宏观修正 & 风险 ===
            yield_10y = self.extract(t, 'year10', "10Y Yield", required=False)
            macro_discount_factor = 1.0 
            if yield_10y and yield_10y > 4.8:
                macro_discount_factor = 0.7
                self.signals.add("MACRO_HEADWIND")
                self.logs.append(f"[宏观压制] 美债收益率 {yield_10y}%，估值模型承压。")
            elif yield_10y and yield_10y < 3.8:
                macro_discount_factor = 1.5
                self.signals.add("MACRO_TAILWIND")
                self.logs.append(f"[宏观红利] 美债收益率 {yield_10y}%，有利于估值扩张。")

            # VaR Calculation (Only Percent)
            if beta and price and isinstance(vix_data, dict):
                vix_val = vix_data.get("price")
                if vix_val:
                    estimated_vol_annual = beta * (vix_val / 100.0)
                    monthly_vol = estimated_vol_annual * math.sqrt(1/12)
                    var_95_pct = 1.65 * monthly_vol
                    self.risk_var = f"-{format_percent(var_95_pct)}"
                    if var_95_pct > 0.25: self.signals.add("RISK_EXTREME_VAR")

            # === 3. 维度收集与详尽因子分析 ===
            
            # (A) 赛道与属性
            is_blue_ocean = False        
            is_hard_tech = False 
            sec_str = str(sector).lower(); ind_str = str(industry).lower()
            
            for kw in BLUE_OCEAN_KEYWORDS:
                if kw in sec_str or kw in ind_str: is_blue_ocean = True; break
            for kw in HARD_TECH_KEYWORDS:
                if kw in sec_str or kw in ind_str: is_hard_tech = True; break
            if self.ticker in HARD_TECH_TICKERS:
                if not is_blue_ocean: is_hard_tech = True
            
            if is_blue_ocean: self.signals.add("BLUE_OCEAN")
            if is_hard_tech: self.signals.add("HARD_TECH")
            is_giant = m_cap is not None and m_cap > 200_000_000_000
            if is_giant: self.signals.add("GIANT_CAP")

            # (B) Meme / 信仰值分析
            meme_score = 0
            vol_today = self.extract(q, "volume", "Volume", required=False)
            vol_avg = self.extract(q, "avgVolume", "Avg Volume", required=False)
            
            if price and price_200ma:
                if price > price_200ma * 1.4: meme_score += 2
                elif price > price_200ma * 1.15: meme_score += 1
            if (ps_ratio and ps_ratio > 20) or (ev_ebitda and ev_ebitda > 80): meme_score += 4
            elif (ps_ratio and ps_ratio > 10) or (ev_ebitda and ev_ebitda > 40): meme_score += 2
            elif (ps_ratio and ps_ratio > 8) or (ev_ebitda and ev_ebitda > 30): meme_score += 1
            if beta > 2.0: meme_score += 2
            elif beta > 1.3: meme_score += 1
            if price and price_200ma and price > price_200ma:
                bad_fcf = (fcf_yield_api is not None and fcf_yield_api < 0.01)
                bad_peg = (peg_ttm is not None and (peg_ttm < 0 or peg_ttm > 4.0))
                if bad_fcf or bad_peg: meme_score += 2
            if vol_today and vol_avg and vol_avg > 0:
                if vol_today > vol_avg * 1.2: meme_score += 1
            if roic and roic > 0.20 and (peg_ttm and 0 < peg_ttm < 3.0):
                 meme_score -= 3 
            
            meme_score = max(0, min(10, meme_score))
            meme_pct = int(meme_score * 10)
            
            if meme_pct >= 80: self.signals.add("MEME_EXTREME")
            
            is_faith_mode = meme_pct >= 50
            if is_faith_mode:
                meme_log = ""
                if 50 <= meme_pct < 60:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。市场关注度提升，资金动量正在影响短期价格走势。"
                elif 60 <= meme_pct < 70:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪高度活跃，体现出显著的**资金共识**和高流动性。"
                elif 70 <= meme_pct < 80:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。资金聚焦度极高，公司获得大量**关注溢价**，价格驱动力强劲。"
                elif 80 <= meme_pct < 90:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪已进入非理性繁荣区间，价格体现出**极致的资金动能**。"
                elif meme_pct >= 90:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪处于顶峰，反映出**极强的短期向上动量**。"
                
                if meme_log: self.logs.insert(0, meme_log)

            # (C) 盈利质量
            if net_margin and net_margin > 0.20:
                self.logs.append(f"[盈利质量] 净利率 ({format_percent(net_margin)}) 极高，展现出强大的产品定价权或成本控制力。")
            if net_margin and net_margin < -0.10: self.signals.add("DEEP_LOSS")

            # (D) 成长性 (PEG & Growth)
            forward_peg = None
            fwd_growth = None
            if estimates and len(estimates) > 0 and price:
                try:
                    estimates.sort(key=lambda x: x.get("date", "0000-00-00"))
                    future_estimates = [e for e in estimates if e.get("date", "") > today_str]
                    if len(future_estimates) >= 2:
                        fy1 = future_estimates[0]; fy2 = future_estimates[1] 
                        eps_fy1 = fy1.get("epsAvg"); eps_fy2 = fy2.get("epsAvg")
                        if eps_fy1 and eps_fy1 > 0 and eps_fy2:
                            fwd_pe = price / eps_fy1
                            fwd_growth = (eps_fy2 - eps_fy1) / eps_fy1
                            if fwd_growth > 0: forward_peg = fwd_pe / (fwd_growth * 100)
                except Exception: pass
            
            peg_used = forward_peg if forward_peg is not None else peg_ttm
            is_forward_peg_used = (forward_peg is not None)
            
            # Log PEG Decision
            logger.info(f"[PEG Decision] Forward: {format_num(forward_peg)} | TTM: {format_num(peg_ttm)} | Used: {format_num(peg_used)}")

            growth_list = [x for x in [rev_growth, ni_growth, fwd_growth] if x is not None]
            max_growth = max(growth_list) if growth_list else 0
            
            growth_desc = "低成长"
            if max_growth > 0.5: growth_desc = "超高速"; self.signals.add("GROWTH_HYPER")
            elif max_growth > 0.2: growth_desc = "高速"; self.signals.add("GROWTH_HIGH")
            elif max_growth > 0.05: growth_desc = "稳健"; self.signals.add("GROWTH_STABLE")
            else: self.signals.add("GROWTH_LOW")

            if peg_used is not None:
                peg_display = format_num(peg_used)
                peg_type_str = "Forward" if is_forward_peg_used else "TTM"
                peg_status = "N/A"
                peg_comment = ""
                
                if peg_used < 0.8: self.signals.add("PEG_UNDERVALUED")
                elif peg_used < 1.5: self.signals.add("PEG_CHEAP")
                elif peg_used > 3.0: self.signals.add("PEG_EXPENSIVE")

                if is_blue_ocean: 
                    if peg_used < 0.5: peg_status = "极低/数据失真"; peg_comment = "基数过小可能导致失真，参考意义有限。"
                    elif peg_used < 1.5: peg_status = "低估"; peg_comment = f"相对于未来的爆发潜力，当前价格处于低位 ({peg_type_str})。"
                    elif peg_used <= 4.0: peg_status = "合理 (高容忍)"; peg_comment = f"市场给予蓝海赛道极高的增长容忍度 ({peg_type_str})。"
                    else: peg_status = "高估/透支"; peg_comment = "预期已大幅透支，需警惕回调。"
                elif is_hard_tech: 
                    if peg_used < 1.0: peg_status = "极度低估/罕见"; peg_comment = f"对于硬科技资产，此 {peg_type_str} PEG 属于罕见的低估区间。"
                    elif peg_used <= 2.0: peg_status = "合理 (GARP)"; peg_comment = f"属于合理的成长股估值区间 ({peg_type_str})。"
                    elif peg_used <= 3.0: peg_status = "溢价"; peg_comment = "包含了一定的情绪溢价，但在牛市中可接受。"
                    else: peg_status = "泡沫化风险"; peg_comment = "估值已脱离基本面引力，风险较高。"
                else: 
                    if peg_used < 0.8: peg_status = "低估"; peg_comment = "具备极高的安全边际。"
                    elif peg_used <= 1.5: peg_status = "合理"; peg_comment = "估值与增长匹配。"
                    elif peg_used > 3.0: peg_status = "泡沫化风险"; peg_comment = "估值已脱离基本面引力，风险较高。"
                
                if peg_status != "N/A":
                    self.logs.append(f"[成长锚点] PEG ({peg_type_str}): {peg_display} ({peg_status})。{peg_comment}")

            # (E) 估值水平 (Valuation - Modified for Scientific Logic)
            sector_avg = get_sector_benchmark(sector)
            
            # --- P/S 逻辑 (亏损强制显示，盈利条件显示) ---
            if ps_ratio is not None:
                # 判别逻辑：如果是亏损股，或者P/S极高（泡沫预警），或者属于BlueOcean（行业惯例），则显示P/S
                should_show_ps = (not is_profitable_strict) or (ps_ratio > 10.0) or is_blue_ocean
                
                if should_show_ps:
                    th_low, th_fair, th_high = 1.5, 3.0, 8.0
                    if is_blue_ocean: th_low, th_fair, th_high = 2.0, 5.0, 15.0
                    th_low *= macro_discount_factor; th_fair *= macro_discount_factor; th_high *= macro_discount_factor
                    
                    ps_desc = ""
                    if ps_ratio < th_low: 
                        self.signals.add("PS_LOW")
                        ps_desc = "处于历史低位，相对于营收规模被低估"
                    elif ps_ratio < th_fair: 
                        ps_desc = "处于合理区间"
                    elif ps_ratio < th_high: 
                        ps_desc = "较高，市场给予了较高的增长溢价"
                    else: 
                        self.signals.add("PS_EXTREME")
                        ps_desc = "极高，价格已透支未来多年的增长"
                    
                    tag = "[蓝海赛道]" if is_blue_ocean else "[估值警示]"
                    if not is_profitable_strict: tag = "[核心估值]" # 亏损股的核心指标
                    
                    self.logs.append(f"{tag} P/S 估值：{format_num(ps_ratio)} ({ps_desc})。")

            # --- EV/EBITDA 逻辑 (盈利强制显示，亏损隐藏) ---
            if is_profitable_strict and ev_ebitda is not None:
                ratio = ev_ebitda / sector_avg
                adj_ratio = ratio / macro_discount_factor if macro_discount_factor != 0 else ratio
                
                if adj_ratio < 0.7: 
                    self.signals.add("VALUATION_CHEAP")
                    self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 低于行业均值 ({sector_avg})，折扣明显。")
                elif adj_ratio > 1.3: 
                    self.signals.add("VALUATION_EXPENSIVE")
                    if ("高速" in growth_desc or "超高速" in growth_desc) and (peg_used is not None and peg_used < 2.0):
                         self.logs.append(f"[成长特权] 虽 EV/EBITDA ({format_num(ev_ebitda)}) 偏高，但 PEG 较低，属于越涨越便宜。")
                    else:
                         self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 远高于行业均值 ({sector_avg})，且缺乏增长支撑。")
                else: 
                    self.signals.add("VALUATION_FAIR")
                    self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 与行业均值 ({sector_avg}) 接近，估值处于合理区间。")

            # (F) 质量与效率 (Quality & FCF)
            adj_fcf_yield = None
            if len(cf_list) >= 4 and m_cap and m_cap > 0:
                ttm_cfo = sum(self.extract(q, "netCashProvidedByOperatingActivities", "", default=0, required=False) for q in cf_list)
                ttm_da = sum(self.extract(q, "depreciationAndAmortization", "", default=0, required=False) for q in cf_list)
                if ttm_cfo != 0:
                    adj_fcf = ttm_cfo - (ttm_da * 0.5) 
                    adj_fcf_yield = adj_fcf / m_cap
                    self.fcf_yield_display = format_percent(adj_fcf_yield)

            fcf_used = adj_fcf_yield if adj_fcf_yield is not None else fcf_yield_api
            
            # Log Cash Flow
            logger.info(f"[Cash Flow] TTM FCF Yield: {format_percent(fcf_yield_api)} | Adj FCF Yield: {format_percent(adj_fcf_yield)}")

            # ROIC Logs
            if roic and roic > 0.20: 
                self.signals.add("QUALITY_TOP_TIER") 
                self.logs.append(f"[护城河] ROIC ({format_percent(roic)}) 极高，资本效率顶级。")
            elif roic and roic > 0.10: 
                self.signals.add("QUALITY_GOOD")
            elif roic and roic < 0:
                self.signals.add("QUALITY_BAD")
            else:
                 self.signals.add("QUALITY_AVG")

            # FCF Analysis Logs
            if fcf_used is not None:
                if fcf_used > 0.035: self.signals.add("CASHFLOW_RICH") 
                elif fcf_used > 0.015: self.signals.add("CASHFLOW_HEALTHY")
                elif fcf_used < -0.01: self.signals.add("CASHFLOW_NEGATIVE")
                
                if adj_fcf_yield is not None and fcf_yield_api is not None:
                     if adj_fcf_yield > (fcf_yield_api + 0.0005):
                        if roic and roic > 0.15:
                            self.signals.add("QUALITY_EXPANSION")
                            self.logs.append(f"[价值修正] Adj FCF Yield ({self.fcf_yield_display}) 高于 原始 FCF ({format_percent(fcf_yield_api)})。结合极高的 **ROIC ({format_percent(roic)})**，说明巨额资本开支正高效转化为增长，高强度的扩张投入掩盖了其真实的现金流产生能力。")
                        else:
                            self.logs.append(f"[价值修正] Adj FCF Yield ({self.fcf_yield_display}) 高于 原始 FCF ({format_percent(fcf_yield_api)})，反映出增长性资本支出的积极影响。")

            # (G) 业绩 Alpha
            valid_earnings = []
            if isinstance(earnings_raw, list):
                sorted_earnings = sorted(earnings_raw, key=lambda x: x.get("date", "0000-00-00"), reverse=True)
                recent_earnings = sorted_earnings[:12]
                for e in recent_earnings:
                    date = e.get("date")
                    if date and date <= today_str:
                        rev = self.extract(e, "revenueActual", "Revenue", default=e.get("revenue"))
                        eps = self.extract(e, "epsActual", "EPS")
                        est = self.extract(e, "epsEstimated", "EPS Est")
                        if rev is not None and eps is not None:
                            valid_earnings.append({"date": date, "rev": rev, "eps": eps, "est": est})
            
            trend_data = sorted(valid_earnings, key=lambda x: x["date"])
            recent_4 = trend_data[-4:] 
            if len(recent_4) > 0:
                beats = sum(1 for x in recent_4 if x["est"] is not None and x["eps"] > x["est"])
                total = len(recent_4)
                if total > 0:
                    beat_rate = beats / total
                    if beat_rate >= 0.75:
                        self.logs.append(f"[Alpha] 过去 {total} 季度中有 {beats} 次业绩超预期，机构情绪乐观。")
                    else:
                        self.logs.append(f"[Alpha] 过去 {total} 季度中有 {total - beats} 次业绩不及预期，需警惕。")

            # Other Risks
            if price and price_200ma and price < price_200ma: self.signals.add("DOWNTREND")
            if pe_ttm and pe_ttm < 8 and rev_growth and rev_growth < -0.05: 
                self.signals.add("VALUE_TRAP_RISK")
                self.logs.append(f"[陷阱] PE ({format_num(pe_ttm)}) 虽低，但营收负增长，疑似周期顶部信号。")
            if beta and beta < 0.6: self.signals.add("LOW_VOLATILITY")

            # === 4. 综合策略解算 (穷举模式) ===
            self.determine_strategy_exhaustive()
            
            self.short_term_verdict = self.get_short_term_verdict(ev_ebitda, sector_avg)
            self.long_term_verdict = self.get_long_term_verdict()

            return {
                "price": price,
                "beta": beta,
                "m_cap": m_cap,
                "growth_desc": growth_desc,
                "risk_var": self.risk_var,
                "meme_pct": meme_pct,
                "is_profitable": is_profitable_strict
            }
        except Exception as e:
            logger.error(f"Analyze Error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    # --- 穷举策略核心函数 ---
    def determine_strategy_exhaustive(self):
        s = self.signals
        
        # 预计算布尔值，方便组合逻辑书写
        is_cheap = "VALUATION_CHEAP" in s
        is_expensive = "VALUATION_EXPENSIVE" in s
        is_fair = "VALUATION_FAIR" in s
        
        is_quality_top = "QUALITY_TOP_TIER" in s
        is_quality_bad = "QUALITY_BAD" in s
        is_quality_avg = "QUALITY_AVG" in s or "QUALITY_GOOD" in s
        
        is_cash_rich = "CASHFLOW_RICH" in s or "CASHFLOW_HEALTHY" in s
        is_cash_bad = "CASHFLOW_NEGATIVE" in s
        
        is_growth_high = "GROWTH_HIGH" in s or "GROWTH_HYPER" in s
        is_growth_low = "GROWTH_LOW" in s
        is_peg_cheap = "PEG_CHEAP" in s or "PEG_UNDERVALUED" in s
        
        is_risk = "DEEP_LOSS" in s or "DOWNTREND" in s or "VALUE_TRAP_RISK" in s

        # --- 策略库 (Total: 31种) ---
        
        # 1. 风险组
        if "DEEP_LOSS" in s and "DOWNTREND" in s:
            self.strategy = "【接飞刀风险】深度亏损且股价处于下降趋势，基本面与技术面双杀，风险极大，建议回避。"
            return
        if is_growth_high and is_cash_bad and not is_quality_top:
            self.strategy = "【烧钱陷阱】增长主要依赖高额烧钱（负现金流），且资本效率(ROIC)一般。流动性收紧时面临融资风险。"
            return
        if is_growth_low and is_quality_bad:
            self.strategy = "【僵尸股风险】缺乏增长引擎，且资本效率低下。机会成本极高，不建议持有。"
            return
        if "VALUE_TRAP_RISK" in s:
            self.strategy = "【低估值陷阱】市盈率极低，但营收处于萎缩周期。这通常是周期顶部的信号，谨防业绩暴雷。"
            return

        # 2. 特殊组
        if "MEME_EXTREME" in s:
            self.strategy = "【Meme动量】市场情绪处于极度狂热状态。基本面指标已失效，交易需完全基于资金面和动量指标，快进快出。"
            return
        if "BLUE_OCEAN" in s:
            self.strategy = "【蓝海卡位】处于前沿科技赛道，估值锚点在于远期的行业垄断地位。适合在技术回调时分批布局，博弈长线爆发。"
            return
        if "HARD_TECH" in s and "PS_LOW" in s:
            self.strategy = "【硬科技期权】硬科技属性明确，且P/S处于低位。市场暂时忽略了其技术壁垒，具备看涨期权属性。"
            return

        # 3. 黄金/核心组 (Priority High)
        if is_cheap and is_quality_top and is_cash_rich:
            self.strategy = "【黄金配置窗口】不可能三角达成！顶级资本效率(ROIC) + 充沛现金流 + 估值折价。极为罕见的错杀机会，强烈建议买入。"
            return
        
        if is_quality_top and is_peg_cheap:
            self.strategy = "【成长型核心资产】既具备低PEG的强劲进攻性，又有顶级ROIC构建的深厚护城河。完美结合了成长与质量，值得重仓。"
            return
            
        if is_quality_top and is_cheap and not is_cash_rich:
            self.strategy = "【优质扩张】 资本效率极高且估值合理。现金流低是因为正在进行高回报的再投入，长期复利效应显著。"
            return
            
        if is_quality_top and is_cash_rich and (is_fair or is_cheap):
            self.strategy = "【现金牛核心】 行业统治力强，造血能力极强。当前价格公道，适合作为组合的压舱石长期持有。"
            return
            
        if is_quality_top and is_expensive and is_growth_high:
            self.strategy = "【溢价核心】公司极其优秀，市场已给予很高的确定性溢价。适合通过长期持有，用业绩增长来消化估值。"
            return

        # === 补丁 ===
        if is_quality_top and is_fair and is_growth_low:
             self.strategy = "【成熟稳健】顶级护城河，但进入成熟期增长放缓。估值合理，适合作为防御性底仓，赚取稳健的业绩回报。"
             return

        if is_quality_top and is_expensive and not is_growth_high:
            self.strategy = "【高估值债】虽然质量极好，但增长放缓，当前高估值透支了未来收益，类似一张昂贵的债券，性价比不高。"
            return

        # 4. 成长组
        if is_peg_cheap and not is_quality_top:
            self.strategy = "【高性价比成长】PEG极低，显示增长潜力被低估。虽然护城河不如核心资产深，但赔率很高，适合做进攻配置。"
            return
        
        if is_expensive and is_growth_high:
            self.strategy = "【动量成长】高估值完全由高增长支撑。只要业绩不减速，股价仍有惯性上冲可能，需设置止损紧密跟踪。"
            return
            
        if is_growth_high and "PS_EXTREME" in s:
            self.strategy = "【高风偏博弈】增长极快但市销率极高，价格完美定价了未来多年的预期。任何微小的业绩瑕疵都可能引发剧烈回调。"
            return

        # 5. 价值组
        if is_cheap and is_cash_rich:
            self.strategy = "【深度价值/烟蒂】估值极低且账上现金充沛，下行空间被现金价值封杀。属于经典的防御性价值投资。"
            return
        
        # === 补丁 ===
        if is_cheap and is_cash_bad and is_growth_high:
             self.strategy = "【投机性反弹】现金流虽差，但估值极低且预期增长极高。若非破产风险，存在巨大的估值修复空间，博赔率。"
             return

        if is_cheap and is_cash_bad:
            self.strategy = "【困境反转】价格极低，反映了市场对其现金流问题的担忧。若能改善经营，反弹空间巨大，属于高风险高回报。"
            return
            
        if is_cheap and not is_quality_bad:
            self.strategy = "【超跌反弹】估值显著低于行业平均，存在修复需求。建议关注是否有基本面改善的催化剂。"
            return

        # 6. 泡沫/高估组
        if is_expensive and not is_growth_high and not is_quality_top:
            self.strategy = "【泡沫风险】估值昂贵，且缺乏高增长或高效率支撑。价格严重脱离基本面，建议回避或减仓。"
            return
            
        if is_expensive and is_growth_high and is_quality_bad:
            self.strategy = "【概念炒作】虽然有增长，但质量极差且估值过高。典型的概念炒作特征，潮水退去后风险很大。"
            return

        # 7. 中庸/防御组
        if is_fair and is_growth_high:
            self.strategy = "【GARP策略】以合理价格买入高成长。性价比较高，是比较稳健的成长股投资策略。"
            return
            
        if is_fair and is_quality_top:
            self.strategy = "【守正出奇】估值合理，质量顶尖。虽无暴利机会，但胜在稳健，适合稳健型投资者持有。"
            return
        
        if "LOW_VOLATILITY" in s and is_cash_rich:
            self.strategy = "【防御收息】低波动且现金流好，具备类债券属性。适合在市场动荡时作为避险配置。"
            return
            
        if "GIANT_CAP" in s and is_fair:
            self.strategy = "【巨头躺平】超大市值巨头，估值合理。预期收益率即为市场平均回报，适合被动配置。"
            return

        # === 补丁 ===
        if is_fair and is_quality_avg and not is_growth_high:
            self.strategy = "【中性/观望】基本面平庸，估值合理。缺乏明显的做多或做空理由，建议观望，等待业绩催化或估值变动。"
            return

        # 8. 兜底 (真空区)
        self.strategy = "数据不足 (未命中任何策略模型)"

    def get_short_term_verdict(self, ev_ebitda, sector_avg):
        s = self.signals
        if "PEG_UNDERVALUED" in s: return "便宜 (高成长)"
        if "VALUATION_CHEAP" in s: return "低估"
        if "VALUATION_EXPENSIVE" in s: return "昂贵" if "GROWTH_HIGH" not in s else "合理溢价"
        return "估值合理"

    def get_long_term_verdict(self):
        s = self.signals
        if "QUALITY_TOP_TIER" in s: return "优质/核心资产"
        if "BLUE_OCEAN" in s: return "战略卡位"
        if "DEEP_LOSS" in s: return "高风险"
        if "CASHFLOW_RICH" in s: return "稳健"
        return "中性"

class AnalysisBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        logger.info("Syncing commands...")
        await self.tree.sync() 
        self.session = aiohttp.ClientSession()
        logger.info("Commands synced & Session created.")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

bot = AnalysisBot()

@bot.tree.command(name="privacy", description="切换隐私查询模式 (开启后分析结果仅自己可见)")
async def privacy(interaction: discord.Interaction):
    user_id = interaction.user.id
    is_on = PRIVACY_MODE.get(user_id, False)
    new_state = not is_on
    PRIVACY_MODE[user_id] = new_state
    status = "已开启 (查询结果仅自己可见)" if new_state else "已关闭 (查询结果公开)"
    await interaction.response.send_message(f"[Info] 隐私模式切换成功。\n当前状态: **{status}**", ephemeral=True)

async def process_analysis(interaction: discord.Interaction, ticker: str, force_private: bool = False):
    is_privacy_mode = force_private or PRIVACY_MODE.get(interaction.user.id, False)
    ephemeral_result = is_privacy_mode
    
    await interaction.response.defer(thinking=True, ephemeral=ephemeral_result) 

    model = ValuationModel(ticker)
    success = await model.fetch_data(interaction.client.session)
    
    if is_privacy_mode and success:
        public_embed = discord.Embed(
            description=f"**{interaction.user.display_name}** 开启《稳-量化估值系统》\n“{ticker.upper()}”分析报告已发送给用户✅",
            color=0x2b2d31
        )
        try:
            await interaction.channel.send(embed=public_embed) 
        except Exception as e:
            logger.error(f"Failed to send public status message: {e}")
    
    if not success:
        await interaction.followup.send(f"[Error] 获取数据失败: `{ticker.upper()}`", ephemeral=ephemeral_result)
        return

    data = model.analyze()
    if not data:
        await interaction.followup.send(f"[Warning] 数据不足。", ephemeral=ephemeral_result)
        return

    profit_label = "盈利" if data.get('is_profitable', False) else "亏损"

    embed = discord.Embed(
        title=f"估值分析: {ticker.upper()}",
        description=f"现价: ${data['price']:.2f} | 市值: {format_market_cap(data['m_cap'])} | {profit_label}",
        color=0x2b2d31
    )

    verdict_text = (
        f"> **短期:** {model.short_term_verdict}\n"
        f"> **长期:** {model.long_term_verdict}"
    )
    embed.add_field(name="估值结论", value=verdict_text, inline=False)

    beta_val = data['beta']
    beta_desc = "低波动" if beta_val < 0.8 else ("高波动" if beta_val > 1.3 else "适中")
    
    meme_pct = data['meme_pct']
    meme_desc = "低关注度"
    if meme_pct >= 80: meme_desc = "资金狂热"
    elif meme_pct >= 60: meme_desc = "高流动性"
    elif meme_pct >= 30: meme_desc = "市场关注"
    
    core_factors = (
        f"> **Beta:** `{format_num(beta_val)}` ({beta_desc})\n"
        f"> **Meme值:** `{meme_pct}%` ({meme_desc})"
    )
    embed.add_field(name="核心特征", value=core_factors, inline=False)
    
    if data['risk_var'] != "N/A":
        embed.add_field(
            name="95% VaR (月度风险)", 
            value=f"> 最大回撤可能在 **{data['risk_var']}** 附近", 
            inline=False
        )

    log_content = []
    if model.flags: log_content.extend(model.flags) 
    log_content.extend([f"{log}" for log in model.logs])
    
    formatted_logs = []
    for log in log_content:
        if log.startswith("[") and "]" in log:
            tag_end = log.find("]") + 1
            tag = log[:tag_end]
            content = log[tag_end:]
            formatted_logs.append(f"> **{tag}**{content}")
        else:
            formatted_logs.append(f"> {log}")

    factor_str = "\n> \n".join(formatted_logs)
    strategy_text = f"**[策略]** {model.strategy}"
    full_log_str = f"{factor_str}\n\n{strategy_text}"
    
    if len(full_log_str) > 1000: full_log_str = full_log_str[:990] + "..."

    embed.add_field(name="因子分析", value=full_log_str, inline=False)
    embed.set_footer(text="(模型建议，仅作参考，不构成投资建议)")

    await interaction.followup.send(embed=embed, ephemeral=ephemeral_result)

@bot.tree.command(name="analyze", description="估值分析 (结果可见性由/privacy决定)")
@app_commands.describe(ticker="股票代码 (如 NVDA)")
async def analyze_command(interaction: discord.Interaction, ticker: str):
    await process_analysis(interaction, ticker, force_private=False)

@bot.tree.command(name="private_analyze", description="私密估值分析 (结果仅自己可见，但会在频道内发布状态)")
@app_commands.describe(ticker="股票代码 (如 NVDA)")
async def private_analyze_command(interaction: discord.Interaction, ticker: str):
    await process_analysis(interaction, ticker, force_private=True)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable not set.")
    else:
        if not FMP_API_KEY:
             logger.error("FMP_API_KEY environment variable not set.")
        try:
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            logger.error(f"Bot failed to run: {e}")
