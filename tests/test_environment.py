"""
Does this interpreter run what production runs?

WHY THIS IS A TEST AND NOT A README LINE. On 2026-10-02 three defects reached
production in one evening and every one of them was green locally, because the dev
venv ran pandas 2.3.3 on Python 3.9 and the box runs pandas 3.0.3 on Python 3.13.
The worst of them — assigning int64 values into an int16 column — is a
FutureWarning that silently succeeds on pandas 2 and a TypeError on pandas 3. 03b
crashed at 9pm and nothing published.

A suite that executes different library semantics than production is not a suite
for that class of defect at all. This file makes the divergence a red test instead
of a crash, which is the only form of the fix that survives someone rebuilding a
venv in six months.

It reads the pins out of requirements.lock.txt rather than repeating them, so the
lock stays the single place a version is written down.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._srcutil import executable_source   # after sys.path, deliberately

LOCK = ROOT / "requirements.lock.txt"

# pandas 3.0 requires >= 3.11. The box's exact minor is not yet confirmed; this is
# what the venv is built on. A mismatch here is a smaller risk than a library
# mismatch but the same kind, so it is reported rather than ignored.
PYTHON_EXPECTED = (3, 13)
PYTHON_MINIMUM = (3, 11)          # below this, pandas 3 cannot install at all

# The three the box was read for. Anything else in the lock is locally resolved.
VERIFIED = ("pandas", "numpy", "pyarrow")

FAILS = []
WARNS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def soft(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'WARN'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        WARNS.append(f"{name}: {detail}")


def locked() -> dict:
    """{package: version} from the lock, comments stripped."""
    out = {}
    if not LOCK.exists():
        return out
    for line in LOCK.read_text().splitlines():
        line = line.split("#")[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s]+)$", line)
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


print("\n── the lock exists and says what it is ──")
check("requirements.lock.txt is present", LOCK.exists())
pins = locked()
check("it pins packages", len(pins) > 10, f"{len(pins)} found")
txt = LOCK.read_text() if LOCK.exists() else ""
check("it records WHY it exists", "2026-10-02" in txt and "FutureWarning" in txt)
for p in VERIFIED:
    check(f"  {p} is pinned", p in pins, "missing from the lock")

print("\n── the interpreter can host the pinned pandas ──")
cur = sys.version_info[:2]
check(f"Python >= {PYTHON_MINIMUM[0]}.{PYTHON_MINIMUM[1]} (pandas 3 requires it)",
      cur >= PYTHON_MINIMUM, f"running {cur[0]}.{cur[1]}")
soft(f"Python is {PYTHON_EXPECTED[0]}.{PYTHON_EXPECTED[1]} as the lock assumes",
     cur == PYTHON_EXPECTED, f"running {cur[0]}.{cur[1]}")

print("\n── the running versions match the lock ──")
import importlib

for p in VERIFIED:
    want = pins.get(p)
    try:
        mod = importlib.import_module(p)
        got = getattr(mod, "__version__", None)
    except Exception as e:
        got = f"<import failed: {type(e).__name__}>"
    check(f"{p} == {want}", want is not None and got == want,
          f"lock says {want}, running {got}")

print("\n── the pandas 3 behaviour the box depends on ──")
# Not a version string: the actual semantic that broke production. If a future
# pandas quietly goes back to widening, these pins are no longer protecting
# anything and the invariant tests elsewhere become the only guard.
import numpy as np
import pandas as pd

df = pd.DataFrame({"g": np.zeros(4, dtype=np.int16)})
mask = np.array([True, True, False, False])
raised = None
try:
    df.loc[mask, "g"] = np.array([4, 4], dtype=np.int64)
except Exception as e:
    raised = type(e).__name__
check("a partial int64 -> int16 masked set RAISES, as on the box",
      raised == "TypeError",
      f"got {raised or 'no error, dtype now ' + str(df['g'].dtype)}")

# and pandas still has no Series shift, which is the other crash
s = pd.Series([1, 2], dtype="int64")
sh = None
try:
    s << 1
except TypeError:
    sh = "TypeError"
check("Series << int still raises (the first crash)", sh == "TypeError",
      f"got {sh or 'it worked'}")

print("\n── no module silences deprecation warnings at import ──")
# warnings.filterwarnings("ignore") in a pipeline module overrides PYTHONWARNINGS
# and hid the year of notice pandas gave about the dtype change. Four stages had
# it.
# EXECUTABLE SOURCE, and a regex with the closing paren. This check has now been
# wrong twice in the same five minutes, both times by reading something that is not
# code:
#   1. a substring search for 'filterwarnings("ignore")' matched inside
#      'filterwarnings("ignore", category=RuntimeWarning)' -- the narrowed call
#      that IS the fix;
#   2. with the paren required, it matched the explanatory COMMENT above each
#      narrowed call, which quotes the old blanket form in order to say it is gone.
# Seventh occurrence of a check failing on its own prose in this repo.
# tests/_srcutil.py strips comments and docstrings; the paren distinguishes the
# bare one-argument form, which is the only form that is a blanket suppression.
BLANKET = re.compile(
    r"""warnings\.(?:filterwarnings|simplefilter)\(\s*["']ignore["']\s*\)""")
offenders = []
for py in sorted(ROOT.glob("engine/*.py")) + sorted(ROOT.glob("atlas/**/*.py")) \
        + sorted(ROOT.glob("meridian/*.py")) + sorted(ROOT.glob("backtest/*.py")):
    try:
        body = executable_source(py)
    except SyntaxError:
        continue                      # not ours to parse; a linter's problem
    for m in BLANKET.finditer(body):
        offenders.append(str(py.relative_to(ROOT)))
check("no blanket warning suppression in the pipeline", not offenders,
      "; ".join(offenders[:6]))
# and the narrowed form must still be present where the noise was real, or someone
# has deleted the suppression entirely and the logs are now unreadable
narrowed = sum(1 for py in sorted(ROOT.glob("engine/*.py"))
               if "category=RuntimeWarning" in py.read_text(encoding="utf-8",
                                                            errors="replace"))
soft("the four noisy stages still suppress RuntimeWarning by name", narrowed >= 4,
     f"{narrowed} of 4")

print()
for w in WARNS:
    print(f"NOTE  {w}")
if FAILS:
    print(f"\nFAILED {len(FAILS)}: {FAILS}")
    print("\nThe dev environment does not match production. Fix it before trusting")
    print("any other suite — see the header of requirements.lock.txt.")
    sys.exit(1)
print("Environment matches the production lock.")
