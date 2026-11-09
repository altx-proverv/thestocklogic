#!/usr/bin/env python3
"""A FILL THAT CANNOT BE READ IS NEVER RECORDED (OPEN D-022).

reconcile promoted a COMPLETE order to OPEN with `average_price or
row.entry_price` and `filled_quantity or row.qty` -- the pre-order price and
the ordered quantity, written into the ledger that gets published. Now an
unreadable fill leaves the row PENDING, is recorded as refused, and alerts on
every pass. Nothing reaches Kite, Supabase or Telegram.
"""
import os
import sys
import logging
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ.setdefault("ATLAS_HALT_FILE", "/tmp/atlas_test_halt_rec")
EV = Path(tempfile.mkdtemp()) / "ev.jsonl"
os.environ["TSL_ORDER_EVENTS_FILE"] = str(EV)
logging.basicConfig(level=logging.CRITICAL)

import json                                                     # noqa: E402
import atlas.execution.order_events as OE                       # noqa: E402
OE._post = lambda row: None
OE.LOCAL = EV
import atlas.execution.reconcile as RC                          # noqa: E402
import atlas.reporting.telegram as TG                           # noqa: E402
import atlas.execution.protection as PR                         # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<64} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


ROW = {"id": 41, "symbol": "TESTSYM", "direction": "LONG", "qty": 10,
       "entry_price": 100.0, "stop_price": 95.0}
patches, sent, registered = [], [], []
RC._patch = lambda rid, p: patches.append((rid, p)) or True
TG.send = lambda m: sent.append(m) or True
PR.register = lambda t, why, wanted_stop=None: registered.append(t) or {}


def run(order):
    patches.clear(); sent.clear(); registered.clear(); OE._seen.clear()
    RC.stale_pending = lambda now=None: [dict(ROW)]
    RC._orders_by_tag = lambda: {41: order}
    return RC.settle()


def promoted():
    return [p for _, p in patches if p.get("status") == "OPEN"]


print("\nUNREADABLE FILLS ARE NOT RECORDED")
for label, o in (("average_price missing", {"order_id": "O1", "status": "COMPLETE", "filled_quantity": 10}),
                 ("average_price 0", {"order_id": "O1", "status": "COMPLETE", "average_price": 0, "filled_quantity": 10}),
                 ("average_price garbage", {"order_id": "O1", "status": "COMPLETE", "average_price": "x", "filled_quantity": 10}),
                 ("filled_quantity missing", {"order_id": "O1", "status": "COMPLETE", "average_price": 101.5}),
                 ("filled_quantity 0", {"order_id": "O1", "status": "COMPLETE", "average_price": 101.5, "filled_quantity": 0})):
    s = run(o)
    check(f"{label}: not promoted to OPEN", promoted(), [])
    check(f"  no pre-order price written",
          any(p.get("entry_price") == ROW["entry_price"] for _, p in patches), False)
    check(f"  counted unsettled and fill_unreadable",
          (s["unsettled"], s.get("fill_unreadable")), (1, 1))
    check(f"  alerted, naming the symbol",
          len(sent) == 1 and "TESTSYM" in sent[0] and "FILL UNREADABLE" in sent[0], True)
    check(f"  recorded REFUSED FILL_UNREADABLE",
          any(json.loads(l)["code"] == "FILL_UNREADABLE"
              for l in EV.read_text().splitlines()), True)

print("\nTHE NEXT PASS ALERTS AGAIN")
run({"order_id": "O1", "status": "COMPLETE", "filled_quantity": 10})
check("a second pass sends a second alert", len(sent), 1)

print("\nA READABLE FILL IS RECORDED FROM THE BROKER, NOT THE ROW")
s = run({"order_id": "O2", "status": "COMPLETE", "average_price": 101.5, "filled_quantity": 7})
check("promoted to OPEN", len(promoted()), 1)
check("  at the broker's average price", promoted()[0]["entry_price"], 101.5)
check("  with the broker's filled quantity", promoted()[0]["qty"], 7)
check("  and registered unprotected with that quantity", registered[0]["qty"], 7)
check("  no fill-unreadable alert", any("FILL UNREADABLE" in m for m in sent), False)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — a fill is read from the broker or not recorded at all")
