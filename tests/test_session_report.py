#!/usr/bin/env python3
"""
THE REPORTING CADENCE
=====================

Two things are asserted here, and the second is the one that matters.

ONE: the right messages were silenced. Routine per-cycle status does not reach
Telegram; entries, self-halts, protection failures and PRECLOSE BLIND still do.
A regression here is silent in the worst way -- nobody notices an alert that
stopped arriving until the session it was needed.

TWO: "CONFIRMED" in a report means read back from the broker. On 2026-10-15
four protective mechanisms all reported placing a stop and all four orders were
rejected, so a report built from what ATLAS attempted would have said the
position was protected. Every assertion below drives at that: an unreadable
broker reports UNKNOWN and never CONFIRMED, a recorded GTT id that is not
active reports RECORDED BUT NOT LIVE, and an open position without a confirmed
stop puts a warning in the first lines of the message rather than the middle.

Everything is stubbed: no network, no broker, no Supabase.
"""

import os
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ["ATLAS_DRILL"] = "1"
logging.basicConfig(level=logging.CRITICAL)

from atlas.reporting import cadence                      # noqa: E402
from atlas.reporting import session_report as SR          # noqa: E402

ENTRY = 1453.70


def _trade(**kw):
    t = {"id": 1, "symbol": "ATHERENERG", "direction": "LONG", "qty": 7,
         "entry_price": ENTRY, "stop_price": 1417.40, "target_price": 1523.30,
         "published_stop": 1418.86, "stop_source": "bull_fvg_low",
         "publication_kind": "candidate", "status": "OPEN", "product": "CNC",
         "exit_legs": {}, "exit_mechanism": None, "created_at":
         "2026-10-16T09:21:04+05:30"}
    t.update(kw)
    return t


def _prot(ok=True, orders=None, gtts=None, why=""):
    return {"ok": ok, "why": why, "orders": orders or {}, "gtts": gtts or {}}


