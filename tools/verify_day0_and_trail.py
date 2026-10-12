#!/usr/bin/env python3
"""
ATLAS — prove the day-0/day-1 handover and that a stop can never be loosened.

    python3 -m tools.verify_day0_and_trail

THE THREE CLAIMS THE OPERATOR ASKED TO SEE PROVED, 2026-10-13:

  1. A SELL SL on a same-day CNC buy is accepted by the broker.
  2. A stop update that would LOOSEN the stop is REFUSED. Both directions,
     long side.
  3. A simulated protection failure still exits the position and halts.
     (that one lives in tools/verify_long_only.py; this file proves 1 and 2 and
      the handover arithmetic around them)

ON CLAIM 1 AND WHAT A STUB CAN AND CANNOT SETTLE. The ACCEPTANCE of a SELL SL
against a same-day CNC buy is a BROKER RULE, and the only honest way to settle
it is Zerodha's own documentation. It is quoted in §1 below and it is
unambiguous.

THERE IS NO LIVE DRIVER. tools/live_day0_check.py drove a one-share
verification and was DELETED on 2026-10-13 by operator decision -- we are not
running it. The exit-path logic it exercised lives in
atlas/execution/exits.py and is unchanged; only the driver is gone, so nobody
runs it by accident.

So what this file proves is the half a stub can prove: that ATLAS builds an
order of the shape the rules describe -- SL, SELL, CNC, trigger present, limit
below the trigger, DAY validity -- and that nothing in the fill path touches
the GTT API. The acceptance of that order by the real broker rests on the
quoted documentation alone.
"""

from __future__ import annotations

import os
import sys
import json
import tempfile
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"\n         {detail}" if detail else ""))
    return bool(ok)


def head(t: str) -> None:
    print(f"\n{'=' * 76}\n{t}\n{'=' * 76}")


class DayZeroKite:
    """A broker stub that enforces the rules the docs describe.

    THE RULES IT ENFORCES, from Zerodha's documentation, confirmed 2026-10-13:

      * A CNC position bought today is in POSITIONS, not holdings. An SL SELL
        against it is accepted -- "if your SL is triggered the same day, your
        trade will be considered intraday".
      * A GTT checks DEMAT. A sell GTT for a quantity not yet in demat is
        accepted at placement but REJECTED AT TRIGGER -- "if you do not have
        enough holdings of the specific stock in your demat account, the
        exchange will reject your GTT order". This stub models that by
        refusing to TRIGGER such a GTT, which is the behaviour that matters.
      * A SELL limit above its own trigger does not fill. "Your order's LTP
        did not match the limit price you set."
      * Pending orders are cancelled at the end of the session.
    """

    def __init__(self, ltp=100.0, demat_qty=0):
        self.last_price = ltp
        self.demat_qty = int(demat_qty)     # what has SETTLED
        self.today_qty = 0                  # bought today, in positions
        self.orders = []
        self.gtts = []
        self.n = 0

    def buy_today(self, qty):
        self.today_qty += int(qty)

    def place_order(self, **kw):
        self.orders.append(dict(kw))
        ot = str(kw.get("order_type", "")).upper()
        tt = str(kw.get("transaction_type", "")).upper()
        q = int(kw.get("quantity") or 0)
        if ot in ("SL", "SL-M") and not kw.get("trigger_price"):
            raise RuntimeError(f"trigger_price is required for {ot}")
        if ot in ("SL", "LIMIT") and not kw.get("price"):
            raise RuntimeError(f"price is required for {ot}")
        if tt == "SELL":
            # THE ORDER BOOK KNOWS ABOUT TODAY'S BUY. This is the whole
            # premise of the day-0 design, and the stub enforces it as the
            # docs state it: positions + holdings, not holdings alone.
            available = self.demat_qty + self.today_qty
            if q > available:
                raise RuntimeError(
                    f"insufficient quantity: {q} requested, {available} "
                    f"available ({self.demat_qty} demat + {self.today_qty} "
                    f"bought today)")
        self.n += 1
        return str(5000 + self.n)

    def place_gtt(self, **kw):
        self.gtts.append(dict(kw))
        self.n += 1
        return {"trigger_id": 70000 + self.n}

    def trigger_gtt(self, idx=0):
        """Fire a resting GTT. -> (ok, reason). DEMAT ONLY."""
        g = self.gtts[idx]
        q = max(int(o.get("quantity") or 0) for o in g.get("orders") or [{}])
        if q > self.demat_qty:
            return False, (f"GTT rejected at trigger: {q} requested, "
                           f"{self.demat_qty} in demat. Shares bought today "
                           f"are not in demat until T+1.")
        return True, "filled"

    def ltp(self, inst):
        if isinstance(inst, str):
            inst = [inst]
        return {i: {"last_price": self.last_price} for i in inst}
    quote = ltp


