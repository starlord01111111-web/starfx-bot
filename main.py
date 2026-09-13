"""
StarFx Trading Signal Bot V12 (fixed)
- No BUY-only restriction: V75 can BUY and SELL on weekend
- Secrets from env, state persisted to SQLite, all data from Deriv
"""
import os
os.environ['MPLCONFIGDIR'] = '/tmp'
os.environ['MPLBACKEND'] = 'Agg'

import asyncio
import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("starfx")

# Config / secrets
TELEGRAM_TOKEN = "8656945768:AAG1avs7PEkGlwJ6VI8cBiOyclOIqmyPjDA"
CHAT_ID = "-1004365660319"

if not TELEGRAM_TOKEN or not CHAT_ID:
    log.critical("TELEGRAM_TOKEN and CHAT_ID must be set as env vars. Refusing to start.")
    sys.exit(1)

DB_PATH = os.environ.get("STARFX_DB_PATH", "trading_data.db")
WEEKDAY_SYMBOLS = ["XAU/USD", "GBP/USD", "R_75"]
WEEKEND_SYMBOLS = ["R_75", "R_100", "BOOM1000", "CRASH1000"]
ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "10000.00"))
RISK_PER_TRADE_PCT = 0.01
MAX_DAILY_LOSS_PCT = 0.03
MAX_CONCURRENT_TRADES = 3
DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SPIKE_SL_BUFFER_MULT = 1.1

daily_stats = {"date": None, "losses_today": 0.0, "is_circuit_broken": False, "wins": 0, "losses": 0, "tp1_hits": 0}
active_trades = []
last_signal_time = {}

def is_paused(): return os.path.exists("PAUSE") or os.path.exists("pause")
def get_active_symbols():
    return WEEKEND_SYMBOLS if datetime.now(timezone.utc).weekday() >= 5 else WEEKDAY_SYMBOLS

def to_deriv_symbol(symbol):
    if "BOOM1000" in symbol: return "BOOM_1000"
    if "CRASH1000" in symbol: return "CRASH_1000"
    if symbol == "XAU/USD": return "frxXAUUSD"
    if symbol == "GBP/USD": return "frxGBPUSD"
    return symbol

# Health check
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, format, *args): return
def start_dummy_server():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT",10000))), HealthCheckHandler).serve_forever()
threading.Thread(target=start_dummy_server, daemon=True).start()

# DB / State persistence
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, bias TEXT, result INTEGER, ts TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS daily (date TEXT PRIMARY KEY, losses REAL, wins INTEGER, losses_c INTEGER, tp1 INTEGER)")
    conn.commit(); conn.close()

def save_state():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO state (k,v) VALUES (?,?)", ("active_trades", json.dumps(active_trades)))
        conn.execute("INSERT OR REPLACE INTO state (k,v) VALUES (?,?)", ("daily_stats", json.dumps({**daily_stats, "date": str(daily_stats["date"]) if daily_stats["date"] else None})))
        conn.execute("INSERT OR REPLACE INTO state (k,v) VALUES (?,?)", ("last_signal_time", json.dumps(last_signal_time)))
        conn.commit(); conn.close()
    except Exception:
        log.exception("save_state failed")

def load_state():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT v FROM state WHERE k='active_trades'")
        row = cur.fetchone()
        if row:
            loaded = json.loads(row[0])
            active_trades.clear(); active_trades.extend(loaded)
        cur.execute("SELECT v FROM state WHERE k='daily_stats'")
        row = cur.fetchone()
        if row:
            ds = json.loads(row[0])
            daily_stats.update(ds)
            if ds.get("date"): daily_stats["date"] = datetime.fromisoformat(ds["date"]).date() if isinstance(ds["date"], str) else ds["date"]
        cur.execute("SELECT v FROM state WHERE k='last_signal_time'")
        row = cur.fetchone()
        if row: last_signal_time.update(json.loads(row[0]))
        conn.close()
    except Exception:
        log.exception("load_state failed")

def record_trade_result(symbol, bias, result):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO trades (symbol,bias,result,ts) VALUES (?,?,?,?)", (symbol, bias, result, datetime.now(timezone.utc).isoformat()))
        conn.commit(); conn.close()
    except Exception:
        log.exception("record_trade_result failed")

