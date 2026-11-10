#!/usr/bin/env python3
"""EVERY ROW, OR AN ERROR (audit D-T1D-21/24).

PostgREST returns at most 1,000 rows per request whatever `limit` says. The
follow-through record read one page and reported success for four weeks. This
pins the pager's rules and that the record's readers use it.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import supabase_read as S                          # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<64} {str(got)[:24]:<26} {'ok' if ok else 'FAIL'}")


class R:
    def __init__(self, code, rows=None, text=""):
        self.status_code, self._rows, self.text = code, rows or [], text

    def json(self):
        return self._rows


def server(total, fail_at=None, cap=1000):
    """A PostgREST stand-in: honours offset, caps every response at `cap`."""
    calls = []

    def get(url, headers=None, timeout=None):
        q = dict(kv.split("=", 1) for kv in url.split("?", 1)[1].split("&"))
        off, lim = int(q.get("offset", 0)), min(int(q.get("limit", cap)), cap)
        calls.append(off)
        if fail_at is not None and off >= fail_at:
            return R(503, text="gateway")
        return R(200, [{"id": i} for i in range(off, min(off + lim, total))])
    get.calls = calls
    return get


print("\nPAGES UNTIL A SHORT PAGE")
g = server(2500)
rows = S.get_all("u", {}, "signals", "order=id.asc", getter=g)
check("2,500 rows behind a 1,000 cap all come back", len(rows), 2500)
check("  in order, none repeated", [r["id"] for r in rows] == list(range(2500)), True)
check("  three requests", g.calls, [0, 1000, 2000])
check("exactly 1,000 rows needs one more (empty) page", len(S.get_all("u", {}, "t", "order=id.asc", getter=server(1000))), 1000)

print("\nA PARTIAL READ IS AN ERROR, NEVER A SHORT LIST")
try:
    S.get_all("u", {}, "signals", "order=id.asc", getter=server(2500, fail_at=1000))
    check("page 2 failing raises", "returned", "raised")
except S.ReadFailed:
    check("page 2 failing raises", "raised", "raised")
for bad, why in (("select=id", "no order"), ("order=id.asc&limit=5000", "caller limit")):
    try:
        S.get_all("u", {}, "t", bad, getter=server(10))
        check(f"{why} is refused", "accepted", "refused")
    except ValueError:
        check(f"{why} is refused", "refused", "refused")

print("\nTHE FOLLOW-THROUGH RECORD READS THROUGH IT")
from tests._srcutil import executable_source                   # noqa: E402
for f in ("engine/mark_signals.py", "engine/update_outcomes.py"):
    src = executable_source(ROOT / f)
    check(f"{f} calls get_all", "get_all(" in src, True)
    check(f"{f} has no single-shot limit=5000/20000 read",
          "limit=5000" in src or "limit=20000" in src, False)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — every row, or an error")