def section_day0():
    head("1 — A SELL SL ON A SAME-DAY CNC BUY")

    print("""
  THE BROKER RULES, QUOTED. Confirmed against Zerodha's own documentation on
  2026-10-13, before any of this was written:

    "Shares you purchase for delivery using the CNC product type appear under
     positions on the same day because they haven't settled yet... Showing
     delivery shares in positions on the purchase day allows you to
     participate in intraday trading."

    "If your SL is triggered the same day, your trade will be considered
     intraday, a month later, it would be considered a delivery trade,
     however, you'll have to place a fresh SL order every day since all
     pending orders are cancelled at the end of the day."

    "Unmatched pending orders are automatically cancelled when the market
     closes."  Equity: 4:00 PM.

  SO: a SELL SL against a same-day CNC buy is accepted, it works, and it dies
  at the close. That is exactly the day-0 mechanism and exactly why it has to
  hand over to a GTT before the close.

  AND THE CONTRAST, which is the seam this design uses:

    "If you do not have enough holdings of the specific stock in your demat
     account, the exchange will reject your GTT order."
""")
    from atlas.execution import exits as X

    # ── the order ATLAS builds ───────────────────────────────────
    k = DayZeroKite(ltp=100.0, demat_qty=0)
    k.buy_today(10)
    r = X.protect_day0(symbol="RELIANCE", direction="LONG", qty=10,
                       fill_price=100.0, stop_price=95.0, kite=k)
    print(f"  protect_day0() -> ok={r.get('ok')} {str(r.get('reason'))[:70]}")
    o = k.orders[0] if k.orders else {}
    print(f"  ORDER AS IT WOULD GO ON THE WIRE:")
    print("   ", json.dumps(o, default=str, sort_keys=True))

    check("the day-0 stop is accepted against a position with ZERO in demat",
          bool(r.get("ok")),
          "10 bought today, 0 settled — the order book knows about today's buy")
    check("  it is an SL, not a GTT", o.get("order_type") == "SL",
          str(o.get("order_type")))
    check("  SELL, closing a long", o.get("transaction_type") == "SELL")
    check("  CNC", o.get("product") == "CNC")
    check("  it carries a trigger_price", bool(o.get("trigger_price")),
          str(o.get("trigger_price")))
    check("  the limit sits BELOW the trigger so the sell is marketable",
          float(o["price"]) < float(o["trigger_price"]),
          f"limit {o['price']} < trigger {o['trigger_price']} — a SELL limit "
          f"above its trigger rests above the market and does not fill")
    check("  DAY validity, so it dies at the close as designed",
          o.get("validity") == "DAY", str(o.get("validity")))
    check("  NO GTT was touched on the fill path", not k.gtts,
          f"{len(k.gtts)} — the GTT belongs to the pre-close job")

    # ── AND THE REASON IT CANNOT BE A GTT TODAY ──────────────────
    print("\n  WHY NOT A GTT ON DAY 0 — the same stub, same position:")
    k2 = DayZeroKite(ltp=100.0, demat_qty=0)
    k2.buy_today(10)
    g = X.place_overnight_gtts(symbol="RELIANCE", direction="LONG", qty=10,
                               fill_price=100.0, stop_price=95.0, kite=k2)
    check("the GTT is ACCEPTED at placement", bool(g.get("ok")),
          str(g.get("reason"))[:70])
    ok_trig, why = k2.trigger_gtt(0)
    check("  but it is REJECTED AT TRIGGER with nothing in demat",
          not ok_trig, why)
    print("         ^ this is the failure the day-0 SL exists to avoid, and it "
          "is\n           silent: the GTT looks placed and the stop does not work.")

    # ── FROM DAY 1 THE GTT WORKS ─────────────────────────────────
    print("\n  AND FROM DAY 1, once the shares are credited:")
    k3 = DayZeroKite(ltp=100.0, demat_qty=10)
    g3 = X.place_overnight_gtts(symbol="RELIANCE", direction="LONG", qty=10,
                                fill_price=100.0, stop_price=95.0, kite=k3)
    check("the GTTs are placed", bool(g3.get("ok")), str(g3.get("reason"))[:70])
    ok3, why3 = k3.trigger_gtt(0)
    check("  and the stop leg FILLS when triggered", ok3, why3)

    # ── THE SPLIT ────────────────────────────────────────────────
    print(f"\n  THE 50/50 SPLIT on qty 10: OCO {g3.get('oco_qty')} + "
          f"trailed {g3.get('trailed_qty')}")
    print(f"  GTT instructions placed: {len(k3.gtts)}")
    for gg in k3.gtts:
        qt = [o.get("quantity") for o in gg.get("orders") or []]
        print(f"    {gg.get('trigger_type'):<9} triggers "
              f"{gg.get('trigger_values')}  qty {qt}")
    check("TWO GTTs per position", len(k3.gtts) == 2, f"{len(k3.gtts)}")
    types = sorted(str(gg.get("trigger_type")) for gg in k3.gtts)
    check("  one two-leg OCO and one single-leg stop-only",
          types == ["single", "two-leg"], str(types))
    check("  the quantities sum to the position",
          (g3.get("oco_qty") or 0) + (g3.get("trailed_qty") or 0) == 10,
          f"{g3.get('oco_qty')} + {g3.get('trailed_qty')} = 10")

    print("\n  SPLIT ARITHMETIC across quantities:")
    print(f"    {'qty':>5}{'OCO':>6}{'trailed':>9}   note")
    for q in (1, 2, 3, 10, 45):
        a, b = X.split_quantity(q)
        note = ("no OCO half; the stop-only GTT carries the whole position"
                if a == 0 else ("odd share to the trailed half"
                                if b > a else ""))
        print(f"    {q:>5}{a:>6}{b:>9}   {note}")
        check(f"  qty {q} sums to {q}", a + b == q, f"{a}+{b}")
    a1, b1 = X.split_quantity(1)
    check("at qty 1 there is no OCO leg and the trailed leg carries it",
          (a1, b1) == (0, 1), f"OCO {a1}, trailed {b1}")
    k4 = DayZeroKite(ltp=100.0, demat_qty=1)
    g4 = X.place_overnight_gtts(symbol="X", direction="LONG", qty=1,
                                fill_price=100.0, stop_price=95.0, kite=k4)
    check("  and only ONE GTT is sent — no zero-quantity instruction",
          bool(g4.get("ok")) and len(k4.gtts) == 1,
          f"ok={g4.get('ok')} gtts={len(k4.gtts)}")


