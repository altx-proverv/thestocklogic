#!/usr/bin/env python3
"""
THE LOOP'S NON-NEGOTIABLES
==========================
Each of these is a bug this codebase has already had, asserted against the
market-hours engine rather than trusted to review.

    a cycle never treats "cannot read" as "nothing there"
    /pause stops ORDER PLACEMENT in a RUNNING loop -- not observation
    the window is IST on a UTC box
    shadow does not re-log the same symbol every cycle
    a failure alerts once, not 360 times

The last two are specific to running in a loop. Under the 09:37 cron a
duplicate SHADOW row and a repeated alert were both impossible; at 360 cycles a
day they are the default unless something stops them.

Everything is stubbed: no network, no broker, no Supabase.
"""

import os
import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ["ATLAS_HALT_FILE"] = "/tmp/atlas_loop_test_halt"
logging.basicConfig(level=logging.CRITICAL)

from atlas.signal import market_open as mo      # noqa: E402
from atlas.risk import breaker                  # noqa: E402


class Calls(list):
    """The calls enter_trade received, with the decision log attached.

    ONE RETURN VALUE CARRYING BOTH, because "was it evaluated" and "what was
    recorded" are the two halves of the watch-only contract and a test that can
    only see one of them cannot tell a halted-and-observing loop from a
    halted-and-blind one.
    """
    logged = ()

# stub() replaces mo.paused, so hold the real one to test it directly.
REAL_PAUSED = mo.paused

IST = timezone(timedelta(hours=5, minutes=30))
HALT = Path(os.environ["ATLAS_HALT_FILE"])

# publication_kind IS PART OF THE FIXTURE NOW. The loop tests it positively --
# only 'signal' is enterable -- because historical rows are deliberately NULL and
# the old `!= "signal"` with a default of "signal" would have made a NULL row
# tradeable. get_signals always supplies the key (setdefault in the
# missing-column path), so a row without it is not a production state; the case
# below asserts it fails SAFE anyway.
SIG = {"symbol": "TCS", "direction": "LONG", "entry_ref": 100.0,
       "entry_low": 99.5, "entry_high": 100.5, "sl": 96.0, "stop_pct": 4.0,
       "publication_kind": "signal"}


def fresh_state():
    HALT.unlink(missing_ok=True)
    breaker.reset()
    mo._alerted.clear()
    s = mo.Session()
    s.batch_date = "2026-09-11"
    s.zone_map = {("TCS", "LONG"): dict(SIG)}
    return s


def stub(paused=(False, "NORMAL"), committed=(True, set()),
         quotes=None, entered=None):
    """-> the list of (symbol, dry_run) pairs enter_trade was called with.

    dry_run IS PART OF THE RECORD. "was it evaluated" and "could it have placed
    an order" are different questions, and before 2026-10-08 this stub could
    only answer the first -- which is why a paused loop that evaluated nothing
    looked correct.
    """
    mo.paused = lambda: paused
    mo.load_zone_map = lambda state: True
    mo.committed_today = lambda: committed
    mo.fetch_quotes = lambda syms: ({"TCS": 100.0} if quotes is None else quotes)
    mo.push_live_zones = lambda st, rows, keys: {"rows": len(rows)}
    logged = []
    calls = Calls()
    mo.log_decision = lambda sig, res: logged.append(
        (sig.get("symbol"), res.get("status")))

    def _enter(sig, dry_run=False):
        calls.append((sig["symbol"], dry_run))
        if dry_run:
            return {"status": "WOULD_ENTER", "reason": "all gates passed"}
        return entered or {"status": "SHADOW_INTENT", "reason": ""}
    mo.enter_trade = _enter
    calls.logged = logged
    return calls


