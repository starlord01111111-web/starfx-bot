import os, time, threading, pytz, yfinance as yf, pandas as pd
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import requests

TELEGRAM_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHAT_ID = "-1004365660319"
EAT = pytz.timezone('Africa/Kampala')

SYMBOLS = {"GOLD": "GC=F", "GBPUSD": "GBPUSD=X", "BTCUSD": "BTC-USD", "EURUSD": "EURUSD=X"}

active_trades = []
last_signal_time = {}
today_signals = []
a_plus_count = 0

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except: pass

def get_data(ticker, period="5d", interval="15m"):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except: return None

def detect_structure(df):
    if df is None or len(df) < 30: return "RANGING", None
    last_close = df['Close'].iloc[-1]
    prev_high = df['High'].iloc[-25:-5].max()
    prev_low = df['Low'].iloc[-25:-5].min()
    if last_close > prev_high * 1.0002: return "BULLISH BOS", "BUY"
    elif last_close < prev_low * 0.9998: return "BEARISH BOS", "SELL"
    else: return "RANGING", None

def detect_confirmation(df):
    last = df.iloc[-1]; prev = df.iloc[-2]
    body = abs(last['Close'] - last['Open'])
    rng = last['High'] - last['Low']
    wick_down = min(last['Close'], last['Open']) - last['Low']
    wick_up = last['High'] - max(last['Close'], last['Open'])
    # ENGULFING
    if last['Close'] > last['Open'] and prev['Close'] < prev['Open']:
        if last['Close'] > prev['Open'] and last['Open'] < prev['Close']:
            return "BULLISH ENGULFING", "BUY"
    if last['Close'] < last['Open'] and prev['Close'] > prev['Open']:
        if last['Close'] < prev['Open'] and last['Open'] > prev['Close']:
            return "BEARISH ENGULFING", "SELL"
    if wick_down > body * 1.5: return "BULLISH PIN", "BUY"
    if wick_up > body * 1.5: return "BEARISH PIN", "SELL"
    if rng > 0 and body / rng > 0.6:
        return ("BULLISH MOMENTUM" if last['Close'] > last['Open'] else "BEARISH MOMENTUM"), ("BUY" if last['Close'] > last['Open'] else "SELL")
    return None, None

def calculate_levels(entry, side, symbol, df):
    atr = (df['High'] - df['Low']).tail(14).mean()
    if symbol == "GOLD": sl_dist = max(30.0, atr * 1.5)
    elif symbol == "BTCUSD": sl_dist = max(350, atr * 1.5)
    else: sl_dist = max(0.0008, atr * 1.5)
    if side == "BUY": sl = entry - sl_dist; tp1 = entry + sl_dist*2; tp2 = entry + sl_dist*3
    else: sl = entry + sl_dist; tp1 = entry - sl_dist*2; tp2 = entry - sl_dist*3
    return sl, tp1, tp2

def scan():
    global a_plus_count
    now = datetime.now(EAT)
    if now.hour < 10 or now.hour >= 23: return
    for name, ticker in SYMBOLS.items():
        df = get_data(ticker);
        if df is None: continue
        struct, struct_side = detect_structure(df)
        conf, conf_side = detect_confirmation(df)
        if conf is None: continue
        side = conf_side
        # FIX: BOS must match side
        if "BULLISH BOS" in struct and side!= "BUY": continue
        if "BEARISH BOS" in struct and side!= "SELL": continue
        # FIX: 1 trade per symbol
        if any(t['symbol']==name for t in active_trades): continue
        # FIX: 60 min cooldown
        if name in last_signal_time and time.time() - last_signal_time[name] < 3600: continue
        # GRADE
        if "BOS" in struct and "ENGULFING" in conf: grade = "A+ ⭐ SNIPER"
        elif "BOS" in struct: grade = "A SNIPER"
        else: grade = "B SCALP - RANGING"
        entry = float(df['Close'].iloc[-1])
        sl, tp1, tp2 = calculate_levels(entry, side, name, df)
        emoji = "🟢" if side=="BUY" else "🔴"
        msg = f"{emoji} {grade} - {name} {side} {emoji}\n\nEntry: {entry:.2f}\nSL: {sl:.2f}\nTP1: {tp1:.2f} (1:2)\nTP2: {tp2:.2f} (1:3)\n\n✅ {struct}\n✅ {conf}\n\n⏰ {now.strftime('%H:%M EAT FAST')}"
        send_telegram(msg)
        active_trades.append({'symbol':name,'side':side,'entry':entry,'sl':sl,'tp1':tp1,'tp2':tp2,'tp1_hit':False})
        last_signal_time[name]=time.time(); today_signals.append(1)
        if "A+" in grade: a_plus_count+=1

def watcher():
    while True:
        time.sleep(60)
        for t in active_trades[:]:
            try:
                df = yf.download(SYMBOLS[t['symbol']], period="1d", interval="1m", progress=False)
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                price = float(df['Close'].iloc[-1]); hit=None
                if t['side']=="BUY":
                    if price <= t['sl']: hit=f"❌ SL HIT - {t['symbol']} BUY"
                    elif price >= t['tp2']: hit=f"✅ TP2 WIN! {t['symbol']} BUY 1:3"
                    elif price >= t['tp1'] and not t['tp1_hit']: hit=f"✅ TP1 WIN! {t['symbol']} BUY 1:2 - Move BE"; t['tp1_hit']=True; send_telegram(hit); continue
                else:
                    if price >= t['sl']: hit=f"❌ SL HIT - {t['symbol']} SELL"
                    elif price <= t['tp2']: hit=f"✅ TP2 WIN! {t['symbol']} SELL 1:3"
                    elif price <= t['tp1'] and not t['tp1_hit']: hit=f"✅ TP1 WIN! {t['symbol']} SELL 1:2 - Move BE"; t['tp1_hit']=True; send_telegram(hit); continue
                if hit: send_telegram(hit); active_trades.remove(t)
            except: pass

def daily_report(): send_telegram(f"📊 DAILY REPORT 23:00 EAT\nSignals Today: {len(today_signals)}\nA+ Sniper: {a_plus_count}\nOpen: {len(active_trades)}")
def weekly_report(): send_telegram("📈 WEEKLY REPORT - Sunday 23:30")

sched = BackgroundScheduler(timezone=EAT)
sched.add_job(daily_report, 'cron', hour=23, minute=0)
sched.add_job(weekly_report, 'cron', day_of_week='sun', hour=23, minute=30)
sched.start()

threading.Thread(target=watcher, daemon=True).start()
send_telegram("🚀 V7.9.4 STABLE LIVE - Fixed BOS + Spam + SL/TP + 23:00 Report")
while True:
    try: scan(); time.sleep(40)
    except: time.sleep(10)
