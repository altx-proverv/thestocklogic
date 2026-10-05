"""
Does the measuring instrument size the way it claims to?

ATLAS restarted live on 2026-10-06 at a fixed Rs10,000 notional. This is a
measurement, not a capital allocation, and the properties that matter are not the
ones a risk-budget sizer has:

  ROUND, NOT FLOOR. Flooring makes every stock between Rs5,000 and Rs10,000 a
  half-sized position purely on share price. Across 1,484 published signals,
  flooring to a multiple of 5 put the median position 10.7% off target; rounding to
  the nearest single share puts it 1.7% off.

  NEVER ZERO, NO PRICE CEILING. A share costing more than the whole target is bought
  anyway -- the instrument must not be blind to MRF, MARUTI or ABBOTINDIA because of
  its own test size -- and flagged, because the record has to know.

  NO RISK BUDGET. Risk is an OUTPUT. Reporting Rs3,000 while sizing from Rs10,000 of
  notional would be false and atlas_trades.risk_inr reads risk_actual straight
  through.
"""

import sys
from math import floor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.config import (SIZING_MODE, FIXED_NOTIONAL_PER_TRADE,
                          QUANTITY_MULTIPLE, MAX_RISK_PER_TRADE)
from atlas.risk.position_sizing import (size_for_trade, size_fixed_notional,
                                        size_by_risk, SIZING_BASIS,
                                        _round_to_multiple)
from atlas.risk import costs as C

FAILS = []
T = FIXED_NOTIONAL_PER_TRADE


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def size(px, stop_pct=0.02, direction="LONG", funds=1e7):
    stop = px * (1 - stop_pct) if direction == "LONG" else px * (1 + stop_pct)
    return size_for_trade(px, stop, direction, available_funds=funds)


print("\n── the mode is in force ──")
check("SIZING_MODE is fixed_notional", SIZING_MODE == "fixed_notional", SIZING_MODE)
check("target is Rs10,000", T == 10000.0, str(T))
check("QUANTITY_MULTIPLE is 1", QUANTITY_MULTIPLE == 1, str(QUANTITY_MULTIPLE))
check("the basis names the rule", SIZING_BASIS == "fixed-notional-10000-mult1-v1",
      SIZING_BASIS)

print("\n── round to nearest, not floor ──")
# Rs6,000: floor gives 1 share at Rs6,000 (60% of target); round gives 2 at Rs12,000.
r = size(6000.0)
check("a Rs6,000 stock gets 2 shares, not 1", r["qty"] == 2, f"{r['qty']}")
check("  so the position is Rs12,000, not Rs6,000", r["notional"] == 12000.0)
check("half rounds UP, not to even", _round_to_multiple(1.5, 1) == 2,
      str(_round_to_multiple(1.5, 1)))
check("  and 2.5 also rounds up", _round_to_multiple(2.5, 1) == 3)
# the boundary the bands were drawn around
check("Rs6,666 is the 1.5-share boundary: just above takes 1",
      size(6700.0)["qty"] == 1)
check("  just below takes 2", size(6600.0)["qty"] == 2)

print("\n── never zero, no price ceiling ──")
for px, label in ((11549.0, "ULTRACEMCO"), (43610.0, "PAGEIND"), (129290.0, "MRF")):
    r = size(px)
    check(f"{label} at Rs{px:,.0f} takes one share", r["qty"] == 1, str(r["qty"]))
    check(f"  and is flagged oversized", r["oversized"] is True)
    check(f"  with its realised size recorded ({r['notional_pct']:.0f}% of target)",
          r["notional_pct"] > 100)
check("a normal position is NOT flagged oversized", size(1400.0)["oversized"] is False)
check("_round_to_multiple never returns zero", _round_to_multiple(0.001, 1) == 1)

print("\n── risk is an output, never a budget ──")
r = size(1000.0)
check("no risk_budget key at all", "risk_budget" not in r)
check("risk_actual is what the stop implies",
      abs(r["risk_actual"] - r["qty"] * 20.0) < 0.01,
      f"{r['risk_actual']} on {r['qty']} shares at a Rs20 stop")
check("  which is far below the old Rs3,000 budget",
      r["risk_actual"] < 0.1 * MAX_RISK_PER_TRADE,
      f"Rs{r['risk_actual']:,.0f} vs Rs{MAX_RISK_PER_TRADE:,.0f}")
check("MAX_RISK_PER_TRADE is not consulted",
      "MAX_RISK_PER_TRADE" not in __import__("inspect").getsource(size_fixed_notional))

