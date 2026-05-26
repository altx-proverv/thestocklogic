"""
THE STOCK LOGIC — Upstox OAuth Token Manager
=============================================
Handles daily token generation and storage.

Flow:
  1. Generate login URL
  2. User visits URL, logs in, gets redirected to callback
  3. Capture auth code from callback URL
  4. Exchange code for access token
  5. Store token for WebSocket use

Run once daily before market opens:
  python3 engine/upstox_auth.py --login    # prints login URL
  python3 engine/upstox_auth.py --exchange CODE  # exchanges code for token
  python3 engine/upstox_auth.py --check    # checks if token is valid
"""

import os, sys, json, logging, requests
from pathlib import Path
from datetime import datetime, date

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

API_KEY      = os.environ.get("UPSTOX_API_KEY", "")
API_SECRET   = os.environ.get("UPSTOX_API_SECRET", "")
REDIRECT_URI = os.environ.get("UPSTOX_REDIRECT_URI", "https://thestocklogic.com/callback")
TOKEN_FILE   = Path("data/upstox_token.json")

BASE_URL     = "https://api.upstox.com/v2"
AUTH_URL     = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL    = "https://api.upstox.com/v2/login/authorization/token"


def get_login_url() -> str:
    """Generate the Upstox OAuth login URL."""
    params = {
        "client_id":     API_KEY,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
    }
    from urllib.parse import urlencode
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(auth_code: str) -> dict:
    """Exchange auth code for access token."""
    payload = {
        "code":          auth_code,
        "client_id":     API_KEY,
        "client_secret": API_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept":       "application/json",
    }

    r = requests.post(TOKEN_URL, data=payload, headers=headers)
    if r.status_code != 200:
        log.error(f"Token exchange failed: {r.status_code} — {r.text}")
        return {}

    token_data = r.json()
    token_data["date"] = date.today().isoformat()

    # Save token
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    log.info(f"Token saved to {TOKEN_FILE}")
    log.info(f"Token type: {token_data.get('token_type')}")
    log.info(f"Expires in: {token_data.get('expires_in')} seconds")
    return token_data


def load_token() -> str:
    """Load today's access token."""
    if not TOKEN_FILE.exists():
        return None

    with open(TOKEN_FILE) as f:
        data = json.load(f)

    # Check if token is from today
    token_date = data.get("date", "")
    if token_date != date.today().isoformat():
        log.warning(f"Token is from {token_date}, not today. Need to re-authenticate.")
        return None

    return data.get("access_token")


def check_token() -> bool:
    """Verify token is valid by calling profile endpoint."""
    token = load_token()
    if not token:
        log.warning("No valid token found for today")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    }

    r = requests.get(f"{BASE_URL}/user/profile", headers=headers)
    if r.status_code == 200:
        profile = r.json()
        name = profile.get("data", {}).get("user_name", "Unknown")
        log.info(f"Token valid. User: {name}")
        return True
    else:
        log.error(f"Token invalid: {r.status_code} — {r.text}")
        return False


def get_headers() -> dict:
    """Get auth headers for API calls."""
    token = load_token()
    if not token:
        raise ValueError("No valid token. Run: python3 engine/upstox_auth.py --login")
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    }


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)

    if not API_KEY or not API_SECRET:
        log.error("UPSTOX_API_KEY and UPSTOX_API_SECRET must be set")
        sys.exit(1)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "--help"

    if cmd == "--login":
        url = get_login_url()
        print(f"\n{'='*60}")
        print("UPSTOX LOGIN")
        print(f"{'='*60}")
        print(f"\nOpen this URL in your browser:\n")
        print(url)
        print(f"\nAfter login, copy the 'code' from the callback URL:")
        print(f"https://thestocklogic.com/callback?code=XXXX")
        print(f"\nThen run:")
        print(f"python3 engine/upstox_auth.py --exchange XXXX")
        print(f"{'='*60}\n")

    elif cmd == "--exchange":
        if len(sys.argv) < 3:
            log.error("Usage: python3 engine/upstox_auth.py --exchange AUTH_CODE")
            sys.exit(1)
        code = sys.argv[2]
        token = exchange_code(code)
        if token:
            log.info("Token exchange successful")
            check_token()
        else:
            log.error("Token exchange failed")

    elif cmd == "--check":
        valid = check_token()
        sys.exit(0 if valid else 1)

    else:
        print("Usage:")
        print("  python3 engine/upstox_auth.py --login")
        print("  python3 engine/upstox_auth.py --exchange AUTH_CODE")
        print("  python3 engine/upstox_auth.py --check")
