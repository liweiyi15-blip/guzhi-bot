import discord
from discord import app_commands
from discord.ext import commands
import requests
import os
import asyncio
import logging
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
FMP_API_KEY = os.getenv('FMP_API_KEY')

BASE_URL = "https://financialmodelingprep.com/stable"

# --- 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("ValuationBot")

# --- 1. 数据工具函数 ---

def get_fmp_data(endpoint, ticker, params=""):
    # 隐藏 Key 用于日志
    url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey={FMP_API_KEY}&{params}"
    safe_url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey=***&{params}"
    
    try:
        logger.info(f"📡 Requesting: {safe_url}")
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
             logger.error(f"❌ API Failed: {response.status_code} for {endpoint}")
             return None
        
        data = response.json()
        
        if isinstance(data, list):
            if len(data) > 0:
                return data[0]
            else:
                return None
        return data
    except Exception as e:
        logger.error(f"❌ Exception fetching {endpoint}: {e}")
        return None

def format_percent(num):
    if num is None: return "N/A"
    return f"{num * 100:.2f}%"

def format_num(num):
    if num is None: return "N/A"
    return f"{num:.2f}"

def format_market_cap(num):
    if num is None: return "N/A"
    if num >= 1e12: return f"${num/1e12:.2f}T (万亿)"
    if num >= 1e9: return f"${num/1e9:.2f}B (十亿)"
    return f"${num/1e6:.2f}M (百万)"

# --- 2. 行业基准数据 (Sector Benchmarks) ---
SECTOR_EBITDA_MEDIAN = {
    "Technology": 32.0,
    "Consumer Electronics": 25.0,
    "Communication Services": 20.0,
    "Healthcare": 18.0,
    "Financial Services": 12.0,
    "Energy": 10.0,
    "Utilities": 12.0,
    "Unknown": 18.0
}

def get_sector_benchmark(sector):
    for key, val in SECTOR_EBITDA_MEDIAN.items():
        if key in sector: return val
    return 18.0

# --- 3. 估值判断模型 (Valuation Model) ---

class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        self.market_regime = "未知"
        
        self.logs = []
        self.flags = [] 

    async def fetch_data(self):
        logger.info(f"--- Starting Analysis for {self.ticker} ---")
        loop = asyncio.get_event_loop()
        tasks = {
            "profile": loop.run_in_executor(None, get_fmp_data, "profile", self.ticker, ""),
            "quote": loop.run_in_executor(None, get_fmp_data, "quote", self.ticker, ""),
            "metrics": loop.run_in_executor(None, get_fmp_data, "key-metrics-ttm", self.ticker, ""),
            "ratios": loop.run_in_executor(None, get_fmp_data, "ratios-ttm", self.ticker, ""),
            "bs": loop.run_in_executor(None, get_fmp_data, "balance-sheet-statement", self.ticker, "limit=1"),
            "vix": loop.run_in_executor(None, get_fmp_data, "quote", "^VIX", "")
        }
        results = await asyncio.gather(*tasks.values())
        self.data = dict(zip(tasks.keys(), results))
        
        return self.data["profile"] is not None and self.data["quote"] is not None

    def analyze(self):
        p = self.data.get("profile", {}) or {}
        q = self.data.get("quote", {}) or {}
        m = self.data.get("metrics", {}) or {}
        bs = self.data.get("bs", {}) or {}
        vix_data = self.data.get("vix", {}) or {}
        
        if not p or not q: return None

        price = q.get("price")
        sector = p.get("sector", "Unknown")
        beta = p.get("beta", 1.0)
        m_cap = p.get("mktCap", 0) # 获取市值
        
        # --- 0. 市场情绪 (VIX Regime) ---
        vix = vix_data.get("price", 20)
        if vix < 20: self.market_regime = f"🟢 风平浪静 (VIX {vix:.1f})"
        elif vix < 30: self.market_regime = f"🟡 市场震荡 (VIX {vix:.1f})"
        else: self.market_regime = f"🔴 恐慌模式 (VIX {vix:.1f})"

        # --- 1. 短期估值 (相对行业) ---
        ev_ebitda = m.get("enterpriseValueOverEBITDATTM")
        sector_avg = get_sector_benchmark(sector)
        
        st_status = "中性"
        if ev_ebitda:
            ratio = ev_ebitda / sector_avg
            if ratio < 0.7:
                st_status = "🟢 显著低估 (Cheap)"
                self.logs.append(f"⚡ 短期: EV/EBITDA {format_num(ev_ebitda)} vs 行业 {sector_avg} (折价 {(1-ratio)*100:.0f}%)")
            elif ratio > 1.3:
                st_status = "🔴 显著高估 (Expensive)"
                self.logs.append(f"⚡ 短期: EV/EBITDA {format_num(ev_ebitda)} vs 行业 {sector_avg} (溢价 {(ratio-1)*100:.0f}%)")
            else:
                st_status = "🟡 中性 (Fair)"
                self.logs.append(f"⚡ 短期: 估值与行业同步 ({format_num(ev_ebitda)}x)")
        
        self.short_term_verdict = st_status

        # --- 2. 长期估值 (FCF + Moat) ---
        fcf_yield = m.get("freeCashFlowYieldTTM")
        roic = m.get("returnOnInvestedCapitalTTM")
        
        # 债务审计
        net_debt = m.get("netDebt")
        total_assets = bs.get("totalAssets")
        debt_risk = False
        if net_debt and total_assets and net_debt > total_assets * 0.6:
            debt_risk = True

        lt_status = "中性"
        if fcf_yield:
            if fcf_yield > 0.04:
                if debt_risk:
                    lt_status = "🔴 价值陷阱"
                    self.flags.append(f"⚠️ **高负债风险**: FCF Yield 高但负债重")
                else:
                    lt_status = "🟢 便宜 / 值得持有"
                    self.logs.append(f"⏳ 长期: FCF Yield {format_percent(fcf_yield)} 回报丰厚")
            elif fcf_yield > 0.02:
                lt_status = "🟡 合理"
                self.logs.append(f"⏳ 长期: FCF Yield {format_percent(fcf_yield)} 支撑一般")
            else:
                lt_status = "🔴 贵 / 透支未来"
                self.logs.append(f"⏳ 长期: FCF Yield {format_percent(fcf_yield)} 极低")
            
            # 护城河
            if roic and roic > 0.15:
                self.logs.append(f"🏰 **深护城河**: ROIC {format_percent(roic)} (高效率)")
                if lt_status == "🟡 合理": lt_status = "🟢 优质合理"

        self.long_term_verdict = lt_status

        # 这里不再设置 self.color，颜色将由 Embed 统一指定

        return {
            "price": price,
            "beta": beta,
            "sector": sector,
            "m_cap": m_cap,
            "ev_ebitda": ev_ebitda,
            "fcf_yield": fcf_yield,
            "roic": roic
        }

