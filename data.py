import random
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from config import config


class MarketData:

    def __init__(self):

        self.cache = {}

        self.pause_until = None

    ##################################################

    def paused(self):

        if self.pause_until is None:
            return False

        return datetime.utcnow() < self.pause_until

    ##################################################

    def _download(self, ticker, period, interval):

        if self.paused():
            return None

        key = f"{ticker}_{period}_{interval}"

        now = time.time()

        if key in self.cache:

            cached = self.cache[key]

            if now - cached["time"] < config.CACHE_SECONDS:
                return cached["data"]

        delay = 1

        for attempt in range(config.YAHOO_RETRIES):

            try:

         df = yf.download(
    ticker=ticker,
    period=period,
    interval=interval,
    progress=False,
    auto_adjust=False,
    threads=False,
    timeout=20
         ) print(f"Downloading {ticker} {interval}")      
                    
                
                
                
                
                
                

                if len(df) == 0: print(f"No data returned for {ticker}")
                    raise Exception("Empty dataframe")

                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                df = df.rename(columns=str.title)

                required = [
                    "Open",
                    "High",
                    "Low",
                    "Close",
                    "Volume"
                ]

                for col in required:

                    if col not in df.columns:
                        raise Exception(f"Missing column {col}")

                df.dropna(inplace=True)

                self.cache[key] = {

                    "time": now,

                    "data": df

                }

                return df

            except Exception as e:
    print(f"[ERROR] {ticker} ({interval}): {e}")

    import traceback
    traceback.print_exc()

    time.sleep(delay + random.random())
    delay *= 2
        
      self.pause_until = datetime.utcnow() + timedelta(
    minutes=config.YAHOO_PAUSE_MINUTES
)

print("=" * 60)
print("⚠️ Yahoo Finance rate limit reached!")
print(f"Bot paused until: {self.pause_until}")
print("No market data will be downloaded during this period.")
print("=" * 60)

return None  
        

        

    ##################################################

    def validate(self, df):

        if df is None:
            return False

        if len(df) < 50:
            return False

        try:

            last = df.index[-1]

            if hasattr(last, "tz_localize"):
                last = last.tz_localize(None)

            age = datetime.utcnow() - last.to_pydatetime()

            if age.total_seconds() > 6 * 3600:
                return False

        except Exception:

            return False

        return True

    ##################################################

    def h4(self, ticker):

        df = self._download(
            ticker,
            "90d",
            "1h"
        )

        if not self.validate(df):
            return None

        return df.resample("4H").agg({

            "Open": "first",

            "High": "max",

            "Low": "min",

            "Close": "last",

            "Volume": "sum"

        }).dropna()

    ##################################################

    def h1(self, ticker):

        df = self._download(
            ticker,
            "30d",
            "1h"
        )

        if not self.validate(df):
            return None

        return df

    ##################################################

    def m15(self, ticker):

        df = self._download(
            ticker,
            "10d",
            "15m"
        )

        if not self.validate(df):
            return None

        return df

    ##################################################

    def m1(self, ticker):

        df = self._download(
            ticker,
            "1d",
            "1m"
        )

        if not self.validate(df):
            return None

        return df

    ##################################################

    def get_all(self, ticker):

        h4 = self.h4(ticker)

        h1 = self.h1(ticker)

        m15 = self.m15(ticker)

        if h4 is None or h1 is None or m15 is None:
            return None

        return {

            "H4": h4,

            "H1": h1,

            "M15": m15

        }


market = MarketData()
