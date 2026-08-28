"""
BACKTEST — Test A, resolver parity
==================================
Feeds the ACTUAL published signals through the backtest's own resolution path
and requires it to reproduce the live record exactly at the pinned window.

WHAT THIS DOES AND DOES NOT PROVE. resolve_signal is imported from
mark_signals unchanged, so the per-signal verdicts match by construction --
that is the point of reusing it, not a weakness. What is genuinely under test
is everything wrapped around it: fetching the right signals, joining the right
price history, replicating the rolling window, and aggregating on both bases.
That is precisely where the errors were: the same table read as 239, 278, 417,
423 and 289 resolved depending on how the window and the per-signal collapse
were done. A gate that catches those is worth having.

Test B (generator parity over 13-27 Aug, where the signal-generating code is
stable) is the separate check that the pipeline replay produces the right
CANDIDATES. Neither alone is sufficient.
"""

import sys
import logging
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import store, window as W
from backtest.config import BASELINE_EXPECTED, BASELINE_WINDOW_END

log = logging.getLogger("BACKTEST-BASELINE")

# Tolerance for the summed-percent comparisons. The live values are rounded to
# 3dp by the view; anything looser would let a real drift through.
PCT_TOL = 0.005


def load_live_marks() -> pd.DataFrame:
    """
    The live record. Paged on a UNIQUE order -- signal_marks is keyed on
    (signal_date, symbol, direction, mark_date) and PostgREST offset paging
    over a non-unique order silently skips and repeats rows.
    """
    rows = store.read_live_readonly(
        "signal_marks",
        select=("signal_date,symbol,direction,setup_name,resolution,"
                "resolved_pnl_pct,resolved_on,mark_date"),
        order="signal_date.asc,symbol.asc,direction.asc,mark_date.asc")
    df = pd.DataFrame(rows)

    exact = store.exact_count("signal_marks")
    if len(df) != exact:
        raise RuntimeError(f"paged {len(df)} rows but the table holds {exact} "
                           f"-- pagination lost or duplicated rows")
    dupes = df.duplicated(subset=["signal_date", "symbol", "direction", "mark_date"]).sum()
    if dupes:
        raise RuntimeError(f"{dupes} duplicate key rows in the paged read")
    log.info(f"live signal_marks: {len(df):,} rows, verified against exact count")
    return df


def compare(actual: dict, expected: dict) -> list:
    """Field-by-field. Returns the list of mismatches, empty if identical."""
    bad = []
    for field, exp in expected.items():
        act = actual.get(field)
        if isinstance(exp, float):
            ok = act is not None and abs(float(act) - exp) <= PCT_TOL
        else:
            ok = act == exp
        if not ok:
            bad.append((field, exp, act))
    return bad


def run(window_end: str = BASELINE_WINDOW_END) -> dict:
    """Returns {'passed': bool, 'bases': {...}, 'mismatches': {...}}."""
    marks = load_live_marks()
    start, end = W.window_bounds(sorted(marks["mark_date"].unique()), window_end)
    log.info(f"window {start} .. {end} (20 distinct mark dates)")

    pub = W.add_first_signal_flag(W.publications(marks, start, end))

    results, mismatches = {}, {}
    for basis, first_only in (("all_publications", False),
                              ("first_signal_only", True)):
        got = W.summarise(pub, first_only=first_only)
        results[basis] = got
        mismatches[basis] = compare(got, BASELINE_EXPECTED[basis])

    passed = not any(mismatches.values())
    return {"passed": passed, "window_start": start, "window_end": end,
            "bases": results, "mismatches": mismatches, "publications": pub}


def report(result: dict) -> None:
    """Explicit comparison, per the acceptance criterion."""
    print("=" * 74)
    print("TEST A — RESOLVER PARITY AGAINST THE LIVE RECORD")
    print(f"window {result['window_start']} .. {result['window_end']}"
          "   (20 distinct mark dates, as v_signal_window defines it)")
    print("=" * 74)

    for basis in ("all_publications", "first_signal_only"):
        got = result["bases"][basis]
        exp = BASELINE_EXPECTED[basis]
        bad = dict((f, (e, a)) for f, e, a in result["mismatches"][basis])
        label = ("ALL PUBLICATIONS  (of everything published, how did it do)"
                 if basis == "all_publications" else
                 "FIRST-SIGNAL ONLY (what the book would have taken — Gate 3b)")
        print(f"\n  {label}")
        print(f"    {'field':<16}{'expected':>12}{'actual':>12}   ")
        print("    " + "-" * 44)
        for field in ("n_signals", "resolved", "unresolved", "STOP", "TARGET",
                      "SAME_DAY", "EXPIRED", "long_n", "short_n",
                      "long_sum_pct", "short_sum_pct", "total_sum_pct"):
            if field not in exp:
                continue
            e, a = exp[field], got.get(field)
            flag = "  <-- MISMATCH" if field in bad else ""
            fmt = (lambda v: f"{v:,.3f}") if isinstance(e, float) else (lambda v: f"{v:,}")
            print(f"    {field:<16}{fmt(e):>12}{fmt(a):>12}{flag}")
        print(f"    hit_rate {got['hit_rate_pct']}%")

    print("\n" + "=" * 74)
    if result["passed"]:
        print("RESULT: PASS — every figure reproduced on both bases.")
    else:
        n = sum(len(v) for v in result["mismatches"].values())
        print(f"RESULT: FAIL — {n} mismatch(es). "
              "Nothing this engine says about any other config is trustworthy.")
    print("=" * 74)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = run()
    report(r)
    sys.exit(0 if r["passed"] else 1)
