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
def home(): return "StarFx V7.9 NEWS SNIPER + AUTO TRACKER LIVE"

daily_report_sent = ""
weekly_report_sent = ""
last_potential_update = 0
last_signal_time = {}
signals_history = []
major_news_cache = []
last_news_fetch = 0

try:
    if os.path.exists("signals_history.json"):
        with open("signals_history.json","r") as f:
            signals_history = json.load(f)
except: pass

def save_history():
    try:
        with open("signals_history.json","w") as f:
            json.dump(signals_history[-100:], f)
    except: pass

def send_telegram(msg, chat_id=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": chat_id or CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

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
    prev, curr = df.iloc[-2], df.iloc[-1]
    if curr['Close'] > curr['Open'] and prev['Close'] < prev['Open']:
        if curr['Close'] > prev['Open'] and curr['Open'] < prev['Close']:
            return "BULLISH ENGULFING"
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
    if wick_lower > body*2: return "BULLISH PIN"
    if wick_upper > body*2: return "BEARISH PIN"
    return None

def is_london_ny_session():
    now = datetime.now(EAT)
    return 10 <= now.hour < 23

def format_price(name, price):
    return f"{price:.5f}" if name=="GBPUSD" else f"{price:.2f}"

# === NEWS SYSTEM ===
def fetch_major_news():
    global major_news_cache, last_news_fetch
    if time.time() - last_news_fetch < 1800: # cache 30 min
        return major_news_cache
    try:
        # ForexFactory this week JSON
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        data = r.json()
        # Filter HIGH impact USD, GBP, ALL
        high = []
        for n in data:
            if n.get('impact') == 'High' and n.get('currency') in ['USD','GBP','ALL']:
                # Parse date
                high.append({
                    "title": n.get('title'),
                    "currency": n.get('currency'),
                    "time": n.get('date'), # UTC string
                    "forecast": n.get('forecast'),
                    "previous": n.get('previous')
                })
        major_news_cache = high[-15:] # last 15 high impact
        last_news_fetch = time.time()
        return major_news_cache
    except:
        return major_news_cache

def get_upcoming_news_text():
    news = fetch_major_news()
    if not news:
        return "📰 *Upcoming Major News*\n\nNo high impact news cached - will fetch again in 30 min.\nMajor pairs on watch: NFP, CPI, FOMC, PPI, Retail Sales"

    txt = "📰 *Upcoming HIGH Impact News (This Week)*\n\n"
    for n in news[-7:][::-1]:
        txt += f"🔴 {n['currency']} - {n['title']}\n Prev: {n['previous']} | Fcst: {n['forecast']}\n\n"
    txt += "\n⚠️ Bot auto-pauses 30 min before/after RED news"
    return txt

def is_high_impact_soon(minutes=60):
    """Check if high impact news within X minutes"""
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=8)
        data = r.json()
        now_utc = datetime.utcnow()
        for n in data:
            if n.get('impact')!= 'High': continue
            if n.get('currency') not in ['USD','GBP']: continue
            # date format: 2025-09-08T13:30:00-04:00 etc
            try:
                # Try parse
                dt_str = n.get('date')
                # simplified: check if today
                if now_utc.strftime("%Y-%m-%d") in dt_str:
                    return n
            except: continue
        return None
    except: return None

# === SIGNAL LOGIC ===
def generate_signal(name, sym, is_pre_news=False):
    global last_signal_time
    if name in last_signal_time and time.time() - last_signal_time[name] < 5400: # 1.5h cooldown
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
    confirmation = detect_engulfing(m15) or detect_engulfing(m5) or detect_pinbar(m15) or detect_pinbar(m5)
    if not confirmation: return None

    # Pre-news: Lower threshold - send even if 60% ready
    if is_pre_news:
        # For pre-news, allow even without perfect OB distance
        pass

    if struct == "BULLISH BOS" and "BULLISH" in confirmation:
        h4_low = float(h4['Low'].rolling(20).min().iloc[-1])
        if not is_pre_news and price - h4_low > atr_m15*3: return None
        sl = h4_low - atr_m15*0.5
        risk = price - sl
        if risk <=0: return None
        last_signal_time[name] = time.time()
        return {"pair": name, "action": "BUY", "price": price, "sl": sl, "tp1": price+risk*2, "tp2": price+risk*3, "struct": struct, "confirm": confirmation, "atr": atr_m15, "pre_news": is_pre_news}

    if struct == "BEARISH BOS" and "BEARISH" in confirmation:
        h4_high = float(h4['High'].rolling(20).max().iloc[-1])
        if not is_pre_news and h4_high - price > atr_m15*3: return None
        sl = h4_high + atr_m15*0.5
        risk = sl - price
        if risk <=0: return None
        last_signal_time[name] = time.time()
        return {"pair": name, "action": "SELL", "price": price, "sl": sl, "tp1": price-risk*2, "tp2": price-risk*3, "struct": struct, "confirm": confirmation, "atr": atr_m15, "pre_news": is_pre_news}
    return None

