"""
ATLAS Entry Logic -- TSL Rules Compliant (Phase: TRAINING)
==========================================================
Agent IDENTIFIES and ENTERS trades only. No SL, no target, no exit.
All exits are MANUAL this phase. Scalable: exit management is a separate
module gated by ENABLE_EXIT_MANAGEMENT (off now).

Gate stack (all must pass, in order):
  1. Live/shadow  -- LIVE_TRADING_ENABLED (default deny -> shadow log only)
  2. Regime->side  -- bullish=long, bearish=short, mixed=either/cash, unknown=cash
  3. Daily limit  -- MAX_TRADES_PER_DAY
  4. Entry range  -- LTP within [entry_low, entry_high], else skip (no chasing)
  5. Sizing       -- Rs1L notional, multiple of 5 (size_by_notional)
  6. Kill switch
  7. Funds        -- (checked in sizing when funds provided)
Then: ENTER ONLY. Never places SL/target. Notify MANUAL RISK REQUIRED.
"""
import sys, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY, LIVE_TRADING_ENABLED,
    MAX_TRADES_PER_DAY, ENFORCE_ENTRY_RANGE,
    ALLOW_LONG_IN_BULLISH, ALLOW_SHORT_IN_BULLISH,
    ALLOW_LONG_IN_BEARISH, ALLOW_SHORT_IN_BEARISH,
    DEFAULT_ON_UNKNOWN_REGIME,
)
from atlas.risk.position_sizing import size_by_notional
from atlas.risk.kill_switch import check as kill_switch_check
from atlas.execution.broker import place_order, get_ltp

log = logging.getLogger("ATLAS-ENTRY")
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}


