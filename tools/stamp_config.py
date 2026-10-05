#!/usr/bin/env python3
"""
Stamp config constants into the static pages.
=============================================

    python3 tools/stamp_config.py --check     # report drift, exit 1 if any
    python3 tools/stamp_config.py --write     # rewrite the pages

WHY. Every page here is static and nothing serves config to the browser, so each
figure was a hand-maintained literal. signals.html carried a comment saying so
outright -- "if Rule 1 changes in config.py, change it here too. It read Rs5,000
until 28 Aug 2026 and matched nothing." That is not a lapse, it is the mechanism:
four operator-facing claims went stale the same way, and the comment shows that
knowing about it does not prevent it.

So the numbers are stamped from the source of truth, and tests/test_page_claims.py
runs --check, which fails the suite the moment a page and atlas/config.py
disagree. The test is the part that matters -- stamping alone just moves the
hand-maintenance somewhere else.

MARKERS. A stamped span looks like:

    <!--stamp:risk_per_trade-->&#8377;3,000<!--/stamp-->

Everything between the markers is replaced. The markers survive, so stamping is
idempotent, and a page with no markers is untouched rather than guessed at.

WHAT IS NOT STAMPED. Anything that is a claim rather than a number -- "winners
are trailed", "SL at structure". A wrong sentence cannot be fixed by
substitution, and pretending otherwise would give false assurance. Those are
covered by the assertions in tests/test_page_claims.py instead.
"""

from __future__ import annotations

import re
import sys
import argparse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGES = ("signals.html", "index.html", "waitlist.html", "tsl-dashboard.html",
         "atlas.html", "admin.html", "performance.html")

MARK = re.compile(r"(<!--stamp:([a-z0-9_]+)-->)(.*?)(<!--/stamp-->)", re.S)


def values() -> dict:
    """The source of truth. Rupee figures use &#8377; so the pages stay ASCII."""
    from atlas.config import (MAX_RISK_PER_TRADE, MAX_NOTIONAL_PER_TRADE,
                              MAX_TRADES_PER_DAY, MIN_STOP_PCT, MAX_STOP_PCT,
                              SIZING_MODE, FIXED_NOTIONAL_PER_TRADE)
    from engine.universe import ALL_SYMBOLS
    from engine.trading_calendar import NSE_HOLIDAYS, CALENDAR_COVERAGE
    # The entry-distance gate was removed on 2026-10-01. Stamped as "none" rather
    # than omitted, because a page that simply stops mentioning a threshold reads
    # as an oversight; one that says there isn't one reads as a decision.
    MAX_ENTRY_DIST_PCT = None

    def rupees(n: float) -> str:
        """Indian digit grouping: 1,00,000 rather than 100,000."""
        n = int(n)
        head, last3 = divmod(n, 1000)
        if not head:
            return f"&#8377;{last3}"
        groups = []
        while head > 99:
            groups.append(f"{head % 100:02d}")
            head //= 100
        groups.append(str(head))
        return "&#8377;" + ",".join(reversed(groups)) + f",{last3:03d}"

    # THE PAGES MUST NOT CLAIM A RISK BUDGET THAT IS NOT IN FORCE. In
    # fixed_notional mode quantity comes from the Rs10,000 target and risk is
    # whatever the stop implies -- about Rs205 at the median, not Rs3,000. A page
    # saying "Max Rs3,000 risk per trade" while sizing at Rs10,000 of notional is a
    # false claim to a subscriber, which is the class of thing this stamper exists
    # to prevent.
    _fixed = SIZING_MODE == "fixed_notional"
    v = {
        # A FIGURE, NOT A SENTENCE. The first version of this stamped a whole
        # clause into a card whose prose already read "Max ... risk per trade",
        # producing "Max whatever the structural stop implies ... risk per trade".
        # A stamp replaces a NUMBER; the sentence around it is the page's job, and
        # when the meaning changes the sentence has to change too.
        "risk_per_trade":     ("~" + rupees(200) if _fixed
                               else rupees(MAX_RISK_PER_TRADE)),
        "position_size":      (rupees(FIXED_NOTIONAL_PER_TRADE) if _fixed
                               else rupees(MAX_NOTIONAL_PER_TRADE)),
        "max_notional":       (rupees(FIXED_NOTIONAL_PER_TRADE)
                               + " per trade, fixed"
                               if _fixed else rupees(MAX_NOTIONAL_PER_TRADE)),
        "max_trades_per_day": str(MAX_TRADES_PER_DAY),
        "universe_count":     str(len(ALL_SYMBOLS)),
        "stop_band":          f"{MIN_STOP_PCT}%&ndash;{MAX_STOP_PCT}%",
    }
    v["entry_distance"] = ("none — taken at market when price reaches the zone"
                           if MAX_ENTRY_DIST_PCT is None
                           else f"{MAX_ENTRY_DIST_PCT:.2f}%")

    # THE NSE HOLIDAY LIST, FOR THE BROWSER.
    #
    # Every page computes "the session a reader is entitled to signals for" by
    # stepping forward from a date, and every one of them skipped weekends only.
    # Friday 2 October 2026 was a holiday, so Thursday's batch was correctly the
    # current one for Monday the 5th -- and the page said "NO SIGNALS for Fri, 2
    # Oct", labelled a valid batch stale, and zeroed its own counter. The rule has
    # always been TRADING days: a batch carries to the next trading session however
    # many non-trading days sit between.
    #
    # The list lives in engine/trading_calendar.py and nothing served it to the
    # browser, which is the whole reason the page could not know. Stamped rather
    # than duplicated, so tests/test_page_claims.py --check fails the moment the
    # two disagree -- the same guarantee the rupee figures get.
    #
    # BOUNDED TO THE CURRENT AND NEXT CALENDAR YEAR. The page only ever steps a
    # few days forward from a recent date, so 2023 holidays are dead weight in
    # every page load. Past years stay in trading_calendar.py for the backfill.
    this_year = date.today().year
    window = sorted(h for h in NSE_HOLIDAYS
                    if this_year <= int(h[:4]) <= this_year + 1)
    v["nse_holidays"] = ",".join(window)
    # The year the stamped window runs out, so the page can say it does not know
    # rather than treating January as fully open. CALENDAR_COVERAGE is the list's
    # own limit; the stamped window cannot exceed it.
    v["holiday_coverage_to"] = str(min(this_year + 1, CALENDAR_COVERAGE[1]))
    return v


