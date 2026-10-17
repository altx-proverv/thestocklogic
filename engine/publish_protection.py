#!/usr/bin/env python3
"""
PUBLISH WHAT THE BROKER ACTUALLY HOLDS against each open position.
==================================================================

THE BANNER THIS EXISTS FOR. /atlas read "MANUAL RISK REQUIRED — 20 open
position(s). ATLAS does not place stop-losses" while all 20 had confirmed GTTs
resting at the broker. That is not a stale label, it is an instruction to do
something harmful: a second stop on shares that already have one means
whichever fires first leaves the other trying to sell stock that is gone.

READ BACK FROM THE BROKER, never from the recorded exit_legs column.
ATHERENERG's exit_legs was {} while two GTTs sat live -- the column is empty
exactly when placement failed to record, which is the case a banner most needs
to get right.

THREE STATES AND NO FOURTH. CONFIRMED means read back. NONE means the broker
answered and there is nothing there. UNKNOWN means the broker could not be
read -- and then broker_ok is false and the page reports NEITHER state rather
than inventing one.

A browser cannot reach the broker, so this is the usual one-way shape: this
job reads, writes a table, and the page can only SELECT from it.
"""

import os
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests                                              # noqa: E402

from atlas.config import SUPABASE_URL as _CFG_URL, SUPABASE_KEY as _CFG_KEY  # noqa: E402

log = logging.getLogger("publish-protection")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or _CFG_URL or "").rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or _CFG_KEY or "")

OPEN_STATUSES = ("OPEN", "GTT_PENDING")


def _headers(extra=None):
    h = {"apikey": SUPABASE_KEY,
         "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def open_positions() -> list:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades"
        f"?status=in.({','.join(OPEN_STATUSES)})"
        f"&select=id,symbol,direction,qty,product,entry_price",
        headers=_headers(), timeout=20)
    if r.status_code != 200:
        log.error(f"open positions read failed: HTTP {r.status_code} "
                  f"{r.text[:160]}")
        return []
    return r.json() or []


def run() -> int:
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    from atlas.execution.protection import broker_protection, confirm_legs

    rows = open_positions()
    if not rows:
        print("no open positions")
        return 0

    prot = broker_protection()
    now = datetime.now(IST).replace(microsecond=0).isoformat()

    payload = []
    for t in rows:
        legs = confirm_legs(t, prot)
        payload.append({
            "trade_id": t["id"], "symbol": t["symbol"],
            "qty": t.get("qty"), "product": t.get("product"),
            "stop_state": legs["stop"], "target_state": legs["target"],
            "stop_at": legs["stop_at"],
            "gtt_ids": ",".join(str(i) for i in legs["gtt_ids"]) or None,
            "detail": (legs["detail"] or prot.get("why") or "")[:500],
            "broker_ok": bool(prot.get("ok")),
            "checked_at": now,
        })

    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/position_protection",
            headers=_headers({"Prefer": "resolution=merge-duplicates"}),
            json=payload, timeout=30)
    except Exception as e:
        log.error(f"write failed: {type(e).__name__}: {e}")
        return 1
    if resp.status_code not in (200, 201, 204):
        log.error(f"write rejected: HTTP {resp.status_code} {resp.text[:200]}")
        return 1

    # ROWS FOR POSITIONS THAT ARE NO LONGER OPEN MUST GO, or a closed symbol
    # keeps reporting a stop that is not protecting anything.
    ids = ",".join(str(t["id"]) for t in rows)
    try:
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/position_protection?trade_id=not.in.({ids})",
            headers=_headers(), timeout=20)
    except Exception as e:
        log.warning(f"could not prune closed positions: {e}")

    conf = sum(1 for p in payload if p["stop_state"] == "CONFIRMED")
    naked = [p["symbol"] for p in payload if p["stop_state"] == "NONE"]
    if not prot.get("ok"):
        print(f"broker unreadable ({prot.get('why')}) — {len(payload)} "
              f"position(s) recorded as UNKNOWN")
    else:
        print(f"{conf} of {len(payload)} open position(s) protected at the "
              f"broker" + (f"; UNPROTECTED: {', '.join(naked)}" if naked else ""))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run())
