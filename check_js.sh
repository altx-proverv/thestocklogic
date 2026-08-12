#!/bin/bash
# Pre-commit checks for the dashboards.
#
# There is no build step, so these two checks are the only thing standing
# between an edit and a broken page in production.
#
#   1. JS syntax   — every inline <script> block parses
#   2. Schema      — every table/column a page (or a writer) names actually
#                    exists. Run separately: tools/check_schema.py
#
# The file list below used to name three pages by hand and omitted
# tsl-dashboard.html, admin.html, performance.html and waitlist.html — so the
# performance dashboard was never syntax-checked at all. It is globbed now;
# a new page is covered the moment it is added.
echo "=== JS Syntax Check ==="

# Without node every file reported "❌ ERROR: node: command not found", which
# reads as a syntax failure in the HTML rather than a missing dependency. The
# AWS box has no node, so the check silently looked like a broken dashboard.
# Say what is actually wrong and exit non-zero.
if ! command -v node >/dev/null 2>&1; then
  echo "  SKIPPED — node is not installed, so JS cannot be syntax-checked here."
  echo "  Install node, or run this on a machine that has it, before committing"
  echo "  changes to any dashboard."
  exit 2
fi

fail=0
shopt -s nullglob
for f in *.html; do
  grep -q "<script" "$f" || continue
  python3 -c "
import re, sys
content = open('$f').read()
scripts = re.findall(r'<script[^>]*>(.*?)</script>', content, re.DOTALL)
# Skip src-only tags; they have no inline body to check.
js = '\n'.join(s for s in scripts if s.strip())
open('/tmp/check_$f.js','w').write(js)
"
  [ -s "/tmp/check_$f.js" ] || { echo "  —  $f (no inline JS)"; continue; }
  result=$(node --check "/tmp/check_$f.js" 2>&1)
  if [ -z "$result" ]; then
    echo "  ✅ $f — OK"
  else
    echo "  ❌ $f — ERROR:"
    echo "$result"
    fail=1
  fi
done

echo "=== Done ==="
if [ "$fail" -ne 0 ]; then
  echo
  echo "Fix the syntax errors above before committing."
  exit 1
fi

echo
echo "Reminder: if this change touched a table, a column or a status value, run"
echo "  SUPABASE_SERVICE_KEY=... python3 tools/check_schema.py"
echo "and update the dashboards in the SAME commit. The frontend has drifted"
echo "from the backend four times; each time the query returned 400 or an"
echo "unjoinable result and the page rendered nothing."
