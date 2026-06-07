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
    INITIAL_CAPITAL, DAILY_LOSS_CAP_PCT, WEEKLY_DRAWDOWN_PCT,
    MAX_OPEN_POSITIONS, AGENT_MODES, DEFAULT_AGENT_MODE
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ATLAS-RISK] %(message)s")
log = logging.getLogger(__name__)

# ── SUPABASE HELPERS ──────────────────────────────────────────────
def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }

def get_agent_state():
    """Fetch current agent state from Supabase."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_state?limit=1&order=updated_at.desc",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        return r.json()[0]
    return {"mode": DEFAULT_AGENT_MODE, "capital": INITIAL_CAPITAL, "daily_pnl": 0.0, "weekly_pnl": 0.0}

def get_open_positions():
    """Count currently open positions."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?status=eq.OPEN&select=id",
        headers=_headers()
    )
    if r.status_code == 200:
        return len(r.json())
    return 0

def get_today_pnl():
    """Get today's realised P&L from closed trades."""
    today = date.today().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?exit_date=eq.{today}&status=eq.CLOSED&select=pnl",
        headers=_headers()
    )
    if r.status_code == 200:
        trades = r.json()
        return sum(float(t.get("pnl", 0)) for t in trades)
    return 0.0

def get_week_pnl():
    """Get this week's realised P&L."""
    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?exit_date=gte.{week_start}&status=eq.CLOSED&select=pnl",
        headers=_headers()
    )
    if r.status_code == 200:
        trades = r.json()
        return sum(float(t.get("pnl", 0)) for t in trades)
    return 0.0

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
    state         = get_agent_state()
    mode          = state.get("mode", DEFAULT_AGENT_MODE)
    mode_config   = AGENT_MODES.get(mode, AGENT_MODES[DEFAULT_AGENT_MODE])
    capital       = float(state.get("capital", INITIAL_CAPITAL))
    daily_pnl     = get_today_pnl()
    weekly_pnl    = get_week_pnl()
    open_positions = get_open_positions()

    daily_loss_cap  = capital * DAILY_LOSS_CAP_PCT
    weekly_loss_cap = capital * WEEKLY_DRAWDOWN_PCT

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
            can, avail, cap_reason = can_deploy(capital_required, product)
            if not can:
                log.warning(f"KILL SWITCH: Capital fence — {cap_reason}")
                return KillSwitchResult(False, cap_reason, details)

    # CHECK 6 — Signal conviction check
    if signal:
        conviction = float(signal.get("conviction", 0))
        min_conv   = mode_config["min_conviction"]
        if conviction < min_conv:
            log.warning(f"KILL SWITCH: Conviction too low — {conviction}/{min_conv} in {mode} mode")
            return KillSwitchResult(False, f"Conviction {conviction} below threshold {min_conv}", details)

    # CHECK 6b — Regime-conviction matrix
    if signal:
        direction  = str(signal.get("direction", "LONG")).upper()
        conviction = float(signal.get("conviction", 0))

        # Get current market regime
        regime = "mixed"
        try:
            from datetime import date
            today = date.today().isoformat()
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/sector_heatmap?signal_date=eq.{today}&limit=1",
                headers=_headers()
            )
            if r.status_code == 200 and r.json():
                regime = r.json()[0].get("market_direction", "mixed").lower()
        except Exception:
            pass

        # Regime-conviction thresholds
        if regime == "bullish":
            min_long_conv  = 78   # Normal threshold for longs
            min_short_conv = 85   # Counter-trend — higher bar
        elif regime == "mixed":
            min_long_conv  = 82   # Raise bar in mixed market
            min_short_conv = 85   # Keep short bar high
        elif regime == "bearish":
            min_long_conv  = 999  # No longs in bearish — stay cash
            min_short_conv = 78   # Normal threshold for shorts
        else:
            min_long_conv  = 82
            min_short_conv = 85

        if direction == "LONG" and conviction < min_long_conv:
            log.warning(f"KILL SWITCH: Regime {regime.upper()} — LONG conviction {conviction:.0f} below {min_long_conv}")
            return KillSwitchResult(False,
                f"LONG blocked in {regime} regime — conviction {conviction:.0f} < {min_long_conv}", details)

        if direction == "SHORT" and conviction < min_short_conv:
            log.warning(f"KILL SWITCH: Regime {regime.upper()} — SHORT conviction {conviction:.0f} below {min_short_conv}")
            return KillSwitchResult(False,
                f"SHORT blocked in {regime} regime — conviction {conviction:.0f} < {min_short_conv}", details)

        log.info(f"Regime check PASSED — {regime.upper()} | {direction} Conv:{conviction:.0f} >= {min_long_conv if direction=='LONG' else min_short_conv}")

    # CHECK 7 — Daily P&L approaching cap (warn at 75%)
    warning_threshold = daily_loss_cap * 0.75
    if daily_pnl <= -warning_threshold:
        remaining = daily_loss_cap - abs(daily_pnl)
        log.warning(f"KILL SWITCH WARNING: Approaching daily cap — INR {remaining:,.0f} remaining")

    log.info(f"Kill switch PASSED — Mode:{mode} | Daily P&L:INR {daily_pnl:,.0f} | Open:{open_positions}/{max_trades}")
    return KillSwitchResult(True, "All checks passed", details)


def status() -> dict:
    """Get current kill switch status summary."""
    state          = get_agent_state()
    daily_pnl      = get_today_pnl()
    weekly_pnl     = get_week_pnl()
    open_positions = get_open_positions()
    capital        = float(state.get("capital", INITIAL_CAPITAL))

    return {
        "mode":              state.get("mode", DEFAULT_AGENT_MODE),
        "capital":           capital,
        "daily_pnl":         daily_pnl,
        "weekly_pnl":        weekly_pnl,
        "daily_loss_cap":    capital * DAILY_LOSS_CAP_PCT,
        "weekly_loss_cap":   capital * WEEKLY_DRAWDOWN_PCT,
        "open_positions":    open_positions,
        "max_positions":     AGENT_MODES.get(state.get("mode", DEFAULT_AGENT_MODE), {}).get("max_trades", 3),
        "kill_switch_active": daily_pnl <= -(capital * DAILY_LOSS_CAP_PCT),
    }


if __name__ == "__main__":
    print("=== ATLAS KILL SWITCH STATUS ===")
    s = status()
    for k, v in s.items():
        print(f"  {k:<25} {v}")
    print("\n=== CHECK RESULT ===")
    result = check()
    print(result)
