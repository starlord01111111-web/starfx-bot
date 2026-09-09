from datetime import datetime

from config import config
from indicators import ind


class SignalEngine:

    def __init__(self):
        pass

    # --------------------------------------------

    def _session_ok(self):

        hour = datetime.now(config.TIMEZONE).hour

        return config.SESSION_START <= hour < config.SESSION_END

    # --------------------------------------------

    def analyze(self, data):

        if not self._session_ok():
            return None

        h4 = data["H4"]
        h1 = data["H1"]
        m15 = data["M15"]

        # ============================
        # H4 TREND
        # ============================

        ema50 = ind.ema(h4["Close"], 50).iloc[-1]
        ema200 = ind.ema(h4["Close"], 200).iloc[-1]
        price = h4["Close"].iloc[-1]

        trend = None

        if price > ema50 > ema200:
            trend = "BUY"

        elif price < ema50 < ema200:
            trend = "SELL"

        else:
            return None

        # ============================
        # H1 STRUCTURE
        # ============================

        bos = ind.detect_bos(h1)
        choch = ind.detect_choch(h1)
        sweep = ind.liquidity_sweep(h1)
        ob = ind.order_block(h1)

        bullish_fvg = ind.bullish_fvg(h1)
        bearish_fvg = ind.bearish_fvg(h1)

        # ============================
        # M15 ENTRY
        # ============================

        rsi = ind.rsi(m15["Close"]).iloc[-1]

        macd, signal, hist = ind.macd(m15["Close"])

        volume_ma = ind.volume_ma(m15).iloc[-1]

        last_volume = m15["Volume"].iloc[-1]

        engulf_buy = ind.bullish_engulfing(m15)
        engulf_sell = ind.bearish_engulfing(m15)

        pin = ind.pinbar(m15)

        score = 0

        reasons = []

        side = trend

        # ------------------------------
        # Trend
        # ------------------------------

        score += 2
        reasons.append("EMA Trend")

        # ------------------------------
        # BOS
        # ------------------------------

        if bos == side:
            score += 2
            reasons.append("BOS")
        else:
            return None

        # ------------------------------

        if choch == side:
            score += 1
            reasons.append("CHOCH")

        # ------------------------------

        if sweep == side:
            score += 1
            reasons.append("Liquidity Sweep")

        # ------------------------------

        if ob == side:
            score += 1
            reasons.append("Order Block")

        # ------------------------------

        if side == "BUY" and bullish_fvg:
            score += 1
            reasons.append("Bullish FVG")

        if side == "SELL" and bearish_fvg:
            score += 1
            reasons.append("Bearish FVG")

        # ------------------------------
        # Candle confirmation
        # ------------------------------

        if side == "BUY":

            if engulf_buy:
                score += 2
                reasons.append("Bullish Engulfing")

            elif pin == "BUY":
                score += 1
                reasons.append("Bullish Pin")

        else:

            if engulf_sell:
                score += 2
                reasons.append("Bearish Engulfing")

            elif pin == "SELL":
                score += 1
                reasons.append("Bearish Pin")

        # ------------------------------
        # RSI Filter
        # ------------------------------

        if side == "BUY" and rsi > 70:
            return None

        if side == "SELL" and rsi < 30:
            return None

        # ------------------------------
        # MACD Filter
        # ------------------------------

        if side == "BUY":

            if macd.iloc[-1] > signal.iloc[-1]:
                score += 1
                reasons.append("MACD")

            else:
                return None

        else:

            if macd.iloc[-1] < signal.iloc[-1]:
                score += 1
                reasons.append("MACD")

            else:
                return None

        # ------------------------------
        # Volume Filter
        # ------------------------------

        if last_volume > volume_ma:
            score += 1
            reasons.append("Volume")

        # ------------------------------
        # Grade
        # ------------------------------

        if score >= 10:
            grade = "A+ ⭐ SNIPER"

        elif score >= 8:
            grade = "A SNIPER"

        elif score >= 6:
            grade = "B SCALP"

        else:
            return None

        return {

            "side": side,

            "grade": grade,

            "score": score,

            "entry": float(m15["Close"].iloc[-1]),

            "atr": float(ind.atr(m15).iloc[-1]),

            "reasons": reasons

        }


engine = SignalEngine()
from data import market
from config import config
from risk import risk
from database import db
from telegram_bot import telegram


def scan():

    for symbol in config.SYMBOLS:

        try:

            ticker = config.SYMBOLS[symbol]
data = market.get_all(ticker)

            if data is None:
                continue

            signal = engine.analyze(data)

            if signal is None:
                continue

            if db.check_duplicate(symbol):
                continue

            entry = signal["entry"]

            atr = signal["atr"]

            sl, tp1, tp2 = risk.levels(
                symbol,
                signal["side"],
                entry,
                atr
            )

            trade = {
                "symbol": symbol,
                "side": signal["side"],
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "grade": signal["grade"],
                "score": signal["score"],
                "reasons": signal["reasons"],
                "tp1_hit": False,
                "sl_distance": abs(entry - sl)
            }

            db.add_trade(trade)

            telegram.signal(trade)

        except Exception as e:

            print(f"{symbol}: {e}")
