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
    # 日志脱敏
    safe_url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey=***&{params}"
    
    try:
        logger.info(f"📡 Requesting: {safe_url}")
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
             logger.error(f"❌ API Failed: {response.status_code} for {endpoint}")
             return None
        
        data = response.json()
        
        if isinstance(data, list) and "historical" not in endpoint and "surprises" not in endpoint:
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

# --- 2. 行业基准 (横向对比) ---
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

# --- 3. 估值判断模型 (v2.0 中文版) ---

class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        self.market_regime = "未知"
        
        self.logs = [] # 因子分析日志
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
            "vix": loop.run_in_executor(None, get_fmp_data, "quote", "^VIX", ""),
            # v2.0: 盈利惊喜
            "earnings": loop.run_in_executor(None, get_fmp_data, "earnings-surprises", self.ticker, "limit=4")
        }
        results = await asyncio.gather(*tasks.values())
        self.data = dict(zip(tasks.keys(), results))
        
        return self.data["profile"] is not None and self.data["quote"] is not None

    def analyze(self):
        p = self.data.get("profile", {}) or {}
        q = self.data.get("quote", {}) or {}
        m = self.data.get("metrics", {}) or {} 
        r = self.data.get("ratios", {}) or {}
        bs = self.data.get("bs", {}) or {}
        vix_data = self.data.get("vix", {}) or {}
        earnings = self.data.get("earnings", []) or []
        
        if not p or not q: return None

        price = q.get("price")
        sector = p.get("sector", "Unknown")
        beta = p.get("beta", 1.0)
        
        # 兼容性读取
        ev_ebitda = m.get("evToEBITDA") or m.get("enterpriseValueOverEBITDATTM") or r.get("enterpriseValueMultipleTTM")
        fcf_yield = m.get("freeCashFlowYield") or m.get("freeCashFlowYieldTTM")
        roic = m.get("returnOnInvestedCapital") or m.get("returnOnInvestedCapitalTTM")

        # --- 0. 市场情绪 ---
        vix = vix_data.get("price", 20)
        if vix < 20: self.market_regime = f"风平浪静 (VIX {vix:.1f})"
        elif vix < 30: self.market_regime = f"市场震荡 (VIX {vix:.1f})"
        else: self.market_regime = f"恐慌模式 (VIX {vix:.1f})"

        # --- 1. 短期估值 (行业对比) ---
        sector_avg = get_sector_benchmark(sector)
        st_status = "中性"
        
        if ev_ebitda:
            ratio = ev_ebitda / sector_avg
            if ratio < 0.7:
                st_status = "显著低估"
                self.logs.append(f"[板块] EV/EBITDA {format_num(ev_ebitda)} 低于行业均值 {sector_avg} 超过 30%。")
            elif ratio > 1.3:
                st_status = "显著高估"
                self.logs.append(f"[板块] EV/EBITDA {format_num(ev_ebitda)} 高于行业均值 {sector_avg} 超过 30%。")
            else:
                self.logs.append(f"[板块] 估值倍数与行业同步。")
        else:
             self.logs.append(f"[板块] 缺少 EV/EBITDA 数据。")
        
        self.short_term_verdict = st_status

        # --- 2. 长期估值 (FCF + 护城河 + 盈利修正) ---
        lt_status = "中性"
        if fcf_yield:
            if fcf_yield > 0.04:
                lt_status = "便宜"
                self.logs.append(f"[价值] FCF Yield {format_percent(fcf_yield)} 显示回报丰厚。")
            elif fcf_yield < 0.02:
                lt_status = "昂贵"
                self.logs.append(f"[价值] FCF Yield {format_percent(fcf_yield)} 极低，透支未来。")
            
            if roic and roic > 0.15:
                self.logs.append(f"[护城河] ROIC 高达 {format_percent(roic)}，竞争优势显著。")
                if lt_status == "中性": lt_status = "优质"

        # C. 盈利惊喜 (Alpha 因子)
        if earnings and isinstance(earnings, list):
            beats = 0
            total = 0
            for e in earnings:
                est = e.get("estimatedEarning")
                act = e.get("actualEarningResult")
                if est is not None and act is not None:
                    total += 1
                    if act > est: beats += 1
            
            if total > 0:
                beat_rate = beats / total
                if beat_rate == 1.0:
                    self.logs.append(f"[Alpha] 业绩惊喜: 过去 {total} 个季度连续超预期。")
                    if lt_status == "中性": lt_status = "动能强劲"
                elif beat_rate < 0.5:
                    self.logs.append(f"[风险] 业绩雷: 过去 {total} 个季度中有 {total - beats} 次不及预期。")

        self.long_term_verdict = lt_status

        return {
            "price": price,
            "beta": beta,
            "market_regime": self.market_regime
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

@bot.tree.command(name="analyze", description="[v2.0] 机构级估值模型 (极简版)")
@app_commands.describe(ticker="股票代码 (如 NVDA)")
async def analyze(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    
    model = ValuationModel(ticker)
    success = await model.fetch_data()
    
    if not success:
        await interaction.followup.send(f"❌ 获取数据失败: `{ticker.upper()}`", ephemeral=True)
        return

    data = model.analyze()
    if not data:
        await interaction.followup.send(f"⚠️ 数据不足。", ephemeral=True)
        return

    # 极简风格：深色背景
    embed = discord.Embed(
        title=f"深度透视: {ticker.upper()}",
        description=f"现价: ${data['price']} | 市场情绪: {model.market_regime}",
        color=0x2b2d31 # Discord 深灰/黑色背景色
    )

    # 1. 估值结论 (无 Emoji，纯文本)
    verdict_text = (
        f"短期: **{model.short_term_verdict}**\n"
        f"长期: **{model.long_term_verdict}**"
    )
    embed.add_field(name="估值结论", value=verdict_text, inline=False)

    # 2. Beta
    beta_val = data['beta']
    beta_desc = "低波动" if beta_val < 0.8 else ("高波动" if beta_val > 1.3 else "适中")
    embed.add_field(name="Beta", value=f"{format_num(beta_val)} ({beta_desc})", inline=False)

    # 3. 因子分析 (核心逻辑整合)
    if model.logs:
        log_str = "\n".join([f"- {log}" for log in model.logs])
        embed.add_field(name="因子分析", value=f"```\n{log_str}\n```", inline=False)

    embed.set_footer(text="Model v2.0 | 包含历史分位与盈利修正")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(DISCORD_TOKEN)
