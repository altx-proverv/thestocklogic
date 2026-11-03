#!/usr/bin/env python3
"""THE DISK ALARM: SILENT ABOVE 500 MB, ONE MESSAGE BELOW IT, HELD WHILE IT STAYS LOW.

Every send is captured; nothing here reaches Telegram.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TSL_DISK_ALARM_STATE"] = str(Path(tempfile.mkdtemp()) / "s.json")

from engine import disk_alarm as D                             # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<66} {str(got)[:16]:<18} {'ok' if ok else 'FAIL'}")


class Box:
    def __init__(self):
        self.msgs = []

    def __call__(self, m):
        self.msgs.append(m)
        return True, 1, 2


T = 1_800_000_000
D.STATE = Path(os.environ["TSL_DISK_ALARM_STATE"])
b = Box()
check("well above the threshold: no message", (D.run(sender=b, free=900, now=T), len(b.msgs)), (0, 0))
check("exactly at the threshold: no message",
      (D.run(sender=b, free=D.THRESHOLD_MB, now=T), len(b.msgs)), (0, 0))
check("below it: one message", (D.run(sender=b, free=D.THRESHOLD_MB - 1, now=T), len(b.msgs)), (1, 1))
check("  it says LOW and the real figure", "LOW — 499 MB free" in b.msgs[-1], True)
check("still low an hour later: held, not resent",
      (D.run(sender=b, free=480, now=T + 3600), len(b.msgs)), (1, 1))
check("after the hold: sent again, with the held count",
      (D.run(sender=b, free=470, now=T + D.HOLD_HOURS * 3600 + 1), len(b.msgs)), (1, 2))
check("  naming how many were held", "1 further low reading(s) were held" in b.msgs[-1], True)
p = Box()
check("--prove sends even when the disk is fine", (D.run(True, sender=p, free=900, now=T), len(p.msgs)),
      (0, 1))
check("  under a PROOF banner", p.msgs[0].startswith("🧪 <b>PROOF"), True)
check("  and it does not start a hold", (D.run(sender=b, free=490, now=T + D.HOLD_HOURS * 3600 * 3),
                                          len(b.msgs)), (1, 3))
check("the threshold is the 500 MB the evidence supports", D.THRESHOLD_MB, 500)

print()
if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the disk alarm is silent above 500 MB and speaks once below it")
