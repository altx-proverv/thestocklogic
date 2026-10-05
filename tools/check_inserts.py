#!/usr/bin/env python3
"""
Do the INSERT statements in a migration have as many values as columns?

WHY A TOOL AND NOT AN EYE. The detections.reach2r INSERT in
PENDING_learning_backfill.sql listed twelve columns and supplied eleven values --
`family` was missing -- and the operator found it by running it. Counting by eye
fails here for two specific reasons, and both are present in that file:

  STRINGS CONTAIN PARENTHESES. 'first-touch rate at +2R vs 1/(1+2)' and
  '(score equals their sum on all 845 rows)' both do, so a parenthesis-depth scan
  that does not understand quoting loses the value list entirely.

  ADJACENT LITERALS CONCATENATE. 'a' 'b' is ONE value in SQL, and every long
  rationale in that file is written that way across several lines. A comma count
  gets the right answer; a literal count does not.

    python3 tools/check_inserts.py migrations/*.sql
"""

import re
import sys
from pathlib import Path


def strip_comments(s: str) -> str:
    """Remove -- comments, but never inside a string literal.

    A comment is legal anywhere in SQL, including in the middle of a VALUES tuple,
    and comments contain commas. The first version of this tool did not strip them
    and counted the commas in its own explanatory comment as value separators --
    reporting 14 values where there were 12. A parser that trips on legal input is
    not a check.
    """
    out, i, in_str = [], 0, False
    while i < len(s):
        c = s[i]
        if in_str:
            out.append(c)
            if c == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "-" and i + 1 < len(s) and s[i + 1] == "-":
            while i < len(s) and s[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def split_top_level(s: str) -> list:
    """Split on commas at paren-depth 0, respecting '' -escaped string literals."""
    s = strip_comments(s)
    out, buf, depth, i, in_str = [], [], 0, 0, False
    while i < len(s):
        c = s[i]
        if in_str:
            buf.append(c)
            if c == "'":
                if i + 1 < len(s) and s[i + 1] == "'":   # '' is a literal quote
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            buf.append(c)
        elif c in "([":
            depth += 1
            buf.append(c)
        elif c in ")]":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def check(path: Path) -> list:
    """[(table, n_cols, n_vals, ok, first_col_or_val_mismatch)]"""
    src = path.read_text(encoding="utf-8")
    rows = []
    for m in re.finditer(
            r"INSERT\s+INTO\s+([a-zA-Z_.\"]+)\s*\((.*?)\)\s*VALUES\s*",
            src, re.S | re.I):
        table = m.group(1)
        cols = split_top_level(m.group(2))
        # the value tuple: from the '(' after VALUES to its matching ')'
        j = src.index("(", m.end() - 1)
        depth, k, in_str = 0, j, False
        while k < len(src):
            c = src[k]
            if in_str:
                if c == "'":
                    if k + 1 < len(src) and src[k + 1] == "'":
                        k += 2
                        continue
                    in_str = False
            elif c == "'":
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        vals = split_top_level(src[j + 1:k])
        rows.append((table, cols, vals))
    return rows


def main() -> int:
    bad = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.exists():
            continue
        for table, cols, vals in check(p):
            ok = len(cols) == len(vals)
            flag = "ok" if ok else f"*** {len(cols)} columns, {len(vals)} values ***"
            print(f"  {p.name:42} {table:30} {len(cols):3} cols {len(vals):3} vals  {flag}")
            if not ok:
                bad += 1
                # name the likely culprit: the first column whose value does not
                # look like it belongs to it
                for i, c in enumerate(cols):
                    v = vals[i] if i < len(vals) else "<MISSING>"
                    print(f"      {i+1:2}. {c:16} <- {v[:46]}")
    if bad:
        print(f"\n{bad} INSERT(s) with mismatched counts")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
