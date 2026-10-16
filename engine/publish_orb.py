#!/usr/bin/env python3
"""
PUBLISH OPENING-RANGE BREAKS FOR THE PUBLIC TAPE. One-way.
==========================================================

Reads ATLAS's range breaker through atlas/execution/breaker_public.py -- the
read-only accessor, which imports nothing from ATLAS -- and upserts into
public.orb_breaks, which the browser can only SELECT.

THE DIRECTION OF THIS ARROW IS THE WHOLE POINT:

    breaker state files  --(read)-->  breaker_public  --(read)-->  this job
                                                                      |
                                                                 (write)
                                                                      v
                                                             public.orb_breaks
                                                                      |
                                                                 (SELECT only)
                                                                      v
                                                                 the browser

There is no arrow back. This module must never import range_breaker,
sentiment_window, atlas.risk.breaker or atlas.risk.symbol_block, and
tests/test_page_cannot_write_breaker.py fails if it ever does -- checked by
walking the import graph, not by grepping for a name.

Run every 5 minutes through the session. Idempotent: the primary key is
(session_date, symbol), so re-publishing a break updates it rather than
duplicating the tape.
"""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

# THE BOOTSTRAP IS INLINE, deliberately. cron invokes this BY FILE PATH, so
# the repo root is not on sys.path and `from atlas...` cannot resolve without
# it -- and tests/test_crontab.py only recognises the bootstrap when
# parents[1] appears in the insert call itself, which is the convention every
# other file-path script here follows.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests                                              # noqa: E402

from atlas.execution.breaker_public import breaks, state     # noqa: E402

log = logging.getLogger("publish-orb")

IST = timezone(timedelta(hours=5, minutes=30))
# THE BOX DOES NOT SET SUPABASE_URL. /etc/atlas.env carries the service key
# and not the URL, which is why every other module takes it from the project
# config rather than the environment. Taking it from os.environ gave
# "Invalid URL '/rest/v1/index_quotes': No scheme supplied" on the first run.
from atlas.config import SUPABASE_URL as _CFG_URL, SUPABASE_KEY as _CFG_KEY

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or _CFG_URL or "").rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or _CFG_KEY or "")


def _headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates"}


def _change_pct(symbols: list) -> dict:
    """change_pct per symbol from live_prices, for the tape's figure."""
    if not symbols:
        return {}
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/live_prices"
                         f"?select=symbol,change_pct&limit=2000",
                         headers=_headers(), timeout=20)
        if r.status_code != 200:
            log.warning(f"live_prices read failed: HTTP {r.status_code}")
            return {}
        return {x["symbol"]: x.get("change_pct") for x in (r.json() or [])}
    except Exception as e:
        log.warning(f"live_prices read failed: {e}")
        return {}


def run() -> int:
    st = state()
    rows = [b for b in breaks() if b["scope"] == "symbol" and b["symbol"]]
    log.info(f"breaker session {st['session_date']}: {len(rows)} symbol "
             f"break(s), {st['ranges_held']} range(s) held")
    if not rows:
        print(f"no opening-range breaks to publish "
              f"(session {st['session_date']})")
        return 0

    chg = _change_pct([r["symbol"] for r in rows])
    payload = [{
        "session_date": st["session_date"],
        "symbol": r["symbol"],
        "side": r["side"] or "",
        "range_low": r["range_low"],
        "range_high": r["range_high"],
        "break_close": r["break_close"],
        "break_minute": r["break_minute"],
        "change_pct": chg.get(r["symbol"]),
        "detected_at": r["at"] or datetime.now(IST).isoformat(),
    } for r in rows]

    try:
        resp = requests.post(f"{SUPABASE_URL}/rest/v1/orb_breaks",
                             headers=_headers(), json=payload, timeout=30)
    except Exception as e:
        log.error(f"orb_breaks write failed: {type(e).__name__}: {e}")
        return 1
    if resp.status_code not in (200, 201, 204):
        log.error(f"orb_breaks write rejected: HTTP {resp.status_code} "
                  f"{resp.text[:200]}")
        return 1
    print(f"published {len(payload)} opening-range break(s) for "
          f"{st['session_date']}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run())
