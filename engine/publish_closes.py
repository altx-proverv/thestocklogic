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
from datetime import date

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

    # THE GATE BELONGS ON THE DAY BEING PUBLISHED, not on today. This job is
    # routinely run for an explicit past session (the `session_date` argument
    # backfills), so "is today a trading day" would be both the wrong question
    # and a backfill-breaking one. What must be true is that the stamp about to
    # be written names a session that existed.
    #
    # Nothing has reached this check yet, and that is luck rather than design:
    # the parquet only ever contains bhavcopy dates and NSE publishes no
    # bhavcopy for a holiday, so the upstream absence has been doing the
    # guard's job. An upstream absence is not a guard -- it is a guard that
    # works until the data source changes.
    from engine.trading_calendar import require_trading_day
    if not require_trading_day(date.fromisoformat(stamp), "publish_closes", log):
        log.error(f"refusing to publish daily_closes for {stamp}: it is not an "
                  f"NSE trading day")
        return 1

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
    hi20, lo20, lo52, hist = {}, {}, {}, {}
    missing = 0
    for sym in day["symbol"].astype(str):
        f = STOCKS / f"{sym}.parquet"
        if not f.exists():
            missing += 1
            continue
        try:
            h = pd.read_parquet(f, columns=["date", "high", "low"])
            h["date"] = pd.to_datetime(h["date"])
            upto = h[h["date"] <= pd.Timestamp(stamp)]
            # HOW MUCH HISTORY EXISTS AT ALL, which is what guards the level
            # phrases: "a one-month high" needs more than a month behind it.
            hist[sym] = int(len(upto))
            w = upto.tail(20)
            if len(w) >= 10:        # too short a window is not a month
                hi20[sym] = float(w["high"].max())
                lo20[sym] = float(w["low"].min())
            # THE 52-WEEK LOW, on the SAME window high_52w uses. 252 sessions
            # is not a guess: it is the only tail length that reproduces the
            # stored high_52w on all 40 symbols sampled -- tail(250) missed
            # one, tail(240) missed six, 365 calendar days missed three. A low
            # computed on a different window from the high beside it would be
            # a pair that cannot be compared.
            y = upto.tail(252)
            if len(y) >= 240:       # the same bar the level guard already sets
                lo52[sym] = float(y["low"].min())
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
                     "high_52w": _n(r.get("high_52w")),
                     "low_52w": _n(lo52.get(sym)),
                     "history_sessions": hist.get(sym)})

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

    log_vocabulary_gaps(stamp, rows)

    adv = sum(1 for r in rows if r["change_pct"] > 0)
    dec = sum(1 for r in rows if r["change_pct"] < 0)
    print(f"session {stamp}: published {sent} symbol(s) "
          f"({adv} advancing, {dec} declining"
          + (f", {skipped} skipped for a missing close" if skipped else "")
          + ")")
    return 0




# ── THE VOCABULARY GAP LOG ────────────────────────────────────────
#
# Every time a fact clears its bar and tape-vocabulary.json has no phrase for
# it, the page still shows it -- the catch-all states the measurement plainly,
# because a silently dropped stock is one the reader never learns about. This
# records the gap so that in a month there is an evidence-based list of what
# the vocabulary is missing, drawn from what the market did rather than from
# imagining cases.
#
# EVALUATED HERE, AGAINST THE SAME FILE THE PAGE READS. The thresholds live in
# that JSON and both sides read them, so the two cannot drift on a number. The
# page cannot write anything -- by design, it holds only a read key -- so the
# log belongs on this side.

def log_vocabulary_gaps(stamp: str, rows: list) -> int:
    """-> how many gaps were recorded for this session."""
    import json

    vf = ROOT / "tape-vocabulary.json"
    try:
        vocab = json.loads(vf.read_text())
    except Exception as e:
        log.error(f"cannot read {vf.name} ({e}) — no gap check this run")
        return 0

    phrases = vocab.get("phrases") or {}
    th = vocab.get("thresholds") or {}

    def t(name, default):
        v = th.get(name)
        return default if v is None else v

    gaps = []
    for r in rows:
        px, vol = r.get("close"), r.get("volume")
        if not px or not vol or px * vol < t("min_turnover_inr", 20000000):
            continue
        hsess = r.get("history_sessions") or 0
        facts = []

        if (r.get("high_52w") and px >= r["high_52w"] * 0.999
                and hsess >= t("min_history_for_year", 200)):
            facts.append(("high_52w", f"{r['high_52w']}"))
        elif (r.get("low_52w") and px <= r["low_52w"] * 1.001
                and hsess >= t("min_history_for_year", 200)):
            facts.append(("low_52w", f"{r['low_52w']}"))
        elif (r.get("high_20d") and px >= r["high_20d"] * 0.999
                and hsess >= t("min_history_for_month", 60)):
            facts.append(("high_20d", f"{r['high_20d']}"))
        elif (r.get("low_20d") and px <= r["low_20d"] * 1.001
                and hsess >= t("min_history_for_month", 60)):
            facts.append(("low_20d", f"{r['low_20d']}"))

        rv = r.get("rvol")
        if rv and rv >= t("heavy_volume_x", 3.0):
            facts.append(("heavy_volume", f"{rv}x"))
        elif rv and 0 < rv <= t("light_volume_x", 0.5):
            facts.append(("light_volume", f"{rv}x"))

        hi, lo = r.get("high"), r.get("low")
        edge = t("range_edge_pct", 0.1)
        if hi and lo and hi > lo:
            pos = (px - lo) / (hi - lo)
            if pos >= 1 - edge:
                facts.append(("closed_high", f"{pos:.2f} of range"))
            elif pos <= edge:
                facts.append(("closed_low", f"{pos:.2f} of range"))

        for fact, measure in facts:
            if not phrases.get(fact):
                gaps.append({"session_date": stamp, "symbol": r["symbol"],
                             "fact": fact, "measurement": measure,
                             "change_pct": r.get("change_pct")})

    if not gaps:
        print("vocabulary: every qualifying fact had a phrase")
        return 0

    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/tape_vocabulary_gaps",
            headers=_headers(), json=gaps, timeout=30)
        if resp.status_code not in (200, 201, 204):
            log.error(f"gap log rejected: HTTP {resp.status_code} "
                      f"{resp.text[:160]}")
            return 0
    except Exception as e:
        log.error(f"gap log failed: {type(e).__name__}: {e}")
        return 0

    from collections import Counter
    by = Counter(g["fact"] for g in gaps)
    print(f"vocabulary: {len(gaps)} fact(s) with no phrase — "
          + ", ".join(f"{k} x{v}" for k, v in by.most_common()))
    return len(gaps)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
