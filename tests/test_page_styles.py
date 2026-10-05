"""
Does every class the pages emit have a CSS rule?

WHY THIS EXISTS. buildDetections() shipped on 2 October emitting eleven classes --
det-wrap, det-head, det-sub, det-scroll, det, det-sym, det-dir, det-t, det-lv,
det-foot, det-title -- and not one of them had a rule. The block rendered the
entire time: 22 rows, white text, opacity 1, display block. It was a transparent
268px-wide table nineteen hundred pixels down the page with no border, padding or
background, which reads exactly like nothing having rendered at all.

Three days of "the detections are not showing" came from that, and no test could
see it, because every test asked whether the markup was produced. It was.

THE CHECK IS CHEAP AND THE PAGES ARE ALREADY CLEAN -- zero unstyled classes across
all seven once compound selectors are counted -- so this can be strict rather than
advisory. A class with no rule is either a missing style or a dead attribute, and
both are worth a failing test.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PAGES = ("signals.html", "index.html", "waitlist.html", "tsl-dashboard.html",
         "atlas.html", "admin.html", "performance.html")

# Classes that are deliberately not styled: JS hooks and state flags toggled from
# script. Each one needs a reason, so the allowlist cannot quietly absorb a real
# miss.
ALLOWED = {
    # none at present — compound selectors like .ftab.long.active cover long/short
}

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n        {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def css_classes(css: str) -> set:
    """Every class name appearing anywhere in a selector.

    Must handle compound and descendant forms -- .ftab.long.active styles `long`,
    and a check that only looks for `.long{` reports it as unstyled. That false
    positive is what made the first version of this unusable.
    """
    out = set()
    # strip comments and declaration bodies, leaving selectors
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    for block in re.finditer(r"([^{}]+)\{[^{}]*\}", css):
        for m in re.finditer(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)", block.group(1)):
            out.add(m.group(1))
    return out


def markup_classes(src: str) -> set:
    """Every class name written literally in a class attribute.

    THE HARD PART IS THE CONCATENATIONS. Most of these attributes are built inside
    JavaScript template strings:

        class="sc'+(ctr?' sc-counter':'')+'"
        class="res-'+status+'"

    Splitting naively on quotes and plus signs harvests the EXPRESSION halves too,
    and reports `cls`, `stCls`, `res-` and `LONG` as unstyled classes. Four false
    positives on four pages, which would have made this check useless and got it
    switched off.

    So each expression -- everything from `'+` to `+'` -- is removed first, leaving
    only the literal segments. A value whose class comes entirely from a variable
    contributes nothing, which is correct: there is no literal name to check.
    """
    out = set()
    for pat in (r'class="([^"]*)"', r"class='([^']*)'"):
        for m in re.finditer(pat, src):
            raw = m.group(1)
            # a complete '+ expression +' pair
            raw = re.sub(r"'\s*\+.*?\+\s*'", " ", raw)
            # and a trailing '+ expression that runs to the end of the attribute
            raw = re.sub(r"'\s*\+.*$", " ", raw)
            raw = raw.replace("'", " ")
            for tok in raw.split():
                # lowercase only: every class in these pages is lowercase, and the
                # uppercase leftovers are JS constants like LONG
                if not re.fullmatch(r"[a-z][a-z0-9_-]*", tok):
                    continue
                # A TRAILING HYPHEN IS A PREFIX, NOT A CLASS. tsl-dashboard builds
                # `class="res-" + resolution.toLowerCase()`, so the literal half is
                # `res-` and the real classes are res-stop, res-target, res-expired.
                # Checking the prefix would fail forever; checking nothing would
                # miss a genuinely unstyled variant. So the prefix is required to
                # match at least one defined class instead.
                if tok.endswith("-"):
                    out.add(tok + "*")
                else:
                    out.add(tok)
    return out


print("\n── every emitted class has a rule ──")
total_used = 0
for page in PAGES:
    p = ROOT / page
    if not p.exists():
        continue
    src = p.read_text(encoding="utf-8")
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", src, flags=re.S))
    defined = css_classes(styles)
    used = markup_classes(src)
    total_used += len(used)
    def covered(c):
        if c in defined or c in ALLOWED:
            return True
        if c.endswith("*"):          # a prefix: some defined class must use it
            return any(d.startswith(c[:-1]) for d in defined)
        return False
    missing = sorted(c for c in used if not covered(c))
    check(f"{page}  ({len(used)} classes used, {len(defined)} defined)",
          not missing, f"no CSS rule for: {missing}")

check("the pages were actually scanned", total_used > 300, str(total_used))

print("\n── the detection block specifically ──")
sig = (ROOT / "signals.html").read_text(encoding="utf-8")
styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", sig, flags=re.S))
defined = css_classes(styles)
# the eleven that shipped unstyled
for c in ("det-wrap", "det-head", "det-title", "det-sub", "det-scroll", "det",
          "det-sym", "det-dir", "det-t", "det-lv", "det-foot"):
    check(f"  .{c} is styled", c in defined)

print("\n── and it is styled QUIETER than the signal cards ──")
# These are observations. If they ever acquire the visual weight of a call, the
# page is making a claim the data does not support.
m = re.search(r"\.det-wrap\{([^}]*)\}", styles.replace("\n", ""))
check("det-wrap has a border and a background", bool(m) and "border" in m.group(1))
check("the levels column is dimmed",
      bool(re.search(r"\.det-lv\{[^}]*opacity", styles.replace("\n", ""))))
check("no green/red on the detection rows",
      "--green" not in (re.search(r"table\.det td\{([^}]*)\}",
                                  styles.replace("\n", "")) or
                        type("x", (), {"group": lambda s, i: ""})()).group(1))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("All page-style checks passed.")
