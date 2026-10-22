#!/usr/bin/env python3
"""
A SERIES THAT IS NOT COMPARABLE MUST NOT PRODUCE A LEVEL OR A SCORE
====================================================================
Two consumers, one detector, and the reason both are asserted here.

WHAT HAPPENED. radar gated its card levels on a corporate-action test that
could not fire. The test compared prev_close[i] against close[i-1] on the
premise that "NSE ADJUSTS PREV_CLOSE on the ex-date", and the premise is false:
across 31 split/bonus events in the last 252 sessions, NSE adjusted prev_close
on exactly 0. It appeared to work for one symbol because eleven sessions were
missing from that symbol's parquet, so it was comparing across a hole and
reading the hole as an adjustment.

Meanwhile engine/ had no gate at all. On 2026-10-19 daily_closes published a
high_52w that NO post-split session had traded at, for 31 of 31 affected
symbols -- INDIAGLYCO 1219.00 against a post-split high of 399.00, MCX
11219.00 against 3481.40. pct_from_52w_high then read -70% to -90%, which is
the MAXIMUM rr_ext bonus for a long in 03b_score, on 20 symbols that were in
the enterable batch.

The four dates the old detector did fire on -- KOTAKBANK 2026-01-15, V2RETAIL
2026-03-26, IRB 2026-03-31, JPPOWER 2026-05-28 -- are all mis-dated bhavcopy
files (cm15Jan2026 holds 01-14 data, and so on). Its only successes were
artifacts of a different defect. tests/test_bhavcopy_dates.py owns that one.

WHAT IS ASSERTED HERE:
  the mechanism       a gap in the OPENING print, which the price band makes
                      unreachable by trading, separated from an intraday fall
  one detector        no second copy anywhere, so the two consumers cannot
                      disagree again
  suppression         levels publish NULL, never a pre-split figure
  exclusion           the symbol leaves the enterable universe, and is not
                      rescued by a neutral substitute or by the accumulation
                      screen's undo list
  the real artifact   ALL_ROWS actually carries the flag, which is the one
                      thing a synthetic frame cannot prove
"""
import ast
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from tests._srcutil import executable_source, normalised   # noqa: E402

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:24]:<26} {'ok' if ok else 'FAIL':<6} {note}")


def head(t):
    print()
    print(t)
    print("-" * 112)


def frame(rows):
    """[(date, open, high, low, close, prev_close)] -> a bars-shaped frame."""
    return pd.DataFrame(
        [{"date": date(2026, 1, 1) + pd.Timedelta(days=i).to_pytimedelta(),
          "open": o, "high": max(o, c), "low": min(o, c), "close": c,
          "prev_close": p} for i, (o, c, p) in enumerate(rows)])


# ══════════════════════════════════════════════════════════════════
head("THE MECHANISM — where the gap sits, not how big it is")
# ══════════════════════════════════════════════════════════════════
from engine.price_basis import (                                  # noqa: E402
    CORP_ACTION_GAP_PCT, FEED_CONTINUITY_TOLERANCE_PCT,
    corporate_actions, feed_discontinuities, discontinuities,
    spans_discontinuity)

check("the threshold is NSE's widest price band", CORP_ACTION_GAP_PCT, 20.0)

# A SPLIT: the whole move is in the opening print, and the basis holds.
split = frame([(100, 100, 100), (100, 100, 100), (20, 20, 100), (21, 21, 20)])
check("a 1:5 split is detected", len(corporate_actions(split)), 1,
      "open 20 against a prior close of 100")

# A CRASH: opens near the prior close and falls through the session. POLICYBZR's
# real shape -- it fell 36.0%, further than several real splits, and must not be
# called an action.
crash = frame([(100, 100, 100), (100, 100, 100), (90, 64, 100), (65, 66, 64)])
check("a -36% intraday crash is NOT detected", corporate_actions(crash), [],
      "opened -10%, closed -36%: trading, not a new basis")

# A GAP THAT RECOVERS is not a basis change either: the open gapped, the close
# came back. One bad opening print must not suppress a year of levels.
recover = frame([(100, 100, 100), (100, 100, 100), (70, 97, 100), (97, 98, 97)])
check("an opening gap that recovers is NOT detected",
      corporate_actions(recover), [], "the close returned inside the band")

# SYMMETRIC: a reverse split gaps up by the same mechanism.
rev = frame([(100, 100, 100), (100, 100, 100), (500, 505, 100), (505, 500, 505)])
check("a reverse split (gap UP) is detected", len(corporate_actions(rev)), 1)

