#!/usr/bin/env python3
"""
THE BOX'S DISK, WATCHED. A full root filesystem stops every job at once.
=======================================================================

On 2026-11-04 the box had 902 MB free of 6.7 GB and nothing alarmed on it.
Most of the fall from ~1.3 GB was one-off: 191.5 MB of apt packages fetched
by unattended-upgrades on 28 Oct and a 56 MB backup from 23 Oct. But the
next one-off lands just as silently, and when the disk fills, the bhavcopy
download, the parquet rewrites, the logs and the observation ledger's callers
all fail together.

THE THRESHOLD, 500 MB. The largest single-day consumption found on the box
is 191.5 MB (the apt download, 28 Oct). 500 MB leaves more than two days of
that worst day after the alarm fires -- time to act, not to read about it.

EVERY RUN WRITES A HEARTBEAT LINE (reports/disk_alarm.log), alarm or not, so
"the disk was fine" is distinguishable from "the check never ran".

REPEATS ARE HELD 12 HOURS while the disk stays low: an alarm every hour is
one people mute. The held count goes into the next message.

    python -m engine.disk_alarm            # the scheduled run
    python -m engine.disk_alarm --prove    # send once, PROOF banner, real numbers
"""
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("disk-alarm")
IST = timezone(timedelta(hours=5, minutes=30))

THRESHOLD_MB = 500
HOLD_HOURS = 12
PATH = "/"
STATE = Path(os.environ.get("TSL_DISK_ALARM_STATE",
                            ROOT / "data" / "reference" / "disk_alarm.json"))


def free_mb(path=PATH) -> float:
    return shutil.disk_usage(path).free / 1048576


def run(prove: bool = False, *, sender=None, free=None, now=None) -> int:
    """0 if the disk is above the threshold (or the proof was accepted)."""
    from engine import ledger_alerts
    sender = sender or ledger_alerts.send
    f = free_mb() if free is None else free
    now = now or time.time()
    total = shutil.disk_usage(PATH).total / 1048576
    log.info(f"heartbeat: {f:.0f} MB free of {total:.0f} MB on {PATH} "
             f"(alarm below {THRESHOLD_MB} MB)")
    low = f < THRESHOLD_MB
    if not low and not prove:
        return 0
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {"last_sent": 0, "held": 0}
    if low and not prove and now - st.get("last_sent", 0) < HOLD_HOURS * 3600:
        st["held"] = st.get("held", 0) + 1
        _save(st)
        log.error(f"disk low ({f:.0f} MB) — alarm held ({st['held']} since the last one)")
        return 1
    msg = (f"💽 <b>BOX DISK {'LOW' if low else 'CHECK'} — {f:.0f} MB free</b> of "
           f"{total:.0f} MB on {PATH}\nAlarm threshold: {THRESHOLD_MB} MB. When the "
           f"root filesystem fills, the bhavcopy chain, the parquet rewrites, the "
           f"logs and every publisher fail together.")
    if st.get("held"):
        msg += f"\n{st['held']} further low reading(s) were held since the last alarm."
    if prove:
        msg = ("🧪 <b>PROOF — not a real event.</b> This is the disk alarm, fired on "
               "purpose to prove it reaches you. The numbers are real.\n\n" + msg)
    ok, mid, cid = sender(msg)
    log.info(f"alarm {'accepted' if ok else 'NOT ACCEPTED'}: message_id={mid} chat.id={cid}")
    if ok and not prove:
        _save({"last_sent": now, "held": 0})
    return 0 if (ok and (prove or not low)) else 1


def _save(st) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st))
    except Exception as e:
        log.error(f"disk alarm state not written: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run(prove="--prove" in sys.argv))
