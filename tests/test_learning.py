"""
Is the learning loop's evidence valid, and does it accumulate without double-counting?

THE THING THIS GUARDS. The loop re-tests every standing hypothesis every night. A
fixed-alpha p-value under that regime crosses 0.05 in 36.8% of runs with the null
true -- so the whole design rests on three invariants, and each one fails silently:

  DELTA-ONLY      the product may multiply in only observations it has not seen.
                  An earlier draft stored the cutoff it STARTED from instead of the
                  last date consumed; n_through went 5, 15, 30 on five new rows a
                  night and nothing looked wrong.
  ONE BASIS       likelihood ratios from different definitions of the data cannot be
                  multiplied. The basis changed on 2026-10-03.
  PRE-REGISTERED  observations count from seed_from forward, never from the earliest
                  available row.

Type-I error and coverage are checked by simulation, not by inspecting the formulae.
"""

import sys
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from engine.learning import (bernoulli_log_e, crossed, mean_cs, ebh, wilson)
from engine import learning_loop as L

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


rng = np.random.default_rng(4242)

print("\n── the e-process respects its own bound under optional stopping ──")
# The bound is what licenses nightly looks. Checked by simulation because a wrong
# sign or a mis-clipped bet would still produce plausible-looking numbers.
for p0, side in ((0.5, "greater"), (0.5, "two-sided"), (1 / 3, "greater")):
    ever = 0
    T = 1500
    for _ in range(T):
        st = None
        for _night in range(200):
            st = bernoulli_log_e(rng.binomial(1, p0, 4), p0, side, st)
            if crossed(st["log_e"], 0.05):
                ever += 1
                break
    rate = ever / T
    check(f"p0={p0:.3f} {side:10} type-I {100*rate:.2f}% <= 5%", rate <= 0.05,
          f"{100*rate:.2f}%")

print("\n── and still finds a real effect ──")
det = 0
for _ in range(300):
    st = None
    for _night in range(200):
        st = bernoulli_log_e(rng.binomial(1, 0.45, 4), 1 / 3, "greater", st)
        if crossed(st["log_e"], 0.05):
            det += 1
            break
check("45% against a 33.3% breakeven is detected", det / 300 > 0.9,
      f"{100*det/300:.0f}%")

print("\n── bets are predictable: the current observation cannot change its own bet ──")
# If lambda were computed from x_t, a single extreme observation could launch the
# process. Five heads from a standing start must move log_e by exactly nothing.
st = bernoulli_log_e([1, 1, 1, 1, 1], 0.5, "greater", None)
check("the first five observations bet nothing", abs(st["log_e"]) < 1e-12,
      f"log_e={st['log_e']}")
check("  but they are counted", st["n"] == 5 and st["s"] == 5)
st2 = bernoulli_log_e([1], 0.5, "greater", st)
check("  and the sixth bets on what the first five showed", st2["log_e"] > 0)

print("\n── two-sided is a MIXTURE, never a maximum ──")
# max(E_up, E_dn) is not an e-value and would double the error rate.
up = bernoulli_log_e([1] * 40, 0.5, "greater", None)["log_e"]
dn = bernoulli_log_e([1] * 40, 0.5, "less", None)["log_e"]
two = bernoulli_log_e([1] * 40, 0.5, "two-sided", None)["log_e"]
check("two-sided < max of the one-sided halves", two < max(up, dn))
check("  and equals the mean of the two e-values",
      abs(math.exp(two) - 0.5 * (math.exp(up) + math.exp(dn))) < 1e-6 * math.exp(two))

print("\n── e-BH controls the family, not each hypothesis ──")
check("one e=30 among 18 nulls is NOT enough on its own",
      "h0" not in ebh({**{f"n{i}": 0.5 for i in range(17)}, "h0": 30.0}, 0.05)["rejected"],
      "30 >= 1/0.05 alone, but 18/(0.05*1) = 360 is the family bar")
check("  an e=400 is", "h0" in ebh({**{f"n{i}": 0.5 for i in range(17)},
                                    "h0": 400.0}, 0.05)["rejected"])
check("all-null rejects nothing", ebh({f"n{i}": 0.9 for i in range(18)}, 0.05)["k"] == 0)

print("\n── the confidence sequence covers at every t at once ──")
miss = 0
for _ in range(800):
    st = None
    for _n in range(100):
        st = mean_cs(rng.uniform(0, 1, 3), 0.0, 1.0, 0.05, st)
        if not (st["lo"] <= 0.5 <= st["hi"]):
            miss += 1
            break
check(f"uniform coverage {100*(1-miss/800):.1f}% >= 95%", miss / 800 <= 0.05)

print("\n── DELTA-ONLY: a row is consumed exactly once ──")
ROWS = []


