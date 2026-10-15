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
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    MAX_RISK_PER_TRADE, MAX_NOTIONAL_PER_TRADE,   # noqa: F401 (see _rules_line)
)
from atlas.reporting.telegram import send
from atlas.reporting.directives import _rules_line


def _halt_line() -> str:
    """The halt file and the halt state, in the first message of the day.

    WHY THIS IS IN THE 08:30 MESSAGE. The halt used to resolve from an
    environment variable that THIS job does not set, so zerodha_morning read
    ~/.atlas/halt while the market-hours service read /var/lib/atlas/halt. Two
    processes, two answers to "is ATLAS halted", and nothing printed either
    path -- so the operator's only daily signal said nothing about the one
    control that stops the engine. Now it names the file and states the answer,
    and shouts if the path is not the canonical one.
    """
    from atlas.config import halt_file_state
    st = halt_file_state()
    if not st["is_canonical"]:
        return (f"\u26a0\ufe0f <b>HALT PATH WRONG</b> \u2014 resolved "
                f"<code>{st['path']}</code>, expected "
                f"<code>{st['canonical']}</code>. A halt written to the "
                f"canonical path would NOT stop the engine.")
    if st["halted"]:
        return (f"\U0001f6d1 <b>HALTED</b> \u2014 <code>{st['path']}</code>"
                + (f"\n{st['reason'][:160]}" if st["reason"] else "")
                + "\nNo entries will be placed. /resume or delete the file.")
    return f"\u25b6\ufe0f Not halted \u00b7 <code>{st['path']}</code>"


