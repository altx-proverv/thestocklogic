"""
THE RANGE-BREAK BREAKERS
========================

A box around the previous 45 minutes. Price printing outside it, on the side
that goes AGAINST the position, is a confirmed change of character. One print
is enough. A structural fact, not a vote -- there is no threshold here and
none is to be added.

THE THREE THINGS THESE CHECKS EXIST FOR, in order of how badly they would hurt:

  1  DIRECTION. A break of the ceiling while long is a breakout in ATLAS's
     favour. Firing on it would stop ATLAS on its winners, which is the single
     worst outcome available here. All four side/level combinations are
     asserted, plus the two no-fire cases.
  2  THE WINDOW EXCLUDES THE CURRENT READING. A box that contains the current
     price cannot be broken -- its high is by construction at least the current
     price. Such a breaker reads all-clear forever and nobody notices.
  3  IT NEVER EXITS. The stop-loss order owns that decision; two systems
     racing to close one position is how it goes wrong.
"""

import os
import sys
import json
import tempfile
import importlib
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<60}{str(got)[:16]:<18}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


def fresh():
    os.environ["ATLAS_BREAKER_BLOCKED_FILE"] = tempfile.mktemp(suffix=".json")
    os.environ["ATLAS_SENTIMENT_STANDDOWN_FILE"] = tempfile.mktemp(suffix=".json")
    import atlas.config as C
    importlib.reload(C)
    import atlas.execution.sentiment_window as SD
    importlib.reload(SD)
    import atlas.execution.range_breaker as RB
    importlib.reload(RB)
    import atlas.reporting.telegram as TG
    TG.send = lambda *a, **k: None
    return RB, SD


def candles(start_min: str, bars: list, day="2026-10-15") -> list:
    """[(hi, lo)] -> 1-minute candles starting at start_min, one per minute."""
    h, m = (int(x) for x in start_min.split(":"))
    t0 = dt.datetime(2026, 10, 15, h, m, tzinfo=IST)
    out = []
    for i, (hi, lo) in enumerate(bars):
        ts = (t0 + dt.timedelta(minutes=i)).isoformat()
        out.append([ts, lo, hi, lo, hi, 100, 0])     # [ts,o,h,l,c,v,oi]
    return out


