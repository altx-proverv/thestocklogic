"""
Heat, decay, and the Hot 10 set.

docs/TSL_FLASH.md §06. There is no fixed card lifetime: the page shows exactly
hot_cards_n cards ranked by heat, and a card leaves the list because something
hotter arrived, not because a timer expired.

    severity = max( |move%| / move_threshold% , vol_multiple / vol_threshold )
    heat     = severity x 0.5 ^ (sessions_since_event / heat_half_life_sessions)

Severity is dimensionless on purpose. Both terms read as "how many times over
the bar it cleared", so a 4x move and a 4x volume spike are the same number and
the max picks whichever was more extreme. The alternative -- weighting a
percentage against a multiple -- would need a constant with no defensible
value.
"""

from __future__ import annotations

import logging
from datetime import date

from radar import bars

log = logging.getLogger("RADAR-HEAT")


def severity(hit: dict) -> float:
    """How far past its own threshold the trigger cleared.

    Reads the thresholds the DETECTOR recorded on the hit, not today's config.
    A threshold change must not silently re-score events detected under the old
    one: that would rewrite history every time the operator edits a knob, and
    the base-rate library would be built on cohorts that moved underneath it.
    """
    move_t = float(hit.get("move_threshold") or 0)
    vol_t = float(hit.get("vol_threshold") or 0)
    terms = []
    if move_t > 0 and hit.get("move_pct") is not None:
        terms.append(abs(float(hit["move_pct"])) / move_t)
    if vol_t > 0 and hit.get("vol_multiple") is not None:
        terms.append(float(hit["vol_multiple"]) / vol_t)
    if not terms:
        return 0.0
    return round(max(terms), 3)


def decay(sev: float, sessions_since: int, half_life: float) -> float:
    """severity halved every `half_life` sessions.

    SESSIONS, from radar.bars.sessions_between, which reads the NSE calendar.
    Calendar days would age a Friday card by three over a weekend -- more than
    half a half-life -- and the page's "sessions since" counter reads the same
    function, so the number on screen and the number in the arithmetic cannot
    drift apart.
    """
    if half_life <= 0:
        return round(float(sev), 4)
    return round(float(sev) * (0.5 ** (max(0, int(sessions_since)) / float(half_life))), 4)


def heat_for(hit: dict, as_of: date, cfg, sev: float | None = None) -> tuple:
    """(heat, severity, sessions_since) for one event as of a session."""
    sev = severity(hit) if sev is None else float(sev)
    n = bars.sessions_between(hit["event_date"], as_of)
    return decay(sev, n, cfg.heat_half_life_sessions), sev, n


def rank_hot(events: list, as_of: date, cfg) -> dict:
    """Recompute heat for every event and settle the Hot 10.

    -> {"live": [...], "displaced": [...]}  each sorted by heat, descending.

    `events` carries every event not yet 'closed': the ones currently shown AND
    the ones previously displaced, because heat has to be recomputed for all of
    them before the set can be settled.

    TWO RULES THAT LOOK LIKE EDGE CASES AND ARE NOT:

    1. A WEAK NEW DETECTION CAN DISPLACE ITSELF. Insert, then trim to the top
       hot_cards_n -- if the newcomer enters below the current tenth it is the
       one trimmed. It is still recorded and still measured; it is simply never
       shown. The alternative, letting every new detection in by force, would
       mean ten quiet days could push a genuine shock off the page.

    2. DISPLACEMENT IS TERMINAL FOR DISPLAY. An event already marked displaced
       stays displaced even if the cards above it decay below it. Re-entry would
       make the Hot 10 churn on arithmetic rather than on news, and a reader who
       watched a card vanish would see it reappear with no event behind it.
    """
    scored = []
    for e in events:
        sev = e.get("severity")
        h, sev, n = heat_for(e, as_of, cfg, sev=sev)
        row = dict(e)
        row.update({"heat": h, "severity": sev, "sessions_since": n})
        scored.append(row)

    # Rule 2: anything already displaced is not a candidate for the live set.
    candidates = [r for r in scored if str(r.get("status", "live")) != "displaced"]
    already = [r for r in scored if str(r.get("status", "live")) == "displaced"]

    # Ties broken by event_date then symbol so two identical runs agree. An
    # unstable sort here would make the replay's rank check fail on nothing.
    candidates.sort(key=lambda r: (-r["heat"], r["event_date"], r["symbol"]))

    n_keep = int(cfg.hot_cards_n)
    live = candidates[:n_keep]
    trimmed = candidates[n_keep:]

    for r in live:
        r["status"] = "live"
    for r in trimmed:
        r["status"] = "displaced"

    displaced = already + trimmed
    displaced.sort(key=lambda r: (-r["heat"], r["event_date"], r["symbol"]))

    if trimmed:
        log.info(f"displaced {len(trimmed)}: "
                 + ", ".join(f"{r['symbol']}@{r['heat']:.3f}" for r in trimmed))
    return {"live": live, "displaced": displaced}
