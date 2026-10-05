"""
Can a migration be pasted without Postgres rejecting it on arithmetic?

The detections.reach2r INSERT in PENDING_learning_backfill.sql listed twelve
columns and supplied eleven values. `family` was missing, so every value from
basis_version onward shifted one place -- 'standing' would have landed in a date
column -- and Postgres rejected the count mismatch. The operator found it by
running it. Nothing in the suite looked at migration SQL at all.

Counting by eye fails on this file for two reasons, both present in it:
string literals contain parentheses ('1/(1+2)'), and adjacent literals concatenate
into one value, which every long rationale relies on. tools/check_inserts.py
understands both; a third attempt also had to teach it that -- comments are legal
mid-tuple and contain commas.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.check_inserts import check as count_insert, split_top_level

FAILS = []


def ok(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


print("\n── every INSERT balances columns against values ──")
mig = sorted((ROOT / "migrations").glob("*.sql"))
ok("there are migrations to check", len(mig) > 5, str(len(mig)))
total = 0
for p in mig:
    for table, cols, vals in count_insert(p):
        total += 1
        ok(f"{p.name[:40]:40} {table[:26]:26} {len(cols)}/{len(vals)}",
           len(cols) == len(vals),
           f"{len(cols)} columns vs {len(vals)} values")
print(f"        {total} INSERT statement(s) across {len(mig)} migration(s)")

print("\n── the counter understands SQL, not just commas ──")
# Each of these broke a previous version of the counter.
ok("parentheses inside a literal are not nesting",
   len(split_top_level("'a/(b+c)', 2, 'd'")) == 3,
   str(split_top_level("'a/(b+c)', 2, 'd'")))
ok("adjacent literals are ONE value",
   len(split_top_level("'part one ' 'part two', 2")) == 2,
   str(split_top_level("'part one ' 'part two', 2")))
ok("a doubled quote is a quote, not a terminator",
   len(split_top_level("'it''s one, really', 2")) == 2,
   str(split_top_level("'it''s one, really', 2")))
ok("a -- comment mid-tuple is not two values",
   len(split_top_level("'a', -- one, two, three\n  'b'")) == 2,
   str(split_top_level("'a', -- one, two, three\n  'b'")))
ok("a comma inside a literal is not a separator",
   len(split_top_level("'2,832 detections', 2")) == 2)

print("\n── the specific row that failed ──")
bk = ROOT / "migrations/PENDING_learning_backfill.sql"
if bk.exists():
    rows = {v[0]: (c, v) for _t, c, v in
            [(t, c, v) for t, c, v in count_insert(bk)]}
    det = [(c, v) for _t, c, v in count_insert(bk)
           if v and v[0] == "'detections.reach2r'"]
    ok("detections.reach2r is present", bool(det))
    if det:
        cols, vals = det[0]
        ok("  it balances now", len(cols) == len(vals), f"{len(cols)}/{len(vals)}")
        ok("  family is supplied", "family" in cols
           and vals[cols.index("family")].strip("'") == "detection-breakeven",
           vals[cols.index("family")] if "family" in cols else "no family column")
        ok("  and basis_version is the basis, not the family",
           vals[cols.index("basis_version")].strip("'") == "detection-path-v1")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All migration checks passed.")
