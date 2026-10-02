"""
Does the detection hypothesis get measured honestly?

Three things this guards, each of which has already gone wrong somewhere in this
codebase:

  1. PAGINATION. PostgREST hands back 1,000 rows and HTTP 200 whatever limit you
     ask for. The previous scorer fetched 2,941 live_signals in one call, got the
     newest 1,000, and printed a tally as though it covered all of them.
  2. TIES ARE A RANGE. signal_outcomes holds 228 losses at exactly -1.00R because
     the resolver that wrote it checks the stop first and assigns every same-bar
     tie to LOSS. A verdict function that silently picks a side reintroduces that.
  3. AN EMPTY TABLE IS NOT A 0% HIT RATE. Reporting over no data is worse than
     reporting nothing.
"""

import sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine.score_live_outcomes as S

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def path(*steps):
    """steps = (mfe_r, mae_r) per bar, cumulative as the table stores them."""
    return [{"day_offset": i + 1, "mfe_r": m, "mae_r": a, "close_r": 0.0}
            for i, (m, a) in enumerate(steps)]


print("\n── verdict: first touch ──")

# reaches 1.5R on bar 2, never near the stop
check("target on bar 2 -> WIN at 2",
      S.verdict(path((0.4, 0.2), (1.6, 0.2), (0.9, 0.3)), 1.5)[:2] == ("WIN", 2))

# stop first, target later: the later target must not be credited
check("stop on bar 1, target on bar 3 -> LOSS at 1",
      S.verdict(path((0.3, 1.1), (0.5, 1.1), (2.0, 1.1)), 1.5)[:2] == ("LOSS", 1))

# both crossed inside one bar -> unknowable
check("stop and target in the same bar -> AMBIGUOUS",
      S.verdict(path((0.2, 0.1), (1.9, 1.2)), 1.5)[:2] == ("AMBIGUOUS", 2))

# a path that reaches neither
check("neither side reached -> OPEN",
      S.verdict(path((0.4, 0.3), (0.9, 0.6)), 1.5)[:2] == ("OPEN", None))

# the SAME path answered at three targets, which is the point of storing a path
p = path((0.8, 0.3), (2.2, 0.4), (3.4, 0.4))
check("one path, 1.5R -> WIN bar 2", S.verdict(p, 1.5)[:2] == ("WIN", 2))
check("one path, 2.0R -> WIN bar 2", S.verdict(p, 2.0)[:2] == ("WIN", 2))
check("one path, 3.0R -> WIN bar 3", S.verdict(p, 3.0)[:2] == ("WIN", 3))

# cumulative, not per-bar: mfe already past the target on bar 1 still counts
check("target already exceeded on bar 1 -> WIN at 1",
      S.verdict(path((1.7, 0.2), (1.9, 0.2)), 1.5)[:2] == ("WIN", 1))

# horizon truncates rather than reading past it
check("horizon 1 turns a bar-2 win into OPEN",
      S.verdict(path((0.4, 0.2), (1.6, 0.2)), 1.5, horizon=1)[:2] == ("OPEN", None))

# mfe is returned regardless of outcome, so a loser's excursion is not lost
check("MFE reported on a loss", S.verdict(path((0.9, 1.2),), 1.5)[2] == 0.9)

# the recorded 2R flag must NOT be what decides a 3R question
flagged = [{"day_offset": 1, "mfe_r": 2.1, "mae_r": 0.2,
            "target_2r_touched": True, "target_3r_touched": False}]
check("3R verdict ignores the recorded 2R flag",
      S.verdict(flagged, 3.0)[0] == "OPEN")

print("\n── ties are a range, never a point ──")
c = Counter({"WIN": 10, "LOSS": 10, "AMBIGUOUS": 10})
band = S._band(c)
check("a band is printed when ties exist", "–" in band and "%" in band, band)
lo, hi = [float(x.strip("%")) for x in band.split(" of ")[0].split("–")]
check("lower bound assigns ties to LOSS", abs(lo - 100 * 10 / 30) < 0.05, f"{lo}")
check("upper bound assigns ties to WIN", abs(hi - 100 * 20 / 30) < 0.05, f"{hi}")
check("the band is wide when ties are a third of the sample", hi - lo > 30)

clean = S._band(Counter({"WIN": 10, "LOSS": 10}))
check("no ties -> a CI, not a band", "95% CI" in clean, clean)

print("\n── Wilson, because the splits run small ──")
p_, lo_, hi_ = S.wilson(1, 4)
check("n=4 interval is honestly wide", hi_ - lo_ > 0.5, f"{lo_:.2f}-{hi_:.2f}")
check("n=0 does not divide by zero", S.wilson(0, 0) == (0.0, 0.0, 0.0))
_, lo9, hi9 = S.wilson(500, 1000)
check("n=1000 interval tightens", hi9 - lo9 < 0.07, f"{lo9:.3f}-{hi9:.3f}")
_, lo0, hi0 = S.wilson(0, 20)
check("0 of 20 does not claim 0%", hi0 > 0.0 and lo0 == 0.0, f"{lo0}-{hi0}")

print("\n── pagination ──")
# EXECUTABLE SOURCE, not inspect.getsource. The module's docstring says
# "limit=200000 returns exactly 1,000" in order to explain why that approach is
# wrong, and a text search over the whole file finds that sentence and fails. The
# fifth time this has happened here; tests/_srcutil.py is the fix.
from tests._srcutil import executable_source
code = executable_source(ROOT / "engine/score_live_outcomes.py")
src = __import__("inspect").getsource(S)
check("the 1,000-row cap is paged, not raised by limit=N",
      "offset=" in code and "200000" not in code,
      "200000 still in executable code" if "200000" in code else "no offset")
check("the explanation IS in the file, just not in the code",
      "200000" in src and "200000" not in code)
check("_get_all keeps paging until a short page",
      "len(chunk) < PAGE" in code)

calls = {"n": 0}
class R:
    status_code = 200
    def __init__(self, n): self._n = n
    def json(self): return [{"signal_date": "2026-09-01", "symbol": f"S{i}",
                             "direction": "LONG", "day_offset": 1}
                            for i in range(self._n)]
def fake_get(url, **kw):
    calls["n"] += 1
    off = int(url.split("offset=")[1])
    return R(1000 if off == 0 else 1000 if off == 1000 else 787)
S.requests.get, _real = fake_get, S.requests.get
try:
    rows = S._get_all("live_signals", "select=*")
finally:
    S.requests.get = _real
check("2,787 rows arrive as 2,787, not 1,000", len(rows) == 2787, f"{len(rows)}")
check("it took three pages", calls["n"] == 3, f"{calls['n']}")

print("\n── an empty table is not a result ──")
check("report() on no paths returns nothing and says so", S.report({}, "x") == {})
check("main refuses to report over an empty table",
      "Refusing to report a hypothesis over no data" in code)

print("\n── the published targets are NOT the yardstick ──")
# The docstring discusses target_1 and target_2 at length. What matters is that
# no executing line reads either as a level.
check("target_1/target_2 are never read as levels", "target_1" not in code
      and "target_2" not in code.replace("target_2r_touched", ""))
check("R targets are explicit and include the published 1.5",
      1.5 in S.R_TARGETS and 2.0 in S.R_TARGETS and 3.0 in S.R_TARGETS)

print("\n── small groups are pooled, not printed as a hit rate ──")
check("by_group pools small groups", "min_n" in code and "pooled" in code)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All detection-scoring checks passed.")
