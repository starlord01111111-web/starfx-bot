import os
os.environ['MPLCONFIGDIR'] = '/tmp'
os.environ['MPLBACKEND'] = 'Agg'

import asyncio
from datetime import datetime, timezone
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import sqlite3
import ccxt
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TELEGRAM_TOKEN =  "8656945768:AAG1avs7PEkGlwJ6VI8cBiOyclOIqmyPjDA"
CHAT_ID =  "-1004365660319"

# V10.7 - WEEKEND MODE
WEEKDAY_SYMBOLS = ["XAU/USD", "GBP/USD", "R_75"]
WEEKEND_SYMBOLS = ["R_75", "R_100", "BOOM1000", "CRASH1000"]
NEWS_CURRENCY = ["USD", "GBP"]

ACCOUNT_BALANCE = 10000.00
RISK_PER_TRADE_PCT = 0.01
MAX_DAILY_LOSS_PCT = 0.03
MAX_CONCURRENT_TRADES = 3

exchange = ccxt.kraken({'enableRateLimit': True})
daily_stats = {"date": None, "losses_today": 0.0, "is_circuit_broken": False, "wins": 0, "losses": 0, "tp1_hits": 0}
active_trades = []
last_signal_time = {}

def get_active_symbols():
    now = datetime.now(timezone.utc)
    # Saturday=5, Sunday=6 - Forex closed
    if now.weekday() >= 5:
        return WEEKEND_SYMBOLS
    return WEEKDAY_SYMBOLS

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, format, *args):
        return

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()
threading.Thread(target=start_dummy_server, daemon=True).start()

def format_price(symbol, price):
    if price is None:
        return "N/A"
    if "XAU" in symbol or "R_" in symbol or "BOOM" in symbol or "CRASH" in symbol:
        return f"{price:.2f}"
    return f"{price:.5f}"

def calculate_position_size(balance, risk_pct, entry, sl):
    risk = abs(entry - sl)
    if risk == 0:
        return 0
    return round((balance * risk_pct) / risk, 4)

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

def fetch_deriv_data(symbol, timeframe, limit=100):
    try:
        import websocket
        gran_map = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
        gran = gran_map.get(timeframe, 300)
        deriv_symbol = symbol
        if "BOOM1000" in symbol:
            deriv_symbol = "BOOM_1000"
        elif "CRASH1000" in symbol:
            deriv_symbol = "CRASH_1000"
        ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089", timeout=10)
        req = {"ticks_history": deriv_symbol, "adjust_start_time": 1, "count": limit, "end": "latest", "granularity": gran, "style": "candles"}
        ws.send(json.dumps(req))
        res = json.loads(ws.recv())
        ws.close()
        if "candles" not in res:
            return None
        candles = res["candles"]
        df = pd.DataFrame(candles)
        df['timestamp'] = pd.to_datetime(df['epoch'], unit='s', utc=True)
        df.set_index('timestamp', inplace=True)
        df = df[['open', 'high', 'low', 'close']].astype(float)
        df['volume'] = 1000
        return df
    except Exception as e:
        print(f"Deriv {symbol} error: {e}")
        return None

def fetch_data(symbol, timeframe, limit=100):
    if "R_" in symbol or "BOOM" in symbol or "CRASH" in symbol:
        return fetch_deriv_data(symbol, timeframe, limit)
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        return df
    except:
        return None

def fetch_current_price(symbol):
    if "R_" in symbol or "BOOM" in symbol or "CRASH" in symbol:
        try:
            import websocket
            deriv_symbol = symbol
            if "BOOM1000" in symbol:
                deriv_symbol = "BOOM_1000"
            elif "CRASH1000" in symbol:
                deriv_symbol = "CRASH_1000"
            ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089", timeout=5)
            ws.send(json.dumps({"ticks": deriv_symbol}))
            res = json.loads(ws.recv())
            ws.close()
            if "tick" in res:
                return float(res["tick"]["quote"])
        except:
            pass
        df = fetch_deriv_data(symbol, '1m', 2)
        if df is not None:
            return df['close'].iloc[-1]
        return None
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker['last']
    except:
        df = fetch_data(symbol, '1m', 2)
        if df is not None:
            return df['close'].iloc[-1]
    return None

