#!/usr/bin/env python3
"""
PROVE THE SHORT PATH, END TO END, WITHOUT PLACING ANYTHING.
===========================================================

REPLACES tools/verify_long_only.py, which is deleted rather than edited. That
tool existed to prove two things that are now deliberately false: that no code
path can place a SHORT, and that a failed stop exits the position and halts the
account. Both were reversed by ruling on 2026-10-16. A verification tool whose
subject no longer exists cannot be weakened into agreement -- it has to be
replaced by one that proves the new guarantee.

THE NEW GUARANTEE, and it is narrower than "shorts work":

    A SHORT IS PERMITTED, AND A SHORT IS ALWAYS INTRADAY.

The second half is the one that matters. A CNC short is a DELIVERY short:
there are no shares to deliver, settlement fails, and the exchange buys them
in at auction. The auction price is set by whoever turns up, so it is the only
loss in ATLAS that a Rs10,000 notional does not bound -- every other failure
mode is capped by the notional and this one is not.

Every entry ATLAS has ever placed was CNC. This path has never run live, so
every layer of it is walked here and shown refusing or choosing correctly.

NOTHING IS PLACED. The broker is a recording stub; the only real reads are the
tick map and (with --live) the instrument master.

    python3 -m tools.verify_short_path
    python3 -m tools.verify_short_path --live    # real ticks from the box map
"""

import os
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")

# ── THIS HARNESS DECLARES ITSELF A DRILL ──────────────────────────
#
# It drives real code paths -- place_order, protect_day0, emergency_exit --
# against a stub broker, and real code paths write real state. On 2026-10-13 a
# verification tool run on the box set atlas_state.mode to PAUSED mid-session,
# because it drove a failure through code that PATCHes shared state. Every
# state path is redirected and ATLAS_DRILL is set, and the fence is ASSERTED
# below rather than assumed.
import tempfile as _tempfile
_tmpdir = Path(_tempfile.gettempdir())
os.environ["ATLAS_HALT_FILE"] = str(_tmpdir / "atlas_short_path_halt")
os.environ["ATLAS_DRILL"] = "1"
os.environ.setdefault("ATLAS_SYMBOL_BLOCK_FILE",
                      str(_tmpdir / "atlas_short_path_symbol_block.json"))
os.environ.setdefault("ATLAS_UNPROTECTED_FILE",
                      str(_tmpdir / "atlas_short_path_unprotected.json"))

# A TICK MAP FOR THE THREE SYMBOLS, seeded before any ATLAS import reads the
# path. Without it every price refuses with NO_TICK_SIZE_CANNOT_PRICE, which is
# the correct behaviour and makes this tool unable to prove anything -- the
# same reason tests/run_all.py seeds one. The ticks are the real values from
# the box map; --live re-resolves them.
if "ATLAS_TICK_MAP_FILE" not in os.environ:
    import json as _json
    import tempfile as _tmp
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _day = _dt.now(_tz(_td(hours=5, minutes=30))).date().isoformat()
    _seed = {"ACC": 0.10, "ADANIPOWER": 0.05, "AIAENG": 0.10,
             "RELIANCE": 0.05}
    _f = Path(_tmp.gettempdir()) / "atlas_short_path_tickmap.json"
    _f.write_text(_json.dumps({"days": {_day: _seed}, "last_known": _seed}))
    os.environ["ATLAS_TICK_MAP_FILE"] = str(_f)

from atlas.execution import exits as X                        # noqa: E402
from atlas.risk.position_sizing import size_for_trade          # noqa: E402
from atlas.config import (ALLOW_SHORT_ENTRIES, ALLOW_LONG_ENTRIES,  # noqa: E402
                          SHORT_PRODUCT_TYPE, LONG_PRODUCT_TYPE,
                          MIS_EXIT_TIME, MIS_BROKER_CUTOFF,
                          FIXED_NOTIONAL_PER_TRADE)

# REAL SHORT CANDIDATES from the published batch of 2026-10-15 -- the batch
# today's session trades. entry_ref and sl are verbatim from public.signals;
# the tick is the instrument's own, from /var/lib/atlas/tick_map.json.
CANDIDATES = [
    # symbol        entry_ref   published sl   tick
    ("ACC",          1236.10,     1282.86,     0.10),
    ("ADANIPOWER",    203.80,      206.97,     0.05),
    ("AIAENG",       3790.90,     3967.92,     0.10),
]


