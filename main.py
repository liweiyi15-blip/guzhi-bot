import discord
from discord import app_commands
from discord.ext import commands
import requests
import os
import asyncio
import logging
from dotenv import load_dotenv
from datetime import datetime

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
    except Exception as e:
        logger.error(f"Error fetching {endpoint}: {e}")
        return None

def get_earnings_data(ticker):
    url = f"{BASE_URL}/earnings?symbol={ticker}&apikey={FMP_API_KEY}&limit=40"
    try:
        response = requests.get(url, timeout=10)
        return response.json() if response.status_code == 200 else []
    except: return []

def format_percent(num):
    return f"{num * 100:.2f}%" if num is not None else "N/A"

def format_num(num):
    return f"{num:.2f}" if num is not None else "N/A"

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
    if not sector: return 18.0
    for key in SECTOR_EBITDA_MEDIAN:
        if key.lower() in str(sector).lower(): return SECTOR_EBITDA_MEDIAN[key]
    return 18.0

# --- 3. 估值判断模型 (v6.2 Final Logic) ---

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
            "earnings": loop.run_in_executor(None, get_earnings_data, self.ticker)
        }
        results = await asyncio.gather(*tasks.values())
        self.data = dict(zip(tasks.keys(), results))
        return self.data["profile"] is not None and self.data["quote"] is not None

    def analyze(self):
        p = self.data.get("profile", {}) or {}
        q = self.data.get("quote", {}) or {}
        m = self.data.get("metrics", {}) or {} 
        r = self.data.get("ratios", {}) or {}
        vix_data = self.data.get("vix", {}) or {}
        earnings = self.data.get("earnings", []) or []
        
        if not p or not q: return None

        price = q.get("price")
        price_200ma = q.get("priceAvg200")
        vol_today = q.get("volume")
        vol_avg = q.get("avgVolume")
        sector = p.get("sector", "Unknown")
        beta = p.get("beta")
        if beta is None: beta = 1.0 
        
        m_cap = q.get("marketCap") or m.get("marketCap") or p.get("mktCap", 0)
        ev_ebitda = m.get("evToEBITDA") or m.get("enterpriseValueOverEBITDATTM") or r.get("enterpriseValueMultipleTTM")
        fcf_yield = m.get("freeCashFlowYield") or m.get("freeCashFlowYieldTTM")
        roic = m.get("returnOnInvestedCapital") or m.get("returnOnInvestedCapitalTTM")
        net_margin = r.get("netProfitMarginTTM")
        ps_ratio = r.get("priceToSalesRatioTTM")
        
        # PEG 计算
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

        growth_list = [x for x in [rev_growth, ni_growth, implied_growth] if x is not None]
        max_growth = max(growth_list) if growth_list else 0
        
        growth_desc = "低成长"
        if max_growth > 0.5: growth_desc = "超高速"
        elif max_growth > 0.2: growth_desc = "高速"
        elif max_growth > 0.05: growth_desc = "稳健"
        if peg and peg > 3.0: growth_desc = "高预期"

        # VIX 分析
        vix = vix_data.get("price", 20)
        if vix < 20: self.market_regime = f"平静 (VIX {vix:.1f})"
        elif vix < 30: self.market_regime = f"震荡 (VIX {vix:.1f})"
        else: self.market_regime = f"恐慌 (VIX {vix:.1f})"

        # 风险计算
        if price and beta and vix:
            monthly_risk_pct = (vix / 100) * beta * 1.0 * 100
            self.risk_var = f"-{monthly_risk_pct:.1f}%"

        # --- Meme/信仰值模型 (v6.2 Dynamic Labels) ---
        meme_score = 0
        
        # 1. 价格趋势 (FOMO): 偏离年线 (Max 2)
        if price and price_200ma:
            if price > price_200ma * 1.4: meme_score += 2
            elif price > price_200ma * 1.15: meme_score += 1
            
        # 2. 极致估值 (Hype): 区分普通贵和离谱贵 (Max 4)
        if (ps_ratio and ps_ratio > 20) or (ev_ebitda and ev_ebitda > 80): 
            meme_score += 4
        elif (ps_ratio and ps_ratio > 10) or (ev_ebitda and ev_ebitda > 40): 
            meme_score += 2
        elif (ps_ratio and ps_ratio > 8) or (ev_ebitda and ev_ebitda > 30): 
            meme_score += 1
            
        # 3. 波动率 (Action): (Max 2)
        if beta > 2.0: meme_score += 2
        elif beta > 1.3: meme_score += 1
            
        # 4. 现实扭曲因子 (Distortion): 越烂越涨 (Max 2)
        # 股价在高位但基本面崩坏
        if price and price_200ma and price > price_200ma:
            bad_fcf = (fcf_yield is not None and fcf_yield < 0.01)
            bad_peg = (peg is not None and (peg < 0 or peg > 4.0))
            if bad_fcf or bad_peg:
                meme_score += 2
            
        # 5. 人群聚集 (Crowd): 放量 (Max 1)
        if vol_today and vol_avg and vol_avg > 0:
            if vol_today > vol_avg * 1.2: meme_score += 1
        
        meme_score = min(10, meme_score)
        meme_pct = int(meme_score * 10)
        is_faith_mode = meme_pct >= 60

        sector_avg = get_sector_benchmark(sector)
        st_status = "估值合理"
        
        # 短期估值逻辑
        if ev_ebitda is not None:
            ratio = ev_ebitda / sector_avg
            if ("高速" in growth_desc or "预期" in growth_desc) and (peg is not None and 0 < peg < 1.5):
                st_status = "便宜 (高成长)"
                self.logs.append(f"[成长特权] 虽 EV/EBITDA ({format_num(ev_ebitda)}) 偏高，但 PEG ({format_num(peg)}) 极低，属于越涨越便宜。")
            elif ratio < 0.7:
                st_status = "便宜"
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 低于行业均值 ({sector_avg})，折扣明显。")
            elif ratio > 1.3:
                if ("高速" in growth_desc or "预期" in growth_desc) and (peg is not None and 0 < peg < 2.0):
                     st_status = "合理溢价"
                     self.logs.append(f"[成长特权] 高估值 ({format_num(ev_ebitda)}) 被高增长消化，溢价合理。")
                else:
                    st_status = "昂贵"
                    self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 远高于行业均值 ({sector_avg})，且缺乏增长支撑。")
            else:
                st_status = "估值合理"
                self.logs.append(f"[板块] EV/EBITDA ({format_num(ev_ebitda)}) 与行业均值 ({sector_avg}) 接近，估值处于合理区间。")
        else:
             self.logs.append(f"[板块] 缺少 EV/EBITDA 数据，无法对比。")
        
        self.short_term_verdict = st_status

        # --- 长期估值 ---
        lt_status = "中性"
        is_value_trap = False

        if net_margin is not None and net_margin < 0 and price_200ma and price < price_200ma:
            is_value_trap = True
            lt_status = "风险极大"
            st_status = "下跌趋势"
            self.logs.append(f"[风险] 公司长期亏损且股价位于年线下方，看似低估实为“价值陷阱”。")
            self.strategy = "趋势与基本面双弱，需警惕'接飞刀'风险"
        
        if not is_value_trap:
            if is_faith_mode:
                self.logs.insert(0, f"[信仰] Meme值 {meme_pct}%。股价脱离基本面引力，进入“纯资金博弈”模式。")
                if "昂贵" in st_status: st_status += " / 资金博弈"
                if "昂贵" in lt_status: lt_status = "高溢价 (信仰支撑)"
                self.strategy = "基本面内含极高预期，但资金动量主导短期走势。顺势交易需严设止损。"

            if fcf_yield is not None:
                fcf_str = format_percent(fcf_yield)
                if fcf_yield < 0.025 and roic and roic > 0.20:
                    if not is_faith_mode:
                        lt_status = "优质/值得等待"
                        self.strategy = "此类资产通常不会便宜，适合分批配置或等待回调。"
                    self.logs.append(f"[辩证] FCF Yield ({fcf_str}) 虽低，但 ROIC ({format_percent(roic)}) 极高，属于'优质溢价'。")
                elif fcf_yield > 0.04:
                    lt_status = "便宜"
                    self.logs.append(f"[价值] FCF Yield ({fcf_str}) 丰厚，提供良好安全垫。")
                    if not is_faith_mode: self.strategy = "当前价格具备较好的安全边际。"
                elif fcf_yield < 0.02:
                    if not is_faith_mode: lt_status = "昂贵"
                    if "高速" in growth_desc:
                         self.logs.append(f"[价值] FCF Yield ({fcf_str}) 较低，当前估值高度依赖未来高增长兑现。")
                         if not is_faith_mode: self.strategy = "估值包含较高增长预期，股价波动可能随业绩剧烈放大。"
                    else:
                        self.logs.append(f"[价值] FCF Yield ({fcf_str}) 极低且无增长，隐含预期过高，风险较大。")
                        if not is_faith_mode: self.strategy = "风险收益比不佳，当前估值缺乏基本面支撑。"
            
                if roic and roic > 0.15 and "昂贵" not in lt_status and not is_value_trap:
                    self.logs.append(f"[护城河] ROIC ({format_percent(roic)}) 优秀，资本效率高。")
                    if lt_status == "中性": lt_status = "优质"
            
            if fcf_yield is None:
                if not is_faith_mode: self.strategy = "当前数据不足以形成明确的估值倾向。"

        # D. Alpha 信号 (v5.8 Time-Aware)
        valid_earnings = []
        today_str = datetime.now().strftime("%Y-%m-%d")

        if isinstance(earnings, list):
            for e in earnings:
                est = e.get("epsEstimated")
                act = e.get("epsActual")
                date = e.get("date")
                if est is not None and act is not None and date is not None:
                    if date < today_str:
                        valid_earnings.append({"est": est, "act": act, "date": date})
        
        valid_earnings.sort(key=lambda x: x["date"], reverse=True)
        recent = valid_earnings[:4]
        
        if len(recent) > 0:
            beats = sum(1 for x in recent if x["act"] > x["est"])
            total = len(recent)
            beat_rate = beats / total
            last_report_date = recent[0]['date']
            
            if beat_rate >= 0.75:
                self.logs.append(f"[Alpha] 截至 {last_report_date}，过去 {total} 季度中有 {beats} 次业绩超预期，机构情绪乐观。")
            else:
                self.logs.append(f"[Alpha] 截至 {last_report_date}，过去 {total} 季度中有 {total - beats} 次业绩不及预期，需警惕。")
        else:
            self.logs.append(f"[Alpha] 暂无有效历史财报数据，无法判断业绩趋势。")

        # --- 策略修正层 (v6.0 Strategy) ---
        if pe and pe < 8 and rev_growth and rev_growth < -0.05 and "风险" not in lt_status:
            self.strategy = "看似估值极低，但营收处于萎缩周期，需警惕'低估值陷阱'。"
            lt_status = "周期性风险"
            self.logs.append(f"[陷阱] PE ({format_num(pe)}) 虽低，但营收负增长 ({format_percent(rev_growth)})，疑似周期顶部。")

        elif beta and beta < 0.6 and fcf_yield and fcf_yield > 0.03 and "陷阱" not in self.strategy:
            self.strategy = "低波动防御性资产，适合作为市场震荡时的避险配置。"
            lt_status = "防御/收息"
            self.logs.append(f"[防御] Beta ({format_num(beta)}) 极低且现金流健康，具备债性特征。")

        if net_margin and net_margin < 0:
            if len(recent) >= 3:
                beats_check = sum(1 for x in recent if x["act"] > x["est"])
                if beats_check / len(recent) >= 0.75:
                    self.strategy = "基本面虽处于亏损，但业绩连续超预期，关注'困境反转'机会。"
                    lt_status = "观察/反转"
                    self.logs.append(f"[反转] 尽管年度亏损，但近期业绩强劲，基本面可能有边际改善。")

        self.long_term_verdict = lt_status

        return {
            "price": price,
            "beta": beta,
            "market_regime": self.market_regime,
            "peg": peg,
            "m_cap": m_cap,
            "growth_desc": growth_desc,
            "risk_var": self.risk_var,
            "meme_pct": meme_pct 
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

@bot.tree.command(name="analyze", description="[v6.2] 估值分析 (Meme分级优化版)")
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

    embed = discord.Embed(
        title=f"估值分析: {ticker.upper()}",
        description=f"现价: ${data['price']:.2f} | 市值: {format_market_cap(data['m_cap'])}",
        color=0x2b2d31
    )

    verdict_text = (
        f"短期: **{model.short_term_verdict}**\n"
        f"长期: **{model.long_term_verdict}**"
    )
    embed.add_field(name="估值结论", value=verdict_text, inline=False)

    beta_val = data['beta']
    beta_desc = "低波动" if beta_val < 0.8 else ("高波动" if beta_val > 1.3 else "适中")
    peg_display = format_num(data['peg']) if data['peg'] is not None else "N/A"
    
    # [核心修改] 动态信仰评级
    meme_pct = data['meme_pct']
    meme_desc = "冷门资产"
    if meme_pct >= 80: meme_desc = "狂热宗教"
    elif meme_pct >= 60: meme_desc = "散户信仰"
    elif meme_pct >= 30: meme_desc = "机构共识"
    
    core_factors = (
        f"**Beta:** {format_num(beta_val)} ({beta_desc})\n"
        f"**PEG:** {peg_display} ({data['growth_desc']})\n"
        f"**Meme值:** {meme_pct}% ({meme_desc})"
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
        if len(log_str) > 1000: log_str = log_str[:990] + "..."
        embed.add_field(name="因子分析", value=f"```\n{log_str}\n```", inline=False)

    embed.set_footer(text="FMP Ultimate API • 机构级多因子模型 | 模型建议，仅作参考")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(DISCORD_TOKEN)