def generate_tradingview_chart(df, symbol, setup, filename="chart.png"):
    plot_df = df.iloc[-60:].copy()
    mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit', volume='in')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", gridcolor="#2a2e39", facecolor="#131722")
    display_sym = symbol.replace("R_75", "Volatility 75 Index").replace("R_100", "Volatility 100 Index").replace("BOOM1000", "Boom 1000").replace("CRASH1000", "Crash 1000")
    hlines = [setup['price'], setup['tp1'], setup['tp2'], setup['sl']]
    colors = ['#2962ff', '#00e676', '#00c853', '#ff1744']
    fig, _ = mpf.plot(plot_df, type='candle', style=style, title=f"\n{display_sym} - {setup['bias']}", hlines=dict(hlines=hlines, colors=colors, linestyle='--', linewidths=1.2), savefig=filename, returnfig=True, figratio=(16,9), figscale=1.2)
    plt.close(fig)
    return filename

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
    if c1['close'] > c1['open'] and df.iloc[-3]['close'] < df.iloc[-3]['open']:
        return "BULLISH_ENGULFING"
    if c1['close'] < c1['open'] and df.iloc[-3]['close'] > df.iloc[-3]['open']:
        return "BEARISH_ENGULFING"
    return None

def evaluate_aplus_setup(symbol):
    if not check_circuit_breaker() or len(active_trades) >= MAX_CONCURRENT_TRADES:
        return None
    h4 = fetch_data(symbol, '4h')
    h1 = fetch_data(symbol, '1h')
    m5 = fetch_data(symbol, '5m')
    if h4 is None or h1 is None or m5 is None:
        return None
    h4_bias = analyze_structure(h4)
    h1_bias = analyze_structure(h1)
    if h4_bias!= h1_bias or h4_bias == "NEUTRAL":
        return None
    m5_pa = detect_price_action(m5)
    if not m5_pa:
        return None
    price = m5['close'].iloc[-1]
    atr = (m5['high'] - m5['low']).rolling(14).mean().iloc[-1]
    if pd.isna(atr) or atr == 0:
        atr = price * 0.001

    if "R_" in symbol or "BOOM" in symbol or "CRASH" in symbol:
        min_sl = price * 0.002
        atr_mult = 2.0
        sl = (m5['low'].iloc[-3:].min() - max(min_sl, atr*atr_mult)) if "BULLISH" in m5_pa else (m5['high'].iloc[-3:].max() + max(min_sl, atr*atr_mult))
    elif "XAU" in symbol:
        sl = (m5['low'].iloc[-3:].min() - max(2.0, atr*1.5)) if "BULLISH" in m5_pa else (m5['high'].iloc[-3:].max() + max(2.0, atr*1.5))
    else:
        sl = (m5['low'].iloc[-3:].min() - atr*1.2) if "BULLISH" in m5_pa else (m5['high'].iloc[-3:].max() + atr*1.2)

    risk = abs(price - sl)
    if risk <=0:
        return None
    if "BULLISH" in m5_pa:
        return {"symbol": symbol, "bias": "BUY", "price": price, "sl": sl, "tp1": price + risk*1.5, "tp2": price + risk*3.0, "position_units": calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, price, sl), "df": m5, "tp1_hit": False}
    else:
        return {"symbol": symbol, "bias": "SELL", "price": price, "sl": sl, "tp1": price - risk*1.5, "tp2": price - risk*3.0, "position_units": calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, price, sl), "df": m5, "tp1_hit": False}