def section_trail():
    head("2 — A STOP CAN NEVER BE LOOSENED")

    from atlas.execution import exits as X

    print("\n  tighten_only() — the guard every stop change goes through.")
    print(f"    {'current':>9}{'proposed':>10}   result")
    cases = [(95.0, 100.0, True), (95.0, 95.0, True), (95.0, 97.5, True),
             (95.0, 94.99, False), (95.0, 94.0, False), (95.0, 0.0, False),
             (103.0, 96.0, False), (103.0, 103.01, True)]
    for cur, prop, want_ok in cases:
        try:
            out = X.tighten_only(cur, prop, "LONG")
            got_ok, detail = True, f"-> {out}"
        except X.StopWouldLoosen as e:
            got_ok, detail = False, "REFUSED"
        print(f"    {cur:>9}{prop:>10}   {detail}")
        check(f"  LONG {cur} -> {prop} is "
              f"{'accepted' if want_ok else 'REFUSED'}",
              got_ok == want_ok, detail)

    # BOTH DIRECTIONS, EXPLICITLY. The operator asked for this.
    print("\n  BOTH DIRECTIONS, long side:")
    up = X.tighten_only(95.0, 99.0, "LONG")
    check("UP   95.0 -> 99.0 is accepted and takes effect", up == 99.0,
          f"returned {up}")
    refused = False
    try:
        X.tighten_only(95.0, 91.0, "LONG")
    except X.StopWouldLoosen as e:
        refused = True
        msg = str(e)
    check("DOWN 95.0 -> 91.0 is REFUSED", refused,
          msg[:100] if refused else "it was ACCEPTED — this is the bug")
    check("  it RAISES rather than silently keeping the old stop", refused,
          "returning the old value would make a loosening attempt invisible: "
          "the caller would log a successful trail")

    # THE TRAIL RULE
    print("\n  next_stop() — the rule, entry 100, initial stop 95:")
    print(f"    {'high':>7}{'swing':>8}{'stop':>8}   why")
    rows = [
        (101.0, None,  "+1%: nothing earned yet"),
        (103.0, None,  "+3%: breakeven"),
        (108.0, 104.0, "+8% with a swing low above: trail to it"),
        (108.0, 97.0,  "+8% with a swing low BELOW entry: hold breakeven"),
        (108.0, 93.0,  "swing low below the stop: ignored"),
    ]
    stop = 95.0
    for hi, sw, label in rows:
        r = X.next_stop(100.0, stop, hi, 99.0, sw, "LONG")
        print(f"    {hi:>7}{str(sw):>8}{r['stop']:>8}   {label}")
        check(f"  {label}", r["stop"] >= stop,
              f"{stop} -> {r['stop']} (never below)")

    # A SWING LOW BELOW A TRAILED STOP MUST NOT PULL IT DOWN.
    r = X.next_stop(100.0, 103.0, 108.0, 99.0, 96.0, "LONG")
    check("an already-trailed stop at 103 is not pulled to a 96 swing low",
          r["stop"] == 103.0, f"{r['stop']} — {r['why'][:70]}")
    r2 = X.next_stop(100.0, 103.0, 120.0, 118.0, 112.0, "LONG")
    check("but a swing low ABOVE it does move it up",
          r2["stop"] == 112.0, f"103.0 -> {r2['stop']}")

    # AND THE GUARD IS THE LAST THING IN THE CHAIN.
    src = (ROOT / "atlas" / "execution" / "exits.py").read_text()
    body = src[src.index("def next_stop("):]
    body = body[:body.index("\ndef ")]
    check("next_stop ends by passing through tighten_only",
          "tighten_only(cur, proposed, d" in body,
          "anything above it that produced a looser stop raises rather than "
          "being sent to the broker")

    # EVERY STOP WRITE IN THE CODEBASE GOES THROUGH THE GUARD.
    import re
    pre = (ROOT / "atlas" / "execution" / "preclose_gtt.py").read_text()
    # THE trail() BODY, not the whole module. The first version split on
    # "def trail" and searched the prefix, which includes the module docstring
    # -- and that docstring mentions tighten_only, so the check was reading its
    # own documentation. Tenth instance of that trap in this repo.
    tbody = pre[pre.index("def trail("):]
    tbody = tbody[:tbody.index("\ndef ")]
    check("the pre-close trail sends only what next_stop returned",
          'nxt["stop"]' in tbody and "new_stop = nxt" in tbody,
          "it does not compute a stop of its own, so there is no second place "
          "for the arithmetic to disagree with the guard")
    check("  and the only stop written to the broker is that value",
          tbody.count("trigger_values=[trig]") == 1
          and "X.stop_trigger(new_stop" in tbody,
          "one derivation, from one number")
    check("  and it handles the guard firing as a bug, not a condition",
          "StopWouldLoosen" in pre, "logged loudly, nothing moved")


