#!/usr/bin/env python3
"""
THE THREE RULINGS OF 2026-10-16
===============================

Each of these reverses behaviour this codebase shipped, and each reversal is
the kind that looks like a regression to anyone reading the diff without the
ruling. So they are asserted here, with the ruling named.

  1. A POSITION THAT CANNOT BE PROTECTED IS KEPT, not closed.
     ATLAS takes the trade and alerts until a human places a stop. It used to
     fire a marketable LIMIT and then halt the account.

  2. ATLAS DOES NOT SWITCH ITSELF OFF.
     A condition that used to stand the day down blocks ONE SYMBOL for ONE
     SESSION and alerts, naming how to pause. On 2026-10-15 ATHERENERG's tick
     size set the account halt and 443 published rows were refused the next
     morning in every sector.

  3. PROTECTION IS DECIDED BY THE BROKER, NOT BY entry_date OR exit_legs.
     An OPEN position with no stop at the broker gets one placed whatever its
     age. exit_legs is empty precisely when placement failed, so a repair that
     reads it concludes "nothing to do" from the fingerprint of the bug.

Everything is stubbed: no network, no broker, no Supabase.
"""

import os
import sys
import json
import logging
import tempfile
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.gettempdir())
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ["ATLAS_DRILL"] = "1"
os.environ["ATLAS_HALT_FILE"] = str(TMP / "atlas_unprot_test_halt")
os.environ["ATLAS_SYMBOL_BLOCK_FILE"] = str(TMP / "atlas_unprot_test_block.json")
os.environ["ATLAS_UNPROTECTED_FILE"] = str(TMP / "atlas_unprot_test_reg.json")

IST = timezone(timedelta(hours=5, minutes=30))
_day = datetime.now(IST).date().isoformat()
_TICKS = {"ATHERENERG": 0.10, "ACC": 0.10, "RELIANCE": 0.05}
_tm = TMP / "atlas_unprot_test_tickmap.json"
_tm.write_text(json.dumps({"days": {_day: _TICKS}, "last_known": _TICKS}))
os.environ["ATLAS_TICK_MAP_FILE"] = str(_tm)

logging.basicConfig(level=logging.CRITICAL)

from atlas.execution import exits as X                    # noqa: E402
from atlas.execution import protection as PR              # noqa: E402
from atlas.risk import symbol_block as SB                 # noqa: E402

ENTRY = 1453.70
STOP = 1418.86          # NOT a multiple of ATHERENERG's 0.10 tick, verbatim
                        # from row 22 -- the value the broker refused.


def _trade(**kw):
    t = {"id": 22, "symbol": "ATHERENERG", "direction": "LONG", "qty": 7,
         "entry_price": ENTRY, "stop_price": STOP, "product": "CNC",
         "entry_date": "2026-10-15", "exit_legs": {}}
    t.update(kw)
    return t