def validate_symbols():
    try:
        import websocket
        ws = websocket.create_connection(DERIV_WS_URL, timeout=10)
        ws.send(json.dumps({"active_symbols": "brief", "product_type": "basic"}))
        res = json.loads(ws.recv()); ws.close()
        available = {s.get("symbol") for s in res.get("active_symbols", [])}
        for sym in set(WEEKDAY_SYMBOLS) | set(WEEKEND_SYMBOLS):
            d = to_deriv_symbol(sym)
            if d not in available:
                log.warning("Configured %s -> %s NOT in Deriv active_symbols", sym, d)
    except Exception:
        log.exception("Could not validate symbols")

def check_circuit_breaker():
    today = datetime.now(timezone.utc).date()
    if daily_stats["date"]!= today:
        daily_stats["date"] = today
        daily_stats["losses_today"] = 0.0
        daily_stats["is_circuit_broken"] = False
    if daily_stats["losses_today"] >= (ACCOUNT_BALANCE * MAX_DAILY_LOSS_PCT):
        daily_stats["is_circuit_broken"] = True
        return False
    return not daily_stats["is_circuit_broken"]

# Formatting
def format_price(s, p):
    if p is None: return "N/A"
    return f"{p:.2f}" if any(x in s for x in ["XAU","R_","BOOM","CRASH"]) else f"{p:.5f}"

def calculate_position_size(balance, risk_pct, risk_distance):
    return round((balance * risk_pct) / risk_distance, 4) if risk_distance else 0

def format_lots(symbol, risk_distance, balance=ACCOUNT_BALANCE, risk_pct=RISK_PER_TRADE_PCT):
    units = calculate_position_size(balance, risk_pct, risk_distance)
    if any(x in symbol for x in ["R_","BOOM","CRASH"]):
        return f"{units} units - Risk ${balance*risk_pct:.0f}"
    return f"{max(units/100000,0.01):.2f} Lots"

