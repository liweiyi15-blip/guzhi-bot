import discord
from discord import app_commands
from discord.ext import commands
import requests
import os
import asyncio
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
FMP_API_KEY = os.getenv('FMP_API_KEY')

# FMP 接口配置
BASE_URL = "https://financialmodelingprep.com/stable"

# --- 数据获取与处理逻辑 ---

def get_fmp_data(endpoint, ticker):
    url = f"{BASE_URL}/{endpoint}/{ticker}?apikey={FMP_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        return data
    except Exception as e:
        print(f"Error fetching {endpoint} for {ticker}: {e}")
        return None

def format_num(num, is_currency=False):
    if num is None: return "N/A"
    if is_currency: return f"${num:,.2f}"
    return f"{num:.2f}"

class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        self.score = 0
        self.verdict = "未知"
        self.risk_tag = "未知"

    async def fetch_all(self):
        loop = asyncio.get_event_loop()
        tasks = {
            "profile": loop.run_in_executor(None, get_fmp_data, "profile", self.ticker),
            "dcf": loop.run_in_executor(None, get_fmp_data, "discounted-cash-flow", self.ticker),
            "ratios": loop.run_in_executor(None, get_fmp_data, "ratios-ttm", self.ticker),
            "metrics": loop.run_in_executor(None, get_fmp_data, "key-metrics-ttm", self.ticker)
        }
        results = await asyncio.gather(*tasks.values())
        self.data = dict(zip(tasks.keys(), results))
        return self.data["profile"] is not None

    def calculate_valuation(self):
        profile = self.data.get("profile", {})
        dcf_data = self.data.get("dcf", {})
        ratios = self.data.get("ratios", {})
        metrics = self.data.get("metrics", {})

        if not profile: return None

        current_price = profile.get("price")
        beta = profile.get("beta", 1.0)
        dcf_value = dcf_data.get("dcf")
        
        peg = ratios.get("pegRatioTTM")
        pe = ratios.get("priceEarningsRatioTTM")
        ev_ebitda = metrics.get("enterpriseValueOverEBITDATTM")

        # 1. 风险定性
        if beta > 1.5:
            self.risk_tag = "⚠️ 高波动 (High Beta)"
            margin_requirement = 1.25
        elif beta < 0.8:
            self.risk_tag = "🛡️ 防御型 (Low Beta)"
            margin_requirement = 1.0
        else:
            self.risk_tag = "⚖️ 市场平均波动"
            margin_requirement = 1.1

        analysis_log = []

        # 2. 估值打分 (逻辑保持严谨)
        # DCF
        if dcf_value:
            upside = (dcf_value - current_price) / current_price
            if upside > 0.2 * margin_requirement:
                self.score += 4
                analysis_log.append(f"✅ 价格低于内在价值 (空间 +{upside*100:.1f}%)")
            elif upside > 0:
                self.score += 2
                analysis_log.append(f"☑️ 价格接近内在价值 (公允)")
            elif upside < -0.2:
                self.score -= 2
                analysis_log.append(f"❌ 价格高于内在价值 (溢价 {abs(upside*100):.1f}%)")
            else:
                analysis_log.append(f"⚠️ 价格略有溢价")

        # PEG
        if peg:
            if 0 < peg < 1.0:
                self.score += 3
                analysis_log.append(f"✅ PEG {peg:.2f} < 1 (成长性被低估)")
            elif 1.0 <= peg < 1.5:
                self.score += 1
                analysis_log.append(f"☑️ PEG {peg:.2f} (估值与成长匹配)")
            elif peg > 2.0:
                self.score -= 2
                analysis_log.append(f"❌ PEG {peg:.2f} (透支未来业绩)")

        # EV/EBITDA
        if ev_ebitda:
            if ev_ebitda < 15:
                self.score += 3
                analysis_log.append(f"✅ EV/EBITDA {ev_ebitda:.1f} 处于低位区间")
            elif ev_ebitda > 25:
                self.score -= 1
                analysis_log.append(f"⚠️ EV/EBITDA {ev_ebitda:.1f} 处于高位区间")
            else:
                self.score += 1
                analysis_log.append(f"☑️ EV/EBITDA 估值中性")

        # 3. 评判结论
        if self.score >= 7:
            self.verdict = "🟢 极度低估 (Deep Value)"
        elif self.score >= 4:
            self.verdict = "🔵 适度低估 (Undervalued)"
        elif self.score >= 1:
            self.verdict = "🟡 估值公允 (Fair Value)"
        elif self.score >= -2:
            self.verdict = "🟠 略微高估 (Overvalued)"
        else:
            self.verdict = "🔴 严重高估 (Expensive)"

        return {
            "price": current_price,
            "dcf": dcf_value,
            "beta": beta,
            "pe": pe,
            "peg": peg,
            "ev_ebitda": ev_ebitda,
            "logs": analysis_log,
            "company_name": profile.get("companyName"),
            "image": profile.get("image")
        }

