"""
THE STOCK LOGIC — the detection hypothesis, measured.

WHAT THIS IS FOR
----------------
2,787 of the 2,941 rows in `live_signals` are intraday detections -- 2,001 Range
Breakout rows since 2026-08-14 and 786 ORB rows from the retired morning session.
Every one was published with an entry, a stop and two targets. Not one has ever
been scored. They have been shown on the site as calls, counted in the headline,
and kept entirely outside the accuracy record -- a published population that the
record does not know exists.

So the question "do intraday breakouts work" has had 2,787 observations available
to answer it and no answer, for seven weeks.

WHY THIS NO LONGER WALKS BARS ITSELF
------------------------------------
The previous version of this file loaded each symbol's parquet and walked five
days looking for target_1 or sl. engine/excursions.py now walks twenty days for
both populations and writes the path, so a second walk here would be a second
implementation of the same arithmetic, free to drift from the first. It had
already drifted: five days against excursions' twenty, and a 1,000-row ceiling
(see below) that silently hid two thirds of the detections it was pointed at.

This reads signal_excursions instead. One walker, one convention, and every
figure below is recomputable from rows anyone can query.

WHY THE PUBLISHED TARGETS ARE NOT USED
--------------------------------------
target_1 is a clean 1.50R on all 2,001 RBE rows. target_2 is not usable: on 688
of them (34%) it is not beyond target_1 at all, it ranges from -0.86R to +28.64R,
and at the negative end it sits on the LOSING side of entry -- a "second target"
that is hit by the trade going wrong. Scoring against it would measure the bug.

R multiples off entry and stop are used instead. They are defined for every row,
they mean the same thing in both populations, and they let the same path answer
1.5R, 2R and 3R at once rather than one pre-chosen line.

FIRST TOUCH, AND WHAT IT CANNOT KNOW
------------------------------------
A daily bar that touches both the stop and the target gives no order. Those are
counted and reported as AMBIGUOUS rather than assigned, because assigning them is
how signal_outcomes came to hold 228 losses at exactly -1.00R. The two bounds --
ambiguous-as-win and ambiguous-as-loss -- are printed as a range, and the honest
hit rate is somewhere inside it.

Read-only. Writes nothing anywhere.
"""

import os
import sys
import math
import logging
import argparse
from collections import Counter, defaultdict

import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("SCORE-LIVE")

SUPABASE_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# PostgREST returns at most 1,000 rows and an explicit limit does not raise it:
# limit=200000 returns exactly 1,000, HTTP 200, no warning. The old scorer asked
# for every live_signal in one call and was handed the newest 1,000 of 2,941 --
# it reported a tally over 34% of the data as though it were all of it.
PAGE = 1000

R_TARGETS = (1.5, 2.0, 3.0)


