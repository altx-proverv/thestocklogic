#!/usr/bin/env python3
"""THE MOBILE TAB BAR EXISTS TWICE AND MUST BE THE SAME TWICE.

home.html and atlas.html each carry their own copy of the bar -- there is no
shared stylesheet on this site, every page is self-contained, and tonight that
was accepted rather than fixed. What is not accepted is the copies drifting
apart without anyone noticing, which is the failure this file exists to
prevent: /home and /atlas have already diverged three times, each time in a
way nobody saw until it was on screen.

So the two blocks are compared BYTE FOR BYTE, not parsed and not normalised.
A single changed space fails this, deliberately: the moment someone edits one
copy and not the other, the next run says so and names the difference.

The bar is written so that one block can serve two pages that do not share a
palette. Every tab is an anchor, so the markup needs no per-page variation --
home.html intercepts the three view links in script and lets /atlas navigate;
atlas.html lets all four navigate. The active tab is set by each page's own
script, never in the markup. And every colour carries a hex fallback, so the
CSS works on atlas.html, which defines --sky but not --header, --hair or
--micro.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ("home.html", "atlas.html")

MARKUP_START = "<!-- ==== TAB BAR / SHARED BLOCK ===="
MARKUP_END = "<!-- ==== END TAB BAR ==== -->"
CSS_START = "/* ==== TAB BAR / SHARED BLOCK ===="
CSS_END = "/* ==== END TAB BAR ==== */"

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<56} {str(got)[:28]:<30} {'ok' if ok else 'FAIL':<6} {note}")


def block(text, start, end, fname, what):
    if text.count(start) != 1 or text.count(end) != 1:
        failures.append(f"{fname}: {what} markers not found exactly once "
                        f"(start {text.count(start)}, end {text.count(end)})")
        return None
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[i:j]


print()
print("THE TAB BAR IS PRESENT IN BOTH PAGES")
print("-" * 104)
src = {}
for f in PAGES:
    p = ROOT / f
    check(f"{f} exists", p.exists(), True)
    src[f] = p.read_text(encoding="utf-8") if p.exists() else ""

blocks = {}
for what, (a, b) in (("markup", (MARKUP_START, MARKUP_END)),
                     ("css", (CSS_START, CSS_END))):
    for f in PAGES:
        got = block(src[f], a, b, f, what)
        blocks[(what, f)] = got
        check(f"  {f} carries the {what} block", got is not None, True,
              f"{len(got)} chars" if got else "")

print()
print("AND THE TWO COPIES ARE BYTE-IDENTICAL")
print("-" * 104)
for what in ("markup", "css"):
    h, t = blocks[(what, "home.html")], blocks[(what, "atlas.html")]
    same = h is not None and h == t
    note = ""
    if h is not None and t is not None and h != t:
        # name the first difference rather than just saying they differ
        for n, (x, y) in enumerate(zip(h, t)):
            if x != y:
                note = (f"first difference at char {n}: "
                        f"home {x!r} vs atlas {y!r} -- "
                        f"...{h[max(0, n-40):n+20]!r}...")
                break
        else:
            note = f"same prefix, different length: {len(h)} vs {len(t)}"
    check(f"{what} identical in both pages", same, True, note)

print()
print("AND IT IS BUILT SO ONE BLOCK CAN SERVE TWO PALETTES")
print("-" * 104)
css = blocks[("css", "home.html")] or ""
mk = blocks[("markup", "home.html")] or ""
bare = re.findall(r"var\((--[a-z0-9-]+)\)(?!\s*,)", css)
check("every colour in the block has a hex fallback", bare, [],
      "atlas.html defines --sky but not --header, --hair or --micro")
# JUDGE THE RULES, NOT THE PROSE ABOUT THEM. The block's own comment explains
# why it is not keyed to hover, and an earlier version of this check matched
# that sentence and failed on it.
css_rules = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
conds = re.findall(r"@media([^{]*)\{", css_rules)
check("the breakpoint is 620px",
      any("max-width:620px" in c.replace(" ", "") for c in conds), True)
check("  and not keyed to hover",
      any("hover" in c for c in conds), False,
      "the bar drives view switching that only exists below 620")
check("  exactly one media query in the block", len(conds), 1)
check("every tab is an anchor", mk.count("<a class=\"tab\""), 4,
      "so the markup needs no per-page variation")
check("no tab is a button", "<button class=\"tab\"" in mk, False)
check("the active tab is not set in the markup",
      "aria-current" in mk, False, "each page's script sets it")
check("the three view tabs point at /home", mk.count('href="/home#'), 3)
check("the fourth points at /atlas", mk.count('href="/atlas"'), 1)
for v in ("market", "tape", "sectors", "atlas"):
    check(f"  data-view={v} present", f'data-view="{v}"' in mk, True)

print()
print("AND EACH PAGE WIRES IT THE WAY ITS OWN ROLE NEEDS")
print("-" * 104)
check("home.html intercepts the view tabs",
      "dv!=='atlas'" in src["home.html"].replace(" ", ""), True)
check("home.html opens the view named in the hash",
      "location.hash" in src["home.html"], True)
check("atlas.html marks its own tab current",
      'data-view="atlas"' in src["atlas.html"]
      and "aria-current" in src["atlas.html"], True)

print("-" * 104)
if failures:
    print(f"TAB BAR SHARED BLOCK: *** {len(failures)} FAILURE(S) ***")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TAB BAR SHARED BLOCK: identical")
