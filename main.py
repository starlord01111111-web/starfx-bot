import asyncio
from datetime import datetime, timezone
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import sqlite3
import ccxt
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIG ---
TELEGRAM_TOKEN =  "8656945768:AAG1avs7PEkGlwJ6VI8cBiOyclOIqmyPjDA"
CHAT_ID = "-1004365660319"
SYMBOLS = ["XAU/USD", "GBP/USD", "BTC/USDT"]
NEWS_CURRENCY = ["USD", "GBP"]

ACCOUNT_BALANCE = 10000.00
RISK_PER_TRADE_PCT = 0.01
MAX_DAILY_LOSS_PCT = 0.03
MAX_CONCURRENT_TRADES = 2

exchange = ccxt.kraken({'enableRateLimit': True})

daily_stats = {"date": None, "losses_today": 0.0, "is_circuit_broken": False, "wins": 0, "losses": 0}
active_trades = []
last_price_cache = {}

# --- HEALTH SERVER FIXED ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()

# --- RISK ENGINE ---
def calculate_position_size(account_balance, risk_pct, entry, stop_loss):
    risk_amount = account_balance * risk_pct
    price_risk = abs(entry - stop_loss)
    if price_risk == 0:
        return 0.0
    return round(risk_amount / price_risk, 4)

def check_circuit_breaker():
    today = datetime.now(timezone.utc).date()
    if daily_stats["date"]!= today:
        daily_stats["date"] = today
        daily_stats["losses_today"] = 0.0
        daily_stats["is_circuit_broken"] = False
    if daily_stats["losses_today"] >= (ACCOUNT_BALANCE * MAX_DAILY_LOSS_PCT):
        daily_stats["is_circuit_broken"] = True
        return False
    return True

# --- SESSION ---
def is_valid_trading_session():
    now_utc = datetime.now(timezone.utc)
    eat_hour = (now_utc.hour + 3) % 24
    if eat_hour < 9:
        return False, "OFF_HOURS"
    if 9 <= eat_hour < 18:
        return True, "LONDON_SESSION"
    elif 16 <= eat_hour < 22:
        return True, "NEW_YORK_SESSION"
    return False, "OFF_HOURS"

def is_london_scalp_time():
    now_utc = datetime.now(timezone.utc)
    return 7 <= now_utc.hour <= 11

# --- DATA ---
def fetch_data(symbol, timeframe, limit=100):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        last_price_cache[symbol] = df['close'].iloc[-1]
        return df
    except Exception as e:
        print(f"Fetch error {symbol} {timeframe}: {e}")
        return None

# --- CHART WITH LEVELS ---
def generate_tradingview_chart(df, symbol, setup, filename="chart.png"):
    plot_df = df.iloc[-60:].copy()
    mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit', volume='in')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", gridcolor="#2a2e39", facecolor="#131722")
    hlines = [setup['price'], setup['tp1'], setup['tp2'], setup['sl']]
    colors = ['#2962ff', '#00e676', '#00c853', '#ff1744']
    if 'ob_level' in setup and setup['ob_level']:
        hlines.append(setup['ob_level'])
        colors.append('#ff9800')
    if 'fvg_level' in setup and setup['fvg_level']:
        hlines.append(setup['fvg_level'])
        colors.append('#9c27b0')
    fig, _ = mpf.plot(plot_df, type='candle', style=style,
        title=f"\n{symbol} - {setup['bias']}",
        hlines=dict(hlines=hlines, colors=colors, linestyle='--', linewidths=1.2),
        savefig=filename, returnfig=True, figratio=(16,9), figscale=1.2)
    plt.close(fig)
    return filename

# --- NEWS ---
def fetch_news_window():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            events = r.json()
            now = datetime.now(timezone.utc)
            pre = []
            for ev in events:
                if ev.get("impact") == "High" and ev.get("country") in NEWS_CURRENCY:
                    try:
                        ev_time = datetime.fromisoformat(ev["date"])
                        mins = (ev_time - now).total_seconds()/60
                        if 30 <= mins <= 120:
                            pre.append(ev)
                    except:
                        pass
            return pre
    except Exception as e:
        print(f"News error {e}")
    return []

