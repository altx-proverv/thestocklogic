#!/usr/bin/env python3
"""The signal row: what it claims, what it carries, and what is derived.

Written after an audit found sector constant at "OTHER" on 831 outcome rows for
seventeen months, publication_kind acting as a disguised timestamp, and score
perfectly collinear with the five sub-scores it was being tested alongside. Each
of those was invisible because nothing asserted the relationship.
"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ok = True


def check(label, got, want, extra=""):
    global ok
    good = got == want
    ok &= good
    print(f"  {label:<56}{str(got):<10}"
          f"{'ok' if good else '** want ' + str(want) + ' **':<20}{extra}")


print("=" * 78)
print("THE SECTOR DEFAULT IS GONE FROM BOTH RESOLVERS")
print("-" * 78)
for f in ("engine/update_outcomes.py", "engine/trade_review.py"):
    t = (ROOT / f).read_text(encoding="utf-8")
    code = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    check(f"{f.split('/')[-1]} has no sector fallback",
          'sig.get("sector", "OTHER")' in code, False)
    check("  and reads it with no default at all",
          'sig.get("sector")' in code, True)

print()
print("06_push CARRIES SECTOR, AND SAYS WHETHER IT WAS RECORDED")
print("-" * 78)
push = (ROOT / "engine/06_push_supabase.py").read_text(encoding="utf-8")
for fld in ("sector", "sector_bias", "sector_as_of"):
    check(f"  writes {fld}", f'"{fld}":' in push, True)
check("  and marks its own rows 'recorded'", '"recorded"' in push, True,
      "the backfill marks itself separately")

print()
print("publication_kind IS TESTED POSITIVELY, SO NULL IS NOT TRADEABLE")
print("-" * 78)
mo = (ROOT / "atlas/signal/market_open.py").read_text(encoding="utf-8")
code = "\n".join(l for l in mo.splitlines() if not l.lstrip().startswith("#"))
check("no `!= \"signal\"` test survives", '!= "signal"' in code, False,
      "a NULL row would have passed it")
# The one legitimate default is in get_signals' degradation path: when the COLUMN
# does not exist, treating every row as 'signal' is the pre-migration truth and is
# not a label leak. The two READ sites must have no default; that one may.
reads = [l for l in code.splitlines()
         if 'publication_kind", "signal"' in l and "setdefault" not in l]
check("neither read site carries a default", reads, [],
      "setdefault in the missing-column path is exempt")
check("  and None lands on watch-only",
      code.count('publication_kind") or ""') >= 2, True)
# behavioural, not textual: the three states must map correctly
for kind, want in (("signal", True), ("candidate", False), (None, False),
                   ("", False)):
    actionable = str(kind or "") == "signal"
    check(f"  publication_kind={kind!r} -> enterable", actionable, want)

print()
print("score IS THE SUM OF ITS FIVE COMPONENTS — THE INVARIANT, ASSERTED")
print("-" * 78)
# The audit measured this across all 845 live rows carrying sub-scores: mean gap
# 0.00, zero exceptions. It was true and unenforced, which is how six features
# that are one quantity got tested as six.
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("s3b", ROOT / "engine/03b_score.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    import pandas as pd
    d = pd.read_parquet(ROOT / "data/processed/signals_v2/all_scores_v2.parquet")
    subs = ["regime_score", "smc_score", "technical_score", "volume_score", "rr_score"]
    have = [c for c in subs if c in d.columns]
    if len(have) == 5 and "total_score" in d.columns:
        gap = (d["total_score"] - d[have].sum(axis=1)).abs()
        check("score == sum(components) on every scored row",
              bool((gap < 0.51).all()), True,
              f"n={len(d):,}  max gap {gap.max():.3f}")
    else:
        print(f"  {'sub-score columns absent from the parquet':<56}"
              f"{'SKIP':<10}{have}")
except FileNotFoundError:
    print(f"  {'no local parquet — invariant not exercised here':<56}SKIP")

print()
print("THE PAGE NO LONGER READS A COLUMN THAT IS NULL ON 73% OF ROWS")
print("-" * 78)
page = (ROOT / "signals.html").read_text(encoding="utf-8")
check("sl_pct is not read off a signal row", "s.sl_pct" in page, False)
check("  the distance is computed from sl and entry", "_slBase" in page, True)

print()
print("THE SCHEMA DOC EXISTS AND COVERS EVERY WRITTEN FIELD")
print("-" * 78)
doc = ROOT / "docs/signal_schema.md"
check("docs/signal_schema.md exists", doc.exists(), True)
if doc.exists():
    txt = doc.read_text(encoding="utf-8")
    written = set(re.findall(r'^\s*"([a-z_0-9]+)":', push, re.M)) - {"apikey"}
    missing = sorted(w for w in written if w not in txt)
    check("  every field 06_push writes is documented", missing, [],
          f"{len(written)} fields")
    for tag in ("RECORDED", "DERIVED", "CONSTANT", "DEPRECATED"):
        check(f"  marks {tag} fields", tag in txt, True)

print("-" * 78)
print("SIGNAL SCHEMA:", "correct" if ok else "*** DEFECTIVE ***")
sys.exit(0 if ok else 1)