def get_market_regime() -> str:
    """Latest market_direction from sector_heatmap. Returns bullish/bearish/mixed/unknown."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/sector_heatmap?select=market_direction,signal_date"
            f"&order=signal_date.desc&limit=1", headers=_headers(), timeout=10)
        if r.status_code == 200 and r.json():
            d = (r.json()[0].get("market_direction") or "unknown").lower()
            return d if d in ("bullish", "bearish", "mixed") else "unknown"
    except Exception as e:
        log.warning(f"Regime fetch failed: {e}")
    return "unknown"


def regime_allows_side(regime: str, direction: str) -> tuple:
    """Rules 8,9,10,11. Returns (allowed: bool, reason: str)."""
    d = direction.upper()
    if regime == "bullish":
        if d == "LONG" and ALLOW_LONG_IN_BULLISH: return True, "bullish->long ok"
        return False, "bullish regime -- shorts blocked (rule 8)"
    if regime == "bearish":
        if d == "SHORT" and ALLOW_SHORT_IN_BEARISH: return True, "bearish->short ok"
        return False, "bearish regime -- longs blocked (rule 10)"
    if regime == "mixed":
        # rule 11 -- sentiment decides; allow both, let other gates filter
        return True, "mixed regime -- side permitted, other gates apply"
    # unknown/stale -> cash (rule: DEFAULT_ON_UNKNOWN_REGIME)
    return False, f"regime unknown/stale -> {DEFAULT_ON_UNKNOWN_REGIME} (no trade)"


def check_entry_range(direction: str, ltp: float, entry_low: float, entry_high: float) -> tuple:
    """Enter ONLY if LTP within [entry_low, entry_high]. Both directions. Returns (ok, reason)."""
    if not ENFORCE_ENTRY_RANGE:
        return True, "range check off"
    if not entry_low or not entry_high or entry_low <= 0 or entry_high <= 0:
        return False, "no entry band defined -- cannot verify range"
    lo, hi = min(entry_low, entry_high), max(entry_low, entry_high)
    if lo <= ltp <= hi:
        return True, f"LTP Rs{ltp:.1f} within band Rs{lo:.1f}-Rs{hi:.1f}"
    return False, f"LTP Rs{ltp:.1f} outside entry band Rs{lo:.1f}-Rs{hi:.1f} -- no chase"


def get_today_trade_count() -> int:
    today = datetime.now(IST).date().isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades?entry_date=eq.{today}&select=id",
            headers=_headers(), timeout=10)
        return len(r.json()) if r.status_code == 200 else 0
    except Exception:
        return 0


def enter_trade(signal: dict) -> dict:
    """
    Full ATLAS entry gate stack + enter-only. Returns status dict.
    SHADOW mode (LIVE_TRADING_ENABLED=false): logs intent, places NO order.
    """
    symbol    = signal.get("symbol", "")
    direction = signal.get("direction", "LONG").upper()
    entry_ref = float(signal.get("entry_ref", signal.get("entry", 0)) or 0)
    entry_low = float(signal.get("entry_low", 0) or 0)
    entry_high= float(signal.get("entry_high", 0) or 0)
    mode = "LIVE" if LIVE_TRADING_ENABLED else "SHADOW"

    log.info(f"[{mode}] Evaluating {symbol} {direction}")

    # GATE 1 -- MARKET DIRECTION (Nifty/BankNifty opening range)
    # No trade until the index shows a clear direction. WAIT -> cash.
    try:
        from atlas.execution.index_state import get_market_direction
        mkt = get_market_direction()
        mkt_dir = mkt.get("direction", "WAIT")
    except Exception as e:
        log.warning(f"market direction check failed: {e}")
        mkt_dir = "WAIT"
    if mkt_dir == "WAIT":
        return {"status": "SKIPPED_MARKET_WAIT", "reason": "No clear Nifty opening-range direction -- staying cash"}
    if mkt_dir == "LONG" and direction != "LONG":
        return {"status": "SKIPPED_MARKET_DIR", "reason": f"Market is LONG -- {direction} blocked"}
    if mkt_dir == "SHORT" and direction != "SHORT":
        return {"status": "SKIPPED_MARKET_DIR", "reason": f"Market is SHORT -- {direction} blocked"}

    # GATE 2 -- regime -> side
    regime = get_market_regime()
    ok, reason = regime_allows_side(regime, direction)
    if not ok:
        return {"status": "SKIPPED_REGIME", "reason": reason, "regime": regime}

    # GATE 3 -- daily limit
    count = get_today_trade_count()
    if count >= MAX_TRADES_PER_DAY:
        return {"status": "SKIPPED_LIMIT", "reason": f"Daily limit {count}/{MAX_TRADES_PER_DAY}"}

    # GATE 4 -- entry range (live price must be within band)
    ltp = get_ltp(symbol) or entry_ref
    ok, reason = check_entry_range(direction, ltp, entry_low, entry_high)
    if not ok:
        return {"status": "SKIPPED_RANGE", "reason": reason}

    # GATE 5 -- sizing (Rs1L, mult of 5)
    sizing = size_by_notional(entry_price=ltp, direction=direction)
    if sizing.get("qty", 0) <= 0:
        return {"status": "REJECTED_SIZE", "reason": sizing.get("error", "zero qty")}

    # GATE 6 -- kill switch
    signal["capital_required"] = sizing.get("capital_required", 0)
    if not kill_switch_check(signal):
        return {"status": "BLOCKED_KILLSWITCH", "reason": "kill switch active"}

    qty = sizing["qty"]
    intent = {
        "symbol": symbol, "direction": direction, "qty": qty,
        "entry_price": round(ltp, 2), "notional": sizing["notional"],
        "product": sizing["product"], "regime": regime,
        "capital_required": sizing["capital_required"],
        "setup_name": signal.get("setup_name", ""),
    }

    # SHADOW -- log intent, place NO order
    if not LIVE_TRADING_ENABLED:
        log.info(f"[SHADOW] WOULD ENTER {direction} {qty} {symbol} @ Rs{ltp:.1f} | notional Rs{sizing['notional']:,.0f}")
        _log_intent(intent, shadow=True)
        return {"status": "SHADOW_INTENT", **intent}

    # LIVE -- place ENTRY order only (NO SL, NO target)
    order = place_order(symbol=symbol, direction=direction, qty=qty,
                        order_type="MARKET", tag="ATLAS")
    if not order.get("success"):
        return {"status": "ORDER_FAILED", "reason": order.get("reason", "order failed")}

    intent["order_id"] = order.get("order_id")
    _log_intent(intent, shadow=False)
    return {"status": "ENTERED", **intent}


def _log_intent(intent: dict, shadow: bool):
    """Persist the entry (or shadow intent) to atlas_trades."""
    rec = {
        "symbol": intent["symbol"], "direction": intent["direction"],
        "entry_price": intent["entry_price"], "qty": intent["qty"],
        "status": "SHADOW" if shadow else "OPEN",
        "entry_date": datetime.now(IST).date().isoformat(),
        "agent_mode": "SHADOW" if shadow else "LIVE",
        "setup_name": intent.get("setup_name", ""),
        "notes": (f"SHADOW intent -- no order placed | notional Rs{intent.get('notional',0):,.0f} | {intent.get('regime','')}" if shadow
                  else f"MANUAL RISK REQUIRED - Order {intent.get('order_id','')} | notional Rs{intent.get('notional',0):,.0f}"),
    }
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/atlas_trades",
                      headers=_headers(), json=rec, timeout=10)
    except Exception as e:
        log.warning(f"intent log failed: {e}")
