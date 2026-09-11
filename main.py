import asyncio
from datetime import datetime, timezone, timedelta
import os
import sqlite3
import ccxt
import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_TOKEN = "8656945768:AAFOQ03HPbaUvegYP2Or8xkSWY1S4Da7lVo"
CHAT_ID = "-1004365660319"
SYMBOLS = ["XAU/USD", "GBP/USD", "BTC/USDT"]
NEWS_CURRENCY = ["USD", "GBP"]

# Account Risk Config
ACCOUNT_BALANCE = 10000.00  # Default equity reference ($)
RISK_PER_TRADE_PCT = 0.01   # Risk 1% of account balance per trade
MAX_DAILY_LOSS_PCT = 0.03   # Block new trades if daily drawdown reaches 3%
MAX_CONCURRENT_TRADES = 2   # Maximum open active trades allowed

exchange = ccxt.binance()

# State Tracking Variables
daily_stats = {"date": None, "losses_today": 0.0, "is_circuit_broken": False}
active_trades = []


# --- RISK & POSITION SIZING ENGINE ---
def calculate_position_size(account_balance, risk_pct, entry, stop_loss):
    """
    Calculates dynamic position size based on exact dollar risk.
    """
    risk_amount = account_balance * risk_pct
    price_risk = abs(entry - stop_loss)
    if price_risk == 0:
        return 0.0
    units = risk_amount / price_risk
    return round(units, 4)


def check_circuit_breaker():
    """Resets daily stats at midnight UTC and verifies daily drawdown caps."""
    global daily_stats
    today = datetime.now(timezone.utc).date()
    
    if daily_stats["date"] != today:
        daily_stats["date"] = today
        daily_stats["losses_today"] = 0.0
        daily_stats["is_circuit_broken"] = False

    if daily_stats["losses_today"] >= (ACCOUNT_BALANCE * MAX_DAILY_LOSS_PCT):
        daily_stats["is_circuit_broken"] = True
        return False  # Trading disabled due to hitting daily loss limit

    return True


# --- TIME & SESSION ENGINE (EAT = UTC + 3) ---
def is_valid_trading_session():
    """
    Validates if current time is within trading sessions starting at 09:00 AM EAT.
    """
    now_utc = datetime.now(timezone.utc)
    eat_hour = (now_utc.hour + 3) % 24
    
    # 1. Daily Start Filter: No trades before 09:00 AM EAT
    if eat_hour < 9:
        return False, "OFF_HOURS"

    # 2. Session Filters (EAT Times)
    # London Session: 09:00 EAT to 18:00 EAT
    # New York Session: 16:00 EAT to 22:00 EAT
    if 9 <= eat_hour < 18:
        return True, "LONDON_SESSION"
    elif 16 <= eat_hour < 22:
        return True, "NEW_YORK_SESSION"
        
    return False, "OFF_HOURS"


# --- DATA ENGINE ---
def fetch_data(symbol, timeframe, limit=100):
    bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    return df


# --- TRADINGVIEW CHART ENGINE ---
def generate_tradingview_chart(df, symbol, setup, filename="chart.png"):
    plot_df = df.iloc[-40:].copy()
    
    mc = mpf.make_marketcolors(
        up='#26a69a', down='#ef5350',
        edge='inherit', wick='inherit', volume='in'
    )
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", gridcolor="#2a2e39", facecolor="#131722")
    
    hlines = [setup['price'], setup['tp1'], setup['tp2'], setup['sl']]
    colors = ['#2962ff', '#00e676', '#00c853', '#ff1744']
    
    fig, _ = mpf.plot(
        plot_df,
        type='candle',
        style=style,
        title=f"\n{symbol} - {setup['bias']} (A+ Setup)",
        hlines=dict(hlines=hlines, colors=colors, linestyle='--', linewidths=1.5),
        savefig=filename,
        returnfig=True,
        figratio=(16, 9),
        figscale=1.2
    )
    plt.close(fig)
    return filename