# JUST INSIDE AND JUST OUTSIDE the band, so the boundary is asserted not assumed.
inside = frame([(100, 100, 100), (100, 100, 100), (82, 82, 100), (82, 82, 82)])
outside = frame([(100, 100, 100), (100, 100, 100), (79, 79, 100), (79, 79, 79)])
check("  a -18% gap (inside the band) is not an action",
      corporate_actions(inside), [])
check("  a -21% gap (outside the band) is", len(corporate_actions(outside)), 1)

# ── the continuity test, under its own name ──
check("the continuity tolerance is unchanged in value",
      FEED_CONTINUITY_TOLERANCE_PCT, 15.0)
hole = frame([(100, 100, 100), (100, 100, 100), (100, 100, 300)])
check("a prev_close that disagrees with our prior close is a FEED fault",
      len(feed_discontinuities(hole)), 1, "a missing or mis-dated session")
check("  and is NOT reported as a corporate action",
      corporate_actions(hole), [])
check("the split's prev_close is honest, so continuity stays silent",
      feed_discontinuities(split), [],
      "NSE never adjusts it — the premise the old detector was built on")
check("discontinuities() is the union",
      len(discontinuities(split)) == 1 and len(discontinuities(hole)) == 1, True)

# ── the window gate ──
long_clean = frame([(100, 100, 100)] * 300)
check("spans_discontinuity is False on a clean series",
      spans_discontinuity(long_clean, 252), False)
with_split = frame([(100, 100, 100)] * 50 + [(20, 20, 100)] + [(20, 20, 20)] * 10)
check("  True when the window contains the action",
      spans_discontinuity(with_split, 252), True)
check("  and False for a window that ends before it",
      spans_discontinuity(with_split, 5), False,
      "per-lookback: a stock can lose its 200-DMA and keep its 20")


class _Boom:
    def __len__(self): return 9
    def __getitem__(self, k): raise RuntimeError("unreadable series")
    @property
    def iloc(self): raise RuntimeError("unreadable series")


check("an unexaminable series FAILS CLOSED",
      spans_discontinuity(_Boom(), 252), True,
      "cannot certify comparable -> refuse")


# ══════════════════════════════════════════════════════════════════
head("ONE DETECTOR — the two consumers cannot disagree again")
# ══════════════════════════════════════════════════════════════════
# The threshold and the loop must exist in exactly one module. A second copy is
# how radar refused a split on a card while 03b scored it in a signal.
owners = []
for d in ("engine", "radar", "atlas"):
    for f in sorted((ROOT / d).rglob("*.py")):
        if "legacy" in f.parts or "__pycache__" in f.parts:
            continue
        src = executable_source(f)
        if "CORP_ACTION_GAP_PCT" in src and "def corporate_actions" in src:
            owners.append(str(f.relative_to(ROOT)))
check("exactly one module defines the detector", owners,
      ["engine/price_basis.py"])
check("radar re-exports rather than reimplementing",
      "from engine.price_basis import" in executable_source(ROOT / "radar/bars.py"),
      True)
check("  and radar.bars.corporate_actions IS the shared one",
      __import__("radar.bars", fromlist=["x"]).corporate_actions.__module__,
      "engine.price_basis")
check("the old misleading constant name is gone from the tree",
      any("CORP_ACTION_TOLERANCE_PCT" in executable_source(f)
          for d in ("engine", "radar", "atlas")
          for f in (ROOT / d).rglob("*.py")
          if "legacy" not in f.parts and "__pycache__" not in f.parts),
      False, "it described a job it was not doing")

# The derivation has to travel with the number.
pb = (ROOT / "engine/price_basis.py").read_text()
for fact in ("31", "TRENT", "POLICYBZR", "1:10 bonus", "BRIGADE"):
    check(f"  the derivation states {fact!r}", fact in pb, True)


# ══════════════════════════════════════════════════════════════════
head("SUPPRESSION — a level over a discontinuity publishes NULL")
# ══════════════════════════════════════════════════════════════════
smc = executable_source(ROOT / "engine/02b_smc_signals.py")
check("02b suppresses rather than scales",
      "discontinuities" in smc and "nan" in smc.lower(), True)
check("  high_52w is suppressed on its own 252 window",
      "('high_52w', 252)" in smc.replace('"', "'"), True)
check("  pct_from_52w_high too — it is what the scorer reads",
      "('pct_from_52w_high', 252)" in smc.replace('"', "'"), True)
for col, win in (("ema200", 200), ("ema50", 50), ("ema20", 20), ("rsi", 14)):
    check(f"  {col} on its own {win}-session window",
          f"('{col}', {win})" in smc.replace('"', "'"), True)
