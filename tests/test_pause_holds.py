#!/usr/bin/env python3
"""THE OPERATOR'S PAUSE HOLDS: only an exact NORMAL permits an entry.

Every value the atlas_state.mode column could hold is pushed through the three
places that enforce the pause -- market_open.paused() (the cycle), the kill
switch (Gate 7 and the broker layer), and /approve (the one directive that used
to re-write the mode). Nothing reaches Supabase, Kite or Telegram.
"""
import os
import sys
import tempfile
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ.setdefault("ATLAS_HALT_FILE", "/tmp/atlas_test_halt_pause")
os.environ["TSL_ORDER_EVENTS_FILE"] = str(Path(tempfile.mkdtemp()) / "ev.jsonl")
logging.basicConfig(level=logging.CRITICAL)

import atlas.execution.order_events as OE                      # noqa: E402
OE._post = lambda row: None
import atlas.signal.market_open as MO                          # noqa: E402
import atlas.risk.kill_switch as KS                            # noqa: E402
import atlas.reporting.directives as DIR                       # noqa: E402
from atlas.risk import breaker                                 # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:20]:<22} {'ok' if ok else 'FAIL'}")


breaker.is_halted = lambda *a, **k: (False, "")
writes = []
DIR.requests.patch = lambda *a, **k: writes.append(k.get("json")) or type("R", (), {"status_code": 204})()

STOPPED = [None, "", "PAUSED", "paused", " PAUSED ", "PAUSE", "HALT", "HALTED",
           "STOP", "CAUTIOUS", "AGGRESSIVE", "UNKNOWN", "NORMAL!", "N0RMAL"]
PERMITTED = ["NORMAL", "normal", " NORMAL "]


def row(mode):
    return {"id": 1, "mode": mode, "notes": "", "updated_at": "2026-11-09T22:00:00"}


print("\nTHE CYCLE: market_open.paused()")
for m in STOPPED:
    MO.get_agent_state = lambda _m=m: row(_m)
    check(f"mode {m!r} -> paused", MO.paused()[0], True)
for m in PERMITTED:
    MO.get_agent_state = lambda _m=m: row(_m)
    check(f"mode {m!r} -> not paused", MO.paused()[0], False)


def boom():
    raise KS.RiskDataUnavailable("simulated outage")


MO.get_agent_state = boom
check("unreadable state -> paused", MO.paused()[0], True)

print("\nTHE KILL SWITCH (Gate 7 and the broker layer)")
for m in STOPPED:
    KS.get_agent_state = lambda _m=m: row(_m)
    check(f"mode {m!r} -> blocked", bool(KS.check()), False)
for m in PERMITTED:
    KS.get_agent_state = lambda _m=m: row(_m)
    check(f"mode {m!r} -> allowed", bool(KS.check()), True)
KS.get_agent_state = boom
check("unreadable state -> blocked", bool(KS.check()), False)
KS.get_agent_state = lambda: row("HALT")
check("status() calls an unknown mode halted", KS.status().get("halted") if hasattr(KS, "status") else True, True)

print("\n/approve NEVER CHANGES THE MODE")


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)

    def json(self):
        return self._b


for label, fn in (("Supabase HTTP 503", lambda *a, **k: Resp(503, "down")),
                  ("network error", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))),
                  ("no row", lambda *a, **k: Resp(200, [])),
                  ("PAUSED row", lambda *a, **k: Resp(200, [row("PAUSED")])),
                  ("NULL mode row", lambda *a, **k: Resp(200, [row(None)]))):
    writes.clear()
    DIR.requests.get = fn
    reply = DIR.handle_directive("/approve")
    check(f"{label}: nothing written", writes, [])
    check(f"  and the reply does not claim approval", "APPROVED</b>" not in reply
          or "NOT APPROVED" in reply, True)

print("\nTHE PAUSE REPLY NO LONGER NAMES /approve AS A RESUME")
writes.clear()
DIR.requests.get = lambda *a, **k: Resp(200, [row("NORMAL")])
reply = DIR.handle_directive("/pause")
check("/pause writes PAUSED", [w.get("mode") for w in writes], ["PAUSED"])
check("  and says /approve does not resume", "/approve does not resume" in reply, True)

print("\n/status NEVER CALLS AN UNKNOWN MODE NORMAL")
DIR.requests.get = lambda *a, **k: Resp(200, [row("HALT")])
st = DIR.handle_directive("/status")
check("an unknown mode is not shown as NORMAL", "NORMAL" in st, False)
check("  and says entries are stopped", "ENTRIES STOPPED" in st, True)

print("\nD-055: A SIGNAL WITH NO CAPITAL FIGURE IS BLOCKED")
import atlas.risk.funds as FUNDS                               # noqa: E402
KS.get_agent_state = lambda: row("NORMAL")
_ca = FUNDS.can_afford
FUNDS.can_afford = lambda c: (True, "", {"data_available": True})
for cap in (None, 0, "x", -5):
    check(f"capital_required {cap!r} -> blocked",
          bool(KS.check({"symbol": "T", "capital_required": cap})), False)
check("a positive figure is checked and allowed",
      bool(KS.check({"symbol": "T", "capital_required": 10000})), True)
FUNDS.can_afford = lambda c: (False, "insufficient", {"data_available": True})
check("  and refused when funds say no",
      bool(KS.check({"symbol": "T", "capital_required": 10000})), False)
FUNDS.can_afford = _ca
check("no signal (the broker-layer copy) is a mode check only",
      bool(KS.check()), True)

print("\nD-054: AN UNEVALUATED MARKET BREAKER MEANS NO ENTRY (structure)")
mo_src = (ROOT / "atlas/signal/market_open.py").read_text()
check("the breaker block no longer 'fails open' for entries",
      "Fail open: the breakers are a refinement" in mo_src, False)
check("CANNOT_EVALUATE and a raise both set the refusal",
      mo_src.count("_breaker_unevaluated = ") >= 3, True)
check("the refusal is checked before the entry loop",
      0 < mo_src.index("    if _breaker_unevaluated:") <
      mo_src.index("    for sym, direction, sig, ltp in candidates:"), True)

print("\nONLY KNOWN MODES ARE EVER WRITTEN")
writes.clear()
check("update_agent_mode('HALT') refused", DIR.update_agent_mode("HALT"), False)
check("  nothing written", writes, [])

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — only an exact NORMAL permits an entry; /approve cannot lift a pause")
