import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import config
from watcher import watcher
from reports import reports
from telegram_bot import telegram
from signals import while True:
    # call your scan function here
    time.sleep(config.SCAN_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"StarFX V8.1 PRO is running")

    def log_message(self, format, *args):
        return


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

    start_scheduler()

    threading.Thread(target=start_watcher, daemon=True).start()

    scan_loop()


if __name__ == "__main__":

    threading.Thread(target=run_bot, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), Handler)

    print(f"Listening on port {port}")

    server.serve_forever()
