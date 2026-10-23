#!/usr/bin/env python3
"""
TWO PATHS LET A TRADE PAST THE GATES. BOTH ARE CLOSED HERE.
============================================================

1. THE ACCUMULATION UNDO LEFT LATER GATE FAILURES UNAPPLIED.

   The disqualifier funnel SHORT-CIRCUITS: it stops at a row's first failing
   gate, so disqualify_reason names only that one. accumulation's screen then
   clears the disqualification whenever that reason is in ACCUMULATION_KILLERS
   -- and every later gate the row also failed was never the reason, so nothing
   re-applied it.

   Measured on the 2026-10-22 batch: 60 of 125 qualifying rows still failed at
   least one gate, every one LONG, and 50 still failed no_recent_bos_choch --
   bit 8, after adq_ranging_market at bit 5. Seven of the eleven entries taken
   on 2026-10-23 came through this hole, SOLARINDS and CSBBANK among them.

   WHAT IS FORGIVEN STAYS FORGIVEN. very_low_volume and adq_ranging_market are
   the point of the accumulation screen, not oversights in it: a quiet, ranging
   stock is what accumulation looks like. So those two are still forgiven and
   every other gate is re-applied.

2. `oversized` WAS COMPUTED AND IGNORED.

   position_sizing set oversized=True when one share already cost more than the
   target notional, and the entry gate logged a warning and carried on.
   SOLARINDS was entered on 2026-10-23 at Rs18,808 for one share against a
   Rs10,000 target -- 188% of target, the largest position of the day. MRF at
   Rs118,860 would have been 1,189% in a single share, and it was enterable.

   FIXED_NOTIONAL_PER_TRADE REMAINS A TARGET, NOT A CEILING (config.py:130).
   Only the case where ONE share exceeds it is refused; the round-half-up is
   untouched, so 33 shares of a Rs303 stock at Rs10,004 is still intended.

NEITHER FIX ADDS A GATE OR RENUMBERS A BIT. gates_failed_mask bit i is
GATE_ORDER[i] and those bits are persisted in reject_sample; a renumbering
would reinterpret every stored row. The re-check re-uses the same conds.
"""
import ast
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from tests._srcutil import executable_source                     # noqa: E402

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<60} {str(got)[:24]:<26} {'ok' if ok else 'FAIL':<6} {note}")


def head(t):
    print()
    print(t)
    print("-" * 112)


_reported = []


def report():
    """Idempotent, and ARMED FOR A CRASH.

    A traceback before the summary printed the mutation harness a clean
    "tripped nothing" for two mutations that had in fact already failed an
    assertion -- indistinguishable from a test that had gone quiet. Whatever
    happens, the recorded failures get printed.
    """
    if _reported:
        return _reported[0]
    print()
    print("=" * 112)
    rc = 1 if failures else 0
    if failures:
        print(f"FAILED — {len(failures)} assertion(s)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("PASSED — later gates are re-applied after the undo, and a "
              "share price that cannot be sized is refused")
    _reported.append(rc)
    return rc


def _on_crash(t, v, tb):
    import traceback
    failures.append(f"the test CRASHED before finishing: {t.__name__}: {v}")
    traceback.print_exception(t, v, tb)
    report()


sys.excepthook = _on_crash


import importlib.util as ilu                                      # noqa: E402

_s = ilu.spec_from_file_location("sc", ROOT / "engine/03b_score.py")
SC = ilu.module_from_spec(_s); _s.loader.exec_module(SC)
from engine.accumulation import ACCUMULATION_KILLERS, apply_accumulation_screen  # noqa: E402

ORDER = list(SC.GATE_ORDER)
FORGIVEN = set(ACCUMULATION_KILLERS)


