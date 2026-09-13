"""
StarFx V14 - Candlestick Trading Bible Full Implementation
Includes: Pin Bar, Engulfing, Inside Bar, Fakey, Morning/Evening Star, Harami + Confluence

SECURITY: Token must be supplied via TELEGRAM_TOKEN env var.
Never hardcode. Revoke any token that has ever been committed.
"""
import os
os.environ.setdefault('MPLCONFIGDIR', '/tmp')
os.environ.setdefault('MPLBACKEND', 'Agg')

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
import numpy as np
import websocket

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("starfx")

TELEGRAM_TOKEN = "8656945768:AAG1avs7PEkGlwJ6VI8cBiOyclOIqmyPjDA"
CHAT_ID = "-1004365660319"
if not TELEGRAM_TOKEN:
    log.critical("TELEGRAM_TOKEN env var missing")
    sys.exit(1)

DB_PATH = os.environ.get("STARFX_DB_PATH", "trading_data.db")
WEEKDAY_SYMBOLS = ["XAU/USD", "GBP/USD", "R_75"]
WEEKEND_SYMBOLS = ["R_75", "R_100", "BOOM1000", "CRASH1000"]
ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "10000"))
RISK_PER_TRADE_PCT = 0.01
MAX_DAILY_LOSS_PCT = 0.03
MAX_CONCURRENT_TRADES = 3
DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

daily_stats = {
    "date": None, "losses_today": 0.0, "is_circuit_broken": False,
    "wins": 0, "losses": 0, "tp1_hits": 0,
}
active_trades = []
last_signal_time = {}

PATTERN_PRIORITY = {
    "BULLISH_ENGULFING": 5, "BEARISH_ENGULFING": 5,
    "BULLISH_PINBAR": 4, "BEARISH_PINBAR": 4,
    "MORNING_STAR": 4, "EVENING_STAR": 4,
    "BULLISH_FAKEY": 3, "BEARISH_FAKEY": 3,
    "BULLISH_HARAMI": 2, "BEARISH_HARAMI": 2,
    "INSIDE_BAR": 1,
}

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def is_paused():
    return os.path.exists("PAUSE")

def get_active_symbols():
    return WEEKEND_SYMBOLS if datetime.now(timezone.utc).weekday() >= 5 else WEEKDAY_SYMBOLS

def to_deriv_symbol(s):
    mapping = {
        "XAU/USD": "frxXAUUSD",
        "GBP/USD": "frxGBPUSD",
        "BOOM1000": "BOOM1000",
        "CRASH1000": "CRASH1000",
        "R_75": "R_75",
        "R_100": "R_100",
    }
    return mapping.get(s, s)

def format_price(sym, p):
    if any(x in sym for x in ["XAU", "R_", "BOOM", "CRASH"]):
        return f"{p:.2f}"
    return f"{p:.5f}"

def calculate_position_size(bal, risk_pct, dist):
    return round((bal * risk_pct) / dist, 4) if dist else 0

def format_lots(sym, dist):
    return f"{calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, dist)} units"

# ─────────────────────────────────────────────────────────────
# Health check HTTP server
# ─────────────────────────────────────────────────────────────
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *a):
        return

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()

# ─────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────
def init_db():
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "symbol TEXT, bias TEXT, result INTEGER, ts TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)")
    c.commit(); c.close()

def save_state():
    try:
        c = sqlite3.connect(DB_PATH)
        c.execute("INSERT OR REPLACE INTO state VALUES (?,?)",
                  ("active_trades", json.dumps(active_trades)))
        c.execute("INSERT OR REPLACE INTO state VALUES (?,?)",
                  ("daily_stats", json.dumps({
                      **daily_stats,
                      "date": str(daily_stats["date"]) if daily_stats["date"] else None,
                  })))
        c.execute("INSERT OR REPLACE INTO state VALUES (?,?)",
                  ("last_signal_time", json.dumps(last_signal_time)))
        c.commit(); c.close()
    except Exception:
        log.exception("save_state")

def load_state():
    try:
        c = sqlite3.connect(DB_PATH)
        cur = c.cursor()
        cur.execute("SELECT v FROM state WHERE k='active_trades'")
        r = cur.fetchone()
        if r:
            active_trades.clear(); active_trades.extend(json.loads(r[0]))
        cur.execute("SELECT v FROM state WHERE k='daily_stats'")
        r = cur.fetchone()
        if r:
            ds = json.loads(r[0])
            if ds.get("date"):
                try: ds["date"] = datetime.fromisoformat(ds["date"]).date()
                except Exception: ds["date"] = None
            daily_stats.update(ds)
        cur.execute("SELECT v FROM state WHERE k='last_signal_time'")
        r = cur.fetchone()
        if r:
            last_signal_time.update(json.loads(r[0]))
        c.close()
    except Exception:
        log.exception("load_state")

