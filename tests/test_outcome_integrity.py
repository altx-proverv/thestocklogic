"""
Can the resolver book a win that lost money?

It could, for five months. Three defects in one block of engine/update_outcomes.py,
each independently sufficient to corrupt the published accuracy figure:

  A  The win level was the STORED target_1, computed at publish time from
     entry_ref. The trade fills at actual_entry, a gap away. So "2R" was 2R of a
     risk nobody took -- 2.082R off the fill at the median, 1.73R at p10, 6.71R at
     p90 across the 293 resolved rows.
  B  Nothing checked that the target was beyond the fill. On 17 rows it was not,
     so price touched a level BETWEEN the entry and the stop and the resolver
     wrote WIN_T1 immediately. 10 of those are in the record as wins.
  C  pnl = abs(exit_price - actual_entry) * qty. The abs() turned those into
     POSITIVE P&L. OFSS 2026-06-01 is recorded at +226.36 per share while
     actually losing 226.36.

The fixtures below are the real rows, with their real numbers.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from engine.update_outcomes import evaluate
from engine.zone_entry import measurement_targets

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def frame(rows):
    d = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    d["date"] = pd.to_datetime(d["date"]).dt.date
    return d.set_index("date")


print("\n── B: a target between the entry and the stop is not a win ──")
# OFSS 2026-06-01 LONG, verbatim: entry_ref 10345.5, sl 9914.18, stored t1
# 10063.64 -- which sits BELOW the fill of 10290.0 and ABOVE the stop. The old
# code booked WIN_T1 on day 1 and reported +226.36 per share.
OFSS = {"signal_date": "2026-06-01", "symbol": "OFSS", "direction": "LONG",
        "entry_ref": 10345.5, "entry_low": 10200.0, "entry_high": 10290.0,
        "sl": 9914.18, "target_1": 10063.64, "qty": 10, "risk_inr": 3000.0}
bars = frame([
    ("2026-06-02", 10290.0, 10310.0, 10050.0, 10100.0),   # touches 10063.64
    ("2026-06-03", 10100.0, 10150.0, 9900.0, 9950.0),     # then takes the stop
    ("2026-06-04", 9950.0, 9980.0, 9890.0, 9900.0),
    ("2026-06-05", 9900.0, 9950.0, 9850.0, 9880.0),
    ("2026-06-08", 9880.0, 9920.0, 9800.0, 9850.0),
])
r = evaluate(OFSS, bars)
check("the stored sub-fill target no longer books a win", r["outcome"] != "WIN_T1",
      f"got {r['outcome']}")
check("it resolves as the loss it was", r["outcome"] == "LOSS", f"got {r['outcome']}")
check("P&L is negative", r["pnl"] < 0, f"got {r['pnl']}")
check("the fill is still the fill", abs(r["actual_entry"] - 10290.0) < 0.01)

print("\n── A: the yardstick comes off the fill, not off entry_ref ──")
# A long whose fill is well below entry_ref. The stored target is 2R off the ref;
# the honest target is 2R off the fill, which is nearer and smaller in absolute R.
SIG = {"signal_date": "2026-06-01", "symbol": "X", "direction": "LONG",
       "entry_ref": 1000.0, "entry_low": 950.0, "entry_high": 960.0,
       "sl": 900.0, "target_1": 1200.0, "qty": 10, "risk_inr": 3000.0}
t_ref, _ = measurement_targets(1000.0, 900.0, "LONG")     # 1200 -> 2R off the ref
t_fill, _ = measurement_targets(960.0, 900.0, "LONG")     # 1080 -> 2R off the fill
check("the two yardsticks genuinely differ", abs(t_ref - t_fill) > 100,
      f"{t_ref} vs {t_fill}")
# price reaches 1080 but never 1200
bars = frame([
    ("2026-06-02", 960.0, 1000.0, 955.0, 990.0),
    ("2026-06-03", 990.0, 1090.0, 980.0, 1085.0),          # clears 1080, not 1200
    ("2026-06-04", 1085.0, 1100.0, 1070.0, 1090.0),
    ("2026-06-05", 1090.0, 1095.0, 1080.0, 1085.0),
    ("2026-06-08", 1085.0, 1090.0, 1075.0, 1080.0),
])
r = evaluate(SIG, bars)
check("a 2R move off the fill is a win", r["outcome"] == "WIN_T1", f"got {r['outcome']}")
check("the exit is the fill-based target", abs(r["exit_price"] - t_fill) < 0.01,
      f"exit {r['exit_price']}, expected {t_fill}")
check("and it really is 2R of the risk taken",
      abs((r["exit_price"] - r["actual_entry"]) / (r["actual_entry"] - 900.0) - 2.0) < 0.01)
check("P&L is positive and signed", r["pnl"] > 0)

print("\n── C: P&L is signed, never abs() ──")
import inspect
from engine import update_outcomes, trade_review
# EXECUTABLE SOURCE. The first version of this check read the raw file, found the
# comment that explains why the abs() was removed, and failed on its own
# rationale. That is the sixth time in this repo, and the second since
# tests/_srcutil.py was written to stop it -- having the helper is not the same as
# reaching for it.
from tests._srcutil import executable_source
import ast


def win_branch(path):
    """The source of the `if outcome == "WIN_T1"` branch BODY, and nothing else.

    Scoped with ast rather than by slicing text. A fixed-width window past the
    marker runs straight into the LOSS branch, where abs() is correct -- so a
    text slice reports the adjacent correct code as the defect. The second way
    this one check went wrong.
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "outcome"
                and isinstance(t.comparators[0], ast.Constant)
                and t.comparators[0].value == "WIN_T1"
                and any(isinstance(n, ast.Assign) for n in node.body)):
            return "\n".join(ast.unparse(n) for n in node.body)
    return None


