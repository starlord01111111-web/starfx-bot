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


# -----------------------------------------------------
# Scanner Loop
# -----------------------------------------------------

def scanner_loop():

    print(">>> Scanner loop started")

    while True:

        try:

            if telegram.paused:
                print("Scanner paused...")
            else:
                print(">>> Running market scan...")
                scan()

        except Exception:
            print(traceback.format_exc())

        time.sleep(config.SCAN_INTERVAL)


# -----------------------------------------------------
# Trading Bot
# -----------------------------------------------------

def run_bot():

    try:

        print("Starting StarFX V8.1...")

        telegram.send("🚀 StarFX V8.1 started")

        # Start scheduled reports
        start_scheduler()

        # Start trade watcher
        threading.Thread(
            target=start_watcher,
            daemon=True
        ).start()

        # Start scanner
        scanner_loop()

    except Exception:

        error = traceback.format_exc()

        print(error)

        telegram.send(
            "❌ BOT CRASHED\n\n"
            f"```{error}```"
        )


# -----------------------------------------------------
# Main
# -----------------------------------------------------

if __name__ == "__main__":

    # Start bot in background
    threading.Thread(
        target=run_bot,
        daemon=True
    ).start()

    # HTTP server for Render health checks
    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), Handler)

    print(f"Listening on port {port}")

    server.serve_forever()




    

    
    

    
    

    
    


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), Handler)

    print(f"Listening on port {port}")

    run_bot()   # Run directly instead of in a thread
