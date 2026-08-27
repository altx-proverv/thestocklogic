"""
THE STOCK LOGIC — Zerodha Fully Automated Daily Login
=====================================================
Pure-HTTP Kite Connect login with a TOTP second factor. No browser, no
human. Mirrors engine/upstox_auto_login.py: run it on a cron before the
market opens and the day's access token is waiting in Supabase.

Flow:
  1. GET  kite.login_url()                    — seeds the session cookies
  2. POST /api/login  {user_id, password}     — returns request_id
  3. POST /api/twofa  {..., twofa_value}      — TOTP from pyotp
  4. GET  login_url + "&skip_session=true"    — redirects to the callback
  5. request_token out of the redirect chain
  6. kite.generate_session()                  — access_token
  7. store in broker_tokens (atlas.execution.zerodha_login.store_token)

This is the primary path. It is deliberately NOT the only one: any
failure — including a rejected broker_tokens write — exits non-zero,
skips the success line, and reports to Telegram, so the 08:30 IST
zerodha_morning.py run finds nothing valid and falls back to asking for a
request_token over Telegram. Failing loudly is what arms that fallback.
A generated token that was not stored counts as a failure: nothing
downstream reads it from anywhere but broker_tokens, so it is lost.

Cron: 55 2 * * 1-5 (8:25 AM IST = 2:55 UTC) Mon-Fri — 5 minutes ahead of
      the 03:00 UTC zerodha_morning check.
Run:  python3 engine/zerodha_auto_login.py

Env:  ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID,
      ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET
"""

import os, re, sys, time, logging
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from atlas.config import (
    SUPABASE_KEY,
    ZERODHA_API_KEY, ZERODHA_API_SECRET,
    ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP,
)
from atlas.execution.zerodha_login import store_token, verify_token
from atlas.reporting.telegram import send

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

KITE_API = "https://kite.zerodha.com/api"
TIMEOUT  = 20

# request_token as it appears in a raw string — used only when the token
# is not in a parseable query, i.e. buried in an exception message.
TOKEN_RE = re.compile(r"request_token=([A-Za-z0-9_-]+)")


# ══════════════════════════════════════════════════════════════════════
# token extraction
# ══════════════════════════════════════════════════════════════════════

def token_from_url(url: str) -> str:
    """request_token from a URL's query string. '' if not present."""
    try:
        values = parse_qs(urlparse(url).query).get("request_token") or []
    except Exception:
        return ""
    return values[0].strip() if values and values[0].strip() else ""


def token_from_text(text: str) -> str:
    """request_token from arbitrary text — an exception message, a body."""
    match = TOKEN_RE.search(text or "")
    return match.group(1) if match else ""


# ══════════════════════════════════════════════════════════════════════
# login steps
# ══════════════════════════════════════════════════════════════════════

def totp_now(secret: str) -> str:
    """
    Current TOTP code. Waits out the tail of a 30-second window first: a
    code generated with 2 seconds left is very likely to be rejected by
    the time the POST lands, and this runs unattended.
    """
    import pyotp
    totp      = pyotp.TOTP(secret)
    remaining = totp.interval - (int(time.time()) % totp.interval)
    if remaining <= 3:
        log.info(f"TOTP window closing in {remaining}s — waiting for the next one")
        time.sleep(remaining + 1)
    return totp.now()


def post(session: requests.Session, path: str, payload: dict, label: str) -> dict:
    """POST to a kite endpoint and return data, raising on any failure."""
    r = session.post(f"{KITE_API}/{path}", data=payload, timeout=TIMEOUT)
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(
            f"{label} returned non-JSON (HTTP {r.status_code}): {r.text[:300]}"
        )
    if r.status_code != 200 or body.get("status") != "success":
        raise RuntimeError(
            f"{label} failed (HTTP {r.status_code}): "
            f"{body.get('message') or body}"
        )
    return body.get("data") or {}


def fetch_request_token(session: requests.Session, login_url: str) -> str:
    """
    Step 4-5. Follow the post-2FA redirect to the registered callback and
    pull request_token out of it.

    Two failure modes are documented and both are real:

      A. The final URL has no request_token. The callback host may redirect
         onward, or answer with a page — the token was on an earlier hop.
         So every URL in the chain is checked, not just resp.url.

      B. requests never reaches a final URL: the callback host is
         unreachable or the scheme is one it will not follow, and it
         raises. The redirect target, request_token and all, is in the
         exception message and nowhere else.
    """
    url = f"{login_url}&skip_session=true"

    try:
        resp = session.get(url, allow_redirects=True, timeout=TIMEOUT)
    except Exception as e:                                        # mode B
        token = token_from_text(f"{e}")
        if token:
            log.info("request_token recovered from the redirect exception")
            return token
        raise RuntimeError(
            f"Redirect to the callback failed and its error carried no "
            f"request_token: {type(e).__name__}: {e}"
        ) from e

    chain = [h.url for h in resp.history] + [resp.url]
    for hop in chain:                                             # mode A
        token = token_from_url(hop)
        if token:
            return token

    # Not in any query. Some callbacks render the redirect URL into the page.
    token = token_from_text(resp.text)
    if token:
        log.info("request_token recovered from the response body")
        return token

    raise RuntimeError(
        "No request_token anywhere in the login redirect.\n"
        f"  HTTP {resp.status_code} · {len(resp.history)} redirect(s)\n"
        f"  chain: {' -> '.join(chain)}\n"
        f"  body:  {resp.text[:500]}"
    )


