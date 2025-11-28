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

# FMP 稳定接口
BASE_URL = "https://financialmodelingprep.com/stable"

# --- 1. 数据工具函数 (已修复 URL 结构) ---

def get_fmp_data(endpoint, ticker, params=""):
    """
    针对 Stable 接口的通用请求函数
    结构: /endpoint?symbol=TICKER&apikey=KEY
    """
    url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey={FMP_API_KEY}&{params}"
    
    try:
        response = requests.get(url, timeout=10)
        # 调试用：如果非200，打印状态码
        if response.status_code != 200:
             print(f"⚠️ API Request Failed: {response.status_code} for {endpoint}")
        
        response.raise_for_status()
        data = response.json()
        
        # 统一处理 FMP 返回 List 的情况
        if isinstance(data, list):
            if len(data) > 0:
                return data[0]
            else:
                return None
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

# --- 2. 核心量化模型 (Quant Alpha v1.2) ---

class QuantAlphaModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        
        # 评分系统
        self.score = 0
        self.max_score = 100
        self.verdict = "N/A"
        
        # 日志与警报
        self.logs = []
        self.flags = [] # 重大风险红牌

    async def fetch_data(self):
        """并行获取所有核心数据"""
        loop = asyncio.get_event_loop()
        tasks = {
            "profile": loop.run_in_executor(None, get_fmp_data, "profile", self.ticker, ""),
            "quote": loop.run_in_executor(None, get_fmp_data, "quote", self.ticker, ""),
            "metrics": loop.run_in_executor(None, get_fmp_data, "key-metrics-ttm", self.ticker, ""),
            "ratios": loop.run_in_executor(None, get_fmp_data, "ratios-ttm", self.ticker, ""),
            # 取最近一份年报做现金流审计
            "cash_flow": loop.run_in_executor(None, get_fmp_data, "cash-flow-statement", self.ticker, "limit=1") 
        }
        
        results = await asyncio.gather(*tasks.values())
        self.data = dict(zip(tasks.keys(), results))
        
        # 核心数据检查
        return self.data["profile"] is not None and self.data["quote"] is not None

    def analyze(self):
        # 提取数据
        p = self.data.get("profile", {})
        q = self.data.get("quote", {})
        m = self.data.get("metrics", {})
        r = self.data.get("ratios", {})
        cf = self.data.get("cash_flow", {}) 

        if not p or not q: return None

        price = q.get("price")
        sector = p.get("sector", "Unknown")
        beta = p.get("beta", 1.0)
        m_cap = p.get("mktCap", 0)
        payout = r.get("payoutRatioTTM", 0)
        
        # ----------------------------------------------------
        # 预审：市值规模 (Market Cap Logic)
        # ----------------------------------------------------
        if m_cap < 500 * 1e6: # < 5亿 (微盘股)
            self.score -= 10
            self.logs.append(f"⚠️ **市值过小**: ${format_num(m_cap/1e6)}M (流动性差/高风险)")
        elif m_cap > 100 * 1e9: # > 1000亿 (蓝筹)
            self.score += 5
            self.logs.append(f"🛡️ **蓝筹护城河**: ${format_num(m_cap/1e9)}B (抗风险能力强)")

        # ----------------------------------------------------
        # 第一关：财务质量排雷 (Accounting Quality)
        # ----------------------------------------------------
        net_income = cf.get("netIncome") if cf else 0
        ocf = cf.get("operatingCashFlow") if cf else 0
        
        quality_score = 20
        if net_income and ocf:
            # 经营现金流 < 净利润的 80% -> 可能是假账或回款困难
            if ocf < net_income * 0.8:
                quality_score = 0
                self.flags.append(f"🚩 **财报质量差**: 经营现金流远低于净利润 (Accruals Risk)")
                self.logs.append(f"❌ 现金流健康度: 差 (NI ${format_num(net_income/1e6)}M vs OCF ${format_num(ocf/1e6)}M)")
            elif ocf > net_income * 1.1:
                self.logs.append(f"✅ 现金流强劲: OCF 高质量覆盖净利润")
            else:
                self.logs.append(f"☑️ 现金流与利润匹配")
        elif not cf:
             self.logs.append("⚠️ 缺少现金流数据，跳过质量审计")
             quality_score = 10 # 给个平均分
        
        self.score += quality_score

        # ----------------------------------------------------
        # 补丁：派息安全审计 (Dividend Safety)
        # ----------------------------------------------------
        if payout and payout > 1.2: # 赚100块分120块
            self.score -= 10
            self.flags.append(f"🚩 **高股息陷阱**: 派息率 {format_percent(payout)} (不可持续)")
        elif payout and 0.01 < payout < 0.6:
            self.logs.append(f"☑️ 分红安全: 派息率 {format_percent(payout)} 健康")

        # ----------------------------------------------------
        # 第二关：硬核估值 (Value) - 权重 40
        # ----------------------------------------------------
        fcf_yield = m.get("freeCashFlowYieldTTM")
        ev_ebitda = m.get("enterpriseValueOverEBITDATTM")
        
        val_score = 0
        
        # FCF Yield
        if fcf_yield:
            if fcf_yield > 0.08: # >8%
                val_score += 20
                self.logs.append(f"✅ **FCF Yield**: {format_percent(fcf_yield)} (极度便宜)")
            elif fcf_yield > 0.04: # >4%
                val_score += 15
                self.logs.append(f"☑️ **FCF Yield**: {format_percent(fcf_yield)} (合理)")
            elif fcf_yield > 0.01:
                val_score += 5
                self.logs.append(f"⚠️ **FCF Yield**: {format_percent(fcf_yield)} (微薄)")
            else:
                self.logs.append(f"❌ **FCF Yield**: {format_percent(fcf_yield)} (负收益/太贵)")
        
        # EV/EBITDA
        if ev_ebitda:
            limit = 20 if "Tech" in sector else 12
            if ev_ebitda < limit:
                val_score += 20
                self.logs.append(f"✅ **EV/EBITDA**: {format_num(ev_ebitda)} (低估)")
            elif ev_ebitda < limit * 1.5:
                val_score += 10
                self.logs.append(f"☑️ **EV/EBITDA**: {format_num(ev_ebitda)} (中性)")
            else:
                self.logs.append(f"❌ **EV/EBITDA**: {format_num(ev_ebitda)} (高估)")

        self.score += val_score

        # ----------------------------------------------------
        # 第三关：趋势与风险 (Trend & Risk) - 权重 20
        # ----------------------------------------------------
        trend_score = 0
        
        # Beta
        beta_threshold = 1.5 if "Tech" in sector else 1.0
        if beta and beta > beta_threshold + 0.5:
            trend_score -= 5
            self.logs.append(f"⚠️ **Beta ({beta})**: 高于行业适宜水平")
        elif beta and beta < 0.8:
            trend_score += 5
            self.logs.append(f"✅ **Beta ({beta})**: 防御性好")
        else:
            trend_score += 5
            self.logs.append(f"☑️ **Beta ({beta})**: 适中")

        # SMA 200
        sma200 = q.get("priceAvg200")
        if sma200:
            if price > sma200:
                trend_score += 15
                self.logs.append(f"📈 **技术面**: 价格 > 200日均线 (多头)")
            else:
                self.logs.append(f"📉 **技术面**: 价格 < 200日均线 (空头)")
        
        self.score += max(0, trend_score)

        # ----------------------------------------------------
        # 第四关：成长性 (Growth) - 权重 20
        # ----------------------------------------------------
        rev_growth = m.get("revenueGrowthTTM")
        
        growth_score = 0
        if rev_growth:
            if rev_growth > 0.2: 
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
        # 如果有严重红牌，分数封顶 59
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

