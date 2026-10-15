#!/usr/bin/env python3
"""
RECONCILE — AN UNREADABLE BROKER IS NOT A FLAT BROKER
=====================================================
reconcile.settle() decides the fate of PENDING rows by asking the broker what
happened. Every branch turns on telling three states apart:

    a filled order      -> promote to OPEN
    no order at all     -> CANCELLED; we died before placing
    CANNOT TELL         -> change nothing, keep blocking

The third is the one worth a test. "No orders" and "cannot read orders" are the
same shape -- an empty result -- and treating them alike is how this codebase
closed six positions from a single exception (fixed in c71c6bd). The same
mistake here cancels a row whose order is live and frees the symbol to be
entered a second time, which is precisely what the PENDING row exists to
prevent.

So _orders_by_tag() returns None for unreadable and {} for genuinely-none, and
this asserts the two never collapse into each other.

ALSO COVERED: the recovery is bounded. Rows that cannot be settled are not
retried forever -- past MAX_UNSETTLED the breaker halts, because ATLAS that
cannot read its own book has no business adding to it.

Everything is stubbed. No network, no credentials.
"""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ.setdefault("ATLAS_HALT_FILE", "/tmp/atlas_test_halt_reconcile")
logging.basicConfig(level=logging.CRITICAL)

from atlas.execution import reconcile as rc      # noqa: E402
from atlas.risk import breaker                   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
STALE = (datetime.now(IST) - timedelta(minutes=10)).isoformat()
HALT = Path(os.environ["ATLAS_HALT_FILE"])

LEDGER = {}

ROW = {"id": 7, "symbol": "BANKINDIA", "direction": "LONG", "qty": 10,
       "entry_price": 147.72, "stop_price": 144.0, "created_at": STALE,
       "status": "PENDING", "order_id": None}


def _install(rows):
    LEDGER.clear()
    for r in rows:
        LEDGER[r["id"]] = dict(r)
    rc.stale_pending = lambda now=None: [dict(r) for r in LEDGER.values()
                                         if r["status"] == "PENDING"]


def _patch(row_id, patch):
    LEDGER[row_id].update(patch)
    return True


rc._patch = _patch
breaker.requests.patch = lambda *a, **k: type(
    "R", (), {"status_code": 204, "text": ""})()
import atlas.reporting.telegram as _tg           # noqa: E402
_tg.send = lambda *a, **k: True


def run(rows, orders):
    """Settle once against a given broker answer. Returns (statuses, halted)."""
    HALT.unlink(missing_ok=True)
    breaker.reset()
    _install(rows)
    rc._orders_by_tag = lambda: orders
    rc.settle()
    return {i: LEDGER[i]["status"] for i in LEDGER}, breaker.is_halted()[0]


def main() -> int:
    ok = True
    print("SETTLING A PENDING ROW AGAINST THE BROKER")
    print("-" * 78)
    print(f"  {'broker says':<40}{'result':<16}verdict")

    cases = [
        ("order COMPLETE",
         {7: {"status": "COMPLETE", "order_id": "O1",
              "average_price": 147.85, "filled_quantity": 10}}, "OPEN"),
        ("order REJECTED",
         {7: {"status": "REJECTED", "status_message": "margin"}}, "CANCELLED"),
        ("no order carries our tag", {}, "CANCELLED"),
        ("order still in flight", {7: {"status": "OPEN"}}, "PENDING"),
        ("CANNOT READ THE BROKER", None, "PENDING"),
    ]
    for label, orders, want in cases:
        got, _ = run([ROW], orders)
        good = got[7] == want
        ok &= good
        print(f"  {label:<40}{got[7]:<16}"
              f"{'ok' if good else '** expected ' + want + ' **'}")

    print()
    print("THE DISTINCTION THAT MATTERS")
    print("-" * 78)
    unreadable, _ = run([ROW], None)
    empty, _ = run([ROW], {})
    distinct = unreadable[7] == "PENDING" and empty[7] == "CANCELLED"
    ok &= distinct
    print(f"  unreadable broker   -> {unreadable[7]}")
    print(f"  genuinely no orders -> {empty[7]}")
    print(f"  {'distinguished:':<40}"
          f"{'ok — unreadable is not flat' if distinct else '** CONFLATED **'}")

    print()
    print("BOUNDED — AND IT NO LONGER STANDS THE DAY DOWN")
    print("-" * 78)
    # THIS ASSERTION IS INVERTED FROM WHAT IT WAS, by Hemal's ruling of
    # 2026-10-16: ATLAS does not switch itself off. It used to read
    # "MAX_UNSETTLED unsettleable rows halt" and pass.
    #
    # The containment that mattered was never the account-wide one: every
    # PENDING row still blocks its OWN symbol, because Gate 3b counts PENDING,
    # so no symbol can be entered twice whatever this does. The stand-down only
    # ever stopped the OTHER symbols -- the whole universe over N stuck rows.
    # It flags instead, which alerts and names how to pause.
    flags = []
    _real_flag = breaker.flag
    breaker.flag = lambda kind, detail: (flags.append((kind, detail)), False)[1]
    try:
        rows = [dict(ROW, id=i) for i in range(7, 7 + rc.MAX_UNSETTLED)]
        _, halted = run(rows, None)
        good = (not halted) and len(flags) >= 1
        ok &= good
        print(f"  {str(rc.MAX_UNSETTLED) + ' unsettleable rows -> NOT halted':<46}"
              f"{'ok' if not halted else '** STOOD THE DAY DOWN **'}")
        print(f"  {'  and it flagged instead':<46}"
              f"{'ok — ' + flags[0][0] if flags else '** SILENT **'}")

        flags.clear()
        _, halted_one = run([ROW], None)
        fine = not halted_one and not flags
        ok &= fine
        print(f"  {'1 unsettleable row -> no halt, no flag':<46}"
              f"{'ok' if fine else '** reacted to noise **'}")
    finally:
        breaker.flag = _real_flag

    HALT.unlink(missing_ok=True)
    print("-" * 78)
    print("RECONCILE:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
