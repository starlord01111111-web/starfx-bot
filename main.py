"""
StarFX V16 - day / scalp / swing multi-mode bot
Commands: /mode day | /mode scalp | /mode swing
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

TELEGRAM_TOKEN = "8656945768:AAG9pfWjGZoWAaNC9HoiFzwRL_PfgdF2pXo"
TELEGRAM_CHAT_ID = "-1004365660319"
DERIV_APP_ID = os.environ.get("DERIV_APP_ID", "1089")
DB_PATH = os.environ.get("DB_PATH", "signals.db")

SYMBOL_MAP = {
    "XAU/USD": "frxXAUUSD",
    "GBP/USD": "frxGBPUSD",
    "R_75": "R_75",
    "R_100": "R_100",
}
GRAN = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}

MODES = {
    "day":   {"symbols": ["XAU/USD", "R_100"], "cooldown": 3600,  "rr": 2.0},
    "scalp": {"symbols": ["XAU/USD", "R_100"], "cooldown": 1800,  "rr": 1.5},
    "swing": {"symbols": ["XAU/USD", "R_100"], "cooldown": 14400, "rr": 3.0},
}
CURRENT_MODE = "day"

def to_deriv(s): return SYMBOL_MAP.get(s, s)
def fmt_price(sym, p):
    if "R_" in sym or "XAU" in sym: return f"{p:.2f}"
    return f"{p:.5f}"
def get_active_symbols(): return MODES[CURRENT_MODE]["symbols"]
def current_rr(): return MODES[CURRENT_MODE]["rr"]
def current_cooldown(): return MODES[CURRENT_MODE]["cooldown"]

_flask = Flask("starfx")
@_flask.route("/")
def _home(): return "StarFX V16 alive"
def _run_flask():
    _flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
Thread(target=_run_flask, daemon=True).start()

_cache, _cache_lock = {}, Lock()
CACHE_TTL = 55

async def _ws_fetch(symbol, gran, count, end="latest"):
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    async with websockets.connect(uri, ping_interval=20, close_timeout=5) as ws:
        req = {"ticks_history": symbol, "count": count, "end": end,
               "style": "candles", "granularity": gran}
        await ws.send(json.dumps(req))
        resp = await asyncio.wait_for(ws.recv(), timeout=20)
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
            for c in ("open", "high", "low", "close"): df[c] = df[c].astype(float)
            df["epoch"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
            with _cache_lock: _cache[key] = (df, now)
            return df
        except Exception as e:
            print(f"fetch {symbol} {gran} try{attempt+1}: {e}")
            await asyncio.sleep(1)
    return None

async def fetch_data(symbol, tf="M5", count=200):
    return await fetch_candles(to_deriv(symbol), GRAN[tf], count)

async def fetch_price(symbol):
    df = await fetch_candles(to_deriv(symbol), 60, 2, use_cache=False)
    if df is not None and len(df): return float(df["close"].iloc[-1])
    return None

def atr(df, n=14):
    v = (df["high"] - df["low"]).rolling(n).mean().iloc[-1]
    return v if not pd.isna(v) else (df["high"] - df["low"]).mean()

def htf_bias(df):
    if df is None or len(df) < 50: return "NEUTRAL"
    e21 = df["close"].ewm(span=21).mean().iloc[-1]
    e50 = df["close"].ewm(span=50).mean().iloc[-1]
    p = df["close"].iloc[-1]
    if p > e21 > e50: return "BULL"
    if p < e21 < e50: return "BEAR"
    return "NEUTRAL"

def detect_pattern(df):
    if df is None or len(df) < 4: return None
    last, prev = df.iloc[-1], df.iloc[-2]
    body = abs(last["close"] - last["open"]) or 1e-9
    uw = last["high"] - max(last["open"], last["close"])
    lw = min(last["open"], last["close"]) - last["low"]
    if lw > body * 2 and last["close"] > last["open"]:
        return {"pattern": "PinBar", "bias": "BULL"}
    if uw > body * 2 and last["close"] < last["open"]:
        return {"pattern": "PinBar", "bias": "BEAR"}
    if (last["close"] > last["open"] and prev["close"] < prev["open"]
        and last["close"] > prev["open"] and last["open"] < prev["close"]):
        return {"pattern": "Engulfing", "bias": "BULL"}
    if (last["close"] < last["open"] and prev["close"] > prev["open"]
        and last["open"] > prev["close"] and last["close"] < prev["open"]):
        return {"pattern": "Engulfing", "bias": "BEAR"}
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

def find_pivots(df, left=2, right=2):
    h, l = df["high"].values, df["low"].values
    piv = []
    for i in range(left, len(df) - right):
        if h[i] == max(h[i-left:i+right+1]): piv.append((i, h[i], "H"))
        if l[i] == min(l[i-left:i+right+1]): piv.append((i, l[i], "L"))
    return piv

def detect_zones(df, lookback=120, impulse_atr=1.0, single_base=False):
    df = df.reset_index(drop=True)
    a = (df["high"] - df["low"]).rolling(14).mean()
    zones, n = [], len(df)
    start = max(20, n - lookback)
    for i in range(start, n - 3):
        if pd.isna(a.iloc[i]) or a.iloc[i] == 0: continue
        body = abs(df["close"].iloc[i] - df["open"].iloc[i])
        if body < a.iloc[i] * impulse_atr: continue
        if single_base:
            base = df.iloc[i-1:i]
        else:
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
        after = df.iloc[z["created_idx"]+1:last_idx]
        if len(after) == 0: z["fresh"] = True; continue
        if z["side"] == "demand":
            broken = (after["close"] < z["bot"] - a * 0.3).any()
        else:
            broken = (after["close"] > z["top"] + a * 0.3).any()
        z["fresh"] = not broken
    return zones

def price_interacts_zone(df, zone, lookback=6, pad=0.0):
    recent = df.iloc[-lookback:]
    top, bot = zone["top"] + pad, zone["bot"] - pad
    return bool(((recent["low"] <= top) & (recent["high"] >= bot)).any())

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

def detect_sweep(df, pool, lookahead=20):
    last = max(pool["idx"])
    if last >= len(df) - 1: return None
    level = pool["price"]
    scan = df.iloc[last+1:last+1+lookahead]
    for i, row in scan.iterrows():
        if pool["kind"] == "H" and row["high"] > level and row["close"] < level:
            return {"kind": "sweep_high", "level": level, "idx": int(i), "bias": "BEAR"}
        if pool["kind"] == "L" and row["low"] < level and row["close"] > level:
            return {"kind": "sweep_low", "level": level, "idx": int(i), "bias": "BULL"}
    return None

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
            "r2": float(r2), "kind": kind,
            "x0": int(xs[0]), "x1": int(xs[-1])}

def tl_value(tl, x): return tl["slope"] * x + tl["intercept"]
def tl_touch(tl, df, idx, tol_atr, a):
    return abs(df["close"].iloc[idx] - tl_value(tl, idx)) < tol_atr * a
def tl_break(tl, df, idx):
    line = tl_value(tl, idx); c = df["close"].iloc[idx]
    return c > line if tl["kind"] == "H" else c < line

def find_sr_levels(df, tolerance=0.002):
    piv = find_pivots(df, left=3, right=3)
    levels = []
    for kind in ("H", "L"):
        pts = sorted([p for p in piv if p[2] == kind], key=lambda x: x[1])
        used = set()
        for i in range(len(pts)):
            if i in used: continue
            cl = [pts[i]]; used.add(i)
            for j in range(i+1, len(pts)):
                if j in used: continue
                if abs(pts[j][1] - pts[i][1]) / pts[i][1] < tolerance:
                    cl.append(pts[j]); used.add(j)
            if len(cl) >= 2:
                levels.append({"kind": kind,
                               "price": float(np.mean([c[1] for c in cl])),
                               "touches": len(cl),
                               "last_idx": max(c[0] for c in cl)})
    return sorted(levels, key=lambda x: -x["touches"])

def price_near_level(price, level, tolerance_atr, a):
    return abs(price - level["price"]) < tolerance_atr * a

def detect_break_retest(df, level, side, break_lookback=30):
    n = len(df)
    price = level["price"]
    start = max(0, n - break_lookback)
    recent = df.iloc[start:n]
    if side == "BULL":
        broke = recent[recent["close"] > price]
        if len(broke) == 0: return False
        bi = broke.index[0]
        after = df.iloc[bi+1:n]
        if len(after) == 0: return False
        retest = ((after["low"] <= price * 1.001) & (after["high"] >= price * 0.999)).any()
        return bool(retest)
    else:
        broke = recent[recent["close"] < price]
        if len(broke) == 0: return False
        bi = broke.index[0]
        after = df.iloc[bi+1:n]
        if len(after) == 0: return False
        retest = ((after["high"] >= price * 0.999) & (after["low"] <= price * 1.001)).any()
        return bool(retest)

async def fetch_multi_tf_zones(symbol, tfs=("H1", "M30", "M15")):
    zones = []
    for tf in tfs:
        df = await fetch_data(symbol, tf, count=200)
        if df is None or len(df) < 40: continue
        for z in detect_zones(df, 120):
            z["tf"] = tf
            zones.append(z)
    return zones

def evaluate_day_setup(df_m5, zones_htf, atr_val, target_bias):
    if df_m5 is None or len(df_m5) < 60: return None
    df = df_m5.reset_index(drop=True)
    pat = detect_pattern(df)
    if not pat: return None
    if pat["bias"] != target_bias and pat["bias"] != "NEUTRAL": return None
    price = float(df["close"].iloc[-1])
    zone = None
    for z in zones_htf:
        if z["side"] != ("demand" if target_bias == "BULL" else "supply"): continue
        if not z["fresh"]: continue
        if price_interacts_zone(df, z, 6, atr_val * 0.35):
            zone = z; break
    if not zone: return None
    sr_levels = find_sr_levels(df)
    sr_hit = None
    for lv in sr_levels:
        if price_near_level(price, lv, 1.0, atr_val):
            sr_hit = lv; break
    if not sr_hit: return None
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
    tl_ok = False
    if target_bias == "BULL":
        if tl_l and tl_touch(tl_l, df, idx, 0.6, atr_val): tl_ok = True
        elif tl_h and tl_break(tl_h, df, idx): tl_ok = True
    else:
        if tl_h and tl_touch(tl_h, df, idx, 0.6, atr_val): tl_ok = True
        elif tl_l and tl_break(tl_l, df, idx): tl_ok = True
    br_ok = False
    for lv in sr_levels:
        if detect_break_retest(df, lv, target_bias, 30):
            br_ok = True; break
    score = (1 if sweep else 0) + (1 if tl_ok else 0) + (1 if br_ok else 0)
    if score < 1: return None
    last_c = df.iloc[-1]
    if target_bias == "BULL":
        sl = float(last_c["low"] - atr_val * 0.5); risk = price - sl
    else:
        sl = float(last_c["high"] + atr_val * 0.5); risk = sl - price
    if risk <= 0 or risk > atr_val * 2.5: return None
    tp = price + current_rr() * risk if target_bias == "BULL" else price - current_rr() * risk
    return {"bias": target_bias, "pattern": pat["pattern"], "entry": price,
            "sl": sl, "tp": tp, "atr": float(atr_val),
            "zone_kind": zone["kind"], "zone_tf": zone.get("tf", "H1"),
            "sweep": sweep["kind"] if sweep else "none",
            "tl": "yes" if tl_ok else "no", "br": "yes" if br_ok else "no",
            "score": score, "df": df}

def evaluate_scalp_setup(df_entry, target_bias, atr_val):
    if df_entry is None or len(df_entry) < 40: return None
    df = df_entry.reset_index(drop=True)
    pat = detect_pattern(df)
    if not pat: return None
    if pat["bias"] != target_bias and pat["bias"] != "NEUTRAL": return None
    price = float(df["close"].iloc[-1])
    sr = find_sr_levels(df)
    sr_hit = None
    for lv in sr:
        if price_near_level(price, lv, 1.2, atr_val):
            sr_hit = lv; break
    if not sr_hit: return None
    br_ok = detect_break_retest(df, sr_hit, target_bias, 30)
    if not br_ok: return None
    piv = find_pivots(df)
    tl_h = fit_trendline(piv, "H", 2)
    tl_l = fit_trendline(piv, "L", 2)
    idx = len(df) - 1
    tl_ok = False
    tl_reason = ""
    if target_bias == "BULL":
        if tl_l and tl_touch(tl_l, df, idx, 0.7, atr_val):
            tl_ok = True; tl_reason = "TL_touch"
        elif tl_h and tl_break(tl_h, df, idx):
            tl_ok = True; tl_reason = "TL_break"
    else:
        if tl_h and tl_touch(tl_h, df, idx, 0.7, atr_val):
            tl_ok = True; tl_reason = "TL_touch"
        elif tl_l and tl_break(tl_l, df, idx):
            tl_ok = True; tl_reason = "TL_break"
    if not tl_ok: return None
    last_c = df.iloc[-1]
    if target_bias == "BULL":
        sl = float(last_c["low"] - atr_val * 0.4); risk = price - sl
    else:
        sl = float(last_c["high"] + atr_val * 0.4); risk = sl - price
    if risk <= 0 or risk > atr_val * 3: return None
    tp = price + current_rr() * risk if target_bias == "BULL" else price - current_rr() * risk
    return {"bias": target_bias, "pattern": pat["pattern"], "entry": price,
            "sl": sl, "tp": tp, "atr": float(atr_val),
            "sr_kind": sr_hit["kind"], "sr_price": sr_hit["price"],
            "br": "yes", "tl": tl_reason, "df": df}

def evaluate_swing_setup(df_entry, zones_htf, atr_val, target_bias):
    if df_entry is None or len(df_entry) < 40: return None
    df = df_entry.reset_index(drop=True)
    price = float(df["close"].iloc[-1])
    zone = None
    for z in zones_htf:
        if z["side"] != ("demand" if target_bias == "BULL" else "supply"): continue
        if not z["fresh"]: continue
        if price_interacts_zone(df, z, 8, atr_val * 0.4):
            zone = z; break
    if not zone: return None
    piv = find_pivots(df)
    tl_h = fit_trendline(piv, "H", 2)
    tl_l = fit_trendline(piv, "L", 2)
    idx = len(df) - 1
    tl_ok = False
    if target_bias == "BULL":
        if tl_l and tl_touch(tl_l, df, idx, 0.8, atr_val): tl_ok = True
    else:
        if tl_h and tl_touch(tl_h, df, idx, 0.8, atr_val): tl_ok = True
    last_c = df.iloc[-1]
    if target_bias == "BULL":
        sl = float(last_c["low"] - atr_val * 0.7); risk = price - sl
    else:
        sl = float(last_c["high"] + atr_val * 0.7); risk = sl - price
    if risk <= 0 or risk > atr_val * 3.5: return None
    tp = price + current_rr() * risk if target_bias == "BULL" else price - current_rr() * risk
    return {"bias": target_bias, "pattern": "Swing",
            "entry": price, "sl": sl, "tp": tp, "atr": float(atr_val),
            "zone_kind": zone["kind"], "zone_tf": zone.get("tf", "H1"),
            "sweep": "none", "tl": "yes" if tl_ok else "no",
            "br": "no", "score": 1, "df": df}

async def evaluate_setup(symbol):
    mode = CURRENT_MODE
    if mode == "day":
        m5 = await fetch_data(symbol, "M5")
        h1 = await fetch_data(symbol, "H1")
        h4 = await fetch_data(symbol, "H4")
        if m5 is None or h1 is None or h4 is None: return None
        bh1, bh4 = htf_bias(h1), htf_bias(h4)
        if bh4 == "NEUTRAL": return None
        target = bh4
        zones = await fetch_multi_tf_zones(symbol, ("H1", "M30", "M15"))
        zones = mark_freshness(zones, m5)
        r = evaluate_day_setup(m5, zones, atr(m5), target)
        if r:
            r.update({"symbol": symbol, "mode": mode, "tf": "M5",
                      "bh1": bh1, "bh4": bh4})
            return r
    elif mode == "scalp":
        m1 = await fetch_data(symbol, "M1")
        m5 = await fetch_data(symbol, "M5")
        m15 = await fetch_data(symbol, "M15")
        if m1 is None or m5 is None or m15 is None: return None
        bh15 = htf_bias(m15)
        if bh15 == "NEUTRAL": return None
        for tf_name, df in [("M1", m1), ("M5", m5)]:
            r = evaluate_scalp_setup(df, bh15, atr(df))
            if r:
                r.update({"symbol": symbol, "mode": mode, "tf": tf_name,
                          "bh1": bh15, "bh4": bh15})
                return r
    elif mode == "swing":
        m30 = await fetch_data(symbol, "M30")
        h1 = await fetch_data(symbol, "H1")
        h4 = await fetch_data(symbol, "H4")
        if m30 is None or h1 is None or h4 is None: return None
        bh1, bh4 = htf_bias(h1), htf_bias(h4)
        if bh4 == "NEUTRAL" or bh1 != bh4: return None
        zones = []
        for tf in ("H4", "H1", "M30"):
            zdf = await fetch_data(symbol, tf, count=200)
            if zdf is None or len(zdf) < 40: continue
            for z in detect_zones(zdf, 120, single_base=True):
                z["tf"] = tf
                zones.append(z)
        zones = mark_freshness(zones, m30)
        r = evaluate_swing_setup(m30, zones, atr(m30), bh4)
        if r:
            r.update({"symbol": symbol, "mode": mode, "tf": "M30",
                      "bh1": bh1, "bh4": bh4})
            return r
    return None

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
             s.get("zone_kind", ""), s.get("sweep", ""), s.get("tl", "")))
        c.commit(); return cur.lastrowid

def db_open():
    with _db_lock, sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM signals WHERE status='OPEN'").fetchall()]

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

def build_chart(setup):
    df = setup["df"]
    df_plot = df.tail(90).copy()
    offset = len(df) - len(df_plot)
    df_plot = df_plot.set_index("epoch")
    title = f"{setup['symbol']} {setup['mode']} {setup['bias']} {setup['pattern']} | {setup['tf']}"
    fig, axlist = mpf.plot(df_plot, type="candle", style="charles",
                            returnfig=True, figsize=(15, 8), title=title,
                            ylabel="Price", volume=False)
    ax = axlist[0]; n = len(df_plot)
    if setup["mode"] == "day":
        zones = mark_freshness(detect_zones(df, 120), df)
        for z in zones[-6:]:
            x0 = max(0, z["created_idx"] - offset)
            if x0 >= n: continue
            color = "#00ff88" if z["side"] == "demand" else "#ff4466"
            ax.add_patch(Rectangle((x0, z["bot"]), n-x0, z["top"]-z["bot"],
                                   facecolor=color, alpha=0.2, edgecolor=color, linewidth=0.8))
        sr = find_sr_levels(df)
        for lv in sr[:5]:
            ax.axhline(lv["price"], color="#ffaa00", linestyle="-", linewidth=0.8, alpha=0.6)
    elif setup["mode"] == "scalp":
        sr = find_sr_levels(df)
        for lv in sr[:6]:
            ax.axhline(lv["price"], color="#ffaa00", linestyle="-", linewidth=1.0, alpha=0.8)
            ax.text(n*0.005, lv["price"], f"SR {lv['touches']}", fontsize=7, color="#ffaa00")
    elif setup["mode"] == "swing":
        zones = mark_freshness(detect_zones(df, 120, single_base=True), df)
        for z in zones[-6:]:
            x0 = max(0, z["created_idx"] - offset)
            if x0 >= n: continue
            color = "#00ff88" if z["side"] == "demand" else "#ff4466"
            ax.add_patch(Rectangle((x0, z["bot"]), n-x0, z["top"]-z["bot"],
                                   facecolor=color, alpha=0.25, edgecolor=color, linewidth=1.0))
            ax.text(x0+0.3, z["top"], z["kind"], fontsize=7, color=color)
    pools = equal_levels(df, 0.0025)
    for pool in pools[-6:]:
        ax.axhline(pool["price"], color="#ffcc00", linestyle=":", linewidth=0.8, alpha=0.6)
    piv = find_pivots(df)
    tl_h = fit_trendline(piv, "H", 2); tl_l = fit_trendline(piv, "L", 2)
    for tl in (tl_h, tl_l):
        if not tl: continue
        xs = np.arange(max(0, tl["x0"] - offset), n)
        ys = tl_value(tl, xs + offset)
        ax.plot(xs, ys, color="#00aaff", linewidth=1.4, linestyle="--")
    ax.axhline(setup["entry"], color="white", linewidth=1.2)
    ax.axhline(setup["sl"], color="#ff2222", linewidth=1.4)
    ax.axhline(setup["tp"], color="#22ff22", linewidth=1.4)
    ax.text(n*0.995, setup["entry"], f" ENTRY {fmt_price(setup['symbol'], setup['entry'])}",
            fontsize=7, color="white", va="bottom", ha="right")
    ax.text(n*0.995, setup["sl"], f" SL {fmt_price(setup['symbol'], setup['sl'])}",
            fontsize=7, color="#ff2222", va="top", ha="right")
    ax.text(n*0.995, setup["tp"], f" TP {fmt_price(setup['symbol'], setup['tp'])}",
            fontsize=7, color="#22ff22", va="bottom", ha="right")
    fd, path = tempfile.mkstemp(suffix=".png", prefix="sfx_")
    os.close(fd)
    fig.savefig(path, dpi=110, bbox_inches="tight", facecolor="#0e1117")
    plt.close(fig); return path

    async def download_history(deriv_sym, gran, months=3):
    total_needed = months * 30 * 24 * (3600 // gran) + 500
    all_c, end, attempts = [], "latest", 0
    while len(all_c) < total_needed and attempts < 200:
        attempts += 1
        try:
            resp = await _ws_fetch(deriv_sym, gran, 2000, end=end)
            got = len(resp.get("candles", []))
            print(f"[{deriv_sym}/{gran}] batch {attempts}: {got} (total {len(all_c)})")
        except asyncio.TimeoutError:
            await asyncio.sleep(2); continue
        except Exception as e:
            print(f"[{deriv_sym}] {e}"); await asyncio.sleep(2); continue
        if "candles" not in resp: break
        batch = resp["candles"]
        if not batch: break
        all_c = batch + all_c
        end = batch[0]["epoch"] - 1
    if not all_c: return None
    df = pd.DataFrame(all_c)
    for c in ("open", "high", "low", "close"): df[c] = df[c].astype(float)
    df["epoch"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
    return df.drop_duplicates("epoch").reset_index(drop=True)

def _prepare(df_m):
    df = df_m.copy()
    idx = df.set_index("epoch")
    def rs(rule):
        return (idx.resample(rule).agg({"open":"first","high":"max","low":"min","close":"last"}).dropna().reset_index())
    return df, rs("5min"), rs("15min"), rs("30min"), rs("1h"), rs("4h")

def simulate_trade(df, i, bias, sl, tp, max_bars=300):
    for j in range(i+1, min(i+max_bars, len(df))):
        b = df.iloc[j]
        if bias == "BULL":
            if b["low"] <= sl: return "SL"
            if b["high"] >= tp: return "TP"
        else:
            if b["high"] >= sl: return "SL"
            if b["low"] <= tp: return "TP"
    return "EXPIRED"

async def run_backtest(symbols, months, gran_min, progress_cb=None):
    mode = CURRENT_MODE
    results = []
    for sym in symbols:
        if progress_cb:
            await progress_cb(f"Downloading {months}mo {gran_min}m for {sym}")
        df_full = await download_history(to_deriv(sym), gran_min*60, months)
        if df_full is None or len(df_full) < 500:
            results.append({"symbol": sym, "error": "no data"}); continue
        df5, df15, df30, dfh1, dfh4 = await asyncio.to_thread(_prepare, df_full)
        if progress_cb:
            await progress_cb(f"Backtesting {sym} mode={mode} ({len(df_full)} bars)")
        r = await asyncio.to_thread(_bt_symbol, df_full, df5, df15, df30, dfh1, dfh4, sym, mode)
        results.append(r)
    return results

  def _bt_symbol(df_full, df5, df15, df30, dfh1, dfh4, symbol, mode):
    n = len(df_full)
    a_full = (df_full["high"] - df_full["low"]).rolling(14).mean()
    trades, last_i = [], -999
    step = 5 if mode == "scalp" else 2
    warm = 200
    cooldown = 6 if mode == "scalp" else 12
    for i in range(warm, n - 300, step):
        if i - last_i < cooldown: continue
        av = a_full.iloc[i]
        if pd.isna(av) or av == 0: continue
        w = df_full.iloc[max(0, i-200):i+1].reset_index(drop=True)
        target = None
        r = None
        if mode == "day":
            h4_pos = min(i // 12, len(dfh4)-1) if len(dfh4) > 50 else -1
            h1_pos = min(i // 12, len(dfh1)-1) if len(dfh1) > 50 else -1
            if h4_pos < 50 or h1_pos < 50: continue
            bh4 = htf_bias(dfh4.iloc[max(0,h4_pos-200):h4_pos+1])
            bh1 = htf_bias(dfh1.iloc[max(0,h1_pos-200):h1_pos+1])
            if bh4 == "NEUTRAL": continue
            target = bh4
            zones = []
            for zdf, tf in [(dfh1, "H1"), (df30, "M30"), (df15, "M15")]:
                pos = min(i // max(1, len(df_full)//max(1,len(zdf))), len(zdf)-1)
                if pos < 40: continue
                zw = zdf.iloc[max(0, pos-120):pos+1].reset_index(drop=True)
                for z in detect_zones(zw, 120):
                    z["tf"] = tf; zones.append(z)
            zones = mark_freshness(zones, w) if zones else []
            r = evaluate_day_setup(w, zones, av, target)
        elif mode == "scalp":
            bh15_pos = min(i // 3, len(df15)-1) if len(df15)>50 else -1
            if bh15_pos < 50: continue
            bh15 = htf_bias(df15.iloc[max(0,bh15_pos-200):bh15_pos+1])
            if bh15 == "NEUTRAL": continue
            target = bh15
            r = evaluate_scalp_setup(w, target, av)
        elif mode == "swing":
            bh4_pos = min(i // 12, len(dfh4)-1) if len(dfh4)>50 else -1
            bh1_pos = min(i // 12, len(dfh1)-1) if len(dfh1)>50 else -1
            if bh4_pos < 50 or bh1_pos < 50: continue
            bh4 = htf_bias(dfh4.iloc[max(0,bh4_pos-200):bh4_pos+1])
            bh1 = htf_bias(dfh1.iloc[max(0,bh1_pos-200):bh1_pos+1])
            if bh4 == "NEUTRAL" or bh1 != bh4: continue
            target = bh4
            zones = []
            for zdf, tf in [(dfh4, "H4"), (dfh1, "H1"), (df30, "M30")]:
                pos = min(i // max(1, len(df_full)//max(1,len(zdf))), len(zdf)-1)
                if pos < 40: continue
                zw = zdf.iloc[max(0, pos-120):pos+1].reset_index(drop=True)
                for z in detect_zones(zw, 120, single_base=True):
                    z["tf"] = tf; zones.append(z)
            zones = mark_freshness(zones, w) if zones else []
            r = evaluate_swing_setup(w, zones, av, target)
        if not r: continue
        outcome = simulate_trade(df_full, i, target, r["sl"], r["tp"])
        trades.append({"bias": target, "outcome": outcome})
        last_i = i
    wins = sum(1 for t in trades if t["outcome"] == "TP")
    losses = sum(1 for t in trades if t["outcome"] == "SL")
    expd = sum(1 for t in trades if t["outcome"] == "EXPIRED")
    n_done = wins + losses
    wr = wins / n_done * 100 if n_done else 0
    rr = current_rr()
    expct = (wins * rr - losses) / max(1, len(trades))
    pf = (wins * rr) / max(1, losses)
    return {"symbol": symbol, "mode": mode, "trades": len(trades),
            "wins": wins, "losses": losses, "expired": expd,
            "wr": wr, "expectancy": expct, "pf": pf}

    last_signal_time = {}

async def tracker_loop(app):
    while True:
        try:
            for s in db_open():
                p = await fetch_price(s["symbol"])
                if p is None: continue
                hit = None
                if s["bias"] == "BULL":
                    if p <= s["sl"]: hit = "SL"
                    elif p >= s["tp"]: hit = "TP"
                else:
                    if p >= s["sl"]: hit = "SL"
                    elif p <= s["tp"]: hit = "TP"
                if not hit: continue
                db_close(s["id"], hit)
                txt = hit + " HIT " + s["symbol"] + " id#" + str(s["id"])
                try:
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=txt)
                except Exception as e:
                    print("tracker notify:", e)
        except Exception as e:
            print("tracker:", e)
        await asyncio.sleep(60)

async def autoscan_once(app):
    global last_signal_time
    for sym in get_active_symbols():
        s = await evaluate_setup(sym)
        if not s: continue
        cd = current_cooldown()
        if time.time() - last_signal_time.get(sym, 0) < cd: continue
        last_signal_time[sym] = time.time()
        sid = db_save(s)
        cp = None
        try:
            cp = build_chart(s)
        except Exception as e:
            print("chart:", e); continue
        side = "BUY" if s["bias"] == "BULL" else "SELL"
        cap = side + " " + sym + " " + s["pattern"] + " " + s["mode"] + " " + s["tf"] + " id#" + str(sid)
        try:
            f = open(cp, "rb")
            await app.bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=f, caption=cap)
            f.close()
        except Exception as e:
            print("send:", e)
        if cp and os.path.exists(cp):
            try: os.remove(cp)
            except: pass

async def autoscan_loop(app):
    await asyncio.sleep(30)
    while True:
        print("autoscan tick mode=" + CURRENT_MODE)
        try:
            await autoscan_once(app)
        except Exception as e:
            print("autoscan:", e)
        await asyncio.sleep(300)

      async def start_cmd(upd, ctx):
    txt = "StarFX V16 mode=" + CURRENT_MODE + "\n"
    txt += "Symbols: " + ", ".join(get_active_symbols()) + "\n"
    txt += "/mode day|scalp|swing\n/signal /price /report /diag /backtest"
    await upd.message.reply_text(txt)

async def mode_cmd(upd, ctx):
    global CURRENT_MODE
    args = list(ctx.args or [])
    if not args:
        await upd.message.reply_text("Current mode: " + CURRENT_MODE + "\nUsage: /mode day|scalp|swing")
        return
    m = args[0].lower()
    if m not in ("day", "scalp", "swing"):
        await upd.message.reply_text("Unknown. Use day, scalp or swing")
        return
    CURRENT_MODE = m
    await upd.message.reply_text("Mode: " + CURRENT_MODE.upper())

async def price_cmd(upd, ctx):
    lines = ["Live:"]
    for sym in get_active_symbols():
        p = await fetch_price(sym)
        lines.append(sym + ": " + (fmt_price(sym, p) if p else "lag"))
    await upd.message.reply_text("\n".join(lines))

async def signal_cmd(upd, ctx):
    await upd.message.reply_text("Scanning mode=" + CURRENT_MODE)
    found = 0
    for sym in get_active_symbols():
        try: s = await evaluate_setup(sym)
        except Exception as e: print("eval:", e); continue
        if not s: continue
        found += 1
        last_signal_time[sym] = time.time()
        sid = db_save(s)
        cp = None
        try: cp = build_chart(s)
        except Exception as e: print("chart:", e); continue
        side = "BUY" if s["bias"] == "BULL" else "SELL"
        cap = side + " " + sym + " " + s["pattern"] + " " + s["mode"] + " " + s["tf"] + " id#" + str(sid)
        try:
            f = open(cp, "rb")
            await upd.message.reply_photo(photo=f, caption=cap)
            f.close()
        except Exception as e: print("send:", e)
        if cp and os.path.exists(cp):
            try: os.remove(cp)
            except: pass
    if found == 0:
        await upd.message.reply_text("No setups in mode=" + CURRENT_MODE)

async def report_cmd(upd, ctx):
    s = db_stats()
    wins = s.get("TP", 0); losses = s.get("SL", 0)
    opens = s.get("OPEN", 0); expd = s.get("EXPIRED", 0)
    n_done = wins + losses
    wr = wins / n_done * 100 if n_done else 0
    rr = current_rr()
    expct = (wins * rr - losses) / max(1, n_done) if n_done else 0
    pf = (wins * rr) / max(1, losses)
    txt = "REPORT mode=" + CURRENT_MODE + "\n"
    txt += "Total: " + str(s.get("total", 0)) + "\n"
    txt += "W: " + str(wins) + " L: " + str(losses) + " E: " + str(expd) + " Open: " + str(opens) + "\n"
    txt += "WR: " + f"{wr:.1f}%" + " Exp: " + f"{expct:+.2f}R" + " PF: " + f"{pf:.2f}"
    await upd.message.reply_text(txt)


async def diag_cmd(upd, ctx):
    args = list(ctx.args or [])
    months = 1
    if args and args[-1].isdigit():
        months = int(args[-1]); args = args[:-1]
    syms = [args[0]] if (args and args[0] in SYMBOL_MAP) else get_active_symbols()
    await upd.message.reply_text("Diag mode=" + CURRENT_MODE + " syms=" + ",".join(syms))
    for sym in syms:
        df = await download_history(to_deriv(sym), 300, months)
        if df is None or len(df) < 500:
            await upd.message.reply_text(sym + ": no data"); continue
        await upd.message.reply_text(sym + ": " + str(len(df)) + " bars, analyzing...")
        count = {"bars": 0, "hit": 0}
        df5, df15, df30, dfh1, dfh4 = await asyncio.to_thread(_prepare, df)
        n = len(df)
        a_full = (df["high"] - df["low"]).rolling(14).mean()
        for i in range(200, n - 200, 10):
            count["bars"] += 1
            av = a_full.iloc[i]
            if pd.isna(av) or av == 0: continue
            w = df.iloc[max(0, i-200):i+1].reset_index(drop=True)
            try:
                if CURRENT_MODE == "day":
                    h4i = min(i//12, len(dfh4)-1)
                    if h4i < 50: continue
                    bh4 = htf_bias(dfh4.iloc[max(0,h4i-200):h4i+1])
                    if bh4 == "NEUTRAL": continue
                    zones = []
                    for zdf, tf in [(dfh1, "H1"), (df30, "M30"), (df15, "M15")]:
                        pos = min(i // max(1, n//max(1,len(zdf))), len(zdf)-1)
                        if pos < 40: continue
                        zw = zdf.iloc[max(0,pos-120):pos+1].reset_index(drop=True)
                        for z in detect_zones(zw, 120):
                            z["tf"] = tf; zones.append(z)
                    zones = mark_freshness(zones, w) if zones else []
                    if evaluate_day_setup(w, zones, av, bh4): count["hit"] += 1
                elif CURRENT_MODE == "scalp":
                    m15i = min(i//3, len(df15)-1)
                    if m15i < 50: continue
                    bh15 = htf_bias(df15.iloc[max(0,m15i-200):m15i+1])
                    if bh15 == "NEUTRAL": continue
                    if evaluate_scalp_setup(w, bh15, av): count["hit"] += 1
                elif CURRENT_MODE == "swing":
                    h4i = min(i//12, len(dfh4)-1)
                    h1i = min(i//12, len(dfh1)-1)
                    if h4i < 50 or h1i < 50: continue
                    bh4 = htf_bias(dfh4.iloc[max(0,h4i-200):h4i+1])
                    bh1 = htf_bias(dfh1.iloc[max(0,h1i-200):h1i+1])
                    if bh4 == "NEUTRAL" or bh1 != bh4: continue
                    zones = []
                    for zdf, tf in [(dfh4, "H4"), (dfh1, "H1"), (df30, "M30")]:
                        pos = min(i // max(1, n//max(1,len(zdf))), len(zdf)-1)
                        if pos < 40: continue
                        zw = zdf.iloc[max(0,pos-120):pos+1].reset_index(drop=True)
                        for z in detect_zones(zw, 120, single_base=True):
                            z["tf"] = tf; zones.append(z)
                    zones = mark_freshness(zones, w) if zones else []
                    if evaluate_swing_setup(w, zones, av, bh4): count["hit"] += 1
            except Exception as e:
                print("diag loop:", e); continue
        await upd.message.reply_text(sym + " bars=" + str(count["bars"]) + " hits=" + str(count["hit"]))

async def backtest_cmd(upd, ctx):
    args = list(ctx.args or [])
    months = 3
    if args and args[-1].isdigit():
        months = int(args[-1]); args = args[:-1]
    syms = [args[0]] if (args and args[0] in SYMBOL_MAP) else get_active_symbols()
    await upd.message.reply_text("Backtest mode=" + CURRENT_MODE + " " + str(months) + "mo " + ",".join(syms))
    try:
        results = await run_backtest(syms, months, 5, progress_cb=None)
    except Exception as e:
        await upd.message.reply_text("Failed: " + str(e)); return
    for r in results:
        if "error" in r:
            await upd.message.reply_text(r["symbol"] + ": " + r["error"]); continue
        txt = r["symbol"] + " " + r["mode"] + "\n"
        txt += "Trades: " + str(r["trades"]) + " W:" + str(r["wins"]) + " L:" + str(r["losses"]) + " E:" + str(r["expired"]) + "\n"
        txt += "WR: " + f"{r['wr']:.1f}%" + " Exp: " + f"{r['expectancy']:+.2f}R" + " PF: " + f"{r['pf']:.2f}"
        await upd.message.reply_text(txt)

async def error_handler(upd, ctx):
    print("PTB error:", ctx.error)

async def post_init(app):
    db_init()
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="StarFX V16 booted - mode=" + CURRENT_MODE)
        print("[boot] ping ok")
    except Exception as e:
        print("[boot] ping fail:", e)
    asyncio.create_task(tracker_loop(app))
    asyncio.create_task(autoscan_loop(app))

def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN required")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("diag", diag_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_error_handler(error_handler)
    print("StarFX V16 running")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()                
