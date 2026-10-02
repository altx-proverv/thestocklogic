"""
THE STOCK LOGIC — the negative class.

THE QUESTION THIS EXISTS TO ANSWER
----------------------------------
Every analysis so far has compared winners against losers: signals that qualified
and then did or did not work. The 2026-09-30 feature study found nothing
separating them, and the 2026-10-02 exit study found no exit rule that rescues
them. Neither could ask the prior question -- whether qualifying at all means
anything -- because the rejected stock-days are not persisted anywhere.

So the engine's central premise has never been tested. Twelve gates reject about
460 symbol-days a night to pass roughly 3.6, and nobody knows whether those 3.6
behave any differently from 3.6 drawn at random from the 460.

WHAT MAKES THIS DIFFERENT FROM A FEATURE STUDY. The winner/loser question is
badly powered -- the win class is ~19% of 293 resolved signals and grows at about
71/month. The membership question is well powered, because the negative class is
128x larger than the positive one and costs nothing to enlarge: at k=10 controls
per case, three months of sampling detects AUC 0.56. That is the best-powered
question available on this record.

WHY A SAMPLE AND NOT EVERYTHING
-------------------------------
Not for storage. The "370k rejected stock-days" is the whole 788-day backfill that
03b rescores on every run -- one night's NEW rejects are 232 symbols x 2
directions = 464 rows, which is 70 MB/year stored whole. It is affordable.

It is that a 128:1 control ratio buys almost nothing. The standard error of a
two-group difference scales as sqrt(1/n1 + 1/n2); with n2 = k*n1 that is
sqrt(1/n1) * sqrt(1 + 1/k):

      k=1    1.414x the width of infinite controls
      k=5    1.095x
      k=10   1.049x        <- 4.9% wider, 1/13th the rows
      k=20   1.025x
      k=128  1.004x

Past k=10 the curve is flat. k=10 it is.

HOW THE STRATA WERE CHOSEN
--------------------------
Stratifying on disqualify_reason alone is a trap. Each gate is masked on
(~disqualified), so the reason is the FIRST gate a row failed, and failure count
is confounded with gate POSITION: the last gate's rejects have necessarily passed
the other eight and are all near misses, while the first gate's rejects fail a
median of three. Sampling on reason alone therefore buys a near-miss rate that is
an artefact of the funnel's ordering.

So the strata are (first failed gate) x (near miss or not) x direction, and
gates_failed / gates_failed_mask are carried on every row so the distinction is
recoverable rather than inferred. A near miss is gates_failed == 1: a row whose
features were acceptable everywhere except one place.

WHAT THIS CANNOT ANSWER. Whether a rejected day would have WON -- not from
membership alone. That needs the forward path, which engine/excursions.py walks
for source='reject' rows using the same 20-day window and the same conventions as
signals and detections. Sampling without the path would only describe what the
filters key on, which is already known from having written them.
"""

import os
import sys
import json
import logging
import argparse
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from engine.provenance import engine_sha

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("REJECT-SAMPLE")

SUPABASE_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SIGNALS_DIR = Path("data/signals")

CONTROLS_PER_CASE = 10          # see the note above; past 10 the curve is flat
MIN_PER_STRATUM = 1             # every stratum present gets at least one row
PAGE = 1000

# The features carried on a sampled reject. Deliberately the SAME names the
# qualifying side records in signals_features, because the whole point is a
# like-for-like comparison and a column that exists on only one side of it is
# worse than useless -- it looks comparable and is not.
FEATURE_COLS = [
    "rvol", "atr_pct", "adx", "adx_ranging", "rsi", "delivery_pct",
    "market_regime", "sector", "sector_bias", "weekly_bullish", "weekly_bearish",
    "near_demand_ob", "near_supply_ob", "price_in_bull_fvg", "price_in_bear_fvg",
    "bos_bull", "bos_bear", "choch_bull", "choch_bear",
    "bull_liq_sweep", "bear_liq_sweep", "recent_bos_choch",
    "active_zone_high", "active_zone_low", "active_zone_source",
    "zone_age_days", "zone_dist_pct", "structure_trend",
    "accumulation_score", "is_accumulation", "rs_5d", "rs_20d",
    "advance_count", "decline_count", "nifty_close",
    "close", "open", "high", "low", "volume",
]


