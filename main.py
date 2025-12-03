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
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')

# *** 核心：完全还原原代码的全局唯一接口地址 (Stable) ***
BASE_URL = "https://financialmodelingprep.com/stable"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"

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

# --- 1. 异步数据工具函数 (完全还原原版) ---

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

# 【还原】原版通用函数
async def get_fmp_data(session: aiohttp.ClientSession, endpoint: str, ticker: str, params: str = ""):
    url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey={FMP_API_KEY}"
    if params: url += f"&{params}"
    return await get_json_safely(session, url)

# 【还原】独立函数，limit=10
async def get_estimates_data(session: aiohttp.ClientSession, ticker: str):
    url = f"{BASE_URL}/analyst-estimates?symbol={ticker}&period=annual&limit=10&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url)
    return data if data else []

# 【还原】独立函数
async def get_earnings_data(session: aiohttp.ClientSession, ticker: str):
    url = f"{BASE_URL}/earnings?symbol={ticker}&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url)
    return data if data else []

# --- DeepSeek 分析引擎 ---
async def ask_deepseek_strategy(session: aiohttp.ClientSession, ticker: str, context_str: str):
    if not DEEPSEEK_API_KEY: return "未配置 DeepSeek Key，无法生成策略。"
    
    system_prompt = (
        "你是一位拥有十年华尔街实战经验的机构分析师。请基于提供的数据，对该标的进行深度的定性策略分析。\n"
        "【严格执行以下要求】：\n"
        "1. **严禁出现数字**：用“估值极高”、“资金分歧”等描述代替具体数据。\n"
        "2. **通俗且专业**：用大白话讲透上涨/下跌背后的逻辑（是基本面驱动还是情绪博弈？）。\n"
        "3. **字数限制**：严格控制在 80 字以内！不要超过！\n"
        "4. **不要给出操作建议**：严禁出现“建议买入”、“止损”、“低吸”等具体交易指令。只陈述事实和逻辑判断。"
    )
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"标的：{ticker}\n\n{context_str}"}
        ],
        "temperature": 0.6,
        "max_tokens": 100
    }
    
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    try:
        async with session.post(DEEPSEEK_BASE_URL, headers=headers, json=payload, timeout=12) as response:
            if response.status == 200:
                res = await response.json()
                return res['choices'][0]['message']['content'].strip()
            return "AI 服务暂时不可用。"
    except: return "AI 请求超时。"

# --- 格式化工具 ---
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

