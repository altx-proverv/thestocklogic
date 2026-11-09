#!/usr/bin/env python3
"""tools/check_tick_map.py BITES: each way the real map can be wrong is planted
in a temp file and must be reported. Never reads the production file."""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging                                                  # noqa: E402
logging.basicConfig(level=logging.CRITICAL)
from tools import check_tick_map as C                           # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:20]:<22} {'ok' if ok else 'FAIL'}")


NOW = datetime(2026, 11, 11, 11, 0, tzinfo=C.IST)      # Wednesday, after the refresh
# (2026-11-10 is an NSE holiday, Diwali-Balipratipada -- used below.)
SYMS = ["AAA", "BBB", "CCC"]


def write(d):
    p = Path(tempfile.mkdtemp()) / "tick_map.json"
    p.write_text(json.dumps(d) if not isinstance(d, str) else d)
    return p


GOOD = {"days": {"2026-11-11": {"AAA": 0.05, "BBB": 0.1}}, "last_known": {"CCC": 0.01}}
check("a sane map passes", C.check(write(GOOD), NOW, SYMS)["ok"], True)
check("missing file fails", C.check(Path("/nonexistent/x.json"), NOW, SYMS)["ok"], False)
check("unparseable file fails", C.check(write("{not json"), NOW, SYMS)["ok"], False)
check("wrong shape fails", C.check(write({"ticks": {}}), NOW, SYMS)["ok"], False)
stale = {"days": {"2026-11-06": {"AAA": 0.05, "BBB": 0.1}}, "last_known": {"CCC": 0.01}}
r = C.check(write(stale), NOW, SYMS)
check("no map for today after the refresh fails", r["ok"], False)
check("  and names the day it wanted", "2026-11-11" in " ".join(r["problems"]), True)
early = datetime(2026, 11, 11, 8, 0, tzinfo=C.IST)
prev = {"days": {"2026-11-09": {"AAA": 0.05, "BBB": 0.1}}, "last_known": {"CCC": 0.01}}
check("before the refresh, the last session's map (9th; the 10th is a holiday)",
      C.check(write(prev), early, SYMS)["ok"], True)
gap = {"days": {"2026-11-11": {"AAA": 0.05}}, "last_known": {}}
r = C.check(write(gap), NOW, SYMS)
check("a universe symbol with no tick fails", r["ok"], False)
check("  and names it", "BBB" in " ".join(r["problems"]), True)
odd = {"days": {"2026-11-11": {"AAA": 0.05, "BBB": 0.07}}, "last_known": {"CCC": 0.01}}
check("a tick outside the six the master uses fails", C.check(write(odd), NOW, SYMS)["ok"], False)
zero = {"days": {"2026-11-11": {"AAA": 0.05, "BBB": 0}}, "last_known": {"CCC": 0.01}}
check("a zero tick fails", C.check(write(zero), NOW, SYMS)["ok"], False)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the tick-map check reports every planted fault")
