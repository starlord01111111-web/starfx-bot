import logging
import time

from data import market
from database import db
from risk import risk
from telegram_bot import telegram

log = logging.getLogger(__name__)


class Watcher:

    def __init__(self):
        self.running = True

    # -------------------------------------------------

    def price(self, symbol, cache):

        if symbol not in cache:

            df = market.m1(symbol)

            if df is None:
                return None

            cache[symbol] = float(df["Close"].iloc[-1])

        return cache[symbol]

    # -------------------------------------------------

    def process_trade(self, trade, current_price):

        if current_price is None:
            return

        # -------------------------
        # Expiry
        # -------------------------

        if risk.expired(trade):

            db.close_trade(
                trade["id"],
                "EXPIRED"
            )

            telegram.send(
                f"⌛ {trade['symbol']} expired."
            )

            return

        side = trade["side"]

        # =========================
        # BUY
        # =========================

        if side == "BUY":

            if current_price <= trade["sl"]:

                db.close_trade(
                    trade["id"],
                    "SL"
                )

                risk.record_result(-1)

                telegram.sl(trade)

                return

            if (
                current_price >= trade["tp1"]
                and not trade["tp1_hit"]
            ):

                trade = risk.move_break_even(trade)

                telegram.tp1(trade)

            if current_price >= trade["tp2"]:

                db.close_trade(
                    trade["id"],
                    "TP2"
                )

                risk.record_result(3)

                telegram.tp2(trade)

                return

        # =========================
        # SELL
        # =========================

        else:

            if current_price >= trade["sl"]:

                db.close_trade(
                    trade["id"],
                    "SL"
                )

                risk.record_result(-1)

                telegram.sl(trade)

                return

            if (
                current_price <= trade["tp1"]
                and not trade["tp1_hit"]
            ):

                trade = risk.move_break_even(trade)

                telegram.tp1(trade)

            if current_price <= trade["tp2"]:

                db.close_trade(
                    trade["id"],
                    "TP2"
                )

                risk.record_result(3)

                telegram.tp2(trade)

                return

        # -------------------------
        # Trailing stop
        # -------------------------

        if trade["tp1_hit"]:

            atr = trade["sl_distance"] / 1.5

            risk.trail_stop(
                trade,
                current_price,
                atr
            )

    # -------------------------------------------------

    def loop(self):

        while self.running:

            try:

                trades = db.open_trades()

                price_cache = {}

                for trade in trades:

                    price = self.price(
                        trade["symbol"],
                        price_cache
                    )

                    self.process_trade(
                        trade,
                        price
                    )

            except Exception:

                log.exception(
                    "Watcher error"
                )

            time.sleep(60)


watcher = Watcher()
def start_watcher():
    watcher.loop()
