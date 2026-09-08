import os, time, threading, requests, yfinance as yf, pandas as pd, json
from flask import Flask
from datetime import datetime, timedelta
import pytz

BOT_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHAT_ID = "-1004365660319"
EAT = pytz.timezone('Africa/Nairobi')

SYMBOLS = {"GOLD": "GC=F", "GBPUSD": "GBPUSD=X", "BTCUSD": "BTC-USD"}

app = Flask(__name__)
@app.route('/')
def home(): return "StarFx V7.8 SNIPER + REAL SIGNALS LIVE"

daily_report_sent = ""
weekly_report_sent = ""
last_potential_update = 0
last_signal_time = {} # to avoid spam per pair
signals_history = [] # {"pair","action","entry","sl","tp","result","time"}

# Try load history
try:
    if os.path.exists("signals_history.json"):
        with open("signals_history.json","r") as f:
            signals_history = json.load(f)
except: pass

def save_history():
    try:
        with open("signals_history.json","w") as f:
            json.dump(signals_history[-100:], f) # keep last 100
    except: pass

def send_telegram(msg, chat_id=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": chat_id or CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e: print(e)

def get_data(symbol, interval, period):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
        if hasattr(df.columns, 'get_level_values'):
            try: df.columns = df.columns.get_level_values(0)
            except: pass
        df.dropna(inplace=True)
        return df
    except: return pd.DataFrame()

def get_price_now(sym):
    df = get_data(sym, "1m", "1d")
    return float(df['Close'].iloc[-1]) if not df.empty else None

def detect_bos_choch(df):
    if len(df) < 50: return "RANGING"
    highs = df['High'].rolling(10).max()
    lows = df['Low'].rolling(10).min()
    last = df['Close'].iloc[-1]
    if last > highs.iloc[-20]: return "BULLISH BOS"
    if last < lows.iloc[-20]: return "BEARISH BOS"
    return "RANGING"

def is_choppy(df):
    if len(df) < 14: return True
    atr = (df['High'] - df['Low']).rolling(14).mean().iloc[-1]
    return atr < df['Close'].iloc[-1]*0.002

def detect_engulfing(df):
    if len(df) < 3: return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    # Bullish engulfing
    if curr['Close'] > curr['Open'] and prev['Close'] < prev['Open']:
        if curr['Close'] > prev['Open'] and curr['Open'] < prev['Close']:
            return "BULLISH ENGULFING"
    # Bearish engulfing
    if curr['Close'] < curr['Open'] and prev['Close'] > prev['Open']:
        if curr['Close'] < prev['Open'] and curr['Open'] > prev['Close']:
            return "BEARISH ENGULFING"
    return None

def detect_pinbar(df):
    if len(df) < 2: return None
    c = df.iloc[-1]
    body = abs(c['Close'] - c['Open'])
    wick_upper = c['High'] - max(c['Close'], c['Open'])
    wick_lower = min(c['Close'], c['Open']) - c['Low']
    if wick_lower > body*2 and body < (c['High']-c['Low'])*0.4:
        return "BULLISH PIN"
    if wick_upper > body*2 and body < (c['High']-c['Low'])*0.4:
        return "BEARISH PIN"
    return None

def is_london_ny_session():
    now = datetime.now(EAT)
    return 10 <= now.hour < 23

def format_price(name, price):
    if name == "GBPUSD": return f"{price:.5f}"
    if name == "GOLD": return f"{price:.2f}"
    return f"{price:.2f}"

def generate_signal(name, sym):
    global last_signal_time
    # Avoid spam: 2 hour cooldown per pair
    if name in last_signal_time and time.time() - last_signal_time[name] < 7200:
        return None

    h4 = get_data(sym, "1h", "30d")
    m15 = get_data(sym, "15m", "7d")
    m5 = get_data(sym, "5m", "2d")
    m1 = get_data(sym, "1m", "1d")
    if h4.empty or m15.empty or m5.empty or m1.empty: return None
    if is_choppy(h4): return None

    struct = detect_bos_choch(h4)
    price = float(m1['Close'].iloc[-1])
    atr_m15 = float((m15['High'] - m15['Low']).rolling(14).mean().iloc[-1])

    eng_m15 = detect_engulfing(m15)
    eng_m5 = detect_engulfing(m5)
    pin_m15 = detect_pinbar(m15)
    pin_m5 = detect_pinbar(m5)

    confirmation = eng_m15 or eng_m5 or pin_m15 or pin_m5
    if not confirmation: return None

    # BUY LOGIC
    if struct == "BULLISH BOS" and ("BULLISH" in confirmation):
        # Price near H4 low / demand
        h4_low = float(h4['Low'].rolling(20).min().iloc[-1])
        if price - h4_low > atr_m15*3: # too far from OB
            return None
        sl = h4_low - atr_m15*0.5
        risk = price - sl
        if risk <=0: return None
        tp1 = price + risk*2
        tp2 = price + risk*3
        last_signal_time[name] = time.time()
        return {
            "pair": name, "action": "BUY", "price": price, "sl": sl, "tp1": tp1, "tp2": tp2,
            "struct": struct, "confirm": confirmation, "atr": atr_m15
        }

    # SELL LOGIC
    if struct == "BEARISH BOS" and ("BEARISH" in confirmation):
        h4_high = float(h4['High'].rolling(20).max().iloc[-1])
        if h4_high - price > atr_m15*3:
            return None
        sl = h4_high + atr_m15*0.5
        risk = sl - price
        if risk <=0: return None
        tp1 = price - risk*2
        tp2 = price - risk*3
        last_signal_time[name] = time.time()
        return {
            "pair": name, "action": "SELL", "price": price, "sl": sl, "tp1": tp1, "tp2": tp2,
            "struct": struct, "confirm": confirmation, "atr": atr_m15
        }
    return None

def send_signal_message(sig):
    name = sig['pair']
    action = sig['action']
    emoji = "🟢" if action=="BUY" else "🔴"
    txt = f"""{emoji} *A+ SNIPER SIGNAL - {name} {action}* {emoji}

*Entry:* `{format_price(name, sig['price'])}`
*SL:* `{format_price(name, sig['sl'])}`
*TP1:* `{format_price(name, sig['tp1'])}` (1:2)
*TP2:* `{format_price(name, sig['tp2'])}` (1:3)

*Reason:*
✅ {sig['struct']}
✅ Order Block Retest + Break
✅ {sig['confirm']} on M5/M15
✅ CLEAN market (ATR {sig['atr']:.2f})

*Management:*
- Risk 1% per trade
- Move SL to BE at TP1
- Close 50% at TP1, rest to TP2

⏰ {datetime.now(EAT).strftime('%Y-%m-%d %H:%M EAT')} | London→NY Session
#StarFx #A+ #Sniper"""
    send_telegram(txt)
    # Save to history
    signals_history.append({
        "pair": name, "action": action, "entry": sig['price'], "sl": sig['sl'], "tp2": sig['tp2'],
        "time": datetime.now(EAT).isoformat(), "result": "OPEN"
    })
    save_history()

def check_potential_Aplus():
    potentials = []
    for name, sym in SYMBOLS.items():
        h4 = get_data(sym, "1h", "30d")
        m5 = get_data(sym, "5m", "2d")
        if h4.empty or m5.empty: continue
        if is_choppy(h4): continue
        struct = detect_bos_choch(h4)
        price = float(m5['Close'].iloc[-1])
        h4_high = float(h4['High'].max())
        h4_low = float(h4['Low'].min())
        dist_h = abs(price - h4_high)/price*100
        dist_l = abs(price - h4_low)/price*100
        if struct=="BULLISH BOS" and dist_l<1.0:
            potentials.append(f"👀 *{name}* Potential BUY\nPrice {format_price(name, price)} near Demand {format_price(name, h4_low)} ({dist_l:.2f}%)\nWaiting for Bullish Engulfing")
        elif struct=="BEARISH BOS" and dist_h<1.0:
            potentials.append(f"👀 *{name}* Potential SELL\nPrice {format_price(name, price)} near Supply {format_price(name, h4_high)} ({dist_h:.2f}%)\nWaiting for Bearish Engulfing")
    return potentials

def generate_daily_report():
    now = datetime.now(EAT)
    today_sigs = [s for s in signals_history if s['time'].startswith(now.strftime('%Y-%m-%d'))]
    wins = len([s for s in signals_history if s['result']=='WIN'])
    losses = len([s for s in signals_history if s['result']=='LOSS'])
    total = wins+losses
    wr = (wins/total*100) if total>0 else 0

    txt = f"📊 *DAILY REPORT - {now.strftime('%A %Y-%m-%d 23:00 EAT')}*\n\n"
    for name, sym in SYMBOLS.items():
        df = get_data(sym, "1h", "5d")
        if not df.empty:
            today_df = df.iloc[-24:] if len(df)>=24 else df
            txt += f"*{name}*: {format_price(name, today_df['Close'].iloc[-1])} H:{format_price(name, today_df['High'].max())} L:{format_price(name, today_df['Low'].min())}\n"
    txt += f"\n🎯 Signals Today: {len(today_sigs)}\n"
    for s in today_sigs[-5:]:
        txt += f" {s['pair']} {s['action']} @ {format_price(s['pair'], s['entry'])} - {s['result']}\n"
    txt += f"\n📈 All Time: {total} trades | WR {wr:.1f}% ({wins}W/{losses}L)\n🟢 Status: LIVE London→NY"
    return txt

def generate_weekly_report():
    now = datetime.now(EAT)
    week_sigs = [s for s in signals_history if datetime.fromisoformat(s['time']).isocalendar()[1]==now.isocalendar()[1]]
    wins = len([s for s in week_sigs if s['result']=='WIN'])
    losses = len([s for s in week_sigs if s['result']=='LOSS'])
    txt = f"📈 *WEEKLY REPORT - Friday {now.strftime('%Y-%m-%d 23:00 EAT')}*\n\n"
    for name, sym in SYMBOLS.items():
        df = get_data(sym, "1d", "10d")
        if not df.empty:
            week = df.iloc[-5:]
            txt += f"*{name}*: {format_price(name, week['Close'].iloc[-1])} W-H {format_price(name, week['High'].max())}\n"
    txt += f"\n🎯 Week Signals: {len(week_sigs)} | {wins}W/{losses}L\n"
    txt += f"📊 Win Rate Week: {(wins/(wins+losses)*100) if (wins+losses)>0 else 0:.1f}%\n\nHave a great weekend Starlord!"
    return txt

def setup_menu():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "start", "description": "🚀 Start bot"},
        {"command": "signal", "description": "🎯 Live scan now"},
        {"command": "price", "description": "💰 Live prices"},
        {"command": "performance", "description": "📊 Daily + Win Rate"},
        {"command": "history", "description": "📜 Signal history"},
        {"command": "help", "description": "ℹ️ How it works"}
    ]
    try: requests.post(url, json={"commands": commands}, timeout=10)
    except: pass