class StubBroker:
    """Records every order it is asked to place. Places nothing."""

    def __init__(self, ltp=None):
        self.orders_placed = []
        self.gtts_placed = []
        self._ltp = ltp or 100.0

    def place_order(self, **kw):
        self.orders_placed.append(kw)
        return f"STUB-{len(self.orders_placed)}"

    def place_gtt(self, **kw):
        self.gtts_placed.append(kw)
        return {"trigger_id": 90000 + len(self.gtts_placed)}

    def ltp(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        return {k: {"last_price": self._ltp} for k in keys}

    def orders(self):
        return []

    def get_gtts(self):
        return []

    def positions(self):
        return {"net": []}


def on_tick(px, tick) -> bool:
    if px is None:
        return False
    q = float(px) / float(tick)
    return abs(round(q) - q) < 1e-6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="resolve ticks from the real map instead of the "
                         "values recorded above")
    a = ap.parse_args()
    ok = True

    def check(label, got, want, note=""):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {label:<54}{str(got):<12}"
              f"{'ok' if good else '** want ' + str(want) + ' **':<22}{note}")
        return good

    print("=" * 110)
    print("THE FENCE, FIRST — this tool must not be able to touch production")
    print("=" * 110)
    from atlas.risk import breaker as _B
    _drilling, _dwhy = _B.is_drill()
    check("is_drill() is true", _drilling, True, str(_dwhy)[:50])
    check("the halt file is redirected",
          str(_B.SENTINEL) != "/var/lib/atlas/halt", True, str(_B.SENTINEL))
    if not _drilling:
        print("  ** REFUSING TO RUN: the fence is not up **")
        return 1

    print()
    print("=" * 110)
    print("LAYER 0 — THE CONFIG. Necessary, not sufficient.")
    print("=" * 110)
    check("ALLOW_SHORT_ENTRIES", ALLOW_SHORT_ENTRIES, True)
    check("ALLOW_LONG_ENTRIES still True", ALLOW_LONG_ENTRIES, True,
          "shorts are added, longs are not removed")
    check("SHORT_PRODUCT_TYPE", SHORT_PRODUCT_TYPE, "MIS",
          "a CNC short is a delivery short -> auction")
    check("LONG_PRODUCT_TYPE", LONG_PRODUCT_TYPE, "CNC")

    print()
    print("=" * 110)
    print("LAYER 1 — THE DIRECTION GATE names a SHORT on a bearish read")
    print("=" * 110)
    from atlas.execution.atlas_entry import allowed_side, regime_allows_side
    from atlas.execution import live_sentiment as _LS

    def _inject(verdict):
        """The gate reads live_sentiment.read() directly, so that is the
        injection point -- the same one tests/test_regime_gate.py uses."""
        _LS.read = lambda *a, **k: {"ok": True, "verdict": verdict,
                                    "reason_code": None,
                                    "detail": f"injected {verdict}"}

    _inject("bearish")
    ctx = {"regime": "bear", "source": "live", "extreme_bearish": True,
           "allow_accumulation": False}
    side, why, blk = allowed_side(ctx)
    check("allowed_side(bear + bearish)", side, "SHORT", str(why)[:40])
    check("  and it is not blocked", blk, None)
    check("regime_allows_side(..., SHORT)", regime_allows_side(ctx, "SHORT")[0],
          True)
    _inject("bullish")
    bull = {"regime": "bull", "source": "live", "extreme_bearish": False,
            "allow_accumulation": True}
    check("a bullish read still names LONG", allowed_side(bull)[0], "LONG",
          "the long path is unchanged")
    _inject("bearish")

    print()
    print("=" * 110)
    print("LAYER 2 — SIZING chooses MIS for a short, CNC for a long")
    print("=" * 110)
    sz = size_for_trade(entry_price=1236.10, stop_price=1282.86,
                        direction="SHORT")
    check("size_for_trade(SHORT).product", sz.get("product"), "MIS")
    check("size_for_trade(LONG).product",
          size_for_trade(entry_price=100.0, stop_price=97.0,
                         direction="LONG").get("product"), "CNC")
    print(f"      ACC at Rs{FIXED_NOTIONAL_PER_TRADE:,.0f} notional -> "
          f"qty {sz.get('qty')}, risk Rs{sz.get('risk_actual', 0):,.0f}, "
          f"stop {sz.get('stop_pct')}%")

    print()
    print("=" * 110)
    print("LAYER 3 — THE BROKER FLOOR refuses a SHORT that is not MIS")
    print("=" * 110)
    # THE GUARD THAT MAKES THE FLAG SAFE. place_order is the single funnel every
    # entry passes, and it re-decides this regardless of what the caller
    # believes -- the same two-guard shape the long-only rule used.
    from atlas.execution import broker as B
    _real_kite = B.get_kite
    B.get_kite = lambda *a, **k: StubBroker()
    try:
        bad = B.place_order(symbol="ACC", direction="SHORT", qty=1,
                            product="CNC", order_type="LIMIT", price=1236.10,
                            intent="ENTRY")
        check("SHORT + CNC is REFUSED at the floor", bad.get("success"), False,
              str(bad.get("blocked_by")))
        check("  and the refusal is determinate", bad.get("determinate"), True,
              "not retried as a transient")
        check("  named so it is countable",
              bad.get("blocked_by"), "short_must_be_intraday")
    finally:
        B.get_kite = _real_kite

    print()
    print("=" * 110)
    print("THE FULL ORDER SPEC — every price on the instrument's own tick")
    print("=" * 110)
    for symbol, entry_ref, published_sl, tick in CANDIDATES:
        # THE TICK COMES FROM THE MAP, ALWAYS. The values in CANDIDATES are
        # the real ones from the box, kept as a label and a fallback -- but
        # asserting them would make this tool fail under any harness that
        # seeds a different map (tests/run_all.py seeds one), and "every price
        # is on the tick the engine would use" is the property that matters,
        # not "every price is on the tick I wrote down".
        from atlas.execution.tick_map import tick_for
        t, src = tick_for(symbol, allow_refresh=a.live)
        if t:
            if float(t) != float(tick):
                print(f"\n[{symbol}] tick {t} from the map ({src}) — the "
                      f"recorded box value is {tick}; using the map")
            else:
                print(f"\n[{symbol}] tick {t} from the map ({src})")
            tick = float(t)
        else:
            print(f"\n[{symbol}] tick {tick} (recorded; the map has none: "
                  f"{src})")

        sz = size_for_trade(entry_price=entry_ref, stop_price=published_sl,
                            direction="SHORT")
        qty = int(sz.get("qty") or 0)
        product = sz.get("product")
        if qty < 1:
            print(f"  unaffordable at Rs{FIXED_NOTIONAL_PER_TRADE:,.0f} "
                  f"(qty 0) — skipped")
            continue

        rows = []
        # 1. THE ENTRY. A short entry SELLS, and the limit rounds DOWN so it is
        #    marketable on the way in.
        entry_px = X.price_for(symbol, entry_ref, "entry", "SHORT", tick=tick)
        rows.append(("entry", "SELL", product, "LIMIT", None, entry_px, qty))

        # 2. THE STOP. protect_day0 is the WHOLE protection for a short: MIS
        #    has no GTT, so there is no handover and nothing overnight.
        k = StubBroker(ltp=entry_px)
        r = X.protect_day0(symbol=symbol, direction="SHORT", qty=qty,
                           fill_price=entry_px, stop_price=published_sl,
                           kite=k, tick=tick)
        if not r.get("ok"):
            print(f"  ** protect_day0 refused: {r.get('reason')}")
            ok = False
            continue
        for o in k.orders_placed:
            rows.append(("stop (SL)", o["transaction_type"], o["product"],
                         o["order_type"], o.get("trigger_price"),
                         o.get("price"), o["quantity"]))

        # 3. THE TARGET. Computed and recorded; see the note printed below on
        #    why it is not PLACED as a leg for an MIS short.
        tgt = X.target_price(entry_px, published_sl, "SHORT", symbol=symbol,
                             tick=tick)
        rows.append(("target (recorded, not placed)", "BUY", product, "LIMIT",
                     None, tgt, qty))

        # 4. THE EXIT, if a human or a path ever needs it: a marketable LIMIT
        #    through the book. For a short that is a BUY, priced ABOVE the LTP.
        k2 = StubBroker(ltp=entry_px)
        ex = X.emergency_exit(symbol, "SHORT", qty, product,
                              why="verification", kite=k2)
        for o in k2.orders_placed:
            rows.append(("exit through book", o["transaction_type"],
                         o["product"], o["order_type"], o.get("trigger_price"),
                         o.get("price"), o["quantity"]))

        print(f"  {'LEG':<32}{'SIDE':<6}{'PROD':<6}{'TYPE':<7}"
              f"{'TRIGGER':>12}{'PRICE':>12}{'QTY':>6}  ON TICK?")
        print("  " + "-" * 104)
        for leg, side_, prod, typ, trig, px, q in rows:
            marks = [on_tick(v, tick) for v in (trig, px) if v is not None]
            good = all(marks)
            ok &= good
            if prod != SHORT_PRODUCT_TYPE:
                ok = False
            print(f"  {leg:<32}{side_:<6}{prod:<6}{typ:<7}"
                  f"{(f'{trig:.2f}' if trig is not None else '—'):>12}"
                  f"{(f'{px:.2f}' if px is not None else '—'):>12}{q:>6}"
                  f"  {'yes' if good else '** NO **'}")
        # THE RELATIONSHIPS, not just the rounding. A short's stop is ABOVE the
        # entry and its limit is ABOVE the trigger, or it triggers and rests
        # unfilled -- the mirror of the long-side bug Zerodha names.
        sl_rows = [r_ for r_ in rows if r_[0].startswith("stop")]
        for _, _, _, _, trig, px, _ in sl_rows:
            check(f"  {symbol}: stop trigger is ABOVE the entry",
                  trig > entry_px, True, f"{trig} > {entry_px}")
            check(f"  {symbol}: BUY limit is ABOVE its trigger",
                  px > trig, True, f"{px} > {trig} — marketable")
        check(f"  {symbol}: target is BELOW the entry", tgt < entry_px, True,
              f"{tgt} < {entry_px} — a short profits downward")

    print()
    print("=" * 110)
    print("THE SQUARE-OFF — what closes an MIS short if nothing else does")
    print("=" * 110)
    from atlas.execution import mis_squareoff as MS
    print(f"  ATLAS squares off MIS at      {MIS_EXIT_TIME} IST "
          f"(atlas/execution/mis_squareoff.py, cron 09:30 UTC)")
    print(f"  Zerodha's own cutoff is       {MIS_BROKER_CUTOFF} IST — the "
          f"BACKSTOP, not the plan")
    check("ATLAS exits before the broker does",
          MIS_EXIT_TIME < MIS_BROKER_CUTOFF, True,
          "config asserts this at import")
    check("the square-off selects on product=MIS",
          "product=eq.MIS" in open(ROOT / "atlas/execution/mis_squareoff.py"
                                   ).read(), True,
          "so a short is picked up by it")

    print()
    print("=" * 110)
    print("WHAT A SHORT DOES NOT GET, STATED RATHER THAN DISCOVERED")
    print("=" * 110)
    print("  · NO OCO AND NO TARGET LEG AT THE BROKER. A GTT does not exist")
    print("    for MIS -- that is the fact that forced SL-M on 2026-10-07 --")
    print("    so a short's protection is the SL order alone. The 2R target is")
    print("    computed and recorded on the row; it is not resting. Placing it")
    print("    as a second independent LIMIT would reintroduce the orphan")
    print("    bookkeeping the 2026-10-12 simplification removed, and")
    print("    designing that is not something to invent in passing.")
    print("  · NO OVERNIGHT HANDOVER. An MIS position is closed the same day,")
    print("    so preclose_gtt never routes a short to place_overnight_gtts.")
    print("  · NO TRAIL. The trail modifies a stop-only GTT, which a short")
    print("    does not have.")
    k3 = StubBroker()
    r3 = X.protect(symbol="ACC", direction="SHORT", qty=1, product="MIS",
                   fill_price=1236.10, stop_price=1282.86, kite=k3, tick=0.10)
    check("protect() (the CNC OCO GTT) refuses a short", r3.get("ok"), False,
          str(r3.get("reason"))[:46])
    check("  and it placed no GTT", len(k3.gtts_placed), 0)

    print()
    print("=" * 110)
    print("SHORT PATH:", "VERIFIED" if ok else "*** DEFECTIVE ***")
    print("=" * 110)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