def _headers() -> dict:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _get_all(path: str, params: str) -> list:
    out, offset = [], 0
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}?{params}"
                         f"&limit={PAGE}&offset={offset}",
                         headers=_headers(), timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"{path} HTTP {r.status_code}: {r.text[:200]}")
        chunk = r.json() or []
        out += chunk
        if len(chunk) < PAGE:
            return out
        offset += PAGE
        if offset > 500_000:
            raise RuntimeError(f"{path}: {offset} rows and still paging")


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """95% interval on a proportion. Normal approximation is wrong at the tails
    and these samples run small once they are split by setup."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, c - h), min(1.0, c + h))


def fetch_paths(source: str, since: str = None) -> dict:
    """{(signal_date, symbol, direction): [row, ...] ordered by day_offset}."""
    params = (f"select=signal_date,symbol,direction,day_offset,mfe_r,mae_r,"
              f"close_r,stop_touched,target_2r_touched,target_3r_touched,"
              f"both_same_bar&source=eq.{source}&order=signal_date.desc,"
              f"symbol.asc,day_offset.asc")
    if since:
        params += f"&signal_date=gte.{since}"
    rows = _get_all("signal_excursions", params)
    paths = defaultdict(list)
    for x in rows:
        paths[(str(x["signal_date"])[:10], x["symbol"],
               str(x["direction"]).upper())].append(x)
    for k in paths:
        paths[k].sort(key=lambda r: r["day_offset"])
    return paths


def verdict(path: list, r_target: float, horizon: int = 0) -> tuple:
    """(outcome, day_offset, mfe_r) for one path at one R target.

    Walks forward. The first bar that reaches either side decides it; a bar that
    reaches both within its own range is AMBIGUOUS and stops the walk, because
    nothing after it is conditional on an outcome we do not know.

    mae_r crossing 1.0 is the stop, not stop_touched, so the same path can be
    re-asked at a different target without the recorded 2R/3R flags constraining
    it. mfe_r and mae_r are cumulative, so "first bar at or beyond" is read from
    the step, not the level.
    """
    best = 0.0
    prev_mfe = prev_mae = 0.0
    for row in path:
        off = int(row["day_offset"])
        if horizon and off > horizon:
            return ("OPEN", None, best)
        mfe = float(row.get("mfe_r") or 0.0)
        mae = float(row.get("mae_r") or 0.0)
        best = max(best, mfe)
        hit_t = mfe >= r_target and prev_mfe < r_target
        hit_s = mae >= 1.0 and prev_mae < 1.0
        if hit_t and hit_s:
            return ("AMBIGUOUS", off, best)
        if hit_t:
            return ("WIN", off, best)
        if hit_s:
            return ("LOSS", off, best)
        prev_mfe, prev_mae = mfe, mae
    return ("OPEN", None, best)


def _band(c: Counter) -> str:
    """Hit rate as the range the ambiguous bars leave it in."""
    w, l, a = c["WIN"], c["LOSS"], c["AMBIGUOUS"]
    dec = w + l + a
    if dec == 0:
        return "no decided trades"
    lo_p, lo_a, _ = wilson(w, dec)
    hi_p, _, hi_b = wilson(w + a, dec)
    out = f"{100*lo_p:.1f}%–{100*hi_p:.1f}% of {dec}"
    if a == 0:
        out = f"{100*lo_p:.1f}% of {dec}  [95% CI {100*lo_a:.1f}–{100*hi_b:.1f}]"
    return out


def report(paths: dict, label: str, horizon: int = 0) -> dict:
    if not paths:
        log.info(f"{label}: no measured paths. engine/excursions.py has not run "
                 f"for this population, or signal_excursions is not applied.")
        return {}
    log.info("")
    log.info(f"━━ {label} ━━ {len(paths)} measured path(s), "
             f"{sum(len(v) for v in paths.values())} bar(s)"
             + (f", horizon {horizon}d" if horizon else ""))

    mfes = sorted(max((float(r.get("mfe_r") or 0) for r in p), default=0.0)
                  for p in paths.values())
    maes = sorted(max((float(r.get("mae_r") or 0) for r in p), default=0.0)
                  for p in paths.values())
    def q(a, f): return a[min(len(a) - 1, int(f * len(a)))] if a else 0.0
    log.info(f"   MFE reached:  median {q(mfes,.5):+.2f}R   "
             f"p75 {q(mfes,.75):+.2f}R   p90 {q(mfes,.9):+.2f}R   "
             f"max {mfes[-1]:+.2f}R")
    log.info(f"   MAE suffered: median {q(maes,.5):.2f}R   "
             f"p75 {q(maes,.75):.2f}R   p90 {q(maes,.9):.2f}R   "
             f"max {maes[-1]:.2f}R")
    beyond = sum(1 for m in maes if m > 1.0)
    log.info(f"   went further against the trade than the stop: "
             f"{beyond}/{len(maes)} ({100*beyond//max(1,len(maes))}%) — "
             f"these fill below -1.00R with slippage")

    out = {}
    for rt in R_TARGETS:
        c = Counter()
        for p in paths.values():
            c[verdict(p, rt, horizon)[0]] += 1
        exp_lo = (c["WIN"] * rt - c["LOSS"]) / max(1, c["WIN"] + c["LOSS"] + c["AMBIGUOUS"])
        exp_hi = ((c["WIN"] + c["AMBIGUOUS"]) * rt - c["LOSS"]) / max(1, c["WIN"] + c["LOSS"] + c["AMBIGUOUS"])
        breakeven = 1.0 / (1.0 + rt)
        log.info(f"   at {rt:.1f}R: hit {_band(c):38} "
                 f"| open {c['OPEN']:4} amb {c['AMBIGUOUS']:4} "
                 f"| E {exp_lo:+.3f}R to {exp_hi:+.3f}R "
                 f"| breakeven needs {100*breakeven:.1f}%")
        out[rt] = c
    return out


def by_group(paths: dict, meta: dict, field: str, r_target: float,
             min_n: int = 30) -> None:
    """Split the hit rate by one field of the originating row. Groups under
    min_n are pooled into one line rather than printed as noise -- a 3-of-7
    'hit rate' invites exactly the reading it cannot support."""
    groups = defaultdict(Counter)
    for key, p in paths.items():
        g = str((meta.get(key) or {}).get(field) or "—")
        groups[g][verdict(p, r_target)[0]] += 1
    small = Counter()
    log.info(f"   by {field} at {r_target:.1f}R:")
    for g, c in sorted(groups.items(),
                       key=lambda kv: -(kv[1]["WIN"] + kv[1]["LOSS"] + kv[1]["AMBIGUOUS"])):
        dec = c["WIN"] + c["LOSS"] + c["AMBIGUOUS"]
        if dec < min_n:
            small.update(c)
            continue
        log.info(f"      {g[:34]:34} {_band(c)}")
    if small["WIN"] + small["LOSS"] + small["AMBIGUOUS"]:
        log.info(f"      {'(groups under ' + str(min_n) + ', pooled)':34} "
                 f"{_band(small)}")


def fetch_meta(source: str) -> dict:
    """The originating rows, keyed like the paths, for the splits."""
    if source == "detection":
        params = ("select=signal_date,symbol,direction,session,setup_name,rvol,"
                  "signal_time&order=signal_date.desc")
        rows = _get_all("live_signals", params)
    else:
        params = ("select=signal_date,symbol,direction,setup_name,grade,regime,"
                  "sector&order=signal_date.desc")
        rows = _get_all("signals", params)
    return {(str(x["signal_date"])[:10], x["symbol"],
             str(x["direction"]).upper()): x for x in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="YYYY-MM-DD lower bound")
    ap.add_argument("--horizon", type=int, default=0,
                    help="stop the walk after N sessions (0 = the full 20)")
    ap.add_argument("--target", type=float, default=1.5,
                    help="R target used for the per-group splits")
    a = ap.parse_args()

    if not SUPABASE_KEY:
        log.error("SUPABASE_SERVICE_KEY is not set")
        return 2

    try:
        det = fetch_paths("detection", since=a.since)
        sig = fetch_paths("signal", since=a.since)
    except Exception as e:
        log.error(f"could not read signal_excursions: {e}")
        return 1

    if not det and not sig:
        # NOT a zero result. Reporting "0% hit rate" from an empty table is the
        # failure mode this whole file is a response to.
        log.error("signal_excursions holds no paths at all. Nothing is measured "
                  "yet — run engine/excursions.py, and check that "
                  "migrations/PENDING_signal_excursions.sql is applied. "
                  "Refusing to report a hypothesis over no data.")
        return 1

    report(det, "DETECTIONS  (intraday, filled at the detection price)",
           horizon=a.horizon)
    if det:
        try:
            meta = fetch_meta("detection")
            by_group(det, meta, "setup_name", a.target)
            by_group(det, meta, "direction", a.target)
        except Exception as e:
            log.warning(f"group splits unavailable: {e}")

    report(sig, "SIGNALS  (EOD, filled at the next open)", horizon=a.horizon)

    log.info("")
    log.info("CAVEATS, which belong next to every number above:")
    log.info("  • The two populations have different fills and are not comparable")
    log.info("    as a like-for-like win rate. A detection is filled at the LTP")
    log.info("    that triggered it, intraday, on the detection day; a signal is")
    log.info("    filled at the next open, which is a gap away.")
    log.info("  • Neither fill is a broker fill. There is no slippage and no")
    log.info("    partial anywhere in this, and the MAE>1R share above is the")
    log.info("    measure of how much that flatters the loss side.")
    log.info("  • AMBIGUOUS bars are a RANGE, not a number. The lower bound")
    log.info("    assigns every tie to the stop, the upper to the target.")
    log.info("  • Nothing here is wired to a threshold and nothing should be")
    log.info("    until a span longer than seven weeks has accumulated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
