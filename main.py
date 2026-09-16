"""
StarFX V15.2 — complete bot
Env vars: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, DERIV_APP_ID
"""
import os, asyncio, time, json, sqlite3, tempfile
from datetime import datetime, timezone
from threading import Thread, Lock
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import mplfinance as mpf
import websockets
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================== CONFIG ==============================
TELEGRAM_TOKEN   = "8656945768:AAGWePpyEbPmn8CYySpJedPOHc6LaNWHpkg"
TELEGRAM_CHAT_ID = "-1004365660319"
DERIV_APP_ID     = os.environ.get("DERIV_APP_ID", "1089")

WEEKDAY_SYMBOLS = ["XAU/USD", "GBP/USD", "R_75"]
WEEKEND_SYMBOLS = ["R_75", "R_100"]

SYMBOL_MAP = {
    "XAU/USD": "frxXAUUSD",
    "GBP/USD": "frxGBPUSD",
    "R_75":    "R_75",
    "R_100":   "R_100",
}
GRAN = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}

COOLDOWN_SEC   = 3600
RR             = 2.0
DB_PATH        = os.environ.get("DB_PATH", "signals.db")
WARM_BARS      = 200
COOLDOWN_BARS  = 12
STEP           = 2
MAX_TRADE_BARS = 300
DL_TIMEOUT     = 20
DL_BATCH       = 2000

def to_deriv(s): return SYMBOL_MAP.get(s, s)
def fmt_price(sym, p): return f"{p:.2f}" if "R_" in sym else f"{p:.5f}"
def get_active_symbols():
    # R_100 trades 24/7; XAU/USD only weekdays
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return ["R_100"]                    # weekend: R_100 only
    return ["XAU/USD", "R_100"]             # weekday: both
# ============================== FLASK ==============================
_flask = Flask("starfx")
@_flask.route("/")
def _home(): return "StarFX V15.2 alive"
def _run_flask():
    _flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
Thread(target=_run_flask, daemon=True).start()

# ============================== DATA ==============================
_cache, _cache_lock = {}, Lock()
CACHE_TTL = 55

async def _ws_fetch(symbol, gran, count, end="latest"):
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    async with websockets.connect(uri, ping_interval=20, close_timeout=5) as ws:
        req = {"ticks_history": symbol, "count": count, "end": end,
               "style": "candles", "granularity": gran}
        await ws.send(json.dumps(req))
        resp = await asyncio.wait_for(ws.recv(), timeout=DL_TIMEOUT)
        return json.loads(resp)

