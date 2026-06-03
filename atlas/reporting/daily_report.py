"""
ATLAS Reporting — Daily Report Generator
==========================================
Runs at 7 PM IST after market close.
Pulls all trades, signals, P&L for the day.
Sends formatted report to Telegram.
Awaits your directive for tomorrow.
"""

import sys, requests, logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    INITIAL_CAPITAL, DAILY_LOSS_CAP_PCT, WEEKLY_DRAWDOWN_PCT,
    DEFAULT_AGENT_MODE, AGENT_MODES
)
from atlas.reporting.telegram import send

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ATLAS-REPORT] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def get_agent_state():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_state?limit=1&order=updated_at.desc",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        return r.json()[0]
    return {"mode": DEFAULT_AGENT_MODE, "capital": INITIAL_CAPITAL}


def get_today_trades():
    today = date.today().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?entry_date=eq.{today}&order=created_at.asc",
        headers=_headers()
    )
    return r.json() if r.status_code == 200 else []


def get_today_signals():
    today = date.today().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals?signal_date=eq.{today}&select=id,symbol,direction,score",
        headers=_headers()
    )
    return r.json() if r.status_code == 200 else []


def get_week_pnl():
    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?entry_date=gte.{week_start}"
        f"&status=eq.CLOSED&select=pnl",
        headers=_headers()
    )
    if r.status_code == 200:
        return sum(float(t.get("pnl", 0)) for t in r.json())
    return 0.0


def get_sector_regime():
    today = date.today().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/sector_heatmap?signal_date=eq.{today}"
        f"&order=rank.asc&limit=1",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        return r.json()[0].get("market_direction", "MIXED").upper()
    return "MIXED"


def determine_next_mode(state, daily_pnl, capital, consecutive_losses):
    """Auto-suggest agent mode for tomorrow based on today's performance."""
    current_mode = state.get("mode", DEFAULT_AGENT_MODE)
    daily_loss_cap = capital * DAILY_LOSS_CAP_PCT

    # Kill switch triggered today
    if daily_pnl <= -daily_loss_cap:
        return "DEFENSIVE", "Kill switch triggered today"

    # Two or more consecutive losses
    if consecutive_losses >= 2:
        return "CAUTIOUS", f"{consecutive_losses} consecutive losses"

    # Positive day
    if daily_pnl > 0:
        if current_mode == "CAUTIOUS":
            return "NORMAL", "Recovery after cautious day"
        return current_mode, "Performing well"

    # Small loss — stay current
    return current_mode, "Within acceptable range"


def generate_and_send():
    """Generate daily report and send to Telegram."""
    now     = datetime.now(IST)
    state   = get_agent_state()
    trades  = get_today_trades()
    signals = get_today_signals()
    capital = float(state.get("capital", INITIAL_CAPITAL))
    mode    = state.get("mode", DEFAULT_AGENT_MODE)

    # P&L calculations
    closed_trades = [t for t in trades if t.get("status") == "CLOSED"]
    open_trades   = [t for t in trades if t.get("status") == "OPEN"]
    wins          = [t for t in closed_trades if float(t.get("pnl", 0)) > 0]
    losses        = [t for t in closed_trades if float(t.get("pnl", 0)) < 0]
    daily_pnl     = sum(float(t.get("pnl", 0)) for t in closed_trades)
    weekly_pnl    = get_week_pnl()
    daily_pnl_pct = daily_pnl / capital * 100
    win_rate      = len(wins) / max(len(wins) + len(losses), 1) * 100
    regime        = get_sector_regime()
    consecutive_losses = len(losses)  # simplified

    # Suggest tomorrow's mode
    next_mode, mode_reason = determine_next_mode(state, daily_pnl, capital, consecutive_losses)

    # Build trade lines
    trade_lines = ""
    for t in closed_trades:
        pnl  = float(t.get("pnl", 0))
        icon = "✅" if pnl > 0 else "❌"
        trade_lines += f"\n{icon} {t['symbol']} {t['direction']} ₹{pnl:+,.0f} ({t.get('exit_reason','')})"
    for t in open_trades:
        entry = float(t.get("entry_price", 0))
        trade_lines += f"\n⏳ {t['symbol']} {t['direction']} ₹{entry:,.1f} → OPEN (holding)"

    if not trade_lines:
        trade_lines = "\n  No trades executed today"

    # Signal quality
    signals_generated = len(signals)
    signals_traded    = len(trades)
    signals_filtered  = max(0, signals_generated - signals_traded)

    # P&L icon
    pnl_icon = "🟢" if daily_pnl >= 0 else "🔴"

    # Daily loss cap status
    cap_used_pct = abs(daily_pnl) / (capital * DAILY_LOSS_CAP_PCT) * 100 if daily_pnl < 0 else 0
    cap_status   = f"₹{capital * DAILY_LOSS_CAP_PCT:,.0f} cap — {cap_used_pct:.0f}% used"

    msg = f"""
📊 <b>ATLAS DAILY REPORT</b>
{now.strftime('%d %b %Y · %H:%M IST')}
━━━━━━━━━━━━━━━━━━━━━━━━

{pnl_icon} <b>CAPITAL STATUS</b>
Capital:      ₹{capital:,.0f}
Today P&amp;L:    ₹{daily_pnl:+,.0f} ({daily_pnl_pct:+.2f}%)
Weekly P&amp;L:   ₹{weekly_pnl:+,.0f}
Risk cap:     {cap_status}

📈 <b>TRADES TODAY</b>{trade_lines}

📡 <b>SIGNAL QUALITY</b>
Generated:  {signals_generated}
Filtered:   {signals_filtered}
Traded:     {signals_traded}
Win rate:   {win_rate:.0f}% ({len(wins)}W / {len(losses)}L)

🌐 <b>TOMORROW'S CONTEXT</b>
Regime:      {regime}
Today mode:  {mode}
Suggested:   {next_mode} ({mode_reason})
Kill-switch: ₹{capital * DAILY_LOSS_CAP_PCT:,.0f} daily hard stop

<b>Reply with directive:</b>
/approve — proceed ({next_mode} mode)
/pause — no trading tomorrow
/cautious — reduce to CAUTIOUS
/aggressive — increase to AGGRESSIVE
/normal — reset to NORMAL
""".strip()

    ok = send(msg)
    if ok:
        log.info(f"Daily report sent — P&L: ₹{daily_pnl:+,.0f} | Trades: {len(trades)} | Mode: {mode}")
    else:
        log.error("Failed to send daily report")
    return ok


if __name__ == "__main__":
    generate_and_send()


def run():
    """Full daily report + directive listening cycle."""
    log.info("Starting daily report cycle...")
    ok = generate_and_send()
    if ok:
        # Listen for directives for 5 minutes after report
        from atlas.reporting.directives import poll
        poll(duration_seconds=300)
    else:
        log.error("Report failed — skipping directive poll")

if __name__ == "__main__":
    run()
