import discord
from discord.ext import commands
import aiohttp
import os
import json
from datetime import datetime
from dotenv import load_dotenv
import asyncio

# 加载环境变量
load_dotenv()

# 配置部分
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
FMP_API_KEY = os.getenv("FMP_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

# 配置 Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 辅助函数：获取 FMP 全量数据 (含预期和历史) ---
async def get_fmp_data(symbol):
    """从 FMP 获取 过去、现在、未来 的全量数据"""
    async with aiohttp.ClientSession() as session:
        try:
            # 1. 实时行情
            quote_url = f"https://financialmodelingprep.com/api/v3/quote/{symbol}?apikey={FMP_API_KEY}"
            
            # 2. 核心指标 (TTM) - 包含 PE, PEG, PS, PB, Debt/Eq 等
            metrics_url = f"https://financialmodelingprep.com/api/v3/key-metrics-ttm/{symbol}?apikey={FMP_API_KEY}"
            
            # 3. 现金流表 (取最近2年，用于对比趋势)
            cf_url = f"https://financialmodelingprep.com/api/v3/cash-flow-statement/{symbol}?period=annual&limit=2&apikey={FMP_API_KEY}"

            # 4. 损益表 (取最近2年，用于对比营收利润趋势)
            is_url = f"https://financialmodelingprep.com/api/v3/income-statement/{symbol}?period=annual&limit=2&apikey={FMP_API_KEY}"
            
            # 5. 盈利惊喜 (过去表现)
            earn_history_url = f"https://financialmodelingprep.com/api/v3/earnings-surprises/{symbol}?apikey={FMP_API_KEY}"

            # 6. 分析师预期 (未来预期) - 获取明年的预期 EPS 和 营收
            estimates_url = f"https://financialmodelingprep.com/api/v3/analyst-estimates/{symbol}?limit=1&apikey={FMP_API_KEY}"

            # 辅助请求函数
            async def fetch(url):
                async with session.get(url) as response:
                    try:
                        return await response.json()
                    except:
                        return []

            # 并发请求所有接口
            data_quote, data_metrics, data_cf, data_is, data_history, data_est = await asyncio.gather(
                fetch(quote_url), fetch(metrics_url), fetch(cf_url), 
                fetch(is_url), fetch(earn_history_url), fetch(estimates_url)
            )

            if not data_quote: return None

            return {
                "quote": data_quote[0],
                "metrics": data_metrics[0] if data_metrics else {},
                "cf": data_cf if data_cf else [],     # List usually
                "income": data_is if data_is else [], # List usually
                "history": data_history if data_history else [],
                "estimates": data_est[0] if data_est else {}
            }

        except Exception as e:
            print(f"FMP API Error: {e}")
            return None

# --- 核心逻辑：DeepSeek 分析 (全数据喂养) ---
async def get_deepseek_analysis(symbol, data):
    """构建包含过去、现在、未来的超级 Prompt"""
    
    # --- 1. 现在 (估值与价格) ---
    q = data['quote']
    m = data['metrics']
    price = q.get('price', 0)
    pe = q.get('pe', 'N/A')
    peg = m.get('pegRatioTTM', 'N/A')
    ps = m.get('priceToSalesRatioTTM', 'N/A')
    pb = m.get('priceToBookRatioTTM', 'N/A')
    beta = m.get('beta', 1.0)
    
    # --- 2. 过去 (财务趋势：今年 vs 去年) ---
    inc = data['income'] # List
    cf = data['cf']      # List
    
    # 营收趋势
    rev_trend = "未知"
    if len(inc) >= 2:
        rev_now = inc[0].get('revenue', 0)
        rev_prev = inc[1].get('revenue', 0)
        rev_trend = "增长" if rev_now > rev_prev else "下滑"
    
    # 利润趋势
    ni_trend = "未知"
    if len(inc) >= 2:
        ni_now = inc[0].get('netIncome', 0)
        ni_prev = inc[1].get('netIncome', 0)
        ni_trend = "增长" if ni_now > ni_prev else "下滑"
        
    # 现金流趋势
    fcf_trend = "未知"
    if len(cf) >= 2:
        fcf_now = cf[0].get('freeCashFlow', 0)
        fcf_prev = cf[1].get('freeCashFlow', 0)
        fcf_trend = "流入增加" if fcf_now > fcf_prev else "流入减少"

    # --- 3. 历史博弈 (最近4次财报) ---
    hist = data['history']
    miss_count = 0
    for h in hist[:4]:
        if h.get('estimatedEps', 0) > h.get('actualEps', 0):
            miss_count += 1
    beat_status = f"过去4季度{4-miss_count}次超预期，{miss_count}次不及预期"

    # --- 4. 未来 (分析师预期) ---
    est = data['estimates']
    est_eps = est.get('estimatedEpsAvg', 'N/A')
    est_rev = est.get('estimatedRevenueAvg', 'N/A')
    
    # 构建 Prompt
    prompt = f"""
    分析标的: {symbol}
    
    [全维度数据面板]
    1. **现状 (估值风险)**:
       - 价格: ${price}
       - 估值: PE={pe}, PEG={peg}, P/S={ps}, P/B={pb}
       - 波动: Beta={beta}
    
    2. **过去 (经营趋势)**:
       - 营收趋势: {rev_trend}
       - 净利润趋势: {ni_trend}
       - 自由现金流: {fcf_trend}
       - 历史战绩: {beat_status}
       
    3. **未来 (市场预期)**:
       - 华尔街预计下期EPS: {est_eps}
       - 华尔街预计下期营收: {est_rev}
    
    任务：请根据上述“过去表现”与“未来预期”的匹配度，结合当前“估值水位”，写一段策略总结。
    
    【绝对禁令】：
    1. **禁止出现任何数字**。不要写"PE是50"，要写"估值极高"；不要写"增长10%"，要写"温和增长"。
    2. **禁止给标签**。不要输出【Meme】之类的标题。
    3. **60字以内**。
    4. 风格：像一个老练的基金经理在做简报，只说核心逻辑（比如：业绩能否支撑估值，是否存在错杀）。
    
    输出示例：
    基本面稳健且现金流持续改善，但当前估值已透支未来两年的增长预期，建议等待回调后再行介入。
    """

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一个资深基本面量化分析师。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 1.1, 
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(DEEPSEEK_API_URL, headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    content = result['choices'][0]['message']['content']
                    return content.strip()
                else:
                    return "数据逻辑复杂，建议结合图表判断。"
        except Exception as e:
            print(f"DeepSeek Error: {e}")
            return "AI分析服务暂时离线。"

# --- 核心逻辑：计算因子 (保留原逻辑) ---
def calculate_factors(data):
    quote = data['quote']
    metrics = data['metrics']
    # 注意：现在 cf 是个 list
    cf_list = data['cf']
    cf_item = cf_list[0] if cf_list else {}
    
    factors = []
    
    # 1. 信仰/Meme 因子
    beta = metrics.get('beta', 1.0)
    pe = quote.get('pe', 0)
    meme_score = 0
    if beta > 1.5: meme_score += 40
    if pe is None or pe > 100: meme_score += 40
    
    if meme_score >= 60:
        factors.append(f"**[信仰]** Meme值 {meme_score}%。市场情绪已进入非理性繁荣区间，价格体现出**极致的资金动能**。")
    
    # 2. 成长锚点 (PEG)
    peg = metrics.get('pegRatioTTM')
    if peg is None: peg = 0
        
    if peg > 3:
        factors.append(f"**[成长锚点]** PEG (Forward): {peg:.2f} (泡沫化风险)。估值已脱离基本面引力，风险较高。")
    elif peg < 1 and peg > 0:
        factors.append(f"**[成长锚点]** PEG: {peg:.2f} (低估)。相对于其增长速度，当前价格具有极高性价比。")

    # 3. 核心估值 (P/S)
    ps = metrics.get('priceToSalesRatioTTM', 0)
    if ps > 15:
         factors.append(f"**[核心估值]** P/S 估值: {ps:.2f} (极高，价格已透支未来多年的增长)。")

    # 4. 价值修正 (FCF Yield)
    fcf = cf_item.get('freeCashFlow', 0)
    market_cap = quote.get('marketCap', 1)
    fcf_yield = (fcf / market_cap) * 100
    adj_fcf_yield = fcf_yield * 1.2 
    
    if adj_fcf_yield > 3:
        factors.append(f"**[价值修正]** Adj FCF Yield ({adj_fcf_yield:.2f}%) 显示出现金流支撑强劲。")
    elif adj_fcf_yield < 0.5:
        factors.append(f"**[价值修正]** Adj FCF Yield ({adj_fcf_yield:.2f}%) 高于 原始 FCF，反映出增长性资本支出的积极影响。")

    # 5. Alpha (业绩)
    earnings = data.get('history', [])
    misses = 0
    for e in earnings[:4]:
        if e.get('estimatedEps', 0) > e.get('actualEps', 0):
            misses += 1
            
    if misses >= 3:
        factors.append(f"**[Alpha]** 过去 4 季度中有 {misses} 次业绩不及预期，需警惕。")
    
    return factors, meme_score, beta

# --- 命令：!analyze ---
@bot.command(name="analyze")
async def analyze_stock(ctx, symbol: str):
    symbol = symbol.upper()
    status_msg = await ctx.send(f"🔄 正在全网搜集 {symbol} 的 历史财报、未来预期 及 实时估值数据 ...")

    # 1. 获取数据
    data = await get_fmp_data(symbol)
    if not data:
        await status_msg.edit(content=f"❌ 无法获取 {symbol} 的数据，请检查代码或 API。")
        return

    # 2. 计算因子
    factors_list, meme_val, beta = calculate_factors(data)
    
    # 3. 获取 AI 点评 (全量数据 + 无数字模式)
    ai_strategy = await get_deepseek_analysis(symbol, data)

    # 4. 构建 Embed
    price = data['quote']['price']
    market_cap_t = data['quote']['marketCap'] / 1e12 
    is_profit = "盈利" if data['quote'].get('eps', 0) > 0 else "亏损"
    
    embed = discord.Embed(
        title=f"估值分析: {symbol}",
        description=f"现价: ${price} | 市值: ${market_cap_t:.2f}T | {is_profit}",
        color=0x2b2d31 
    )
    
    embed.set_author(name="稳-量化估值系统 APP", icon_url="https://via.placeholder.com/50/000000/FFFFFF/?text=Wen")

    # --- 样式: 竖线引用 ---
    short_term = "合理溢价" if meme_val < 60 else "极度高估"
    long_term = "中性"
    val_conclusion = f"> 短期: {short_term}\n> 长期: {long_term}"
    embed.add_field(name="估值结论", value=val_conclusion, inline=False)

    beta_desc = "(高波动)" if beta > 1.5 else "(低波动)"
    meme_desc = "(资金狂热)" if meme_val > 50 else "(情绪平稳)"
    core_features = f"> **Beta**: {beta:.2f} {beta_desc}\n> **Meme值**: {meme_val}% {meme_desc}"
    embed.add_field(name="核心特征", value=core_features, inline=False)

    # --- 样式: VaR 竖线 ---
    var_95 = beta * -9.14 
    var_text = f"> 最大回撤可能在 **{var_95:.2f}%** 附近"
    embed.add_field(name="95% VaR (月度风险)", value=var_text, inline=False)

    # --- 样式: 因子分析 (空一行且不断开竖线) ---
    if factors_list:
        formatted_factors = [f"> {f}" for f in factors_list]
        factors_text = "\n> \n".join(formatted_factors)
        embed.add_field(name="因子分析", value=factors_text, inline=False)

    # --- 样式: 策略 (不换行，不加粗标题，纯文字) ---
    # 根据您的要求，这里直接放 deepseek 的返回结果
    strategy_content = f"**[策略]** {ai_strategy}"
    embed.add_field(name="", value=strategy_content, inline=False)

    # Footer
    embed.set_footer(text="(模型建议，仅作参考，不构成投资建议)")

    await status_msg.edit(content="", embed=embed)

# 启动 Bot
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
