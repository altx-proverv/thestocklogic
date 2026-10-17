#!/usr/bin/env python3
"""
THE HOME PAGE'S DATA CONTRACT.
==============================

The page is a static file. Nothing re-renders it when the universe changes, so
the things it hard-codes have to be asserted against their source or they rot
silently -- and a rotted market page is wrong in a way that still looks right.
"""

import re
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.universe import SYMBOL_SECTOR_MAP          # noqa: E402

# THE MARKET VIEW MOVED TO /home ON 2026-10-17. index.html is the public
# marketing landing page again; this file asserts the gated market view.
PAGE = ROOT / "home.html"


def main() -> int:
    ok = True

    def check(label, got, want, note=""):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {label:<58}{str(got)[:14]:<16}"
              f"{'ok' if good else '** want ' + str(want)[:24] + ' **':<26}{note}")
        return good

    html = PAGE.read_text(encoding="utf-8")

    print("THE EMBEDDED SYMBOL MAP IS THE UNIVERSE, EXACTLY")
    print("-" * 104)
    # THE CONTRACT FORBIDS GLOBBING THE PRICE DIRECTORY: there were 19 orphan
    # parquets once, and a page that globs draws a tile for each. So the map
    # is embedded from SYMBOL_SECTOR_MAP -- and embedding means it can drift,
    # which is what this asserts away.
    m = re.search(r"var SECTOR_MAP=(\{.*?\});", html, re.S)
    if not m:
        print("  ** SECTOR_MAP is not embedded in the page **")
        return 1
    embedded = json.loads(m.group(1))
    check("embedded symbol count", len(embedded), len(SYMBOL_SECTOR_MAP))
    missing = sorted(set(SYMBOL_SECTOR_MAP) - set(embedded))
    extra = sorted(set(embedded) - set(SYMBOL_SECTOR_MAP))
    check("symbols in the universe but not the page", missing, [],
          ", ".join(missing[:6]))
    check("symbols on the page but not the universe", extra, [],
          ", ".join(extra[:6]))
    wrong = sorted(s for s in embedded
                   if embedded[s] != SYMBOL_SECTOR_MAP.get(s))
    check("symbols whose sector disagrees", wrong, [], ", ".join(wrong[:6]))
    check("the page never globs a price directory",
          ("glob(" in html or "readdir" in html), False)

    print()
    print("THE HEAT SCALE IS THE CONTRACT'S, TO THE EXACT THRESHOLD")
    print("-" * 104)
    # Every one of these is a number from docs/home-page-spec.md. A heat scale
    # that drifts is a page that lies about the market in a way no reader can
    # detect.
    # RESTATED 17 Oct 2026 against the brand ground. The operator ruled the
    # near-black #080F1C fork out at every width; docs/home-page-spec.md was
    # amended in the same change. Seven steps and seven thresholds as before --
    # this is the ramp moved onto navy, not a ramp loosened.
    for token, hexv in (("--h3", "#0E7D55"), ("--h2", "#126046"),
                        ("--h1", "#15463C"), ("--h0", "#1B3A6B"),
                        ("--hm1", "#5E2233"), ("--hm2", "#8A2740"),
                        ("--hm3", "#B42D4C")):
        check(f"{token} is {hexv}", f"{token}:{hexv}" in html.replace(" ", ""),
              True)
    for want in (">=2", ">=1", ">=0.3", ">-0.3", ">-1", ">-2"):
        check(f"heat() branches on {want}",
              want.replace(">=", "v>=").replace(">-", "v>-") in
              html.replace(" ", ""), True)

    print()
    print("BRAND GREEN IS NOT A HEAT COLOUR")
    print("-" * 104)
    # The contract is explicit: #00FF88 is the logo, the active nav underline
    # and the live dot ONLY. If bright green ever becomes "up", every heat
    # tile starts shouting.
    brand_uses = len(re.findall(r"var\(--brand[,)]", html))
    check("brand green is referenced a handful of times, not per tile",
          brand_uses <= 4, True, f"{brand_uses} use(s)")
    check("heat() never returns brand green",
          "--brand" in html[html.find("function heat("):
                            html.find("function sign(")], False)

    print()
    print("THE PAGE SAYS WHAT IT CANNOT KNOW")
    print("-" * 104)
    # Out of hours and between refreshes the page must not imply it is live.
    # THE PAGE SAYS WHICH SESSION IT IS SHOWING, which replaced a provenance
    # line that recited the feed's refresh schedule. That was a note to
    # ourselves on a page a member reads.
    check("it names the session being shown",
          "Showing the close of" in html, True)
    check("it says when live prices come back",
          "Live prices resume at 09:15" in html, True)
    # THE RULE ITSELF, not just the markup. The .tag CSS was deleted once by
    # a region rewrite and the tags kept rendering -- as bare inline text
    # jammed against the heading. Markup without its style is a silent defect.
    check("the session tag has a style rule",
          ".tag{" in html.replace(" ", "").replace("\n", ""), True)
    check("  and it separates itself from the heading",
          "margin-left:8px" in html.replace(" ", ""), True)
    check("every block carries a session tag",
          all(t in html for t in ("sc-tag", "ix-tag", "bd-tag", "hv-tag",
                                  "ev-tag", "lb-tag")), True)
    check("  and the header names the close it is showing",
          "showing the close of" in html, True)
    check("the breadth block names its own denominator",
          "stocks followed" in html, True)
    # THE HEAVY-VOLUME BLOCK IS REAL NOW, not an empty state. It was built
    # empty on the brief's premise that no intraday volume exists; rbe_engine
    # receives volume_traded on every tick and range_map carries avg_vol_20d,
    # so it does. These assert the block is driven by that feed and is honest
    # about which ratio it means and what it counted.
    print()
    print("THE DAY CHANGE DOES NOT COME FROM THE DEAD COLUMN")
    print("-" * 104)
    # live_prices.change_pct is 0.0 for all 745 rows and always has been. If
    # the page ever reads it for colour again, every tile goes neutral and
    # nothing on the page says so.
    check("the page merges an intraday source",
          "intraday_prices" in html, True)
    check("  and overwrites change_pct from it",
          "p.change_pct=i.change_pct" in html.replace(" ", ""), True)
    check("  uncovered symbols are marked, not zeroed",
          "p.change_pct=null" in html.replace(" ", ""), True)
    check("  they get their own tile label",
          "LAST CLOSE / not on the intraday feed" in html, True)
    check("  sector aggregates skip uncovered symbols",
          "r.covered===false" in html.replace(" ", ""), True)
    check("  and the page states the coverage",
          "of 745 symbols" in html or "Object.keys(SECTOR_MAP).length" in html,
          True)
    check("the mover rule uses the intraday volume ratio",
          "p.volume_ratio" in html, True)
    check("  and no longer reads end-of-day signals.rvol",
          "select=symbol,rvol" in html, False)

    print()
    print("THE HEAVY-VOLUME BLOCK")
    print("-" * 104)
    check("the heavy-volume block reads the real feed",
          "heavy_quiet" in html, True)
    check("  it says the ratio is pace, not cumulative",
          "pace, not cumulative" in html, True)
    check("  it names the sweep it is showing",
          "as of " in html, True)
    # Both blocks still state their denominator; the wording moved out of
    # jargon ("not the full 745-symbol universe") into reader language
    # ("of the 745 stocks followed") when the empty states were rewritten.
    check("  and carries the same denominator caveat as breadth",
          html.count("stocks followed") >= 2, True,
          "breadth and heavy-volume both say it")
    # THE CALENDAR'S TWO HALVES HAVE DIFFERENT ANSWERS, and the page must not
    # claim "no source" for the half that has one. Every NSE endpoint this
    # codebase uses is backward-looking, so "scheduled" genuinely has nothing;
    # "landed" is radar_events, which is live.
    check("the scheduled half says what will appear, plainly",
          "board meetings" in html and "ex-dividend" in html, True)
    check("  with no endpoint names or internal mechanics",
          ("corporate-announcements" in html or "NSE endpoints" in html), False)
    check("the landed half resolves its own session",
          "eventSessions()" in html and "S.src.landed" in html, True)
    check("  and never shows the session Flash is showing",
          "d!==S.src.events" in html.replace(" ", ""), True)

    print()
    print("THE PAGE DOES NOT BLANK OUT OF HOURS")
    print("-" * 104)
    # The whole point of this pass. Out of hours the grid falls back to the
    # last completed session's close-to-close for the FULL universe -- which
    # covers more symbols than the live feed, not fewer.
    check("the grid falls back to daily_closes",
          "daily_closes" in html, True)
    check("  per symbol, after the live feed",
          "p.live=false" in html.replace(" ", ""), True)
    check("  and the fallback still colours tiles",
          "heat(" in html, True)
    check("the tape resolves its own session",
          "S.src.tape" in html, True)
    check("  and does not blank out of hours",
          "vocabThreshold('move_pct'" in html and "tapeReason(" in html, True,
          "items are included by move + a second fact")

    print()
    print("THE TAPE'S REASON IS NOT THE PERCENTAGE RESTATED")
    print("-" * 104)
    # CODE, NOT COMMENTS. The comments in home.html quote the banned phrasings
    # to explain why they went -- "AEGISLOG -3.11% closed down on Fri 16 Oct"
    # is in the file as the example being argued against. A ban that fires on
    # its own rationale gets the rationale deleted, which is the wrong fix.
    code = re.sub(r"/\*.*?\*/", " ", html, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", " ", code)
    code = re.sub(r"<!--.*?-->", " ", code, flags=re.S)
    # The rule: the percentage is WHAT happened; the reason must be what the
    # percentage cannot tell you. These phrasings restate it and are banned.
    for banned in ("closed down", "closed up", "moved up", "moved down",
                   "'moved '"):
        check(f"  no {banned!r}", banned in code, False)
    # And bare quality words rank a stock instead of naming an event.
    for banned in ("strong", "weak", "looks good", "attractive"):
        check(f"  no quality word {banned!r}",
              re.search(r"\b" + banned + r"\b", html, re.I) is not None, False)
    # Word boundary: EUREKAFORB is a real symbol in the embedded map.
    check("  ORB is gone from the tape",
          re.search(r"\\bORB\\b", code) is not None, False)
    check("  it says morning range, not opening range",
          "morning range" in html, True)
    check("  break rows never render a 0.00% placeholder",
          "i.p===null?''" in html.replace(" ", ""), True)
    check("  the arrow comes from the event, not the day change",
          "r.down!==undefined" in html.replace(" ", ""), True)
    check("  the per-item session tag is gone",
          "badge\">'+esc(i.badge)" in html, False)
    check("the reading switches to the past tense",
          "At the close on" in html, True)
    check("the three new tables say when they first fill",
          ("first reading at 08:45" in html
           and html.count("starts filling at 09:45") >= 2), True)
    check("a missing disclosure says so",
          "No disclosed trigger found" in html, True)
    check("both stale labels exist",
          ("NEW LISTING / building history" in html
           and "INVESTIGATE / no data" in html), True)

    print()
    print("THE SPACE ADJUSTMENT IS PRESENT AND MEASURES BOTH SIDES")
    print("-" * 104)
    check("it measures before the re-render",
          "getBoundingClientRect().top" in html, True)
    check("it corrects scrollTop by the difference",
          "scrollTop+=delta" in html.replace(" ", ""), True)
    check("and falls back to the window below 1100px",
          "window.scrollBy" in html, True)

    print()
    print("THE ARCHIVE IS HIDDEN, NOT DELETED")
    print("-" * 104)
    vi = ROOT / ".vercelignore"
    check(".vercelignore exists", vi.exists(), True)
    if vi.exists():
        check("archive_merged.json is unpublished",
              "archive_merged.json" in vi.read_text(), True)
    check("the archive file still exists in the repo",
          (ROOT / "archive_merged.json").exists(), True,
          "hidden, not deleted")

    print("-" * 104)
    print("HOME PAGE:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
