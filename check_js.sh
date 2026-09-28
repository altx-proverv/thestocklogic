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

# 3. Config drift — every figure a page states about the trading rules.
#
# There is no build step, so each of those was a hand-maintained literal, and
# four of them outlived the rule they described: a Rs2,000 daily cap that no
# longer exists, Rs5,000 risk per trade when it was Rs3,000, a flat Rs1L capital
# basis after the view was rebuilt on real notional, and targets at 2:1/3:1
# after targets were dropped entirely. signals.html even carried a comment
# admitting the mechanism, which did not stop it happening three more times.
#
# tools/stamp_config.py needs no venv and no third-party packages, so it runs
# here on whatever python3 exists.
echo
echo "=== Config Stamp Check ==="
if command -v python3 > /dev/null 2>&1; then
  if ! python3 tools/stamp_config.py --check; then
    echo
    echo "A page states a figure that atlas/config.py disagrees with."
    echo "Run:  python3 tools/stamp_config.py --write"
    exit 1
  fi
else
  echo "python3 not found — config stamp NOT checked."
  echo "This is the check that catches a page promising a rule that changed."
  exit 1
fi

echo
echo "Reminder: if this change touched a table, a column or a status value, run"
echo "  SUPABASE_SERVICE_KEY=... python3 tools/check_schema.py"
echo "and update the dashboards in the SAME commit. The frontend has drifted"
echo "from the backend four times; each time the query returned 400 or an"
echo "unjoinable result and the page rendered nothing."
