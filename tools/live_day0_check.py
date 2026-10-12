#!/usr/bin/env python3
"""
ATLAS — the LIVE one-share day-0/day-1 check.

    python3 -m tools.live_day0_check --preflight
    python3 -m tools.live_day0_check --run --live

THE ONLY THING THAT SETTLES THE BROKER RULES. Every other harness runs against
a stub that enforces what the documentation says, and a stub cannot tell you
what Zerodha does. The 2026-10-07 incident happened because the exit path "had
never called the real Zerodha API".

THE ONE CLAIM THE WHOLE DAY-0 DESIGN RESTS ON, and the reason step 1 of this
check comes first: a SELL SL order is accepted against a CNC position bought
the same session. The documentation says it is -- "if your SL is triggered the
same day, your trade will be considered intraday" -- and this is where that
stops being a quotation.

IF STEP 2 IS REFUSED, STOP. Do not work around it. The day-0 mechanism has no
substitute: a GTT cannot protect a same-day buy, because it checks demat and
the shares are not there until T+1. Report the exact rejection text.

WHAT IT DOES, on ONE share:

    1. buy 1 share CNC with a LIMIT at the live price
    2. place the day-0 SL SELL and READ IT BACK — the claim above
    3. place both overnight GTTs and READ THEM BACK, checking triggers
    4. cancel the SL, delete both GTTs
    5. sell the share, confirm flat with nothing left resting

Cost: brokerage plus the spread. A few rupees.

A TOOL MUST NEVER PRINT READY WHEN ONE OF ITS OWN CHECKS FAILED ITS STATED
EXPECTATION. Every stated expectation goes through expect() and the verdict is
the AND of all of them.
"""

from __future__ import annotations

import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_SYMBOL = "RELIANCE"

# FAR ENOUGH THAT NOTHING CAN TRIGGER during the check. 20% away on a large cap
# inside one session is not a price that happens.
STOP_BELOW_PCT = 0.20

_RESULTS: list = []


