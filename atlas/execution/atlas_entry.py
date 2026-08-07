"""
ATLAS Entry Logic -- TSL Rules Compliant (Phase: TRAINING)
==========================================================
Agent IDENTIFIES and ENTERS trades only. No SL, no target, no exit orders.
All exits are MANUAL this phase. Exit management is a separate module gated by
ENABLE_EXIT_MANAGEMENT (off now).

Gate stack (all must pass, in order):
  1. Market direction -- Nifty opening-range (LONG/SHORT/WAIT). WAIT -> cash.
  2. Regime -> side   -- bullish=long, bearish=short, mixed=either, unknown=cash
  3. Daily limit      -- MAX_TRADES_PER_DAY, counting LIVE trades only
  4. Entry range      -- LTP within the zone band, else skip (no chasing)
  5. Sizing           -- Rs3k risk / Rs1L notional dual cap, structural stop
  6. Kill switch
Then: ENTER ONLY. Never places SL/target. Notify MANUAL RISK REQUIRED.

CHANGES THIS REVISION
---------------------
  - size_by_notional -> size_by_risk. Sizing now derives qty from the distance
    between entry and the STRUCTURAL STOP (swing low/high), giving a standard
    Rs3,000 risk budget per trade with a Rs1L notional ceiling. A signal
    without a valid stop cannot be sized and is rejected.
  - Stop-distance band (1.5-7%) enforced inside sizing -- doubles as a quality
    filter: signals with clean, appropriately-distant structure survive.
  - Daily trade count now filters agent_mode=LIVE. Shadow intents were
    consuming live slots.
  - _log_intent writes session/score/grade/sector/stop_price/zone_source.
    Without these, /atlas-live shows "--" and the learning loop has no
    attribution data to work from.

NOTE: the stop is recorded, NOT placed. ATLAS does not send SL orders in this
phase -- the operator places them manually. stop_price is the sizing input and
the trade's invalidation level.
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
from atlas.risk.position_sizing import size_by_risk
from atlas.risk.kill_switch import check as kill_switch_check
from atlas.execution.broker import place_order, get_ltp

log = logging.getLogger("ATLAS-ENTRY")
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}


def get_market_regime() -> str:
    """Latest market_direction from sector_heatmap. bullish/bearish/mixed/unknown."""
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
    """Rules 8,9,10,11. Returns (allowed, reason)."""
    d = direction.upper()
    if regime == "bullish":
        if d == "LONG" and ALLOW_LONG_IN_BULLISH: return True, "bullish->long ok"
        return False, "bullish regime -- shorts blocked (rule 8)"
    if regime == "bearish":
        if d == "SHORT" and ALLOW_SHORT_IN_BEARISH: return True, "bearish->short ok"
        return False, "bearish regime -- longs blocked (rule 10)"
    if regime == "mixed":
        return True, "mixed regime -- side permitted, other gates apply"
    return False, f"regime unknown/stale -> {DEFAULT_ON_UNKNOWN_REGIME} (no trade)"


def check_entry_range(direction: str, ltp: float, entry_low: float, entry_high: float) -> tuple:
    """Enter ONLY if LTP is inside the zone band. Both directions."""
    if not ENFORCE_ENTRY_RANGE:
        return True, "range check off"
    if not entry_low or not entry_high or entry_low <= 0 or entry_high <= 0:
        return False, "no entry band defined -- cannot verify range"
    lo, hi = min(entry_low, entry_high), max(entry_low, entry_high)
    if lo <= ltp <= hi:
        return True, f"LTP Rs{ltp:.1f} within zone Rs{lo:.1f}-Rs{hi:.1f}"
    return False, f"LTP Rs{ltp:.1f} outside zone Rs{lo:.1f}-Rs{hi:.1f} -- no chase"


def get_today_trade_count() -> int:
    """LIVE trades taken today. Shadow intents do NOT consume live slots."""
    today = datetime.now(IST).date().isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?entry_date=eq.{today}&agent_mode=eq.LIVE&select=id",
            headers=_headers(), timeout=10)
        return len(r.json()) if r.status_code == 200 else 0
    except Exception:
        return 0


def enter_trade(signal: dict) -> dict:
    """Full gate stack + enter-only. Returns status dict.
    SHADOW (LIVE_TRADING_ENABLED=false): logs intent, places NO order."""
    symbol     = signal.get("symbol", "")
    direction  = signal.get("direction", "LONG").upper()
    entry_ref  = float(signal.get("entry_ref", signal.get("entry", 0)) or 0)
    entry_low  = float(signal.get("entry_low", 0) or 0)
    entry_high = float(signal.get("entry_high", 0) or 0)
    stop_price = float(signal.get("sl", 0) or 0)
    mode = "LIVE" if LIVE_TRADING_ENABLED else "SHADOW"

    log.info(f"[{mode}] Evaluating {symbol} {direction}")

    # GATE 0 -- structural stop is mandatory for risk-based sizing
    if stop_price <= 0:
        return {"status": "REJECTED_NO_STOP",
                "reason": "no structural stop on signal -- cannot size by risk"}

    # GATE 1 -- MARKET DIRECTION (Nifty opening range). WAIT -> cash.
    try:
        from atlas.execution.index_state import get_market_direction
        mkt = get_market_direction()
        mkt_dir = mkt.get("direction", "WAIT")
    except Exception as e:
        log.warning(f"market direction check failed: {e}")
        mkt_dir = "WAIT"
    if mkt_dir == "WAIT":
        return {"status": "SKIPPED_MARKET_WAIT",
                "reason": "No clear Nifty opening-range direction -- staying cash"}
    if mkt_dir == "LONG" and direction != "LONG":
        return {"status": "SKIPPED_MARKET_DIR", "reason": f"Market is LONG -- {direction} blocked"}
    if mkt_dir == "SHORT" and direction != "SHORT":
        return {"status": "SKIPPED_MARKET_DIR", "reason": f"Market is SHORT -- {direction} blocked"}

    # GATE 2 -- regime -> side
    regime = get_market_regime()
    ok, reason = regime_allows_side(regime, direction)
    if not ok:
        return {"status": "SKIPPED_REGIME", "reason": reason, "regime": regime}

    # GATE 3 -- daily limit (LIVE trades only)
    count = get_today_trade_count()
    if count >= MAX_TRADES_PER_DAY:
        return {"status": "SKIPPED_LIMIT", "reason": f"Daily limit {count}/{MAX_TRADES_PER_DAY}"}

    # GATE 4 -- entry range (live price must be inside the zone)
    ltp = get_ltp(symbol) or entry_ref
    ok, reason = check_entry_range(direction, ltp, entry_low, entry_high)
    if not ok:
        return {"status": "SKIPPED_RANGE", "reason": reason}

    # GATE 5 -- sizing (Rs3k risk / Rs1L notional, stop-distance band enforced)
    sizing = size_by_risk(entry_price=ltp, stop_price=stop_price, direction=direction)
    if sizing.get("qty", 0) <= 0:
        return {"status": "REJECTED_SIZE", "reason": sizing.get("error", "zero qty")}

    # GATE 6 -- kill switch
    signal["capital_required"] = sizing.get("capital_required", 0)
    if not kill_switch_check(signal):
        return {"status": "BLOCKED_KILLSWITCH", "reason": "kill switch active"}

    qty = sizing["qty"]
    intent = {
        "symbol": symbol, "direction": direction, "qty": qty,
        "entry_price": round(ltp, 2), "stop_price": sizing["stop_price"],
        "stop_pct": sizing["stop_pct"], "risk_actual": sizing["risk_actual"],
        "notional": sizing["notional"], "product": sizing["product"],
        "binding_cap": sizing["binding_cap"], "regime": regime,
        "capital_required": sizing["capital_required"],
        "setup_name": signal.get("setup_name", ""),
        "session": signal.get("session", ""),
        "score": signal.get("score", 0),
        "grade": signal.get("grade", ""),
        "sector": signal.get("sector", ""),
        "zone_source": signal.get("zone_source", ""),
    }

    # SHADOW -- log intent, place NO order
    if not LIVE_TRADING_ENABLED:
        log.info(f"[SHADOW] WOULD ENTER {direction} {qty} {symbol} @ Rs{ltp:.1f} "
                 f"| stop Rs{stop_price:.1f} ({sizing['stop_pct']}%) "
                 f"| risk Rs{sizing['risk_actual']:,.0f} | notional Rs{sizing['notional']:,.0f}")
        _log_intent(intent, shadow=True)
        return {"status": "SHADOW_INTENT", **intent}

    # LIVE -- place ENTRY order only (NO SL, NO target)
    order = place_order(symbol=symbol, direction=direction, qty=qty,
                        order_type="MARKET", tag="ATLAS",
                        product=sizing["product"])
    if not order.get("success"):
        return {"status": "ORDER_FAILED", "reason": order.get("reason", "order failed")}

    intent["order_id"] = order.get("order_id")
    _log_intent(intent, shadow=False)
    return {"status": "ENTERED", **intent}


def _log_intent(intent: dict, shadow: bool):
    """Persist the entry (or shadow intent) to atlas_trades, with the
    attribution fields the learning loop needs."""
    rec = {
        "symbol": intent["symbol"], "direction": intent["direction"],
        "entry_price": intent["entry_price"], "qty": intent["qty"],
        "stop_price": intent.get("stop_price"),
        "status": "SHADOW" if shadow else "OPEN",
        "entry_date": datetime.now(IST).date().isoformat(),
        "agent_mode": "SHADOW" if shadow else "LIVE",
        "setup_name": intent.get("setup_name", ""),
        "session": intent.get("session", ""),
        "score": intent.get("score", 0),
        "grade": intent.get("grade", ""),
        "sector": intent.get("sector", ""),
        "zone_source": intent.get("zone_source", ""),
        "notes": (
            f"SHADOW intent -- no order placed | stop Rs{intent.get('stop_price',0)} "
            f"({intent.get('stop_pct',0)}%) | risk Rs{intent.get('risk_actual',0):,.0f} "
            f"| notional Rs{intent.get('notional',0):,.0f} | cap:{intent.get('binding_cap','')} "
            f"| {intent.get('regime','')}"
            if shadow else
            f"MANUAL RISK REQUIRED - place SL at Rs{intent.get('stop_price',0)} "
            f"| Order {intent.get('order_id','')} | risk Rs{intent.get('risk_actual',0):,.0f} "
            f"| notional Rs{intent.get('notional',0):,.0f}"
        ),
    }
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/atlas_trades",
                      headers=_headers(), json=rec, timeout=10)
    except Exception as e:
        log.warning(f"intent log failed: {e}")
