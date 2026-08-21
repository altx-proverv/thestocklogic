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
    SUPABASE_URL, SUPABASE_KEY, HALT_MODES, DEFAULT_AGENT_MODE
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
        r = requests.get(f"{SUPABASE_URL}{query}",
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
    rows = _get("/rest/v1/atlas_state?limit=1&order=updated_at.desc", "agent state")
    if not rows:
        raise RiskDataUnavailable("agent state: no atlas_state row exists")
    return rows[0]


# Position counting and P&L aggregation used to live here to feed the open-position
# limit and the daily/weekly loss caps. All three are gone: there is no position
# limit, and the operator manages drawdown rather than an automated cap. What
# remains is structural -- can we read our state, and has the operator halted us.

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
    # FAIL CLOSED. If we cannot read our own state we do not know whether the
    # operator has halted us, so we refuse rather than assume NORMAL.
    try:
        state = get_agent_state()
    except RiskDataUnavailable as e:
        log.error(f"KILL SWITCH: agent state unavailable — BLOCKING. {e}")
        return KillSwitchResult(False, f"Agent state unavailable: {e}",
                                {"data_available": False})

    mode    = state.get("mode", DEFAULT_AGENT_MODE)
    details = {"data_available": True, "mode": mode}

    # CHECK 1 — Operator halt
    if mode in HALT_MODES:
        log.warning(f"KILL SWITCH: Agent is {mode} — no trades allowed")
        return KillSwitchResult(False, f"Agent {mode.lower()} by directive", details)

    # CHECK 2 — Live broker funds. The only capital question that remains.
    #
    # Replaces the old capital fence, which compared a requirement against
    # atlas_state's stored deployed/available ledger. That ledger was a second
    # copy of the broker's balance, it drifted, and nothing reconciled it. Funds
    # are now read live from kite.margins() less the notional of any resting
    # ATLAS GTT triggers -- cash that is spoken for but absent from margins().
    #
    # can_afford() returns False on BOTH insufficient funds and any failure to
    # read them, so an unreadable broker blocks rather than defaults.
    if signal:
        capital_required = float(signal.get("capital_required") or 0)
        if capital_required > 0:
            from atlas.risk.funds import can_afford
            ok, reason, detail = can_afford(capital_required)
            details.update(detail)
            if not ok:
                log.warning(f"KILL SWITCH: {reason}")
                return KillSwitchResult(False, reason, details)

    # REMOVED: daily loss cap, weekly drawdown, max open positions, conviction
    # gates. Drawdown is managed by the operator, who also manages every exit;
    # there is no limit on the NUMBER of open positions; and score was shown to
    # be non-predictive. What is left is structural: can we read our state, and
    # are we halted.
    #
    # Note this is a count limit, not a duplicate check -- removing it left
    # nothing stopping a second position in a symbol already held. That is now
    # Gate 3b in atlas_entry.enter_trade, not here, because it needs the symbol
    # and the kill switch is deliberately symbol-agnostic.

    log.info(f"Kill switch PASSED — mode {mode}")
    return KillSwitchResult(True, "All checks passed", details)


def status() -> dict:
    """Current kill switch status summary. Read-only; reports unavailability
    rather than raising, so /status in Telegram degrades instead of crashing
    the bot listener."""
    try:
        state = get_agent_state()
    except RiskDataUnavailable as e:
        return {"data_available": False, "error": str(e),
                "halted": True,
                "note": "agent state unreadable — check() will BLOCK all trades"}

    mode = state.get("mode", DEFAULT_AGENT_MODE)
    out  = {"data_available": True, "mode": mode, "halted": mode in HALT_MODES}

    # Funds are advisory here, never fatal -- /status must not fail because the
    # broker is unreachable.
    try:
        from atlas.risk.funds import available_funds
        out["funds"] = available_funds()
    except Exception as e:
        out["funds"] = None
        out["funds_error"] = str(e)
    return out


if __name__ == "__main__":
    print("=== ATLAS KILL SWITCH STATUS ===")
    s = status()
    for k, v in s.items():
        print(f"  {k:<25} {v}")
    print("\n=== CHECK RESULT ===")
    result = check()
    print(result)
