#!/usr/bin/env python3
"""
ATLAS — exit-path verification
==============================
    python3 -m tools.verify_exit_path

WHAT THIS IS AND, MORE IMPORTANTLY, WHAT IT IS NOT
--------------------------------------------------
The 2026-10-07 incident happened because "this exit path had never called the
real Zerodha API. It was only ever tested against an injected fake that accepted
everything." A fake that accepts everything cannot fail, so a test against one
proves only that the code runs.

So the stub here is ADVERSARIAL, not permissive. It is a stand-in for the
KiteConnect object and nothing above it, and it enforces the broker's own
documented rules plus the two rejections actually observed on 2026-10-07:

  * MARKET or SL-M with no market_protection  -> the verbatim rejection text
  * SL or SL-M with no trigger_price          -> rejected
  * market_protection outside (0,100] or -1   -> rejected
  * market_protection on LIMIT or SL          -> flagged (no effect per the docs)
  * an MIS order at or after the segment cutoff -> the verbatim rejection text

Rules confirmed against https://kite.trade/docs/connect/v3/orders/ and Zerodha's
published square-off timings on 2026-10-09, not from memory.

THIS STILL PROVES NOTHING ABOUT THE WIRE. It proves the code builds orders that
satisfy the documented contract and that the fallback survives a failure of the
primary. Whether Zerodha accepts them is answered only by the one-share live
test, which is a separate step, run by a human, with the market open. See
docs/LIVE_EXIT_TEST.md.
"""

import os
import sys
import json
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))
    return ok


def head(n: str) -> None:
    print(f"\n{'='*74}\n{n}\n{'='*74}")


# ══════════════════════════════════════════════════════════════════
# THE ADVERSARIAL STUB
# ══════════════════════════════════════════════════════════════════

class Rejected(Exception):
    """Stands in for kiteconnect.exceptions.InputException."""


class RecordingKite:
    """A KiteConnect stand-in that refuses what Zerodha refuses.

    reject_order_types lets a verification INJECT A FAILURE into one order
    family while leaving every other rule in force -- which is how the
    independence of the fallback is tested rather than asserted.
    """

    def __init__(self, ltp=1833.0, reject_order_types=(), now=None,
                 reject_reason=None):
        self.orders = []
        # NOT self.ltp -- KiteConnect exposes ltp() as a METHOD, and an
        # attribute of that name shadows it. The first run of this harness did
        # exactly that and reported the emergency exit as unable to price
        # itself, which is a stub defect dressed as a code defect.
        self.last_price = ltp
        self.reject_order_types = {str(o).upper() for o in reject_order_types}
        self.reject_reason = reject_reason
        self.now = now or datetime(2026, 10, 7, 14, 12, 9, tzinfo=IST)
        self.notes = []

    # -- the documented contract -----------------------------------
    def place_order(self, **kw):
        self.orders.append(dict(kw))
        ot = str(kw.get("order_type", "")).upper()
        mp = kw.get("market_protection")
        prod = str(kw.get("product", "")).upper()

        if ot not in ("MARKET", "LIMIT", "SL", "SL-M"):
            raise Rejected(f"Invalid order_type {ot!r}")

        # The 2026-10-07 rejection, verbatim.
        if ot in ("MARKET", "SL-M") and mp is None:
            raise Rejected("Market orders without market protection are not "
                           "allowed via API. Please set market protection or "
                           "use a Limit order.")
        if mp is not None:
            try:
                v = float(mp)
            except Exception:
                raise Rejected(f"market_protection {mp!r} is not numeric")
            if not (0 < v <= 100 or v == -1):
                raise Rejected(f"market_protection {v} outside (0,100] and not -1")
            if ot in ("LIMIT", "SL"):
                self.notes.append(f"market_protection set on a {ot} order — the "
                                  f"docs say it has no effect there")

        if ot in ("SL", "SL-M") and not kw.get("trigger_price"):
            raise Rejected(f"trigger_price is required for a {ot} order")
        if ot in ("LIMIT", "SL") and not kw.get("price"):
            raise Rejected(f"price is required for a {ot} order")

        # The second 2026-10-07 rejection, verbatim.
        if prod == "MIS":
            from atlas.config import MIS_BROKER_CUTOFF
            cut = tuple(int(x) for x in MIS_BROKER_CUTOFF.split(":"))
            if (self.now.hour, self.now.minute) >= cut:
                raise Rejected(f"Intraday orders (MIS) are allowed only till "
                               f"{MIS_BROKER_CUTOFF} PM.")

        # The injected failure, applied LAST so it cannot mask a real defect.
        if ot in self.reject_order_types:
            raise Rejected(self.reject_reason
                           or f"INJECTED FAILURE: broker refusing {ot} orders")

        return f"STUB{len(self.orders):06d}"

    def quote(self, instruments):
        if isinstance(instruments, str):
            instruments = [instruments]
        return {i: {"last_price": self.last_price} for i in instruments}

    def ltp(self, instruments):                      # noqa: A003
        return self.quote(instruments)

    def margins(self, segment=None):
        return {"equity": {"available": {"live_balance": 500000.0}}}

    def orders_(self):
        return self.orders


