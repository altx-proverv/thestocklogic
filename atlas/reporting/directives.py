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
    INITIAL_CAPITAL, AGENT_MODES, DEFAULT_AGENT_MODE
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
    return {"mode": DEFAULT_AGENT_MODE, "capital": INITIAL_CAPITAL, "id": 1}


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


def handle_directive(text: str) -> str:
    """
    Process a directive command.
    Returns response message to send back.
    """
    text = text.strip().lower()
    now  = datetime.now(IST).strftime("%d %b %Y %H:%M IST")

    if text in ["/approve", "approve"]:
        state = get_agent_state()
        mode  = state.get("mode", DEFAULT_AGENT_MODE)
        mode_config = AGENT_MODES.get(mode, AGENT_MODES[DEFAULT_AGENT_MODE])
        update_agent_mode(mode, f"Approved by directive at {now}")
        return (
            f"✅ <b>APPROVED</b>\n"
            f"Agent proceeds tomorrow in <b>{mode}</b> mode\n"
            f"Max trades: {mode_config['max_trades']} | "
            f"Min conviction: {mode_config['min_conviction']}/100\n"
            f"Capital risk cap: ₹{float(state.get('capital', INITIAL_CAPITAL)) * 0.02:,.0f}"
        )

    elif text in ["/pause", "pause"]:
        update_agent_mode("PAUSED", f"Paused by directive at {now}")
        return (
            "⏸ <b>AGENT PAUSED</b>\n"
            "No trades will be executed tomorrow.\n"
            "Send /approve or /normal to resume."
        )

    elif text in ["/cautious", "cautious"]:
        update_agent_mode("CAUTIOUS", f"Set to CAUTIOUS by directive at {now}")
        cfg = AGENT_MODES["CAUTIOUS"]
        return (
            f"🟡 <b>CAUTIOUS MODE SET</b>\n"
            f"Max trades: {cfg['max_trades']}\n"
            f"Min conviction: {cfg['min_conviction']}/100\n"
            f"Position size: {int(cfg['size_pct']*100)}% of normal"
        )

    elif text in ["/aggressive", "aggressive"]:
        update_agent_mode("AGGRESSIVE", f"Set to AGGRESSIVE by directive at {now}")
        cfg = AGENT_MODES["AGGRESSIVE"]
        return (
            f"🔴 <b>AGGRESSIVE MODE SET</b>\n"
            f"Max trades: {cfg['max_trades']}\n"
            f"Min conviction: {cfg['min_conviction']}/100\n"
            f"Position size: {int(cfg['size_pct']*100)}% of normal\n"
            f"⚠️ Increased risk — monitor closely"
        )

    elif text in ["/normal", "normal"]:
        update_agent_mode("NORMAL", f"Reset to NORMAL by directive at {now}")
        cfg = AGENT_MODES["NORMAL"]
        return (
            f"🔵 <b>NORMAL MODE SET</b>\n"
            f"Max trades: {cfg['max_trades']}\n"
            f"Min conviction: {cfg['min_conviction']}/100\n"
            f"Position size: {int(cfg['size_pct']*100)}% of normal"
        )

    elif text in ["/defensive", "defensive"]:
        update_agent_mode("DEFENSIVE", f"Set to DEFENSIVE by directive at {now}")
        cfg = AGENT_MODES["DEFENSIVE"]
        return (
            f"🛡 <b>DEFENSIVE MODE SET</b>\n"
            f"Max trades: {cfg['max_trades']}\n"
            f"Min conviction: {cfg['min_conviction']}/100\n"
            f"Position size: {int(cfg['size_pct']*100)}% of normal"
        )

    elif text in ["/status", "status"]:
        state = get_agent_state()
        mode  = state.get("mode", DEFAULT_AGENT_MODE)
        cap   = float(state.get("capital", INITIAL_CAPITAL))
        return (
            f"📊 <b>ATLAS STATUS</b>\n"
            f"Mode:    {mode}\n"
            f"Capital: ₹{cap:,.0f}\n"
            f"Daily cap: ₹{cap * 0.02:,.0f}\n"
            f"Time:    {now}"
        )

    elif text in ["/help", "help"]:
        return (
            "🤖 <b>ATLAS DIRECTIVES</b>\n\n"
            "/approve — proceed with suggested mode\n"
            "/pause — no trading tomorrow\n"
            "/normal — NORMAL mode (3 trades, 78+ conv)\n"
            "/cautious — CAUTIOUS mode (2 trades, 82+ conv)\n"
            "/aggressive — AGGRESSIVE mode (3 trades, 75+ conv)\n"
            "/defensive — DEFENSIVE mode (1 trade, 87+ conv)\n"
            "/status — current agent status\n"
            "/help — show this menu"
        )

    return None


def poll(duration_seconds: int = 120):
    """
    Poll for directives for a given duration.
    Called after daily report — waits for your response.
    """
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
