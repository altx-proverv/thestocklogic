"""
Does gates_failed say anything disqualify_reason could not, and did extracting
the gate conditions change the funnel?

WHY gates_failed EXISTS. Every disqualifier in 03b is masked on (~disqualified),
so disqualify_reason is the FIRST gate a row failed and carries no information
about the rest. A row rejected at `warmup` may also fail nine other gates; a row
that fails only `no_recent_bos_choch` is a near miss. For "what separates a setup
from a non-setup" the near misses are the only informative negatives, and
short-circuited they are indistinguishable from total failures.

WHY THIS TEST IS AN EQUIVALENCE PROOF. The conditions were inline and are now
declared in gate_conditions(). The pre-refactor block is replayed verbatim from a
captured copy against the same random frames, and the two must agree on
`disqualified` and `disqualify_reason` for every row. The funnel tally is a
published number; a refactor that moved it would be a silent regression.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import importlib.util

spec = importlib.util.spec_from_file_location("s3b", ROOT / "engine/03b_score.py")
s3b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3b)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


COLS = ["is_warmup", "no_trade_zone", "rvol", "atr_pct", "market_regime",
        "bull_liq_sweep", "choch_bull", "adx_ranging", "weekly_bullish",
        "weekly_bearish", "near_demand_ob", "price_in_bull_fvg", "bos_bull",
        "near_supply_ob", "price_in_bear_fvg", "bos_bear", "bear_liq_sweep",
        "recent_bos_choch"]


def synth(n, seed, direction="long"):
    rng = np.random.default_rng(seed)
    d = pd.DataFrame({
        "symbol": [f"S{i}" for i in range(n)],
        "direction": direction,
        "is_warmup": rng.random(n) < 0.08,
        "no_trade_zone": (rng.random(n) < 0.10).astype(int),
        "rvol": rng.uniform(0.1, 3.0, n),
        "atr_pct": rng.uniform(0.5, 12.0, n),
        "market_regime": rng.choice(["bull", "bear", "mixed", "sideways"], n),
        "adx_ranging": (rng.random(n) < 0.3).astype(int),
        "weekly_bullish": (rng.random(n) < 0.4).astype(int),
        "weekly_bearish": (rng.random(n) < 0.4).astype(int),
        "recent_bos_choch": (rng.random(n) < 0.5).astype(int),
    })
    for c in ("bull_liq_sweep", "choch_bull", "near_demand_ob", "price_in_bull_fvg",
              "bos_bull", "near_supply_ob", "price_in_bear_fvg", "bos_bear",
              "bear_liq_sweep"):
        d[c] = (rng.random(n) < 0.25).astype(int)
    return d


# In the repo, not in a scratch directory: an equivalence proof that only runs on
# the machine the refactor happened on proves nothing to anyone else.
OLD_SRC = ROOT / "tests/fixtures/gates_pre_20261002.py"


def run_old(df):
    """The pre-refactor block, executed verbatim."""
    df = df.copy()
    ns = {"df": df, "pd": pd, "np": np}
    exec(compile(OLD_SRC.read_text(), "old_gates", "exec"), ns)
    return ns["df"]


def run_new(df):
    df = df.copy()
    conds, _ = s3b.gate_conditions(df)
    df["disqualified"] = False
    df["disqualify_reason"] = ""
    for name, cond in conds.items():
        m = (~df["disqualified"]) & cond
        df.loc[m, "disqualified"] = True
        df.loc[m, "disqualify_reason"] = name
    failed = np.zeros(len(df), dtype=np.int16)
    bits = np.zeros(len(df), dtype=np.int32)
    for i, (name, cond) in enumerate(conds.items()):
        c = np.asarray(cond, dtype=bool)
        failed += c
        bits |= (c.astype(np.int32) << i)
    df["gates_failed"] = failed
    df["gates_failed_mask"] = bits
    return df


print("\n── the refactor changed nothing (equivalence vs the captured old block) ──")
if not OLD_SRC.exists():
    print("  SKIP  the captured pre-refactor block is not on this machine")
else:
    for seed, direction in ((1, "long"), (2, "short"), (3, "long"), (4, "short")):
        a = run_old(synth(4000, seed, direction))
        b = run_new(synth(4000, seed, direction))
        same_flag = bool((a["disqualified"].values == b["disqualified"].values).all())
        same_why = bool((a["disqualify_reason"].values == b["disqualify_reason"].values).all())
        check(f"seed {seed} {direction}: same disqualified flag", same_flag)
        check(f"seed {seed} {direction}: same disqualify_reason", same_why,
              f"{(a['disqualify_reason'].values != b['disqualify_reason'].values).sum()} differ")

print("\n── gates_failed is not short-circuited ──")
df = run_new(synth(6000, 11, "long"))
dq = df[df["disqualified"]]
check("every disqualified row fails at least one gate", bool((dq["gates_failed"] >= 1).all()))
# NOT "qualifying rows fail no gates" -- that is false in production and this
# assertion used to claim it. The accumulation screen runs AFTER the gate block
# and can recover a disqualified row, clearing `disqualified` and
# `disqualify_reason` while gates_failed keeps its count. So a qualifying row can
# carry gates_failed >= 1, and that is the intended meaning: gates_failed says
# "this row's features failed a gate", NOT "this row was rejected".
#
# It matters for the membership test. Cases must be defined by `qualifies`, never
# by gates_failed == 0, or every screen-recovered row lands in the control group
# and the comparison measures the screen instead of the filters.
check("rows that failed nothing are all qualifying",
      bool((~df.loc[df["gates_failed"] == 0, "disqualified"]).all()))
check("some rows fail more than one gate", int((df["gates_failed"] > 1).sum()) > 0,
      f"max {int(df['gates_failed'].max())}")
check("gates_failed exceeds 1 for most rejects — the point of the column",
      float((dq["gates_failed"] > 1).mean()) > 0.3,
      f"{100*float((dq['gates_failed'] > 1).mean()):.0f}%")
nm = dq[dq["gates_failed"] == 1]
check("and near misses are identifiable at all", len(nm) > 0, f"{len(nm)} rows")
print(f"        {len(dq)} rejects: {len(nm)} fail exactly one gate, "
      f"{int((dq['gates_failed'] >= 3).sum())} fail three or more")

print("\n── a near miss is NOT distinguishable from disqualify_reason alone ──")
# The first version of this asserted that EVERY reason spans several failure
# counts, and no_recent_bos_choch failed it with a span of exactly 1. That is
# correct behaviour, not a defect: it is the LAST gate, so any row reaching it has
# already passed the other eight and gates_failed is necessarily 1.
#
# Which is the sharper form of the argument. Failure count is confounded with GATE
# POSITION: the last gate's rejects are all near misses by construction, the
# first gate's are mostly not. Stratifying a sample on disqualify_reason alone
# therefore buys a near-miss rate that is an artefact of where the gate sits in
# the funnel -- so the sampler stratifies on gates_failed as well.
early = dq[dq["disqualify_reason"] == "warmup"]
late = dq[dq["disqualify_reason"] == "no_recent_bos_choch"]
check("the FIRST gate's rejects span many failure counts",
      int(early["gates_failed"].nunique()) >= 3,
      f"{int(early['gates_failed'].nunique())}")
check("the FIRST gate's rejects are mostly not near misses",
      float((early["gates_failed"] == 1).mean()) < 0.5,
      f"{100*float((early['gates_failed'] == 1).mean()):.0f}% are")
check("the LAST gate's rejects are all near misses, by construction",
      bool((late["gates_failed"] == 1).all()))
for nm_, g in (("warmup (gate 1)", early), ("no_recent_bos_choch (gate 9)", late)):
    if len(g):
        print(f"        {nm_}: {len(g)} rows, gates_failed "
              f"{int(g['gates_failed'].min())}..{int(g['gates_failed'].max())}, "
              f"median {int(g['gates_failed'].median())}")
check("so failure count is NOT recoverable from the reason",
      int(early["gates_failed"].median()) != int(late["gates_failed"].median()))

print("\n── the bitmask agrees with the count, bit for bit ──")
order = s3b.GATE_ORDER
check("GATE_ORDER has 9 entries", len(order) == 9, f"{len(order)}")
popcount = np.array([bin(int(x)).count("1") for x in df["gates_failed_mask"]])
check("popcount(mask) == gates_failed", bool((popcount == df["gates_failed"].values).all()))
# the first set bit must be the reported reason
first_bit = [(int(m) & -int(m)).bit_length() - 1 if m else -1
             for m in df["gates_failed_mask"]]
named = [order[i] if i >= 0 else "" for i in first_bit]
check("lowest set bit names the reported reason",
      bool((np.array(named) == df["disqualify_reason"].values).all()),
      f"{int((np.array(named) != df['disqualify_reason'].values).sum())} differ")
check("the zone-entry gate has its own bit above the nine",
      s3b.GATE_ZONE_ENTRY_BIT == 9)

print("\n── the gate order is pinned ──")
# the mask is stored in the database; reordering GATE_ORDER silently reinterprets
# every historical row
check("order is exactly the published funnel order",
      order == ("warmup", "no_trade_zone", "very_low_volume", "atr_too_high",
                "bear_regime_no_reversal", "adq_ranging_market",
                "weekly_structure_misaligned", "no_smc_signal",
                "no_recent_bos_choch"), str(order))
check("gate_conditions asserts its own order",
      "gate order drifted" in __import__("inspect").getsource(s3b.gate_conditions))

print("\n── GATE 10 THROUGH THE REAL FUNCTIONS, NOT A REIMPLEMENTATION ──")
# THIS SECTION EXISTS BECAUSE THE SUITE MISSED A CRASH. run_new() above rebuilds
# the mask with numpy arrays and passed; the production path builds it from a
# pandas COLUMN inside compute_trade_levels_vectorized, and `series << 9` raises
# TypeError because pandas implements & | ^ on a Series and not << or >>. 03b died
# on the box with nothing qualifying and no parquet written.
#
# A second defect was sitting behind it: gates_failed and gates_failed_mask were
# missing from process_direction's column-propagation list, so even once the shift
# worked, gate 10 was computed and then discarded -- three lines below a comment
# warning about exactly that. A row rejected by zone validation is the nearest miss
# the funnel produces, and the sampler would have recorded it as failing nothing.
#
# So: call the real functions. A test that reimplements the thing it is checking
# tests the reimplementation.
FAM = ["active_demand_ob_high", "active_demand_ob_low",
       "active_bull_fvg_high", "active_bull_fvg_low",
       "active_supply_ob_high", "active_supply_ob_low",
       "active_bear_fvg_high", "active_bear_fvg_low"]


def real_frame():
    """Two longs: one with a usable demand zone and swing stop, one whose zone is
    absent so zone validation rejects it. Both already passed the nine feature
    gates, which is the only way a row reaches this stage."""
    d = pd.DataFrame({
        "symbol": ["GOOD", "BADZONE"], "direction": ["long", "long"],
        "date": [pd.Timestamp("2026-10-02")] * 2,
        "close": [98.0, 98.0], "atr": [2.0, 2.0],
        "last_swing_low": [95.0, np.nan], "last_swing_high": [105.0, 105.0],
        "active_zone_high": [99.0, np.nan], "active_zone_low": [97.0, np.nan],
        "active_zone_source": ["demand_ob", "demand_ob"],
        "gates_failed": [0, 0], "gates_failed_mask": [0, 0],
        "disqualified": [False, False], "disqualify_reason": ["", ""],
        "qualifies": [True, True],
    })
    for c in FAM:
        d[c] = np.nan
    d.loc[0, "active_demand_ob_high"] = 99.0
    d.loc[0, "active_demand_ob_low"] = 97.0
    return d


try:
    lv = s3b.compute_trade_levels_vectorized(real_frame())
    crashed = None
except Exception as e:
    lv, crashed = None, f"{type(e).__name__}: {e}"
check("compute_trade_levels_vectorized does not raise", crashed is None, crashed or "")

if lv is not None:
    GB = 1 << s3b.GATE_ZONE_ENTRY_BIT
    good = lv[lv["symbol"] == "GOOD"].iloc[0]
    bad = lv[lv["symbol"] == "BADZONE"].iloc[0]
    check("the valid-zone row passes zone validation", bool(good["entry_valid"]),
          str(good.get("reject_reason")))
    check("  and carries no gate-10 bit", not (int(good["gates_failed_mask"]) & GB))
    check("  and still fails nothing", int(good["gates_failed"]) == 0,
          str(int(good["gates_failed"])))
    check("the broken-zone row is rejected", not bool(bad["entry_valid"]))
    check("  and carries the gate-10 bit", bool(int(bad["gates_failed_mask"]) & GB),
          f"mask {int(bad['gates_failed_mask'])}")
    check("  and is a NEAR MISS — exactly one gate", int(bad["gates_failed"]) == 1,
          str(int(bad["gates_failed"])))
    check("  popcount still matches the count",
          bin(int(bad["gates_failed_mask"])).count("1") == int(bad["gates_failed"]))
    check("the mask stays an integer dtype, not object",
          str(lv["gates_failed_mask"].dtype).startswith("int"),
          str(lv["gates_failed_mask"].dtype))

print("\n── and it survives process_direction's column propagation ──")
# The list at process_direction copies named columns off the levels frame and
# drops everything else. Gate 10 is computed downstream of it, so omission here is
# silent: the value is correct inside the function and gone outside it.
import inspect
pd_src = inspect.getsource(s3b.process_direction)
check("gates_failed is in the propagation list", '"gates_failed"' in pd_src)
check("gates_failed_mask is in the propagation list", '"gates_failed_mask"' in pd_src)

print("\n── a screen-recovered row is a CASE, not a control ──")
# process_direction runs the accumulation screen AFTER the gate block, and the
# screen can un-disqualify a row whose features failed a gate: it clears
# `disqualified` and `disqualify_reason` while gates_failed keeps its count. So a
# qualifying row can carry gates_failed >= 1 at the same time, and that is the
# intended meaning -- gates_failed says "this row's features failed a gate", not
# "this row was rejected".
#
# It matters for the membership test. Defining the control group as
# gates_failed >= 1 instead of disqualified == True would put every recovered row
# on the wrong side of the comparison, and the test would measure the screen
# rather than the filters.
rec = pd.DataFrame({
    "symbol": ["RECOVERED"], "direction": ["long"],
    "date": [pd.Timestamp("2026-10-02")], "close": [98.0], "open": [98.0],
    "high": [99.0], "low": [97.0], "volume": [1e6], "atr": [2.0],
    "atr_pct": [2.0], "rvol": [1.5], "rsi": [55.0], "is_warmup": [False],
    "no_trade_zone": [0], "market_regime": ["bull"], "adx": [25.0],
    "adx_ranging": [1],                           # fails gate 6
    "weekly_bullish": [1], "weekly_bearish": [0], "near_demand_ob": [1],
    "price_in_bull_fvg": [0], "bos_bull": [1], "choch_bull": [0],
    "bull_liq_sweep": [0], "near_supply_ob": [0], "price_in_bear_fvg": [0],
    "bos_bear": [0], "bear_liq_sweep": [0], "recent_bos_choch": [1],
    "last_swing_low": [95.0], "last_swing_high": [105.0],
    "active_zone_high": [99.0], "active_zone_low": [97.0],
    "active_zone_source": ["demand_ob"],
})
for c in FAM:
    rec[c] = np.nan
rec["active_demand_ob_high"] = 99.0
rec["active_demand_ob_low"] = 97.0
rr = s3b.process_direction(rec.copy(), "long", {}, {}).iloc[0]
check("the screen can recover a gate failure", bool(rr["qualifies"]),
      f"reason {rr['disqualify_reason']!r}")
check("  and gates_failed still records the failure", int(rr["gates_failed"]) >= 1,
      str(int(rr["gates_failed"])))
check("  so gates_failed==0 is NOT the definition of a case",
      bool(rr["qualifies"]) and int(rr["gates_failed"]) > 0)
from engine.reject_sample import draw as _draw
check("the sampler defines controls by `disqualified`, not by gates_failed",
      'df["disqualified"] == True' in inspect.getsource(_draw))

print("\n── PANDAS 3: A MIXED MASK, WHICH IS THE ONLY SHAPE THAT FAILS ──")
# THE SECOND BOX CRASH. score_vectorized built gates_failed as int16 and
# compute_trade_levels_vectorized rebuilt it as int64; process_direction then
# assigned those int64 values back through df.loc[qual_mask, col]. pandas 2 warns
# and silently widens. pandas 3 -- which the box runs and this venv does not --
# raises TypeError: Invalid value '[4 4 4 ... 4 1 1]' for dtype 'int16'.
#
# WHY THE EARLIER TEST STILL PASSED. The single-row fixture above has an all-True
# mask, and pandas treats a full-coverage .loc assignment as a column REPLACEMENT,
# which is allowed to change dtype and never warns. Only a PARTIAL mask takes the
# in-place path that objects. So the frame below must have some rows qualify and
# some not -- the one property the previous fixtures lacked, and the one every
# real batch has.
#
# Under PYTHONWARNINGS=error::FutureWarning (which tests/run_all.py now sets) this
# section fails against the pre-fix code on pandas 2 as well, so the suite no
# longer depends on being run against the box's pandas to catch this class.
def mixed_frame():
    n = 4
    d = pd.DataFrame({
        "symbol": ["PASS", "BADZONE", "NOSTRUCT", "ALSOPASS"],
        "direction": ["long"] * n,
        "date": [pd.Timestamp("2026-10-02")] * n,
        "close": [98.0] * n, "open": [98.0] * n, "high": [99.0] * n,
        "low": [97.0] * n, "volume": [1e6] * n, "atr": [2.0] * n,
        "atr_pct": [2.0] * n, "rvol": [1.5] * n, "rsi": [55.0] * n,
        "is_warmup": [False] * n, "no_trade_zone": [0] * n,
        "market_regime": ["bull"] * n, "adx": [25.0] * n, "adx_ranging": [0] * n,
        "weekly_bullish": [1] * n, "weekly_bearish": [0] * n,
        "near_demand_ob": [1] * n, "price_in_bull_fvg": [0] * n,
        "bos_bull": [1] * n, "choch_bull": [0] * n, "bull_liq_sweep": [0] * n,
        "near_supply_ob": [0] * n, "price_in_bear_fvg": [0] * n,
        "bos_bear": [0] * n, "bear_liq_sweep": [0] * n,
        "recent_bos_choch": [1, 1, 0, 1],          # NOSTRUCT fails gate 9
        "last_swing_low": [95.0, np.nan, 95.0, 95.0],
        "last_swing_high": [105.0] * n,
        "active_zone_high": [99.0, np.nan, 99.0, 99.0],
        "active_zone_low": [97.0, np.nan, 97.0, 97.0],
        "active_zone_source": ["demand_ob"] * n,
    })
    for c in FAM:
        d[c] = np.nan
    d["active_demand_ob_high"] = [99.0, np.nan, 99.0, 99.0]
    d["active_demand_ob_low"] = [97.0, np.nan, 97.0, 97.0]
    return d


try:
    mixed = s3b.process_direction(mixed_frame(), "long", {}, {})
    mcrash = None
except Exception as e:
    mixed, mcrash = None, f"{type(e).__name__}: {str(e)[:110]}"
check("process_direction survives a PARTIAL qualifying mask", mcrash is None, mcrash or "")

if mixed is not None:
    n_q = int(mixed["qualifies"].sum())
    check("  the mask really was partial", 0 < n_q < len(mixed),
          f"{n_q} of {len(mixed)} qualified")
    check("  gates_failed keeps its declared dtype",
          mixed["gates_failed"].dtype == s3b.GATES_FAILED_DTYPE,
          f"{mixed['gates_failed'].dtype} not {s3b.GATES_FAILED_DTYPE}")
    check("  gates_failed_mask keeps its declared dtype",
          mixed["gates_failed_mask"].dtype == s3b.GATES_MASK_DTYPE,
          f"{mixed['gates_failed_mask'].dtype} not {s3b.GATES_MASK_DTYPE}")
    check("  and the values are still right",
          int(mixed.loc[mixed['symbol'] == 'NOSTRUCT', 'gates_failed'].iloc[0]) == 1
          and int(mixed.loc[mixed['symbol'] == 'PASS', 'gates_failed'].iloc[0]) == 0)
    check("both build sites agree on dtype",
          s3b.GATES_FAILED_DTYPE == np.int16 and s3b.GATES_MASK_DTYPE == np.int32)

print("\n── THE INVARIANT: a stage must hand back the dtype it was given ──")
# This is the regression test for the box crash, stated as an invariant rather
# than as "does it crash". Crash-based tests for this are unreliable here: pandas 2
# only WARNS, the warning only fires on a PARTIAL mask, and 03b suppressed all
# warnings at import anyway. The invariant fails against the pre-fix code
# immediately and on any pandas version: int16 went in, int64 came out.
try:
    from accumulation import apply_accumulation_screen as _scr
except Exception:
    from engine.accumulation import apply_accumulation_screen as _scr
sv = s3b.score_vectorized(mixed_frame(), {}, {}, screen=_scr)
sv["setup_name"] = s3b.determine_setup_names(sv)
qm = sv["qualifies"].to_numpy(dtype=bool, copy=True)
check("the fixture produces a partial qualifying mask", 0 < qm.sum() < len(qm),
      f"{qm.sum()} of {len(qm)}")
lv2 = s3b.compute_trade_levels_vectorized(sv[qm].copy())
for c in ("gates_failed", "gates_failed_mask"):
    check(f"compute_trade_levels_vectorized preserves {c}'s dtype",
          lv2[c].dtype == sv[c].dtype,
          f"{sv[c].dtype} in, {lv2[c].dtype} out")

print("\n── 03b no longer silences its own deprecation warnings ──")
# warnings.filterwarnings("ignore") at module import is why pandas spent a year
# saying this would break and nobody heard it. A deprecation is the only advance
# notice that a library upgrade will take the pipeline down.
import warnings as _w
cat_all = [f for f in _w.filters
           if f[0] == "ignore" and f[2] is Warning and f[1] is None and f[3] is None]
check("no catch-all ignore filter is installed", not cat_all, str(cat_all[:1]))
mod_src = (ROOT / "engine/03b_score.py").read_text()
check("the blanket filterwarnings(\"ignore\") is gone",
      'warnings.filterwarnings("ignore")\n' not in mod_src)
check("FutureWarning is not suppressed by category",
      'category=FutureWarning' not in mod_src)
check("RuntimeWarning still is, by name", 'category=RuntimeWarning' in mod_src)

print("\n── the propagation widens explicitly instead of relying on pandas ──")
src = inspect.getsource(s3b.process_direction)
check("the loop goes through _assign_masked", "_assign_masked" in src)
check("no bare df.loc[qual_mask, col] = assignment remains",
      "df.loc[qual_mask, col] = levels" not in src)
hsrc = inspect.getsource(s3b._assign_masked)
check("_assign_masked widens the destination first", "result_type" in hsrc
      and "astype(want)" in hsrc)
# it must be a no-op when the dtypes already agree
probe = pd.DataFrame({"a": np.zeros(4, dtype=np.int16)})
s3b._assign_masked(probe, np.array([True, True, False, False]),
                   "a", np.array([3, 3], dtype=np.int16))
check("  and does not widen when it need not", probe["a"].dtype == np.int16,
      str(probe["a"].dtype))
s3b._assign_masked(probe, np.array([True, True, False, False]),
                   "a", np.array([3.5, 3.5]))
check("  but does widen when it must", probe["a"].dtype == np.float64,
      str(probe["a"].dtype))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All gate-audit checks passed.")
