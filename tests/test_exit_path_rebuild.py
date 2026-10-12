#!/usr/bin/env python3
"""
THE EXIT PATH, AFTER 2026-10-07
===============================
On 2026-10-07 a BHARTIARTL MIS short went unprotected for 68 minutes. The SL-M
stop was rejected -- "Market orders without market protection are not allowed
via API" -- and the emergency market exit was rejected with the SAME error for
the SAME reason, because the fallback was the same order family as the thing
that had just failed. Two layers of protection were one layer twice.

WHAT THIS FILE GUARDS, AND WHY EACH ONE IS HERE RATHER THAN DESCRIBED IN A
COMMENT:

  1. THE PAIR MUST FAIL FOR INDEPENDENT REASONS. Not "should" -- the module
     refuses to import otherwise. A future edit that makes the fallback a
     market-type order again, or gives it a trigger, breaks the build.
  2. THE STOP IS AN SL WITH A TRIGGER AND A LIMIT PAST IT. The limit's SIDE is
     asserted per direction: a LONG stop sells BELOW the trigger, a SHORT stop
     buys ABOVE it. Get that backwards and the order rests un-fillable, which
     looks like protection and is not.
  3. THE ENGINE'S SQUARE-OFF IS BEFORE THE BROKER'S CUTOFF, and the systemd
     timer matches the config. 15:15 was three minutes PAST the 15:12 that
     applies to F&O-segment stocks, so that square-off had never been able to
     work on most of the universe.
  4. A HALTED ATLAS OBSERVES AND PLACES NOTHING, enforced twice: once in the
     loop and once inside the order-placement function, so a caller that
     forgets cannot reach the broker.
  5. THE OPERATOR-FACING NUMBERS COME FROM CONFIG. Two strings were wrong for
     days -- a risk budget that had been removed, and "ATLAS places no
     stop-losses" while it was trying to place one.

THE FAKE HERE REFUSES WHAT ZERODHA REFUSES. The incident's root cause was that
this path "was only ever tested against an injected fake that accepted
everything", so a permissive fake is itself the defect being guarded against.
tools/verify_exit_path.py carries the full adversarial stub; this file asserts
the invariants that must never regress.

NONE OF THIS IS EVIDENCE ABOUT THE LIVE API. There is no verification trade --
the first real trades are the verification. See docs/ATLAS_LONG_ONLY.md s08.

    python3 tests/test_exit_path_rebuild.py
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

from atlas.execution import exits as X                          # noqa: E402
from atlas import config as C                                   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<60}{str(got):<10}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


class StrictKite:
    """Refuses what Zerodha refuses. reject names order types to fail."""

    def __init__(self, ltp=1833.0, reject=(), reject_gtt=False,
                 gtt_pending=0, reject_reason=None):
        self.reject = {str(r).upper() for r in reject}
        self.reject_reason = reject_reason
        self.orders = []
        self.gtts = []
        self.reject_gtt = reject_gtt
        # Zerodha's ToS: "At a particular point of time, You / each client may
        # only have a maximum of 500 pending GTTs." Confirmed 2026-10-12.
        self.gtt_pending = int(gtt_pending)
        self.last_price = ltp
        self.n = 0

    # ── THE GTT ENDPOINT, WITH ITS OWN RULE SET ──────────────────
    #
    # This is the point of the independence axis: /gtt/triggers enforces
    # different things from /orders, so a refusal here is not a refusal there.
    # Rules taken from https://kite.trade/docs/connect/v3/gtt/ and
    # https://zerodha.com/tos/gtt/ on 2026-10-12.
    GTT_MAX_PENDING = 500

    def place_gtt(self, **kw):
        self.gtts.append(kw)
        tt = str(kw.get("trigger_type", ""))
        if tt not in ("single", "two-leg"):
            raise RuntimeError(f"invalid trigger_type {tt!r}")
        tv = list(kw.get("trigger_values") or [])
        orders = list(kw.get("orders") or [])
        if tt == "two-leg" and len(tv) != 2:
            raise RuntimeError("two-leg requires exactly two trigger_values")
        if len(orders) != len(tv):
            raise RuntimeError("orders must pair with trigger_values")
        if tt == "two-leg" and not (tv[0] < tv[1]):
            raise RuntimeError("trigger_values must be ascending")
        if not kw.get("last_price"):
            raise RuntimeError("last_price is required")
        for o in orders:
            if str(o.get("product", "")).upper() != "CNC":
                # GTT does not exist for MIS. This is the fact that forced SL-M
                # on 7 October.
                raise RuntimeError(f"GTT supports CNC only, got "
                                   f"{o.get('product')}")
        if self.gtt_pending >= self.GTT_MAX_PENDING:
            raise RuntimeError(f"maximum {self.GTT_MAX_PENDING} pending GTTs "
                               f"reached")
        if self.reject_gtt:
            raise RuntimeError("INJECTED: broker refusing GTT placement")
        self.n += 1
        return {"trigger_id": 70000 + self.n}

    def get_gtts(self):
        return [{"id": 70000 + i + 1, "status": "active", **g}
                for i, g in enumerate(self.gtts)]

    def delete_gtt(self, **kw):
        self.gtts.append(("delete", kw))
        return {"trigger_id": kw.get("trigger_id")}

    def place_order(self, **kw):
        self.orders.append(kw)
        ot = str(kw.get("order_type", "")).upper()
        if ot in ("MARKET", "SL-M") and kw.get("market_protection") is None:
            raise RuntimeError("Market orders without market protection are not "
                               "allowed via API. Please set market protection "
                               "or use a Limit order.")
        if ot in ("SL", "SL-M") and not kw.get("trigger_price"):
            raise RuntimeError(f"trigger_price is required for {ot}")
        if ot in ("SL", "LIMIT") and not kw.get("price"):
            raise RuntimeError(f"price is required for {ot}")
        if ot in self.reject:
            raise RuntimeError(self.reject_reason or f"broker refusing {ot}")
        self.n += 1
        return str(9000 + self.n)

    def ltp(self, instruments):
        if isinstance(instruments, str):
            instruments = [instruments]
        return {i: {"last_price": self.last_price} for i in instruments}

    quote = ltp

    def cancel_order(self, **kw):
        self.orders.append(kw)


def types_sent(k):
    return [str(o.get("order_type", "")).upper() for o in k.orders
            if o.get("order_type")]


def main() -> int:
    print("=" * 78)
    print("1 — THE INDEPENDENCE CONTRACT")
    print("-" * 78)
    check("the shipping pair passes assert_independent",
          X.assert_independent() is None, True,
          f"{X.PRIMARY_STOP} + {X.FALLBACK_EXIT}")
    # THE 7 OCT PAIRS MUST NOT BE CONSTRUCTIBLE. Each of these was a live
    # configuration or one edit away from being one.
    for a, b, why in (("SLM", "MARKET", "the actual 2026-10-07 pair"),
                      ("SLM", "SLM", "same order type"),
                      ("MARKET", "MARKET", "same order type"),
                      ("SL", "SL", "same order type"),
                      ("SL", "SLM", "both need a trigger"),
                      ("LIMIT_THROUGH", "SL", "the fallback would not be immediate")):
        refused = False
        try:
            X.assert_independent(a, b)
        except AssertionError:
            refused = True
        check(f"{a} + {b} is refused", refused, True, why)

    print()
    print("=" * 78)
    print("2 — THE PROTECTION IS ONE TWO-LEG OCO GTT, CNC, LONG")
    print("-" * 78)
    # PAYLOAD CONFIRMED against https://kite.trade/docs/connect/v3/gtt/ on
    # 2026-10-12: type "two-leg", a condition carrying exchange, tradingsymbol,
    # trigger_values[2] and last_price, and an orders array of two objects.
    # StrictKite.place_gtt enforces that shape plus the 500-pending cap from
    # Zerodha's GTT terms, so a malformed instruction fails here rather than at
    # the broker.
    k = StrictKite()
    r = X.protect(symbol="RELIANCE", direction="LONG", qty=10, product="CNC",
                  fill_price=100.0, stop_price=95.0, kite=k)
    check("a CNC LONG gets an OCO GTT", bool(r.get("ok")), True,
          str(r.get("reason", ""))[:60])
    check("mechanism is GTT_OCO", r.get("mechanism"), "GTT_OCO")
    check("exactly one GTT instruction was sent", len(k.gtts), 1)
    check("NO regular order was sent for protection", types_sent(k), [])
    g = k.gtts[0]
    check("  trigger_type is two-leg", g.get("trigger_type"), "two-leg")
    check("  two trigger values", len(g.get("trigger_values") or []), 2)
    check("  two order legs", len(g.get("orders") or []), 2)
    check("  last_price is sent", bool(g.get("last_price")), True,
          str(g.get("last_price")))
    tv = g["trigger_values"]
    check("  trigger_values ascend (stop below target)", tv[0] < tv[1], True,
          f"{tv}")
    legs = g["orders"]
    check("  both legs SELL to close a long",
          all(o["transaction_type"] == "SELL" for o in legs), True)
    check("  both legs CNC", all(o["product"] == "CNC" for o in legs), True)
    check("  both legs LIMIT", all(o["order_type"] == "LIMIT" for o in legs),
          True)
    check("  leg 0 is the stop, leg 1 the target",
          legs[0]["price"] < legs[1]["price"], True,
          f"stop {legs[0]['price']} / target {legs[1]['price']}")
    # BOTH LEG LIMITS SIT BELOW THEIR OWN TRIGGER, because both legs are SELLs.
    # This asserted the stop leg was priced AT the stop (95.0) with the trigger
    # at 94.90 -- the limit ABOVE the trigger, which is a stop that fires and
    # then rests above the market, unfilled. Zerodha names it: "Your order's
    # LTP did not match the limit price you set... position your limit price
    # more favorably (below trigger for sells)." A stop that triggers and does
    # not fill is worse than no stop, because the position looks protected.
    check("  the stop leg's limit is BELOW its trigger",
          legs[0]["price"] < tv[0], True,
          f"limit {legs[0]['price']} < trigger {tv[0]}")
    check("  the target leg's limit is BELOW its trigger too",
          legs[1]["price"] < tv[1], True,
          f"limit {legs[1]['price']} < trigger {tv[1]}")
    check("  the target TRIGGER is 2R from the actual fill", tv[1], 110.0,
          "fill 100, stop 95 -> risk 5 -> 2R = 110")

    # THE RETIRED PATHS REFUSE. Nothing routes a short or an MIS position
    # anywhere, and nothing reaches either API.
    for d, prod, label in (("SHORT", "MIS", "a SHORT MIS position"),
                           ("SHORT", "CNC", "a SHORT CNC position"),
                           ("LONG", "MIS", "an MIS LONG position")):
        kx = StrictKite()
        rx = X.protect(symbol="X", direction=d, qty=5, product=prod,
                       fill_price=100.0, stop_price=95.0, kite=kx)
        check(f"{label} is refused", bool(rx.get("ok")), False,
              str(rx.get("reason", ""))[:54])
        check(f"  and nothing reached either API",
              (len(kx.orders), len(kx.gtts)), (0, 0))

    kr = StrictKite()
    rr = X._protect_mis_sl("TATASTEEL", "SHORT", 90, 100.0, 105.0, 90.0, kr)
    check("the retired SL path refuses if called directly",
          bool(rr.get("ok")), False, str(rr.get("reason"))[:54])
    check("  and sends nothing", (len(kr.orders), len(kr.gtts)), (0, 0))

    # THE 500-GTT CAP IS A REAL LIMIT AND IS HANDLED AS A FAILURE, not as a
    # silently unprotected position. Zerodha's GTT terms: "a maximum of 500
    # pending GTTs".
    kc = StrictKite(gtt_pending=500)
    rc = X.protect(symbol="RELIANCE", direction="LONG", qty=10, product="CNC",
                   fill_price=100.0, stop_price=95.0, kite=kc)
    check("at the 500-GTT cap, protection FAILS loudly", bool(rc.get("ok")),
          False, str(rc.get("reason"))[:60])

    print()
    print("=" * 78)
    print("3 — THE FALLBACK SURVIVES A FAILURE OF THE PRIMARY")
    print("-" * 78)
    # THE INJECTION IS NOW ON THE GTT ENDPOINT, which is the whole point: the
    # primary fails at /gtt/triggers and the fallback goes to /orders, so there
    # is no rule either of them shares.
    # ON FILL THE PRIMARY IS THE DAY-0 SL, so the injection is on the SL order.
    # The GTT path is exercised separately below against place_overnight_gtts,
    # which is what the pre-close job calls. Injecting on the GTT here
    # exercised nothing once the day-0/day-1 split landed.
    for kwargs, label in (
        ({"reject": ("SL",)}, "the broker refuses the day-0 SL"),
        ({"reject": ("SL",),
          "reject_reason": "Trigger price out of the permitted range"},
         "the day-0 SL trigger is refused"),
    ):
        k3 = StrictKite(**kwargs)
        out = X.protect_or_exit(symbol="RELIANCE", direction="LONG", qty=10,
                                product="CNC", fill_price=100.0,
                                stop_price=95.0, kite=k3)
        check(f"[{label}] the primary failed", out["protected"], False)
        check(f"[{label}] the FALLBACK completed", out["exited"], True,
              str((out["exit"] or {}).get("reason", ""))[:40])
        check(f"[{label}] it completed with a LIMIT", types_sent(k3),
              ["SL", "LIMIT"])
        check(f"[{label}] and NO GTT was placed on fill", len(k3.gtts), 0,
              "the GTT belongs to the pre-close job; nothing on the fill path "
              "touches /gtt/triggers")
        check(f"[{label}] must_halt is set", out.get("must_halt"), True,
              "a broken protection mechanism stops further entries even when "
              "this position closed cleanly")

    # AND THE CONVERSE: if the fallback's own family is refused, the result is
    # an honest "unprotected", not a silent success.
    k4 = StrictKite(reject=("SL", "LIMIT"))
    out = X.protect_or_exit(symbol="RELIANCE", direction="LONG", qty=10,
                            product="CNC", fill_price=100.0, stop_price=95.0,
                            kite=k4)
    check("both refused -> unprotected is reported, not swallowed",
          out["unprotected"], True, out["reason"][:50])

    # NO LTP MEANS NO FALLBACK ORDER, NOT A MARKET ORDER. Falling back to MARKET
    # here would reinstate the shared failure mode.
    class NoPrice(StrictKite):
        def ltp(self, instruments):
            raise RuntimeError("quote endpoint down")
        quote = ltp

    k5 = NoPrice()
    e = X.emergency_exit("BHARTIARTL", "SHORT", 5, "MIS", why="t", kite=k5)
    check("an unpriceable fallback refuses rather than sending MARKET",
          bool(e.get("ok")), False, str(e.get("reason"))[:48])
    check("and it sent no order at all", types_sent(k5), [])

    print()
    print("=" * 78)
    print("4 — SQUARE-OFF TIMING")
    print("-" * 78)
    cut = tuple(int(x) for x in C.MIS_BROKER_CUTOFF.split(":"))
    check("the engine's exit is before the broker's binding cutoff",
          (C.MIS_EXIT_HOUR, C.MIS_EXIT_MIN) < cut, True,
          f"{C.MIS_EXIT_TIME} < {C.MIS_BROKER_CUTOFF}")
    check("the binding cutoff is the earliest published one",
          C.MIS_BROKER_CUTOFF, min(C.MIS_BROKER_CUTOFFS.values()))
    check("the margin is at least 10 minutes",
          C.MIS_EXIT_MARGIN_MIN >= 10, True, f"{C.MIS_EXIT_MARGIN_MIN} min")
    # THE TIMER AND THE CONFIG MUST AGREE. They are in different files and
    # nothing at runtime compares them, so this is the only thing that does.
    timer = (ROOT / "deploy" / "atlas-mis-squareoff.timer").read_text()
    oncal = [l for l in timer.splitlines() if l.startswith("OnCalendar=")]
    check("the systemd timer fires at MIS_EXIT_TIME",
          bool(oncal) and f" {C.MIS_EXIT_TIME} " in oncal[0], True,
          oncal[0] if oncal else "no OnCalendar line")
    check("exits.py reads the time from config rather than defining it",
          "MIS_EXIT_HOUR, MIS_EXIT_MIN = 15" in
          (ROOT / "atlas" / "execution" / "exits.py").read_text(), False)

    def at(t):
        h, m = map(int, t.split(":"))
        return X.squareoff_mis([], now_ist=datetime(2026, 10, 9, h, m, tzinfo=IST))

    check("not due a minute before the exit time",
          at("14:59")["due"], False)
    check("due at the exit time", at(C.MIS_EXIT_TIME)["due"], True)
    check("not flagged late at the exit time", at(C.MIS_EXIT_TIME)["late"], False)
    check("flagged LATE at the cutoff", at(C.MIS_BROKER_CUTOFF)["late"], True)
    check("and it alerts when late",
          bool(at(C.MIS_BROKER_CUTOFF)["alerts"]), True)

    # THE HOLE THIS MOVE COULD HAVE OPENED. Moving the square-off earlier would
    # strand any MIS position opened after it. Longs are CNC and shorts -- the
    # only MIS product -- are morning-session only, so there is no gap. If that
    # ever changes, this fails rather than a position being stranded.
    from atlas.risk import position_sizing as PS
    src = (ROOT / "atlas" / "risk" / "position_sizing.py").read_text()
    check("MIS is used for shorts only",
          'product     = "CNC" if d == "LONG" else "MIS"' in src, True)
    sel = (ROOT / "atlas" / "execution" / "session_selector.py").read_text()
    check("shorts are allowed in the morning session only",
          sel.count('"SHORT"'), 1,
          "if a later session gains SHORT, the square-off time must move too")

    print()
    print("=" * 78)
    print("5 — A HALTED ATLAS OBSERVES, AND CANNOT PLACE")
    print("-" * 78)
    import atlas.signal.market_open as M
    import atlas.execution.broker as B
    mo = (ROOT / "atlas" / "signal" / "market_open.py").read_text()
    br = (ROOT / "atlas" / "execution" / "broker.py").read_text()
    ae = (ROOT / "atlas" / "execution" / "atlas_entry.py").read_text()

    # GUARD 1 -- the loop no longer returns on the pause check.
    i_pause = mo.index("state.observe_only, watch_why = paused()")
    i_zones = mo.index("if not load_zone_map(state):")
    check("the pause check runs BEFORE the zone map loads", i_pause < i_zones, True)
    check("and it does not return early",
          "return out" in mo[i_pause:i_zones], False,
          "a return here is the 360-cycle blindness of 2026-10-08")
    check("Session carries observe_only", hasattr(M.Session(), "observe_only"), True)
    check("it defaults to False, i.e. the guarded live path",
          M.Session().observe_only, False)

    # GUARD 2 -- the lowest level. The default intent is ENTRY so an unlabelled
    # order fails safe.
    check("place_order takes an intent defaulting to ENTRY",
          'intent: str = "ENTRY"' in br, True)
    body = br[br.index("def place_order("):]
    body = body[:body.index("\ndef ")]
    check("the halt is checked inside place_order",
          "is_halted" in body, True)
    check("and before the kite client is acquired",
          body.index("is_halted") < body.index("get_kite"), True)

    # GUARD 3 -- enter_trade's own dry_run, which must default to the live path.
    import inspect
    from atlas.execution.atlas_entry import enter_trade
    sig = inspect.signature(enter_trade)
    check("enter_trade has a dry_run parameter",
          "dry_run" in sig.parameters, True)
    check("dry_run defaults to False (a forgetful caller gets the guards)",
          sig.parameters["dry_run"].default, False)
    check("dry_run returns before the ledger reservation",
          ae.index("if dry_run:") < ae.index("_reserve_intent(intent)"), True)
    check("the watch-only path calls enter_trade with dry_run=True",
          "enter_trade(signal, dry_run=True)" in mo, True)
    # ONE SEAM. evaluate_only must resolve the SAME callable the live path does,
    # or a patch to one leaves the other pointing at the real enter_trade --
    # which on the watch-only path means an order.
    check("and it does not re-import enter_trade behind the module name",
          "from atlas.execution.atlas_entry import enter_trade as _et" in mo,
          False)
    check("and its statuses are distinct from a live entry's",
          '"WATCH_ONLY"' in mo, True)

    print()
    print("=" * 78)
    print("6 — THE OPERATOR-FACING NUMBERS COME FROM CONFIG")
    print("-" * 78)
    zm = (ROOT / "atlas" / "execution" / "zerodha_morning.py").read_text()
    # THE 08:30 MESSAGE. It said "Risk/trade Rs3,000 - Max notional Rs1,00,000"
    # while the sizer used Rs10,000 notional and no risk budget at all.
    check("the 08:30 message renders the sizing rule from the shared renderer",
          "_rules_line()" in zm, True)
    check("and no longer hardcodes the risk/notional pair",
          'f"Risk/trade ₹{MAX_RISK_PER_TRADE:,.0f} · "' in zm, False)
    from atlas.reporting.directives import _rules_line
    line = _rules_line()
    if C.SIZING_MODE == "fixed_notional":
        check("it names the notional actually in force",
              f"{C.FIXED_NOTIONAL_PER_TRADE:,.0f}" in line, True, line[:52])
        check("and does not quote a risk budget that does not exist",
              "Risk/trade" in line, False)

    # THE REPORT FOOTER. It said "ATLAS places no stop-losses. Set them
    # manually." every day since 2026-09-30, including the day it tried.
    dr = (ROOT / "atlas" / "reporting" / "daily_report.py").read_text()
    from atlas.reporting.daily_report import _footer
    check("the footer is a function, evaluated at send time",
          "def _footer()" in dr, True)
    check("it is not a module-level constant",
          'FOOTER = ("REMINDER: ATLAS places no stop-losses' in dr, False)
    f = _footer()
    if C.ENABLE_EXIT_MANAGEMENT:
        check("with exit management ON it does not claim no stops are placed",
              "places no stop-losses" in f, False, f.split(".")[0][:48])
        check("and it names the square-off time from config",
              C.MIS_EXIT_TIME in f, True)
    else:
        check("with exit management OFF it says so plainly",
              "places no stop-losses" in f, True)

    print()
    print("=" * 78)
    print("7 — THE HALT PATH DOES NOT FAIL OPEN")
    print("-" * 78)
    # THE BUG: breaker.py resolved the halt file as
    #   os.environ.get("ATLAS_HALT_FILE", "~/.atlas/halt")
    # while the real file is /var/lib/atlas/halt. The variable was set in ONE
    # of the eight places that run ATLAS code, so seven of them read a path
    # that does not exist, concluded "not halted", and would have traded.
    import os
    import importlib
    import atlas.config as CFG
    import atlas.risk.breaker as BR
    check("the canonical halt path is /var/lib/atlas/halt",
          CFG.HALT_FILE_CANONICAL, "/var/lib/atlas/halt")
    # THE DEFAULT IS THE REAL PATH. Asserted without the env var set, which is
    # the configuration seven entry points actually had.
    env_saved = os.environ.pop("ATLAS_HALT_FILE", None)
    try:
        import importlib
        importlib.reload(CFG)
        check("with NO environment variable, it defaults to the real file",
              str(CFG.HALT_FILE), "/var/lib/atlas/halt",
              "an unset environment is now the CORRECT configuration, not the "
              "broken one")
        check("  and it reports itself as canonical",
              CFG.halt_file_state()["is_canonical"], True)
    finally:
        if env_saved is not None:
            os.environ["ATLAS_HALT_FILE"] = env_saved
        importlib.reload(CFG)

    # breaker MUST read the constant, not re-derive the path.
    #
    # COMMENT-FREE SOURCE. Both files still EXPLAIN the old resolution in a
    # comment -- quoting the broken line is the clearest way to record what was
    # wrong -- and a raw substring search finds the explanation and reports the
    # defect it is meant to prevent. This repo has made that mistake nine times;
    # tests/_srcutil.executable_source exists for it.
    from _srcutil import executable_source
    br_src = (ROOT / "atlas" / "risk" / "breaker.py").read_text()
    br_exec = executable_source(ROOT / "atlas" / "risk" / "breaker.py")
    check("breaker imports the path from config",
          "from atlas.config import HALT_FILE as SENTINEL" in br_src, True)
    check("  and no executable line reads ATLAS_HALT_FILE itself",
          "ATLAS_HALT_FILE" in br_exec, False,
          "the home-directory fallback is what made the halt fail open; it "
          "survives only as a comment recording the bug")

    # AN OVERRIDE MUST ANNOUNCE ITSELF. This is what keeps the remaining env
    # read from being the same silent trap in a different coat.
    import tempfile
    tmpdir = tempfile.mkdtemp()
    os.environ["ATLAS_HALT_FILE"] = str(Path(tmpdir) / "halt")
    try:
        importlib.reload(CFG)
        stx = CFG.halt_file_state()
        check("an override is reported as NOT canonical",
              stx["is_canonical"], False)
        check("  and the summary says so in words",
              "NOT THE CANONICAL PATH" in stx["summary"], True,
              stx["summary"][:80])
    finally:
        if env_saved is None:
            os.environ.pop("ATLAS_HALT_FILE", None)
        else:
            os.environ["ATLAS_HALT_FILE"] = env_saved
        importlib.reload(CFG)
        importlib.reload(BR)

    # UNREADABLE COUNTS AS HALTED. Not knowing whether the operator stopped us
    # is not permission to trade.
    saved_path = CFG.HALT_FILE
    try:
        # A DIRECTORY. exists() is True, read_text() raises IsADirectoryError.
        # The first attempt used a path with a NUL byte, which exists()
        # answers False for on macOS -- so it tested "absent", not
        # "unreadable", and passed for the wrong reason.
        CFG.HALT_FILE = Path(tmpdir)
        stx = CFG.halt_file_state()
        check("an unreadable halt file counts as HALTED", stx["halted"], True,
              stx["reason"][:70])
        check("  and it is reported as unreadable, not as absent",
              stx["readable"], False)
    finally:
        CFG.HALT_FILE = saved_path

    # THE 08:30 MESSAGE CARRIES IT, which is the only daily signal the operator
    # gets and said nothing about the halt before.
    zm = (ROOT / "atlas" / "execution" / "zerodha_morning.py").read_text()
    check("the 08:30 Telegram renders a halt line",
          "_halt_line()" in zm, True)
    from atlas.execution.zerodha_morning import _halt_line
    check("  and the line names the file",
          str(CFG.HALT_FILE) in _halt_line(), True, _halt_line()[:80])
    mo = (ROOT / "atlas" / "signal" / "market_open.py").read_text()
    check("the engine logs the halt path on every startup",
          "log_halt_state(log)" in mo, True)
    check("  and alerts when the path is not canonical",
          'alert("HALT PATH WRONG"' in mo, True)

    # THE VERIFICATION TOOL READS THE SAME CONSTANT rather than hardcoding it.
    vt = (ROOT / "tools" / "verify_exit_path.py").read_text()
    vt_exec = executable_source(ROOT / "tools" / "verify_exit_path.py")
    check("verify_exit_path reads the constant, not a literal",
          "HALT_FILE_CANONICAL" in vt, True)
    check("  and no executable line hardcodes the path",
          "/var/lib/atlas/halt" in vt_exec, False,
          "it used to print one path and recommend another")

    print()
    print("=" * 78)
    print("PASS" if OK else "FAIL")
    print("=" * 78)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