# BEHAVIOURAL. `"history_discontinuity" in smc` survives deleting the
# assignment, because the column is still initialised to False above it.
import importlib.util as _ilu                                     # noqa: E402
_ss = _ilu.spec_from_file_location("smc_mod", ROOT / "engine/02b_smc_signals.py")
_smc = _ilu.module_from_spec(_ss); _ss.loader.exec_module(_smc)
_n = 300
_rows = []
for i in range(_n):
    px = 100.0 if i < 280 else 20.0
    prev = 100.0 if i <= 280 else 20.0
    _rows.append({"date": date(2025, 1, 1) + pd.Timedelta(days=i).to_pytimedelta(),
                  "open": px, "high": px, "low": px, "close": px,
                  "prev_close": prev, "volume": 1000.0, "delivery_pct": 50.0})
_tf = _smc.compute_technicals(pd.DataFrame(_rows))
check("  compute_technicals SETS the flag on a split series",
      bool(_tf["history_discontinuity"].iloc[-1]), True,
      "a 1:5 gap 20 sessions before the end")
check("  and NULLs high_52w on the same rows",
      _tf["high_52w"].iloc[-1] != _tf["high_52w"].iloc[-1], True, "NaN")
_clean = [dict(r, open=100.0, high=100.0, low=100.0, close=100.0,
               prev_close=100.0) for r in _rows]
_cf = _smc.compute_technicals(pd.DataFrame(_clean))
check("  a clean series keeps its flag False and its level",
      (bool(_cf["history_discontinuity"].iloc[-1]),
       _cf["high_52w"].iloc[-1] == 100.0), (False, True))
check("02b does NOT also compute low_52w",
      'df["low_52w"]' in smc or "df['low_52w']" in smc, False,
      "publish_closes owns it; two sources is the original defect")

pc = executable_source(ROOT / "engine/publish_closes.py")
# ON THE GUARD ITSELF, not on the variable's existence. The first version of
# these two asserted `"bad252" in pc`, which survives deleting the guard --
# the assignment stays and only the `if` loses its condition. ast.unparse
# renders it `and (not bad252)`, so that is what is matched.
pcn = normalised(ROOT / "engine/publish_closes.py")
check("publish_closes gates the 52-week pair",
      "and(notbad252)" in pcn, True, "the low is withheld, not just computed")
check("  and the 20-day pair on its own window",
      "and(notbad20)" in pcn, True)
check("  both windows are evaluated separately",
      "bad20=spans_discontinuity" in pcn and "bad252=spans_discontinuity" in pcn,
      True)
check("  it reads open/close/prev_close to be able to tell",
      "prev_close" in pc and "open" in pc, True)


# ══════════════════════════════════════════════════════════════════
head("EXCLUSION — the symbol leaves the enterable universe")
# ══════════════════════════════════════════════════════════════════
sc = executable_source(ROOT / "engine/03b_score.py")
check("03b disqualifies on the flag",
      "history_discontinuity" in sc, True)
check("  with its own reason, countable in the funnel",
      "'history_discontinuity'" in sc.replace('"', "'"), True)
check("  and a NaN flag is treated as discontinuous",
      "fillna(True)" in sc, True, "unknown is not clean")
check("  the exclusion runs BEFORE the market gates",
      sc.index("history_discontinuity") < sc.index("for name, cond in conds.items()"),
      True)
check("GATE_ORDER is untouched — persisted mask bits keep their meaning",
      "history_discontinuity" in str(
          ast.literal_eval(re.search(r"GATE_ORDER = (\([^)]*\))",
                                     (ROOT / "engine/03b_score.py").read_text(),
                                     re.S).group(1))),
      False, "a tenth gate would reinterpret every stored reject_sample row")

acc = executable_source(ROOT / "engine/accumulation.py")
import engine.accumulation as ACC                                 # noqa: E402
check("the accumulation screen cannot undo this exclusion",
      "history_discontinuity" in ACC.ACCUMULATION_KILLERS, False,
      f"undoes only {ACC.ACCUMULATION_KILLERS}")
check("accumulation requires a REAL off-high reading",
      "off_high.notna()" in acc, True)
check("  and no longer defaults it to 0.0",
      'pct_from_52w_high", 0.0' in acc or "pct_from_52w_high', 0.0" in acc,
      False, "0.0 means 'at the 52-week high', which is a claim")

# BEHAVIOURAL, not textual: the flag must actually disqualify.
import importlib.util                                             # noqa: E402
_s = importlib.util.spec_from_file_location("s3b", ROOT / "engine/03b_score.py")
_m = importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)
base = {"direction": "long", "is_warmup": False, "no_trade_zone": 0,
        "rvol": 1.5, "atr_pct": 2.0, "market_regime": "bull",
        "bull_liq_sweep": 1, "choch_bull": 1, "adx_ranging": 0,
        "weekly_bearish": 0, "weekly_bullish": 1, "near_demand_ob": 1,
        "price_in_bull_fvg": 1, "bos_bull": 1, "bear_liq_sweep": 0,
        "near_supply_ob": 0, "price_in_bear_fvg": 0, "bos_bear": 0,
        "recent_bos_choch": 1, "pct_from_52w_high": -80.0,
        # process_direction computes trade levels, so the price columns and a
        # usable zone have to be present or it fails before the exclusion.
        "close": 100.0, "high": 101.0, "low": 99.0, "prev_close": 99.5,
        "atr": 2.0, "rvol_20": 1.5, "sector": "IT",
        "active_zone_high": 101.0, "active_zone_low": 98.0,
        "date": date(2026, 10, 20)}
