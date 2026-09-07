import time, requests, os
from datetime import datetime

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHANNEL_ID, "text": msg, "parse_mode": "Markdown"}
        r = requests.post(url, json=payload, timeout=10)
        print("TELEGRAM RESPONSE:", r.text)
    except Exception as e:
        print("Error:", e)

def get_gold_data():
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=10).json()
        return float(r['price'])
    except:
        return 4406.5

last_signal = 0
while True:
    try:
        price = get_gold_data()
        print(f"Gold: {price}")
        if time.time() - last_signal > 900:
            msg = f"🚀 *STARFX V5 LIVE* \nGold: {price}\nTrend: BULLISH + BOS"
            send_telegram(msg)
            last_signal = time.time()
        time.sleep(60)
    except Exception as e:
        print(e)
        time.sleep(10)
