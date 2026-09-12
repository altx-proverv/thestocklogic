#!/usr/bin/env python3
"""
THE EXCLUSION LIST MUST BE A PURE FUNCTION OF ITS INPUTS
========================================================
compare_to_current used to baseline on ALL_SYMBOLS -- the map MINUS the
exclusions this tool itself wrote. Since the emitted block REPLACES EXCLUDED
wholesale, an already-excluded symbol could never appear in `drop`, so it was
dropped from the list and silently re-admitted.

EXCLUDED became a function of the PREVIOUS EXCLUDED rather than of
(map, filters, data), and that oscillates with period 2:

    run 1   35 excluded,  5 re-admitted
    run 2    5 excluded, 35 re-admitted
    run 3   35 excluded,  5 re-admitted   ...

Observed on the box: HEG and HFCL, both series BE with a 5% circuit band, were
re-admitted by the first apply and were tradeable again. An oscillating list
looks perfectly stable until someone runs the tool twice, which might be six
months apart -- so it gets a test rather than a comment.

Baselined on SYMBOL_SECTOR_MAP the identity holds exactly:

    ALL_SYMBOLS = map - EXCLUDED = map - (map - tradeable) = map & tradeable
                = keep

and apply_artifact asserts it before keeping a write.

    python3 tests/test_universe_idempotency.py
"""

import io
import sys
import json
import shutil
import logging
import tempfile
import contextlib
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

spec = importlib.util.spec_from_file_location("uf", ROOT / "tools/universe_filter.py")
uf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uf)


# ── the oscillation, in miniature ─────────────────────────────────
MAP = {f"S{i}" for i in range(20)}
TRADEABLE = {f"S{i}" for i in range(10)}      # S10..S19 always fail


def generation_old(excluded: set) -> set:
    """The old logic: baseline = ALL_SYMBOLS, block replaces EXCLUDED."""
    return (MAP - excluded) - TRADEABLE


def generation_new(excluded: set) -> set:
    """The fix: baseline = the map. `excluded` is not an input at all."""
    return MAP - TRADEABLE


def converges(step, start: set, runs: int = 6) -> tuple:
    """(is_fixed_point_from_run_2, [sizes]) for successive generations."""
    sizes, seen, exc = [], [], set(start)
    for _ in range(runs):
        exc = step(exc)
        sizes.append(len(exc))
        seen.append(frozenset(exc))
    stable = all(s == seen[0] for s in seen)
    return stable, sizes


