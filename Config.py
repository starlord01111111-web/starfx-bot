import os
import pytz
from dotenv import load_dotenv

load_dotenv()


class Config:

    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    CHAT_ID = os.getenv("CHAT_ID")

    TIMEZONE = pytz.timezone("Africa/Kampala")

    SYMBOLS = {
        "GOLD": "GC=F",
        "BTCUSD": "BTC-USD",
        "GBPUSD": "GBPUSD=X",
        "EURUSD": "EURUSD=X"
    }

    # Scanner

    SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 40))
    COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", 60))

    # Risk

    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", 0.01))
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", 0.05))
    MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", 10))
    MAX_SIMULTANEOUS = int(os.getenv("MAX_SIMULTANEOUS", 3))

    TRADE_EXPIRY_HOURS = int(os.getenv("TRADE_EXPIRY_HOURS", 8))

    # Minimum SL

    GOLD_MIN_SL = 30
    BTC_MIN_SL = 350
    FOREX_MIN_SL = 0.0008

    # Trading Sessions (EAT)

    SESSION_START = 10
    SESSION_END = 23

    DATABASE = "starfx.db"

    LOG_FILE = "starfx.log"

    CACHE_SECONDS = 30

    YAHOO_RETRIES = 4

    YAHOO_PAUSE_MINUTES = 10


config = Config()