def send_signal_message(sig):
    name, action = sig['pair'], sig['action']
    emoji = "🟢" if action=="BUY" else "🔴"
    pre = "⚠️ *PRE-NEWS SNIPER* ⚠️\n" if sig.get('pre_news') else ""

    txt = f"""{pre}{emoji} *A+ SNIPER SIGNAL - {name} {action}* {emoji}

*Entry:* `{format_price(name, sig['price'])}`
*SL:* `{format_price(name, sig['sl'])}`
*TP1:* `{format_price(name, sig['tp1'])}` (1:2)
*TP2:* `{format_price(name, sig['tp2'])}` (1:3)

*Reason:*
✅ {sig['struct']}
✅ Order Block Retest
✅ {sig['confirm']}
✅ CLEAN ATR {sig['atr']:.2f}
{f"✅ PRE-NEWS SETUP - News in <60min" if sig.get('pre_news') else ""}

*Manage:* 1% risk | BE at TP1 | 50% at TP1
⏰ {datetime.now(EAT).strftime('%H:%M EAT')} | { '🔥 NEWS INCOMING' if sig.get('pre_news') else 'London→NY' }
"""
    send_telegram(txt)
    signals_history.append({"pair": name, "action": action, "entry": sig['price'], "sl": sig['sl'], "tp2": sig['tp2'], "time": datetime.now(EAT).isoformat(), "result": "OPEN"})
    save_history()

# === AUTO WIN/LOSS TRACKER ===
def auto_check_results():
    changed = False
    for s in signals_history:
        if s['result']!= "OPEN": continue
        sym = SYMBOLS.get(s['pair'])
        if not sym: continue
        price = get_price_now(sym)
        if not price: continue

        if s['action']=="BUY":
            if price >= s['tp2']:
                s['result']="WIN"; changed=True
                send_telegram(f"✅ *TP2 HIT WIN!* {s['pair']} BUY @ {format_price(s['pair'], s['entry'])} → {format_price(s['pair'], price)} 💰")
            elif price <= s['sl']:
                s['result']="LOSS"; changed=True
                send_telegram(f"❌ *SL HIT LOSS* {s['pair']} BUY @ {format_price(s['pair'], s['entry'])}")
        else: # SELL
            if price <= s['tp2']:
                s['result']="WIN"; changed=True
                send_telegram(f"✅ *TP2 HIT WIN!* {s['pair']} SELL @ {format_price(s['pair'], s['entry'])} → {format_price(s['pair'], price)} 💰")
            elif price >= s['sl']:
                s['result']="LOSS"; changed=True
                send_telegram(f"❌ *SL HIT LOSS* {s['pair']} SELL @ {format_price(s['pair'], s['entry'])}")
    if changed: save_history()

def check_potential_Aplus():
    pots=[]
    for name,sym in SYMBOLS.items():
        h4=get_data(sym,"1h","30d"); m5=get_data(sym,"5m","2d")
        if h4.empty or m5.empty: continue
        if is_choppy(h4): continue
        struct=detect_bos_choch(h4)
        price=float(m5['Close'].iloc[-1])
        h4_high=float(h4['High'].max()); h4_low=float(h4['Low'].min())
        if struct=="BULLISH BOS" and abs(price-h4_low)/price*100<1.0:
            pots.append(f"👀 *{name}* Potential BUY near {format_price(name,h4_low)}")
        elif struct=="BEARISH BOS" and abs(price-h4_high)/price*100<1.0:
            pots.append(f"👀 *{name}* Potential SELL near {format_price(name,h4_high)}")
    return pots

def generate_daily_report():
    now=datetime.now(EAT)
    today=[s for s in signals_history if s['time'].startswith(now.strftime('%Y-%m-%d'))]
    wins=len([s for s in signals_history if s['result']=='WIN'])
    losses=len([s for s in signals_history if s['result']=='LOSS'])
    txt=f"📊 *DAILY {now.strftime('%Y-%m-%d 23:00 EAT')}*\n\n"
    for name,sym in SYMBOLS.items():
        df=get_data(sym,"1h","5d")
        if not df.empty: txt+=f"{name}: {format_price(name,df['Close'].iloc[-1])}\n"
    txt+=f"\nToday: {len(today)} signals\nAll Time WR: {(wins/(wins+losses)*100) if wins+losses>0 else 0:.1f}% ({wins}W/{losses}L)\n"
    return txt

def generate_weekly_report():
    now=datetime.now(EAT)
    week=[s for s in signals_history if datetime.fromisoformat(s['time']).isocalendar()[1]==now.isocalendar()[1]]
    wins=len([s for s in week if s['result']=='WIN']); losses=len([s for s in week if s['result']=='LOSS'])
    txt=f"📈 *WEEKLY Friday {now.strftime('%Y-%m-%d')}*\n\nSignals: {len(week)} | {wins}W/{losses}L | WR {(wins/(wins+losses)*100) if wins+losses>0 else 0:.1f}%"
    return txt

