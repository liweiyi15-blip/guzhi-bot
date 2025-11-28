import discord
from discord import app_commands
from discord.ext import commands
import requests
import os
import asyncio
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta

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

# --- 2. 行业基准 (PE Median) ---
SECTOR_PE_MEDIAN = {
    "Technology": 28.0, "Consumer Electronics": 22.0, "Communication Services": 18.0,
    "Healthcare": 25.0, "Financial Services": 10.0, "Energy": 8.0,
    "Utilities": 15.0, "Unknown": 18.0
}

def fetch_dynamic_sector_pe_benchmark(sector):
    if not sector or sector == "Unknown": return None
    
    today = datetime.now().strftime('%Y-%m-%d')
    # 查找过去7天的数据，确保抓取到最新值
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d') 
    
    # 使用 BASE_URL 和 historical-industry-pe 接口
    url = f"{BASE_URL}/historical-industry-pe?industry={sector}&from={seven_days_ago}&to={today}&apikey={FMP_API_KEY}"
    
    try:
        logger.info(f"📡 Requesting Sector PE Median for: {sector} (Range: {seven_days_ago} to {today})")
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data and data[0].get("pe"):
                median_value = data[0]["pe"]
                logger.info(f"✅ Dynamic PE Median for {sector}: {median_value:.2f}")
                return median_value
        logger.warning(f"⚠️ FMP returned no valid dynamic PE median data for {sector}.")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch dynamic PE median for {sector}. Error: {e}")
        return None

def get_sector_benchmark(sector, dynamic_median=None):
    if dynamic_median is not None:
        return dynamic_median
    
    # 使用硬编码回落
    if not sector: return 18.0
    for key, value in SECTOR_PE_MEDIAN.items():
        if key.lower() in str(sector).lower(): return value
    return 18.0

