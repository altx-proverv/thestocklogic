#!/usr/bin/env python3
"""
THE EXCLUSION BLOCK, BUILT AND APPLIED, WITHOUT CREDENTIALS
===========================================================
--write-exclusions died on the box with

    UnboundLocalError: cannot access local variable 'emit'

because the block-builder lived inline inside main()'s `if emitting:` branch,
which returns early without a service key. Every local run exercised the
refusal path; the builder itself had never executed outside the box. An
ordering error -- emit() called fifteen times before it was defined -- shipped
and failed there.

A code path that can only run where it cannot be tested will eventually only
fail there. So the builder is a pure function now, and this drives it and the
writer with invented arguments: no network, no key, no atlas_trades.

    python3 tests/test_exclusion_block.py
"""

import io
import sys
import shutil
import contextlib
import logging
import tempfile
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

spec = importlib.util.spec_from_file_location("uf", ROOT / "tools/universe_filter.py")
uf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uf)

REASONS = {"price < Rs50": ["IOB", "IDEA", "JPPOWER"],
           "circuit band < 10%": ["CEMPRO", "CPPLUS"],
           "< 250 sessions": ["ABLBL"]}


def universe_members(n: int) -> list:
    u = importlib.util.spec_from_file_location("u", ROOT / "engine/universe.py")
    m = importlib.util.module_from_spec(u)
    u.loader.exec_module(m)
    return sorted(m.SYMBOL_SECTOR_MAP)[:n]


