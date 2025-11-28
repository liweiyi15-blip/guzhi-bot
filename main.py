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
V3_URL = "https://financialmodelingprep.com/api/v3"

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
        if response.status_code != 200: return None
        data = response.json()
        if isinstance(data, list) and "historical" not in endpoint:
            return data[0] if len(data) > 0 else None
        return data
    except: return None

def get_fmp_list_data(endpoint, ticker, limit=4):
    url = f"{BASE_URL}/{endpoint}/{ticker}?apikey={FMP_API_KEY}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200: return []
        return response.json()
    except: return []

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

# --- 2. 行业基准 ---
SECTOR_EBITDA_MEDIAN = {
    "Technology": 32.0, "Consumer Electronics": 25.0, "Communication Services": 20.0,
    "Healthcare": 18.0, "Financial Services": 12.0, "Energy": 10.0,
    "Utilities": 12.0, "Unknown": 18.0
}

def get_sector_benchmark(sector):
    for key in SECTOR_EBITDA_MEDIAN:
        if key in str(sector): return SECTOR_EBITDA_MEDIAN[key]
    return 18.0

# --- 3. 估值判断模型 (v4.4) ---

class ValuationModel:
    def __init__(self, ticker):
        self.ticker = ticker.upper()
        self.data = {}
        self.short_term_verdict = "未知"
        self.long_term_verdict = "未知"
        self.market_regime = "未知"
        self.risk_var = "N/A" 
        self.logs = [] 
        self.flags = [] 
        self.strategy = "数据不足" 

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
            "earnings": loop.run_in_executor(None, get_fmp_list_data, "earnings-surprises", self.ticker, 8)
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
        raw_earnings = self.data.get("earnings", []) or []
        
        if not p or not q: return None

        price = q.get("price")
        price_200ma = q.get("priceAvg200")
        sector = p.get("sector", "Unknown")
        beta = p.get("beta", 1.0)
        
        m_cap = q.get("marketCap") or m.get("marketCap") or p.get("mktCap", 0)
        ev_ebitda = m.get("evToEBITDA") or m.get("enterpriseValueOverEBITDATTM") or r.get("enterpriseValueMultipleTTM")
        fcf_yield = m.get("freeCashFlowYield") or m.get("freeCashFlowYieldTTM")
        roic = m.get("returnOnInvestedCapital") or m.get("returnOnInvestedCapitalTTM")
        net_margin = r.get("netProfitMarginTTM")
        
        # PEG
        peg = r.get("priceToEarningsGrowthRatioTTM") or r.get("pegRatioTTM")
        pe = r.get("priceEarningsRatioTTM") or m.get("peRatioTTM")
        ni_growth = m.get("netIncomeGrowthTTM")
        rev_growth = m.get("revenueGrowthTTM")

        if peg is None and pe and ni_growth and ni_growth > 0:
            try: peg = pe / (ni_growth * 100)
            except: pass

        implied_growth = 0
        if peg and pe and peg > 0:
            implied_growth = (pe / peg) / 100.0

        max_growth = max(filter(None, [rev_growth, ni_growth, implied_growth])) if any([rev_growth, ni_growth, implied_growth]) else 0
        
        growth_desc = "低成长"
        if max_growth > 0.5: growth_desc = "超高速"
        elif max_growth > 0.2: growth_desc = "高速"
        elif max_growth > 0.05: growth_desc = "稳健"
        
        # PEG > 3.0 标记为高预期/动量
        if peg and peg > 3.0: growth_desc = "高预期/动量"

        vix = vix_data.get("price", 20)
        if vix < 20: self.market_regime = f"平静 (VIX {vix:.1f})"
        elif vix < 30: self.market_regime = f"震荡 (VIX {vix:.1f})"
        else: self.market_regime = f"恐慌 (VIX {vix:.1f})"

        if price and beta and vix:
            monthly_risk_pct = (vix / 100) * beta * 1.0 * 100
            self.risk_var = f"-{monthly_risk_pct:.1f}%"

        # =========================================================
        # 🔥 信仰/脱锚检测
        # =========================================================
        faith_score = 0
        if (ev_ebitda and ev_ebitda > 80) or (pe and pe > 100): faith_score += 3
        if price and price_200ma and price > price_200ma * 1.3: faith_score += 3
        if beta and beta > 1.5: faith_score += 2

        is_faith_mode = faith_score >= 5
        # =========================================================

        sector_avg = get_sector_benchmark(sector)
        st_status = "估值合理"
        
        if ev_ebitda:
            ratio = ev_ebitda / sector_avg
            if ("高速" in growth_desc or "动量" in growth_desc) and peg and peg < 1.5:
                st_status = "便宜 (高成长)"
                self.logs.append(f"[成长特权] 虽 EV/EBITDA ({format_num(ev_ebitda)}) 偏高，但 PEG ({format_num(peg)}) 极低，属于越涨越便宜。")
            elif ratio < 0.7:
                st_status = "便宜"
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 低于行业均值 ({sector_avg})，折扣明显。")
            elif ratio > 1.3:
                if ("高速" in growth_desc or "动量" in growth_desc) and peg and peg < 2.0:
                     st_status = "合理溢价"
                     self.logs.append(f"[成长特权] 高估值 ({format_num(ev_ebitda)}) 被高增长消化，溢价合理。")
                else:
                    st_status = "昂贵"
                    self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 远高于行业均值 ({sector_avg})，且缺乏增长支撑。")
            else:
                st_status = "估值合理"
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 与行业均值 ({sector_avg}) 接近，估值处于合理区间。")
        else:
             self.logs.append(f"[数据] 缺少 EV/EBITDA 数据，无法进行板块对比。")
        
        self.short_term_verdict = st_status

        # --- 3. 长期估值与策略 ---
        lt_status = "中性"
        is_value_trap = False

        if net_margin and net_margin < 0 and price_200ma and price < price_200ma:
            is_value_trap = True
            lt_status = "风险极大"
            st_status = "下跌趋势"
            self.logs.append(f"[风险] 公司长期亏损且股价位于年线下方，看似低估实为“价值陷阱”。")
            self.strategy = "趋势与基本面双弱，需警惕'接飞刀'风险"
        
        if not is_value_trap:
            # 信仰模式：这里是你要求的修改点
            if is_faith_mode:
                self.logs.insert(0, f"[信仰] 股价强势运行于年线之上，散户狂热叠加机构抱团，做多情绪已凝聚成强烈的“资金共识”。")
                if "昂贵" in st_status:
                    st_status += " / 资金博弈"
                if "昂贵" in lt_status:
                    lt_status = "高溢价 (信仰支撑)"
                
                self.strategy = "基本面包含极高预期，但资金动量主导短期走势。顺势交易需严设止损。"

            if fcf_yield:
                # A: 优质溢价
                if fcf_yield < 0.025 and roic and roic > 0.20:
                    if not is_faith_mode:
                        lt_status = "优质/值得等待"
                        self.strategy = "此类资产通常不会便宜，适合分批配置或等待回调。"
                    self.logs.append(f"[辩证] FCF Yield 虽低，但 ROIC ({format_percent(roic)}) 极高，属于'优质溢价'。")
                
                # B: 便宜
                elif fcf_yield > 0.04:
                    lt_status = "便宜"
                    self.logs.append(f"[价值] FCF Yield {format_percent(fcf_yield)} 丰厚，提供良好安全垫。")
                    if not is_faith_mode: self.strategy = "当前价格具备较好的安全边际。"
                
                # C: 昂贵
                elif fcf_yield < 0.02:
                    if not is_faith_mode: lt_status = "昂贵"
                    if "高速" in growth_desc:
                         self.logs.append(f"[价值] FCF Yield 较低，当前估值高度依赖未来高增长兑现。")
                         if not is_faith_mode: self.strategy = "估值包含较高增长预期，股价波动可能随业绩剧烈放大。"
                    else:
                        self.logs.append(f"[价值] FCF Yield 极低且无增长，隐含预期过高，风险较大。")
                        if not is_faith_mode: self.strategy = "风险收益比不佳，当前估值缺乏基本面支撑。"
            
                if roic and roic > 0.15 and "昂贵" not in lt_status:
                    self.logs.append(f"[护城河] ROIC {format_percent(roic)} 优秀，资本效率高。")
                    if lt_status == "中性": lt_status = "优质"
            
            if not fcf_yield:
                if not is_faith_mode: self.strategy = "当前数据不足以形成明确的估值倾向。"

        if not is_value_trap and earnings and isinstance(earnings, list):
            valid_earnings = []
            for e in earnings:
                est = e.get("epsEstimated") or e.get("estimatedEarning")
                act = e.get("epsActual") or e.get("eps") or e.get("actualEarningResult")
                if est is not None and act is not None:
                    valid_earnings.append({"est": est, "act": act})
            
            recent = valid_earnings[:4]
            if len(recent) > 0:
                beats = sum(1 for x in recent if x["act"] > x["est"])
                total = len(recent)
                beat_rate = beats / total
                if beat_rate >= 0.75:
                    self.logs.append(f"[Alpha] 过去 {total} 季度中有 {beats} 次业绩超预期，机构情绪乐观。")

        self.long_term_verdict = lt_status

        return {
            "price": price,
            "beta": beta,
            "market_regime": self.market_regime,
            "peg": peg,
            "m_cap": m_cap,
            "growth_desc": growth_desc,
            "risk_var": self.risk_var
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

@bot.tree.command(name="analyze", description="[v4.4] 估值分析 (散户信仰版)")
@app_commands.describe(ticker="股票代码 (如 PLTR)")
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

    embed = discord.Embed(
        title=f"估值分析: {ticker.upper()}",
        description=f"现价: ${data['price']} | 市值: {format_market_cap(data['m_cap'])} | 市场情绪: {model.market_regime}",
        color=0x2b2d31
    )

    verdict_text = (
        f"短期: **{model.short_term_verdict}**\n"
        f"长期: **{model.long_term_verdict}**"
    )
    embed.add_field(name="估值结论", value=verdict_text, inline=False)

    beta_val = data['beta']
    beta_desc = "低波动" if beta_val < 0.8 else ("高波动" if beta_val > 1.3 else "适中")
    peg_display = format_num(data['peg']) if data['peg'] else "N/A"
    
    core_factors = (
        f"**Beta:** {format_num(beta_val)} ({beta_desc})\n"
        f"**PEG:** {peg_display} ({data['growth_desc']})"
    )
    embed.add_field(name="核心特征", value=core_factors, inline=False)
    
    if data['risk_var'] != "N/A":
        embed.add_field(name="95% VaR (月度风险)", value=f"最大回撤可能达 **{data['risk_var']}**", inline=False)

    log_content = []
    if model.flags: log_content.extend(model.flags) 
    log_content.extend([f"- {log}" for log in model.logs])
    log_content.append(f"\n- [策略] {model.strategy}") 

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