async def fetch_candles(symbol, gran=300, count=200, use_cache=True):
    key = (symbol, gran)
    now = time.time()
    if use_cache:
        with _cache_lock:
            if key in _cache:
                df, ts = _cache[key]
                if now - ts < CACHE_TTL: return df
    for attempt in range(3):
        try:
            data = await _ws_fetch(symbol, gran, count)
            if "candles" not in data: return None
            df = pd.DataFrame(data["candles"])
            for c in ("open","high","low","close"): df[c] = df[c].astype(float)
            df["epoch"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
            with _cache_lock: _cache[key] = (df, now)
            return df
        except Exception as e:
            print(f"fetch {symbol} {gran} attempt {attempt+1}: {e}")
            await asyncio.sleep(1)
    return None

async def fetch_data(symbol, tf="M5", count=200):
    return await fetch_candles(to_deriv(symbol), GRAN[tf], count)

async def fetch_price(symbol):
    df = await fetch_candles(to_deriv(symbol), 60, 2, use_cache=False)
    if df is not None and len(df): return float(df["close"].iloc[-1])
    return None

# ============================== UTIL ==============================
def atr(df, n=14):
    v = (df["high"] - df["low"]).rolling(n).mean().iloc[-1]
    return v if not pd.isna(v) else (df["high"] - df["low"]).mean()

def htf_bias(df):
    if df is None or len(df) < 50: return "NEUTRAL"
    ema21 = df["close"].ewm(span=21).mean().iloc[-1]
    ema50 = df["close"].ewm(span=50).mean().iloc[-1]
    p = df["close"].iloc[-1]
    if p > ema21 > ema50: return "BULL"
    if p < ema21 < ema50: return "BEAR"
    return "NEUTRAL"

# ============================== PATTERN ==============================
def detect_pattern(df):
    if df is None or len(df) < 4: return None
    last, prev = df.iloc[-1], df.iloc[-2]
    body = abs(last["close"] - last["open"]) or 1e-9
    uw = last["high"] - max(last["open"], last["close"])
    lw = min(last["open"], last["close"]) - last["low"]
    if lw > body * 2 and last["close"] > last["open"]:
        return {"pattern": "PinBar BULL", "bias": "BULL"}
    if uw > body * 2 and last["close"] < last["open"]:
        return {"pattern": "PinBar BEAR", "bias": "BEAR"}
    if (last["close"] > last["open"] and prev["close"] < prev["open"]
        and last["close"] > prev["open"] and last["open"] < prev["close"]):
        return {"pattern": "Engulfing BULL", "bias": "BULL"}
    if (last["close"] < last["open"] and prev["close"] > prev["open"]
        and last["open"] > prev["close"] and last["close"] < prev["open"]):
        return {"pattern": "Engulfing BEAR", "bias": "BEAR"}
    if last["high"] < prev["high"] and last["low"] > prev["low"]:
        return {"pattern": "InsideBar", "bias": "NEUTRAL"}
    if len(df) >= 3:
        p2, mid = df.iloc[-3], df.iloc[-2]
        mb = abs(mid["close"] - mid["open"])
        if (p2["close"] < p2["open"] and mb < body * 0.6
            and last["close"] > last["open"] and last["close"] > p2["open"]):
            return {"pattern": "MorningStar", "bias": "BULL"}
        if (p2["close"] > p2["open"] and mb < body * 0.6
            and last["close"] < last["open"] and last["close"] < p2["open"]):
            return {"pattern": "EveningStar", "bias": "BEAR"}
    return None

# ============================== ZONES ==============================
def find_pivots(df, left=2, right=2):
    h, l = df["high"].values, df["low"].values
    piv = []
    for i in range(left, len(df) - right):
        if h[i] == max(h[i-left:i+right+1]): piv.append((i, h[i], "H"))
        if l[i] == min(l[i-left:i+right+1]): piv.append((i, l[i], "L"))
    return piv

def detect_zones(df, lookback=120, impulse_atr=1.0):
    df = df.reset_index(drop=True)
    a = (df["high"] - df["low"]).rolling(14).mean()
    zones, n = [], len(df)
    start = max(20, n - lookback)
    for i in range(start, n - 3):
        if pd.isna(a.iloc[i]) or a.iloc[i] == 0: continue
        body = abs(df["close"].iloc[i] - df["open"].iloc[i])
        if body < a.iloc[i] * impulse_atr: continue
        base = df.iloc[max(0, i-3):i]
        if len(base) < 1: continue
        bt, bb = base["high"].max(), base["low"].min()
        if (bt - bb) > a.iloc[i] * 2.5: continue
        bull = df["close"].iloc[i] > df["open"].iloc[i]
        pre = df.iloc[max(0, i-6):max(0, i-3)]
        pre_bull = len(pre) >= 2 and pre["close"].iloc[-1] > pre["open"].iloc[0]
        if bull and not pre_bull:   kind = "DBR"
        elif bull and pre_bull:     kind = "RBR"
        elif not bull and pre_bull: kind = "RBD"
        else:                       kind = "DBD"
        zones.append({"side": "demand" if bull else "supply", "kind": kind,
                      "top": float(bt), "bot": float(bb),
                      "created_idx": int(i), "fresh": True})
    zones.sort(key=lambda z: z["created_idx"], reverse=True)
    keep = []
    for z in zones:
        if all(z["side"] != k["side"] or z["top"] < k["bot"]
               or z["bot"] > k["top"] for k in keep): keep.append(z)
    return keep

def mark_freshness(zones, df):
    last_idx = len(df) - 1
    a = atr(df)
    for z in zones:
        after = df.iloc[z["created_idx"] + 1 : last_idx]
        if len(after) == 0:
            z["fresh"] = True; continue
        if z["side"] == "demand":
            broken = (after["close"] < z["bot"] - a * 0.3).any()
        else:
            broken = (after["close"] > z["top"] + a * 0.3).any()
        z["fresh"] = not broken
    return zones

def price_interacts_zone(df, zone, side, lookback=3, pad=0.0):
    recent = df.iloc[-lookback:]
    top, bot = zone["top"] + pad, zone["bot"] - pad
    return bool(((recent["low"] <= top) & (recent["high"] >= bot)).any())

# ============================== LIQUIDITY ==============================
def equal_levels(df, tolerance=0.0025, min_touches=2):
    h, l = df["high"].values, df["low"].values
    piv = []
    for i in range(2, len(df) - 2):
        if h[i] == max(h[i-2:i+3]): piv.append(("H", i, h[i]))
        if l[i] == min(l[i-2:i+3]): piv.append(("L", i, l[i]))
    pools = []
    for kind in ("H", "L"):
        pts = sorted([p for p in piv if p[0] == kind], key=lambda x: x[2])
        used = set()
        for i in range(len(pts)):
            if i in used: continue
            cl = [pts[i]]; used.add(i)
            for j in range(i+1, len(pts)):
                if j in used: continue
                if abs(pts[j][2] - pts[i][2]) / pts[i][2] < tolerance:
                    cl.append(pts[j]); used.add(j)
            if len(cl) >= min_touches:
                pools.append({"kind": kind,
                              "price": float(np.mean([c[2] for c in cl])),
                              "idx": [c[1] for c in cl]})
    return pools

def detect_sweep(df, pool, lookahead=8):
    last = max(pool["idx"])
    if last >= len(df) - 1: return None
    level = pool["price"]
    scan = df.iloc[last + 1: last + 1 + lookahead]
    for i, row in scan.iterrows():
        if pool["kind"] == "H" and row["high"] > level and row["close"] < level:
            return {"kind": "sweep_high", "level": level, "idx": int(i), "bias": "BEAR"}
        if pool["kind"] == "L" and row["low"] < level and row["close"] > level:
            return {"kind": "sweep_low", "level": level, "idx": int(i), "bias": "BULL"}
    return None

# ============================== TRENDLINES ==============================
def fit_trendline(pivots, kind="H", min_pts=2):
    pts = [(i, p) for (i, p, k) in pivots if k == kind]
    if len(pts) < min_pts: return None
    pts = pts[-min_pts:]
    xs = np.array([p[0] for p in pts], float)
    ys = np.array([p[1] for p in pts], float)
    if xs.ptp() == 0: return None
    slope, inter = np.polyfit(xs, ys, 1)
    pred = slope * xs + inter
    ss_res = ((ys - pred) ** 2).sum()
    ss_tot = ((ys - ys.mean()) ** 2).sum() or 1e-9
    r2 = 1 - ss_res / ss_tot
    if r2 < 0.4: return None
    return {"slope": float(slope), "intercept": float(inter),
            "r2": float(r2), "kind": kind, "x0": int(xs[0]), "x1": int(xs[-1])}

def tl_value(tl, x): return tl["slope"] * x + tl["intercept"]
def tl_touch(tl, df, idx, tol_atr, a):
    return abs(df["close"].iloc[idx] - tl_value(tl, idx)) < tol_atr * a
def tl_break(tl, df, idx):
    line = tl_value(tl, idx); c = df["close"].iloc[idx]
    return c > line if tl["kind"] == "H" else c < line

# ============================== SETUP EVAL ==============================
def evaluate_setup_sync(df, target_bias, atr_val=None, min_bars=60):
    if df is None or len(df) < min_bars: return None
    df = df.reset_index(drop=True)
    if atr_val is None: atr_val = atr(df)
    if pd.isna(atr_val) or atr_val == 0: return None

    pat = detect_pattern(df)
    if not pat: return None
    if pat["bias"] != target_bias and pat["bias"] != "NEUTRAL":
        return None

    zones = mark_freshness(detect_zones(df, 120), df)
    side = "demand" if target_bias == "BULL" else "supply"
    price = float(df["close"].iloc[-1])
    zone = next((z for z in zones if z["side"] == side and z["fresh"]
                 and price_interacts_zone(df, z, side, 6, atr_val * 0.35)), None)
    if not zone: return None

    pools = equal_levels(df, 0.0025)
    sweep = None
    for pool in pools:
        sw = detect_sweep(df, pool, 20)
        if sw and sw["bias"] == target_bias and sw["idx"] >= len(df) - 20:
            sweep = sw; break

    piv = find_pivots(df)
    tl_h = fit_trendline(piv, "H", 2)
    tl_l = fit_trendline(piv, "L", 2)
    idx = len(df) - 1
    tl_ok, tl_reason, tl_obj = False, "", None
    if target_bias == "BULL":
        if tl_l and tl_touch(tl_l, df, idx, 0.6, atr_val):
            tl_ok, tl_reason, tl_obj = True, "TL_demand", tl_l
        elif tl_h and tl_break(tl_h, df, idx):
            tl_ok, tl_reason, tl_obj = True, "TL_break_up", tl_h
    else:
        if tl_h and tl_touch(tl_h, df, idx, 0.6, atr_val):
            tl_ok, tl_reason, tl_obj = True, "TL_supply", tl_h
        elif tl_l and tl_break(tl_l, df, idx):
            tl_ok, tl_reason, tl_obj = True, "TL_break_dn", tl_l

    last_candle = df.iloc[-1]
    if target_bias == "BULL":
        sl = float(last_candle["low"] - atr_val * 0.3)
        risk = price - sl
        tp = price + RR * risk
    else:
        sl = float(last_candle["high"] + atr_val * 0.3)
        risk = sl - price
        tp = price - RR * risk
    if risk <= 0 or risk > atr_val * 2: return None

    return {"bias": target_bias, "pattern": pat["pattern"],
            "entry": price, "sl": sl, "tp": tp, "atr": float(atr_val),
            "zone_kind": zone["kind"],
            "sweep": sweep["kind"] if sweep else "none",
            "tl": tl_reason, "tl_obj": tl_obj}
# ============================== LIVE ==============================
async def evaluate_setup(symbol):
    if time.time() - last_signal_time.get(symbol, 0) < COOLDOWN_SEC:
        return None
    m5  = await fetch_data(symbol, "M5")
    m15 = await fetch_data(symbol, "M15")
    h1  = await fetch_data(symbol, "H1")
    h4  = await fetch_data(symbol, "H4")
    if m5 is None or h1 is None or h4 is None: return None
    bh1, bh4 = htf_bias(h1), htf_bias(h4)
    if bh4 == "NEUTRAL": return None
    if bh1 == bh4 or bh1 == "NEUTRAL":
        mode, target = "WITH_TREND", bh4
        cands = [("M5", m5)]
    else:
        mode, target = "COUNTER", bh4
        cands = [("M5", m5), ("M15", m15)]

    for tf_name, df in cands:
        if df is None or len(df) < 60: continue
        r = evaluate_setup_sync(df, target)
        if r:
            r.update({"symbol": symbol, "mode": mode, "tf": tf_name,
                      "bh1": bh1, "bh4": bh4, "df": df})
            return r
    return None
# ============================== DB ==============================
_db_lock = Lock()

def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, symbol TEXT, mode TEXT, tf TEXT,
            pattern TEXT, bias TEXT, entry REAL, sl REAL, tp REAL,
            zone_kind TEXT, sweep TEXT, tl TEXT,
            status TEXT DEFAULT 'OPEN', closed_ts TEXT)""")
        c.commit()

def db_save(s):
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        cur = c.execute("""INSERT INTO signals
            (ts,symbol,mode,tf,pattern,bias,entry,sl,tp,zone_kind,sweep,tl)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(),
             s["symbol"], s["mode"], s["tf"], s["pattern"], s["bias"],
             s["entry"], s["sl"], s["tp"],
             s.get("zone_kind",""), s.get("sweep",""), s.get("tl","")))
        c.commit(); return cur.lastrowid