def main() -> int:
    ok = True
    print("THE BUILDER RUNS AT ALL")
    print("-" * 78)
    # The regression: this used to raise UnboundLocalError before producing a line.
    try:
        block, n = uf.build_exclusion_block(REASONS, [], "2026-09-12")
        print(f"  ok   built {len(block.splitlines())} lines, n_excluded={n}")
        ok &= n == 6
    except Exception as e:
        print(f"  ** {type(e).__name__}: {e}")
        return 1

    print()
    print("HELD SYMBOLS ARE WITHHELD AND NAMED")
    print("-" * 78)
    block, n = uf.build_exclusion_block(REASONS, ["IOB"], "2026-09-12")
    checks = [
        ("IOB named in the HELD section", "#   IOB" in block),
        ('IOB NOT in the excluded set', '    "IOB",' not in block),
        ("IDEA still excluded", '    "IDEA",' in block),
        ("count drops to 5", n == 5),
        ("withheld count stated", "1 held symbol(s) withheld" in block),
        ("the consequence is explained", "stopped moving" in block),
    ]
    for label, good in checks:
        ok &= good
        print(f"  {label:<44}{'ok' if good else '** WRONG **'}")

    print()
    print("NO HELD SYMBOLS: NO HELD SECTION")
    print("-" * 78)
    block, n = uf.build_exclusion_block(REASONS, [], "2026-09-12")
    good = "HELD, THEREFORE NOT EXCLUDED" not in block and n == 6
    ok &= good
    print(f"  {'clean block, all 6 excluded':<44}{'ok' if good else '** WRONG **'}")

    print()
    print("A REASON EMPTIED BY HOLDINGS DISAPPEARS")
    print("-" * 78)
    # circuit band has exactly two symbols; hold both and the heading must go.
    block, n = uf.build_exclusion_block(REASONS, ["CEMPRO", "CPPLUS"], "2026-09-12")
    good = "# circuit band < 10%" not in block and n == 4
    ok &= good
    print(f"  {'empty reason group omitted':<44}{'ok' if good else '** WRONG **'}")

    print()
    print("THE BLOCK APPLIES TO A REAL universe.py")
    print("-" * 78)
    real = universe_members(6)
    block, n = uf.build_exclusion_block({"test group": real}, [], "2026-09-12")
    tmp = Path(tempfile.mkdtemp()) / "universe.py"
    shutil.copy(ROOT / "engine/universe.py", tmp)
    rc = uf.write_exclusions(block, n, path=tmp)
    probe = uf._probe(tmp)
    good = rc == 0 and probe is not None and probe[1] == 6 and probe[2] == probe[0] - 6
    ok &= good
    print(f"  write rc={rc}  map={probe[0]} EXCLUDED={probe[1]} ALL_SYMBOLS={probe[2]}"
          f"   {'ok' if good else '** WRONG **'}")
    good = (tmp.with_suffix(".py.bak")).exists()
    ok &= good
    print(f"  {'backup kept':<44}{'ok' if good else '** MISSING **'}")

    print()
    print("THE WRITTEN BLOCK IS VALID PYTHON AND ROUND-TRIPS")
    print("-" * 78)
    text = tmp.read_text()
    good = "EXCLUDED = {" in text and text.count("EXCLUDED = {") == 1
    ok &= good
    print(f"  {'exactly one EXCLUDED block':<44}{'ok' if good else '** WRONG **'}")
    m = importlib.util.spec_from_file_location("u_rt", tmp)
    mod = importlib.util.module_from_spec(m)
    m.loader.exec_module(mod)
    good = set(mod.EXCLUDED) == set(real)
    ok &= good
    print(f"  {'EXCLUDED round-trips to the same symbols':<44}"
          f"{'ok' if good else '** WRONG **'}")

    print()
    print("main()'s EMITTING BRANCH RUNS — THE BRANCH THAT CRASHED")
    print("-" * 78)
    # The bug was not in the builder's logic, it was that this branch had never
    # executed without a service key. Every collaborator is stubbed so it runs
    # with no network, no bhavcopy and no atlas_trades.
    import io
    import contextlib
    import pandas as pd

    surv = pd.DataFrame(index=["AAA", "BBB"],
                        data={"med_turnover_lacs": [900.0, 800.0],
                              "last_close": [100.0, 200.0],
                              "sessions": [300, 300]})
    saved = (uf.funnel, uf.fetch_sec_list, uf.bhavcopy_stats,
             uf.compare_to_current, uf.explain_drops, uf.open_positions)
    try:
        uf.funnel = lambda verbose=True: {"survivors": surv,
                                          "window": ("2026-01-01", "2026-09-12"),
                                          "steps": []}
        uf.fetch_sec_list = lambda *a, **k: pd.DataFrame(
            {"Symbol": ["AAA"], "Series": ["EQ"], "Band": ["20"]})
        uf.bhavcopy_stats = lambda *a, **k: (surv, "2026-01-01", "2026-09-12")
        uf.compare_to_current = lambda s: {"current": set(real), "pass": {"AAA"},
                                           "keep": [], "add": ["AAA"],
                                           "drop": list(real)}
        uf.explain_drops = lambda d, sl, st: {"price < Rs50": list(real)}
        uf.open_positions = lambda: (True, set())

        sys.argv = ["universe_filter", "--emit-exclusions"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = uf.main()
        out = buf.getvalue()
        good = rc == 0 and "EXCLUDED = {" in out
        ok &= good
        print(f"  --emit-exclusions  rc={rc}, produced a block: "
              f"{'EXCLUDED = {' in out}   {'ok' if good else '** WRONG **'}")

        # and the refusal, which is the only path that used to be reachable
        uf.open_positions = lambda: (False, set())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = uf.main()
        good = rc == 1 and "EXCLUDED = {" not in buf.getvalue()
        ok &= good
        print(f"  unreadable holdings -> refuses, emits nothing   "
              f"{'ok' if good else '** WRONG **'}")
    finally:
        (uf.funnel, uf.fetch_sec_list, uf.bhavcopy_stats,
         uf.compare_to_current, uf.explain_drops, uf.open_positions) = saved

    print()
    print("THE BOX -> MAC HANDOFF, AND ITS REFUSALS")
    print("-" * 78)
    # engine/universe.py is a TRACKED file and the box resets to origin/main
    # every five minutes, so an in-place edit there lasts minutes. The list can
    # only be computed on the box and only committed from a checkout that
    # pushes, so it travels as an untracked artifact under data/artifacts/.
    import json as _json
    blk, n_blk = uf.build_exclusion_block({"price < Rs50": real[:4]}, [],
                                          "2026-09-13")
    base = {"schema": 1, "generated_at": "2026-09-13T10:00:00+00:00",
            "window": ["a", "b"], "lookback_sessions": 300, "thresholds": {},
            "tradeable": 843, "current_universe": 495, "keep": 460,
            "additions_held_back": [], "drops_by_reason": {"price < Rs50": real[:4]},
            "held_not_excluded": [], "holdings_readable": True,
            "n_excluded": n_blk, "block": blk}

    def handoff(label, mutate, expect):
        nonlocal ok
        payload = dict(base)
        mutate(payload)
        art = Path(tempfile.mkdtemp()) / "a.json"
        art.write_text(_json.dumps(payload))
        tgt = Path(tempfile.mkdtemp()) / "universe.py"
        shutil.copy(ROOT / "engine/universe.py", tgt)
        before = tgt.read_text()
        b = io.StringIO()
        with contextlib.redirect_stdout(b):
            rc = uf.apply_artifact(art, universe=tgt)
        untouched = tgt.read_text() == before
        good = rc == expect and (untouched if expect == 1 else not untouched)
        ok &= good
        print(f"  {label:<46}rc={rc}  {'ok' if good else '** want ' + str(expect) + ' **'}")

    handoff("a valid artifact applies", lambda p: None, 0)
    handoff("unknown schema refuses", lambda p: p.update(schema=99), 1)
    handoff("holdings unreadable when computed refuses",
            lambda p: p.update(holdings_readable=False), 1)
    handoff("no EXCLUDED block refuses", lambda p: p.update(block="# nothing"), 1)
    handoff("a held symbol inside the block refuses",
            lambda p: p.update(held_not_excluded=[real[0]]), 1)
    handoff("count mismatch refuses and restores",
            lambda p: p.update(n_excluded=99), 1)

    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        rc = uf.apply_artifact(Path("/nonexistent/a.json"))
    ok &= rc == 1
    print(f"  {'a missing artifact refuses':<46}rc={rc}  "
          f"{'ok' if rc == 1 else '** WRONG **'}")

    print("-" * 78)
    print("EXCLUSION BLOCK:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
