"""
THE UNIVERSE EXCLUSION IS ENFORCED AT THE PUBLISH, AND NOWHERE ELSE
===================================================================

WHAT THIS GUARDS, AND WHY IT HAD TO EXIST. Until 2026-10-13,
engine/universe.EXCLUDED was enforced by 01b_download_bhavcopy refusing to
WRITE excluded symbols. Nothing downstream re-applied it: 02b globs, 03b globs,
06_push had no universe filter, and atlas/signal/market_open.py reads
public.signals with none either. The exclusion worked only because the symbols
had no fresh data and 02b's staleness guard deleted their SMC output.

That side effect froze 37 parquets at their 2026-09-11 bar for a month, and
those frozen files kept publishing a month-old daily return into
sector_heatmap as if it were the session's.

Ingestion now writes the whole universe, so engine/06_push_supabase.py is THE
gate -- the only thing between a scored row and both the screener and ATLAS. A
symbol reaching public.signals is a symbol ATLAS may size and buy. These checks
assert the gate exists, that it is a refusal and not a warning, and that it is
reachable from every publish path rather than only the one that had the bug.
"""

import sys
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<58}{str(got):<10}{'ok' if good else f'** want {want} **'}  {extra}")


def load_push():
    """06_push_supabase cannot be imported by name -- it starts with a digit."""
    spec = importlib.util.spec_from_file_location(
        "push_supabase", ROOT / "engine" / "06_push_supabase.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> int:
    import pandas as pd
    from engine.universe import EXCLUDED, SYMBOL_SECTOR_MAP, ALL_SYMBOLS

    print("=" * 78)
    print("1 — THE ARITHMETIC THE GATE RESTS ON")
    print("-" * 78)
    check("EXCLUDED is a subset of the map",
          set(EXCLUDED) <= set(SYMBOL_SECTOR_MAP), True)
    check("ALL_SYMBOLS == map - EXCLUDED",
          set(ALL_SYMBOLS) == set(SYMBOL_SECTOR_MAP) - set(EXCLUDED), True)
    check("EXCLUDED is not empty", len(EXCLUDED) > 0, True,
          f"{len(EXCLUDED)} symbols — an empty set would make every check "
          f"below pass vacuously")

    print()
    print("=" * 78)
    print("2 — INGESTION WRITES THE WHOLE UNIVERSE")
    print("-" * 78)
    # The point of the change: data for every member, trading for a subset.
    # Asserted on source because running 01b downloads and writes.
    from _srcutil import executable_source
    dl = executable_source(ROOT / "engine" / "01b_download_bhavcopy.py")
    check("01b's write loop is driven by SYMBOL_SECTOR_MAP",
          "NIFTY100_SYMBOLS = set(SYMBOL_SECTOR_MAP)" in dl, True)
    check("  and no longer by ALL_SYMBOLS",
          "ALL_SYMBOLS" in dl, False,
          "enforcing a trading filter by withholding data is the defect")

    print()
    print("=" * 78)
    print("3 — THE PUBLISH FILTER DROPS EXCLUDED SYMBOLS")
    print("-" * 78)
    push = load_push()
    excl = sorted(EXCLUDED)
    keep = sorted(ALL_SYMBOLS)[:3]
    df = pd.DataFrame({"symbol": keep + excl[:4],
                       "qualifies": [True] * (len(keep) + 4)})
    out = push._drop_excluded(df, "test")
    check("the filter removes every excluded symbol",
          sorted(set(out["symbol"]) & set(EXCLUDED)), [])
    check("  and keeps every permitted one",
          sorted(out["symbol"]) == keep, True)
    check("  a frame of only excluded symbols comes back empty",
          len(push._drop_excluded(pd.DataFrame({"symbol": excl}), "test")), 0)
    check("  a frame with no symbol column is passed through",
          len(push._drop_excluded(pd.DataFrame({"x": [1, 2]}), "test")), 2,
          "the filter must not be the thing that raises on a shape change")

    print()
    print("=" * 78)
    print("4 — THE ASSERTION REFUSES, IT DOES NOT WARN")
    print("-" * 78)
    # THIS IS THE CHECK THE WHOLE FILE EXISTS FOR. If someone deletes the
    # filter in section 3, or adds a third publish path that forgets it, the
    # push must fail rather than succeed quietly.
    clean = [{"symbol": s} for s in keep]
    check("a clean payload passes and reports its size",
          push._refuse_if_excluded(clean, "test"), len(keep))

    for name in (excl[0], excl[len(excl) // 2], excl[-1]):
        payload = [{"symbol": s} for s in keep] + [{"symbol": name}]
        raised = False
        try:
            push._refuse_if_excluded(payload, "test")
        except RuntimeError:
            raised = True
        check(f"  one excluded symbol ({name}) is refused", raised, True)

    # EVERY excluded symbol, not a sample -- the set is 40 long and a per-symbol
    # typo in the universe block would otherwise pass on a three-name sample.
    import logging
    logging.getLogger("push_supabase").setLevel(logging.CRITICAL)
    missed = []
    for name in excl:
        try:
            push._refuse_if_excluded([{"symbol": name}], "test")
            missed.append(name)
        except RuntimeError:
            pass
    check("all 40 excluded symbols are refused individually", missed, [])

    check("a missing symbol key does not crash the assertion",
          push._refuse_if_excluded([{"direction": "LONG"}], "test"), 1)
    logging.getLogger("push_supabase").setLevel(logging.NOTSET)

    print()
    print("=" * 78)
    print("5 — THE ASSERTION IS WIRED INTO EVERY PUBLISH PATH")
    print("-" * 78)
    src = (ROOT / "engine" / "06_push_supabase.py").read_text(encoding="utf-8")
    pe = executable_source(ROOT / "engine" / "06_push_supabase.py")
    posts = [ln for ln in pe.splitlines() if "requests.post(base_url" in ln]
    check("publish POSTs to the signals table", len(posts), 2,
          "the signal path and the candidate watchlist")
    # CALL SITES, not occurrences -- the definitions carry the names too.
    def calls(name):
        return sum(1 for ln in pe.splitlines()
                   if f"{name}(" in ln and not ln.strip().startswith("def "))
    check("  both are preceded by the refusal",
          calls("_refuse_if_excluded"), 2)
    check("  both frames are filtered first", calls("_drop_excluded"), 2)
    check("the refusal raises rather than returning",
          "raise RuntimeError(" in pe, True)
    check("  and it is not downgraded to a warning anywhere",
          "log.warning(f\"PUBLISH REFUSED" in src, False)

    print()
    print("=" * 78)
    print("6 — NOTHING ELSE CLAIMS TO ENFORCE THE EXCLUSION")
    print("-" * 78)
    # If a second enforcement point appears, this check is where to record it.
    # Two gates that disagree is worse than one gate that is known.
    enforcers = []
    for p in sorted((ROOT / "engine").glob("*.py")) + \
             sorted((ROOT / "atlas").rglob("*.py")):
        if p.name in ("universe.py", "paths.py"):
            continue
        t = p.read_text(encoding="utf-8", errors="ignore")
        if "EXCLUDED" in t and "import" in t:
            if "from engine.universe import EXCLUDED" in t or \
               "from universe import EXCLUDED" in t:
                enforcers.append(p.relative_to(ROOT).as_posix())
    check("exactly one module imports EXCLUDED to enforce it",
          enforcers, ["engine/06_push_supabase.py"])

    print()
    print("=" * 78)
    print("PASS" if OK else "FAIL")
    print("=" * 78)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
