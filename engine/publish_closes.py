#!/usr/bin/env python3
"""
PUBLISH THE LAST SESSION'S CLOSE-TO-CLOSE, for the whole universe.
==================================================================

WHY THE PAGE NEEDS THIS. Out of hours the home page must show the last
completed session, not a grey grid. The intraday websocket reaches ~498
symbols and writes nothing after the close; the nightly chain reaches all 745
and already computes `close` and `prev_close` for every one of them. This
copies that into a table the browser can read.

ONE ROW PER SYMBOL PER SESSION, keyed (session_date, symbol), so re-running is
idempotent and the page can ask for the newest session without guessing.

Run after the nightly chain, which is what produces the parquet this reads.
"""

import os
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests                                              # noqa: E402
import pandas as pd                                          # noqa: E402

from atlas.config import SUPABASE_URL as _CFG_URL, SUPABASE_KEY as _CFG_KEY  # noqa: E402
from engine.paths import ALL_ROWS                            # noqa: E402

log = logging.getLogger("publish-closes")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or _CFG_URL or "").rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or _CFG_KEY or "")

BATCH = 500


def _headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates"}


def run(session_date: str = None) -> int:
    df = pd.read_parquet(ALL_ROWS)
    df["date"] = pd.to_datetime(df["date"])
    day = (df[df["date"] == pd.Timestamp(session_date)] if session_date
           else df[df["date"] == df["date"].max()])
    if not len(day):
        log.error(f"no rows for {session_date or 'the newest date'}")
        return 1
    stamp = day["date"].iloc[0].date().isoformat()

    # ONE ROW PER SYMBOL. The frame carries a row per symbol per direction, so
    # a plain read would publish each symbol twice and let the second silently
    # win. close and prev_close are identical across a symbol's rows -- they
    # describe the instrument, not the setup -- so the first is correct.
    day = day.drop_duplicates(subset=["symbol"], keep="first")

    # ── THE 20-DAY WINDOW, from the per-symbol history ────────────
    #
    # NOT from all_rows: 03b writes that frame for the LATEST DATE ONLY, so a
    # 20-session window computed there is one session wide and "at a one-month
    # high" would be true of every stock that closed at its own day high. The
    # real history is data/processed/stocks/<SYM>.parquet -- 745 files, ~925
    # sessions each.
    #
    # INCLUDING TODAY, so the window and the close beside it cannot disagree.
    STOCKS = ROOT / "data" / "processed" / "stocks"
    hi20, lo20 = {}, {}
    missing = 0
    for sym in day["symbol"].astype(str):
        f = STOCKS / f"{sym}.parquet"
        if not f.exists():
            missing += 1
            continue
        try:
            h = pd.read_parquet(f, columns=["date", "high", "low"])
            h["date"] = pd.to_datetime(h["date"])
            w = h[h["date"] <= pd.Timestamp(stamp)].tail(20)
            if len(w) >= 10:        # too short a window is not a month
                hi20[sym] = float(w["high"].max())
                lo20[sym] = float(w["low"].min())
        except Exception:
            missing += 1
    log.info(f"20-day window built for {len(hi20)} symbol(s)"
             + (f"; {missing} without usable history" if missing else ""))

    rows = []
    skipped = 0
    for _, r in day.iterrows():
        c, p = r.get("close"), r.get("prev_close")
        try:
            c, p = float(c), float(p)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not (c > 0 and p > 0):
            skipped += 1
            continue
        sym = str(r["symbol"])

        def _n(v, nd=2):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return None if f != f else round(f, nd)   # NaN-safe

        rows.append({"session_date": stamp, "symbol": sym,
                     "close": round(c, 2), "prev_close": round(p, 2),
                     "change_pct": round((c - p) / p * 100.0, 2),
                     "high": _n(r.get("high")), "low": _n(r.get("low")),
                     "volume": (int(r["volume"])
                                if str(r.get("volume", "")).replace(".", "").isdigit()
                                else None),
                     "rvol": _n(r.get("rvol")),
                     "high_20d": _n(hi20.get(sym)),
                     "low_20d": _n(lo20.get(sym)),
                     "high_52w": _n(r.get("high_52w"))})

    if not rows:
        log.error("nothing publishable")
        return 1

    sent = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        try:
            resp = requests.post(f"{SUPABASE_URL}/rest/v1/daily_closes",
                                 headers=_headers(), json=chunk, timeout=40)
        except Exception as e:
            log.error(f"write failed: {type(e).__name__}: {e}")
            return 1
        if resp.status_code not in (200, 201, 204):
            log.error(f"write rejected: HTTP {resp.status_code} "
                      f"{resp.text[:200]}")
            return 1
        sent += len(chunk)

    adv = sum(1 for r in rows if r["change_pct"] > 0)
    dec = sum(1 for r in rows if r["change_pct"] < 0)
    print(f"session {stamp}: published {sent} symbol(s) "
          f"({adv} advancing, {dec} declining"
          + (f", {skipped} skipped for a missing close" if skipped else "")
          + ")")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
