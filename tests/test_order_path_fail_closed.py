#!/usr/bin/env python3
"""THE ORDER PATH FAILS CLOSED: no price -> no order, an unreadable order state
-> UNKNOWN (never "not filled"), an unreadable count -> an exception (never 0).
Every refusal is recorded with its reason.

Everything is stubbed. No network, no credentials, no order can be placed:
place_order and _reserve_intent raise if reached.
"""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ.setdefault("ATLAS_HALT_FILE", "/tmp/atlas_test_halt_fc")
EV = Path(tempfile.mkdtemp()) / "events.jsonl"
os.environ["TSL_ORDER_EVENTS_FILE"] = str(EV)
logging.basicConfig(level=logging.CRITICAL)

import atlas.config as CFG                                     # noqa: E402
import atlas.execution.atlas_entry as AE                       # noqa: E402
import atlas.execution.broker as BR                            # noqa: E402
import atlas.execution.order_events as OE                      # noqa: E402
from atlas.execution import range_breaker as RB                # noqa: E402
from atlas.risk import breaker, symbol_block as SB             # noqa: E402
from atlas.risk.kill_switch import RiskDataUnavailable         # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<66} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


class Reached(Exception):
    pass


def must_not_run(*a, **k):
    raise AssertionError("an order-writing primitive was reached")


posted, alerts = [], []
OE._post = lambda row: posted.append(row)
OE.LOCAL = EV
AE._alert = lambda kind, text: alerts.append((kind, text))


def events():
    return [json.loads(l) for l in EV.read_text().splitlines()] if EV.exists() else []


# ── :885 — no price, no order ─────────────────────────────────────────
saved = dict(halt=breaker.is_halted, em=CFG.ENABLE_EXIT_MANAGEMENT, rb=RB.is_blocked,
             sb=SB.is_blocked, ctx=AE.get_market_context, reg=AE.regime_allows_side,
             pos=AE.get_open_position, ltp=AE.get_ltp, chk=RB.stock_check,
             res=AE._reserve_intent, po=AE.place_order)
try:
    breaker.is_halted = lambda *a, **k: (False, "")
    CFG.ENABLE_EXIT_MANAGEMENT = True
    RB.is_blocked = lambda s: (False, "")
    SB.is_blocked = lambda s: (False, "", {})
    AE.get_market_context = lambda: {}
    AE.regime_allows_side = lambda ctx, d: (True, "", None)
    AE.get_open_position = lambda s: (True, None)
    AE._reserve_intent = must_not_run
    AE.place_order = must_not_run

    def stop_after_gate4(symbol, side, last_price=None, **k):
        raise Reached(last_price)
    RB.stock_check = stop_after_gate4

    SIG = {"symbol": "TESTSYM", "direction": "LONG", "entry_ref": 100.0,
           "entry_low": 99.5, "entry_high": 100.5, "sl": 97.0}

    print("\nNO PRICE, NO ORDER (atlas_entry.py, was :885)")
    for label, sig_ltp, broker_ltp in (("signal.ltp missing, get_ltp 0.0", None, 0.0),
                                       ("signal.ltp 0, get_ltp None", 0, None),
                                       ("signal.ltp 'nan', get_ltp raises->0", "nan", 0.0)):
        AE.get_ltp = lambda s, _v=broker_ltp: _v
        sig = dict(SIG, ltp=sig_ltp) if sig_ltp is not None else dict(SIG)
        try:
            got = AE.enter_trade(sig)
        except Reached as r:
            got = {"status": f"PASSED GATE 4 on {r}"}
        check(f"{label}: refused", got.get("status"), "SKIPPED_NO_PRICE")
    n_ref = [e for e in events() if e["code"] == "NO_PRICE"]
    check("  recorded as REFUSED, once per session for the symbol", len(n_ref), 1)
    check("  alerted once, not per cycle", len([a for a in alerts if "NO PRICE" in a[0]]), 1)
    check("  the reason names both sources", "signal.ltp" in n_ref[0]["reason"]
          and "get_ltp" in n_ref[0]["reason"], True)

    print("\nA REAL PRICE STILL REACHES THE NEXT GATE")
    AE.get_ltp = must_not_run
    try:
        AE.enter_trade(dict(SIG, ltp=100.1))
        got = None
    except Reached as r:
        got = float(str(r))
    check("signal.ltp used, get_ltp not called", got, 100.1)
    AE.get_ltp = lambda s: 100.2
    try:
        AE.enter_trade(dict(SIG))
        got = None
    except Reached as r:
        got = float(str(r))
    check("no signal.ltp: Kite price used", got, 100.2)
    check("  entry_ref (100.0) is never the gated price", got != SIG["entry_ref"], True)
finally:
    breaker.is_halted = saved["halt"]; CFG.ENABLE_EXIT_MANAGEMENT = saved["em"]
    RB.is_blocked = saved["rb"]; SB.is_blocked = saved["sb"]
    AE.get_market_context = saved["ctx"]; AE.regime_allows_side = saved["reg"]
    AE.get_open_position = saved["pos"]; AE.get_ltp = saved["ltp"]
    RB.stock_check = saved["chk"]; AE._reserve_intent = saved["res"]
    AE.place_order = saved["po"]