class Kite:
    """Records. Places nothing. `fail` names the order types it refuses."""

    def __init__(self, fail=(), orders=None, gtts=None):
        self.fail = tuple(fail)
        self.placed = []
        self.gtts_placed = []
        self._orders = orders or []
        self._gtts = gtts or []

    def place_order(self, **kw):
        if str(kw.get("order_type", "")).upper() in self.fail:
            raise RuntimeError(f"stub refuses {kw.get('order_type')}")
        self.placed.append(kw)
        return f"O{len(self.placed)}"

    def place_gtt(self, **kw):
        if "GTT" in self.fail:
            raise RuntimeError("stub refuses GTT")
        self.gtts_placed.append(kw)
        return {"trigger_id": 70000 + len(self.gtts_placed)}

    def ltp(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        return {k: {"last_price": ENTRY} for k in keys}

    def orders(self):
        return self._orders

    def get_gtts(self):
        return self._gtts

    def positions(self):
        return {"net": [{"tradingsymbol": "ATHERENERG", "quantity": 7}]}


def _reset():
    SB.reset_for_tests()
    Path(os.environ["ATLAS_UNPROTECTED_FILE"]).unlink(missing_ok=True)


def main() -> int:
    ok = True

    def check(label, got, want, note=""):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {label:<58}{str(got):<10}"
              f"{'ok' if good else '** want ' + str(want) + ' **':<20}{note}")
        return good

    print("1 — A FAILED STOP KEEPS THE POSITION AND DOES NOT HALT")
    print("-" * 100)
    _reset()
    k = Kite(fail=("SL",))
    pe = X.protect_or_flag(symbol="ATHERENERG", direction="LONG", qty=7,
                           product="CNC", fill_price=ENTRY, stop_price=STOP,
                           kite=k)
    check("the stop failed", pe["protected"], False)
    check("the position was NOT exited", pe["exited"], False)
    check("must_halt is False", pe["must_halt"], False)
    check("unprotected is reported", pe["unprotected"], True)
    check("the wanted stop is carried for the alert",
          pe.get("wanted_stop"), STOP)
    check("ONLY the SL was attempted",
          [o["order_type"] for o in k.placed], [],
          "the refused SL never landed; no fallback LIMIT was sent")

    print()
    print("2 — THE BLOCK IS ONE SYMBOL, FOR ONE SESSION")
    print("-" * 100)
    _reset()
    sent = []
    SB.block("ATHERENERG", SB.REASON_PROTECTION_FAILED, "tick size refused",
             send=lambda t: sent.append(t) or True)
    check("the symbol is blocked", SB.is_blocked("ATHERENERG")[0], True)
    check("  with a named reason",
          SB.is_blocked("ATHERENERG")[1], SB.REASON_PROTECTION_FAILED)
    check("ANOTHER symbol is NOT blocked", SB.is_blocked("TCS")[0], False,
          "one stock's tick size took 443 rows off the table on 2026-10-16")
    check("it alerted once", len(sent), 1)
    check("  and the alert says how to pause",
          "/pause" in sent[0], True)
    check("  and says the day is not stood down",
          "NOT stood down" in sent[0], True)
    SB.block("ATHERENERG", SB.REASON_PROTECTION_FAILED, "again",
             send=lambda t: sent.append(t) or True)
    check("blocking twice alerts once", len(sent), 1)

    # IT DOES NOT SURVIVE THE SESSION. The file is keyed to the session date;
    # a file from another day is ignored rather than inherited.
    stale = {"session_date": "2026-01-01",
             "symbols": {"ATHERENERG": {"reason": "old", "detail": "old"}}}
    Path(os.environ["ATLAS_SYMBOL_BLOCK_FILE"]).write_text(json.dumps(stale))
    check("yesterday's block does not apply today",
          SB.is_blocked("ATHERENERG")[0], False,
          "a fresh morning starts live unless the operator paused it")

    print()
    print("3 — THE ALERT REPEATS UNTIL THE BROKER CONFIRMS A STOP")
    print("-" * 100)
    _reset()
    e1 = PR.register(_trade(), "tick size refused", wanted_stop=STOP)
    check("registered", PR.seen_today(), 1)
    check("  and it is the first of the day", e1["ordinal"], 1)
    sends = []
    # Nothing resting at the broker -> it stays registered and re-alerts.
    prot_none = {"ok": True, "why": "", "orders": {}, "gtts": {}}
    sw = PR.sweep([_trade()], prot=prot_none, send=lambda t: sends.append(t) or True)
    check("it alerted", sw["alerted"], 1)
    check("  the alert carries the running count",
          "1 of 1 today" in sends[0], True)
    check("  and names the repeat interval",
          f"{PR.UNPROTECTED_REPEAT_SECONDS // 60} minutes" in sends[0], True)
    check("  and tells the operator how to pause", "/pause" in sends[0], True)

    # IMMEDIATELY AGAIN IS NOT DUE. The repeat is throttled, not per cycle --
    # at 360 cycles a day an unthrottled repeat is 360 messages.
    sends.clear()
    sw = PR.sweep([_trade()], prot=prot_none, send=lambda t: sends.append(t) or True)
    check("a second sweep seconds later does NOT re-alert", len(sends), 0)
    check("  but it is still outstanding", sw["outstanding"], 1)

    # AND IT IS DUE AGAIN AFTER THE INTERVAL.
    later = datetime.now(IST) + timedelta(
        seconds=PR.UNPROTECTED_REPEAT_SECONDS + 1)
    sends.clear()
    sw = PR.sweep([_trade()], prot=prot_none,
                  send=lambda t: sends.append(t) or True, now=later)
    check("after the interval it alerts again", len(sends), 1,
          "a single alert that goes quiet is how ATHERENERG sat naked")

    # A CONFIRMED STOP STOPS THE ALERT -- including one placed by hand.
    sends.clear()
    prot_live = {"ok": True, "why": "", "orders": {},
                 "gtts": {"ATHERENERG": [{"trigger_id": 70001,
                                          "type": "two-leg",
                                          "triggers": [1417.4, 1523.3],
                                          "last_price": ENTRY, "legs": []}]}}
    sw = PR.sweep([_trade()], prot=prot_live,
                  send=lambda t: sends.append(t) or True, now=later)
    check("a CONFIRMED stop resolves it", sw["resolved"], 1)
    check("  and nothing is outstanding", sw["outstanding"], 0)
    check("  but the day's COUNT is not decremented", PR.seen_today(), 1,
          "two failures fixed is still a day protection failed twice")

    # AN UNREADABLE BROKER IS NOT A CONFIRMATION.
    _reset()
    PR.register(_trade(), "tick size refused")
    sw = PR.sweep([_trade()], prot={"ok": False, "why": "token expired",
                                    "orders": {}, "gtts": {}},
                  send=lambda t: None, now=later)
    check("an unreadable broker does NOT resolve it", sw["resolved"], 0,
          "UNKNOWN is not protected")

    print()
    print("4 — THE REPAIR READS THE BROKER, NOT entry_date OR exit_legs")
    print("-" * 100)
    _reset()
    # THE EXACT ATHERENERG CASE: entered yesterday, exit_legs empty, nothing
    # resting. Before 2026-10-16 every path reported success and placed
    # nothing.
    k = Kite()
    r = PR.ensure_protected(_trade(), kite=k,
                            now_ist=datetime(2026, 10, 16, 11, 0, tzinfo=IST))
    check("an older unprotected position gets a GTT",
          r["action"], "placed_gtt",
          "its shares have settled, and a GTT checks demat")
    check("  and it succeeded", r["ok"], True)
    check("  exit_legs was empty and did NOT mean 'nothing to do'",
          len(k.gtts_placed) > 0, True)

    # ENTERED TODAY, MID-SESSION -> the day-0 SL, because the shares have not
    # reached demat and a GTT could be accepted and then rejected for want of
    # holdings.
    _reset()
    k = Kite()
    r = PR.ensure_protected(_trade(entry_date="2026-10-16"), kite=k,
                            now_ist=datetime(2026, 10, 16, 11, 0, tzinfo=IST))
    check("a position entered TODAY gets a day-0 SL",
          r["action"], "placed_day0_sl")
    check("  as a regular order, not a GTT", len(k.gtts_placed), 0)

    # AT THE PRE-CLOSE it is a GTT either way: a day order placed at 15:20 is
    # flushed at 16:00, which is the window the handover exists to close.
    _reset()
    k = Kite()
    r = PR.ensure_protected(_trade(entry_date="2026-10-16"), kite=k,
                            now_ist=datetime(2026, 10, 16, 15, 20, tzinfo=IST),
                            at_preclose=True)
    check("at the pre-close, today's position gets a GTT too",
          r["action"], "placed_gtt",
          "a day order placed at 15:20 dies at 16:00")

    # ALREADY PROTECTED -> NOTHING. The repair must be idempotent or a second
    # cycle stacks a second stop on a live one.
    _reset()
    k = Kite()
    r = PR.ensure_protected(_trade(), kite=k, prot=prot_live)
    check("an already-protected position is left alone", r["action"], "none")
    check("  and nothing was placed",
          len(k.placed) + len(k.gtts_placed), 0)

    # AN UNREADABLE BROKER -> REFUSE, and report not-ok so the caller alerts.
    _reset()
    k = Kite()
    r = PR.ensure_protected(_trade(), kite=k,
                            prot={"ok": False, "why": "token expired",
                                  "orders": {}, "gtts": {}})
    check("an unreadable broker refuses to place", r["action"],
          "refused_unreadable", "cannot tell a missing stop from an unread one")
    check("  and reports not-ok so the caller alerts", r["ok"], False)
    check("  and placed nothing", len(k.placed) + len(k.gtts_placed), 0)

    # AND THE PRICE IT PLACES IS ON TICK. 1418.86 is not a multiple of 0.10 --
    # that is the value the broker refused four times on 2026-10-15.
    _reset()
    k = Kite()
    PR.ensure_protected(_trade(), kite=k,
                        now_ist=datetime(2026, 10, 16, 11, 0, tzinfo=IST))
    trigs = []
    for g in k.gtts_placed:
        for t in (g.get("trigger_values") or []):
            trigs.append(float(t))
    on_tick = all(abs(round(t / 0.10) - t / 0.10) < 1e-6 for t in trigs)
    check("every repaired trigger is on the 0.10 tick", on_tick, True,
          f"{trigs} from a recorded stop of {STOP}")
    check("  and the raw recorded stop was NOT",
          abs(round(STOP / 0.10) - STOP / 0.10) < 1e-6, False,
          "so the tick path, not the row, is what made it placeable")

    print()
    print("5 — trail() NO LONGER REPORTS OK WHEN NOTHING IS PROTECTING")
    print("-" * 100)
    from atlas.execution import preclose_gtt as PG
    # NOTHING RESTING -> a FAILURE that the caller repairs. This returned
    # {"ok": True, "moved": False} and that single line is what let ATHERENERG
    # go a session and a night naked.
    r = PG.trail(_trade(), Kite(), date(2026, 10, 16))
    check("no recorded leg AND no broker stop -> not ok", r["ok"], False)
    check("  it says it needs protection", r.get("needs_protection"), True)
    check("  and the reason names the state plainly",
          "NOTHING IS PROTECTING THIS POSITION" in r["reason"], True)

    # PROTECTED BY SOMETHING WE DID NOT RECORD -> genuinely nothing to do.
    # This is the case the old early return was accidentally right about, and
    # it has to stay right or every hand-placed stop becomes a false alarm.
    k = Kite(gtts=[{"id": 70001, "status": "active", "type": "two-leg",
                    "condition": {"tradingsymbol": "ATHERENERG",
                                  "trigger_values": [1417.4, 1523.3],
                                  "last_price": ENTRY},
                    "orders": []}])
    r = PG.trail(_trade(), k, date(2026, 10, 16))
    check("no recorded leg but a CONFIRMED stop -> ok", r["ok"], True)
    check("  and it moved nothing", r["moved"], False)

    print()
    print("-" * 100)
    print("UNPROTECTED ENTRY:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
