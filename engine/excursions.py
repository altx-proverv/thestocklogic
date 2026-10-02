"""
THE STOCK LOGIC — signal excursions: the daily path after entry.

    python3 -m engine.excursions                    # nightly: newly resolvable
    python3 -m engine.excursions --backfill         # every signal with bars
    python3 -m engine.excursions --since 2026-05-01
    python3 -m engine.excursions --dry-run --limit 5

WHY. Every row in signal_outcomes exits at exactly -1.00R or +2.00R: 228 of 228
losses at `sl`, 65 of 65 wins at `target_1` which is exactly 2R. That is not what
the market did, it is how resolution was built. So no exit rule is testable from
that table -- a 3R target, a trailing stop, a time stop each need to know where
price actually went, and the record only says which of two pre-set lines it
crossed first.

ENTRY CONVENTION IS COPIED FROM update_outcomes, deliberately and exactly, so the
two are comparable. A path measured from a different fill than the outcome was
scored from would answer a question about a trade nobody took:

    entry session   the first trading day AFTER signal_date
    LONG            MISSED if next_open > entry_high * 1.005
                    INVALIDATED if next_open < sl
                    else fill at min(next_open, entry_high)
    SHORT           mirrored

NO SWING DATA. The trailing study that produced +0.371R had to shift swing
confirmations by five bars, because detect_swing_points reads LB bars either side
and the forward-filled last_swing_low is not causal mid-series. This module reads
high/low/close only, all known at the close of the bar they describe, so nothing
built on its output can inherit that look-ahead.

WHERE IT CAN RUN. It needs daily bars for the symbol, from
data/processed/stocks/<SYMBOL>.parquet. On a developer machine those are stale --
they stop months before the resolved signals do -- so the backfill belongs on the
box. A symbol with no bars, or bars that stop before the signal, is SKIPPED WITH A
REASON rather than silently producing a short path.
"""

import sys
import time
import logging
import argparse
from datetime import timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas.config import SUPABASE_URL, SUPABASE_KEY          # noqa: E402
from engine.provenance import engine_sha                      # noqa: E402

log = logging.getLogger("EXCURSIONS")
IST = timezone(timedelta(hours=5, minutes=30))

STOCKS_DIR = Path(__file__).resolve().parent.parent / "data/processed/stocks"
WINDOW_DAYS = 20

# A reject has no stop, so one is constructed to make its R comparable. These
# mirror atlas/config.py's MIN_STOP_PCT / MAX_STOP_PCT so a counterfactual reject
# is clipped to the same band a real trade would have been.
REJECT_STOP_FALLBACK_PCT = 2.0
MIN_STOP_PCT_REF = 0.5
MAX_STOP_PCT_REF = 5.0          # trading days of path recorded per signal
GAP_TOLERANCE = 0.005     # the 0.5% the entry convention allows past the band


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


# ══════════════════════════════════════════════════════════════════
# INPUTS
# ══════════════════════════════════════════════════════════════════

# PostgREST caps a response at 1,000 rows and an explicit `limit` CANNOT raise it
# -- limit=200000 returns 1,000 exactly like no limit at all, with HTTP 200 and no
# warning anywhere. Every fetch here is over a table that crosses that line:
# 2,787 detections, 845 signals and climbing, and one day_offset=1 row per
# measured item. Unpaged, this module would have measured the newest 1,000
# detections, declared the rest unmeasured every night, and re-walked them
# forever. Paged in one place so a future caller cannot reintroduce it.
PAGE = 1000


def _get_all(path: str, params: str, hard_limit: int = 0) -> list:
    """Every row matching `params`, following Range pages until one comes short.

    `hard_limit` is the caller's own --limit: a deliberate cap for a test run,
    which is a different thing from the transport's cap and is applied here
    rather than handed to PostgREST.
    """
    out, offset = [], 0
    while True:
        url = f"{SUPABASE_URL}/rest/v1/{path}?{params}&limit={PAGE}&offset={offset}"
        r = requests.get(url, headers=_headers(), timeout=60)
        if r.status_code != 200:
            return r                      # caller inspects and raises/degrades
        chunk = r.json() or []
        out += chunk
        if len(chunk) < PAGE:
            break
        offset += PAGE
        if hard_limit and len(out) >= hard_limit:
            break
        if offset > 500_000:              # a runaway loop is worse than a short read
            log.error(f"{path}: stopped paging at {offset} rows — unexpected volume")
            break
    return out[:hard_limit] if hard_limit else out


