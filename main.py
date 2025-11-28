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
    """从 FMP API 获取数据"""
    url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey={FMP_API_KEY}&{params}"
    safe_url = f"{BASE_URL}/{endpoint}?symbol={ticker}&apikey=***&{params}"
    try:
        logger.info(f"📡 Requesting: {safe_url}")
        response = requests.get(url, timeout=10)
        if response.status_code != 200: 
            logger.warning(f"FMP API returned status {response.status_code} for {endpoint}")
            return None
        data = response.json()
        # 针对部分端点返回列表的情况，取第一个元素
        if isinstance(data, list) and "historical" not in endpoint:
            return data[0] if len(data) > 0 else None
        return data
    except Exception as e:
        logger.error(f"Error fetching {endpoint}: {e}")
        return None

def get_earnings_data(ticker):
    """获取历史财报预期与实际数据"""
    url = f"{BASE_URL}/earnings?symbol={ticker}&apikey={FMP_API_KEY}&limit=40"
    try:
        response = requests.get(url, timeout=10)
        return response.json() if response.status_code == 200 else []
    except: 
        return []

def format_percent(num):
    """格式化为百分比"""
    return f"{num * 100:.2f}%" if num is not None and isinstance(num, (int, float)) else "N/A"

def format_num(num):
    """格式化为两位小数的数字"""
    return f"{num:.2f}" if num is not None and isinstance(num, (int, float)) else "N/A"

def format_market_cap(num):
    """格式化市值 (T, B, M)"""
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
    """根据行业获取平均 EV/EBITDA 倍数"""
    if not sector: return 18.0
    for key in SECTOR_EBITDA_MEDIAN:
        if key.lower() in str(sector).lower(): return SECTOR_EBITDA_MEDIAN[key]
    return 18.0

