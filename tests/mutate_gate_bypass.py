#!/usr/bin/env python3
"""MUTATION PROOF. Break the shipped code on purpose; the test must fail, and
must fail on the assertion that is supposed to be guarding that exact thing.

A test that matches nothing passes. Each entry below names the assertion the
mutation should trip, so a mutation that merely fails SOMEWHERE does not count
as proof -- a shape change that silently stops matching would look identical.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SC   = ROOT / "engine/03b_score.py"
PS   = ROOT / "atlas/risk/position_sizing.py"
AE   = ROOT / "atlas/execution/atlas_entry.py"
TEST = ROOT / "tests/test_gate_bypass.py"

MUTATIONS = [
    # ── FIX 1 ─────────────────────────────────────────────────────
    ("delete the re-check entirely (the hole, reopened)", SC,
     None, None, "DELETE_RECHECK", "score_vectorized re-disqualifies ranging + no-BOS"),
    ("re-check forgives every gate", SC,
     "_recheck = [(n, c) for n, c in conds.items() if n not in _forgiven]",
     "_recheck = []", None, "score_vectorized re-disqualifies ranging + no-BOS"),
    ("re-check forgives nothing (kills the accumulation screen)", SC,
     "_forgiven = set(ACCUMULATION_KILLERS)", "_forgiven = set()", None,
     "score_vectorized still forgives ranging alone"),
    ("forgive only one of the two killers", SC,
     "_forgiven = set(ACCUMULATION_KILLERS)",
     "_forgiven = {'adq_ranging_market'}", None,
     "score_vectorized still forgives quiet alone"),
    ("re-list the killers locally instead of importing", SC,
     "_forgiven = set(ACCUMULATION_KILLERS)",
     "_forgiven = {'very_low_volume', 'adq_ranging_market'}", None,
     "the forgiven set is imported, not re-listed"),
    ("run the re-check BEFORE the screen instead of after", SC,
     None, None, "MOVE_BEFORE", "the re-check runs INSIDE the screen branch"),
    ("renumber the gate bits (swap 2 and 5)", SC,
     '    "warmup", "no_trade_zone", "very_low_volume", "atr_too_high",\n'
     '    "bear_regime_no_reversal", "adq_ranging_market",',
     '    "warmup", "no_trade_zone", "adq_ranging_market", "atr_too_high",\n'
     '    "bear_regime_no_reversal", "very_low_volume",', None,
     "and in the same order"),
    # ── FIX 2 ─────────────────────────────────────────────────────
    ("delete the oversized refusal from the sizer", PS,
     None, None, "DELETE_REFUSAL", "MRF at Rs118,860.00 is refused"),
    ("compare against MAX_NOTIONAL (100k) not the target (10k)", PS,
     "if entry_price > target_notional:",
     "if entry_price > MAX_NOTIONAL_PER_TRADE:", None,
     "SOLARINDS at Rs18,808.00 is refused"),
    ("loosen the boundary to >=", PS,
     "if entry_price > target_notional:",
     "if entry_price >= target_notional:", None,
     "the boundary is strict >, so exactly at target is allowed"),
    ("drop the reason_code from the refusal", PS,
     '"reason_code": REASON_OVERSIZED,', '', None,
     "MRF at Rs118,860.00 is refused"),
    ("reuse the generic code instead of its own", PS,
     'REASON_OVERSIZED = "OVERSIZED"', 'REASON_OVERSIZED = "SIZE"', None,
     "the reason code is its own"),
    ("round-half-up becomes floor (CSBBANK 33 -> 32)", PS,
     "return max(m, int(floor(n / m + 0.5)) * m)",
     "return max(m, int(floor(n / m)) * m)", None,
     "CSBBANK still sizes to 33"),
    ("entry gate stops mapping the code to a status", AE,
     'return {"status": f"SKIPPED_{_rc}" if _rc else "REJECTED_SIZE",',
     'return {"status": "REJECTED_SIZE",', None,
     "the entry gate maps reason_code to its own status"),
    ("restore the warning-only oversized branch", AE,
     None, None, "RESTORE_WARN",
     "and no longer merely logs on `oversized`"),
]

DELETIONS = {
    "DELETE_RECHECK": (SC, r"\n        # ── RE-CHECK EVERY GATE.*?forgiven: \{sorted\(_forgiven\)\}\)\"\)\n"),
    "DELETE_REFUSAL": (PS, r"\n    if entry_price > target_notional:\n.*?\"sizing_basis\": SIZING_BASIS\}\n"),
}

import os

def run_test():
    # NUKE THE BYTECODE CACHE. The gate-renumber mutation swaps two names of
    # different length inside one tuple, so the file's SIZE is unchanged; with
    # mtime at filesystem granularity the stale .pyc validated and the MUTATED
    # bytecode was reused on the FOLLOWING mutation too. One mutation then
    # reported a failure that belonged to the previous one.
    for d in ROOT.rglob("__pycache__"):
        shutil.rmtree(d, ignore_errors=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, str(TEST)], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr

def apply(mut):
    label, path, old, new, special, _ = mut
    src = path.read_text()
    if special in DELETIONS:
        p, pat = DELETIONS[special]
        n = len(re.findall(pat, src, re.S))
        if n != 1:
            return f"deletion pattern matched {n} times, expected 1"
        path.write_text(re.sub(pat, "\n", src, flags=re.S))
        return None
    if special == "MOVE_BEFORE":
        m = re.search(r"\n        # ── RE-CHECK EVERY GATE.*?forgiven: \{sorted\(_forgiven\)\}\)\"\)\n",
                      src, re.S)
        if not m:
            return "could not locate the re-check block"
        block = m.group(0)
        src = src.replace(block, "\n")
        # Re-insert ABOVE the screen call, dedented out of the branch.
        anchor = "    if screen is not None:\n        df = screen(df)\n"
        if src.count(anchor) != 1:
            return "screen anchor not unique"
        moved = "\n".join(l[4:] if l.startswith("        ") else l
                          for l in block.splitlines())
        path.write_text(src.replace(anchor, moved + "\n" + anchor))
        return None
    if special == "RESTORE_WARN":
        anchor = '    if sizing.get("qty", 0) <= 0:\n'
        if src.count(anchor) != 1:
            return "gate 5 anchor not unique"
        warn = ('    if sizing.get("oversized"):\n'
                '        log.warning("oversized, continuing anyway")\n')
        path.write_text(src.replace(anchor, warn + anchor))
        return None
    if src.count(old) != 1:
        return f"anchor matched {src.count(old)} times, expected 1"
    path.write_text(src.replace(old, new))
    return None

# Baseline must be green, or nothing below means anything.
rc, out = run_test()
print(f"BASELINE  rc={rc}  {'PASSED' if rc == 0 else 'FAILED — ABORTING'}")
if rc != 0:
    print(out[-2000:]); sys.exit(1)
print()
print(f"{'mutation':<56} {'test':<8} {'caught by the right assertion?'}")
print("-" * 112)

backups = {p: p.read_text() for p in (SC, PS, AE)}
bad = []
for mut in MUTATIONS:
    label, path, *_ , want = mut
    for p, t in backups.items():
        p.write_text(t)
    err = apply(mut)
    if err:
        print(f"{label:<56} {'SETUP':<8} {err}")
        bad.append(f"{label}: {err}")
        continue
    rc, out = run_test()
    failed_on = [l.split(": got")[0].strip("  - ").strip()
                 for l in out.splitlines() if l.strip().startswith("- ")]
    hit = any(want in f for f in failed_on)
    status = "FAILED" if rc != 0 else "passed"
    verdict = ("yes" if hit else
               f"NO — tripped {failed_on[:2] or 'nothing'}")
    print(f"{label:<56} {status:<8} {verdict}")
    if rc == 0 or not hit:
        bad.append(f"{label}: rc={rc}, tripped={failed_on}")

for p, t in backups.items():
    p.write_text(t)
rc, out = run_test()
print("-" * 112)
print(f"RESTORED  rc={rc}  {'PASSED' if rc == 0 else 'FAILED — SOURCE NOT CLEAN'}")
if bad or rc != 0:
    print(f"\n{len(bad)} mutation(s) survived or tripped the wrong assertion:")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print(f"\nALL {len(MUTATIONS)} MUTATIONS CAUGHT, each by the assertion guarding it.")