# --- NEWS & TECHNICAL ENGINE ---
def fetch_news_window():
    try:
        url = "https://n8n.forexfactory.com/ff_calendar_thisweek.json"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            events = response.json()
            now = datetime.now(timezone.utc)
            pre_news_events = []
            for ev in events:
                if ev.get("impact") == "High" and ev.get("country") in NEWS_CURRENCY:
                    ev_time = datetime.fromisoformat(ev["date"])
                    mins_until_news = (ev_time - now).total_seconds() / 60
                    if 30 <= mins_until_news <= 120:
                        pre_news_events.append(ev)
            return pre_news_events
    except Exception as e:
        print(f"News Fetch Error: {e}")
    return []


def analyze_structure(df, window=20):
    recent = df.iloc[:-1]
    high = recent['high'].iloc[-window:].max()
    low = recent['low'].iloc[-window:].min()
    current_close = df['close'].iloc[-1]
    
    if current_close > high: return "BULLISH"
    if current_close < low: return "BEARISH"
    
    ema = df['close'].ewm(span=20).mean().iloc[-1]
    return "BULLISH" if current_close > ema else "BEARISH"


def detect_price_action(df):
    c1 = df.iloc[-2]
    body_1 = abs(c1['close'] - c1['open'])
    upper_wick = c1['high'] - max(c1['close'], c1['open'])
    lower_wick = min(c1['close'], c1['open']) - c1['low']
    
    if lower_wick >= (2 * body_1) and upper_wick <= (0.5 * body_1):
        return "BULLISH_PINBAR"
    if upper_wick >= (2 * body_1) and lower_wick <= (0.5 * body_1):
        return "BEARISH_PINBAR"
    if c1['close'] > c1['open'] and df.iloc[-3]['close'] < df.iloc[-3]['open'] and body_1 > abs(df.iloc[-3]['close'] - df.iloc[-3]['open']):
        return "BULLISH_ENGULFING"
    if c1['close'] < c1['open'] and df.iloc[-3]['close'] > df.iloc[-3]['open'] and body_1 > abs(df.iloc[-3]['close'] - df.iloc[-3]['open']):
        return "BEARISH_ENGULFING"
    return None


def detect_liquidity_sweep(df, window=30):
    recent_high = df['high'].iloc[-window:-2].max()
    recent_low = df['low'].iloc[-window:-2].min()
    c1, c0 = df.iloc[-2], df.iloc[-1]
    
    if c1['low'] < recent_low and c0['close'] > recent_low:
        return "BULLISH_SWEEP"
    if c1['high'] > recent_high and c0['close'] < recent_high:
        return "BEARISH_SWEEP"
    return None


# --- A+ EVALUATOR WITH RISK CONTROLS ---
def evaluate_aplus_setup(symbol):
    # Check circuit breaker & max concurrent trades
    if not check_circuit_breaker():
        return None
    if len(active_trades) >= MAX_CONCURRENT_TRADES:
        return None

    session_active, session_name = is_valid_trading_session()
    if not session_active:
        return None

    pre_news = fetch_news_window()
    
    tf_data = {
        'H4': fetch_data(symbol, '4h'),
        'H1': fetch_data(symbol, '1h'),
        'M15': fetch_data(symbol, '15m'),
        'M5': fetch_data(symbol, '5m')
    }
    
    h4_bias = analyze_structure(tf_data['H4'])
    h1_bias = analyze_structure(tf_data['H1'])
    
    if h4_bias != h1_bias:
        return None
        
    m15_sweep = detect_liquidity_sweep(tf_data['M15'])
    m5_sweep = detect_liquidity_sweep(tf_data['M5'])
    m5_pa = detect_price_action(tf_data['M5'])
    price = tf_data['M5']['close'].iloc[-1]
    
    score = 0
    if h4_bias == h1_bias: score += 30
    if m15_sweep or m5_sweep: score += 30
    if m5_pa: score += 25
    if pre_news: score += 15

    if score < 85:
        return None

    # BULLISH SETUP
    if h4_bias == "BULLISH" and m5_pa in ["BULLISH_PINBAR", "BULLISH_ENGULFING"]:
        sl = tf_data['M5']['low'].iloc[-3:].min() * 0.9995
        risk = price - sl
        position_units = calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, price, sl)
        
        return {
            "symbol": symbol,
            "bias": "BUY (A+ CONFLUENCE)",
            "price": price,
            "sl": sl,
            "tp1": price + (risk * 1.5),
            "tp2": price + (risk * 3.0),
            "position_units": position_units,
            "session": session_name,
            "df": tf_data['M5'],
            "pre_news": True if pre_news else False
        }

    # BEARISH SETUP
    if h4_bias == "BEARISH" and m5_pa in ["BEARISH_PINBAR", "BEARISH_ENGULFING"]:
        sl = tf_data['M5']['high'].iloc[-3:].max() * 1.0005
        risk = sl - price
        position_units = calculate_position_size(ACCOUNT_BALANCE, RISK_PER_TRADE_PCT, price, sl)

        return {
            "symbol": symbol,
            "bias": "SELL (A+ CONFLUENCE)",
            "price": price,
            "sl": sl,
            "tp1": price - (risk * 1.5),
            "tp2": price - (risk * 3.0),
            "position_units": position_units,
            "session": session_name,
            "df": tf_data['M5'],
            "pre_news": True if pre_news else False
        }

    return None


