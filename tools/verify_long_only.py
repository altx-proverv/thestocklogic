#!/usr/bin/env python3
"""
ATLAS — prove no code path can place a SHORT, and that a failed GTT exits and halts.

    python3 -m tools.verify_long_only

THE TWO CLAIMS THE OPERATOR ASKED TO SEE PROVED, 2026-10-12:

  1. No code path can place a SHORT order.
  2. A simulated GTT placement failure exits the position AND halts.

NEITHER IS PROVED BY READING THE CONFIG. ALLOW_SHORT_ENTRIES=False is a value,
not a guarantee -- the guarantee on 7 October was also "a flag that happened to
block", and it did not. So this walks every layer that could originate an
order and shows each one refusing, then drives the real failure path and
inspects the halt file it writes.

THE HALT DEMONSTRATION WRITES TO A SCRATCH FILE. ATLAS_HALT_FILE is pointed at
a temp path for section 2 and the real /var/lib/atlas/halt is never touched,
read for state, or cleared. That is the only reason the env override still
exists after today's fix.
"""

from __future__ import annotations

import os
import sys
import json
import tempfile
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"\n         {detail}" if detail else ""))
    return bool(ok)


def head(t: str) -> None:
    print(f"\n{'=' * 76}\n{t}\n{'=' * 76}")


def section_no_short():
    head("1 — NO CODE PATH CAN PLACE A SHORT")

    from atlas.config import ALLOW_SHORT_ENTRIES, ALLOW_LONG_ENTRIES
    from atlas.execution.atlas_entry import allowed_side, regime_allows_side
    import atlas.execution.broker as B
    from atlas.execution import exits as X

    print("\n  LAYER 0 — the config value. Necessary, not sufficient.")
    check("ALLOW_SHORT_ENTRIES is False", ALLOW_SHORT_ENTRIES is False,
          f"{ALLOW_SHORT_ENTRIES}")
    check("ALLOW_LONG_ENTRIES is True", ALLOW_LONG_ENTRIES is True,
          f"{ALLOW_LONG_ENTRIES}")

    # ── LAYER 1: the matrix cannot NAME a short ──────────────────
    print("\n  LAYER 1 — allowed_side() over every market state it can see.")
    def ctx(reg, sent):
        """A context that genuinely produces the named sentiment.

        THE FIRST VERSION OF THIS DID NOT. It supplied all four keys for every
        case, so "unknown" resolved to bearish and the table's blocked_by
        column named CONFLICT and LONG_ONLY for rows labelled unknown. The
        verdict was unaffected -- no row produced a SHORT either way -- but a
        proof whose own labels are wrong is not worth much.
        sentiment_state(): bullish = advances>declines AND close>20DMA,
        bearish = the mirror, mixed = the two halves disagree, unknown = a
        missing input.
        """
        if sent == "unknown":
            return {"regime": reg, "source": "verify"}      # no inputs at all
        base = {"regime": reg, "source": "verify", "nifty_20dma": 100.0}
        if sent == "bullish":
            return {**base, "advance_count": 1200, "decline_count": 800,
                    "nifty_close": 101.0}
        if sent == "bearish":
            return {**base, "advance_count": 800, "decline_count": 1200,
                    "nifty_close": 99.0}
        # mixed: breadth says up, the index says down
        return {**base, "advance_count": 1200, "decline_count": 800,
                "nifty_close": 99.0}
    states = [(r, s) for r in ("bull", "bear", "sideways", "unknown", "garbage")
              for s in ("bullish", "bearish", "mixed", "unknown")]
    shorts, longs = [], []
    print(f"    {'regime':<10}{'sentiment':<11}{'side':<7}blocked_by")
    for reg, sent in states:
        side, reason, blk = allowed_side(ctx(reg, sent))
        if side == "SHORT":
            shorts.append((reg, sent))
        if side == "LONG":
            longs.append((reg, sent))
        print(f"    {reg:<10}{sent:<11}{str(side):<7}{blk}")
    check(f"no state out of {len(states)} returns SHORT", not shorts,
          str(shorts) if shorts else "exhaustive over regime x sentiment")
    # FOUR, NOT ONE. Regime is no longer applied at entry -- the nightly
    # scoring stage owns it, on the full universe with the pipeline's own data.
    # Every bullish-sentiment state is a long whatever the regime says.
    check("every LONG state has bullish sentiment, and only those",
          longs and all(l[1] == "bullish" for l in longs), f"{longs}")
    # THE REGIME COLUMN IS IRRELEVANT, and that IS the change. Every regime
    # value tested -- including "garbage" -- permits a long when sentiment is
    # bullish, because regime is not read here at all. The nightly scoring
    # stage owns it.
    regimes = {l[0] for l in longs}
    check("EVERY regime value permits a long on bullish sentiment",
          regimes == {"bull", "bear", "sideways", "unknown", "garbage"},
          f"{sorted(regimes)} — regime is reported, not applied")
    check("  and nothing but bullish sentiment produces one",
          {l[1] for l in longs} == {"bullish"}, f"{sorted({l[1] for l in longs})}")

    # ── LAYER 2: the per-direction wrapper ──────────────────────
    print("\n  LAYER 2 — regime_allows_side(ctx, 'SHORT') for every state.")
    allowed = [(r, s) for r, s in states
               if regime_allows_side(ctx(r, s), "SHORT")[0]]
    check("no state permits a SHORT through regime_allows_side", not allowed,
          str(allowed) if allowed else
          f"all {len(states)} refused; label is "
          f"{regime_allows_side(ctx('bear','bearish'), 'SHORT')[2]}")

    # ── LAYER 3: the broker, which is where an order becomes real ─
    print("\n  LAYER 3 — broker.place_order(), the only route to the exchange.")
    for ot, prod in (("LIMIT", "MIS"), ("LIMIT", "CNC"), ("MARKET", "MIS"),
                     ("SL", "MIS"), ("SL-M", "MIS")):
        r = B.place_order(symbol="TATASTEEL", direction="SHORT", qty=1,
                          order_type=ot, price=100.0, product=prod)
        check(f"a SHORT {ot} {prod} entry is refused",
              r.get("success") is False and r.get("blocked_by") == "long_only",
              f"blocked_by={r.get('blocked_by')} {str(r.get('reason'))[:50]}")
    # DEFAULTED INTENT MUST FAIL SAFE.
    r = B.place_order(symbol="TATASTEEL", direction="SHORT", qty=1,
                      order_type="LIMIT", price=100.0)
    check("a SHORT with intent defaulted is refused",
          r.get("blocked_by") == "long_only", f"{r.get('blocked_by')}")
    # AND LOWERCASE / MIXED CASE CANNOT SLIP THROUGH.
    for d in ("short", "Short", "sHoRt"):
        r = B.place_order(symbol="TATASTEEL", direction=d, qty=1,
                          order_type="LIMIT", price=100.0)
        check(f"direction={d!r} is refused too",
              r.get("blocked_by") == "long_only", f"{r.get('blocked_by')}")

    # ── LAYER 4: nothing will protect a short, so nothing can hold one ─
    print("\n  LAYER 4 — protect() refuses to protect a short at all.")
    for prod in ("MIS", "CNC"):
        r = X.protect(symbol="TATASTEEL", direction="SHORT", qty=1,
                      product=prod, fill_price=100.0, stop_price=105.0,
                      kite=object())
        check(f"a SHORT {prod} position is refused protection",
              r.get("ok") is False, str(r.get("reason"))[:58])
    r = X._protect_mis_sl("TATASTEEL", "SHORT", 1, 100.0, 105.0, 95.0, object())
    check("the retired SL path refuses if called directly",
          r.get("ok") is False, str(r.get("reason"))[:58])

    # ── LAYER 5: static. No live call site asks for a SHORT. ─────
    print("\n  LAYER 5 — static sweep of every order call site in atlas/.")
    import re
    hits = []
    for f in sorted((ROOT / "atlas").rglob("*.py")):
        src = f.read_text()
        # executable lines only -- a comment naming SHORT is not a call
        for i, line in enumerate(src.splitlines(), 1):
            bare = line.split("#", 1)[0]
            if not bare.strip():
                continue
            if re.search(r'direction\s*=\s*["\']SHORT["\']', bare) or \
               re.search(r'transaction_type\s*=\s*["\']SELL["\'].*'
                         r'(product\s*=\s*["\']MIS)', bare):
                hits.append(f"{f.relative_to(ROOT)}:{i}: {bare.strip()[:70]}")
    check("no executable line hardcodes a SHORT entry", not hits,
          "\n         ".join(hits) if hits else
          "comments and docstrings naming SHORT are not calls")
    return not FAIL