# ══════════════════════════════════════════════════════════════════
head("THE BITS ARE UNTOUCHED — reject_sample reads them")
# ══════════════════════════════════════════════════════════════════
check("GATE_ORDER is still nine gates", len(ORDER), 9)
check("  and in the same order", ORDER, [
    "warmup", "no_trade_zone", "very_low_volume", "atr_too_high",
    "bear_regime_no_reversal", "adq_ranging_market",
    "weekly_structure_misaligned", "no_smc_signal", "no_recent_bos_choch"])
check("  the zone-entry bit is still 9", SC.GATE_ZONE_ENTRY_BIT, 9)
check("the re-check adds no gate",
      "GATE_ORDER" in executable_source(ROOT / "engine/03b_score.py"), True,
      "the assert in gate_conditions still binds")

# The forgiven set must come from accumulation, never be re-listed in 03b.
sc_src = executable_source(ROOT / "engine/03b_score.py")
# The two killer names each appear exactly twice in 03b, both times by
# necessity: once in GATE_ORDER (which fixes the bit numbering) and once as the
# key gate_conditions assigns. A THIRD occurrence would mean the forgiven set
# was re-listed here, and two copies of it drift -- one side would keep
# forgiving a gate the other had stopped forgiving. So the count is derived
# from where the names are structurally required, not chosen.
_required = {k: sum(1 for ln in sc_src.splitlines()
                    if k in ln and ("GATE_ORDER" in ln or ln.strip().startswith("g[")))
             for k in ACCUMULATION_KILLERS}
check("the forgiven set is imported, not re-listed",
      ("ACCUMULATION_KILLERS" in sc_src,
       {k: sc_src.count(k) for k in ACCUMULATION_KILLERS}, _required),
      (True, {k: 2 for k in ACCUMULATION_KILLERS}, {k: 2 for k in ACCUMULATION_KILLERS}),
      "both occurrences are GATE_ORDER + the conds key")
# NOT src.index() -- a mutation that deletes the block would raise here and
# the behavioural assertions below would never run. A missing block is a
# FAILURE to report, not a crash.
def _between(src, a, b):
    i = src.find(a)
    j = src.find(b, i + 1) if i >= 0 else -1
    return src[i:j] if i >= 0 and j > i else None

_recheck_src = _between(sc_src, "_forgiven", "post-screen gate re-check")
check("the re-check block is present in 03b", _recheck_src is not None, True)
check("  and names no gate literally",
      [k for k in ORDER if k in (_recheck_src or "")], [],
      "it iterates conds, so it cannot fall behind GATE_ORDER")


# ══════════════════════════════════════════════════════════════════
head("FIX 1 — a gate the undo may not forgive is re-applied")
# ══════════════════════════════════════════════════════════════════
BASE = {
    "symbol": "X", "direction": "long", "is_warmup": False, "no_trade_zone": 0,
    "rvol": 1.5, "atr_pct": 2.0, "market_regime": "bull", "bull_liq_sweep": 1,
    "choch_bull": 1, "adx_ranging": 0, "weekly_bearish": 0, "weekly_bullish": 1,
    "near_demand_ob": 1, "price_in_bull_fvg": 1, "bos_bull": 1,
    "bear_liq_sweep": 0, "near_supply_ob": 0, "price_in_bear_fvg": 0,
    "bos_bear": 0, "recent_bos_choch": 1, "pct_from_52w_high": -8.0,
    "history_discontinuity": False, "close": 100.0, "high": 101.0,
    "low": 99.0, "prev_close": 99.5, "sector": "IT",
    "active_zone_high": 101.0, "active_zone_low": 98.0,
}

# Four shapes. "ranging" is forgiven; "no_bos" is not.
rows = [
    dict(BASE, symbol="CLEAN"),
    dict(BASE, symbol="RANGING_ONLY", adx_ranging=1),
    dict(BASE, symbol="RANGING_AND_NOBOS", adx_ranging=1, recent_bos_choch=0),
    dict(BASE, symbol="QUIET_AND_NOBOS", rvol=0.2, recent_bos_choch=0),
]
df = pd.DataFrame(rows)