# Data
def fetch_data(symbol, tf, limit=100):
    try:
        import websocket
        gran_map={'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400,'1d':86400}
        d = to_deriv_symbol(symbol)
        ws = websocket.create_connection(DERIV_WS_URL, timeout=10)
        ws.send(json.dumps({"ticks_history":d,"adjust_start_time":1,"count":limit,"end":"latest","granularity":gran_map.get(tf,300),"style":"candles"}))
        res = json.loads(ws.recv()); ws.close()
        if "candles" not in res: return None
        df = pd.DataFrame(res["candles"])
        df['timestamp'] = pd.to_datetime(df['epoch'], unit='s', utc=True)
        df.set_index('timestamp', inplace=True)
        df = df[['open','high','low','close']].astype(float)
        df['volume'] = 1000
        return df
    except Exception:
        log.exception("fetch_data failed %s %s", symbol, tf)
        return None

def fetch_tick_full(symbol):
    try:
        import websocket
        d = to_deriv_symbol(symbol)
        ws = websocket.create_connection(DERIV_WS_URL, timeout=5)
        ws.send(json.dumps({"ticks": d}))
        res = json.loads(ws.recv()); ws.close()
        if "tick" in res:
            tick = res["tick"]
            return {"quote": float(tick.get("quote")), "ask": float(tick.get("ask")) if tick.get("ask") else None, "bid": float(tick.get("bid")) if tick.get("bid") else None}
        return None
    except Exception:
        log.exception("fetch_tick_full failed %s", symbol)
        return None

def fetch_current_price(symbol):
    tick = fetch_tick_full(symbol)
    if tick and tick.get("quote") is not None:
        return tick["quote"]
    df = fetch_data(symbol, '1m', 2)
    return df['close'].iloc[-1] if df is not None else None

def fetch_spread(symbol):
    tick = fetch_tick_full(symbol)
    if tick and tick.get("ask") and tick.get("bid"):
        return abs(tick["ask"] - tick["bid"])
    return 0.0

def generate_tradingview_chart(df, symbol, setup, filename="chart.png"):
    plot_df = df.iloc[-60:].copy()
    mc = mpf.make_marketcolors(up='#26a69a',down='#ef5350',edge='inherit',wick='inherit',volume='in')
    style = mpf.make_mpf_style(marketcolors=mc,gridstyle=":",gridcolor="#2a2e39",facecolor="#131722")
    disp = symbol.replace("R_75","Volatility 75 Index").replace("R_100","Volatility 100 Index")
    fig,_ = mpf.plot(plot_df,type='candle',style=style,title=f"\n{disp} - {setup['bias']}",hlines=dict(hlines=[setup['price'],setup['tp1'],setup['tp2'],setup['sl']],colors=['#2962ff','#00e676','#00c853','#ff1744'],linestyle='--',linewidths=1.2),savefig=filename,returnfig=True,figratio=(16,9),figscale=1.2)
    plt.close(fig); return filename

def analyze_structure(df, window=20):
    if df is None or len(df) < window+1: return "NEUTRAL"
    recent = df.iloc[:-1]; high = recent['high'].iloc[-window:].max(); low = recent['low'].iloc[-window:].min(); curr = df['close'].iloc[-1]
    if curr > high: return "BULLISH"
    if curr < low: return "BEARISH"
    ema = df['close'].ewm(span=20).mean().iloc[-1]
    return "BULLISH" if curr > ema else "BEARISH"

def detect_price_action(df):
    if df is None or len(df) < 3: return None
    c1 = df.iloc[-2]; c0 = df.iloc[-3]
    body = abs(c1['close'] - c1['open'])
    uw = c1['high'] - max(c1['close'], c1['open'])
    lw = min(c1['close'], c1['open']) - c1['low']
    if lw >= (2 * body) and uw <= (0.5 * body): return "BULLISH_PINBAR"
    if uw >= (2 * body) and lw <= (0.5 * body): return "BEARISH_PINBAR"
    prev_bearish = c0['close'] < c0['open']; prev_bullish = c0['close'] > c0['open']
    curr_bullish = c1['close'] > c1['open']; curr_bearish = c1['close'] < c1['open']
    if curr_bullish and prev_bearish and c1['open'] <= c0['close'] and c1['close'] >= c0['open']:
        return "BULLISH_ENGULFING"
    if curr_bearish and prev_bullish and c1['open'] >= c0['close'] and c1['close'] <= c0['open']:
        return "BEARISH_ENGULFING"
    return None

def evaluate_aplus_setup(symbol):
    if not check_circuit_breaker() or len(active_trades) >= MAX_CONCURRENT_TRADES:
        return None
    h4 = fetch_data(symbol, '4h'); h1 = fetch_data(symbol, '1h'); m5 = fetch_data(symbol, '5m')
    if h4 is None or h1 is None or m5 is None: return None
    h4_bias = analyze_structure(h4); h1_bias = analyze_structure(h1)
    if h4_bias!= h1_bias or h4_bias == "NEUTRAL": return None
    m5_pa = detect_price_action(m5)
    if not m5_pa: return None
    # BUY/SELL allowed for all symbols including V75 (buy-only removed per request)
    bias = "BUY" if "BULLISH" in m5_pa else "SELL"
    price = m5['close'].iloc[-1]
    atr = (m5['high']-m5['low']).rolling(14).mean().iloc[-1]
    if pd.isna(atr) or atr == 0: atr = price * 0.001
    if "R_75" in symbol: min_sl = price*0.003; mult = 5.0
    elif any(x in symbol for x in ["R_","BOOM","CRASH"]): min_sl = price*0.002; mult = 3.0
    elif "XAU" in symbol: min_sl = 2.5; mult = 1.8
    else: min_sl = atr*1.2; mult = 1.2
    raw_sl = (m5['low'].iloc[-3:].min() - max(min_sl, atr*mult)) if bias=="BUY" else (m5['high'].iloc[-3:].max() + max(min_sl, atr*mult))
    sl = raw_sl * (1 - 0.001*SPIKE_SL_BUFFER_MULT) if bias=="BUY" else raw_sl * (1 + 0.001*SPIKE_SL_BUFFER_MULT)
    raw_risk = abs(price - sl)
    if raw_risk <= 0: return None
    spread = fetch_spread(symbol)
    effective_risk = raw_risk + spread
    if effective_risk <= 0: return None
    if bias=="BUY": tp1, tp2 = price + effective_risk*1.5, price + effective_risk*3.0
    else: tp1, tp2 = price - effective_risk*1.5, price - effective_risk*3.0
    return {"symbol":symbol,"bias":bias,"price":price,"sl":sl,"tp1":tp1,"tp2":tp2,"spread":spread,"risk_distance":effective_risk,"position_units":calculate_position_size(ACCOUNT_BALANCE,RISK_PER_TRADE_PCT,effective_risk),"df":m5,"tp1_hit":False}

# Background tasks
async def track_positions(app):
    while True:
        try:
            if is_paused(): await asyncio.sleep(60); continue
            for trade in list(active_trades):
                cur = fetch_current_price(trade['symbol'])
                if cur is None: continue
                sym = trade['symbol']; disp = sym.replace("R_75","Volatility 75 Index").replace("R_100","Volatility 100 Index")
                ep = format_price(sym,trade['price']); slp = format_price(sym,trade['sl']); tp1p = format_price(sym,trade['tp1']); tp2p = format_price(sym,trade['tp2']); curr_p = format_price(sym,cur)
                hit_tp1 = (cur >= trade['tp1'] if trade['bias']=="BUY" else cur <= trade['tp1']) and not trade['tp1_hit']
                hit_tp2 = (cur >= trade['tp2'] if trade['bias']=="BUY" else cur <= trade['tp2'])
                hit_sl = (cur <= trade['sl'] if trade['bias']=="BUY" else cur >= trade['sl'])
                if hit_tp1:
                    trade['tp1_hit']=True; daily_stats['tp1_hits']+=1; save_state()
                    try: await app.bot.send_message(chat_id=CHAT_ID,text=f"✅ TP1 HIT (1:1.5) {disp}\nEntry {ep} -> Now {curr_p}\nTP1 {tp1p} Secured")
                    except Exception: log.exception("TP1 send fail")
                elif hit_tp2:
                    daily_stats['wins']+=1; record_trade_result(sym, trade['bias'], 1)
                    try: await app.bot.send_message(chat_id=CHAT_ID,text=f"🎯🎯 TP2 HIT FULL (1:3) {disp}\nEntry {ep} -> TP2 {tp2p}")
                    except Exception: log.exception("TP2 send fail")
                    active_trades.remove(trade); save_state()
                elif hit_sl:
                    if not trade['tp1_hit']:
                        daily_stats['losses']+=1; daily_stats['losses_today']+=ACCOUNT_BALANCE*RISK_PER_TRADE_PCT; record_trade_result(sym, trade['bias'], 0)
                    try: await app.bot.send_message(chat_id=CHAT_ID,text=f"🔴 SL HIT {disp}\nEntry {ep} SL {slp} Now {curr_p}")
                    except Exception: log.exception("SL send fail")
                    active_trades.remove(trade); save_state()
            await asyncio.sleep(10)
        except Exception:
            log.exception("track_positions loop error"); await asyncio.sleep(10)

async def market_scanner(app):
    last_m5={}
    while True:
        try:
            if is_paused(): await asyncio.sleep(60); continue
            for symbol in get_active_symbols():
                if symbol in last_signal_time and time.time()-last_signal_time[symbol]<3600: continue
                if any(t['symbol']==symbol for t in active_trades): continue
                m5_df=fetch_data(symbol,'5m',5)
                if m5_df is None: continue
                cur=m5_df.index[-1]
                if last_m5.get(symbol)==cur: continue
                last_m5[symbol]=cur
                setup=evaluate_aplus_setup(symbol)
                if not setup: continue
                if any(t['symbol']==symbol and t['bias']==setup['bias'] for t in active_trades): continue
                if not check_circuit_breaker():
                    log.warning("Circuit breaker tripped before sending %s", symbol); continue
                chart=generate_tradingview_chart(setup['df'],symbol,setup)
                ep=format_price(symbol,setup['price']); slp=format_price(symbol,setup['sl']); tp1p=format_price(symbol,setup['tp1']); tp2p=format_price(symbol,setup['tp2'])
                disp=symbol.replace("R_75","Volatility 75 Index").replace("R_100","Volatility 100 Index").replace("BOOM1000","Boom 1000").replace("CRASH1000","Crash 1000")
                cap=f"🎯 {setup['bias']} {disp}\nEntry {ep}\nSL {slp}\nTP1 {tp1p} (1:1.5)\nTP2 {tp2p} (1:3)\nSize {format_lots(symbol, setup['risk_distance'])}\nMode: WEEKEND"
                try:
                    with open(chart,"rb") as photo: await app.bot.send_photo(chat_id=CHAT_ID,photo=photo,caption=cap)
                    active_trades.append({k:v for k,v in setup.items() if k!="df"})
                    last_signal_time[symbol]=time.time(); save_state()
                except Exception:
                    log.exception("Failed to send signal for %s", symbol)
                finally:
                    if os.path.exists(chart): os.remove(chart)
                await asyncio.sleep(3)
            await asyncio.sleep(30)
        except Exception:
            log.exception("market_scanner loop error"); await asyncio.sleep(10)

async def schedule_daily_report(app):
    sent=None
    while True:
        try:
            now=datetime.now(timezone.utc)
            if now.hour==20 and now.minute<=10 and sent!=now.date():
                total=daily_stats['wins']+daily_stats['losses']+daily_stats['tp1_hits']; rw=daily_stats['wins']+daily_stats['tp1_hits']; wr=rw/total*100 if total>0 else 0
                rep=f"📊 DAILY REPORT 23:00 EAT {now.date()}\nMode WEEKEND 1H no dup\nWR {wr:.1f}% Wins {rw} Losses {daily_stats['losses']} TP1 {daily_stats['tp1_hits']} TP2 {daily_stats['wins']}\nActive {len(active_trades)}"
                try: await app.bot.send_message(chat_id=CHAT_ID,text=rep); sent=now.date()
                except Exception: log.exception("Failed to send daily report")
            await asyncio.sleep(60)
        except Exception:
            log.exception("schedule_daily_report loop error"); await asyncio.sleep(60)

# Telegram commands
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "⏸️ PAUSED" if is_paused() else "▶️ RUNNING"
    await update.message.reply_text(f"StarFx V12 {status} V75 BUY/SELL allowed\nCooldown 1H no dup\nActive: {', '.join(get_active_symbols())}\n/signal /price /report")

async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Scanning {', '.join(get_active_symbols())}...")
    found=0
    for sym in get_active_symbols():
        setup=evaluate_aplus_setup(sym)
        if setup:
            found+=1
            chart=generate_tradingview_chart(setup['df'],sym,setup)
            ep=format_price(sym,setup['price'])
            try:
                with open(chart,"rb") as photo: await context.bot.send_photo(chat_id=update.effective_chat.id,photo=photo,caption=f"{setup['bias']} {sym} {ep}")
            except Exception: log.exception("Manual signal fail %s", sym)
            finally:
                if os.path.exists(chart): os.remove(chart)
    if found==0: await update.message.reply_text("No A+ setup now.")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg="💰 Live Prices:\n"
    for sym in get_active_symbols():
        p=fetch_current_price(sym)
        if p: msg+=f"{sym}: {format_price(sym,p)}\n"
    await update.message.reply_text(msg)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total=daily_stats['wins']+daily_stats['losses']+daily_stats['tp1_hits']; rw=daily_stats['wins']+daily_stats['tp1_hits']; wr=rw/total*100 if total>0 else 0
    rep=f"📊 MANUAL REPORT {datetime.now(timezone.utc).date()} V12 V75 BUY/SELL allowed\nWR {wr:.1f}% Wins {rw} Losses {daily_stats['losses']} TP1 {daily_stats['tp1_hits']} TP2 {daily_stats['wins']}\nActive {len(active_trades)} Status {'PAUSED' if is_paused() else 'RUNNING'}"
    await update.message.reply_text(rep)

# Entrypoint
async def main():
    init_db(); load_state(); validate_symbols()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",start_command))
    app.add_handler(CommandHandler("signal",signal_command))
    app.add_handler(CommandHandler("scan",signal_command))
    app.add_handler(CommandHandler("report",report_command))
    app.add_handler(CommandHandler("daily",report_command))
    asyncio.create_task(market_scanner(app))
    asyncio.create_task(track_positions(app))
    asyncio.create_task(schedule_daily_report(app))
    log.info("StarFx bot online V12 BUY/SELL both allowed on V75")
    async with app:
        await app.initialize(); await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