def scan(page: Path, vals: dict) -> list:
    """[(key, found, expected)] for every stamped span, drifted or not."""
    if not page.exists():
        return []
    out = []
    for m in MARK.finditer(page.read_text(encoding="utf-8")):
        key, found = m.group(2), m.group(3)
        out.append((key, found, vals.get(key)))
    return out


def drifted(vals: dict = None, pages=PAGES) -> list:
    """[(page, key, found, expected)] for spans that disagree with config."""
    vals = vals or values()
    bad = []
    for name in pages:
        for key, found, expected in scan(ROOT / name, vals):
            if expected is None:
                bad.append((name, key, found, "<UNKNOWN KEY>"))
            elif found != expected:
                bad.append((name, key, found, expected))
    return bad


def write(vals: dict = None, pages=PAGES) -> int:
    vals = vals or values()
    changed = 0
    for name in pages:
        p = ROOT / name
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")

        def sub(m):
            nonlocal changed
            key, found = m.group(2), m.group(3)
            want = vals.get(key)
            if want is None:
                # An unknown key is left alone and reported, never blanked.
                print(f"  {name}: unknown stamp key {key!r} — left as-is")
                return m.group(0)
            if want != found:
                changed += 1
                print(f"  {name}: {key}  {found!r} -> {want!r}")
            return m.group(1) + want + m.group(4)

        out = MARK.sub(sub, src)
        if out != src:
            p.write_text(out, encoding="utf-8")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    vals = values()

    if a.write:
        n = write(vals)
        print(f"stamped {n} value(s) from atlas/config.py")
        return 0

    bad = drifted(vals)
    total = sum(len(scan(ROOT / n, vals)) for n in PAGES)
    print(f"{total} stamped span(s) across {len(PAGES)} page(s)")
    for k, v in sorted(vals.items()):
        print(f"  {k:<20}{v}")
    if not bad:
        print("\nno drift")
        return 0
    print(f"\nDRIFT — {len(bad)} span(s) disagree with atlas/config.py:")
    for name, key, found, expected in bad:
        print(f"  {name:<22}{key:<20}page={found!r}  config={expected!r}")
    print("\nRun: python3 tools/stamp_config.py --write")
    return 1


if __name__ == "__main__":
    sys.exit(main())
