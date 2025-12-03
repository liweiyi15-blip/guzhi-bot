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

# --- 1. 异步数据工具函数 (完全还原) ---

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

# --- DeepSeek 分析引擎 ---
async def ask_deepseek_strategy(session: aiohttp.ClientSession, ticker: str, context_str: str):
    if not DEEPSEEK_API_KEY: return "未配置 DeepSeek Key，无法生成策略。"
    
    system_prompt = (
        "你是一位拥有十年华尔街实战经验的机构交易高手。请基于提供的数据，站在【多头视角】，对该标的做出科学、辩证、客观且极具实战性的策略分析。\n"
        "【严格执行以下要求】：\n"
        "1. **严禁出现数字**：用“估值处于高位”、“资金分歧巨大”等专业定性描述代替具体数据。\n"
        "2. **通俗且专业**：用大白话讲透核心逻辑，拒绝晦涩。\n"
        "3. **字数限制**：80字以内！\n"
        "4. **实战侧重**：结合市场情绪与基本面，明确上涨逻辑与潜在隐患，给出具体操作指引（如：趋势未破可持股、回踩重要均线低吸）。"
    )
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"标的：{ticker}\n\n{context_str}"}
        ],
        "temperature": 0.6,
        "max_tokens": 120
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

# --- 2. 核心：还原 ValuationModel ---
class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        self.risk_var = "N/A"  
        self.logs = []  
        self.flags = []  
        self.strategy = "数据不足" 
        self.fcf_yield_display = "N/A" 
        self.fcf_yield_api = None 
        
        self.signals = set()
        self.context_for_ai = "" # AI Context

    # 【重要】恢复 extract 方法，这是空值保护的核心
    def extract(self, source, key, desc, default=None, required=True):
        val = source.get(key)
        if val is None:
            if default is not None:
                return default
            elif not required:
                return None
            else:
                # logger.warning(f"[Missing] {desc} ({key}) is None!") # 减少日志噪音
                return None
        else:
            return val

    # 【重要】恢复 fetch_data 原始逻辑
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
        
        # 数据整理逻辑 (恢复原样)
        for k in tasks_generic.keys():
            raw = self.data[k]
            list_keys = ["earnings", "estimates", "cf"]
            if k in list_keys:
                if isinstance(raw, list) and len(raw) > 0:
                    self.data[k] = raw
                else:
                    self.data[k] = []
            else:
                if isinstance(raw, list) and len(raw) > 0:
                    self.data[k] = raw[0]
                elif isinstance(raw, list) and len(raw) == 0:
                    self.data[k] = {}
                elif raw is None:
                    self.data[k] = {}
        
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

            # === 1. 基础数据收集 (恢复原代码字段) ===
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

            # === 3. 维度收集 ===
            
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

            # (B) Meme / 信仰值分析
            meme_score = 0
            vol_today = self.extract(q, "volume", "Volume", required=False)
            vol_avg = self.extract(q, "avgVolume", "Avg Volume", required=False)
            
            if price and price_200ma:
                if price > price_200ma: self.signals.add("UPTREND")
                else: self.signals.add("DOWNTREND")
                if price > price_200ma * 1.4: meme_score += 2
                elif price > price_200ma * 1.15: meme_score += 1

            if (ps_ratio and ps_ratio > 20) or (ev_ebitda and ev_ebitda > 80): meme_score += 4
            elif (ps_ratio and ps_ratio > 10) or (ev_ebitda and ev_ebitda > 40): meme_score += 2
            if beta > 2.0: meme_score += 2
            elif beta > 1.3: meme_score += 1
            
            meme_score = max(0, min(10, meme_score))
            meme_pct = int(meme_score * 10)
            if meme_pct >= 80: 
                self.signals.add("MEME_EXTREME")
                self.logs.insert(0, f"[信仰] Meme值 {meme_pct}%，资金情绪极度狂热。")

            # (C) PEG & Growth
            forward_peg = None
            fwd_growth = None
            if estimates and len(estimates) > 0 and price:
                try:
                    estimates.sort(key=lambda x: x.get("date", "0000-00-00"))
                    future_estimates = [e for e in estimates if e.get("date", "") > datetime.now().strftime("%Y-%m-%d")]
                    if len(future_estimates) >= 2:
                        fy1 = future_estimates[0]; fy2 = future_estimates[1] 
                        eps_fy1 = fy1.get("epsAvg"); eps_fy2 = fy2.get("epsAvg")
                        if eps_fy1 and eps_fy1 > 0 and eps_fy2:
                            fwd_pe = price / eps_fy1
                            fwd_growth = (eps_fy2 - eps_fy1) / eps_fy1
                            if fwd_growth > 0: forward_peg = fwd_pe / (fwd_growth * 100)
                except Exception: pass
            
            peg_used = forward_peg if forward_peg is not None else peg_ttm
            
            growth_list = [x for x in [rev_growth, ni_growth, fwd_growth] if x is not None]
            max_growth = max(growth_list) if growth_list else 0
            
            growth_desc = "低成长"
            if max_growth > 0.5: growth_desc = "超高速"; self.signals.add("GROWTH_HYPER")
            elif max_growth > 0.2: growth_desc = "高速"; self.signals.add("GROWTH_HIGH")
            elif max_growth > 0.05: growth_desc = "稳健"; self.signals.add("GROWTH_STABLE")
            else: self.signals.add("GROWTH_LOW")

            if peg_used is not None:
                peg_display = format_num(peg_used)
                peg_desc = "合理"
                if peg_used < 0.8: self.signals.add("PEG_UNDERVALUED"); peg_desc = "低估"
                elif peg_used > 3.0: self.signals.add("PEG_EXPENSIVE"); peg_desc = "泡沫"
                self.logs.append(f"[成长锚点] PEG: {peg_display} ({peg_desc})。")

            # (E) 估值水平
            sector_avg = get_sector_benchmark(sector)
            if ps_ratio is not None:
                 if ps_ratio > 20.0: self.signals.add("PS_EXTREME"); self.logs.append(f"[估值] PS {format_num(ps_ratio)} 极高。")
                 if ps_ratio < 2.0: self.signals.add("PS_LOW")

            if is_profitable_strict and ev_ebitda is not None:
                ratio = ev_ebitda / sector_avg
                adj_ratio = ratio / macro_discount_factor if macro_discount_factor != 0 else ratio
                if adj_ratio < 0.7: 
                    self.signals.add("VALUATION_CHEAP")
                    self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 低于均值，折扣明显。")
                elif adj_ratio > 1.3: 
                    self.signals.add("VALUATION_EXPENSIVE")
                    self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 高于均值。")
                else: 
                    self.signals.add("VALUATION_FAIR")

            # (F) 质量与效率
            adj_fcf_yield = None
            if len(cf_list) >= 4 and m_cap and m_cap > 0:
                ttm_cfo = sum(self.extract(q, "netCashProvidedByOperatingActivities", "", default=0, required=False) for q in cf_list)
                ttm_da = sum(self.extract(q, "depreciationAndAmortization", "", default=0, required=False) for q in cf_list)
                if ttm_cfo != 0:
                    adj_fcf = ttm_cfo - (ttm_da * 0.5) 
                    adj_fcf_yield = adj_fcf / m_cap
                    self.fcf_yield_display = format_percent(adj_fcf_yield)

            fcf_used = adj_fcf_yield if adj_fcf_yield is not None else fcf_yield_api
            
            if roic and roic > 0.20: 
                self.signals.add("QUALITY_TOP_TIER") 
                self.logs.append(f"[护城河] ROIC ({format_percent(roic)}) 极高。")
            
            if fcf_used is not None:
                if fcf_used > 0.035: self.signals.add("CASHFLOW_RICH"); self.logs.append(f"[造血] 现金流强劲。")
                elif fcf_used < -0.01: self.signals.add("CASHFLOW_NEGATIVE")

            # (G) 业绩 Alpha 【完全恢复原代码逻辑】
            valid_earnings = []
            today_str = datetime.now().strftime("%Y-%m-%d")
            if isinstance(earnings_raw, list):
                sorted_earnings = sorted(earnings_raw, key=lambda x: x.get("date", "0000-00-00"), reverse=True)
                recent_earnings = sorted_earnings[:12]
                for e in recent_earnings:
                    date = e.get("date")
                    if date and date <= today_str:
                        # 【关键】使用 self.extract 保护空值
                        rev = self.extract(e, "revenueActual", "Revenue", default=e.get("revenue"))
                        eps = self.extract(e, "epsActual", "EPS")
                        est = self.extract(e, "epsEstimated", "EPS Est")
                        # 【关键】只有当 rev 和 eps 都不为空时才加入有效列表
                        if rev is not None and eps is not None:
                            valid_earnings.append({"date": date, "rev": rev, "eps": eps, "est": est})
            
            trend_data = sorted(valid_earnings, key=lambda x: x["date"])
            recent_4 = trend_data[-4:] 
            earns_str = ""
            if len(recent_4) > 0:
                # 【安全】这里已经经过 None 过滤，可以直接比较
                beats = sum(1 for x in recent_4 if x["est"] is not None and x["eps"] > x["est"])
                earns_str = f"过去4季 {beats} 次超预期"
                self.logs.append(f"[Alpha] {earns_str}。")
                
                # Turnaround Check
                if len(recent_4) >= 3:
                    epss = [x["eps"] for x in recent_4]
                    if all(e < 0 for e in epss[:-1]) and epss[-1] > 0:
                        self.signals.add("TURNAROUND_PROFIT")
                        self.logs.append(f"[反转信号] 本季扭亏为盈。")

            # 4. 构造 Context For AI
            self.context_for_ai = f"""
            [基础] 价格:{price}, 市值:{format_market_cap(m_cap)}, Beta:{beta}, 行业:{sector}
            [估值] PE:{format_num(pe_ttm)}, PEG:{format_num(peg_used)}, PS:{format_num(ps_ratio)}, EV/EBITDA:{format_num(ev_ebitda)}
            [效率] ROIC:{format_percent(roic)}, 净利率:{format_percent(net_margin)}, FCF Yield:{format_percent(fcf_used)}
            [成长] 营收增长:{format_percent(rev_growth)}, 净利增长:{format_percent(ni_growth)}, 预期增长:{format_percent(fwd_growth)}
            [趋势] 现价 vs 200均线: {"高于" if price and price_200ma and price>price_200ma else "低于"}
            [风险] 月度VaR:{self.risk_var}, 宏观美债:{yield_10y}%
            [已识别因子] {', '.join(list(self.signals))}
            [近期业绩] {earns_str}
            """

            self.short_term_verdict = "高估" if "VALUATION_EXPENSIVE" in self.signals else "合理"
            self.long_term_verdict = "优质" if "QUALITY_TOP_TIER" in self.signals else "中性"

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

    # DeepSeek 分析
    strategy_text = await ask_deepseek_strategy(interaction.client.session, ticker.upper(), model.context_for_ai)

    profit_label = "盈利" if data.get('is_profitable', False) else "亏损"

    embed = discord.Embed(
        title=f"估值分析: {ticker.upper()}",
        description=f"现价: ${data['price']:.2f} | 市值: {format_market_cap(data['m_cap'])} | {profit_label}",
        color=0x2b2d31
    )

    embed.add_field(name="💡 投资策略 (AI多头视角)", value=f"```\n{strategy_text}\n```", inline=False)

    beta_val = data['beta']
    beta_desc = "低波动" if beta_val < 0.8 else ("高波动" if beta_val > 1.3 else "适中")
    meme_pct = data['meme_pct']
    meme_desc = "低关注度"
    if meme_pct >= 80: meme_desc = "资金狂热"
    elif meme_pct >= 60: meme_desc = "高流动性"
    
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
    if len(factor_str) > 1000: factor_str = factor_str[:990] + "..."

    embed.add_field(name="因子分析 (证据)", value=factor_str, inline=False)
    embed.set_footer(text="(AI辅助分析，不构成投资建议)")

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
