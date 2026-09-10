from datetime import datetime

from config import config
from indicators import ind
from data import market
from risk import risk
from database import db
from telegram_bot import telegram


class SignalEngine:

    def session_ok(self):
        hour = datetime.now(config.TIMEZONE).hour
        return config.SESSION_START <= hour < config.SESSION_END

    def analyze(self, data):

        if not self.session_ok():
            return None

        h4 = data["H4"]
        h1 = data["H1"]
        m15 = data["M15"]

        # H4 Trend
        ema50 = ind.ema(h4["Close"], 50).iloc[-1]
        ema200 = ind.ema(h4["Close"], 200).iloc[-1]
        price = h4["Close"].iloc[-1]

        if price > ema50 > ema200:
            side = "BUY"
        elif price < ema50 < ema200:
            side = "SELL"
        else:
            return None

        score = 2
        reasons = ["EMA Trend"]

        # H1 Structure
        bos = ind.detect_bos(h1)
        choch = ind.detect_choch(h1)
        sweep = ind.liquidity_sweep(h1)
        ob = ind.order_block(h1)

        bullish_fvg = ind.bullish_fvg(h1)
        bearish_fvg = ind.bearish_fvg(h1)

        if bos != side:
            return None

        score += 2
        reasons.append("BOS")

        if choch == side:
            score += 1
            reasons.append("CHOCH")

        if sweep == side:
            score += 1
            reasons.append("Liquidity Sweep")

        if ob == side:
            score += 1
            reasons.append("Order Block")

        if side == "BUY" and bullish_fvg:
            score += 1
            reasons.append("Bullish FVG")

        if side == "SELL" and bearish_fvg:
            score += 1
            reasons.append("Bearish FVG")

        # M15 Entry
        rsi = ind.rsi(m15["Close"]).iloc[-1]
        macd, signal, hist = ind.macd(m15["Close"])

        volume_ma = ind.volume_ma(m15).iloc[-1]
        volume = m15["Volume"].iloc[-1]

        engulf_buy = ind.bullish_engulfing(m15)
        engulf_sell = ind.bearish_engulfing(m15)
        pin = ind.pinbar(m15)

        if side == "BUY":

            if rsi > 70:
                return None

            if macd.iloc[-1] <= signal.iloc[-1]:
                return None

            score += 1
            reasons.append("MACD")

            if engulf_buy:
                score += 2
                reasons.append("Bullish Engulfing")
            elif pin == "BUY":
                score += 1
                reasons.append("Bullish Pin")

        else:

            if rsi < 30:
                return None

            if macd.iloc[-1] >= signal.iloc[-1]:
                return None

            score += 1
            reasons.append("MACD")

            if engulf_sell:
                score += 2
                reasons.append("Bearish Engulfing")
            elif pin == "SELL":
                score += 1
                reasons.append("Bearish Pin")

        if volume > volume_ma:
            score += 1
            reasons.append("Volume")

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
            "reasons": reasons,
        }


engine = SignalEngine()


def scan():

    print("=" * 60)
    print("SCAN STARTED")
    print("=" * 60)

    if not risk.trading_allowed():
        print("Trading is currently blocked.")
        return

    for symbol, ticker in config.SYMBOLS.items():

        print(f"Checking {symbol} ({ticker})...")

        try:

            data = market.get_all(ticker)

            if data is None:
                print(f"No market data for {symbol}")
                continue

            signal = engine.analyze(data)

            if signal is None:
                print(f"No setup found for {symbol}")
                continue

            print(f"Signal found for {symbol}")

            trade = risk.create_trade(symbol, signal)

            db.add_trade(trade)

            telegram.signal(trade)

            print(f"Signal sent to Telegram for {symbol}")

        except Exception as e:
            print(f"ERROR while scanning {symbol}: {e}")

    print("SCAN FINISHED")
    print("=" * 60)
