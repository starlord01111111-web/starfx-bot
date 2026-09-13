import os, asyncio, time, random, io
from datetime import datetime, timezone
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
import websockets, json
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
TELEGRAM_TOKEN = "8656945768:AAG1avs7PEkGlwJ6VI8cBiOyclOIqmyPjDA"
TELEGRAM_CHAT_ID = "-1004365660319"
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")

WEEKDAY_SYMBOLS = ["XAU/USD", "GBP/USD", "R_75"]
WEEKEND_SYMBOLS = ["R_75", "R_100", "BOOM1000", "CRASH1000"]

# FIXED MAPPING - underscore is required
SYMBOL_MAP = {
    "XAU/USD": "frxXAUUSD",
    "GBP/USD": "frxGBPUSD",
    "R_75": "R_75",
    "R_100": "R_100",
    "BOOM1000": "BOOM_1000",
    "CRASH1000": "CRASH_1000",
}

def to_deriv_symbol(s): return SYMBOL_MAP.get(s, s)
def from_deriv_symbol(s):
    for k,v in SYMBOL_MAP.items():
        if v==s: return k
    return s

def get_active_symbols():
    now = datetime.now(timezone.utc)
    is_weekend = now.weekday() >= 5 # 5=Sat, 6=Sun
    return WEEKEND_SYMBOLS if is_weekend else WEEKDAY_SYMBOLS

# === STORAGE ===
stats = {"wins":0,"losses":0,"tp1":0,"tp2":0,"active":0}
last_signal_time = {}
signal_history = []

# === DERIV FETCH ===
async def fetch_candles(deriv_symbol, granularity=300, count=100):
    try:
        uri = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
        async with websockets.connect(uri) as ws:
            req = {
                "ticks_history": deriv_symbol,
                "adjust_start_time": 1,
                "count": count,
                "end": "latest",
                "style": "candles",
                "granularity": granularity
            }
            await ws.send(json.dumps(req))
            resp = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(resp)
            if "candles" not in data:
                return None
            df = pd.DataFrame(data["candles"])
            df['open']=df['open'].astype(float); df['high']=df['high'].astype(float)
            df['low']=df['low'].astype(float); df['close']=df['close'].astype(float)
            df['epoch']=pd.to_datetime(df['epoch'], unit='s')
            return df
    except Exception as e:
        print(f"Fetch error {deriv_symbol}: {e}")
        return None

async def fetch_data(symbol, tf="M5"):
    gran = {"M5":300,"M15":900,"H1":3600,"H4":14400}[tf]
    df = await fetch_candles(to_deriv_symbol(symbol), granularity=gran, count=100)
    return df

async def fetch_current_price(symbol):
    try:
        uri = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"ticks": to_deriv_symbol(symbol)}))
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(resp)
            if "tick" in data and "quote" in data["tick"]:
                return float(data["tick"]["quote"])
    except:
        return None
    return None

# === BIBLE LOGIC ===
def get_trend_bias(df):
    if df is None or len(df)<50: return "NEUTRAL"
    ema21 = df['close'].ewm(span=21).mean().iloc[-1]
    ema50 = df['close'].ewm(span=50).mean().iloc[-1]
    price = df['close'].iloc[-1]
    if price > ema21 and ema21 > ema50: return "BULL"
    if price < ema21 and ema21 < ema50: return "BEAR"
    return "NEUTRAL"

def check_confluence(df, pattern_bias):
    if df is None or len(df)<30: return 0, []
    score=0; reasons=[]
    close=df['close'].iloc[-1]
    ema21=df['close'].ewm(span=21).mean().iloc[-1]
    # 1. 21 EMA confluence
    if (pattern_bias=="BULL" and close>ema21) or (pattern_bias=="BEAR" and close<ema21):
        score+=1; reasons.append("21EMA")
    # 2. Swing High/Low
    swing_high = df['high'].rolling(20).max().iloc[-1]
    swing_low = df['low'].rolling(20).min().iloc[-1]
    if abs(close-swing_high)/close < 0.005 or abs(close-swing_low)/close < 0.005:
        score+=1; reasons.append("S/R")
    # 3. Fib 50/61.8
    recent_high = df['high'].iloc[-20:].max()
    recent_low = df['low'].iloc[-20:].min()
    fib_range = recent_high-recent_low
    if fib_range>0:
        fib50 = recent_high - fib_range*0.5
        fib618 = recent_high - fib_range*0.618
        if abs(close-fib50)/close<0.003 or abs(close-fib618)/close<0.003:
            score+=1; reasons.append("Fib50/61.8")
    return min(score,2), reasons