# Replicate score_vectorized's disqualifier block, then the screen, then the
# re-check -- the same three steps in the same order as the shipped code.
conds, _ = SC.gate_conditions(df)
df["disqualified"] = False
df["disqualify_reason"] = ""
for n, c in conds.items():
    m = (~df["disqualified"]) & c
    df.loc[m, "disqualified"] = True
    df.loc[m, "disqualify_reason"] = n
pre = df[["symbol", "disqualified", "disqualify_reason"]].copy()

df = apply_accumulation_screen(df)
mid = df[["symbol", "disqualified", "disqualify_reason"]].copy()

for n, c in [(n, c) for n, c in conds.items() if n not in FORGIVEN]:
    m = (~df["disqualified"]) & c
    df.loc[m, "disqualified"] = True
    df.loc[m, "disqualify_reason"] = n
post = df[["symbol", "disqualified", "disqualify_reason"]].copy()

def at(frame, sym, col):
    return frame.loc[frame["symbol"] == sym, col].iloc[0]

print("  symbol                 after funnel        after undo          after re-check")
for s in ("CLEAN", "RANGING_ONLY", "RANGING_AND_NOBOS", "QUIET_AND_NOBOS"):
    print(f"    {s:<20} "
          f"{str(at(pre,s,'disqualify_reason') or 'qualified'):<19} "
          f"{str(at(mid,s,'disqualify_reason') or 'qualified'):<19} "
          f"{str(at(post,s,'disqualify_reason') or 'qualified')}")

check("a clean row is untouched throughout",
      bool(at(post, "CLEAN", "disqualified")), False)
check("a forgiven-only failure survives the re-check",
      bool(at(post, "RANGING_ONLY", "disqualified")), False,
      "adq_ranging_market is the accumulation feature")
check("the undo DID clear it first",
      bool(at(mid, "RANGING_ONLY", "disqualified")), False)
check("ranging AND no-BOS is RE-DISQUALIFIED",
      (bool(at(post, "RANGING_AND_NOBOS", "disqualified")),
       at(post, "RANGING_AND_NOBOS", "disqualify_reason")),
      (True, "no_recent_bos_choch"), "this is the hole, closed")
check("  and the undo had wrongly cleared it",
      bool(at(mid, "RANGING_AND_NOBOS", "disqualified")), False,
      "pre-fix behaviour, reproduced")
check("quiet AND no-BOS is RE-DISQUALIFIED too",
      (bool(at(post, "QUIET_AND_NOBOS", "disqualified")),
       at(post, "QUIET_AND_NOBOS", "disqualify_reason")),
      (True, "no_recent_bos_choch"), "very_low_volume is the other killer")

# ── THE BINDING ASSERTIONS GO THROUGH score_vectorized ────────────
# The table above replicates the three steps to SHOW the sequence, but a
# replica is my code, not the shipped code: deleting the re-check from 03b
# left every one of those assertions green. Only these bind.
#
# BOTH KILLERS GET THEIR OWN ROW. With only a ranging fixture, forgiving just
# one of the two passed every behavioural check -- the quiet-only row is what
# catches a half-applied forgiven set.
d2 = pd.DataFrame([
    dict(BASE, symbol="R_AND_N", adx_ranging=1, recent_bos_choch=0),
    dict(BASE, symbol="Q_AND_N", rvol=0.2, recent_bos_choch=0),
    dict(BASE, symbol="R_ONLY",  adx_ranging=1),
    dict(BASE, symbol="Q_ONLY",  rvol=0.2),
    dict(BASE, symbol="CLEAN2"),
])
out = SC.score_vectorized(d2.copy(), {}, {}, screen=apply_accumulation_screen)
by = {r["symbol"]: r for _, r in out.iterrows()}
check("score_vectorized re-disqualifies ranging + no-BOS",
      (bool(by["R_AND_N"]["disqualified"]),
       by["R_AND_N"]["disqualify_reason"]), (True, "no_recent_bos_choch"),
      "7 of 11 entries on 2026-10-23 came through here")
