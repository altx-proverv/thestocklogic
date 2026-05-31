"""
THE STOCK LOGIC — Upstox Fully Automated Daily Login
=====================================================
Uses upstox-totp library for zero-human-intervention token generation.
Stores token in Supabase + local file for all session crons.

Cron: 0 3 * * 1-5 (8:30 AM IST = 3:00 UTC) Mon-Fri
Run:  python3 engine/upstox_auto_login.py
"""

import os, sys, json, logging, requests
from pathlib import Path
from datetime import date

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TOKEN_FILE   = Path("data/upstox_token.json")
SUPABASE_URL = os.environ.get("SUPABASE_URL",
               "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def get_token() -> str:
    from upstox_totp import UpstoxTOTP
    upx = UpstoxTOTP(
        username      = os.environ["UPSTOX_MOBILE"],
        password      = os.environ["UPSTOX_MOBILE"],
        pin_code      = os.environ["UPSTOX_PIN"],
        totp_secret   = os.environ["UPSTOX_TOTP_SECRET"],
        client_id     = os.environ["UPSTOX_API_KEY"],
        client_secret = os.environ["UPSTOX_API_SECRET"],
        redirect_uri  = "https://thestocklogic.com/callback",
    )
    response = upx.app_token.get_access_token()
    if response.success and response.data:
        log.info(f"Token received. User: {response.data.user_name}")
        return response.data.access_token
    log.error(f"Token generation failed: {response}")
    return None


def store_token(token: str):
    today = date.today().isoformat()

    # Local file
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": token, "date": today}, f)
    log.info(f"Token saved locally")

    # Supabase
    if SUPABASE_KEY:
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates",
        }
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/upstox_tokens?token_date=eq.{today}",
            headers=headers
        )
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/upstox_tokens",
            headers=headers,
            json={"token_date": today, "access_token": token}
        )
        if r.status_code in (200, 201):
            log.info("Token stored in Supabase")
        else:
            log.warning(f"Supabase store failed: {r.status_code} — {r.text[:100]}")


def verify_token(token: str) -> bool:
    r = requests.get(
        "https://api.upstox.com/v2/user/profile",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=10
    )
    if r.status_code == 200:
        name = r.json().get("data", {}).get("user_name", "Unknown")
        log.info(f"Token verified. User: {name}")
        return True
    log.error(f"Token invalid: {r.status_code}")
    return False


def main():
    log.info("="*50)
    log.info("UPSTOX AUTO LOGIN")
    log.info("="*50)

    os.chdir(Path(__file__).parent.parent)

    token = get_token()
    if not token:
        log.error("Failed to get token")
        sys.exit(1)

    store_token(token)

    if verify_token(token):
        log.info("AUTO LOGIN COMPLETE ✓")
    else:
        log.error("Token verification failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