def fetch_signals(since: str = None, limit: int = 0) -> list:
    """Signals to measure, newest first.

    EVERY published signal, not only the resolved ones. A signal that MISSED its
    entry has no path by definition, and one still OPEN has a path that simply has
    not finished -- but a signal whose outcome is unresolved is exactly the one an
    exit study wants, because the 5-day window is what left it unresolved.
    """
    params = ("select=signal_date,symbol,direction,entry_ref,entry_low,entry_high,sl"
              "&order=signal_date.desc")
    if since:
        params += f"&signal_date=gte.{since}"
    got = _get_all("signals", params, hard_limit=limit)
    if not isinstance(got, list):
        raise RuntimeError(f"signal fetch failed: HTTP {got.status_code} "
                           f"{got.text[:200]}")
    return got


def fetch_detections(since: str = None, limit: int = 0) -> list:
    """Intraday breakout detections from live_signals, kind='detection'.

    A DIFFERENT ENTRY CONVENTION, and that is the whole reason source exists on
    the table. A signal is filled at the next open because it is published after
    the close. A detection already happened: rbe_engine fires when LTP crosses the
    level, so the fill is that LTP on that day, and day_offset 1 is the detection
    day itself rather than the one after.

    Blending the two would produce a column that means "R from entry" for one
    population and "R from tomorrow's open" for the other.
    """
    params = ("select=signal_date,symbol,direction,entry,sl,session,kind,signal_time"
              "&kind=eq.detection&order=signal_date.desc")
    if since:
        params += f"&signal_date=gte.{since}"
    got = _get_all("live_signals", params, hard_limit=limit)
    if not isinstance(got, list):
        if got.status_code in (400, 404) and "kind" in got.text:
            log.warning("live_signals.kind does not exist — "
                        "migrations/PENDING_live_signals_kind.sql not applied. "
                        "No detections measured this run.")
            return []
        raise RuntimeError(f"detection fetch failed: HTTP {got.status_code} "
                           f"{got.text[:200]}")
    return got


def fetch_rejects(since: str = None, limit: int = 0) -> list:
    """Sampled rejected stock-days, for the membership test.

    THE THIRD ENTRY CONVENTION, and the one that needs the most care. A reject has
    no entry band and no stop, because it never became a setup -- so there is
    nothing to fill at and nothing to risk. A path still has to be measured for it,
    or the question "did the filters select better-than-random stock-days" has only
    one side.

    The convention is deliberately the most neutral available: fill at the NEXT
    OPEN, exactly as a signal does, and size the risk off the same ATR band the
    engine would have used had the row qualified. That makes R comparable across
    the two populations without inventing a stop the engine never chose.

    A reject's R is therefore counterfactual and a signal's is not. The `source`
    column is what keeps the two from being averaged, and no consumer should pool
    them without saying which question it is asking.
    """
    params = ("select=sample_date,symbol,direction,close,atr_pct,"
              "disqualify_reason,gates_failed&order=sample_date.desc")
    if since:
        params += f"&sample_date=gte.{since}"
    got = _get_all("reject_sample", params, hard_limit=limit)
    if not isinstance(got, list):
        if got.status_code in (400, 404):
            log.warning("reject_sample not reachable — "
                        "migrations/PENDING_reject_sample.sql not applied. "
                        "No rejects measured this run.")
            return []
        raise RuntimeError(f"reject fetch failed: HTTP {got.status_code} "
                           f"{got.text[:200]}")
    # Normalised onto the shape measure() expects, so the walker needs no third
    # branch: signal_date is the key name everywhere downstream.
    out = []
    for r in got:
        r = dict(r)
        r["signal_date"] = r.pop("sample_date")
        out.append(r)
    return out