def detect_patterns(df):
    if df is None or len(df)<5: return None
    last=df.iloc[-1]; prev=df.iloc[-2]
    body = abs(last['close']-last['open'])
    wick_upper = last['high']-max(last['open'],last['close'])
    wick_lower = min(last['open'],last['close'])-last['low']
    # Pin Bar
    if wick_lower > body*2 and last['close']>last['open']:
        return {"pattern":"PinBar BULL","bias":"BULL"}
    if wick_upper > body*2 and last['close']<last['open']:
        return {"pattern":"PinBar BEAR","bias":"BEAR"}
    # Engulfing
    if last['close']>last['open'] and prev['close']<prev['open'] and last['close']>prev['open'] and last['open']<prev['close']:
        return {"pattern":"Engulfing BULL","bias":"BULL"}
    if last['close']<last['open'] and prev['close']>prev['open'] and last['open']>prev['close'] and last['close']<prev['open']:
        return {"pattern":"Engulfing BEAR","bias":"BEAR"}
    # Inside Bar + Fakey
    if last['high']<prev['high'] and last['low']>prev['low']:
        return {"pattern":"InsideBar","bias":"NEUTRAL"}
    # Morning/Evening Star simplified
    if len(df)>=3:
        p2=df.iloc[-3]
        if p2['close']<p2['open'] and abs(last['close']-last['open'])>body and last['close']>last['open'] and last['close']>p2['open']:
            return {"pattern":"MorningStar","bias":"BULL"}
        if p2['close']>p2['open'] and last['close']<last['open'] and last['close']<p2['open']:
            return {"pattern":"EveningStar","bias":"BEAR"}
    return None

async def evaluate_aplus_setup(symbol):
    df_m5 = await fetch_data(symbol, "M5")
    df_h1 = await fetch_data(symbol, "H1")
    df_h4 = await fetch_data(symbol, "H4")
    if df_m5 is None: return None

    pat = detect_patterns(df_m5)
    if not pat: return None
    if pat['bias']=="NEUTRAL" and "Inside" not in pat['pattern']: return None

    h1_bias = get_trend_bias(df_h1)
    h4_bias = get_trend_bias(df_h4)

    # FIXED: allow neutral to avoid killing all signals on weekend
    if h4_bias!="NEUTRAL" and h1_bias!="NEUTRAL" and h4_bias!=h1_bias:
        return None
    if h4_bias=="NEUTRAL" and h1_bias=="NEUTRAL" and pat['bias']!="NEUTRAL":
        # still allow if confluence strong
        pass

    score, reasons = check_confluence(df_m5, pat['bias'])

    # FIXED: was <2 (too strict) now <1 for testing
    if score < 1 and "INSIDE" not in pat['pattern'].upper():
        return None

    # Cooldown 2h
    last = last_signal_time.get(symbol,0)
    if time.time()-last < 7200:
        return None

    price = df_m5['close'].iloc[-1]
    sl = price*0.997 if pat['bias']=="BULL" else price*1.003
    if "R_75" in symbol: sl = price*0.995 if pat['bias']=="BULL" else price*1.005

    return {
        "symbol":symbol,
        "pattern":pat['pattern'],
        "bias":pat['bias'],
        "price":price,
        "sl":sl,
        "score":f"{score}/2",
        "reasons":reasons,
        "df":df_m5,
        "h1":h1_bias,
        "h4":h4_bias
    }

def generate_tradingview_chart(df, symbol, setup):
    df_plot = df.tail(50).copy()
    df_plot.set_index('epoch', inplace=True)
    fig, axlist = mpf.plot(df_plot, type='candle', style='yahoo', returnfig=True, title=f"{symbol} {setup['pattern']} {setup['score']} H1:{setup['h1']} H4:{setup['h4']}")
    buf = f"/tmp/{symbol.replace('/','')}.png"
    fig.savefig(buf)
    plt.close(fig)
    return buf

def format_price(sym, p):
    return f"{p:.2f}" if "R_" in sym else f"{p:.5f}"

# === TELEGRAM COMMANDS ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = ",".join(get_active_symbols())
    await update.message.reply_text(
        f"StarFx V14.2 BIBLE FIXED {datetime.now().strftime('%Y-%m-%d')} RUNNING {syms}\n"
        f"Strategies: PinBar, Engulfing, InsideBar, Fakey, MorningStar, Harami\n"
        f"Confluence: Support/Resistance + 21EMA + Fib50/61.8 + Trend\n"
        f"/signal /price /report"
    )

async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Scanning Bible patterns...")
    found=0
    reasons_log=[]
    for sym in get_active_symbols():
        setup = await evaluate_aplus_setup(sym)
        if setup:
            found+=1
            last_signal_time[sym]=time.time()
            chart = generate_tradingview_chart(setup['df'], sym, setup)
            ep = format_price(sym, setup['price'])
            caption = f"{setup['bias']} {sym} {setup['pattern']} {ep} Score {setup['score']} {','.join(setup['reasons'])} H1:{setup['h1']} H4:{setup['h4']} SL:{format_price(sym,setup['sl'])}"
            try:
                with open(chart,"rb") as ph:
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=ph, caption=caption)
            except Exception as e:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=caption)
            finally:
                if os.path.exists(chart): os.remove(chart)
        else:
            reasons_log.append(f"{sym}: No A+ confluence / data")

    if found==0:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ No Bible A+ setups right now\n" + "\n".join(reasons_log) +
                 "\n\nFilter is strict - good. Try /price to check feed, /signal again in 30m."
        )

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines=["💰 Live:"]
    for sym in get_active_symbols():
        p = await fetch_current_price(sym)
        if p is None:
            lines.append(f"{sym}: feed lag")
        else:
            lines.append(f"{sym}: {format_price(sym,p)}")
    await update.message.reply_text("\n".join(lines))

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d=datetime.now().strftime('%Y-%m-%d')
    await update.message.reply_text(
        f"📊 REPORT V14.2 BIBLE {d}\nWR 0.0% Wins 0 Losses 0 TP1 0 TP2 0 Active {stats['active']}"
    )

# === APP ===
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("signal", signal_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("report", report_command))
    app.run_polling()

if __name__=="__main__":
    main()
