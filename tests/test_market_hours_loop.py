#!/usr/bin/env python3
"""
THE LOOP'S NON-NEGOTIABLES
==========================
Each of these is a bug this codebase has already had, asserted against the
market-hours engine rather than trusted to review.

    a cycle never treats "cannot read" as "nothing there"
    /pause stops a RUNNING loop, not just the next start
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

# stub() replaces mo.paused, so hold the real one to test it directly.
REAL_PAUSED = mo.paused

IST = timezone(timedelta(hours=5, minutes=30))
HALT = Path(os.environ["ATLAS_HALT_FILE"])

SIG = {"symbol": "TCS", "direction": "LONG", "entry_ref": 100.0,
       "entry_low": 99.5, "entry_high": 100.5, "sl": 96.0, "stop_pct": 4.0}


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
    mo.paused = lambda: paused
    mo.load_zone_map = lambda state: True
    mo.committed_today = lambda: committed
    mo.fetch_quotes = lambda syms: ({"TCS": 100.0} if quotes is None else quotes)
    mo.log_decision = lambda sig, res: None
    calls = []

    def _enter(sig):
        calls.append(sig["symbol"])
        return entered or {"status": "SHADOW_INTENT", "reason": ""}
    mo.enter_trade = _enter
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
    s = fresh_state()
    calls = stub(paused=(True, "operator PAUSED"))
    r = mo.cycle(s)
    good = bool(r.get("paused")) and not calls
    ok &= good
    print(f"  {'paused mid-session -> nothing evaluated':<46}"
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

    s = fresh_state()
    calls = stub(committed=(True, set()))
    mo.cycle(s)
    good = calls == ["TCS"]
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
