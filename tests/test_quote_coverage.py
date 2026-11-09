#!/usr/bin/env python3
"""UPSTOX QUOTE COVERAGE: a dropped key alarms by name, a known gap does not,
zero quotes is the loudest case, a repeat is held, a healed exception is
reported, and a run is always recorded. Nothing reaches Telegram or Upstox."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TSL_QUOTE_COVERAGE_DIR"] = tempfile.mkdtemp()

from engine import quote_coverage as Q                          # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<66} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


class Box:
    def __init__(self, mid=7):
        self.msgs, self.mid = [], mid

    def __call__(self, m):
        self.msgs.append(m)
        return self.mid


# Real exception names, synthetic everything else.
KEYS = {"AAA": "NSE_EQ|INE000A01011", "BBB": "NSE_EQ|INE000B01011",
        "CCC": "NSE_EQ|INE000C01011",
        "JSWDULUX": "NSE_EQ|INE133A01011",            # known missing
        "ICICIPRULI": "NSE_EQ|INE726G01019"}          # known renamed


def quotes(drop=(), rename=None):
    out = {}
    for s, k in KEYS.items():
        if s in drop or s == "JSWDULUX":
            continue
        sym = "ICICILIFE" if s == "ICICIPRULI" else s
        if rename and s in rename:
            sym = rename[s]
        out[f"NSE_EQ:{sym}"] = {"instrument_token": k, "symbol": sym, "last_price": 1.0}
    return out


T = datetime(2026, 11, 10, 9, 0, tzinfo=Q.IST)
want_expected = len(KEYS) - 1                    # derived: one known-missing key

print("\nTODAY'S NORMAL STATE")
b = Box()
r = Q.check(KEYS, quotes(), "morning", sender=b, now=T)
check("expected = requested minus known-missing", r["expected_returned"], want_expected)
check("returned == expected", r["returned"], want_expected)
check("no alarm on the known gap and the known rename", len(b.msgs), 0)
check("the run is recorded", len(json.loads(Q._state_path("2026-11-10").read_text())["runs"]), 1)

print("\nTHE NEXT DROP")
b = Box()
r = Q.check(KEYS, quotes(drop={"BBB"}), "midday", sender=b, now=T)
check("one alarm", len(b.msgs), 1)
check("  naming the dropped symbol", "MISSING BBB" in b.msgs[0], True)
check("  and not the known one", "JSWDULUX" in b.msgs[0], False)
b2 = Box()
Q.check(KEYS, quotes(drop={"BBB"}), "afternoon", sender=b2, now=T)
check("the same drop later that session is held", len(b2.msgs), 0)
b3 = Box()
Q.check(KEYS, quotes(drop={"BBB", "CCC"}), "close", sender=b3, now=T)
check("a NEW drop alarms at once", len(b3.msgs), 1)
check("  with only the new symbol", ("MISSING CCC" in b3.msgs[0], "MISSING BBB" in b3.msgs[0]), (True, False))

print("\nA COUNT CHECK WOULD MISS THIS")
T2 = datetime(2026, 11, 11, 9, 0, tzinfo=Q.IST)
healed = quotes(drop={"BBB"})
healed["NSE_EQ:JSWDULUX"] = {"instrument_token": KEYS["JSWDULUX"], "symbol": "JSWDULUX"}
b = Box()
r = Q.check(KEYS, healed, "morning", sender=b, now=T2)
check("count equals the expected count", r["returned"], want_expected)
check("  yet the drop is named", "MISSING BBB" in b.msgs[0], True)
check("  and the healed exception reported stale", "STALE EXCEPTION JSWDULUX" in b.msgs[0], True)

print("\nZERO QUOTES")
T3 = datetime(2026, 11, 12, 9, 0, tzinfo=Q.IST)
b = Box()
Q.check(KEYS, {}, "morning", sender=b, now=T3)
check("an empty fetch alarms", len(b.msgs), 1)
check("  as the whole feed, not a list of every symbol", "ZERO quotes" in b.msgs[0] and "MISSING AAA" not in b.msgs[0], True)

print("\nAN UNEXPECTED RENAME")
T4 = datetime(2026, 11, 13, 9, 0, tzinfo=Q.IST)
b = Box()
Q.check(KEYS, quotes(rename={"AAA": "AAANEW"}), "morning", sender=b, now=T4)
check("alarms by name", "RENAMED AAA -&gt; AAANEW" in b.msgs[0], True)

print("\nA FAILED SEND IS NOT RECORDED AS ALARMED")
T5 = datetime(2026, 11, 14, 9, 0, tzinfo=Q.IST)
Q.check(KEYS, quotes(drop={"AAA"}), "morning", sender=Box(mid=None), now=T5)
b = Box()
Q.check(KEYS, quotes(drop={"AAA"}), "midday", sender=b, now=T5)
check("the next run tries again", len(b.msgs), 1)

print("\nTHE DAILY LINE")
check("a session with runs", Q.daily_line("2026-11-10").startswith("Upstox coverage 2026-11-10: 4 run(s)"), True)
check("a session with none says so", "NO RUN RECORDED" in Q.daily_line("2026-11-20"), True)

print("\nNEVER RAISES")
r = Q.check(None, None, "broken", sender=Box(), now=T5)
check("garbage in -> error dict, no exception", "error" in r, True)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the quote guard names the next drop and stays quiet on the known ones")
