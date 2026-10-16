#!/usr/bin/env python3
"""
THE SIX INDEX TILES. Nothing wrote them before this.
====================================================

public.live_prices holds 747 equities and not one index: NIFTY 50, NIFTY BANK,
NIFTY MIDCAP 100, NIFTY SMLCAP 100, INDIA VIX and USDINR were all absent, so
the home page's index strip had no source at all.

EXCHANGE NAMES, NOT COLLOQUIAL ONES. "BANK NIFTY" and "NIFTY SMALLCAP 100" do
not exist at the exchange -- the instruments are NIFTY BANK and
NIFTY SMLCAP 100. The page labels what it actually read.

USDINR IS RESOLVED, NEVER PINNED. There is no spot USDINR instrument: the
nearest tradeable thing is a currency future on the CDS segment, and it
EXPIRES. Pinning token 328707 would have shown a stale October contract from
29 October onwards and then a dead one. So the near-month is looked up in the
instrument master on every run, the expiry is stored, and the page labels the
tile as a FUTURE because it carries basis and is not the spot rate.

Writes public.index_quotes, which the browser can only SELECT.
"""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

# THE BOOTSTRAP IS INLINE, deliberately. cron invokes this BY FILE PATH, so
# the repo root is not on sys.path and `from atlas...` cannot resolve without
# it -- and tests/test_crontab.py only recognises the bootstrap when
# parents[1] appears in the insert call itself, which is the convention every
# other file-path script here follows.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests                                              # noqa: E402

log = logging.getLogger("index-quotes")

IST = timezone(timedelta(hours=5, minutes=30))
# THE BOX DOES NOT SET SUPABASE_URL. /etc/atlas.env carries the service key
# and not the URL, which is why every other module takes it from the project
# config rather than the environment. Taking it from os.environ gave
# "Invalid URL '/rest/v1/index_quotes': No scheme supplied" on the first run.
from atlas.config import SUPABASE_URL as _CFG_URL, SUPABASE_KEY as _CFG_KEY

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or _CFG_URL or "").rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or _CFG_KEY or "")

# The five cash indices, by their REAL exchange tradingsymbol. The token is
# recorded for provenance; the lookup is by name so a token change cannot
# silently point a tile at the wrong index.
INDICES = [
    ("NIFTY50",     "NIFTY 50",         "NIFTY 50",         256265),
    ("NIFTYBANK",   "NIFTY BANK",       "NIFTY BANK",       260105),
    ("NIFTYMIDCAP", "NIFTY MIDCAP 100", "NIFTY MIDCAP 100", 256777),
    ("NIFTYSMLCAP", "NIFTY SMLCAP 100", "NIFTY SMLCAP 100", 267017),
    ("INDIAVIX",    "INDIA VIX",        "INDIA VIX",        264969),
]


def _headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates"}


def near_month_usdinr(kite) -> dict:
    """The nearest UNEXPIRED USDINR future on CDS. {} if none resolvable.

    Weekly and monthly contracts both exist, so this takes the nearest expiry
    that has not passed rather than assuming a monthly.
    """
    today = datetime.now(IST).date()
    try:
        rows = kite.instruments("CDS") or []
    except Exception as e:
        log.error(f"CDS instrument master unreadable: {type(e).__name__}: {e}")
        return {}
    live = []
    for r in rows:
        if r.get("name") != "USDINR" or r.get("instrument_type") != "FUT":
            continue
        exp = r.get("expiry")
        if isinstance(exp, datetime):
            exp = exp.date()
        if not isinstance(exp, date) or exp < today:
            continue
        live.append((exp, r))
    if not live:
        log.error("no unexpired USDINR future found on CDS")
        return {}
    live.sort(key=lambda t: t[0])
    exp, r = live[0]
    return {"tradingsymbol": r["tradingsymbol"], "expiry": exp,
            "token": r["instrument_token"], "exchange": "CDS"}


def run() -> int:
    from atlas.execution.broker import get_kite
    kite = get_kite()
    if kite is None:
        log.error("no broker session — index tiles not updated")
        return 1

    payload, missing = [], []

    keys = [f"NSE:{name}" for _, name, _, _ in INDICES]
    usd = near_month_usdinr(kite)
    if usd:
        keys.append(f"{usd['exchange']}:{usd['tradingsymbol']}")

    try:
        q = kite.quote(keys) or {}
    except Exception as e:
        log.error(f"quote failed: {type(e).__name__}: {e}")
        return 1

    def _row(symbol, display, instrument, exchange, key, is_future=False,
             expiry=None):
        row = q.get(key) or {}
        ltp = row.get("last_price")
        prev = ((row.get("ohlc") or {}).get("close"))
        if ltp is None:
            missing.append(display)
            return None
        chg = (None if not prev else
               round((float(ltp) - float(prev)) / float(prev) * 100.0, 2))
        return {"symbol": symbol, "display_name": display,
                "instrument": instrument, "exchange": exchange,
                "ltp": float(ltp),
                "prev_close": float(prev) if prev else None,
                "change_pct": chg, "is_future": is_future,
                "expiry": expiry.isoformat() if expiry else None,
                "updated_at": datetime.now(IST).isoformat()}

    for sym, name, instrument, _tok in INDICES:
        r = _row(sym, name, instrument, "NSE", f"NSE:{name}")
        if r:
            payload.append(r)

    if usd:
        # LABELLED AS A FUTURE, deliberately. It is not the spot rate.
        r = _row("USDINR", "USDINR FUT", usd["tradingsymbol"], "CDS",
                 f"CDS:{usd['tradingsymbol']}", is_future=True,
                 expiry=usd["expiry"])
        if r:
            payload.append(r)
    else:
        missing.append("USDINR FUT")

    if missing:
        # NAMED, NOT SWALLOWED. A tile with no data must be visibly absent
        # rather than quietly carrying yesterday's number.
        log.error(f"no quote for: {', '.join(missing)} — those tiles keep "
                  f"their previous row and will read stale on the page")

    if not payload:
        log.error("nothing to write")
        return 1

    try:
        resp = requests.post(f"{SUPABASE_URL}/rest/v1/index_quotes",
                             headers=_headers(), json=payload, timeout=30)
    except Exception as e:
        log.error(f"index_quotes write failed: {type(e).__name__}: {e}")
        return 1
    if resp.status_code not in (200, 201, 204):
        log.error(f"index_quotes write rejected: HTTP {resp.status_code} "
                  f"{resp.text[:200]}")
        return 1

    for p in payload:
        print(f"  {p['display_name']:<18} {p['ltp']:>12,.2f}  "
              f"{(f'{p[chr(99)+chr(104)+chr(97)+chr(110)+chr(103)+chr(101)+chr(95)+chr(112)+chr(99)+chr(116)]:+.2f}%' if p['change_pct'] is not None else '—'):>8}"
              + (f"  [FUT exp {p['expiry']}]" if p["is_future"] else ""))
    print(f"wrote {len(payload)} index row(s)"
          + (f"; MISSING {', '.join(missing)}" if missing else ""))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run())
