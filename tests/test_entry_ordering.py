#!/usr/bin/env python3
"""
THE ENTRY ORDERING INVARIANT
============================

    No death at any point in enter_trade can leave a position at the broker
    that the next evaluation cannot see.

That is the whole reason atlas_entry writes a PENDING row before it places an
order. It is a property of a SEQUENCE spanning atlas_entry, broker and config,
so no single module's __main__ can assert it.

WHAT IT IS PROTECTING AGAINST
-----------------------------
The original order was place_order() then _log_intent(). Gate 3b reads
atlas_trades, so a death in between left a holding nothing could see. Under the
09:37 cron that was one unrecorded position found by a human the next morning
-- _alert_unrecorded exists because it happened, GRASIM GTT 331263278 on
11 Aug. Under a market-hours loop the next cycle finds no row and re-enters,
every cycle, until something stops it.

The test kills the path at each point and asserts the same thing every time: if
a position exists, a row in a BLOCKING status exists too. The reverse is
allowed -- a row with no position over-blocks one symbol until reconcile clears
it, which is the safe direction.

ALSO COVERED: a failed reserve must place NO order. That is the half of the
guarantee that cannot be seen by inspecting the happy path.

Everything is stubbed. No network, no credentials, no order can be placed.
"""

import os
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ.setdefault("ATLAS_HALT_FILE", "/tmp/atlas_test_halt")
logging.basicConfig(level=logging.CRITICAL)

import atlas.execution.atlas_entry as ae          # noqa: E402
from atlas.config import BLOCKING_STATUSES        # noqa: E402
from atlas.risk import breaker                    # noqa: E402


class Died(Exception):
    """A process death, at a chosen point in the sequence."""


class World:
    """A fake ledger and a fake broker, so the two can be compared."""

    def __init__(self):
        self.rows = {}
        self.orders = []
        self.next_id = 2          # atlas_trades.id is a bigint; rows 2, 3, 4...
        self.die_at = None

    def reset(self, die_at=None):
        self.rows.clear()
        self.orders.clear()
        self.next_id = 2
        self.die_at = die_at
        breaker.reset()
        Path(os.environ["ATLAS_HALT_FILE"]).unlink(missing_ok=True)

    def gate3b_sees(self, symbol):
        """Exactly what Gate 3b queries: a row in a BLOCKING status."""
        return [r for r in self.rows.values()
                if r["symbol"] == symbol and r["status"] in BLOCKING_STATUSES]

    def position_at_broker(self, symbol):
        return [o for o in self.orders if o["symbol"] == symbol]


W = World()
SYMBOL = "BANKINDIA"
INTENT = {"symbol": SYMBOL, "direction": "LONG", "qty": 10,
          "entry_price": 147.72, "stop_price": 144.0,
          "risk_actual": 372.0, "notional": 1477.2}


# ── stubs ─────────────────────────────────────────────────────────
def stub_reserve(intent):
    rid = W.next_id
    W.next_id += 1
    W.rows[rid] = {"id": rid, "symbol": intent["symbol"], "status": "PENDING"}
    if W.die_at == "after_reserve":
        raise Died("after reserve, before order")
    return rid, None, ""


def stub_reserve_fails(intent):
    return None, 503, "gateway timeout"


def stub_place_order(**kw):
    W.orders.append({"tag": kw.get("tag"), "symbol": kw["symbol"]})
    if W.die_at == "after_order":
        raise Died("after order, before complete")
    return {"success": True, "order_id": "OID-1"}


def stub_place_order_refused(**kw):
    """Kite answered and refused. No order exists."""
    return {"success": False, "reason": "insufficient margin",
            "error_type": "MarginException", "determinate": True}


def stub_place_order_timeout(**kw):
    """We never heard back. The order may be live."""
    W.orders.append({"tag": kw.get("tag"), "symbol": kw["symbol"]})
    return {"success": False, "reason": "read timeout",
            "error_type": "NetworkException", "determinate": False}


