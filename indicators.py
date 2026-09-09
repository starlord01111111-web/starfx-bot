import numpy as np
import pandas as pd


class Indicators:

    # -------------------------------------------------
    # EMA
    # -------------------------------------------------

    @staticmethod
    def ema(series, period):

        return series.ewm(
            span=period,
            adjust=False
        ).mean()

    # -------------------------------------------------
    # ATR (True ATR)
    # -------------------------------------------------

    @staticmethod
    def atr(df, period=14):

        high = df["High"]
        low = df["Low"]
        close = df["Close"]

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)

        return tr.rolling(period).mean()

    # -------------------------------------------------
    # RSI
    # -------------------------------------------------

    @staticmethod
    def rsi(close, period=14):

        delta = close.diff()

        gain = delta.clip(lower=0)

        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(period).mean()

        avg_loss = loss.rolling(period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)

        return 100 - (100 / (1 + rs))

    # -------------------------------------------------
    # MACD
    # -------------------------------------------------

    @staticmethod
    def macd(close):

        ema12 = Indicators.ema(close, 12)

        ema26 = Indicators.ema(close, 26)

        macd = ema12 - ema26

        signal = macd.ewm(
            span=9,
            adjust=False
        ).mean()

        hist = macd - signal

        return macd, signal, hist

    # -------------------------------------------------
    # Volume MA
    # -------------------------------------------------

    @staticmethod
    def volume_ma(df, period=20):

        return df["Volume"].rolling(period).mean()

    # -------------------------------------------------
    # Swing High
    # -------------------------------------------------

    @staticmethod
    def swing_high(df, lookback=5):

        highs = df["High"]

        return highs == highs.rolling(
            lookback * 2 + 1,
            center=True
        ).max()

    # -------------------------------------------------
    # Swing Low
    # -------------------------------------------------

    @staticmethod
    def swing_low(df, lookback=5):

        lows = df["Low"]

        return lows == lows.rolling(
            lookback * 2 + 1,
            center=True
        ).min()

    # -------------------------------------------------
    # Break of Structure (BOS)
    # -------------------------------------------------

    @staticmethod
    def detect_bos(df):

        recent_high = df["High"].iloc[-25:-5].max()

        recent_low = df["Low"].iloc[-25:-5].min()

        close = df["Close"].iloc[-1]

        if close > recent_high:

            return "BUY"

        if close < recent_low:

            return "SELL"

        return None

    # -------------------------------------------------
    # Change of Character (CHOCH)
    # -------------------------------------------------

    @staticmethod
    def detect_choch(df):

        h1 = df["High"].iloc[-4]

        h2 = df["High"].iloc[-2]

        l1 = df["Low"].iloc[-4]

        l2 = df["Low"].iloc[-2]

        if h2 > h1 and l2 > l1:

            return "BUY"

        if h2 < h1 and l2 < l1:

            return "SELL"

        return None

    # -------------------------------------------------
    # Bullish Fair Value Gap
    # -------------------------------------------------

    @staticmethod
    def bullish_fvg(df):

        c1 = df.iloc[-3]

        c3 = df.iloc[-1]

        return c3["Low"] > c1["High"]

    # -------------------------------------------------
    # Bearish Fair Value Gap
    # -------------------------------------------------

    @staticmethod
    def bearish_fvg(df):

        c1 = df.iloc[-3]

        c3 = df.iloc[-1]

        return c3["High"] < c1["Low"]

    # -------------------------------------------------
    # Liquidity Sweep
    # -------------------------------------------------

    @staticmethod
    def liquidity_sweep(df):

        last = df.iloc[-1]

        recent_high = df["High"].iloc[-20:-1].max()

        recent_low = df["Low"].iloc[-20:-1].min()

        if last["High"] > recent_high and last["Close"] < recent_high:

            return "SELL"

        if last["Low"] < recent_low and last["Close"] > recent_low:

            return "BUY"

        return None

    # -------------------------------------------------
    # Order Block (simple version)
    # -------------------------------------------------

    @staticmethod
    def order_block(df):

        last = df.iloc[-2]

        if last["Close"] < last["Open"]:

            return "BUY"

        if last["Close"] > last["Open"]:

            return "SELL"

        return None

    # -------------------------------------------------
    # Bullish Engulfing
    # -------------------------------------------------

    @staticmethod
    def bullish_engulfing(df):

        prev = df.iloc[-2]

        last = df.iloc[-1]

        return (
            prev["Close"] < prev["Open"]
            and last["Close"] > last["Open"]
            and last["Close"] > prev["Open"]
            and last["Open"] < prev["Close"]
        )

    # -------------------------------------------------
    # Bearish Engulfing
    # -------------------------------------------------

    @staticmethod
    def bearish_engulfing(df):

        prev = df.iloc[-2]

        last = df.iloc[-1]

        return (
            prev["Close"] > prev["Open"]
            and last["Close"] < last["Open"]
            and last["Close"] < prev["Open"]
            and last["Open"] > prev["Close"]
        )

    # -------------------------------------------------
    # Pin Bar
    # -------------------------------------------------

    @staticmethod
    def pinbar(df):

        last = df.iloc[-1]

        body = abs(last["Close"] - last["Open"])

        rng = last["High"] - last["Low"]

        if rng == 0:
            return None

        upper = last["High"] - max(last["Close"], last["Open"])

        lower = min(last["Close"], last["Open"]) - last["Low"]

        if lower > body * 2 and body < rng * 0.30:

            return "BUY"

        if upper > body * 2 and body < rng * 0.30:

            return "SELL"

        return None


ind = Indicators()