def fake(path, params):
    """A FAITHFUL stub. It must honour the filters the real query sends, or the
    test exercises the stub instead of the code -- which is how the first version
    of the basis-isolation case "failed": last_result does filter on
    basis_version, and the stub returned the ledger wholesale regardless."""
    if "learning_results" in path:
        rows = fake.ledger
        for f in params.split("&"):
            if f.startswith("slug=eq."):
                rows = [r for r in rows if r.get("slug") == f[len("slug=eq."):]]
            elif f.startswith("basis_version=eq."):
                want = f[len("basis_version=eq."):]
                rows = [r for r in rows if r.get("basis_version") == want]
        return rows
    cut = params.split("gt.")[1].split("&")[0] if "gt." in params else "0"
    key = "mark_date" if "mark_date" in params else "signal_date"
    return [r for r in ROWS if str(r.get(key, "")) > cut]


fake.ledger = []
_real = L._get_all
L._get_all = fake
L.ensure_registered = lambda h: None
_sha = L.engine_sha
L.engine_sha = lambda: "test"
try:
    seen = []
    for night, day in enumerate(["2026-10-06", "2026-10-07", "2026-10-08"], 1):
        ROWS.extend([{"signal_date": day, "symbol": f"S{night}{i}",
                      "direction": "LONG", "grade": "B", "outcome": "LOSS",
                      "actual_entry": 100.0, "sl": 98.0} for i in range(5)])
        out = L.run(day, dry=True)
        r = out["results"]["breakeven.grade.B"]
        seen.append((r["n_through"], r["n_new"]))
        fake.ledger = [dict(r)]
    check("n_through is 5, 10, 15 — not 5, 15, 30",
          [t for t, _ in seen] == [5, 10, 15], str([t for t, _ in seen]))
    check("n_new is 5 every night", [n for _, n in seen] == [5, 5, 5],
          str([n for _, n in seen]))
    check("the high-water mark is the last date CONSUMED",
          "through=2026-10-08" in out["results"]["breakeven.grade.B"]["verdict"])

    print("\n── rows a stratum filters out are still consumed ──")
    # Otherwise they are offered again forever and the water mark never advances.
    ROWS.append({"signal_date": "2026-10-09", "symbol": "X", "direction": "SHORT",
                 "grade": "A", "outcome": "LOSS", "actual_entry": 100.0, "sl": 98.0})
    out = L.run("2026-10-09", dry=True)
    rb = out["results"]["breakeven.grade.B"]
    check("grade B saw no new observations", rb["n_new"] == 0)
    check("  but its water mark still advanced", "through=2026-10-09" in rb["verdict"])

    print("\n── ONE BASIS: a result from another basis is not a starting state ──")
    # The query is asserted as well as the behaviour: the isolation lives in the
    # filter last_result sends, and a stub can only ever confirm the stub.
    import inspect as _i
    check("last_result filters on basis_version",
          "basis_version=eq." in _i.getsource(L.last_result))
    fake.ledger = [{**dict(rb), "basis_version": "some-older-basis",
                    "n_through": 999, "verdict": "up=50.0;dn=0;through=2026-10-09"}]
    out = L.run("2026-10-10", dry=True)
    rb2 = out["results"]["breakeven.grade.B"]
    check("the stale-basis lineage is ignored", rb2["n_through"] < 900,
          f"n_through={rb2['n_through']}")
    check("  and log_e did not inherit 50.0", float(rb2["log_e"]) < 10)
finally:
    L._get_all = _real
    L.engine_sha = _sha

print("\n── PRE-REGISTRATION: every hypothesis declares where it starts ──")
for slug, h in L.STANDING.items():
    check(f"  {slug} has a seed date and a basis",
          bool(h["seed_from"]) and bool(h["basis_version"]))
check("outcome hypotheses seed from the re-resolution, not the first row",
      all(h["seed_from"] == L.BASIS_OUTCOME_FROM
          for h in L.STANDING.values() if h["family"] != "directional"))
check("every hypothesis carries a rationale",
      all(h.get("rationale") for h in L.STANDING.values()))
check("nulls are fixed constants, not estimated",
      all(isinstance(h["null_value"], float) for h in L.STANDING.values()))

print("\n── silence is the default ──")
quiet = {"results": {"a": {"crossed_ebh": False, "slug": "a", "log_e": 0.0}},
         "ebh": {"k": 0}}
check("nothing crossed -> empty report", L.report(quiet) == "")

print("\n── Wilson is reporting-only ──")
src = (ROOT / "engine/learning.py").read_text(encoding="utf-8")
check("it says so in the code", "never for deciding" in src.lower()
      or "NEVER for deciding" in src)
p, lo, hi = wilson(99, 181)
check("54.7% of 181 has an interval spanning 50%", lo < 0.5 < hi,
      f"{100*lo:.1f}-{100*hi:.1f}%")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All learning-loop checks passed.")
