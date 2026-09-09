import io
import logging

import matplotlib.pyplot as plt
import requests

from config import config
from database import db

log = logging.getLogger(__name__)


class TelegramBot:

    def __init__(self):

        self.base = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"

        self.paused = False

    # -------------------------------------------------

    def send(self, text, markdown=True):

        payload = {

            "chat_id": config.CHAT_ID,

            "text": text

        }

        if markdown:
            payload["parse_mode"] = "Markdown"

        try:

            requests.post(

                self.base + "/sendMessage",

                json=payload,

                timeout=15

            )

        except Exception as e:

            log.exception(e)

    # -------------------------------------------------

    def send_chart(self, symbol, df):

        try:

            plt.figure(figsize=(8,4))

            plt.plot(df["Close"])

            plt.title(symbol)

            plt.grid(True)

            buf = io.BytesIO()

            plt.savefig(buf, format="png")

            plt.close()

            buf.seek(0)

            requests.post(

                self.base + "/sendPhoto",

                data={

                    "chat_id": config.CHAT_ID

                },

                files={

                    "photo": ("chart.png", buf)

                },

                timeout=20

            )

        except Exception as e:

            log.exception(e)

    # -------------------------------------------------

    def signal(self, trade):

        side = "🟢 BUY" if trade["side"] == "BUY" else "🔴 SELL"

        reasons = "\n".join(

            f"• {r}" for r in trade["reasons"]

        )

        msg = f"""
🚀 *{trade['grade']}*

*{trade['symbol']}*

{side}

Entry: `{trade['entry']:.5f}`

SL: `{trade['sl']:.5f}`

TP1: `{trade['tp1']:.5f}`

TP2: `{trade['tp2']:.5f}`

Score: *{trade['score']}*

Reasons

{reasons}
"""

        self.send(msg)

    # -------------------------------------------------

    def tp1(self, trade):

        self.send(

            f"✅ TP1 HIT\n\n"

            f"{trade['symbol']} "

            f"{trade['side']}\n"

            f"Move SL to Break-even."

        )

    # -------------------------------------------------

    def tp2(self, trade):

        self.send(

            f"🏆 TP2 HIT\n\n"

            f"{trade['symbol']} "

            f"{trade['side']}"

        )

    # -------------------------------------------------

    def sl(self, trade):

        self.send(

            f"❌ STOP LOSS\n\n"

            f"{trade['symbol']} "

            f"{trade['side']}"

        )

    # -------------------------------------------------

    def heartbeat(self):

        self.send(

            "💚 Bot online."

        )

    # -------------------------------------------------

    def report(self):

        trades = db.open_trades()

        self.send(

            f"""
📊 *STATUS*

Open Trades: {len(trades)}

Paused: {self.paused}
"""
        )

    # -------------------------------------------------

    def stats(self):

        self.send(

            "📈 Statistics module "

            "will read SQLite "

            "performance records."

        )

    # -------------------------------------------------

    def handle_command(self, text):

        cmd = text.strip().lower()

        if cmd == "/status":

            self.report()

        elif cmd == "/open":

            trades = db.open_trades()

            if not trades:

                self.send("No open trades.")

                return

            msg = "*OPEN TRADES*\n\n"

            for t in trades:

                msg += (

                    f"{t['symbol']} "

                    f"{t['side']} "

                    f"{t['entry']}\n"

                )

            self.send(msg)

        elif cmd == "/pause":

            self.paused = True

            self.send("⏸ Scanner paused.")

        elif cmd == "/resume":

            self.paused = False

            self.send("▶ Scanner resumed.")

        elif cmd == "/stats":

            self.stats()

        else:

            self.send("Unknown command.")


telegram = TelegramBot()
