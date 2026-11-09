#!/usr/bin/env python3
"""A FAILED READ NEVER RETURNS THE SAME VALUE AS AN EMPTY ONE (OPEN D-020, D-021).

The GTT list and the open-positions list both returned [] on failure. Orphan
cleanup then saw no triggers -- and an orphaned trigger can RE-ENTER a closed
symbol -- and the protection sweep saw no positions and reported a clean book.
Each read now returns None when it could not ask, [] when it asked and found
nothing. Nothing reaches Kite or Supabase.
"""
import os
import sys
import logging
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ.setdefault("ATLAS_HALT_FILE", "/tmp/atlas_test_halt_unread")
os.environ["TSL_ORDER_EVENTS_FILE"] = str(Path(tempfile.mkdtemp()) / "ev.jsonl")
logging.basicConfig(level=logging.CRITICAL)

import atlas.execution.order_events as OE                      # noqa: E402
OE._post = lambda row: None
import atlas.execution.broker as BR                            # noqa: E402
import atlas.execution.gtt as G                                # noqa: E402
import atlas.execution.exits as X                              # noqa: E402
import atlas.signal.market_open as MO                          # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<64} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


class Kite:
    def __init__(self, gtts=None, raises=False, positions=None, orders=None):
        self._g, self._r = gtts, raises
        self._p = positions or []
        self._o = orders or []
        self.deleted, self.cancelled = [], []

    def get_gtts(self):
        if self._r:
            raise OSError("broker down")
        return self._g

    def positions(self):
        return {"net": self._p}

    def orders(self):
        return self._o

    def delete_gtt(self, trigger_id):
        self.deleted.append(trigger_id)

    def cancel_order(self, variety, order_id):
        self.cancelled.append(order_id)


class R:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)

    def json(self):
        return self._b


ACTIVE = [{"id": 7, "status": "active"}, {"id": 8, "status": "triggered"}]

print("\nlist_active_gtts")
saved_gk = BR.get_kite
BR.get_kite = lambda: None
check("no broker session -> None", G.list_active_gtts(), None)
BR.get_kite = lambda: Kite(raises=True)
check("broker raises -> None", G.list_active_gtts(), None)
BR.get_kite = lambda: Kite(gtts=None)
check("broker returns None -> None", G.list_active_gtts(), None)
BR.get_kite = lambda: Kite(gtts=[])
check("broker answers, nothing resting -> []", G.list_active_gtts(), [])
BR.get_kite = lambda: Kite(gtts=ACTIVE)
check("only active ones", [g["id"] for g in G.list_active_gtts()], [7])

print("\nlist_atlas_gtts")
import requests                                                 # noqa: E402
saved_get = requests.get
BR.get_kite = lambda: Kite(raises=True)
check("broker unreadable -> None", G.list_atlas_gtts(), None)
BR.get_kite = lambda: Kite(gtts=ACTIVE)
requests.get = lambda *a, **k: R(503, "down")
check("recorded ids unreadable (503) -> None", G.list_atlas_gtts(), None)
requests.get = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
check("recorded ids unreadable (raise) -> None", G.list_atlas_gtts(), None)
requests.get = lambda *a, **k: R(200, [{"gtt_trigger_id": 7}])
check("both readable -> matched", [g["id"] for g in G.list_atlas_gtts()], [7])
BR.get_kite = lambda: Kite(gtts=[])
check("nothing resting -> []", G.list_atlas_gtts(), [])

print("\ngtt_summary")
BR.get_kite = lambda: Kite(raises=True)
s = G.gtt_summary()
check("unreadable is not 'No pending'", "No pending" in s, False)
check("  and says unknown", "UNKNOWN" in s, True)
requests.get = saved_get
BR.get_kite = saved_gk

print("\nopen_positions_for_reconcile")
saved_mo_get = MO.requests.get
MO.requests.get = lambda *a, **k: R(500, "down")
check("HTTP 500 -> None", MO.open_positions_for_reconcile(), None)
MO.requests.get = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
check("network error -> None", MO.open_positions_for_reconcile(), None)
MO.requests.get = lambda *a, **k: R(200, [])
check("answered, nothing open -> []", MO.open_positions_for_reconcile(), [])
MO.requests.get = saved_mo_get

print("\nreconcile_exits: a flat position with the GTT list unreadable is flagged")
trade = {"id": 1, "symbol": "TESTSYM", "exit_legs": {"stop_trigger_id": 7}}
k = Kite(positions=[], orders=[])
out = X.reconcile_exits([trade], kite=k, positions=[], orders=[],
                        atlas_gtts=None, gtts_unreadable=True)
check("flagged unreadable", out.get("gtts_unreadable"), True)
check("  with an alert naming the symbol",
      any("TESTSYM" in a and "could not be read" in a for a in out["alerts"]), True)
check("  the recorded leg is still cancelled", k.deleted, [7])
out2 = X.reconcile_exits([trade], kite=Kite(), positions=[], orders=[],
                         atlas_gtts=[], gtts_unreadable=False)
check("readable empty list: no unreadable flag", out2.get("gtts_unreadable"), None)

