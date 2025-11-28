import discord
from discord.ext import commands
import requests
import os
import asyncio
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
FMP_API_KEY = os.getenv('FMP_API_KEY')

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 依然使用 Stable 接口
BASE_URL = "https://financialmodelingprep.com/stable"

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
        self.risk_tag = "未知" # 用标签代替具体的风控建议

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

        if not profile: return

        current_price = profile.get("price")
        beta = profile.get("beta", 1.0)
        dcf_value = dcf_data.get("dcf")
        
        peg = ratios.get("pegRatioTTM")
        pe = ratios.get("priceEarningsRatioTTM")
        ev_ebitda = metrics.get("enterpriseValueOverEBITDATTM")

        # 1. 风险定性 (Risk Assessment)
        if beta > 1.5:
            self.risk_tag = "⚠️ 高波动 (High Beta)"
            margin_requirement = 1.25 # 高波动需要更大的折扣才算便宜
        elif beta < 0.8:
            self.risk_tag = "🛡️ 防御型 (Low Beta)"
            margin_requirement = 1.0
        else:
            self.risk_tag = "⚖️ 市场平均波动"
            margin_requirement = 1.1

        analysis_log = []

        # 2. 估值打分 (逻辑保持科学严谨)
        
        # DCF (绝对估值)
        if dcf_value:
            upside = (dcf_value - current_price) / current_price
            # 根据 Beta 调整判定标准
            if upside > 0.2 * margin_requirement:
                self.score += 4
                analysis_log.append(f"✅ 价格低于内在价值 (低估幅度 {upside*100:.1f}%)")
            elif upside > 0:
                self.score += 2
                analysis_log.append(f"☑️ 价格接近内在价值 (公允)")
            elif upside < -0.2:
                self.score -= 2
                analysis_log.append(f"❌ 价格高于内在价值 (溢价 {abs(upside*100):.1f}%)")
            else:
                analysis_log.append(f"⚠️ 价格略有溢价")

        # PEG (成长性修正)
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

        # EV/EBITDA (机构倍数)
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

        # 3. 最终评判 (只说贵贱，不说买卖)
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

@bot.event
async def on_ready():
    print(f'Valuation Bot Logged in as {bot.user}')

@bot.command(name='value')
async def valuation(ctx, ticker: str):
    msg = await ctx.send(f"🔄 正在测算 {ticker.upper()} 的估值水平...")
    
    model = ValuationModel(ticker)
    success = await model.fetch_all()
    
    if not success:
        await msg.edit(content=f"❌ 无法获取 {ticker.upper()} 数据，请检查拼写。")
        return

    result = model.calculate_valuation()
    
    # 颜色：绿色代表便宜，红色代表贵
    embed = discord.Embed(
        title=f"📊 估值评测: {result['company_name']} ({ticker.upper()})",
        description=f"**当前评价:** {model.verdict}\n基于 DCF、PEG 及 EV/EBITDA 多因子模型测算。",
        color=0x00ff00 if model.score >= 4 else (0xff0000 if model.score < 0 else 0xffaa00)
    )
    
    if result['image']:
        embed.set_thumbnail(url=result['image'])

    # 第一行：价格与内在价值对比
    embed.add_field(name="当前价格", value=f"${result['price']}", inline=True)
    embed.add_field(name="内在价值 (DCF)", value=format_num(result['dcf'], True), inline=True)
    embed.add_field(name="风险属性 (Beta)", value=f"{format_num(result['beta'])} \n{model.risk_tag}", inline=True)

    # 第二行：机构核心指标
    metrics_str = (
        f"**P/E (TTM):** {format_num(result['pe'])}\n"
        f"**PEG Ratio:** {format_num(result['peg'])}\n"
        f"**EV/EBITDA:** {format_num(result['ev_ebitda'])}"
    )
    embed.add_field(name="估值倍数 (Valuation Multiples)", value=metrics_str, inline=False)

    # 第三行：评判逻辑细节
    log_str = "\n".join(result['logs'])
    embed.add_field(name="🔬 评测详情", value=f"```{log_str}```", inline=False)

    embed.set_footer(text="Data: Financial Modeling Prep | 结果仅供参考，不构成投资建议")

    await msg.delete()
    await ctx.send(embed=embed)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
