"""
ATLAS Entry Logic -- Accumulation Architecture (Phase: TRAINING)
=================================================================
Agent IDENTIFIES and ENTERS. No SL, no target, no exit orders. Exits manual.

GATE STACK
  0. Structural stop present      -- required for risk-based sizing
  1. Regime -> side               -- accumulation in bull+sideways;
                                     shorts only when extreme_bearish
  2. Opening range                -- SHORTS ONLY (intraday directional check)
  3. Open positions               -- MAX_OPEN_POSITIONS
  4. New entries today            -- MAX_TRADES_PER_DAY (secondary)
  5. Entry range                  -- inside zone -> enter; above zone on a
                                     LONG -> rest a GTT at the zone edge
  6. Sizing                       -- Rs3k risk / Rs1L notional dual cap
  7. Capital deployed             -- MAX_CAPITAL_DEPLOYED, the real limit
  8. Kill switch

WHAT CHANGED FROM THE INTRADAY VERSION
--------------------------------------
OPENING-RANGE GATE now applies to SHORTS only. It asks whether Nifty broke its
first 15 minutes -- meaningful for an intraday hedge, noise for a multi-year
hold. Applied to longs it returned WAIT on flat days, which is exactly when
institutions accumulate and retail stops watching.

DAILY TRADE COUNT is no longer the binding constraint. Accumulation longs are
held indefinitely, so capital does not recycle. MAX_CAPITAL_DEPLOYED is what
actually stops further entries; the daily count is a secondary guard.

REGIME now comes from data/processed/market.parquet (bull / sideways / bear,
200 DMA with a 3% neutral band) rather than sector_heatmap's
bullish/bearish/mixed vocabulary. build_market.py writes it nightly in the EOD
chain, ahead of 02b. Falls back to sector_heatmap if the parquet is absent or
STALE; if both sources are stale the regime is 'unknown', which means no trade.
"""
import sys, requests, logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY, LIVE_TRADING_ENABLED,
    MAX_TRADES_PER_DAY, MAX_OPEN_POSITIONS, MAX_CAPITAL_DEPLOYED,
    ENFORCE_ENTRY_RANGE, OPENING_RANGE_GATE_APPLIES_TO,
    ALLOW_LONG_IN_BULLISH, ALLOW_LONG_IN_SIDEWAYS,
    ALLOW_SHORT_IN_BEARISH, REQUIRE_EXTREME_BEARISH_FOR_SHORTS,
    DEFAULT_ON_UNKNOWN_REGIME,
)
from atlas.risk.position_sizing import size_by_risk
from atlas.risk.kill_switch import check as kill_switch_check
from atlas.execution.broker import place_order, get_ltp

log = logging.getLogger("ATLAS-ENTRY")
IST = timezone(timedelta(hours=5, minutes=30))

MARKET_FILE = Path(__file__).parent.parent.parent / "data" / "processed" / "market.parquet"
OPEN_STATUSES = ("OPEN", "GTT_PENDING")

# Refuse a regime older than this. market.parquet is rebuilt nightly by
# build_market.py (EOD chain, ahead of 02b); a gap this wide means that job
# stopped running. Reading iloc[-1] unconditionally meant a months-old regime
# could gate live entries with no signal that anything was wrong. Sized to
# survive a long weekend plus an NSE holiday, and matched to
# market_open.MAX_BATCH_AGE_DAYS so the two staleness guards agree.
MAX_REGIME_AGE_DAYS = 5


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}


# ─────────────────────────────────────────────────────────────────────
# MARKET CONTEXT
# ─────────────────────────────────────────────────────────────────────

def _stale(as_of: str, label: str) -> bool:
    """True if `as_of` (YYYY-MM-DD) is older than MAX_REGIME_AGE_DAYS.

    An unparseable date is treated as STALE. This gate is fail-closed by
    design: a regime we cannot date is a regime we cannot trust, and
    regime_allows_side() turns 'unknown' into no-trade.
    """
    if not as_of:
        log.error(f"{label}: no date on regime row -- treating as stale")
        return True
    try:
        age = (datetime.now(IST).date() - date.fromisoformat(str(as_of)[:10])).days
    except Exception as e:
        log.error(f"{label}: unparseable regime date {as_of!r} ({e}) -- treating as stale")
        return True
    if age > MAX_REGIME_AGE_DAYS:
        log.error(f"STALE REGIME -- {label} is {age} days old ({as_of}); "
                  f"max {MAX_REGIME_AGE_DAYS}. Is build_market.py still running?")
        return True
    log.info(f"{label} regime {as_of} -- {age} day(s) old")
    return False


