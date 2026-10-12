#!/usr/bin/env python3
"""
ATLAS — the LIVE one-share OCO GTT check.

    python3 -m tools.live_gtt_check --preflight
    python3 -m tools.live_gtt_check --run --live

THE ONLY THING THAT ANSWERS THE QUESTION. tools/verify_exit_path.py runs
against a stub that enforces the documented rules, and a stub cannot tell you
what Zerodha actually does. The 2026-10-07 incident happened because the exit
path "had never called the real Zerodha API", and no amount of stub testing
fixes that.

WHAT IT DOES, in order, on ONE share:

    1. buy 1 share CNC with a LIMIT at the live price
    2. place the real two-leg OCO GTT through exits.protect()
    3. READ IT BACK from Zerodha and check both legs and both trigger prices
    4. delete the GTT
    5. sell the 1 share to go flat

Cost: brokerage plus the spread. On a Rs 1,500 share that is a few rupees.

A TOOL MUST NEVER PRINT READY WHEN ONE OF ITS OWN CHECKS FAILED ITS STATED
EXPECTATION. The previous preflight printed the halt state with "(expected
True)" next to it and did not fold that into the verdict, so it could print
READY while the one condition it had just declared necessary was false. Every
stated expectation here goes through expect(), and the verdict is the AND of
all of them. There is no way to print READY with a failed check.

THE ONE THING THIS CHECK CANNOT SETTLE, and it must be read before relying on
the result. Zerodha's GTT terms say: "At all times while placing GTT, You are
required to maintain sufficient cash balance & sufficient quantity of holdings
of the respective scrip", and the sell-GTT rejection article says "If you do
not have enough holdings of the specific stock in your demat account, the
exchange will reject your GTT order." Shares bought today are not in demat
until T+1. So a stop leg that TRIGGERS on the entry day may be rejected for
insufficient holdings even though the GTT was accepted. This check proves
acceptance and read-back; it does not and cannot prove same-day trigger
execution. See docs/ATLAS_LONG_ONLY.md.
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

# A liquid, mid-priced name. One share is a meaningful notional and the spread
# is a paisa or two. Override with --symbol.
DEFAULT_SYMBOL = "RELIANCE"

# How far the legs sit from the market. FAR, so neither can trigger during the
# check -- the point is to prove the instruction is accepted and reads back
# correctly, not to have it fire.
STOP_BELOW_PCT = 0.20
TARGET_ABOVE_PCT = 0.20

_RESULTS: list = []


def expect(label: str, ok: bool, detail: str = "") -> bool:
    """Record a STATED EXPECTATION. The verdict is the AND of all of these.

    Nothing in this file prints a verdict from anything else. That is the fix
    for a preflight that said READY with a failed check sitting above it.
    """
    _RESULTS.append((label, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"\n         {detail}" if detail else ""))
    return bool(ok)


def note(label: str, value) -> None:
    """Information, NOT an expectation. Printed, never scored.

    Separated from expect() on purpose: the old preflight blurred the two and
    that is exactly how a failed condition ended up next to a READY verdict.
    """
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

    from atlas.config import (halt_file_state, LIVE_TRADING_ENABLED,
                              ENABLE_EXIT_MANAGEMENT, ALLOW_SHORT_ENTRIES,
                              ALLOW_AUTOMATED_TARGET, ENTRY_ORDER_TYPE,
                              STRICT_BULL_AND_BULLISH)
    from atlas.execution.broker import get_kite, get_ltp
    from atlas.execution import exits as X

    now = datetime.now(IST)
    expect("the market is open", (9, 15) <= (now.hour, now.minute) <= (15, 30),
           f"{now:%H:%M} IST")

    st = halt_file_state()
    note("halt file", st["path"])
    expect("the halt file is the canonical path", st["is_canonical"],
           f"{st['path']} vs {st['canonical']}")
    # EXPECTED HALTED, AND SCORED. ATLAS stays halted until this check passes,
    # so a NOT-halted engine means someone resumed it early -- which is a
    # finding, not a footnote. This is the exact check the old preflight
    # printed and did not score.
    expect("ATLAS is HALTED (it stays halted until this check passes)",
           st["halted"], st["reason"][:90] or "no halt file present")

    expect("shorts are disabled", ALLOW_SHORT_ENTRIES is False,
           f"ALLOW_SHORT_ENTRIES={ALLOW_SHORT_ENTRIES}")
    expect("the entry order type is LIMIT", ENTRY_ORDER_TYPE == "LIMIT",
           ENTRY_ORDER_TYPE)
    expect("the target leg is enabled (an OCO needs both legs)",
           ALLOW_AUTOMATED_TARGET is True, f"{ALLOW_AUTOMATED_TARGET}")
    expect("exit management is on", ENABLE_EXIT_MANAGEMENT is True,
           f"{ENABLE_EXIT_MANAGEMENT}")
    note("LIVE_TRADING_ENABLED", LIVE_TRADING_ENABLED)
    note("STRICT_BULL_AND_BULLISH", STRICT_BULL_AND_BULLISH)

    note("PRIMARY_STOP", f"{X.PRIMARY_STOP} -> "
                         f"{X.ORDER_SPECS[X.PRIMARY_STOP]}")
    note("FALLBACK_EXIT", f"{X.FALLBACK_EXIT} -> "
                          f"{X.ORDER_SPECS[X.FALLBACK_EXIT]}")
    try:
        X.assert_independent()
        expect("the primary and fallback are independent", True,
               f"{X.PRIMARY_STOP} on the "
               f"{X.ORDER_SPECS[X.PRIMARY_STOP]['api']} API, "
               f"{X.FALLBACK_EXIT} on the "
               f"{X.ORDER_SPECS[X.FALLBACK_EXIT]['api']} API")
    except AssertionError as e:
        expect("the primary and fallback are independent", False, str(e))

    kite = get_kite()
    expect("a broker session exists", kite is not None,
           "run atlas/execution/zerodha_morning.py if not")
    if kite:
        ltp = get_ltp(symbol, kite=kite)
        expect(f"{symbol} has a live price", bool(ltp and ltp > 0), str(ltp))
        try:
            gtts = kite.get_gtts() or []
            active = [g for g in gtts
                      if str(g.get("status", "")).lower() == "active"]
            note("active GTTs on the account", f"{len(active)} of 500")
            # THE CAP IS REAL: Zerodha's GTT terms allow a maximum of 500
            # pending GTTs per client. Headroom is an expectation, not trivia.
            expect("there is headroom under the 500-GTT cap", len(active) < 500,
                   f"{len(active)} active")
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

    return verdict("PREFLIGHT")


def _show_order(kite, oid) -> dict:
    try:
        for o in kite.orders():
            if str(o.get("order_id")) == str(oid):
                keep = ("order_id", "status", "status_message", "order_type",
                        "transaction_type", "product", "quantity",
                        "filled_quantity", "average_price", "price", "tag")
                return {k: o.get(k) for k in keep if k in o}
    except Exception as e:
        return {"error": f"orders() failed: {e}"}
    return {"error": "order id not in the order book"}


def run_check(symbol: str) -> int:
    from atlas.execution.broker import get_kite, get_ltp
    from atlas.execution import exits as X

    kite = get_kite()
    if not kite:
        print("  no broker session — run zerodha_morning first")
        return 1
    ltp = get_ltp(symbol, kite=kite)
    if not ltp:
        print(f"  no live price for {symbol}")
        return 1

    print(f"\nLIVE OCO GTT CHECK — 1 share of {symbol} at ~{ltp}\n")
    gid = None
    entry_id = None

    # ── 1. buy one share, CNC, LIMIT at the live price ────────────
    entry = dict(variety="regular", exchange="NSE", tradingsymbol=symbol,
                 transaction_type="BUY", quantity=1, product="CNC",
                 order_type="LIMIT", price=round(float(ltp), 2),
                 validity="DAY", tag="ATLAS_GTTCHK")
    print(f"  [1/5] entering: {json.dumps(entry, default=str, sort_keys=True)}")
    try:
        entry_id = kite.place_order(**entry)
    except Exception as e:
        expect("the CNC LIMIT entry is accepted", False,
               f"{type(e).__name__}: {e}")
        return verdict("LIVE OCO GTT CHECK")
    expect("the CNC LIMIT entry is accepted", True, f"order_id {entry_id}")

    # A LIMIT may not fill. Wait briefly, then report what happened.
    filled, avg = 0, 0.0
    for _ in range(20):
        time.sleep(1.0)
        back = _show_order(kite, entry_id)
        try:
            filled = int(float(back.get("filled_quantity") or 0))
            avg = float(back.get("average_price") or 0)
        except (TypeError, ValueError):
            pass
        if filled >= 1:
            break
    print(f"        broker says: {json.dumps(_show_order(kite, entry_id), default=str, sort_keys=True)}")
    if filled < 1:
        expect("the entry LIMIT filled", False,
               "it did not fill in 20s. Cancel it by hand if it is still "
               "resting, then re-run closer to the touch.")
        try:
            kite.cancel_order(variety="regular", order_id=entry_id)
            print("        cancelled the unfilled entry")
        except Exception as e:
            print(f"        !! COULD NOT CANCEL {entry_id}: {e} — DO IT BY HAND")
        return verdict("LIVE OCO GTT CHECK")
    expect("the entry LIMIT filled", True, f"{filled} @ {avg}")

    fill = avg or float(ltp)
    stop = round(fill * (1 - STOP_BELOW_PCT), 1)
    print(f"\n  [2/5] exits.protect() — stop {stop} "
          f"({STOP_BELOW_PCT*100:.0f}% below), target 2R above")
    prot = {}
    try:
        prot = X.protect(symbol=symbol, direction="LONG", qty=1, product="CNC",
                         fill_price=fill, stop_price=stop, kite=kite)
    except Exception as e:
        expect("the OCO GTT is accepted by Zerodha", False,
               f"{type(e).__name__}: {e}")
    print(f"        -> {json.dumps(prot, default=str, sort_keys=True)}")
    gid = (prot.get("legs") or {}).get("oco_trigger_id")
    expect("the OCO GTT is accepted by Zerodha", bool(prot.get("ok")) and bool(gid),
           str(prot.get("reason"))[:140])
    expect("the mechanism is GTT_OCO", prot.get("mechanism") == "GTT_OCO",
           str(prot.get("mechanism")))

    # ── 3. READ IT BACK. The order id is not the evidence. ────────
    if gid:
        print(f"\n  [3/5] reading GTT {gid} back from Zerodha")
        got = None
        try:
            for g in (kite.get_gtts() or []):
                if str(g.get("id")) == str(gid):
                    got = g
                    break
        except Exception as e:
            expect("the GTT reads back from the broker", False,
                   f"get_gtts() failed: {type(e).__name__}: {e}")
        if got is None:
            try:
                got = kite.get_gtt(trigger_id=gid)
            except Exception:
                got = None
        expect("the GTT reads back from the broker", got is not None,
               "a trigger_id with no readable GTT behind it is not protection")
        if got:
            print(f"        {json.dumps(got, default=str, sort_keys=True)[:1100]}")
            cond = got.get("condition") or {}
            orders = got.get("orders") or []
            tv = list(cond.get("trigger_values") or [])
            expect("it is a two-leg GTT",
                   str(got.get("type", "")).lower() == "two-leg",
                   str(got.get("type")))
            expect("it is active",
                   str(got.get("status", "")).lower() == "active",
                   str(got.get("status")))
            expect("BOTH legs exist at the broker", len(orders) == 2,
                   f"{len(orders)} leg(s)")
            expect("there are two trigger values", len(tv) == 2, f"{tv}")
            if len(tv) == 2:
                expect("the trigger values ascend (stop below target)",
                       float(tv[0]) < float(tv[1]), f"{tv}")
                # THE PRICES MUST BE THE ONES WE ASKED FOR. A GTT accepted with
                # the legs transposed is armed inside out and nothing on the
                # screen would say so.
                want_stop = prot.get("stop_trigger") or prot.get("stop")
                expect("the stop trigger is the one we sent",
                       abs(float(tv[0]) - float(want_stop)) < 0.6,
                       f"broker {tv[0]} vs sent {want_stop}")
                expect("the target trigger is the one we sent",
                       abs(float(tv[1]) - float(prot.get("target"))) < 0.6,
                       f"broker {tv[1]} vs sent {prot.get('target')}")
            if len(orders) == 2:
                expect("both legs are SELL",
                       all(str(o.get("transaction_type", "")).upper() == "SELL"
                           for o in orders),
                       str([o.get("transaction_type") for o in orders]))
                expect("both legs are CNC",
                       all(str(o.get("product", "")).upper() == "CNC"
                           for o in orders),
                       str([o.get("product") for o in orders]))
                expect("both legs are for 1 share",
                       all(int(o.get("quantity") or 0) == 1 for o in orders),
                       str([o.get("quantity") for o in orders]))

        # ── 4. delete the GTT ─────────────────────────────────────
        print(f"\n  [4/5] deleting GTT {gid}")
        try:
            kite.delete_gtt(trigger_id=gid)
            expect("the GTT is deleted", True, f"trigger {gid}")
        except Exception as e:
            expect("the GTT is deleted", False,
                   f"{type(e).__name__}: {e} — DELETE IT BY HAND IN KITE NOW. "
                   f"A resting sell GTT on a position you have closed will "
                   f"SELL SHARES YOU DO NOT OWN when it triggers.")

    # ── 5. go flat ───────────────────────────────────────────────
    print(f"\n  [5/5] selling the 1 share to go flat")
    try:
        exit_px = X.exit_limit_price(float(ltp), "LONG")
        sid = kite.place_order(variety="regular", exchange="NSE",
                               tradingsymbol=symbol, transaction_type="SELL",
                               quantity=1, product="CNC", order_type="LIMIT",
                               price=exit_px, validity="DAY",
                               tag="ATLAS_GTTCHK")
        print(f"        sell order {sid} at {exit_px}")
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
        left = [g for g in (kite.get_gtts() or [])
                if str(g.get("status", "")).lower() == "active"
                and (g.get("condition") or {}).get("tradingsymbol") == symbol]
        expect("no GTT is left resting on this symbol", not left,
               f"{len(left)} active — a resting sell GTT with no position "
               f"behind it will short the stock when it triggers")
    except Exception as e:
        expect("no GTT is left resting on this symbol", False, str(e))

    return verdict("LIVE OCO GTT CHECK")


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
        print("Refusing: --run places REAL orders on a REAL account and buys a "
              "share. Add --live if that is what you mean.")
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