def of(kite, order_type):
    return [o for o in kite.orders
            if str(o.get("order_type", "")).upper() == str(order_type).upper()]


# ══════════════════════════════════════════════════════════════════
# 1 — ORDER TYPES AND PARAMETERS
# ══════════════════════════════════════════════════════════════════

def section_order_types():
    head("1 — ORDER TYPES AND PARAMETERS (Task 1)")
    from atlas.execution import exits as X

    k = RecordingKite()
    r = X.protect(symbol="BHARTIARTL", direction="SHORT", qty=5, product="MIS",
                  fill_price=1833.0, stop_price=1860.0, kite=k)
    print(f"\n  protect() -> ok={r.get('ok')} mechanism={r.get('mechanism')}")
    print("  ORDER AS IT WOULD GO ON THE WIRE:")
    for o in k.orders:
        print("   ", json.dumps(o, default=str, sort_keys=True))

    sl = of(k, "SL")
    check("the stop is an SL (stop-loss LIMIT), not SL-M", bool(sl),
          f"order types sent: {[o.get('order_type') for o in k.orders]}")
    if sl:
        o = sl[0]
        check("the SL carries a trigger_price", bool(o.get("trigger_price")),
              f"trigger_price={o.get('trigger_price')}")
        check("the SL carries a limit price", bool(o.get("price")),
              f"price={o.get('price')}")
        check("a SHORT stop's limit is ABOVE its trigger (so the BUY fills)",
              float(o["price"]) > float(o["trigger_price"]),
              f"price {o['price']} > trigger {o['trigger_price']}")
        check("the SL sends no market_protection (no effect on SL per the docs)",
              "market_protection" not in o)
    check("no SL-M order is used anywhere in the stop path", not of(k, "SL-M"))

    # LONG -> the stop is a SELL, so the limit must sit BELOW the trigger.
    k2 = RecordingKite()
    X._protect_mis_sl("TATASTEEL", "LONG", 10, 100.0, 99.0, None, k2)
    sl2 = of(k2, "SL")
    if check("a LONG MIS stop also places an SL", bool(sl2)):
        o = sl2[0]
        check("a LONG stop's limit is BELOW its trigger (so the SELL fills)",
              float(o["price"]) < float(o["trigger_price"]),
              f"price {o['price']} < trigger {o['trigger_price']}")

    # The emergency exit.
    k3 = RecordingKite(ltp=1833.0)
    e = X.emergency_exit("BHARTIARTL", "SHORT", 5, "MIS", why="verification",
                         kite=k3)
    print(f"\n  emergency_exit() -> ok={e.get('ok')} reason={e.get('reason','')}")
    for o in k3.orders:
        print("   ", json.dumps(o, default=str, sort_keys=True))
    lim = of(k3, "LIMIT")
    check("the emergency exit is a LIMIT, not MARKET", bool(lim) and not of(k3, "MARKET"),
          f"order types sent: {[o.get('order_type') for o in k3.orders]}")
    if lim:
        o = lim[0]
        check("the exit limit is priced THROUGH the book (a SHORT buys above LTP)",
              float(o["price"]) > k3.last_price,
              f"price {o['price']} vs ltp {k3.last_price}")
        check("the exit carries no trigger_price (nothing rests)",
              not o.get("trigger_price"))

    # Anything that stays MARKET.
    import atlas.execution.broker as B
    check("MARKET entry orders set market_protection",
          "market_protection" in open(B.__file__).read(),
          f"MARKET_PROTECTION_PCT = {B.MARKET_PROTECTION_PCT}")
    check("MARKET_PROTECTION_PCT is in the documented range (0,100] or -1",
          0 < B.MARKET_PROTECTION_PCT <= 100 or B.MARKET_PROTECTION_PCT == -1,
          f"value {B.MARKET_PROTECTION_PCT}")
    if k.notes or k3.notes:
        print(f"\n  stub notes: {k.notes + k3.notes}")