def main() -> int:
    ok = True
    sent = []
    mo.send = lambda text: sent.append(text)

    print("CANNOT READ IS NOT NOTHING THERE")
    print("-" * 78)

    # An unreadable ledger must skip the cycle, not evaluate against an empty
    # holdings set -- that is how a held symbol gets entered a second time.
    s = fresh_state()
    calls = stub(committed=(False, set()))
    r = mo.cycle(s)
    good = bool(r.get("error")) and not calls
    ok &= good
    print(f"  {'ledger unreadable -> cycle skipped':<46}"
          f"{'ok' if good else '** EVALUATED ANYWAY **'}  ({r.get('error')})")

    # No quotes must mean no candidates evaluated, not "nothing is near a zone".
    s = fresh_state()
    calls = stub(quotes={})
    r = mo.cycle(s)
    good = bool(r.get("error")) and not calls
    ok &= good
    print(f"  {'no quotes -> cycle skipped':<46}"
          f"{'ok' if good else '** EVALUATED ANYWAY **'}  ({r.get('error')})")

    print()
    print("/PAUSE STOPS A RUNNING LOOP")
    print("-" * 78)
    # THIS ASSERTION IS INVERTED FROM WHAT IT WAS, deliberately. It used to read
    # "paused mid-session -> nothing evaluated", and it passed: cycle() returned
    # on the pause check before the zone map was built. That is exactly the
    # 2026-10-08 failure -- ATLAS ran 360 cycles while halted and produced no
    # batch, no zones and no entry_log rows, so the one system whose job is to
    # know what the market did went blind for a session. A pause must stop ORDER
    # PLACEMENT, not observation.
    s = fresh_state()
    calls = stub(paused=(True, "operator PAUSED"))
    r = mo.cycle(s)
    evaluated = [c for c in calls]
    good = (bool(r.get("observe_only"))
            and len(evaluated) == 1
            and evaluated[0][1] is True          # dry_run
            and len(calls.logged) == 1
            and str(calls.logged[0][1]).startswith("WATCH_ONLY"))
    ok &= good
    print(f"  {'paused -> evaluated, logged, nothing placed':<46}"
          f"{'ok' if good else '** ' + str(evaluated) + str(calls.logged) + ' **'}")
    print(f"  {'  and the entry call was dry_run=True':<46}"
          f"{'ok' if evaluated and evaluated[0][1] is True else '** LIVE CALL **'}")
    print(f"  {'  and the decision says WATCH_ONLY':<46}"
          f"{'ok' if calls.logged and str(calls.logged[0][1]).startswith('WATCH_ONLY') else '** ' + str(calls.logged) + ' **'}")

    # AND THE LIVE PATH IS UNCHANGED: not paused means a real call.
    s = fresh_state()
    calls = stub(paused=(False, "NORMAL"))
    r = mo.cycle(s)
    good2 = len(calls) == 1 and calls[0][1] is False
    ok &= good2
    print(f"  {'not paused -> the entry call is live':<46}"
          f"{'ok' if good2 else '** ' + str(list(calls)) + ' **'}")

    s = fresh_state()
    calls = stub(paused=(True, "operator PAUSED"))
    r = mo.cycle(s)
    good = bool(r.get("observe_only"))
    ok &= good
    print(f"  {'the cycle reports why it is observing':<46}"
          f"{'ok' if good else '** KEPT TRADING **'}")

    # The two below exercise the REAL paused(), which stub() replaces -- keep a
    # reference before stubbing or the test measures its own stub.
    HALT.unlink(missing_ok=True)
    breaker.reset()

    # Unreadable agent state counts as paused: not knowing whether the operator
    # halted us is not permission to trade.
    mo.get_agent_state = lambda: (_ for _ in ()).throw(
        mo.RiskDataUnavailable("supabase down"))
    is_p, why = REAL_PAUSED()
    ok &= is_p
    print(f"  {'agent state unreadable -> paused':<46}"
          f"{'ok' if is_p else '** TRADED ON UNKNOWN STATE **'}  ({why[:30]})")

    # A self-halt stops the loop too, and survives the process.
    mo.get_agent_state = lambda: {"mode": "NORMAL"}
    HALT.write_text("halted for test")
    is_p, why = REAL_PAUSED()
    ok &= is_p
    print(f"  {'self-halt sentinel -> paused':<46}"
          f"{'ok' if is_p else '** IGNORED THE HALT **'}  ({why[:30]})")
    HALT.unlink(missing_ok=True)

    # And a healthy state does NOT pause, or the test above proves nothing.
    is_p, why = REAL_PAUSED()
    ok &= not is_p
    print(f"  {'healthy state -> runs':<46}"
          f"{'ok' if not is_p else '** PAUSED WHEN IT SHOULD NOT **'}  ({why[:30]})")

    print()
    print("THE WINDOW IS IST, ON A BOX THAT RUNS UTC")
    print("-" * 78)
    checks = [("09:19 IST", datetime(2026, 9, 11, 9, 19, tzinfo=IST), False),
              ("09:20 IST", datetime(2026, 9, 11, 9, 20, tzinfo=IST), True),
              ("12:00 IST", datetime(2026, 9, 11, 12, 0, tzinfo=IST), True),
              ("15:19 IST", datetime(2026, 9, 11, 15, 19, tzinfo=IST), True),
              ("15:20 IST", datetime(2026, 9, 11, 15, 20, tzinfo=IST), False)]
    for label, dt, want in checks:
        got = mo.in_window(dt)
        good = got == want
        ok &= good
        print(f"  {label:<46}{'in' if got else 'out':<6}"
              f"{'ok' if good else '** WRONG **'}")

    # The trap itself. 04:00 UTC is 09:30 IST -- inside the trading window. A
    # naive clock on this box reads 04:00, compares it to 09:20-15:20 and says
    # closed, so the engine would sit out the morning. That is not
    # hypothetical: three of four scheduled upstox_ws windows matched nothing
    # for exactly this reason and live_prices was written once a day.
    utc_moment = datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc)
    naive_says = mo.WINDOW_START <= utc_moment.time() < mo.WINDOW_END
    ist_says = mo.in_window(utc_moment.astimezone(IST))
    good = (not naive_says) and ist_says
    ok &= good
    print(f"  {'04:00 UTC = 09:30 IST':<46}"
          f"{'in' if ist_says else 'out':<6}"
          f"{'ok — naive clock would say closed' if good else '** TRAP NOT COVERED **'}")

    print()
    print("SHADOW DOES NOT RE-LOG THE SAME SYMBOL EVERY CYCLE")
    print("-" * 78)
    # committed_today folds in today's SHADOW rows, so once logged the symbol
    # is skipped. Without that this is 360 rows per symbol per day.
    s = fresh_state()
    calls = stub(committed=(True, {("TCS", "LONG")}))
    mo.cycle(s)
    good = not calls
    ok &= good
    print(f"  {'already shadowed today -> skipped':<46}"
          f"{'ok' if good else '** RE-LOGGED **'}")

    # ONLY 'signal' IS ENTERABLE, and the unsafe directions are the ones worth
    # asserting: a candidate must not be entered, and neither must a row whose
    # kind is NULL or missing -- that is what the old default got wrong.
    for kind, want_entered in (("signal", True), ("candidate", False),
                               (None, False), ("", False), ("__absent__", False)):
        s = fresh_state()
        row = dict(SIG)
        if kind == "__absent__":
            row.pop("publication_kind")
        else:
            row["publication_kind"] = kind
        s.zone_map = {("TCS", "LONG"): row}
        calls = stub(committed=(True, set()))
        mo.cycle(s)
        got = list(calls) == [("TCS", False)]
        good = got == want_entered
        ok &= good
        label = "absent" if kind == "__absent__" else repr(kind)
        print(f"  {('publication_kind ' + label + ' -> entered'):<46}"
              f"{'ok' if good else '** want ' + str(want_entered) + ' **'}")

    s = fresh_state()
    calls = stub(committed=(True, set()))
    mo.cycle(s)
    good = list(calls) == [("TCS", False)]
    ok &= good
    print(f"  {'not yet shadowed -> evaluated once':<46}"
          f"{'ok' if good else '** ' + str(calls) + ' **'}")

    print()
    print("A FAILURE ALERTS ONCE, NOT 360 TIMES")
    print("-" * 78)
    mo._alerted.clear()
    sent.clear()
    for _ in range(50):
        mo.alert("NO PRICES", "Upstox returned nothing", key="upstox")
    good = len(sent) == 1
    ok &= good
    print(f"  {'50 identical failures -> alerts sent':<46}{len(sent):<6}"
          f"{'ok' if good else '** FLOOD **'}")

    sent.clear()
    mo.alert("NO PRICES", "a", key="upstox")
    mo.alert("LEDGER UNREADABLE", "b", key="ledger")
    mo.alert("NO PRICES", "c", key="TCS")
    good = len(sent) == 2
    ok &= good
    print(f"  {'different kinds/keys still get through':<46}{len(sent):<6}"
          f"{'ok' if good else '** SUPPRESSED TOO MUCH **'}")

    print()
    print("THE WINDOW IS A TRADING DAY, NOT JUST A TIME OF DAY")
    print("-" * 78)
    # A holiday is the WORST day to run: the batch is one day old, which
    # MAX_BATCH_AGE_DAYS=5 does not call stale, and Upstox returns the previous
    # close -- prices sitting exactly where the zones were computed, which is
    # what near_zone looks for. It would find the most candidates it ever finds
    # and send them into a closed market. Mon-Fri on the timer cannot see this;
    # Diwali is a Thursday.
    sys.path.insert(0, str(ROOT / "engine"))
    from trading_calendar import is_trading_day, NSE_HOLIDAYS   # noqa: E402

    midday = lambda d: datetime(d.year, d.month, d.day, 12, 0, tzinfo=IST)
    from datetime import date as _date
    holidays = sorted(h for h in NSE_HOLIDAYS
                      if _date.fromisoformat(h).weekday() < 5)
    weekday_holiday = _date.fromisoformat(holidays[len(holidays) // 2])
    good = not mo.in_window(midday(weekday_holiday))
    ok &= good
    print(f"  {'NSE holiday on a weekday, 12:00 IST':<46}"
          f"{'out' if good else 'IN':<6}"
          f"{'ok (' + weekday_holiday.isoformat() + ')' if good else '** WOULD TRADE A CLOSED MARKET **'}")

    sat = _date(2026, 9, 12)
    while sat.weekday() != 5:
        sat = _date(sat.year, sat.month, sat.day + 1)
    good = not mo.in_window(midday(sat))
    ok &= good
    print(f"  {'Saturday, 12:00 IST':<46}{'out' if good else 'IN':<6}"
          f"{'ok' if good else '** WRONG **'}")

    open_day = _date(2026, 9, 14)
    while not is_trading_day(open_day):
        open_day = _date(open_day.year, open_day.month, open_day.day + 1)
    good = mo.in_window(midday(open_day))
    ok &= good
    print(f"  {'a real trading day, 12:00 IST':<46}{'in' if good else 'OUT':<6}"
          f"{'ok (' + open_day.isoformat() + ')' if good else '** WOULD SIT OUT **'}")

    print()
    print("THE SERVICE REFUSES TO START DEAF")
    print("-" * 78)
    # telegram.send() logs "not configured" and returns, so a missing token
    # fails nothing and silences every alert for the session. Under systemd,
    # which never sees the crontab header, that is the default unless
    # /etc/atlas.env supplies it.
    saved = {k: os.environ.get(k) for k, _ in mo.REQUIRED_ENV}
    try:
        for k, _ in mo.REQUIRED_ENV:
            os.environ[k] = "set"
        good = mo.preflight() == []
        ok &= good
        print(f"  {'all env present -> starts':<46}"
              f"{'ok' if good else '** BLOCKED WRONGLY **'}")

        for missing in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                        "SUPABASE_SERVICE_KEY"):
            for k, _ in mo.REQUIRED_ENV:
                os.environ[k] = "set"
            os.environ.pop(missing)
            names = [n for n, _ in mo.preflight()]
            good = names == [missing]
            ok &= good
            print(f"  {'missing ' + missing + ' -> refuses':<46}"
                  f"{'ok' if good else '** STARTED ANYWAY: ' + str(names) + ' **'}")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    HALT.unlink(missing_ok=True)
    print("-" * 78)
    print("MARKET-HOURS LOOP:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
