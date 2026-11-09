#!/usr/bin/env python3
"""A TEST HELPER CAN NEVER DELETE OR OVERWRITE A PATH OUTSIDE THE TEMP DIR (D-060).

Each destructive test helper is aimed at a REAL file -- a sacrificial file in
the repo, created here and checked afterwards -- and must refuse, loudly, and
leave it untouched. A symlink in the temp dir pointing at that file must be
refused too. Nothing here can reach production: the only non-temp file it
creates is its own, and it is removed at the end.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TSL_ORDER_EVENTS_FILE"] = str(Path(tempfile.mkdtemp()) / "ev.jsonl")
import logging                                                  # noqa: E402
logging.basicConfig(level=logging.CRITICAL)

from atlas.disposable import require_disposable, is_disposable, NotDisposable  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<64} {str(got)[:20]:<22} {'ok' if ok else 'FAIL'}")


def refused(fn):
    try:
        fn()
        return False
    except NotDisposable:
        return True


SAC = ROOT / "tests" / "_sacrificial_d060.json"        # a real, non-temp file
SAC.write_text('{"keep": true}')
try:
    tmpd = Path(tempfile.mkdtemp())
    link = tmpd / "link.json"
    link.symlink_to(SAC)

    print("\nTHE GUARD")
    check("a temp file is disposable", is_disposable(tmpd / "x.json"), True)
    check("a repo file is not", is_disposable(SAC), False)
    check("the live tick map path is not", is_disposable("/var/lib/atlas/tick_map.json"), False)
    check("a temp-dir symlink to a repo file is not", is_disposable(link), False)
    check("require_disposable refuses the repo file",
          refused(lambda: require_disposable(SAC, "test")), True)

    print("\nTHE THREE HELPERS, AIMED AT A REAL FILE")
    import atlas.execution.tick_map as TM
    import atlas.execution.sentiment_window as SW
    import atlas.risk.symbol_block as SB
    TM._path = lambda: SAC
    check("tick_map.reset_for_tests refuses", refused(TM.reset_for_tests), True)
    check("sentiment_window.reset_for_tests refuses",
          refused(lambda: SW.reset_for_tests(SAC)), True)
    SB.BLOCK_FILE = SAC
    check("symbol_block.reset_for_tests refuses", refused(SB.reset_for_tests), True)
    TM._path = lambda: link
    check("tick_map via a temp symlink refuses", refused(TM.reset_for_tests), True)
    check("the real file survived all of it", SAC.read_text(), '{"keep": true}')

    print("\nAND A TEMP FILE IS STILL RESETTABLE")
    t = tmpd / "tick_map.json"
    t.write_text("{}")
    TM._path = lambda: t
    TM.reset_for_tests()
    check("tick_map.reset_for_tests removes a temp file", t.exists(), False)

    print("\nORDER EVENTS UNDER A DRILL NEVER POST")
    import atlas.execution.order_events as OE
    posted = []
    OE._post = lambda row: posted.append(row)
    OE._seen.clear()
    from atlas.risk.breaker import is_drill
    check("this script is a drill", is_drill()[0], True)
    OE.record("REFUSED", "test", "X", "CODE", "why")
    check("nothing was POSTed", posted, [])
finally:
    SAC.unlink(missing_ok=True)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — a test helper cannot touch a path outside the temp dir")
