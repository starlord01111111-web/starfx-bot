import os
import time
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import config
from signals import scan
from watcher import start_watcher
from reports import start_scheduler
from telegram_bot import telegram


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"StarFX V8.1 PRO is running")

    def log_message(self, format, *args):
        return


# =====================================================
# Scanner Loop
# =====================================================

def scanner_loop():

    print("=" * 60)
    print("Scanner loop started")
    print("=" * 60)

    while True:

        try:

            if telegram.paused:
                print("Scanner is paused.")

            else:
                print("Running market scan...")
                scan()
                print("Market scan finished.")

        except Exception:

            print("Scanner crashed:")
            print(traceback.format_exc())

            telegram.send(
                "❌ Scanner Error\n\n"
                f"```{traceback.format_exc()}```"
            )

        print(f"Sleeping {config.SCAN_INTERVAL} seconds...\n")

        time.sleep(config.SCAN_INTERVAL)


# =====================================================
# Trading Bot
# =====================================================

def run_bot():

    try:

        print("=" * 60)
        print("Starting StarFX V8.1")
        print("=" * 60)

        telegram.send("🚀 StarFX V8.1 started")

        print("Starting scheduler...")
        start_scheduler()
        print("Scheduler started.")

        print("Starting watcher...")
        threading.Thread(
            target=start_watcher,
            daemon=True
        ).start()
        print("Watcher started.")

        print("Starting scanner...")
        scanner_loop()

    except Exception:

        error = traceback.format_exc()

        print(error)

        telegram.send(
            "❌ BOT CRASHED\n\n"
            f"```{error}```"
        )


# =====================================================
# Main
# =====================================================

if __name__ == "__main__":

    print("Launching bot thread...")

    bot_thread = threading.Thread(
        target=run_bot,
        daemon=True
    )

    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(
        ("0.0.0.0", port),
        Handler
    )

    print("=" * 60)
    print(f"HTTP server listening on port {port}")
    print("=" * 60)

    server.serve_forever()    


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), Handler)

    print(f"Listening on port {port}")

    run_bot()   # Run directly instead of in a thread
