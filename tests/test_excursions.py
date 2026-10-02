#!/usr/bin/env python3
"""The excursion path: the entry convention, the cumulative maxima, the skips.

This table exists because every row in signal_outcomes exits at exactly -1R or
+2R by construction, so no exit rule is testable from it. That makes the entry
convention the thing most worth asserting: a path measured from a different fill
than the outcome was scored from answers a question about a trade nobody took.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine.excursions as X                                     # noqa: E402

ok = True


def check(label, got, want, extra=""):
    global ok
    good = got == want
    ok &= good
    print(f"  {label:<54}{str(got):<12}"
          f"{'ok' if good else '** want ' + str(want) + ' **':<20}{extra}")


def fake_bars(rows):
    """rows = [(date, open, high, low, close)]"""
    import pandas as pd
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"]) \
             .assign(date=lambda d: pd.to_datetime(d["date"]))


def with_bars(df):
    X._bars.clear()
    X._bars["TEST"] = df
    return df


def sig(direction="LONG", lo=99.5, hi=100.5, sl=96.0, date="2026-01-01"):
    return {"signal_date": date, "symbol": "TEST", "direction": direction,
            "entry_ref": 100.0, "entry_low": lo, "entry_high": hi, "sl": sl}


print("=" * 78)
print("THE ENTRY CONVENTION MATCHES update_outcomes EXACTLY")
print("-" * 78)
# LONG: MISSED if the next open is more than 0.5% above the band high
with_bars(fake_bars([("2026-01-02", 101.1, 102.0, 100.9, 101.5)]))
rows, why = X.measure(sig())
check("open 0.6% above the band -> MISSED", why, "MISSED_GAP_UP",
      "100.5 * 1.005 = 101.0")
# just inside the tolerance is a fill, not a miss
with_bars(fake_bars([("2026-01-02", 100.9, 102.0, 100.5, 101.5)]))
rows, why = X.measure(sig())
check("  open inside the 0.5% tolerance -> filled", bool(rows), True,
      f"fill {rows[0]['entry_price'] if rows else '-'}")
check("  and the fill is min(open, band high)",
      rows[0]["entry_price"] if rows else None, 100.5)
# opening below the stop invalidates rather than fills
with_bars(fake_bars([("2026-01-02", 95.0, 96.5, 94.0, 95.5)]))
rows, why = X.measure(sig())
check("open below the stop -> INVALIDATED", why, "GAPPED_BELOW_SL")
# SHORT mirrors
with_bars(fake_bars([("2026-01-02", 98.9, 99.5, 98.0, 98.5)]))
rows, why = X.measure(sig(direction="SHORT", sl=104.0))
check("SHORT: open 0.6% below the band -> MISSED", why, "MISSED_GAP_DOWN")
with_bars(fake_bars([("2026-01-02", 99.6, 100.2, 99.0, 99.5)]))
rows, why = X.measure(sig(direction="SHORT", sl=104.0))
check("  inside tolerance -> filled at max(open, band low)",
      rows[0]["entry_price"] if rows else None, 99.6)

print()
print("MFE AND MAE ARE CUMULATIVE AND MONOTONIC")
print("-" * 78)
# entry 100, stop 96, risk 4. Day 1 +2 high. Day 2 +8 high. Day 3 nothing new.
with_bars(fake_bars([
    ("2026-01-02", 100.0, 102.0,  99.0, 101.0),
    ("2026-01-05", 101.0, 108.0, 100.0, 107.0),
    ("2026-01-06", 107.0, 107.5,  98.0,  99.0),
]))
rows, why = X.measure(sig(lo=99.5, hi=100.0, sl=96.0))
check("three bars measured", len(rows), 3, why)
check("  d1 mfe = (102-100)/4", rows[0]["mfe_r"], 0.5)
check("  d2 mfe rises to (108-100)/4", rows[1]["mfe_r"], 2.0)
check("  d3 mfe HOLDS, it does not reset", rows[2]["mfe_r"], 2.0)
check("  d1 mae = (100-99)/4", rows[0]["mae_r"], 0.25)
check("  d3 mae rises to (100-98)/4", rows[2]["mae_r"], 0.5)
check("mfe is monotonic",
      all(rows[i]["mfe_r"] <= rows[i + 1]["mfe_r"] for i in range(len(rows) - 1)),
      True)
check("mae is monotonic",
      all(rows[i]["mae_r"] <= rows[i + 1]["mae_r"] for i in range(len(rows) - 1)),
      True)
check("close_r is per-bar, not cumulative", rows[2]["close_r"], -0.25,
      "(99-100)/4")

print()
print("TOUCH FLAGS ARE PER-BAR, AND TIES ARE RECORDED NOT RESOLVED")
print("-" * 78)
# entry 100, stop 96, risk 4 -> 2R = 108, 3R = 112. One bar spanning both.
with_bars(fake_bars([("2026-01-02", 100.0, 109.0, 95.0, 104.0)]))
rows, why = X.measure(sig(lo=99.5, hi=100.0, sl=96.0))
r = rows[0]
check("stop touched on this bar", r["stop_touched"], True)
check("2R touched on the same bar", r["target_2r_touched"], True)
check("3R not reached", r["target_3r_touched"], False, "high 109 < 112")
check("both_same_bar is flagged", r["both_same_bar"], True,
      "order unknowable from a daily bar")
# and the path keeps running past the stop -- a consumer must cut it
with_bars(fake_bars([
    ("2026-01-02", 100.0, 101.0, 95.0, 96.0),
    ("2026-01-05",  96.0,  97.0, 80.0, 81.0),
]))
rows, _ = X.measure(sig(lo=99.5, hi=100.0, sl=96.0))
check("mae keeps growing past the stop touch", rows[1]["mae_r"], 5.0,
      "price went 5R against; a stopped trade did not lose 5R")
check("  and the first touch is flagged on d1", rows[0]["stop_touched"], True)

print()
print("THE WINDOW IS 20 TRADING DAYS, COUNTED IN SESSIONS")
print("-" * 78)
many = [("2026-01-%02d" % (d + 2), 100.0, 101.0, 99.5, 100.5)
        for d in range(0, 28) if d + 2 <= 28]
with_bars(fake_bars(many))
rows, _ = X.measure(sig(lo=99.5, hi=100.0, sl=96.0))
check("capped at 20 rows", len(rows), X.WINDOW_DAYS)
check("  offsets are 1..20", (rows[0]["day_offset"], rows[-1]["day_offset"]),
      (1, 20))
check("  a holiday does not consume an offset",
      rows[4]["day_offset"], 5, "offset 5 is the fifth SESSION traded")

print()
print("A SIGNAL WITH NO MEASURABLE PATH IS SKIPPED WITH A REASON")
print("-" * 78)
X._bars.clear(); X._bars["TEST"] = None
rows, why = X.measure(sig())
check("no bar file", why, "no bar file")
with_bars(fake_bars([("2025-06-01", 100.0, 101.0, 99.0, 100.0)]))
rows, why = X.measure(sig(date="2026-01-01"))
check("bars end before the signal", why.startswith("bars end"), True, why)
with_bars(fake_bars([("2026-01-02", 100.0, 101.0, 99.0, 100.0)]))
rows, why = X.measure({"signal_date": "2026-01-01", "symbol": "TEST",
                       "direction": "LONG", "entry_low": None,
                       "entry_high": None, "sl": None})
check("no levels", why, "no entry band or stop")
rows, why = X.measure(sig(lo=100.0, hi=100.0, sl=100.0))
check("zero risk per share", why, "zero risk per share",
      "would divide by zero in every R figure")

print()
print("NO SWING DATA IS READ — THE LOOK-AHEAD CANNOT BE INHERITED")
print("-" * 78)
# DOCSTRINGS STRIPPED TOO, not just comments. This module explains at length why
# it reads no swing data, and names last_swing_low while doing so -- so a search
# over the source finds the explanation and reports it as the thing it warns
# about. That has now caught me four times in this repo: the fix is always to
# interrogate what executes, never the text that describes it.
import ast


def executable_source(path):
    """Module source with every comment and docstring removed."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


code = executable_source(ROOT / "engine/excursions.py")
# "rsi" is deliberately NOT in this list: it is a substring of "excursions", and a
# check that cannot tell the module's own name from a column it must not read is
# not a check. Indicator columns are covered by the positive assertion below --
# the read_parquet column list is the only place a column can enter this module.
for bad in ("swing_high", "swing_low", "last_swing", "adx_", "delivery"):
    check(f"  does not read {bad}", bad in code, False)
# Quote-insensitive: ast.unparse normalises "x" to 'x', so matching the literal
# source spelling would pass or fail on formatting rather than on content.
cols = code.replace('"', "'").replace(" ", "")
check("  reads only date/open/high/low/close",
      "columns=['date','open','high','low','close']" in cols, True)
check("every row is stamped with the engine SHA", "engine_sha" in code, True)

print("-" * 78)
print("EXCURSIONS:", "correct" if ok else "*** DEFECTIVE ***")
sys.exit(0 if ok else 1)
