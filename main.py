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
                logger.warning(f"⚠️ {endpoint}: Received empty list []")
                return None
        return data
    except Exception as e:
        logger.error(f"❌ Exception fetching {endpoint}: {e}")
        return None

def get_list_data(endpoint, ticker, limit=4):
    url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey={FMP_API_KEY}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        return response.json() if response.status_code == 200 else []
    except Exception as e:
        logger.error(f"❌ Exception fetching list {endpoint}: {e}")
        return []

def format_percent(num):
    if num is None: return "N/A"
    return f"{num * 100:.2f}%"

def format_num(num):
    if num is None: return "N/A"
    return f"{num:.2f}"

# --- 2. 估值判断模型 (Valuation Judgment) ---

class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        
        # 结果容器
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
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
            "cash_flow": loop.run_in_executor(None, get_fmp_data, "cash-flow-statement", self.ticker, "limit=1")
        }
        results = await asyncio.gather(*tasks.values())
        self.data = dict(zip(tasks.keys(), results))
        
        return self.data["profile"] is not None and self.data["quote"] is not None

    def analyze(self):
        p = self.data.get("profile", {}) or {}
        q = self.data.get("quote", {}) or {}
        m = self.data.get("metrics", {}) or {}
        r = self.data.get("ratios", {}) or {}
        
        if not p or not q: return None

        price = q.get("price")
        sector = p.get("sector", "Unknown")
        beta = p.get("beta", 1.0)
        
        # --- 核心指标提取 ---
        ev_ebitda = m.get("enterpriseValueOverEBITDATTM")
        fcf_yield = m.get("freeCashFlowYieldTTM")
        pe = r.get("priceEarningsRatioTTM")
        
        # ==========================================
        # 1. 短期估值判断 (基于 EV/EBITDA 和 PE)
        # ==========================================
        # 逻辑：跟行业平均比，现在买入的倍数是否过高
        st_limit = 25 if "Tech" in sector else 15
        
        st_status = "中性 (Fair)"
        if ev_ebitda:
            if ev_ebitda < st_limit:
                st_status = "🟢 便宜 (Cheap)"
                self.logs.append(f"✅ 短期: EV/EBITDA {format_num(ev_ebitda)} 低于行业水位 ({st_limit})")
            elif ev_ebitda > st_limit * 1.5:
                st_status = "🔴 贵 (Expensive)"
                self.logs.append(f"❌ 短期: EV/EBITDA {format_num(ev_ebitda)} 显著高估")
            else:
                self.logs.append(f"⚖️ 短期: 估值倍数合理")
        else:
            # 如果没有 EBITDA，用 PE 兜底
            if pe and pe > 50: st_status = "🔴 贵 (Expensive)"
            elif pe and pe < 15: st_status = "🟢 便宜 (Cheap)"
        
        self.short_term_verdict = st_status

        # ==========================================
        # 2. 长期估值判断 (基于 FCF Yield 和 护城河)
        # ==========================================
        # 逻辑：长期持有的真实回报率 (FCF Yield) 是否诱人
        lt_status = "中性 (Fair)"
        
        if fcf_yield:
            if fcf_yield > 0.04: # >4% 无风险收益之上
                lt_status = "🟢 便宜 / 高性价比"
                self.logs.append(f"✅ 长期: FCF Yield {format_percent(fcf_yield)} 回报率可观")
            elif fcf_yield > 0.02:
                lt_status = "⚖️ 合理"
                self.logs.append(f"⚖️ 长期: FCF Yield {format_percent(fcf_yield)} 支撑力一般")
            else:
                lt_status = "🔴 贵 / 透支未来"
                self.logs.append(f"❌ 长期: FCF Yield {format_percent(fcf_yield)} 极低，完全依赖高增长预期")
        
        self.long_term_verdict = lt_status

        return {
            "price": price,
            "beta": beta,
            "sector": sector,
            "ev_ebitda": ev_ebitda,
            "fcf_yield": fcf_yield
        }

# --- 3. Bot Setup ---

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

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")

@bot.tree.command(name="analyze", description="美股长短期估值判断 (Short/Long Term Valuation)")
@app_commands.describe(ticker="股票代码 (e.g. TSLA)")
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

    # 构建 Embed
    embed = discord.Embed(
        title=f"⚖️ 估值透视: {ticker.upper()}",
        color=0x3498db
    )

    # 1. 核心结论区 (长短期)
    verdict_text = (
        f"⚡ **短期估值:** {model.short_term_verdict}\n"
        f"⏳ **长期估值:** {model.long_term_verdict}"
    )
    embed.add_field(name="🎯 估值判定", value=verdict_text, inline=False)

    # 2. 核心指标数据
    metrics_text = (
        f"**现价:** ${data['price']}\n"
        f"**EV/EBITDA (短期锚点):** {format_num(data['ev_ebitda'])}\n"
        f"**FCF Yield (长期锚点):** {format_percent(data['fcf_yield'])}"
    )
    embed.add_field(name="📊 核心数据", value=metrics_text, inline=True)

    # 3. Beta 展示区
    beta_val = data['beta']
    beta_desc = "中等波动"
    if beta_val > 1.5: beta_desc = "🔥 高波动"
    elif beta_val < 0.8: beta_desc = "🛡️ 低波动"
    
    embed.add_field(name="🌊 Beta (波动率)", value=f"**{format_num(beta_val)}** ({beta_desc})", inline=True)

    # 4. 逻辑日志
    log_str = "\n".join(model.logs)
    embed.add_field(name="🧠 判定逻辑", value=f"```diff\n{log_str}\n```", inline=False)

    # 5. Beta 脚注 (用户指定需求)
    beta_footnote = (
        "ℹ️ **Beta 意味着什么？**\n"
        "Beta 衡量股票相对于大盘的波动性。\n"
        "• **Beta = 1.0**: 波动与大盘同步。\n"
        "• **Beta > 1.5**: 进攻型。大盘涨1%，它可能涨1.5%；大盘跌1%，它可能跌更多。\n"
        "• **Beta < 0.8**: 防御型。大盘暴跌时，它通常比较抗跌。"
    )
    embed.add_field(name="📚 知识库", value=beta_footnote, inline=False)

    embed.set_footer(text="Data: Financial Modeling Prep | 仅供参考，不构成投资建议")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(DISCORD_TOKEN)