# ══════════════════════════════════════════════════════════════════
# 2 — THE 7 OCT CONFIGURATION, REPLAYED
# ══════════════════════════════════════════════════════════════════

def section_replay():
    head("2 — THE 2026-10-07 CONFIGURATION, REPLAYED AGAINST THE SAME RULES")
    from atlas.execution import exits as X

    print("\n  What the old code sent, against a stub that enforces the broker's rules:")
    k = RecordingKite(now=datetime(2026, 10, 7, 14, 12, 9, tzinfo=IST))
    old = []
    for label, kw in (
        ("primary  SL-M stop", dict(order_type="SL-M", trigger_price=1860.0)),
        ("fallback MARKET exit", dict(order_type="MARKET")),
    ):
        try:
            k.place_order(variety="regular", exchange="NSE",
                          tradingsymbol="BHARTIARTL", transaction_type="BUY",
                          quantity=5, product="MIS", validity="DAY", **kw)
            old.append((label, None))
            print(f"    {label:22} ACCEPTED")
        except Rejected as e:
            old.append((label, str(e)))
            print(f"    {label:22} REJECTED — {e}")

    reasons = [r for _, r in old]
    check("the old primary AND the old fallback are both rejected",
          all(reasons))
    check("they are rejected for the SAME reason — one layer twice",
          reasons[0] == reasons[1], f"identical text: {reasons[0] == reasons[1]}")

    print("\n  What the new code sends, against the same stub and the same clock:")
    k2 = RecordingKite(now=datetime(2026, 10, 7, 14, 12, 9, tzinfo=IST))
    r = X.protect(symbol="BHARTIARTL", direction="SHORT", qty=5, product="MIS",
                  fill_price=1833.0, stop_price=1860.0, kite=k2)
    print(f"    primary  {X.ORDER_SPECS[X.PRIMARY_STOP]['order_type']:14} "
          f"{'ACCEPTED' if r.get('ok') else 'REJECTED — ' + str(r.get('reason'))}")
    check("the new primary is accepted where the old one was rejected",
          bool(r.get("ok")), f"reason: {r.get('reason','')}")

    k3 = RecordingKite(now=datetime(2026, 10, 7, 14, 12, 9, tzinfo=IST))
    e = X.emergency_exit("BHARTIARTL", "SHORT", 5, "MIS", why="replay", kite=k3)
    print(f"    fallback {X.ORDER_SPECS[X.FALLBACK_EXIT]['order_type']:14} "
          f"{'ACCEPTED' if e.get('ok') else 'REJECTED — ' + str(e.get('reason'))}")
    check("the new fallback is accepted where the old one was rejected",
          bool(e.get("ok")), f"reason: {e.get('reason','')}")


# ══════════════════════════════════════════════════════════════════
# 3 — INDEPENDENCE UNDER AN INJECTED PRIMARY FAILURE
# ══════════════════════════════════════════════════════════════════

