"""
ATLAS Execution — Zerodha Auto Login
=====================================
Automates Zerodha Kite Connect login using TOTP.
Stores access token in Supabase for use by broker.py.
Runs once daily before market open.
"""

import os, sys, requests, logging, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    ZERODHA_API_KEY, ZERODHA_API_SECRET,
    ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP
)

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def generate_totp(secret: str) -> str:
    """Generate TOTP code from secret."""
    try:
        import pyotp
        return pyotp.TOTP(secret).now()
    except ImportError:
        log.error("pyotp not installed — run: pip install pyotp")
        return ""


def login() -> str:
    """
    Perform Zerodha Kite Connect login.
    Returns access token if successful.
    
    Note: Zerodha requires manual login URL visit for first-time setup.
    After that, use request_token from callback URL.
    """
    if not ZERODHA_API_KEY or not ZERODHA_API_SECRET:
        log.error("Zerodha API credentials not configured")
        return ""

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=ZERODHA_API_KEY)

        # Generate login URL
        login_url = kite.login_url()
        log.info(f"Zerodha login URL: {login_url}")

        return login_url

    except Exception as e:
        log.error(f"Zerodha login failed: {e}")
        return ""


def complete_login(request_token: str) -> str:
    """
    Complete login with request_token from callback URL.
    Call this after visiting login URL and getting redirected.
    Returns access token.
    """
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=ZERODHA_API_KEY)
        data = kite.generate_session(request_token, api_secret=ZERODHA_API_SECRET)
        access_token = data["access_token"]

        # Store in Supabase
        store_token(access_token, data.get("user_id", ZERODHA_USER_ID))
        log.info(f"Zerodha login complete. User: {data.get('user_name', '')}")
        return access_token

    except Exception as e:
        log.error(f"Session generation failed: {e}")
        return ""


def store_token(access_token: str, user_id: str = ""):
    """Store Zerodha access token in Supabase."""
    record = {
        "broker":       "zerodha",
        "access_token": access_token,
        "user_id":      user_id or ZERODHA_USER_ID,
        "created_at":   datetime.now(IST).isoformat(),
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/broker_tokens",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=record
    )
    if r.status_code in (200, 201, 204):
        log.info("Zerodha token stored in Supabase")
    else:
        log.error(f"Token storage failed: {r.status_code} {r.text[:100]}")


def get_stored_token() -> str:
    """Retrieve stored Zerodha token."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/broker_tokens"
        f"?broker=eq.zerodha&order=created_at.desc&limit=1",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        return r.json()[0].get("access_token", "")
    return ""


def verify_token(access_token: str) -> bool:
    """Verify if stored token is still valid."""
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=ZERODHA_API_KEY)
        kite.set_access_token(access_token)
        profile = kite.profile()
        log.info(f"Token valid. User: {profile.get('user_name', '')}")
        return True
    except Exception as e:
        log.warning(f"Token invalid: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s [ATLAS-ZERODHA] %(message)s")

    import sys
    if len(sys.argv) > 1:
        request_token = sys.argv[1]
        log.info(f"Completing login with request token: {request_token[:10]}...")
        token = complete_login(request_token)
        if token:
            print(f"Login successful. Token stored.")
        else:
            print("Login failed.")
    else:
        # Check existing token
        token = get_stored_token()
        if token and verify_token(token):
            print("Existing token valid — no login needed")
        else:
            # Generate login URL
            url = login()
            print(f"\nVisit this URL to login:")
            print(url)
            print(f"\nAfter login, you will be redirected to:")
            print(f"https://thestocklogic.com/callback?request_token=XXXXX")
            print(f"\nRun: python3 atlas/execution/zerodha_login.py <request_token>")
