#!/usr/bin/env python3
"""A CARD'S CAUSE HAS THREE STATES, END TO END.

  found        a filing was selected and is cited
  none_found   NSE answered and nothing usable came back
  not_checked  NSE could not be asked

Before 2026-11-09 there were two: attribute() set checked = bool(docs) or True,
and fetch_filings returned [] on any failure, so an unreachable NSE printed "No
disclosed trigger found". And home.html's eventCard read payload fields no card
carries, so every event printed that sentence -- including the ones citing a
filing. This test runs the real fetch, the real card builder and validator, and
the real page functions (in node). Nothing reaches NSE or Supabase.
"""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging                                                  # noqa: E402
logging.basicConfig(level=logging.CRITICAL)
from radar import cards, documents as D                         # noqa: E402
from radar.config import load                                   # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<64} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


class Sess:
    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises = status, body, raises

    def get(self, *a, **k):
        if self.raises:
            raise self.raises
        s = self

        class R:
            status_code = s.status

            def json(self):
                if isinstance(s.body, Exception):
                    raise s.body
                return s.body
        return R()


ON = date(2026, 11, 6)
D.REQUEST_DELAY = 0
print("\nFETCH: None when NSE could not be asked, [] when it answered with nothing")
check("HTTP 503 -> None", D.fetch_filings("X", ON, session=Sess(503)), None)
check("network error -> None", D.fetch_filings("X", ON, session=Sess(raises=OSError("x"))), None)
check("garbage body -> None", D.fetch_filings("X", ON, session=Sess(200, ValueError("bad"))), None)
check("a dict, not a list -> None", D.fetch_filings("X", ON, session=Sess(200, {"err": 1})), None)
check("answered, empty -> []", D.fetch_filings("X", ON, session=Sess(200, [])), [])

print("\nATTRIBUTE: the state follows what happened")
CFG = load()
real = D.fetch_filings
D.fetch_filings = lambda *a, **k: None
a = D.attribute("X", ON, CFG)
check("unreachable NSE -> not_checked", a["cause_state"], D.CAUSE_NOT_CHECKED)
check("  with the not-checked sentence", a["cause_text"], D.UNCHECKED_CAUSE_TEXT)
check("  and checked False", a["checked"], False)
D.fetch_filings = lambda *a, **k: []
a = D.attribute("X", ON, CFG)
check("answered with nothing -> none_found", a["cause_state"], D.CAUSE_NONE_FOUND)
check("  with the no-trigger sentence", a["cause_text"], D.NO_CAUSE_TEXT)
check("  and checked True", a["checked"], True)
D.fetch_filings = real
check("the two sentences differ", D.NO_CAUSE_TEXT != D.UNCHECKED_CAUSE_TEXT, True)
check("the not-checked sentence claims no absence",
      "no disclosed trigger found" in D.UNCHECKED_CAUSE_TEXT.lower(), False)
check("had_recent_filing: None when unreachable",
      (lambda: (setattr(D, "fetch_filings", lambda *a, **k: None),
                D.had_recent_filing("X", ON, CFG))[1])(), None)
D.fetch_filings = real

print("\nCARDS: three states build, validate, and disagreements are refused")
EV = {"symbol": "TESTSYM", "event_date": ON, "trigger_type": "shock_1d",
      "direction": "up", "move_pct": 10.0, "vol_multiple": 20.0, "close": 100.0,
      "rank": 1, "heat": 5.0, "severity": 5.0, "sector": "IT", "delivery_pct": 50.0}
CAUSES = {
    "found": {"cause_state": "found", "cause_text": "Outcome of Board Meeting",
              "cause_url": "https://nsearchives.nseindia.com/x.pdf",
              "cause_published_at": None, "cause_sourced": True, "checked": True},
    "none_found": {"cause_state": "none_found", "cause_sourced": False, "checked": True},
    "not_checked": {"cause_state": "not_checked", "cause_sourced": False, "checked": False},
}
built = {}
for st, c in CAUSES.items():
    p = cards.build(EV, [], c, None, 0, ON, CFG)
    built[st] = p
    check(f"{st}: state carried", p["q01"]["why"]["state"], st)
    check(f"{st}: validates", cards.validate(p)[0], "passed")
    check(f"{st}: provenance.cause_checked", p["provenance"]["cause_checked"], st != "not_checked")
legacy = cards.build(EV, [], {"cause_sourced": False, "checked": True}, None, 0, ON, CFG)
check("a cause with no state is NOT CHECKED, never none_found",
      legacy["q01"]["why"]["state"], "not_checked")
bad = json.loads(json.dumps(built["not_checked"], default=str))
bad["q01"]["why"]["cause_text"] = D.NO_CAUSE_TEXT
check("not_checked carrying the no-trigger sentence is REFUSED", cards.validate(bad)[0], "failed")
bad = json.loads(json.dumps(built["none_found"], default=str))
bad["provenance"]["cause_checked"] = False
check("none_found with cause_checked False is REFUSED", cards.validate(bad)[0], "failed")
bad = json.loads(json.dumps(built["none_found"], default=str))
bad["q01"]["why"]["state"] = None
check("a missing state is REFUSED", cards.validate(bad)[0], "failed")

import shutil as _shutil                                         # noqa: E402
if not _shutil.which("node"):
    print("\nPAGES: SKIPPED — node is not installed here (the box has none); "
          "the page checks run where node exists")
    if failures:
        print(f"FAILED — {len(failures)}")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASSED (page checks skipped: no node)")
    sys.exit(0)
print("\nPAGES: the real functions, run in node")
home = (ROOT / "home.html").read_text()
flash = (ROOT / "flash.html").read_text()


def fn(src, name):
    """The function's full text, by brace matching from its declaration."""
    i = src.find("function " + name + "(")
    if i < 0:
        failures.append(f"page has no function {name}")
        return ""
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    return ""


_ext = re.search(r"var EXT_LINK=.*?;\n", home)
js = ((_ext.group(0) if _ext else "") +
      "\n".join(fn(home, n) for n in ("sign", "pct", "esc", "extLink", "eventCard")))
cases = {st: {"symbol": "TESTSYM", "trigger_type": "shock_1d", "move_pct": 10.0,
              "payload": json.loads(json.dumps(p, default=str))} for st, p in built.items()}
cases["no_card"] = {"symbol": "TESTSYM", "trigger_type": "shock_1d", "move_pct": 1, "payload": {}}
prog = js + "\nvar C=" + json.dumps(cases) + ";var o={};for(var k in C)o[k]=eventCard(C[k]);console.log(JSON.stringify(o));"
try:
    _r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    if _r.returncode:
        failures.append(f"node: {_r.stderr[:300]}")
    out = json.loads(_r.stdout)
except Exception as e:
    out = {}
    failures.append(f"node could not run eventCard: {e}")
check("home: found shows the filing subject", "Outcome of Board Meeting" in out.get("found", ""), True)
check("  and links the exchange filing", "nsearchives" in out.get("found", ""), True)
check("home: none_found shows the no-trigger sentence",
      "No disclosed trigger found" in out.get("none_found", ""), True)
check("home: not_checked shows the not-checked sentence",
      "could not be checked" in out.get("not_checked", ""), True)
check("  and never the no-trigger sentence",
      "No disclosed trigger found" in out.get("not_checked", "x"), False)
check("home: no card claims nothing about the cause",
      "trigger" in out.get("no_card", "trigger").lower(), False)
check("flash: the Hot 10 fallback claims no absence",
      "r.cause_text||'No disclosed trigger found" in flash, False)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — found, none found, and could not check are three different things on the page")