def main() -> int:
    ok = True

    print("THE OLD BASELINE OSCILLATES; THE NEW ONE DOES NOT")
    print("-" * 78)
    start = {"S10", "S11"}
    old_stable, old_sizes = converges(generation_old, start)
    new_stable, new_sizes = converges(generation_new, start)
    print(f"  baseline = ALL_SYMBOLS   sizes {old_sizes}   stable={old_stable}")
    print(f"  baseline = map           sizes {new_sizes}   stable={new_stable}")
    # The point of the test is that the OLD one is unstable and the new one is
    # not. If the old one ever became stable this test would be asserting
    # nothing, so both halves are checked.
    good = (not old_stable) and new_stable
    ok &= good
    print(f"  {'old unstable, new stable':<48}{'ok' if good else '** WRONG **'}")

    print()
    print("REAL universe.py: A PREVIOUSLY-EXCLUDED SYMBOL STILL APPEARS AS A DROP")
    print("-" * 78)
    import pandas as pd
    u = importlib.util.spec_from_file_location("u", ROOT / "engine/universe.py")
    um = importlib.util.module_from_spec(u)
    u.loader.exec_module(um)
    symbols = sorted(um.SYMBOL_SECTOR_MAP)
    already = set(um.EXCLUDED)
    fails = already | set(symbols[:20])
    tradeable = [s for s in symbols if s not in fails]
    surv = pd.DataFrame(index=tradeable,
                        data={"med_turnover_lacs": [900.0] * len(tradeable),
                              "last_close": [100.0] * len(tradeable),
                              "sessions": [300] * len(tradeable)})
    cmp = uf.compare_to_current(surv)
    checks = [
        ("baseline is the map, not ALL_SYMBOLS",
         len(cmp["current"]) == len(um.SYMBOL_SECTOR_MAP)),
        ("keep + drop == map", len(cmp["keep"]) + len(cmp["drop"]) == len(cmp["current"])),
        ("already-excluded symbols appear in drop",
         already <= set(cmp["drop"]) if already else True),
    ]
    for label, good in checks:
        ok &= good
        print(f"  {label:<48}{'ok' if good else '** WRONG **'}")

    print()
    print("apply_artifact REFUSES WHEN THE ARITHMETIC DOES NOT CLOSE")
    print("-" * 78)
    block, n = uf.build_exclusion_block({"stub": cmp["drop"]}, [], "2026-09-13")

    def artifact(keep, map_size=None):
        return {"schema": 1, "generated_at": "2026-09-13T12:00:00+00:00",
                "window": ["a", "b"], "lookback_sessions": 300, "thresholds": {},
                "tradeable": len(cmp["pass"]), "current_universe": len(cmp["current"]),
                "map_size": map_size if map_size is not None else len(cmp["current"]),
                "keep": keep, "keep_symbols_sha": uf._sha(cmp["keep"]),
                "readmitted": [], "previously_excluded": sorted(already),
                "additions_held_back": cmp["add"],
                "drops_by_reason": {"stub": sorted(cmp["drop"])},
                "held_not_excluded": [], "holdings_readable": True,
                "n_excluded": n, "block": block}

    def run(payload, label, expect):
        nonlocal ok
        art = Path(tempfile.mkdtemp()) / "a.json"
        art.write_text(json.dumps(payload))
        tgt = Path(tempfile.mkdtemp()) / "universe.py"
        shutil.copy(ROOT / "engine/universe.py", tgt)
        before = tgt.read_text()
        b = io.StringIO()
        with contextlib.redirect_stdout(b):
            rc = uf.apply_artifact(art, universe=tgt)
        reverted = tgt.read_text() == before
        good = rc == expect and (reverted if expect == 1 else True)
        ok &= good
        extra = "  and reverted" if expect == 1 and reverted else ""
        print(f"  {label:<48}rc={rc}{extra}  "
              f"{'ok' if good else '** want ' + str(expect) + ' **'}")

    run(artifact(len(cmp["keep"])), "keep matches -> applies", 0)
    run(artifact(len(cmp["keep"]) + 5), "keep off by +5 -> refuses", 1)
    run(artifact(len(cmp["keep"]) - 1), "keep off by -1 -> refuses", 1)
    run(artifact(len(cmp["keep"]), map_size=999),
        "artifact from a different map -> refuses", 1)

    print()
    print("APPLYING TWICE CHANGES NOTHING THE SECOND TIME")
    print("-" * 78)
    tgt = Path(tempfile.mkdtemp()) / "universe.py"
    shutil.copy(ROOT / "engine/universe.py", tgt)
    art = Path(tempfile.mkdtemp()) / "a.json"
    art.write_text(json.dumps(artifact(len(cmp["keep"]))))
    states = []
    for _ in range(2):
        b = io.StringIO()
        with contextlib.redirect_stdout(b):
            uf.apply_artifact(art, universe=tgt)
        pr = uf._probe(tgt)
        states.append((pr[1], pr[2]))
    good = states[0] == states[1]
    ok &= good
    print(f"  run 1 (EXCLUDED, ALL_SYMBOLS) = {states[0]}")
    print(f"  run 2 (EXCLUDED, ALL_SYMBOLS) = {states[1]}")
    print(f"  {'identical':<48}{'ok' if good else '** DRIFTED **'}")

    print("-" * 78)
    print("UNIVERSE IDEMPOTENCY:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
