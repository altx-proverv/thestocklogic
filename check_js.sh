#!/bin/bash
# Run before every commit to catch JS syntax errors in HTML files
echo "=== JS Syntax Check ==="
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
