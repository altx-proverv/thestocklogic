#!/usr/bin/env python3
"""THE 15:20 CLOSE MESSAGE MUST NOT CRASH ON A DAY WITH AN ENTRY (audit D-T1A-6).

state.entered holds dicts; serve() joined them as strings and raised TypeError
at the close, after state.write() and before the ENGINE DOWN message was sent.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging                                                  # noqa: E402
logging.disable(logging.CRITICAL)
from atlas.signal import market_open as M                      # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<60} {str(got)[:30]:<32} {'ok' if ok else 'FAIL'}")


print("\nTHE CLOSE MESSAGE'S ENTRY LIST")
e = {"symbol": "ABB", "direction": "LONG", "at": "10:01:02", "entry": 5000.0, "qty": 2}
check("no entries", M.entered_lines([]), "no entries")
check("a dict entry formats, does not raise", M.entered_lines([e]), "ABB LONG 2 @ 5000.0 (10:01:02)")
check("capped at ten", M.entered_lines([e] * 12).count("\n"), 9)
# the shape the cycle actually appends, read from the source so a change to it is seen
src = (ROOT / "atlas/signal/market_open.py").read_text()
check("cycle() still appends dicts to state.entered", "state.entered.append({" in src, True)
check("serve() uses entered_lines, not a raw join", "entered_lines(state.entered)" in src
      and '"\\n".join(state.entered' not in src, True)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the close message formats entries instead of raising")
