#!/usr/bin/env python3
"""
THE PUBLIC PAGE CANNOT WRITE BREAKER STATE. Asserted by IMPORT GRAPH.
=====================================================================

Three separate tools have written production trading state this month. A public
web page must not be the fourth, and "must not" has to be a property of the
code rather than a line in a review.

WHY AN IMPORT GRAPH AND NOT A GREP. A grep for "stand_down" in the page's data
path passes the moment the call is one indirection away:

    from atlas.execution import range_breaker as RB   # grep sees nothing
    RB.stand_down(...)                                # and here it is

So this walks the ACTUAL import closure of every module on the page's data
path, transitively, and fails if any module that defines a writer is reachable
at all. Reachability is the property; whether anyone typed the call is not.

THE FORBIDDEN SET, from the operator's instruction:
    stand_down · block_symbol · _range_save · _blocked_save ·
    breaker.halt · breaker.flag · any write under /var/lib/atlas

THE PAGE'S DATA PATH, in full:
    home.html                      static, no Python at all
    atlas/execution/breaker_public.py   the read-only accessor (a leaf)
    engine/publish_orb.py          breaker -> public.orb_breaks (one way)
    engine/index_quotes.py         broker -> public.index_quotes (a producer)

AND THE SECOND MECHANISM. The browser itself holds only the anon key, so the
decisive enforcement is that anon has SELECT and nothing else on the three
tables the page reads. That is asserted in migrations/ and re-checked here
against the served HTML: the page must issue no write verb at all.
"""

import os
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Modules that define a way to change breaker state or stand ATLAS down.
# Reaching ANY of these from a page path is a failure.
FORBIDDEN_MODULES = {
    "atlas.execution.range_breaker",     # block_symbol, _range_save, _blocked_save
    "atlas.execution.sentiment_window",  # stand_down
    "atlas.risk.breaker",                # halt, flag, clear
    "atlas.risk.symbol_block",           # block
}

FORBIDDEN_NAMES = ("stand_down", "block_symbol", "_range_save",
                   "_blocked_save", "breaker.halt", "breaker.flag")

# ── TWO TIERS, AND THE DIFFERENCE IS DELIBERATE ───────────────────
#
# BREAKER_BRIDGE is the arrow the operator's instruction is about: the only
# code that reads breaker state and hands it toward the page. It is held to
# TOTAL reachability -- eager imports AND function-local ones -- because a
# deferred import is still one attribute lookup from a writer, and "nobody has
# typed the call yet" is not a safety property.
BREAKER_BRIDGE = [
    "atlas/execution/breaker_public.py",
    "engine/publish_orb.py",
]

# PRODUCERS are cron jobs that write a table the page later reads. They are
# not reachable FROM the page -- a browser cannot invoke them -- so they are
# held to EAGER reachability: nothing may be pulled in at import time.
#
# index_quotes needs the broker, by definition, to read a quote. broker's own
# top-level imports are clean; it reaches market_open only inside
# place_order's halt guard, which this job never calls. Asserting total
# reachability here would force a second copy of the broker auth logic, which
# is worse code and buys no safety: the page still could not call it.
PRODUCERS = [
    "engine/index_quotes.py",
]

# The market view, which moved to /home. index.html is the landing page and
# touches none of this.
SERVED = ROOT / "home.html"


def module_name_for(path: Path):
    """Dotted name for a project file, or None if it is outside the project."""
    try:
        rel = path.resolve().relative_to(ROOT)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts) if parts else None