async def track_positions(app: Application):
    print("Tracker V10.7 Weekend Mode")
    while True:
        try:
            for trade in list(active_trades):
                current_price = fetch_current_price(trade['symbol'])
                if current_price is None:
                    continue
                sym = trade['symbol']
                display_sym = sym.replace("R_75", "Volatility 75 Index").replace("R_100", "Volatility 100 Index")
                ep = format_price(sym, trade['price'])
                slp = format_price(sym, trade['sl'])
                tp1p = format_price(sym, trade['tp1'])
                tp2p = format_price(sym, trade['tp2'])
                curr_p = format_price(sym, current_price)

                if trade['bias'] == "BUY":
                    if not trade['tp1_hit'] and current_price >= trade['tp1']:
                        trade['tp1_hit'] = True
                        daily_stats['tp1_hits'] += 1
                        try: await app.bot.send_message(chat_id=CHAT_ID, text=f"✅ TP1 HIT (1:1.5) {display_sym}\nEntry {ep} -> Now {curr_p}\nTP1 {tp1p} Secured")
                        except: pass
                    elif current_price >= trade['tp2']:
                        daily_stats['wins'] += 1
                        try: await app.bot.send_message(chat_id=CHAT_ID, text=f"🎯🎯 TP2 HIT FULL (1:3) {display_sym}\nEntry {ep} -> TP2 {tp2p}")
                        except: pass
                        active_trades.remove(trade)
                    elif current_price <= trade['sl']:
                        if not trade['tp1_hit']:
                            daily_stats['losses'] += 1
                            daily_stats['losses_today'] += ACCOUNT_BALANCE * RISK_PER_TRADE_PCT
                        try: await app.bot.send_message(chat_id=CHAT_ID, text=f"🔴 SL HIT {display_sym}\nEntry {ep} SL {slp} Now {curr_p}")
                        except: pass
                        active_trades.remove(trade)
                else:
                    if not trade['tp1_hit'] and current_price <= trade['tp1']:
                        trade['tp1_hit'] = True
                        daily_stats['tp1_hits'] += 1
                        try: await app.bot.send_message(chat_id=CHAT_ID, text=f"✅ TP1 HIT (1:1.5) {display_sym}\nEntry {ep} -> Now {curr_p}\nTP1 {tp1p} Secured")
                        except: pass
                    elif current_price <= trade['tp2']:
                        daily_stats['wins'] += 1
                        try: await app.bot.send_message(chat_id=CHAT_ID, text=f"🎯🎯 TP2 HIT FULL (1:3) {display_sym}\nEntry {ep} -> TP2 {tp2p}")
                        except: pass
                        active_trades.remove(trade)
                    elif current_price >= trade['sl']:
                        if not trade['tp1_hit']:
                            daily_stats['losses'] += 1
                            daily_stats['losses_today'] += ACCOUNT_BALANCE * RISK_PER_TRADE_PCT
                        try: await app.bot.send_message(chat_id=CHAT_ID, text=f"🔴 SL HIT {display_sym}\nEntry {ep} SL {slp} Now {curr_p}")
                        except: pass
                        active_trades.remove(trade)
            await asyncio.sleep(10)
        except Exception as e:
            print(f"Tracker err {e}")
            await asyncio.sleep(10)

async def market_scanner(app: Application):
    print("Scanner V10.7 Weekend Mode Live")
    last_m5 = {}
    while True:
        try:
            symbols_now = get_active_symbols()
            for symbol in symbols_now:
                if symbol in last_signal_time and time.time() - last_signal_time[symbol] < 1800:
                    continue
                m5_df = fetch_data(symbol, '5m', 5)
                if m5_df is None:
                    continue
                cur = m5_df.index[-1]
                if last_m5.get(symbol) == cur:
                    continue
                last_m5[symbol]=cur
                setup = evaluate_aplus_setup(symbol)
                if setup:
                    chart = generate_tradingview_chart(setup['df'], symbol, setup)
                    ep = format_price(symbol, setup['price'])
                    slp = format_price(symbol, setup['sl'])
                    tp1p = format_price(symbol, setup['tp1'])
                    tp2p = format_price(symbol, setup['tp2'])
                    lots = setup['position_units'] / 100000
                    display_sym = symbol.replace("R_75", "Volatility 75 Index").replace("R_100", "Volatility 100 Index").replace("BOOM1000", "Boom 1000").replace("CRASH1000", "Crash 1000")
                    caption = f"🎯 {setup['bias']} {display_sym}\nEntry {ep}\nSL {slp}\nTP1 {tp1p} (1:1.5)\nTP2 {tp2p} (1:3)\nSize {lots:.2f} Lots\nMode: {'WEEKEND' if datetime.now(timezone.utc).weekday()>=5 else 'WEEKDAY'}"
                    try:
                        with open(chart, "rb") as photo:
                            await app.bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=caption)
                        active_trades.append(setup)
                        last_signal_time[symbol] = time.time()
                        print(f"Sent {symbol}")
                    except Exception as e:
                        print(f"Ghost prevented {symbol}: {e}")
                    if os.path.exists(chart):
                        os.remove(chart)
                    await asyncio.sleep(3)
            await asyncio.sleep(30)
        except Exception as e:
            print(f"Scanner err {e}")
            await asyncio.sleep(10)

