#!/usr/bin/env python3
"""
STOPS AND TARGETS: A FILLED POSITION IS NEVER LEFT UNPROTECTED
=============================================================
Before this module ENABLE_EXIT_MANAGEMENT was False and place_zone_gtt had been
deleted, so ATLAS opened positions and left them to a human. These are the three
decisions that shape it, and each is asserted rather than described:

  1. FILL DETECTION IS BLOCKING. Detecting on the next 60s cycle would leave a
     minute of unprotected exposure on every entry in a system nobody watches.
  2. A STOP THAT CANNOT BE PLACED MEANS EXIT AT MARKET, IMMEDIATELY. Not retry.
     An unprotected position is an unbounded loss; a bad exit costs a known
     amount once.
  3. SHORTS GET AN SL-M, NOT A GTT. GTT is CNC-only and switching a short to CNC
     to obtain one would add overnight gap risk to a trade designed to be flat
     by 15:20.

Offline: kiteconnect is not installed in development, so every broker call goes
to an injected fake. That is also why exits.py uses the documented REST strings
rather than SDK constants.

    python3 tests/test_exits.py
"""

import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

from atlas.execution import exits as X      # noqa: E402


class FakeKite:
    """Records calls; fails whichever ones the test asks it to."""

    def __init__(self, fail=(), gtt_two_leg=True):
        self.fail = set(fail)
        self.gtt_two_leg = gtt_two_leg
        self.calls = []
        self.n = 0

    def _id(self):
        self.n += 1
        return 1000 + self.n

    def place_gtt(self, **kw):
        self.calls.append(("place_gtt", kw))
        if kw.get("trigger_type") == "two-leg" and not self.gtt_two_leg:
            raise RuntimeError("two-leg GTT not enabled for this account")
        if "gtt" in self.fail:
            raise RuntimeError("gtt refused")
        if "gtt_target" in self.fail and len(kw.get("trigger_values") or []) == 1 \
                and kw["orders"][0]["price"] > kw["last_price"]:
            raise RuntimeError("target gtt refused")
        return {"trigger_id": self._id()}

    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        t = kw.get("order_type")
        if t == "SL-M" and "slm" in self.fail:
            raise RuntimeError("SL-M refused")
        if t == "LIMIT" and "limit" in self.fail:
            raise RuntimeError("LIMIT refused")
        if t == "MARKET" and "market" in self.fail:
            raise RuntimeError("MARKET refused")
        return str(self._id())

    def cancel_order(self, **kw):
        self.calls.append(("cancel_order", kw))
        if "cancel" in self.fail:
            raise RuntimeError("cancel refused")

    def delete_gtt(self, **kw):
        self.calls.append(("delete_gtt", kw))
        if "cancel" in self.fail:
            raise RuntimeError("delete refused")


