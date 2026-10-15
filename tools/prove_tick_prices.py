"""
EVERY PRICE THAT REACHES THE BROKER, AT EVERY TICK SIZE. PLACES NOTHING.
========================================================================

On 2026-10-15 ATHERENERG (0.10 tick) had four prices refused because all four
rounded through a hardcoded 0.05. This computes all eight broker-bound prices
for a given symbol/tick and checks each is a multiple of that instrument's tick.

STUB BROKER, NO ORDERS. The kite object records what it was asked to send and
returns a fake id; nothing reaches a network. Run it anywhere.

    python3 -m tools.prove_tick_prices
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.execution import exits as X


class Recorder:
    """Records every price it is asked to send. Places nothing."""

    def __init__(self, last):
        # NOT self.ltp -- that shadows the ltp() method the broker helper calls.
        self._last = last
        self.sent = []

    def place_order(self, **kw):
        self.sent.append(("order", kw))
        return "ORD-STUB"

    def place_gtt(self, **kw):
        self.sent.append(("gtt", kw))
        return {"trigger_id": 999}

    def ltp(self, inst):
        if isinstance(inst, str):
            inst = [inst]
        return {i: {"last_price": self._last} for i in inst}

    def quote(self, inst):
        return self.ltp(inst)


def on_tick(v, tick) -> bool:
    if v in (None, 0):
        return True
    q = float(v) / float(tick)
    return abs(q - round(q)) < 1e-6


def seed(symbol, tick):
    """Put this symbol's tick in the map so the emergency exit -- which
    resolves its OWN tick, by design, see assert_independent's price axis --
    can find it without a broker session."""
    import json
    from datetime import datetime, timezone, timedelta
    from atlas.execution import tick_map as TM
    IST = timezone(timedelta(hours=5, minutes=30))
    day = datetime.now(IST).date().isoformat()
    path = TM._path()
    d = {}
    if path.exists():
        try:
            d = json.loads(path.read_text())
        except Exception:
            d = {}
    d.setdefault("days", {}).setdefault(day, {})[symbol] = tick
    d.setdefault("last_known", {})[symbol] = tick
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d))
    TM._MEM.update({"day": None, "map": None})


def prices_for(symbol, tick, fill, stop, qty=7):
    seed(symbol, tick)
    """Every broker-bound price for one symbol at one tick. -> [(label, value)]"""
    out = []
    # 1-2  the day-0 SL: trigger and limit
    k = Recorder(fill)
    r = X.protect_day0(symbol, "LONG", qty, fill, stop, kite=k, tick=tick)
    for kind, kw in k.sent:
        out.append(("day-0 SL trigger", kw.get("trigger_price")))
        out.append(("day-0 SL limit", kw.get("price")))
    # 3    the emergency exit limit
    k2 = Recorder(fill)
    e = X.emergency_exit(symbol, "LONG", qty, "CNC", why="proof", kite=k2)
    for kind, kw in k2.sent:
        out.append(("emergency exit limit", kw.get("price")))
    # 4-7  the overnight pair: OCO stop trigger + limit, target trigger + limit,
    #      and the stop-only GTT trigger + limit
    k3 = Recorder(fill)
    g = X.place_overnight_gtts(symbol, "LONG", qty, fill, stop, kite=k3,
                               tick=tick)
    for kind, kw in k3.sent:
        tv = kw.get("trigger_values") or []
        legs = kw.get("orders") or []
        if len(tv) == 2:
            out.append(("OCO stop trigger", tv[0]))
            out.append(("OCO target trigger", tv[1]))
            if len(legs) == 2:
                out.append(("OCO stop leg limit", legs[0].get("price")))
                out.append(("OCO target leg limit", legs[1].get("price")))
        elif len(tv) == 1:
            out.append(("stop-only GTT trigger", tv[0]))
            if legs:
                out.append(("stop-only GTT limit", legs[0].get("price")))
        out.append(("GTT last_price", kw.get("last_price")))
    # 8    the entry limit
    out.append(("entry LIMIT", X.price_for(symbol, fill, "entry", "LONG",
                                           tick=tick)))
    return out, r, g


def main() -> int:
    CASES = [
        ("ATHERENERG", 0.10, 1453.70, 1418.86,
         "the 2026-10-15 trade, verbatim"),
        ("SONATSOFTW", 0.05, 240.90, 232.80, "a 0.05 symbol — no regression"),
        ("IDEA", 0.01, 12.52, 12.09, "the finest tick in the universe"),
        ("MRF", 5.00, 122270.00, 117380.00, "the coarsest tick, 10 symbols"),
    ]
    allok = True
    for sym, tick, fill, stop, note in CASES:
        print("=" * 78)
        print(f"{sym}  tick {tick}  fill {fill}  published stop {stop}")
        print(f"  ({note})")
        print("=" * 78)
        rows, r, g = prices_for(sym, tick, fill, stop)
        print(f"  {'PRICE':<24}{'VALUE':>13}{'/TICK':>12}  ON TICK?")
        print("  " + "-" * 60)
        for label, v in rows:
            if v in (None, 0):
                continue
            ok = on_tick(v, tick)
            allok &= ok
            print(f"  {label:<24}{float(v):>13.4f}{float(v)/tick:>12.1f}"
                  f"  {'yes' if ok else '** OFF TICK **'}")
        print(f"  day-0 SL   : {r.get('reason')}")
        print(f"  overnight  : {str(g.get('reason'))[:120]}")
        print()
    print("=" * 78)
    print(f"EVERY BROKER-BOUND PRICE ON ITS INSTRUMENT'S TICK: "
          f"{'yes' if allok else '*** NO ***'}")
    print("=" * 78)
    print("Nothing was placed: the kite object is a recorder.")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