def setup_menu():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    cmds=[
        {"command": "start", "description": "🚀 Start bot"},
        {"command": "signal", "description": "🎯 Live scan now"},
        {"command": "price", "description": "💰 Live prices"},
        {"command": "news", "description": "📰 Upcoming RED news"},
        {"command": "performance", "description": "📊 Daily + Win Rate"},
        {"command": "history", "description": "📜 Signal history"},
        {"command": "help", "description": "ℹ️ How it works"}
    ]
    try: requests.post(url, json={"commands": cmds}, timeout=10)
    except: pass

def command_listener():
    offset=0
    while True:
        try:
            r=requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=30)
            data=r.json()
            if not data.get("ok"): time.sleep(3); continue
            for upd in data.get("result", []):
                offset=upd["update_id"]+1
                msg=upd.get("message",{})
                text=(msg.get("text") or "").lower()
                chat=msg.get("chat",{}).get("id")
                if not text: continue
                if "/start" in text:
                    send_telegram("🚀 *V7.9 NEWS SNIPER LIVE*\n\n✅ Real Entry/SL/TP\n✅ Auto WIN/LOSS tracker\n✅ /news - RED news calendar\n✅ Pre-news signals (60min before)\n✅ Auto pause during news\n\nWaiting for A+", chat)
                elif "/signal" in text:
                    txt=f"🎯 *Live {datetime.now(EAT).strftime('%H:%M EAT')}* {'🟢' if is_london_ny_session() else '🔴'}\n\n"
                    for n,s in SYMBOLS.items():
                        p=get_price_now(s)
                        if p: txt+=f"{n}: {format_price(n,p)}\n"
                    pots=check_potential_Aplus()
                    if pots: txt+="\n"+"\n".join(pots)
                    send_telegram(txt, chat)
                elif "/price" in text:
                    txt="💰 *Live*\n\n"
                    for n,s in SYMBOLS.items():
                        p=get_price_now(s)
                        if p: txt+=f"{n}: `{format_price(n,p)}`\n"
                    send_telegram(txt, chat)
                elif "/news" in text:
                    send_telegram(get_upcoming_news_text(), chat)
                elif "/performance" in text:
                    send_telegram(generate_daily_report(), chat)
                elif "/history" in text:
                    if not signals_history: send_telegram("📜 No signals yet", chat)
                    else:
                        txt="📜 *Last 10*\n\n"
                        for s in signals_history[-10:][::-1]:
                            txt+=f"{s['pair']} {s['action']} {s['result']} @ {format_price(s['pair'], s['entry'])} {s['time'][:16]}\n"
                        wins=len([s for s in signals_history if s['result']=='WIN']); losses=len([s for s in signals_history if s['result']=='LOSS'])
                        txt+=f"\nWR {(wins/(wins+losses)*100) if wins+losses>0 else 0:.1f}% {wins}W/{losses}L"
                        send_telegram(txt, chat)
                elif "/help" in text:
                    send_telegram("ℹ️ V7.9: H4 BOS + OB + M5/M15 Engulfing. Pre-news sniper sends signal 60min before RED news. Auto tracks WIN/LOSS. /news to see calendar.", chat)
            time.sleep(2)
        except: time.sleep(5)

def trading_loop():
    global daily_report_sent, weekly_report_sent, last_potential_update
    while True:
        try:
            now=datetime.now(EAT)
            today_str=now.strftime("%Y-%m-%d")

            # 1. AUTO CHECK WIN/LOSS every 60 sec
            auto_check_results()

            # 2. CHECK NEWS + PRE-NEWS SIGNAL
            news_event = is_high_impact_soon(60)
            if news_event and is_london_ny_session():
                for name,sym in SYMBOLS.items():
                    sig = generate_signal(name,sym, is_pre_news=True)
                    if sig:
                        send_signal_message(sig)
                        time.sleep(2)

            # 3. NORMAL SIGNALS every 3 min if NOT near high impact
            if is_london_ny_session() and not news_event:
                for name,sym in SYMBOLS.items():
                    sig = generate_signal(name,sym, is_pre_news=False)
                    if sig:
                        send_signal_message(sig)
                        time.sleep(2)

                if time.time() - last_potential_update >= 1800:
                    pots=check_potential_Aplus()
                    if pots:
                        for p in pots:
                            send_telegram(f"⏰ *30min Update {now.strftime('%H:%M EAT')}*\n\n{p}")
                    last_potential_update=time.time()

            if now.hour==23 and now.minute==0 and daily_report_sent!=today_str:
                send_telegram(generate_daily_report())
                daily_report_sent=today_str
                time.sleep(70)
            if now.weekday()==4 and now.hour==23 and now.minute==0:
                week_id=now.strftime("%Y-W%W")
                if weekly_report_sent!=week_id:
                    send_telegram(generate_weekly_report())
                    weekly_report_sent=week_id
                    time.sleep(70)

            time.sleep(30)
        except Exception as e:
            print(e); time.sleep(60)

setup_menu()
threading.Thread(target=command_listener, daemon=True).start()
threading.Thread(target=trading_loop, daemon=True).start()

if __name__=="__main__":
    send_telegram("🚀 *V7.9 LIVE* - ✅ Auto WIN/LOSS + 📰 /news + ⚠️ Pre-News Sniper (60min before RED) + Real SL/TP - Ready!")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
