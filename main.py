import logging
import signal
import sys
import threading
import time

from config import config
from data import market
from database import db
from reports import reports
from risk import risk
from signals import engine
from telegram_bot import telegram
from watcher import watcher


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    filename=config.LOG_FILE
)

log = logging.getLogger(__name__)


running = True


# -------------------------------------------------
# Validate configuration
# -------------------------------------------------

def validate():

    if not config.TELEGRAM_TOKEN:"8656945768:AAE4-rNQ6EDm7wPNorQctAXWfcSYkCv1b2U"
        

    if not config.CHAT_ID:"-1004365660319"
    


# -------------------------------------------------
# Shutdown
# -------------------------------------------------

def stop(*_):

    global running

    running = False

    watcher.running = False

    telegram.send("🛑 Bot shutting down...")

    sys.exit(0)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


# -------------------------------------------------
# Scanner
# -------------------------------------------------

def scan():

    for symbol, ticker in config.SYMBOLS.items():

        try:

            if telegram.paused:
                return

            if not risk.trading_allowed():
                return

            if db.duplicate_trade(symbol):
                continue

            data = market.get_all(ticker)

            if data is None:
                continue

            signal_data = engine.analyze(data)

            if signal_data is None:
                continue

            if not db.cooldown_ok(
                symbol,
                signal_data["side"],
                config.COOLDOWN_MIN
            ):
                continue

            trade = risk.create_trade(
                symbol,
                signal_data
            )

            db.save_trade(trade)

            db.register_signal(
                symbol,
                signal_data["side"]
            )

            telegram.signal(trade)

            try:
                telegram.send_chart(
                    symbol,
                    data["M15"]
                )
            except Exception:
                log.exception(
                    "Chart failed"
                )

        except Exception:

            log.exception(
                "Scan error"
            )


# -------------------------------------------------
# Main
# -------------------------------------------------

def main():

    validate()

    reports.start()

    threading.Thread(
        target=watcher.loop,
        daemon=True
    ).start()

    telegram.send("🚀 StarFX V8.0 PRO Started")

    while running:

        try:

            scan()

        except Exception:

            log.exception(
                "Main loop"
            )

        time.sleep(
            config.SCAN_INTERVAL
        )


if __name__ == "__main__":

    main()