def existing_keys() -> set:
    """(source, signal_date, symbol, direction) already measured, so a nightly run
    does not re-walk the whole history every time."""
    keys = set()
    try:
        got = _get_all("signal_excursions",
                       "select=source,signal_date,symbol,direction"
                       "&day_offset=eq.1&order=signal_date.desc")
        if isinstance(got, list):
            for x in got:
                keys.add((str(x.get("source") or "signal"),
                          str(x["signal_date"])[:10], x["symbol"],
                          str(x["direction"]).upper()))
        elif got.status_code in (400, 404):
            log.warning("signal_excursions not reachable — "
                        "migrations/PENDING_signal_excursions.sql not applied?")
        else:
            log.warning(f"existing keys HTTP {got.status_code} — will recompute")
    except Exception as e:
        log.warning(f"could not read existing keys ({e}) — will recompute")
    return keys


_bars = {}


def bars(symbol: str):
    """Daily OHLC for one symbol, cached. None when absent."""
    if symbol in _bars:
        return _bars[symbol]
    import pandas as pd
    f = STOCKS_DIR / f"{symbol}.parquet"
    if not f.exists():
        _bars[symbol] = None
        return None
    try:
        d = pd.read_parquet(f, columns=["date", "open", "high", "low", "close"])
    except Exception as e:
        log.warning(f"{symbol}: bars unreadable ({e})")
        _bars[symbol] = None
        return None
    d["date"] = pd.to_datetime(d["date"])
    _bars[symbol] = d.sort_values("date").reset_index(drop=True)
    return _bars[symbol]


# ══════════════════════════════════════════════════════════════════
# MEASURE
# ══════════════════════════════════════════════════════════════════

def measure(sig: dict, source: str = "signal") -> tuple:
    """(rows, skip_reason). rows is [] when there is no measurable path.

    source="signal"     EOD row. Fill at the next open, per update_outcomes.
    source="detection"  intraday row. Fill at the recorded detection price, and
                        the window starts on the detection day, because the entry
                        already happened on it.
    """
    import pandas as pd
    import numpy as np

    sym = sig.get("symbol")
    d = bars(sym)
    if d is None:
        return [], "no bar file"

    sd = pd.Timestamp(str(sig["signal_date"])[:10])
    if d["date"].max() < sd:
        # Bars stop before the signal: on a developer machine this is most of the
        # record. Named rather than silently skipped, because "no bars yet" and
        # "no bars ever" are different facts about the same empty result.
        return [], f"bars end {str(d['date'].max())[:10]}, before the signal"

    # A detection was filled intraday on signal_date, so its own day is offset 1.
    # A signal is published after the close, so its first session is the next one.
    fwd = (d[d["date"] >= sd] if source == "detection" else d[d["date"] > sd]) \
        .head(WINDOW_DAYS).reset_index(drop=True)
    if len(fwd) < 1:
        return [], "no session after the signal"

    is_long = str(sig.get("direction", "LONG")).upper() != "SHORT"

    if source == "reject":
        # NO STOP EXISTS, so one is constructed from the same ATR band the engine
        # would have applied. REJECT_STOP_ATR is the midpoint of the configured
        # 0.5-5.0% band expressed in ATR terms, and the fallback is used only when
        # atr_pct is missing -- a constructed stop is a stated assumption, a
        # silently-zero one would make every R infinite.
        try:
            entry = float(fwd.iloc[0]["open"])
        except (TypeError, ValueError, KeyError):
            return [], "no open on the next session"
        atr = sig.get("atr_pct")
        try:
            atr = float(atr)
        except (TypeError, ValueError):
            atr = float("nan")
        pct = atr if np.isfinite(atr) and atr > 0 else REJECT_STOP_FALLBACK_PCT
        pct = min(max(pct, MIN_STOP_PCT_REF), MAX_STOP_PCT_REF)
        stop = entry * (1 - pct / 100.0) if is_long else entry * (1 + pct / 100.0)
        if entry <= 0:
            return [], "unusable open"
        return _walk(fwd, entry, stop, is_long, sig, source)

    if source == "detection":
        # No band and no gap test: the fill is the price the detection fired at.
        try:
            entry = float(sig["entry"]); stop = float(sig["sl"])
        except (TypeError, ValueError, KeyError):
            return [], "no detection entry or stop"
        if not all(np.isfinite([entry, stop])) or entry <= 0:
            return [], "unusable levels"
        return _walk(fwd, entry, stop, is_long, sig, source)

    try:
        lo_band = float(sig["entry_low"]); hi_band = float(sig["entry_high"])
        stop = float(sig["sl"])
    except (TypeError, ValueError, KeyError):
        return [], "no entry band or stop"
    if not all(np.isfinite([lo_band, hi_band, stop])) or min(lo_band, hi_band) <= 0:
        return [], "unusable levels"
    lo_band, hi_band = min(lo_band, hi_band), max(lo_band, hi_band)

    nopen = float(fwd.iloc[0]["open"])
    # update_outcomes' entry convention, verbatim.
    if is_long:
        if nopen > hi_band * (1 + GAP_TOLERANCE):
            return [], "MISSED_GAP_UP"
        if nopen < stop:
            return [], "GAPPED_BELOW_SL"
        entry = min(nopen, hi_band)
    else:
        if nopen < lo_band * (1 - GAP_TOLERANCE):
            return [], "MISSED_GAP_DOWN"
        if nopen > stop:
            return [], "GAPPED_ABOVE_SL"
        entry = max(nopen, lo_band)

    return _walk(fwd, entry, stop, is_long, sig, source)