# --- 3. 估值判断模型 ---

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
        # 新增用于显示的调整后 FCF Yield
        self.adj_fcf_yield_display = "N/A"

    async def fetch_data(self):
        """异步获取所有 FMP 数据 (新增 Cash Flow Statement)"""
        logger.info(f"--- Starting Analysis for {self.ticker} ---")
        loop = asyncio.get_event_loop()
        tasks = {
            "profile": loop.run_in_executor(None, get_fmp_data, "profile", self.ticker, ""),
            "quote": loop.run_in_executor(None, get_fmp_data, "quote", self.ticker, ""),
            "metrics": loop.run_in_executor(None, get_fmp_data, "key-metrics-ttm", self.ticker, ""),
            "ratios": loop.run_in_executor(None, get_fmp_data, "ratios-ttm", self.ticker, ""),
            "bs": loop.run_in_executor(None, get_fmp_data, "balance-sheet-statement", self.ticker, "limit=1"),
            # **新增 Cash Flow Statement**
            "cf": loop.run_in_executor(None, get_fmp_data, "cash-flow-statement", self.ticker, "limit=1"), 
            "vix": loop.run_in_executor(None, get_fmp_data, "quote", "^VIX", ""),
            "earnings": loop.run_in_executor(None, get_earnings_data, self.ticker)
        }
        results = await asyncio.gather(*tasks.values())
        self.data = dict(zip(tasks.keys(), results))
        return self.data["profile"] is not None and self.data["quote"] is not None

    def analyze(self):
        """核心估值分析逻辑"""
        p = self.data.get("profile", {}) or {}
        q = self.data.get("quote", {}) or {}
        m = self.data.get("metrics", {}) or {} 
        r = self.data.get("ratios", {}) or {}
        vix_data = self.data.get("vix", {}) or {}
        earnings = self.data.get("earnings", []) or {}
        cf = self.data.get("cf", {}) or {} # 新增 Cash Flow Statement 数据
        
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
        fcf_yield_api = m.get("freeCashFlowYield") or m.get("freeCashFlowYieldTTM") # 原始 API FCF Yield
        
        roic = m.get("returnOnInvestedCapital") or m.get("returnOnInvestedCapitalTTM")
        net_margin = r.get("netProfitMarginTTM")
        ps_ratio = r.get("priceToSalesRatioTTM")
        
        # PEG 计算 (略 - 保持不变)
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
        
        # --- Adjusted FCF Yield (方案二核心) ---
        adj_fcf_yield = None
        cfo = cf.get("cashFlowFromOperations")
        dep_amort = cf.get("depreciationAndAmortization")
        
        if cfo is not None and dep_amort is not None and m_cap and m_cap > 0:
            # 估算维护性 CapEx 为 D&A 的 50%
            MAINTENANCE_CAPEX_RATIO = 0.5 
            maintenance_capex = dep_amort * MAINTENANCE_CAPEX_RATIO
            adj_fcf = cfo - maintenance_capex
            adj_fcf_yield = adj_fcf / m_cap
            self.adj_fcf_yield_display = format_percent(adj_fcf_yield)
        
        # 使用 Adjusted FCF Yield 进行判断，如果 Adjusted FCF Yield 不可用，则回退到原始 API 数据
        fcf_yield_used = adj_fcf_yield if adj_fcf_yield is not None else fcf_yield_api

        # VIX/风险/Meme模型 (保持不变)
        vix = vix_data.get("price", 20)
        if vix < 20: self.market_regime = f"平静 (VIX {vix:.1f})"
        elif vix < 30: self.market_regime = f"震荡 (VIX {vix:.1f})"
        else: self.market_regime = f"恐慌 (VIX {vix:.1f})"

        if price and beta and vix:
            monthly_risk_pct = (vix / 100) * beta * 1.0 * 100
            self.risk_var = f"-{monthly_risk_pct:.1f}%"

        # ... (Meme/信仰值模型 保持不变) ...
        meme_score = 0
        
        # 计分逻辑...
        if price and price_200ma:
            if price > price_200ma * 1.4: meme_score += 2
            elif price > price_200ma * 1.15: meme_score += 1
            
        if (ps_ratio and ps_ratio > 20) or (ev_ebitda and ev_ebitda > 80): 
            meme_score += 4
        elif (ps_ratio and ps_ratio > 10) or (ev_ebitda and ev_ebitda > 40): 
            meme_score += 2
        elif (ps_ratio and ps_ratio > 8) or (ev_ebitda and ev_ebitda > 30): 
            meme_score += 1
            
        if beta > 2.0: meme_score += 2
        elif beta > 1.3: meme_score += 1
            
        if price and price_200ma and price > price_200ma:
            bad_fcf = (fcf_yield_api is not None and fcf_yield_api < 0.01) # 注意：这里沿用 API FCF 判断极端扭曲
            bad_peg = (peg is not None and (peg < 0 or peg > 4.0))
            if bad_fcf or bad_peg:
                meme_score += 2
            
        if vol_today and vol_avg and vol_avg > 0:
            if vol_today > vol_avg * 1.2: meme_score += 1
        
        if roic and roic > 0.20:
            if peg and 0 < peg < 3.0: meme_score -= 3
            else: meme_score -= 1
        
        meme_score = max(0, min(10, meme_score))
        meme_pct = int(meme_score * 10)
        is_faith_mode = meme_pct >= 50

        sector_avg = get_sector_benchmark(sector)
        st_status = "估值合理"
        
        # --- 短期估值逻辑 (保持不变) ---
        is_distressed = False
        if (net_margin is not None and net_margin < -0.05) or (fcf_yield_api is not None and fcf_yield_api < -0.02):
            is_distressed = True
            st_status = "极其昂贵"
            self.logs.append(f"[预警] 净利率或原始 FCF 为负，EV/EBITDA 指标已失效。")
        
        if not is_distressed:
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

        # --- 长期估值 (使用 Adjusted FCF Yield) ---
        lt_status = "中性"
        is_value_trap = False

        if net_margin is not None and net_margin < 0 and price_200ma and price < price_200ma:
            is_value_trap = True
            lt_status = "风险极大"
            st_status = "下跌趋势"
            self.logs.append(f"[风险] 公司长期亏损且股价位于年线下方，看似低估实为“价值陷阱”。")
            self.strategy = "趋势与基本面双弱，存在‘接飞刀’的风险"
        
        if not is_value_trap:
            # --- 信仰模式 (Meme Score 50%+) 的细化分析 (保持不变) ---
            if is_faith_mode:
                if 50 <= meme_pct < 60:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。市场关注度提升，资金动量正在影响短期价格走势。"
                    meme_strategy = "价格波动性可能增加，交易决策可以结合市场动量指标。"
                elif 60 <= meme_pct < 70:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪高度活跃，体现出显著的**资金共识**和高流动性。"
                    meme_strategy = "较高的关注度和交易量反映了市场的积极情绪，但应注意伴随的高波动性。"
                elif 70 <= meme_pct < 80:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。资金聚焦度极高，公司获得大量**关注溢价**，价格驱动力强劲。"
                    meme_strategy = "估值中已包含极高的未来预期，投资行为应考虑资金潮退却的潜在风险。"
                elif 80 <= meme_pct < 90:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪已进入非理性繁荣区间，价格体现出**极致的资金动能**。"
                    meme_strategy = "此时价格驱动因素主要为情绪和资金流，应极为谨慎评估其风险收益比。"
                elif meme_pct >= 90:
                    meme_log = f"[信仰] Meme值 {meme_pct}%。市场情绪处于顶峰，反映出**极强的短期向上动量**。"
                    meme_strategy = "市场波动和回调风险已处于历史高位，对于中长期投资者而言，保持警惕性至关重要。"

                self.logs.insert(0, meme_log)
                if "昂贵" in st_status: st_status += " / 资金动量"
                if "昂贵" in lt_status: lt_status = "高溢价 (资金动量)"
                
                if self.strategy == "数据不足":
                    self.strategy = meme_strategy

            # --- 长期估值判断逻辑 (使用 fcf_yield_used) ---
            if fcf_yield_used is not None:
                # 优先使用调整后的 FCF Yield
                fcf_str = self.adj_fcf_yield_display if adj_fcf_yield is not None else format_percent(fcf_yield_used)
                
                # 只有当 Adjusted FCF Yield 可用时，才进行 CapEx 投资修正的日志记录
                if adj_fcf_yield is not None:
                    # 报告 Adjustment 的效果
                    if adj_fcf_yield > fcf_yield_api:
                        self.logs.append(f"[价值修正] Adjusted FCF Yield ({fcf_str}) 高于 API 原始值 ({format_percent(fcf_yield_api)})，反映出**增长性资本支出**的积极影响。")
                    
                    if adj_fcf_yield < 0.02 and "高速" in growth_desc:
                         self.logs.append(f"[增长性 CapEx] Adjusted FCF Yield ({fcf_str}) 仍较低，表明公司持续将现金流大量投入高增长项目。")

                
                if fcf_yield_used > 0.04 and not is_faith_mode:
                    lt_status = "便宜"
                    self.logs.append(f"[价值] FCF Yield ({fcf_str}) 丰厚，提供良好安全垫。")
                    if self.strategy == "数据不足": self.strategy = "当前价格具备较好的安全边际，存在价值投资的可能。"
                
                elif fcf_yield_used < 0.02 and not is_faith_mode:
                    lt_status = "昂贵"
                    if "高速" in growth_desc:
                         self.logs.append(f"[价值] FCF Yield ({fcf_str}) 较低，当前估值高度依赖未来高增长兑现。")
                         if self.strategy == "数据不足": self.strategy = "估值包含较高增长预期，股价波动可能随业绩剧烈放大，需要警惕。"
                    else:
                        self.logs.append(f"[价值] FCF Yield ({fcf_str}) 极低且无明显增长支撑，隐含预期过高，风险较大。")
                        if self.strategy == "数据不足": self.strategy = "风险收益比不佳，当前估值缺乏基本面支撑，应审慎。"
                
                elif fcf_yield_used < 0.025 and roic and roic > 0.20:
                    # 对于高 ROIC 公司，即使调整后 FCF Yield 仍然中等偏低，也属于“优质溢价”
                    if not is_faith_mode:
                        lt_status = "优质/值得等待"
                        if self.strategy == "数据不足": self.strategy = "此类高效率资产的低 FCF Yield 是资本扩张所致，适合长期配置者择机分批建仓。"
                    self.logs.append(f"[辩证] FCF Yield ({fcf_str}) 虽低，但 ROIC ({format_percent(roic)}) 极高，属于'优质溢价'或高效率增长投资。")


            if roic and roic > 0.15 and "昂贵" not in lt_status and not is_value_trap:
                self.logs.append(f"[护城河] ROIC ({format_percent(roic)}) 优秀，资本效率高。")
                if lt_status == "中性": lt_status = "优质"
            
            if fcf_yield_used is None:
                if not is_faith_mode: self.strategy = "当前数据不足以形成明确的估值倾向。"

            # D. Alpha 信号 (保持不变)
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
                
                if beat_rate >= 0.75:
                    self.logs.append(f"[Alpha] 过去 {total} 季度中有 {beats} 次业绩超预期，机构情绪乐观。")
                else:
                    self.logs.append(f"[Alpha] 过去 {total} 季度中有 {total - beats} 次业绩不及预期，需警惕。")
            else:
                self.logs.append(f"[Alpha] 暂无有效历史财报数据，无法判断业绩趋势。")

            # --- 策略修正层 (保持不变) ---
            if pe and pe < 8 and rev_growth and rev_growth < -0.05 and "风险" not in lt_status:
                self.strategy = "估值看似极低，但营收处于萎缩周期，需要警惕‘低估值陷阱’。"
                lt_status = "周期性风险"
                self.logs.append(f"[陷阱] PE ({format_num(pe)}) 虽低，但营收负增长 ({format_percent(rev_growth)})，疑似周期顶部信号。")

            elif beta and beta < 0.6 and fcf_yield_used and fcf_yield_used > 0.03 and "陷阱" not in self.strategy:
                self.strategy = "低波动防御性资产，可视为市场震荡环境下的潜在避险配置。"
                lt_status = "防御/收息"
                self.logs.append(f"[防御] Beta ({format_num(beta)}) 极低且现金流健康，具备类似债券的特征。")

            if net_margin and net_margin < 0:
                if len(recent) >= 3:
                    beats_check = sum(1 for x in recent if x["act"] > x["est"])
                    if beats_check / len(recent) >= 0.75:
                        self.strategy = "基本面虽处于亏损，但业绩连续超预期，可关注‘困境反转’的可能性。"
                        lt_status = "观察/反转"
                        self.logs.append(f"[反转] 尽管年度亏损，但近期业绩强劲，基本面可能有边际改善的信号。")

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