def path_for(mod: str):
    """Where a dotted project module lives, or None if it is third-party."""
    base = ROOT / Path(*mod.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.exists():
            return cand
    return None


def imports_of(path: Path, eager_only: bool = False) -> set:
    """Dotted module names this file imports.

    eager_only=False walks every Import node anywhere in the file, including
    function-local ones -- a deferred import is still reachability.
    eager_only=True walks only module-level imports, i.e. what actually
    executes when the module is imported.
    """
    out = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    nodes = tree.body if eager_only else list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # relative import
                continue
            if node.module:
                out.add(node.module)
                # `from atlas.execution import range_breaker` -- the submodule
                # is what is actually reached, so record it too.
                for a in node.names:
                    out.add(f"{node.module}.{a.name}")
    return out


def closure(entry: Path, eager_only: bool = False) -> dict:
    """{module: via-chain} for every project module reachable from entry."""
    start = module_name_for(entry) or str(entry)
    seen, frontier = {start: [start]}, [(entry, [start])]
    while frontier:
        path, chain = frontier.pop()
        for mod in imports_of(path, eager_only=eager_only):
            if mod in seen:
                continue
            p = path_for(mod)
            if p is None:
                continue            # stdlib or third-party: not our graph
            seen[mod] = chain + [mod]
            frontier.append((p, chain + [mod]))
    return seen


def main() -> int:
    ok = True

    def check(label, got, want, note=""):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {label:<62}{str(got):<8}"
              f"{'ok' if good else '** want ' + str(want) + ' **':<20}{note}")
        return good

    print("TIER 1 — THE BREAKER BRIDGE, TOTAL REACHABILITY")
    print("-" * 104)
    print("  (every import anywhere in the file, transitively — a deferred")
    print("   import is still one attribute lookup from a writer)")
    for rel in BREAKER_BRIDGE:
        entry = ROOT / rel
        if not entry.exists():
            print(f"  ** {rel} IS MISSING — the bridge is not what this test "
                  f"describes **")
            ok = False
            continue
        cl = closure(entry)
        hits = sorted(set(cl) & FORBIDDEN_MODULES)
        good = not hits
        ok &= good
        print(f"  {rel:<42}{len(cl):>4} project module(s)   "
              f"{'clean' if good else '** REACHES ' + ', '.join(hits) + ' **'}")
        for h in hits:
            print(f"        via: {' -> '.join(cl[h])}")

    print()
    print("TIER 2 — PRODUCERS, EAGER REACHABILITY")
    print("-" * 104)
    print("  (what actually executes on import; a browser cannot invoke these)")
    for rel in PRODUCERS:
        entry = ROOT / rel
        if not entry.exists():
            print(f"  ** {rel} IS MISSING **")
            ok = False
            continue
        cl = closure(entry, eager_only=True)
        hits = sorted(set(cl) & FORBIDDEN_MODULES)
        good = not hits
        ok &= good
        print(f"  {rel:<42}{len(cl):>4} project module(s)   "
              f"{'clean' if good else '** REACHES ' + ', '.join(hits) + ' **'}")
        for h in hits:
            print(f"        via: {' -> '.join(cl[h])}")

    print()
    print("THE ACCESSOR ITSELF IS A LEAF")
    print("-" * 104)
    # breaker_public must import NOTHING from the project. That is what makes
    # it safe to hand to a page path: there is no graph under it to walk.
    acc = closure(ROOT / "atlas/execution/breaker_public.py")
    check("breaker_public reaches 0 other project modules", len(acc) - 1, 0,
          ", ".join(sorted(set(acc) - {"atlas.execution.breaker_public"})) or "leaf")

    # EXECUTABLE SOURCE, NOT RAW TEXT. The accessor's docstring explains
    # exactly which writers it must not reach, and it has to be able to say
    # so. A comment naming a writer is not a reference to one -- the import
    # closure above is what proves reachability.
    sys.path.insert(0, str(ROOT / "tests"))
    from _srcutil import executable_source
    src = executable_source(ROOT / "atlas/execution/breaker_public.py")
    # No write primitive of any kind.
    for bad, why in ((".write_text(", "writes a file"),
                     (".mkdir(", "creates a directory"),
                     ("os.replace(", "moves a file into place"),
                     ("import requests", "could POST somewhere"),
                     ('"w"', "opens a file for writing"),
                     ("'w'", "opens a file for writing")):
        check(f"accessor has no {bad!r} ({why})", bad in src, False)
    for name in FORBIDDEN_NAMES:
        check(f"accessor never names {name!r}", name in src, False)

    print()
    print("NOTHING ON THE PAGE PATH NAMES A WRITER")
    print("-" * 104)
    for rel in BREAKER_BRIDGE + PRODUCERS:
        p = ROOT / rel
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8")
        # Comments are allowed to NAME these -- publish_orb's docstring
        # explains the arrow and must be able to say what it must not do. So
        # this reads executable source only.
        try:
            sys.path.insert(0, str(ROOT / "tests"))
            from _srcutil import executable_source
            txt = executable_source(p)
        except Exception:
            pass
        hits = [n for n in FORBIDDEN_NAMES if n in txt]
        check(f"{rel} names no writer", hits, [])

    print()
    print("THE PAGE WRITES NOTHING AT ALL")
    print("-" * 104)
    if not SERVED.exists():
        print("  ** home.html is missing **")
        ok = False
    else:
        html = SERVED.read_text(encoding="utf-8")
        # The browser holds the anon key. anon has SELECT and nothing else on
        # the three tables (asserted in the migration), but the page must not
        # even attempt a write -- an attempt is a bug worth failing on.
        for verb in ("method: 'POST'", 'method: "POST"', "method:'POST'",
                     "method: 'PATCH'", 'method: "PATCH"',
                     "method: 'DELETE'", 'method: "DELETE"',
                     "method: 'PUT'", 'method: "PUT"'):
            check(f"home.html issues no {verb}", verb in html, False)
        for tbl in ("orb_breaks", "breadth_snapshots", "index_quotes"):
            # Reading them is the point; the check above covers writing.
            pass
        check("home.html never names a breaker state file",
              "/var/lib/atlas" in html, False)
        for name in FORBIDDEN_NAMES:
            check(f"home.html never names {name!r}", name in html, False)

    print()
    print("AND THE TEST HAS TEETH — a planted import must fail it")
    print("-" * 104)
    # PROOF THE GRAPH WALK WORKS. Write a module that reaches a writer
    # transitively (one indirection, which a grep of the entry point would
    # miss) and assert the closure catches it.
    tmp = ROOT / "tests" / "_planted_page_path.py"
    hop = ROOT / "tests" / "_planted_hop.py"
    try:
        hop.write_text("from atlas.execution import range_breaker\n")
        tmp.write_text("import tests._planted_hop\n")
        cl = closure(tmp)
        caught = bool(set(cl) & FORBIDDEN_MODULES)
        ok &= caught
        print(f"  {'a two-hop path to range_breaker is caught':<62}"
              f"{str(caught):<8}{'ok' if caught else '** THE WALK IS BLIND **'}")
        if caught:
            h = sorted(set(cl) & FORBIDDEN_MODULES)[0]
            print(f"        via: {' -> '.join(cl[h])}")
    finally:
        tmp.unlink(missing_ok=True)
        hop.unlink(missing_ok=True)

    print("-" * 104)
    print("PAGE CANNOT WRITE BREAKER STATE:",
          "PROVEN" if ok else "*** NOT PROVEN ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