def _walk(fwd, entry, stop, is_long, sig, source):
    """The path itself. Shared by both entry conventions so a change to how MFE is
    accumulated cannot apply to one population and not the other."""
    risk = abs(entry - stop)
    if risk <= 0:
        return [], "zero risk per share"

    t2 = entry + 2 * risk if is_long else entry - 2 * risk
    t3 = entry + 3 * risk if is_long else entry - 3 * risk

    sha = engine_sha()
    rows, run_mfe, run_mae = [], 0.0, 0.0
    for i in range(len(fwd)):
        bar = fwd.iloc[i]
        hi, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if is_long:
            fav, adv = hi - entry, entry - low
            hit_stop = low <= stop
            hit_t2, hit_t3 = hi >= t2, hi >= t3
            close_r = (close - entry) / risk
        else:
            fav, adv = entry - low, hi - entry
            hit_stop = hi >= stop
            hit_t2, hit_t3 = low <= t2, low <= t3
            close_r = (entry - close) / risk
        # CUMULATIVE, not per-bar. The question an exit rule asks is "how far has
        # this been in profit by now", and a per-bar maximum cannot answer it.
        # The path does NOT stop at the stop. run_mae keeps growing past a touch,
        # because this table records what price did, not what a position would
        # have done. A consumer simulating a stop must cut the path at the first
        # stop_touched row -- an mae_r of 4.57 means price went 4.57R against the
        # entry, not that a stopped trade lost 4.57R.
        run_mfe = max(run_mfe, fav / risk)
        run_mae = max(run_mae, adv / risk)
        rows.append({
            "source": source,
            "signal_date": str(sig["signal_date"])[:10],
            "symbol": sig.get("symbol"),
            "direction": "LONG" if is_long else "SHORT",
            "day_offset": i + 1,
            "bar_date": str(bar["date"])[:10],
            "high": round(hi, 2), "low": round(low, 2), "close": round(close, 2),
            "entry_price": round(entry, 2), "stop_price": round(stop, 2),
            "risk_per_share": round(risk, 4),
            "mfe_r": round(run_mfe, 4), "mae_r": round(run_mae, 4),
            "mfe_pct": round(run_mfe * risk / entry * 100, 4),
            "mae_pct": round(run_mae * risk / entry * 100, 4),
            "close_r": round(close_r, 4),
            "stop_touched": bool(hit_stop),
            "target_2r_touched": bool(hit_t2),
            "target_3r_touched": bool(hit_t3),
            # Order unknowable from a daily bar. The resolver that wrote
            # signal_outcomes checks the stop first and assigns every tie to LOSS;
            # this makes the size of that assumption measurable.
            "both_same_bar": bool(hit_stop and hit_t2),
            "engine_sha": sha,
        })
    return rows, ""