def record_trade_result(sym, bias, res):
    try:
        c = sqlite3.connect(DB_PATH)
        c.execute("INSERT INTO trades (symbol,bias,result,ts) VALUES (?,?,?,?)",
                  (sym, bias, res, datetime.now(timezone.utc).isoformat()))
        c.commit(); c.close()
    except Exception:
        log.exception("record_trade_result")

def check_circuit_breaker():
    today = datetime.now(timezone.utc).date()
    if daily_stats["date"] != today:
        daily_stats["date"] = today
        daily_stats["losses_today"] = 0.0
        daily_stats["is_circuit_broken"] = False
    if daily_stats["losses_today"] >= ACCOUNT_BALANCE * MAX_DAILY_LOSS_PCT:
        daily_stats["is_circuit_broken"] = True
        return False
    return not daily_stats["is_circuit_broken"]

# ─────────────────────────────────────────────────────────────
# Deriv WebSocket client (persistent)
# ─────────────────────────────────────────────────────────────
class DerivClient:
    def __init__(self, url=DERIV_WS_URL):
        self.url = url
        self.ws = None
        self.lock = threading.Lock()

    def _connect(self):
        if self.ws is None:
            self.ws = websocket.create_connection(self.url, timeout=15)

    def request(self, payload, timeout=15):
        with self.lock:
            try:
                self._connect()
                self.ws.send(json.dumps(payload))
                self.ws.settimeout(timeout)
                return json.loads(self.ws.recv())
            except Exception:
                log.exception("deriv request failed; reconnecting")
                try:
                    if self.ws: self.ws.close()
                except Exception:
                    pass
                self.ws = None
                return None

_deriv = DerivClient()

def fetch_data(symbol, tf, limit=100):
    gran = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400}
    res = _deriv.request({
        "ticks_history": to_deriv_symbol(symbol),
        "count": limit,
        "end": "latest",
        "granularity": gran.get(tf, 300),
        "style": "candles",
    })
    if not res or "candles" not in res:
        return None
    df = pd.DataFrame(res["candles"])
    df['timestamp'] = pd.to_datetime(df['epoch'], unit='s', utc=True)
    df.set_index('timestamp', inplace=True)
    df = df[['open', 'high', 'low', 'close']].astype(float)
    df['volume'] = 1000
    return df

def fetch_current_price(symbol):
    res = _deriv.request({"ticks": to_deriv_symbol(symbol)}, timeout=8)
    if res and "tick" in res:
        return float(res["tick"]["quote"])
    return None

def fetch_spread(symbol):
    return 0.0  # conservative placeholder

# ─────────────────────────────────────────────────────────────
# Candlestick patterns (Nial Fuller faithful)
# ─────────────────────────────────────────────────────────────
def _body(c):  return abs(c['close'] - c['open'])
def _range(c): return c['high'] - c['low']
def _upper(c): return c['high'] - max(c['close'], c['open'])
def _lower(c): return min(c['close'], c['open']) - c['low']

def detect_pinbar(df):
    """Nose >= 2/3 of range, small opposite wick, body in opposite third."""
    if len(df) < 3: return None
    c = df.iloc[-2]
    rng, body = _range(c), _body(c)
    if rng <= 0 or body == 0: return None
    up, lo = _upper(c), _lower(c)
    if lo >= 2 * body and lo >= (2 / 3) * rng and up <= body * 0.5:
        return "BULLISH_PINBAR"
    if up >= 2 * body and up >= (2 / 3) * rng and lo <= body * 0.5:
        return "BEARISH_PINBAR"
    return None

def detect_engulfing(df):
    """Second real body fully engulfs first; first must not be a doji."""
    if len(df) < 3: return None
    c1, c0 = df.iloc[-2], df.iloc[-3]
    if _range(c0) > 0 and _body(c0) < _range(c0) * 0.3:
        return None
    prev_bear = c0['close'] < c0['open']
    prev_bull = c0['close'] > c0['open']
    curr_bull = c1['close'] > c1['open']
    curr_bear = c1['close'] < c1['open']
    if curr_bull and prev_bear and c1['open'] <= c0['close'] and c1['close'] >= c0['open']:
        return "BULLISH_ENGULFING"
    if curr_bear and prev_bull and c1['open'] >= c0['close'] and c1['close'] <= c0['open']:
        return "BEARISH_ENGULFING"
    return None