# --- TECHNICALS ---
def analyze_structure(df, window=20):
    if df is None or len(df) < window+1:
        return "NEUTRAL"
    recent = df.iloc[:-1]
    high = recent['high'].iloc[-window:].max()
    low = recent['low'].iloc[-window:].min()
    curr = df['close'].iloc[-1]
    if curr > high:
        return "BULLISH"
    if curr < low:
        return "BEARISH"
    ema = df['close'].ewm(span=20).mean().iloc[-1]
    return "BULLISH" if curr > ema else "BEARISH"

def detect_price_action(df):
    if df is None or len(df) < 3:
        return None
    c1 = df.iloc[-2]
    body = abs(c1['close'] - c1['open'])
    uw = c1['high'] - max(c1['close'], c1['open'])
    lw = min(c1['close'], c1['open']) - c1['low']
    if lw >= (2*body) and uw <= (0.5*body):
        return "BULLISH_PINBAR"
    if uw >= (2*body) and lw <= (0.5*body):
        return "BEARISH_PINBAR"
    if c1['close'] > c1['open'] and df.iloc[-3]['close'] < df.iloc[-3]['open'] and body > abs(df.iloc[-3]['close']-df.iloc[-3]['open']):
        return "BULLISH_ENGULFING"
    if c1['close'] < c1['open'] and df.iloc[-3]['close'] > df.iloc[-3]['open'] and body > abs(df.iloc[-3]['close']-df.iloc[-3]['open']):
        return "BEARISH_ENGULFING"
    return None

def detect_liquidity_sweep(df, window=30):
    if df is None or len(df) < window:
        return None
    rh = df['high'].iloc[-window:-2].max()
    rl = df['low'].iloc[-window:-2].min()
    c1, c0 = df.iloc[-2], df.iloc[-1]
    if c1['low'] < rl and c0['close'] > rl:
        return "BULLISH_SWEEP"
    if c1['high'] > rh and c0['close'] < rh:
        return "BEARISH_SWEEP"
    return None

def detect_ob_fvg(df):
    if df is None or len(df) < 5:
        return None, None
    ob = df['low'].iloc[-3] if df['close'].iloc[-1] > df['open'].iloc[-1] else df['high'].iloc[-3]
    fvg = (df['high'].iloc[-3] + df['low'].iloc[-1])/2
    return float(ob), float(fvg)

# --- SCALP ENGINE ---
def evaluate_scalp_london(symbol):
    if not is_london_scalp_time():
        return None
    if not check_circuit_breaker() or len(active_trades) >= MAX_CONCURRENT_TRADES:
        return None
    m15 = fetch_data(symbol, '15m', 100)
    m5 = fetch_data(symbol, '5m', 100)
    m1 = fetch_data(symbol, '1m', 100)
    if m15 is None or m5 is None or m1 is None:
        return None
    m15_bias = analyze_structure(m15, 20)
    if m15_bias == "NEUTRAL":
        return None
    m5_pa = detect_price_action(m5)
    m1_pa = detect_price_action(m1)
    m5_sweep = detect_liquidity_sweep(m5, 20)
    m1_sweep = detect_liquidity_sweep(m1, 20)
    if not (m5_pa or m1_pa or m5_sweep or m1_sweep):
        return None
    price = m5['close'].iloc[-1]
    ob_level, fvg_level = detect_ob_fvg(m5)
    if m15_bias == "BULLISH" and ("BULLISH" in str(m5_pa) or "BULLISH" in str(m1_pa) or "BULLISH" in str(m5_sweep)):
        sl = m1['low'].iloc[-5:].min() * 0.9995
        risk = price - sl
        if risk <= 0:
            return None
        return {"symbol": symbol, "bias": "BUY SCALP (London)", "price": price, "sl": sl, "tp1": price + risk*1.5, "tp2": price + risk*3.0, "position_units": calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, price, sl), "session": "LONDON_SCALP", "df": m5, "ob_level": ob_level, "fvg_level": fvg_level, "is_scalp": True, "pre_news": False}
    if m15_bias == "BEARISH" and ("BEARISH" in str(m5_pa) or "BEARISH" in str(m1_pa) or "BEARISH" in str(m5_sweep)):
        sl = m1['high'].iloc[-5:].max() * 1.0005
        risk = sl - price
        if risk <= 0:
            return None
        return {"symbol": symbol, "bias": "SELL SCALP (London)", "price": price, "sl": sl, "tp1": price - risk*1.5, "tp2": price - risk*3.0, "position_units": calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, price, sl), "session": "LONDON_SCALP", "df": m5, "ob_level": ob_level, "fvg_level": fvg_level, "is_scalp": True, "pre_news": False}
    return None

