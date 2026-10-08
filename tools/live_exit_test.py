#!/usr/bin/env python3
"""
ATLAS — the LIVE one-share exit test
====================================
THE ONLY THING THAT ANSWERS THE QUESTION. Everything in tools/verify_exit_path.py
runs against a stub. The 2026-10-07 incident happened because this exit path had
never called the real Zerodha API, and no amount of stub testing fixes that.

RUN BY A HUMAN, WITH THE MARKET OPEN, ONE PHASE AT A TIME. Nothing here runs
without an explicit phase number and the live acknowledgement flag.

    python3 -m tools.live_exit_test --preflight
    python3 -m tools.live_exit_test --phase 1 --live   # SL shape      no position
    python3 -m tools.live_exit_test --phase 2 --live   # LIMIT shape   no position
    python3 -m tools.live_exit_test --phase 3 --live   # the real chain, 1 share

THE PHASES ARE ORDERED BY COMMITMENT, NOT BY CONVENIENCE:
  1  places a REAL SL order far from the market, reads it back, cancels it.
     No position is opened and nothing can fill. This alone answers the
     question that killed 2026-10-07: does Zerodha accept this order shape?
  2  the same for the marketable-LIMIT fallback, priced far from the market so
     it rests instead of filling, then cancelled.
  3  the whole chain on ONE SHARE: enter, let exits.protect() place the real
     stop, read it back from the broker, then close with the real fallback.
     This one transacts. Expect to pay brokerage and about a rupee of spread.

See docs/LIVE_EXIT_TEST.md for what a pass looks like.
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))

# A liquid, high-priced name so one share is a meaningful notional and the
# spread is a paisa or two. Override with --symbol.
DEFAULT_SYMBOL = "BHARTIARTL"

# FAR ENOUGH THAT IT CANNOT FILL. 15% away on a large cap inside one session is
# not a price that happens; the order rests and is cancelled a second later.
FAR_PCT = 0.15


def ist():
    return datetime.now(IST)


def fail(msg, code=1):
    print(f"\n  FAIL — {msg}")
    sys.exit(code)


def preflight(symbol):
    """Everything that must be true before a live order is worth attempting."""
    print(f"PREFLIGHT  {ist():%Y-%m-%d %H:%M:%S} IST\n")
    ok = True

    from atlas.config import (MIS_EXIT_TIME, MIS_BROKER_CUTOFF,
                              LIVE_TRADING_ENABLED, ENABLE_EXIT_MANAGEMENT)
    from atlas.execution.broker import get_kite, get_ltp
    from atlas.execution import exits as X
    from atlas.risk import breaker

    now = ist()
    market_open = (9, 15) <= (now.hour, now.minute) <= (15, 30)
    print(f"  market hours            : {market_open}  ({now:%H:%M} IST)")
    ok &= market_open

    cut = tuple(int(x) for x in MIS_BROKER_CUTOFF.split(":"))
    before_cutoff = (now.hour, now.minute) < cut
    print(f"  before the MIS cutoff   : {before_cutoff}  "
          f"(cutoff {MIS_BROKER_CUTOFF}, engine exit {MIS_EXIT_TIME})")
    ok &= before_cutoff

    # PHASE 3 NEEDS ROOM BEFORE THE SQUARE-OFF. Opening a one-share MIS short at
    # 14:58 hands it to the timer, which is a different test.
    roomy = (now.hour * 60 + now.minute) < (cut[0] * 60 + cut[1] - 45)
    print(f"  >45 min before cutoff   : {roomy}  (phase 3 needs the room)")

    halted, why = breaker.is_halted()
    print(f"  ATLAS halted            : {halted}  {why[:80]}")
    print(f"    (expected True — this test runs WHILE halted. The exit path is")
    print(f"     not halt-gated by design; entries are.)")

    kite = get_kite()
    print(f"  broker session           : {'OK' if kite else 'NONE'}")
    if not kite:
        print("    -> run the morning login first: "
              "python3 -m atlas.execution.zerodha_morning")
        ok = False
    else:
        ltp = get_ltp(symbol, kite=kite)
        print(f"  {symbol} LTP            : {ltp}")
        ok &= ltp > 0
        try:
            m = kite.margins()
            avail = m.get("equity", {}).get("available", {}).get("live_balance")
            print(f"  available balance        : {avail}")
        except Exception as e:
            print(f"  available balance        : unreadable ({e})")

    print(f"\n  LIVE_TRADING_ENABLED     : {LIVE_TRADING_ENABLED}")
    print(f"  ENABLE_EXIT_MANAGEMENT   : {ENABLE_EXIT_MANAGEMENT}")
    print(f"  PRIMARY_STOP             : {X.PRIMARY_STOP} "
          f"-> {X.ORDER_SPECS[X.PRIMARY_STOP]['order_type']}")
    print(f"  FALLBACK_EXIT            : {X.FALLBACK_EXIT} "
          f"-> {X.ORDER_SPECS[X.FALLBACK_EXIT]['order_type']}")
    try:
        X.assert_independent()
        print("  independence contract    : PASS")
    except AssertionError as e:
        print(f"  independence contract    : FAIL — {e}")
        ok = False

    print(f"\n  {'READY' if ok else 'NOT READY'}")
    return 0 if ok else 1


def _show_order(kite, oid):
    """Read the order back FROM THE BROKER. The order id is not the evidence --
    the broker's own status is."""
    try:
        for o in kite.orders():
            if str(o.get("order_id")) == str(oid):
                keep = ("order_id", "status", "status_message", "order_type",
                        "transaction_type", "product", "quantity",
                        "trigger_price", "price", "tradingsymbol", "validity",
                        "market_protection", "tag")
                return {k: o.get(k) for k in keep if k in o}
    except Exception as e:
        return {"error": f"orders() failed: {e}"}
    return {"error": "order id not in the order book"}


