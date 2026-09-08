import os, time, threading, requests, yfinance as yf, pandas as pd
from flask import Flask
from datetime import datetime
import pytz

BOT_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHAT_ID = "-1004365660319"
EAT = pytz.timezone('Africa/Nairobi')

SYMBOLS = {"GOLD": "GC=F", "GBPUSD": "GBPUSD=X", "BTCUSD": "BTC-USD"}

app = Flask(__name__)
@app.route('/')
def home(): return "StarFx V7.9.2 FAST FIX LIVE"

last_signal_time = {}
signals_history = []
major_news_cache = []
last_news_fetch = 0

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
    if len(df) < 20: return "RANGING"
    highs = df['High'].rolling(10).max()
    lows = df['Low'].rolling(10).min()
    last = df['Close'].iloc[-1]
    if last > highs.iloc[-20]: return "BULLISH BOS"
    if last < lows.iloc[-20]: return "BEARISH BOS"
    return "RANGING"

def detect_engulfing(df):
    if len(df) < 3: return None
    prev, curr = df.iloc[-2], df.iloc[-1]
    if curr['Close'] > curr['Open'] and prev['Close'] < prev['Open']:
        if curr['Close'] > prev['Open']: return "BULLISH ENGULFING"
    if curr['Close'] < curr['Open'] and prev['Close'] > prev['Open']:
        if curr['Close'] < prev['Open']: return "BEARISH ENGULFING"
    return None

def detect_pinbar(df):
    if len(df) < 2: return None
    c = df.iloc[-1]
    body = abs(c['Close'] - c['Open'])
    if body==0: return None
    wick_lower = min(c['Close'], c['Open']) - c['Low']
    wick_upper = c['High'] - max(c['Close'], c['Open'])
    if wick_lower > body*1.5: return "BULLISH PIN"
    if wick_upper > body*1.5: return "BEARISH PIN"
    return None

def is_london_ny_session():
    now = datetime.now(EAT)
    return 10 <= now.hour < 23

def format_price(name, price):
    return f"{price:.5f}" if name=="GBPUSD" else f"{price:.2f}"

def fetch_major_news():
    global major_news_cache, last_news_fetch
    if time.time() - last_news_fetch < 1800 and major_news_cache:
        return major_news_cache
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        data = r.json()
        high = []
        for n in data:
            if n.get('impact') == 'High' and n.get('currency') in ['USD','GBP']:
                high.append(n)
        major_news_cache = high[-10:]
        last_news_fetch = time.time()
        return major_news_cache
    except:
        return major_news_cache

def get_upcoming_news_text():
    news = fetch_major_news()
    if not news: return "📰 No RED news this week - NFP/CPI done"
    txt = "📰 *RED NEWS THIS WEEK*\n\n"
    for n in news[-7:][::-1]:
        txt += f"🔴 {n.get('currency')} - {n.get('title')}\n"
    return txt

def generate_signal(name, sym, is_pre_news=False):
    global last_signal_time
    if name in last_signal_time and time.time() - last_signal_time[name] < 1800:
        return None
    h4 = get_data(sym, "1h", "15d")
    m15 = get_data(sym, "15m", "3d")
    m5 = get_data(sym, "5m", "1d")
    m1 = get_data(sym, "1m", "1d")
    if h4.empty or m15.empty or m5.empty: return None

    struct_h4 = detect_bos_choch(h4)
    struct_m15 = detect_bos_choch(m15)
    price = float(m1['Close'].iloc[-1]) if not m1.empty else float(m5['Close'].iloc[-1])
    atr = float((m15['High'] - m15['Low']).rolling(14).mean().iloc[-1])

    conf = detect_engulfing(m15) or detect_engulfing(m5) or detect_pinbar(m15) or detect_pinbar(m5)
    if not conf:
        last_m5 = m5.iloc[-1]
        body = abs(last_m5['Close'] - last_m5['Open'])
        rng = last_m5['High'] - last_m5['Low']
        if rng>0 and body/rng>0.6:
            conf = "BULLISH MOMENTUM" if last_m5['Close']>last_m5['Open'] else "BEARISH MOMENTUM"
    if not conf: return None

    if "BULLISH" in conf or "BULLISH" in struct_h4 or "BULLISH" in struct_m15:
        sl = price - atr*1.5
        risk = price - sl
        if risk<=0: return None
        last_signal_time[name]=time.time()
        grade = "A+ ⭐" if "BOS" in struct_h4 else "A"
        return {"pair":name,"action":"BUY","price":price,"sl":sl,"tp1":price+risk*2,"tp2":price+risk*3,"struct":struct_h4,"confirm":conf,"atr":atr,"pre_news":is_pre_news,"grade":grade}
    if "BEARISH" in conf or "BEARISH" in struct_h4 or "BEARISH" in struct_m15:
        sl = price + atr*1.5
        risk = sl - price
        if risk<=0: return None
        last_signal_time[name]=time.time()
        grade = "A+ ⭐" if "BOS" in struct_h4 else "A"
        return {"pair":name,"action":"SELL","price":price,"sl":sl,"tp1":price-risk*2,"tp2":price-risk*3,"struct":struct_h4,"confirm":conf,"atr":atr,"pre_news":is_pre_news,"grade":grade}
    return None