def get_access_token() -> tuple:
    """Run the whole login. Returns (access_token, user_id)."""
    from kiteconnect import KiteConnect

    kite      = KiteConnect(api_key=ZERODHA_API_KEY)
    login_url = kite.login_url()

    session = requests.Session()
    session.headers.update({
        "User-Agent":     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "X-Kite-Version": "3",
    })

    # 1 — seed cookies
    session.get(login_url, timeout=TIMEOUT)

    # 2 — password
    data = post(session, "login", {
        "user_id":  ZERODHA_USER_ID,
        "password": ZERODHA_PASSWORD,
    }, "Password login")

    request_id = data.get("request_id")
    if not request_id:
        raise RuntimeError(f"Password login returned no request_id: {data}")
    log.info("Password accepted")

    # 3 — TOTP
    post(session, "twofa", {
        "user_id":      ZERODHA_USER_ID,
        "request_id":   request_id,
        "twofa_value":  totp_now(ZERODHA_TOTP),
        "twofa_type":   "totp",
        "skip_session": "true",
    }, "TOTP 2FA")
    log.info("TOTP accepted")

    # 4, 5 — request_token
    request_token = fetch_request_token(session, login_url)
    log.info(f"request_token obtained: {request_token[:6]}...")

    # 6 — exchange for an access token
    session_data = kite.generate_session(request_token,
                                         api_secret=ZERODHA_API_SECRET)
    log.info(f"Session generated. User: {session_data.get('user_name', '')}")
    return session_data["access_token"], session_data.get("user_id", "")


def check_config():
    """
    Every credential must be present — a blank one fails deep and vague.

    SUPABASE_SERVICE_KEY is checked here with the rest even though it is not
    a login credential, because it is only needed at the very last step. Left
    unchecked, a blank one lets the whole login succeed and then 401s the
    broker_tokens write. It lives in the crontab header and NOT in an
    interactive shell, so that is the normal outcome of a hand-run.
    """
    missing = [name for name, value in (
        ("ZERODHA_API_KEY",      ZERODHA_API_KEY),
        ("ZERODHA_API_SECRET",   ZERODHA_API_SECRET),
        ("ZERODHA_USER_ID",      ZERODHA_USER_ID),
        ("ZERODHA_PASSWORD",     ZERODHA_PASSWORD),
        ("ZERODHA_TOTP_SECRET",  ZERODHA_TOTP),
        ("SUPABASE_SERVICE_KEY", SUPABASE_KEY),
    ) if not value]
    if missing:
        log.error(f"Missing credentials: {', '.join(missing)}")
        sys.exit(1)


def fail(reason: str):
    """
    Exit non-zero without printing the success line, and say so on Telegram.

    The log alone is not enough. zerodha_morning will ask the operator for a
    manual login 5 minutes later, and if atlas.log claims this run succeeded
    that request looks spurious and gets ignored — which is how a stale
    broker_tokens row turns into a silent no-trade day. This message is what
    makes the fallback visibly armed rather than merely armed.
    """
    log.error(f"AUTO LOGIN FAILED — {reason}")
    sent = send(
        "❌ <b>ZERODHA AUTO-LOGIN FAILED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{reason}\n\n"
        "No usable token was stored. The 08:30 check will ask you to log in "
        "manually — that request is real, please action it."
    )
    if not sent:
        log.error("Telegram notification did not send — failure is log-only")
    sys.exit(1)


def main():
    log.info("=" * 50)
    log.info("ZERODHA AUTO LOGIN")
    log.info("=" * 50)

    os.chdir(Path(__file__).parent.parent)
    check_config()

    try:
        access_token, user_id = get_access_token()
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")

    # 7 — same broker_tokens write the Telegram path uses. Storage is NOT
    # best-effort: a token that was generated but not stored is simply lost,
    # because nothing downstream reads it from anywhere else.
    try:
        stored = store_token(access_token, user_id or ZERODHA_USER_ID)
    except Exception as e:
        fail(f"broker_tokens write raised {type(e).__name__}: {e}")

    if not stored:
        fail("broker_tokens write was rejected by Supabase (status logged "
             "above). A token was generated but not saved.")

    if not verify_token(access_token):
        fail("token stored but it failed verification against Kite")

    log.info("AUTO LOGIN COMPLETE ✓")


if __name__ == "__main__":
    main()
