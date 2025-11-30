import discord
from discord import app_commands
from discord.ext import commands
import aiohttp  # 异步请求库
import os
import asyncio
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import Optional

# 加载环境变量
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
FMP_API_KEY = os.getenv('FMP_API_KEY')

# *** 核心：全局唯一接口地址 (Stable) ***
BASE_URL = "https://financialmodelingprep.com/stable"

# --- 全局状态 ---
PRIVACY_MODE = {}

# --- 白名单 ---
HARD_TECH_TICKERS = ["RKLB", "LUNR", "ASTS", "SPCE", "PLTR", "IONQ", "RGTI", "DNA", "JOBY", "ACHR", "BABA", "NIO", "XPEV", "LI", "TSLA", "NVDA", "AMD", "MSFT", "GOOG", "GOOGL"]

# --- 关键词词典 ---
BLUE_OCEAN_KEYWORDS = ["aerospace", "defense", "space", "satellite", "rocket", "quantum"]
HARD_TECH_KEYWORDS = ["semiconductor", "artificial intelligence", "software", "auto", "biotech", "internet"]

# --- 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("ValuationBot")

# --- 1. 异步数据工具函数 ---

async def get_json_safely(session: aiohttp.ClientSession, url: str):
    """安全获取 JSON (异步)"""
    try:
        async with session.get(url, timeout=10) as response:
            if response.status != 200:
                logger.warning(f"API Status {response.status} for {url}")
                return None
            
            try:
                data = await response.json()
            except Exception:
                logger.error(f"Failed to parse JSON from {url}")
                return None

            if isinstance(data, dict) and "Error Message" in data:
                logger.warning(f"API Error for {url}: {data['Error Message']}")
                return None
                
            return data
    except Exception as e:
        logger.error(f"Request failed for {url}: {e}")
        return None

async def get_treasury_rates(session: aiohttp.ClientSession):
    """获取最新的国债收益率 (异步)"""
    today = datetime.now()
    start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")
    
    url = f"{BASE_URL}/treasury-rates?from={start_date}&to={end_date}&apikey={FMP_API_KEY}"
    
    data = await get_json_safely(session, url)
    if data and isinstance(data, list) and len(data) > 0:
        logger.info(f"✅ [API] Treasury Data fetched: {len(data)} records.")
        return data[0]
    
    return None

async def get_company_profile_smart(session: aiohttp.ClientSession, ticker: str):
    """智能获取公司 Profile (异步)"""
    url_profile = f"{BASE_URL}/profile?symbol={ticker}&apikey={FMP_API_KEY}"
    logger.info(f"📡 Trying Profile Endpoint: {ticker}")
    data = await get_json_safely(session, url_profile)
    
    if data and isinstance(data, list) and len(data) > 0:
        logger.info(f"✅ [API] Profile Data fetched for {ticker}.")
        return data[0]
    
    logger.info(f"⚠️ Profile failed. Switching to Screener for {ticker}")
    url_screener = f"{BASE_URL}/stock-screener?symbol={ticker}&apikey={FMP_API_KEY}"
    data_scr = await get_json_safely(session, url_screener)
    
    if data_scr and isinstance(data_scr, list) and len(data_scr) > 0:
        item = data_scr[0]
        logger.info(f"✅ [API] Profile fetched via Screener for {ticker}.")
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
    """通用接口获取函数 (异步)"""
    url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey={FMP_API_KEY}"
    if params:
        url += f"&{params}"
        
    data = await get_json_safely(session, url)
    if data:
        count = len(data) if isinstance(data, list) else (1 if data else 0)
        logger.info(f"✅ [API] {endpoint} fetched: {count} items.")
    else:
        logger.warning(f"⚠️ [API] {endpoint} returned None/Empty.")
    return data

async def get_estimates_data(session: aiohttp.ClientSession, ticker: str):
    """获取分析师预期数据 (年度) - 异步"""
    url = f"{BASE_URL}/analyst-estimates?symbol={ticker}&period=annual&limit=10&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url)
    if data:
        logger.info(f"✅ [API] Estimates fetched: {len(data)} years.")
    return data if data else []

async def get_earnings_data(session: aiohttp.ClientSession, ticker: str):
    """获取历史财报预期与实际数据 (异步)"""
    url = f"{BASE_URL}/earnings?symbol={ticker}&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url)
    if data:
        logger.info(f"✅ [API] Earnings fetched: {len(data)} quarters (Raw).")
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

# --- 2. 行业基准 ---
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

# --- 3. 估值判断模型 ---