def send_signal_message(sig):
    emoji = "🟢" if sig['action']=="BUY" else "🔴"
    pre = "⚠️ *PRE-NEWS* ⚠️\n" if sig.get('pre_news') else ""
    txt = f"{pre}{emoji} *{sig['grade']} SNIPER - {sig['pair']} {sig['action']}* {emoji}\n\n*Entry:* `{format_price(sig['pair'], sig['price'])}`\n*SL:* `{format_price(sig['pair'], sig['sl'])}`\n*TP1:* `{format_price(sig['pair'], sig['tp1'])}` (1:2)\n*TP2:* `{format_price(sig['pair'], sig['tp2'])}` (1:3)\n\n✅ {sig['struct']}\n✅ {sig['confirm']}\n\n⏰ {datetime.now(EAT).strftime('%H:%M EAT')} FAST"
    send_telegram(txt)
    signals_history.append({"pair":sig['pair'],"action":sig['action'],"entry":sig['price'],"sl":sig['sl'],"tp2":sig['tp2'],"time":datetime.now(EAT).isoformat(),"result":"OPEN"})

def auto_check_results():
    for s in signals_history:
        if s['result']!="OPEN": continue
        price=get_price_now(SYMBOLS.get(s['pair']))
        if not price: continue
        if s['action']=="BUY":
            if price>=s['tp2']: s['result']="WIN"; send_telegram(f"✅ *TP2 WIN* {s['pair']}")
            elif price<=s['sl']: s['result']="LOSS"; send_telegram(f"❌ *SL LOSS* {s['pair']}")
        else:
            if price<=s['tp2']: s['result']="WIN"; send_telegram(f"✅ *TP2 WIN* {s['pair']}")
            elif price>=s['sl']: s['result']="LOSS"; send_telegram(f"❌ *SL LOSS* {s['pair']}")

def setup_menu():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    cmds=[{"command":"start","description":"🚀 Start"},{"command":"signal","description":"🎯 Scan"},{"command":"price","description":"💰 Price"},{"command":"news","description":"📰 News"},{"command":"performance","description":"📊 WR"}]
    try: requests.post(url, json={"commands":cmds}, timeout=10)
    except: pass

def command_listener():
    offset=0
    while True:
        try:
            r=requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params={"offset":offset,"timeout":25}, timeout=30).json()
            if not r.get("ok"): time.sleep(3); continue
            for upd in r.get("result",[]):
                offset=upd["update_id"]+1
                text=(upd.get("message",{}).get("text") or "").lower()
                chat=upd.get("message",{}).get("chat",{}).get("id")
                if not text: continue
                if "/start" in text: send_telegram("🚀 *V7.9.2 FAST FIX LIVE* - Deploy SUCCESS! FAST 3-5/day", chat)
                elif "/signal" in text:
                    txt=f"🎯 {datetime.now(EAT).strftime('%H:%M')} {'🟢' if is_london_ny_session() else '🔴'}\n"
                    for n,s in SYMBOLS.items():
                        p=get_price_now(s)
                        if p: txt+=f"{n}: {format_price(n,p)}\n"
                    send_telegram(txt, chat)
                elif "/price" in text:
                    txt="💰 Live\n"
                    for n,s in SYMBOLS.items():
                        p=get_price_now(s)
                        if p: txt+=f"{n}: {format_price(n,p)}\n"
                    send_telegram(txt, chat)
                elif "/news" in text: send_telegram(get_upcoming_news_text(), chat)
                elif "/performance" in text:
                    w=len([s for s in signals_history if s['result']=='WIN']); l=len([s for s in signals_history if s['result']=='LOSS'])
                    send_telegram(f"📊 WR {(w/(w+l)*100) if w+l>0 else 0:.1f}% {w}W/{l}L", chat)
        except: time.sleep(5)

def trading_loop():
    while True:
        try:
            auto_check_results()
            if is_london_ny_session():
                for name,sym in SYMBOLS.items():
                    sig=generate_signal(name,sym)
                    if sig: send_signal_message(sig); time.sleep(2)
            time.sleep(40)
        except Exception as e:
            print(e); time.sleep(60)

setup_menu()
threading.Thread(target=command_listener, daemon=True).start()
threading.Thread(target=trading_loop, daemon=True).start()

if __name__=="__main__":
    send_telegram("🚀 *V7.9.2 FAST FIX LIVE* - Deploy FIXED! Ready for FAST signals!")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
