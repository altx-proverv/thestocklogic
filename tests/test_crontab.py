"""
Does the schedule reference anything that no longer exists?

WHY. atlas/signal/decay.py was deleted on 2026-10-02 as dead code. Its cron line
survived it: `*/10 3-10 * * 1-5`, forty-eight invocations a weekday, every one of
them failing with a missing-file error into reports/atlas.log. Three days, nobody
reading that log, and the only reason it surfaced is that the crontab was finally
pasted into a conversation.

The reverse gap was just as quiet: market_flash, reject_sample, excursions and
score_live_outcomes were written, tested, merged into deploy/*.cron -- and never
installed. signal_excursions and reject_sample are empty because nothing has ever
run them, which I had put down to "the cron fires tonight".

So both directions are checked here: every path the schedule names must exist, and
every job the repo ships must be IN the schedule.

deploy/crontab.box is the single authority. The four fragments it replaced were
four places to look, which is how a collision between update_outcomes and
daily_report sat on the same minute unnoticed.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CRON = ROOT / "deploy/crontab.box"
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n        {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


print("\n── the install instruction cannot destroy the credential header ──")
# The first version of this file said "REPLACE, NEVER APPEND" and gave
# `crontab deploy/crontab.box` as the command. The live crontab carries nineteen
# environment variables holding broker tokens and the Supabase service key; this
# file carries three non-secret ones. Running that command would have deleted all
# nineteen, and nothing would have errored at install time -- every job would simply
# start failing to authenticate.
_hdr = CRON.read_text(encoding="utf-8") if CRON.exists() else ""
check("the header says MERGE, not replace",
      "MERGE" in _hdr and "DO NOT RUN `crontab deploy/crontab.box`" in _hdr)
check("it says why — the credential header is not in this file",
      "CARRIES NO CREDENTIALS" in _hdr)
check("it gives a command that preserves the variables",
      "crontab -l | grep -E" in _hdr and "A-Za-z0-9_]*=" in _hdr)
check("it still warns against appending", "APPEND EITHER" in _hdr)

print("\n── no job sources a file cron cannot read ──")
# market_flash produced NO LOG FILE on its first night: `. /etc/atlas.env` is
# root-owned and chmod 600, cron runs as ubuntu, the source failed, the && chain
# short-circuited and python never ran -- so the redirect never created the file. A
# job that fails BEFORE its redirect leaves no trace anywhere, which is the worst
# way for a scheduled job to fail. The pattern came from the systemd units, which
# run as root with EnvironmentFile.
_c = CRON.read_text(encoding="utf-8") if CRON.exists() else ""
_execlines = [l for l in _c.splitlines()
              if l.strip() and not l.lstrip().startswith("#")]
rooted = [l[:60] for l in _execlines if "/etc/atlas.env" in l]
check("no job sources /etc/atlas.env", not rooted, f"{rooted}")
check("the reason is recorded in the file", "root-owned and chmod 600" in _c)

print("\n── the authority exists and is the only one ──")
check("deploy/crontab.box exists", CRON.exists())
src = CRON.read_text(encoding="utf-8") if CRON.exists() else ""
strays = sorted(p.name for p in (ROOT / "deploy").glob("*.cron"))
check("no competing .cron fragments remain", not strays, f"found {strays}")

jobs = [l for l in src.splitlines()
        if l.strip() and not l.lstrip().startswith("#")
        and re.match(r"^\s*([0-9*/,-]+\s+){4}[0-9*/,-]+\s+\S|^\s*@reboot", l)]
check("it has the expected number of jobs", 20 <= len(jobs) <= 40, str(len(jobs)))

# EXECUTABLE LINES ONLY, NOT THE WHOLE FILE. The first version of this scanned
# `src` and failed on the comment at the top of crontab.box that names
# atlas/signal/decay.py in order to record that it was REMOVED. Eighth time a check
# in this repo has failed on its own rationale; a cron comment starts with '#' and
# `jobs` already excludes them.
EXEC = "\n".join(jobs)

print("\n── a script cron runs BY FILE PATH must be importable that way ──")
# THE BUG THIS EXISTS FOR. On 2026-10-05, commit 6125216 added an unguarded
# `from engine.paths import SIGNALS_DIR, ALL_ROWS` to engine/03b_score.py. cron
# invokes that script by FILE PATH, which puts engine/ on sys.path and NOT the repo
# root, so the import could not resolve. The script died before opening its log --
# leaving no 03b log to find -- and because the chain is &&-joined, 06_push and
# mark_signals never ran either. Three sessions lost, behind a page that said only
# "Nothing published".
#
# A package import is safe under file-path invocation if EITHER the file puts the
# repo root on sys.path first, OR the import is guarded by a try/except
# ImportError with a bare-module fallback. Neither, and the script cannot start.
import ast as _ast


def _pkg_import_risk(path):
    """[(lineno, module)] for package imports that cannot resolve by file path."""
    src = Path(path).read_text(encoding="utf-8")
    tree = _ast.parse(src)
    # does the module put the REPO ROOT on sys.path, and on which line?
    boot = None
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Call):
            txt = _ast.unparse(n)
            if ("sys.path.insert" in txt or "sys.path.append" in txt) and \
               ("parents[1]" in txt or "parent.parent" in txt):
                boot = n.lineno if boot is None else min(boot, n.lineno)
    # which imports sit inside a try/except ImportError with a fallback?
    guarded = set()
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Try) and n.handlers:
            names = {h.type.id for h in n.handlers
                     if isinstance(h.type, _ast.Name)}
            names |= {e.id for h in n.handlers
                      if isinstance(h.type, _ast.Tuple)
                      for e in h.type.elts if isinstance(e, _ast.Name)}
            if names & {"ImportError", "ModuleNotFoundError", "Exception"}:
                for sub in _ast.walk(n):
                    if isinstance(sub, (_ast.Import, _ast.ImportFrom)):
                        guarded.add(sub.lineno)
    risky = []
    for n in _ast.walk(tree):
        mods = []
        if isinstance(n, _ast.ImportFrom) and n.module:
            mods = [n.module]
        elif isinstance(n, _ast.Import):
            mods = [a.name for a in n.names]
        for m in mods:
            if "." not in m or m.split(".")[0] not in (
                    "engine", "atlas", "meridian", "tools", "tests", "backtest"):
                continue
            if n.lineno in guarded:
                continue
            if boot is not None and boot < n.lineno:
                continue
            risky.append((n.lineno, m))
    return risky


_bypath = set()
for l in jobs:
    for m in re.finditer(r"python3?\s+((?:engine|atlas|scripts|tools)/[A-Za-z0-9_/]+\.py)", l):
        _bypath.add(m.group(1))
check("cron does invoke scripts by file path", len(_bypath) > 10, str(len(_bypath)))
_allrisk = {}
for sc in sorted(_bypath):
    if not (ROOT / sc).exists():
        continue
    r = _pkg_import_risk(ROOT / sc)
    if r:
        _allrisk[sc] = r
check("no file-path script has an unresolvable package import", not _allrisk,
      "; ".join(f"{k} line {v[0][0]} imports {v[0][1]}" for k, v in _allrisk.items()))
check("03b specifically is safe", "engine/03b_score.py" not in _allrisk)
check("  and it does carry the bootstrap",
      "RUN-ANYWHERE BOOTSTRAP" in (ROOT / "engine/03b_score.py").read_text(encoding="utf-8"))

print("\n── the chain cannot report success having published nothing ──")
_push = (ROOT / "engine/06_push_supabase.py").read_text(encoding="utf-8")
check("06_push has a completion check", "COMPLETION CHECK" in _push)
check("  it READS BACK rather than trusting the POST",
      "could not read back the batch" in _push)
check("  zero published exits non-zero", "_chain_failed" in _push
      and "sys.exit(1)" in _push)
check("  and alerts on Telegram", "SIGNAL CHAIN PUBLISHED NOTHING" in _push)

print("\n── every path it runs exists ──")
missing = []
for m in re.finditer(r"(?:^|\s)((?:engine|atlas|scripts|tools)/[A-Za-z0-9_/]+\.(?:py|sh))", EXEC):
    rel = m.group(1)
    if not (ROOT / rel).exists():
        missing.append(rel)
check("no cron line names a deleted file", not missing, f"MISSING: {sorted(set(missing))}")

print("\n── every module it runs is importable as named ──")
badmod = []
for m in re.finditer(r"python3?\s+-m\s+([a-z_][a-z_0-9.]*)", EXEC):
    mod = m.group(1)
    rel = Path(mod.replace(".", "/") + ".py")
    if not (ROOT / rel).exists():
        badmod.append(mod)
check("no -m target is missing", not badmod, f"MISSING: {sorted(set(badmod))}")

print("\n── the deleted job is gone and cannot come back ──")
check("no job runs decay.py", "decay.py" not in EXEC)
check("  and the file is named in the comments, as removed",
      "decay.py" in src and "decay.py" not in EXEC)
check("  and the file really is deleted", not (ROOT / "atlas/signal/decay.py").exists())
check("no job runs orb_engine", "orb_engine" not in EXEC)

print("\n── everything the repo ships is scheduled ──")
# A job written, tested and merged but never installed is indistinguishable from a
# job that is broken: the table stays empty either way.
MUST_RUN = ["engine.market_flash", "engine.reject_sample", "engine.excursions",
            "engine.score_live_outcomes", "engine.learning_loop",
            "meridian.iv_recorder", "engine.tier1_fetch", "engine.fundamentals"]
for mod in MUST_RUN:
    check(f"  {mod} is scheduled", mod in EXEC)
for script in ["engine/rbe_startup.py", "engine/rbe_engine.py",
               "engine/01b_download_bhavcopy.py", "engine/03b_score.py",
               "engine/06_push_supabase.py", "engine/mark_signals.py",
               "engine/update_outcomes.py", "engine/btst_engine.py"]:
    check(f"  {script} is scheduled", script in EXEC)

print("\n── ordering constraints that are dependencies, not preferences ──")


def minute_of(pattern):
    """(hour, minute) of the first job matching pattern, UTC."""
    for l in jobs:
        if pattern in l:
            p = l.split()
            return int(p[1]), int(p[0])
    return None


def after(a, b):
    """is a strictly later in the day than b"""
    return a is not None and b is not None and a > b


chain = minute_of("01b_download_bhavcopy")
check("the EOD chain runs at 13:05 UTC (18:35 IST)", chain == (13, 5), str(chain))
check("update_outcomes is after the chain",
      after(minute_of("update_outcomes"), chain), str(minute_of("update_outcomes")))
check("reject_sample is after the chain",
      after(minute_of("reject_sample"), chain))
check("excursions runs AFTER reject_sample, so tonight's sample is walked tonight",
      after(minute_of("engine.excursions"), minute_of("reject_sample")))
check("score_live_outcomes runs after excursions",
      after(minute_of("score_live_outcomes"), minute_of("engine.excursions")))
# The learning loop reads what every other job wrote, so it must be last.
check("the learning loop runs after everything it reads",
      after(minute_of("learning_loop"), minute_of("score_live_outcomes"))
      and after(minute_of("learning_loop"), minute_of("engine.excursions"))
      and after(minute_of("learning_loop"), minute_of("update_outcomes")),
      str(minute_of("learning_loop")))
check("rbe_engine starts after rbe_startup builds the map",
      after(minute_of("rbe_engine"), minute_of("rbe_startup")))
check("both broker logins precede the range-map build",
      after(minute_of("rbe_startup"), minute_of("upstox_auto_login"))
      and after(minute_of("rbe_startup"), minute_of("zerodha_auto_login")))

print("\n── daily_report must not share a minute with what it reads ──")
# They sat on 35 13 together. daily_report reads signal_outcomes; update_outcomes
# writes it. Same minute means the report is built from however much of the update
# had landed -- a different number every night, and never the right one.
check("daily_report is strictly after update_outcomes",
      after(minute_of("daily_report"), minute_of("update_outcomes")),
      f"daily_report {minute_of('daily_report')} vs update_outcomes {minute_of('update_outcomes')}")

print("\n── no two jobs write the same log in the same minute ──")
slots = {}
for l in jobs:
    if l.lstrip().startswith("@"):
        continue
    p = l.split()
    key = (p[0], p[1])
    log = re.search(r">>\s*(\S+)", l)
    slots.setdefault((key, log.group(1) if log else "?"), []).append(l[:60])
clash = {k: v for k, v in slots.items() if len(v) > 1}
check("no log is appended by two jobs in one minute", not clash,
      f"{[(k[0], k[1].split('/')[-1]) for k in clash]}")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All crontab checks passed.")