def phase1(symbol):
    """The SL shape, far from the market, then cancelled. No position."""
    from atlas.execution.broker import get_kite, get_ltp
    from atlas.execution import exits as X

    kite = get_kite() or fail("no broker session")
    ltp = get_ltp(symbol, kite=kite) or fail("no LTP")

    # A SHORT's stop is a BUY above the market. FAR above, so it rests.
    trig = X.round_tick(ltp * (1 + FAR_PCT))
    lim = X.stop_limit_price(trig, "SHORT")
    print(f"PHASE 1 — the SL (stop-loss LIMIT) shape\n")
    print(f"  {symbol} LTP {ltp}  ->  trigger {trig}  limit {lim}  "
          f"({FAR_PCT*100:.0f}% away; it cannot fill)")

    params = dict(variety="regular", exchange="NSE", tradingsymbol=symbol,
                  transaction_type="BUY", quantity=1, product="MIS",
                  order_type=X.ORDER_SPECS[X.PRIMARY_STOP]["order_type"],
                  trigger_price=trig, price=lim, validity="DAY",
                  tag="ATLAS_LIVETEST")
    print(f"  sending: {json.dumps(params, default=str, sort_keys=True)}")
    try:
        oid = kite.place_order(**params)
    except Exception as e:
        print(f"\n  REJECTED by the real API: {type(e).__name__}: {e}")
        print("  -> THIS IS THE ANSWER THE STUB COULD NOT GIVE. The order shape "
              "is wrong.\n     Do not resume ATLAS. Report this text.")
        return 1

    print(f"\n  ACCEPTED — order_id {oid}")
    back = _show_order(kite, oid)
    print(f"  broker says: {json.dumps(back, default=str, sort_keys=True)}")

    try:
        kite.cancel_order(variety="regular", order_id=oid)
        print(f"  cancelled {oid}")
    except Exception as e:
        print(f"\n  !! COULD NOT CANCEL {oid}: {e}")
        print("     CANCEL IT BY HAND IN KITE BEFORE DOING ANYTHING ELSE.")
        return 1

    st = str(back.get("status", "")).upper()
    good = st in ("TRIGGER PENDING", "OPEN", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED")
    print(f"\n  {'PASS' if good else 'CHECK'} — status {st!r}")
    if not good:
        print("     Expected TRIGGER PENDING. Anything else, read status_message.")
    return 0 if good else 1


def phase2(symbol):
    """The fallback LIMIT shape, priced so it rests, then cancelled."""
    from atlas.execution.broker import get_kite, get_ltp
    from atlas.execution import exits as X

    kite = get_kite() or fail("no broker session")
    ltp = get_ltp(symbol, kite=kite) or fail("no LTP")

    # The real fallback buys ABOVE the market to be marketable. Here we buy far
    # BELOW so the identical order type rests instead of filling.
    price = X.round_tick(ltp * (1 - FAR_PCT))
    print(f"PHASE 2 — the fallback LIMIT shape\n")
    print(f"  the live fallback would price a SHORT exit at "
          f"{X.exit_limit_price(ltp, 'SHORT')} (through the ask, so it fills)")
    print(f"  this test prices it at {price} instead ({FAR_PCT*100:.0f}% below), "
          f"so the SAME order type rests and can be cancelled")

    params = dict(variety="regular", exchange="NSE", tradingsymbol=symbol,
                  transaction_type="BUY", quantity=1, product="MIS",
                  order_type=X.ORDER_SPECS[X.FALLBACK_EXIT]["order_type"],
                  price=price, validity="DAY", tag="ATLAS_LIVETEST")
    print(f"  sending: {json.dumps(params, default=str, sort_keys=True)}")
    try:
        oid = kite.place_order(**params)
    except Exception as e:
        print(f"\n  REJECTED by the real API: {type(e).__name__}: {e}")
        print("  -> The FALLBACK shape is wrong. Do not resume ATLAS.")
        return 1

    print(f"\n  ACCEPTED — order_id {oid}")
    back = _show_order(kite, oid)
    print(f"  broker says: {json.dumps(back, default=str, sort_keys=True)}")
    try:
        kite.cancel_order(variety="regular", order_id=oid)
        print(f"  cancelled {oid}")
    except Exception as e:
        print(f"\n  !! COULD NOT CANCEL {oid}: {e}")
        print("     CANCEL IT BY HAND IN KITE BEFORE DOING ANYTHING ELSE.")
        return 1

    st = str(back.get("status", "")).upper()
    good = st in ("OPEN", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED")
    print(f"\n  {'PASS' if good else 'CHECK'} — status {st!r}")
    return 0 if good else 1


def phase3(symbol):
    """The whole chain on ONE SHARE. This transacts."""
    from atlas.execution.broker import get_kite, get_ltp
    from atlas.execution import exits as X

    kite = get_kite() or fail("no broker session")
    ltp = get_ltp(symbol, kite=kite) or fail("no LTP")

    print(f"PHASE 3 — the real chain, ONE SHARE of {symbol} at ~{ltp}\n")
    print("  This opens a real MIS short, places the real stop, reads it back")
    print("  from the broker, and closes with the real fallback. It costs")
    print("  brokerage plus the spread.\n")

    # 1 — enter. Straight to kite, with market protection, because this is not
    # an ATLAS entry and must not consume a slot or a ledger row.
    from atlas.execution.broker import MARKET_PROTECTION_PCT
    entry = dict(variety="regular", exchange="NSE", tradingsymbol=symbol,
                 transaction_type="SELL", quantity=1, product="MIS",
                 order_type="MARKET", market_protection=MARKET_PROTECTION_PCT,
                 validity="DAY", tag="ATLAS_LIVETEST")
    print(f"  [1/4] entering: {json.dumps(entry, default=str, sort_keys=True)}")
    try:
        eid = kite.place_order(**entry)
    except Exception as e:
        print(f"\n  REJECTED: {type(e).__name__}: {e}")
        print("  -> the MARKET entry shape is wrong (market_protection?). "
              "Nothing is open.")
        return 1
    print(f"        order_id {eid}")
    back = _show_order(kite, eid)
    print(f"        broker says: {json.dumps(back, default=str, sort_keys=True)}")
    fill = float(back.get("average_price") or back.get("price") or ltp)

    # 2 — the real stop, through the real exits.protect().
    stop = X.round_tick(fill * 1.02)       # 2% against a short
    print(f"\n  [2/4] exits.protect() with stop {stop} "
          f"(2% above a fill of {fill})")
    prot = X.protect(symbol=symbol, direction="SHORT", qty=1, product="MIS",
                     fill_price=fill, stop_price=stop, kite=kite)
    print(f"        -> {json.dumps(prot, default=str, sort_keys=True)}")

    # stop_order_id, which is what _protect_mis_sl writes -- not "stop",
    # which is the stop PRICE. Reading the wrong key here would leave a live
    # stop resting against a position this test has just closed.
    sl_id = (prot.get("legs") or {}).get("stop_order_id")
    if prot.get("ok") and sl_id:
        print(f"\n  [3/4] reading the stop back from the broker:")
        print(f"        {json.dumps(_show_order(kite, sl_id), default=str, sort_keys=True)}")
    else:
        print(f"\n  [3/4] NO STOP RESTING — protect() failed: {prot.get('reason')}")
        print("        This is the 2026-10-07 failure. The fallback runs next;")
        print("        if it also fails, CLOSE THE POSITION BY HAND IMMEDIATELY.")

    # 3 — close with the real fallback, whatever happened above.
    print(f"\n  [4/4] closing with exits.emergency_exit() — the real fallback")
    ex = X.emergency_exit(symbol, "SHORT", 1, "MIS",
                          why="live one-share exit test", kite=kite)
    print(f"        -> {json.dumps(ex, default=str, sort_keys=True)}")
    if ex.get("order_id"):
        print(f"        broker says: "
              f"{json.dumps(_show_order(kite, ex['order_id']), default=str, sort_keys=True)}")

    # 4 — the stop must not be left resting against a closed position.
    if sl_id:
        try:
            kite.cancel_order(variety="regular", order_id=sl_id)
            print(f"\n        cancelled the resting stop {sl_id}")
        except Exception as e:
            print(f"\n  !! COULD NOT CANCEL THE STOP {sl_id}: {e}")
            print("     A resting stop against a closed position can OPEN a new "
                  "one.\n     CANCEL IT BY HAND IN KITE NOW.")

    try:
        pos = [p for p in kite.positions().get("net", [])
               if p.get("tradingsymbol") == symbol and int(p.get("quantity") or 0)]
        print(f"\n  positions in {symbol} after the test: "
              f"{[{'qty': p['quantity'], 'product': p.get('product')} for p in pos]}")
        flat = not pos
    except Exception as e:
        print(f"\n  could not read positions ({e}) — CHECK KITE BY HAND")
        flat = False

    good = bool(prot.get("ok")) and bool(ex.get("ok")) and flat
    print(f"\n  {'PASS' if good else 'CHECK'} — stop placed: {bool(prot.get('ok'))}, "
          f"fallback exit: {bool(ex.get('ok'))}, flat: {flat}")
    return 0 if good else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--phase", type=int, choices=(1, 2, 3))
    ap.add_argument("--live", action="store_true",
                    help="required: this places REAL orders on a REAL account")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    a = ap.parse_args()

    if a.preflight:
        return preflight(a.symbol)
    if not a.phase:
        print(__doc__)
        return 0
    if not a.live:
        print("Refusing: --phase places REAL orders. Add --live if that is "
              "what you mean.")
        return 2

    rc = preflight(a.symbol)
    if rc:
        print("\nPreflight did not pass. Not placing anything.")
        return rc
    print(f"\n{'='*70}\n")
    return {1: phase1, 2: phase2, 3: phase3}[a.phase](a.symbol)


if __name__ == "__main__":
    sys.exit(main())
