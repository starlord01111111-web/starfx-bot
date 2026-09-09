from datetime import datetime, timedelta

from config import config
from database import db


class RiskManager:

    def __init__(self):
        self.daily_loss = 0.0
        self.daily_trades = 0
        self.last_reset = datetime.now(config.TIMEZONE).date()

    # -------------------------------------------------
    # Reset counters each day
    # -------------------------------------------------

    def _reset_if_new_day(self):
        today = datetime.now(config.TIMEZONE).date()

        if today != self.last_reset:
            self.last_reset = today
            self.daily_loss = 0.0
            self.daily_trades = 0

    # -------------------------------------------------
    # Trading allowed?
    # -------------------------------------------------

    def trading_allowed(self):

        self._reset_if_new_day()

        if self.daily_loss >= config.MAX_DAILY_LOSS:
            return False

        if self.daily_trades >= config.MAX_DAILY_TRADES:
            return False

        if len(db.open_trades()) >= config.MAX_SIMULTANEOUS:
            return False

        return True

    # -------------------------------------------------
    # Stop Loss distance
    # -------------------------------------------------

    def sl_distance(self, symbol, atr):

        if symbol == "GOLD":
            return max(config.GOLD_MIN_SL, atr * 1.5)

        if symbol == "BTCUSD":
            return max(config.BTC_MIN_SL, atr * 1.5)

        return max(config.FOREX_MIN_SL, atr * 1.5)

    # -------------------------------------------------
    # Position Size
    # -------------------------------------------------

    def position_size(self, balance, risk_percent, sl_distance):

        if sl_distance <= 0:
            return 0

        risk_amount = balance * risk_percent

        return round(risk_amount / sl_distance, 2)

    # -------------------------------------------------
    # Build Trade
    # -------------------------------------------------

    def create_trade(
        self,
        symbol,
        signal,
        balance=10000
    ):

        atr = signal["atr"]

        entry = signal["entry"]

        side = signal["side"]

        sl_dist = self.sl_distance(symbol, atr)

        if side == "BUY":

            sl = entry - sl_dist

            tp1 = entry + sl_dist * 2

            tp2 = entry + sl_dist * 3

        else:

            sl = entry + sl_dist

            tp1 = entry - sl_dist * 2

            tp2 = entry - sl_dist * 3

        lot = self.position_size(
            balance,
            config.RISK_PER_TRADE,
            sl_dist
        )

        expiry = datetime.utcnow() + timedelta(
            hours=config.TRADE_EXPIRY_HOURS
        )

        return {

            "symbol": symbol,

            "side": side,

            "grade": signal["grade"],

            "score": signal["score"],

            "entry": entry,

            "sl": sl,

            "tp1": tp1,

            "tp2": tp2,

            "sl_distance": sl_dist,

            "lot": lot,

            "tp1_hit": False,

            "expiry": expiry.isoformat(),

            "reasons": signal["reasons"]

        }

    # -------------------------------------------------
    # Trade expired?
    # -------------------------------------------------

    def expired(self, trade):

        expiry = datetime.fromisoformat(
            trade["expiry"]
        )

        return datetime.utcnow() > expiry

    # -------------------------------------------------
    # Move to Break-even
    # -------------------------------------------------

    def move_break_even(self, trade):

        trade["sl"] = trade["entry"]

        trade["tp1_hit"] = True

        return trade

    # -------------------------------------------------
    # Trailing Stop
    # -------------------------------------------------

    def trail_stop(self, trade, current_price, atr):

        if not trade["tp1_hit"]:
            return trade

        distance = atr * 1.2

        if trade["side"] == "BUY":

            new_sl = current_price - distance

            if new_sl > trade["sl"]:
                trade["sl"] = new_sl

        else:

            new_sl = current_price + distance

            if new_sl < trade["sl"]:
                trade["sl"] = new_sl

        return trade

    # -------------------------------------------------
    # Record result
    # -------------------------------------------------

    def record_result(self, rr):

        self._reset_if_new_day()

        self.daily_trades += 1

        if rr < 0:
            self.daily_loss += abs(rr)


risk = RiskManager()