# --- 3. 估值判断模型 (v7.0.2 Meme Update) ---

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
        self.sector = "Unknown" 

    def get_meme_log_description(self, meme_pct):
        """根据 Meme 值百分比返回详细的日志描述。"""
        if meme_pct >= 90:
            return "股价完全脱离地心引力，进入“Meme 宇宙”模式。风险与回报都被放大至极限。"
        elif meme_pct >= 80:
            return "极端信仰：机构与散户的共识达到高潮，定价完全基于未来预期。市场已无基本面逻辑可言。"
        elif meme_pct >= 70:
            return "狂热资金流：资金流主导，波动性剧增。基本面已不再是股价的主要驱动力。"
        elif meme_pct >= 60:
            return "情绪高估：明显高估，情绪正在取代理性。任何负面消息都可能引发剧烈调整。"
        elif meme_pct >= 50:
            return "预期拉满：估值溢价显著，大量资金涌入。市场进入“追涨”阶段，需要警惕风险。"
        else: 
            return "股价由基本面和机构共识主导。"


    async def fetch_data(self):
        logger.info(f"--- Starting Analysis for {self.ticker} ---")
        loop = asyncio.get_event_loop()
        
        # 步骤 1 & 2: 获取 profile, quote, sector, PE median
        tasks = {
            "profile": loop.run_in_executor(None, get_fmp_data, "profile", self.ticker, ""),
            "quote": loop.run_in_executor(None, get_fmp_data, "quote", self.ticker, ""),
        }
        results = await asyncio.gather(*tasks.values())
        self.data.update(dict(zip(tasks.keys(), results)))
        
        if self.data["profile"]:
            self.sector = self.data["profile"].get("sector", "Unknown")

        median_task = loop.run_in_executor(None, fetch_dynamic_sector_pe_benchmark, self.sector)
        dynamic_median = await median_task
        self.data["sector_median"] = dynamic_median 
        
        # 步骤 3: 获取剩余数据
        tasks = {
            "metrics": loop.run_in_executor(None, get_fmp_data, "key-metrics-ttm", self.ticker, ""),
            "ratios": loop.run_in_executor(None, get_fmp_data, "ratios-ttm", self.ticker, ""),
            "bs": loop.run_in_executor(None, get_fmp_data, "balance-sheet-statement", self.ticker, "limit=1"),
            "vix": loop.run_in_executor(None, get_fmp_data, "quote", "^VIX", ""),
            "earnings": loop.run_in_executor(None, get_earnings_data, self.ticker)
        }
        results = await asyncio.gather(*tasks.values())
        self.data.update(dict(zip(tasks.keys(), results)))

        return self.data["profile"] is not None and self.data["quote"] is not None

    def analyze(self):
        p = self.data.get("profile", {}) or {}
        q = self.data.get("quote", {}) or {}
        m = self.data.get("metrics", {}) or {} 
        r = self.data.get("ratios", {}) or {}
        vix_data = self.data.get("vix", {}) or {}
        earnings = self.data.get("earnings", []) or []
        
        if not p or not q: return None

        # ... (数据提取和增长率计算逻辑不变) ...
        price = q.get("price")
        price_200ma = q.get("priceAvg200")
        vol_today = q.get("volume")
        vol_avg = q.get("avgVolume")
        sector = self.sector
        beta = p.get("beta")
        if beta is None: beta = 1.0 
        
        m_cap = q.get("marketCap") or m.get("marketCap") or p.get("mktCap", 0)
        
        # --- 核心指标定义与数据完整性检查 ---
        ev_ebitda = m.get("evToEBITDA") or m.get("enterpriseValueOverEBITDATTM") or r.get("enterpriseValueMultipleTTM")
        fcf_yield = m.get("freeCashFlowYield") or m.get("freeCashFlowYieldTTM")
        roic = m.get("returnOnInvestedCapital") or m.get("returnOnInvestedCapitalTTM")
        net_margin = r.get("netProfitMarginTTM")
        ps_ratio = r.get("priceToSalesRatioTTM")
        
        peg_status = "N/A"
        peg = r.get("priceToEarningsGrowthRatioTTM") or r.get("pegRatioTTM")
        pe = r.get("priceEarningsRatioTTM") or m.get("peRatioTTM") 
        ni_growth = m.get("netIncomeGrowthTTM")
        rev_growth = m.get("revenueGrowthTTM")

        # --- 数据缺失/回落 状态日志 ---
        sector_median = self.data.get("sector_median")
        sector_avg = get_sector_benchmark(sector, sector_median) # PE Median
        
        if sector_median is not None:
            self.logs.append(f"[基准] 使用动态 PE 行业中位数: **{sector_median:.2f}** ({sector})")
        else:
            self.logs.append(f"[基准] 动态基准获取失败，使用硬编码 PE 回落 ({sector}): **{sector_avg:.2f}**")

        # ... (Missing Metrics and PEG logic) ...
        missing_metrics = []
        if ev_ebitda is None: missing_metrics.append("EV/EBITDA")
        if fcf_yield is None: missing_metrics.append("FCF Yield")
        if roic is None: missing_metrics.append("ROIC")
        if net_margin is None: missing_metrics.append("Net Margin")
        if pe is None: missing_metrics.append("PE Ratio") 
        
        if missing_metrics:
            self.logs.append(f"[核心缺失] 估值模型缺少关键指标: {', '.join(missing_metrics)}。部分分析将跳过。")
            if "FCF Yield" in missing_metrics and self.strategy == "数据不足":
                 self.strategy = "关键长期价值指标缺失，无法形成明确的估值倾向。"

        if peg is None and pe and ni_growth and ni_growth > 0:
            try: 
                peg = pe / (ni_growth * 100)
                peg_status = "Derived"
                self.logs.append(f"[数据补全] PEG ({format_num(peg)}) 为 PE/NI Growth 估算值，非 FMP 原始数据。")
            except: 
                peg_status = "N/A"
        elif peg is not None:
            peg_status = "Fetched"
        else:
            if "PEG" not in missing_metrics:
                self.logs.append(f"[数据缺失] 缺少 PEG, PE, 或净利润增长数据。成长评估指标缺失。")
            peg_status = "N/A"
            
        # --- 增长率计算 (依赖 PEG) ---
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

        # VIX/风险计算 (不变)
        vix = vix_data.get("price", 20)
        if vix < 20: self.market_regime = f"平静 (VIX {vix:.1f})"
        elif vix < 30: self.market_regime = f"震荡 (VIX {vix:.1f})"
        else: self.market_regime = f"恐慌 (VIX {vix:.1f})"

        if price and beta and vix:
            monthly_risk_pct = (vix / 100) * beta * 1.0 * 100
            self.risk_var = f"-{monthly_risk_pct:.1f}%"

        # --- Meme/信仰值模型 (不变) ---
        meme_score = 0
        # ... (Meme scoring logic remains the same) ...
        # 1. 价格趋势
        if price and price_200ma:
            if price > price_200ma * 1.4: meme_score += 2
            elif price > price_200ma * 1.15: meme_score += 1
            
        # 2. 极致估值 (使用 EV/EBITDA/PS)
        if (ps_ratio and ps_ratio > 20) or (ev_ebitda and ev_ebitda > 80): meme_score += 4
        elif (ps_ratio and ps_ratio > 10) or (ev_ebitda and ev_ebitda > 40): meme_score += 2
        elif (ps_ratio and ps_ratio > 8) or (ev_ebitda and ev_ebitda > 30): meme_score += 1
            
        # 3. 波动率
        if beta > 2.0: meme_score += 2
        elif beta > 1.3: meme_score += 1
            
        # 4. 现实扭曲
        if price and price_200ma and price > price_200ma:
            bad_fcf = (fcf_yield is not None and fcf_yield < 0.01)
            bad_peg = (peg is not None and (peg < 0 or peg > 4.0))
            if bad_fcf or bad_peg: meme_score += 2
            
        # 5. 人群聚集
        if vol_today and vol_avg and vol_avg > 0:
            if vol_today > vol_avg * 1.2: meme_score += 1
        
        # 业绩护盾
        if roic and roic > 0.20:
            if peg and 0 < peg < 3.0: meme_score -= 3
            else: meme_score -= 1
        
        meme_score = max(0, min(10, meme_score))
        meme_pct = int(meme_score * 10)
        is_faith_mode = meme_pct >= 50 # [修正] 触发阈值从 60% 降至 50%

        st_status = "估值合理"
        
        # --- 短期估值逻辑 (不变) ---
        is_distressed = False
        if (net_margin is not None and net_margin < -0.05) or (fcf_yield is not None and fcf_yield < -0.02):
            is_distressed = True
            st_status = "极其昂贵"
            self.logs.append(f"[预警] 净利率或现金流为负，PE 指标已失效，转为‘极其昂贵’。")
        
        if not is_distressed:
            if pe is not None:
                ratio = pe / sector_avg
                if ("高速" in growth_desc or "预期" in growth_desc) and (peg is not None and 0 < peg < 1.0):
                    st_status = "便宜 (高成长)"
                    self.logs.append(f"[成长特权] PE/PEG 估值极低，属于越涨越便宜。")
                elif ratio < 0.7:
                    st_status = "便宜"
                    self.logs.append(f"[板块] PE ({format_num(pe)}) 低于行业均值 ({sector_avg})，折扣明显。")
                elif ratio > 1.3:
                    if ("高速" in growth_desc or "预期" in growth_desc) and (peg is not None and 0 < peg < 2.0):
                        st_status = "合理溢价"
                        self.logs.append(f"[成长特权] 高PE ({format_num(pe)}) 被高增长消化，溢价合理。")
                    else:
                        st_status = "昂贵"
                        self.logs.append(f"[板块] PE ({format_num(pe)}) 远高于行业均值 ({sector_avg})，且缺乏增长支撑。")
                else:
                    st_status = "估值合理"
                    self.logs.append(f"[板块] PE ({format_num(pe)}) 与行业均值 ({sector_avg}) 接近，估值处于合理区间。")
            else:
                self.logs.append(f"[板块] 缺少 PE Ratio 数据，无法对比。")
        
        self.short_term_verdict = st_status
        
        # --- 长期估值 ---
        lt_status = "中性"

        if net_margin is not None and net_margin < 0 and price_200ma and price < price_200ma:
            is_value_trap = True
            lt_status = "风险极大"
            st_status = "下跌趋势"
            self.logs.append(f"[风险] 公司长期亏损且股价位于年线下方，看似低估实为“价值陷阱”。")
            self.strategy = "趋势与基本面双弱，需警惕'接飞刀'风险"
        
        if not is_value_trap:
            if is_faith_mode:
                # [修正] 使用更详细的日志描述和策略
                meme_log_desc = self.get_meme_log_description(meme_pct)
                self.logs.insert(0, f"[信仰] Meme值 {meme_pct}%。{meme_log_desc}")
                
                if "昂贵" in st_status: st_status += " / 资金博弈"
                if "昂贵" in lt_status: lt_status = "高溢价 (信仰支撑)"
                
                if meme_pct >= 90:
                    self.strategy = "极度狂热：风险与回报都被放大至极限，纯粹的资金动量博弈，严格执行止盈止损。"
                elif meme_pct >= 70:
                    self.strategy = "基本面内含极高预期，但短期走势被资金动量主导。顺势交易需严设止损。"
                else: # 50% or 60%
                    self.strategy = "估值包含较高情绪溢价，适合具备高风险承受能力的交易者。"

            if fcf_yield is not None:
                # ... (FCF/ROIC logic remains the same) ...
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
            
        # D. Alpha 信号 (不变)
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

        # --- 策略修正层 (不变) ---
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
    # [修正] 详细 Meme 描述 (50%+)
    if meme_pct >= 90: meme_desc = "极端狂热 (Meme 宇宙)"
    elif meme_pct >= 80: meme_desc = "高潮博弈 (纯情绪驱动)"
    elif meme_pct >= 70: meme_desc = "狂热资金流 (高位风险)"
    elif meme_pct >= 60: meme_desc = "情绪溢价 (散户信仰)"
    elif meme_pct >= 50: meme_desc = "预期拉满 (估值上限)"
    elif meme_pct >= 30: meme_desc = "机构共识 (稳健关注)"
    else: meme_desc = "冷门资产 (基本面主导)"
    
    core_factors = (
        f"> **Beta:** `{format_num(beta_val)}` ({beta_desc})\n"
        f"> **PEG:** `{peg_display}` ({data['growth_desc']})\n"
        f"> **Meme值:** `{meme_pct}%` ({meme_desc})"
    )
    embed.add_field(name="核心特征", value=core_factors, inline=False)
    
    # [排版] Risk 字段
    if data['risk_var'] != "N/A":
        embed.add_field(
            name="95% VaR (月度风险)", 
            value=f"> 最大回撤可能达 **{data['risk_var']}**", 
            inline=False
        )

    # [排版] 因子分析：使用 \n> \n 来连接，制造连贯的竖线
    log_content = []
    if model.flags: log_content.extend(model.flags) 
    log_content.extend([f"{log}" for log in model.logs]) 
    
    # 策略单独处理
    strategy_text = f"**[策略]** {model.strategy}"
    
    formatted_logs = []
    for log in log_content:
        # 标签加粗
        if log.startswith("[") and "]" in log:
            tag_end = log.find("]") + 1
            tag = log[:tag_end]
            content = log[tag_end:]
            formatted_logs.append(f"**{tag}**{content}")
        else:
            formatted_logs.append(log)

    # [核心技巧] 构造连续竖线
    # 1. 对每一行内容加 Quote
    quoted_factors = [f"> {log}" for log in formatted_logs]
    # 2. 用带 Quote 的空行连接，保证竖线不断
    factor_str = "\n> \n".join(quoted_factors)
    
    # 组合：因子引用块 + 双换行 + 策略（策略不加引用）
    full_log_str = f"{factor_str}\n\n{strategy_text}"
    
    if len(full_log_str) > 1000: full_log_str = full_log_str[:990] + "..."

    embed.add_field(name="因子分析", value=full_log_str, inline=False)

    embed.set_footer(text="FMP Ultimate API • 机构级多因子模型 | 模型建议，仅作参考")

    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(DISCORD_TOKEN)