def db_open():
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT * FROM signals WHERE status='OPEN'").fetchall()]

def db_close(sid, status):
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        c.execute("UPDATE signals SET status=?, closed_ts=? WHERE id=?",
                  (status, datetime.now(timezone.utc).isoformat(), sid))
        c.commit()

def db_stats():
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT status, COUNT(*) FROM signals GROUP BY status").fetchall()
        tot = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    return {"total": tot, **dict(rows)}

# ============================== CHART ==============================
def build_chart(setup):
    df = setup["df"]
    df_plot = df.tail(90).copy()
    offset = len(df) - len(df_plot)
    df_plot = df_plot.set_index("epoch")
    title = (f"{setup['symbol']} {setup['bias']} {setup['pattern']} | "
             f"{setup['mode']} {setup['tf']} | H1:{setup['bh1']} H4:{setup['bh4']}")
    fig, axlist = mpf.plot(df_plot, type="candle", style="charles",
                            returnfig=True, figsize=(15, 8), title=title,
                            ylabel="Price", volume=False)
    ax = axlist[0]; n = len(df_plot)
    zones = mark_freshness(detect_zones(df, 120), df)
    for z in zones[-6:]:
        x0 = max(0, z["created_idx"] - offset)
        if x0 >= n: continue
        color = "#00ff88" if z["side"] == "demand" else "#ff4466"
        alpha = 0.22 if z["fresh"] else 0.08
        ax.add_patch(Rectangle((x0, z["bot"]), n - x0, z["top"] - z["bot"],
                                facecolor=color, alpha=alpha,
                                edgecolor=color, linewidth=0.8))
        ax.text(x0+0.3, z["top"], f"{z['kind']}{'*' if z['fresh'] else ''}",
                fontsize=7, color=color, va="bottom")
    for pool in equal_levels(df, 0.0008)[-6:]:
        ax.axhline(pool["price"], color="#ffcc00", linestyle=":",
                   linewidth=0.9, alpha=0.7)
        ax.text(n*0.005, pool["price"], "LIQ", fontsize=7,
                color="#ffcc00", va="center")
    if setup.get("tl_obj"):
        tl = setup["tl_obj"]
        xs = np.arange(max(0, tl["x0"] - offset), n)
        ys = tl_value(tl, xs + offset)
        ax.plot(xs, ys, color="#00aaff", linewidth=1.6, linestyle="--",
                label=f"TL r²={tl['r2']:.2f}")
        ax.legend(loc="upper left", fontsize=7, framealpha=0.3)
    ax.axhline(setup["entry"], color="white", linewidth=1.2)
    ax.axhline(setup["sl"], color="#ff2222", linewidth=1.4)
    ax.axhline(setup["tp"], color="#22ff22", linewidth=1.4)
    ax.text(n*0.995, setup["entry"],
            f" ENTRY {fmt_price(setup['symbol'], setup['entry'])}",
            fontsize=7, color="white", va="bottom", ha="right")
    ax.text(n*0.995, setup["sl"],
            f" SL {fmt_price(setup['symbol'], setup['sl'])}",
            fontsize=7, color="#ff2222", va="top", ha="right")
    ax.text(n*0.995, setup["tp"],
            f" TP(2R) {fmt_price(setup['symbol'], setup['tp'])}",
            fontsize=7, color="#22ff22", va="bottom", ha="right")
    fd, path = tempfile.mkstemp(suffix=".png", prefix="sfx_")
    os.close(fd)
    fig.savefig(path, dpi=110, bbox_inches="tight", facecolor="#0e1117")
    plt.close(fig); return path