def get_market_context() -> dict:
    """Regime from market.parquet. Falls back to sector_heatmap if the parquet
    is absent OR stale; returns 'unknown' (-> no trade) if both are stale."""
    try:
        import pandas as pd
        if MARKET_FILE.exists():
            d = pd.read_parquet(MARKET_FILE).sort_values("date")
            if len(d):
                last = d.iloc[-1]
                as_of = str(last.get("date", ""))[:10]
                # Do NOT trust iloc[-1] on date alone -- build_market.py is the
                # only writer, and if it stops the last row sits there forever.
                if not _stale(as_of, "market.parquet"):
                    return {
                        "regime":             str(last.get("market_regime", "unknown")),
                        "allow_accumulation": bool(last.get("allow_accumulation", False)),
                        "extreme_bearish":    bool(last.get("extreme_bearish", False)),
                        "as_of":              as_of,
                        "source":             "market.parquet",
                    }
    except Exception as e:
        log.warning(f"market.parquet read failed: {e}")

    # Fallback -- sector_heatmap uses bullish/bearish/mixed
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/sector_heatmap?select=market_direction,signal_date"
            f"&order=signal_date.desc&limit=1", headers=_headers(), timeout=10)
        if r.status_code == 200 and r.json():
            row = r.json()[0]
            as_of = row.get("signal_date", "")
            # The fallback needs the same guard: it orders by signal_date desc
            # with no lower bound, so a dead 07_sector_momentum yields an
            # arbitrarily old row that still looks like a live answer.
            if not _stale(as_of, "sector_heatmap"):
                d = (row.get("market_direction") or "unknown").lower()
                mapped = {"bullish": "bull", "bearish": "bear",
                          "mixed": "sideways"}.get(d, "unknown")
                return {
                    "regime":             mapped,
                    "allow_accumulation": mapped in ("bull", "sideways"),
                    # Fallback cannot establish extreme_bearish -- it needs the
                    # 200DMA and VIX. Deny shorts rather than guess.
                    "extreme_bearish":    False,
                    "as_of":              as_of,
                    "source":             "sector_heatmap (fallback)",
                }
    except Exception as e:
        log.warning(f"regime fallback failed: {e}")

    log.error("No usable regime source -- both market.parquet and sector_heatmap "
              "are absent or stale. Refusing to trade.")
    return {"regime": "unknown", "allow_accumulation": False,
            "extreme_bearish": False, "as_of": "", "source": "none"}


def regime_allows_side(ctx: dict, direction: str) -> tuple:
    """Accumulation in bull+sideways. Shorts only when genuinely bearish."""
    d = direction.upper()
    regime = ctx.get("regime", "unknown")

    if regime == "unknown":
        return False, f"regime unknown/stale -> {DEFAULT_ON_UNKNOWN_REGIME}"

    if d == "LONG":
        if not ctx.get("allow_accumulation"):
            return False, f"{regime} regime -- accumulation blocked"
        if regime == "bull" and not ALLOW_LONG_IN_BULLISH:
            return False, "longs disabled in bull"
        if regime == "sideways" and not ALLOW_LONG_IN_SIDEWAYS:
            return False, "longs disabled in sideways"
        return True, f"{regime} -> accumulation permitted"

    if d == "SHORT":
        if REQUIRE_EXTREME_BEARISH_FOR_SHORTS and not ctx.get("extreme_bearish"):
            return False, ("hedge shorts require extreme_bearish "
                           "(200DMA-3%, 50<200DMA, VIX>18)")
        if not ALLOW_SHORT_IN_BEARISH:
            return False, "shorts disabled"
        return True, "extreme bearish -> hedge short permitted"

    return False, f"unknown direction {d}"


