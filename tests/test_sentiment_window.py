"""
THE CONFIRM WINDOW AND THE REVERSAL BREAKER
===========================================

8172f33 made Gate 1 a live read. Replaying six sessions showed the raw read is
an honest observation and a poor permission: a median of 4 state changes a
session, 24 on 2026-10-07, 14 crossings in and out of bullish on 2026-10-05.
So the raw read no longer authorises anything by itself.

    ESTABLISH   24 of the last 30 cycles reading one state
    REVERSAL    once a side is established, 36 of the last 45 reading the
                OPPOSITE side stands the session down, both sides, one-way

Four constants in atlas/config.py and nothing else. These checks cover the
arithmetic, the one-way property, that MIXED never reverses, that the window
counts CYCLES rather than candidate evaluations, that a fired breaker survives
a restart -- and that EXITS ARE UNTOUCHED, because a breaker that also stopped
protecting open positions would be worse than no breaker.
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
    print(f"  {label:<60}{str(got)[:18]:<20}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


def fresh():
    """A window module pointed at an empty file, with alerts suppressed."""
    os.environ["ATLAS_SENTIMENT_WINDOW_FILE"] = tempfile.mktemp(suffix=".json")
    import atlas.config as C
    importlib.reload(C)
    import atlas.execution.sentiment_window as W
    importlib.reload(W)
    import atlas.reporting.telegram as TG
    TG.send = lambda *a, **k: None
    W.reset_for_tests()
    return W


def main() -> int:
    from atlas.config import (SENTIMENT_ESTABLISH_WINDOW as EW,
                              SENTIMENT_ESTABLISH_MIN as EM,
                              SENTIMENT_REVERSAL_WINDOW as RW,
                              SENTIMENT_REVERSAL_MIN as RM)
    from _srcutil import executable_source

    print("=" * 78)
    print("1 — THE FOUR CONSTANTS ARE THE WHOLE POLICY")
    print("-" * 78)
    check("establish window / min", (EW, EM), (30, 24))
    check("reversal window / min", (RW, RM), (45, 36))
    check("the reversal window is the longer one", RW > EW, True,
          "a reversal must take more evidence than an establishment")
    check("both minima are a majority of their window",
          (EM > EW / 2, RM > RW / 2), (True, True))
    # NO OTHER SMOOTHING. The operator asked for exactly these four levers.
    w_src = executable_source(ROOT / "atlas" / "execution" / "sentiment_window.py")
    for banned in ("ewm", "rolling(", "half_life", "alpha=", "hysteresis",
                   "cooldown", "smooth"):
        check(f"  no {banned!r} anywhere in the module", banned in w_src, False)
    # ONE PLACE READS CONFIG, AND NO THRESHOLD IS WRITTEN AS A NUMBER. If a
    # literal 24 or 45 appears here, changing config.py stops changing the
    # policy -- which is the one property the operator asked for.
    check("exactly one site imports the constants",
          w_src.count("from atlas.config import"), 1, "_cfg()")
    import re as _re
    # The IST offset is `minutes=30` and is not a threshold; scoped out by name
    # rather than by loosening the pattern, so a real literal 30 still fails.
    scoped = "\n".join(l for l in w_src.splitlines() if "timedelta(" not in l)
    literals = [n for n in (str(EW), str(EM), str(RW), str(RM))
                if _re.search(rf"(?<![\w.]){n}(?![\w.])", scoped)]
    check("no threshold appears as a literal in the module", literals, [],
          "changing config.py must be the only way to change the policy")

    print()
    print("=" * 78)
    print("2 — A SIDE MUST ESTABLISH BEFORE IT PERMITS ANYTHING")
    print("-" * 78)
    W = fresh()
    s = W.state()
    check("an empty window permits nothing", s["permits"], None)
    check("  and says it is FORMING, not broken",
          s["reason_code"], W.REASON_FORMING,
          "a declared state: every session is here before ~09:50")
    check("  and says how many cycles short",
          s["shortfalls"]["cycles_to_window"], EW)

    for i in range(1, EW):
        s = W.observe_cycle("bullish", i)
    check(f"at {EW - 1} unanimous bullish cycles, still nothing",
          s["permits"], None, f"{s['reason_code']}")
    s = W.observe_cycle("bullish", EW)
    check(f"at {EW} it establishes", s["established"], "bullish")
    check("  and permits that side", s["permits"], "bullish")
    check("  and the reason code clears", s["reason_code"], None)

    # THE THRESHOLD IS A COUNT, NOT UNANIMITY: EM of EW, so EW-EM may dissent.
    W = fresh()
    seq = ["bullish"] * EM + ["mixed"] * (EW - EM)
    for i, v in enumerate(seq, 1):
        s = W.observe_cycle(v, i)
    check(f"{EM} bullish and {EW - EM} mixed still establishes bullish",
          s["established"], "bullish")
    W = fresh()
    seq = ["bullish"] * (EM - 1) + ["mixed"] * (EW - EM + 1)
    for i, v in enumerate(seq, 1):
        s = W.observe_cycle(v, i)
    check(f"{EM - 1} bullish does NOT", s["established"], None)
    check("  and it is NOT_ESTABLISHED, not FORMING",
          s["reason_code"], W.REASON_NOT_ESTABLISHED,
          "the window is full; the market is undecided")

    print()
    print("=" * 78)
    print("3 — A CONFIRMED MIXED PERMITS NOTHING, AND NEVER REVERSES")
    print("-" * 78)
    W = fresh()
    for i in range(1, EW + 1):
        s = W.observe_cycle("mixed", i)
    check("mixed can be the established state", s["established"], "mixed")
    check("  but permits no side", s["permits"], None)
    check("  under SENTIMENT — a readable market saying wait",
          s["reason_code"], "SENTIMENT")
    check("  and sets no session side", s["session_side"], None)

    W = fresh()
    for i in range(1, EW + 1):
        W.observe_cycle("bullish", i)
    n = EW
    for _ in range(RW + 15):
        n += 1
        s = W.observe_cycle("mixed", n)
    check(f"{RW + 15} mixed cycles after a bullish side: no stand-down",
          s["stood_down"], False,
          "only a confirmed OPPOSITE SIDE reverses")
    check("  the confirmed state is mixed", s["established"], "mixed")
    check("  so entries stop anyway, without the breaker",
          s["permits"], None)

    print()
    print("=" * 78)
    print("4 — THE REVERSAL BREAKER")
    print("-" * 78)
    W = fresh()
    for i in range(1, EW + 1):
        W.observe_cycle("bullish", i)
    n, fired_at = EW, None
    for k in range(1, RM + 5):
        n += 1
        s = W.observe_cycle("bearish", n)
        if s["stood_down"] and fired_at is None:
            fired_at = k
    check(f"fires at exactly {RM} opposite cycles", fired_at, RM)
    check("  and names both states",
          (s["reversal_from"], s["reversal_to"]), ("bullish", "bearish"))
    check("  the detail carries the counts",
          all(x in s["stand_down_detail"] for x in ("36", "45", "bullish",
                                                    "bearish")), True)
    check("  permits nothing", s["permits"], None)
    check("  under its own reason code",
          s["reason_code"], W.REASON_STAND_DOWN)

    # ONE-WAY. A market that turns back does not re-arm the day.
    for _ in range(RW * 2):
        n += 1
        s = W.observe_cycle("bullish", n)
    check("a full window of the original side does NOT re-arm",
          s["stood_down"], True, "one-way, for the rest of the session")
    check("  permits still nothing", s["permits"], None)
    check("  though the raw read is still observed", s["raw"], "bullish",
          "the observation never stops; only the permission does")

    # THE SESSION SIDE DOES NOT CHASE THE MARKET. If it followed the latest
    # establishment the reversal reference would move and never fire.
    W = fresh()
    for i in range(1, EW + 1):
        W.observe_cycle("bullish", i)
    n = EW
    for _ in range(EM):
        n += 1
        s = W.observe_cycle("bearish", n)
    check("after a bearish establishment the session side is still bullish",
          s["session_side"], "bullish",
          "the reversal reference is the FIRST side of the day")
    check("  and the confirmed state has flipped to bearish",
          s["established"], "bearish")

    print()
    print("=" * 78)
    print("5 — THE WINDOW COUNTS CYCLES, NOT CANDIDATE EVALUATIONS")
    print("-" * 78)
    # THE TRAP THIS MODULE IS SHAPED TO AVOID. Gate 1 runs once per candidate,
    # nine or ten times a cycle. If the window advanced per call, "30 cycles of
    # agreement" would mean three minutes.
    W = fresh()
    for i in range(1, EW + 1):
        for _ in range(10):                       # ten candidates in one cycle
            s = W.observe_cycle("bullish", i)
    check("ten calls per cycle advance the window once",
          s["establish_counts"]["n"], EW)
    check("  and it establishes on schedule", s["established"], "bullish")
    ae = executable_source(ROOT / "atlas" / "execution" / "atlas_entry.py")
    check("the gate reads state(), it does not observe",
          "observe_cycle" in ae, False,
          "observing in the gate is the defect this prevents")
    mo = executable_source(ROOT / "atlas" / "signal" / "market_open.py")
    check("the loop observes once, in cycle()",
          mo.count("observe_cycle("), 1)

    print()
    print("=" * 78)
    print("6 — A FIRED BREAKER SURVIVES A RESTART; A STALE FILE DOES NOT")
    print("-" * 78)
    W = fresh()
    path = Path(os.environ["ATLAS_SENTIMENT_WINDOW_FILE"])
    for i in range(1, EW + 1):
        W.observe_cycle("bullish", i)
    n = EW
    for _ in range(RM):
        n += 1
        W.observe_cycle("bearish", n)
    check("the breaker fired", W.state()["stood_down"], True)
    importlib.reload(W)                            # a new process, same file
    check("  and a reloaded module still sees it", W.state()["stood_down"], True,
          "forgetting it would re-arm trading: a fail-OPEN")
    check("  with the reason intact",
          W.state()["reason_code"], W.REASON_STAND_DOWN)

    # A FILE FROM ANOTHER DAY IS HISTORY, NOT STATE.
    d = json.loads(path.read_text())
    d["session_date"] = "2001-01-01"
    path.write_text(json.dumps(d))
    importlib.reload(W)
    s = W.state()
    check("yesterday's stand-down does not carry into today",
          s["stood_down"], False)
    check("  and the window starts empty", s["establish_counts"]["n"], 0)
    check("  so entries are refused as FORMING, not permitted",
          s["reason_code"], W.REASON_FORMING)

    # AN UNREADABLE CYCLE IS RECORDED, NOT DROPPED. Dropping it would let a
    # dead feed hold a stale established side indefinitely.
    W = fresh()
    for i in range(1, EW + 1):
        W.observe_cycle("bullish", i)
    n = EW
    for _ in range(EW - EM + 1):
        n += 1
        s = W.observe_cycle("unavailable", n)
    check("enough unavailable cycles age the established side out",
          s["established"], None,
          "a dead feed must not keep permitting on the last good read")
    check("  and they are counted, not silently skipped",
          s["establish_counts"]["unavailable"] > 0, True)

    print()
    print("=" * 78)
    print("7 — EXITS ARE UNTOUCHED BY A STAND-DOWN")
    print("-" * 78)
    # A BREAKER THAT ALSO STOPPED PROTECTING POSITIONS WOULD BE WORSE THAN NO
    # BREAKER. The stand-down is an ENTRY control. Asserted structurally: the
    # exit path must not consult the window at all.
    for name, rel in (("exits", "atlas/execution/exits.py"),
                      ("preclose_gtt (15:20 handover)",
                       "atlas/execution/preclose_gtt.py"),
                      ("trade_management", "atlas/execution/trade_management.py"),
                      ("trailing", "atlas/risk/trailing.py")):
        src = (ROOT / rel).read_text(encoding="utf-8")
        clean = ("sentiment_window" not in src and "stood_down" not in src
                 and "SENTIMENT_REVERSAL" not in src)
        check(f"{name} never consults the window", clean, True, rel)
    # And the window itself must not reach into the exit path.
    check("the window module places no orders",
          any(x in w_src for x in ("place_order", "place_gtt", "modify_gtt",
                                   "cancel_order")), False)
    check("  and reads no position state",
          any(x in w_src for x in ("atlas_trades", "positions", "holdings")),
          False, "it observes the index and nothing else")
    # The stand-down lands in the ENTRY gate only.
    check("the stand-down code is consumed by the entry gate",
          "sentiment_window" in ae, True)
    ex = (ROOT / "atlas" / "execution" / "exits.py").read_text(encoding="utf-8")
    check("protect_or_exit still exists and is untouched by this change",
          "def protect_or_exit(" in ex, True)

    print()
    print("=" * 78)
    print("8 — THE WINDOW IS VISIBLE, AND THE REPLAY SHARES THE POLICY")
    print("-" * 78)
    W = fresh()
    for i in range(1, 20):
        W.observe_cycle("bullish", i)
    ln = W.line()
    for want in ("confirmed", "raw", "establish", "need", "reversal"):
        check(f"the line carries {want!r}", want in ln, True)
    check("  and the distance to the window",
          "more cycle(s)" in ln, True, ln[:60])
    sw = executable_source(ROOT / "tools" / "atlas_switches.py")
    check("the inventory lists the window", "sentiment window" in sw, True)
    zm = executable_source(ROOT / "atlas" / "execution" / "zerodha_morning.py")
    check("the 08:30 report carries it too",
          "sentiment window (Gate 1 confirm)" in zm, True)
    rp = executable_source(ROOT / "tools" / "replay_live_sentiment.py")
    check("the replay imports fold()", "fold" in rp, True,
          "one implementation of the policy, not two")
    check("  and reports the near-miss count", "worst_opposite" in rp, True)

    # ONE IMPLEMENTATION: the pure fold and the stateful path must agree.
    W = fresh()
    seq = (["bullish"] * EW + ["mixed"] * 5 + ["bearish"] * RM)
    for i, v in enumerate(seq, 1):
        s = W.observe_cycle(v, i)
    f = W.fold(seq)
    check("fold() agrees with the live path on stand-down",
          f["stood_down"], s["stood_down"])
    check("  and on the session side", f["session_side"], s["session_side"])
    check("  and on the confirmed state", f["established"], s["established"])

    print()
    print("=" * 78)
    print("9 — THE SUITE CANNOT WRITE THE LIVE WINDOW")
    print("-" * 78)
    # SECOND INSTANCE OF THE 2026-10-13 CLASS. verify_long_only could pause the
    # live engine because halt() writes shared state; this window is the same
    # shape -- tests/test_market_hours_loop calls the real cycle(), cycle()
    # advances the window, and the window persists to /var/lib/atlas. Running
    # the suite injected an observation into the LIVE window.
    ra = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")
    check("run_all declares the drill for every suite",
          'os.environ["ATLAS_DRILL"] = "1"' in ra, True,
          "fencing each writer as it is found is a losing game")
    check("  and redirects the window file",
          "ATLAS_SENTIMENT_WINDOW_FILE" in ra, True)
    check("the window refuses the canonical path while drilling",
          "/var/lib/atlas/sentiment_window.json" in w_src
          and "is_drill" in w_src, True,
          "belt and braces, if a suite forgets to redirect")
    # And prove the fence fires rather than just existing.
    import atlas.execution.sentiment_window as _W
    os.environ["ATLAS_DRILL"] = "1"
    os.environ["ATLAS_SENTIMENT_WINDOW_FILE"] = \
        "/var/lib/atlas/sentiment_window.json"
    import atlas.config as _C
    importlib.reload(_C)
    importlib.reload(_W)
    before = Path("/var/lib/atlas/sentiment_window.json")
    existed = before.exists()
    _W.observe_cycle("bullish", 1)
    check("a drilling observe_cycle writes no live file",
          before.exists(), existed,
          "unchanged whether or not the real file was already there")

    print()
    print("=" * 78)
    print("PASS" if OK else "FAIL")
    print("=" * 78)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
