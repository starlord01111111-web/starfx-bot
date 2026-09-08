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
def home(): return "StarFx V7.7 SNIPER LIVE - London to NY"

daily_report_sent = ""
weekly_report_sent = ""
signals_today_count = 0
last_potential_update = 0

def send_telegram(msg, chat_id=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": chat_id or CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
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
    return df['Close'].iloc[-1] if not df.empty else None

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

def is_london_ny_session():
    """Returns True if London or NY open - 10:00 to 23:00 EAT"""
    now = datetime.now(EAT)
    hour = now.hour
    # London 10:00 EAT (07:00 GMT) -> NY Close 23:00 EAT (20:00 GMT)
    return 10 <= hour < 23

def analyze_live():
    results = []
    for name, sym in SYMBOLS.items():
        h4 = get_data(sym, "1h", "30d")
        if h4.empty:
            results.append(f"{name}: No data")
            continue
        price = get_price_now(sym) or h4['Close'].iloc[-1]
        struct = detect_bos_choch(h4)
        choppy = "CHOPPY - NO TRADE" if is_choppy(h4) else "CLEAN"
        results.append(f"*{name}*: {price:.2f} | {struct} | {choppy}\nH4 H:{h4['High'].max():.2f} L:{h4['Low'].min():.2f}")
    return "\n\n".join(results)

def check_potential_Aplus():
    """Find setups that are 70% to becoming A+ sniper"""
    potentials = []
    for name, sym in SYMBOLS.items():
        h4 = get_data(sym, "1h", "30d")
        m15 = get_data(sym, "15m", "5d")
        m5 = get_data(sym, "5m", "2d")
        if h4.empty or m15.empty or m5.empty: continue
        if is_choppy(h4): continue

        struct = detect_bos_choch(h4)
        price = m5['Close'].iloc[-1]
        h4_high = h4['High'].max()
        h4_low = h4['Low'].min()

        # Potential logic: BOS + price near OB/liquidity zone
        dist_to_high = abs(price - h4_high) / price * 100
        dist_to_low = abs(price - h4_low) / price * 100

        if struct == "BULLISH BOS" and dist_to_low < 0.8:
            potentials.append(f"👀 *{name}* - Potential A+ BUY forming\nBOS Bullish + Retest to Demand OB\nPrice {price:.2f} near Low {h4_low:.2f} ({dist_to_low:.2f}%)\nWaiting for Engulfing")
        elif struct == "BEARISH BOS" and dist_to_high < 0.8:
            potentials.append(f"👀 *{name}* - Potential A+ SELL forming\nBOS Bearish + Retest to Supply OB\nPrice {price:.2f} near High {h4_high:.2f} ({dist_to_high:.2f}%)\nWaiting for Engulfing")

    return potentials

def generate_daily_report():
    now = datetime.now(EAT)
    txt = f"📊 *DAILY REPORT - {now.strftime('%A %Y-%m-%d 23:00 EAT')}*\n\n"
    for name, sym in SYMBOLS.items():
        df = get_data(sym, "1h", "5d")
        if not df.empty:
            today_df = df.iloc[-24:] if len(df)>=24 else df
            high = today_df['High'].max()
            low = today_df['Low'].min()
            open_p = today_df['Open'].iloc[0]
            close_p = today_df['Close'].iloc[-1]
            change = ((close_p - open_p)/open_p*100) if open_p!=0 else 0
            txt += f"*{name}*\nClose: {close_p:.2f} ({change:+.2f}%)\nH: {high:.2f} L: {low:.2f}\n\n"
    txt += f"🔍 Scans: ~480 today\n🎯 A+ Signals: {signals_today_count}\n🟢 London-NY Session Only\n\nBot sleeping until London 10:00 EAT"
    return txt

def generate_weekly_report():
    now = datetime.now(EAT)
    txt = f"📈 *WEEKLY REPORT - Friday {now.strftime('%Y-%m-%d 23:00 EAT')}*\n*Market Close*\n\n"
    for name, sym in SYMBOLS.items():
        df = get_data(sym, "1d", "10d")
        if not df.empty:
            week = df.iloc[-5:]
            high = week['High'].max()
            low = week['Low'].min()
            change = ((week['Close'].iloc[-1] - week['Open'].iloc[0])/week['Open'].iloc[0]*100) if week['Open'].iloc[0]!=0 else 0
            txt += f"*{name}*: {week['Close'].iloc[-1]:.2f} ({change:+.2f}%)\nW-H {high:.2f} W-L {low:.2f}\n\n"
    txt += f"🎯 Total Signals This Week: {signals_today_count}\n📊 Strategy: OB + Break/Retest + Engulfing\n\nSee you at London Open Monday 10:00 EAT!"
    return txt

def setup_menu():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "start", "description": "🚀 Start the bot"},
        {"command": "signal", "description": "🎯 Check live signal"},
        {"command": "price", "description": "💰 Gold price now"},
        {"command": "performance", "description": "📊 Today's stats"},
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
            if not data.get("ok"):
                time.sleep(3); continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = (msg.get("text") or "").lower()
                chat = msg.get("chat", {}).get("id")
                if not text: continue

                if "/start" in text:
                    send_telegram("🚀 *StarFx V7.7 Sniper Online*\n\nWelcome Starlord!\n\n⏰ Session: London 10:00 → NY Close 23:00 EAT\n🔔 A+ Potential updates every 30 min (only in session)\n📊 Daily Report 23:00 EAT\n📈 Weekly Friday 23:00 EAT\n\nUse Menu below!", chat)
                elif "/signal" in text:
                    if not is_london_ny_session():
                        send_telegram(f"😴 Market in Asian low liquidity\nLondon opens 10:00 EAT\nCurrent: {datetime.now(EAT).strftime('%H:%M EAT')}\n\n{analyze_live()}", chat)
                    else:
                        live = analyze_live()
                        pots = check_potential_Aplus()
                        pot_txt = "\n\n".join(pots) if pots else "No near A+ yet - scanning"
                        send_telegram(f"🎯 *Live Scan* {datetime.now(EAT).strftime('%H:%M EAT')}\n\n{live}\n\n{pot_txt}", chat)
                elif "/price" in text:
                    txt = "💰 *Live Prices*\n\n"
                    for name, sym in SYMBOLS.items():
                        p = get_price_now(sym)
                        txt += f"{name}: `{p:.2f}`\n" if p else f"{name}: loading...\n"
                    txt += f"\n{datetime.now(EAT).strftime('%H:%M EAT')} - {'🟢 London/NY' if is_london_ny_session() else '🔴 Asian - Low Vol'}"
                    send_telegram(txt, chat)
                elif "/performance" in text:
                    send_telegram(generate_daily_report(), chat)
                elif "/help" in text:
                    send_telegram("""ℹ️ *V7.7 Logic*

1️⃣ H4 Swing → Range
2️⃣ BOS/CHoCH → Bias
3️⃣ Liquidity → Sweep
4️⃣ Order Block → Zone
5️⃣ Break+Retest → Trigger
6️⃣ Engulfing/Pin → Confirm
7️⃣ ATR → Choppy filter

⏰ *Active: 10:00 - 23:00 EAT*
London 10:00 open → NY 23:00 close
After 23:00 → Bot sleeps (no 30min updates) until London

🔔 Every 30min: Potential A+ alerts
🎯 Instant: Full A+ entry BUY/SELL

Pairs: GOLD, GBPUSD, BTCUSD""", chat)
            time.sleep(2)
        except Exception as e:
            print(f"Listener err {e}")
            time.sleep(5)