for mod, path in ((update_outcomes, "engine/update_outcomes.py"),
                  (trade_review, "engine/trade_review.py")):
    body = win_branch(ROOT / path)
    nm = mod.__name__.split(".")[-1]
    check(f"{nm}: the win branch exists and was found", body is not None)
    check(f"{nm}: the win branch has no abs()", body is not None and "abs(" not in body,
          (body or "").strip()[:90])
    check(f"{nm}: the win branch is direction-aware",
          body is not None and "LONG" in body and "qty" in body)
    # and the loss branch SHOULD still use a magnitude -- proof the check is
    # scoped, not just absent
    whole = executable_source(ROOT / path)
    check(f"{nm}: the loss branch still uses a magnitude", "abs(actual_entry - sl)" in whole)

print("\n── the short side gets the same treatment ──")
SH = {"signal_date": "2026-06-01", "symbol": "Y", "direction": "SHORT",
      "entry_ref": 1000.0, "entry_low": 1040.0, "entry_high": 1060.0,
      "sl": 1100.0, "target_1": 800.0, "qty": 10, "risk_inr": 3000.0}
bars = frame([
    ("2026-06-02", 1040.0, 1050.0, 1000.0, 1010.0),
    ("2026-06-03", 1010.0, 1015.0, 910.0, 920.0),          # 2R off a 1040 fill = 920
    ("2026-06-04", 920.0, 930.0, 900.0, 910.0),
    ("2026-06-05", 910.0, 915.0, 905.0, 910.0),
    ("2026-06-08", 910.0, 912.0, 900.0, 905.0),
])
r = evaluate(SH, bars)
t_sh, _ = measurement_targets(1040.0, 1100.0, "SHORT")
check("a short wins at 2R off its own fill", r["outcome"] == "WIN_T1", f"got {r['outcome']}")
check("short exit is the fill-based target", abs(r["exit_price"] - t_sh) < 0.01,
      f"{r['exit_price']} vs {t_sh}")
check("short P&L is positive when price falls", r["pnl"] > 0, f"got {r['pnl']}")
# a short whose "target" is above the fill must not book a win
SH_BAD = dict(SH, target_1=1080.0)
bars2 = frame([
    ("2026-06-02", 1040.0, 1085.0, 1030.0, 1080.0),        # touches 1080 at once
    ("2026-06-03", 1080.0, 1105.0, 1075.0, 1100.0),        # then stops out
    ("2026-06-04", 1100.0, 1110.0, 1090.0, 1100.0),
    ("2026-06-05", 1100.0, 1105.0, 1095.0, 1100.0),
    ("2026-06-08", 1100.0, 1102.0, 1090.0, 1095.0),
])
r = evaluate(SH_BAD, bars2)
check("a short's above-fill stored target is not a win", r["outcome"] != "WIN_T1",
      f"got {r['outcome']}")

print("\n── the stored target_1 is no longer read as a level at all ──")
src = inspect.getsource(update_outcomes.evaluate)
after = src.split("actual_entry = max(next_open, entry_low)")[1]
check("no sig.get('target_1') after the fill is known", "target_1" not in after)
check("measurement_targets is called with actual_entry",
      "measurement_targets(actual_entry" in src)
check("a target not beyond the fill fails closed", "BAD_TARGET" in src)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All outcome-integrity checks passed.")
