"""
THE STOCK LOGIC — what a round trip actually costs.

WHY THIS EXISTS. Nothing in the system modelled costs. atlas_trades.pnl is
(exit - entry) * qty, gross, and there were no cost columns at all. At the previous
Rs1,00,000 notional that was a rounding error; at Rs10,000 it is 5% to 11% of one R,
which is the difference between a measurement and a misleading one.

THE LOOP STILL MEASURES GROSS, deliberately. Costs are a separate question that
matters when real sizing returns, and mixing them into the outcome record would mean
the hit-rate hypotheses were testing the broker's fee schedule as well as the
engine's edge. So gross and net are recorded side by side and nothing averages them.

EVERY RATE BELOW IS UNVERIFIED. They are the published discount-broker structure as
I understand it on 2026-10-06, not figures read off a contract note, and a wrong rate
silently biases net P&L in one direction forever. Check them against an actual
contract note and correct RATES_VERIFIED_ON when you do.

THE ASYMMETRY IS THE OPPOSITE OF INTUITION, and it is worth knowing before reading
any net figure: a DELIVERY LONG costs about twice an INTRADAY SHORT at this size.
Delivery STT is 0.1% on both legs; intraday STT is 0.025% on the sell leg only, and
delivery brokerage is zero where intraday is not. On Rs10,000:

    LONG  (CNC)   ~Rs22.2   0.22% of notional   11% of 1R at a 2% stop
    SHORT (MIS)   ~Rs10.6   0.11% of notional    5% of 1R at a 2% stop
"""

import logging

log = logging.getLogger("ATLAS-COSTS")

# The date these rates were last reconciled against a contract note. None means
# never -- read every net figure with that in mind.
RATES_VERIFIED_ON = None

# Brokerage. Equity delivery is free; intraday is the LOWER of a flat fee and a
# percentage, and at Rs10,000 the percentage binds: 0.03% is Rs3, not Rs20. That
# distinction is worth four times the cost at this size.
BROKERAGE_FLAT_PER_ORDER = 20.0
BROKERAGE_PCT_PER_ORDER = 0.0003          # 0.03%
BROKERAGE_DELIVERY = 0.0                  # CNC: free

# Securities Transaction Tax
STT_DELIVERY_PCT = 0.001                  # 0.1%, BOTH legs
STT_INTRADAY_SELL_PCT = 0.00025           # 0.025%, SELL leg only

# Exchange transaction charges (NSE cash), on turnover
EXCHANGE_TXN_PCT = 0.0000297              # 0.00297%
SEBI_PCT = 0.000001                       # Rs10 per crore
GST_PCT = 0.18                            # on brokerage + exchange + SEBI
STAMP_DELIVERY_BUY_PCT = 0.00015          # 0.015%, BUY leg only
STAMP_INTRADAY_BUY_PCT = 0.00003          # 0.003%, BUY leg only


def _brokerage(turnover: float, product: str) -> float:
    if (product or "").upper() == "CNC":
        return BROKERAGE_DELIVERY
    return min(BROKERAGE_FLAT_PER_ORDER, turnover * BROKERAGE_PCT_PER_ORDER)


def round_trip(entry_price: float, exit_price: float, qty: int,
               product: str = "CNC") -> dict:
    """Total cost of entering and exiting one position. -> a breakdown.

    Both legs are priced at their own turnover, not at the entry twice: a position
    that moved 10% has a 10% larger sell-side turnover and the percentage charges
    follow it.
    """
    try:
        qty = int(qty)
        buy = float(entry_price) * qty
        sell = float(exit_price) * qty
    except (TypeError, ValueError):
        return {"total": 0.0, "error": "unusable prices or quantity"}
    if qty <= 0 or buy <= 0 or sell <= 0:
        return {"total": 0.0, "error": "non-positive turnover"}

    delivery = (product or "").upper() == "CNC"
    turnover = buy + sell

    brokerage = _brokerage(buy, product) + _brokerage(sell, product)
    stt = (STT_DELIVERY_PCT * turnover if delivery
           else STT_INTRADAY_SELL_PCT * sell)
    exch = EXCHANGE_TXN_PCT * turnover
    sebi = SEBI_PCT * turnover
    gst = GST_PCT * (brokerage + exch + sebi)
    stamp = (STAMP_DELIVERY_BUY_PCT if delivery else STAMP_INTRADAY_BUY_PCT) * buy

    total = brokerage + stt + exch + sebi + gst + stamp
    return {
        "brokerage": round(brokerage, 2), "stt": round(stt, 2),
        "exchange": round(exch, 2), "sebi": round(sebi, 4),
        "gst": round(gst, 2), "stamp_duty": round(stamp, 2),
        "total": round(total, 2),
        "pct_of_notional": round(100.0 * total / buy, 4),
        "product": (product or "").upper(),
        "rates_verified_on": RATES_VERIFIED_ON,
    }


def net_pnl(gross: float, entry_price: float, exit_price: float, qty: int,
            product: str = "CNC") -> tuple:
    """(net, costs_total). Gross is passed in rather than recomputed, so this can
    never disagree with the P&L the rest of the system already recorded."""
    c = round_trip(entry_price, exit_price, qty, product)
    total = float(c.get("total") or 0.0)
    return round(float(gross) - total, 2), total


if __name__ == "__main__":
    print(f"rates verified on: {RATES_VERIFIED_ON or 'NEVER — treat net figures as indicative'}")
    print(f"\n{'case':28} {'cost':>8} {'% notional':>11} {'% of 1R @2% stop':>17}")
    for label, px, ex, q, prod in (
            ("Rs10k LONG flat (CNC)", 1000.0, 1000.0, 10, "CNC"),
            ("Rs10k SHORT flat (MIS)", 1000.0, 1000.0, 10, "MIS"),
            ("Rs10k LONG +2% (CNC)", 1000.0, 1020.0, 10, "CNC"),
            ("Rs10k SHORT -2% (MIS)", 1000.0, 980.0, 10, "MIS"),
            ("Rs1,00,000 LONG (CNC)", 1000.0, 1000.0, 100, "CNC"),
            ("MRF one share (CNC)", 129290.0, 129290.0, 1, "CNC")):
        c = round_trip(px, ex, q, prod)
        notional = px * q
        r1 = notional * 0.02
        print(f"{label:28} {c['total']:8.2f} {c['pct_of_notional']:10.3f}% "
              f"{100*c['total']/r1:16.1f}%")
