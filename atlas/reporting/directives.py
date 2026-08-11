"""
ATLAS Reporting — Telegram Directive Handler
=============================================
Listens for your Telegram commands and updates agent state.
Commands: /approve /pause /cautious /aggressive /normal /status /help
Runs as a polling loop — call once after daily report is sent.
"""

import sys, requests, logging, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    AGENT_MODES, DEFAULT_AGENT_MODE, HALT_MODES,
    MAX_RISK_PER_TRADE, MAX_NOTIONAL_PER_TRADE, MAX_TRADES_PER_DAY,
)
from atlas.reporting.telegram import send

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ATLAS-DIRECTIVE] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def get_updates(offset=None):
    """Fetch new Telegram messages."""
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


def get_agent_state():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_state?limit=1&order=updated_at.desc",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        return r.json()[0]
    return {"mode": DEFAULT_AGENT_MODE, "id": 1}


def update_agent_mode(mode: str, notes: str = "") -> bool:
    """Update agent mode in Supabase."""
    state = get_agent_state()
    state_id = state.get("id", 1)
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/atlas_state?id=eq.{state_id}",
        headers=_headers(),
        json={
            "mode":       mode,
            "notes":      notes,
            "updated_at": datetime.now(IST).isoformat(),
        }
    )
    return r.status_code in (200, 204)


def _rules_line() -> str:
    """The complete rule set, rendered from config. Nothing hardcoded."""
    return (f"Risk/trade ₹{MAX_RISK_PER_TRADE:,.0f} · "
            f"max notional ₹{MAX_NOTIONAL_PER_TRADE:,.0f} · "
            f"max {MAX_TRADES_PER_DAY} entries/day")


def handle_directive(text: str) -> str:
    """
    Process a directive command.
    Returns response message to send back.
    """
    text = text.strip().lower()
    now  = datetime.now(IST).strftime("%d %b %Y %H:%M IST")

    # Mode replies no longer quote per-mode sizing or conviction. Position size
    # comes solely from the Rs3,000 risk budget and the stop distance, and the
    # only trade limits are MAX_TRADES_PER_DAY and live broker funds -- so
    # "Position size: 70% of normal" described a lever that does not exist.
    if text in ["/approve", "approve"]:
        state = get_agent_state()
        mode  = state.get("mode", DEFAULT_AGENT_MODE)
        update_agent_mode(mode, f"Approved by directive at {now}")
        return (
            f"✅ <b>APPROVED</b>\n"
            f"Agent proceeds in <b>{mode}</b> mode\n"
            f"{_rules_line()}"
        )

    elif text in ["/pause", "pause"]:
        update_agent_mode("PAUSED", f"Paused by directive at {now}")
        return (
            "⏸ <b>AGENT PAUSED</b>\n"
            "No new entries will be taken.\n"
            "Send /approve or /normal to resume."
        )

    elif text.lstrip("/") in ("cautious", "aggressive", "normal", "defensive"):
        target = text.lstrip("/").upper()
        icon = {"CAUTIOUS": "🟡", "AGGRESSIVE": "🔴",
                "NORMAL": "🔵", "DEFENSIVE": "🛡"}[target]
        update_agent_mode(target, f"Set to {target} by directive at {now}")
        return (
            f"{icon} <b>{target} MODE SET</b>\n"
            f"Recorded as operator intent. Only PAUSED changes agent behaviour.\n"
            f"{_rules_line()}"
        )

    elif text in ["/status", "status"]:
        state = get_agent_state()
        mode  = state.get("mode", DEFAULT_AGENT_MODE)
        halted = " (HALTED)" if mode in HALT_MODES else ""
        return (
            f"📊 <b>ATLAS STATUS</b>\n"
            f"Mode:    {mode}{halted}\n"
            f"{_rules_line()}\n"
            f"Time:    {now}\n"
            f"Send /capital for live broker funds."
        )

    elif text in ["/login", "login"]:
        from atlas.execution.zerodha_login import login as gen_login_url
        from atlas.execution.zerodha_login import get_stored_token, verify_token
        token = get_stored_token()
        if token and verify_token(token):
            return "✅ <b>Already logged in</b>\nZerodha token is valid. No action needed."
        url = gen_login_url()
        if url:
            return (
                f"🔐 <b>ZERODHA LOGIN</b>\n"
                f"Tap to login:\n{url}\n\n"
                f"After redirect, paste the token=XXXXX value here."
            )
        return "❌ Could not generate login URL. Check API credentials."

    elif text in ["/capital", "capital", "/funds", "funds"]:
        # Live from the broker. There is no allocated pool or deployed ledger
        # to report -- those were a stored copy of this number that drifted.
        from atlas.risk.funds import available_funds, FundsUnavailable
        try:
            f = available_funds()
        except FundsUnavailable as e:
            return (f"⚠️ <b>FUNDS UNAVAILABLE</b>\n{e}\n\n"
                    f"Every entry is BLOCKED while this persists (fail closed).")
        return (
            f"💰 <b>LIVE BROKER FUNDS</b>\n"
            f"Broker balance:  ₹{f['margin']:,.0f}\n"
            f"Resting GTTs:   -₹{f['gtt_committed']:,.0f}\n"
            f"Available:       ₹{f['available']:,.0f}\n\n"
            f"{_rules_line()}"
        )

    elif text.startswith("/trade") or text.startswith("/skip") or text.startswith("/watch"):
        # These drove atlas.execution.trade_executor, which is retired and has
        # been removed. They could not have worked in any case: PENDING_TRADES
        # was only ever assigned {} and .clear()ed, so /trade always answered
        # "No pending trade", and /watch read pending["expires_at"] and
        # pending["sizing"] -- keys nothing ever set.
        #
        # Worse, the import raised ImportError (trade_executor imported
        # `calculate, validate` from position_sizing; neither exists). That
        # propagated out of handle_directive into bot_listener.run()'s except,
        # which logged "Polling error" and slept 5s -- after the offset had
        # already advanced, so every other update in the same batch was lost.
        return ("Manual trade commands are not available in this phase.\n"
                "ATLAS enters autonomously at 09:37 via market_open.\n"
                "Use /positions to see open trades, /pause to stop trading.")

    elif text in ["/positions", "positions"]:
        import requests as _req, os
        url = "https://eibdlcanpudjgmkjxrga.supabase.co"
        key = os.environ.get("SUPABASE_SERVICE_KEY","")
        r = _req.get(f"{url}/rest/v1/atlas_trades?status=eq.OPEN&order=created_at.desc",
            headers={"apikey":key,"Authorization":f"Bearer {key}"})
        trades = r.json() if r.status_code == 200 else []
        if not trades:
            return "No open positions"
        out = "OPEN POSITIONS"
        for t in trades:
            sym   = t.get("symbol","")
            dirn  = t.get("direction","")
            entry = float(t.get("entry_price",0))
            sl    = float(t.get("sl",0))
            t1    = float(t.get("target_1",0))
            out  += f"\n{sym} {dirn} Entry:Rs{entry:.1f} SL:Rs{sl:.1f} T1:Rs{t1:.1f}"
        return out

    elif text in ["/report", "report"]:
        from atlas.reporting.daily_report import generate_and_send
        generate_and_send()
        return "📊 Report generated and sent."

    elif text in ["/help", "help"]:
        return (
            "🤖 <b>ATLAS DIRECTIVES</b>\n\n"
            "/approve — proceed with suggested mode\n"
            "/pause — no trading tomorrow\n"
            "/normal — NORMAL mode\n"
            "/cautious — CAUTIOUS mode\n"
            "/aggressive — AGGRESSIVE mode\n"
            "/defensive — DEFENSIVE mode\n"
            "/login — generate Zerodha login URL\n"
            "/capital — show capital status\n"
            "/status — current agent status\n"
            "/help — show this menu"
        )

    return None