# --- POSITION TRACKER WITH RISK BREAKER ---
async def track_positions(app: Application):
    global active_trades, daily_stats
    while True:
        try:
            for trade in list(active_trades):
                ticker = exchange.fetch_ticker(trade['symbol'])
                current_price = ticker['last']
                
                # BUY Trajectory
                if "BUY" in trade['bias']:
                    if current_price >= trade['tp1'] and not trade.get('tp1_hit'):
                        trade['tp1_hit'] = True
                        await app.bot.send_message(
                            chat_id=CHAT_ID, 
                            text=f"✅ *TP1 HIT (1:1.5 RR)* for `{trade['symbol']}`\nLocking in partials, moving SL to entry.", 
                            parse_mode="Markdown"
                        )
                    elif current_price >= trade['tp2']:
                        await app.bot.send_message(
                            chat_id=CHAT_ID, 
                            text=f"🎯🎯 *TP2 FULL TARGET HIT (1:3 RR)* for `{trade['symbol']}`", 
                            parse_mode="Markdown"
                        )
                        active_trades.remove(trade)
                    elif current_price <= trade['sl']:
                        daily_stats['losses_today'] += (ACCOUNT_BALANCE * RISK_PER_TRADE_PCT)
                        await app.bot.send_message(
                            chat_id=CHAT_ID, 
                            text=f"🛑 *STOP LOSS HIT* for `{trade['symbol']}`\nRisk Management logged -1% loss.", 
                            parse_mode="Markdown"
                        )
                        active_trades.remove(trade)

                # SELL Trajectory
                elif "SELL" in trade['bias']:
                    if current_price <= trade['tp1'] and not trade.get('tp1_hit'):
                        trade['tp1_hit'] = True
                        await app.bot.send_message(
                            chat_id=CHAT_ID, 
                            text=f"✅ *TP1 HIT (1:1.5 RR)* for `{trade['symbol']}`\nLocking in partials, moving SL to entry.", 
                            parse_mode="Markdown"
                        )
                    elif current_price <= trade['tp2']:
                        await app.bot.send_message(
                            chat_id=CHAT_ID, 
                            text=f"🎯🎯 *TP2 FULL TARGET HIT (1:3 RR)* for `{trade['symbol']}`", 
                            parse_mode="Markdown"
                        )
                        active_trades.remove(trade)
                    elif current_price >= trade['sl']:
                        daily_stats['losses_today'] += (ACCOUNT_BALANCE * RISK_PER_TRADE_PCT)
                        await app.bot.send_message(
                            chat_id=CHAT_ID, 
                            text=f"🛑 *STOP LOSS HIT* for `{trade['symbol']}`\nRisk Management logged -1% loss.", 
                            parse_mode="Markdown"
                        )
                        active_trades.remove(trade)

            await asyncio.sleep(10)
        except Exception as e:
            print(f"Tracking Loop Exception: {e}")
            await asyncio.sleep(10)


