"""
ATLAS Risk Engine — Kill Switch
================================
Non-bypassable circuit breaker.
Checks daily loss, weekly drawdown, and agent mode.
Every trade MUST pass through here before execution.
"""

import os, sys, requests, logging
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    INITIAL_CAPITAL, DAILY_LOSS_CAP_INR, WEEKLY_DRAWDOWN_INR,
    MAX_OPEN_POSITIONS, AGENT_MODES, DEFAULT_AGENT_MODE, OPEN_STATUSES
)

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10


class RiskDataUnavailable(Exception):
    """A risk input could not be read.

    This is NEVER swallowed into a default. Every helper below used to return a
    safe-looking zero on a non-200 -- mode=NORMAL, daily_pnl=0, open=0 -- so a
    Supabase 500 or an expired key made all five checks pass and the switch
    returned ALLOWED. The layer that exists to stop trading was the one layer
    that opened up when its data went missing.
    """


# ── SUPABASE HELPERS ──────────────────────────────────────────────
def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }

def _get(query: str, what: str) -> list:
    """GET or raise. No timeout meant a hung Supabase blocked the order path
    indefinitely; no status check meant an error body became an empty result."""
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{query}",
                         headers=_headers(), timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise RiskDataUnavailable(f"{what}: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise RiskDataUnavailable(f"{what}: HTTP {r.status_code} {r.text[:120]}")
    try:
        return r.json()
    except ValueError as e:
        raise RiskDataUnavailable(f"{what}: unparseable response") from e

def get_agent_state():
    """Current agent state. Raises RiskDataUnavailable if it cannot be read."""
    rows = _get("atlas_state?limit=1&order=updated_at.desc", "agent state")
    if not rows:
        raise RiskDataUnavailable("agent state: no atlas_state row exists")
    return rows[0]

def get_open_positions():
    """Count positions holding live capital.

    Counts GTT_PENDING as well as OPEN. A resting GTT has committed the capital
    at the broker even though it has not filled, and atlas_entry has always
    counted it -- this function counted only OPEN, so the position limit
    undercounted by every pending trigger.
    """
    rows = _get(f"atlas_trades?status=in.({','.join(OPEN_STATUSES)})"
                f"&agent_mode=eq.LIVE&select=id", "open positions")
    return len(rows)

def get_today_pnl():
    """Today's realised P&L from closed trades."""
    today = date.today().isoformat()
    rows = _get(f"atlas_trades?exit_date=eq.{today}&status=eq.CLOSED&select=pnl",
                "today P&L")
    return sum(float(t.get("pnl") or 0) for t in rows)

def get_week_pnl():
    """This week's realised P&L."""
    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    rows = _get(f"atlas_trades?exit_date=gte.{week_start}&status=eq.CLOSED&select=pnl",
                "week P&L")
    return sum(float(t.get("pnl") or 0) for t in rows)

# ── KILL SWITCH CHECKS ────────────────────────────────────────────
class KillSwitchResult:
    def __init__(self, allowed: bool, reason: str, details: dict = None):
        self.allowed = allowed
        self.reason  = reason
        self.details = details or {}

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        status = "ALLOWED" if self.allowed else "BLOCKED"
        return f"KillSwitch[{status}]: {self.reason}"


def check(signal: dict = None) -> KillSwitchResult:
    """
    Master kill switch check.
    Call before EVERY trade execution.
    Returns KillSwitchResult — if False, trade is BLOCKED.
    """
    # FAIL CLOSED. If any risk input cannot be read we do not know the P&L, the
    # mode, or the exposure, so we refuse rather than assume zero.
    try:
        state          = get_agent_state()
        daily_pnl      = get_today_pnl()
        weekly_pnl     = get_week_pnl()
        open_positions = get_open_positions()
    except RiskDataUnavailable as e:
        log.error(f"KILL SWITCH: risk data unavailable — BLOCKING. {e}")
        return KillSwitchResult(False, f"Risk data unavailable: {e}",
                                {"data_available": False})

    mode          = state.get("mode", DEFAULT_AGENT_MODE)
    mode_config   = AGENT_MODES.get(mode, AGENT_MODES[DEFAULT_AGENT_MODE])
    capital       = float(state.get("capital") or INITIAL_CAPITAL)

    # ABSOLUTE caps, per config.py: "3 trades x Rs3k = Rs9,000 worst case in one
    # day. The old 2%-of-1.5L rule produced Rs3,000 -- one stop-out halted the
    # system." This read capital * DAILY_LOSS_CAP_PCT, and atlas_state.capital is
    # 150000, so it was enforcing 150000*0.03 = Rs4,500 -- the percentage still
    # applied to the same Rs1.5L the comment warns about. DAILY_LOSS_CAP_INR had
    # no readers at all.
    daily_loss_cap  = DAILY_LOSS_CAP_INR
    weekly_loss_cap = WEEKLY_DRAWDOWN_INR

    details = {
        "mode":           mode,
        "capital":        capital,
        "daily_pnl":      daily_pnl,
        "weekly_pnl":     weekly_pnl,
        "open_positions": open_positions,
        "daily_loss_cap": daily_loss_cap,
        "weekly_loss_cap": weekly_loss_cap,
    }

    # CHECK 1 — Agent mode PAUSED
    if mode == "PAUSED":
        log.warning("KILL SWITCH: Agent is PAUSED — no trades allowed")
        return KillSwitchResult(False, "Agent paused by directive", details)

    # CHECK 2 — Daily loss cap breached
    if daily_pnl <= -daily_loss_cap:
        log.warning(f"KILL SWITCH: Daily loss cap breached — P&L: INR {daily_pnl:,.0f} / Cap: INR {-daily_loss_cap:,.0f}")
        return KillSwitchResult(False, f"Daily loss cap breached (INR {daily_pnl:,.0f})", details)

    # CHECK 3 — Weekly drawdown breached
    if weekly_pnl <= -weekly_loss_cap:
        log.warning(f"KILL SWITCH: Weekly drawdown breached — P&L: INR {weekly_pnl:,.0f} / Cap: INR {-weekly_loss_cap:,.0f}")
        return KillSwitchResult(False, f"Weekly drawdown breached (INR {weekly_pnl:,.0f})", details)

    # CHECK 4 — Max open positions
    max_trades = mode_config["max_trades"]
    if open_positions >= max_trades:
        log.warning(f"KILL SWITCH: Max open positions reached — {open_positions}/{max_trades}")
        return KillSwitchResult(False, f"Max positions reached ({open_positions}/{max_trades})", details)

    # CHECK 5 — Capital fence check
    if signal:
        from atlas.risk.capital_manager import can_deploy
        capital_required = float(signal.get("capital_required", 0))
        direction        = str(signal.get("direction", "LONG")).upper()
        product          = "MIS" if direction == "SHORT" else "CNC"
        if capital_required > 0 or product == "CNC":
            try:
                can, avail, cap_reason = can_deploy(capital_required, product)
            except Exception as e:
                # capital_manager reads atlas_state over HTTP with no timeout
                # and no status check of its own. Treat any failure as a block.
                log.error(f"KILL SWITCH: capital fence unreadable — BLOCKING. {e}")
                return KillSwitchResult(False, f"Capital fence unreadable: {e}", details)
            if not can:
                log.warning(f"KILL SWITCH: Capital fence — {cap_reason}")
                return KillSwitchResult(False, cap_reason, details)

    # CHECK 6 / 6b -- REMOVED (conviction gates)
    #
    # Both gated on signal["conviction"], i.e. the 0-100 score. Validation
    # showed score does not predict outcomes; setups + direction + trend do.
    # The gate was removed from atlas/config.py, 06_push_supabase.py and
    # market_open.py -- this was the fourth place it survived.
    #
    # It was blocking unconditionally: market_open no longer sends a
    # "conviction" key, so it arrived as 0.0 against a threshold of 78.
    #
    # CHECK 6b was additionally reading sector_heatmap for TODAY's date, which
    # is not written until ~18:35 -- at 09:37 it always fell back to "mixed".
    # It gated on a default value, not the real regime. Regime -> side blocking
    # is handled correctly by Gate 2 in atlas_entry.enter_trade.
    #
    # Ranking by setup/direction/trend belongs in session_selector, not here.
    # The kill switch gates capital, drawdown and exposure -- not signal quality.

    # CHECK 7 — Daily P&L approaching cap (warn at 75%)
    warning_threshold = daily_loss_cap * 0.75
    if daily_pnl <= -warning_threshold:
        remaining = daily_loss_cap - abs(daily_pnl)
        log.warning(f"KILL SWITCH WARNING: Approaching daily cap — INR {remaining:,.0f} remaining")

    log.info(f"Kill switch PASSED — Mode:{mode} | Daily P&L:INR {daily_pnl:,.0f} | Open:{open_positions}/{max_trades}")
    return KillSwitchResult(True, "All checks passed", details)


def status() -> dict:
    """Current kill switch status summary. Read-only; reports unavailability
    rather than raising, so /status in Telegram degrades instead of crashing
    the bot listener."""
    try:
        state          = get_agent_state()
        daily_pnl      = get_today_pnl()
        weekly_pnl     = get_week_pnl()
        open_positions = get_open_positions()
    except RiskDataUnavailable as e:
        return {"data_available": False, "error": str(e),
                "kill_switch_active": True,
                "note": "risk data unreadable — check() will BLOCK all trades"}

    capital = float(state.get("capital") or INITIAL_CAPITAL)
    mode    = state.get("mode", DEFAULT_AGENT_MODE)
    return {
        "data_available":    True,
        "mode":              mode,
        "capital":           capital,
        "daily_pnl":         daily_pnl,
        "weekly_pnl":        weekly_pnl,
        "daily_loss_cap":    DAILY_LOSS_CAP_INR,
        "weekly_loss_cap":   WEEKLY_DRAWDOWN_INR,
        "open_positions":    open_positions,
        "max_positions":     AGENT_MODES.get(mode, {}).get("max_trades", 3),
        "kill_switch_active": daily_pnl <= -DAILY_LOSS_CAP_INR,
    }


if __name__ == "__main__":
    print("=== ATLAS KILL SWITCH STATUS ===")
    s = status()
    for k, v in s.items():
        print(f"  {k:<25} {v}")
    print("\n=== CHECK RESULT ===")
    result = check()
    print(result)
