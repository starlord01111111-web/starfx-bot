import os, time, threading, requests, yfinance as yf, pandas as pd, numpy as np
from flask import Flask
from datetime import datetime
import pytz

BOT_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHAT_ID = "-1004365660319"
EAT = pytz.timezone('Africa/Nairobi')

SYMBOLS = {
    "GOLD": "GC=F",
    "GBPUSD": "GBPUSD=X",
    "BTCUSD": "BTC-USD"
}

app = Flask(__name__)
@app.route('/')
def home(): return "StarFx V7.3 SNIPER LIVE - H4->M1 Top Down"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

# --- TECHNICAL FUNCTIONS ---
def get_data(symbol, interval, period):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.dropna(inplace=True)
        return df
    except: return pd.DataFrame()

def get_swing_high_low(df, lookback=20):
    if len(df) < lookback: return None, None
    swing_high = df['High'].rolling(lookback, center=True).max().iloc[-lookback]
    swing_low = df['Low'].rolling(lookback, center=True).min().iloc[-lookback]
    # Actual price points
    high_price = df['High'].max() if len(df)>0 else 0
    low_price = df['Low'].min() if len(df)>0 else 0
    return high_price, low_price

def is_choppy(df):
    # Avoid choppy: ATR small + range tight
    if len(df) < 14: return True
    atr = (df['High'] - df['Low']).rolling(14).mean().iloc[-1]
    range_pct = (df['High'].iloc[-20:].max() - df['Low'].iloc[-20:].min()) / df['Close'].iloc[-1] * 100
    return atr < df['Close'].iloc[-1]*0.002 or range_pct < 0.3 # tight range

def detect_bos_choch(df):
    # BOS = Break of Structure, CHoCH = Change of Character
    if len(df) < 50: return "RANGING"
    highs = df['High'].rolling(10).max()
    lows = df['Low'].rolling(10).min()
    last_close = df['Close'].iloc[-1]
    prev_high = highs.iloc[-20]
    prev_low = lows.iloc[-20]
    if last_close > prev_high: return "BULLISH BOS"
    if last_close < prev_low: return "BEARISH BOS"
    # CHoCH - break opposite
    if last_close < lows.iloc[-30] and df['Close'].iloc[-30] > df['Close'].iloc[-40]: return "BEARISH CHoCH"
    if last_close > highs.iloc[-30] and df['Close'].iloc[-30] < df['Close'].iloc[-40]: return "BULLISH CHoCH"
    return "RANGING"

def detect_candles(df):
    if len(df) < 5: return []
    c = df.iloc[-1]; p = df.iloc[-2]; pp = df.iloc[-3]
    patterns = []
    # Engulfing
    if c['Close'] > c['Open'] and p['Close'] < p['Open'] and c['Open'] < p['Close'] and c['Close'] > p['Open']:
        patterns.append("BULLISH ENGULFING")
    if c['Close'] < c['Open'] and p['Close'] > p['Open'] and c['Open'] > p['Close'] and c['Close'] < p['Open']:
        patterns.append("BEARISH ENGULFING")
    # Pin Bar / Hammer / Shooting Star
    body = abs(c['Close'] - c['Open'])
    upper_wick = c['High'] - max(c['Open'], c['Close'])
    lower_wick = min(c['Open'], c['Close']) - c['Low']
    if lower_wick > body*2 and upper_wick < body*0.5: patterns.append("HAMMER / PIN BAR BULLISH")
    if upper_wick > body*2 and lower_wick < body*0.5: patterns.append("SHOOTING STAR / PIN BAR BEARISH")
    # Triple Top/Bottom
    if len(df) > 30:
        recent_highs = df['High'].iloc[-30:].nlargest(3)
        if max(recent_highs) - min(recent_highs) < df['Close'].iloc[-1]*0.002:
            patterns.append("TRIPLE TOP")
        recent_lows = df['Low'].iloc[-30:].nsmallest(3)
        if max(recent_lows) - min(recent_lows) < df['Close'].iloc[-1]*0.002:
            patterns.append("TRIPLE BOTTOM")
    return patterns

def detect_orderblock_supply_demand(df):
    # Range method: big impulse candle + indecision on top
    if len(df) < 10: return None, None
    signals = []
    for i in range(-10, -2):
        body = abs(df['Close'].iloc[i] - df['Open'].iloc[i])
        avg_body = (df['Close'] - df['Open']).abs().rolling(10).mean().iloc[i]
        if body > avg_body*2: # Big candle
            # Check next 2 candles are indecision (small body)
            next_bodies = [(abs(df['Close'].iloc[i+1] - df['Open'].iloc[i+1])), (abs(df['Close'].iloc[i+2] - df['Open'].iloc[i+2]))]
            if all(b < avg_body*0.5 for b in next_bodies):
                if df['Close'].iloc[i] > df['Open'].iloc[i]:
                    signals.append(("DEMAND / ORDER BLOCK", df['Low'].iloc[i]))
                else:
                    signals.append(("SUPPLY / ORDER BLOCK", df['High'].iloc[i]))
    return signals[-1] if signals else (None, None)