class SampleUnavailable(Exception):
    """The scored frame could not be read. No sample rather than a wrong one."""


def _headers() -> dict:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _clean(v):
    """NaN, inf and '' all become NULL. Postgres numerics reject inf, and a
    silently-coerced 0.0 is a value that looks measured."""
    if v is None:
        return None
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, str):
        return v.strip() or None
    if pd.isna(v):
        return None
    return v


def seed_for(day: str) -> int:
    """A deterministic per-day seed, so re-running a date reproduces its sample
    exactly. A wall-clock seed would make the same night's sample differ between
    a first run and a re-run, and the sample IS the data -- it has to be a
    function of the date, not of when the job happened to fire."""
    return int(hashlib.sha256(f"reject-sample:{day}".encode()).hexdigest()[:8], 16)


def load_scored(day: str = None) -> pd.DataFrame:
    """The full scored frame for one date, rejects included.

    all_scores_v2.parquet holds only the QUALIFYING rows, so it cannot be the
    source here. The rejects exist only inside 03b's own frame, which is why this
    reads the all-rows artifact and fails loudly when it is absent rather than
    sampling from the qualifying set and calling it a control group.
    """
    path = SIGNALS_DIR / "all_rows_v2.parquet"
    if not path.exists():
        raise SampleUnavailable(
            f"{path} does not exist. 03b must be run with --write-all-rows for "
            f"the reject population to be sampleable; all_scores_v2.parquet holds "
            f"only the rows that qualified and is not a control group.")
    df = pd.read_parquet(path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        if day:
            df = df[df["date"].dt.strftime("%Y-%m-%d") == day]
        else:
            day = df["date"].max().strftime("%Y-%m-%d")
            df = df[df["date"].dt.strftime("%Y-%m-%d") == day]
    if df.empty:
        raise SampleUnavailable(f"no rows for {day} in {path}")
    for c in ("disqualified", "gates_failed", "gates_failed_mask",
              "disqualify_reason"):
        if c not in df.columns:
            raise SampleUnavailable(
                f"{path} has no `{c}`. The gate audit in 03b has not run or is "
                f"an older build -- sampling without it would record a negative "
                f"class whose near misses cannot be told from total failures.")
    return df, day


def stratify(df: pd.DataFrame) -> pd.Series:
    """(first failed gate, near miss?, direction) per row.

    Near miss is gates_failed == 1 and is a separate axis from the reason because
    the two are confounded by gate position -- the last gate's rejects are all
    near misses by construction. Keeping them separate is what stops the sample
    inheriting the funnel's ordering as if it were a property of the market.
    """
    reason = df["disqualify_reason"].fillna("").astype(str).replace("", "unknown")
    near = np.where(df["gates_failed"].fillna(0).astype(int) <= 1, "near", "far")
    direction = df.get("direction", pd.Series("long", index=df.index)).astype(str)
    return pd.Series([f"{r}|{n}|{d}" for r, n, d in zip(reason, near, direction)],
                     index=df.index)


def draw(df: pd.DataFrame, day: str, k: int = CONTROLS_PER_CASE) -> pd.DataFrame:
    """k controls per qualifying case, allocated across strata by size.

    PROPORTIONAL, NOT EQUAL. An equal draw per stratum would over-represent the
    rare gates by a factor of fifty and make the sample's marginal distribution
    nothing like the population's, which matters because the comparison of
    interest is against the population a signal was actually drawn from. Rare
    strata still get MIN_PER_STRATUM so they are present at all -- that is a
    deliberate, documented departure from proportionality and the reason the
    stratum and its population size are stored on every row, so a consumer can
    reweight.
    """
    rejects = df[df["disqualified"] == True]
    cases = int((~df["disqualified"]).sum())
    if rejects.empty:
        return rejects.head(0)
    want = max(k, cases * k)
    strata = stratify(rejects)
    sizes = strata.value_counts()
    rng = np.random.default_rng(seed_for(day))

    # proportional allocation, floor, then hand the remainder to the largest
    alloc = {}
    for s, n in sizes.items():
        alloc[s] = max(MIN_PER_STRATUM, int(np.floor(want * n / len(rejects))))
    over = sum(alloc.values()) - want
    if over > 0:
        for s in sizes.index:                    # largest first
            if over <= 0:
                break
            can = alloc[s] - MIN_PER_STRATUM
            cut = min(can, over)
            alloc[s] -= cut
            over -= cut

    picks = []
    for s, n in alloc.items():
        idx = strata[strata == s].index
        take = min(n, len(idx))
        picks.extend(rng.choice(idx.to_numpy(), size=take, replace=False))
    out = rejects.loc[sorted(picks)].copy()
    out["stratum"] = stratify(out)
    out["stratum_population"] = out["stratum"].map(sizes).astype(int)
    out["sample_weight"] = out["stratum_population"] / out.groupby("stratum")["stratum"].transform("size")

    # SAY SO WHEN THE SAMPLE IS COVERAGE-DRIVEN RATHER THAN PROPORTIONAL. With
    # ~3.6 qualifying cases a night, k=10 is ~36 rows spread over ~31 strata, so
    # MIN_PER_STRATUM accounts for nearly all of the allocation and proportional
    # weighting barely operates. That is acceptable -- across sixty nights each
    # stratum accumulates sixty rows and sample_weight recovers the marginals --
    # but it is NOT true that a single night's sample mirrors that night's
    # population, and anyone analysing one date needs to know which it is.
    at_min = sum(1 for st, n in alloc.items() if n <= MIN_PER_STRATUM)
    log.info(f"{day}: {cases} qualifying, {len(rejects)} rejects, "
             f"{len(out)} sampled across {len(alloc)} strata (k={k})")
    if len(alloc) and at_min / len(alloc) > 0.5:
        log.warning(
            f"{at_min}/{len(alloc)} strata drew only the MIN_PER_STRATUM floor — "
            f"tonight's sample is coverage-driven, not proportional. Rare gates "
            f"are over-represented by design; apply sample_weight before reading "
            f"any marginal distribution off a single date.")
    return out


def to_rows(out: pd.DataFrame, day: str) -> list:
    sha = engine_sha()
    rows = []
    for _, r in out.iterrows():
        row = {
            "sample_date": day,
            "symbol": _clean(r.get("symbol")),
            "direction": str(r.get("direction", "long")).upper(),
            "disqualify_reason": _clean(r.get("disqualify_reason")),
            "gates_failed": int(r.get("gates_failed") or 0),
            "gates_failed_mask": int(r.get("gates_failed_mask") or 0),
            "stratum": _clean(r.get("stratum")),
            "stratum_population": int(r.get("stratum_population") or 0),
            "sample_weight": _clean(r.get("sample_weight")),
            "engine_sha": sha,
        }
        for c in FEATURE_COLS:
            if c in out.columns:
                row[c] = _clean(r.get(c))
        rows.append(row)
    return rows


def write(rows: list) -> int:
    if not rows:
        return 0
    n = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/reject_sample"
            f"?on_conflict=sample_date,symbol,direction",
            headers={**_headers(),
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=chunk, timeout=90)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"write failed: HTTP {resp.status_code} "
                               f"{resp.text[:300]}")
        n += len(chunk)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default: latest)")
    ap.add_argument("--k", type=int, default=CONTROLS_PER_CASE)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.dry_run and not SUPABASE_KEY:
        log.error("SUPABASE_SERVICE_KEY is not set")
        return 2
    try:
        df, day = load_scored(a.day)
    except SampleUnavailable as e:
        log.error(str(e))
        return 1

    out = draw(df, day, k=a.k)
    if out.empty:
        log.warning(f"{day}: nothing to sample — no disqualified rows")
        return 0
    rows = to_rows(out, day)
    log.info(f"  near misses in the sample: "
             f"{sum(1 for r in rows if r['gates_failed'] <= 1)}/{len(rows)}")
    by = {}
    for r in rows:
        by[r["disqualify_reason"]] = by.get(r["disqualify_reason"], 0) + 1
    for k_, v in sorted(by.items(), key=lambda kv: -kv[1])[:12]:
        log.info(f"    {k_:38} {v}")
    if a.dry_run:
        log.info(f"DRY RUN — {len(rows)} row(s) not written")
        print(json.dumps(rows[0], indent=1, default=str))
        return 0
    n = write(rows)
    log.info(f"wrote {n} row(s) to reject_sample")
    return 0


if __name__ == "__main__":
    sys.exit(main())
