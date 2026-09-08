import os
import time
import threading
import requests
import yfinance as yf
from flask import Flask

# --- CONFIG ---
BOT_TOKEN = "8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
CHAT_ID = "-1004365660319"
SYMBOL = "GC=F"  # Gold Futures

app = Flask(__name__)

@app.route('/')
def home():
    return "StarFX V7.3 Gold Bot is Live! ✅"

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN or CHAT_ID in Render Env Vars!")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"})
        print(f"Telegram: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"Telegram Error: {e}")

def check_gold():
    while True:
        try:
            print("Checking XAUUSD...")
            data = yf.download(SYMBOL, period="1d", interval="5m", progress=False)
            if data.empty:
                time.sleep(60)
                continue
            
            price = float(data['Close'].iloc[-1])
            print(f"Gold Price: {price}")

            # SIMPLE V7.3 STRATEGY - Sends every hour for testing
            # You can add your real strategy here
            # Example: if price > 2650: send signal
            
            # For now, just log - uncomment next line to test Telegram
            # send_telegram(f"✅ V7.3 LIVE CHECK\nGold: ${price:.2f}\nTime: {time.ctime()}")

            time.sleep(300)  # Check every 5 minutes
        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(60)

# Start trading loop in background
threading.Thread(target=check_gold, daemon=True).start()

if __name__ == "__main__":
    send_telegram("🚀 *StarFX V7.3 Started Successfully!* \nBot is now monitoring Gold.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
