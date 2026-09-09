import sqlite3
from datetime import datetime, timedelta

from config import config


class Database:

    def __init__(self):

        self.conn = sqlite3.connect(
            config.DATABASE,
            check_same_thread=False
        )

        self.conn.row_factory = sqlite3.Row

        self.create_tables()

    def create_tables(self):

        cur = self.conn.cursor()

        cur.execute("""

        CREATE TABLE IF NOT EXISTS trades(

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            symbol TEXT,

            side TEXT,

            entry REAL,

            sl REAL,

            tp1 REAL,

            tp2 REAL,

            tp1_hit INTEGER DEFAULT 0,

            opened TEXT,

            closed TEXT,

            result TEXT,

            rr REAL,

            status TEXT

        )

        """)

        cur.execute("""

        CREATE TABLE IF NOT EXISTS signals(

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            symbol TEXT,

            side TEXT,

            signal_time TEXT

        )

        """)

        self.conn.commit()

    def save_trade(self, trade):

        cur = self.conn.cursor()

        cur.execute("""

        INSERT INTO trades(

            symbol,

            side,

            entry,

            sl,

            tp1,

            tp2,

            opened,

            status

        )

        VALUES(?,?,?,?,?,?,?,?)

        """, (

            trade["symbol"],

            trade["side"],

            trade["entry"],

            trade["sl"],

            trade["tp1"],

            trade["tp2"],

            datetime.utcnow().isoformat(),

            "OPEN"

        ))

        self.conn.commit()

    def open_trades(self):

        cur = self.conn.cursor()

        cur.execute(

            "SELECT * FROM trades WHERE status='OPEN'"

        )

        return [dict(r) for r in cur.fetchall()]

    def close_trade(self, trade_id, result):

        cur = self.conn.cursor()

        cur.execute("""

        UPDATE trades

        SET

        status='CLOSED',

        result=?,

        closed=?

        WHERE id=?

        """, (

            result,

            datetime.utcnow().isoformat(),

            trade_id

        ))

        self.conn.commit()

    def duplicate_trade(self, symbol):

        cur = self.conn.cursor()

        cur.execute("""

        SELECT id

        FROM trades

        WHERE symbol=?

        AND status='OPEN'

        """, (symbol,))

        return cur.fetchone() is not None

    def cooldown_ok(self, symbol, side, minutes):

        cur = self.conn.cursor()

        cur.execute("""

        SELECT signal_time

        FROM signals

        WHERE symbol=?

        AND side=?

        ORDER BY id DESC

        LIMIT 1

        """, (symbol, side))

        row = cur.fetchone()

        if row is None:
            return True

        last = datetime.fromisoformat(row["signal_time"])

        return datetime.utcnow() - last > timedelta(minutes=minutes)

    def register_signal(self, symbol, side):

        cur = self.conn.cursor()

        cur.execute("""

        INSERT INTO signals(

        symbol,

        side,

        signal_time

        )

        VALUES(?,?,?)

        """, (

            symbol,

            side,

            datetime.utcnow().isoformat()

        ))

        self.conn.commit()


db = Database()