# ══════════════════════════════════════════════════════════════════
# WRITE
# ══════════════════════════════════════════════════════════════════

def write(rows: list) -> int:
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/signal_excursions"
            f"?on_conflict=source,signal_date,symbol,direction,day_offset",
            headers={**_headers(),
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=chunk, timeout=90)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"write failed: HTTP {r.status_code} "
                               f"{r.text[:200]}")
        written += len(chunk)
    return written


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [EXCURSIONS] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="every signal, not just the unmeasured ones")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD lower bound")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    started = time.monotonic()
    try:
        sigs = fetch_signals(since=a.since, limit=a.limit)
    except Exception as e:
        log.error(f"cannot read signals: {e}")
        return 1
    if not sigs:
        log.info("no signals to measure")
        return 0

    # DETECTIONS TOO. 2,001 RBE rows have been published as calls since August
    # and not one has ever been scored -- a published population outside the
    # record, which is the wrong-side-zone problem in a different place. Measured
    # here so they accumulate as a hypothesis rather than as unexamined claims.
    try:
        dets = fetch_detections(since=a.since, limit=a.limit)
    except Exception as e:
        log.warning(f"detections unavailable ({e}) — measuring signals only")
        dets = []

    # REJECTS, the negative class for the membership test. Sampled at k=10 per
    # qualifying case by engine/reject_sample, so this is ~36 rows a night rather
    # than 464.
    try:
        rejs = fetch_rejects(since=a.since, limit=a.limit)
    except Exception as e:
        log.warning(f"rejects unavailable ({e}) — measuring signals and detections only")
        rejs = []

    done = set() if a.backfill else existing_keys()
    todo = [("signal", x) for x in sigs
            if ("signal", str(x["signal_date"])[:10], x["symbol"],
                str(x.get("direction", "")).upper()) not in done]
    todo += [("detection", x) for x in dets
             if ("detection", str(x["signal_date"])[:10], x["symbol"],
                 str(x.get("direction", "")).upper()) not in done]
    todo += [("reject", x) for x in rejs
             if ("reject", str(x["signal_date"])[:10], x["symbol"],
                 str(x.get("direction", "")).upper()) not in done]
    log.info(f"{len(sigs)} signal(s) + {len(dets)} detection(s) + "
             f"{len(rejs)} reject(s) fetched, {len(done)} already measured, "
             f"{len(todo)} to walk")

    all_rows, skips = [], {}
    for source, s in todo:
        rows, why = measure(s, source=source)
        if rows:
            all_rows.extend(rows)
        else:
            skips[f"{source}: {why}"] = skips.get(f"{source}: {why}", 0) + 1

    log.info(f"{len(all_rows)} excursion row(s) from "
             f"{len(all_rows) // WINDOW_DAYS if all_rows else 0}+ signal(s)")
    for why, n in sorted(skips.items(), key=lambda x: -x[1]):
        # Every skip NAMED and counted. A backfill that quietly measured a third
        # of the record would be the same class of failure as the recorder that
        # wrote nothing for three months.
        log.info(f"  skipped {n:>5}  {why}")

    if a.dry_run:
        for r in all_rows[:WINDOW_DAYS]:
            log.info(f"  d{r['day_offset']:<3}{r['bar_date']}  "
                     f"mfe {r['mfe_r']:+.2f}R  mae {r['mae_r']:+.2f}R  "
                     f"close {r['close_r']:+.2f}R"
                     f"{'  STOP' if r['stop_touched'] else ''}"
                     f"{'  2R' if r['target_2r_touched'] else ''}"
                     f"{'  3R' if r['target_3r_touched'] else ''}"
                     f"{'  AMBIGUOUS' if r['both_same_bar'] else ''}")
        log.info("DRY RUN — nothing written")
        return 0

    try:
        n = write(all_rows)
    except Exception as e:
        log.error(f"{e}")
        return 1
    log.info(f"wrote {n} row(s) in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