async def schedule_daily_report(app: Application):
    sent_today = None
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            if now_utc.hour == 20 and now_utc.minute <= 10:
                if sent_today!= now_utc.date():
                    symbols_now = get_active_symbols()
                    report = f"📊 DAILY REPORT 23:00 EAT {now_utc.date()}\nMode: {'WEEKEND' if now_utc.weekday()>=5 else 'WEEKDAY'}\nActive Symbols: {', '.join(symbols_now)}\n\n"
                    for sym in symbols_now:
                        df = fetch_data(sym, '1d', 5)
                        if df is not None:
                            dname = sym.replace("R_75", "V75").replace("R_100", "V100")
                            report+= f"{dname}: {format_price(sym, df['close'].iloc[-1])}\n"
                    total = daily_stats['wins'] + daily_stats['losses']
                    wr = daily_stats['wins']/total*100 if total>0 else 0
                    report+= f"\nWR {wr:.1f}% Wins {daily_stats['wins']} Losses {daily_stats['losses']} TP1 {daily_stats['tp1_hits']}\nActive {len(active_trades)}"
                    try:
                        await app.bot.send_message(chat_id=CHAT_ID, text=report)
                        sent_today = now_utc.date()
                    except:
                        pass
            await asyncio.sleep(60)
        except:
            await asyncio.sleep(60)

def init_db():
    conn = sqlite3.connect("trading_data.db")
    conn.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, bias TEXT, result INTEGER)")
    conn.commit()
    conn.close()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = "WEEKEND" if datetime.now(timezone.utc).weekday()>=5 else "WEEKDAY"
    await update.message.reply_text(f"StarFx V10.7 Live {mode} Mode\nActive: {', '.join(get_active_symbols())}\n/signal /price /report")

async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Scanning {', '.join(get_active_symbols())}...")
    found=0
    for sym in get_active_symbols():
        setup = evaluate_aplus_setup(sym)
        if setup:
            found+=1
            chart = generate_tradingview_chart(setup['df'], sym, setup)
            ep = format_price(sym, setup['price'])
            dname = sym.replace("R_75", "Volatility 75 Index")
            with open(chart, "rb") as photo:
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo, caption=f"{setup['bias']} {dname} Entry {ep}")
            if os.path.exists(chart):
                os.remove(chart)
    if found==0:
        await update.message.reply_text("No A+ setup now.")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=f"💰 Live Prices - {'WEEKEND' if datetime.now(timezone.utc).weekday()>=5 else 'WEEKDAY'} Mode:\n"
    for sym in get_active_symbols():
        p = fetch_current_price(sym)
        dname = sym.replace("R_75", "Volatility 75 Index").replace("R_100", "Volatility 100").replace("BOOM1000", "Boom 1000").replace("CRASH1000", "Crash 1000")
        if p is not None:
            msg+= f"{dname}: {format_price(sym, p)}\n"
    await update.message.reply_text(msg)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbols_now = get_active_symbols()
    report = f"📊 MANUAL REPORT {datetime.now(timezone.utc).date()} Mode: {'WEEKEND' if datetime.now(timezone.utc).weekday()>=5 else 'WEEKDAY'}\n\n"
    for sym in symbols_now:
        df = fetch_data(sym, '1d', 5)
        if df is not None:
            report+= f"{sym}: {format_price(sym, df['close'].iloc[-1])}\n"
    total = daily_stats['wins'] + daily_stats['losses']
    wr = daily_stats['wins']/total*100 if total>0 else 0
    report+= f"\nWR {wr:.1f}% Wins {daily_stats['wins']} Losses {daily_stats['losses']} TP1 {daily_stats['tp1_hits']}"
    await update.message.reply_text(report)
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=report)
    except:
        pass

async def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("signal", signal_command))
    app.add_handler(CommandHandler("scan", signal_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("daily", report_command))
    asyncio.create_task(market_scanner(app))
    asyncio.create_task(track_positions(app))
    asyncio.create_task(schedule_daily_report(app))
    print("V10.7 Online - Weekend Mode")
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