# --- SWING ENGINE ---
def evaluate_aplus_setup(symbol):
    if not check_circuit_breaker() or len(active_trades) >= MAX_CONCURRENT_TRADES:
        return None
    session_active, session_name = is_valid_trading_session()
    if not session_active:
        return None
    pre_news = fetch_news_window()
    tf_data = {'H4': fetch_data(symbol, '4h'), 'H1': fetch_data(symbol, '1h'), 'M15': fetch_data(symbol, '15m'), 'M5': fetch_data(symbol, '5m')}
    if any(v is None for v in tf_data.values()):
        return None
    h4_bias = analyze_structure(tf_data['H4'])
    h1_bias = analyze_structure(tf_data['H1'])
    if h4_bias!= h1_bias or h4_bias == "NEUTRAL":
        return None
    m15_sweep = detect_liquidity_sweep(tf_data['M15'])
    m5_sweep = detect_liquidity_sweep(tf_data['M5'])
    m5_pa = detect_price_action(tf_data['M5'])
    price = tf_data['M5']['close'].iloc[-1]
    score = 0
    if h4_bias == h1_bias:
        score+=30
    if m15_sweep or m5_sweep:
        score+=30
    if m5_pa:
        score+=25
    if pre_news:
        score+=15
    if score < 85:
        return None
    ob_level, fvg_level = detect_ob_fvg(tf_data['M5'])
    if h4_bias == "BULLISH" and m5_pa in ["BULLISH_PINBAR", "BULLISH_ENGULFING"]:
        sl = tf_data['M5']['low'].iloc[-3:].min()*0.9995
        risk = price - sl
        return {"symbol": symbol, "bias": "BUY (A+ CONFLUENCE)", "price": price, "sl": sl, "tp1": price + risk*1.5, "tp2": price + risk*3.0, "position_units": calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, price, sl), "session": session_name, "df": tf_data['M5'], "ob_level": ob_level, "fvg_level": fvg_level, "is_scalp": False, "pre_news": bool(pre_news)}
    if h4_bias == "BEARISH" and m5_pa in ["BEARISH_PINBAR", "BEARISH_ENGULFING"]:
        sl = tf_data['M5']['high'].iloc[-3:].max()*1.0005
        risk = sl - price
        return {"symbol": symbol, "bias": "SELL (A+ CONFLUENCE)", "price": price, "sl": sl, "tp1": price - risk*1.5, "tp2": price - risk*3.0, "position_units": calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, price, sl), "session": session_name, "df": tf_data['M5'], "ob_level": ob_level, "fvg_level": fvg_level, "is_scalp": False, "pre_news": bool(pre_news)}
    return None