# ─────────────────────────────────────────────────────────────────────
# EXPOSURE
# ─────────────────────────────────────────────────────────────────────

def get_exposure() -> dict:
    """Live positions and capital deployed. GTT_PENDING counts -- the capital
    is committed the moment the trigger rests, even before it fills."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?agent_mode=eq.LIVE&status=in.({','.join(OPEN_STATUSES)})"
            f"&select=entry_price,qty,status", headers=_headers(), timeout=10)
        if r.status_code != 200:
            log.error(f"exposure query failed: HTTP {r.status_code}")
            return {"positions": 0, "deployed": 0.0, "ok": False}
        rows = r.json() or []
        deployed = sum(float(x.get("entry_price") or 0) * float(x.get("qty") or 0)
                       for x in rows)
        return {"positions": len(rows), "deployed": deployed, "ok": True}
    except Exception as e:
        log.error(f"exposure fetch failed: {e}")
        return {"positions": 0, "deployed": 0.0, "ok": False}


def get_today_entry_count() -> int:
    """New LIVE entries recorded today."""
    today = datetime.now(IST).date().isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?entry_date=eq.{today}&agent_mode=eq.LIVE&select=id",
            headers=_headers(), timeout=10)
        return len(r.json()) if r.status_code == 200 else 0
    except Exception:
        return 0


def check_entry_range(direction: str, ltp: float, lo_in: float, hi_in: float) -> tuple:
    """Enter ONLY if LTP is inside the zone band."""
    if not ENFORCE_ENTRY_RANGE:
        return True, "range check off"
    if not lo_in or not hi_in or lo_in <= 0 or hi_in <= 0:
        return False, "no entry band defined"
    lo, hi = min(lo_in, hi_in), max(lo_in, hi_in)
    if lo <= ltp <= hi:
        return True, f"LTP Rs{ltp:.1f} inside zone Rs{lo:.1f}-Rs{hi:.1f}"
    return False, f"LTP Rs{ltp:.1f} outside zone Rs{lo:.1f}-Rs{hi:.1f}"


# ─────────────────────────────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────────────────────────────

def enter_trade(signal: dict) -> dict:
    symbol     = signal.get("symbol", "")
    direction  = signal.get("direction", "LONG").upper()
    entry_ref  = float(signal.get("entry_ref", signal.get("entry", 0)) or 0)
    entry_low  = float(signal.get("entry_low", 0) or 0)
    entry_high = float(signal.get("entry_high", 0) or 0)
    stop_price = float(signal.get("sl", 0) or 0)
    mode = "LIVE" if LIVE_TRADING_ENABLED else "SHADOW"

    log.info(f"[{mode}] Evaluating {symbol} {direction}")

    # GATE 0 -- structural stop mandatory
    if stop_price <= 0:
        return {"status": "REJECTED_NO_STOP",
                "reason": "no structural stop -- cannot size by risk"}

    # GATE 1 -- regime -> side
    ctx = get_market_context()
    ok, reason = regime_allows_side(ctx, direction)
    if not ok:
        return {"status": "SKIPPED_REGIME", "reason": reason,
                "regime": ctx.get("regime"), "regime_source": ctx.get("source")}

    # GATE 2 -- opening range. SHORTS ONLY.
    if direction in OPENING_RANGE_GATE_APPLIES_TO:
        try:
            from atlas.execution.index_state import get_market_direction
            mkt_dir = get_market_direction().get("direction", "WAIT")
        except Exception as e:
            log.warning(f"opening-range check failed: {e}")
            mkt_dir = "WAIT"
        if mkt_dir != "SHORT":
            return {"status": "SKIPPED_MARKET_WAIT",
                    "reason": f"opening range is {mkt_dir} -- hedge short needs SHORT"}

    # GATE 3 -- open positions
    # FAIL CLOSED. If exposure cannot be read, we do not know how much capital
    # is already committed. Trading blind here could breach every capital limit
    # at once, so refuse rather than assume zero.
    exp = get_exposure()
    if not exp.get("ok"):
        return {"status": "BLOCKED_NO_EXPOSURE_DATA",
                "reason": "cannot read current exposure -- refusing to trade blind"}
    if exp["positions"] >= MAX_OPEN_POSITIONS:
        return {"status": "SKIPPED_LIMIT",
                "reason": f"open positions {exp['positions']}/{MAX_OPEN_POSITIONS}"}

    # GATE 4 -- new entries today (secondary; capital usually binds first)
    todays = get_today_entry_count()
    if todays >= MAX_TRADES_PER_DAY:
        return {"status": "SKIPPED_LIMIT",
                "reason": f"entries today {todays}/{MAX_TRADES_PER_DAY}"}

    # GATE 5 -- entry range, or rest a GTT at the zone
    ltp = get_ltp(symbol) or entry_ref
    in_range, range_reason = check_entry_range(direction, ltp, entry_low, entry_high)

    if not in_range:
        if direction != "LONG":
            return {"status": "SKIPPED_RANGE",
                    "reason": f"{range_reason} (no GTT for shorts -- CNC-only)"}
        if entry_ref <= 0 or entry_ref >= ltp:
            return {"status": "SKIPPED_RANGE",
                    "reason": f"{range_reason} (zone not below LTP -- no GTT)"}
        return _place_gtt_entry(signal, symbol, entry_ref, stop_price, ltp, ctx, exp)

    # GATE 6 -- sizing
    sizing = size_by_risk(entry_price=ltp, stop_price=stop_price, direction=direction)
    if sizing.get("qty", 0) <= 0:
        return {"status": "REJECTED_SIZE", "reason": sizing.get("error", "zero qty")}

    # GATE 7 -- capital deployed
    need = sizing["capital_required"]
    if exp["deployed"] + need > MAX_CAPITAL_DEPLOYED:
        return {"status": "SKIPPED_CAPITAL",
                "reason": (f"deployed Rs{exp['deployed']:,.0f} + Rs{need:,.0f} "
                           f"exceeds cap Rs{MAX_CAPITAL_DEPLOYED:,.0f}")}

    # GATE 8 -- kill switch
    signal["capital_required"] = need
    if not kill_switch_check(signal):
        return {"status": "BLOCKED_KILLSWITCH", "reason": "kill switch active"}

    intent = _build_intent(signal, symbol, direction, ltp, sizing, ctx)

    if not LIVE_TRADING_ENABLED:
        log.info(f"[SHADOW] WOULD ENTER {direction} {sizing['qty']} {symbol} @ Rs{ltp:.1f} "
                 f"| stop Rs{stop_price:.1f} ({sizing['stop_pct']}%) "
                 f"| risk Rs{sizing['risk_actual']:,.0f}")
        _log_intent(intent, shadow=True)
        return {"status": "SHADOW_INTENT", **intent}

    order = place_order(symbol=symbol, direction=direction, qty=sizing["qty"],
                        order_type="MARKET", tag="ATLAS", product=sizing["product"])
    if not order.get("success"):
        return {"status": "ORDER_FAILED", "reason": order.get("reason", "order failed")}

    intent["order_id"] = order.get("order_id")
    _log_intent(intent, shadow=False)
    return {"status": "ENTERED", **intent}


def _build_intent(signal, symbol, direction, price, sizing, ctx) -> dict:
    return {
        "symbol": symbol, "direction": direction, "qty": sizing["qty"],
        "entry_price": round(price, 2), "stop_price": sizing["stop_price"],
        "stop_pct": sizing["stop_pct"], "risk_actual": sizing["risk_actual"],
        "notional": sizing["notional"], "product": sizing["product"],
        "binding_cap": sizing["binding_cap"], "regime": ctx.get("regime", ""),
        "capital_required": sizing["capital_required"],
        "setup_name": signal.get("setup_name", ""), "session": signal.get("session", ""),
        "score": signal.get("score", 0), "grade": signal.get("grade", ""),
        "sector": signal.get("sector", ""), "zone_source": signal.get("zone_source", ""),
    }


def _place_gtt_entry(signal, symbol, entry_ref, stop_price, ltp, ctx, exp) -> dict:
    """Price sits above the zone on a LONG. Size at the ZONE price -- that is
    where the fill happens -- and rest a GTT trigger there."""
    from atlas.execution.gtt import place_zone_gtt

    sizing = size_by_risk(entry_price=entry_ref, stop_price=stop_price, direction="LONG")
    if sizing.get("qty", 0) <= 0:
        return {"status": "REJECTED_SIZE", "reason": sizing.get("error", "zero qty")}

    need = sizing["capital_required"]
    if exp["deployed"] + need > MAX_CAPITAL_DEPLOYED:
        return {"status": "SKIPPED_CAPITAL",
                "reason": (f"deployed Rs{exp['deployed']:,.0f} + Rs{need:,.0f} "
                           f"exceeds cap Rs{MAX_CAPITAL_DEPLOYED:,.0f}")}

    if not kill_switch_check({**signal, "capital_required": need}):
        return {"status": "BLOCKED_KILLSWITCH", "reason": "kill switch active"}

    intent = _build_intent(signal, symbol, "LONG", entry_ref, sizing, ctx)
    intent["product"] = "CNC"

    if not LIVE_TRADING_ENABLED:
        log.info(f"[SHADOW] WOULD PLACE GTT {sizing['qty']} {symbol} "
                 f"trigger Rs{entry_ref:.1f} (LTP Rs{ltp:.1f})")
        _log_intent(intent, shadow=True)
        return {"status": "SHADOW_GTT", **intent}

    gtt = place_zone_gtt(symbol=symbol, qty=sizing["qty"],
                         trigger_price=entry_ref, last_price=ltp)
    if not gtt.get("success"):
        return {"status": "GTT_FAILED", "reason": gtt.get("reason", "gtt failed")}

    intent["gtt_trigger_id"] = gtt["trigger_id"]
    intent["trigger_price"]  = gtt["trigger_price"]
    _log_intent(intent, shadow=False, gtt=True)
    return {"status": "GTT_PLACED", **intent}


def _log_intent(intent: dict, shadow: bool, gtt: bool = False):
    """status: SHADOW | GTT_PENDING (trigger resting) | OPEN (filled)."""
    rec = {
        "symbol": intent["symbol"], "direction": intent["direction"],
        "entry_price": intent["entry_price"], "qty": intent["qty"],
        "stop_price": intent.get("stop_price"),
        "gtt_trigger_id": intent.get("gtt_trigger_id"),
        "trigger_price": intent.get("trigger_price"),
        "status": "SHADOW" if shadow else ("GTT_PENDING" if gtt else "OPEN"),
        "entry_date": datetime.now(IST).date().isoformat(),
        "agent_mode": "SHADOW" if shadow else "LIVE",
        "setup_name": intent.get("setup_name", ""),
        "session": intent.get("session", ""),
        "score": intent.get("score", 0),
        "grade": intent.get("grade", ""),
        "sector": intent.get("sector", ""),
        "zone_source": intent.get("zone_source", ""),
        "notes": (
            f"SHADOW -- no order placed | stop Rs{intent.get('stop_price',0)} "
            f"({intent.get('stop_pct',0)}%) | risk Rs{intent.get('risk_actual',0):,.0f} "
            f"| notional Rs{intent.get('notional',0):,.0f} | {intent.get('regime','')}"
            if shadow else
            (f"GTT RESTING at Rs{intent.get('trigger_price',0)} -- not filled. "
             f"On fill place SL at Rs{intent.get('stop_price',0)} "
             f"| trigger {intent.get('gtt_trigger_id','')} "
             f"| risk Rs{intent.get('risk_actual',0):,.0f}"
             if gtt else
             f"MANUAL RISK REQUIRED - place SL at Rs{intent.get('stop_price',0)} "
             f"| Order {intent.get('order_id','')} "
             f"| risk Rs{intent.get('risk_actual',0):,.0f} "
             f"| notional Rs{intent.get('notional',0):,.0f}")
        ),
    }
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/atlas_trades",
                      headers=_headers(), json=rec, timeout=10)
    except Exception as e:
        log.warning(f"intent log failed: {e}")
