"""
THE STOCK LOGIC — FRESHNESS: a value that does not change is not a reading
==========================================================================

WHY THIS MODULE EXISTS, in one sentence: a counter that is reported but not
differenced is not a heartbeat.

2026-10-20 cost a whole session to that. Two independent feeds froze and both
reported health:

  rbe_engine      the WebSocket connected at 09:14 IST, subscribed 498 tokens,
                  received exactly 498 ticks -- one per token, the opening
                  snapshot -- and never another. The heartbeat logged
                  "Heartbeat — ticks: 498" 360 times. The line was printed, the
                  number was true, and nothing compared it with the previous
                  one. Monday had delivered 5,770,958.

  market_open     the NIFTY futures quote read 22894.00 in cycle 1 and
                  22894.00 in cycle 360, six hours later, with session VWAP
                  frozen at 22991.18 throughout. The engine made 342 entry
                  decisions against it.

THE TIMESTAMP CHECK DID NOT CATCH IT, and that is the part worth keeping in
mind. live_sentiment already computed an age from the quote's own timestamp and
already refused a quote past LIVE_SENTIMENT_MAX_AGE_SECONDS. Every one of those
360 cycles logged "[quote 0.0s old]". Upstox stamped a current timestamp onto a
price that had not moved since the open, so an age computed from the provider's
clock said "fresh" about a dead feed for six hours.

So freshness cannot be delegated to whoever is supplying the data. The only
test that survives a lying timestamp is differencing the VALUE: did the number
change between two observations we made ourselves.

WHAT THIS IS NOT. It is not a liveness check on the market. A market can be
genuinely quiet; a price can legitimately repeat. That is why the threshold
below is derived from how often a repeat actually happens on a live day rather
than assumed, and why the monitor arms only after it has seen one real change
(see Monitor.observe) -- a feed that has not started yet is not a feed that has
died.
"""

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("ATLAS-FRESHNESS")

IST = timezone(timedelta(hours=5, minutes=30))


# ── THE THRESHOLD, DERIVED ────────────────────────────────────────
#
# MEASURED, NOT CHOSEN. reports/rbe.log carries one cumulative tick count per
# 60-second heartbeat since 2026-06-12. Differencing consecutive heartbeats
# gives the per-interval tick delta for every session on the box:
#
#   live sessions in the log                                            47
#   of those, sessions with ANY 60s interval at zero new ticks           0
#   smallest per-interval delta ever observed on a live session      3,156
#                                                        (2026-08-31)
#   Monday 2026-10-19      min 12,075   p1 14,432   median 15,983   max 17,709
#   Tuesday 2026-10-20     360 of 360 intervals at zero, cumulative stuck at 498
#
# Six early sessions (2026-06-12 to 2026-08-06) parse as having zero-deltas.
# They do not: those days wrote every heartbeat line twice (722 lines = 361 x 2,
# two handlers attached before the StreamHandler-only fix noted in
# rbe_engine.py), so the "zero" is the gap between a duplicated pair. Excluding
# them changes nothing -- the other 41 sessions contain no zero interval either.
#
# So a single 60-second interval with no new ticks has NEVER happened on a live
# session. Any N >= 1 is already above the observed maximum of 0. N = 3 is
# three intervals beyond anything ever recorded, which buys margin against one
# transient -- a dropped poll, a heartbeat landing between batches -- while
# bounding detection at three minutes. Tuesday would have been caught at
# 09:18 IST instead of running to 15:15.
#
# IF THE SAMPLING INTERVAL CHANGES, THIS NUMBER IS NO LONGER DERIVED. The
# distribution above is measured on 60-second intervals. A monitor sampling
# every 5 seconds would see genuine quiet far more often and needs its own
# measurement, not this constant.
MAX_IDENTICAL_INTERVALS = 3

# The interval the threshold above was measured on. A caller sampling at a
# different cadence must say so, and gets told it is off-evidence.
DERIVED_FOR_INTERVAL_SECONDS = 60


