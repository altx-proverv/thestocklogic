"""
NO PRICE SENT TO THE BROKER MAY BE OFF ITS INSTRUMENT'S TICK
============================================================

2026-10-15: ATHERENERG trades on a 0.10 tick; exits.py rounded every
protective price through a module constant TICK = 0.05. The broker refused the
day-0 SL, the emergency exit LIMIT, the OCO GTT and the stop-only GTT -- four
mechanisms built so that no single fault could defeat them all -- and the
position sat open with no stop.

Six ticks exist across the 745-symbol universe. These checks cover all six, and
the sixth independence axis that would have caught the shared constant.
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
# every tick the master reports across the universe
ALL_TICKS = (0.01, 0.05, 0.10, 0.50, 1.00, 5.00)
OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<58}{str(got)[:18]:<20}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


def on_tick(v, tick) -> bool:
    if v in (None, 0):
        return True
    q = float(v) / float(tick)
    return abs(q - round(q)) < 1e-6


def fresh_map(entries: dict):
    """A tick map file holding exactly `entries`, for today."""
    os.environ["ATLAS_TICK_MAP_FILE"] = tempfile.mktemp(suffix=".json")
    day = dt.datetime.now(IST).date().isoformat()
    Path(os.environ["ATLAS_TICK_MAP_FILE"]).write_text(
        json.dumps({"days": {day: entries}, "last_known": dict(entries)}))
    import atlas.config as C
    importlib.reload(C)
    import atlas.execution.tick_map as TM
    importlib.reload(TM)
    import atlas.execution.exits as X
    importlib.reload(X)
    import atlas.reporting.telegram as TG
    TG.send = lambda *a, **k: None
    return TM, X


class Rec:
    def __init__(self, last):
        self._last = last
        self.sent = []

    def place_order(self, **kw):
        self.sent.append(kw); return "O"

    def place_gtt(self, **kw):
        self.sent.append(kw); return {"trigger_id": 1}

    def ltp(self, inst):
        if isinstance(inst, str):
            inst = [inst]
        return {i: {"last_price": self._last} for i in inst}

    def quote(self, inst):
        return self.ltp(inst)


def broker_prices(X, sym, tick, fill, stop):
    """Every price that would reach the broker, from the real functions."""
    out = []
    k = Rec(fill)
    X.protect_day0(sym, "LONG", 7, fill, stop, kite=k, tick=tick)
    for kw in k.sent:
        out += [("day0 trigger", kw.get("trigger_price")),
                ("day0 limit", kw.get("price"))]
    k2 = Rec(fill)
    X.emergency_exit(sym, "LONG", 7, "CNC", why="t", kite=k2)
    for kw in k2.sent:
        out.append(("exit limit", kw.get("price")))
    k3 = Rec(fill)
    X.place_overnight_gtts(sym, "LONG", 7, fill, stop, kite=k3, tick=tick)
    for kw in k3.sent:
        for i, t in enumerate(kw.get("trigger_values") or []):
            out.append((f"gtt trigger {i}", t))
        for i, o in enumerate(kw.get("orders") or []):
            out.append((f"gtt leg {i} limit", o.get("price")))
        out.append(("gtt last_price", kw.get("last_price")))
    out.append(("entry limit", X.price_for(sym, fill, "entry", "LONG",
                                           tick=tick)))
    return out


def main() -> int:
    from _srcutil import executable_source

    print("=" * 80)
    print("1 — ROUNDING IS DIRECTIONAL, AND THE DIRECTION IS THE SAFE ONE")
    print("=" * 80)
    TM, X = fresh_map({"S": 0.10})
    # a stop rounds AWAY from entry; nearest-rounding would have gone up here
    check("LONG stop 1418.86 @0.10 -> 1418.8 (down, away)",
          X.price_for("S", 1418.86, "stop", "LONG", tick=0.10), 1418.8)
    check("SHORT stop 1418.84 @0.10 -> 1418.9 (up, away)",
          X.price_for("S", 1418.84, "stop", "SHORT", tick=0.10), 1418.9)
    check("LONG entry 1453.72 @0.10 -> 1453.8 (up, to fill)",
          X.price_for("S", 1453.72, "entry", "LONG", tick=0.10), 1453.8)
    check("SHORT entry 1453.78 @0.10 -> 1453.7 (down, to fill)",
          X.price_for("S", 1453.78, "entry", "SHORT", tick=0.10), 1453.7)
    check("LONG target 1523.47 @0.10 -> 1523.4 (toward entry)",
          X.price_for("S", 1523.47, "target", "LONG", tick=0.10), 1523.4)
    check("SHORT target 1523.42 @0.10 -> 1523.5 (toward entry)",
          X.price_for("S", 1523.42, "target", "SHORT", tick=0.10), 1523.5)
    check("LONG exit 1439.17 @0.10 -> 1439.1 (toward marketable)",
          X.price_for("S", 1439.17, "exit", "LONG", tick=0.10), 1439.1)
    # A STOP IS NEVER ROUNDED TIGHTER THAN ASKED. The property, not an example.
    worse = all(X.price_for("S", p, "stop", "LONG", tick=0.10) <= p
                for p in (1418.86, 1000.01, 99.99, 12.345))
    check("a LONG stop is never rounded UP", worse, True,
          "rounding toward entry would silently tighten risk")

    print()
    print("=" * 80)
    print("2 — AN UNKNOWN TICK REFUSES; IT DOES NOT DEFAULT")
    print("=" * 80)
    TM, X = fresh_map({"KNOWN": 0.05})
    check("a known symbol resolves", TM.tick_for("KNOWN")[0], 0.05)
    check("  from today's map", TM.tick_for("KNOWN")[1], "today")
    t, src = TM.tick_for("NEVERSEEN")
    check("an unknown symbol returns no tick", t, None)
    check("  under its own reason code", src, "NO_TICK_SIZE_CANNOT_PRICE")
    raised = False
    try:
        X.price_for("NEVERSEEN", 100.0, "stop", "LONG")
    except X.TickUnknown:
        raised = True
    check("price_for RAISES rather than guessing", raised, True,
          "a default tick is what refused four orders")
    # THE LAST KNOWN TICK covers a failed refresh.
    os.environ["ATLAS_TICK_MAP_FILE"] = tempfile.mktemp(suffix=".json")
    Path(os.environ["ATLAS_TICK_MAP_FILE"]).write_text(json.dumps(
        {"days": {"2001-01-01": {"OLD": 0.50}}, "last_known": {"OLD": 0.50}}))
    import atlas.config as C
    importlib.reload(C)
    importlib.reload(TM)
    t2, src2 = TM.tick_for("OLD", allow_refresh=False)
    check("a symbol absent from today falls back to last known", t2, 0.50)
    check("  and says which day it came from",
          str(src2).startswith("last_known"), True, src2)

    print()
    print("=" * 80)
    print("3 — EVERY BROKER PRICE, AT EVERY TICK IN THE UNIVERSE")
    print("=" * 80)
    CASES = {0.01: (12.52, 12.09), 0.05: (240.90, 232.80),
             0.10: (1453.70, 1418.86), 0.50: (4899.00, 4700.55),
             1.00: (10250.00, 9870.40), 5.00: (122270.00, 117380.00)}
    for tick in ALL_TICKS:
        fill, stop = CASES[tick]
        TM, X = fresh_map({"SYM": tick})
        rows = broker_prices(X, "SYM", tick, fill, stop)
        bad = [(l, v) for l, v in rows if not on_tick(v, tick)]
        check(f"tick {tick:<5} — {len(rows)} broker price(s), none off-tick",
              bad, [], f"fill {fill}")
    # and the one that actually failed, verbatim
    TM, X = fresh_map({"ATHERENERG": 0.10})
    rows = broker_prices(X, "ATHERENERG", 0.10, 1453.70, 1418.86)
    check("ATHERENERG 2026-10-15, verbatim: every price on tick",
          [(l, v) for l, v in rows if not on_tick(v, 0.10)], [])
    check("  and it produced prices at all", len(rows) >= 8, True,
          f"{len(rows)} prices")
    # THE OLD CONSTANT WOULD HAVE FAILED THIS. Proof the test has teeth.
    check("the old 0.05 rounding IS off-tick on a 0.10 instrument",
          on_tick(X.round_to_tick(1418.86, 0.05, X.NEAREST), 0.10), False,
          "1418.85 is not a multiple of 0.10 — the 2026-10-15 rejection")

    print()
    print("=" * 80)
    print("4 — NO PRODUCTION PRICE PATH USES A CONSTANT TICK")
    print("=" * 80)
    for rel in ("atlas/execution/exits.py", "atlas/execution/preclose_gtt.py",
                "atlas/execution/atlas_entry.py"):
        src = executable_source(ROOT / rel)
        # round_tick with no tick argument is the legacy shim; it must not
        # appear on any path that reaches the broker.
        # TWO NAMED EXEMPTIONS, both documented in exits.py and neither
        # broker-bound:
        #   _rp()             a reporting fallback for summary strings
        #   swing_low_since() a structural level, re-rounded with the real
        #                     tick by tighten_only before it becomes a trigger
        EXEMPT = ("return round_tick(price)",
                  "return round_tick(found) if found is not None else None")
        bad = [l.strip() for l in src.splitlines()
               if "round_tick(" in l and "round_to_tick" not in l
               and "def round_tick" not in l and "tick=" not in l
               and "price_for" not in l and l.strip() not in EXEMPT]
        check(f"{rel.split('/')[-1]:<22}no bare round_tick on a price path",
              bad, [], f"{len(bad)} line(s)")
    ex = executable_source(ROOT / "atlas" / "execution" / "exits.py")
    check("price_for has no default tick", "tick: float=None" in ex, True,
          "None means look it up or raise, not 0.05")
    check("the emergency exit resolves its OWN tick",
          "_etick" in ex, True, "not the primary's resolved value")

    print()
    print("=" * 80)
    print("5 — THE SIXTH INDEPENDENCE AXIS: PRICE VALIDITY")
    print("=" * 80)
    TM, X = fresh_map({"S": 0.10})
    check("every spec declares a tick_source",
          sorted({v.get("tick_source") for k, v in X.ORDER_SPECS.items()
                  if k in ("GTT_OCO", "LIMIT_THROUGH", "SL", "SLM")}),
          ["per_instrument"])
    check("the live pair passes the axis",
          (X.assert_independent("SL"), True)[1], True)
    saved = X.ORDER_SPECS["SL"]["tick_source"]
    try:
        X.ORDER_SPECS["SL"]["tick_source"] = "constant"
        raised = False
        try:
            X.assert_independent("SL")
        except AssertionError as e:
            raised = "CONSTANT tick" in str(e)
        check("a CONSTANT tick fails the axis", raised, True,
              "this is what would have caught 2026-10-15")
        X.ORDER_SPECS["SL"]["tick_source"] = "shared_resolved"
        X.ORDER_SPECS["LIMIT_THROUGH"]["tick_source"] = "shared_resolved"
        raised2 = False
        try:
            X.assert_independent("SL")
        except AssertionError as e:
            raised2 = "reuse one resolved tick" in str(e)
        check("  and so does reusing one resolved value", raised2, True)
    finally:
        X.ORDER_SPECS["SL"]["tick_source"] = saved
        X.ORDER_SPECS["LIMIT_THROUGH"]["tick_source"] = "per_instrument"
    src = (ROOT / "atlas" / "execution" / "exits.py").read_text()
    check("the axis is documented with its cause",
          "A SIXTH AXIS, ADDED 2026-10-16" in src, True)

    print()
    print("=" * 80)
    print("6 — THE MAP IS REFRESHED BEFORE THE OPEN, AND SURFACED")
    print("=" * 80)
    zm = executable_source(ROOT / "atlas" / "execution" / "zerodha_morning.py")
    check("the 08:30 job refreshes the tick map", "tick_map" in zm, True)
    check("  and reports it either way", "_tmline" in zm, True)
    sw = executable_source(ROOT / "tools" / "atlas_switches.py")
    check("the inventory shows the tick map", "tick map" in sw, True)
    ra = (ROOT / "tests" / "run_all.py").read_text()
    check("the suite seeds a fixture map rather than hitting the broker",
          "ATLAS_TICK_MAP_FILE" in ra, True)
    tmsrc = (ROOT / "atlas" / "execution" / "tick_map.py").read_text()
    check("the map refuses to write the live file while drilling",
          "is_drill" in tmsrc and "/var/lib/atlas/tick_map.json" in tmsrc, True)

    print()
    print("=" * 80)
    print("PASS" if OK else "FAIL")
    print("=" * 80)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
