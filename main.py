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

    print(">>> Scanner loop started")

    while True:

        print(">>> Calling scan()")

        try:
            scan()
            print(">>> Scan completed")
        except Exception as e:
            print(f">>> Scan error: {e}")

        time.sleep(config.SCAN_INTERVAL)
    
        
            
        
            

        



def run_bot():
    print(">>> run_bot started")

    try:
        print(">>> Sending startup message")
        telegram.send("🚀 StarFX V8.1 started")

        print(">>> Starting scheduler")
        start_scheduler()

        print(">>> Starting watcher")
        threading.Thread(
            target=start_watcher,
            daemon=True
        ).start()

        print(">>> Starting scanner")
        scanner_loop()

    except Exception:
        print(traceback.format_exc())
        telegram.send(
            "❌ BOT CRASHED\n\n" + traceback.format_exc()
        )


        







    

    
    

    
    

    
    


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), Handler)

    print(f"Listening on port {port}")

    run_bot()   # Run directly instead of in a thread
