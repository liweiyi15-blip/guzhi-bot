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

# --- 环境变量 ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") 
FMP_API_KEY = os.getenv("FMP_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

# --- Bot 设置 (增加自动同步功能) ---
class MyBot(commands.Bot):
    async def setup_hook(self):
        # 这步操作是将本地的命令同步到 Discord 服务器
        # 注意：全局同步可能需要几分钟生效，但在当前服务器通常很快
        await self.tree.sync()
        print("✅ 命令树已同步 (Command Tree Synced)")

intents = discord.Intents.default()
intents.message_content = True
bot = MyBot(command_prefix='!', intents=intents)

# --- 辅助函数：获取 FMP 全量数据 (保持原始数据逻辑) ---
async def get_fmp_data(symbol):
    """从 FMP 获取所有维度的全量数据"""
    async with aiohttp.ClientSession() as session:
        try:
            # 1. 实时行情
            quote_url = f"https://financialmodelingprep.com/api/v3/quote/{symbol}?apikey={FMP_API_KEY}"
            
            # 2. 核心指标
            metrics_url = f"https://financialmodelingprep.com/api/v3/key-metrics-ttm/{symbol}?apikey={FMP_API_KEY}"
            
            # 3. 现金流表 (取2年)
            cf_url = f"https://financialmodelingprep.com/api/v3/cash-flow-statement/{symbol}?period=annual&limit=2&apikey={FMP_API_KEY}"

            # 4. 损益表 (取2年)
            is_url = f"https://financialmodelingprep.com/api/v3/income-statement/{symbol}?period=annual&limit=2&apikey={FMP_API_KEY}"
            
            # 5. 盈利惊喜
            earn_history_url = f"https://financialmodelingprep.com/api/v3/earnings-surprises/{symbol}?apikey={FMP_API_KEY}"

            # 6. 分析师预期
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

# --- 核心逻辑：DeepSeek 分析 (投喂原始数据) ---
async def get_deepseek_analysis(symbol, data):
    """直接投喂原始财务数字，不做主观加工"""
    
    # 1. 原始价格与估值数据
    q = data['quote']
    m = data['metrics']
    price = q.get('price', 0)
    high_52 = q.get('yearHigh', 0)
    low_52 = q.get('yearLow', 0)
    
    pe = q.get('pe', 'N/A')
    peg = m.get('pegRatioTTM', 'N/A')
    pb = m.get('priceToBookRatioTTM', 'N/A')
    roe = m.get('roeTTM', 'N/A')
    debt_equity = m.get('debtToEquityTTM', 'N/A')
    
    # 2. 原始财务数据 (本期 vs 上期) - 单位 B (Billion)
    inc = data['income']
    rev_curr = 0; rev_prev = 0
    ni_curr = 0; ni_prev = 0
    
    if len(inc) >= 1:
        rev_curr = inc[0].get('revenue', 0) / 1e9
        ni_curr = inc[0].get('netIncome', 0) / 1e9
    if len(inc) >= 2:
        rev_prev = inc[1].get('revenue', 0) / 1e9
        ni_prev = inc[1].get('netIncome', 0) / 1e9
        
    # 3. 原始现金流数据
    cf = data['cf']
    fcf_curr = 0; fcf_prev = 0
    if len(cf) >= 1: fcf_curr = cf[0].get('freeCashFlow', 0) / 1e9
    if len(cf) >= 2: fcf_prev = cf[1].get('freeCashFlow', 0) / 1e9

    # 4. 原始预期数据 (分歧的具体数值)
    est = data['estimates']
    est_eps_avg = est.get('estimatedEpsAvg', 'N/A')
    est_eps_high = est.get('estimatedEpsHigh', 'N/A')
    est_eps_low = est.get('estimatedEpsLow', 'N/A')
    est_rev_avg = est.get('estimatedRevenueAvg', 0) / 1e9

    # 构建 Prompt：只陈列事实
    prompt = f"""
    分析标的: {symbol} (请基于以下原始数据做专业判断)
    
    [市场数据]
    - 现价: ${price} (52周范围: ${low_52} - ${high_52})
    - 估值: PE={pe}, PEG={peg}, PB={pb}
    
    [财务表现 (本期 vs 上期)]
    - 营收: ${rev_curr:.2f}B (上期: ${rev_prev:.2f}B)
    - 净利润: ${ni_curr:.2f}B (上期: ${ni_prev:.2f}B)
    - 自由现金流: ${fcf_curr:.2f}B (上期: ${fcf_prev:.2f}B)
    - 核心质量: ROE={roe}, 负债权益比={debt_equity}
    
    [华尔街预期 (下期)]
    - EPS预测: 均值${est_eps_avg} (最乐观${est_eps_high} vs 最悲观${est_eps_low})
    - 营收预测: ${est_rev_avg:.2f}B
    
    任务：作为一个对冲基金经理，请自行计算增长率、评估分歧大小，并结合估值给出简报。
    
    【输出要求】：
    1. **禁止在最终回复中出现具体数字** (你在内心算完后，直接告诉我定性结果，如“业绩倍增”、“增长停滞”、“分歧巨大”)。
    2. **60字以内**。
    3. 风格：一针见血，直击核心逻辑。
    
    输出示例：
    营收虽翻倍增长，但巨大的分析师分歧和极高的负债率表明风险并未消除，当前高估值下盈亏比不佳。
    """

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一个基于原始数据进行独立思考的资深交易员。"},
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

# --- 核心逻辑：计算因子 ---
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

# --- 命令：analyze (支持斜杠 /analyze 和 前缀 !analyze) ---
@bot.hybrid_command(name="analyze", description="分析股票 (全量数据 + AI深度策略)")
async def analyze_stock(ctx, symbol: str):
    symbol = symbol.upper()
    
    # 关键点：斜杠命令必须 defer，否则3秒后会超时
    await ctx.defer() 
    
    # 1. 获取数据
    data = await get_fmp_data(symbol)
    if not data:
        await ctx.send(f"❌ 无法获取 {symbol} 的数据，请检查代码或 API。")
        return

    # 2. 计算因子
    factors_list, meme_val, beta = calculate_factors(data)
    if beta is None: beta = 1.0 

    # 3. 获取 AI 点评 (原始数据版)
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

    # 发送最终结果
    await ctx.send(embed=embed)

# 启动 Bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("【错误】未检测到 DISCORD_TOKEN，请检查环境变量。")
    else:
        bot.run(DISCORD_TOKEN)