print("\nD-049 reconcile: unreadable PENDING rows are not a clean pass")
import atlas.execution.reconcile as RC                          # noqa: E402
_g = RC.requests.get
RC.requests.get = lambda *a, **k: R(503, "down")
check("stale_pending HTTP 503 -> None", RC.stale_pending(), None)
s1 = RC.settle()
check("settle reports pending_readable False", s1.get("pending_readable"), False)
check("  and checked None, not 0", s1.get("checked"), None)
RC.requests.get = lambda *a, **k: R(200, [])
check("stale_pending answered empty -> []", RC.stale_pending(), [])
check("  settle: readable, 0 checked",
      (RC.settle().get("pending_readable"), RC.settle().get("checked")), (True, 0))
RC.requests.get = _g

print("\nD-050 daily report: unreadable positions and mode are said, not hidden")
import atlas.reporting.daily_report as DR                       # noqa: E402
DR._get = lambda q, what: None
check("get_positions unreadable -> None", DR.get_positions(), None)
check("get_agent_state unreadable -> UNREADABLE", DR.get_agent_state()["mode"], "UNREADABLE")
DR.get_regime = lambda: {"regime": "unknown", "source": "test"}
DR.get_funds = lambda: (None, "test")
DR.get_today_decisions = lambda: []
DR.get_live_prices = lambda syms: ({}, False, "")
body = DR.build_report()
check("the report says positions are UNREADABLE", "Positions: UNREADABLE" in body, True)
check("  and shows the mode as UNREADABLE", "Mode: UNREADABLE" in body, True)

print("\nD-051 batch date: unreadable raises; empty is ''")
MO.requests.get = lambda *a, **k: R(503, "down")
try:
    MO.get_latest_batch_date(); got = "returned"
except Exception:
    got = "raised"
check("HTTP 503 -> raises (SIGNALS UNREADABLE)", got, "raised")
MO.requests.get = lambda *a, **k: R(200, [])
check("answered, no batch -> ''", MO.get_latest_batch_date(), "")
MO.requests.get = saved_mo_get

print("\nD-052 trail: unreadable bars are 'not evaluated', not 'no move needed'")
import atlas.execution.preclose_gtt as PG                       # noqa: E402
from datetime import date as _date                              # noqa: E402
check("no bars file -> None", PG._bars_for("NO_SUCH_SYMBOL_XYZ", _date(2026, 11, 6)), None)
PG._bars_for = lambda sym, today, n=40: None
tr = PG.trail({"symbol": "TESTSYM", "direction": "LONG", "qty": 5, "stop_price": 95.0,
               "entry_price": 100.0, "entry_date": "2026-11-01", "id": 9,
               "exit_legs": {"trail_trigger_id": 1, "trail_qty": 5}},
              None, _date(2026, 11, 6), dry=True)
check("trail: ok (stop held), but evaluated False",
      (tr.get("ok"), tr.get("evaluated"), tr.get("moved")), (True, False, False))

print("\nD-053 confirmed items")
sys.path.insert(0, str(ROOT / "engine"))
import engine.rbe_engine as RBE                                 # noqa: E402
_rg = RBE.requests.get
RBE.requests.get = lambda *a, **k: R(503, {"message": "down"})
eng = RBE.RBEngine.__new__(RBE.RBEngine)
check("rbe fired state, HTTP 503 -> None (not set())", eng._load_fired_today(), None)
RBE.requests.get = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
check("rbe fired state, network error -> None", eng._load_fired_today(), None)
eng.fired = None
eng._fired_retry_at = 0
check("unknown fired state: nothing may fire", eng._fired_known(), False)
RBE.requests.get = lambda *a, **k: R(200, [{"symbol": "AAA", "direction": "LONG"}])
eng._fired_retry_at = 0
check("readable again: fires resume with the real state",
      (eng._fired_known(), eng.fired), (True, {"AAA:LONG"}))
RBE.requests.get = _rg

import engine.publish_protection as PP                          # noqa: E402
_pg = PP.requests.get
PP.requests.get = lambda *a, **k: R(500, "down")
check("publish_protection: unreadable -> None", PP.open_positions(), None)
check("  run() exits non-zero, not 'no open positions'", PP.run(), 1)
PP.requests.get = _pg

import radar.detect as RD                                       # noqa: E402
src_rd = (ROOT / "radar/detect.py").read_text()
check("radar: an unparseable session row is logged, not silent",
      "session row unparseable" in src_rd, True)

print("\nTHE CYCLE (structure -- the 700-line cycle is not driven here)")
src = (ROOT / "atlas/signal/market_open.py").read_text()
import re                                                      # noqa: E402
check("reconcile and the sweep share ONE positions read",
      len(re.findall(r"(?<![\w])_open = open_positions_for_reconcile\(\)", src)), 1)
check("an unreadable GTT list alerts", 'alert("GTT LIST UNREADABLE"' in src, True)
check("unreadable positions alert", 'alert("OPEN POSITIONS UNREADABLE"' in src, True)
check("  and the sweep reports unknown, not 0",
      'out["unprotected_outstanding"] = None' in src, True)
check("no caller turns the GTT list into [] silently",
      "atlas_gtts=list_atlas_gtts()" in src, False)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — could-not-read and nothing-there are different values, and the cycle says which")
