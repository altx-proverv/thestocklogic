"""
A REPLACE NEVER LEAVES ITS SCOPE EMPTY (D-083, 2026-11-11).

On 2026-11-10 the signal publish deleted every row for 9 Nov -- candidates
included -- and relied on the candidate step to put them back. That step was
refused; 167 watchlist rows were gone. These checks run engine.supabase_replace
against an in-memory table and then read the publishers' source, so a
delete-before-insert cannot come back unnoticed.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.supabase_replace import replace_rows  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}, want {want!r}"))
    if not ok:
        fails.append(name)


class Resp:
    def __init__(self, code, body=None):
        self.status_code, self._b, self.text = code, body, str(body)

    def json(self):
        return self._b


class FakeTable:
    """Rows with id, signal_date, kind. Filters understood: signal_date=eq.X,
    kind=eq.X, id=in.(..). Enough to judge what a replace deleted."""
    def __init__(self, rows, post_code=201, get_code=200, del_code=204):
        self.rows, self.next = [dict(r) for r in rows], 1000
        self.post_code, self.get_code, self.del_code = post_code, get_code, del_code
        self.calls = []

    def _match(self, url):
        q = url.split("?", 1)[1] if "?" in url else ""
        keep = list(self.rows)
        for part in q.split("&"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            if v.startswith("eq."):
                keep = [r for r in keep if str(r.get(k)) == v[3:]]
            elif v.startswith("in.("):
                ids = {int(x) for x in v[4:-1].split(",") if x}
                keep = [r for r in keep if r[k] in ids]
        return keep

    def get(self, url, headers=None, timeout=None):
        self.calls.append("GET")
        if self.get_code != 200:
            return Resp(self.get_code, "boom")
        return Resp(200, [{"id": r["id"]} for r in self._match(url)])

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append("POST")
        if self.post_code not in (200, 201):
            return Resp(self.post_code, "refused")
        for r in json:
            self.next += 1
            self.rows.append({**r, "id": self.next})
        return Resp(self.post_code, None)

    def delete(self, url, headers=None, timeout=None):
        self.calls.append("DELETE")
        if self.del_code not in (200, 204):
            return Resp(self.del_code, "nope")
        gone = {r["id"] for r in self._match(url)}
        self.rows = [r for r in self.rows if r["id"] not in gone]
        return Resp(self.del_code, None)


SEED = [{"id": 1, "signal_date": "2026-11-09", "kind": "signal"},
        {"id": 2, "signal_date": "2026-11-09", "kind": "candidate"},
        {"id": 3, "signal_date": "2026-11-09", "kind": "candidate"},
        {"id": 4, "signal_date": "2026-11-06", "kind": "candidate"}]
NEW = [{"signal_date": "2026-11-09", "kind": "candidate"}]
SCOPE = "signal_date=eq.2026-11-09&kind=eq.candidate"


def ids(t, **f):
    return sorted(r["id"] for r in t.rows if all(str(r.get(k)) == v for k, v in f.items()))


print("1. A SUCCESSFUL REPLACE")
t = FakeTable(SEED)
check("returns True", replace_rows("u", {}, "t", SCOPE, NEW, http=t), True)
check("the old rows in scope are gone, the new one is in",
      ids(t, signal_date="2026-11-09", kind="candidate"), [1001])
check("a row outside the scope (another kind) is untouched", ids(t, kind="signal"), [1])
check("a row outside the scope (another date) is untouched", ids(t, signal_date="2026-11-06"), [4])
check("order is list, insert, then delete", t.calls, ["GET", "POST", "DELETE"])

print("2. A REFUSED INSERT DELETES NOTHING  (the 2026-11-10 failure)")
t = FakeTable(SEED, post_code=400)
check("returns False", replace_rows("u", {}, "t", SCOPE, NEW, http=t), False)
check("all four rows survive", ids(t), [1, 2, 3, 4])
check("no DELETE was sent", "DELETE" in t.calls, False)

print("3. AN UNREADABLE SCOPE CHANGES NOTHING")
t = FakeTable(SEED, get_code=500)
check("returns False", replace_rows("u", {}, "t", SCOPE, NEW, http=t), False)
check("nothing inserted or deleted", t.calls, ["GET"])

print("4. AN EMPTY REPLACEMENT DELETES NOTHING")
t = FakeTable(SEED)
check("returns False", replace_rows("u", {}, "t", SCOPE, [], http=t), False)
check("all four rows survive", ids(t), [1, 2, 3, 4])

print("5. A FAILED DELETE LEAVES DUPLICATES, NEVER A HOLE")
t = FakeTable(SEED, del_code=500)
check("returns True (the new rows are live)", replace_rows("u", {}, "t", SCOPE, NEW, http=t), True)
check("old and new both present", ids(t, signal_date="2026-11-09", kind="candidate"), [2, 3, 1001])

print("6. A ROW ADDED BY ANOTHER WRITER MID-REPLACE IS NOT DELETED")


class Racing(FakeTable):
    def post(self, url, headers=None, json=None, timeout=None):
        self.rows.append({"id": 77, "signal_date": "2026-11-09", "kind": "candidate"})
        return super().post(url, headers, json, timeout)


t = Racing(SEED)
replace_rows("u", {}, "t", SCOPE, NEW, http=t)
check("the concurrent row survives", 77 in ids(t), True)

print("7. NO PUBLISHER DELETES BEFORE IT INSERTS")
# Comments stripped: judge the code, not the prose about it.


def code(p):
    return "\n".join(l.split("#", 1)[0] for l in (ROOT / p).read_text().splitlines())


for p in ["engine/btst_engine.py", "engine/upstox_ws.py", "engine/07_sector_momentum.py"]:
    src = code(p)
    check(f"{p}: no direct requests.delete on live_signals/sector_heatmap",
          bool(re.search(r"requests\.delete\([^)]*(live_signals|sector_heatmap)", src, re.S)), False)
    check(f"{p}: replaces through replace_rows", "replace_rows(" in src, True)

push = code("engine/06_push_supabase.py")
sig = push[push.index("OWN_SIGNALS ="):push.index("Superseded")]
check("06 signal step: the ids it supersedes exclude candidates",
      "publication_kind.eq.signal" in sig and "publication_kind.is.null" in sig, True)
check("06 signal step: the delete is scoped the same way",
      bool(re.search(r"requests\.delete\(\s*f\"\{base_url\}\?signal_date=eq\.\{day_str\}&\{OWN_SIGNALS\}", sig)), True)
cand = push[push.index("def push_candidates"):push.index("def _chain_failed")]
check("06 candidate step: old ids are listed BEFORE the insert",
      cand.index("old_cids = ") < cand.index("requests.post(base_url"), True)
check("06 candidate step: the delete names those ids and the candidate kind",
      "publication_kind=eq.candidate" in cand[cand.index("requests.delete"):] and
      "old_cids" in cand[cand.index("requests.delete"):], True)

print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("PASSED")