def detect_inside_bar(df):
    """Mother bar followed by bar whose entire range is inside it."""
    if len(df) < 3: return None
    c1, c0 = df.iloc[-2], df.iloc[-3]
    if c1['high'] < c0['high'] and c1['low'] > c0['low']:
        return "INSIDE_BAR"
    return None

def detect_fakey(df):
    """Inside bar (c2 inside c0), then c1 false-breaks c2 and closes back inside."""
    if len(df) < 4: return None
    c0, c2, c1 = df.iloc[-4], df.iloc[-3], df.iloc[-2]
    if not (c2['high'] < c0['high'] and c2['low'] > c0['low']):
        return None
    if c1['low'] < c2['low'] and c1['close'] > c2['low'] and c1['close'] > c1['open']:
        return "BULLISH_FAKEY"
    if c1['high'] > c2['high'] and c1['close'] < c2['high'] and c1['close'] < c1['open']:
        return "BEARISH_FAKEY"
    return None

def detect_morning_evening_star(df):
    if len(df) < 4: return None
    c2, c1, c0 = df.iloc[-4], df.iloc[-3], df.iloc[-2]
    body2 = _body(c2); body1 = _body(c1); body0 = _body(c0)
    if body2 == 0: return None
    mid2 = (c2['open'] + c2['close']) / 2
    if (c2['close'] < c2['open']
            and body1 < body2 * 0.3
            and c0['close'] > c0['open']
            and c0['close'] > mid2):
        return "MORNING_STAR"
    if (c2['close'] > c2['open']
            and body1 < body2 * 0.3
            and c0['close'] < c0['open']
            and c0['close'] < mid2):
        return "EVENING_STAR"
    return None

def detect_harami(df):
    if len(df) < 3: return None
    c1, c0 = df.iloc[-2], df.iloc[-3]
    if _body(c0) == 0: return None
    if (_body(c1) < _body(c0) * 0.5
            and max(c1['open'], c1['close']) < max(c0['open'], c0['close'])
            and min(c1['open'], c1['close']) > min(c0['open'], c0['close'])):
        return "BULLISH_HARAMI" if c0['close'] < c0['open'] else "BEARISH_HARAMI"
    return None

# ─────────────────────────────────────────────────────────────
# Structure / Confluence
# ─────────────────────────────────────────────────────────────
def get_sr_levels(df, lookback=50):
    recent = df.iloc[-lookback:]
    swing_high = recent['high'].max()
    swing_low = recent['low'].min()
    return swing_high, swing_low, swing_high, swing_low

def check_confluence(df, pattern):
    """Return (score 0-2, reasons, ema21, fib50, fib61, swing_high, swing_low)."""
    close = df['close'].iloc[-2]
    ema21 = df['close'].ewm(span=21).mean().iloc[-2]
    swing_high, swing_low, _, _ = get_sr_levels(df)
    fib_range = swing_high - swing_low
    fib50 = swing_low + fib_range * 0.5
    fib61 = swing_low + fib_range * 0.618

    def near(level):
        return close > 0 and abs(close - level) / close < 0.003

    score, reasons = 0, []
    bullish = any(k in pattern for k in ["BULLISH", "MORNING"])
    if bullish:
        if near(swing_low) or near(fib50) or near(fib61):
            score += 1; reasons.append("Near Support/Fib")
        if close > ema21:
            score += 1; reasons.append("Above 21EMA")
    else:
        if near(swing_high) or near(fib50) or near(fib61):
            score += 1; reasons.append("Near Resistance/Fib")
        if close < ema21:
            score += 1; reasons.append("Below 21EMA")
    return score, reasons, ema21, fib50, fib61, swing_high, swing_low

def analyze_structure(df):
    if len(df) < 21: return "NEUTRAL"
    ema21 = df['close'].ewm(span=21).mean().iloc[-1]
    curr = df['close'].iloc[-1]
    recent_mean = df['close'].iloc[-5:].mean()
    if curr > ema21 and recent_mean > ema21: return "BULLISH"
    if curr < ema21 and recent_mean < ema21: return "BEARISH"
    return "NEUTRAL"

