#!/usr/bin/env python3
"""
Split a migration into independently runnable statements, and print them.

    python3 -m tools.sql_statements migrations/20261010_radar_r1.sql
    python3 -m tools.sql_statements migrations/20261010_radar_r1.sql --n 7

WHY THIS EXISTS. The Supabase SQL editor has twice applied only a PREFIX of a
pasted file and reported success -- 3 of 12 statements once, 1 of 6 another --
so migrations here are applied ONE STATEMENT AT A TIME. Doing that by eye on a
500-line file is how a statement gets skipped.

A NAIVE SPLIT ON ';' IS WRONG and would be worse than no tool: a DO $$ ... $$
block contains its own semicolons, so splitting on every one would cut the
guarded ALTER TABLE statements into fragments that each fail. This respects
dollar-quoted blocks and single-quoted strings.
"""

import re
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def split_statements(sql: str) -> list:
    """-> [statement, ...] with comments kept, dollar-quotes respected."""
    out, buf = [], []
    i, n = 0, len(sql)
    dollar_tag = None
    in_squote = False
    in_line_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if dollar_tag:
            buf.append(ch)
            if sql.startswith(dollar_tag, i):
                buf.extend(sql[i + 1:i + len(dollar_tag)])
                i += len(dollar_tag)
                dollar_tag = None
                continue
            i += 1
            continue
        if in_squote:
            buf.append(ch)
            if ch == "'":
                # '' is an escaped quote, not a close.
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
        if m:
            dollar_tag = m.group(0)
            buf.append(dollar_tag)
            i += len(dollar_tag)
            continue
        if ch == ";":
            buf.append(ch)
            stmt = "".join(buf).strip()
            if re.sub(r"--[^\n]*", "", stmt).strip(" ;\n\t"):
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if re.sub(r"--[^\n]*", "", tail).strip(" ;\n\t"):
        out.append(tail)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--n", type=int, help="print only statement N (1-based)")
    ap.add_argument("--list", action="store_true",
                    help="one summary line per statement")
    a = ap.parse_args()

    p = Path(a.path)
    if not p.is_absolute():
        p = ROOT / p
    stmts = split_statements(p.read_text())

    if a.list:
        print(f"{len(stmts)} statement(s) in {p.name}\n")
        for i, s in enumerate(stmts, start=1):
            body = re.sub(r"--[^\n]*", "", s).strip()
            first = " ".join(body.split())[:88]
            print(f"  {i:>2}. {first}")
        return 0

    if a.n:
        if not 1 <= a.n <= len(stmts):
            print(f"statement {a.n} does not exist; the file has {len(stmts)}")
            return 1
        print(stmts[a.n - 1])
        return 0

    for i, s in enumerate(stmts, start=1):
        print(f"\n{'=' * 74}\n-- STATEMENT {i} of {len(stmts)}\n{'=' * 74}")
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
