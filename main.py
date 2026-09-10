import os
import time
import sqlite3
import logging
import signal
import requests
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import yfinance as yf
import pandas as pd

# Try importing psycopg2 for production PostgreSQL support
try:
    import psycopg2
    import psycopg2.pool
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

# ========= CONFIGURATION =========
BOT_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHANNEL_ID = "-1004365660319"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "120"))
MIN_RRR = float(os.getenv("MIN_RRR", "1.8"))
DATABASE_URL = os.getenv("DATABASE_URL", "")

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
    format="%(asctime)s - [%(levelname)s] - %(message)s"
)

CACHE = {}
CACHE_TIME = 300  # 5 minutes cache for market data


# ========= HEALTH SERVER FOR HOSTING =========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"StarFx Bot V9.0 Engine Operational")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logging.info(f"Health check endpoint active on port {port}")
    server.serve_forever()


# ========= PRODUCTION DATABASE ENGINE =========
class ProductionDatabase:
    def __init__(self):
        self.is_postgres = HAS_POSTGRES and DATABASE_URL.startswith("postgres")
        if self.is_postgres:
            logging.info("Initializing PostgreSQL Connection Pool...")
            self.pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)
        else:
            logging.info("Using Local SQLite Storage (starfx.db)...")
            self.db_path = "starfx.db"
        self._init_db()

    def get_connection(self):
        if self.is_postgres:
            return self.pool.getconn()
        return sqlite3.connect(self.db_path, timeout=15)

    def release_connection(self, conn):
        if self.is_postgres:
            self.pool.putconn(conn)
        else:
            conn.close()

    def _init_db(self):
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        id SERIAL PRIMARY KEY,
                        symbol VARCHAR(32),
                        type VARCHAR(32),
                        price DOUBLE PRECISION,
                        score INT,
                        reasons TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_signals_cd ON signals (symbol, type, created_at);
                """)
            else:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT,
                        type TEXT,
                        price REAL,
                        score INTEGER,
                        reasons TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_signals_cd ON signals (symbol, type, created_at);
                """)
            conn.commit()
        finally:
            self.release_connection(conn)

    def is_duplicate(self, symbol, signal_type, cooldown_mins):
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    "SELECT 1 FROM signals WHERE symbol=%s AND type=%s AND created_at > NOW() - INTERVAL %s;",
                    (symbol, signal_type, f"{cooldown_mins} minutes")
                )
            else:
                cur.execute(
                    "SELECT 1 FROM signals WHERE symbol=? AND type=? AND created_at > datetime('now', ?);",
                    (symbol, signal_type, f"-{cooldown_mins} minutes")
                )
            return cur.fetchone() is not None
        finally:
            self.release_connection(conn)

    def save(self, symbol, signal_type, price, score, reasons):
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            placeholder = "%s, %s, %s, %s, %s" if self.is_postgres else "?, ?, ?, ?, ?"
            cur.execute(
                f"INSERT INTO signals (symbol, type, price, score, reasons) VALUES ({placeholder})",
                (symbol, signal_type, price, score, reasons)
            )
            conn.commit()
        finally:
            self.release_connection(conn)

    def get_total_count(self):
        conn = self.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM signals;")
            return cur.fetchone()[0]
        finally:
            self.release_connection(conn)


db = ProductionDatabase()


# ========= TELEGRAM INTERFACE =========
class Telegram:
    def __init__(self):
        self.base_url = f"https://api.telegram.org/bot{BOT_TOKEN}"

    def send(self, text, chat_id=None):
        target = chat_id if chat_id else CHANNEL_ID
        if not BOT_TOKEN or not target:
            return
        url = f"{self.base_url}/sendMessage"
        for attempt in range(3):
            try:
                res = requests.post(
                    url,
                    json={"chat_id": target, "text": text, "parse_mode": "Markdown"},
                    timeout=8
                )
                if res.status_code == 200:
                    break
            except Exception as e:
                logging.error(f"Telegram retry {attempt+1}/3 failed: {e}")
                time.sleep(1)

    def send_signal(self, symbol, s):
        msg = (
            f"🚀 *{symbol} {s['type']}*\n"
            f"💰 Entry: `{s['price']:.5f}`\n"
            f"🛑 SL: `{s['sl']:.5f}` | 🎯 TP: `{s['tp']:.5f}`\n"
            f"📊 Score: *{s['score']}/7* | Confluences: _{s['reasons']}_\n"
            f"⚖️ RRR ≥ {MIN_RRR} | V9.0 Institutional Engine"
        )
        self.send(msg)