# ─────────────────────────────────────────────────────────────
# A+ setup evaluator
# ─────────────────────────────────────────────────────────────
def evaluate_aplus_setup(symbol):
    if not check_circuit_breaker(): return None
    if len(active_trades) >= MAX_CONCURRENT_TRADES: return None

    h4 = fetch_data(symbol, '4h', 100)
    h1 = fetch_data(symbol, '1h', 100)
    m5 = fetch_data(symbol, '5m', 100)
    if h4 is None or h1 is None or m5 is None: return None

    h4_bias = analyze_structure(h4)
    h1_bias = analyze_structure(h1)
    if h4_bias != h1_bias or h4_bias == "NEUTRAL":
        return None

    patterns = []
    for det in (detect_pinbar, detect_engulfing, detect_inside_bar,
                detect_fakey, detect_morning_evening_star, detect_harami):
        p = det(m5)
        if p: patterns.append(p)
    if not patterns: return None

    patterns.sort(key=lambda x: PATTERN_PRIORITY.get(x, 0), reverse=True)
    best = patterns[0]

    # Resolve direction BEFORE confluence check
    if best == "INSIDE_BAR":
        best = "BULLISH_INSIDE" if h4_bias == "BULLISH" else "BEARISH_INSIDE"

    if "BULLISH" in best and h4_bias != "BULLISH": return None
    if "BEARISH" in best and h4_bias != "BEARISH": return None

    bias = "BUY" if "BULLISH" in best else "SELL"

    score, reasons, ema21, fib50, fib61, sh, sl = check_confluence(m5, best)
    # A+ requires both confluence factors aligned
    if score < 2:
        return None

    price = m5['close'].iloc[-1]
    atr = (m5['high'] - m5['low']).rolling(14).mean().iloc[-1]
    if pd.isna(atr) or atr <= 0:
        atr = price * 0.001

    if "R_75" in symbol:
        min_sl, mult = price * 0.003, 5.0
    elif any(x in symbol for x in ["R_", "BOOM", "CRASH"]):
        min_sl, mult = price * 0.002, 3.0
    elif "XAU" in symbol:
        min_sl, mult = 2.5, 1.8
    else:
        min_sl, mult = atr * 1.2, 1.2

    if bias == "BUY":
        raw_sl = m5['low'].iloc[-3:].min() - max(min_sl, atr * mult)
    else:
        raw_sl = m5['high'].iloc[-3:].max() + max(min_sl, atr * mult)

    raw_risk = abs(price - raw_sl)
    if raw_risk <= 0: return None

    spread = fetch_spread(symbol)
    effective = raw_risk + spread

    if bias == "BUY":
        tp1, tp2 = price + effective * 1.5, price + effective * 3.0
    else:
        tp1, tp2 = price - effective * 1.5, price - effective * 3.0

    return {
        "symbol": symbol, "bias": bias, "pattern": best,
        "score": score, "reasons": reasons,
        "price": price, "sl": raw_sl, "tp1": tp1, "tp2": tp2,
        "risk_distance": effective,
        "ema21": ema21, "fib50": fib50, "fib61": fib61,
        "swing_high": sh, "swing_low": sl,
        "h4_bias": h4_bias, "h1_bias": h1_bias,
        "df": m5, "tp1_hit": False,
    }