# ── :628-634 — unreadable order state is UNKNOWN ──────────────────────
print("\nUNREADABLE ORDER STATE IS UNKNOWN (was :628-634)")
saved_c, saved_s = BR.cancel_order, BR.get_order_status
try:
    def settle(cancel_ok, status):
        BR.cancel_order = lambda oid: cancel_ok
        BR.get_order_status = lambda oid: status
        return AE._settle_unfilled_limit("OID-9", "TESTSYM")["outcome"]

    check("status unreadable ({}) after a successful cancel", settle(True, {}), "UNKNOWN")
    check("status None after a successful cancel", settle(True, None), "UNKNOWN")
    check("CANCELLED but filled_quantity missing", settle(True, {"status": "CANCELLED"}), "UNKNOWN")
    check("CANCELLED but filled_quantity garbage",
          settle(True, {"status": "CANCELLED", "filled_quantity": "x"}), "UNKNOWN")
    check("filled 5 but average_price missing",
          settle(True, {"status": "CANCELLED", "filled_quantity": 5}), "UNKNOWN")
    check("filled 5 at 0.0", settle(True, {"status": "CANCELLED", "filled_quantity": 5,
                                            "average_price": 0}), "UNKNOWN")
    check("status string blank, 0 filled, cancel ok -> UNKNOWN (was NO_FILL)",
          settle(True, {"status": "", "filled_quantity": 0}), "UNKNOWN")
    check("status string missing, 0 filled, cancel ok -> UNKNOWN",
          settle(True, {"filled_quantity": 0}), "UNKNOWN")
    check("readable: CANCELLED, 0 filled -> NO_FILL",
          settle(True, {"status": "CANCELLED", "filled_quantity": 0}), "NO_FILL")
    check("readable: filled 5 @ 101.2 -> FILLED",
          settle(False, {"status": "OPEN", "filled_quantity": 5, "average_price": 101.2}), "FILLED")
    check("readable: OPEN, 0 filled, cancel failed -> UNKNOWN",
          settle(False, {"status": "OPEN", "filled_quantity": 0}), "UNKNOWN")
    check("each unreadable case recorded", len([e for e in events()
          if e["code"] == "ORDER_STATE_UNREADABLE"]) >= 1, True)
finally:
    BR.cancel_order, BR.get_order_status = saved_c, saved_s

# ── :466 — unreadable count raises ────────────────────────────────────
print("\nAN UNREADABLE COUNT RAISES (was :466)")


class R:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)

    def json(self):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b


saved_g = AE.requests.get
try:
    for label, fn in (("HTTP 503", lambda *a, **k: R(503, "down")),
                      ("network error", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))),
                      ("garbage body", lambda *a, **k: R(200, ValueError("bad json")))):
        AE.requests.get = fn
        try:
            AE.get_today_entry_count()
            got = "returned a number"
        except RiskDataUnavailable:
            got = "raised"
        check(f"{label}", got, "raised")
    AE.requests.get = lambda *a, **k: R(200, [{"id": 1}, {"id": 2}])
    check("readable: the count", AE.get_today_entry_count(), 2)
finally:
    AE.requests.get = saved_g

print("\nFAIL-OPEN PATHS ARE NOW COUNTED (behaviour unchanged)")
import atlas.reporting.directives as DIR                       # noqa: E402
OE._seen.clear()
bad = Path(tempfile.mkdtemp()) / "block.json"
bad.write_text("{not json")
_saved_bf = SB.BLOCK_FILE
SB.BLOCK_FILE = bad
try:
    check("unreadable symbol-block file still blocks nothing", SB.is_blocked("ANY")[0], False)
finally:
    SB.BLOCK_FILE = _saved_bf
check("  and is recorded as CHECK_SKIPPED",
      any(e["code"] == "SYMBOL_BLOCK_UNREADABLE" and e["event"] == "CHECK_SKIPPED"
          for e in events()), True)
check("an unknown mode still reads NORMAL", DIR.normalise_mode("PAUSE"), "NORMAL")
check("  and is recorded as SUBSTITUTED",
      any(e["code"] == "MODE_DEFAULTED" for e in events()), True)
check("a known mode is not recorded", DIR.normalise_mode("PAUSED"), "PAUSED")
check("  (one MODE_DEFAULTED row, not two)",
      len([e for e in events() if e["code"] == "MODE_DEFAULTED"]), 1)

print("\nA SIGNAL WITH NO DIRECTION, AND AN ESTIMATED MARGIN, ARE RECORDED")
src = Path(ROOT / "atlas/execution/atlas_entry.py").read_text()
check("DIRECTION_DEFAULTED recorded where LONG is assumed",
      'if not signal.get("direction"):' in src and "DIRECTION_DEFAULTED" in src, True)
check("MARGIN_ESTIMATED recorded on the estimate path",
      src.index("MARGIN_ESTIMATED") < src.index("using ESTIMATED margin"), True)

print("\nNOTHING WAS POSTED ANYWHERE REAL")
check("every event went to the stub poster", all(isinstance(p, dict) for p in posted), True)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — no price no order; unreadable is UNKNOWN; a read failure is never a number")