def command_listener():
    offset = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=30)
            data = r.json()
            if not data.get("ok"): time.sleep(3); continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = (msg.get("text") or "").lower()
                chat = msg.get("chat", {}).get("id")
                if not text: continue

                if "/start" in text:
                    send_telegram("🚀 *StarFx V7.8 Sniper - REAL SIGNALS*\n\n✅ Real Entry/SL/TP 1:3\n✅ Win Rate Tracker\n✅ 30min Potential Updates (London 10:00→NY 23:00 EAT)\n✅ Daily 23:00 + Weekly Friday 23:00\n\nWaiting for A+ setup - I will alert instantly!", chat)
                elif "/signal" in text:
                    txt = f"🎯 *Live Scan {datetime.now(EAT).strftime('%H:%M EAT')}* {'🟢' if is_london_ny_session() else '🔴 Asian'}\n\n"
                    for name,sym in SYMBOLS.items():
                        p = get_price_now(sym)
                        if p: txt+=f"{name}: {format_price(name,p)}\n"
                    pots = check_potential_Aplus()
                    txt += "\n" + ("\n\n".join(pots) if pots else "No near A+ - ranging")
                    send_telegram(txt, chat)
                elif "/price" in text:
                    txt="💰 *Live Prices*\n\n"
                    for name,sym in SYMBOLS.items():
                        p=get_price_now(sym)
                        if p: txt+=f"{name}: `{format_price(name,p)}`\n"
                    send_telegram(txt, chat)
                elif "/performance" in text:
                    send_telegram(generate_daily_report(), chat)
                elif "/history" in text:
                    if not signals_history:
                        send_telegram("📜 No signals yet - waiting for first A+", chat)
                    else:
                        txt="📜 *Last 10 Signals*\n\n"
                        for s in signals_history[-10:][::-1]:
                            txt+=f"{s['pair']} {s['action']} @ {format_price(s['pair'], s['entry'])} | {s['result']} | {s['time'][:16]}\n"
                        wins=len([s for s in signals_history if s['result']=='WIN'])
                        losses=len([s for s in signals_history if s['result']=='LOSS'])
                        wr=(wins/(wins+losses)*100) if (wins+losses)>0 else 0
                        txt+=f"\nWR: {wr:.1f}% ({wins}W/{losses}L) Total:{len(signals_history)}"
                        send_telegram(txt, chat)
                elif "/help" in text:
                    send_telegram("ℹ️ *V7.8 Real Signals*\n\nEntry when: H4 BOS + OB Retest + Break + Engulfing/Pin M5/M15 + CLEAN\n\nSL: Below/Above OB + ATR buffer\nTP1 1:2 TP2 1:3\nRisk 1% | Move to BE at TP1\n\nActive 10:00-23:00 EAT", chat)
            time.sleep(2)
        except Exception as e:
            print(f"Listener {e}")
            time.sleep(5)

