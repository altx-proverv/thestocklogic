"""
ATLAS Position Sizing — Risk-Based (₹3,000/trade) with Notional Cap
====================================================================
Supersedes size_by_notional(). Reinstates risk-based sizing, which is
correct again now that structural stops exist (it was archived 4 Aug only
because the no-SL rule made it meaningless).

    risk_per_share = |entry - stop|
    qty_risk       = MAX_RISK_PER_TRADE / risk_per_share
    qty_notional   = MAX_NOTIONAL_PER_TRADE / entry
    qty            = floor_to_5( min(qty_risk, qty_notional) )

BOTH caps apply, lower wins. Without the notional cap a tight stop produces
absurd size: entry 2000, stop 1990 -> 300 sh -> ₹6L notional on a ₹3k budget.

STOP-DISTANCE FILTER (quality gate, not just a guard). Band comes from
config.MIN_STOP_PCT / MAX_STOP_PCT, shared with engine/zone_entry.py:
  < 1.5%  -> reject. Inside normal noise; also forces the notional cap to
             bind, which breaks the ₹3k standardisation.
  > 7.0%  -> reject. Position too small for costs to be worth it. Was 6.0
             here while zone_entry published against 7.0, so setups with a
             6-7% stop were published and then refused at entry. 7.0 is the
             measured ceiling -- swing-low distances cluster 2-9%.
Signals with clean, appropriately-distant structure are better signals.

PRODUCT: longs CNC (delivery, hold winners, GTT-eligible).
         shorts MIS (intraday only, bearish regime, morning session).
"""

import sys, logging
from math import floor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
# Rules 1, 2, 3 and 16 come from config. This module used to restate them as
# RISK_PER_TRADE / MAX_NOTIONAL / QTY_MULTIPLE and a bare 0.20, which made
# config.py's documented rules decorative: editing MAX_RISK_PER_TRADE there
# changed the operator-facing text and nothing about how a position was
# actually sized. Two copies of one number with nothing reconciling them is
# exactly what let the kill switch enforce Rs4,500 against a documented
# Rs9,000 -- see the capital note in config.py.
from atlas.config import (
    MAX_RISK_PER_TRADE, MAX_NOTIONAL_PER_TRADE, QUANTITY_MULTIPLE,
    SHORT_MARGIN_PCT_ESTIMATE, MIN_STOP_PCT, MAX_STOP_PCT,
)

log = logging.getLogger("ATLAS-SIZE")

# config states the band in PERCENT. This module compares against a FRACTION,
# abs(entry-stop)/entry. Convert once here and compare only against the _FRAC
# names: comparing a fraction to 7.0 is a 700% ceiling that rejects nothing,
# and neither mistake raises. The band was 0.060 here against zone_entry's 7.0,
# which is how a 6-7% stop got published and then refused at entry.
MIN_STOP_FRAC = MIN_STOP_PCT / 100.0
MAX_STOP_FRAC = MAX_STOP_PCT / 100.0


def _floor_to_multiple(n: float, m: int = QUANTITY_MULTIPLE) -> int:
    return int(floor(n / m) * m)


def validate_stop(entry_price: float, stop_price: float, direction: str) -> tuple:
    """(ok, stop_pct, reason). Checks side sanity and distance band."""
    d = (direction or "").upper()

    if not entry_price or entry_price <= 0:
        return False, 0.0, "invalid entry price"
    if not stop_price or stop_price <= 0:
        return False, 0.0, "no structural stop -- zone/swing not resolved"

    if d == "LONG" and stop_price >= entry_price:
        return False, 0.0, f"long stop {stop_price:.2f} must sit BELOW entry {entry_price:.2f}"
    if d == "SHORT" and stop_price <= entry_price:
        return False, 0.0, f"short stop {stop_price:.2f} must sit ABOVE entry {entry_price:.2f}"

    stop_pct = abs(entry_price - stop_price) / entry_price

    if stop_pct < MIN_STOP_FRAC:
        return False, stop_pct, (f"stop {stop_pct*100:.2f}% too tight "
                                 f"(min {MIN_STOP_PCT:.1f}%) -- inside noise")
    if stop_pct > MAX_STOP_FRAC:
        return False, stop_pct, (f"stop {stop_pct*100:.2f}% too wide "
                                 f"(max {MAX_STOP_PCT:.1f}%) -- size too small to justify costs")

    return True, stop_pct, f"stop {stop_pct*100:.2f}% within band"


