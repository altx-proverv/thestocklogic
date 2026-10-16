"""
THE SESSION STAND-DOWN — MECHANISM KEPT, TRIGGER NOT YET WIRED
==============================================================

REPLACES tests/test_sentiment_window.py, which was DELETED rather than
weakened. That file's sections 1-6 and 8 asserted the 24-of-30 confirm window
and the 36-of-45 reversal vote: the establishment arithmetic, the threshold
boundaries, the one-way reversal, MIXED never reversing, the window counting
cycles rather than candidate evaluations, fold() agreeing with the stateful
path, and the window's rows on the inventory and the 08:30 report. The operator
vetoed that mechanism on 2026-10-15, so the tests for it went with it -- a test
kept alive by loosening its assertion is worse than no test.

What carried over, because the behaviour carried over:

  from its section 6   a fired stand-down survives a restart, and a file from
                       another session does not carry into today
  from its section 7   exits are untouched by a stand-down
  from its section 9   the suite cannot write production state

Plus what is new: that NOTHING fires it. The mechanism is complete and
reachable only by an explicit call, and no code path makes that call. Tomorrow's
range-break check is the intended trigger.
"""

import os
import sys
import json
import tempfile
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<62}{str(got)[:16]:<18}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


def fresh():
    os.environ["ATLAS_SENTIMENT_STANDDOWN_FILE"] = tempfile.mktemp(suffix=".json")
    import atlas.config as C
    importlib.reload(C)
    import atlas.execution.sentiment_window as SW
    importlib.reload(SW)
    import atlas.reporting.telegram as TG
    TG.send = lambda *a, **k: None
    SW.reset_for_tests()
    return SW