@bot.tree.command(name="analyze", description="估值分析")
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

    # [排版] 标题
    embed = discord.Embed(
        title=f"估值分析: {ticker.upper()}",
        description=f"现价: ${data['price']:.2f} | 市值: {format_market_cap(data['m_cap'])}",
        color=0x2b2d31
    )

    # [排版] 估值结论：引用块 >
    verdict_text = (
        f"> **短期:** {model.short_term_verdict}\n"
        f"> **长期:** {model.long_term_verdict}"
    )
    embed.add_field(name="估值结论", value=verdict_text, inline=False)

    # [排版] 核心数据：每一行使用 Quote Block
    beta_val = data['beta']
    beta_desc = "低波动" if beta_val < 0.8 else ("高波动" if beta_val > 1.3 else "适中")
    peg_display = format_num(data['peg']) if data['peg'] is not None else "N/A"
    
    meme_pct = data['meme_pct']
    meme_desc = "低关注度"
    if meme_pct >= 80: meme_desc = "资金狂热"
    elif meme_pct >= 60: meme_desc = "高流动性"
    elif meme_pct >= 30: meme_desc = "市场关注"
    
    # 将调整后的 FCF Yield 纳入核心特征显示（如果没有则显示 N/A）
    adj_fcf_display = model.adj_fcf_yield_display
    
    core_factors = (
        f"> **Beta:** `{format_num(beta_val)}` ({beta_desc})\n"
        f"> **PEG:** `{peg_display}` ({data['growth_desc']})\n"
        f"> **Adj FCF Yield:** `{adj_fcf_display}` (已修正 CapEx)\n"
        f"> **Meme值:** `{meme_pct}%` ({meme_desc})"
    )
    embed.add_field(name="核心特征", value=core_factors, inline=False)
    
    # [排版] Risk 字段
    if data['risk_var'] != "N/A":
        embed.add_field(
            name="95% VaR (月度风险)", 
            value=f"> 最大回撤可能在 **{data['risk_var']}** 附近", 
            inline=False
        )

    # [排版] 因子分析
    log_content = []
    if model.flags: log_content.extend(model.flags) 
    log_content.extend([f"{log}" for log in model.logs])
    
    formatted_logs = []
    for log in log_content:
        if log.startswith("[") and "]" in log:
            tag_end = log.find("]") + 1
            tag = log[:tag_end]
            content = log[tag_end:]
            formatted_logs.append(f"> **{tag}**{content}")
        else:
            formatted_logs.append(f"> {log}")

    factor_str = "\n> \n".join(formatted_logs)
    strategy_text = f"**[策略]** {model.strategy}"
    full_log_str = f"{factor_str}\n\n{strategy_text}"
    
    if len(full_log_str) > 1000: full_log_str = full_log_str[:990] + "..."

    embed.add_field(name="因子分析", value=full_log_str, inline=False)

    embed.set_footer(text="FMP Ultimate API • 机构级多因子模型 | 模型建议，仅作参考，不构成投资建议")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable not set.")
    else:
        if not FMP_API_KEY:
             logger.error("FMP_API_KEY environment variable not set. FMP data fetching will fail.")
        bot.run(DISCORD_TOKEN)
