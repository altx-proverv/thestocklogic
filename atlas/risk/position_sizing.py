"""
ATLAS Position Sizing — Risk-Based (₹3,000/trade) with Notional Cap
====================================================================
Supersedes size_by_notional(). Reinstates risk-based sizing, which is
correct again now that structural stops exist (it was archived 4 Aug only
because the no-SL rule made it meaningless).

    risk_per_share = |entry - stop|
    qty_risk       = RISK_PER_TRADE / risk_per_share
    qty_notional   = MAX_NOTIONAL / entry
    qty            = floor_to_5( min(qty_risk, qty_notional) )

BOTH caps apply, lower wins. Without the notional cap a tight stop produces
absurd size: entry 2000, stop 1990 -> 300 sh -> ₹6L notional on a ₹3k budget.

STOP-DISTANCE FILTER (quality gate, not just a guard):
  < 1.5%  -> reject. Inside normal noise; also forces the notional cap to
             bind, which breaks the ₹3k standardisation.
  > 6.0%  -> reject. Position too small for costs to be worth it.
Signals with clean, appropriately-distant structure are better signals.

PRODUCT: longs CNC (delivery, hold winners, GTT-eligible).
         shorts MIS (intraday only, bearish regime, morning session).
"""

import logging
from math import floor

log = logging.getLogger("ATLAS-SIZE")

RISK_PER_TRADE = 3000.0     # rupees at risk if stop is hit
MAX_NOTIONAL   = 100000.0   # ₹1L per trade
QTY_MULTIPLE   = 5
MIN_STOP_PCT   = 0.015      # 1.5%
MAX_STOP_PCT   = 0.060      # 6.0%


def _floor_to_multiple(n: float, m: int = QTY_MULTIPLE) -> int:
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

    if stop_pct < MIN_STOP_PCT:
        return False, stop_pct, (f"stop {stop_pct*100:.2f}% too tight "
                                 f"(min {MIN_STOP_PCT*100:.1f}%) -- inside noise")
    if stop_pct > MAX_STOP_PCT:
        return False, stop_pct, (f"stop {stop_pct*100:.2f}% too wide "
                                 f"(max {MAX_STOP_PCT*100:.1f}%) -- size too small to justify costs")

    return True, stop_pct, f"stop {stop_pct*100:.2f}% within band"


def size_by_risk(entry_price: float, stop_price: float, direction: str,
                 risk_per_trade: float = RISK_PER_TRADE,
                 max_notional: float = MAX_NOTIONAL,
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

    if qty < QTY_MULTIPLE:
        return {"qty": 0,
                "error": (f"qty {qty} below minimum {QTY_MULTIPLE} -- "
                          f"stock too expensive for ₹{risk_per_trade:,.0f} risk "
                          f"at a {stop_pct*100:.2f}% stop"),
                "stop_pct": stop_pct}

    notional    = qty * entry_price
    risk_actual = qty * risk_per_share
    product     = "CNC" if d == "LONG" else "MIS"

    # Longs are delivery: full value blocked. Shorts intraday: ~20% margin.
    capital_required = notional if d == "LONG" else notional * 0.20

    if available_funds is not None and capital_required > available_funds:
        reduced = _floor_to_multiple(
            available_funds / (entry_price if d == "LONG" else entry_price * 0.20))
        if reduced < QTY_MULTIPLE:
            return {"qty": 0,
                    "error": f"insufficient funds: need ₹{capital_required:,.0f}, have ₹{available_funds:,.0f}",
                    "stop_pct": stop_pct}
        log.warning(f"reduced {qty} -> {reduced} to fit available funds")
        qty              = reduced
        notional         = qty * entry_price
        risk_actual      = qty * risk_per_share
        capital_required = notional if d == "LONG" else notional * 0.20
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
