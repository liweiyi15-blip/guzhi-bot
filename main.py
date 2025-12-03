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

# --- 修正点：改回 DISCORD_TOKEN ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") 
FMP_API_KEY = os.getenv("FMP_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

# 配置 Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 辅助函数：获取 FMP 全量数据 (过去、现在、未来、风险、位置) ---
async def get_fmp_data(symbol):
    """从 FMP 获取所有维度的全量数据"""
    async with aiohttp.ClientSession() as session:
        try:
            # 1. 实时行情 (含 52周高低, 交易量)
            quote_url = f"https://financialmodelingprep.com/api/v3/quote/{symbol}?apikey={FMP_API_KEY}"
            
            # 2. 核心指标 (含 负债率, ROE, 毛利率 等)
            metrics_url = f"https://financialmodelingprep.com/api/v3/key-metrics-ttm/{symbol}?apikey={FMP_API_KEY}"
            
            # 3. 现金流表 (趋势)
            cf_url = f"https://financialmodelingprep.com/api/v3/cash-flow-statement/{symbol}?period=annual&limit=2&apikey={FMP_API_KEY}"

            # 4. 损益表 (趋势)
            is_url = f"https://financialmodelingprep.com/api/v3/income-statement/{symbol}?period=annual&limit=2&apikey={FMP_API_KEY}"
            
            # 5. 盈利惊喜 (历史战绩)
            earn_history_url = f"https://financialmodelingprep.com/api/v3/earnings-surprises/{symbol}?apikey={FMP_API_KEY}"

            # 6. 分析师预期 (未来分歧)
            estimates_url = f"https://financialmodelingprep.com/api/v3/analyst-estimates/{symbol}?limit=1&apikey={FMP_API_KEY}"

            async def fetch(url):
                async with session.get(url) as response:
                    try:
                        return await response.json()
                    except:
                        return []

            data_quote, data_metrics, data_cf, data_is, data_history, data_est = await asyncio.gather(
                fetch(quote_url), fetch(metrics_url), fetch(cf_url), 
                fetch(is_url), fetch(earn_history_url), fetch(estimates_url)
            )

            if not data_quote: return None

            return {
                "quote": data_quote[0],
                "metrics": data_metrics[0] if data_metrics else {},
                "cf": data_cf if data_cf else [],
                "income": data_is if data_is else [],
                "history": data_history if data_history else [],
                "estimates": data_est[0] if data_est else {}
            }

        except Exception as e:
            print(f"FMP API Error: {e}")
            return None

# --- 核心逻辑：DeepSeek 分析 (全量数据) ---
async def get_deepseek_analysis(symbol, data):
    """构建包含 估值、基本面、风险、分歧、价格位置 的全量 Prompt"""
    
    # 1. 价格与位置
    q = data['quote']
    price = q.get('price', 0)
    high_52 = q.get('yearHigh', price)
    dist_high = ((price - high_52) / high_52) * 100 if high_52 else 0 
    
    # 2. 估值与效率
    m = data['metrics']
    pe = q.get('pe', 'N/A')
    peg = m.get('pegRatioTTM', 'N/A')
    pb = m.get('priceToBookRatioTTM', 'N/A')
    roe = m.get('roeTTM', 'N/A') 
    
    # 3. 财务健康
    debt_equity = m.get('debtToEquityTTM', 'N/A') 
    current_ratio = m.get('currentRatioTTM', 'N/A') 
    
    # 4. 过去趋势
    inc = data['income']
    # 毛利率
    gross_margin = "N/A"
    if inc:
        rev = inc[0].get('revenue', 1)
        gp = inc[0].get('grossProfit', 0)
        gross_margin = f"{(gp/rev)*100:.2f}%" if rev else "0%"
    
    rev_trend = "持平"
    if len(inc) >= 2:
        rev_trend = "增长" if inc[0].get('revenue', 0) > inc[1].get('revenue', 0) else "下滑"

    # 5. 未来预期与分歧
    est = data['estimates']
    est_eps_high = est.get('estimatedEpsHigh', 0)
    est_eps_low = est.get('estimatedEpsLow', 0)
    divergence = "极大" if (est_eps_high - est_eps_low) > 1 else "一致" 

    # 构建上帝视角 Prompt
    prompt = f"""
    深度分析标的: {symbol}
    
    [全息数据面板]
    1. **交易盘口**: 现价${price} (距离52周高点 {dist_high:.1f}%)。
    2. **估值水位**: PE={pe}, PEG={peg}, PB={pb}。
    3. **盈利质量**: ROE(净资产收益率)={roe}, 毛利率={gross_margin}。
    4. **财务排雷**: 负债权益比={debt_equity} (关注是否过高), 流动比率={current_ratio} (短期偿债能力)。
    5. **趋势动能**: 营收{rev_trend}，历史业绩符合度(是否经常暴雷)。
    6. **预期分歧**: 华尔街对下期EPS预测分歧度为[{divergence}] (High:{est_eps_high} vs Low:{est_eps_low})。
    
    任务：请综合“估值性价比”、“财务安全性”和“市场预期差”这三个维度，给出一份简报。
    
    【绝对禁令】：
    1. **禁止出现任何数字** (把数字转化为定性描述，如：负债高企、毛利极厚、估值低估)。
    2. **禁止给标签** (不要输出【XXX】)。
    3. **60字以内**。
    4. 风格：像华尔街首席策略师的晨会发言，一针见血。
    
    输出示例：
    虽然毛利极厚且现金流充裕，但极高的负债率和市场对未来的巨大分歧限制了上涨空间，当前价格风险收益比不佳。
    """

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一个基于全量数据做决策的对冲基金经理。"},
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
                    return "数据模型计算中，暂时无法输出策略。"
        except Exception as e:
            print(f"DeepSeek Error: {e}")
            return "AI 接口暂时离线。"

# --- 核心逻辑：计算因子 (保持原样) ---
def calculate_factors(data):
    quote = data['quote']
    metrics = data['metrics']
    cf_list = data['cf']
    cf_item = cf_list[0] if cf_list else {}
    
    factors = []
    
    # 1. 信仰/Meme 因子
    beta = metrics.get('beta', 1.0)
    pe = quote.get('pe', 0)
    meme_score = 0
    if beta is not None and beta > 1.5: meme_score += 40
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
    status_msg = await ctx.send(f"🔄 正在全网搜集 {symbol} 的全息数据 (含财务健康、分歧度及未来预期)...")

    # 1. 获取数据
    data = await get_fmp_data(symbol)
    if not data:
        await status_msg.edit(content=f"❌ 无法获取 {symbol} 的数据，请检查代码或 API。")
        return

    # 2. 计算因子
    factors_list, meme_val, beta = calculate_factors(data)
    if beta is None: beta = 1.0 # fallback

    # 3. 获取 AI 点评 (全量数据)
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

    # --- 样式: 策略 (纯文字) ---
    strategy_content = f"**[策略]** {ai_strategy}"
    embed.add_field(name="", value=strategy_content, inline=False)

    # Footer
    embed.set_footer(text="(模型建议，仅作参考，不构成投资建议)")

    await status_msg.edit(content="", embed=embed)

# 启动 Bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("【错误】未检测到 DISCORD_TOKEN，请检查环境变量。")
    else:
        bot.run(DISCORD_TOKEN)
