import os
import time
import sqlite3
import logging
import signal
import requests
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import yfinance as yf
import pandas as pd

# ========= CONFIG =========
BOT_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHANNEL_ID = "-1004365660319"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "120"))
MIN_RRR = float(os.getenv("MIN_RRR", "1.8"))

SYMBOLS = ["frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxXAUUSD", "frxBTCUSD"]

MAP = {
    "frxEURUSD": "EURUSD=X",
    "frxGBPUSD": "GBPUSD=X",
    "frxUSDJPY": "USDJPY=X",
    "frxXAUUSD": "GC=F",
    "frxBTCUSD": "BTC-USD",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

CACHE = {}
CACHE_TIME = 600


# ========= HEALTH SERVER (Required for Render) =========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"StarFx Bot is running")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logging.info(f"Health server running on port {port}")
    server.serve_forever()


# ========= DATABASE =========
class Database:
    def __init__(self):
        self.conn = sqlite3.connect("starfx.db", check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                type TEXT,
                price REAL,
                score INTEGER,
                reasons TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_cooldown
            ON signals (symbol, type, created_at)
        """)
        self.conn.commit()

    def is_duplicate(self, symbol, signal_type, cooldown):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM signals WHERE symbol=? AND type=? AND created_at > datetime('now', ?)",
            (symbol, signal_type, f"-{cooldown} minutes")
        )
        return cur.fetchone() is not None

    def save(self, symbol, signal_type, price, score, reasons):
        self.conn.execute(
            "INSERT INTO signals (symbol, type, price, score, reasons) VALUES (?,?,?,?,?)",
            (symbol, signal_type, price, score, reasons)
        )
        self.conn.commit()


db = Database()


# ========= TELEGRAM =========
class Telegram:
    def __init__(self):
        self.url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    def send(self, text, chat_id=None):
        target = chat_id if chat_id else CHANNEL_ID
        if not BOT_TOKEN or not target:
            return
        try:
            requests.post(
                self.url,
                json={"chat_id": target, "text": text, "parse_mode": "Markdown"},
                timeout=10
            )
        except Exception as e:
            logging.error(f"Telegram error: {e}")

    def send_signal(self, symbol, s):
        msg = (
            f"🚀 *{symbol} {s['type']}*\n"
            f"Price: `{s['price']:.5f}`\n"
            f"SL: `{s['sl']:.5f}` | TP: `{s['tp']:.5f}`\n"
            f"Score: {s['score']}/7 | {s['reasons']}\n"
            f"RRR ≥ {MIN_RRR} | V8.5 CLEAN"
        )
        self.send(msg)


# ========= DATA FEED =========
class DataFeed:
    def _flatten(self, df):
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df

    def get(self, symbol, interval, period, retries=3):
        yahoo = MAP.get(symbol)
        if not yahoo:
            return None

        for attempt in range(retries):
            try:
                df = yf.download(
                    yahoo,
                    period=period,
                    interval=interval,
                    progress=False,
                    auto_adjust=True,
                    threads=False
                )
                if df is not None and not df.empty and len(df) > 50:
                    df = self._flatten(df)
                    if all(col in df.columns for col in ["Open", "High", "Low", "Close"]):
                        return df
            except Exception as e:
                logging.warning(f"Download error {symbol} {interval}: {e}")
                time.sleep(1.5 * (attempt + 1))
        return None

    def get_mtf(self, symbol):
        return {
            "15m": self.get(symbol, "15m", "7d"),
            "1h": self.get(symbol, "1h", "15d"),
            "4h": self.get(symbol, "4h", "30d"),
        }


feed = DataFeed()


def get_cached_mtf(symbol):
    now = time.time()
    if symbol in CACHE and (now - CACHE[symbol]["time"]) < CACHE_TIME:
        return CACHE[symbol]["data"]

    data = feed.get_mtf(symbol)
    if data.get("15m") is not None:
        CACHE[symbol] = {"data": data, "time": now}
    return data


# ========= STRATEGY =========
class SignalEngine:
    def __init__(self):
        self.min_score = 4

    def analyze(self, df15, df1h, df4h):
        if df15 is None or len(df15) < 200:
            return None

        t4 = self.trend(df4h)
        t1 = self.trend(df1h)

        if t4 == "NEUTRAL" or t1 == "NEUTRAL" or t4 != t1:
            return None

        df = df15.copy()
        df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
        df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
        df["ATR"] = self.atr(df)
        df["RSI"] = self.rsi(df["Close"])
        df["MACD"], df["SIG"] = self.macd(df["Close"])

        if "Volume" in df.columns and df["Volume"].sum() > 0:
            df["VOLAVG"] = df["Volume"].rolling(20).mean()
        else:
            df["VOLAVG"] = 0

        last = df.iloc[-1]
        score = 0
        reasons = []

        # EMA Structure
        if t4 == "BULLISH" and last["Close"] > last["EMA50"] > last["EMA200"]:
            score += 1
            reasons.append("EMA Bull")
        elif t4 == "BEARISH" and last["Close"] < last["EMA50"] < last["EMA200"]:
            score += 1
            reasons.append("EMA Bear")

        # Momentum
        if t4 == "BULLISH" and last["RSI"] > 55 and last["MACD"] > last["SIG"]:
            score += 1
            reasons.append("RSI+MACD")
        elif t4 == "BEARISH" and last["RSI"] < 45 and last["MACD"] < last["SIG"]:
            score += 1
            reasons.append("RSI+MACD")

        # Volume
        if last.get("VOLAVG", 0) > 0 and last["Volume"] > last["VOLAVG"] * 1.2:
            score += 1
            reasons.append("Vol")

        # Structure Concepts
        if self.fvg(df, t4):
            score += 1
            reasons.append("FVG")
        if self.ob(df):
            score += 1
            reasons.append("OB")
        if self.bos(df, t4):
            score += 1
            reasons.append("BOS")
        if self.sweep(df, t4):
            score += 1
            reasons.append("Liq")

        if not self.valid_session():
            return None

        if score < self.min_score:
            return None

        atr = float(last["ATR"])
        price = float(last["Close"])
        if atr <= 0:
            return None

        if t4 == "BULLISH":
            return {
                "type": f"BUY [Score {score}/7]",
                "price": price,
                "sl": price - atr * 1.5,
                "tp": price + atr * 3.0,
                "score": score,
                "reasons": ", ".join(reasons)
            }
        else:
            return {
                "type": f"SELL [Score {score}/7]",
                "price": price,
                "sl": price + atr * 1.5,
                "tp": price - atr * 3.0,
                "score": score,
                "reasons": ", ".join(reasons)
            }

    def trend(self, df):
        if df is None or len(df) < 200:
            return "NEUTRAL"
        try:
            e50 = df["Close"].ewm(span=50, adjust=False).mean().iloc[-1]
            e200 = df["Close"].ewm(span=200, adjust=False).mean().iloc[-1]
            return "BULLISH" if e50 > e200 else "BEARISH"
        except:
            return "NEUTRAL"

    def atr(self, df, period=14):
        hl = df["High"] - df["Low"]
        hc = (df["High"] - df["Close"].shift()).abs()
        lc = (df["Low"] - df["Close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def rsi(self, series, period=14):
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def macd(self, series):
        ema12 = series.ewm(span=12, adjust=False).mean()
        ema26 = series.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal = macd_line.ewm(span=9, adjust=False).mean()
        return macd_line, signal

    def fvg(self, df, trend):
        try:
            if trend == "BULLISH":
                return df["Low"].iloc[-1] > df["High"].iloc[-3]
            return df["High"].iloc[-1] < df["Low"].iloc[-3]
        except:
            return False

    def ob(self, df):
        try:
            body = abs(df["Close"].iloc[-1] - df["Open"].iloc[-1])
            return body > df["ATR"].iloc[-1] * 0.8
        except:
            return False

    def bos(self, df, trend):
        try:
            if trend == "BULLISH":
                return df["High"].iloc[-1] > df["High"].iloc[-2]
            return df["Low"].iloc[-1] < df["Low"].iloc[-2]
        except:
            return False

    def sweep(self, df, trend):
        try:
            recent_low = df["Low"].iloc[-10:-1].min()
            recent_high = df["High"].iloc[-10:-1].max()
            if trend == "BULLISH":
                return df["Low"].iloc[-1] < recent_low and df["Close"].iloc[-1] > recent_low
            return df["High"].iloc[-1] > recent_high and df["Close"].iloc[-1] < recent_high
        except:
            return False

    def valid_session(self):
        hour = datetime.utcnow().hour
        return hour in {7, 8, 9, 10, 12, 13, 14, 15, 16}


class Risk:
    def validate(self, s):
        if not s:
            return False
        risk = abs(s["price"] - s["sl"])
        reward = abs(s["tp"] - s["price"])
        if risk == 0:
            return False
        return (reward / risk) >= MIN_RRR


# ========= INSTANCES =========
tg = Telegram()
engine = SignalEngine()
risk = Risk()
running = True


# ========= BACKTEST =========
def backtest_fast(symbol):
    start = time.time()
    mtf = get_cached_mtf(symbol)
    df = mtf.get("15m")
    if df is None or len(df) < 250:
        return "No sufficient data"

    wins = losses = 0
    for i in range(200, len(df) - 15):
        sig = engine.analyze(df.iloc[:i], mtf["1h"], mtf["4h"])
        if not sig:
            continue
        future = df.iloc[i:i+12]
        if "BUY" in sig["type"]:
            hit_tp = (future["High"] >= sig["tp"]).any()
            hit_sl = (future["Low"] <= sig["sl"]).any()
        else:
            hit_tp = (future["Low"] <= sig["tp"]).any()
            hit_sl = (future["High"] >= sig["sl"]).any()
        if hit_tp:
            wins += 1
        elif hit_sl:
            losses += 1

    total = wins + losses
    wr = (wins / total * 100) if total else 0.0
    elapsed = time.time() - start
    return (
        f"📊 *Backtest {symbol}*\n"
        f"Total: {total}\n"
        f"Wins: {wins} | Losses: {losses}\n"
        f"Winrate: {wr:.1f}%\n"
        f"Time: {elapsed:.1f}s"
    )


# ========= COMMANDS =========
def handle_cmd(text, chat_id):
    low = text.lower().strip()

    if "V8." in text and "/" in text and len(text) > 20:
        return

    if low.startswith("/start"):
        tg.send(
            "🔥 *V8.5 CLEAN LIVE*\n"
            "/status - bot status\n"
            "/stats - total signals\n"
            "/scan or /signal - force scan\n"
            "/backtest EURUSD\n"
            "/backtest XAUUSD\n"
            "/score 3 - more signals\n"
            "/score 6 - sniper mode\n"
            "/price - Gold price",
            chat_id
        )

    elif low.startswith("/status"):
        cached = ", ".join(CACHE.keys()) if CACHE else "Empty - run /scan"
        tg.send(
            f"🟢 *V8.5 Running*\n"
            f"Min score: {engine.min_score}/7\n"
            f"Cache: {cached}\n"
            f"Cooldown: {COOLDOWN_MINUTES}m",
            chat_id
        )

    elif low.startswith(("/stats", "/performance")):
        cur = db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM signals")
        total = cur.fetchone()[0]
        tg.send(f"📈 Total signals: {total}\nScore filter: {engine.min_score}/7", chat_id)

    elif low.startswith(("/scan", "/signal")):
        tg.send("🔍 Scanning...", chat_id)
        found = 0
        for sym in SYMBOLS:
            try:
                mtf = get_cached_mtf(sym)
                sig = engine.analyze(mtf["15m"], mtf["1h"], mtf["4h"])
                if sig and risk.validate(sig):
                    base = sig["type"].split("[")[0].strip()
                    if not db.is_duplicate(sym, base, COOLDOWN_MINUTES):
                        tg.send_signal(sym, sig)
                        db.save(sym, base, sig["price"], sig["score"], sig["reasons"])
                        found += 1
            except Exception as e:
                logging.error(f"Scan error {sym}: {e}")

        if found == 0:
            tg.send("✅ No A+ setup right now.\nTry `/score 3` for more signals.", chat_id)
        else:
            tg.send(f"⚡ Sent {found} signal(s)", chat_id)

    elif low.startswith("/backtest"):
        parts = low.split()
        raw = parts[1].upper() if len(parts) > 1 else "EURUSD"
        sym = "frx" + raw.replace("FRX", "") if not raw.startswith("FRX") else raw.lower()
        tg.send(f"⏳ Backtesting {sym}...", chat_id)
        tg.send(backtest_fast(sym), chat_id)

    elif low.startswith("/score"):
        try:
            ns = int(low.split()[1])
            if 3 <= ns <= 7:
                engine.min_score = ns
                mode = "SNIPER" if ns >= 6 else "BALANCED" if ns >= 4 else "LOOSE"
                tg.send(f"✅ Score set to {ns}/7 → {mode}", chat_id)
            else:
                tg.send("Use score between 3 and 7", chat_id)
        except:
            tg.send("Usage: /score 4", chat_id)

    elif low.startswith("/price"):
        try:
            mtf = get_cached_mtf("frxXAUUSD")
            if mtf and mtf["15m"] is not None:
                price = float(mtf["15m"]["Close"].iloc[-1])
                tg.send(f"💰 *XAUUSD*: `{price:.2f}`", chat_id)
            else:
                tg.send("Price data not ready - try again in \~30s", chat_id)
        except Exception as e:
            logging.error(f"Price error: {e}")
            tg.send("Price error - try /scan first", chat_id)


def cmd_listener():
    offset = 0
    bot_id = 0
    try:
        me = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10).json()
        bot_id = me["result"]["id"]
    except Exception as e:
        logging.warning(f"Could not get bot id: {e}")

    logging.info(f"Command listener started (bot_id={bot_id})")

    while running:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=25"
            r = requests.get(url, timeout=30).json()
            for update in r.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue
                if msg.get("from", {}).get("id") == bot_id or msg.get("from", {}).get("is_bot"):
                    continue
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if text and text.startswith("/"):
                    handle_cmd(text, chat_id)
        except Exception as e:
            logging.error(f"Listener error: {e}")
        time.sleep(1.5)


def shutdown(signum, frame):
    global running
    running = False
    logging.info("Shutdown signal received")


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


# ========= MAIN =========
if __name__ == "__main__":
    if not BOT_TOKEN:
        logging.warning("BOT_TOKEN is empty")

    # Start health server for Render
    threading.Thread(target=start_health_server, daemon=True).start()

    tg.send("✅ *V8.5 CLEAN LIVE*\nType /start in bot DM")

    threading.Thread(target=cmd_listener, daemon=True).start()
    logging.info("V8.5 CLEAN started")

    while running:
        try:
            for sym in SYMBOLS:
                mtf = get_cached_mtf(sym)
                sig = engine.analyze(mtf.get("15m"), mtf.get("1h"), mtf.get("4h"))
                if sig and risk.validate(sig):
                    base = sig["type"].split("[")[0].strip()
                    if not db.is_duplicate(sym, base, COOLDOWN_MINUTES):
                        tg.send_signal(sym, sig)
                        db.save(sym, base, sig["price"], sig["score"], sig["reasons"])
                        logging.info(f"SENT {sym} {sig['type']}")
        except Exception as e:
            logging.error(f"Main loop error: {e}")

        for _ in range(SCAN_INTERVAL // 5):
            if not running:
                break
            time.sleep(5)

    logging.info("Bot stopped cleanly")