def main() -> int:
    from _srcutil import executable_source
    RB, SD = fresh()
    src = (ROOT / "atlas" / "execution" / "range_breaker.py").read_text()
    exe = executable_source(ROOT / "atlas" / "execution" / "range_breaker.py")

    print("=" * 82)
    print("1 — DIRECTION: THE ONE THAT MUST NOT BE BACKWARDS")
    print("-" * 82)
    B = {"ok": True, "high": 100.0, "low": 90.0}
    cases = [
        ("LONG",  "floor broken",      {"probe_low": 89.0, "probe_high": 95.0},
         True,  "floor"),
        ("SHORT", "ceiling broken",    {"probe_low": 95.0, "probe_high": 101.0},
         True,  "ceiling"),
        # THE TWO THAT MUST NOT FIRE. A break in ATLAS's favour.
        ("LONG",  "CEILING broken",    {"probe_low": 95.0, "probe_high": 101.0},
         False, None),
        ("SHORT", "FLOOR broken",      {"probe_low": 89.0, "probe_high": 95.0},
         False, None),
        ("LONG",  "inside the box",    {"probe_low": 92.0, "probe_high": 98.0},
         False, None),
        ("SHORT", "inside the box",    {"probe_low": 92.0, "probe_high": 98.0},
         False, None),
    ]
    for side, label, probe, want_fire, want_level in cases:
        v = RB.verdict({**B, **probe}, side)
        check(f"{side:<5} {label:<18} fires={want_fire}",
              (v["broke"], v["level"]), (want_fire, want_level))
    # The favourable ones are RECORDED, so the replay can count them.
    check("a favourable break is recorded, not fired",
          RB.verdict({**B, "probe_low": 95.0, "probe_high": 101.0},
                     "LONG")["favourable_break"], True)
    # EQUALITY IS NOT A BREAK. Printing exactly ON the level is not outside it.
    check("exactly ON the floor is not a break",
          RB.verdict({**B, "probe_low": 90.0, "probe_high": 95.0},
                     "LONG")["broke"], False)
    check("exactly ON the ceiling is not a break",
          RB.verdict({**B, "probe_low": 95.0, "probe_high": 100.0},
                     "SHORT")["broke"], False)
    check("direction lives in exactly one function",
          exe.count("def verdict("), 1)

    print()
    print("=" * 82)
    print("2 — THE WINDOW EXCLUDES THE CURRENT READING")
    print("-" * 82)
    # 46 candles from 09:15. At 10:01 the box is the 45 before 10:01, i.e.
    # 09:16-10:00, and the 10:01 candle is the probe -- never in the box.
    bars = [(100.0, 90.0)] * 45 + [(120.0, 80.0)]      # the last one is extreme
    cs = candles("09:15", bars)
    now = dt.datetime(2026, 10, 15, 10, 0, tzinfo=IST)   # the 46th candle
    b = RB.box("X", now=now, candles=cs)
    check("the box is built", b["ok"], True, b["detail"][:40])
    check("  it used exactly 45 candles", b["candles_used"], 45)
    check("  the box high EXCLUDES the current minute's 120", b["high"], 100.0,
          "a box containing the current price could never be broken")
    check("  and its low excludes the current minute's 80", b["low"], 90.0)
    check("  the current minute is the PROBE", b["probe_low"], 80.0)
    check("  so the floor breaks", RB.verdict(b, "LONG")["broke"], True)
    check("  the box spans 09:15-09:59", (b["first"], b["last"]),
          ("09:15", "09:59"))
    # A SANITY TRAP: if the exclusion were wrong, this is what it would look
    # like -- the probe inside the box and nothing ever breaking.
    bad = dict(b, high=120.0, low=80.0)
    check("  (with the probe inside the box nothing breaks)",
          RB.verdict(bad, "LONG")["broke"], False,
          "the shape the bug would have had")

    print()
    print("=" * 82)
    print("3 — WHEN THE WINDOW CANNOT BE BUILT: FAIL OPEN, LOUDLY")
    print("-" * 82)
    short = RB.box("X", now=dt.datetime(2026, 10, 15, 9, 55, tzinfo=IST),
                   candles=candles("09:15", [(100.0, 90.0)] * 40))
    check("40 candles is not a 45-candle box", short["ok"], False)
    check("  it reports CANNOT_EVALUATE", short["reason_code"],
          RB.REASON_CANNOT_EVALUATE)
    check("  and says how far short", "40 of 45" in short["detail"], True)
    check("  it does NOT fire", RB.verdict(short, "LONG")["broke"], False)
    check("  and it is not an all-clear either",
          short["reason_code"] is not None, True)
    empty = RB.box("X", now=dt.datetime(2026, 10, 15, 11, 0, tzinfo=IST),
                   candles=[])
    check("no candles at all reports CANNOT_EVALUATE", empty["reason_code"],
          RB.REASON_CANNOT_EVALUATE)
    check("the fail-open reasoning is written down",
          "FAIL OPEN, DELIBERATELY, AND DO NOT REVERSE THIS" in src, True)
    check("  and says why: the breaker is one-way",
          "ONE-WAY" in src and "stop-loss" in src, True)

    print()
    print("=" * 82)
    print("4 — ARMING, AND DORMANCY WHEN FLAT")
    print("-" * 82)
    check("before 09:50 nothing is armed",
          RB.armed(dt.datetime(2026, 10, 15, 9, 49, tzinfo=IST))[0], False)
    check("at 09:50 it is armed",
          RB.armed(dt.datetime(2026, 10, 15, 9, 50, tzinfo=IST))[0], True)
    # DORMANT WHEN FLAT. A break against a position ATLAS does not have is not
    # an event, and firing then would stop a day for nothing.
    check("no open positions -> no side", RB.held_side([])[0], None)
    check("  and the reason says dormant", "dormant" in RB.held_side([])[1], True)
    check("longs open -> LONG",
          RB.held_side([{"direction": "LONG"}])[0], "LONG")
    check("shorts open -> SHORT",
          RB.held_side([{"direction": "SHORT"}])[0], "SHORT")
    check("both sides open -> dormant, deliberately",
          RB.held_side([{"direction": "LONG"}, {"direction": "SHORT"}])[0], None)

    print()
    print("=" * 82)
    print("5 — THE MARKET BREAKER STANDS THE DAY DOWN, UNDER ITS OWN CODE")
    print("-" * 82)
    check("the reason code names a RANGE BREAK",
          RB.REASON_MARKET_BREAK, "RANGE_BREAK_STAND_DOWN")
    check("  and the stand-down module agrees",
          SD.REASON_STAND_DOWN, "RANGE_BREAK_STAND_DOWN")
    # The VALUE is gone; the comment recording the rename is deliberate, so
    # this reads the executable source rather than the whole file.
    check("  the old sentiment name is gone from the code",
          "SENTIMENT_REVERSAL_STAND_DOWN" in
          executable_source(ROOT / "atlas" / "execution" / "sentiment_window.py"),
          False)
    RB, SD = fresh()
    cs = candles("09:15", [(100.0, 90.0)] * 45 + [(95.0, 80.0)])
    now = dt.datetime(2026, 10, 15, 10, 0, tzinfo=IST)
    m = RB.market_check(now=now, rows=[{"direction": "LONG"}], candles=cs,
                        last_price=80.0)
    check("a floor break while long fires", m["fired"], True)
    check("  under the range-break code", m["state"], "RANGE_BREAK_STAND_DOWN")
    check("  it stands the day down", SD.state()["stood_down"], True)
    for want in ("floor", "box", "broken at", "IST"):
        check(f"  the alert detail names the {want}", want in m["detail"], True)
    check("  and the box's high and low are in it",
          "90.00-100.00" in m["detail"], True)
    # THE ONE THAT MUST NOT FIRE, through the whole call path this time.
    RB, SD = fresh()
    cs = candles("09:15", [(100.0, 90.0)] * 45 + [(120.0, 95.0)])
    m = RB.market_check(now=now, rows=[{"direction": "LONG"}], candles=cs,
                        last_price=120.0)
    check("a CEILING break while long does NOT fire", m["fired"], False,
          "firing here would stop ATLAS on its winners")
    check("  the day is not stood down", SD.state()["stood_down"], False)
    check("  and it is reported as favourable",
          "FAVOURABLE" in m["detail"], True)
    # DORMANT: the same break with nothing open.
    RB, SD = fresh()
    cs = candles("09:15", [(100.0, 90.0)] * 45 + [(95.0, 80.0)])
    m = RB.market_check(now=now, rows=[], candles=cs, last_price=80.0)
    check("the same break with nothing open stays DORMANT", m["fired"], False)
    check("  reported as dormant", m["state"], RB.REASON_DORMANT)

    print()
    print("=" * 82)
    print("6 — THE STOCK BREAKER BLOCKS ONE SYMBOL, NOT THE DAY")
    print("-" * 82)
    RB, SD = fresh()
    cs = candles("09:15", [(100.0, 90.0)] * 45 + [(95.0, 80.0)])
    r = RB.stock_check("TESTSYM", "LONG", instrument_key="NSE_EQ|X", now=now,
                       candles=cs, last_price=80.0)
    check("a floor break on a stock fires", r["broke"], True)
    check("  under its own code", r["state"], RB.REASON_SYMBOL_BREAK)
    check("  the symbol is blocked", RB.is_blocked("TESTSYM")[0], True)
    check("  another symbol is NOT", RB.is_blocked("OTHERSYM")[0], False)
    check("  and the DAY is not stood down", SD.state()["stood_down"], False,
          "only the market breaker does that")
    check("  the detail names the box and the price",
          all(x in r["detail"] for x in ("90.00-100.00", "broken at")), True)
    check("a blocked symbol stays blocked on the next cycle",
          RB.stock_check("TESTSYM", "LONG", instrument_key="NSE_EQ|X", now=now,
                         candles=candles("09:15", [(100.0, 90.0)] * 46)
                         )["state"], RB.REASON_SYMBOL_BREAK)
    # Persistence and the session boundary.
    path = Path(os.environ["ATLAS_BREAKER_BLOCKED_FILE"])
    check("it is written to disk", path.exists(), True)
    importlib.reload(RB)
    check("  and survives a restart", RB.is_blocked("TESTSYM")[0], True)
    d = json.loads(path.read_text()); d["session_date"] = "2001-01-01"
    path.write_text(json.dumps(d))
    importlib.reload(RB)
    check("yesterday's block does not carry into today",
          RB.is_blocked("TESTSYM")[0], False)

    print()
    print("=" * 82)
    print("7 — NEITHER BREAKER EVER EXITS A POSITION")
    print("-" * 82)
    # EXECUTABLE SOURCE, not the raw file: the docstring that promises this
    # module places no orders has to name the calls it does not make.
    for bad in ("place_order", "place_gtt", "modify_gtt", "cancel_order",
                "square_off", "exit_position"):
        check(f"the module never calls {bad}", bad in exe, False)
    check("  nor imports the exit path",
          any(x in exe for x in ("from atlas.execution.exits",
                                 "import exits")), False)
    check("  nor the broker", "atlas.execution.broker" in exe, False)
    check("it says so in the docstring",
          "IT NEVER EXITS A POSITION" in src, True)
    for name, rel in (("exits", "atlas/execution/exits.py"),
                      ("preclose_gtt", "atlas/execution/preclose_gtt.py"),
                      ("trade_management", "atlas/execution/trade_management.py"),
                      ("trailing", "atlas/risk/trailing.py")):
        t = (ROOT / rel).read_text(encoding="utf-8")
        check(f"{name} never consults the breaker",
              "range_breaker" in t, False, rel)

    print()
    print("=" * 82)
    print("8 — WIRING, CONSTANTS AND SURFACES")
    print("-" * 82)
    import atlas.config as C
    check("BREAKER_WINDOW_CYCLES", C.BREAKER_WINDOW_CYCLES, 45)
    check("BREAKER_ARMS_AT", C.BREAKER_ARMS_AT, "09:50")
    # Two config imports exist and both are legitimate: the breaker constants
    # in _cfg(), and SUPABASE_* in held_side(). Scoped to the ones that are
    # the policy.
    check("the breaker constants are read in exactly one place",
          sum(1 for l in exe.splitlines()
              if "from atlas.config import" in l and "BREAKER_" in l), 1,
          "_cfg()")
    import re
    scoped = "\n".join(l for l in exe.splitlines() if "timedelta(" not in l)
    check("no threshold is written as a literal",
          [n for n in ("45",) if re.search(rf"(?<![\w.]){n}(?![\w.])", scoped)],
          [], "changing config must be the only way")
    mo = executable_source(ROOT / "atlas" / "signal" / "market_open.py")
    check("the loop runs the market breaker once a cycle",
          mo.count("market_check("), 1)
    check("  and checks open positions", "stock_check(" in mo, True)
    check("  and sets the quote cycle", "set_cycle(" in mo, True)
    ae = executable_source(ROOT / "atlas" / "execution" / "atlas_entry.py")
    check("the gate checks the symbol block", "is_blocked(" in ae, True)
    check("  and runs the stock breaker on a candidate",
          "stock_check(" in ae, True)
    sw = executable_source(ROOT / "tools" / "atlas_switches.py")
    check("the inventory shows the market box",
          "range breaker (market)" in sw, True)
    check("  and the blocked-symbol count",
          "range breaker (per symbol)" in sw, True)
    check("  and it never fires from a tool", "market_check(" in sw, False)
    zm = executable_source(ROOT / "atlas" / "execution" / "zerodha_morning.py")
    check("the 08:30 report carries the box", "range breaker (market)" in zm, True)

    print()
    print("=" * 82)
    print("9 — THE PER-CYCLE QUOTE CACHE IS A CACHE, NOT A WINDOW")
    print("-" * 82)
    from atlas.execution import live_sentiment as LS
    calls = {"n": 0}
    saved = LS._quote
    try:
        LS._quote = lambda keys: (calls.__setitem__("n", calls["n"] + 1), {})[1]
        LS.set_cycle(1)
        for _ in range(6):
            LS.read()
        check("six candidates in one cycle -> one fetch", calls["n"], 1)
        LS.set_cycle(2)
        for _ in range(6):
            LS.read()
        check("the next cycle fetches again", calls["n"], 2)
        LS.set_cycle(None)
        before = calls["n"]
        for _ in range(3):
            LS.read()
        check("a caller with no cycle is never cached",
              calls["n"] - before, 3, "tools and the 08:30 report")
    finally:
        LS._quote = saved
        LS.set_cycle(None)
    ls = (ROOT / "atlas" / "execution" / "live_sentiment.py").read_text()
    check("it is invalidated by cycle number, not a TTL",
          "INVALIDATED BY CYCLE NUMBER, NOT BY A TTL" in ls, True)
    # Executable source: "average price" is all over the VWAP docstring, which
    # is describing the exchange's field rather than any smoothing of ours.
    lse = executable_source(ROOT / "atlas" / "execution" / "live_sentiment.py")
    check("  and no smoothing crept in with it",
          any(x in lse for x in ("ewm", "rolling(", "mean(", "median")),
          False)

    print()
    print("=" * 82)
    print("10 — THE SUITE CANNOT WRITE PRODUCTION STATE")
    print("-" * 82)
    ra = (ROOT / "tests" / "run_all.py").read_text()
    check("run_all declares the drill",
          'os.environ["ATLAS_DRILL"] = "1"' in ra, True)
    check("  and redirects the blocked-symbol file",
          "ATLAS_BREAKER_BLOCKED_FILE" in ra, True)
    check("the module refuses the canonical path while drilling",
          "is_drill" in src and "/var/lib/atlas/breaker_blocked.json" in src,
          True)
    os.environ["ATLAS_DRILL"] = "1"
    os.environ["ATLAS_BREAKER_BLOCKED_FILE"] = "/var/lib/atlas/breaker_blocked.json"
    importlib.reload(C)
    importlib.reload(RB)
    live = Path("/var/lib/atlas/breaker_blocked.json")
    existed = live.exists()
    RB.block_symbol("DRILLSYM", "must not reach production")
    check("a drilling block writes no live file", live.exists(), existed)

    print()
    print("=" * 82)
    print("PASS" if OK else "FAIL")
    print("=" * 82)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
