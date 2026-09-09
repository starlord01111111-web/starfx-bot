# 🚀 StarFX V8.0 PRO

StarFX V8.0 PRO is an advanced Telegram trading signal bot built with Python.

It scans multiple financial markets using Smart Money Concepts (SMC), trend confirmation, and technical indicators to generate high-quality trading signals.

---

# Features

## Multi-Timeframe Analysis

- H4 Trend Analysis
- H1 Market Structure
- M15 Entry Confirmation

---

## Smart Money Concepts

- Break of Structure (BOS)
- Change of Character (CHOCH)
- Fair Value Gap (FVG)
- Order Blocks
- Liquidity Sweeps
- Supply & Demand Zones

---

## Technical Indicators

- EMA 50
- EMA 200
- ATR
- RSI
- MACD
- Volume Confirmation

---

## Risk Management

- ATR Stop Loss
- Dynamic Position Sizing
- Take Profit 1 (1:2 RR)
- Take Profit 2 (1:3 RR)
- Automatic Break Even
- Trailing Stop
- Trade Expiry
- Maximum Daily Loss
- Maximum Daily Trades
- Maximum Simultaneous Trades

---

## Telegram Features

- Signal Alerts
- TP1 Notifications
- TP2 Notifications
- Stop Loss Notifications
- Daily Reports
- Weekly Reports
- Monthly Reports
- Heartbeat Messages

Commands:

- /status
- /open
- /stats
- /pause
- /resume

---

## Database

SQLite is used for:

- Open Trades
- Trade History
- Signal Cooldowns
- Performance Statistics

---

# Supported Markets

- Gold (GC=F)
- Bitcoin (BTC-USD)
- GBP/USD
- EUR/USD

Additional symbols can be added in `config.py`.

---

# Installation

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/StarFX.git
```

Go into the project:

```bash
cd StarFX
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

# Configuration

Create a `.env` file from `.env.example`.

Example:

```env
TELEGRAM_TOKEN=YOUR_BOT_TOKEN
CHAT_ID=YOUR_CHAT_ID

RISK_PER_TRADE=0.01
MAX_DAILY_LOSS=0.05
MAX_DAILY_TRADES=10
MAX_SIMULTANEOUS=3

SCAN_INTERVAL=40
COOLDOWN_MIN=60
TRADE_EXPIRY_HOURS=8
```

---

# Running

Run locally:

```bash
python main.py
```

---

# Deploying to Render

Build Command

```text
pip install -r requirements.txt
```

Start Command

```text
python main.py
```

Add the following Environment Variables in Render:

- TELEGRAM_TOKEN
- CHAT_ID
- RISK_PER_TRADE
- MAX_DAILY_LOSS
- MAX_DAILY_TRADES
- MAX_SIMULTANEOUS
- SCAN_INTERVAL
- COOLDOWN_MIN
- TRADE_EXPIRY_HOURS

---

# Project Structure

```
StarFX/
│
├── config.py
├── data.py
├── indicators.py
├── signals.py
├── risk.py
├── database.py
├── telegram_bot.py
├── watcher.py
├── reports.py
├── main.py
├── requirements.txt
├── .env.example
├── README.md
└── .gitignore
```

---

# Disclaimer

This software is provided for educational and research purposes only.

Trading financial markets involves substantial risk. Past performance does not guarantee future results.

Always test thoroughly before using any trading strategy with real funds.

---

# License

MIT License

Copyright (c) 2026 StarFX

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the software, subject to the conditions of the MIT License.
