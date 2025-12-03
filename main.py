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

# --- 2. DeepSeek 分析引擎 (Prompt修正) ---
async def ask_deepseek_strategy(session: aiohttp.ClientSession, ticker: str, context_str: str):
    if not DEEPSEEK_API_KEY: return "未配置 DeepSeek Key，无法生成策略。"
    
    # 修改：要求定性分析，但不要给操作建议
    system_prompt = (
        "你是一位拥有十年华尔街实战经验的机构分析师。请基于提供的数据，对该标的进行深度的定性策略分析。\n"
        "【严格执行以下要求】：\n"
        "1. **严禁出现数字**：用“估值极高”、“资金分歧”等描述代替具体数据。\n"
        "2. **通俗且专业**：用大白话讲透上涨/下跌背后的逻辑（是基本面驱动还是情绪博弈？）。\n"
        "3. **字数限制**：80字以内！\n"
        "4. **不要给出操作建议**：严禁出现“建议买入”、“止损”、“低吸”等具体交易指令。只陈述事实和逻辑判断（例如：“当前价格完全由情绪主导，基本面已失效”）。"
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

# --- 3. 核心模型 ---
class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        self.logs = []
        self.signals = set()
        self.risk_var = "N/A"
        # 恢复 结论字段
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        self.context_for_ai = ""

    def extract(self, src, key, desc, default=None, required=True):
        val = src.get(key)
        if val is None:
            return default if default is not None else None
        return val

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
        price = self.extract(q, "price", "Price", default=0)
        m_cap = self.extract(q, "marketCap", "MCap", default=0)
        beta = self.extract(p, "beta", "Beta", default=1.0)
        sector = self.extract(p, "sector", "Sector", default="Unknown")
        industry = self.extract(p, "industry", "Industry", default="Unknown")
        
        pe = r.get("priceToEarningsRatioTTM")
        peg = r.get("priceToEarningsGrowthRatioTTM")
        ps = r.get("priceToSalesRatioTTM")
        ev_ebitda = r.get("enterpriseValueMultipleTTM") or m.get("enterpriseValueOverEBITDATTM")
        roic = m.get("returnOnInvestedCapitalTTM")
        net_margin = r.get("netProfitMarginTTM")
        
        # 盈利状态
        is_profitable = (pe is not None and pe > 0)
        
        # 2. 因子分析
        
        # [宏观]
        yield_10y = d["treasury"].get("year10", 4.0) if d["treasury"] else 4.0
        macro_factor = 1.0
        if yield_10y > 4.8:
            self.signals.add("MACRO_HEADWIND")
            self.logs.append(f"[宏观压制] 美债收益率 {yield_10y}%，估值模型承压。")
            macro_factor = 0.7
        elif yield_10y < 3.8:
            self.signals.add("MACRO_TAILWIND")
            self.logs.append(f"[宏观红利] 美债收益率 {yield_10y}%，有利于估值扩张。")

        # [属性]
        is_hard_tech = self.ticker in HARD_TECH_TICKERS or any(k in str(sector).lower() for k in HARD_TECH_KEYWORDS)
        is_blue_ocean = any(k in str(sector).lower() for k in BLUE_OCEAN_KEYWORDS)
        if is_hard_tech: self.signals.add("HARD_TECH")
        if is_blue_ocean: self.signals.add("BLUE_OCEAN")

        # [Meme]
        price_200ma = q.get("priceAvg200")
        meme_score = 0
        if price and price_200ma:
            if price > price_200ma * 1.4: meme_score += 3
        if ps and ps > 20: meme_score += 3
        if beta > 1.8: meme_score += 2
        meme_pct = min(99, meme_score * 10)
        if meme_pct > 80: 
            self.signals.add("MEME_EXTREME")
            self.logs.insert(0, f"[信仰] Meme值 {meme_pct}%。市场情绪已进入非理性繁荣区间，价格体现出极致的资金动能。")
        elif meme_pct > 60:
             self.logs.insert(0, f"[信仰] Meme值 {meme_pct}%。市场情绪高度活跃。")

        # [PEG]
        fwd_pe, fwd_growth = None, None
        ests = d.get("estimates", [])
        if ests and len(ests)>=2:
            ests.sort(key=lambda x:x['date'])
            fut = [e for e in ests if e['date']>datetime.now().strftime('%Y-%m-%d')]
            if len(fut)>=2 and fut[0]['epsAvg']>0:
                fwd_pe = price / fut[0]['epsAvg']
                fwd_growth = (fut[1]['epsAvg'] - fut[0]['epsAvg']) / fut[0]['epsAvg']
        peg_used = (fwd_pe / (fwd_growth*100)) if fwd_pe and fwd_growth and fwd_growth>0 else peg

        if peg_used is not None:
            peg_desc = "合理"
            if peg_used < 0.8: self.signals.add("PEG_UNDERVALUED"); peg_desc = "低估"
            elif peg_used > 3.0: self.signals.add("PEG_EXPENSIVE"); peg_desc = "泡沫化风险"
            self.logs.append(f"[成长锚点] PEG (Forward): {format_num(peg_used)} ({peg_desc})。估值已脱离基本面引力，风险较高。" if peg_used > 3.0 else f"[成长锚点] PEG: {format_num(peg_used)} ({peg_desc})。")

        # [硬科技/PS]
        if ps and ps > 10 and is_hard_tech:
            self.logs.append(f"[硬科技] P/S 估值: {format_num(ps)} (极高，价格已透支未来多年的增长)。")

        # [EV/EBITDA]
        sector_avg = get_sector_benchmark(sector)
        if ev_ebitda:
            ratio = ev_ebitda / sector_avg
            if ratio > 1.3:
                self.signals.add("VALUATION_EXPENSIVE")
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 远高于行业均值 ({sector_avg})，且缺乏增长支撑。")
            elif ratio < 0.7:
                self.signals.add("VALUATION_CHEAP")
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 低于均值。")

        # [价值修正/现金流]
        cfs = d.get("cf", [])
        fcf_yield_api = m.get("freeCashFlowYieldTTM")
        adj_fcf_yield = None
        if len(cfs) >= 4 and m_cap > 0:
            ttm_cfo = sum(self.extract(x, "netCashProvidedByOperatingActivities", "", 0, False) for x in cfs)
            ttm_da = sum(self.extract(x, "depreciationAndAmortization", "", 0, False) for x in cfs)
            adj_fcf_yield = (ttm_cfo - ttm_da*0.5) / m_cap
        
        if adj_fcf_yield and fcf_yield_api and adj_fcf_yield > fcf_yield_api + 0.004:
             self.logs.append(f"[价值修正] Adj FCF Yield ({format_percent(adj_fcf_yield)}) 高于 原始 FCF ({format_percent(fcf_yield_api)})，反映出增长性资本支出的积极影响。")

        # [Alpha]
        earns = d.get("earnings", [])
        earns_str = ""
        if earns:
            earns.sort(key=lambda x:x.get('date', '0000-00-00'), reverse=True)
            recent = earns[:4]
            beats = sum(1 for e in recent if e.get('epsEstimated') is not None and e.get('epsActual') is not None and e['epsActual'] > e['epsEstimated'])
            if beats < 2:
                 self.logs.append(f"[Alpha] 过去 4 季度中有 {4-beats} 次业绩不及预期，需警惕。")
            else:
                 self.logs.append(f"[Alpha] 过去 4 季度中有 {beats} 次业绩超预期。")

        # [VaR]
        vix = d.get("vix", {}).get("price")
        if vix and beta:
            vol = beta * (vix/100) * math.sqrt(1/12) * 1.65
            self.risk_var = f"-{format_percent(vol)}"

        # 3. 结论生成
        self.short_term_verdict = "合理溢价" if "MEME_EXTREME" in self.signals else ("高估" if "VALUATION_EXPENSIVE" in self.signals else "中性")
        self.long_term_verdict = "中性"
        if "QUALITY_TOP_TIER" in self.signals: self.long_term_verdict = "优质"
        if "HARD_TECH" in self.signals and "PEG_UNDERVALUED" in self.signals: self.long_term_verdict = "低估"

        # 4. Context for AI
        self.context_for_ai = f"""
        [基础] 价格:{price}, 市值:{format_market_cap(m_cap)}, Beta:{beta}, 行业:{sector}
        [估值] PE:{format_num(pe)}, PEG:{format_num(peg_used)}, PS:{format_num(ps)}, EV/EBITDA:{format_num(ev_ebitda)}
        [效率] ROIC:{format_percent(roic)}, 净利率:{format_percent(net_margin)}
        [趋势] 现价 vs 200均线: {"高于" if price and price_200ma and price>price_200ma else "低于"}
        [风险] 月度VaR:{self.risk_var}, 宏观美债:{yield_10y}%
        [已识别因子] {', '.join(list(self.signals))}
        """

        return {
            "price": price, "m_cap": m_cap, "beta": beta, "meme_pct": meme_pct, "is_profit": is_profitable
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
    if not data:
        await interaction.followup.send(f"[Warning] 数据不足。", ephemeral=ephemeral_result)
        return

    # DeepSeek 分析
    strategy_text = await ask_deepseek_strategy(interaction.client.session, ticker.upper(), model.context_for_ai)

    profit_label = "盈利" if data.get('is_profit', False) else "亏损"

    embed = discord.Embed(
        title=f"估值分析: {ticker.upper()}",
        description=f"现价: ${data['price']:.2f} | 市值: {format_market_cap(data['m_cap'])} | {profit_label}",
        color=0x2b2d31
    )

    # 1. 估值结论 (还原)
    verdict_str = (
        f"**短期:** {model.short_term_verdict}\n"
        f"**长期:** {model.long_term_verdict}"
    )
    embed.add_field(name="估值结论", value=verdict_str, inline=False)

    # 2. 核心特征 (还原)
    beta_val = data['beta']
    beta_desc = "高波动" if beta_val > 1.3 else "低波动"
    meme_pct = data['meme_pct']
    meme_desc = "资金狂热" if meme_pct >= 80 else "正常"
    core_str = (
        f"**Beta:** {format_num(beta_val)} ({beta_desc})\n"
        f"**Meme值:** {meme_pct}% ({meme_desc})"
    )
    embed.add_field(name="核心特征", value=core_str, inline=False)

    # 3. 风险 (还原)
    if model.risk_var != "N/A":
        embed.add_field(
            name="95% VaR (月度风险)", 
            value=f"最大回撤可能在 **{model.risk_var}** 附近", 
            inline=False
        )

    # 4. 因子分析 (还原详细Log)
    formatted_logs = []
    for log in model.logs:
        if log.startswith("[") and "]" in log:
            tag_end = log.find("]") + 1
            tag = log[:tag_end]
            content = log[tag_end:]
            formatted_logs.append(f"> **{tag}**{content}")
        else:
            formatted_logs.append(f"> {log}")
    
    factor_str = "\n\n".join(formatted_logs) # 使用双换行增加间距，或者单换行
    if not factor_str: factor_str = "> 数据平淡，未触发显著因子。"
    
    embed.add_field(name="因子分析", value=factor_str, inline=False)

    # 5. 策略 (放到底部，无格式)
    embed.add_field(name="[策略]", value=strategy_text, inline=False)

    # Footer (还原)
    embed.set_footer(text="(模型建议，仅作参考，不构成投资建议)")

    await interaction.followup.send(embed=embed, ephemeral=is_private)

@bot.tree.command(name="privacy", description="切换隐私模式")
async def privacy(interaction: discord.Interaction):
    uid = interaction.user.id
    PRIVACY_MODE[uid] = not PRIVACY_MODE.get(uid, False)
    await interaction.response.send_message(f"隐私模式: {PRIVACY_MODE[uid]}", ephemeral=True)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