# ============================== DOWNLOAD ==============================
async def download_history(deriv_sym, months=9):
    total_needed = months * 30 * 24 * 12 + 500
    all_c = []
    end = "latest"
    attempts = 0
    max_attempts = 200
    while len(all_c) < total_needed and attempts < max_attempts:
        attempts += 1
        try:
            resp = await _ws_fetch(deriv_sym, 300, DL_BATCH, end=end)
            got = len(resp.get("candles", []))
            print(f"  [{deriv_sym}] batch {attempts}: {got} bars (total {len(all_c)})")
        except asyncio.TimeoutError:
            print(f"  [{deriv_sym}] batch {attempts}: TIMEOUT, retry")
            await asyncio.sleep(2)
            continue
        except Exception as e:
            print(f"  [{deriv_sym}] batch {attempts}: err {e}")
            await asyncio.sleep(2)
            continue
        if "candles" not in resp:
            break
        batch = resp["candles"]
        if not batch:
            break
        all_c = batch + all_c
        end = batch[0]["epoch"] - 1
    if not all_c:
        return None
    df = pd.DataFrame(all_c)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["epoch"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
    df = df.drop_duplicates("epoch").reset_index(drop=True)
    print(f"  [{deriv_sym}] downloaded {len(df)} bars")
    return df


def _prepare_dataframes(df_m5):
    df = df_m5.copy()
    for c in ("open","high","low","close"): df[c] = df[c].astype(float)
    if not pd.api.types.is_datetime64_any_dtype(df["epoch"]):
        df["epoch"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
    df = df.sort_values("epoch").reset_index(drop=True)
    idx = df.set_index("epoch")
    def rs(rule):
        return (idx.resample(rule)
                .agg({"open":"first","high":"max","low":"min","close":"last"})
                .dropna().reset_index())
    return df, rs("15min"), rs("1h"), rs("4h")


def simulate_trade(df, i, bias, sl, tp, max_bars=MAX_TRADE_BARS):
    for j in range(i + 1, min(i + max_bars, len(df))):
        b = df.iloc[j]
        if bias == "BULL":
            if b["low"]  <= sl: return "SL"
            if b["high"] >= tp: return "TP"
        else:
            if b["high"] >= sl: return "SL"
            if b["low"]  <= tp: return "TP"
    return "EXPIRED"


def _bt_one_symbol(df_m5, df_m15, df_h1, df_h4, symbol):
    df_m5 = df_m5.reset_index(drop=True); df_m15 = df_m15.reset_index(drop=True)
    df_h1 = df_h1.reset_index(drop=True); df_h4 = df_h4.reset_index(drop=True)
    n = len(df_m5)
    a5_full = (df_m5["high"] - df_m5["low"]).rolling(14).mean()
    m5_ep = df_m5["epoch"].values; m15_ep = df_m15["epoch"].values
    h1_ep = df_h1["epoch"].values; h4_ep = df_h4["epoch"].values
    trades, last_i = [], -999
    for i in range(WARM_BARS, n - MAX_TRADE_BARS, STEP):
        if i - last_i < COOLDOWN_BARS: continue
        av5 = a5_full.iloc[i]
        if pd.isna(av5) or av5 == 0: continue
        ep = m5_ep[i]
        h1_pos  = np.searchsorted(h1_ep,  ep, side="right") - 1
        h4_pos  = np.searchsorted(h4_ep,  ep, side="right") - 1
        m15_pos = np.searchsorted(m15_ep, ep, side="right") - 1
        if h1_pos < 50 or h4_pos < 50: continue
        bh1 = htf_bias(df_h1.iloc[max(0, h1_pos-200):h1_pos+1])
        bh4 = htf_bias(df_h4.iloc[max(0, h4_pos-200):h4_pos+1])
        if bh4 != "NEUTRAL" and bh1 == bh4:
            mode, target = "WITH_TREND", bh4
            cands = [("M5", df_m5.iloc[max(0, i-200):i+1], av5)]
        elif bh4 != "NEUTRAL" and bh1 != "NEUTRAL" and bh4 != bh1:
            mode, target = "COUNTER", bh4
            cands = [("M5", df_m5.iloc[max(0, i-200):i+1], av5)]
            if m15_pos >= 60:
                w15 = df_m15.iloc[max(0, m15_pos-200):m15_pos+1]
                av15 = (w15["high"] - w15["low"]).rolling(14).mean().iloc[-1]
                cands.append(("M15", w15, av15))
        else: continue
        matched, matched_tf = None, None
        for tf_name, w, av_tf in cands:
            if pd.isna(av_tf) or av_tf == 0: continue
            r = evaluate_setup_sync(w, target, atr_val=av_tf)
            if r: matched, matched_tf = r, tf_name; break
        if not matched: continue
        outcome = simulate_trade(df_m5, i, target, matched["sl"], matched["tp"])
        trades.append({"i": i, "bias": target, "mode": mode,
                       "tf": matched_tf, "pattern": matched["pattern"],
                       "outcome": outcome})
        last_i = i
    wins = sum(1 for t in trades if t["outcome"] == "TP")
    losses = sum(1 for t in trades if t["outcome"] == "SL")
    expd = sum(1 for t in trades if t["outcome"] == "EXPIRED")
    n_done = wins + losses
    wr = wins / n_done * 100 if n_done else 0
    expct = (wins * RR - losses) / max(1, len(trades))
    pf = (wins * RR) / max(1, losses)
    def _by(key):
        g = {}
        for t in trades: g.setdefault(t[key], []).append(t)
        out = {}
        for k, ts in g.items():
            w = sum(1 for t in ts if t["outcome"] == "TP")
            l = sum(1 for t in ts if t["outcome"] == "SL")
            out[k] = {"n": len(ts), "w": w, "l": l,
                      "wr": w/(w+l)*100 if (w+l) else 0}
        return out
    return {"symbol": symbol, "trades": len(trades), "wins": wins,
            "losses": losses, "expired": expd, "wr": wr,
            "expectancy": expct, "pf": pf,
            "by_mode": _by("mode"), "by_tf": _by("tf")}


async def run_backtest(symbols, months=9, progress_cb=None):
    results = []
    for sym in symbols:
        if progress_cb:
            await progress_cb(f"📥 Downloading {months}mo M5 for {sym}…")
        try:
            df_m5 = await asyncio.wait_for(
                download_history(to_deriv(sym), months), timeout=300)
        except asyncio.TimeoutError:
            results.append({"symbol": sym, "error": "download timed out"})
            continue
        if df_m5 is None or len(df_m5) < 500:
            results.append({"symbol": sym, "error": "no data"}); continue
        if progress_cb:
            await progress_cb(f"⚙️ Backtesting {sym} ({len(df_m5)} bars)…")
        df5, df15, dfh1, dfh4 = await asyncio.to_thread(_prepare_dataframes, df_m5)
        r = await asyncio.to_thread(_bt_one_symbol, df5, df15, dfh1, dfh4, sym)
        results.append(r)
    return results


# ============================== DIAG ==============================
def _diag_one_symbol(df_m5, df_m15, df_h1, df_h4, symbol):
    df_m5 = df_m5.reset_index(drop=True); df_m15 = df_m15.reset_index(drop=True)
    df_h1 = df_h1.reset_index(drop=True); df_h4 = df_h4.reset_index(drop=True)
    n = len(df_m5)
    a5_full = (df_m5["high"] - df_m5["low"]).rolling(14).mean()
    m5_ep = df_m5["epoch"].values; h1_ep = df_h1["epoch"].values
    h4_ep = df_h4["epoch"].values
    c = {"bars":0,"htf_ok":0,"pattern":0,"zone":0,"sweep":0,"trendline":0,"trade":0}
    for i in range(WARM_BARS, n - 200, 5):
        c["bars"] += 1
        av5 = a5_full.iloc[i]
        if pd.isna(av5) or av5 == 0: continue
        ep = m5_ep[i]
        h1_pos = np.searchsorted(h1_ep, ep, side="right") - 1
        h4_pos = np.searchsorted(h4_ep, ep, side="right") - 1
        if h1_pos < 50 or h4_pos < 50: continue
        bh1 = htf_bias(df_h1.iloc[max(0,h1_pos-200):h1_pos+1])
        bh4 = htf_bias(df_h4.iloc[max(0,h4_pos-200):h4_pos+1])
        if bh4 == "NEUTRAL": continue
        if bh1 == bh4 or bh1 == "NEUTRAL":
            c["htf_ok"] += 1; target = bh4
        else:
            c["htf_ok"] += 1; target = bh4

        w = df_m5.iloc[max(0, i-200):i+1].reset_index(drop=True)
        pat = detect_pattern(w)
        if not pat: continue
        if pat["bias"] != target and pat["bias"] != "NEUTRAL": continue
        c["pattern"] += 1

        zones = mark_freshness(detect_zones(w, 120), w)
        side = "demand" if target == "BULL" else "supply"
        zone = next((z for z in zones if z["side"] == side and z["fresh"]
                     and price_interacts_zone(w, z, side, 6, av5*0.35)), None)
        if not zone: continue
        c["zone"] += 1

        pools = equal_levels(w, 0.0025)
        sweep = None
        for pool in pools:
            sw = detect_sweep(w, pool, 20)
            if sw and sw["bias"] == target and sw["idx"] >= len(w) - 20:
                sweep = sw; break
        if sweep: c["sweep"] += 1

        piv = find_pivots(w)
        tl_h = fit_trendline(piv, "H", 2); tl_l = fit_trendline(piv, "L", 2)
        idx = len(w) - 1; tl_ok = False
        if target == "BULL":
            if tl_l and tl_touch(tl_l, w, idx, 0.6, av5): tl_ok = True
            elif tl_h and tl_break(tl_h, w, idx): tl_ok = True
        else:
            if tl_h and tl_touch(tl_h, w, idx, 0.6, av5): tl_ok = True
            elif tl_l and tl_break(tl_l, w, idx): tl_ok = True
        if tl_ok: c["trendline"] += 1

        last_candle = w.iloc[-1]
        if target == "BULL":
            sl = float(last_candle["low"] - av5*0.3)
            risk = w["close"].iloc[-1] - sl
        else:
            sl = float(last_candle["high"] + av5*0.3)
            risk = sl - w["close"].iloc[-1]
        if risk <= 0 or risk > av5 * 2: continue
        c["trade"] += 1

    return {"symbol": symbol, **c}

# ============================== TRACKER ==============================
async def tracker_loop():
    while True:
        try:
            for s in db_open():
                p = await fetch_price(s["symbol"])
                if p is None: continue
                if s["bias"] == "BULL":
                    if p <= s["sl"]: db_close(s["id"], "SL")
                    elif p >= s["tp"]: db_close(s["id"], "TP")
                else:
                    if p >= s["sl"]: db_close(s["id"], "SL")
                    elif p <= s["tp"]: db_close(s["id"], "TP")
        except Exception as e:
            print("tracker:", e)
        await asyncio.sleep(60)


# ============================== HANDLERS ==============================
async def start_cmd(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_text(
        "StarFX V15.2 — S/D + Liquidity + Trendlines + Counter-Trend\n"
        f"Active: {', '.join(get_active_symbols())}\n\n"
        "/signal — scan symbols\n"
        "/price — live prices\n"
        "/report — performance stats\n"
        "/diag [symbol] [months] — filter funnel\n"
        "/backtest [symbol|all] [months]"
    )


async def price_cmd(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["💰 Live:"]
    for sym in get_active_symbols():
        p = await fetch_price(sym)
        lines.append(f"{sym}: {fmt_price(sym, p) if p else 'feed lag'}")
    await upd.message.reply_text("\n".join(lines))


async def signal_cmd(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global last_signal_time
    await upd.message.reply_text("🔍 Scanning…")
    found = 0
    for sym in get_active_symbols():
        try: setup = await evaluate_setup(sym)
        except Exception as e: print("eval err", sym, e); continue
        if not setup: continue
        found += 1
        last_signal_time[sym] = time.time()
        sid = db_save(setup)
        chart_path = None
        try:
            chart_path = build_chart(setup)
            cap = (f"{setup['bias']} {sym} | {setup['pattern']} | "
                   f"{setup['mode']} {setup['tf']}\n"
                   f"Zone {setup['zone_kind']}  Sweep {setup['sweep']}  "
                   f"TL {setup['tl']}\n"
                   f"H1:{setup['bh1']}  H4:{setup['bh4']}\n"
                   f"Entry {fmt_price(sym, setup['entry'])}  "
                   f"SL {fmt_price(sym, setup['sl'])}  "
                   f"TP {fmt_price(sym, setup['tp'])}  2R  id#{sid}")
            with open(chart_path, "rb") as ph:
                await ctx.bot.send_photo(chat_id=upd.effective_chat.id,
                                          photo=ph, caption=cap)
        except Exception as e:
            print("chart err", e)
            await ctx.bot.send_message(chat_id=upd.effective_chat.id,
                text=f"{sym} setup #{sid} (chart failed: {e})")
        finally:
            if chart_path and os.path.exists(chart_path):
                try: os.remove(chart_path)
                except: pass
    if found == 0:
        await ctx.bot.send_message(chat_id=upd.effective_chat.id,
            text="❌ No qualifying setups right now.\n"
     f"Auto-scanner runs every 5 min. Active: {', '.join(get_active_symbols())}")
async def autoscan_once(app):
    global last_signal_time
    for sym in get_active_symbols():
        setup = await evaluate_setup(sym)
        if not setup:
            continue
        last_signal_time[sym] = time.time()
        sid = db_save(setup)
        chart_path = None
        try:
            chart_path = build_chart(setup)
            cap = "AUTO " + setup["bias"] + " " + sym + " " + setup["pattern"]
            with open(chart_path, "rb") as ph:
                await app.bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=ph, caption=cap)
        except Exception as e:
            print("autoscan err:", e)
        finally:
            if chart_path and os.path.exists(chart_path):
                try: os.remove(chart_path)
                except: pass

async def autoscan_loop(app):
    while True:
        try:
            print("[autoscan] loop starting")
            await asyncio.sleep(30)
            while True:
                print(f"[autoscan] tick {datetime.now(timezone.utc).isoformat()} symbols={get_active_symbols()}")
                try:
                    await autoscan_once(app)
                except Exception as e:
                    print(f"[autoscan] inner error: {e}")
                await asyncio.sleep(300)
        except Exception as e:
            print(f"[autoscan] supervisor caught: {e} — restarting in 30s")
            await asyncio.sleep(30)
                    


async def report_cmd(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = db_stats()
    wins = s.get("TP", 0); losses = s.get("SL", 0)
    opens = s.get("OPEN", 0); expd = s.get("EXPIRED", 0)
    n_done = wins + losses
    wr = wins / n_done * 100 if n_done else 0
    expct = (wins * RR - losses) / max(1, n_done) if n_done else 0
    pf = (wins * RR) / max(1, losses)
    await upd.message.reply_text(
        f"📊 REPORT — {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Total: {s.get('total', 0)}\n"
        f"Wins: {wins}  Losses: {losses}  Exp: {expd}  Open: {opens}\n"
        f"WR: {wr:.1f}%  Exp: {expct:+.2f}R  PF: {pf:.2f}"
    )


async def diag_cmd(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = list(ctx.args or [])
    months = 3
    if args and args[-1].isdigit():
        months = int(args[-1]); args = args[:-1]
    syms = [args[0]] if (args and args[0] in SYMBOL_MAP) else list(SYMBOL_MAP.keys())
    chat_id = upd.effective_chat.id
    await ctx.bot.send_message(chat_id=chat_id,
        text=f"🔬 Diagnosing {', '.join(syms)} over {months}mo…")
    lines = [f"🔬 FILTER FUNNEL ({months}mo)\n"]
    for sym in syms:
        await ctx.bot.send_message(chat_id=chat_id, text=f"📥 {sym}: downloading…")
        try:
            df_m5 = await asyncio.wait_for(
                download_history(to_deriv(sym), months), timeout=180)
        except asyncio.TimeoutError:
            lines.append(f"{sym}: ⏱ timeout"); continue
        if df_m5 is None or len(df_m5) < 500:
            lines.append(f"{sym}: ❌ no data"); continue
        await ctx.bot.send_message(chat_id=chat_id,
            text=f"⚙️ {sym}: {len(df_m5)} bars — analyzing…")
        df5, df15, dfh1, dfh4 = await asyncio.to_thread(_prepare_dataframes, df_m5)
        r = await asyncio.to_thread(_diag_one_symbol, df5, df15, dfh1, dfh4, sym)
        lines.append(
            f"── {r['symbol']} ──\n"
            f"Bars: {r['bars']}\n"
            f"HTF ok:  {r['htf_ok']}\n"
            f"+pattern: {r['pattern']}\n"
            f"+zone:    {r['zone']}\n"
            f"+sweep:   {r['sweep']}\n"
            f"+trendline: {r['trendline']}\n"
            f"= SETUPS: {r['trade']}"
        )
    txt = "\n\n".join(lines)
    for chunk in [txt[i:i+3800] for i in range(0, len(txt), 3800)]:
        await ctx.bot.send_message(chat_id=chat_id, text=chunk)


async def backtest_cmd(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = list(ctx.args or [])
    months = 9
    if args and args[-1].isdigit():
        months = int(args[-1]); args = args[:-1]
    syms = [args[0]] if (args and args[0] in SYMBOL_MAP) else list(SYMBOL_MAP.keys())
    chat_id = upd.effective_chat.id
    await ctx.bot.send_message(chat_id=chat_id,
        text=f"🧪 Backtest {', '.join(syms)} — {months}mo…")
    async def progress_cb(msg):
        try: await ctx.bot.send_message(chat_id=chat_id, text=msg)
        except: pass
    try:
        results = await run_backtest(syms, months=months, progress_cb=progress_cb)
    except Exception as e:
        await ctx.bot.send_message(chat_id=chat_id, text=f"Failed: {e}"); return
    lines = [f"📈 BACKTEST ({months}mo)\n"]
    for r in results:
        if "error" in r:
            lines.append(f"{r['symbol']}: ❌ {r['error']}"); continue
        wt = r["by_mode"].get("WITH_TREND", {"n":0,"wr":0})
        ct = r["by_mode"].get("COUNTER",    {"n":0,"wr":0})
        m5s = r["by_tf"].get("M5",  {"n":0,"wr":0})
        m15 = r["by_tf"].get("M15", {"n":0,"wr":0})
        lines.append(
            f"── {r['symbol']} ──\n"
            f"Trades: {r['trades']}  W {r['wins']}  L {r['losses']}  E {r['expired']}\n"
            f"WR: {r['wr']:.1f}%  Exp: {r['expectancy']:+.2f}R  PF: {r['pf']:.2f}\n"
            f"  With-trend: {wt['n']}t  WR {wt['wr']:.1f}%\n"
            f"  Counter:    {ct['n']}t  WR {ct['wr']:.1f}%\n"
            f"  M5:  {m5s['n']}t  WR {m5s['wr']:.1f}%\n"
            f"  M15: {m15['n']}t  WR {m15['wr']:.1f}%"
        )
    txt = "\n\n".join(lines)
    for chunk in [txt[i:i+3800] for i in range(0, len(txt), 3800)]:
        await ctx.bot.send_message(chat_id=chat_id, text=chunk)


async def error_handler(upd, ctx):
    print("PTB error:", ctx.error)


# ============================== MAIN ==============================
async def post_init(app):
    db_init()
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID,
                                    text="StarFX booted - this chat receives auto-signals")
        print(f"[boot] ping OK to {TELEGRAM_CHAT_ID}")
    except Exception as e:
        print(f"[boot] ping FAILED: {e}")
    asyncio.create_task(tracker_loop())
    asyncio.create_task(autoscan_loop(app))
def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN env var required")
    app = (Application.builder().token(TELEGRAM_TOKEN)
           .post_init(post_init).build())
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("diag", diag_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_error_handler(error_handler)
    print("StarFX V15.2 running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