def main() -> int:
    from _srcutil import executable_source
    w_src = executable_source(ROOT / "atlas" / "execution" / "sentiment_window.py")
    ae = executable_source(ROOT / "atlas" / "execution" / "atlas_entry.py")

    print("=" * 82)
    print("1 — THE VOTE IS GONE, NOT DISABLED")
    print("-" * 82)
    import atlas.config as C
    for name in ("SENTIMENT_ESTABLISH_WINDOW", "SENTIMENT_ESTABLISH_MIN",
                 "SENTIMENT_REVERSAL_WINDOW", "SENTIMENT_REVERSAL_MIN"):
        check(f"config has no {name}", hasattr(C, name), False)
    SW = fresh()
    for name in ("fold", "observe_cycle", "REASON_FORMING",
                 "REASON_NOT_ESTABLISHED"):
        check(f"the module has no {name}()", hasattr(SW, name), False)
    for gone in ("SENTIMENT_WINDOW_FORMING", "SENTIMENT_NOT_ESTABLISHED",
                 "establish", "reversal_counts", "shortfall"):
        check(f"  no trace of {gone!r} in the module", gone in w_src, False)
    check("the reason code that survives", SW.REASON_STAND_DOWN,
          "RANGE_BREAK_STAND_DOWN",
          "renamed from SENTIMENT_REVERSAL_* when the trigger arrived")
    sw = executable_source(ROOT / "tools" / "atlas_switches.py")
    zm = executable_source(ROOT / "atlas" / "execution" / "zerodha_morning.py")
    check("the inventory has no confirm-window row",
          "sentiment window (Gate 1 confirm)" in sw, False)
    check("  but still lists the stand-down", "session stand-down" in sw, True,
          "it can stop an entry, so it belongs in the complete list")
    check("the 08:30 report has no window line",
          "Confirm window" in zm, False,
          "a number every morning that governs nothing")

    print()
    print("=" * 82)
    print("2 — GATE 1 IS THE INSTANTANEOUS READ, WITH NO WARM-UP")
    print("-" * 82)
    check("the gate reads live_sentiment directly", "live_sentiment" in ae, True)
    check("  and no longer asks for a confirmed state",
          "permits" in ae, False)
    # NO WARM-UP CONCEPT LEFT IN THE GATE. Scoped to the vetoed vocabulary
    # rather than to the word "window", which still appears as the module name
    # the stand-down lives in.
    scoped = "\n".join(l for l in ae.splitlines()
                       if "import sentiment_window" not in l)
    check("  no minimum cycle count anywhere in the gate",
          any(x in scoped for x in ("establish", "ESTABLISH", "cycle_n",
                                    "observe_cycle")), False,
          '"unconfirmed" appears in an unrelated fill message, so the check '
          'names the vetoed vocabulary rather than the word "confirmed"')
    # The stand-down is still checked, and first.
    check("the gate still checks the stand-down", "sentiment_window" in ae, True)
    i_sd = ae.find("sentiment_window")
    i_ls = ae.find("live_sentiment")
    check("  before it reads the feed", 0 <= i_sd < i_ls, True,
          "a stood-down session must not spend a quote call per candidate")
    mo = executable_source(ROOT / "atlas" / "signal" / "market_open.py")
    check("the loop no longer advances a per-cycle window",
          "observe_cycle" in mo, False)

    print()
    print("=" * 82)
    print("3 — EXACTLY ONE THING FIRES IT")
    print("-" * 82)
    # INVERTED 2026-10-15, not deleted. This section asserted that NOTHING
    # called stand_down(), which was the correct invariant for the day the
    # mechanism shipped without a trigger. The trigger arrived, so the
    # invariant becomes the one that matters now: exactly one caller, and it is
    # the market range breaker. A second caller appearing is something to find
    # here rather than in a session.
    callers = []
    for d in ("atlas", "engine", "radar", "tools"):
        for q in (ROOT / d).rglob("*.py"):
            if q.name == "sentiment_window.py":
                continue
            # EXECUTABLE SOURCE, NOT RAW TEXT. A COMMENT IS NOT A CALL: on
            # 2026-10-16 a comment in atlas_entry.py explaining which module
            # fires the stand-down made this report two callers. Stripping
            # comments also means a real call can no longer hide behind one.
            try:
                t = executable_source(q)
            except Exception:
                t = q.read_text(encoding="utf-8", errors="ignore")
            if ".stand_down(" in t:
                callers.append(q.relative_to(ROOT).as_posix())
    check("exactly one module calls stand_down()", sorted(set(callers)),
          ["atlas/execution/range_breaker.py"],
          "the market range breaker, and nothing else")
    check("the module names its trigger",
          "range_breaker.market_check" in w_src or "range break" in w_src.lower(),
          True)
    SW = fresh()
    check("a fresh session is not stood down", SW.state()["stood_down"], False)
    check("  so the gate is not blocked by it",
          SW.state()["reason_code"], None)

    print()
    print("=" * 82)
    print("4 — WHEN IT IS FIRED, IT BLOCKS AND IT PERSISTS")
    print("-" * 82)
    SW = fresh()
    path = Path(os.environ["ATLAS_SENTIMENT_STANDDOWN_FILE"])
    s = SW.stand_down("RANGE_BREAK", "injected: the day's range broke")
    check("stand_down() sets it", s["stood_down"], True)
    check("  with the kind recorded", s["kind"], "RANGE_BREAK")
    check("  and the reason code", s["reason_code"], "RANGE_BREAK_STAND_DOWN")
    check("  it is written to disk", path.exists(), True)
    check("calling it twice does not re-stamp",
          SW.stand_down("OTHER", "again")["kind"], "RANGE_BREAK",
          "one-way, and the alert fires once")
    importlib.reload(SW)                        # a new process, same file
    check("a reloaded module still sees it", SW.state()["stood_down"], True,
          "forgetting it would re-arm trading: a fail-OPEN")
    # The entry gate must refuse while it is set.
    from atlas.execution import atlas_entry as AE
    saved = (AE.__dict__.get("sentiment_state"),)
    st, reason, code = AE.sentiment_state({})
    check("the entry gate refuses", st, "unknown")
    check("  under the stand-down code", code, "RANGE_BREAK_STAND_DOWN")
    side, _, blocked = AE.allowed_side({})
    check("  allowed_side permits no side", side, None)
    check("  and buckets it there", blocked, "RANGE_BREAK_STAND_DOWN")

    # A FILE FROM ANOTHER DAY IS HISTORY, NOT STATE.
    d = json.loads(path.read_text())
    d["session_date"] = "2001-01-01"
    path.write_text(json.dumps(d))
    importlib.reload(SW)
    check("yesterday's stand-down does not carry into today",
          SW.state()["stood_down"], False)
    # AN UNREADABLE FILE FAILS OPEN HERE, on purpose: it cannot prove a
    # stand-down fired, and disarming on a corrupt byte would be worse.
    path.write_text("{not json")
    importlib.reload(SW)
    check("a corrupt file is not treated as a stand-down",
          SW.state()["stood_down"], False,
          "the entry gate has its own independent refusals above this")

    print()
    print("=" * 82)
    print("5 — EXITS ARE UNTOUCHED BY A STAND-DOWN")
    print("-" * 82)
    for name, rel in (("exits", "atlas/execution/exits.py"),
                      ("preclose_gtt (15:20 handover)",
                       "atlas/execution/preclose_gtt.py"),
                      ("trade_management", "atlas/execution/trade_management.py"),
                      ("trailing", "atlas/risk/trailing.py")):
        src = (ROOT / rel).read_text(encoding="utf-8")
        clean = ("sentiment_window" not in src and "stood_down" not in src
                 and "SENTIMENT_REVERSAL" not in src)
        check(f"{name} never consults it", clean, True, rel)
    check("the module places no orders",
          any(x in w_src for x in ("place_order", "place_gtt", "modify_gtt",
                                   "cancel_order")), False)
    check("  and reads no position state",
          any(x in w_src for x in ("atlas_trades", "positions", "holdings")),
          False)
    check("the alert still says exits continue",
          "EXITS ARE UNAFFECTED" in (ROOT / "atlas" / "execution"
                                     / "sentiment_window.py").read_text(), True)
    for leg in ("stops", "targets", "trailing", "15:20 GTT handover"):
        check(f"  naming {leg}",
              leg in (ROOT / "atlas" / "execution"
                      / "sentiment_window.py").read_text(), True)

    print()
    print("=" * 82)
    print("6 — THE SUITE CANNOT WRITE PRODUCTION STATE")
    print("-" * 82)
    ra = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")
    check("run_all declares the drill",
          'os.environ["ATLAS_DRILL"] = "1"' in ra, True)
    check("  and redirects the stand-down file",
          "ATLAS_SENTIMENT_STANDDOWN_FILE" in ra, True)
    check("the module refuses the canonical path while drilling",
          "CANONICAL_FILE" in w_src and "is_drill" in w_src, True)
    os.environ["ATLAS_DRILL"] = "1"
    os.environ["ATLAS_SENTIMENT_STANDDOWN_FILE"] = SW.CANONICAL_FILE
    importlib.reload(C)
    importlib.reload(SW)
    live = Path(SW.CANONICAL_FILE)
    existed = live.exists()
    SW.stand_down("DRILL_TEST", "must not reach production")
    check("a drilling stand_down writes no live file",
          live.exists(), existed,
          "unchanged whether or not the real file was already there")

    print()
    print("=" * 82)
    print("PASS" if OK else "FAIL")
    print("=" * 82)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
