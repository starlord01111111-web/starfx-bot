import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import config
from watcher import start_watcher
from reports import start_scheduler
from telegram_bot import telegram
from signals import scan


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"StarFX V8.1 PRO is running")

    def log_message(self, format, *args):
        return


def scanner_loop():
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Scanner error: {e}")

        time.sleep(config.SCAN_INTERVAL)


def run_bot():
    telegram.send(
        """🚀 StarFX V8.1 PRO

✅ Scanner Online
✅ Watcher Online
✅ Reports Scheduled
✅ Database Connected

Ready to scan.
"""
    )

    # Start scheduled reports
    start_scheduler()

    # Start trade watcher
    threading.Thread(target=start_watcher, daemon=True).start()

    # Start scanner
    scanner_loop()


if __name__ == "__main__":

    # Run trading bot
    threading.Thread(target=run_bot, daemon=True).start()

    # HTTP server for Render health checks
    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), Handler)

    print(f"Listening on port {port}")

    server.serve_forever()