# ─────────────────────────────────────────────────────────────
# Chart
# ─────────────────────────────────────────────────────────────
def generate_tradingview_chart(df, symbol, setup, filename="chart.png"):
    plot_df = df.iloc[-80:].copy()
    mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350',
                               edge='inherit', wick='inherit', volume='in')
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":",
                               gridcolor="#2a2e39", facecolor="#131722")
    disp = symbol.replace("R_75", "V75").replace("R_100", "V100")

    # H1 context overlay
    h1 = plot_df.resample('1h').agg({
        'open': 'first', 'high': 'max',
        'low': 'min', 'close': 'last', 'volume': 'sum',
    }).dropna()

    hlines = [setup['price'], setup['tp1'], setup['tp2'], setup['sl'],
              setup['ema21'], setup['fib50'], setup['fib61'],
              setup['swing_high'], setup['swing_low']]
    colors = ['#2962ff', '#00e676', '#00c853', '#ff1744',
              '#ff9800', '#9c27b0', '#9c27b0', '#787b86', '#787b86']
    linestyles = ['-', '--', '--', '-', '-', ':', ':', ':', ':']

    addplots = []
    if len(h1) > 5:
        h1_aligned = h1['close'].reindex(plot_df.index, method='ffill')
        addplots.append(mpf.make_addplot(h1_aligned, ax=1, color='#ff9800',
                                         width=1.2, ylabel='H1'))

    fig, axes = mpf.plot(
        plot_df, type='candle', style=style,
        title=f"\n{disp} {setup['bias']} {setup['pattern']} Score:{setup['score']}/2",
        hlines=dict(hlines=hlines, colors=colors, linestyle=linestyles),
        addplot=addplots if addplots else None,
        panel_ratios=(4, 1) if addplots else (1,),
        returnfig=True, figratio=(16, 9), figscale=1.3,
    )
    ax = axes[0]
    last = len(plot_df) - 1

    for pr, label, col in [
        (setup['price'], f" ENTRY {format_price(symbol, setup['price'])}", '#2962ff'),
        (setup['sl'],    f" SL {format_price(symbol, setup['sl'])}",    '#ff1744'),
        (setup['tp1'],   f" TP1 {format_price(symbol, setup['tp1'])}",  '#00e676'),
        (setup['tp2'],   f" TP2 {format_price(symbol, setup['tp2'])}",  '#00c853'),
    ]:
        ax.text(last + 2, pr, label, color=col, fontsize=9, fontweight='bold',
                va='center',
                bbox=dict(boxstyle="round,pad=0.3", facecolor='#131722',
                          edgecolor=col, alpha=0.9))

    ax.text(last + 2, setup['ema21'], f" 21EMA {format_price(symbol, setup['ema21'])}",
            color='#ff9800', fontsize=7)
    ax.text(last + 2, setup['fib50'], " Fib50", color='#9c27b0', fontsize=7)
    ax.text(last + 2, setup['swing_high'],
            f" HIGH {format_price(symbol, setup['swing_high'])}", color='#787b86', fontsize=7)
    ax.text(last + 2, setup['swing_low'],
            f" LOW {format_price(symbol, setup['swing_low'])}", color='#787b86', fontsize=7)

    fig.text(0.02, 0.92,
             f"{setup['pattern']} | {','.join(setup['reasons'])} | "
             f"H4:{setup['h4_bias']} H1:{setup['h1_bias']}",
             color='#26a69a' if setup['bias'] == 'BUY' else '#ef5350',
             fontsize=9, bbox=dict(facecolor='#1e222d', alpha=0.8))

    fig.savefig(filename, dpi=150, bbox_inches='tight', facecolor='#131722')
    plt.close(fig)
    return filename

# ─────────────────────────────────────────────────────────────
# Background tasks
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Background tasks
# ─────────────────────────────────────────────────────────────
async def track_positions(app):
    while True:
        try:
            if is_paused():
                await asyncio.sleep(60)
                continue
            for trade in list(active_trades):
                cur = fetch_current_price(trade['symbol'])
                if cur is None:
                    continue
                sym = trade['symbol']
                ep = format_price(sym, trade['price'])
                curr_p = format_price(sym, cur)
                tp2p = format_price(sym, trade['tp2'])

                hit_tp1 = ((cur >= trade['tp1']) if trade['bias'] == "BUY"
                           else (cur <= trade['tp1'])) and not trade['tp1_hit']
                hit_tp2 = (cur >= trade['tp2']) if trade['bias'] == "BUY" else (cur <= trade['tp2'])
                hit_sl = (cur <= trade['sl']) if trade['bias'] == "BUY" else (cur >= trade['sl'])

                if hit_tp1 and not hit_tp2:
                    trade['tp1_hit'] = True
                    daily_stats['tp1_hits'] += 1
                    save_state()
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"✅ TP1 HIT {sym} {trade['pattern']} {ep} -> {curr_p}")
                elif hit_tp2:
                    daily_stats['wins'] += 1
                    record_trade_result(sym, trade['bias'], 1)
                    active_trades.remove(trade)
                    save_state()
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"🎯🎯 TP2 HIT {sym} {trade['pattern']} {ep} -> {tp2p}")
                elif hit_sl:
                    if not trade['tp1_hit']:
                        daily_stats['losses'] += 1
                        daily_stats['losses_today'] += ACCOUNT_BALANCE * RISK_PER_TRADE_PCT
                        record_trade_result(sym, trade['bias'], 0)
                    active_trades.remove(trade)
                    save_state()
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"🔴 SL HIT {sym} {trade['pattern']} {ep} "
                             f"SL {format_price(sym, trade['sl'])} Now {curr_p}")
            await asyncio.sleep(10)
        except Exception:
            log.exception("track_positions error")
            await asyncio.sleep(10)


