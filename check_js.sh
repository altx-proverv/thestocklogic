#!/bin/bash
# Run before every commit to catch JS syntax errors in HTML files
echo "=== JS Syntax Check ==="

# Without node every file reported "❌ ERROR: node: command not found", which
# reads as a syntax failure in the HTML rather than a missing dependency. The
# AWS box has no node, so the check silently looked like a broken dashboard.
# Say what is actually wrong and exit non-zero.
if ! command -v node >/dev/null 2>&1; then
  echo "  SKIPPED — node is not installed, so JS cannot be syntax-checked here."
  echo "  Install node, or run this on a machine that has it, before committing"
  echo "  changes to signals.html / atlas.html / landing.html."
  exit 2
fi
for f in signals.html atlas.html landing.html; do
  if [ -f "$f" ]; then
    python3 -c "
import re, sys
content = open('$f').read()
scripts = re.findall(r'<script[^>]*>(.*?)</script>', content, re.DOTALL)
js = '\n'.join(scripts)
open('/tmp/check_$f.js','w').write(js)
print('Extracted JS from $f')
"
    result=$(node --check /tmp/check_$f.js 2>&1)
    if [ -z "$result" ]; then
      echo "  ✅ $f — OK"
    else
      echo "  ❌ $f — ERROR:"
      echo "$result"
    fi
  fi
done
echo "=== Done ==="