# --- 2. 核心：ValuationModel ---
class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        self.logs = []
        self.signals = set()
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        self.risk_var = "N/A"  
        self.context_for_ai = "" 
        # 【修复点：初始化缺失变量】
        self.fcf_yield_display = "N/A"
        self.fcf_yield_api = None

    def extract(self, source, key, desc, default=None, required=True):
        val = source.get(key)
        if val is None:
            if default is not None:
                return default
            elif not required:
                return None
            else:
                return None
        else:
            return val

    async def fetch_data(self, session: aiohttp.ClientSession):
        logger.info(f"--- Analysis Start: {self.ticker} ---")
        task_profile = get_company_profile_smart(session, self.ticker)
        task_treasury = get_treasury_rates(session)
        
        # 【还原】URL 参数完全对照第一版代码
        tasks_generic = {
            "quote": get_fmp_data(session, "quote", self.ticker, ""),
            "metrics": get_fmp_data(session, "key-metrics-ttm", self.ticker, ""),
            "ratios": get_fmp_data(session, "ratios-ttm", self.ticker, ""),
            "growth": get_fmp_data(session, "financial-growth", self.ticker, "period=annual&limit=1"),
            "bs": get_fmp_data(session, "balance-sheet-statement", self.ticker, "limit=1"),
            "cf": get_fmp_data(session, "cash-flow-statement", self.ticker, "period=quarter&limit=4"), 
            "vix": get_fmp_data(session, "quote", "^VIX", ""),
            "earnings": get_earnings_data(session, self.ticker), # 使用独立函数
            "estimates": get_estimates_data(session, self.ticker) # 使用独立函数(limit=10)
        }
        profile_data, treasury_data, *generic_results = await asyncio.gather(task_profile, task_treasury, *tasks_generic.values())
        self.data = dict(zip(tasks_generic.keys(), generic_results))
        self.data["profile"] = profile_data 
        self.data["treasury"] = treasury_data 
        
        # 【还原】API 统计逻辑
        success_count = 0
        for k in tasks_generic.keys():
            raw = self.data[k]
            list_keys = ["earnings", "estimates", "cf"]
            if k in list_keys:
                if isinstance(raw, list) and len(raw) > 0:
                    self.data[k] = raw
                    success_count += 1
                else:
                    self.data[k] = []
            else:
                if isinstance(raw, list) and len(raw) > 0:
                    self.data[k] = raw[0]
                    success_count += 1
                elif isinstance(raw, list) and len(raw) == 0:
                    self.data[k] = {}
                elif raw is None:
                    self.data[k] = {}
                else:
                    success_count += 1
        
        logger.info(f"[API Status] Success: {success_count} | Failed: {len(tasks_generic) - success_count} endpoints.")
        
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
            
            if not p: return None

            # === 0. 锁定当前时间轴 ===
            today_str = datetime.now().strftime("%Y-%m-%d")

            # === 1. 基础数据 ===
            price = self.extract(q, "price", "Quote Price", default=p.get("price"))
            price_200ma = self.extract(q, "priceAvg200", "200 Day MA", required=False)
            sector = self.extract(p, "sector", "Sector", default="Unknown")
            industry = self.extract(p, "industry", "Industry", default="Unknown")
            beta = self.extract(p, "beta", "Beta", default=1.0)
            m_cap = self.extract(q, "marketCap", "MarketCap", default=p.get("mktCap"))
            vol_today = self.extract(q, "volume", "Volume", required=False)
            vol_avg = self.extract(q, "avgVolume", "Avg Volume", required=False)
            
            ev_ebitda = self.extract(r, "enterpriseValueMultipleTTM", "EV/EBITDA", required=False)
            if ev_ebitda is None: ev_ebitda = self.extract(m, "enterpriseValueOverEBITDATTM", "EV/EBITDA", required=False)
            
            fcf_yield_api = self.extract(m, "freeCashFlowYieldTTM", "FCF Yield", required=False)
            self.fcf_yield_api = fcf_yield_api # 保存API原始值
            
            roic = self.extract(m, "returnOnInvestedCapitalTTM", "ROIC", required=False)
            net_margin = self.extract(r, "netProfitMarginTTM", "Net Margin", required=False)
            ps_ratio = self.extract(r, "priceToSalesRatioTTM", "P/S", required=False)
            peg_ttm = self.extract(r, "priceToEarningsGrowthRatioTTM", "PEG TTM", required=False)
            pe_ttm = self.extract(r, "priceToEarningsRatioTTM", "PE TTM", required=False)
            ni_growth = self.extract(g, "netIncomeGrowth", "NI Growth", required=False)
            rev_growth = self.extract(g, "revenueGrowth", "Rev Growth", required=False)
            
            # 严格盈利判定
            net_income = self.extract(m, "netIncomePerShareTTM", "EPS", default=0)
            is_profitable_strict = (net_income is not None and net_income > 0)

            # Logs
            past_earnings_for_log = [e for e in earnings_raw if e.get("date", "9999-99-99") <= today_str]
            if past_earnings_for_log:
                latest_q = sorted(past_earnings_for_log, key=lambda x: x.get("date", "0000-00-00"), reverse=True)[0]
                logger.info(f"[Earnings] Latest: {latest_q.get('date')} | EPS: {latest_q.get('epsActual')}")
            else:
                logger.info("[Earnings] No past earnings data found.")
            
            logger.info(f"[Data Snapshot] Price: {price} | MCap: {format_market_cap(m_cap)} | Beta: {beta}")

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
                    var_95_pct = beta * (vix_val / 100.0) * math.sqrt(1/12) * 1.65
                    if beta > 1.5 or not is_profitable_strict:
                        var_95_pct *= 1.2
                    self.risk_var = f"-{format_percent(var_95_pct)}"

            # === 3. 维度分析 ===
            
            # (A) 赛道
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

            # (B) Meme
            meme_score = 0
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
            if roic and roic > 0.20 and (peg_ttm and 0 < peg_ttm < 3.0): meme_score -= 3 
            
            meme_score = max(0, min(10, meme_score))
            meme_pct = int(meme_score * 10)
            
            if meme_pct >= 80: self.signals.add("MEME_EXTREME")
            
            if meme_pct >= 50:
                meme_log = ""
                if 50 <= meme_pct < 60: meme_log = f"[信仰] Meme值 {meme_pct}%。市场关注度提升，资金动量正在影响短期价格走势。"
                elif 60 <= meme_pct < 70: meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪高度活跃，体现出显著的**资金共识**和高流动性。"
                elif 70 <= meme_pct < 80: meme_log = f"[信仰] Meme值 {meme_pct}%。资金聚焦度极高，公司获得大量**关注溢价**，价格驱动力强劲。"
                elif 80 <= meme_pct < 90: meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪已进入非理性繁荣区间，价格体现出**极致的资金动能**。"
                elif meme_pct >= 90: meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪处于顶峰，反映出**极强的短期向上动量**。"
                if meme_log: self.logs.insert(0, meme_log)

            # (C) 盈利质量
            if net_margin and net_margin > 0.20:
                self.logs.append(f"[盈利质量] 净利率 ({format_percent(net_margin)}) 极高，展现出强大的产品定价权或成本控制力。")
            if net_margin and net_margin < -0.10: self.signals.add("DEEP_LOSS")

            # (D) PEG & Growth
            forward_peg = None
            fwd_growth = None
            if estimates and len(estimates) > 0 and price:
                try:
                    estimates.sort(key=lambda x: x.get("date", "0000-00-00"))
                    future = [e for e in estimates if e.get("date", "") > today_str]
                    if len(future) >= 2:
                        fy1, fy2 = future[0], future[1]
                        if fy1.get("epsAvg") and fy1.get("epsAvg") > 0:
                            fwd_pe = price / fy1.get("epsAvg")
                            fwd_growth = (fy2.get("epsAvg") - fy1.get("epsAvg")) / fy1.get("epsAvg")
                            if fwd_growth > 0: forward_peg = fwd_pe / (fwd_growth * 100)
                except: pass
            
            peg_used = forward_peg if forward_peg is not None else peg_ttm
            is_forward_peg_used = (forward_peg is not None)
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

            # (E) P/S & EV/EBITDA
            sector_avg = get_sector_benchmark(sector)
            
            if not is_profitable_strict and ps_ratio is not None:
                th_low, th_fair, th_high = 1.5, 3.0, 8.0
                if is_blue_ocean: th_low, th_fair, th_high = 2.0, 5.0, 15.0
                th_low *= macro_discount_factor; th_fair *= macro_discount_factor; th_high *= macro_discount_factor
                
                ps_desc = ""
                if ps_ratio < th_low: self.signals.add("PS_LOW"); ps_desc = "处于历史低位，相对于营收规模被低估"
                elif ps_ratio < th_fair: ps_desc = "处于合理区间"
                elif ps_ratio < th_high: ps_desc = "较高，市场给予了较高的增长溢价"
                else: self.signals.add("PS_EXTREME"); ps_desc = "极高，价格已透支未来多年的增长"
                self.logs.append(f"[核心估值] P/S 估值: {format_num(ps_ratio)} ({ps_desc})。")

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

            # (F) Cash Flow
            adj_fcf_yield = None
            if len(cf_list) >= 4 and m_cap and m_cap > 0:
                ttm_cfo = sum(self.extract(x, "netCashProvidedByOperatingActivities", "", 0, False) for x in cf_list)
                ttm_da = sum(self.extract(x, "depreciationAndAmortization", "", 0, False) for x in cf_list)
                adj_fcf_yield = (ttm_cfo - ttm_da*0.5) / m_cap
                self.fcf_yield_display = format_percent(adj_fcf_yield)
            
            logger.info(f"[Cash Flow] TTM FCF Yield: {format_percent(fcf_yield_api)} | Adj FCF Yield: {format_percent(adj_fcf_yield)}")

            if roic and roic > 0.20: 
                self.signals.add("QUALITY_TOP_TIER") 
                self.logs.append(f"[护城河] ROIC ({format_percent(roic)}) 极高，资本效率顶级。")
            elif roic and roic > 0.10: 
                self.signals.add("QUALITY_GOOD")
            elif roic and roic < 0:
                self.signals.add("QUALITY_BAD")
            else:
                 self.signals.add("QUALITY_AVG")

            if fcf_yield_api is not None:
                if fcf_yield_api > 0.035: self.signals.add("CASHFLOW_RICH") 
                elif fcf_yield_api < -0.01: self.signals.add("CASHFLOW_NEGATIVE")
                
                if adj_fcf_yield is not None:
                     if adj_fcf_yield > (fcf_yield_api + 0.0005):
                        if roic and roic > 0.15:
                            self.signals.add("QUALITY_EXPANSION")
                            self.logs.append(f"[价值修正] Adj FCF Yield ({self.fcf_yield_display}) 高于 原始 FCF ({format_percent(fcf_yield_api)})。结合极高的 **ROIC ({format_percent(roic)})**，说明巨额资本开支正高效转化为增长，高强度的扩张投入掩盖了其真实的现金流产生能力。")
                        else:
                            self.logs.append(f"[价值修正] Adj FCF Yield ({self.fcf_yield_display}) 高于 原始 FCF ({format_percent(fcf_yield_api)})，反映出增长性资本支出的积极影响。")

            # (G) Alpha & Turnaround
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
            earns_str = ""
            if len(recent_4) > 0:
                beats = sum(1 for e in recent_4 if e.get('epsEstimated') is not None and e.get('epsActual') is not None and e['epsActual'] > e['epsEstimated'])
                
                if beats == 4: self.logs.append(f"[Alpha] 过去 4 季度业绩全部超预期，机构情绪乐观。")
                elif beats >= 2: self.logs.append(f"[Alpha] 过去 4 季度中有 {beats} 次业绩超预期。")
                else: self.logs.append(f"[Alpha] 过去 4 季度中有 {4-beats} 次业绩不及预期，需警惕。")
                
                epss = [e.get('epsActual') for e in recent_4 if e.get('epsActual') is not None]
                if len(epss) >= 2:
                    if epss[-1] > 0 and all(x < 0 for x in epss[:-1]):
                        self.signals.add("TURNAROUND_PROFIT")
                        self.logs.append(f"[反转信号] **扭亏为盈**。本季 EPS 首次转正，基本面迎来关键拐点。")
                    elif all(x < 0 for x in epss) and epss[-1] > epss[-2]:
                        self.signals.add("LOSS_NARROWING")
                        self.logs.append(f"[反转信号] 亏损环比收窄。经营效率提升，距离盈利平衡点渐近。")

            # Context
            self.context_for_ai = f"""
            [基础] 价格:{price}, 市值:{format_market_cap(m_cap)}, Beta:{beta}, 行业:{sector}
            [估值] PE:{format_num(pe_ttm)}, PEG:{format_num(peg_used)}, PS:{format_num(ps_ratio)}, EV/EBITDA:{format_num(ev_ebitda)}
            [效率] ROIC:{format_percent(roic)}, 净利率:{format_percent(net_margin)}, FCF Yield:{format_percent(fcf_yield_api)}
            [成长] 营收增长:{format_percent(rev_growth)}, 净利增长:{format_percent(ni_growth)}, 预期增长:{format_percent(fwd_growth)}
            [趋势] 现价 vs 200均线: {"高于" if price and price_200ma and price>price_200ma else "低于"}
            [风险] 月度VaR:{self.risk_var}, 宏观美债:{yield_10y}%
            [已识别因子] {', '.join(list(self.signals))}
            [近期业绩] {earns_str}
            """

            self.short_term_verdict = "合理溢价" if "MEME_EXTREME" in self.signals else ("高估" if "VALUATION_EXPENSIVE" in self.signals else "中性")
            self.long_term_verdict = "中性"
            if "QUALITY_TOP_TIER" in self.signals: self.long_term_verdict = "优质"
            if "HARD_TECH" in self.signals and "PEG_UNDERVALUED" in self.signals: self.long_term_verdict = "低估"

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

class AnalysisBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session = None
    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
    async def close(self):
        if self.session: await self.session.close()
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

    strategy_text = await ask_deepseek_strategy(interaction.client.session, ticker.upper(), model.context_for_ai)

    profit_label = "盈利" if data.get('is_profitable', False) else "亏损"

    embed = discord.Embed(
        title=f"估值分析: {ticker.upper()}",
        description=f"现价: ${data['price']:.2f} | 市值: {format_market_cap(data['m_cap'])} | {profit_label}",
        color=0x2b2d31
    )

    verdict_str = (
        f"**短期:** {model.short_term_verdict}\n"
        f"**长期:** {model.long_term_verdict}"
    )
    embed.add_field(name="估值结论", value=verdict_str, inline=False)

    beta_val = data['beta']
    beta_desc = "高波动" if beta_val > 1.3 else ("低波动" if beta_val < 0.8 else "适中")
    
    meme_pct = data['meme_pct']
    meme_desc = "低关注度"
    if meme_pct >= 80: meme_desc = "资金狂热"
    elif meme_pct >= 60: meme_desc = "高流动性"
    elif meme_pct >= 30: meme_desc = "市场关注"
    
    core_str = (
        f"**Beta:** {format_num(beta_val)} ({beta_desc})\n"
        f"**Meme值:** {meme_pct}% ({meme_desc})"
    )
    embed.add_field(name="核心特征", value=core_str, inline=False)

    if model.risk_var != "N/A":
        embed.add_field(
            name="95% VaR (月度风险)", 
            value=f"最大回撤可能在 **{model.risk_var}** 附近", 
            inline=False
        )

    formatted_logs = []
    for log in model.logs:
        if log.startswith("[") and "]" in log:
            tag_end = log.find("]") + 1
            tag = log[:tag_end]
            content = log[tag_end:]
            formatted_logs.append(f"**{tag}**{content}")
        else:
            formatted_logs.append(f"{log}")
    
    factor_str = "\n".join([f"> {l}" for l in formatted_logs])
    if not factor_str: factor_str = "> 数据平淡，未触发显著因子。"
    
    embed.add_field(name="因子分析", value=factor_str, inline=False)
    embed.add_field(name="[策略]", value=strategy_text, inline=False)
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