def main() -> int:
    ok = True

    def check(label, got, want, extra=""):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {label:<54}{str(got):<12}"
              f"{'ok' if good else f'** want {want} **'}  {extra}")

    print("=" * 78)
    print("PRICES THE EXCHANGE WILL ACCEPT")
    print("-" * 78)
    # An off-tick price is rejected outright, and a rejected stop is an
    # unprotected position -- which becomes a market exit. Rounding is not cosmetic.
    check("1234.567 rounds to the 0.05 tick", X.round_tick(1234.567), 1234.55)
    check("  and 99.999", X.round_tick(99.999), 100.0)
    # 2R is measured from the FILL, not from entry_ref: a MARKET order slips, and
    # risk actually taken is |fill - stop|.
    check("LONG 2R above a 100/95 fill/stop", X.target_price(100, 95, "LONG"), 110.0)
    check("SHORT 2R below a 100/105 fill/stop",
          X.target_price(100, 105, "SHORT"), 90.0)
    check("a slipped long fill moves the target, not the stop",
          X.target_price(101, 95, "LONG"), 113.0, "fill 101 -> 6R risk -> 113")
    check("LONG exit side is SELL", X.exit_side("LONG"), "SELL")
    check("SHORT exit side is BUY", X.exit_side("SHORT"), "BUY")
    try:
        X.target_price(100, 100, "LONG")
        ok = False
        print("  zero-risk fill must raise                            ** DID NOT **")
    except ValueError:
        print("  zero-risk fill raises rather than dividing by zero   ok")

    print()
    print("BLOCKING FILL DETECTION")
    print("-" * 78)
    seq = [{"status": "OPEN"}, {"status": "OPEN"},
           {"status": "COMPLETE", "filled_quantity": 45, "average_price": 101.2}]
    it = iter(seq)
    r = X.await_fill("o1", get_status=lambda _: next(it, seq[-1]),
                     sleep=lambda _: None)
    check("polls until COMPLETE", r["outcome"], "FILLED",
          f"qty {r.get('filled_qty')} @ {r.get('avg_price')}")
    r = X.await_fill("o2", get_status=lambda _: {"status": "REJECTED",
                                                "status_message": "margin"},
                     sleep=lambda _: None)
    check("REJECTED is terminal and distinct", r["outcome"], "REJECTED")
    # NOT the same as rejected: the order may still fill, so nothing may be assumed
    r = X.await_fill("o3", timeout=0, get_status=lambda _: {"status": "OPEN"},
                     sleep=lambda _: None)
    check("a timeout is INDETERMINATE, never 'no position'",
          r["outcome"], "INDETERMINATE")
    r = X.await_fill("o4", timeout=0, get_status=lambda _: {},
                     sleep=lambda _: None)
    check("an order absent from the book is INDETERMINATE",
          r["outcome"], "INDETERMINATE")
    # COMPLETE with nothing filled cannot be sized against
    r = X.await_fill("o5", get_status=lambda _: {"status": "COMPLETE",
                                                "filled_quantity": 0,
                                                "average_price": 0},
                     sleep=lambda _: None)
    check("COMPLETE with zero qty is not a usable fill",
          r["outcome"], "INDETERMINATE")
    # a raising status call must not crash the entry path
    r = X.await_fill("o6", timeout=0,
                     get_status=lambda _: (_ for _ in ()).throw(RuntimeError("net")),
                     sleep=lambda _: None)
    check("a raising status call degrades to INDETERMINATE",
          r["outcome"], "INDETERMINATE")

    print()
    print("LONGS ARE PROTECTED BY GTT, WHICH SURVIVES THE CLOSE")
    print("-" * 78)
    k = FakeKite()
    r = X.protect("RELIANCE", "LONG", 45, "CNC", 100.0, 95.0, kite=k,
                  place_target=True)
    check("two-leg OCO is preferred", r["mechanism"], "GTT_OCO")
    check("  and reports ok", r["ok"], True)
    check("  target is 2R", r["target"], 110.0)
    g = [c for c in k.calls if c[0] == "place_gtt"][0][1]
    check("  legs are SELL for a long",
          all(o["transaction_type"] == "SELL" for o in g["orders"]), True)
    check("  product is CNC so it survives the session",
          all(o["product"] == "CNC" for o in g["orders"]), True)
    # If the account has no two-leg GTT the fallback must still protect
    k = FakeKite(gtt_two_leg=False)
    r = X.protect("RELIANCE", "LONG", 45, "CNC", 100.0, 95.0, kite=k,
                  place_target=True)
    check("no two-leg on the account -> two single GTTs",
          r["mechanism"], "GTT_SINGLE")
    check("  still protected", r["ok"], True)
    check("  both legs recorded for reconciliation", len(r["legs"]), 2)

    print()
    print("SHORTS ARE PROTECTED BY SL-M, AND STAY MIS")
    print("-" * 78)
    k = FakeKite()
    r = X.protect("TATASTEEL", "SHORT", 90, "MIS", 100.0, 105.0, kite=k,
                  place_target=True)
    check("mechanism is SL-M, not GTT", r["mechanism"], "SLM")
    check("  ok", r["ok"], True)
    check("  target is 2R below the fill", r["target"], 90.0)
    orders = [c[1] for c in k.calls if c[0] == "place_order"]
    check("  the stop is SL-M, not SL",
          orders[0]["order_type"], "SL-M",
          "an SL is a limit on trigger and can go unfilled in the move it escapes")
    check("  product stays MIS (no overnight gap risk added)",
          all(o["product"] == "MIS" for o in orders), True)
    check("  legs are BUY to close a short",
          all(o["transaction_type"] == "BUY" for o in orders), True)
    check("  no GTT was placed for a short",
          any(c[0] == "place_gtt" for c in k.calls), False)

    print()
    print("A STOP THAT WILL NOT PLACE MEANS THE POSITION LEAVES")
    print("-" * 78)
    k = FakeKite(fail=("slm",))
    r = X.protect("TATASTEEL", "SHORT", 90, "MIS", 100.0, 105.0, kite=k,
                  place_target=True)
    check("SL-M refused -> protect() reports NOT ok", r["ok"], False)
    k = FakeKite(fail=("gtt",))
    r = X.protect("RELIANCE", "LONG", 45, "CNC", 100.0, 95.0, kite=k,
                  place_target=True)
    check("every GTT refused -> NOT ok", r["ok"], False)
    # A TARGET failure is not an escalation: the position is protected.
    k = FakeKite(fail=("limit",))
    r = X.protect("TATASTEEL", "SHORT", 90, "MIS", 100.0, 105.0, kite=k,
                  place_target=True)
    check("stop placed but target refused -> still ok", r["ok"], True,
          "downside is covered; the upside leg is a missed convenience")
    check("  and it says so", "target" in r["reason"].lower(), True)

    k = FakeKite()
    r = X.emergency_exit("RELIANCE", "LONG", 45, "CNC", "stop failed", kite=k)
    check("emergency exit is a MARKET order", r["ok"], True)
    o = [c[1] for c in k.calls if c[0] == "place_order"][0]
    check("  MARKET, SELL, right product",
          (o["order_type"], o["transaction_type"], o["product"]),
          ("MARKET", "SELL", "CNC"))
    k = FakeKite(fail=("market",))
    r = X.emergency_exit("RELIANCE", "LONG", 45, "CNC", "stop failed", kite=k)
    check("exit itself fails -> ok False, and it does NOT loop", r["ok"], False)
    check("  and the reason names the state plainly",
          "UNPROTECTED" in r["reason"], True)

    print()
    print("SIBLING CANCELLATION FOLLOWS BROKER QUANTITY, NOT OUR RECORD")
    print("-" * 78)
    trade = {"symbol": "RELIANCE", "direction": "LONG", "qty": 45,
             "product": "CNC",
             "exit_legs": {"stop_order_id": "S1", "target_order_id": "T1"}}
    # target filled, position flat -> the stop is an orphan and must be cancelled
    k = FakeKite()
    out = X.reconcile_exits([trade], kite=k,
                            positions=[{"tradingsymbol": "RELIANCE", "quantity": 0}],
                            orders=[{"order_id": "T1", "status": "COMPLETE"},
                                    {"order_id": "S1", "status": "TRIGGER PENDING"}])
    check("flat with the target filled -> stop cancelled", out["cancelled"], 1)
    check("  counted as closed", out["closed"], 1)
    # flat with NOTHING filled: closed externally (manual, or MIS square-off).
    # Our record would say no leg fired and leave both resting.
    k = FakeKite()
    out = X.reconcile_exits([trade], kite=k,
                            positions=[{"tradingsymbol": "RELIANCE", "quantity": 0}],
                            orders=[{"order_id": "T1", "status": "OPEN"},
                                    {"order_id": "S1", "status": "TRIGGER PENDING"}])
    check("flat with no leg filled -> both cancelled", out["cancelled"], 2)
    check("  flagged as an external close", out["external"], 1)
    # still open with one leg filled -> a PARTIAL. Cancelling would strip the
    # protection from the quantity that remains.
    out = X.reconcile_exits([trade], kite=FakeKite(),
                            positions=[{"tradingsymbol": "RELIANCE", "quantity": 20}],
                            orders=[{"order_id": "T1", "status": "COMPLETE"},
                                    {"order_id": "S1", "status": "TRIGGER PENDING"}])
    check("partial fill -> nothing cancelled", out["cancelled"], 0)
    check("  and it is flagged for resizing", out["partial"], 1)
    # an orphan that will not cancel can RE-ENTER a closed position
    out = X.reconcile_exits([trade], kite=FakeKite(fail=("cancel",)),
                            positions=[{"tradingsymbol": "RELIANCE", "quantity": 0}],
                            orders=[{"order_id": "T1", "status": "COMPLETE"}])
    good = any("RE-ENTER" in a for a in out["alerts"])
    ok &= good
    print(f"  {'an uncancellable orphan is escalated':<54}"
          f"{'ok' if good else '** SILENT **'}")
    # an unreadable position book must not be read as flat
    out = X.reconcile_exits([trade], kite=FakeKite(), positions=None, orders=None)
    print(f"  {'unreadable books -> reported, nothing cancelled':<54}"
          f"{'ok' if out['cancelled'] == 0 else '** CANCELLED BLIND **'}")
    ok &= out["cancelled"] == 0

    print()
    print("MIS POSITIONS LEAVE BEFORE THE BROKER TAKES THEM")
    print("-" * 78)
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    short = {"symbol": "TATASTEEL", "direction": "SHORT", "qty": 90,
             "product": "MIS"}
    long_ = {"symbol": "RELIANCE", "direction": "LONG", "qty": 45,
             "product": "CNC"}
    out = X.squareoff_mis([short, long_], now_ist=datetime(2026, 9, 30, 14, 0, tzinfo=IST),
                          kite=FakeKite())
    check("not due before 15:15", out["due"], False)
    check("  nothing exited", out["exited"], 0)
    out = X.squareoff_mis([short, long_], now_ist=datetime(2026, 9, 30, 15, 15, tzinfo=IST),
                          kite=FakeKite())
    check("due at 15:15", out["due"], True)
    check("  the MIS short is exited", out["exited"], 1)
    check("  the CNC long is left alone", out["failed"], 0,
          "CNC has no square-off; its GTT persists")
    out = X.squareoff_mis([short], now_ist=datetime(2026, 9, 30, 15, 16, tzinfo=IST),
                          kite=FakeKite(fail=("market",)))
    good = out["failed"] == 1 and any("15:20" in a for a in out["alerts"])
    ok &= good
    print(f"  {'a failed time exit names the broker deadline':<54}"
          f"{'ok' if good else '** SILENT **'}")

    print()
    print("THE SWITCHES GATE WHAT THEY SAY THEY GATE")
    print("-" * 78)
    # All three were decorative -- nothing in the tree read them, so the
    # "scalable seam" was a comment. False must be the current behaviour (open a
    # position, leave it to a human) so deploying this does not silently start
    # managing money.
    from atlas.config import (ENABLE_EXIT_MANAGEMENT, ALLOW_AUTOMATED_STOP_LOSS,
                              ALLOW_AUTOMATED_TARGET)
    entry_src = (ROOT / "atlas/execution/atlas_entry.py").read_text(encoding="utf-8")
    check("enter_trade reads ENABLE_EXIT_MANAGEMENT",
          "ENABLE_EXIT_MANAGEMENT" in entry_src, True)
    # The GATE is asserted, not its setting. Pinning the deployed value here would
    # break the suite on every deliberate flip, which trains whoever flips it to
    # edit the test -- and a test that gets edited to pass is not a test. The
    # value is printed so a run says plainly which mode is deployed.
    check("  the early return exists when it is off",
          "if not ENABLE_EXIT_MANAGEMENT:" in entry_src, True)
    print(f"  {'DEPLOYED: exit management is ' + ('ON' if ENABLE_EXIT_MANAGEMENT else 'OFF'):<54}"
          f"{'ATLAS places its own stops' if ENABLE_EXIT_MANAGEMENT else 'exits are manual'}")
    check("a stop is not optional when management is on",
          ALLOW_AUTOMATED_STOP_LOSS, True,
          "False would not disable stops, it would just be a lie")

    # ALLOW_AUTOMATED_TARGET=False is the trail-instead-of-2R option, reachable
    # without touching code. A fixed 2R caps every winner at 2R, which is the
    # opposite of what zone_entry describes.
    k = FakeKite()
    r = X.protect("RELIANCE", "LONG", 45, "CNC", 100.0, 95.0, kite=k,
                  place_target=False)
    check("target off -> a single stop GTT", r["mechanism"], "GTT_STOP_ONLY")
    check("  no target price", r["target"], None)
    check("  one leg, so nothing to orphan", len(r["legs"]), 1)
    check("  still protected", r["ok"], True)
    k = FakeKite()
    r = X.protect("TATASTEEL", "SHORT", 90, "MIS", 100.0, 105.0, kite=k,
                  place_target=False)
    check("short with target off is still SL-M protected", r["ok"], True)
    check("  and only the stop was sent",
          [c[1]["order_type"] for c in k.calls if c[0] == "place_order"], ["SL-M"])
    # and a stop failure in stop-only mode is still an escalation
    r = X.protect("RELIANCE", "LONG", 45, "CNC", 100.0, 95.0,
                  kite=FakeKite(fail=("gtt",)), place_target=False)
    check("stop-only, stop refused -> NOT ok", r["ok"], False)
    # the DEFAULT follows config, which is the deployed decision
    r = X.protect("RELIANCE", "LONG", 45, "CNC", 100.0, 95.0, kite=FakeKite())
    check("default (no arg) follows ALLOW_AUTOMATED_TARGET",
          r["target"] is None, not ALLOW_AUTOMATED_TARGET,
          "config says stop-only, so no target leg")

    print("-" * 78)
    print("EXITS:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