df = pd.DataFrame([dict(base, symbol="CLEAN", history_discontinuity=False),
                   dict(base, symbol="SPLIT", history_discontinuity=True),
                   dict(base, symbol="UNKNOWN", history_discontinuity=None)])
conds, _ = _m.gate_conditions(df)
dq = pd.Series(False, index=df.index); rs = pd.Series("", index=df.index)
bad = df["history_discontinuity"].fillna(True).astype(bool)
dq[bad] = True; rs[bad] = "history_discontinuity"
for n, c in conds.items():
    mk = (~dq) & c
    dq[mk] = True; rs[mk] = n
check("a clean row survives the gates", (bool(dq[0]), rs[0]), (False, ""))
check("a flagged row is disqualified", (bool(dq[1]), rs[1]),
      (True, "history_discontinuity"))
check("a NULL flag is disqualified too", (bool(dq[2]), rs[2]),
      (True, "history_discontinuity"))

# AND THROUGH 03b ITSELF, not a replica of its logic. The block above
# reproduces the exclusion; this one calls the real scorer, so deleting the
# exclusion from score_vectorized fails here. process_direction is the entry
# point tests/test_gate_audit.py uses.
_scored = _m.process_direction(df.copy(), "long", {}, {})
_by = {r["symbol"]: r for _, r in _scored.iterrows()}
check("03b itself disqualifies the flagged symbol",
      (bool(_by["SPLIT"]["disqualified"]),
       _by["SPLIT"]["disqualify_reason"]), (True, "history_discontinuity"))
check("  and zeroes its score rather than ranking it",
      float(_by["SPLIT"].get("score") or 0.0), 0.0)
check("  the unknown-flag row is disqualified by the real scorer too",
      bool(_by["UNKNOWN"]["disqualified"]), True)
# THE CONTROL IS SELECTIVITY, not a positive score. A synthetic frame cannot
# satisfy compute_zone_entries (no real swing structure), so every row here is
# eventually disqualified by the zone stage and "CLEAN scores > 0" would be
# asserting the fixture, not the guard. What must hold is that the exclusion
# fires on the flagged rows and ONLY on them.
check("  and the exclusion is selective, not blanket",
      _by["CLEAN"]["disqualify_reason"] != "history_discontinuity", True,
      f"CLEAN was rejected as {_by['CLEAN']['disqualify_reason']!r} instead")


# ══════════════════════════════════════════════════════════════════
head("THE REAL ARTIFACT — a synthetic frame cannot prove this")
# ══════════════════════════════════════════════════════════════════
from engine.paths import ALL_ROWS                                 # noqa: E402
# GATED ON THE ARTIFACT BEING NEWER THAN ITS WRITER, which is the only honest
# version of this assertion. An ALL_ROWS built before 02b learned to write the
# column cannot carry it, and failing on that would be failing on the clock. The
# moment the chain runs once with the new 02b, the artifact is newer and the
# column is mandatory -- so this bites from the first nightly run onward and
# never before.
_writer = ROOT / "engine" / "02b_smc_signals.py"
if not Path(ALL_ROWS).exists():
    print(f"  {ALL_ROWS} absent on this machine — the chain has not run here.")
    print("  This assertion is the one that matters on the box.")
elif Path(ALL_ROWS).stat().st_mtime < _writer.stat().st_mtime:
    print(f"  ALL_ROWS predates engine/02b_smc_signals.py "
          f"(artifact older than its writer), so it cannot carry the new "
          f"column yet. Asserted from the first chain run after this change.")
    cols = pd.read_parquet(ALL_ROWS).columns
    check("  it does still carry pct_from_52w_high to suppress",
          "pct_from_52w_high" in cols, True)
else:
    cols = pd.read_parquet(ALL_ROWS).columns
    check("ALL_ROWS carries history_discontinuity",
          "history_discontinuity" in cols, True,
          "03b defaults it to False when absent, so this is the real guard")
    check("  and still carries pct_from_52w_high to suppress",
          "pct_from_52w_high" in cols, True)

print()
print("=" * 112)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASSED — one detector, levels suppressed, flagged symbols excluded")
