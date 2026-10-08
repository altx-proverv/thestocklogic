#!/usr/bin/env python3
"""
THE EXIT PATH, AFTER 2026-10-07
===============================
On 2026-10-07 a BHARTIARTL MIS short went unprotected for 68 minutes. The SL-M
stop was rejected -- "Market orders without market protection are not allowed
via API" -- and the emergency market exit was rejected with the SAME error for
the SAME reason, because the fallback was the same order family as the thing
that had just failed. Two layers of protection were one layer twice.

WHAT THIS FILE GUARDS, AND WHY EACH ONE IS HERE RATHER THAN DESCRIBED IN A
COMMENT:

  1. THE PAIR MUST FAIL FOR INDEPENDENT REASONS. Not "should" -- the module
     refuses to import otherwise. A future edit that makes the fallback a
     market-type order again, or gives it a trigger, breaks the build.
  2. THE STOP IS AN SL WITH A TRIGGER AND A LIMIT PAST IT. The limit's SIDE is
     asserted per direction: a LONG stop sells BELOW the trigger, a SHORT stop
     buys ABOVE it. Get that backwards and the order rests un-fillable, which
     looks like protection and is not.
  3. THE ENGINE'S SQUARE-OFF IS BEFORE THE BROKER'S CUTOFF, and the systemd
     timer matches the config. 15:15 was three minutes PAST the 15:12 that
     applies to F&O-segment stocks, so that square-off had never been able to
     work on most of the universe.
  4. A HALTED ATLAS OBSERVES AND PLACES NOTHING, enforced twice: once in the
     loop and once inside the order-placement function, so a caller that
     forgets cannot reach the broker.
  5. THE OPERATOR-FACING NUMBERS COME FROM CONFIG. Two strings were wrong for
     days -- a risk budget that had been removed, and "ATLAS places no
     stop-losses" while it was trying to place one.

THE FAKE HERE REFUSES WHAT ZERODHA REFUSES. The incident's root cause was that
this path "was only ever tested against an injected fake that accepted
everything", so a permissive fake is itself the defect being guarded against.
tools/verify_exit_path.py carries the full adversarial stub; this file asserts
the invariants that must never regress.

NONE OF THIS IS EVIDENCE ABOUT THE LIVE API. See docs/LIVE_EXIT_TEST.md.

    python3 tests/test_exit_path_rebuild.py
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

from atlas.execution import exits as X                          # noqa: E402
from atlas import config as C                                   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<60}{str(got):<10}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


class StrictKite:
    """Refuses what Zerodha refuses. reject names order types to fail."""

    def __init__(self, ltp=1833.0, reject=()):
        self.reject = {str(r).upper() for r in reject}
        self.orders = []
        self.last_price = ltp
        self.n = 0

    def place_order(self, **kw):
        self.orders.append(kw)
        ot = str(kw.get("order_type", "")).upper()
        if ot in ("MARKET", "SL-M") and kw.get("market_protection") is None:
            raise RuntimeError("Market orders without market protection are not "
                               "allowed via API. Please set market protection "
                               "or use a Limit order.")
        if ot in ("SL", "SL-M") and not kw.get("trigger_price"):
            raise RuntimeError(f"trigger_price is required for {ot}")
        if ot in ("SL", "LIMIT") and not kw.get("price"):
            raise RuntimeError(f"price is required for {ot}")
        if ot in self.reject:
            raise RuntimeError(f"broker refusing {ot}")
        self.n += 1
        return str(9000 + self.n)

    def ltp(self, instruments):
        if isinstance(instruments, str):
            instruments = [instruments]
        return {i: {"last_price": self.last_price} for i in instruments}

    quote = ltp

    def cancel_order(self, **kw):
        self.orders.append(kw)


def types_sent(k):
    return [str(o.get("order_type", "")).upper() for o in k.orders
            if o.get("order_type")]


def main() -> int:
    print("=" * 78)
    print("1 — THE INDEPENDENCE CONTRACT")
    print("-" * 78)
    check("the shipping pair passes assert_independent",
          X.assert_independent() is None, True,
          f"{X.PRIMARY_STOP} + {X.FALLBACK_EXIT}")
    # THE 7 OCT PAIRS MUST NOT BE CONSTRUCTIBLE. Each of these was a live
    # configuration or one edit away from being one.
    for a, b, why in (("SLM", "MARKET", "the actual 2026-10-07 pair"),
                      ("SLM", "SLM", "same order type"),
                      ("MARKET", "MARKET", "same order type"),
                      ("SL", "SL", "same order type"),
                      ("SL", "SLM", "both need a trigger"),
                      ("LIMIT_THROUGH", "SL", "the fallback would not be immediate")):
        refused = False
        try:
            X.assert_independent(a, b)
        except AssertionError:
            refused = True
        check(f"{a} + {b} is refused", refused, True, why)

    print()
    print("=" * 78)
    print("2 — THE STOP IS AN SL, AND ITS LIMIT IS ON THE FILLABLE SIDE")
    print("-" * 78)
    k = StrictKite()
    r = X.protect(symbol="BHARTIARTL", direction="SHORT", qty=5, product="MIS",
                  fill_price=1833.0, stop_price=1860.0, kite=k)
    check("a SHORT MIS stop is placed", bool(r.get("ok")), True, r.get("reason", ""))
    check("the order type sent is SL", types_sent(k), ["SL"])
    o = k.orders[0]
    check("it carries a trigger_price", bool(o.get("trigger_price")), True,
          str(o.get("trigger_price")))
    check("a SHORT stop's limit is ABOVE its trigger",
          float(o["price"]) > float(o["trigger_price"]), True,
          f"{o['price']} > {o['trigger_price']}")
    check("no market_protection on an SL (no effect per the docs)",
          "market_protection" in o, False)

    k2 = StrictKite()
    X._protect_mis_sl("TATASTEEL", "LONG", 10, 100.0, 99.0, None, k2)
    o2 = k2.orders[0]
    check("a LONG stop's limit is BELOW its trigger",
          float(o2["price"]) < float(o2["trigger_price"]), True,
          f"{o2['price']} < {o2['trigger_price']}")
    # THE SIDE IS THE WHOLE POINT. A long's stop sells; a sell limit above the
    # trigger never fills.
    check("a LONG stop sells", str(o2.get("transaction_type")), "SELL")
    check("a SHORT stop buys", str(o.get("transaction_type")), "BUY")

    print()
    print("=" * 78)
    print("3 — THE FALLBACK SURVIVES A FAILURE OF THE PRIMARY")
    print("-" * 78)
    for reject, label in ((("SL",), "the broker refuses SL"),
                          (("SL", "SL-M"), "the broker refuses the whole stop family")):
        k3 = StrictKite(reject=reject)
        out = X.protect_or_exit(symbol="BHARTIARTL", direction="SHORT", qty=5,
                                product="MIS", fill_price=1833.0,
                                stop_price=1860.0, kite=k3)
        check(f"[{label}] the primary failed", out["protected"], False)
        check(f"[{label}] the FALLBACK completed", out["exited"], True,
              str((out["exit"] or {}).get("reason", ""))[:40])
        check(f"[{label}] it completed with a LIMIT, not another SL",
              types_sent(k3), ["SL", "LIMIT"])

    # AND THE CONVERSE: if the fallback's own family is refused, the result is
    # an honest "unprotected", not a silent success.
    k4 = StrictKite(reject=("SL", "LIMIT"))
    out = X.protect_or_exit(symbol="BHARTIARTL", direction="SHORT", qty=5,
                            product="MIS", fill_price=1833.0, stop_price=1860.0,
                            kite=k4)
    check("both refused -> unprotected is reported, not swallowed",
          out["unprotected"], True, out["reason"][:50])

    # NO LTP MEANS NO FALLBACK ORDER, NOT A MARKET ORDER. Falling back to MARKET
    # here would reinstate the shared failure mode.
    class NoPrice(StrictKite):
        def ltp(self, instruments):
            raise RuntimeError("quote endpoint down")
        quote = ltp

    k5 = NoPrice()
    e = X.emergency_exit("BHARTIARTL", "SHORT", 5, "MIS", why="t", kite=k5)
    check("an unpriceable fallback refuses rather than sending MARKET",
          bool(e.get("ok")), False, str(e.get("reason"))[:48])
    check("and it sent no order at all", types_sent(k5), [])

    print()
    print("=" * 78)
    print("4 — SQUARE-OFF TIMING")
    print("-" * 78)
    cut = tuple(int(x) for x in C.MIS_BROKER_CUTOFF.split(":"))
    check("the engine's exit is before the broker's binding cutoff",
          (C.MIS_EXIT_HOUR, C.MIS_EXIT_MIN) < cut, True,
          f"{C.MIS_EXIT_TIME} < {C.MIS_BROKER_CUTOFF}")
    check("the binding cutoff is the earliest published one",
          C.MIS_BROKER_CUTOFF, min(C.MIS_BROKER_CUTOFFS.values()))
    check("the margin is at least 10 minutes",
          C.MIS_EXIT_MARGIN_MIN >= 10, True, f"{C.MIS_EXIT_MARGIN_MIN} min")
    # THE TIMER AND THE CONFIG MUST AGREE. They are in different files and
    # nothing at runtime compares them, so this is the only thing that does.
    timer = (ROOT / "deploy" / "atlas-mis-squareoff.timer").read_text()
    oncal = [l for l in timer.splitlines() if l.startswith("OnCalendar=")]
    check("the systemd timer fires at MIS_EXIT_TIME",
          bool(oncal) and f" {C.MIS_EXIT_TIME} " in oncal[0], True,
          oncal[0] if oncal else "no OnCalendar line")
    check("exits.py reads the time from config rather than defining it",
          "MIS_EXIT_HOUR, MIS_EXIT_MIN = 15" in
          (ROOT / "atlas" / "execution" / "exits.py").read_text(), False)

    def at(t):
        h, m = map(int, t.split(":"))
        return X.squareoff_mis([], now_ist=datetime(2026, 10, 9, h, m, tzinfo=IST))

    check("not due a minute before the exit time",
          at("14:59")["due"], False)
    check("due at the exit time", at(C.MIS_EXIT_TIME)["due"], True)
    check("not flagged late at the exit time", at(C.MIS_EXIT_TIME)["late"], False)
    check("flagged LATE at the cutoff", at(C.MIS_BROKER_CUTOFF)["late"], True)
    check("and it alerts when late",
          bool(at(C.MIS_BROKER_CUTOFF)["alerts"]), True)

    # THE HOLE THIS MOVE COULD HAVE OPENED. Moving the square-off earlier would
    # strand any MIS position opened after it. Longs are CNC and shorts -- the
    # only MIS product -- are morning-session only, so there is no gap. If that
    # ever changes, this fails rather than a position being stranded.
    from atlas.risk import position_sizing as PS
    src = (ROOT / "atlas" / "risk" / "position_sizing.py").read_text()
    check("MIS is used for shorts only",
          'product     = "CNC" if d == "LONG" else "MIS"' in src, True)
    sel = (ROOT / "atlas" / "execution" / "session_selector.py").read_text()
    check("shorts are allowed in the morning session only",
          sel.count('"SHORT"'), 1,
          "if a later session gains SHORT, the square-off time must move too")

    print()
    print("=" * 78)
    print("5 — A HALTED ATLAS OBSERVES, AND CANNOT PLACE")
    print("-" * 78)
    import atlas.signal.market_open as M
    import atlas.execution.broker as B
    mo = (ROOT / "atlas" / "signal" / "market_open.py").read_text()
    br = (ROOT / "atlas" / "execution" / "broker.py").read_text()
    ae = (ROOT / "atlas" / "execution" / "atlas_entry.py").read_text()

    # GUARD 1 -- the loop no longer returns on the pause check.
    i_pause = mo.index("state.observe_only, watch_why = paused()")
    i_zones = mo.index("if not load_zone_map(state):")
    check("the pause check runs BEFORE the zone map loads", i_pause < i_zones, True)
    check("and it does not return early",
          "return out" in mo[i_pause:i_zones], False,
          "a return here is the 360-cycle blindness of 2026-10-08")
    check("Session carries observe_only", hasattr(M.Session(), "observe_only"), True)
    check("it defaults to False, i.e. the guarded live path",
          M.Session().observe_only, False)

    # GUARD 2 -- the lowest level. The default intent is ENTRY so an unlabelled
    # order fails safe.
    check("place_order takes an intent defaulting to ENTRY",
          'intent: str = "ENTRY"' in br, True)
    body = br[br.index("def place_order("):]
    body = body[:body.index("\ndef ")]
    check("the halt is checked inside place_order",
          "is_halted" in body, True)
    check("and before the kite client is acquired",
          body.index("is_halted") < body.index("get_kite"), True)

    # GUARD 3 -- enter_trade's own dry_run, which must default to the live path.
    import inspect
    from atlas.execution.atlas_entry import enter_trade
    sig = inspect.signature(enter_trade)
    check("enter_trade has a dry_run parameter",
          "dry_run" in sig.parameters, True)
    check("dry_run defaults to False (a forgetful caller gets the guards)",
          sig.parameters["dry_run"].default, False)
    check("dry_run returns before the ledger reservation",
          ae.index("if dry_run:") < ae.index("_reserve_intent(intent)"), True)
    check("the watch-only path calls enter_trade with dry_run=True",
          "enter_trade(signal, dry_run=True)" in mo, True)
    # ONE SEAM. evaluate_only must resolve the SAME callable the live path does,
    # or a patch to one leaves the other pointing at the real enter_trade --
    # which on the watch-only path means an order.
    check("and it does not re-import enter_trade behind the module name",
          "from atlas.execution.atlas_entry import enter_trade as _et" in mo,
          False)
    check("and its statuses are distinct from a live entry's",
          '"WATCH_ONLY"' in mo, True)

    print()
    print("=" * 78)
    print("6 — THE OPERATOR-FACING NUMBERS COME FROM CONFIG")
    print("-" * 78)
    zm = (ROOT / "atlas" / "execution" / "zerodha_morning.py").read_text()
    # THE 08:30 MESSAGE. It said "Risk/trade Rs3,000 - Max notional Rs1,00,000"
    # while the sizer used Rs10,000 notional and no risk budget at all.
    check("the 08:30 message renders the sizing rule from the shared renderer",
          "_rules_line()" in zm, True)
    check("and no longer hardcodes the risk/notional pair",
          'f"Risk/trade ₹{MAX_RISK_PER_TRADE:,.0f} · "' in zm, False)
    from atlas.reporting.directives import _rules_line
    line = _rules_line()
    if C.SIZING_MODE == "fixed_notional":
        check("it names the notional actually in force",
              f"{C.FIXED_NOTIONAL_PER_TRADE:,.0f}" in line, True, line[:52])
        check("and does not quote a risk budget that does not exist",
              "Risk/trade" in line, False)

    # THE REPORT FOOTER. It said "ATLAS places no stop-losses. Set them
    # manually." every day since 2026-09-30, including the day it tried.
    dr = (ROOT / "atlas" / "reporting" / "daily_report.py").read_text()
    from atlas.reporting.daily_report import _footer
    check("the footer is a function, evaluated at send time",
          "def _footer()" in dr, True)
    check("it is not a module-level constant",
          'FOOTER = ("REMINDER: ATLAS places no stop-losses' in dr, False)
    f = _footer()
    if C.ENABLE_EXIT_MANAGEMENT:
        check("with exit management ON it does not claim no stops are placed",
              "places no stop-losses" in f, False, f.split(".")[0][:48])
        check("and it names the square-off time from config",
              C.MIS_EXIT_TIME in f, True)
    else:
        check("with exit management OFF it says so plainly",
              "places no stop-losses" in f, True)

    print()
    print("=" * 78)
    print("PASS" if OK else "FAIL")
    print("=" * 78)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
