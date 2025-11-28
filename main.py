import discord
from discord import app_commands
from discord.ext import commands
import requests
import os
import asyncio
import logging
import math
from datetime import datetime
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
FMP_API_KEY = os.getenv('FMP_API_KEY')

BASE_URL = "https://financialmodelingprep.com/stable"
V4_URL = "https://financialmodelingprep.com/api/v4"

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
        
        # FMP 通用处理
        if isinstance(data, list) and "historical" not in endpoint and "treasury" not in endpoint:
            if len(data) > 0:
                return data[0]
            else:
                return None
        return data
    except Exception as e:
        logger.error(f"❌ Exception fetching {endpoint}: {e}")
        return None

def get_fmp_list_data(endpoint, ticker, limit=4):
    url = f"{BASE_URL}/{endpoint}/{ticker}?apikey={FMP_API_KEY}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200: return []
        return response.json()
    except:
        return []

def get_macro_data():
    """获取宏观数据：联邦基金利率"""
    # 使用 treasury 接口获取利率
    url = f"https://financialmodelingprep.com/api/v4/treasury?from=2024-01-01&to=2026-01-01&apikey={FMP_API_KEY}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data:
                # 找最新的包含 FEDFUNDS 或类似的数据，这里简化取第一个或 10year 近似代替
                # FMP treasury 接口返回的是 year30, year20, year10, month3 等
                # 我们用 month3 近似无风险利率
                return data[0].get("month3", 4.5) 
        return 4.5 # 默认兜底 4.5%
    except:
        return 4.5

def format_percent(num):
    if num is None: return "N/A"
    return f"{num * 100:.2f}%"

def format_num(num):
    if num is None: return "N/A"
    return f"{num:.2f}"

def format_market_cap(num):
    if num is None or num == 0: return "N/A"
    if num >= 1e12: return f"${num/1e12:.2f}T"
    if num >= 1e9: return f"${num/1e9:.2f}B"
    return f"${num/1e6:.2f}M"

# --- 2. 行业基准 (动态调整预留) ---
SECTOR_EBITDA_MEDIAN = {
    "Technology": 32.0, "Consumer Electronics": 25.0, "Communication Services": 20.0,
    "Healthcare": 18.0, "Financial Services": 12.0, "Energy": 10.0,
    "Utilities": 12.0, "Unknown": 18.0
}

def get_sector_benchmark(sector):
    for key, val in SECTOR_EBITDA_MEDIAN.items():
        if key in sector: return val
    return 18.0

# --- 3. 估值判断模型 (v3.0 科学辩证版) ---

