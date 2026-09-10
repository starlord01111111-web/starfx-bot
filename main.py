import os
import time
import sqlite3
import logging
import signal
import requests
import threading
from datetime import datetime
import yfinance as yf
import pandas as pd

# ========= CONFIG =========
BOT_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHANNEL_ID = "-1004365660319"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "120"))
MIN_RRR = float(os.getenv("MIN_RRR", "1.8"))
SYMBOLS = ["frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxXAUUSD", "frxBTCUSD"]
MAP = {"frxEURUSD":"EURUSD=X","frxGBPUSD":"GBPUSD=X","frxUSDJPY":"JPY=X","frxXAUUSD":"GC=F","frxBTCUSD":"BTC-USD"}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# ========= FAST CACHE - 3 SEC BACKTEST =========
CACHE = {}
CACHE_TIME = 600

# ========= DATABASE =========
class Database:
    def __init__(self):
        self.conn = sqlite3.connect("starfx.db", check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, type TEXT, price REAL, score INTEGER, reasons TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        self.conn.commit()
    def is_duplicate(self, symbol, signal_type, cooldown):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM signals WHERE symbol=? AND type=? AND created_at > datetime('now',?)", (symbol, signal_type, f"-{cooldown} minutes"))
        return cur.fetchone() is not None
    def save(self, symbol, signal_type, price, score, reasons):
        self.conn.execute("INSERT INTO signals (symbol,type,price,score,reasons) VALUES (?,?,?,?,?)", (symbol, signal_type, price, score, reasons))
        self.conn.commit()
db = Database()

# ========= TELEGRAM =========
class Telegram:
    def __init__(self):
        self.url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    def send(self, text):
        if not BOT_TOKEN or not CHANNEL_ID: return
        try:
            requests.post(self.url, json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            logging.error(f"TG error: {e}")
    def send_signal(self, symbol, s):
        msg = f"🚀 *{symbol} {s['type']}*\nPrice: `{s['price']:.5f}`\nSL: `{s['sl']:.5f}` | TP: `{s['tp']:.5f}`\nScore: {s['score']}/7 | {s['reasons']}\nRRR 1:2 | V8.3 FIXED"
        self.send(msg)

# ========= DATA WITH CACHE =========
class DataFeed:
    def get(self, symbol, interval, period, retries=2):
        yahoo = MAP.get(symbol, "EURUSD=X")
        for _ in range(retries):
            try:
                df = yf.download(yahoo, period=period, interval=interval, progress=False, auto_adjust=True)
                if df is not None and len(df) > 100:
                    return df
            except: time.sleep(1)
        return None
    def get_mtf(self, symbol):
        return {
            "15m": self.get(symbol, "15m", "5d"),
            "1h": self.get(symbol, "1h", "10d"),
            "4h": self.get(symbol, "4h", "20d")
        }

feed = DataFeed()

def get_cached_mtf(symbol):
    now = time.time()
    if symbol in CACHE and now - CACHE[symbol]['time'] < CACHE_TIME:
        return CACHE[symbol]['data']
    data = feed.get_mtf(symbol)
    if data['15m'] is not None:
        CACHE[symbol] = {'data': data, 'time': now}
    return data

# ========= STRATEGY 7/7 =========
class SignalEngine:
    def __init__(self): self.min_score = 4
    def analyze(self, df15, df1h, df4h):
        if df15 is None or len(df15) < 200: return None
        t4 = self.trend(df4h); t1 = self.trend(df1h)
        if t4 == "NEUTRAL" or t1 == "NEUTRAL" or t4!= t1: return None
        df15['EMA50'] = df15['Close'].ewm(50).mean()
        df15['EMA200'] = df15['Close'].ewm(200).mean()
        df15['ATR'] = self.atr(df15)
        df15['RSI'] = self.rsi(df15['Close'])
        df15['MACD'], df15['SIG'] = self.macd(df15['Close'])
        df15['VOLAVG'] = df15['Volume'].rolling(20).mean()
        last = df15.iloc[-1]
        score = 0; reasons = []
        if t4 == "BULLISH" and last['Close'] > last['EMA50'] > last['EMA200']: score+=1; reasons.append("EMA Bullish")
        elif t4 == "BEARISH" and last['Close'] < last['EMA50'] < last['EMA200']: score+=1; reasons.append("EMA Bearish")
        if t4 == "BULLISH" and last['RSI'] > 55 and last['MACD'] > last['SIG']: score+=1; reasons.append("RSI+MACD Bull")
        elif t4 == "BEARISH" and last['RSI'] < 45 and last['MACD'] < last['SIG']: score+=1; reasons.append("RSI+MACD Bear")
        if last['Volume'] > last['VOLAVG']*1.2: score+=1; reasons.append("Vol Confirm")
        if self.fvg(df15, t4): score+=1; reasons.append("FVG")
        if self.ob(df15): score+=1; reasons.append("OB")
        if self.bos(df15, t4): score+=1; reasons.append("BOS")
        if self.sweep(df15, t4): score+=1; reasons.append("Liq Sweep")
        if not self.valid_session(): return None
        if score >= self.min_score:
            atr = float(last['ATR']); price = float(last['Close'])
            if t4 == "BULLISH":
                return {"type": f"A BUY [Score {score}/7]", "price": price, "sl": price-atr*1.5, "tp": price+atr*3.0, "score": score, "reasons": ", ".join(reasons)}
            else:
                return {"type": f"A SELL [Score {score}/7]", "price": price, "sl": price-atr*1.5, "tp": price-atr*3.0, "score": score, "reasons": ", ".join(reasons)}
        return None
    def trend(self, df):
        if df is None or len(df) < 200: return "NEUTRAL"
        e50 = df['Close'].ewm(50).mean().iloc[-1]; e200 = df['Close'].ewm(200).mean().iloc[-1]
        return "BULLISH" if e50 > e200 else "BEARISH"
    def atr(self, df, p=14):
        hl = df['High']-df['Low']; hc = (df['High']-df['Close'].shift()).abs(); lc = (df['Low']-df['Close'].shift()).abs()
        tr = pd.concat([hl,hc,lc], axis=1).max(axis=1); return tr.rolling(p).mean()
    def rsi(self, s, p=14):
        d=s.diff(); g=d.where(d>0,0).rolling(p).mean(); l=-d.where(d<0,0).rolling(p).mean(); rs=g/l; return 100-(100/(1+rs))
    def macd(self, s):
        e12=s.ewm(12).mean(); e26=s.ewm(26).mean(); m=e12-e26; sig=m.ewm(9).mean(); return m,sig
    def fvg(self, df, t):
        try:
            if t=="BULLISH": return df['Low'].iloc[-1] > df['High'].iloc[-3]
            else: return df['High'].iloc[-1] < df['Low'].iloc[-3]
        except: return False
    def ob(self, df):
        try: return abs(df.iloc[-1]['Close']-df.iloc[-1]['Open']) > df.iloc[-1]['ATR']*0.8
        except: return False
    def bos(self, df, t):
        try:
            if t=="BULLISH": return df['High'].iloc[-1] > df['High'].iloc[-2]
            else: return df['Low'].iloc[-1] < df['Low'].iloc[-2]
        except: return False
    def sweep(self, df, t):
        try:
            rl=df['Low'].iloc[-10:-1].min(); rh=df['High'].iloc[-10:-1].max()
            if t=="BULLISH": return df['Low'].iloc[-1]<rl and df['Close'].iloc[-1]>rl
            else: return df['High'].iloc[-1]>rh and df['Close'].iloc[-1]<rh
        except: return False
    def valid_session(self):
        h = datetime.utcnow().hour; return h in [7,8,9,10,12,13,14,15,16]

class Risk:
    def validate(self, s):
        if not s: return False
        risk = abs(s['price']-s['sl']); reward = abs(s['tp']-s['price'])
        return (reward/risk) >= MIN_RRR if risk!=0 else False

# ========= INSTANCES =========
tg = Telegram()
engine = SignalEngine()
risk = Risk()
running = True

# ========= FIXED COMMANDS + BACKTEST =========
def backtest_fast(symbol):
    start = time.time()
    mtf = get_cached_mtf(symbol)
    df = mtf['15m']
    if df is None: return "No data"
    wins=losses=0
    for i in range(200, len(df)-10):
        sig = engine.analyze(df.iloc[:i], mtf['1h'], mtf['4h'])
        if sig:
            fut = df.iloc[i:i+10]
            if "BUY" in sig['type']:
                if (fut['High'] >= sig['tp']).any(): wins+=1
                elif (fut['Low'] <= sig['sl']).any(): losses+=1
            else:
                if (fut['Low'] <= sig['tp']).any(): wins+=1
                elif (fut['High'] >= sig['sl']).any(): losses+=1
    total = wins+losses
    wr = wins/total*100 if total else 0
    elapsed = time.time() - start
    return f"📊 *Backtest {symbol} FAST*\nTotal: {total}\nW: {wins} L: {losses}\nWR: {wr:.1f}%\nTime: {elapsed:.1f}s ⚡\nCache HIT"

def handle_cmd(text):
    low = text.lower().strip()
    if "V8." in text and "/" in text: # ignore our own menu spam
        return
    if low.startswith("/start"):
        tg.send("🔥 *V8.3 FIXED*\n/status - bot status\n/stats - total signals\n/scan - force scan NOW\n/backtest EURUSD - backtest\n/backtest XAUUSD\n/score 4 or /score 6")
    elif low.startswith("/status"):
        cached = ", ".join(CACHE.keys()) if CACHE else "Empty (first scan loading)"
        tg.send(f"🟢 *V8.3 Running FIXED*\nScore: {engine.min_score}/7\nCache: {cached}\nCooldown: {COOLDOWN_MINUTES}m\nLoop bug: FIXED ✅")
    elif low.startswith("/stats"):
        cur = db.conn.cursor(); cur.execute("SELECT COUNT(*) FROM signals"); total = cur.fetchone()[0]
        tg.send(f"📈 Total Signals Saved: {total}")
    elif low.startswith("/scan"):
        tg.send("🔍 Scanning FAST with cache...")
        found=0
        for sym in SYMBOLS:
            mtf = get_cached_mtf(sym)
            sig = engine.analyze(mtf['15m'], mtf['1h'], mtf['4h'])
            if sig and risk.validate(sig):
                base = sig['type'].split('[')[0].strip()
                if not db.is_duplicate(sym, base, COOLDOWN_MINUTES):
                    tg.send_signal(sym, sig)
                    db.save(sym, base, sig['price'], sig['score'], sig['reasons'])
                    found+=1
        if found==0: tg.send("Scan done - No A+ setup right now. Market waiting.")
        else: tg.send(f"Scan done ⚡ Sent {found} signals")
    elif low.startswith("/backtest"):
        parts = low.split()
        sym = parts[1].upper() if len(parts)>1 else "EURUSD"
        if not sym.startswith("frx"): sym = "frx" + sym.replace("frx","")
        tg.send(f"⏳ Backtesting {sym}...")
        res = backtest_fast(sym)
        tg.send(res)
    elif low.startswith("/score"):
        try:
            ns = int(low.split()[1])
            if 3 <= ns <= 7:
                engine.min_score = ns
                tg.send(f"✅ Score set to {ns}/7 - {'SNIPER' if ns>=6 else 'BALANCED'} 🔥")
        except: tg.send("Use: /score 6")

def cmd_listener():
    offset = 0
    try:
        me = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10).json()
        bot_id = me['result']['id']
    except: bot_id = 0
    logging.info(f"Command listener started, bot_id {bot_id}")
    while running:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=25"
            r = requests.get(url, timeout=30).json()
            for u in r.get('result', []):
                offset = u['update_id'] + 1 # UPDATE FIRST - stops repeat
                msg = u.get('message') # ONLY private DM, ignore channel_post
                if not msg: continue
                from_id = msg.get('from', {}).get('id', 0)
                if from_id == bot_id: continue
                if msg.get('from', {}).get('is_bot'): continue
                text = msg.get('text','')
                if not text or not text.startswith("/"): continue
                logging.info(f"CMD: {text}")
                handle_cmd(text)
        except Exception as e:
            logging.error(f"cmd error {e}")
        time.sleep(2)

# ========= MAIN LOOP =========
def shutdown(a,b):
    global running; running=False
signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

tg.send("✅ *V8.3 FIXED LIVE* 🔥\nLoop Bug Fixed | Commands Working\nType /start in DM")

threading.Thread(target=cmd_listener, daemon=True).start()
logging.info("V8.3 FIXED Started")

while running:
    try:
        for sym in SYMBOLS:
            mtf = get_cached_mtf(sym)
            sig = engine.analyze(mtf['15m'], mtf['1h'], mtf['4h'])
            if sig and risk.validate(sig):
                base = sig['type'].split('[')[0].strip()
                if not db.is_duplicate(sym, base, COOLDOWN_MINUTES):
                    tg.send_signal(sym, sig)
                    db.save(sym, base, sig['price'], sig['score'], sig['reasons'])
                    logging.info(f"SENT {sym} {sig['type']}")
    except Exception as e:
        logging.error(f"Loop error: {e}")
    for _ in range(SCAN_INTERVAL // 5):
        if not running: break
        time.sleep(5)
