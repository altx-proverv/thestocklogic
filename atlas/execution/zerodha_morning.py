"""
ATLAS Execution — Zerodha Morning Login Flow
=============================================
Runs at 8:30 AM IST daily.
1. Checks if today's token is already valid
2. If not — sends login URL to Telegram
3. Polls for your token reply for 10 minutes
4. Completes session and stores token
5. Confirms to Telegram when ready
"""

import sys, requests, logging, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
)
from atlas.reporting.telegram import send
from atlas.execution.zerodha_login import (
    get_stored_token, verify_token, complete_login, login
)

logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s [ATLAS-MORNING] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def get_updates(offset=None):
    params = {"timeout": 10, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=15)
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception as e:
        log.error(f"getUpdates error: {e}")
    return []


def extract_token(text: str) -> str:
    """Extract request token from user message."""
    text = text.strip()
    # Handle formats: raw token, token=XXX, ?token=XXX
    if "token=" in text:
        for part in text.split("&"):
            if "token=" in part:
                return part.split("token=")[-1].strip()
    # Raw token — alphanumeric string of ~32 chars
    if len(text) > 20 and text.replace("-", "").replace("_", "").isalnum():
        return text
    return ""


def poll_for_token(timeout_seconds: int = 600) -> str:
    """
    Poll Telegram for request token reply.
    Returns token string if received, empty string if timeout.
    """
    log.info(f"Polling for token reply ({timeout_seconds}s)...")
    offset   = None
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg    = update.get("message", {})
            chat   = str(msg.get("chat", {}).get("id", ""))
            text   = msg.get("text", "")

            if chat != str(TELEGRAM_CHAT_ID):
                continue
            if not text:
                continue

            token = extract_token(text)
            if token:
                log.info(f"Token received: {token[:10]}...")
                return token

        remaining = int(deadline - time.time())
        if remaining % 60 == 0 and remaining > 0:
            log.info(f"Still waiting for token... {remaining}s remaining")
        time.sleep(5)

    return ""


def run():
    """Main morning login flow."""
    now = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    log.info(f"Morning login check — {now}")

    # Step 1 — Check if token already valid
    token = get_stored_token()
    if token and verify_token(token):
        log.info("Existing token valid — no login needed")
        send(
            f"✅ <b>ATLAS ONLINE</b>\n"
            f"Zerodha connected · Token valid\n"
            f"Time: {now}\n"
            f"Capital at risk: ₹2,000 daily cap\n"
            f"Market opens in 45 minutes."
        )
        return True

    # Step 2 — Token invalid/expired — request new login
    login_url = login()
    if not login_url:
        send("❌ <b>ATLAS LOGIN FAILED</b>\nCould not generate login URL. Check API credentials.")
        return False

    send(
        f"🔐 <b>ZERODHA LOGIN REQUIRED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Tap the link below to login:\n"
        f"{login_url}\n\n"
        f"After login you will be redirected to thestocklogic.com\n"
        f"Copy the <b>token=XXXXX</b> value from the URL\n"
        f"and paste it here.\n\n"
        f"⏰ You have 10 minutes."
    )

    # Token is handled by bot_listener.py (running 24/7)
    # Just remind user to paste token here
    send(
        f"⏰ <b>PASTE TOKEN BEFORE 9:00 AM</b>\n"
        f"Copy the token=XXXXX from the redirect URL\n"
        f"and paste it directly in this chat.\n"
        f"ATLAS starts trading automatically once logged in."
    )
    log.info("Login URL sent — bot_listener will handle token response")
    return True


if __name__ == "__main__":
    run()
