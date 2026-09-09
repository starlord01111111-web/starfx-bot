from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from config import config
from database import db
from telegram_bot import telegram


class Reports:

    def __init__(self):

        self.scheduler = BackgroundScheduler(
            timezone=config.TIMEZONE
        )

    # --------------------------------------------

    def statistics(self):

        conn = db.conn

        cur = conn.cursor()

        cur.execute("""

        SELECT result

        FROM trades

        WHERE status='CLOSED'

        """)

        rows = cur.fetchall()

        wins = 0
        losses = 0
        expired = 0

        for row in rows:

            result = row["result"]

            if result == "TP2":
                wins += 1

            elif result == "SL":
                losses += 1

            elif result == "EXPIRED":
                expired += 1

        total = wins + losses

        winrate = 0

        if total > 0:

            winrate = round(
                wins * 100 / total,
                2
            )

        return {

            "wins": wins,

            "losses": losses,

            "expired": expired,

            "total": total,

            "winrate": winrate

        }

    # --------------------------------------------

    def daily(self):

        s = self.statistics()

        telegram.send(
f"""
📊 *Daily Report*

Trades: {s['total']}

Wins: {s['wins']}

Losses: {s['losses']}

Expired: {s['expired']}

Win Rate: {s['winrate']}%
"""
        )

    # --------------------------------------------

    def weekly(self):

        s = self.statistics()

        telegram.send(
f"""
📈 *Weekly Report*

Trades: {s['total']}

Wins: {s['wins']}

Losses: {s['losses']}

Win Rate: {s['winrate']}%
"""
        )

    # --------------------------------------------

    def monthly(self):

        now = datetime.now(config.TIMEZONE)

        s = self.statistics()

        telegram.send(
f"""
🗓 *Monthly Report*

Month: {now.strftime("%B %Y")}

Trades: {s['total']}

Wins: {s['wins']}

Losses: {s['losses']}

Win Rate: {s['winrate']}%
"""
        )

    # --------------------------------------------

    def heartbeat(self):

        telegram.heartbeat()

    # --------------------------------------------

    def start(self):

        self.scheduler.add_job(
            self.daily,
            "cron",
            hour=23,
            minute=0
        )

        self.scheduler.add_job(
            self.weekly,
            "cron",
            day_of_week="sun",
            hour=23,
            minute=30
        )

        self.scheduler.add_job(
            self.monthly,
            "cron",
            day=1,
            hour=23,
            minute=45
        )

        self.scheduler.add_job(
            self.heartbeat,
            "cron",
            hour=9,
            minute=0
        )

        self.scheduler.start()


reports = Reports()
def start_scheduler():
    reports.start()