def poll(duration_seconds: int = 120):
    """
    Poll for directives for a given duration. MANUAL USE ONLY.

    WARNING: this competes with bot_listener.py for the same Telegram bot
    token. getUpdates has a single logical consumer -- whichever process polls
    first receives an update and the other never sees it. bot_listener runs
    @reboot and is kept alive by scripts/bot_watchdog.sh, so in normal operation
    it is already consuming the stream and this function will silently steal
    from it (or be starved by it).

    daily_report used to call this for 300s after each evening report, which is
    why the prompt reported "No directive received" while the listener was
    healthy. That call is removed. Do not wire this into anything scheduled.
    """
    log.warning("directives.poll() competes with bot_listener for getUpdates — "
                "manual use only; stop the listener first if you need this.")
    log.info(f"Listening for directives for {duration_seconds}s...")
    send("💬 <b>Awaiting your directive.</b>\nSend /help for options.")

    offset    = None
    deadline  = time.time() + duration_seconds
    responded = False

    while time.time() < deadline:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg    = update.get("message", {})
            chat   = str(msg.get("chat", {}).get("id", ""))
            text   = msg.get("text", "")

            # Only respond to your chat
            if chat != str(TELEGRAM_CHAT_ID):
                continue

            if not text.startswith("/") and text.lower() not in [
                "approve","pause","cautious","aggressive","normal","defensive","status","help"
            ]:
                continue

            log.info(f"Directive received: {text}")
            response = handle_directive(text)
            if response:
                send(response)
                responded = True
                log.info(f"Directive processed: {text}")

        time.sleep(5)

    if not responded:
        log.info("No directive received — agent proceeds with current mode")
        send("⏰ No directive received. Agent proceeds with current mode tomorrow.")


if __name__ == "__main__":
    # Test: send /status to the bot and see if it responds
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "poll":
        poll(duration_seconds=60)
    else:
        # Quick test — process a /status command
        response = handle_directive("/status")
        send(response)
        log.info("Status directive test sent")