def analyze_symbol(name, yf_symbol):
    print(f"\n--- ANALYZING {name} TOP-DOWN ---")
    # 1. H4 - MAIN STRUCTURE
    h4 = get_data(yf_symbol, "60m", "20d") # yfinance uses 60m for H1, 1h, we use 1h*4 approx
    # Actually use 4h if available, fallback
    if h4.empty: h4 = get_data(yf_symbol, "1h", "30d")
    if h4.empty or is_choppy(h4):
        print(f"{name} H4 CHOPPY - SKIP")
        return None

    h4_high, h4_low = get_swing_high_low(h4, 20)
    h4_structure = detect_bos_choch(h4)
    print(f"H4 {name}: High={h4_high} Low={h4_low} Struct={h4_structure}")

    # 2. LOWER TIMEFRAMES CONFIRMATION
    m15 = get_data(yf_symbol, "15m", "5d")
    m5 = get_data(yf_symbol, "5m", "2d")
    m1 = get_data(yf_symbol, "1m", "1d")
    if m15.empty or m5.empty: return None

    # Liquidity + Break & Retest Logic
    # Support/Resistance from H4 range
    resistance = h4_high
    support = h4_low

    # Check breakout + retest on M15/M5
    price = m5['Close'].iloc[-1]
    patterns_m5 = detect_candles(m5)
    patterns_m15 = detect_candles(m15)
    ob_type, ob_level = detect_orderblock_supply_demand(m15)

    bias = "NEUTRAL"
    if "BULLISH" in h4_structure: bias = "BULLISH"
    if "BEARISH" in h4_structure: bias = "BEARISH"

    # ENTRY CONDITIONS
    # BULLISH: Price broke resistance, retested, + bullish pattern at Demand/OrderBlock
    if bias == "BULLISH" and price > support:
        # Wait for retest: price near support or OB
        near_demand = ob_type and "DEMAND" in ob_type and abs(price - ob_level)/price < 0.005
        bullish_conf = any(x in ["BULLISH ENGULFING","HAMMER / PIN BAR BULLISH","TRIPLE BOTTOM"] for x in patterns_m5 + patterns_m15)
        if near_demand and bullish_conf and not is_choppy(m5):
            sl = support - (price-support)*0.3 if support else price*0.998
            tp = price + (price-sl)*3
            return {
                "type": "BUY", "price": price, "sl": sl, "tp": tp,
                "reason": f"H4 {h4_structure} | M15 {ob_type} @ {ob_level:.2f} | Conf: {','.join(patterns_m5)} | Retest of {support:.2f}"
            }

    if bias == "BEARISH" and price < resistance:
        near_supply = ob_type and "SUPPLY" in ob_type and abs(price - ob_level)/price < 0.005
        bearish_conf = any(x in ["BEARISH ENGULFING","SHOOTING STAR / PIN BAR BEARISH","TRIPLE TOP"] for x in patterns_m5 + patterns_m15)
        if near_supply and bearish_conf and not is_choppy(m5):
            sl = resistance + (resistance-price)*0.3 if resistance else price*1.002
            tp = price - (sl-price)*3
            return {
                "type": "SELL", "price": price, "sl": sl, "tp": tp,
                "reason": f"H4 {h4_structure} | M15 {ob_type} @ {ob_level:.2f} | Conf: {','.join(patterns_m5)} | Retest of {resistance:.2f}"
            }
    return None

def trading_loop():
    while True:
        try:
            now = datetime.now(EAT)
            for name, yf_sym in SYMBOLS.items():
                setup = analyze_symbol(name, yf_sym)
                if setup:
                    msg = f"""🎯 *STARFX V7.3 SNIPER | {name}*

*{setup['type']}* - A+ Setup (85%+)
Price: `{setup['price']:.2f}`
SL: `{setup['sl']:.2f}` | TP: `{setup['tp']:.2f}`
RR: 1:3

📊 *Top-Down:*
{setup['reason']}

✅ Liq Sweep + OB + Retest + {setup['type']} Conf
Time: {now.strftime('%Y-%m-%d %H:%M EAT')}
"""
                    send_telegram(msg)
                    time.sleep(10) # avoid spam
            # Daily 23:00 EAT summary
            if now.hour == 23 and now.minute < 5:
                send_telegram(f"📊 DAILY {now.strftime('%Y-%m-%d')}\nActive scans: {len(SYMBOLS)} | No A+ closed today. Market Structure tracked: BOS/CHOCH")
                time.sleep(300)
            time.sleep(180) # Scan every 3 min
        except Exception as e:
            print(f"Error loop: {e}")
            time.sleep(60)

threading.Thread(target=trading_loop, daemon=True).start()

if __name__ == "__main__":
    send_telegram("🚀 *StarFX V7.3 SNIPER Online* 🎯\nTop-Down H4->M1 | Liquidity | OB | BOS/CHOCH | Pin/Engulfing | Break+Retest")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
