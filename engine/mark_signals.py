"""
THE STOCK LOGIC — Daily Directional Marking
===========================================
Marks every LIVE signal previous close -> current close, once per trading day.

A signal published in the last WINDOW_DAYS trading days is live. Each evening it
is marked again: LONG up = correct today, SHORT down = correct today. The unit is
TODAY'S DIRECTION, not the outcome of a trade. A signal down 15% since its call
still counts as correct on any day it rises; cumulative move is tracked
separately.

WHY DAILY MARKS AND NOT MARK-TO-TODAY
-------------------------------------
Measured from the signal date to now, accuracy is dominated by how old the
window happens to be -- 42.6% at one day, 71.6% past sixty on this data, because
given enough days a LONG drifts up with the market. A blended figure mostly
reports the age mix. Every daily mark is a one-day move, so all marks are
comparable whatever the signal's age.

Older signals do contribute MORE marks than recent ones (a 20-day-old signal has
been marked 19 times, yesterday's once). That is inherent to a live book and the
page says so.

DELIBERATELY SEPARATE from signal_outcomes and the trade record. Those measure a
trade with an entry, a stop and a target. This measures a directional call. The
two are not comparable and are never mixed.

Run: python3 engine/mark_signals.py [--backfill N] [--date YYYY-MM-DD]
Cron: after 06_push_supabase in the EOD chain.
"""

import os
import sys
import argparse
import logging
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
# Repo root too: build_market imports `engine.upstox_ws` by package path, and
# the Nifty fallback below imports build_market.
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from engine.trading_calendar import is_trading_day
except ModuleNotFoundError:
    from trading_calendar import is_trading_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
STOCKS_DIR   = Path("data/processed/stocks")
MARKET_FILE  = Path("data/processed/market.parquet")

WINDOW_DAYS = 20     # a signal is live for this many trading days
BATCH       = 500    # rows per upsert request


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }


def fetch_signals() -> pd.DataFrame:
    """Every published signal. Deduped on the natural key -- 06_push can leave
    more than one row per (date, symbol, direction) and marks are uniquely keyed
    on exactly that, so duplicates would upsert over each other."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals"
        f"?select=signal_date,symbol,direction,setup_name,entry_ref,sl,target_1&limit=20000",
        headers=_headers(), timeout=60)
    if r.status_code != 200:
        log.error(f"signal fetch failed: HTTP {r.status_code} {r.text[:200]}")
        sys.exit(1)
    df = pd.DataFrame(r.json())
    if df.empty:
        return df
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df["direction"]   = df["direction"].astype(str).str.upper()
    return df.drop_duplicates(subset=["signal_date", "symbol", "direction"])


def load_closes() -> dict:
    """{symbol: DataFrame[date, open, high, low, close]} sorted ascending.

    `open` is for SHORT marks: a short is MIS, one session, entered at the open
    and squared off at the close of the same day.
    `high`/`low` are for resolve_signal(), which needs to know whether a bar
    touched the stop or the target -- a close-only series cannot tell.
    """
    want = ["date", "open", "high", "low", "close"]
    out = {}
    for f in STOCKS_DIR.glob("*.parquet"):
        try:
            d = pd.read_parquet(f, columns=want)
        except Exception:
            # A file missing the OHLC columns should not cost us the symbol
            # entirely -- longs' daily marks only need close. Its shorts cannot
            # be marked and it cannot resolve, which resolve_signal handles.
            try:
                d = pd.read_parquet(f, columns=["date", "close"])
                for c in ("open", "high", "low"):
                    d[c] = float("nan")
                log.warning(f"{f.stem}: no OHLC columns — SHORT marks and resolution unavailable")
            except Exception as e:
                log.warning(f"{f.stem}: unreadable ({e})")
                continue
        d["date"] = pd.to_datetime(d["date"])
        out[f.stem] = d.sort_values("date").reset_index(drop=True)
    return out


def nifty_series() -> pd.DataFrame:
    if not MARKET_FILE.exists():
        log.warning("market.parquet absent — no Nifty benchmark")
        return pd.DataFrame()
    d = pd.read_parquet(MARKET_FILE, columns=["date", "nifty_close"])
    d["date"] = pd.to_datetime(d["date"])
    return d.dropna().sort_values("date").reset_index(drop=True)


def nifty_from_upstox() -> pd.DataFrame:
    """Daily Nifty closes straight from Upstox, same source build_market uses.

    The benchmark used to come ONLY from market.parquet. Nothing in this repo
    schedules build_market.py, which writes that file -- so it froze on
    2026-08-11 and market_marks stopped with it, while signal_marks kept being
    written nightly. The dashboard then compared the book against an index that
    had stopped moving, and the only trace was a log line nobody reads.

    This is not a second opinion on the number; it is a shorter path to the
    same one, so the benchmark survives that file going stale again.
    """
    try:
        from engine.build_market import _fetch_index_daily, NIFTY_KEY
    except Exception as e:                      # noqa: BLE001 - want the reason
        log.error(f"Nifty fallback unavailable: cannot import build_market ({e})")
        return pd.DataFrame()
    try:
        df = _fetch_index_daily(NIFTY_KEY)
    except Exception as e:                      # noqa: BLE001
        log.error(f"Nifty fallback fetch raised: {e}")
        return pd.DataFrame()
    if df.empty or "close" not in df.columns:
        return pd.DataFrame()
    return (df[["date", "close"]].rename(columns={"close": "nifty_close"})
              .dropna().sort_values("date").reset_index(drop=True))


def resolve_signal(s, closes: dict, all_dates: list) -> dict:
    """Walk forward from the signal date and record what happened FIRST.

    Mark-to-market answers "where is price now"; this answers "did the trade as
    designed work" -- entry, stop, target, horizon. A signal resolves once and
    then stops moving, which is the whole point: the number cannot be flattered
    by a later rally or punished by a later drift.

        low  <= sl        -> STOP    at sl
        high >= target_1  -> TARGET  at target_1
        neither in 20 td  -> EXPIRED at that day's close
        SHORT             -> SAME_DAY, its single MIS session

    STOP WINS a bar that breaches the stop and reaches the target. Daily bars
    carry no intraday sequence, so which came first is unknowable; the
    conservative reading is hardcoded rather than guessed.

    ENTRY IS entry_ref FOR LONGS, NOT THE SIGNAL-DATE CLOSE. DELIBERATE.
    ===================================================================
    Shorts are the exception and enter at the day-one open -- see the SHORT
    branch for why the entry_ref argument does not transfer to them.
    The daily marks in mark_one_date() enter at the signal-date close, and that
    is also correct -- for what they measure. The two are different questions
    and they need different entries:

        marks        "what did the market do since we called it"
                     -> close, because the close is where the call was made
        resolved P&L "did the trade as designed work"
                     -> entry_ref, because sl and target_1 are defined
                        RELATIVE TO entry_ref, and nothing else

    Do not "unify" these. It was tried the other way round first and it silently
    destroyed the measurement. Every signal is built at exactly 2R -- verified,
    min = median = max = 2.00 across 344 longs and 77 shorts -- but entering at
    the close instead threw that away, because the close has already drifted
    away from entry_ref by the time the signal publishes:

        signals that resolved TARGET had closed +4.0% ABOVE entry_ref, so the
          target sat only 1.3% away while the stop sat 6.3% away  -> 0.2R
        signals that resolved STOP had closed -1.4% BELOW entry_ref, so the
          stop sat 1.4% away while the target sat 7.2% away       -> 5R

    The near-even STOP/TARGET counts then measured how far price had already
    drifted before the clock started, not whether the design works. Entering at
    entry_ref restores it: every STOP is -1R, every TARGET is +2R.

    This assumes the entry_ref limit fills. It is a design measurement, not a
    fill simulation; a signal whose entry is never reached is not modelled here.

    Returns {} when it cannot resolve: no price data, no usable entry_ref or
    sl, or the signal is simply too young. Unresolved is a real state, not a
    zero.
    """
    d = closes.get(s.symbol)
    if d is None:
        return {}

    def bar(dt):
        hit = d.loc[d["date"] == dt]
        return hit.iloc[0] if len(hit) else None

    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(f) or f <= 0 else f

    # SHORT: MIS, one session. Entered at the OPEN of the first trading day
    # after the signal, squared off at that day's close.
    #
    # NOT entry_ref, unlike the long branch below. The entry_ref argument is
    # that sl and target_1 are defined against it, so entering elsewhere
    # changes the trade's R -- but a SAME_DAY short never touches sl or
    # target_1. It exits at the close. There is no R to preserve, so the
    # argument does not transfer.
    #
    # What entry_ref does here instead is charge the trade for a gap it was
    # never exposed to. entry_ref sits BELOW the day-one open on 50 of 77
    # shorts, 1.73% below on average, and of the -Rs1,65,044 that entry_ref
    # produces, -Rs1,33,046 -- 81% -- is the entry_ref-to-open move. An MIS
    # position does not exist overnight and cannot lose money in that gap.
    # Only the -Rs31,998 open-to-close part is a session that was actually
    # held.
    #
    # This was briefly changed to entry_ref and is deliberately back. Do not
    # "make it consistent" with the long branch: the two branches measure
    # different trades.
    if s.direction == "SHORT":
        later = [x for x in all_dates if x > s.signal_date]
        if not later:
            return {}
        day1 = later[0]
        b = bar(day1)
        if b is None:
            return {}
        entry = num(b["open"])
        if entry is None:
            return {}
        exit_px = float(b["close"])
        return {"resolved_pnl_pct": round((entry - exit_px) / entry * 100.0, 4),
                "resolved_on": day1.date().isoformat(),
                "resolution": "SAME_DAY"}

    # LONG: entry_ref, because sl and target_1 are defined against it and the
    # 2R structure is the thing being measured. See the docstring.
    entry = num(getattr(s, "entry_ref", None))
    if entry is None:
        return {}

    sl  = num(s.sl)
    tgt = num(s.target_1)
    if sl is None:
        return {}

    horizon = [x for x in all_dates if x > s.signal_date][:WINDOW_DAYS]
    if not horizon:
        return {}

    for i, dt in enumerate(horizon):
        b = bar(dt)
        if b is None:
            continue
        lo, hi, cl = b["low"], b["high"], b["close"]
        if pd.isna(lo) or pd.isna(hi):
            continue
        if float(lo) <= sl:                       # stop first, always
            return {"resolved_pnl_pct": round((sl - entry) / entry * 100.0, 4),
                    "resolved_on": dt.date().isoformat(), "resolution": "STOP"}
        if tgt and float(hi) >= tgt:
            return {"resolved_pnl_pct": round((tgt - entry) / entry * 100.0, 4),
                    "resolved_on": dt.date().isoformat(), "resolution": "TARGET"}
        if i == WINDOW_DAYS - 1:                  # full horizon, never touched
            return {"resolved_pnl_pct": round((float(cl) - entry) / entry * 100.0, 4),
                    "resolved_on": dt.date().isoformat(), "resolution": "EXPIRED"}

    return {}                                     # still inside the horizon


def resolve_all(signals: pd.DataFrame, closes: dict, all_dates: list) -> dict:
    """{(signal_date, symbol, direction): resolution}. Computed once per signal
    rather than once per mark -- a long carries the same resolution on all 20 of
    its rows."""
    out, counts = {}, {}
    for s in signals.itertuples():
        r = resolve_signal(s, closes, all_dates)
        out[(s.signal_date, s.symbol, s.direction)] = r
        counts[r.get("resolution") or "UNRESOLVED"] = counts.get(r.get("resolution") or "UNRESOLVED", 0) + 1
    log.info("resolution: " + " | ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return out


def trading_dates(closes: dict) -> list:
    """Every date on which we hold closes, ascending. This is the real trading
    calendar as observed in the data, which beats a hardcoded holiday list."""
    seen = set()
    for d in closes.values():
        seen.update(d["date"].tolist())
    return sorted(seen)


def mark_one_date(signals: pd.DataFrame, closes: dict, all_dates: list,
                  mark_date: pd.Timestamp, resolutions: dict = None) -> list:
    """Rows for every live signal on one mark date."""
    idx = all_dates.index(mark_date)
    if idx == 0:
        return []                                   # no previous close to mark from
    prev_date = all_dates[idx - 1]
    window    = all_dates[max(0, idx - WINDOW_DAYS + 1): idx + 1]
    window_start = window[0]

    live = signals[(signals["signal_date"] >= window_start) &
                   (signals["signal_date"] <= mark_date)]

    rows = []
    for s in live.itertuples():
        # A signal contributes from the day AFTER it publishes. On its own
        # signal date the reference close IS the mark close, so it can be
        # neither correct nor incorrect -- excluded, not counted as a miss.
        if s.signal_date >= mark_date:
            continue
        d = closes.get(s.symbol)
        if d is None:
            continue

        def close_on(dt):
            hit = d.loc[d["date"] == dt, "close"]
            return float(hit.iloc[0]) if len(hit) else None

        def open_on(dt):
            hit = d.loc[d["date"] == dt, "open"]
            if not len(hit):
                return None
            v = float(hit.iloc[0])
            return None if pd.isna(v) or v <= 0 else v

        age = sum(1 for x in all_dates
                  if s.signal_date < x <= mark_date)

        if s.direction == "SHORT":
            # MIS: one session. Entered at the open of the first trading day
            # AFTER the signal published, squared off at that day's close.
            #
            # Not the signal date -- the EOD chain publishes after that close,
            # so entering on it would trade on information that did not exist.
            # Not close-to-close -- that carries the overnight gap, which MIS
            # cannot hold. And no second mark: 1093 of the 1180 SHORT marks
            # this replaces were measuring a position squared off days earlier.
            if age != 1:
                continue
            entry = open_on(mark_date)
            cur   = close_on(mark_date)
            if entry is None or cur is None:
                continue
            daily = (entry - cur) / entry * 100.0     # short profits as price falls
            cum   = daily                             # one session, nothing to accumulate
            correct = None if cur == entry else bool(daily > 0)
            ref = prev = entry                        # the entry is the only reference
        else:
            # LONG: CNC, held, marked close-to-close every day it stays live.
            #
            # ref_close is the SIGNAL-DATE CLOSE and stays that way. This is the
            # correct entry for a mark, which asks what the market did after the
            # call. It is NOT the entry resolve_signal() uses -- that one enters
            # at entry_ref, because it asks whether the design worked and the
            # stop and target are defined against entry_ref. Two questions, two
            # entries, both deliberate. See resolve_signal's docstring before
            # changing either.
            ref, prev, cur = close_on(s.signal_date), close_on(prev_date), close_on(mark_date)
            if ref is None or prev is None or cur is None or ref <= 0 or prev <= 0:
                continue
            daily = (cur - prev) / prev * 100.0
            cum   = (cur - ref) / ref * 100.0
            # Exactly flat: neither correct nor incorrect. NULL, not False.
            correct = None if cur == prev else bool(daily > 0)

        res = (resolutions or {}).get((s.signal_date, s.symbol, s.direction)) or {}

        rows.append({
            "resolved_pnl_pct": res.get("resolved_pnl_pct"),
            "resolved_on":      res.get("resolved_on"),
            "resolution":       res.get("resolution"),
            "signal_date":    s.signal_date.date().isoformat(),
            "symbol":         s.symbol,
            "direction":      s.direction,
            "mark_date":      mark_date.date().isoformat(),
            "ref_close":      round(ref, 2),
            "prev_close":     round(prev, 2),
            "mark_close":     round(cur, 2),
            "daily_move_pct": round(daily, 4),
            "cum_move_pct":   round(cum, 4),
            "correct_today":  correct,
            "setup_name":     s.setup_name,
            "age_days":       age,
        })
    return rows


def upsert(path: str, rows: list, conflict: str) -> bool:
    """`path` carries the literal /rest/v1/<table> so tools/check_schema.py can
    verify it statically. Building it from a variable made the table invisible
    to that check -- the same blind spot the checker itself just flagged."""
    ok = True
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        r = requests.post(f"{SUPABASE_URL}{path}?on_conflict={conflict}",
                          headers=_headers(), json=chunk, timeout=120)
        if r.status_code not in (200, 201, 204):
            log.error(f"{path} upsert failed: HTTP {r.status_code} {r.text[:250]}")
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=1,
                    help="number of most recent trading days to mark (default 1)")
    ap.add_argument("--date", help="mark this date only (YYYY-MM-DD)")
    args = ap.parse_args()

    if not SUPABASE_KEY:
        log.error("SUPABASE_SERVICE_KEY not set")
        return 1

    signals = fetch_signals()
    if signals.empty:
        log.error("no signals to mark")
        return 1
    closes = load_closes()
    if not closes:
        log.error("no close data in data/processed/stocks")
        return 1
    all_dates = trading_dates(closes)
    log.info(f"{len(signals)} signals | {len(closes)} symbols | "
             f"closes through {all_dates[-1].date()}")

    if args.date:
        want = [pd.Timestamp(args.date)]
        if want[0] not in all_dates:
            log.error(f"{args.date} is not a date we hold closes for")
            return 1
    else:
        want = all_dates[-args.backfill:]

    # Resolution is a property of the SIGNAL, not of a mark date, so it is
    # computed once here rather than re-walked for every mark.
    resolutions = resolve_all(signals, closes, all_dates)

    total = 0
    for md in want:
        rows = mark_one_date(signals, closes, all_dates, md, resolutions)
        if not rows:
            log.info(f"{md.date()}: nothing live")
            continue
        if upsert("/rest/v1/signal_marks", rows,
                  "signal_date,symbol,direction,mark_date"):
            scored = [r for r in rows if r["correct_today"] is not None]
            hit = sum(1 for r in scored if r["correct_today"])
            acc = f"{hit / len(scored) * 100:.1f}%" if scored else "n/a"
            log.info(f"{md.date()}: {len(rows)} marks | correct {hit}/{len(scored)} = {acc}")
            total += len(rows)

    # Benchmark
    nf = nifty_series()

    # Does the parquet actually cover the dates we are marking? Previously this
    # was never asked: a stale file produced zero benchmark rows and one
    # warning, and the dashboard quietly compared the book against nothing.
    need = {pd.Timestamp(md) for md in want}
    have = set(nf["date"]) if not nf.empty else set()
    missing = sorted(need - have)
    if missing:
        log.warning(
            f"market.parquet covers {len(need) - len(missing)} of {len(need)} mark date(s); "
            f"missing {missing[0].date()}..{missing[-1].date()} — falling back to Upstox. "
            f"build_market.py has not refreshed the file."
        )
        live = nifty_from_upstox()
        if live.empty:
            log.error("Nifty fallback returned nothing — benchmark will be incomplete")
        else:
            nf = (live if nf.empty
                  else pd.concat([nf, live], ignore_index=True)
                         .drop_duplicates(subset="date", keep="last"))
            nf = nf.sort_values("date").reset_index(drop=True)
            still = sorted(need - set(nf["date"]))
            log.info(f"Nifty series now spans {nf['date'].min().date()}..{nf['date'].max().date()}"
                     + (f"; {len(still)} mark date(s) still unbenchmarked" if still else "; all mark dates covered"))

    if nf.empty:
        # Loud: this is the state that stopped market_marks for eight sessions.
        log.error("no Nifty series from market.parquet or Upstox — benchmark NOT written; "
                  "excess-move on the dashboard will read '—' for these dates")
    else:
        mrows = []
        for md in want:
            i = nf.index[nf["date"] == md]
            if len(i) == 0 or i[0] == 0:
                continue
            i = i[0]
            cur  = float(nf.loc[i, "nifty_close"])
            prev = float(nf.loc[i - 1, "nifty_close"])
            if prev <= 0:
                continue
            mrows.append({"mark_date": md.date().isoformat(),
                          "nifty_close": round(cur, 2),
                          "nifty_move_pct": round((cur - prev) / prev * 100.0, 4)})
        if mrows:
            upsert("/rest/v1/market_marks", mrows, "mark_date")
            log.info(f"benchmark: {len(mrows)} Nifty mark(s) for "
                     f"{mrows[0]['mark_date']}..{mrows[-1]['mark_date']}")
        else:
            # ERROR, not warning. This is the exact condition that ran for eight
            # sessions unnoticed, and it means the dashboard's excess-move
            # figure silently narrows or disappears.
            log.error(f"no Nifty marks written for {len(want)} mark date(s) — neither "
                      f"market.parquet nor Upstox had a usable close for them")

    log.info(f"DONE — {total} mark row(s)")
    return 0


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    sys.exit(main())