# --- DAILY REPORT (23:00 EAT) ---
async def schedule_daily_report(app: Application):
    while True:
        try:
            now = datetime.now(timezone.utc)
            # 23:00 EAT = 20:00 UTC
            if now.hour == 20 and now.minute == 0:
                report = "📊 *23:00 EAT DAILY INSTITUTIONAL REPORT*\n\n"
                for sym in SYMBOLS:
                    df = fetch_data(sym, '1d', limit=5)
                    report += f"**{sym}:** Price: `{df['close'].iloc[-1]:.2f}` | High: `{df['high'].iloc[-1]:.2f}` | Low: `{df['low'].iloc[-1]:.2f}`\n"
                
                report += f"\n🛡️ *Risk Management Summary:*\n• Daily Drawdown Logged: `${daily_stats['losses_today']:.2f}` / `${ACCOUNT_BALANCE * MAX_DAILY_LOSS_PCT:.2f}`"
                
                await app.bot.send_message(chat_id=CHAT_ID, text=report, parse_mode="Markdown")
                await asyncio.sleep(60)
            await asyncio.sleep(20)
        except Exception as e:
            print(f"Report Scheduler Error: {e}")
            await asyncio.sleep(10)


# --- MAIN MARKET SCANNER ---
async def market_scanner(app: Application):
    print("Market Scanner Operational (Signals start at 09:00 EAT)...")
    last_processed_m5 = {}

    while True:
        try:
            for symbol in SYMBOLS:
                m5_df = fetch_data(symbol, '5m', limit=5)
                current_m5_time = m5_df.index[-1]

                if last_processed_m5.get(symbol) != current_m5_time:
                    last_processed_m5[symbol] = current_m5_time

                    setup = evaluate_aplus_setup(symbol)
                    if setup:
                        active_trades.append(setup)

                        chart_file = generate_tradingview_chart(setup['df'], symbol, setup)

                        news_tag = "⚡ *PRE-NEWS ACCUMULATION*" if setup['pre_news'] else "🔥 *A+ STANDARD CONFLUENCE*"
                        caption = (
                            f"🎯 *PRECISION A+ SNIPER SIGNAL* 🎯\n\n"
                            f"**Asset:** `{setup['symbol']}`\n"
                            f"**Active Session:** `{setup['session']}`\n"
                            f"**Setup Type:** {news_tag}\n"
                            f"**Action:** `{setup['bias']}`\n\n"
                            f"📍 *Execution Parameters:*\n"
                            f"• **Entry Price:** `{setup['price']:.2f}`\n"
                            f"• **Stop Loss:** `{setup['sl']:.2f}`\n"
                            f"• **TP1 (Partial 1:1.5 RR):** `{setup['tp1']:.2f}`\n"
                            f"• **TP2 (Full 1:3.0 RR):** `{setup['tp2']:.2f}`\n\n"
                            f"🛡️ *Risk Management Metrics:*\n"
                            f"• **Rec. Position Size:** `{setup['position_units']} Units` (Risk: 1.0% Equity)\n"
                            f"• **Max Allowed Risk per Trade:** `${ACCOUNT_BALANCE * RISK_PER_TRADE_PCT:.2f}`"
                        )

                        with open(chart_file, "rb") as photo:
                            await app.bot.send_photo(
                                chat_id=CHAT_ID, 
                                photo=photo, 
                                caption=caption, 
                                parse_mode="Markdown"
                            )

                        if os.path.exists(chart_file):
                            os.remove(chart_file)

            await asyncio.sleep(15)
        except Exception as e:
            print(f"Scanner Exception: {e}")
            await asyncio.sleep(10)

# --- DATABASE INITIALIZATION ---
def init_db():
    """Initializes trade performance database for adaptive learning."""
    conn = sqlite3.connect("trading_data.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            bias TEXT,
            h4_h1_aligned INTEGER,
            m15_sweep INTEGER,
            m5_pa INTEGER,
            result INTEGER
        )
    """
    )
    conn.commit()
    conn.close()

# --- MAIN ENTRY POINT ---
import asyncio
from telegram.ext import Application


async def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Must use asyncio.create_task for async functions in modern asyncio
    asyncio.create_task(market_scanner(app))
    asyncio.create_task(track_positions(app))
    asyncio.create_task(schedule_daily_report(app))

    print("Adaptive Bot Application Online...")

    # Initialize and run polling within the async context manager
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        # Keep the process alive indefinitely
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
    