def _readiness_block() -> str:
    """Every switch that can stop an entry, and a verdict.

    WHY THE HALT LINE WAS NOT ENOUGH. On 2026-10-13 this message said
    "Not halted · /var/lib/atlas/halt" at 08:30 and it was true -- the halt FILE
    was clear. atlas_state.mode was PAUSED, so the session opened taking no
    entries, and the one message the operator reads before the open did not
    mention it. The halt file is one of a dozen switches; reporting it alone
    reads as a clean bill of health.

    NOT READY IS THE HEADLINE, not a footnote. If anything would refuse an
    entry, that goes first and the blockers are named with what set them.

    tools.atlas_switches never raises -- a probe that cannot read its state
    reports UNKNOWN and counts as blocking. So this cannot stop the 08:30
    message being sent, which is the message that would have to carry the news
    that it failed.
    """
    try:
        from tools.atlas_switches import inventory, readiness
        rows = inventory(include_signal_gates=False)
        ready, blockers = readiness(rows)
    except Exception as e:
        return (f"\u26a0\ufe0f <b>READINESS UNKNOWN</b> — the switch inventory "
                f"would not run ({type(e).__name__}: {e}). Run "
                f"<code>python3 -m tools.atlas_switches</code> on the box.")
    # THE ENTRY GATE'S INPUTS, ALWAYS, ready or not. Gate 1 is a live read as
    # of 2026-10-14 and at 08:30 there is no quote yet -- so what this line
    # carries before the open is the contract it will read and why it cannot
    # answer yet, which is still more than the old message said about the gate
    # that decides every entry.
    sent = next((r for r in rows if r["switch"] == "live sentiment (Gate 1)"),
                None)
    sent_lines = []
    if sent:
        sent_lines = [f"\U0001f4c8 <b>Gate 1 (live)</b>: {sent['value']}",
                      f"<code>{sent['detail'][:260]}</code>"]
    # THE RANGE BREAKER'S BOX, so a tightening box is visible before the open
    # rather than only after it breaks. At 08:30 it reads not-armed with no
    # box, which is the honest statement of where the session starts.
    rb = next((r for r in rows if r["switch"] == "range breaker (market)"), None)
    if rb:
        sent_lines += [f"\U0001f4e6 <b>Range breaker</b>: {rb['value']}",
                       f"<code>{rb['detail'][:300]}</code>"]
    blk = next((r for r in rows
                if r["switch"] == "range breaker (per symbol)"), None)
    if blk and blk["value"] != "none blocked":
        sent_lines += [f"\u26d4 <b>Symbols blocked</b>: {blk['value']} — "
                       f"<code>{blk['detail'][:200]}</code>"]

    # NO CONFIRM-WINDOW LINE. A 24-of-30 vote reported its tallies here for one
    # session; the vote was deleted on 2026-10-15 and the operator does not want
    # to read a number every morning that governs nothing. The stand-down is
    # still in tools/atlas_switches.py, where the complete switch list lives.

    if ready:
        head = ["\u2705 <b>READY</b> — no switch is blocking entries"]
        head += [f"\u00b7 {r['switch']}: {r['value']}" for r in rows
                 if r["switch"] in ("atlas_state.mode", "halt sentinel file",
                                    "LIVE_TRADING_ENABLED")]
        return "\n".join(head + sent_lines)

    out = [f"\U0001f6d1 <b>NOT READY</b> — {len(blockers)} switch(es) would "
           f"refuse every entry:"]
    for b in blockers:
        out.append(f"\u00b7 <code>{b[:220]}</code>")
    out.append("Nothing will be entered until these clear.")
    return "\n".join(out + sent_lines)
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
        # State the limits that actually gate an entry, read from config so
        # this cannot drift from them again. The line here used to read
        # "Capital at risk: Rs2,000 daily cap" -- a hardcoded literal, not a
        # constant, describing a daily loss cap that was removed along with
        # capital tracking. DAILY_LOSS_CAP_INR and DAILY_LOSS_CAP_PCT are gone
        # from config and kill_switch's P&L checks went with them, so nothing
        # enforced it. Telling the operator a cap is watching their downside
        # when none is is worse than saying nothing: drawdown and exits are
        # theirs to manage. See the same fix in telegram.py, which dropped
        # send_startup() for hardcoding "capital * 0.02".
        # ── THE TICK MAP, REFRESHED BEFORE THE OPEN ────────────────
        #
        # HERE because this job already has a live broker session before the
        # open, which is exactly what kite.instruments("NSE") needs, and
        # nothing else has to remember. Every protective price for the session
        # rounds on what this writes.
        #
        # A FAILURE HERE IS NOT FATAL: tick_for() falls back to the last known
        # tick per symbol, which is almost always correct because tick size
        # changes rarely. It is reported in the message either way.
        _tm = {"ok": False, "detail": "not attempted"}
        try:
            from atlas.execution.tick_map import refresh as _tick_refresh
            _tm = _tick_refresh()
        except Exception as e:
            _tm = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
        _tmline = (f"\U0001f4d0 Tick map: {_tm['symbols']} symbols, "
                   f"{_tm.get('ticks')}"
                   if _tm.get("ok") else
                   f"\u26a0\ufe0f <b>Tick map NOT refreshed</b> — "
                   f"{_tm.get('detail')}; protective prices will round on the "
                   f"last known tick per symbol")

        send(
            f"✅ <b>ATLAS ONLINE</b>\n"
            f"Zerodha connected · Token valid\n"
            f"Time: {now}\n"
            # ONE RENDERER FOR THE SIZING RULE, shared with /rules and the
            # report. This line used to hardcode the risk-budget pair, so when
            # sizing moved to fixed notional on 2026-10-06 the 08:30 message
            # went on saying "Risk/trade Rs3,000 - Max notional Rs1,00,000"
            # while the sizer used Rs10,000 notional and no risk budget at all.
            # Both numbers were ten- and tenfold wrong in opposite directions,
            # in the first message of the trading day. Rendering from the same
            # function the sizer's own mode selects means a future mode change
            # cannot leave this behind.
            f"{_rules_line()}\n"
            f"{_halt_line()}\n"
            f"{_readiness_block()}\n"
            f"{_tmline}\n"
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