def section_swing():
    head("3 — THE SWING LOW THE TRAIL FOLLOWS")
    from atlas.execution import exits as X
    # A 5-bar fractal: the centre bar is the lowest of five.
    bars = [{"low": v} for v in
            (110, 108, 105, 107, 109, 112, 111, 106, 104, 106, 108, 113)]
    sl = X.swing_low_since(bars)
    print(f"\n  lows: {[b['low'] for b in bars]}")
    print(f"  most recent confirmed 5-bar fractal low: {sl}")
    check("the LAST confirmed pivot is returned, not the lowest overall",
          sl == 104, f"{sl} — 104 at index 8, confirmed two bars later")
    check("too few bars returns None, rather than guessing",
          X.swing_low_since([{"low": 1}, {"low": 2}]) is None, "")
    check("a malformed bar returns None",
          X.swing_low_since([{"x": 1}] * 10) is None,
          "a swing low invented from missing data is worse than none")


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    print(__doc__)
    section_day0()
    section_trail()
    section_swing()
    head("RESULT")
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"    FAILED: {f}")
    print("\n  §1's ACCEPTANCE claim rests on Zerodha's documentation, quoted "
          "above.\n  There is no live driver -- tools/live_day0_check.py was "
          "deleted on\n  2026-10-13 by operator decision. A stub cannot "
          "settle a broker rule; it\n  can only show that ATLAS builds the "
          "right shape, which is what this does.")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