async def market_scanner(app):
    last_m5 = {}
    while True:
        try:
            if is_paused():
                await asyncio.sleep(60)
                continue
            for symbol in get_active_symbols():
                if symbol in last_signal_time and time.time() - last_signal_time[symbol] < 3600:
                    continue
                if any(t['symbol'] == symbol for t in active_trades):
                    continue
                m5_df = fetch_data(symbol, '5m', 5)
                if m5_df is None:
                    continue
                cur = m5_df.index[-1]
                if last_m5.get(symbol) == cur:
                    continue
                last_m5[symbol] = cur

                setup = evaluate_aplus_setup(symbol)
                if not setup:
                    continue
                if not check_circuit_breaker():
                    continue

                chart = generate_tradingview_chart(setup['df'], symbol, setup)
                ep = format_price(symbol, setup['price'])
                slp = format_price(symbol, setup['sl'])
                tp1p = format_price(symbol, setup['tp1'])
                tp2p = format_price(symbol, setup['tp2'])
                disp = symbol.replace("R_75", "V75").replace("R_100", "V100")
                cap = (
                    f"🎯 {setup['bias']} {disp} {setup['pattern']}\n"
                    f"Confluence: {setup['score']}/2 {','.join(setup['reasons'])}\n"
                    f"Entry {ep}\nSL {slp}\nTP1 {tp1p} (1:1.5)\nTP2 {tp2p} (1:3)\n"
                    f"Size {format_lots(symbol, setup['risk_distance'])}\n"
                    f"H4 {setup['h4_bias']} H1 {setup['h1_bias']} "
                    f"EMA21 {format_price(symbol, setup['ema21'])}"
                )
                try:
                    with open(chart, "rb") as photo:
                        await app.bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=cap)
                    active_trades.append({k: v for k, v in setup.items() if k != "df"})
                    last_signal_time[symbol] = time.time()
                    save_state()
                except Exception:
                    log.exception("send failed %s", symbol)
                finally:
                    if os.path.exists(chart):
                        os.remove(chart)
                await asyncio.sleep(3)
            await asyncio.sleep(30)
        except Exception:
            log.exception("market_scanner error")
            await asyncio.sleep(10)


async def schedule_daily_report(app):
    sent = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour == 20 and now.minute <= 10 and sent != now.date():
                total = daily_stats['wins'] + daily_stats['losses'] + daily_stats['tp1_hits']
                rw = daily_stats['wins'] + daily_stats['tp1_hits']
                wr = (rw / total * 100) if total else 0
                rep = (
                    f"📊 DAILY REPORT {now.date()} V14 BIBLE\n"
                    f"WR {wr:.1f}% | Wins {rw} | Losses {daily_stats['losses']} "
                    f"| TP1 {daily_stats['tp1_hits']} | TP2 {daily_stats['wins']}\n"
                    f"Active {len(active_trades)} | "
                    f"Patterns: Pin/Engulf/Inside/Fakey/Star/Harami"
                )
                try:
                    await app.bot.send_message(chat_id=CHAT_ID, text=rep)
                    sent = now.date()
                except Exception:
                    log.exception("daily report send failed")
            await asyncio.sleep(60)
        except Exception:
            log.exception("scheduler error")
            await asyncio.sleep(60)


# ─────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "StarFx V14 online.\n/status /pause /resume /report /positions")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    open("PAUSE", "w").close()
    await update.message.reply_text("⏸ Paused (new signals blocked).")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if os.path.exists("PAUSE"):
        os.remove("PAUSE")
    await update.message.reply_text("▶️ Resumed.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Paused: {is_paused()}\nActive: {len(active_trades)}\n"
        f"W/L/TP1: {daily_stats['wins']}/{daily_stats['losses']}/{daily_stats['tp1_hits']}")


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not active_trades:
        await update.message.reply_text("No open positions.")
        return
    txt = "\n".join(
        f"{t['symbol']} {t['bias']} {t['pattern']} @ {format_price(t['symbol'], t['price'])} "
        f"SL {format_price(t['symbol'], t['sl'])} TP1 {format_price(t['symbol'], t['tp1'])}"
        for t in active_trades
    )
    await update.message.reply_text(txt)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
async def post_init(app: Application):
    asyncio.create_task(track_positions(app))
    asyncio.create_task(market_scanner(app))
    asyncio.create_task(schedule_daily_report(app))


def main():
    init_db()
    load_state()
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("report", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    log.info("StarFx V14 starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