# --- 4. Bot Setup ---

class AnalysisBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        logger.info("Syncing commands...")
        await self.tree.sync()
        logger.info("Commands synced.")

bot = AnalysisBot()

@bot.tree.command(name="analyze", description="[v1.6] 美股估值深度透视")
@app_commands.describe(ticker="股票代码 (e.g. NVDA)")
async def analyze(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    
    model = ValuationModel(ticker)
    success = await model.fetch_data()
    
    if not success:
        await interaction.followup.send(f"❌ 数据获取失败: `{ticker.upper()}`", ephemeral=True)
        return

    data = model.analyze()
    if not data:
        await interaction.followup.send(f"⚠️ 数据不足。", ephemeral=True)
        return

    # 统一使用 Discord 蓝 (Professional Blue)
    embed = discord.Embed(
        title=f"📊 深度透视: {ticker.upper()}",
        description=f"当前市场情绪: **{model.market_regime}**",
        color=0x3498db # 固定蓝色
    )

    # 1. 估值仪表盘
    verdict_text = (
        f"⚡ **短期 (vs 行业):** {model.short_term_verdict}\n"
        f"⏳ **长期 (vs 回报):** {model.long_term_verdict}"
    )
    embed.add_field(name="🎯 估值判定", value=verdict_text, inline=False)

    # 2. 基础数据 (新增市值)
    base_info = (
        f"**价格:** ${data['price']}\n"
        f"**市值:** {format_market_cap(data['m_cap'])}\n" # 显示市值
        f"**板块:** {data['sector']}"
    )
    embed.add_field(name="📋 基础信息", value=base_info, inline=True)

    # 3. 核心因子
    metric_text = f"**EV/EBITDA:** {format_num(data['ev_ebitda'])}\n"
    metric_text += f"**FCF Yield:** {format_percent(data['fcf_yield'])}\n"
    if data['roic'] and data['roic'] > 0.15:
        metric_text += f"**ROIC:** {format_percent(data['roic'])} (🏰 Moat)"
    else:
        metric_text += f"**ROIC:** {format_percent(data['roic'])}"
        
    embed.add_field(name="🔑 核心因子", value=metric_text, inline=True)

    # 4. 风险因子
    beta_val = data['beta']
    beta_desc = "🛡️ 低波" if beta_val < 0.8 else ("🔥 高波" if beta_val > 1.3 else "⚖️ 适中")
    embed.add_field(name="🌊 Beta", value=f"{format_num(beta_val)} ({beta_desc})", inline=True)

    # 5. 逻辑日志
    log_str = "\n".join(model.logs)
    if model.flags:
        log_str += "\n" + "\n".join(model.flags)
    embed.add_field(name="🧠 模型思考", value=f"```diff\n{log_str}\n```", inline=False)

    # 6. Beta 脚注
    beta_footnote = "Beta > 1.3 为进攻型 (高波)；Beta < 0.8 为防御型 (低波)。"
    embed.add_field(name="ℹ️ Note", value=beta_footnote, inline=False)

    embed.set_footer(text="Model: Sector Relative + Market Regime | Data: FMP Stable")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set.")
    else:
        bot.run(DISCORD_TOKEN)