class Monitor:
    """Consecutive observations of an unchanged value. Never raises.

    USAGE IS TWO LINES: construct once, call observe() once per sampling
    interval with whatever number is supposed to be moving. The return says
    whether the thing has stopped.

        mon = Monitor("rbe ticks")
        ...
        verdict = mon.observe(engine.tick_count)
        if verdict["stale"]:
            ...stand down...

    ARMING, AND WHY IT IS A BASELINE RATHER THAN A TRANSITION. A monitor that
    has never seen data is NOT stale, it is unstarted -- standing the day down
    at 09:15 every morning would be worse than the bug being fixed. So the
    monitor has to tell those apart.

    THE FIRST VERSION OF THIS GOT IT WRONG AND THE PROOF CAUGHT IT. It armed
    only on OBSERVING a change, which fails on the exact sequence it was built
    for: on 2026-10-20 the 498 opening ticks landed before the heartbeat's first
    observation, so the monitor's first sample was already 498, every later
    sample was also 498, no change was ever observed, and it would have sat
    unarmed and silent for the whole session -- reproducing the original defect
    inside the guard meant to catch it. The unit test passed because it fed the
    monitor observe(0) then observe(498) by hand, a sequence the production loop
    never produces.

    `baseline` is the value that means "nothing has arrived yet" -- 0 for a tick
    counter, an empty map's fingerprint for a quote set. Anything different from
    it is proof the feed has produced data, whether or not the monitor watched
    it happen. A change still arms too, for values with no natural baseline.
    """

    def __init__(self, name: str, max_identical: int = MAX_IDENTICAL_INTERVALS,
                 interval_seconds: int = DERIVED_FOR_INTERVAL_SECONDS,
                 baseline=None):
        self.name = str(name)
        self.max_identical = max(1, int(max_identical))
        self.interval_seconds = int(interval_seconds)
        self.baseline = baseline    # the "nothing yet" value, if there is one
        self.last = None            # last observed value
        self.identical = 0          # consecutive observations with no change
        self.armed = False          # has the feed ever produced data?
        self.observations = 0
        self.changes = 0
        self.first_identical_at = None
        if self.interval_seconds != DERIVED_FOR_INTERVAL_SECONDS:
            # Say it once, loudly. The constant is evidence from 60s intervals
            # and nothing else; borrowing it for another cadence is a guess
            # wearing a measurement's clothes.
            log.warning(
                f"{self.name}: MAX_IDENTICAL_INTERVALS was derived for "
                f"{DERIVED_FOR_INTERVAL_SECONDS}s intervals but this monitor "
                f"samples every {self.interval_seconds}s. The threshold is not "
                f"evidence at this cadence — measure it or set max_identical "
                f"explicitly.")

    def observe(self, value, at=None) -> dict:
        """Record one sample. -> verdict dict; never raises."""
        now = at or datetime.now(IST)
        self.observations += 1
        try:
            first = self.last is None
            changed = (not first) and value != self.last

            # ARM ON EVIDENCE THAT DATA EXISTS, not on watching it arrive.
            # Either the value has moved, or it already differs from the
            # "nothing yet" baseline -- the second is what makes the 2026-10-20
            # sequence detectable, where the only change predated the first
            # observation.
            if changed or (self.baseline is not None and value != self.baseline):
                self.armed = True

            if changed:
                self.changes += 1
                self.identical = 0
                self.first_identical_at = None
                self.last = value
                return self.state(now)

            self.last = value
            if first:
                # A baseline sample is not yet an identical INTERVAL -- there is
                # no previous observation to have been identical to.
                return self.state(now)
            self.identical += 1
            if self.first_identical_at is None:
                self.first_identical_at = now
        except Exception as e:
            # A broken comparison must never take the caller down. Report
            # not-stale: this module's job is to detect a dead feed, and
            # failing closed here would stand the day down on a type error.
            log.error(f"{self.name}: could not compare observation ({e})")
        return self.state(now)

    def state(self, at=None) -> dict:
        stale = bool(self.armed and self.identical >= self.max_identical)
        return {
            "name": self.name,
            "stale": stale,
            "armed": self.armed,
            "identical": self.identical,
            "max_identical": self.max_identical,
            "observations": self.observations,
            "changes": self.changes,
            "value": self.last,
            "since": (self.first_identical_at.isoformat(timespec="seconds")
                      if self.first_identical_at else None),
            "detail": self._detail(stale),
        }

    def _detail(self, stale: bool) -> str:
        if not self.armed:
            return (f"{self.name}: not armed yet — no data has arrived "
                    f"({self.observations} observation(s), still at the "
                    f"baseline {self.baseline!r})")
        if not stale:
            return (f"{self.name}: moving — {self.changes} change(s) in "
                    f"{self.observations} observation(s), {self.identical} "
                    f"identical now")
        secs = self.identical * self.interval_seconds
        return (f"{self.name}: UNCHANGED at {self.last!r} for "
                f"{self.identical} consecutive {self.interval_seconds}s "
                f"interval(s) (~{secs}s) since "
                f"{self.first_identical_at.strftime('%H:%M:%S') if self.first_identical_at else '?'} "
                f"IST — limit is {self.max_identical}. No live session on "
                f"record has a single interval without new data, so this is a "
                f"stopped feed, not a quiet market.")

    def reset(self) -> None:
        """Forget the run of identical values, keep the armed state."""
        self.identical = 0
        self.first_identical_at = None


def fingerprint(d: dict) -> tuple:
    """A comparable summary of a {symbol: price} map. Never raises.

    WHY NOT THE WHOLE DICT. A quote map is ~450 floats and comparing it
    wholesale would call a feed stale only when every symbol froze together.
    That is what happened on 2026-10-20, but it is the easy case. The count
    travels alongside the sum so a map that loses symbols, or one where a few
    prices move while the rest are pinned, reads as a change rather than as
    silence -- a partial feed is a different fault and belongs to whoever
    counts symbols, not here.
    """
    try:
        items = [(str(k), round(float(v), 4)) for k, v in (d or {}).items()
                 if v is not None]
        return (len(items), round(sum(v for _, v in items), 4))
    except Exception as e:
        log.error(f"could not fingerprint a quote map ({e})")
        return (-1, -1.0)
