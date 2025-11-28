import discord
from discord import app_commands
from discord.ext import commands
import requests
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
FMP_API_KEY = os.getenv('FMP_API_KEY')

# FMP 稳定接口
BASE_URL = "https://financialmodelingprep.com/stable"

# --- 数据工具函数 ---
def get_fmp_data(endpoint, ticker, params=""):
    url = f"{BASE_URL}/{endpoint}/{ticker}?apikey={FMP_API_KEY}&{params}"
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

def format_percent(num):
    if num is None: return "N/A"
    return f"{num * 100:.2f}%"

def format_num(num):
    if num is None: return "N/A"
    return f"{num:.2f}"

# --- 核心量化模型 (Quant Alpha) ---

class QuantAlphaModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        
        # 评分系统
        self.score = 0
        self.max_score = 100
        self.verdict = "N/A"
        
        # 因子分析日志
        self.logs = []
        self.flags = [] # 严重的红牌警告

    async def fetch_data(self):
        loop = asyncio.get_event_loop()
        # 并行获取 5 个核心接口
        tasks = {
            "profile": loop.run_in_executor(None, get_fmp_data, "profile", self.ticker, ""),
            "quote": loop.run_in_executor(None, get_fmp_data, "quote", self.ticker, ""),
            "metrics": loop.run_in_executor(None, get_fmp_data, "key-metrics-ttm", self.ticker, ""),
            "ratios": loop.run_in_executor(None, get_fmp_data, "ratios-ttm", self.ticker, ""),
            "cash_flow": loop.run_in_executor(None, get_fmp_data, "cash-flow-statement", self.ticker, "limit=1") # 取最新年报做质量审计
        }
        
        results = await asyncio.gather(*tasks.values())
        self.data = dict(zip(tasks.keys(), results))
        
        return self.data["profile"] is not None and self.data["quote"] is not None

    def analyze(self):
        # 提取数据
        p = self.data.get("profile", {})
        q = self.data.get("quote", {})
        m = self.data.get("metrics", {})
        r = self.data.get("ratios", {})
        cf = self.data.get("cash_flow", {}) # 可能返回list

        if not p or not q: return None

        price = q.get("price")
        sector = p.get("sector", "Unknown")
        beta = p.get("beta", 1.0)
        
        # ----------------------------------------------------
        # 第一关：财务质量排雷 (Accounting Quality) - 权重: 20分 / 一票否决
        # ----------------------------------------------------
        net_income = cf.get("netIncome") if cf else 0
        ocf = cf.get("operatingCashFlow") if cf else 0
        
        quality_score = 20
        if net_income and ocf:
            # 逻辑：如果你赚了1亿净利润，但经营现金流只有5000万，说明你在压货或者赊账，财报质量差
            if ocf < net_income * 0.8:
                quality_score = 0
                self.flags.append(f"🚩 **财报质量警报**: 经营现金流大幅低于净利润 (Accruals Risk)")
                self.logs.append(f"❌ 现金流健康度: 差 (NI ${format_num(net_income/1e6)}M vs OCF ${format_num(ocf/1e6)}M)")
            elif ocf > net_income * 1.1:
                self.logs.append(f"✅ 现金流强劲: OCF 覆盖率高 (含金量高)")
            else:
                self.logs.append(f"☑️ 现金流正常匹配")
        else:
            self.logs.append("⚠️ 缺少现金流数据，跳过质量审计")
            quality_score = 10
        
        self.score += quality_score

        # ----------------------------------------------------
        # 第二关：硬核估值 (FCF Yield & EV/EBITDA) - 权重: 40分
        # ----------------------------------------------------
        # 使用 FCF Yield 替代 DCF。FCF Yield > 4% 也就是相当于 25倍 PE 的倒数，但更真实。
        fcf_yield = m.get("freeCashFlowYieldTTM")
        ev_ebitda = m.get("enterpriseValueOverEBITDATTM")
        
        val_score = 0
        
        # FCF Yield 评分 (20分)
        if fcf_yield:
            if fcf_yield > 0.08: # >8% 极度便宜
                val_score += 20
                self.logs.append(f"✅ **FCF Yield**: {format_percent(fcf_yield)} (现金奶牛!)")
            elif fcf_yield > 0.04: # >4% 合理
                val_score += 15
                self.logs.append(f"☑️ **FCF Yield**: {format_percent(fcf_yield)} (合理回报)")
            elif fcf_yield > 0.01:
                val_score += 5
                self.logs.append(f"⚠️ **FCF Yield**: {format_percent(fcf_yield)} (微薄回报)")
            else:
                val_score += 0
                self.logs.append(f"❌ **FCF Yield**: {format_percent(fcf_yield)} (烧钱/太贵)")
        
        # EV/EBITDA 评分 (20分)
        if ev_ebitda:
            # 简单粗暴的行业分位逻辑模拟
            limit = 20 if "Tech" in sector else 12 # 科技股容忍度高
            if ev_ebitda < limit:
                val_score += 20
                self.logs.append(f"✅ **EV/EBITDA**: {format_num(ev_ebitda)} (低于行业阈值 {limit})")
            elif ev_ebitda < limit * 1.5:
                val_score += 10
                self.logs.append(f"☑️ **EV/EBITDA**: {format_num(ev_ebitda)} (中性)")
            else:
                self.logs.append(f"❌ **EV/EBITDA**: {format_num(ev_ebitda)} (过热)")

        self.score += val_score

        # ----------------------------------------------------
        # 第三关：行业 Beta 校准与趋势 (Trend & Risk) - 权重: 20分
        # ----------------------------------------------------
        trend_score = 0
        
        # 1. 行业调整后 Beta
        # 只有在防御性板块 Beta 还很高，或者科技板块 Beta 极高 (>2.0) 时才扣分
        beta_threshold = 1.5 if "Tech" in sector else 1.0
        risk_status = "正常"
        
        if beta and beta > beta_threshold + 0.5:
            trend_score -= 5
            risk_status = "高波动"
            self.logs.append(f"⚠️ **Beta ({beta})**: 高于行业适宜水平 ({beta_threshold})")
        elif beta and beta < 0.8:
            trend_score += 5
            risk_status = "低波动"
            self.logs.append(f"✅ **Beta ({beta})**: 具备防御属性")
        else:
            trend_score += 5
            self.logs.append(f"☑️ **Beta ({beta})**: 行业范围内合理")

        # 2. 200日均线趋势 (牛熊分界线)
        sma200 = q.get("priceAvg200")
        if sma200:
            if price > sma200:
                trend_score += 15
                self.logs.append(f"📈 **技术面**: 价格 > 200日均线 (多头趋势)")
            else:
                self.logs.append(f"📉 **技术面**: 价格 < 200日均线 (空头趋势)")
        
        self.score += max(0, trend_score) # 保证不扣成负数

        # ----------------------------------------------------
        # 第四关：成长性 (Growth) - 权重: 20分
        # ----------------------------------------------------
        # 即使没有 Forward PE，我们可以看营收增长
        rev_growth = m.get("revenueGrowthTTM")
        
        growth_score = 0
        if rev_growth:
            if rev_growth > 0.2: # >20%
                growth_score = 20
                self.logs.append(f"🚀 **营收增长**: {format_percent(rev_growth)} (高成长)")
            elif rev_growth > 0.05:
                growth_score = 10
                self.logs.append(f"☑️ **营收增长**: {format_percent(rev_growth)} (稳健)")
            elif rev_growth < 0:
                self.logs.append(f"❌ **营收增长**: {format_percent(rev_growth)} (萎缩)")
        
        self.score += growth_score

        # ----------------------------------------------------
        # 最终裁决
        # ----------------------------------------------------
        # 如果有严重红牌，分数强制打折
        if self.flags:
            self.score = min(self.score, 59)
            self.verdict = "🚩 存在硬伤 (Major Flags)"
        elif self.score >= 80:
            self.verdict = "🟢 强力买入 (Strong Buy)"
        elif self.score >= 60:
            self.verdict = "🔵 逢低吸纳 (Buy/Accumulate)"
        elif self.score >= 40:
            self.verdict = "🟡 观望/持有 (Hold)"
        else:
            self.verdict = "🔴 卖出/回避 (Sell/Avoid)"

        return {
            "price": price,
            "sma200": sma200,
            "fcf_yield": fcf_yield,
            "ev_ebitda": ev_ebitda,
            "sector": sector,
            "beta": beta,
            "flags": self.flags
        }

