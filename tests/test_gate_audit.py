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
check("qualifying rows fail none",
      bool((df.loc[~df["disqualified"], "gates_failed"] == 0).all()))
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

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All gate-audit checks passed.")
