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
    DEFAULT_AGENT_MODE, MAX_RISK_PER_TRADE, MAX_TRADES_PER_DAY,
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
    return {"mode": DEFAULT_AGENT_MODE}


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


def determine_next_mode(state, daily_pnl, consecutive_losses):
    """Auto-suggest agent mode for tomorrow based on today's performance.

    Advisory only -- the operator decides, and only PAUSED changes behaviour.
    The old "kill switch triggered today" branch compared against a
    capital-derived loss cap that no longer exists; the equivalent signal is now
    simply a day worse than the per-trade risk budget.
    """
    current_mode = state.get("mode", DEFAULT_AGENT_MODE)

    # A day worse than a full stop-out on every allowed trade
    if daily_pnl <= -(MAX_RISK_PER_TRADE * MAX_TRADES_PER_DAY):
        return "DEFENSIVE", "worst-case day realised"

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
    mode    = state.get("mode", DEFAULT_AGENT_MODE)

    # P&L calculations
    closed_trades = [t for t in trades if t.get("status") == "CLOSED"]
    open_trades   = [t for t in trades if t.get("status") == "OPEN"]
    wins          = [t for t in closed_trades if float(t.get("pnl", 0)) > 0]
    losses        = [t for t in closed_trades if float(t.get("pnl", 0)) < 0]
    daily_pnl     = sum(float(t.get("pnl", 0)) for t in closed_trades)
    weekly_pnl    = get_week_pnl()
    win_rate      = len(wins) / max(len(wins) + len(losses), 1) * 100
    regime        = get_sector_regime()
    consecutive_losses = len(losses)  # simplified

    # Suggest tomorrow's mode
    next_mode, mode_reason = determine_next_mode(state, daily_pnl, consecutive_losses)

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

    # Live broker funds. No stored capital figure to report against.
    try:
        from atlas.risk.funds import available_funds
        f = available_funds()
        funds_line = (f"Broker:       ₹{f['margin']:,.0f}\n"
                      f"Resting GTTs: ₹{f['gtt_committed']:,.0f}\n"
                      f"Available:    ₹{f['available']:,.0f}")
    except Exception as e:
        funds_line = f"Broker funds: UNAVAILABLE ({str(e)[:60]}) — entries blocked"

    msg = f"""
📊 <b>ATLAS DAILY REPORT</b>
{now.strftime('%d %b %Y · %H:%M IST')}
━━━━━━━━━━━━━━━━━━━━━━━━

{pnl_icon} <b>P&amp;L</b>
Today P&amp;L:    ₹{daily_pnl:+,.0f}
Weekly P&amp;L:   ₹{weekly_pnl:+,.0f}

💰 <b>FUNDS (live from broker)</b>
{funds_line}

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
Rules:       ₹{MAX_RISK_PER_TRADE:,.0f} risk/trade · max {MAX_TRADES_PER_DAY} entries/day
Stop:        broker funds — no automated loss cap, exits are manual

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


# NOTE: exactly ONE __main__ guard, at end of file.
#
# There used to be two. The first sat here and called generate_and_send();
# run() was then defined BELOW it and a second guard at EOF called run(), which
# called generate_and_send() again. Running the module therefore sent the report
# twice, three seconds apart. Confirmed in production on 2026-08-11:
#
#   13:35:04 [ATLAS-REPORT] Daily report sent — P&L: ₹+0 | Trades: 0
#   13:35:04 [ATLAS-REPORT] Starting daily report cycle...
#   13:35:07 [ATLAS-REPORT] Daily report sent — P&L: ₹+0 | Trades: 0
#
# run()'s directive poll is removed with it. It called directives.poll(300),
# which long-polls Telegram getUpdates -- while bot_listener.py (@reboot, kept
# alive by scripts/bot_watchdog.sh) is already long-polling the SAME bot token
# 24/7. Two consumers on one update stream: whichever polls first consumes the
# update and the other never sees it. That is why the evening prompt reported
# "No directive received" at 13:40 even though the listener was healthy.
#
# bot_listener handles directives around the clock, so the 5-minute window this
# opened was redundant as well as harmful.


if __name__ == "__main__":
    generate_and_send()