# --- Bot Setup ---

class HardcoreBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        print("Syncing commands...")
        await self.tree.sync()
        print("Commands synced.")

bot = HardcoreBot()

@bot.tree.command(name="analyze", description="[硬核版] 机构级量化评分模型 (Quality + Value + Trend)")
@app_commands.describe(ticker="股票代码 (e.g. MSFT)")
async def analyze(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    
    model = QuantAlphaModel(ticker)
    success = await model.fetch_data()
    
    if not success:
        await interaction.followup.send(f"❌ 数据获取失败 `{ticker.upper()}`", ephemeral=True)
        return

    data = model.analyze()
    if not data:
        await interaction.followup.send(f"⚠️ 数据不足以进行量化分析。", ephemeral=True)
        return

    # 颜色逻辑：根据分数变色
    color = 0x2ecc71 if model.score >= 70 else (0xe74c3c if model.score < 40 else 0xf1c40f)
    
    embed = discord.Embed(
        title=f"🛡️ 量化审计报告: {ticker.upper()}",
        description=f"**所属板块:** {data['sector']}\n**当前价格:** ${data['price']}",
        color=color
    )

    # 1. 核心结论区
    verdict_text = f"# {model.verdict}\n**综合评分: {model.score}/100**"
    if model.flags:
        verdict_text += "\n⚠️ **检测到重大财务风险，分数已强制修正**"
    
    embed.add_field(name="🏆 审计结论", value=verdict_text, inline=False)

    # 2. 风险警报区 (如果有)
    if model.flags:
        flag_str = "\n".join(model.flags)
        embed.add_field(name="🚩 风险警示 (RED FLAGS)", value=f"```{flag_str}```", inline=False)

    # 3. 核心因子详情
    # 将日志分为 "优势" 和 "劣势" 或者直接列出
    log_str = "\n".join(model.logs)
    embed.add_field(name="🧠 因子详细分析 (Factor Analysis)", value=f"```diff\n{log_str}\n```", inline=False)

    # 4. 关键指标概览
    metrics_str = (
        f"**FCF Yield:** {format_percent(data['fcf_yield'])}\n"
        f"**EV/EBITDA:** {format_num(data['ev_ebitda'])}\n"
        f"**200日均线:** ${format_num(data['sma200'])}"
    )
    embed.add_field(name="📊 核心量化指标", value=metrics_str, inline=False)

    embed.set_footer(text="Model: Quant Alpha v1.0 | Data: FMP Stable | 不构成投资建议")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