print("\n── the stop band still gates, and funds still cap ──")
wide = size_for_trade(1000.0, 880.0, "LONG", available_funds=1e7)   # 12% stop
check("a 12% stop is refused", wide["qty"] == 0, str(wide.get("error", ""))[:50])
tight = size_for_trade(1000.0, 999.0, "LONG", available_funds=1e7)  # 0.1% stop
check("a 0.1% stop is refused", tight["qty"] == 0)
poor = size_for_trade(1000.0, 980.0, "LONG", available_funds=600.0)
check("a balance below one share refuses", poor["qty"] == 0)
check("  and says insufficient funds", "insufficient funds" in str(poor.get("error", "")))
cap = size_for_trade(1000.0, 980.0, "LONG", available_funds=4000.0)
check("a Rs4,000 balance takes 4 shares, not 10", cap["qty"] == 4, str(cap["qty"]))
check("  and reports funds as the binding cap", cap["binding_cap"] == "funds")

print("\n── a short needs only its margin ──")
sh = size(1000.0, direction="SHORT")
check("a short is MIS", sh["product"] == "MIS")
check("  and blocks ~20% of notional",
      abs(sh["capital_required"] - 0.2 * sh["notional"]) < 1.0,
      f"Rs{sh['capital_required']:,.0f} of Rs{sh['notional']:,.0f}")

print("\n── the realised-size distribution, across the real price range ──")
# The two structurally biased bands are arithmetic, not a finding, and the ledger
# records them as a known property. Asserted so a future change to the rounding
# cannot quietly remove or worsen them.
band_666 = [size(p)["notional_pct"] for p in (6700.0, 7500.0, 8500.0, 9900.0)]
check("Rs6,666-10,000 can never reach target", max(band_666) < 100,
      f"max {max(band_666):.0f}%")
band_over = [size(p)["notional_pct"] for p in (10100.0, 20000.0, 129290.0)]
check("above Rs10,000 can never be under target", min(band_over) >= 100,
      f"min {min(band_over):.0f}%")
cheap = [size(p)["notional_pct"] for p in (82.0, 316.0, 880.0, 1400.0)]
check("under Rs2,000 lands within 9% of target",
      all(abs(x - 100) <= 9 for x in cheap), str([round(x) for x in cheap]))

print("\n── size_by_risk is untouched, so the backtest basis cannot move ──")
rb = size_by_risk(1000.0, 980.0, "LONG")
check("it still reports a risk_budget", rb.get("risk_budget") == MAX_RISK_PER_TRADE)
# At a 2% stop the NOTIONAL cap binds, not the risk leg: 3000/20 = 150 shares by
# risk against 100000/1000 = 100 by notional. That crossover is at a 3.00% stop
# (MAX_RISK_PER_TRADE / MAX_NOTIONAL_PER_TRADE) and config.py documents it -- the
# first version of this expected 150 and was simply wrong about which cap applies.
check("  the notional cap binds at a 2% stop", rb["qty"] == 100, str(rb["qty"]))
check("  and it says so", rb["binding_cap"] == "notional", rb["binding_cap"])
wide_rb = size_by_risk(1000.0, 960.0, "LONG")       # 4% stop, past the crossover
check("  the risk leg binds past a 3% stop", wide_rb["binding_cap"] == "risk",
      f"{wide_rb['binding_cap']} at {wide_rb['qty']} shares")

print("\n── costs: gross and net are separate, and the rates are unverified ──")
check("rates are marked unverified", C.RATES_VERIFIED_ON is None)
long_c = C.round_trip(1000.0, 1000.0, 10, "CNC")
short_c = C.round_trip(1000.0, 1000.0, 10, "MIS")
check("a delivery long costs more than an intraday short",
      long_c["total"] > short_c["total"],
      f"CNC Rs{long_c['total']} vs MIS Rs{short_c['total']}")
check("  because delivery STT is both legs",
      long_c["stt"] > short_c["stt"] * 5)
check("  and delivery brokerage is zero", long_c["brokerage"] == 0.0)
check("the percentage cap binds at Rs10,000, not the flat Rs20",
      short_c["brokerage"] < 10.0, f"Rs{short_c['brokerage']}")
net, tot = C.net_pnl(500.0, 1000.0, 1050.0, 10, "CNC")
check("net = gross less costs", abs(net - (500.0 - tot)) < 0.01)
check("  and gross is passed in, never recomputed",
      "gross" in __import__("inspect").signature(C.net_pnl).parameters)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All sizing checks passed.")