# --- 3. Bot 设置与命令 ---

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
    # 避免超时，先 defer
    await interaction.response.defer(thinking=True)
    
    # 获取数据
    model = QuantAlphaModel(ticker)
    success = await model.fetch_data()
    
    if not success:
        await interaction.followup.send(f"❌ 找不到代码 `{ticker.upper()}` 或 API 数据异常。", ephemeral=True)
        return

    # 运行分析
    data = model.analyze()
    if not data:
        await interaction.followup.send(f"⚠️ 数据不足以进行完整审计。", ephemeral=True)
        return

    # 动态颜色：高分绿，低分红，中等黄
    color = 0x2ecc71 if model.score >= 70 else (0xe74c3c if model.score < 40 else 0xf1c40f)
    
    embed = discord.Embed(
        title=f"🛡️ 量化审计报告: {ticker.upper()}",
        description=f"**所属板块:** {data['sector']}\n**当前价格:** ${data['price']}",
        color=color
    )

    # 1. 结论区
    verdict_text = f"# {model.verdict}\n**综合评分: {model.score}/100**"
    if model.flags:
        verdict_text += "\n⚠️ **检测到重大风险，分数已强制下调**"
    
    embed.add_field(name="🏆 审计结论", value=verdict_text, inline=False)

    # 2. 风险警报 (红牌)
    if model.flags:
        flag_str = "\n".join(model.flags)
        embed.add_field(name="🚩 风险警示 (RED FLAGS)", value=f"```{flag_str}```", inline=False)

    # 3. 详细日志
    log_str = "\n".join(model.logs)
    embed.add_field(name="🧠 因子分析详情", value=f"```diff\n{log_str}\n```", inline=False)

    # 4. 关键指标
    metrics_str = (
        f"**FCF Yield:** {format_percent(data['fcf_yield'])}\n"
        f"**EV/EBITDA:** {format_num(data['ev_ebitda'])}\n"
        f"**200日均线:** ${format_num(data['sma200'])}"
    )
    embed.add_field(name="📊 核心量化指标", value=metrics_str, inline=False)

    embed.set_footer(text="Model: Quant Alpha v1.2 | Data: FMP Stable")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(DISCORD_TOKEN)