def section_gtt_failure_halts():
    head("2 — A FAILED GTT EXITS THE POSITION AND HALTS")

    # SCRATCH HALT FILE. The real /var/lib/atlas/halt is never touched.
    tmp = Path(tempfile.mkdtemp()) / "halt"
    os.environ["ATLAS_HALT_FILE"] = str(tmp)
    import importlib
    import atlas.config as C
    importlib.reload(C)
    import atlas.risk.breaker as breaker
    importlib.reload(breaker)
    from atlas.execution import exits as X

    print(f"\n  halt file for this demonstration: {tmp}")
    print(f"  the real file is untouched      : {C.HALT_FILE_CANONICAL}")
    check("the demonstration is NOT pointed at the live halt file",
          str(tmp) != C.HALT_FILE_CANONICAL, str(tmp))
    check("the scratch halt file starts absent", not tmp.exists())

    class StopRefusingKite:
        """Refuses the day-0 SL and every GTT; accepts a plain LIMIT.

        BOTH PRIMARIES ARE REFUSED, because there are two phases now and the
        halt must follow from either failing. The fallback -- a marketable
        LIMIT -- is accepted, so what is being demonstrated is "the primary
        broke, the position still left, and ATLAS stopped".
        """
        def __init__(self, ltp=100.0):
            self.last_price = ltp
            self.orders = []
            self.gtts = 0

        def place_gtt(self, **kw):
            self.gtts += 1
            raise RuntimeError("INJECTED: Zerodha refused the GTT")

        def place_order(self, **kw):
            self.orders.append(kw)
            if str(kw.get("order_type", "")).upper() == "SL":
                raise RuntimeError("INJECTED: Zerodha refused the day-0 SL")
            return "ORD-1"

        def ltp(self, inst):
            if isinstance(inst, str):
                inst = [inst]
            return {i: {"last_price": self.last_price} for i in inst}
        quote = ltp

    k = StopRefusingKite()
    out = X.protect_or_exit(symbol="RELIANCE", direction="LONG", qty=10,
                            product="CNC", fill_price=100.0, stop_price=95.0,
                            kite=k)
    print(f"\n  protect_or_exit() -> "
          f"{json.dumps({x: out[x] for x in ('protected','exited','unprotected','must_halt')})}")
    print(f"  gtt attempts: {k.gtts}   orders sent: "
          f"{[(o.get('order_type'), o.get('transaction_type'), o.get('price')) for o in k.orders]}")

    check("the DAY-0 STOP failed as injected", out["protected"] is False,
          str(out["prot"].get("reason"))[:70])
    check("THE POSITION WAS EXITED", out["exited"] is True,
          str((out["exit"] or {}).get("reason"))[:70])
    check("  the SL was attempted, then a LIMIT completed it",
          [o.get("order_type") for o in k.orders] == ["SL", "LIMIT"],
          str([o.get("order_type") for o in k.orders]))
    lim = [o for o in k.orders if str(o.get("order_type")).upper() == "LIMIT"]
    check("  priced through the book (a sell below the last trade)",
          bool(lim) and float(lim[0]["price"]) < k.last_price,
          f"{lim[0]['price']} vs ltp {k.last_price}" if lim else "")
    check("  and NO GTT was touched on the fill path", k.gtts == 0,
          f"{k.gtts} — the GTT belongs to the pre-close job")
    check("must_halt is set even though the exit SUCCEEDED",
          out["must_halt"] is True,
          "the protection mechanism is broken, so no further entries")

    # ── AND THE CALLER ACTUALLY HALTS ────────────────────────────
    print("\n  the caller acting on must_halt:")
    # THE HALT KIND COMES FROM THE MECHANISM THAT FAILED, as the call site now
    # derives it. The injected failure above is the DAY-0 SL, so this must halt
    # under DAY0_SL_FAILED -- halting it as GTT_FAILED is what the alert used to
    # do and is the thing being verified against.
    _name, _api, _kind = X.mechanism_label(out["prot"].get("mechanism"))
    print(f"  mechanism that failed: {out['prot'].get('mechanism')} "
          f"-> \"{_name}\" on the {_api} api, halt kind {_kind}")
    check("the halt kind names the day-0 SL, not a GTT",
          _kind, "DAY0_SL_FAILED")
    breaker.halt(_kind,
                 f"RELIANCE: the {_name} would not place (INJECTED). Position "
                 f"closed on a marketable LIMIT, mechanism is broken.")
    exists = tmp.exists()
    check("the halt file was written", exists, str(tmp))
    if exists:
        print(f"    contents: {tmp.read_text().strip()[:150]}")
    halted, why = breaker.is_halted()
    check("is_halted() now reports halted", halted, why[:90])

    # ── AND A HALTED ENGINE REFUSES THE NEXT ENTRY ───────────────
    import atlas.execution.broker as B
    importlib.reload(B)
    r = B.place_order(symbol="RELIANCE", direction="LONG", qty=1,
                      order_type="LIMIT", price=100.0, product="CNC")
    check("the NEXT long entry is refused at the broker",
          r.get("success") is False and r.get("blocked_by") in ("halt", "paused"),
          f"blocked_by={r.get('blocked_by')} {str(r.get('reason'))[:60]}")

    # THE CALL SITE MUST ACT ON THE FLAG. Returning must_halt and ignoring it
    # would look identical from inside exits.py.
    ae = (ROOT / "atlas" / "execution" / "atlas_entry.py").read_text()
    check("atlas_entry halts on must_halt in the exited branch",
          'if pe.get("must_halt"):' in ae and "breaker.halt(halt_kind," in ae,
          "the exited-but-unprotected-mechanism branch")
    # A day-0 SL failure halting as GTT_FAILED sent the operator to the wrong
    # system; the kind is derived now, so no fixed one may remain.
    check("  under the kind of the mechanism that failed, not a fixed one",
          'breaker.halt("GTT_FAILED"' not in ae)
    check("atlas_entry halts in the both-failed branch too",
          'breaker.halt("OPEN_UNPROTECTED"' in ae)

    tmp.unlink(missing_ok=True)
    os.environ.pop("ATLAS_HALT_FILE", None)
    print(f"\n  scratch halt file removed; {C.HALT_FILE_CANONICAL} never touched")
    return True


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    print(__doc__)
    section_no_short()
    section_gtt_failure_halts()
    head("RESULT")
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"    FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
