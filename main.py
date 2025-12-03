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

# *** 接口地址 ***
FMP_BASE_URL = "https://financialmodelingprep.com/stable"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"

# --- 全局状态 ---
PRIVACY_MODE = {}

# --- 白名单与关键词 ---
HARD_TECH_TICKERS = ["RKLB", "LUNR", "ASTS", "SPCE", "PLTR", "IONQ", "RGTI", "DNA", "JOBY", "ACHR", "BABA", "NIO", "XPEV", "LI", "TSLA", "NVDA", "AMD", "MSFT", "GOOG", "GOOGL", "AMZN"]
BLUE_OCEAN_KEYWORDS = ["aerospace", "defense", "space", "satellite", "rocket", "quantum"]
HARD_TECH_KEYWORDS = ["semiconductor", "artificial intelligence", "software", "auto", "biotech", "internet"]

# --- 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("ValuationBot")

# --- 1. 异步工具函数 ---

async def get_json_safely(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, timeout=10) as response:
            if response.status != 200: return None
            try:
                data = await response.json()
                if isinstance(data, dict) and "Error Message" in data: return None
                return data
            except: return None
    except: return None

async def get_fmp_data(session: aiohttp.ClientSession, endpoint: str, ticker: str, params: str = ""):
    url = f"{FMP_BASE_URL}/{endpoint}?symbol={ticker}&apikey={FMP_API_KEY}"
    if params: url += f"&{params}"
    return await get_json_safely(session, url)

async def get_treasury_rates(session: aiohttp.ClientSession):
    today = datetime.now()
    url = f"{FMP_BASE_URL}/treasury-rates?from={(today-timedelta(7)).strftime('%Y-%m-%d')}&to={today.strftime('%Y-%m-%d')}&apikey={FMP_API_KEY}"
    data = await get_json_safely(session, url)
    return data[0] if data and isinstance(data, list) else None

async def get_company_profile(session: aiohttp.ClientSession, ticker: str):
    data = await get_json_safely(session, f"{FMP_BASE_URL}/profile?symbol={ticker}&apikey={FMP_API_KEY}")
    if data and isinstance(data, list): return data[0]
    data_scr = await get_json_safely(session, f"{FMP_BASE_URL}/stock-screener?symbol={ticker}&apikey={FMP_API_KEY}")
    if data_scr and isinstance(data_scr, list): return data_scr[0]
    return None

async def get_earnings_data(session: aiohttp.ClientSession, ticker: str):
    return await get_json_safely(session, f"{FMP_BASE_URL}/earnings?symbol={ticker}&apikey={FMP_API_KEY}") or []

async def get_estimates_data(session: aiohttp.ClientSession, ticker: str):
    return await get_json_safely(session, f"{FMP_BASE_URL}/analyst-estimates?symbol={ticker}&period=annual&limit=5&apikey={FMP_API_KEY}") or []

# --- 2. DeepSeek 分析引擎 (核心修改) ---
async def ask_deepseek_strategy(session: aiohttp.ClientSession, ticker: str, context_str: str):
    if not DEEPSEEK_API_KEY: return "未配置 DeepSeek Key，无法生成策略。"
    
    # 核心 Persona 和 要求设定
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
def format_percent(num): return f"{num*100:.2f}%" if num is not None else "N/A"
def format_num(num): return f"{num:.2f}" if num is not None else "N/A"
def format_cap(num):
    if not num: return "N/A"
    return f"${num/1e12:.2f}T" if num >= 1e12 else (f"${num/1e9:.2f}B" if num >= 1e9 else f"${num/1e6:.2f}M")
def get_sector_avg(sector):
    bench = {"Technology":32,"Consumer Electronics":25,"Communication":20,"Healthcare":18,"Financial":12,"Energy":10}
    for k,v in bench.items(): 
        if k in str(sector): return v
    return 18.0