# ========= DATA FEED PROVIDER =========
class DataFeed:
    def _clean_df(self, df):
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()

    def get(self, symbol, interval, period, retries=3):
        ticker = MAP.get(symbol)
        if not ticker:
            return None

        for attempt in range(retries):
            try:
                df = yf.download(
                    ticker,
                    period=period,
                    interval=interval,
                    progress=False,
                    auto_adjust=True,
                    threads=False
                )
                if df is not None and not df.empty and len(df) > 50:
                    df = self._clean_df(df)
                    if all(col in df.columns for col in ["Open", "High", "Low", "Close"]):
                        return df
            except Exception as e:
                logging.warning(f"Data download retry {attempt+1} for {symbol}: {e}")
                time.sleep(1.5 * (attempt + 1))
        return None

    def get_mtf(self, symbol):
        return {
            "15m": self.get(symbol, "15m", "5d"),
            "1h": self.get(symbol, "1h", "14d"),
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


# ========= VECTORIZED STRATEGY ENGINE =========
class SignalEngine:
    def __init__(self):
        self.min_score = 4

    def compute_indicators(self, df):
        """Pre-calculates all technical indicators across the entire vector."""
        d = df.copy()
        d["EMA50"] = d["Close"].ewm(span=50, adjust=False).mean()
        d["EMA200"] = d["Close"].ewm(span=200, adjust=False).mean()
        
        # ATR
        hl = d["High"] - d["Low"]
        hc = (d["High"] - d["Close"].shift()).abs()
        lc = (d["Low"] - d["Close"].shift()).abs()
        d["ATR"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean()

        # RSI
        delta = d["Close"].diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        d["RSI"] = 100 - (100 / (1 + rs))

        # MACD
        ema12 = d["Close"].ewm(span=12, adjust=False).mean()
        ema26 = d["Close"].ewm(span=26, adjust=False).mean()
        d["MACD"] = ema12 - ema26
        d["SIG"] = d["MACD"].ewm(span=9, adjust=False).mean()

        # Volume Average
        if "Volume" in d.columns and d["Volume"].sum() > 0:
            d["VOLAVG"] = d["Volume"].rolling(20).mean()
        else:
            d["VOLAVG"] = 0

        return d

    def trend(self, df):
        if df is None or len(df) < 200:
            return "NEUTRAL"
        try:
            e50 = df["Close"].ewm(span=50, adjust=False).mean().iloc[-1]
            e200 = df["Close"].ewm(span=200, adjust=False).mean().iloc[-1]
            return "BULLISH" if e50 > e200 else "BEARISH"
        except Exception:
            return "NEUTRAL"

    def analyze(self, df15_raw, df1h, df4h):
        if df15_raw is None or len(df15_raw) < 200:
            return None

        t4 = self.trend(df4h)
        t1 = self.trend(df1h)

        if t4 == "NEUTRAL" or t1 == "NEUTRAL" or t4 != t1:
            return None

        df = self.compute_indicators(df15_raw)
        last = df.iloc[-1]
        score = 0
        reasons = []

        # 1. EMA Trend Confluence
        if t4 == "BULLISH" and last["Close"] > last["EMA50"] > last["EMA200"]:
            score += 1
            reasons.append("EMA Bull Alignment")
        elif t4 == "BEARISH" and last["Close"] < last["EMA50"] < last["EMA200"]:
            score += 1
            reasons.append("EMA Bear Alignment")

        # 2. Momentum
        if t4 == "BULLISH" and last["RSI"] > 52 and last["MACD"] > last["SIG"]:
            score += 1
            reasons.append("RSI+MACD Momentum")
        elif t4 == "BEARISH" and last["RSI"] < 48 and last["MACD"] < last["SIG"]:
            score += 1
            reasons.append("RSI+MACD Momentum")

        # 3. Volume Expansion
        if last.get("VOLAVG", 0) > 0 and last["Volume"] > last["VOLAVG"] * 1.15:
            score += 1
            reasons.append("Volume Spike")

        # Smart Money Concepts Structure Checks
        if self._fvg(df, t4):
            score += 1
            reasons.append("Fair Value Gap")
        if self._ob(df):
            score += 1
            reasons.append("Order Block")
        if self._bos(df, t4):
            score += 1
            reasons.append("Break of Structure")
        if self._sweep(df, t4):
            score += 1
            reasons.append("Liquidity Sweep")

        if not self._valid_session():
            return None

        if score < self.min_score:
            return None

        atr = float(last["ATR"])
        price = float(last["Close"])
        if pd.isna(atr) or atr <= 0:
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

    def _fvg(self, df, trend):
        try:
            if trend == "BULLISH":
                return df["Low"].iloc[-1] > df["High"].iloc[-3]
            return df["High"].iloc[-1] < df["Low"].iloc[-3]
        except Exception:
            return False

    def _ob(self, df):
        try:
            body = abs(df["Close"].iloc[-1] - df["Open"].iloc[-1])
            return body > df["ATR"].iloc[-1] * 0.75
        except Exception:
            return False

    def _bos(self, df, trend):
        try:
            if trend == "BULLISH":
                return df["High"].iloc[-1] > df["High"].iloc[-2]
            return df["Low"].iloc[-1] < df["Low"].iloc[-2]
        except Exception:
            return False

    def _sweep(self, df, trend):
        try:
            recent_low = df["Low"].iloc[-10:-1].min()
            recent_high = df["High"].iloc[-10:-1].max()
            if trend == "BULLISH":
                return df["Low"].iloc[-1] < recent_low and df["Close"].iloc[-1] > recent_low
            return df["High"].iloc[-1] > recent_high and df["Close"].iloc[-1] < recent_high
        except Exception:
            return False

    def _valid_session(self):
        """Active liquidity windows: London & New York sessions (UTC)."""
        hour = datetime.now(timezone.utc).hour
        return hour in {7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19}


class RiskManager:
    def validate(self, s):
        if not s:
            return False
        risk = abs(s["price"] - s["sl"])
        reward = abs(s["tp"] - s["price"])
        if risk == 0:
            return False
        return (reward / risk) >= MIN_RRR


# ========= INSTANTIATIONS =========
tg = Telegram()
engine = SignalEngine()
risk = RiskManager()
running = True


# ========= OPTIMIZED VECTORIZED BACKTEST ENGINE =========
def backtest_fast(symbol):
    start = time.time()
    mtf = get_cached_mtf(symbol)
    df = mtf.get("15m") if mtf else None
    if df is None or len(df) < 250:
        return "Insufficient data available for backtesting."

    # Compute indicators ONCE for the whole dataset
    df_calc = engine.compute_indicators(df)
    wins = losses = 0

    for i in range(200, len(df_calc) - 15):
        # Pass vector slices without triggering indicator recalculations
        sig = engine.analyze(df_calc.iloc[:i], mtf["1h"], mtf["4h"])
        if not sig:
            continue

        future = df_calc.iloc[i:i+12]
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
    wr = (wins / total * 100) if total > 0 else 0.0
    elapsed = time.time() - start
    return (
        f"📊 *Vector Backtest Result: {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Total Trades: `{total}`\n"
        f"Wins: `{wins}` | Losses: `{losses}`\n"
        f"Winrate: *{wr:.1f}%*\n"
        f"Execution Time: `{elapsed:.2f}s`"
    )


# ========= TELEGRAM COMMAND HANDLER =========
def handle_cmd(text, chat_id):
    low = text.lower().strip()

    if low.startswith("/start"):
        tg.send(
            "🔥 *StarFx Institutional Engine V9.0*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "/status - Bot health & parameters\n"
            "/stats - Total historical signals\n"
            "/scan - Run immediate market scan\n"
            "/backtest EURUSD - Backtest vector model\n"
            "/score 5 - Adjust confluence threshold (3-7)\n"
            "/price - Live Gold spot pricing",
            chat_id
        )

    elif low.startswith("/status"):
        cached = ", ".join(CACHE.keys()) if CACHE else "Empty Cache"
        tg.send(
            f"🟢 *System Operational*\n"
            f"Database: `{'PostgreSQL' if db.is_postgres else 'SQLite'}`\n"
            f"Min Confluence Score: `{engine.min_score}/7`\n"
            f"Active Cache: `{cached}`\n"
            f"Signal Cooldown: `{COOLDOWN_MINUTES}m`",
            chat_id
        )

    elif low.startswith(("/stats", "/performance")):
        total = db.get_total_count()
        tg.send(f"📈 Total Executed Signals: `{total}`\nCurrent Min Score: `{engine.min_score}/7`", chat_id)

    elif low.startswith(("/scan", "/signal")):
        tg.send("🔍 Scanning multi-timeframe liquidity...", chat_id)
        found = 0
        for sym in SYMBOLS:
            try:
                mtf = get_cached_mtf(sym)
                if not mtf:
                    continue
                sig = engine.analyze(mtf["15m"], mtf["1h"], mtf["4h"])
                if sig and risk.validate(sig):
                    base = sig["type"].split("[")[0].strip()
                    if not db.is_duplicate(sym, base, COOLDOWN_MINUTES):
                        tg.send_signal(sym, sig)
                        db.save(sym, base, sig["price"], sig["score"], sig["reasons"])
                        found += 1
            except Exception as e:
                logging.error(f"Scan failure on {sym}: {e}")

        if found == 0:
            tg.send("✅ No high-confluence setups detected.\nAdjust threshold via `/score 3` if required.", chat_id)
        else:
            tg.send(f"⚡ Successfully broadcasted {found} setup(s).", chat_id)

    elif low.startswith("/backtest"):
        parts = low.split()
        raw = parts[1].upper() if len(parts) > 1 else "EURUSD"
        sym = "frx" + raw.replace("FRX", "") if not raw.startswith("FRX") else raw.lower()
        tg.send(f"⏳ Running vectorized engine backtest on `{sym}`...", chat_id)
        tg.send(backtest_fast(sym), chat_id)

    elif low.startswith("/score"):
        try:
            ns = int(low.split()[1])
            if 3 <= ns <= 7:
                engine.min_score = ns
                mode = "SNIPER MODE" if ns >= 6 else "BALANCED MODE" if ns >= 4 else "HIGH FREQUENCY"
                tg.send(f"✅ Confluence filter adjusted to `{ns}/7` [{mode}]", chat_id)
            else:
                tg.send("⚠️ Please specify a score between 3 and 7.", chat_id)
        except Exception:
            tg.send("Usage: `/score 5`", chat_id)

    elif low.startswith("/price"):
        try:
            mtf = get_cached_mtf("frxXAUUSD")
            if mtf and mtf.get("15m") is not None:
                price = float(mtf["15m"]["Close"].iloc[-1])
                tg.send(f"💰 *XAUUSD Spot*: `{price:.2f}`", chat_id)
            else:
                tg.send("Fetching current market price...", chat_id)
        except Exception as e:
            logging.error(f"Price check error: {e}")
            tg.send("Unable to resolve price data.", chat_id)


def cmd_listener():
    offset = 0
    bot_id = 0
    try:
        me = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10).json()
        bot_id = me.get("result", {}).get("id", 0)
    except Exception as e:
        logging.warning(f"Failed Telegram authentication check: {e}")

    logging.info(f"Command poller initialized (Bot ID: {bot_id})")

    while running:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            r = requests.get(url, timeout=25).json()
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
            logging.error(f"Polling loop exception: {e}")
        time.sleep(1.0)
    
# ========= MAIN EXECUTION LOOP =========
def signal_handler(sig, frame):
    global running
    logging.info("Shutting down StarFx Engine...")
    running = False


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start Health Check Server Thread
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    # Start Telegram Command Listener Thread
    listener_thread = threading.Thread(target=cmd_listener, daemon=True)
    listener_thread.start()

    logging.info("StarFx Engine fully booted. Active monitoring sequence started.")

    while running:
        for sym in SYMBOLS:
            try:
                mtf = get_cached_mtf(sym)
                if not mtf:
                    continue
                sig = engine.analyze(mtf["15m"], mtf["1h"], mtf["4h"])
                if sig and risk.validate(sig):
                    base = sig["type"].split("[")[0].strip()
                    if not db.is_duplicate(sym, base, COOLDOWN_MINUTES):
                        tg.send_signal(sym, sig)
                        db.save(sym, base, sig["price"], sig["score"], sig["reasons"])
            except Exception as e:
                logging.error(f"Execution error on {sym}: {e}")

        time.sleep(SCAN_INTERVAL)
                