class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        # 补回丢失的属性定义
        self.market_regime = "未知" 
        self.risk_var = "N/A"  
        self.logs = []  
        self.flags = []  
        self.strategy = "数据不足"  
        self.fcf_yield_display = "N/A" 
        self.fcf_yield_api = None 

    def extract(self, source, key, desc, default=None, required=True):
        val = source.get(key)
        if val is None:
            if default is not None:
                logger.info(f"ℹ️ [Info] {desc} ({key}) is None. Using Default: {default}")
                return default
            elif not required:
                logger.info(f"🔹 [Optional] {desc} ({key}) is None. (Skipping)")
                return None
            else:
                logger.warning(f"⚠️ [Missing] {desc} ({key}) is None!")
                return None
        else:
            logger.info(f"✅ [Data] {desc}: {val}")
            return val

    async def fetch_data(self, session: aiohttp.ClientSession):
        """异步获取所有 FMP 数据 (非阻塞并发)"""
        logger.info(f"--- Starting Analysis for {self.ticker} ---")
        
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
        
        profile_data, treasury_data, *generic_results = await asyncio.gather(
            task_profile, 
            task_treasury, 
            *tasks_generic.values()
        )
        
        self.data = dict(zip(tasks_generic.keys(), generic_results))
        self.data["profile"] = profile_data 
        self.data["treasury"] = treasury_data 
        
        for k in ["quote", "metrics", "ratios", "bs", "vix", "growth"]:
            if isinstance(self.data[k], list) and len(self.data[k]) > 0:
                self.data[k] = self.data[k][0]
            elif isinstance(self.data[k], list) and len(self.data[k]) == 0:
                if k != "growth":
                    logger.warning(f"⚠️ [Structure] {k} list is empty.")
                self.data[k] = {} 
            elif self.data[k] is None:
                self.data[k] = {}

        return self.data["profile"] is not None

    def analyze(self):
        try:
            logger.info("--- 🚀 Starting Calculation Logic ---")
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

            # === 1. 基础数据 ===
            price = self.extract(q, "price", "Quote Price", default=p.get("price"))
            price_200ma = self.extract(q, "priceAvg200", "200 Day MA", required=False)
            sector = self.extract(p, "sector", "Sector", "Unknown")
            industry = self.extract(p, "industry", "Industry", "Unknown")
            beta = self.extract(p, "beta", "Beta", default=1.0)
            m_cap = self.extract(q, "marketCap", "MarketCap", default=p.get("mktCap"))
            
            # === 2. 财务指标 ===
            ev_ebitda = self.extract(r, "enterpriseValueMultipleTTM", "EV/EBITDA (Ratio)", required=False)
            if ev_ebitda is None:
                ev_ebitda = self.extract(m, "enterpriseValueOverEBITDATTM", "EV/EBITDA (Metric)", required=False)
            
            fcf_yield_api = self.extract(m, "freeCashFlowYieldTTM", "FCF Yield TTM", required=False)
            self.fcf_yield_api = fcf_yield_api 
            
            roic = self.extract(m, "returnOnInvestedCapitalTTM", "ROIC TTM", required=False)
            net_margin = self.extract(r, "netProfitMarginTTM", "Net Margin TTM", required=False)
            ps_ratio = self.extract(r, "priceToSalesRatioTTM", "P/S Ratio TTM", required=False)
            
            peg_ttm = self.extract(r, "priceToEarningsGrowthRatioTTM", "PEG TTM", required=False)
            pe_ttm = self.extract(r, "priceToEarningsRatioTTM", "PE TTM", required=False)
            
            ni_growth = self.extract(g, "netIncomeGrowth", "Net Income Growth (Annual)", required=False)
            rev_growth = self.extract(g, "revenueGrowth", "Revenue Growth (Annual)", required=False)

            # 盈利检查
            eps_ttm = r.get("netIncomePerShareTTM") or m.get("netIncomePerShareTTM")
            latest_eps = 0
            if isinstance(earnings_raw, list) and len(earnings_raw) > 0:
                sorted_earnings_for_check = sorted(earnings_raw, key=lambda x: x.get("date", "0000-00-00"), reverse=True)
                if sorted_earnings_for_check:
                    val = sorted_earnings_for_check[0].get("epsActual")
                    latest_eps = val if val is not None else 0

            is_profitable_strict = (eps_ttm is not None and eps_ttm > 0) and (latest_eps >= 0)
            logger.info(f"🔎 [Profit Check] Is Profitable Strict: {is_profitable_strict}")

            # === 3. 资产负债 ===
            cash = self.extract(bs, "cashAndCashEquivalents", "Cash", required=False, default=0)
            debt = self.extract(bs, "totalDebt", "Total Debt", required=False, default=0)
            is_cash_rich = (cash > debt) if (cash is not None and debt is not None) else False

            # === 4. Forward PEG 计算 ===
            forward_peg = None
            fwd_pe = None
            fwd_growth = None
            eps_fy1_val = None 
            
            if estimates and len(estimates) > 0 and price:
                try:
                    estimates.sort(key=lambda x: x.get("date", "0000-00-00"))
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    future_estimates = [e for e in estimates if e.get("date", "") > today_str]
                    
                    if len(future_estimates) >= 2:
                        fy1 = future_estimates[0] 
                        fy2 = future_estimates[1] 
                        eps_fy1 = fy1.get("epsAvg")
                        eps_fy2 = fy2.get("epsAvg")
                        eps_fy1_val = eps_fy1 
                        
                        if eps_fy1 is not None and eps_fy1 > 0 and eps_fy2 is not None:
                            fwd_pe = price / eps_fy1
                            fwd_growth = (eps_fy2 - eps_fy1) / eps_fy1
                            if fwd_growth > 0:
                                forward_peg = fwd_pe / (fwd_growth * 100)
                                logger.info(f"✅ [Result] Forward PEG: {forward_peg:.2f}")
                except Exception as e:
                    logger.error(f"❌ Error calculating Forward PEG: {e}")

            peg_used = forward_peg if forward_peg is not None else peg_ttm
            is_forward_peg_used = (forward_peg is not None)

            # Growth Desc
            growth_list = [x for x in [rev_growth, ni_growth, fwd_growth] if x is not None]
            max_growth = max(growth_list) if growth_list else 0
            growth_desc = "低成长"
            if max_growth > 0.5: growth_desc = "超高速"
            elif max_growth > 0.2: growth_desc = "高速"
            elif max_growth > 0.05: growth_desc = "稳健"
            if peg_used and peg_used > 3.0: growth_desc = "高预期"
            
            # === 5. Adjusted FCF Yield ===
            adj_fcf_yield = None
            if len(cf_list) >= 4 and m_cap and m_cap > 0:
                ttm_cfo = 0
                ttm_dep_amort = 0
                quarter_count = 0
                for i, q_data in enumerate(cf_list): 
                    cfo_q = self.extract(q_data, "netCashProvidedByOperatingActivities", f"CF Q{i} CFO", required=False)
                    dep_amort_q = self.extract(q_data, "depreciationAndAmortization", f"CF Q{i} D&A", required=False)
                    if cfo_q is not None and dep_amort_q is not None:
                        ttm_cfo += cfo_q
                        ttm_dep_amort += dep_amort_q
                        quarter_count += 1
                    else:
                        break 

                if ttm_cfo != 0 and quarter_count >= 4:
                    MAINTENANCE_CAPEX_RATIO = 0.5 
                    maintenance_capex = ttm_dep_amort * MAINTENANCE_CAPEX_RATIO
                    adj_fcf = ttm_cfo - maintenance_capex
                    adj_fcf_yield = adj_fcf / m_cap
                    self.fcf_yield_display = format_percent(adj_fcf_yield) 
                
            fcf_yield_used = adj_fcf_yield if adj_fcf_yield is not None else fcf_yield_api
            if fcf_yield_used == fcf_yield_api:
                self.fcf_yield_display = format_percent(fcf_yield_api) 
            
            # === 6. 赛道识别 ===
            is_blue_ocean = False       
            is_hard_tech_growth = False 
            sec_str = str(sector).lower() if sector else ""
            ind_str = str(industry).lower() if industry else ""
            for kw in BLUE_OCEAN_KEYWORDS:
                if kw in sec_str or kw in ind_str:
                    is_blue_ocean = True
                    break
            for kw in HARD_TECH_KEYWORDS:
                if kw in sec_str or kw in ind_str:
                    is_hard_tech_growth = True
                    break
            if self.ticker in HARD_TECH_TICKERS:
                if not is_blue_ocean: is_hard_tech_growth = True

            # === 7. 宏观利率 ===
            yield_10y = self.extract(t, 'year10', "10Y Treasury Yield", required=False)
            macro_discount_factor = 1.0 
            macro_status_log = None
            is_growth_asset = is_blue_ocean or is_hard_tech_growth or (max_growth > 0.15) or (pe_ttm and pe_ttm > 30)

            if is_growth_asset and yield_10y is not None:
                if yield_10y > 4.8:
                    macro_discount_factor = 0.7
                    macro_status_log = f"[宏观压制] 10Y美债收益率 {yield_10y}% (>4.8%)。资金成本高企，成长股估值模型承压，合理估值下修 30%。"
                elif yield_10y < 3.8:
                    macro_discount_factor = 1.5
                    macro_status_log = f"[宏观红利] 10Y美债收益率 {yield_10y}% (<3.8%)。流动性充裕，成长股享受估值扩张，合理估值上浮 50%。"
            
            if macro_status_log:
                self.logs.append(macro_status_log)

            # === 8. VIX & Meme ===
            vix = self.extract(vix_data, "price", "VIX Price", default=20)
            if price and beta and vix:
                monthly_risk_pct = (vix / 100) * beta * 1.0 * 100
                self.risk_var = f"-{monthly_risk_pct:.1f}%"
            
            meme_score = 0
            vol_today = self.extract(q, "volume", "Volume Today", required=False)
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
                bad_peg = (peg_used is not None and (peg_used < 0 or peg_used > 4.0))
                if bad_fcf or bad_peg: meme_score += 2
            if vol_today and vol_avg and vol_avg > 0:
                if vol_today > vol_avg * 1.2: meme_score += 1
            if roic and roic > 0.20:
                if peg_used and 0 < peg_used < 3.0: meme_score -= 3
                else: meme_score -= 1
            meme_score = max(0, min(10, meme_score))
            meme_pct = int(meme_score * 10)
            is_faith_mode = meme_pct >= 50

            # === 9. 估值与策略判定 ===
            sector_avg = get_sector_benchmark(sector)
            st_status = "估值合理"
            is_distressed = False
            
            # 初始化 use_ps_valuation 防止报错
            use_ps_valuation = False
            
            if is_profitable_strict:
                use_ps_valuation = False
            elif is_blue_ocean or is_hard_tech_growth:
                use_ps_valuation = True 
            else:
                if (net_margin is not None and net_margin < -0.05):
                    if rev_growth is not None and rev_growth > 0.10:
                        use_ps_valuation = True
                    else:
                        is_distressed = True
                        st_status = "极其昂贵/困境"
                        self.logs.append(f"[预警] 净利率为负且缺乏增长支撑，EV/EBITDA 指标失效。")
                elif (fcf_yield_api is not None and fcf_yield_api < -0.05):
                     is_distressed = True
                     st_status = "极其昂贵/失血"
                     self.logs.append(f"[预警] 自由现金流严重流失且无增长支撑。")

            if is_cash_rich:
                self.logs.append(f"[资产负债] 公司持有净现金 (现金>债务)，资产负债表健康，抗风险能力强。")
            elif debt and cash and debt > cash * 5:
                self.logs.append(f"[资产负债] 债务负担较重 (债务是现金的5倍以上)，需关注利息支出压力。")

            if net_margin and net_margin > 0.20:
                self.logs.append(f"[盈利质量] 净利率 ({format_percent(net_margin)}) 极高，展现出强大的产品定价权或成本控制力。")

            if not is_distressed:
                if use_ps_valuation:
                    tag = "[蓝海赛道]" if is_blue_ocean else "[硬科技]"
                    if ps_ratio is not None:
                        th_low, th_fair, th_high = 1.5, 3.0, 8.0
                        if is_blue_ocean: th_low, th_fair, th_high = 2.0, 5.0, 15.0
                        th_low *= macro_discount_factor
                        th_fair *= macro_discount_factor
                        th_high *= macro_discount_factor
                        ps_desc = ""
                        if ps_ratio < th_low: 
                            st_status = "低估 (P/S)"
                            ps_desc = "处于历史低位，相对于营收规模被低估"
                            self.strategy = "当前价格包含极高安全边际，关注困境反转逻辑。"
                        elif ps_ratio < th_fair:
                            st_status = "合理 (P/S)"
                            ps_desc = "处于合理区间"
                        elif ps_ratio < th_high:
                            st_status = "溢价 (P/S)"
                            ps_desc = "较高，市场给予了较高的增长溢价"
                        else:
                            st_status = "过热 (P/S)"
                            ps_desc = "极高，价格已透支未来多年的增长"
                        self.logs.append(f"{tag} P/S 估值：{format_num(ps_ratio)} ({ps_desc})。")
                elif ev_ebitda is not None:
                    ratio = ev_ebitda / sector_avg
                    adjusted_ratio = ratio / macro_discount_factor if macro_discount_factor != 0 else ratio
                    if ("高速" in growth_desc or "预期" in growth_desc) and (peg_used is not None and 0 < peg_used < 1.5):
                        st_status = "便宜 (高成长)"
                        self.logs.append(f"[成长特权] 虽 EV/EBITDA ({format_num(ev_ebitda)}) 偏高，但 PEG ({format_num(peg_used)}) 极低，属于越涨越便宜。")
                    elif adjusted_ratio < 0.7:
                        st_status = "便宜"
                        self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 低于行业均值 ({sector_avg})，折扣明显。")
                    elif adjusted_ratio > 1.3:
                        if ("高速" in growth_desc or "预期" in growth_desc) and (peg_used is not None and 0 < peg_used < 2.0):
                            st_status = "合理溢价"
                            self.logs.append(f"[成长特权] 高估值 ({format_num(ev_ebitda)}) 被高增长消化，溢价合理。")
                        else:
                            st_status = "昂贵"
                            self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 远高于行业均值 ({sector_avg})，且缺乏增长支撑。")
                    else:
                        st_status = "估值合理"
                        self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 与行业均值 ({sector_avg}) 接近，估值处于合理区间。")
            
            self.short_term_verdict = st_status

            # --- 长期 ---
            lt_status = "中性"
            is_value_trap = False
            if net_margin is not None and net_margin < 0 and price_200ma and price and price < price_200ma:
                if not use_ps_valuation: 
                    is_value_trap = True
                    lt_status = "风险极大"
                    st_status = "下跌趋势"
                    self.logs.append(f"[风险] 公司长期亏损且股价位于年线下方，看似低估实为“价值陷阱”。")
                    self.strategy = "趋势与基本面双弱，存在‘接飞刀’的风险"
            
            # --- 巨头判断 ---
            # 如果市值 > 2000亿，视为巨头
            is_giant = m_cap is not None and m_cap > 200_000_000_000

            if not is_value_trap:
                # PEG Log
                peg_display = format_num(peg_used) if peg_used is not None else "N/A"
                peg_status = "N/A"
                peg_comment = ""
                peg_type_str = "Forward" if is_forward_peg_used else "TTM"
                
                if peg_used is not None and peg_used > 0:
                    if is_blue_ocean: 
                        if peg_used < 0.5: peg_status = "极低/数据失真"; peg_comment = "基数过小可能导致失真，参考意义有限。"
                        elif peg_used < 1.5: peg_status = "低估"; peg_comment = f"相对于未来的爆发潜力，当前价格处于低位 ({peg_type_str})。"
                        elif peg_used <= 4.0: peg_status = "合理 (高容忍)"; peg_comment = f"市场给予蓝海赛道极高的增长容忍度 ({peg_type_str})。"
                        else: peg_status = "高估/透支"; peg_comment = "预期已大幅透支，需警惕回调。"
                    elif is_hard_tech_growth: 
                        if peg_used < 1.0: peg_status = "极度低估/罕见"; peg_comment = f"对于硬科技资产，此 {peg_type_str} PEG 属于罕见的低估区间。"
                        elif peg_used <= 2.0: peg_status = "合理 (GARP)"; peg_comment = f"属于合理的成长股估值区间 ({peg_type_str})。"
                        elif peg_used <= 3.0: peg_status = "溢价"; peg_comment = "包含了一定的情绪溢价，但在牛市中可接受。"
                        else: peg_status = "泡沫化风险"; peg_comment = "估值已脱离基本面引力，风险较高。"
                    else: 
                        if peg_used < 0.8: peg_status = "低估"; peg_comment = "具备极高的安全边际。"
                        elif peg_used <= 1.5: peg_status = "合理"; peg_comment = "估值与增长匹配。"
                    self.logs.append(f"[成长锚点] PEG ({peg_type_str}): {peg_display} ({peg_status})。{peg_comment}")
                elif peg_used is None:
                    if not is_profitable_strict and (eps_fy1_val is None or eps_fy1_val <= 0):
                         # 修改：亏损企业 PEG 文案 - 简洁事实
                         self.logs.append(f"[成长锚点] PEG ({peg_type_str}): {peg_display} (负值)。公司尚未盈利。")
                    else:
                         self.logs.append(f"[成长锚点] PEG 数据缺失。")
                else:
                     self.logs.append(f"[成长锚点] PEG ({peg_type_str}): {peg_display} (负值)。预期业绩在下滑，注意风险。")

                # Meme
                if is_faith_mode:
                    meme_log = ""
                    meme_strategy_text = "价格波动性可能增加，交易决策可以结合市场动量指标。"

                    if 50 <= meme_pct < 60:
                        meme_log = f"[信仰] Meme值 {meme_pct}%。市场关注度提升，资金动量正在影响短期价格走势。"
                    elif 60 <= meme_pct < 70:
                        meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪高度活跃，体现出显著的**资金共识**和高流动性。"
                    elif 70 <= meme_pct < 80:
                        meme_log = f"[信仰] Meme值 {meme_pct}%。资金聚焦度极高，公司获得大量**关注溢价**，价格驱动力强劲。"
                    elif 80 <= meme_pct < 90:
                        meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪已进入非理性繁荣区间，价格体现出**极致的资金动能**。"
                        meme_strategy_text = "此时价格驱动因素主要为情绪和资金流，应极为谨慎评估其风险收益比。"
                    elif meme_pct >= 90:
                        meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪处于顶峰，反映出**极强的短期向上动量**。"
                        meme_strategy_text = "市场波动和回调风险已处于历史高位，对于中长期投资者而言，保持警惕性至关重要。"
                    
                    # 巨头豁免逻辑：如果是巨头且 Meme < 80%，不显示 Meme 策略，也不强制覆盖
                    if is_giant and meme_pct < 80:
                        if meme_log: self.logs.insert(0, meme_log)
                    else:
                        if meme_log: self.logs.insert(0, meme_log)
                        if "昂贵" in st_status: st_status += " / 资金动量"
                        if "昂贵" in lt_status: lt_status = "高溢价 (资金动量)"
                        if self.strategy == "数据不足": self.strategy = meme_strategy_text

                # FCF Logic
                if fcf_yield_used is not None:
                    fcf_str = self.fcf_yield_display
                    is_high_quality_growth = (
                        ("高速" in growth_desc or "超高速" in growth_desc or 
                        ("稳健" in growth_desc and roic is not None and roic > 0.20))
                        and roic is not None and roic > 0.15
                    )
                    is_adj_fcf_successful = adj_fcf_yield is not None
                    
                    if is_adj_fcf_successful and use_ps_valuation:
                        if fcf_yield_api is not None and adj_fcf_yield > (fcf_yield_api + 0.0005): 
                            self.logs.append(f"[资本开支] Adj FCF Yield ({fcf_str}) 优于 原始 FCF ({format_percent(fcf_yield_api)})，反映出显著的**前置性资本投入**特征。")
                            if adj_fcf_yield > 0.04: lt_status = "便宜"
                    
                    elif is_adj_fcf_successful and not use_ps_valuation:
                        if adj_fcf_yield > 0.04 and not is_faith_mode:
                            lt_status = "便宜"
                            self.logs.append(f"[价值修正] Adj FCF Yield ({fcf_str}) 高于 原始 FCF ({format_percent(fcf_yield_api)})。这表明当前资本开支主要用于**增长性扩张**，剔除此因素后，公司核心造血能力强劲。")
                            if self.strategy == "数据不足": self.strategy = "当前价格具备较好的安全边际，存在价值投资的可能。"
                        elif fcf_yield_api is not None and adj_fcf_yield > (fcf_yield_api + 0.0005):
                            if roic and roic > 0.15:
                                self.logs.append(f"[价值修正] Adj FCF Yield ({fcf_str}) 高于 原始 FCF ({format_percent(fcf_yield_api)})。结合极高的 **ROIC ({format_percent(roic)})**，说明巨额资本开支正高效转化为增长，高强度的扩张投入掩盖了其真实的现金流产生能力。")
                            else:
                                self.logs.append(f"[价值修正] Adj FCF Yield ({fcf_str}) 高于 原始 FCF ({format_percent(fcf_yield_api)})，反映出增长性资本支出的积极影响。")

                    if is_blue_ocean:
                        lt_status = "蓝海/战略卡位"
                        if not is_adj_fcf_successful:
                            self.logs.append(f"[护城河] 处于竞争不充分的蓝海市场，行业壁垒极高，稀缺性溢价合理。")
                        if self.strategy == "数据不足" or "风险" in self.strategy:
                            self.strategy = "估值锚点在于远期市场垄断地位。短期受资金情绪影响大，适合在技术回调时分批布局，非信徒需谨慎。"
                    
                    elif is_hard_tech_growth and use_ps_valuation:
                        lt_status = "观察/成长"
                        if self.strategy == "数据不足" or "风险" in self.strategy:
                            self.strategy = "当前处于以投入换增长的阶段。重点关注营收增速的持续性以及毛利率的边际改善。"

                    if not use_ps_valuation and (not is_adj_fcf_successful or (is_adj_fcf_successful and lt_status != "便宜")):
                        fcf_threshold = 0.01 if (roic and roic > 0.20) else 0.02
                        if fcf_yield_used < fcf_threshold and is_high_quality_growth and not is_faith_mode:
                            lt_status = "预期驱动/投资扩张"
                            self.logs.append(f"[辩证] FCF Yield ({fcf_str}) 较低，但高增长/高ROIC ({format_percent(roic)}) 表明其 CapEx 多为**增长性投资**，当前估值是合理的增长溢价。")
                        elif fcf_yield_used < fcf_threshold and not is_high_quality_growth and not is_faith_mode:
                            lt_status = "昂贵"
                            self.logs.append(f"[价值] FCF Yield ({fcf_str}) 极低且无明显高增长支撑，隐含预期过高，风险较大。")
                            if self.strategy == "数据不足": self.strategy = "风险收益比不佳，当前估值缺乏基本面支撑，应审慎。"
                        elif roic and roic > 0.20 and not is_faith_mode:
                            lt_status = "优质/值得等待"
                            has_value_fix_log = any("[价值修正]" in x for x in self.logs)
                            if not has_value_fix_log:
                                self.logs.append(f"[辩证] ROIC ({format_percent(roic)}) 极高，属于'优质溢价'资产。")
                            if self.strategy == "数据不足" or "风险" in self.strategy:
                                if ev_ebitda is not None and ev_ebitda < sector_avg * 0.9:
                                    self.strategy = "【黄金配置窗口】极为罕见！公司拥有顶级资本效率(高ROIC)，却交易在行业估值折价区。属于‘好行业、好公司、好价格’的不可能三角，强烈建议关注。"
                                else:
                                    # 针对巨头的优化策略
                                    if is_giant and adj_fcf_yield and adj_fcf_yield > 0.03:
                                        self.strategy = "EV/EBITDA 显示其估值处于合理区间，并未出现明显泡沫。强大的经营现金流支撑了当前市值，属于‘价格公道的好公司’，适合稳健配置。"
                                    else:
                                        self.strategy = "行业地位稳固，护城河极深。当前估值与增长潜力匹配度高，属于典型的‘核心资产’。适合作为长期底仓，赚取业绩增长的钱。"

                if roic and roic > 0.15 and "昂贵" not in lt_status and not is_value_trap:
                    has_dialectic = any("[辩证]" in x or "[价值修正]" in x for x in self.logs)
                    if not has_dialectic:
                        self.logs.append(f"[护城河] ROIC ({format_percent(roic)}) 优秀，资本效率高。")
                    if lt_status == "中性": lt_status = "优质"
                
                if fcf_yield_used is None and not use_ps_valuation:
                    self.logs.append(f"[预警] FCF Yield 数据缺失，无法进行基于现金流的长期估值。")

                # --- [扭亏/业绩追踪] ---
                valid_earnings = []
                today_str = datetime.now().strftime("%Y-%m-%d")
                if isinstance(earnings_raw, list):
                    sorted_earnings = sorted(earnings_raw, key=lambda x: x.get("date", "0000-00-00"), reverse=True)
                    recent_earnings = sorted_earnings[:12]
                    logger.info(f"🔄 Processing Recent Earnings List ({len(recent_earnings)} items from top)...")
                    for e in recent_earnings:
                        date = e.get("date")
                        if date and date <= today_str:
                            rev = self.extract(e, "revenueActual", f"Earn {date} Rev", default=e.get("revenue"))
                            eps = self.extract(e, "epsActual", f"Earn {date} EPS")
                            est = self.extract(e, "epsEstimated", f"Earn {date} Est")
                            if rev is not None and eps is not None:
                                valid_earnings.append({"date": date, "rev": rev, "eps": eps, "est": est})
                
                trend_data = sorted(valid_earnings, key=lambda x: x["date"])
                recent_4 = trend_data[-4:] 
                if len(recent_4) >= 3:
                    epss = [x["eps"] for x in recent_4]
                    if all(e < 0 for e in epss[:-1]) and epss[-1] > 0:
                        self.logs.append(f"[反转信号] **扭亏为盈**。本季 EPS 首次转正，基本面迎来关键拐点。")
                    elif all(e < 0 for e in epss):
                        if epss[-1] > epss[-2]:
                            self.logs.append(f"[反转信号] 亏损环比收窄。经营效率提升，距离盈利平衡点渐近。")

                if len(recent_4) > 0:
                    beats = sum(1 for x in recent_4 if x["est"] is not None and x["eps"] > x["est"])
                    total = len(recent_4)
                    if total > 0:
                        beat_rate = beats / total
                        if beat_rate >= 0.75:
                            self.logs.append(f"[Alpha] 过去 {total} 季度中有 {beats} 次业绩超预期，机构情绪乐观。")
                        else:
                            self.logs.append(f"[Alpha] 过去 {total} 季度中有 {total - beats} 次业绩不及预期，需警惕。")
                else:
                    self.logs.append(f"[Alpha] 暂无有效历史财报数据，无法判断业绩趋势。")
                
                # --- 补漏策略 ---
                if self.strategy == "数据不足":
                    if rev_growth and rev_growth > 0.20 and roic and roic < 0 and fcf_yield_used and fcf_yield_used < -0.02:
                        self.strategy = "增长完全依赖外部输血(烧钱)，且资本效率低下(ROIC为负)。在流动性收紧环境下风险极大，需警惕融资困难。"
                    elif rev_growth and abs(rev_growth) < 0.05 and roic and roic < 0.08 and fcf_yield_used and fcf_yield_used < 0.03:
                        self.strategy = "缺乏增长引擎，且资本回报率平庸。属于典型的‘僵尸股’特征，机会成本较高，建议回避。"

                if pe_ttm and pe_ttm < 8 and rev_growth and rev_growth < -0.05 and "风险" not in lt_status:
                    self.strategy = "估值看似极低，但营收处于萎缩周期，需要警惕‘低估值陷阱’。"
                    lt_status = "周期性风险"
                    self.logs.append(f"[陷阱] PE ({format_num(pe_ttm)}) 虽低，但营收负增长 ({format_percent(rev_growth)})，疑似周期顶部信号。")

                elif beta and beta < 0.6 and fcf_yield_used and fcf_yield_used > 0.03 and "陷阱" not in self.strategy:
                    self.strategy = "低波动防御性资产，可视为市场震荡环境下的潜在避险配置。"
                    lt_status = "防御/收息"
                    self.logs.append(f"[防御] Beta ({format_num(beta)}) 极低且现金流健康，具备类似债券的特征。")

            self.long_term_verdict = lt_status

            return {
                "price": price,
                "beta": beta,
                "market_regime": self.market_regime,
                "peg": peg_used,
                "m_cap": m_cap,
                "growth_desc": growth_desc,
                "risk_var": self.risk_var,
                "meme_pct": meme_pct,
                "is_profitable": is_profitable_strict 
            }
        except Exception as e:
            logger.error(f"Analyze Error: {e}")
            return None