# --- 3. 核心模型 (保留逻辑计算) ---
class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        self.logs = []
        self.signals = set()
        self.risk_var = "N/A"
        self.context_for_ai = "" # 专门喂给AI的文本

    def extract(self, src, key, default=None): return src.get(key, default)

    async def fetch_data(self, session):
        logger.info(f"Fetching {self.ticker}...")
        t_prof = get_company_profile(session, self.ticker)
        t_tr = get_treasury_rates(session)
        reqs = {
            "quote": get_fmp_data(session, "quote", self.ticker),
            "metrics": get_fmp_data(session, "key-metrics-ttm", self.ticker),
            "ratios": get_fmp_data(session, "ratios-ttm", self.ticker),
            "growth": get_fmp_data(session, "financial-growth", self.ticker, "period=annual&limit=1"),
            "cf": get_fmp_data(session, "cash-flow-statement", self.ticker, "period=quarter&limit=4"),
            "vix": get_fmp_data(session, "quote", "^VIX"),
            "earnings": get_earnings_data(session, self.ticker),
            "estimates": get_estimates_data(session, self.ticker)
        }
        res = await asyncio.gather(t_prof, t_tr, *reqs.values())
        self.data["profile"], self.data["treasury"] = res[0], res[1]
        for i, k in enumerate(reqs.keys()):
            val = res[i+2]
            self.data[k] = val[0] if isinstance(val, list) and val and k not in ["earnings", "estimates", "cf"] else (val if val else {})
        return self.data["profile"] is not None

    def analyze(self):
        d = self.data
        p, q, m, r, g = d.get("profile",{}), d.get("quote",{}), d.get("metrics",{}), d.get("ratios",{}), d.get("growth",{})
        
        # 1. 基础提取
        price = q.get("price", 0)
        m_cap = q.get("marketCap", 0)
        beta = p.get("beta", 1.0)
        sector = p.get("sector", "Unknown")
        pe = r.get("priceToEarningsRatioTTM")
        peg = r.get("priceToEarningsGrowthRatioTTM")
        ps = r.get("priceToSalesRatioTTM")
        ev_ebitda = r.get("enterpriseValueMultipleTTM") or m.get("enterpriseValueOverEBITDATTM")
        roic = m.get("returnOnInvestedCapitalTTM")
        net_margin = r.get("netProfitMarginTTM")
        
        # 2. 详细因子分析逻辑 (保留你喜欢的Log)
        
        # [宏观]
        yield_10y = d["treasury"].get("year10", 4.0) if d["treasury"] else 4.0
        macro_factor = 1.0
        if yield_10y > 4.8:
            self.signals.add("MACRO_HEADWIND")
            self.logs.append(f"[宏观压制] 美债收益率 {yield_10y}%，压制估值。")
            macro_factor = 0.7
        elif yield_10y < 3.8:
            self.signals.add("MACRO_TAILWIND")
            self.logs.append(f"[宏观红利] 美债收益率 {yield_10y}%，利好估值。")

        # [属性]
        is_hard_tech = self.ticker in HARD_TECH_TICKERS or any(k in str(sector).lower() for k in HARD_TECH_KEYWORDS)
        is_blue_ocean = any(k in str(sector).lower() for k in BLUE_OCEAN_KEYWORDS)
        if is_hard_tech: self.signals.add("HARD_TECH")
        if is_blue_ocean: self.signals.add("BLUE_OCEAN")

        # [Meme/资金]
        price_200ma = q.get("priceAvg200")
        meme_score = 0
        if price and price_200ma:
            if price > price_200ma: self.signals.add("UPTREND")
            else: self.signals.add("DOWNTREND")
            if price > price_200ma * 1.4: meme_score += 3
        if ps and ps > 20: meme_score += 3
        if beta > 1.8: meme_score += 2
        meme_pct = min(99, meme_score * 10)
        if meme_pct > 80: 
            self.signals.add("MEME_EXTREME")
            self.logs.append(f"[信仰] Meme值 {meme_pct}%，资金情绪极度狂热。")

        # [估值 - PEG]
        fwd_pe, fwd_growth = None, None
        ests = d.get("estimates", [])
        if ests and len(ests)>=2:
            ests.sort(key=lambda x:x['date'])
            fut = [e for e in ests if e['date']>datetime.now().strftime('%Y-%m-%d')]
            if len(fut)>=2 and fut[0]['epsAvg']>0:
                fwd_pe = price / fut[0]['epsAvg']
                fwd_growth = (fut[1]['epsAvg'] - fut[0]['epsAvg']) / fut[0]['epsAvg']

        peg_used = (fwd_pe / (fwd_growth*100)) if fwd_pe and fwd_growth and fwd_growth>0 else peg
        peg_desc = "N/A"
        if peg_used:
            if peg_used < 0.8: 
                self.signals.add("PEG_UNDERVALUED")
                peg_desc = "低估"
            elif peg_used > 3.0: 
                self.signals.add("PEG_EXPENSIVE")
                peg_desc = "泡沫"
            else: peg_desc = "合理"
            self.logs.append(f"[成长锚点] PEG: {format_num(peg_used)} ({peg_desc})。")

        # [估值 - PS/EV]
        if ps:
            if ps > 15: 
                self.signals.add("PS_EXTREME")
                self.logs.append(f"[估值] PS {format_num(ps)} 处于极高水位。")
            elif ps < 2: 
                self.signals.add("PS_LOW")
                self.logs.append(f"[估值] PS {format_num(ps)} 处于历史低位。")

        sector_avg = get_sector_avg(sector)
        if ev_ebitda:
            ratio = ev_ebitda / sector_avg
            if ratio * macro_factor > 1.3:
                self.signals.add("VALUATION_EXPENSIVE")
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 显著高于行业均值 ({sector_avg})。")
            elif ratio * macro_factor < 0.7:
                self.signals.add("VALUATION_CHEAP")
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 显著低于行业均值，有折扣。")

        # [效率]
        if roic and roic > 0.2:
            self.signals.add("QUALITY_TOP_TIER")
            self.logs.append(f"[护城河] ROIC {format_percent(roic)} 极高，资本效率顶级。")
        
        # [现金流]
        cfs = d.get("cf", [])
        fcf_yield = m.get("freeCashFlowYieldTTM")
        if fcf_yield:
            if fcf_yield > 0.03: self.signals.add("CASHFLOW_RICH"); self.logs.append(f"[造血] FCF收益率 {format_percent(fcf_yield)}，现金流充沛。")
            elif fcf_yield < -0.01: self.signals.add("CASHFLOW_NEGATIVE"); self.logs.append(f"[失血] FCF收益率 {format_percent(fcf_yield)}，需关注烧钱速度。")

        # [业绩趋势]
        earns = d.get("earnings", [])
        earns_str = ""
        if earns:
            earns.sort(key=lambda x:x['date'], reverse=True)
            recent = earns[:4]
            beats = sum(1 for e in recent if e['epsEstimated'] and e['epsActual'] > e['epsEstimated'])
            earns_str = f"过去4季 {beats} 次超预期"
            self.logs.append(f"[Alpha] {earns_str}。")
            # 扭亏检查
            epss = [e['epsActual'] for e in recent]
            if len(epss)>=2 and epss[0]>0 and all(x<0 for x in epss[1:]):
                self.signals.add("TURNAROUND_PROFIT")
                self.logs.append("[反转] 本季首次扭亏为盈。")

        # [VaR]
        vix = d.get("vix", {}).get("price")
        if vix and beta:
            vol = beta * (vix/100) * math.sqrt(1/12) * 1.65
            self.risk_var = f"-{format_percent(vol)}"

        # 3. 构造喂给 AI 的数据包 (Context)
        self.context_for_ai = f"""
        [基础] 价格:{price}, 市值:{format_cap(m_cap)}, Beta:{beta}, 行业:{sector}
        [估值] PE:{format_num(pe)}, PEG:{format_num(peg_used)}, PS:{format_num(ps)}, EV/EBITDA:{format_num(ev_ebitda)}
        [效率] ROIC:{format_percent(roic)}, 净利率:{format_percent(net_margin)}, FCF Yield:{format_percent(fcf_yield)}
        [成长] 营收增长:{format_percent(g.get('revenueGrowth'))}, 净利增长:{format_percent(g.get('netIncomeGrowth'))}
        [趋势] 现价 vs 200均线: {"高于" if price>price_200ma else "低于" if price_200ma else "N/A"}
        [风险] 月度VaR:{self.risk_var}, 宏观美债:{yield_10y}%
        [已识别因子] {', '.join(list(self.signals))}
        [近期业绩] {earns_str}
        """

        return {
            "price": price, "m_cap": m_cap, "beta": beta, "meme_pct": meme_pct, "is_profit": (pe and pe>0)
        }

