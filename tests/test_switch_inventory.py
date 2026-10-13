"""
THE PAUSE MUST NAME WHAT SET IT, AND A DRILL MUST NOT SET IT
============================================================

2026-10-13, in full. ATLAS reported `observe_only: "operator PAUSED"` from
cycle 121 of a live session. No operator had sent /pause -- the bot log shows
the day's only directives were /resume, /status and /capital, all at 10:05-10:06
IST. What actually happened:

  tests/run_all.py includes tools/verify_long_only.py, which drives a real
  failure through breaker.halt() to prove the halt path works. It redirects
  ATLAS_HALT_FILE to a temp path so /var/lib/atlas/halt is untouched -- and
  halt() has three effects, of which the file is one. The other two are a PATCH
  of the shared atlas_state row and a real Telegram alert. Run on the box with
  production credentials, the suite paused the live engine mid-session.

  paused() then reported it as "operator PAUSED", because that string was
  returned for any PAUSED mode whatever wrote it. The cause was sitting in
  atlas_state.notes and nothing read it.

Two defects, two fences, both checked here: a drill cannot touch shared state,
and a pause reports its real source. Same principle as ad947f7 -- the mechanism
that acted is the mechanism that gets named.
"""

import os
import sys
import importlib
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<60}{str(got)[:16]:<18}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