def expect(label: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append((label, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"\n         {detail}" if detail else ""))
    return bool(ok)


def note(label: str, value) -> None:
    """Information, never scored. Kept separate from expect() on purpose."""
    print(f"  [info] {label}: {value}")


def verdict(title: str) -> int:
    failed = [l for l, ok, _ in _RESULTS if not ok]
    print(f"\n  {len(_RESULTS) - len(failed)} passed, {len(failed)} failed")
    for l in failed:
        print(f"    FAILED: {l}")
    ok = not failed
    print(f"\n  {title}: {'READY' if ok else 'NOT READY'}")
    if not ok:
        print("  At least one stated expectation failed, so this is NOT ready "
              "whatever the rest said.")
    return 0 if ok else 1


def preflight(symbol: str) -> int:
    print(f"PREFLIGHT  {datetime.now(IST):%Y-%m-%d %H:%M:%S} IST\n")
    from atlas.config import (halt_file_state, ALLOW_SHORT_ENTRIES,
                              ENABLE_EXIT_MANAGEMENT, ENTRY_ORDER_TYPE,
                              ALLOW_AUTOMATED_TARGET, PRECLOSE_GTT_TIME,
                              TRAIL_BREAKEVEN_AT_PCT, GTT_SPLIT_OCO_FRACTION)
    from atlas.execution.broker import get_kite, get_ltp
    from atlas.execution import exits as X

    now = datetime.now(IST)
    expect("the market is open", (9, 15) <= (now.hour, now.minute) <= (15, 30),
           f"{now:%H:%M} IST")
    # BEFORE THE PRE-CLOSE TIME. After 15:20 the handover job would be racing
    # this check for the same position.
    cut = tuple(int(x) for x in PRECLOSE_GTT_TIME.split(":"))
    expect(f"it is before the pre-close handover at {PRECLOSE_GTT_TIME}",
           (now.hour, now.minute) < cut, f"{now:%H:%M}")

    st = halt_file_state()
    note("halt file", st["path"])
    expect("the halt file is the canonical path", st["is_canonical"],
           f"{st['path']} vs {st['canonical']}")
    expect("ATLAS is HALTED (it stays halted until this check passes)",
           st["halted"], st["reason"][:90] or "no halt file present")

    expect("shorts are disabled", ALLOW_SHORT_ENTRIES is False,
           f"{ALLOW_SHORT_ENTRIES}")
    expect("the entry order type is LIMIT", ENTRY_ORDER_TYPE == "LIMIT",
           ENTRY_ORDER_TYPE)
    expect("exit management is on", ENABLE_EXIT_MANAGEMENT is True, "")
    expect("the target leg is enabled", ALLOW_AUTOMATED_TARGET is True, "")
    note("split", f"OCO {GTT_SPLIT_OCO_FRACTION:.0%} / trailed the rest")
    note("breakeven trigger", f"+{TRAIL_BREAKEVEN_AT_PCT:.0%} from entry")

    for prim in (X.PRIMARY_STOP_DAY0, X.PRIMARY_STOP_OVERNIGHT):
        try:
            X.assert_independent(prim)
            expect(f"{prim} + {X.FALLBACK_EXIT} are independent", True,
                   f"{X.ORDER_SPECS[prim]['api']} vs "
                   f"{X.ORDER_SPECS[X.FALLBACK_EXIT]['api']} API")
        except AssertionError as e:
            expect(f"{prim} + {X.FALLBACK_EXIT} are independent", False, str(e))

    kite = get_kite()
    expect("a broker session exists", kite is not None,
           "run atlas/execution/zerodha_morning.py if not")
    ltp = None
    if kite:
        ltp = get_ltp(symbol, kite=kite)
        expect(f"{symbol} has a live price", bool(ltp and ltp > 0), str(ltp))
        try:
            gtts = kite.get_gtts() or []
            active = [g for g in gtts
                      if str(g.get("status", "")).lower() == "active"]
            note("active GTTs on the account", f"{len(active)} of 500")
            # TWO are needed, not one: the split places an OCO and a
            # stop-only GTT per position.
            expect("there is headroom for TWO more GTTs under the 500 cap",
                   len(active) <= 498, f"{len(active)} active")
        except Exception as e:
            expect("the GTT list is readable", False, f"{type(e).__name__}: {e}")
        try:
            m = kite.margins()
            avail = (m.get("equity", {}).get("available", {})
                     .get("live_balance"))
            note("available balance", avail)
            expect("the balance covers one share",
                   avail is not None and ltp and float(avail) > float(ltp) * 1.1,
                   f"{avail} vs ~{ltp}")
        except Exception as e:
            expect("margins are readable", False, f"{type(e).__name__}: {e}")

    print("""
  ONE PRECONDITION THIS CHECK CANNOT TEST AND YOU MUST CONFIRM. A sell GTT
  needs DDPI (or POA) on the account, or a CDSL TPIN authorisation that is
  valid for ONE DAY and must be redone every trading day after 7 AM:

    "You must authorise sell GTT orders on equity holdings using the CDSL
     TPIN, unless you have submitted a POA or DDPI."
    "If you have a non-POA/DDPI account, you must pre-authorise your GTT sell
     order by entering the CDSL TPIN before the GTT is triggered to avoid
     rejection."

  WITHOUT DDPI THE OVERNIGHT GTTs REST AND ARE REJECTED AT TRIGGER, silently.
  Confirm DDPI is active on this account before relying on any of this.
""")
    return verdict("PREFLIGHT")


def _show_order(kite, oid) -> dict:
    try:
        for o in kite.orders():
            if str(o.get("order_id")) == str(oid):
                keep = ("order_id", "status", "status_message", "order_type",
                        "transaction_type", "product", "quantity",
                        "filled_quantity", "average_price", "price",
                        "trigger_price", "validity", "tag")
                return {k: o.get(k) for k in keep if k in o}
    except Exception as e:
        return {"error": f"orders() failed: {e}"}
    return {"error": "order id not in the order book"}


def run_check(symbol: str) -> int:
    from atlas.execution.broker import get_kite, get_ltp
    from atlas.execution import exits as X

    kite = get_kite()
    ltp = get_ltp(symbol, kite=kite) if kite else None
    if not (kite and ltp):
        print("  no broker session or no price — nothing attempted")
        return 1

    print(f"\nLIVE DAY-0 / DAY-1 CHECK — 1 share of {symbol} at ~{ltp}\n")
    sl_id = None
    gtt_ids = []

    # ── 1. buy one share ─────────────────────────────────────────
    entry = dict(variety="regular", exchange="NSE", tradingsymbol=symbol,
                 transaction_type="BUY", quantity=1, product="CNC",
                 order_type="LIMIT", price=round(float(ltp), 2),
                 validity="DAY", tag="ATLAS_D0CHK")
    print(f"  [1/5] entering: {json.dumps(entry, default=str, sort_keys=True)}")
    try:
        eid = kite.place_order(**entry)
    except Exception as e:
        expect("the CNC LIMIT entry is accepted", False,
               f"{type(e).__name__}: {e}")
        return verdict("LIVE DAY-0 / DAY-1 CHECK")
    expect("the CNC LIMIT entry is accepted", True, f"order_id {eid}")

    filled, avg = 0, 0.0
    for _ in range(20):
        time.sleep(1.0)
        back = _show_order(kite, eid)
        try:
            filled = int(float(back.get("filled_quantity") or 0))
            avg = float(back.get("average_price") or 0)
        except (TypeError, ValueError):
            pass
        if filled >= 1:
            break
    print(f"        broker says: "
          f"{json.dumps(_show_order(kite, eid), default=str, sort_keys=True)}")
    if filled < 1:
        expect("the entry LIMIT filled", False,
               "it did not fill in 20s — cancel it by hand if still resting "
               "and re-run closer to the touch")
        try:
            kite.cancel_order(variety="regular", order_id=eid)
            print("        cancelled the unfilled entry")
        except Exception as e:
            print(f"        !! COULD NOT CANCEL {eid}: {e} — DO IT BY HAND")
        return verdict("LIVE DAY-0 / DAY-1 CHECK")
    expect("the entry LIMIT filled", True, f"{filled} @ {avg}")

    fill = avg or float(ltp)
    stop = round(fill * (1 - STOP_BELOW_PCT), 1)

    # ── 2. THE CLAIM: a SELL SL on a same-day CNC buy ────────────
    print(f"\n  [2/5] THE CLAIM THE DESIGN RESTS ON — day-0 SL SELL on a "
          f"position bought\n        seconds ago, with ZERO in demat. "
          f"stop {stop}")
    d0 = {}
    try:
        d0 = X.protect_day0(symbol=symbol, direction="LONG", qty=1,
                            fill_price=fill, stop_price=stop, kite=kite)
    except Exception as e:
        expect("a SELL SL is ACCEPTED on a same-day CNC buy", False,
               f"{type(e).__name__}: {e}")
    print(f"        -> {json.dumps(d0, default=str, sort_keys=True)}")
    sl_id = (d0.get("legs") or {}).get("day0_sl_order_id")
    ok2 = expect("a SELL SL is ACCEPTED on a same-day CNC buy",
                 bool(d0.get("ok")) and bool(sl_id),
                 str(d0.get("reason"))[:160])
    if not ok2:
        print("""
  ■■ STOP HERE. The day-0 design rests on this and it was refused.
     Do NOT work around it. Report the rejection text above verbatim --
     a GTT cannot substitute, because it checks demat and the shares are
     not there until T+1.
""")

    if sl_id:
        back = _show_order(kite, sl_id)
        print(f"        reading it back: "
              f"{json.dumps(back, default=str, sort_keys=True)}")
        st = str(back.get("status", "")).upper()
        expect("  the SL order RESTS at the exchange",
               st in ("TRIGGER PENDING", "OPEN", "VALIDATION PENDING",
                      "PUT ORDER REQ RECEIVED"),
               f"status {st!r} — TRIGGER PENDING is what protection looks like")
        expect("  the broker echoes it as an SL",
               str(back.get("order_type", "")).upper() == "SL",
               str(back.get("order_type")))
        expect("  it is a SELL for 1 share, CNC",
               (str(back.get("transaction_type", "")).upper() == "SELL"
                and int(back.get("quantity") or 0) == 1
                and str(back.get("product", "")).upper() == "CNC"),
               f"{back.get('transaction_type')} {back.get('quantity')} "
               f"{back.get('product')}")
        try:
            expect("  the limit sits BELOW the trigger",
                   float(back["price"]) < float(back["trigger_price"]),
                   f"limit {back['price']} < trigger {back['trigger_price']}")
        except (KeyError, TypeError, ValueError):
            expect("  the limit sits BELOW the trigger", False,
                   "the broker did not echo both prices")

    # ── 3. the two overnight GTTs ────────────────────────────────
    print(f"\n  [3/5] the overnight GTTs — OCO + stop-only, 1 share")
    g = {}
    try:
        g = X.place_overnight_gtts(symbol=symbol, direction="LONG", qty=1,
                                   fill_price=fill, stop_price=stop, kite=kite)
    except Exception as e:
        expect("both overnight GTTs are placed", False,
               f"{type(e).__name__}: {e}")
    print(f"        -> {json.dumps(g, default=str, sort_keys=True)}")
    legs = g.get("legs") or {}
    gtt_ids = [v for k, v in legs.items() if k.endswith("_trigger_id") and v]
    expect("the overnight GTTs are placed", bool(g.get("ok")),
           str(g.get("reason"))[:150])
    # AT QTY 1 THERE IS NO OCO HALF. One stop-only GTT carries it, which is
    # the stated behaviour of the split, not a failure.
    expect("at qty 1 exactly ONE GTT is placed (no OCO half)",
           len(gtt_ids) == 1,
           f"{len(gtt_ids)} — split was OCO {g.get('oco_qty')} / trailed "
           f"{g.get('trailed_qty')}; a zero-quantity OCO is never sent")

    for gid in gtt_ids:
        got = None
        try:
            for x in (kite.get_gtts() or []):
                if str(x.get("id")) == str(gid):
                    got = x
                    break
        except Exception as e:
            expect(f"GTT {gid} reads back", False, str(e))
        expect(f"GTT {gid} reads back from Zerodha", got is not None,
               "a trigger_id with no readable GTT behind it is not protection")
        if got:
            print(f"        {json.dumps(got, default=str, sort_keys=True)[:900]}")
            cond = got.get("condition") or {}
            orders = got.get("orders") or []
            tv = list(cond.get("trigger_values") or [])
            expect(f"  GTT {gid} is active",
                   str(got.get("status", "")).lower() == "active",
                   str(got.get("status")))
            expect(f"  GTT {gid} has its leg(s)", len(orders) >= 1,
                   f"{len(orders)}")
            if tv:
                want = d0.get("stop_trigger") or stop
                expect(f"  GTT {gid} stop trigger matches what was sent",
                       abs(float(tv[0]) - float(want)) < 0.6,
                       f"broker {tv[0]} vs sent {want}")
            if orders:
                expect(f"  GTT {gid} legs are SELL / CNC / 1",
                       all(str(o.get("transaction_type", "")).upper() == "SELL"
                           and str(o.get("product", "")).upper() == "CNC"
                           and int(o.get("quantity") or 0) == 1
                           for o in orders),
                       str([(o.get("transaction_type"), o.get("product"),
                             o.get("quantity")) for o in orders]))
                if tv:
                    expect(f"  GTT {gid} leg limit is BELOW its trigger",
                           float(orders[0]["price"]) < float(tv[0]),
                           f"limit {orders[0]['price']} < trigger {tv[0]}")

    # ── 4. cancel everything ─────────────────────────────────────
    print(f"\n  [4/5] cancelling the SL and deleting the GTTs")
    if sl_id:
        try:
            kite.cancel_order(variety="regular", order_id=sl_id)
            expect("the day-0 SL is cancelled", True, f"order {sl_id}")
        except Exception as e:
            expect("the day-0 SL is cancelled", False,
                   f"{type(e).__name__}: {e} — CANCEL IT BY HAND. A resting "
                   f"SL SELL on a position you have closed will SHORT the "
                   f"stock when it triggers.")
    for gid in gtt_ids:
        try:
            kite.delete_gtt(trigger_id=gid)
            expect(f"GTT {gid} is deleted", True, "")
        except Exception as e:
            expect(f"GTT {gid} is deleted", False,
                   f"{type(e).__name__}: {e} — DELETE IT BY HAND IN KITE NOW.")

    # ── 5. go flat ───────────────────────────────────────────────
    print(f"\n  [5/5] selling the 1 share")
    try:
        px = X.exit_limit_price(float(ltp), "LONG")
        sid = kite.place_order(variety="regular", exchange="NSE",
                               tradingsymbol=symbol, transaction_type="SELL",
                               quantity=1, product="CNC", order_type="LIMIT",
                               price=px, validity="DAY", tag="ATLAS_D0CHK")
        print(f"        sell {sid} at {px}")
        time.sleep(3.0)
        print(f"        broker says: "
              f"{json.dumps(_show_order(kite, sid), default=str, sort_keys=True)}")
    except Exception as e:
        expect("the position is closed", False,
               f"{type(e).__name__}: {e} — SELL THE 1 SHARE BY HAND")

    try:
        pos = [p for p in (kite.positions().get("net") or [])
               if p.get("tradingsymbol") == symbol
               and int(p.get("quantity") or 0)]
        print(f"\n  positions in {symbol}: "
              f"{[{'qty': p['quantity'], 'product': p.get('product')} for p in pos]}")
        expect("the account is FLAT in this symbol", not pos, str(pos))
    except Exception as e:
        expect("the account is FLAT in this symbol", False,
               f"could not read positions: {e} — CHECK KITE BY HAND")

    try:
        left = [g2 for g2 in (kite.get_gtts() or [])
                if str(g2.get("status", "")).lower() == "active"
                and (g2.get("condition") or {}).get("tradingsymbol") == symbol]
        expect("no GTT is left resting on this symbol", not left,
               f"{len(left)} active — a resting sell GTT with no position "
               f"behind it will short the stock when it triggers")
    except Exception as e:
        expect("no GTT is left resting on this symbol", False, str(e))

    try:
        orders_left = [o for o in (kite.orders() or [])
                       if o.get("tradingsymbol") == symbol
                       and str(o.get("status", "")).upper()
                       in ("TRIGGER PENDING", "OPEN")]
        expect("no order is left resting on this symbol", not orders_left,
               f"{len(orders_left)} open — "
               f"{[o.get('order_id') for o in orders_left]}")
    except Exception as e:
        expect("no order is left resting on this symbol", False, str(e))

    return verdict("LIVE DAY-0 / DAY-1 CHECK")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="required with --run: places REAL orders")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    a = ap.parse_args()

    if a.preflight:
        return preflight(a.symbol)
    if not a.run:
        print(__doc__)
        return 0
    if not a.live:
        print("Refusing: --run places REAL orders and buys a share. Add "
              "--live if that is what you mean.")
        return 2
    rc = preflight(a.symbol)
    if rc:
        print("\nPreflight did not pass. Placing nothing.")
        return rc
    _RESULTS.clear()
    print(f"\n{'=' * 72}")
    return run_check(a.symbol)


if __name__ == "__main__":
    sys.exit(main())