# --- TRACKER ---
async def track_positions(app: Application):
    global active_trades, daily_stats
    while True:
        try:
            for trade in list(active_trades):
                current_price = exchange.fetch_ticker(trade['symbol'])['last']
                if "BUY" in trade['bias']:
                    if current_price >= trade['tp1'] and not trade.get('tp1_hit'):
                        trade['tp1_hit'] = True
                        await app.bot.send_message(chat_id=CHAT_ID, text=f"TP1 HIT {trade['symbol']} @ {current_price:.2f}", parse_mode="Markdown")
                    elif current_price >= trade['tp2']:
                        daily_stats["wins"]+=1
                        await app.bot.send_message(chat_id=CHAT_ID, text=f"TP2 HIT {trade['symbol']} FULL 1:3", parse_mode="Markdown")
                        active_trades.remove(trade)
                    elif current_price <= trade['sl']:
                        daily_stats["losses"]+=1
                        daily_stats['losses_today']+= ACCOUNT_BALANCE*RISK_PER_TRADE_PCT
                        await app.bot.send_message(chat_id=CHAT_ID, text=f"SL HIT {trade['symbol']}", parse_mode="Markdown")
                        active_trades.remove(trade)
                elif "SELL" in trade['bias']:
                    if current_price <= trade['tp1'] and not trade.get('tp1_hit'):
                        trade['tp1_hit']=True
                        await app.bot.send_message(chat_id=CHAT_ID, text=f"TP1 HIT {trade['symbol']} @ {current_price:.2f}", parse_mode="Markdown")
                    elif current_price <= trade['tp2']:
                        daily_stats["wins"]+=1
                        await app.bot.send_message(chat_id=CHAT_ID, text=f"TP2 HIT {trade['symbol']} FULL 1:3", parse_mode="Markdown")
                        active_trades.remove(trade)
                    elif current_price >= trade['sl']:
                        daily_stats["losses"]+=1
                        daily_stats['losses_today']+= ACCOUNT_BALANCE*RISK_PER_TRADE_PCT
                        await app.bot.send_message(chat_id=CHAT_ID, text=f"SL HIT {trade['symbol']}", parse_mode="Markdown")
                        active_trades.remove(trade)
            await asyncio.sleep(10)
        except Exception as e:
            print(f"Tracking error {e}")
            await asyncio.sleep(10)

# --- DAILY REPORT ---
async def schedule_daily_report(app: Application):
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == 20 and now.minute == 0:
            report = "DAILY REPORT 23:00 EAT\n\n"
            for sym in SYMBOLS:
                df = fetch_data(sym, '1d', 5)
                if df is not None:
                    report+= f"{sym}: {df['close'].iloc[-1]:.2f} H {df['high'].iloc[-1]:.2f} L {df['low'].iloc[-1]:.2f}\n"
            report+= f"\nDrawdown: ${daily_stats['losses_today']:.2f} / ${ACCOUNT_BALANCE*MAX_DAILY_LOSS_PCT:.2f}\nWins: {daily_stats['wins']} Losses: {daily_stats['losses']}"
            await app.bot.send_message(chat_id=CHAT_ID, text=report, parse_mode="Markdown")
            await asyncio.sleep(60)
        await asyncio.sleep(20)

# --- SCANNER ---
async def market_scanner(app: Application):
    print("Scanner LIVE...")
    last_m5 = {}
    while True:
        try:
            for symbol in SYMBOLS:
                m5_df = fetch_data(symbol, '5m', 5)
                if m5_df is None:
                    continue
                cur = m5_df.index[-1]
                if last_m5.get(symbol) == cur:
                    continue
                last_m5[symbol]=cur
                setup = None
                if is_london_scalp_time():
                    setup = evaluate_scalp_london(symbol)
                if not setup:
                    setup = evaluate_aplus_setup(symbol)
                if setup:
                    active_trades.append(setup)
                    chart = generate_tradingview_chart(setup['df'], symbol, setup)
                    caption = f"{setup['bias']} {setup['symbol']}\nEntry {setup['price']:.2f} SL {setup['sl']:.2f} TP1 {setup['tp1']:.2f} TP2 {setup['tp2']:.2f}\nOB {setup.get('ob_level',0):.2f} FVG {setup.get('fvg_level',0):.2f} Size {setup['position_units']}"
                    with open(chart, "rb") as photo:
                        await app.bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=caption, parse_mode="Markdown")
                    if os.path.exists(chart):
                        os.remove(chart)
            await asyncio.sleep(15)
        except Exception as e:
            print(f"Scanner ex {e}")
            await asyncio.sleep(10)

