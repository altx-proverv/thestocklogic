"""
Does the negative class actually represent the population it is drawn from?

A control group that is wrong is worse than no control group, because the
membership test will still produce a number. Four things have to hold:

  1. It draws from the REJECTED rows, never from the qualifying ones.
  2. k=10 controls per case, which is where the precision curve flattens.
  3. The strata separate near misses from total failures. disqualify_reason
     cannot do this: it is the FIRST gate failed and is confounded with gate
     position -- the last gate's rejects are all near misses by construction.
  4. The same date samples identically on a re-run. The sample IS the data, so
     it must be a function of the date, not of when the job fired.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from engine import reject_sample as RS

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


REASONS = ["warmup", "no_trade_zone", "very_low_volume", "atr_too_high",
           "bear_regime_no_reversal", "adq_ranging_market",
           "weekly_structure_misaligned", "no_smc_signal", "no_recent_bos_choch"]


def population(n_rej=460, n_qual=4, seed=5):
    """A night, shaped like a real one: ~460 rejects against a handful of passes,
    with the reason distribution skewed the way the funnel log shows."""
    rng = np.random.default_rng(seed)
    w = np.array([0.03, 0.03, 0.09, 0.01, 0.04, 0.14, 0.05, 0.02, 0.59])
    rows = []
    for i in range(n_rej):
        r = rng.choice(REASONS, p=w / w.sum())
        pos = REASONS.index(r)
        # gates_failed can only be 1 for the last gate; earlier gates vary
        gf = 1 if pos == len(REASONS) - 1 else int(rng.integers(1, 9 - pos + 1))
        rows.append(dict(symbol=f"R{i}", direction=rng.choice(["long", "short"]),
                         disqualified=True, disqualify_reason=r,
                         gates_failed=gf, gates_failed_mask=1 << pos,
                         rvol=rng.uniform(0.2, 3), atr_pct=rng.uniform(0.5, 9),
                         close=rng.uniform(50, 5000)))
    for i in range(n_qual):
        rows.append(dict(symbol=f"Q{i}", direction="long", disqualified=False,
                         disqualify_reason="", gates_failed=0, gates_failed_mask=0,
                         rvol=1.5, atr_pct=2.0, close=100.0))
    d = pd.DataFrame(rows)
    d["date"] = pd.Timestamp("2026-10-02")
    return d


DAY = "2026-10-02"
pop = population()

print("\n── it samples the rejects, never the passes ──")
out = RS.draw(pop, DAY, k=10)
check("nothing qualifying is in the sample",
      not out["symbol"].str.startswith("Q").any(),
      f"{int(out['symbol'].str.startswith('Q').sum())} qualifying rows leaked in")
check("every sampled row failed at least one gate", bool((out["gates_failed"] >= 1).all()))
check("every sampled row is flagged disqualified", bool((out["disqualified"] == True).all()))

print("\n── k=10 is the ratio, and it is the default ──")
check("CONTROLS_PER_CASE is 10", RS.CONTROLS_PER_CASE == 10, str(RS.CONTROLS_PER_CASE))
n_qual = int((~pop["disqualified"]).sum())
check(f"{n_qual} cases -> about {n_qual*10} controls",
      abs(len(out) - n_qual * 10) <= len(out.groupby('stratum')),
      f"got {len(out)} for {n_qual} cases")
out5 = RS.draw(pop, DAY, k=5)
check("k=5 draws roughly half as many", len(out5) < len(out), f"{len(out5)} vs {len(out)}")
check("the sample is far smaller than the population",
      len(out) < 0.25 * int(pop['disqualified'].sum()),
      f"{len(out)} of {int(pop['disqualified'].sum())}")

print("\n── the strata separate a near miss from a total failure ──")
s = RS.stratify(pop[pop["disqualified"]])
check("stratum has three axes", all(x.count("|") == 2 for x in s.head(50)))
check("near and far both appear", "near" in "".join(s.unique()) and "far" in "".join(s.unique()))
# the confound this guards: reason alone cannot recover failure count
rej = pop[pop["disqualified"]]
late = rej[rej["disqualify_reason"] == "no_recent_bos_choch"]
early = rej[rej["disqualify_reason"] == "adq_ranging_market"]
check("the last gate's rejects are all near misses",
      bool((late["gates_failed"] == 1).all()))
check("an earlier gate's rejects are not", float((early["gates_failed"] == 1).mean()) < 0.9,
      f"{100*float((early['gates_failed'] == 1).mean()):.0f}% are")
check("so the stratum carries information the reason does not",
      len(s.unique()) > rej["disqualify_reason"].nunique(),
      f"{len(s.unique())} strata vs {rej['disqualify_reason'].nunique()} reasons")
near_frac = float((out["gates_failed"] <= 1).mean())
check("near misses are present in the drawn sample", near_frac > 0.05,
      f"{100*near_frac:.0f}%")
print(f"        {len(out)} drawn across {out['stratum'].nunique()} strata, "
      f"{100*near_frac:.0f}% near misses")

print("\n── every stratum present is represented ──")
drawn = set(out["stratum"]); allstrata = set(s)
check("no stratum is silently dropped", drawn == allstrata,
      f"{len(allstrata - drawn)} missing")
check("sample_weight recovers the population size",
      bool(np.allclose(out.groupby("stratum")["sample_weight"].first()
                       * out.groupby("stratum").size(),
                       out.groupby("stratum")["stratum_population"].first())))

print("\n── the same date samples identically ──")
a = RS.draw(pop, DAY, k=10)["symbol"].tolist()
b = RS.draw(pop, DAY, k=10)["symbol"].tolist()
check("two draws of the same date agree", a == b)
c = RS.draw(pop, "2026-10-03", k=10)["symbol"].tolist()
check("a different date draws differently", a != c)
check("the seed is a function of the date only",
      RS.seed_for(DAY) == RS.seed_for(DAY) and RS.seed_for(DAY) != RS.seed_for("2026-10-03"))

print("\n── it refuses to run on the wrong artifact ──")
import inspect
src = inspect.getsource(RS.load_scored)
# THE FILENAME MOVED to engine/paths.py, so asserting the literal is in this module
# is now asserting where a constant is typed rather than which file is read. 03b
# wrote all_rows_v2.parquet to one directory and this module read another; the fix
# was one shared definition, and the check follows it.
from engine.paths import ALL_ROWS, ALL_SCORES
check("the source is all_rows_v2, from the shared path",
      RS.ALL_ROWS == ALL_ROWS and ALL_ROWS.name == "all_rows_v2.parquet")
check("  and it is NOT the qualifying-only artifact",
      RS.ALL_ROWS != ALL_SCORES)
check("  and both point at the same directory 03b writes to",
      ALL_ROWS.parent == ALL_SCORES.parent)
check("it says why the qualifying artifact cannot be the control group",
      "NOT a control group" in src)
check("  and it no longer names a flag that does not exist",
      "--write-all-rows" not in src)
check("it requires the gate-audit columns",
      "gates_failed" in src and "raise SampleUnavailable" in src)

print("\n── the rows it writes ──")
rows = RS.to_rows(out, DAY)
check("one row per sampled stock-day", len(rows) == len(out))
r0 = rows[0]
for k_ in ("sample_date", "symbol", "direction", "gates_failed",
           "gates_failed_mask", "stratum", "sample_weight", "engine_sha"):
    check(f"  carries {k_}", k_ in r0)
check("direction is upper-cased for the unique key",
      all(r["direction"] in ("LONG", "SHORT") for r in rows))
check("NaN never reaches the wire",
      all(not (isinstance(v, float) and v != v) for r in rows for v in r.values()))
check("feature names match the qualifying side",
      "rvol" in RS.FEATURE_COLS and "atr_pct" in RS.FEATURE_COLS
      and "recent_bos_choch" in RS.FEATURE_COLS)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All reject-sample checks passed.")
