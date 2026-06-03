"""
ATLAS Risk Engine — Capital Manager
=====================================
Enforces the capital fence between ATLAS and personal funds.
ATLAS allocated capital: INR 1,00,000 (configurable)
Personal capital: NEVER touched regardless of Zerodha balance.

Rules:
- deployed_capital + new_trade_capital <= allocated_capital
- available_capital = allocated_capital - deployed_capital
- Brokerage deducted from allocated capital
- Capital recycled on exit (T1/T2/SL)
"""

import sys, requests, logging, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    INITIAL_CAPITAL, MAX_OPEN_POSITIONS
)

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
BROKERAGE_PER_ORDER = 20  # Zerodha flat fee INR 20 per order


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def get_state() -> dict:
    """Fetch current capital state."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_state?limit=1&order=updated_at.desc",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        return r.json()[0]
    return {
        "id": 1,
        "allocated_capital":  INITIAL_CAPITAL,
        "deployed_capital":   0,
        "available_capital":  INITIAL_CAPITAL,
        "total_brokerage":    0,
        "mode":               "NORMAL",
    }


def update_state(updates: dict) -> bool:
    """Update capital state in Supabase."""
    state = get_state()
    updates["updated_at"] = datetime.now(IST).isoformat()
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/atlas_state?id=eq.{state['id']}",
        headers=_headers(),
        json=updates
    )
    return r.status_code in (200, 204)


def can_deploy(capital_required: float) -> tuple:
    """
    Check if capital is available for a new trade.
    Returns (can_deploy, available, reason)
    """
    state     = get_state()
    available = float(state.get("available_capital", INITIAL_CAPITAL))
    allocated = float(state.get("allocated_capital", INITIAL_CAPITAL))

    # Include brokerage cost (entry + exit = 2 orders minimum)
    total_required = capital_required + (BROKERAGE_PER_ORDER * 2)

    if total_required > available:
        return (
            False,
            available,
            f"Insufficient capital — need INR {total_required:,.0f}, "
            f"available INR {available:,.0f} of INR {allocated:,.0f} allocated"
        )

    return True, available, f"Capital available — INR {available:,.0f}"


def deploy_capital(trade_id: int, capital: float, symbol: str) -> bool:
    """
    Lock capital for a new trade.
    Called immediately after order placement.
    """
    state    = get_state()
    deployed = float(state.get("deployed_capital", 0))
    available= float(state.get("available_capital", INITIAL_CAPITAL))
    brokerage= float(state.get("total_brokerage", 0))

    # Deduct capital + brokerage
    brokerage_cost = BROKERAGE_PER_ORDER * 2  # entry + exit
    new_deployed   = deployed + capital
    new_available  = available - capital - brokerage_cost
    new_brokerage  = brokerage + brokerage_cost

    ok = update_state({
        "deployed_capital":  round(new_deployed, 2),
        "available_capital": round(new_available, 2),
        "total_brokerage":   round(new_brokerage, 2),
        "notes": f"Deployed INR {capital:,.0f} for {symbol} (trade #{trade_id})"
    })

    if ok:
        log.info(
            f"Capital deployed: INR {capital:,.0f} for {symbol} | "
            f"Available: INR {new_available:,.0f}"
        )
    return ok


def release_capital(trade_id: int, capital: float, pnl: float, symbol: str) -> bool:
    """
    Release capital back to available after trade exit.
    Called after T1/T2/SL exit.
    PnL adjusts the available capital (gains add, losses reduce).
    """
    state    = get_state()
    deployed = float(state.get("deployed_capital", 0))
    available= float(state.get("available_capital", INITIAL_CAPITAL))

    # Return capital + PnL to available
    new_deployed  = max(0, deployed - capital)
    new_available = available + capital + pnl

    ok = update_state({
        "deployed_capital":  round(new_deployed, 2),
        "available_capital": round(new_available, 2),
        "notes": f"Released INR {capital:,.0f} from {symbol} | P&L: INR {pnl:+,.0f}"
    })

    if ok:
        log.info(
            f"Capital released: INR {capital:,.0f} from {symbol} | "
            f"P&L: INR {pnl:+,.0f} | "
            f"Available: INR {new_available:,.0f}"
        )
    return ok


def get_capital_status() -> dict:
    """Get full capital status summary."""
    state = get_state()
    allocated = float(state.get("allocated_capital", INITIAL_CAPITAL))
    deployed  = float(state.get("deployed_capital", 0))
    available = float(state.get("available_capital", INITIAL_CAPITAL))
    brokerage = float(state.get("total_brokerage", 0))

    return {
        "allocated":        allocated,
        "deployed":         deployed,
        "available":        available,
        "brokerage_paid":   brokerage,
        "utilisation_pct":  round(deployed / allocated * 100, 1) if allocated else 0,
        "net_capital":      allocated - brokerage,
    }


def recalculate_from_open_trades() -> bool:
    """
    Recalculate deployed capital from open atlas_trades.
    Used to sync state after system restart.
    """
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?status=eq.OPEN"
        f"&select=capital_deployed,symbol",
        headers=_headers()
    )
    if r.status_code != 200:
        return False

    trades   = r.json()
    deployed = sum(float(t.get("capital_deployed", 0)) for t in trades)
    state    = get_state()
    allocated= float(state.get("allocated_capital", INITIAL_CAPITAL))
    available= allocated - deployed

    ok = update_state({
        "deployed_capital":  round(deployed, 2),
        "available_capital": round(available, 2),
        "notes": f"Recalculated from {len(trades)} open trades"
    })

    log.info(
        f"Capital recalculated — {len(trades)} open trades | "
        f"Deployed: INR {deployed:,.0f} | Available: INR {available:,.0f}"
    )
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s [ATLAS-CAPITAL] %(message)s")

    print("=== ATLAS CAPITAL STATUS ===")
    status = get_capital_status()
    for k, v in status.items():
        if isinstance(v, float):
            print(f"  {k:<20} INR {v:>12,.0f}")
        else:
            print(f"  {k:<20} {v}")

    print("\n=== CAPITAL FENCE TEST ===")
    can, avail, reason = can_deploy(33000)
    print(f"  Deploy INR 33,000: {can} — {reason}")

    can2, avail2, reason2 = can_deploy(95000)
    print(f"  Deploy INR 95,000: {can2} — {reason2}")