check("score_vectorized re-disqualifies quiet + no-BOS",
      (bool(by["Q_AND_N"]["disqualified"]),
       by["Q_AND_N"]["disqualify_reason"]), (True, "no_recent_bos_choch"))
check("score_vectorized still forgives ranging alone",
      bool(by["R_ONLY"]["disqualified"]), False,
      "the accumulation screen's whole purpose")
check("score_vectorized still forgives quiet alone",
      bool(by["Q_ONLY"]["disqualified"]), False)
check("score_vectorized leaves a clean row alone",
      bool(by["CLEAN2"]["disqualified"]), False)
check("  the re-check runs INSIDE the screen branch",
      sc_src.find("_forgiven") > sc_src.find("screen(df)") > 0, True)


# ══════════════════════════════════════════════════════════════════
head("FIX 2 — one share over the target is refused, not warned about")
# ══════════════════════════════════════════════════════════════════
from atlas.risk.position_sizing import (size_for_trade,              # noqa: E402
                                        REASON_OVERSIZED)
from atlas.config import FIXED_NOTIONAL_PER_TRADE as TGT             # noqa: E402

check("the target is Rs10,000", TGT, 10000.0)
check("the reason code is its own", REASON_OVERSIZED, "OVERSIZED")

FUNDS = 10_000_000
# MUST BE REFUSED: one share already exceeds the target.
for sym, px, stop in (("MRF", 118860.00, 116000.00),
                      ("SOLARINDS", 18808.00, 18517.89),
                      ("MARUTI", 11170.00, 10900.00),
                      ("just-over", 10000.01, 9900.00)):
    r = size_for_trade(entry_price=px, stop_price=stop, direction="LONG",
                       available_funds=FUNDS)
    check(f"{sym} at Rs{px:,.2f} is refused",
          (r.get("qty", -1), r.get("reason_code")), (0, REASON_OVERSIZED),
          str(r.get("error", ""))[:40])

# MUST STILL SIZE, exactly as it did on 2026-10-23.
for sym, px, stop, want_qty in (("CSBBANK", 303.15, 295.76, 33),
                                ("LUMAXIND", 5157.50, 5107.76, 2),
                                ("RBA", 85.38, 81.12, 117),
                                ("MOTHERSON", 151.23, 147.10, 66),
                                ("at-the-target", 10000.00, 9900.00, 1)):
    r = size_for_trade(entry_price=px, stop_price=stop, direction="LONG",
                       available_funds=FUNDS)
    check(f"{sym} still sizes to {want_qty}", r.get("qty"), want_qty,
          f"notional Rs{(r.get('qty') or 0) * px:,.0f}")

check("the boundary is strict >, so exactly at target is allowed",
      size_for_trade(entry_price=TGT, stop_price=TGT * 0.99, direction="LONG",
                     available_funds=FUNDS).get("qty"), 1)

# The gate must carry the code as a distinct, countable status.
ae = executable_source(ROOT / "atlas/execution/atlas_entry.py")
check("the entry gate maps reason_code to its own status",
      'SKIPPED_{_rc}' in ae.replace('"', "'").replace("f'", "'"), True)
check("  and no longer merely logs on `oversized`",
      "sizing.get('oversized')" in ae.replace('"', "'"), False,
      "the warning-only branch is gone")
check("  the generic REJECTED_SIZE path survives for other refusals",
      "REJECTED_SIZE" in ae, True)

# A refusal for a DIFFERENT reason must not be relabelled OVERSIZED.
bad_stop = size_for_trade(entry_price=100.0, stop_price=100.0,
                          direction="LONG", available_funds=FUNDS)
check("a bad stop is not reported as oversized",
      (bad_stop.get("qty"), bad_stop.get("reason_code")), (0, None),
      str(bad_stop.get("error", ""))[:40])

sys.exit(report())