# -------------------------------------------------------------------
# AnalysisBot 类必须在 bot 实例化之前定义
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# 实例化 Bot
# -------------------------------------------------------------------
bot = AnalysisBot()

@bot.tree.command(name="privacy", description="切换隐私查询模式 (开启后分析结果仅自己可见)")
async def privacy(interaction: discord.Interaction):
    user_id = interaction.user.id
    is_on = PRIVACY_MODE.get(user_id, False)
    new_state = not is_on
    PRIVACY_MODE[user_id] = new_state
    status = "已开启 (查询结果仅自己可见)" if new_state else "已关闭 (查询结果公开)"
    await interaction.response.send_message(f"✅ 隐私模式切换成功。\n当前状态: **{status}**", ephemeral=True)

async def process_analysis(interaction: discord.Interaction, ticker: str, force_private: bool = False):
    is_privacy_mode = force_private or PRIVACY_MODE.get(interaction.user.id, False)
    ephemeral_result = is_privacy_mode
    
    await interaction.response.defer(thinking=True, ephemeral=ephemeral_result) 

    model = ValuationModel(ticker)
    success = await model.fetch_data(interaction.client.session)
    
    if is_privacy_mode and success:
        public_embed = discord.Embed(
            description=f"**{interaction.user.display_name}** 开启《稳-量化估值系统》\n⚡正在分析“{ticker.upper()}”中...",
            color=0x2b2d31
        )
        try:
            await interaction.channel.send(embed=public_embed) 
        except Exception as e:
            logger.error(f"Failed to send public status message: {e}")
    
    if not success:
        await interaction.followup.send(f"❌ 获取数据失败: `{ticker.upper()}`", ephemeral=ephemeral_result)
        return

    data = model.analyze()
    if not data:
        await interaction.followup.send(f"⚠️ 数据不足。", ephemeral=ephemeral_result)
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
