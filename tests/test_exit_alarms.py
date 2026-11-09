#!/usr/bin/env python3
"""EXIT ALARMS FIRE UNTIL A HUMAN CLEARS THEM (OPEN D-058).

Raised by the exit-side conditions; sent on every run of the night job; cleared
only by /ack; an unreadable register is said, not taken as "none". Every path
is a temp file; nothing reaches Telegram, Kite or Supabase.
"""
import os
import sys
import json
import tempfile
import logging
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
T = Path(tempfile.mkdtemp())
os.environ["ATLAS_EXIT_ALARM_FILE"] = str(T / "exit_alarms.json")
os.environ["ATLAS_UNPROTECTED_FILE"] = str(T / "unprotected.json")
os.environ["TSL_ORDER_EVENTS_FILE"] = str(T / "ev.jsonl")
logging.basicConfig(level=logging.CRITICAL)

import atlas.execution.exit_alarms as EA                        # noqa: E402
import atlas.reporting.directives as DIR                        # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:20]:<22} {'ok' if ok else 'FAIL'}")


class Box:
    def __init__(self, mid=99):
        self.msgs, self.mid = [], mid

    def __call__(self, m):
        self.msgs.append(m)
        return self.mid


print("\nRAISED, THEN SENT ON EVERY RUN")
check("nothing outstanding at first", EA.outstanding(), [])
b = Box()
check("an empty register sends nothing", (EA.run(sender=b), len(b.msgs)), (0, 0))
check("daily line: none outstanding", EA.daily_line(), "Exit alarms: none outstanding")
EA.raise_alarm("unprotected:41", "UNPROTECTED", "TESTSYM", "no stop at the broker", 41)
EA.raise_alarm("unprotected:41", "UNPROTECTED", "TESTSYM", "still no stop", 41)
check("raising twice is one alarm", len(EA.outstanding()), 1)
for i in range(3):
    EA.run(sender=b)
check("three runs, three messages -- it keeps firing", len(b.msgs), 3)
check("  each names the alarm id", all("unprotected:41" in m for m in b.msgs), True)
check("  and says how to clear it", "/ack" in b.msgs[0], True)
check("  sent count recorded", EA.outstanding()[0]["sent"], 3)
check("daily line counts it", EA.daily_line().startswith("Exit alarms: 1 OUTSTANDING"), True)

print("\nONLY A HUMAN CLEARS IT")
r = DIR.handle_directive("/ack nonsense-id")
check("/ack of an unknown id clears nothing", len(EA.outstanding()), 1)
check("  and says so", "Nothing matched" in r, True)
r = DIR.handle_directive("/ack UNPROTECTED:41")          # lowercased by the handler
check("/ack <id> (any case) clears it", len(EA.outstanding()), 0)
check("  and confirms", "Cleared" in r, True)
b2 = Box()
check("after the ack the night job sends nothing", (EA.run(sender=b2), len(b2.msgs)), (0, 0))
d = json.loads((T / "exit_alarms.json").read_text())
check("the cleared alarm is kept as history", d["unprotected:41"]["cleared_by"].startswith("telegram"), True)

print("\nAN ID WITH A SYMBOL IN IT (uppercase) CLEARS FROM TELEGRAM")
EA.raise_alarm("orphan:ZZZ:2026-11-11", "ORPHAN_UNCHECKED", "ZZZ", "triggers unchecked")
DIR.handle_directive("/ack orphan:ZZZ:2026-11-11")       # arrives lowercased
check("/ack of an uppercase-symbol id clears it", len(EA.outstanding()), 0)

print("\n/ack all, and a re-raise reopens")
EA.raise_alarm("fill:7", "FILL_UNREADABLE", "AAA", "fill unreadable", 7)
EA.raise_alarm("orphan:BBB:2026-11-11", "ORPHAN_UNCHECKED", "BBB", "triggers unchecked")
check("two outstanding", len(EA.outstanding()), 2)
DIR.handle_directive("/ack all")
check("/ack all clears both", len(EA.outstanding()), 0)
EA.raise_alarm("fill:7", "FILL_UNREADABLE", "AAA", "fill unreadable again", 7)
check("a cleared key raised again is open again", len(EA.outstanding()), 1)
DIR.handle_directive("/ack all")

print("\nIF NOBODY CLEARS IT FOR A DAY")
EA.raise_alarm("unprotected:9", "UNPROTECTED", "CCC", "no stop")
d = json.loads((T / "exit_alarms.json").read_text())
d["unprotected:9"]["raised_at"] = (EA._now() - timedelta(hours=30)).isoformat(timespec="seconds")
(T / "exit_alarms.json").write_text(json.dumps(d))
b3 = Box()
EA.run(sender=b3)
check("after 24h the message is headed UNACKNOWLEDGED", "UNACKNOWLEDGED FOR 24h+" in b3.msgs[0], True)
DIR.handle_directive("/ack all")

print("\nAN UNREADABLE REGISTER IS NOT 'NONE'")
(T / "exit_alarms.json").write_text("{not json")
b4 = Box()
check("run() exits 1", EA.run(sender=b4), 1)
check("  and alarms that the register is unreadable", "UNREADABLE" in b4.msgs[0], True)
check("daily line says UNREADABLE", "UNREADABLE" in EA.daily_line(), True)
check("/alarms says UNKNOWN", "could not be read" in DIR.handle_directive("/alarms"), True)
(T / "exit_alarms.json").write_text("{}")

print("\nA FAILED SEND IS NOT COUNTED AS SENT")
EA.raise_alarm("unprotected:5", "UNPROTECTED", "DDD", "no stop")
check("send not accepted -> exit 1", EA.run(sender=Box(mid=None)), 1)
check("  sent count unchanged", EA.outstanding()[0]["sent"], 0)
DIR.handle_directive("/ack all")

print("\nTHE SOURCES RAISE IT")
import atlas.execution.protection as PR                         # noqa: E402
PR.UNPROTECTED_FILE = T / "unprotected.json"
# A SHORT (MIS): since 2026-11-10 a long-book position is never registered as
# unprotected (atlas/books.py); a book that places stops still is.
PR.register({"id": 77, "symbol": "EEE", "direction": "SHORT", "product": "MIS", "qty": 5,
             "entry_price": 100.0, "stop_price": 105.0}, "handover failed", wanted_stop=105.0)
check("protection.register raises an UNPROTECTED alarm",
      [a["id"] for a in EA.outstanding()], ["unprotected:77"])

print("\nA DRILL NEVER WRITES THE LIVE REGISTER")
os.environ["ATLAS_EXIT_ALARM_FILE"] = EA.LIVE_PATH
check("raising against the live path in a test is refused", EA._save({"x": {}}), False)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — exit alarms fire on every run until a human clears them")
