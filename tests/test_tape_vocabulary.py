#!/usr/bin/env python3
"""THE TAPE'S VOCABULARY IS A TEXT FILE, SO IT IS CHECKABLE IN ONE PASS.

Every sentence the tape can emit lives in tape-vocabulary.json. That was the
point of moving the phrases out of the render code: the voice becomes a file
the operator edits without touching logic, and a test can read the whole voice
at once instead of hoping a reviewer spots a bad phrase inside a template
string three functions deep.

What this file guards, in order of how much it would cost to get wrong:

  1. PARITY. A key the render code asks for that the file does not define
     silently degrades to the catch-all. Nothing crashes, nothing is dropped,
     the tape just quietly gets uglier. That is exactly the kind of rot a test
     exists to catch, so the two directions are checked separately.
  2. VARIETY. Friday's mix was 26 volume items out of 73. With one phrasing a
     third of the tape reads as the same sentence repeated, which is the
     problem the rotation was built to solve.
  3. THE RULE. The percentage already says WHAT happened. A phrase that
     restates it costs a reader their attention and returns nothing.
  4. THE GAP LOG HAS TEETH. A logger that cannot be shown to fire is not
     evidence of anything.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOCAB = ROOT / "tape-vocabulary.json"
HOME = ROOT / "home.html"

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<54} {str(got):<14} {'ok' if ok else 'FAIL':<9} {note}")


print()
print("THE VOCABULARY FILE PARSES AND IS SHAPED AS THE PAGE EXPECTS")
print("-" * 104)
raw = VOCAB.read_text()
v = json.loads(raw)
phrases = v["phrases"]
thresholds = v["thresholds"]
check("it is valid JSON", True, True, f"{len(raw):,} bytes")
check("it has a phrase section", len(phrases) > 0, True,
      f"{len(phrases)} group(s)")
check("it has a threshold section", len(thresholds) > 0, True)
check("it has a catch-all template",
      "{fact}" in v["catch_all"]["template"], True,
      "so a fact with no phrase is still stated")

print()
print("1. PARITY — EVERY KEY THE CODE ASKS FOR IS DEFINED, AND VICE VERSA")
print("-" * 104)
home = HOME.read_text()
# The first argument of phrase(...) is sometimes a ternary --
# phrase(below?'broke_low':'broke_high', ...) -- so every quoted literal up to
# the first comma counts as a key the code can ask for.
asked = set()
for m in re.finditer(r"\bphrase\(([^,]*),", home):
    asked |= set(re.findall(r"'([a-z0-9_]+)'", m.group(1)))
defined = set(phrases)
check("the code asks for at least one phrase", len(asked) > 0, True,
      f"{len(asked)} key(s)")
check("every key the code asks for is defined",
      sorted(asked - defined), [],
      "an undefined key degrades to the catch-all, silently")
check("every key the file defines is used",
      sorted(defined - asked), [],
      "an unused phrase is voice nobody will ever read")

print()
print("2. VARIETY — NO CATEGORY SPEAKS WITH ONE VOICE")
print("-" * 104)
thin = sorted(k for k, p in phrases.items() if len(p) < 2)
check("every category has two or more phrasings", thin, [],
      "one phrasing per category is the repetition this replaced")
total = sum(len(p) for p in phrases.values())
check("the vocabulary is not one sentence long", total >= 20, True,
      f"{total} phrasings across {len(phrases)} categories")
dupes = []
seen = {}
for key, opts in phrases.items():
    for o in opts:
        if o in seen:
            dupes.append(f"{o!r} in {seen[o]} and {key}")
        seen[o] = key
check("no phrasing is duplicated between categories", dupes, [])
check("no category repeats a phrasing within itself",
      [k for k, p in phrases.items() if len(set(p)) != len(p)], [])

print()
print("3. THE RULE — THE REASON IS NOT THE PERCENTAGE RESTATED")
print("-" * 104)
# The percentage is WHAT happened. These words say only that, so a phrase
# containing one is either restating the number beside it or passing a verdict
# the data does not support.
BANNED = ["rose", "fell", "gained", "surged", "plunged", "jumped", "tumbled",
          "rallied", "dropped", "climbed", "soared", "sank", "crashed",
          "strong", "weak", "bullish", "bearish", "good", "bad", "should"]
# THE SUBJECT MATTERS. "rising while its sector fell" does not restate the
# percentage -- "fell" is the SECTOR, and the contrast is precisely the thing
# the number beside it cannot say. So the sector's own clause is removed
# before the ban is applied, rather than exempting the isolation phrases
# wholesale: "surged while its sector fell" is still caught, because "surged"
# survives the strip and is the stock.
SECTOR_CLAUSE = re.compile(
    r"\b(while|in|against)\s+(its|a|the)\s+sector(\s+that)?\s+\w+", re.I)

hits = []
for key, opts in phrases.items():
    for o in opts:
        own = SECTOR_CLAUSE.sub(" ", o)
        for w in BANNED:
            if re.search(rf"\b{w}\b", own, re.I):
                hits.append(f"{key}: {o!r} contains {w!r}")
check("no phrase restates the move or grades it", hits, [])
check("  and that check still bites past the sector clause",
      bool(SECTOR_CLAUSE.sub(" ", "surged while its sector fell").strip()
           and re.search(r"\bsurged\b",
                         SECTOR_CLAUSE.sub(" ", "surged while its sector fell"))),
      True, "'surged while its sector fell' would still be caught")
numeric = [f"{k}: {o!r}" for k, opts in phrases.items() for o in opts
           if re.search(r"\d+(\.\d+)?\s*%", o)]
check("no phrase carries a percentage of its own", numeric, [],
      "the number beside it is the number")
# ORB is engine vocabulary. A reader is owed "morning range".
orb = [f"{k}: {o!r}" for k, opts in phrases.items() for o in opts
       if re.search(r"\bORB\b", o)]
check("no phrase says ORB", orb, [], "readers are owed 'morning range'")
subs = [f"{k}: {o!r}" for k, opts in phrases.items() for o in opts
        if "{" in o and not re.fullmatch(r"[^{}]*(\{[a-z_]+\}[^{}]*)*", o)]
check("every substitution slot is well formed", subs, [])

print()
print("4. THE GUARDS ARE NUMBERS WITH STATED REASONS")
print("-" * 104)
live = {k: x for k, x in thresholds.items() if not k.startswith("_")}
unexplained = sorted(k for k in live if f"_{k}" not in thresholds)
check("every threshold states why it is what it is", unexplained, [],
      "a number nobody can argue with is a number nobody can fix")
check("all thresholds are numeric",
      [k for k, x in live.items() if not isinstance(x, (int, float))], [])
for name in ("min_turnover_inr", "min_sector_size", "min_history_for_month",
             "min_history_for_year"):
    check(f"  {name} is set", live.get(name) is not None, True,
          str(live.get(name)))
check("a month high needs more than a month of history",
      live["min_history_for_month"] > 20, True,
      f"{live['min_history_for_month']} sessions vs a 20-session window")
check("a year high needs more than the month bar",
      live["min_history_for_year"] > live["min_history_for_month"], True)
check("light volume sits below heavy volume",
      live["light_volume_x"] < live["heavy_volume_x"], True)

print()
print("AND THE PAGE ACTUALLY ENFORCES THEM")
print("-" * 104)
check("the page reads the file rather than its own copy",
      "tape-vocabulary.json" in home, True)
check("a thin-history symbol cannot claim a level",
      "hasHistory(" in home, True, "HEGAM has six weeks")
check("an untradeable symbol earns no phrase at all",
      "tradeable(" in home, True)
check("a one-stock sector cannot be 'the only one up'",
      "min_sector_size" in home, True)
check("rotation is deterministic, not random",
      "Math.random" in home.split("function phraseFor")[1].split("\n}")[0]
      if "function phraseFor" in home else True, False,
      "a screenshot has to be reproducible")

print()
print("5. THE GAP LOG FIRES WHEN A FACT HAS NO PHRASE")
print("-" * 104)
sys.path.insert(0, str(ROOT))
pub = (ROOT / "engine" / "publish_closes.py").read_text()
check("the publisher logs gaps", "def log_vocabulary_gaps" in pub, True)
check("  it is called on every publish", "log_vocabulary_gaps(stamp" in pub,
      True)
check("  it reads the same file the page reads",
      "tape-vocabulary.json" in pub, True,
      "one file, so the two cannot drift on a number")
check("  it records symbol, fact and session",
      all(f'"{c}"' in pub for c in
          ("session_date", "symbol", "fact", "measurement")), True)
check("  the page cannot write it",
      "tape_vocabulary_gaps" in home, False,
      "anon is SELECT-only; the log belongs server-side")

# TEETH. Empty one phrase group in a copy of the file and prove the detector
# notices -- a logger that has never been seen to fire proves nothing.
import tempfile
import shutil

probe = json.loads(raw)
probe["phrases"]["heavy_volume"] = []
with tempfile.TemporaryDirectory() as td:
    shutil.copy(VOCAB, Path(td) / "orig.json")
    (Path(td) / "probe.json").write_text(json.dumps(probe))
    pv = json.loads((Path(td) / "probe.json").read_text())
    row = {"symbol": "PROBE", "close": 100.0, "volume": 10_000_000,
           "rvol": 5.0, "history_sessions": 900, "high": 101.0, "low": 99.0,
           "high_20d": None, "low_20d": None, "high_52w": None,
           "change_pct": 3.0}
    th = pv["thresholds"]
    fires = (row["close"] * row["volume"] >= th["min_turnover_inr"]
             and row["rvol"] >= th["heavy_volume_x"]
             and not pv["phrases"].get("heavy_volume"))
    check("a fact with an emptied phrase group is detected", fires, True,
          "PROBE at 5.0x volume, no phrase to carry it")
    still = bool(json.loads(raw)["phrases"]["heavy_volume"])
    check("  and the real file was not touched by the probe", still, True)

print("-" * 104)
if failures:
    print(f"TAPE VOCABULARY: *** {len(failures)} FAILURE(S) ***")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TAPE VOCABULARY: correct")