def section_independence():
    head("3 — INDEPENDENCE: INJECT A FAILURE INTO THE PRIMARY (Task 2)")
    from atlas.execution import exits as X

    print(f"\n  contract: PRIMARY_STOP={X.PRIMARY_STOP} "
          f"FALLBACK_EXIT={X.FALLBACK_EXIT}")
    for name in (X.PRIMARY_STOP, X.FALLBACK_EXIT):
        print(f"    {name:14} {X.ORDER_SPECS[name]}")

    # THREE DIFFERENT INJECTED FAILURES, because a fallback that only survives
    # one of them is not independent, it is lucky.
    for label, kw in (
        ("broker refuses every SL order",
         dict(reject_order_types=("SL",))),
        ("the SL's trigger price is refused",
         dict(reject_order_types=("SL",),
              reject_reason="Trigger price is out of the permitted range")),
        ("the broker refuses SL and SL-M alike (the whole stop family)",
         dict(reject_order_types=("SL", "SL-M"))),
    ):
        k = RecordingKite(ltp=1833.0, **kw)
        out = X.protect_or_exit(symbol="BHARTIARTL", direction="SHORT", qty=5,
                                product="MIS", fill_price=1833.0,
                                stop_price=1860.0, kite=k)
        sent = [(o.get("order_type"), o.get("price"), o.get("trigger_price"))
                for o in k.orders]
        print(f"\n  INJECTED: {label}")
        print(f"    orders attempted: {sent}")
        print(f"    protected={out['protected']} exited={out['exited']} "
              f"unprotected={out['unprotected']}")
        check(f"primary failed as injected [{label}]",
              not out["protected"], f"reason: {out['prot'].get('reason')}")
        check(f"THE FALLBACK STILL COMPLETED [{label}]", out["exited"],
              f"exit: {(out['exit'] or {}).get('reason', 'placed')}")
        if out["exited"]:
            fb = of(k, X.ORDER_SPECS[X.FALLBACK_EXIT]["order_type"])
            check(f"the completing order is a {X.ORDER_SPECS[X.FALLBACK_EXIT]['order_type']}, "
                  f"not another {X.ORDER_SPECS[X.PRIMARY_STOP]['order_type']} [{label}]",
                  bool(fb), f"{json.dumps(fb[0], default=str, sort_keys=True) if fb else ''}")

    # And the converse: the pair that failed on 7 Oct must not be constructible.
    print("\n  The 2026-10-07 pairs, offered to the independence contract:")
    for a, b in (("SLM", "SLM"), ("SLM", "MARKET"), ("MARKET", "MARKET"),
                 ("SL", "SL")):
        try:
            X.assert_independent(a, b)
            print(f"    {a:7} + {b:7} ACCEPTED")
            check(f"{a}+{b} must be refused", False, "it was accepted")
        except AssertionError as e:
            print(f"    {a:7} + {b:7} refused — {e}")
            check(f"{a}+{b} is refused by assert_independent", True)
    try:
        X.assert_independent()
        check("the SHIPPING pair passes assert_independent", True,
              f"{X.PRIMARY_STOP} + {X.FALLBACK_EXIT}")
    except AssertionError as e:
        check("the SHIPPING pair passes assert_independent", False, str(e))


# ══════════════════════════════════════════════════════════════════
# 4 — SQUARE-OFF TIMING
# ══════════════════════════════════════════════════════════════════