def size_by_risk(entry_price: float, stop_price: float, direction: str,
                 risk_per_trade: float = MAX_RISK_PER_TRADE,
                 max_notional: float = MAX_NOTIONAL_PER_TRADE,
                 available_funds: float = None) -> dict:
    """
    Returns dict with qty / notional / risk_actual / product, or qty=0 + error.
    risk_actual is reported explicitly -- flooring to a multiple of 5 always
    reduces real risk below the ₹3k budget, sometimes materially on high-priced
    stocks. Never assume risk == 3000.
    """
    d = (direction or "").upper()
    ok, stop_pct, reason = validate_stop(entry_price, stop_price, d)
    if not ok:
        return {"qty": 0, "error": reason, "stop_pct": stop_pct}

    risk_per_share = abs(entry_price - stop_price)
    qty_risk       = risk_per_trade / risk_per_share
    qty_notional   = max_notional / entry_price
    binding        = "risk" if qty_risk <= qty_notional else "notional"

    qty = _floor_to_multiple(min(qty_risk, qty_notional))

    if qty < QUANTITY_MULTIPLE:
        return {"qty": 0,
                "error": (f"qty {qty} below minimum {QUANTITY_MULTIPLE} -- "
                          f"stock too expensive for ₹{risk_per_trade:,.0f} risk "
                          f"at a {stop_pct*100:.2f}% stop"),
                "stop_pct": stop_pct}

    notional    = qty * entry_price
    risk_actual = qty * risk_per_share
    product     = "CNC" if d == "LONG" else "MIS"

    # Longs are delivery: full value blocked. Shorts intraday: ~20% margin.
    capital_required = (notional if d == "LONG"
                        else notional * SHORT_MARGIN_PCT_ESTIMATE)

    if available_funds is not None and capital_required > available_funds:
        reduced = _floor_to_multiple(
            available_funds / (entry_price if d == "LONG"
                               else entry_price * SHORT_MARGIN_PCT_ESTIMATE))
        if reduced < QUANTITY_MULTIPLE:
            return {"qty": 0,
                    "error": f"insufficient funds: need ₹{capital_required:,.0f}, have ₹{available_funds:,.0f}",
                    "stop_pct": stop_pct}
        log.warning(f"reduced {qty} -> {reduced} to fit available funds")
        qty              = reduced
        notional         = qty * entry_price
        risk_actual      = qty * risk_per_share
        capital_required = (notional if d == "LONG"
                            else notional * SHORT_MARGIN_PCT_ESTIMATE)
        binding          = "funds"

    return {
        "qty":              qty,
        "entry_price":      round(entry_price, 2),
        "stop_price":       round(stop_price, 2),
        "stop_pct":         round(stop_pct * 100, 2),
        "risk_per_share":   round(risk_per_share, 2),
        "risk_actual":      round(risk_actual, 2),
        "risk_budget":      risk_per_trade,
        "notional":         round(notional, 2),
        "capital_required": round(capital_required, 2),
        "product":          product,
        "binding_cap":      binding,
        "reason":           reason,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cases = [
        ("normal long",      2008.0, 1960.0, "LONG"),
        ("tight stop",       2008.0, 1998.0, "LONG"),   # rejected: 0.5%
        ("wide stop",        2008.0, 1850.0, "LONG"),   # rejected: 7.9%
        ("6-7% stop",        1000.0,  935.0, "LONG"),   # accepted: 6.5% -- was
                                                        # rejected here while
                                                        # zone_entry published it
        ("expensive stock",  5720.0, 5490.0, "LONG"),   # notional cap binds
        ("short intraday",   1078.0, 1105.0, "SHORT"),
        ("inverted stop",    2008.0, 2050.0, "LONG"),   # rejected: wrong side
    ]
    for label, e, s, d in cases:
        r = size_by_risk(e, s, d)
        if r["qty"]:
            print(f"{label:18} qty={r['qty']:>5}  notional=₹{r['notional']:>10,.0f}  "
                  f"risk=₹{r['risk_actual']:>7,.0f}  stop={r['stop_pct']}%  cap={r['binding_cap']}")
        else:
            print(f"{label:18} REJECTED — {r['error']}")