class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        self.market_regime = "未知"
        self.risk_var = "N/A" # 在险价值
        
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
            "vix": loop.run_in_executor(None, get_fmp_data, "quote", "^VIX", ""),
            # v3.0: 扩大到 8 个季度以观察长期 Alpha
            "earnings": loop.run_in_executor(None, get_fmp_list_data, "earnings-surprises", self.ticker, 8),
            # v3.0: 宏观利率
            "macro_rate": loop.run_in_executor(None, get_macro_data)
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
        fed_rate = self.data.get("macro_rate", 4.5)
        
        if not p or not q: return None

        price = q.get("price")
        sector = p.get("sector", "Unknown")
        beta = p.get("beta", 1.0)
        
        # 字段提取
        m_cap = q.get("marketCap") or m.get("marketCap") or p.get("mktCap", 0)
        ev_ebitda = m.get("evToEBITDA") or m.get("enterpriseValueOverEBITDATTM") or r.get("enterpriseValueMultipleTTM")
        fcf_yield = m.get("freeCashFlowYield") or m.get("freeCashFlowYieldTTM")
        roic = m.get("returnOnInvestedCapital") or m.get("returnOnInvestedCapitalTTM")
        
        # PEG 处理 (带 epsilon 保护)
        peg = r.get("priceToEarningsGrowthRatioTTM") or r.get("pegRatioTTM")
        ni_growth = m.get("netIncomeGrowthTTM")
        pe = r.get("priceEarningsRatioTTM") or m.get("peRatioTTM")
        
        if peg is None:
            if pe and ni_growth:
                if ni_growth > 0.01: # 避免除以零或微小值
                    peg = pe / (ni_growth * 100)
                    self.logs.append(f"[数据] 手算 PEG: {format_num(peg)} (增长率 {format_percent(ni_growth)})")
                else:
                    self.logs.append(f"[数据] 增长停滞 ({format_percent(ni_growth)})，PEG 失效。")

        # 成长分层 (Growth Tier)
        growth_tier = "Low"
        rev_growth = m.get("revenueGrowthTTM")
        if (rev_growth and rev_growth > 0.5) or (ni_growth and ni_growth > 0.5):
            growth_tier = "Hyper"
        elif (rev_growth and rev_growth > 0.2) or (ni_growth and ni_growth > 0.2):
            growth_tier = "High"

        # --- 0. 宏观叠加与市场情绪 ---
        vix = vix_data.get("price", 20)
        # 利率调整因子: 利率每高出4%一个点，估值压力增加
        rate_adj_factor = 1 + max(0, (fed_rate - 4.0) / 100.0) 
        
        if vix < 20: self.market_regime = f"平静 (VIX {vix:.1f})"
        elif vix < 30: self.market_regime = f"震荡 (VIX {vix:.1f})"
        else: self.market_regime = f"恐慌 (VIX {vix:.1f})"

        # --- 1. 风险量化 (VaR) ---
        # 简单 VaR 模型: 95% 置信度 (Z=1.65) * 月度波动率估计
        # 月波动率 approx = (VIX / 100) / sqrt(12) * beta ... 简化为 beta * vix/100 * Z
        if price and beta and vix:
            # 这是一个经验公式，用于估算极端情况下的月度回撤风险
            monthly_risk_pct = (vix / 100) * beta * 1.0 * 100 # 粗略估计
            self.risk_var = f"-{monthly_risk_pct:.1f}%"
            if monthly_risk_pct > 15:
                self.flags.append(f"⚠️ **高风险预警**: 基于当前 VIX 和 Beta，月度潜在波动极大。")

        # --- 2. 短期估值 (利率自适应) ---
        sector_avg = get_sector_benchmark(sector)
        st_status = "估值合理"
        
        if ev_ebitda:
            # 宏观校准: 熊市/高息环境下，名义倍数需要打折看
            # adjusted_ev 是考虑利率压力后的“体感估值”
            adjusted_ev = ev_ebitda * rate_adj_factor
            
            ratio = adjusted_ev / sector_avg
            
            # PEG 豁免逻辑 (科学分层)
            if growth_tier == "Hyper" and peg and peg < 1.5:
                st_status = "便宜 (超成长)"
                self.logs.append(f"[成长特权] Hyper Growth (>50%) 抵消了高 EV/EBITDA。")
            elif growth_tier == "High" and peg and peg < 1.2:
                st_status = "便宜 (高成长)"
                self.logs.append(f"[成长特权] 强劲增长支撑当前估值，PEG {format_num(peg)} 极具吸引力。")
            elif ratio < 0.7:
                st_status = "便宜"
                self.logs.append(f"[板块] EV/EBITDA 低于行业均值，安全边际充足。")
            elif ratio > 1.3:
                # 即使是成长股，如果 PEG 也不行，那就是真贵
                if growth_tier != "Low" and peg and peg < 2.0:
                     st_status = "合理溢价"
                     self.logs.append(f"[辩证] 高估值是优质成长的合理溢价。")
                else:
                    st_status = "昂贵"
                    self.logs.append(f"[宏观] 考虑利率因素 (Fed {fed_rate}%)，当前倍数显著高估。")
            else:
                st_status = "估值合理"
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 处于合理区间。")
        else:
             self.logs.append(f"[数据] 缺少 EV/EBITDA。")
        
        self.short_term_verdict = st_status

        # --- 3. 长期估值 (FCF vs ROIC 辩证调解) ---
        lt_status = "中性"
        if fcf_yield:
            # 场景 A: 优质溢价 (Good Expensive)
            # 逻辑: FCF Yield 低没关系，只要 ROIC 够高，就是 Worth Waiting
            if fcf_yield < 0.025 and roic and roic > 0.20:
                lt_status = "优质/值得等待"
                self.logs.append(f"[辩证] FCF Yield 虽低，但 ROIC ({format_percent(roic)}) 极高，属于'优质溢价'。")
                self.logs.append(f"[策略] 此类资产通常不会便宜，适合分批配置或等待回调。")
            
            # 场景 B: 价值陷阱 (Cheap Junk)
            elif fcf_yield > 0.05 and roic and roic < 0.05:
                lt_status = "价值陷阱"
                self.logs.append(f"[避雷] FCF Yield 虽高，但 ROIC 极低，缺乏长期造血护城河。")
                
            # 场景 C: 常规判断
            elif fcf_yield > 0.04:
                lt_status = "便宜"
                self.logs.append(f"[价值] FCF Yield {format_percent(fcf_yield)} 丰厚，提供良好安全垫。")
            elif fcf_yield < 0.02:
                lt_status = "昂贵"
                if growth_tier == "Low":
                    self.logs.append(f"[价值] FCF Yield 极低且无增长，正在透支未来。")
            
            # 护城河加强判断
            if roic and roic > 0.15 and lt_status not in ["优质/值得等待", "价值陷阱"]:
                self.logs.append(f"[护城河] ROIC {format_percent(roic)} 优秀，资本效率高。")
                if lt_status == "中性": lt_status = "优质"

        # D. Alpha 信号 (8季度回溯)
        if earnings and isinstance(earnings, list):
            beats = 0
            total = 0
            for e in earnings:
                est = e.get("estimatedEarning")
                act = e.get("actualEarningResult")
                if est is not None and act is not None:
                    total += 1
                    if act > est: beats += 1
            
            if total >= 4:
                beat_rate = beats / total
                if beat_rate >= 0.85: # 允许偶尔一次失误
                    self.logs.append(f"[Alpha] 过去 {total} 季度中有 {beats} 次超预期，机构主力控盘稳健。")
                    if lt_status == "中性": lt_status = "动能强劲"
                elif beat_rate < 0.5:
                     self.logs.append(f"[风险] 业绩经常不及预期 (Win Rate {beat_rate:.0%})，需警惕雷。")

        # E. 流动性警报
        if m_cap and m_cap < 1e9: # 小于 1B
             self.flags.append("⚠️ **微盘股警告**: 市值 < $1B，数据波动大，模型准确度下降。")

        self.long_term_verdict = lt_status

        return {
            "price": price,
            "beta": beta,
            "market_regime": self.market_regime,
            "peg": peg,
            "m_cap": m_cap,
            "ev_ebitda": ev_ebitda, 
            "fcf_yield": fcf_yield,
            "roic": roic,
            "risk_var": self.risk_var,
            "growth_tier": growth_tier
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

@bot.tree.command(name="analyze", description="[v3.0] 估值分析 (科学辩证版)")
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

    # UI 精修: 极简黑
    embed = discord.Embed(
        title=f"估值分析: {ticker.upper()}",
        description=f"现价: ${data['price']} | 市值: {format_market_cap(data['m_cap'])} | 市场情绪: {model.market_regime}",
        color=0x2b2d31
    )

    # 1. 结论
    verdict_text = (
        f"短期: **{model.short_term_verdict}**\n"
        f"长期: **{model.long_term_verdict}**"
    )
    embed.add_field(name="估值结论", value=verdict_text, inline=False)

    # 2. 核心特征
    beta_val = data['beta']
    beta_desc = "低波动" if beta_val < 0.8 else ("高波动" if beta_val > 1.3 else "适中")
    peg_display = format_num(data['peg']) if data['peg'] else "N/A"
    
    core_factors = (
        f"**Beta:** {format_num(beta_val)} ({beta_desc})\n"
        f"**PEG:** {peg_display} ({data['growth_tier']} Growth)"
    )
    embed.add_field(name="核心特征", value=core_factors, inline=False)
    
    # 3. 风险量化 (新功能)
    if data['risk_var'] != "N/A":
        embed.add_field(name="95% VaR (月度风险)", value=f"最大回撤可能达 **{data['risk_var']}**", inline=False)

    # 4. 因子分析
    log_content = []
    if model.flags: log_content.extend(model.flags) # 警报置顶
    log_content.extend([f"- {log}" for log in model.logs])
    
    if log_content:
        log_str = "\n".join(log_content)
        embed.add_field(name="因子分析", value=f"```\n{log_str}\n```", inline=False)

    embed.set_footer(text="FMP Ultimate API • 机构级多因子模型 | 模型建议，仅作参考")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(DISCORD_TOKEN)