# --- Bot 设置与 Slash Command ---

class ValuationBot(commands.Bot):
    def __init__(self):
        # 设置 intents
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # 启动时同步 Slash 命令
        # 注意：全局同步可能需要几分钟到1小时生效。
        # 如果是私有服务器，可以使用 guild=discord.Object(id=YOUR_GUILD_ID) 进行秒级同步
        print("正在同步 Slash 命令...")
        await self.tree.sync()
        print("Slash 命令同步完成！")

bot = ValuationBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')

# 定义 Slash Command
@bot.tree.command(name="value", description="基于机构模型测算美股估值 (DCF/PEG/EBITDA)")
@app_commands.describe(ticker="股票代码 (例如: NVDA, AAPL)")
async def value(interaction: discord.Interaction, ticker: str):
    # 1. 立即回复 "Thinking..." 避免超时
    await interaction.response.defer(thinking=True)
    
    # 2. 调取数据
    model = ValuationModel(ticker)
    success = await model.fetch_all()
    
    if not success:
        # 使用 followup 发送结果
        await interaction.followup.send(f"❌ 找不到代码 `{ticker.upper()}` 或 API 数据不可用。", ephemeral=True)
        return

    result = model.calculate_valuation()
    if not result:
        await interaction.followup.send(f"⚠️ 数据解析失败，请稍后重试。", ephemeral=True)
        return

    # 3. 构建 Embed
    embed = discord.Embed(
        title=f"📊 估值评测: {result['company_name']} ({ticker.upper()})",
        description=f"**当前评价:** {model.verdict}\n基于 DCF、PEG 及 EV/EBITDA 多因子模型测算。",
        color=0x00ff00 if model.score >= 4 else (0xff0000 if model.score < 0 else 0xffaa00)
    )
    
    if result['image']:
        embed.set_thumbnail(url=result['image'])

    # 字段展示
    embed.add_field(name="当前价格", value=f"${result['price']}", inline=True)
    embed.add_field(name="内在价值 (DCF)", value=format_num(result['dcf'], True), inline=True)
    embed.add_field(name="风险属性 (Beta)", value=f"{format_num(result['beta'])} \n{model.risk_tag}", inline=True)

    metrics_str = (
        f"**P/E (TTM):** {format_num(result['pe'])}\n"
        f"**PEG Ratio:** {format_num(result['peg'])}\n"
        f"**EV/EBITDA:** {format_num(result['ev_ebitda'])}"
    )
    embed.add_field(name="估值倍数 (TTM)", value=metrics_str, inline=False)

    log_str = "\n".join(result['logs'])
    embed.add_field(name="🔬 评测详情", value=f"```{log_str}```", inline=False)

    embed.set_footer(text="Data: Financial Modeling Prep | 仅供参考")

    # 4. 发送最终结果
    await interaction.followup.send(embed=embed)

# 运行 Bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(DISCORD_TOKEN)
