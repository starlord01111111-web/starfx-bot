import requests, time, threading, os
from flask import Flask

app = Flask('')
@app.route('/')
def home(): return "✅ STARFX V4 LIVE - Gold SMC Scanning"
def run_flask(): app.run(host='0.0.0.0', port=10000)
threading.Thread(target=run_flask, daemon=True).start()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = "-1004365660319"

def send(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def get_klines(interval, limit=120):
    try:
        url = f"https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT&interval={interval}&limit={limit}"
        r = requests.get(url, timeout=15).json()
        return [{"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),"c":float(x[4])} for x in r]
    except: return []

def ema(data, p):
    if len(data)<p: return None
    k=2/(p+1); ev=sum(data[:p])/p
    for x in data[p:]: ev=x*k+ev*(1-k)
    return ev

def get_trend(h1,m30):
    ch1=[c["c"] for c in h1]; cm30=[c["c"] for c in m30]
    e50=ema(ch1,50); e100=ema(ch1,100); e50m=ema(cm30,50)
    if e50 and e100:
        if e50>e100: return "UPTREND"
        if e50<e100: return "DOWNTREND"
    if e50m:
        if cm30[-1]>e50m: return "UPTREND"
        if cm30[-1]<e50m: return "DOWNTREND"
    return "RANGING"

def is_hammer(c):
    b=abs(c["c"]-c["o"])
    if b==0: return False
    up=c["h"]-max(c["o"],c["c"]); low=min(c["o"],c["c"])-c["l"]
    if low>b*2 and up<b*0.6: return "bullish"
    if up>b*2 and low<b*0.6: return "bearish"
    return False

def is_doji(c): return (c["h"]-c["l"])>0 and abs(c["c"]-c["o"])/(c["h"]-c["l"])<0.25

send("✅ *STARFX V4 DEPLOYED ON RENDER*\n24/7 scanning Gold SMC POIs - will send when found")

while True:
    try:
        h1=get_klines("1h",100); m30=get_klines("30m",80); m5=get_klines("5m",60); m1=get_klines("1m",40)
        if not h1 or not m5: time.sleep(60); continue
        price=m1[-1]["c"]; trend=get_trend(h1,m30)
        if trend=="RANGING":
            print(f"Ranging - Price {price}")
            time.sleep(60); continue

        e5=is_hammer(m5[-1]); e1=is_hammer(m1[-1]); doji=is_doji(m5[-1])
        if e5 or e1 or doji:
            direction = "BUY" if (e5=="bullish" or e1=="bullish") else "SELL" if (e5=="bearish" or e1=="bearish") else "BUY" if trend=="UPTREND" else "SELL"
            if (trend=="UPTREND" and direction=="BUY") or (trend=="DOWNTREND" and direction=="SELL"):
                send(f"{'🟢' if direction=='BUY' else '🔴'} *{direction} GOLD | SMC Entry*\nTrend: {trend}\nEntry: {price:.2f}\nM5 Signal: {e5 or 'doji'}\nTime: {time.strftime('%H:%M')} EAT")
                time.sleep(300)
        time.sleep(60)
    except Exception as e:
        print(e); time.sleep(60)
