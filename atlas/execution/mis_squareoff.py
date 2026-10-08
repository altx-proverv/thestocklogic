#!/usr/bin/env python3
"""
ATLAS — MIS square-off, before the broker's cutoff
=================================================
Closes every MIS position before Zerodha's earliest cutoff. The time comes from
atlas/config.py (MIS_EXIT_TIME, currently 15:00, twelve minutes before the 15:12
CAS cutoff) -- never from a literal here or in the timer alone.

    python3 -m atlas.execution.mis_squareoff            # act
    python3 -m atlas.execution.mis_squareoff --dry-run  # report only

ITS OWN UNIT, NOT PART OF THE LOOP. The market-hours loop stops at 15:20 and a
crashed loop must not be able to strand a short into broker liquidation -- the one
job here is the one that has to happen even when the thing that opened the
position is dead. A timer that fires independently is the only arrangement where
that holds.

WHY EARLY, AND WHY 15:15 WAS NOT EARLY ENOUGH. The cutoff is an ORDER cutoff as
well as a liquidation time: after it, an MIS exit order is rejected. 15:15 sat
three minutes past the 15:12 that applies to every F&O-segment (CAS) stock, so
this job had never been able to close a position in that universe. On 2026-10-07
it did not: "Intraday orders (MIS) are allowed only till 3:12 PM". Past the
cutoff Zerodha squares off at market and we lose the exit price and the audit
trail -- the fill appears with no order of ours behind it, so the trade's own
record cannot say why it closed or at what. Margin keeps the decision ours.

IT RUNS EVEN WHEN ENABLE_EXIT_MANAGEMENT IS FALSE. That switch governs whether
ATLAS places stops; it has nothing to do with whether an intraday position must be
flat by the close. A short opened by hand, or one left over from a session where
exit management was off, still has to leave.
"""

from __future__ import annotations

import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import requests                                            # noqa: E402
from atlas.config import (SUPABASE_URL, SUPABASE_KEY, OPEN_STATUSES,     # noqa: E402
                          MIS_EXIT_TIME, MIS_BROKER_CUTOFF)

log = logging.getLogger("ATLAS-MIS-SQUAREOFF")
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def open_mis_positions() -> tuple:
    """
    (readable, rows). FAILS CLOSED-ISH: unreadable returns readable=False, and the
    caller alerts rather than concluding there is nothing to close. Concluding
    "nothing open" from a failed read is how a short gets left to the broker.
    """
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?status=in.({','.join(OPEN_STATUSES)})&product=eq.MIS"
            f"&select=id,symbol,direction,qty,product",
            headers=_headers(), timeout=20)
        if r.status_code != 200:
            log.error(f"open MIS read failed: HTTP {r.status_code} {r.text[:120]}")
            return False, []
        return True, r.json() or []
    except Exception as e:
        log.error(f"open MIS read failed: {e}")
        return False, []


def run(dry: bool = False, now_ist: datetime = None) -> int:
    from atlas.execution import exits as X

    now = now_ist or datetime.now(IST)
    try:
        sys.path.insert(0, str(ROOT / "engine"))
        from trading_calendar import is_trading_day
        if not is_trading_day(now.date()):
            log.info(f"{now.date()} is not an NSE trading day — nothing to do")
            return 0
    except Exception as e:
        # Reporting on a holiday is a nuisance; failing to close a real position
        # is not. Proceed.
        log.warning(f"trading calendar unreadable ({e}) — proceeding anyway")

    readable, rows = open_mis_positions()
    if not readable:
        _alert("MIS SQUAREOFF BLIND",
               f"Could not read open MIS positions before the "
               f"{MIS_BROKER_CUTOFF} broker cutoff. If a short is open it will "
               f"be liquidated at market with no order of ours behind it.")
        return 1
    if not rows:
        log.info("no open MIS positions")
        return 0

    log.info(f"{len(rows)} open MIS position(s) at {now:%H:%M} IST")
    if dry:
        for t in rows:
            log.info(f"  would exit {t['symbol']} {t['direction']} {t['qty']}")
        return 0

    out = X.squareoff_mis(rows, now_ist=now)
    if not out["due"]:
        log.info(f"not due yet at {now:%H:%M} IST (exits at {MIS_EXIT_TIME})")
        return 0
    log.info(f"exited {out['exited']}, failed {out['failed']}"
             f"{' — RAN LATE' if out.get('late') else ''}")
    for a in out.get("alerts", []):
        _alert("MIS SQUAREOFF LATE" if out.get("late") else
               "MIS SQUAREOFF FAILED", a)
    # A LATE RUN IS A FAILURE EVEN IF EVERY EXIT HAPPENED TO FILL. It means the
    # timer no longer matches the cutoff, which is a standing defect, and a
    # zero exit status here would retire the only signal of it.
    return 1 if (out["failed"] or out.get("late")) else 0


def _alert(kind: str, text: str) -> None:
    log.error(f"[{kind}] {text}")
    try:
        from atlas.reporting.telegram import send
        if not send(f"<b>ATLAS — {kind}</b>\n{text}"):
            log.error(f"ALERT NOT DELIVERED [{kind}] — Telegram failed or is "
                      f"unconfigured. This alert exists only in this log.")
    except Exception as e:
        log.error(f"ALERT NOT DELIVERED [{kind}] ({e})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sys.exit(run(dry=a.dry_run))