def main() -> int:
    print("=" * 78)
    print("1 — THE NOTE SAYS WHO SET THE MODE")
    print("-" * 78)
    from atlas.reporting.directives import mode_source, MODE_SOURCE_TAGS
    cases = [
        ("SELF-HALT: DAY0_SL_FAILED at ... — RELIANCE", "ATLAS self-halt"),
        ("OPERATOR: /pause at 2026-10-13 10:00",        "operator directive"),
        ("RESUMED: operator at 2026-10-13 10:06",       "operator resume"),
        # pre-tag vocabulary, so history reads correctly instead of as unknown
        ("ATLAS self-halt at 2026-10-13 11:21 — X",     "ATLAS self-halt"),
        ("Paused by directive at 2026-10-07",           "operator directive"),
    ]
    for note, want in cases:
        check(f"{note[:44]!r}", mode_source({"notes": note})[0], want)
    # UNKNOWN IS NAMED, NOT GUESSED. A row edited by hand in the console, or
    # written before the tags existed, is exactly where defaulting to
    # "operator" would repeat the bug.
    check("an untagged note is not called an operator action",
          mode_source({"notes": "someone edited this by hand"})[0], "unknown")
    check("an empty note says the row records nothing",
          mode_source({"notes": ""})[0], "unrecorded")
    check("a missing state does not raise", mode_source(None)[0], "unrecorded")
    check("every tag maps to a distinct source",
          len(set(MODE_SOURCE_TAGS.values())), len(MODE_SOURCE_TAGS))

    print()
    print("=" * 78)
    print("2 — paused() REPORTS THE SOURCE, NOT A HARDCODED 'operator'")
    print("-" * 78)
    from atlas.signal import market_open as MO
    from atlas.risk import breaker as BR
    saved = (MO.get_agent_state, BR.is_halted)
    try:
        BR.is_halted = lambda *a, **k: (False, "")
        MO.get_agent_state = lambda *a, **k: {
            "mode": "PAUSED",
            "notes": "SELF-HALT: DAY0_SL_FAILED at 2026-10-13 11:21:23 IST — "
                     "RELIANCE: the day-0 stop-loss order would not place"}
        halted, why = MO.paused()
        check("a self-halt pause is halted", halted, True)
        check("  and does NOT say operator", "operator" in why.lower(), False, why[:50])
        check("  it names the self-halt", "self-halt" in why.lower(), True)

        MO.get_agent_state = lambda *a, **k: {
            "mode": "PAUSED", "notes": "OPERATOR: /pause at 2026-10-13 10:00"}
        halted, why = MO.paused()
        check("an operator pause still says operator",
              "operator" in why.lower(), True, why[:50])

        MO.get_agent_state = lambda *a, **k: {"mode": "NORMAL", "notes": ""}
        check("NORMAL is not paused", MO.paused()[0], False)
    finally:
        MO.get_agent_state, BR.is_halted = saved

    print()
    print("=" * 78)
    print("3 — A DRILL CANNOT TOUCH SHARED STATE")
    print("-" * 78)
    # The fence derives from the redirected sentinel, so a harness that moves
    # the halt file cannot forget to also declare itself.
    tmp = Path(tempfile.mkdtemp()) / "halt"
    os.environ["ATLAS_HALT_FILE"] = str(tmp)
    try:
        import atlas.config as C
        importlib.reload(C)
        import atlas.risk.breaker as B
        importlib.reload(B)

        drilling, why = B.is_drill()
        check("a redirected sentinel IS a drill", drilling, True, why[:48])
        check("  and the canonical path is not this one",
              str(B.SENTINEL) != str(C.HALT_FILE_CANONICAL), True)

        calls = {"patch": 0, "send": 0}
        import requests as _rq
        real_patch = _rq.patch
        _rq.patch = lambda *a, **k: calls.__setitem__("patch", calls["patch"] + 1)
        import atlas.reporting.telegram as TG
        real_send = TG.send
        TG.send = lambda *a, **k: calls.__setitem__("send", calls["send"] + 1)
        try:
            B.halt("TEST_KIND", "a drill must not reach production")
        finally:
            _rq.patch = real_patch
            TG.send = real_send

        check("the sentinel WAS written (locally)", tmp.exists(), True)
        check("  tagged so its source is classifiable",
              tmp.read_text().startswith("SELF-HALT:"), True)
        check("atlas_state was NOT patched", calls["patch"], 0,
              "this is the write that paused the live engine")
        check("no operator alert was sent", calls["send"], 0,
              "a drill that alerts is how trust in the alert is lost")

        # clear() is fenced the same way, or a drill would resume production
        calls["patch"] = 0
        _rq.patch = lambda *a, **k: calls.__setitem__("patch", calls["patch"] + 1)
        try:
            B.clear()
        finally:
            _rq.patch = real_patch
        check("clear() is fenced too", calls["patch"], 0,
              "a drill must not resume the live engine either")
        check("  and it still cleared the local sentinel", tmp.exists(), False)
    finally:
        os.environ.pop("ATLAS_HALT_FILE", None)
        os.environ.pop("ATLAS_DRILL", None)
        import atlas.config as C
        importlib.reload(C)
        import atlas.risk.breaker as B
        importlib.reload(B)

    check("with no redirect, the live engine is NOT a drill",
          B.is_drill()[0], False, "the fence must not disarm production")

    print()
    print("=" * 78)
    print("4 — THE HARNESS DECLARES ITSELF")
    print("-" * 78)
    from _srcutil import executable_source
    vl = executable_source(ROOT / "tools" / "verify_long_only.py")
    # executable_source unparses the AST, so quotes come back normalised.
    check("verify_long_only redirects the halt file",
          "os.environ['ATLAS_HALT_FILE']" in vl, True)
    check("  and sets ATLAS_DRILL", "os.environ['ATLAS_DRILL']" in vl, True)
    check("  and asserts the fence held", "is_drill()" in vl, True)
    check("it is registered in run_all (so this matters)",
          "tools.verify_long_only" in (ROOT / "tests" / "run_all.py").read_text(),
          True)

    print()
    print("=" * 78)
    print("5 — THE SWITCH INVENTORY IS COMPLETE AND NEVER RAISES")
    print("-" * 78)
    from tools.atlas_switches import inventory, readiness, blocking, render
    # fast=True: the suite must not make a live broker call. Running the tests
    # against production is exactly what paused the engine on 2026-10-13.
    rows = inventory(fast=True)
    names = {r["switch"] for r in rows}
    # THE FOUR FOUND BY ACCIDENT. Each cost a session; each must be in the list.
    for want in ("halt sentinel file", "atlas_state.mode",
                 "ENABLE_EXIT_MANAGEMENT", "kill switch (Gate 7)"):
        check(f"inventory covers {want}", want in names, True)
    check("every row says where its state lives",
          all(r["where"] for r in rows), True)
    check("every row says whether it survives a restart",
          all(r["survives_restart"] for r in rows), True)
    check("every row declares a scope",
          {r["scope"] for r in rows} <= {"session", "signal"}, True)
    check("readiness returns a verdict and a reason per blocker",
          len(readiness(rows)[1]), len(blocking(rows)))
    check("render produces a table", "VERDICT:" in render(rows), True)
    # A PROBE THAT CANNOT READ ITS STATE BLOCKS. Not knowing whether we are
    # disarmed is not permission to trade.
    unreadable = [r for r in rows if r["value"] in ("UNKNOWN", "UNREADABLE")
                  and not r["advisory"]]
    check("an unreadable non-advisory probe BLOCKS",
          all(r["blocking"] for r in unreadable), True,
          f"{len(unreadable)} unreadable here (no credentials in the suite)")
    # THE EXCEPTIONS MUST NAME THEMSELVES. advisory=True is the only way a row
    # may report UNKNOWN without blocking, so it is asserted rather than left
    # to whoever adds the next probe.
    # The permanent advisory exceptions, separate from fast mode's not-probed
    # rows. These two are the only switches allowed to report UNKNOWN without
    # blocking, and they are listed here so adding a third is a deliberate act.
    check("  and the advisory exceptions are explicit",
          sorted(r["switch"] for r in rows
                 if r["advisory"] and r["value"] != "not probed"),
          ["atlas-market-hours.timer", "sentiment source (market.parquet)"])
    check("fast mode still LISTS the broker switches",
          {"broker funds", "Zerodha access token", "kill switch (Gate 7)"}
          <= names, True, "skipped probes are rows, not omissions")
    check("  and marks them not-probed rather than passing",
          sorted(r["value"] for r in rows if r["value"] == "not probed"),
          ["not probed"] * 3)

    print()
    print("=" * 78)
    print("6 — THE 08:30 REPORT CHECKS EVERY SWITCH")
    print("-" * 78)
    import atlas.execution.zerodha_morning as ZM
    zm = executable_source(ROOT / "atlas" / "execution" / "zerodha_morning.py")
    check("the message includes the readiness block",
          "_readiness_block()" in zm, True)
    check("  which reads the inventory, not just the halt file",
          "atlas_switches" in zm, True)
    block = ZM._readiness_block()
    check("it renders without credentials", isinstance(block, str), True)
    check("  and says NOT READY when something blocks",
          ("NOT READY" in block) or ("READY" in block), True)
    check("the halt line is still there too", "_halt_line()" in zm, True)

    print()
    print("=" * 78)
    print("PASS" if OK else "FAIL")
    print("=" * 78)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