# --- DB ---
def init_db():
    conn = sqlite3.connect("trading_data.db")
    conn.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, bias TEXT, h4_h1_aligned INTEGER, m15_sweep INTEGER, m5_pa INTEGER, result INTEGER)")
    conn.commit()
    conn.close()

# --- MENU COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("StarFX V9 London Scalp Live\n\n/signal - scan now\n/price - prices\n/news - news\n/performance - WR", parse_mode="Markdown")

async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Scanning FAST (London scalp + Swing)...", parse_mode="Markdown")
    found=0
    for sym in SYMBOLS:
        setup = evaluate_scalp_london(sym) if is_london_scalp_time() else None
        if not setup:
            setup = evaluate_aplus_setup(sym)
        if setup:
            found+=1
            chart = generate_tradingview_chart(setup['df'], sym, setup)
            caption = f"{setup['bias']} {sym} Entry {setup['price']:.2f} SL {setup['sl']:.2f} TP1 {setup['tp1']:.2f} TP2 {setup['tp2']:.2f} OB {setup.get('ob_level',0):.2f} FVG {setup.get('fvg_level',0):.2f}"
            with open(chart, "rb") as photo:
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo, caption=caption, parse_mode="Markdown")
            with open(chart, "rb") as photo:
                await context.bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=caption, parse_mode="Markdown")
            if os.path.exists(chart):
                os.remove(chart)
            active_trades.append(setup)
    if found==0:
        await update.message.reply_text("No A+ setup now. London scalp 07-11 UTC active. Try after London open.", parse_mode="Markdown")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg="Live Prices:\n"
    for sym in SYMBOLS:
        df = fetch_data(sym, '5m', 2)
        if df is not None:
            msg+= f"{sym}: {df['close'].iloc[-1]:.2f}\n"
        else:
            msg+= f"{sym}: {last_price_cache.get(sym,'loading')}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    events = fetch_news_window()
    if not events:
        await update.message.reply_text("No high impact USD/GBP news in next 30-120 mins. Safe to trade.", parse_mode="Markdown")
    else:
        txt="High Impact News Alert:\n"
        for ev in events[:5]:
            txt+= f"{ev.get('country')} {ev.get('title')} @ {ev.get('date')}\n"
        await update.message.reply_text(txt, parse_mode="Markdown")

async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = daily_stats['wins'] + daily_stats['losses']
    if total > 0:
        wr = daily_stats['wins']/total*100
    else:
        wr = 0
    if is_london_scalp_time():
        mode_text = "LONDON SCALP"
    else:
        mode_text = "SWING A+"
    if daily_stats['is_circuit_broken']:
        breaker = "BROKEN"
    else:
        breaker = "OK"
    txt = f"WR Performance\n\nWins: {daily_stats['wins']}\nLosses: {daily_stats['losses']}\nWR: {wr:.1f}%\nDaily PnL: ${-daily_stats['losses_today']:.2f}\nBreaker: {breaker}\nActive: {len(active_trades)}/{MAX_CONCURRENT_TRADES}\nMode: {mode_text}"
    await update.message.reply_text(txt, parse_mode="Markdown")

# --- MAIN ---
async def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("signal", signal_command))
    app.add_handler(CommandHandler("scan", signal_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(CommandHandler("performance", performance_command))
    app.add_handler(CommandHandler("WR", performance_command))
    asyncio.create_task(market_scanner(app))
    asyncio.create_task(track_positions(app))
    asyncio.create_task(schedule_daily_report(app))
    print("V9.1 Fixed Online...")
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_