def main() -> int:
    ok = True

    print("WHAT WAS SILENCED, AND WHAT WAS NOT")
    print("-" * 78)

    # THE KEEP LIST, NAMED. These are the four classes the operator required to
    # keep firing immediately, plus the infrastructure failures: a data failure
    # is not routine status, and waiting until 11:30 to learn there is no signal
    # batch wastes the session.
    must_fire = ["ENTRY ERROR", "RECONCILE FAILED", "EXIT RECONCILE",
                 "EXIT RECONCILE FAILED", "PRECLOSE BLIND", "NO PRICES",
                 "LEDGER UNREADABLE", "SIGNALS UNREADABLE", "NO SIGNAL BATCH",
                 "STALE BATCH", "CYCLE ERROR", "HALT PATH WRONG",
                 "ENTERED", "SKIPPED_NO_TICK_SIZE_CANNOT_PRICE"]
    for k in must_fire:
        quiet, _ = cadence.telegram_silenced(k)
        ok &= not quiet
        print(f"  {k:<46}{'fires' if not quiet else '** SILENCED **'}")

    print()
    must_be_quiet = ["PAUSED", "RECONCILE", "BLOCKED_SENTIMENT",
                     "BLOCKED_REGIME", "BLOCKED_KILLSWITCH",
                     "RANGE_BREAK_OBSERVED"]
    for k in must_be_quiet:
        quiet, why = cadence.telegram_silenced(k)
        ok &= quiet and bool(why)
        print(f"  {k:<46}{'silent, with a reason' if quiet and why else '** STILL SENDS **'}")

    # "RECONCILE FAILED" starts with no silenced prefix but shares a word with
    # a silenced kind. Prefix matching must not swallow it.
    quiet, _ = cadence.telegram_silenced("RECONCILE FAILED")
    ok &= not quiet
    print(f"  {'RECONCILE FAILED is not RECONCILE':<46}"
          f"{'ok' if not quiet else '** SWALLOWED **'}")

    print()
    print("THE JOURNAL KEEPS WHAT TELEGRAM DOES NOT")
    print("-" * 78)
    from atlas.signal import market_open as mo
    sent = []
    mo.send = lambda text: sent.append(text) or True

    class _S:
        def __init__(self): self.failures = []
        def note_failure(self, k): self.failures.append(k)

    mo._alerted.clear()
    mo._session = _S()
    mo.alert("BLOCKED_SENTIMENT", "TCS: nifty below VWAP", key="TCS")
    good = not sent and mo._session.failures == ["BLOCKED_SENTIMENT"]
    ok &= good
    print(f"  {'silenced alert -> counted, not sent':<46}"
          f"{'ok' if good else '** ' + str(sent) + str(mo._session.failures) + ' **'}")

    mo._alerted.clear()
    mo.alert("NO PRICES", "Upstox returned nothing", key="upstox")
    good = len(sent) == 1
    ok &= good
    print(f"  {'kept alert -> sent':<46}"
          f"{'ok' if good else '** NOT SENT **'}")

    # THE ENTRY EVENT IS UNTOUCHED, and it does not go through alert() at all.
    sent.clear()
    mo.notify_entry({"symbol": "TCS", "direction": "LONG", "sl": 96.0},
                    {"status": "ENTERED", "qty": 10, "entry_price": 100.0,
                     "stop_price": 96.0, "risk_actual": 40, "notional": 1000})
    good = len(sent) == 1 and "TCS" in sent[0]
    ok &= good
    print(f"  {'an entry still fires immediately':<46}"
          f"{'ok' if good else '** ENTRY WENT QUIET **'}")
    mo._session = None

    print()
    print("CONFIRMED MEANS READ BACK FROM THE BROKER")
    print("-" * 78)

    # An unreadable broker must never produce CONFIRMED. This is the whole
    # point: UNKNOWN is a different answer from protected, and treating them
    # the same is what a report built from intent does.
    v = SR.confirm_legs(_trade(), _prot(ok=False, why="token expired"))
    good = v["stop"] == "UNKNOWN" and v["target"] == "UNKNOWN"
    ok &= good
    print(f"  {'broker unreadable -> UNKNOWN, not confirmed':<46}"
          f"{'ok' if good else '** ' + str(v) + ' **'}")

    # Readable and nothing there is NONE, which is the honest answer and the
    # one 2026-10-15 needed.
    v = SR.confirm_legs(_trade(), _prot())
    good = v["stop"] == "NONE" and v["target"] == "NONE"
    ok &= good
    print(f"  {'readable, nothing resting -> NONE':<46}"
          f"{'ok' if good else '** ' + str(v) + ' **'}")

    # A resting SL order confirms the stop and says nothing about the target.
    v = SR.confirm_legs(_trade(), _prot(orders={"ATHERENERG": [
        {"order_id": "X1", "trigger": 1417.4, "price": 1410.3, "qty": 7,
         "type": "SL", "status": "OPEN", "side": "SELL"}]}))
    good = (v["stop"] == "CONFIRMED" and v["stop_at"] == 1417.4
            and v["target"] == "NONE")
    ok &= good
    print(f"  {'resting SL order -> stop CONFIRMED':<46}"
          f"{'ok' if good else '** ' + str(v) + ' **'}")

    # AN OCO GTT, AND THE SIDE IS DECIDED BY THE ENTRY, not by leg order. The
    # triggers are given target-first here deliberately -- reading them
    # positionally would call 1523.30 the stop.
    v = SR.confirm_legs(_trade(), _prot(gtts={"ATHERENERG": [
        {"trigger_id": 70001, "type": "two-leg",
         "triggers": [1523.3, 1417.4], "last_price": ENTRY, "legs": []}]}))
    good = (v["stop"] == "CONFIRMED" and v["stop_at"] == 1417.4
            and v["target"] == "CONFIRMED" and v["target_at"] == 1523.3)
    ok &= good
    print(f"  {'OCO legs sorted by entry, not order':<46}"
          f"{'ok' if good else '** ' + str(v) + ' **'}")

    # A SHORT mirrors it: the stop is ABOVE the entry.
    v = SR.confirm_legs(_trade(direction="SHORT"), _prot(gtts={"ATHERENERG": [
        {"trigger_id": 70002, "type": "two-leg",
         "triggers": [1417.4, 1523.3], "last_price": ENTRY, "legs": []}]}))
    good = v["stop_at"] == 1523.3 and v["target_at"] == 1417.4
    ok &= good
    print(f"  {'short: stop is the leg above entry':<46}"
          f"{'ok' if good else '** ' + str(v) + ' **'}")

    # An order that is not resting is not protection. COMPLETE means it fired,
    # REJECTED is what happened four times on 2026-10-15.
    for st in ("REJECTED", "CANCELLED", "COMPLETE"):
        good = st.upper() not in SR.LIVE_ORDER_STATUS
        ok &= good
        print(f"  {('order status ' + st + ' is not protection'):<46}"
              f"{'ok' if good else '** COUNTED AS LIVE **'}")

    print()
    print("AN UNPROTECTED POSITION IS THE FIRST THING IN THE MESSAGE")
    print("-" * 78)

    SR.entries_today = lambda: [_trade()]
    SR.open_trades = lambda: [_trade()]
    SR.closed_today = lambda: []
    SR.decisions_today = lambda: (103, {"BLOCKED_SENTIMENT": 101}.copy(),
                                  ("BLOCKED_SENTIMENT", 101))
    SR.gate_state = lambda: ["halt file    HALTED — PRECLOSE_GTT_FAILED"]
    SR.live_prices = lambda syms: {"ATHERENERG": 1460.0}
    SR.broker_protection = lambda: _prot()

    body, naked = SR.build("1130")
    head = body.split("\n")[:6]
    good = (len(naked) == 1
            and any("NO CONFIRMED STOP" in l for l in head)
            and body.index("NO CONFIRMED STOP") < body.index("ENTRIES TODAY"))
    ok &= good
    print(f"  {'warning is in the first lines, not buried':<46}"
          f"{'ok' if good else '** BURIED **'}")

    # And it is not cried wolf: a confirmed stop produces no warning at all.
    SR.broker_protection = lambda: _prot(gtts={"ATHERENERG": [
        {"trigger_id": 70001, "type": "two-leg",
         "triggers": [1417.4, 1523.3], "last_price": ENTRY, "legs": []}]})
    body2, naked2 = SR.build("1130")
    good = not naked2 and "NO CONFIRMED STOP" not in body2
    ok &= good
    print(f"  {'confirmed stop -> no warning':<46}"
          f"{'ok' if good else '** FALSE ALARM **'}")

    # Running P&L, realised P&L and the day total are all present.
    good = ("DAY P&L" in body2 and "OPEN — 1" in body2
            and "CANDIDATES EVALUATED — 103" in body2
            and "BLOCKED_SENTIMENT" in body2)
    ok &= good
    print(f"  {'P&L, gates and the top blocker are in it':<46}"
          f"{'ok' if good else '** MISSING A SECTION **'}")

    print()
    print("THE 15:50 HANDOVER NAMES EVERY GTT AND WHETHER IT CONFIRMED")
    print("-" * 78)

    # RECORDED IS NOT LIVE. exit_legs holding a trigger_id says ATLAS believes
    # it placed one; only get_gtts() returning it active says it exists. This
    # case -- recorded, not live -- is the 7 October failure and must be named
    # as such rather than shown as a GTT.
    SR.open_trades = lambda: [_trade(exit_legs={"oco_trigger_id": 70001,
                                                "oco_qty": 7},
                                     exit_mechanism="GTT_SPLIT")]
    SR.entries_today = lambda: []
    SR.broker_protection = lambda: _prot()
    body3, naked3 = SR.build("dayend")
    good = ("RECORDED BUT NOT LIVE" in body3 and "70001" in body3
            and "OVERNIGHT GTT HANDOVER" in body3 and len(naked3) == 1)
    ok &= good
    print(f"  {'recorded id, no active GTT -> named':<46}"
          f"{'ok' if good else '** ' + str([l for l in body3.splitlines() if "HANDOVER" in l or "70001" in l]) + ' **'}")

    good = "flushes day orders at 16:00" in body3
    ok &= good
    print(f"  {'day-end says what 16:00 does':<46}"
          f"{'ok' if good else '** NO CONSEQUENCE STATED **'}")

    SR.broker_protection = lambda: _prot(gtts={"ATHERENERG": [
        {"trigger_id": 70001, "type": "two-leg",
         "triggers": [1417.4, 1523.3], "last_price": ENTRY, "legs": []}]})
    body4, naked4 = SR.build("dayend")
    good = "✅ CONFIRMED — GTT 70001" in body4 and not naked4
    ok &= good
    print(f"  {'recorded AND live -> CONFIRMED':<46}"
          f"{'ok' if good else '** ' + str(naked4) + ' **'}")

    # The non-day-end reports must NOT carry the handover block -- it is the
    # 15:50 report's one difference and claiming it at 11:30, before
    # preclose_gtt has run, would report every position as having no GTT.
    body5, _ = SR.build("1130")
    good = "OVERNIGHT GTT HANDOVER" not in body5
    ok &= good
    print(f"  {'11:30 does not claim a handover yet':<46}"
          f"{'ok' if good else '** PREMATURE **'}")

    print()
    print("THE DRILL FENCE")
    print("-" * 78)
    sends = []
    SR.send = lambda text: sends.append(text) or True
    sys.argv = ["session_report", "--slot", "1130"]
    rc = SR.main()
    good = rc == 0 and not sends
    ok &= good
    print(f"  {'ATLAS_DRILL=1 -> built, nothing sent':<46}"
          f"{'ok' if good else '** SENT ' + str(len(sends)) + ' **'}")

    print("-" * 78)
    print("SESSION REPORT:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