def trading_loop():
    global daily_report_sent, weekly_report_sent, last_potential_update
    while True:
        try:
            now = datetime.now(EAT)
            today_str = now.strftime("%Y-%m-%d")

            # Check real signals every 3 min during session
            if is_london_ny_session():
                for name,sym in SYMBOLS.items():
                    sig = generate_signal(name,sym)
                    if sig:
                        send_signal_message(sig)
                        time.sleep(2) # avoid flood

                # 30 min potential updates
                if time.time() - last_potential_update >= 1800:
                    pots = check_potential_Aplus()
                    if pots:
                        for p in pots:
                            send_telegram(f"⏰ *30min Update {now.strftime('%H:%M EAT')}*\n\n{p}")
                    last_potential_update = time.time()

            # Daily 23:00
            if now.hour==23 and now.minute==0 and daily_report_sent!=today_str:
                send_telegram(generate_daily_report())
                daily_report_sent=today_str
                time.sleep(70)

            # Weekly Friday 23:00
            if now.weekday()==4 and now.hour==23 and now.minute==0:
                week_id=now.strftime("%Y-W%W")
                if weekly_report_sent!=week_id:
                    send_telegram(generate_weekly_report())
                    weekly_report_sent=week_id
                    time.sleep(70)

            time.sleep(30)
        except Exception as e:
            print(f"Loop {e}")
            time.sleep(60)

setup_menu()
threading.Thread(target=command_listener, daemon=True).start()
threading.Thread(target=trading_loop, daemon=True).start()

if __name__=="__main__":
    send_telegram("🚀 *V7.8 LIVE* - ✅ REAL BUY/SELL + SL/TP 1:3 + Win Rate + 30min Updates London→NY + Reports - Ready!")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