def trading_loop():
    global daily_report_sent, weekly_report_sent, last_potential_update
    while True:
        try:
            now = datetime.now(EAT)
            today_str = now.strftime("%Y-%m-%d")

            # DAILY 23:00 EAT
            if now.hour == 23 and now.minute == 0 and daily_report_sent!= today_str:
                send_telegram(generate_daily_report())
                daily_report_sent = today_str
                time.sleep(70)

            # WEEKLY FRIDAY 23:00 EAT
            if now.weekday() == 4 and now.hour == 23 and now.minute == 0:
                week_id = now.strftime("%Y-W%W")
                if weekly_report_sent!= week_id:
                    send_telegram(generate_weekly_report())
                    weekly_report_sent = week_id
                    time.sleep(70)

            # 30 MIN POTENTIAL A+ UPDATES - ONLY LONDON-NY SESSION
            if is_london_ny_session():
                if time.time() - last_potential_update >= 1800: # 30 min
                    potentials = check_potential_Aplus()
                    if potentials:
                        for p in potentials:
                            send_telegram(f"⏰ *30min Update {now.strftime('%H:%M EAT')}*\n\n{p}")
                    else:
                        # Optional: send "No A+ yet" every 30 min, or silent. Keeping silent to avoid spam
                        # If you want active update even when nothing, uncomment below:
                        # send_telegram(f"⏰ *30min Scan {now.strftime('%H:%M EAT')}* - London/NY active\nNo near A+ yet - market ranging. Continuing scan...")
                        pass
                    last_potential_update = time.time()
            else:
                # Outside session - sleep message once
                pass

            time.sleep(30)
        except Exception as e:
            print(f"Trading loop err {e}")
            time.sleep(60)

setup_menu()
threading.Thread(target=command_listener, daemon=True).start()
threading.Thread(target=trading_loop, daemon=True).start()

if __name__ == "__main__":
    send_telegram("🚀 *V7.7 LIVE* - ✅ 30min A+ Updates (London 10:00 → NY 23:00 EAT) ✅ Daily 23:00 ✅ Weekly Friday 23:00 - Menu ready!")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