# --- Discord Bot ---
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

@bot.tree.command(name="analyze", description="AI 策略 + 因子分析")
@app_commands.describe(ticker="股票代码")
async def analyze(interaction: discord.Interaction, ticker: str):
    is_private = PRIVACY_MODE.get(interaction.user.id, False)
    await interaction.response.defer(thinking=True, ephemeral=is_private)
    
    model = ValuationModel(ticker)
    if not await model.fetch_data(interaction.client.session):
        await interaction.followup.send("❌ 数据获取失败。", ephemeral=is_private)
        return

    data = model.analyze()
    
    # AI 分析 (使用 Context)
    strategy_text = await ask_deepseek_strategy(interaction.client.session, ticker.upper(), model.context_for_ai)

    # 构造界面
    embed = discord.Embed(title=f"📊 深度分析: {ticker.upper()}", color=0x2b2d31)
    
    # 1. 核心数据
    info = f"**${data['price']:.2f}** | 市值 {format_cap(data['m_cap'])} | Beta {data['beta']}"
    embed.add_field(name="核心指标", value=info, inline=False)
    
    # 2. AI 策略 (置顶)
    embed.add_field(name="💡 投资策略 (AI)", value=f"```\n{strategy_text}\n```", inline=False)
    
    # 3. 因子分析 (详细日志)
    log_str = "\n".join([f"> {l}" for l in model.logs])
    if not log_str: log_str = "> 数据平淡，未触发显著因子。"
    if len(log_str) > 1000: log_str = log_str[:990] + "..."
    embed.add_field(name="因子分析 (证据)", value=log_str, inline=False)
    
    # 4. 风险
    if model.risk_var != "N/A":
        embed.set_footer(text=f"月度潜在回撤风险 (95% VaR): {model.risk_var} | 仅供参考")

    await interaction.followup.send(embed=embed, ephemeral=is_private)

@bot.tree.command(name="privacy", description="切换隐私模式")
async def privacy(interaction: discord.Interaction):
    uid = interaction.user.id
    PRIVACY_MODE[uid] = not PRIVACY_MODE.get(uid, False)
    await interaction.response.send_message(f"隐私模式: {PRIVACY_MODE[uid]}", ephemeral=True)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
