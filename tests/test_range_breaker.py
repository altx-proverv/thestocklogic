"""
THE RANGE-BREAK BREAKERS
========================

The box is the OPENING RANGE: the high and low of 09:15-10:00, computed once
and held for the session. A one-minute CLOSE outside it, on the side that goes
AGAINST the position, is a confirmed change of character. One close is enough.
A structural fact, not a vote -- no threshold, and none to be added.

THE FOUR THINGS THESE CHECKS EXIST FOR, in order of how badly they would hurt:

  1  DIRECTION. A close above the ceiling while long is a breakout in ATLAS's
     favour. Firing on it would stop ATLAS on its winners, which is the single
     worst outcome available here.
  2  THE RANGE IS FIXED. A rolling box descends with the price: it misses a
     slow grind and fires on an ordinary pullback, which is why the rolling
     version fired on 8 of 8 replayed sessions.
  3  CLOSE-BASIS, NOT A WICK. A wick below an obvious low is stops being
     swept, which this codebase's own SMC logic scores as BULLISH.
  4  IT NEVER EXITS. The stop-loss order owns that decision.
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


def fresh(observe_only=True):
    os.environ["ATLAS_BREAKER_BLOCKED_FILE"] = tempfile.mktemp(suffix=".json")
    os.environ["ATLAS_BREAKER_RANGE_FILE"] = tempfile.mktemp(suffix=".json")
    os.environ["ATLAS_SENTIMENT_STANDDOWN_FILE"] = tempfile.mktemp(suffix=".json")
    import atlas.config as C
    importlib.reload(C)
    # The enforcing path must be covered too, so observe-only is overridable
    # here. The DEPLOYED value is asserted separately in section 5.
    C.BREAKER_OBSERVE_ONLY = observe_only
    import atlas.execution.sentiment_window as SD
    importlib.reload(SD)
    import atlas.execution.range_breaker as RB
    importlib.reload(RB)
    import atlas.reporting.telegram as TG
    TG.send = lambda *a, **k: None
    return RB, SD


def candles_ohlc(start_min: str, bars: list) -> list:
    """[(o, h, l, c)] -> 1-minute candles, one per minute from start_min."""
    h, m = (int(x) for x in start_min.split(":"))
    t0 = dt.datetime(2026, 10, 15, h, m, tzinfo=IST)
    return [[(t0 + dt.timedelta(minutes=i)).isoformat(), o, hi, lo, c, 100, 0]
            for i, (o, hi, lo, c) in enumerate(bars)]


def candles(start_min: str, bars: list) -> list:
    """[(hi, lo)] -> candles whose CLOSE is the low.

    The close is the low so a (hi, lo) bar outside the range breaks on a close
    basis; tests that need a wick-without-close use candles_ohlc.
    """
    return candles_ohlc(start_min, [(hi, hi, lo, lo) for hi, lo in bars])


def main() -> int:
    from _srcutil import executable_source
    RB, SD = fresh()
    src = (ROOT / "atlas" / "execution" / "range_breaker.py").read_text()
    exe = executable_source(ROOT / "atlas" / "execution" / "range_breaker.py")

    print("=" * 82)
    print("1 — DIRECTION: THE ONE THAT MUST NOT BE BACKWARDS")
    print("-" * 82)
    # CLOSE-BASIS: verdict() reads probe_close and nothing else.
    B = {"ok": True, "high": 100.0, "low": 90.0}
    cases = [
        ("LONG",  "close BELOW the floor",   89.0,  True,  "floor"),
        ("SHORT", "close ABOVE the ceiling", 101.0, True,  "ceiling"),
        # THE TWO THAT MUST NOT FIRE. A close in ATLAS's favour.
        ("LONG",  "close ABOVE the ceiling", 101.0, False, None),
        ("SHORT", "close BELOW the floor",   89.0,  False, None),
        ("LONG",  "close inside the range",  95.0,  False, None),
        ("SHORT", "close inside the range",  95.0,  False, None),
    ]
    for side, label, close, want_fire, want_level in cases:
        v = RB.verdict({**B, "probe_close": close}, side)
        check(f"{side:<5} {label:<24} fires={want_fire}",
              (v["broke"], v["level"]), (want_fire, want_level))
    # The favourable ones are RECORDED, so the replay can count them.
    check("a favourable close is recorded, not fired",
          RB.verdict({**B, "probe_close": 101.0}, "LONG")["favourable_break"],
          True)
    # EQUALITY IS NOT A BREAK. Closing exactly ON the level is not outside it.
    check("closing exactly ON the floor is not a break",
          RB.verdict({**B, "probe_close": 90.0}, "LONG")["broke"], False)
    check("closing exactly ON the ceiling is not a break",
          RB.verdict({**B, "probe_close": 100.0}, "SHORT")["broke"], False)
    # NO COMPLETED MINUTE YET is not a break either.
    check("no probe close yet is not a break",
          RB.verdict({**B, "probe_close": None}, "LONG")["broke"], False)
    check("direction lives in exactly one function",
          exe.count("def verdict("), 1)

    print()
    print("=" * 82)
    print("2 — THE RANGE IS THE OPENING RANGE, FIXED")
    print("-" * 82)
    # 45 candles 09:15-09:59 pin the range, then price drifts 100 points lower
    # for half an hour. A ROLLING box would have followed it down and never
    # broken; the fixed range is still the morning's.
    base = [(100.0, 90.0)] * 45
    drift = base + [(5.0, 1.0)] * 30
    now = dt.datetime(2026, 10, 15, 10, 46, tzinfo=IST)
    b = RB.opening_range("X", now=now, candles=candles("09:15", drift),
                         use_cache=False)
    check("the range is built", b["ok"], True)
    check("  from the 45 opening-range candles", b["candles_used"], 45)
    check("  spanning 09:15-09:59", (b["first"], b["last"]), ("09:15", "09:59"))
    check("  the high is the MORNING's, not the recent one", b["high"], 100.0)
    check("  and the low likewise", b["low"], 90.0,
          "a rolling box would have descended to 1.0 and never broken")
    check("  a 30-minute decline breaks it",
          RB.verdict(b, "LONG")["broke"], True)
    # And the levels never move once set.
    later = RB.opening_range("X", now=dt.datetime(2026, 10, 15, 14, 0, tzinfo=IST),
                             candles=candles("09:15", drift), use_cache=False)
    check("the range is identical hours later",
          (later["high"], later["low"]), (b["high"], b["low"]))
    # Candles outside 09:15-10:00 are not in the range.
    pre = [(999.0, 1.0)] * 5          # 09:10-09:14, before the open
    cs2 = candles("09:10", pre + base)
    b2 = RB.opening_range("X", now=now, candles=cs2, use_cache=False)
    check("candles before 09:15 are excluded", (b2["high"], b2["low"]),
          (100.0, 90.0), "pre-open prints are not the opening range")

    print()
    print("=" * 82)
    print("3 — CLOSE-BASIS: A WICK IS NOT A BREAK")
    print("-" * 82)
    # The operator's own example, floor at 25,090.
    orc = [(25110.0, 25090.0)] * 45
    for label, hi, lo, close, want in (
            ("low 25085 close 25096 -> no break", 25096.0, 25085.0, 25096.0, False),
            ("low 25085 close 25087 -> break",    25091.0, 25085.0, 25087.0, True)):
        cs3 = candles_ohlc("09:15", [(25100.0, 25110.0, 25090.0, 25100.0)] * 45
                           + [(25090.0, hi, lo, close)])
        bb = RB.opening_range("X", now=dt.datetime(2026, 10, 15, 10, 1, tzinfo=IST),
                              candles=cs3, use_cache=False)
        check(label, RB.verdict(bb, "LONG")["broke"], want,
              f"range {bb['low']:.0f}-{bb['high']:.0f} close {bb['probe_close']:.0f}")
    check("the probe is the LAST COMPLETED minute's close",
          "last COMPLETED minute" in src, True,
          "the current minute is still forming")
    check("the stop-sweep reasoning is written down",
          "bull_liq_sweep" in src, True)
    check("the live last price is display-only",
          "DELIBERATELY NOT PART OF THE BREAK TEST" in src, True,
          "folding it in would reintroduce the wick")

    print()
    print("=" * 82)
    print("3b — WHEN THE RANGE CANNOT BE BUILT: FAIL OPEN, LOUDLY")
    print("-" * 82)
    empty = RB.opening_range("X", now=dt.datetime(2026, 10, 15, 11, 0, tzinfo=IST),
                             candles=[], use_cache=False)
    check("no candles at all reports CANNOT_EVALUATE", empty["reason_code"],
          RB.REASON_CANNOT_EVALUATE)
    check("  and does NOT fire", RB.verdict(empty, "LONG")["broke"], False)
    check("  and is not an all-clear", empty["ok"], False)
    nosess = RB.opening_range("X", now=dt.datetime(2026, 10, 15, 11, 0, tzinfo=IST),
                              candles=candles("11:00", [(100.0, 90.0)] * 10),
                              use_cache=False)
    check("candles but none in the range window reports CANNOT_EVALUATE",
          nosess["reason_code"], RB.REASON_CANNOT_EVALUATE)
    check("the fail-open reasoning is written down",
          "FAIL OPEN, DELIBERATELY, AND DO NOT REVERSE THIS" in src, True)
    check("  and says why: the breaker is one-way",
          "ONE-WAY" in src and "stop-loss" in src, True)

    print()
    print("=" * 82)
    print("4 — ARMING, AND DORMANCY WHEN FLAT")
    print("-" * 82)
    # ARMED WHEN THE RANGE COMPLETES, at 10:00 -- not 09:50. The previous
    # version armed at 09:50 and then reported "cannot evaluate" for ten
    # minutes because its box was not full.
    check("at 09:59 nothing is armed",
          RB.armed(dt.datetime(2026, 10, 15, 9, 59, tzinfo=IST))[0], False)
    check("at 10:00 it is armed",
          RB.armed(dt.datetime(2026, 10, 15, 10, 0, tzinfo=IST))[0], True)
    check("  and an unarmed range says it is still forming",
          "still forming" in RB.armed(
              dt.datetime(2026, 10, 15, 9, 30, tzinfo=IST))[1], True)
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
    print("5 — THE MARKET BREAKER: OBSERVING TODAY, AND WHAT IT WOULD DO")
    print("-" * 82)
    check("the reason code names a RANGE BREAK",
          RB.REASON_MARKET_BREAK, "RANGE_BREAK_STAND_DOWN")
    check("  and the stand-down module agrees",
          SD.REASON_STAND_DOWN, "RANGE_BREAK_STAND_DOWN")
    check("  the old sentiment name is gone from the code",
          "SENTIMENT_REVERSAL_STAND_DOWN" in
          executable_source(ROOT / "atlas" / "execution" / "sentiment_window.py"),
          False)
    import atlas.config as CC
    # ARMED 2026-10-16. This asserted BREAKER_OBSERVE_ONLY was True -- "today's
    # session is evidence, not enforcement" -- and 2026-10-16 WAS that session:
    # 23 distinct symbols broke their opening range floor, the market breaker
    # fired nothing, and the 10 that closed lost Rs546 against Rs577 made by
    # the symbols that did not break. The evidence was collected, so the flag
    # is off.
    # READ FROM THE SOURCE, NOT FROM THE MODULE. fresh() assigns
    # C.BREAKER_OBSERVE_ONLY to drive the two branches below, so the imported
    # attribute reflects the last fixture rather than what is committed -- and
    # what is committed is the thing that decides whether Monday is armed.
    cfg_src = (ROOT / "atlas" / "config.py").read_text(encoding="utf-8")
    check("BREAKER_OBSERVE_ONLY is OFF — the breakers are armed",
          "\nBREAKER_OBSERVE_ONLY = False" in cfg_src, True,
          "exempt from Item 4 by ruling; the only self-inflicted stand-down")

    # THE EXEMPTION, ASSERTED. Item 4 converted ten self-halt sites to
    # alert-only, and the reversal breakers must NOT have been caught by it.
    # Two things have to still be true: stand_down() must not route through
    # breaker.halt() (which now only flags), and the entry gate must still
    # honour the stand-down.
    from atlas.risk import breaker as _BRK
    sw_src = executable_source(ROOT / "atlas" / "execution" / "sentiment_window.py")
    check("the breakers' stand-down does NOT route through breaker.halt",
          "breaker.halt" in sw_src or "halt(" in sw_src, False,
          "it has its own session-keyed file and alert, so Item 4 missed it")
    check("  and breaker.flag exists, so halt was the thing converted",
          callable(getattr(_BRK, "flag", None)), True)
    entry_src = executable_source(ROOT / "atlas" / "execution" / "atlas_entry.py")
    check("the entry gate still honours the stand-down",
          "SW.state()" in entry_src and "sd['stood_down']" in entry_src, True,
          "a stand-down nothing reads is inert")

    RB, SD = fresh()
    broke = candles("09:15", [(100.0, 90.0)] * 45 + [(89.0, 80.0)])
    now = dt.datetime(2026, 10, 15, 10, 1, tzinfo=IST)
    m = RB.market_check(now=now, rows=[{"direction": "LONG"}], candles=broke,
                        last_price=80.0)
    # OBSERVING: it computes, records and alerts -- and blocks nothing.
    check("a floor close while long is detected", m["observed"], True)
    check("  but it does NOT fire", m["fired"], False)
    check("  the state says what it would have done", m["state"],
          "OBSERVING_WOULD_STAND_DOWN")
    check("  the day is NOT stood down", SD.state()["stood_down"], False,
          "BREAKER_OBSERVE_ONLY")
    check("  the would-be fire is recorded", len(RB.observed()), 1)
    for want in ("floor", "opening range", "closed at", "IST"):
        check(f"  the detail names the {want}", want in m["detail"], True)
    check("  and the range's own numbers",
          "90.00-100.00" in m["detail"], True)
    check("  recording is idempotent",
          (RB.market_check(now=now, rows=[{"direction": "LONG"}],
                           candles=broke, last_price=80.0),
           len(RB.observed()))[1], 1, "one alert, not one per cycle")

    # WITH OBSERVE-ONLY OFF it stands the day down. Asserted so the enforcing
    # path is covered before it is ever switched on.
    RB, SD = fresh(observe_only=False)
    m = RB.market_check(now=now, rows=[{"direction": "LONG"}], candles=broke,
                        last_price=80.0)
    check("with observe-only OFF it fires", m["fired"], True)
    check("  under the range-break code", m["state"], "RANGE_BREAK_STAND_DOWN")
    check("  and stands the day down", SD.state()["stood_down"], True)

    # THE ONE THAT MUST NOT FIRE, through the whole call path.
    RB, SD = fresh(observe_only=False)
    up = candles("09:15", [(100.0, 90.0)] * 45 + [(120.0, 110.0)])
    m = RB.market_check(now=now, rows=[{"direction": "LONG"}], candles=up,
                        last_price=120.0)
    check("a CEILING close while long does NOT fire", m["fired"], False,
          "firing here would stop ATLAS on its winners")
    check("  nor is it even observed", m.get("observed"), None)
    check("  the day is not stood down", SD.state()["stood_down"], False)
    check("  and it is reported as favourable",
          "FAVOURABLE" in m["detail"], True)
    # DORMANT: the same break with nothing open.
    RB, SD = fresh(observe_only=False)
    m = RB.market_check(now=now, rows=[], candles=broke, last_price=80.0)
    check("the same break with nothing open stays DORMANT", m["fired"], False)
    check("  reported as dormant", m["state"], RB.REASON_DORMANT)
    check("  and nothing is recorded", len(RB.observed()), 0)

    print()
    print("=" * 82)
    print("6 — THE STOCK BREAKER: ONE SYMBOL, NEVER THE DAY")
    print("-" * 82)
    RB, SD = fresh()
    r = RB.stock_check("TESTSYM", "LONG", instrument_key="NSE_EQ|X", now=now,
                       candles=broke, last_price=80.0)
    check("a floor close on a stock is detected", r["observed"], True)
    check("  but blocks nothing while observing", r["broke"], False)
    check("  and says what it would have done", r["state"],
          "OBSERVING_WOULD_BLOCK")
    check("  the symbol is NOT blocked", RB.is_blocked("TESTSYM")[0], False)
    check("  the would-be block is recorded", len(RB.observed()), 1)

    RB, SD = fresh(observe_only=False)
    r = RB.stock_check("TESTSYM", "LONG", instrument_key="NSE_EQ|X", now=now,
                       candles=broke, last_price=80.0)
    check("with observe-only OFF it blocks", r["broke"], True)
    check("  under its own code", r["state"], RB.REASON_SYMBOL_BREAK)
    check("  the symbol is blocked", RB.is_blocked("TESTSYM")[0], True)
    check("  another symbol is NOT", RB.is_blocked("OTHERSYM")[0], False)
    check("  and the DAY is not stood down", SD.state()["stood_down"], False,
          "only the market breaker does that")
    check("a blocked symbol stays blocked next cycle",
          RB.stock_check("TESTSYM", "LONG", instrument_key="NSE_EQ|X", now=now,
                         candles=candles("09:15", [(100.0, 90.0)] * 46)
                         )["state"], RB.REASON_SYMBOL_BREAK)
    path = Path(os.environ["ATLAS_BREAKER_BLOCKED_FILE"])
    check("it is written to disk", path.exists(), True)
    importlib.reload(RB)
    check("  and survives a restart", RB.is_blocked("TESTSYM")[0], True)
    d = json.loads(path.read_text()); d["session_date"] = "2001-01-01"
    path.write_text(json.dumps(d))
    importlib.reload(RB)
    check("yesterday's block does not carry into today",
          RB.is_blocked("TESTSYM")[0], False)

    # use_cache=False MEANS NEITHER READ NOR WRITE. The replay passes it, and
    # the first version still persisted what it computed -- so running the
    # replay wrote a bogus key into the live range file.
    RB, SD = fresh(observe_only=False)
    rp0 = Path(os.environ["ATLAS_BREAKER_RANGE_FILE"])
    rp0.unlink(missing_ok=True)
    RB.opening_range("replay", now=now, candles=broke, use_cache=False)
    check("use_cache=False writes nothing", rp0.exists(), False,
          "a tool must not persist into the engine's state")

    # THE RANGE PERSISTS TOO -- it cannot be rebuilt this afternoon.
    RB, SD = fresh(observe_only=False)
    rp = Path(os.environ["ATLAS_BREAKER_RANGE_FILE"])
    RB.opening_range("NSE_EQ|P", now=now, candles=broke)
    check("the opening range is persisted", rp.exists(), True)
    importlib.reload(RB)
    cached = RB.opening_range("NSE_EQ|P", now=now, candles=[])
    check("  and survives a restart with no candles at all",
          (cached["ok"], cached["high"], cached["low"]), (True, 100.0, 90.0),
          "a live feed cannot rebuild the morning this afternoon")
    check("  marked as cached", cached["from_cache"], True)
    d = json.loads(rp.read_text()); d["session_date"] = "2001-01-01"
    rp.write_text(json.dumps(d))
    importlib.reload(RB)
    stale = RB.opening_range("NSE_EQ|P", now=now, candles=[])
    check("yesterday's range does not carry into today", stale["ok"], False)

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
    check("OPENING_RANGE_START", C.OPENING_RANGE_START, "09:15")
    check("OPENING_RANGE_END", C.OPENING_RANGE_END, "10:00")
    check("the rolling-window constant is gone",
          hasattr(C, "BREAKER_WINDOW_CYCLES"), False)
    check("  and so is BREAKER_ARMS_AT", hasattr(C, "BREAKER_ARMS_AT"), False)
    # Two config imports exist and both are legitimate: the breaker constants
    # in _cfg(), and SUPABASE_* in held_side(). Scoped to the ones that are
    # the policy.
    check("the breaker constants are read in exactly one place",
          sum(1 for l in exe.splitlines()
              if "from atlas.config import" in l and "BREAKER_" in l), 1,
          "_cfg()")
    import re
    scoped = "\n".join(l for l in exe.splitlines() if "timedelta(" not in l)
    check("no range time is written as a literal",
          [n for n in ("09:15", "10:00")
           if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w.])", scoped)],
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
    check("  the inventory can tell observed from all-clear",
          "OBSERVED_WOULD_FIRE" in sw, True)
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
