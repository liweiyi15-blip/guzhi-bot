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
import json

# 加载环境变量
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
FMP_API_KEY = os.getenv('FMP_API_KEY')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY') # 新增 DeepSeek Key

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
        
        # 信号篮子
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
            
            # 【核心】盈利状态判定
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

            # VaR Calculation
            if beta and price and isinstance(vix_data, dict):
                vix_val = vix_data.get("price")
                if vix_val:
                    estimated_vol_annual = beta * (vix_val / 100.0)
                    monthly_vol = estimated_vol_annual * math.sqrt(1/12)
                    var_95_pct = 1.65 * monthly_vol
                    
                    if beta > 1.5 or not is_profitable_strict:
                        var_95_pct *= 1.2
                    
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
                    elif peg_used > 4.0: peg_status = "高估/透支"; peg_comment = "预期已大幅透支，需警惕回调。"
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

            # (E) 估值水平 (Valuation)
            sector_avg = get_sector_benchmark(sector)
            
            # --- P/S 逻辑 ---
            should_show_ps = (not is_profitable_strict) or (ev_ebitda is None)
            if ps_ratio is not None and should_show_ps:
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
                
                tag = "[蓝海赛道]" if is_blue_ocean else "[核心估值]"
                self.logs.append(f"{tag} P/S 估值：{format_num(ps_ratio)} ({ps_desc})。")
            
            if ps_ratio is not None and not should_show_ps:
                 if ps_ratio > 20.0: self.signals.add("PS_EXTREME")
                 if ps_ratio < 2.0: self.signals.add("PS_LOW")

            # --- EV/EBITDA 逻辑 ---
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
                             self.signals.add("CASHFLOW_HIGH_QUALITY")

        except Exception as e:
            logger.error(f"Analysis Error: {e}")
            self.logs.append("数据分析过程中出现异常。")

    # --- DeepSeek Integration ---
    async def ask_deepseek(self, session: aiohttp.ClientSession):
        """
        将 self.data 中的全量原始数据发送给 DeepSeek 进行评估。
        """
        if not DEEPSEEK_API_KEY:
            self.strategy = "错误: 未配置 DeepSeek API Key。"
            return

        try:
            # 1. 准备全量数据
            # 使用 json.dumps 确保将所有字典/列表转为字符串，default=str 处理 datetime 等非标准对象
            raw_data_json = json.dumps(self.data, default=str, indent=2)

            # 2. 构建 Prompt
            system_prompt = (
                "你是十年经验炒股大神，华尔街机构从业者。你需要用专业科学，辩证的角度评估这些数据，给出合理的评估。"
            )
            user_prompt = (
                f"这是 {self.ticker} 的全量原始财务与市场数据：\n\n{raw_data_json}\n\n"
                "请根据以上数据进行评估。要求：\n"
                "1. 不要具体的操作建议（不准说买入或卖出）。\n"
                "2. 限制在 60 个汉字以内。\n"
                "3. 语言风格：通俗易懂且专业，犀利。\n"
                "4. 必须基于数据说话。"
            )

            # 3. 调用 DeepSeek API
            url = "https://api.deepseek.com/chat/completions"
            headers = {
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "deepseek-chat", # 或者使用 deepseek-reasoner
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 1.0, # 稍微增加创造性
                "max_tokens": 150   # 限制输出 Token，防止废话
            }

            async with session.post(url, headers=headers, json=payload, timeout=30) as response:
                if response.status == 200:
                    result = await response.json()
                    content = result['choices'][0]['message']['content']
                    self.strategy = f"🤖 **大神点评**：{content}"
                else:
                    error_text = await response.text()
                    logger.error(f"DeepSeek API Error: {response.status} - {error_text}")
                    self.strategy = f"DeepSeek 思考超时 (Status: {response.status})"

        except Exception as e:
            logger.error(f"DeepSeek Call Error: {e}")
            self.strategy = "DeepSeek 大脑掉线了..."

# --- Discord Bot Setup ---

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Commands synced.")

bot = MyBot()

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')

@bot.tree.command(name="analyze", description="使用 DeepSeek 大神分析股票")
@app_commands.describe(ticker="股票代码 (例如 AAPL)")
async def analyze(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer() # 这是一个耗时操作，先 defer

    async with aiohttp.ClientSession() as session:
        model = ValuationModel(ticker)
        
        # 1. 获取 FMP 数据
        success = await model.fetch_data(session)
        if not success:
            await interaction.followup.send(f"❌ 找不到代码 **{ticker.upper()}** 的数据，请检查拼写。", ephemeral=True)
            return

        # 2. 执行常规分析
        model.analyze()

        # 3. 召唤 DeepSeek 大神 (传入 Session)
        await model.ask_deepseek(session)

        # 4. 构建 Embed 结果
        p = model.data.get("profile", {})
        price = p.get("price")
        
        embed = discord.Embed(
            title=f"📊 {model.ticker} 深度分析报告",
            description=f"**{p.get('companyName')}** | 现价: ${price}",
            color=0x00ff00 if (price and model.risk_var != "N/A" and "EXTREME" not in str(model.signals)) else 0xff9900
        )
        
        if model.logs:
            # 取前5条重要日志，避免刷屏
            log_str = "\n".join([f"• {log}" for log in model.logs[:5]])
            embed.add_field(name="核心因子", value=log_str, inline=False)
        
        embed.add_field(name="95% VaR (月度风险)", value=model.risk_var, inline=True)
        embed.add_field(name="FCF Yield (Adj)", value=model.fcf_yield_display, inline=True)
        
        # DeepSeek 的结果
        embed.add_field(name="🧠 华尔街大神评估", value=model.strategy, inline=False)
        
        embed.set_footer(text="Data provided by FMP • Analysis by DeepSeek V3")
        
        await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logger.error("Please set DISCORD_TOKEN in .env file")