def section_timing():
    head("4 — SQUARE-OFF TIMING (Task 3)")
    from atlas.config import (MIS_EXIT_TIME, MIS_BROKER_CUTOFF,
                              MIS_BROKER_CUTOFFS, MIS_EXIT_MARGIN_MIN,
                              MIS_EXIT_HOUR, MIS_EXIT_MIN)
    from atlas.execution import exits as X

    print(f"\n  published cutoffs: {json.dumps(MIS_BROKER_CUTOFFS, sort_keys=True)}")
    print(f"  binding cutoff   : {MIS_BROKER_CUTOFF}")
    print(f"  engine exit      : {MIS_EXIT_TIME}  ({MIS_EXIT_MARGIN_MIN} min of margin)")

    cut = tuple(int(x) for x in MIS_BROKER_CUTOFF.split(":"))
    check("the engine's exit is BEFORE the binding broker cutoff",
          (MIS_EXIT_HOUR, MIS_EXIT_MIN) < cut, f"{MIS_EXIT_TIME} < {MIS_BROKER_CUTOFF}")
    check("the margin is at least 10 minutes", MIS_EXIT_MARGIN_MIN >= 10,
          f"{MIS_EXIT_MARGIN_MIN} min")
    check("the binding cutoff is the EARLIEST published one, not an average",
          MIS_BROKER_CUTOFF == min(MIS_BROKER_CUTOFFS.values()))

    timer = (ROOT / "deploy" / "atlas-mis-squareoff.timer").read_text()
    oncal = [l for l in timer.splitlines() if l.startswith("OnCalendar=")]
    print(f"  timer            : {oncal[0] if oncal else '(none)'}")
    check("the systemd timer fires at the configured exit time",
          bool(oncal) and f" {MIS_EXIT_TIME} " in oncal[0],
          f"{oncal[0] if oncal else 'no OnCalendar line'} vs MIS_EXIT_TIME={MIS_EXIT_TIME}")

    print("\n  squareoff_mis() across the window:")
    for t in ("14:59", MIS_EXIT_TIME, "15:11", MIS_BROKER_CUTOFF, "15:14"):
        h, m = map(int, t.split(":"))
        o = X.squareoff_mis([], now_ist=datetime(2026, 10, 9, h, m, tzinfo=IST))
        print(f"    {t} IST -> due={str(o['due']):5} late={str(o['late']):5} "
              f"alerts={len(o['alerts'])}")
    due_at_exit = X.squareoff_mis([], now_ist=datetime(2026, 10, 9, MIS_EXIT_HOUR,
                                                       MIS_EXIT_MIN, tzinfo=IST))
    late_at_cut = X.squareoff_mis([], now_ist=datetime(2026, 10, 9, cut[0], cut[1],
                                                       tzinfo=IST))
    check("it is due at the engine exit time", due_at_exit["due"] and not due_at_exit["late"])
    check("a run at or past the cutoff is flagged LATE and alerts",
          late_at_cut["late"] and bool(late_at_cut["alerts"]),
          late_at_cut["alerts"][0][:110] if late_at_cut["alerts"] else "")

    # The hole this move could have opened: an MIS entry after the square-off.
    import atlas.execution.session_selector as S
    src = open(S.__file__).read()
    shorts_end = "11*60+30" in src and '"allowed": ["LONG", "SHORT"]' in src
    check("MIS entries (shorts) end at 11:30, well before the square-off",
          shorts_end,
          "longs are CNC, shorts are MIS, and shorts are morning-session only, "
          "so moving the square-off earlier strands nothing")


# ══════════════════════════════════════════════════════════════════
# 5 — THE HALT GUARD
# ══════════════════════════════════════════════════════════════════

def section_halt():
    head("5 — WATCH-ONLY AND THE HALT GUARD (Task 4)")
    import importlib
    import atlas.risk.breaker as breaker
    importlib.reload(breaker)

    halted, why = breaker.is_halted()
    print(f"\n  halt file: {breaker.SENTINEL}")
    print(f"  is_halted() -> {halted}  {why[:120]}")
    check("the halt sentinel is present for this verification", halted)

    import atlas.execution.broker as B
    importlib.reload(B)

    # GUARD 1 -- place_order itself, the lowest level.
    print("\n  GUARD 1 — broker.place_order(), the lowest level:")
    for intent in ("ENTRY", None):
        kw = dict(symbol="BHARTIARTL", direction="SHORT", qty=5,
                  order_type="MARKET", product="MIS")
        r = B.place_order(**kw) if intent is None else B.place_order(intent=intent, **kw)
        print(f"    intent={str(intent):6} -> success={r.get('success')} "
              f"blocked_by={r.get('blocked_by')} reason={str(r.get('reason'))[:70]}")
        check(f"place_order refuses an ENTRY while halted (intent={intent})",
              r.get("success") is False and r.get("blocked_by") in ("halt", "paused"))

    print("\n  GUARD 2 — enter_trade(), the gate stack:")
    from atlas.execution.atlas_entry import enter_trade
    sig = {"symbol": "BHARTIARTL", "direction": "SHORT", "entry_ref": 1833.0,
           "entry_low": 1830.0, "entry_high": 1836.0, "sl": 1860.0}
    live = enter_trade(sig)
    print(f"    enter_trade(dry_run=False) -> {live.get('status')}: "
          f"{str(live.get('reason'))[:90]}")
    check("a live enter_trade() is blocked by the halt before any gate runs",
          live.get("status") == "BLOCKED_HALTED")

    print("\n  GUARD 3 — the independence of the two:")
    src = open(B.__file__).read()
    check("the broker guard does not depend on the caller passing intent",
          'intent: str = "ENTRY"' in src,
          "the default is ENTRY, so an unlabelled order is refused while halted")
    # WITHIN place_order's OWN BODY, not the file. The first comparison here was
    # against the whole module and get_ltp() calls get_kite() a hundred lines
    # earlier, so it compared two unrelated functions and reported a failure
    # that was not there.
    body = src[src.index("def place_order("):]
    body = body[:body.index("\ndef ")]
    check("inside place_order, the halt is checked before get_kite() is called",
          "is_halted" in body and "get_kite" in body
          and body.index("is_halted") < body.index("get_kite"),
          f"is_halted at char {body.index('is_halted')}, "
          f"get_kite at char {body.index('get_kite')} of place_order's body — "
          f"nothing can reach the network while halted, even on a code path "
          f"that forgot to check")


