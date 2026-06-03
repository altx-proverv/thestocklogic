"""
ATLAS Reporting — Persistent Bot Listener
==========================================
Runs 24/7 as a background process on AWS.
Listens for Telegram commands at any time.
"""

import sys, requests, logging, time, signal as sig_module
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from atlas.reporting.telegram import send
from atlas.reporting.directives import handle_directive

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ATLAS-BOT] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/home/ubuntu/thestocklogic/reports/bot.log")
    ]
)
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
running = True

def handle_shutdown(signum, frame):
    global running
    log.info("Shutdown signal received")
    running = False

sig_module.signal(sig_module.SIGTERM, handle_shutdown)
sig_module.signal(sig_module.SIGINT, handle_shutdown)

def answer_callback(callback_id: str):
    """Acknowledge button tap to remove loading spinner."""
    try:
        requests.post(f"{BASE_URL}/answerCallbackQuery",
                     json={"callback_query_id": callback_id}, timeout=5)
    except Exception:
        pass


def get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=35)
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception as e:
        log.error(f"getUpdates error: {e}")
    return []

def process_update(update: dict) -> bool:
    # Handle inline button callbacks
    callback = update.get("callback_query", {})
    if callback:
        callback_id = callback.get("id", "")
        chat = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        data = callback.get("data", "")
        answer_callback(callback_id)
        if chat != str(TELEGRAM_CHAT_ID):
            return False
        now = datetime.now(IST).strftime("%H:%M IST")
        log.info(f"Button tap at {now}: {data}")
        if data.startswith("trade_"):
            text = f"/trade {data.replace('trade_','').upper()}"
        elif data.startswith("skip_"):
            text = f"/skip {data.replace('skip_','').upper()}"
        elif data.startswith("watch_"):
            text = f"/watch {data.replace('watch_','').upper()}"
        else:
            text = data
        response = handle_directive(text)
        if response:
            send(response)
        return True

    msg  = update.get("message", {})
    chat = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    if chat != str(TELEGRAM_CHAT_ID):
        return False
    if not text:
        return False
    now = datetime.now(IST).strftime("%H:%M IST")
    log.info(f"Directive received at {now}: {text}")
    # Handle Zerodha request token
    if len(text) > 20 and not text.startswith("/"):
        clean = text.replace("token=", "").strip()
        if len(clean) > 20 and clean.replace("-","").replace("_","").isalnum():
            from atlas.execution.zerodha_login import complete_login
            log.info(f"Completing Zerodha login: {clean[:10]}...")
            access_token = complete_login(clean)
            if access_token:
                send(f"✅ <b>ZERODHA LOGIN COMPLETE</b>\nHemal Dua authenticated\nTime: {now}\nATLAS ready to trade.")
            else:
                send("❌ <b>LOGIN FAILED</b>\nToken invalid or expired. Send /login to try again.")
            return True
    # Handle directives
    response = handle_directive(text)
    if response:
        send(response)
        return True
    send(f"❓ Unknown command: {text}\nSend /help for options.")
    return True

def run():
    log.info("ATLAS Bot Listener starting...")
    send("🤖 <b>ATLAS Bot Listener ONLINE</b>\nListening for directives 24/7\nSend /help for commands.")
    offset = None
    consecutive_errors = 0
    while running:
        try:
            updates = get_updates(offset)
            consecutive_errors = 0
            for update in updates:
                offset = update["update_id"] + 1
                process_update(update)
        except Exception as e:
            consecutive_errors += 1
            log.error(f"Polling error #{consecutive_errors}: {e}")
            if consecutive_errors > 10:
                time.sleep(60)
                consecutive_errors = 0
            else:
                time.sleep(5)
    log.info("Bot listener stopped.")

if __name__ == "__main__":
    run()
