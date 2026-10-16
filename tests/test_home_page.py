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

PAGE = ROOT / "index.html"


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
    for token, hexv in (("--h3", "#1E8A4C"), ("--h2", "#19663D"),
                        ("--h1", "#15492F"), ("--h0", "#1C2E4A"),
                        ("--hm1", "#5E1A20"), ("--hm2", "#8A1F26"),
                        ("--hm3", "#B4232B")):
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
    brand_uses = len(re.findall(r"var\(--brand\)", html))
    check("brand green is referenced a handful of times, not per tile",
          brand_uses <= 4, True, f"{brand_uses} use(s)")
    check("heat() never returns brand green",
          "--brand" in html[html.find("function heat("):
                            html.find("function sign(")], False)

    print()
    print("THE PAGE SAYS WHAT IT CANNOT KNOW")
    print("-" * 104)
    # Out of hours and between refreshes the page must not imply it is live.
    check("it names the four refresh times",
          "08:45, 09:45, 11:30, 15:15" in html, True)
    check("it says updates resume at 09:15",
          "Updates resume at 09:15" in html, True)
    check("the breadth block names its own denominator",
          "not the full" in html, True)
    # THE HEAVY-VOLUME BLOCK IS REAL NOW, not an empty state. It was built
    # empty on the brief's premise that no intraday volume exists; rbe_engine
    # receives volume_traded on every tick and range_map carries avg_vol_20d,
    # so it does. These assert the block is driven by that feed and is honest
    # about which ratio it means and what it counted.
    check("the heavy-volume block reads the real feed",
          "heavy_quiet" in html, True)
    check("  it says the ratio is pace, not cumulative",
          "pace, not cumulative" in html, True)
    check("  it names the sweep it is showing",
          "as of " in html, True)
    check("  and carries the same denominator caveat as breadth",
          html.count("not the full") >= 2, True,
          "breadth and heavy-volume both say it")
    check("the calendar empty state names Radar",
          "Radar" in html, True)
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