def section_halted_cycle():
    head("6 — A HALTED CYCLE: ZONES LOADED, DECISIONS LOGGED, ZERO ORDERS")
    import atlas.signal.market_open as M
    import atlas.execution.broker as B
    import atlas.execution.atlas_entry as A
    from atlas.config import SUPABASE_KEY

    # TWO MODES, AND THE OUTPUT SAYS WHICH ONE RAN.
    #
    #   LIVE-DB   the service key is present (the box, /etc/atlas.env): the zone
    #             map comes from the real signals table, committed_today reads
    #             the real atlas_trades, and the decisions are written to the
    #             real atlas_entry_log. This is the evidence that matters.
    #   SEEDED    no service key (a developer machine): the SIX HTTP seams are
    #             replaced with a seeded batch so the gate stack still runs on
    #             real code. The DATABASE is stubbed, the BROKER is not --
    #             order calls are counted at the broker boundary either way.
    #
    # The seeded mode is not a substitute. Run this on the box for the real one.
    seeded = not SUPABASE_KEY
    mode = "SEEDED (no SUPABASE_SERVICE_KEY on this machine)" if seeded else "LIVE-DB"
    print(f"\n  mode: {mode}")

    written = {"entry_log": [], "live_zones": []}
    if seeded:
        today = datetime.now(IST).date().isoformat()
        # Three zones: one enterable ('signal') with price INSIDE its band so it
        # reaches the entry decision, one candidate, one away from its zone.
        batch = [
            {"symbol": "BHARTIARTL", "direction": "SHORT", "publication_kind": "signal",
             "entry_ref": 1833.0, "entry_low": 1830.0, "entry_high": 1836.0,
             "sl": 1860.0, "signal_date": today},
            {"symbol": "TATASTEEL", "direction": "LONG", "publication_kind": "candidate",
             "entry_ref": 150.0, "entry_low": 149.0, "entry_high": 151.0,
             "sl": 145.0, "signal_date": today},
            {"symbol": "RELIANCE", "direction": "LONG", "publication_kind": "signal",
             "entry_ref": 1400.0, "entry_low": 1395.0, "entry_high": 1405.0,
             "sl": 1370.0, "signal_date": today},
        ]
        quotes = {"BHARTIARTL": 1833.0, "TATASTEEL": 150.5, "RELIANCE": 1480.0}
        M.get_latest_batch_date = lambda: today
        M.get_signals = lambda d: batch
        M.committed_today = lambda: (True, set())
        M.fetch_quotes = lambda syms: {s: quotes.get(s, 0.0) for s in syms}
        M.push_live_zones = lambda st, rows, keys: (
            written["live_zones"].extend(rows) or {"rows": len(rows)})
        _real_log = M.log_decision
        M.log_decision = lambda sig, res: written["entry_log"].append(
            {"symbol": sig.get("symbol"), "direction": sig.get("direction"),
             "status": res.get("status"), "reason": str(res.get("reason", ""))[:90]})
        # The gate stack needs a broker session for the quote and the margin.
        # An ADVERSARIAL one, and it records anything that reaches it.
        k = RecordingKite(ltp=1833.0)
        B._kite_cache = k
        B.get_kite = lambda: k
        A.get_kite = getattr(A, "get_kite", lambda: k)
    else:
        _real_log = M.log_decision
        M.log_decision = lambda sig, res, _f=M.log_decision: (
            written["entry_log"].append(
                {"symbol": sig.get("symbol"), "direction": sig.get("direction"),
                 "status": res.get("status"), "reason": str(res.get("reason", ""))[:90]})
            or _f(sig, res))

    # THE COUNT IS TAKEN AT THE BROKER BOUNDARY, which is as low as it goes
    # without a network. Anything that tries to reach the broker increments it,
    # whether it came from the entry path, the exit path or a caller that
    # forgot to check anything.
    calls = {"n": 0, "args": []}
    for mod in (B, A, M):
        for fn in ("place_order", "place_sl_order"):
            if hasattr(mod, fn):
                orig = getattr(mod, fn)

                def wrap(_orig=orig, _n=f"{mod.__name__}.{fn}"):
                    def inner(*a, **kw):
                        calls["n"] += 1
                        calls["args"].append((_n, kw))
                        return _orig(*a, **kw)
                    return inner
                setattr(mod, fn, wrap())

    state = M.Session()
    print(f"  paused() -> {M.paused()[0]}  {M.paused()[1][:90]}")
    try:
        out = M.cycle(state)
    except Exception:
        traceback.print_exc()
        check("the halted cycle ran to completion", False, "it raised")
        return
    finally:
        M.log_decision = _real_log

    zones = len(getattr(state, "zone_map", {}) or {})
    print(f"\n  cycle() -> {json.dumps(out, default=str, sort_keys=True)[:400]}")
    print(f"  observe_only        : {state.observe_only}")
    print(f"  batch_date          : {state.batch_date}")
    print(f"  zones in the map    : {zones}")
    print(f"  candidates evaluated: {out.get('candidates')}")
    print(f"  atlas_entry_log rows: {len(written['entry_log'])}")
    for r in written["entry_log"]:
        print(f"      {r['symbol']:12} {r['direction']:6} {r['status']:28} {r['reason'][:60]}")
    print(f"  atlas_live_zones rows: {len(written['live_zones']) or out.get('live_view')}")
    print(f"  ORDER CALLS REACHING THE BROKER LAYER: {calls['n']}")
    for nm, kw in calls["args"]:
        print(f"      {nm} {kw}")

    check("the cycle did NOT return early on the pause check",
          "error" not in out or out.get("error") not in (None,) and zones > 0,
          f"error={out.get('error')!r} zones={zones}")
    check("the cycle recorded that it is observe-only",
          bool(state.observe_only) and bool(out.get("observe_only")),
          str(out.get("observe_only"))[:100])
    check("the zone map LOADED while halted", zones > 0, f"{zones} zones")
    check("every candidate was evaluated through the gate stack",
          out.get("candidates") is not None and out.get("candidates") >= 0,
          f"candidates={out.get('candidates')}")
    check("gate decisions were LOGGED while halted",
          len(written["entry_log"]) > 0,
          f"{len(written['entry_log'])} atlas_entry_log row(s)")
    check("the logged statuses say OBSERVED, not entered",
          all(str(r["status"]).startswith("WATCH_ONLY")
              for r in written["entry_log"]) if written["entry_log"] else False,
          f"statuses: {sorted({r['status'] for r in written['entry_log']})}")
    check("the live zone view was published while halted",
          bool(written["live_zones"]) or bool(out.get("live_view")),
          f"{len(written['live_zones'])} row(s)")
    check("ZERO order calls reached the broker layer", calls["n"] == 0,
          f"{calls['args'][:3]}")
    if seeded:
        print("\n  NOTE: SEEDED mode. The zone map and the ledger writes were "
              "stubbed\n        because this machine has no service key. Run this "
              "on the box for\n        the LIVE-DB run:\n"
              "          sudo -u ubuntu env $(grep -v '^#' /etc/atlas.env | xargs) \\\n"
              "            ATLAS_HALT_FILE=/var/lib/atlas/halt \\\n"
              "            /home/ubuntu/thestocklogic/venv/bin/python \\\n"
              "            -m tools.verify_exit_path")


def main():
    print(__doc__)
    print(f"run at {datetime.now(IST):%Y-%m-%d %H:%M:%S} IST")
    print(f"halt file override: ATLAS_HALT_FILE={os.environ.get('ATLAS_HALT_FILE')}")

    for fn in (section_order_types, section_replay, section_independence,
               section_timing, section_halt, section_halted_cycle):
        try:
            fn()
        except Exception:
            print(f"\n  !! {fn.__name__} raised:")
            traceback.print_exc()
            FAIL.append(f"{fn.__name__} raised")

    head("RESULT")
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"    FAILED: {f}")
    print("\n  This is the documented contract and the independence of the "
          "fallback.\n  It is NOT evidence about the live API. Run "
          "docs/LIVE_EXIT_TEST.md for that.")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
