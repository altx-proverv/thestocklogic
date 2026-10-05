"""
Can the publisher name a column the table does not have?

TWO FAILURES, ONE SHAPE. PostgREST rejects an insert naming an unknown column by
discarding the WHOLE payload, and it reports one not-null violation at a time, so a
single wrong key costs the entire batch and the next one stays hidden.

  CANDIDATES HAVE NEVER PUBLISHED. push_candidates omitted `grade`, which is NOT
  NULL with no default: 23502 on every push since the feature shipped, zero rows
  in the table, ever. _watchlist returns the full scored frame so the grade was
  computed and simply not sent.

  THE SIGNAL PUSH WAS ABOUT TO FAIL ENTIRELY. engine_sha and engine_ran_at were
  added to the payload on 2026-10-02; the migration that adds them to `signals` was
  deliberately deferred. The first chain run after that commit would have published
  nothing at all -- 106 signals on the Thursday, zero on the Monday, and the only
  notice a 400 in reports/cron.log.

The second is the dangerous one, because deferring a migration is a normal decision
and nothing about it suggests the publisher will stop working.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib.util

spec = importlib.util.spec_from_file_location("p6", ROOT / "engine/06_push_supabase.py")
p6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p6)

SRC = (ROOT / "engine/06_push_supabase.py").read_text(encoding="utf-8")
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def payload_keys(fn: str) -> set:
    i = SRC.index(f"def {fn}(")
    j = SRC.index("\ndef ", i + 10)
    return set(re.findall(r'"([a-z_][a-z_0-9]*)":\s', SRC[i:j]))


print("\n── the candidate payload carries every NOT NULL column ──")
cand = payload_keys("push_candidates")
# grade is the one that bit. signal_date/symbol/direction were always there.
for col in ("signal_date", "symbol", "direction", "grade", "score", "setup_name",
            "entry_ref", "publication_kind"):
    check(f"  candidates send {col}", col in cand)
check("grade is not a bare constant",
      "_candidate_grade" in SRC and '"grade":            "C"' not in SRC)

print("\n── the grade sent is the one that was computed ──")
check("a real grade passes through", p6._candidate_grade({"grade": "A+"}) == "A+")
check("a missing grade falls back to C, not B",
      p6._candidate_grade({"grade": None}) == "C")
check("  and the signal path's generous default is not reused",
      p6._candidate_grade({"grade": ""}) != "B")
check("the fallback warns", "no grade — publishing as C" in SRC)

print("\n── provenance adapts to the schema instead of assuming it ──")
check("_signals_has exists", hasattr(p6, "_signals_has"))
check("it is cached per process", "_SIGNALS_HAS" in SRC)
check("engine_sha is not an unconditional payload key",
      '"engine_sha":       ENGINE_SHA,\n            "engine_ran_at"' not in SRC)
check("it is added behind the probe", '_signals_has("engine_sha", headers)' in SRC)
check("a failed probe omits rather than includes",
      "publishing without it" in SRC)
check("the warning names the migration to apply",
      "PENDING_signals_features_provenance.sql" in SRC)


class _R:
    def __init__(self, code): self.status_code = code


def fake(code):
    def g(url, **kw):
        return _R(code)
    return g


print("\n── the probe's verdicts ──")
real = p6.requests.get
try:
    for code, want, label in ((200, True, "200 -> present"),
                              (206, True, "206 -> present"),
                              (400, False, "400 -> absent"),
                              (404, False, "404 -> absent")):
        p6._SIGNALS_HAS.clear()
        p6.requests.get = fake(code)
        check(f"  {label}", p6._signals_has("engine_sha", {}) is want)
    # an exception must read as absent: omitting one field publishes the row,
    # naming a column that is not there publishes nothing
    p6._SIGNALS_HAS.clear()
    def boom(url, **kw):
        raise RuntimeError("network down")
    p6.requests.get = boom
    check("  a raised probe reads as ABSENT", p6._signals_has("engine_sha", {}) is False)
    # and it is cached: one probe per column per process
    calls = {"n": 0}
    def counting(url, **kw):
        calls["n"] += 1
        return _R(200)
    p6._SIGNALS_HAS.clear()
    p6.requests.get = counting
    for _ in range(5):
        p6._signals_has("engine_sha", {})
    check("  probed once, not once per batch", calls["n"] == 1, str(calls["n"]))
finally:
    p6.requests.get = real

print("\n── the two payloads write the same table, so NOT NULL must agree ──")
sig = payload_keys("push_signals")
# every column the candidate path omits must be nullable. grade was not, and that
# is the whole bug; this pins the ones since confirmed nullable so a future
# NOT NULL on any of them fails here rather than at 18:35.
NULLABLE = {"delivery_pct", "market_regime", "notional", "qty", "risk_inr", "rr_1",
            "rr_2", "rsi", "rvol", "score_regime", "score_rr", "score_smc",
            "score_technical", "score_volume", "sector_bias", "target_1",
            "target_2", "trade_type", "vix_close", "sector", "sector_as_of",
            "engine_sha", "engine_ran_at", "apikey"}
unexplained = sorted((sig - cand) - NULLABLE)
check("every column candidates omit is accounted for", not unexplained,
      f"unexplained: {unexplained}")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All push-payload checks passed.")