def stub_patch(row_id, patch, what):
    W.rows[row_id].update(patch)
    return True


def entry_sequence():
    """The two-phase body of enter_trade, isolated from the eight gates."""
    rid, code, detail = ae._reserve_intent(INTENT)
    if rid is None:
        breaker.record_ledger_write(False, code, detail, phase="reserve")
        return "BLOCKED_NO_LEDGER"
    breaker.record_ledger_write(True, phase="reserve")

    order = ae.place_order(symbol=INTENT["symbol"], direction="LONG",
                           qty=INTENT["qty"], order_type="MARKET",
                           tag=f"ATLAS:{rid}", product="CNC")
    if not order.get("success"):
        if order.get("determinate"):
            ae._release_intent(rid, order.get("reason", ""))
            return "ORDER_FAILED"
        ae._mark_indeterminate(rid, order.get("reason", ""))
        return "ORDER_INDETERMINATE"

    breaker.record_order_ok()
    INTENT["order_id"] = order.get("order_id")
    ae._complete_intent(rid, INTENT)
    return "ENTERED"


def main() -> int:
    ae._reserve_intent = stub_reserve
    ae.place_order = stub_place_order
    ae._patch_trade = stub_patch

    ok = True
    print("THE INVARIANT: a position at the broker is always visible to Gate 3b")
    print("-" * 78)
    print(f"  {'death point':<40}{'position':<11}{'visible':<10}verdict")

    for label, die in (("no death (happy path)", None),
                       ("after reserve, before order", "after_reserve"),
                       ("after order, before complete", "after_order")):
        W.reset(die)
        try:
            entry_sequence()
        except Died:
            pass
        pos = bool(W.position_at_broker(SYMBOL))
        seen = bool(W.gate3b_sees(SYMBOL))
        safe = (not pos) or seen
        ok &= safe
        print(f"  {label:<40}{str(pos):<11}{str(seen):<10}"
              f"{'ok' if safe else '** UNSAFE **'}")

    # A reserve that fails must place nothing. Without this the guarantee is
    # only as good as the happy path.
    W.reset()
    ae._reserve_intent = stub_reserve_fails
    result = entry_sequence()
    placed = bool(W.position_at_broker(SYMBOL))
    good = (not placed) and result == "BLOCKED_NO_LEDGER"
    ok &= good
    print(f"  {'reserve fails -> no order placed':<40}{str(placed):<11}"
          f"{'n/a':<10}{'ok' if good else '** ORDER WITHOUT A ROW **'}")
    ae._reserve_intent = stub_reserve

    print()
    print("A FAILED ORDER IS NOT AN UNKNOWN ONE")
    print("-" * 78)

    # Determinate refusal: no order exists, so the symbol must be freed.
    W.reset()
    ae.place_order = stub_place_order_refused
    entry_sequence()
    freed = not W.gate3b_sees(SYMBOL)
    ok &= freed
    print(f"  {'broker refused (determinate)':<40}"
          f"symbol freed={str(freed):<8}"
          f"{'ok' if freed else '** symbol blocked all session **'}")

    # Indeterminate: the order may be live, so the row must KEEP blocking.
    # Releasing here is how a timeout becomes a duplicate position.
    W.reset()
    ae.place_order = stub_place_order_timeout
    entry_sequence()
    still_blocked = bool(W.gate3b_sees(SYMBOL))
    ok &= still_blocked
    print(f"  {'timeout (indeterminate)':<40}"
          f"still blocked={str(still_blocked):<6}"
          f"{'ok' if still_blocked else '** RELEASED — would re-enter **'}")

    Path(os.environ["ATLAS_HALT_FILE"]).unlink(missing_ok=True)
    print("-" * 78)
    print("ENTRY ORDERING:", "HOLDS" if ok else "*** VIOLATED ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
