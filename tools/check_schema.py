"""
Schema drift check — do the queries in this repo match the live database?
========================================================================
Run before committing anything that touches a table, a column, a status value,
or a dashboard query:

    SUPABASE_SERVICE_KEY=... python3 tools/check_schema.py

WHY THIS EXISTS
---------------
The frontend and the writers have drifted from the schema four times in two days,
each time silently:

  atlas_trades.gtt_trigger_id   column never created. _log_intent POSTed it,
                                PostgREST returned 400, requests.post does not
                                raise on 4xx, the rejection was discarded. A real
                                GTT rested at the broker with no database row.

  live_signals.delivery_pct     and seven more. Every btst_engine push failed
                                with 400 PGRST204. BTST had never written a row.

  atlas.html statuses           the page rendered four hardcoded statuses and
                                dropped the rest, so CANCELLED trades vanished
                                from the record.

  tsl-dashboard.html            not a schema break, but the same shape: the
                                query succeeded, returned rows that could not
                                join, and the page rendered nothing.

  live_prices RLS               RLS enabled, ZERO policies, from the day the
                                table was created. Anon reads returned
                                200 + [] -- indistinguishable from "no data" --
                                for eleven weeks, while the feed wrote 500
                                symbols twice a day. atlas.html marked open
                                positions against nothing and signals.html
                                showed no LTP. Nothing logged, nothing 400'd.

Every one was a query naming something that did not exist (or could not match),
returning a response nobody checked. This makes that visible before it ships.

WHAT IT CHECKS
--------------
  1. Every /rest/v1/<table> referenced in .html and .py exists.
  2. Every column named in select= / order= / a filter exists on that table.
  3. Every literal dict POSTed or PATCHed to a table has only real columns --
     this is the check that would have caught gtt_trigger_id.
  4. Every table the FRONTEND reads is actually visible to the anon key the
     pages ship with -- anon row count vs service-role row count. A mismatch
     is an RLS policy gap. This one matters more than it looks: the database
     has an event trigger (`ensure_rls` -> rls_auto_enable()) that turns RLS
     ON for every new table in public and creates no policy, so every table
     added from here on is born invisible to the frontend and says nothing
     about it.

WHAT IT CANNOT CHECK
--------------------
Dynamically built column lists, and whether a query can actually JOIN. The
tsl-dashboard failure was a live query returning unjoinable rows; no static
check catches that. Response-status checks in the callers cover the rest.
"""

import ast
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Not columns: PostgREST control parameters.
UNCHECKABLE = []
CONTROL = {"select", "order", "limit", "offset", "on_conflict", "columns", "and", "or"}
SKIP_DIRS = {"venv", ".git", "node_modules", "engine/legacy"}


def live_schema() -> dict:
    """{table: {columns}} from PostgREST's OpenAPI document."""
    r = requests.get(f"{SUPABASE_URL}/rest/v1/",
                     headers={"apikey": SUPABASE_KEY,
                              "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=30)
    r.raise_for_status()
    defs = r.json().get("definitions", {})
    return {t: set(d.get("properties", {})) for t, d in defs.items()}


def anon_key() -> str:
    """The anon key the pages actually ship with, scraped from the HTML.

    Deliberately not an env var first: the point of check 4 is to test the key
    a real browser sends. Reading it from the page is the only way to be sure
    the thing under test is the thing deployed.
    """
    env = os.environ.get("SUPABASE_ANON_KEY", "")
    if env:
        return env
    for p, _ in _files():
        if p.suffix != ".html":
            continue
        m = re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", p.read_text())
        if m:
            return m.group(0)
    return ""


def _count(table: str, key: str):
    """(row_count, error). Exact count via PostgREST's content-range header."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}?select=*&limit=1",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Prefer": "count=exact"}, timeout=30)
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {e}"
    if r.status_code not in (200, 206):
        return None, f"HTTP {r.status_code} {(r.text or '').strip()[:120]}"
    total = r.headers.get("content-range", "").split("/")[-1]
    return (int(total) if total.isdigit() else None), ""


def check_anon_visibility(tables) -> list:
    """Frontend-read tables the anon key cannot actually see.

    An RLS-enabled table with no policy answers 200 with an empty array. It
    looks exactly like a table that happens to have no rows, which is why
    live_prices stayed broken for eleven weeks with nothing in any log.
    Comparing against the service-role count is what tells them apart.
    """
    problems = []
    akey = anon_key()
    if not akey:
        print("  anon key not found in any page — skipping the visibility check.")
        return ["could not find the frontend anon key to run the RLS visibility check"]

    print(f"\nanon visibility ({len(tables)} frontend-read tables):")
    for t in sorted(tables):
        svc, svc_err = _count(t, SUPABASE_KEY)
        anon, anon_err = _count(t, akey)
        if svc_err:
            print(f"  {t:<24} service-role read failed: {svc_err}")
            problems.append(f"{t}: service-role read failed: {svc_err}")
            continue
        if anon_err:
            print(f"  {t:<24} anon read failed: {anon_err}")
            problems.append(f"{t}: anon read failed: {anon_err}")
            continue
        if svc and not anon:
            print(f"  {t:<24} anon 0 / service {svc}   <-- RLS POLICY GAP")
            problems.append(
                f"{t}: anon sees 0 of {svc} rows — RLS is on with no anon SELECT "
                f"policy. The frontend gets 200 and an empty array and cannot tell.")
        elif svc and anon is not None and anon < svc:
            print(f"  {t:<24} anon {anon} / service {svc}   <-- partial (row-filtered)")
            problems.append(
                f"{t}: anon sees {anon} of {svc} rows — a policy is filtering rows. "
                f"Intentional or not, the page cannot tell the difference.")
        else:
            print(f"  {t:<24} anon {anon} / service {svc}   ok")
    return problems


def _files():
    for p in sorted(ROOT.rglob("*")):
        if p.suffix not in (".py", ".html"):
            continue
        rel = str(p.relative_to(ROOT))
        if any(rel.startswith(d) or f"/{d}/" in f"/{rel}" for d in SKIP_DIRS):
            continue
        if p.resolve() == Path(__file__).resolve():
            continue          # this file documents the patterns it looks for
        yield p, rel


def _cols_from_query(qs: str):
    """Column names referenced in a PostgREST query string."""
    out = set()
    for part in qs.split("&"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        if key == "select":
            for c in val.split(","):
                c = c.split("(")[0].split(":")[-1].strip()
                if c and c != "*" and "{" not in c and re.fullmatch(r"[a-z0-9_]+", c):
                    out.add(c)
        elif key == "order":
            for c in val.split(","):
                c = c.split(".")[0].strip()
                if c and "{" not in c and re.fullmatch(r"[a-z0-9_]+", c):
                    out.add(c)
        elif key not in CONTROL and "{" not in key and re.fullmatch(r"[a-z0-9_]+", key):
            out.add(key)
    return out


def scan_urls(text: str):
    """[(table, {columns})] for every /rest/v1/<table>?<query> in the text."""
    found = []
    # Coverage guard, reported by main(). A /rest/v1/ whose table name is built
    # at runtime -- fetch(SB+'/rest/v1/'+path) -- cannot be checked, and silently
    # skipping it is the same failure this tool exists to prevent. A jget()
    # refactor did exactly that to tsl-dashboard.html and the tool still said
    # "no drift".
    # Flag only genuine runtime construction -- an interpolation or a string
    # break immediately after the prefix. Matching "anything that is not a table
    # name" also fired on prose like "/rest/v1/<table>" in docstrings, which is
    # noise: a checker that cries wolf gets ignored, and then it may as well not
    # exist.
    for m in re.finditer(r"""/rest/v1/([{'"`+$])""", text):
        UNCHECKABLE.append(text[max(0, m.start()-40):m.start()+40].strip())
    # Stop only at whitespace or a quote. The character class must NOT exclude
    # commas: PostgREST uses them inside select=, order= and in.(), so excluding
    # them truncated every query at its first column and this checker silently
    # validated only that one -- false confidence, which is worse than no check.
    for m in re.finditer(r"""/rest/v1/([a-z0-9_]+)(\?[^\s'"`]*)?""", text):
        table = m.group(1)
        qs = (m.group(2) or "").lstrip("?")
        found.append((table, _cols_from_query(qs)))
    return found


def scan_payloads(path: Path):
    """[(table, {keys}, lineno)] for literal dicts sent to a /rest/v1/<table>.

    Handles both `json={...}` inline and `json=rec` where rec is a dict literal
    assigned in the same function -- which is the shape _log_intent used.
    """
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []

    def url_table(node):
        """Table name from a literal or f-string URL argument."""
        parts = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts = [node.value]
        elif isinstance(node, ast.JoinedStr):
            parts = [v.value for v in node.values
                     if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        m = re.search(r"/rest/v1/([a-z0-9_]+)", "".join(parts))
        return m.group(1) if m else None

    out = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        # dict literals assigned in this function, by variable name
        local = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        local[t.id] = n.value
        for n in ast.walk(fn):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr not in ("post", "patch", "put"):
                continue
            table = next((url_table(a) for a in n.args if url_table(a)), None)
            if not table:
                continue
            for kw in n.keywords:
                if kw.arg != "json":
                    continue
                d = kw.value
                if isinstance(d, ast.Name):
                    d = local.get(d.id)
                if not isinstance(d, ast.Dict):
                    continue
                keys = {k.value for k in d.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if keys:
                    out.append((table, keys, n.lineno))
    return out


def main() -> int:
    if not SUPABASE_KEY:
        print("SUPABASE_SERVICE_KEY not set — cannot read the live schema.")
        return 2

    schema = live_schema()
    print(f"live schema: {len(schema)} tables\n")

    problems = []
    checked_urls = checked_payloads = 0
    frontend_tables = set()

    for path, rel in _files():
        text = path.read_text()

        for table, cols in scan_urls(text):
            checked_urls += 1
            # Only .html reads travel with the anon key; .py runs as service_role
            # and bypasses RLS entirely, so it can never see this class of gap.
            if path.suffix == ".html":
                frontend_tables.add(table)
            if table not in schema:
                problems.append(f"{rel}: table '{table}' does not exist")
                continue
            for c in sorted(cols - schema[table]):
                problems.append(f"{rel}: {table}.{c} does not exist")

        if path.suffix == ".py":
            for table, keys, line in scan_payloads(path):
                checked_payloads += 1
                if table not in schema:
                    problems.append(f"{rel}:{line}: writes to missing table '{table}'")
                    continue
                for c in sorted(keys - schema[table]):
                    problems.append(f"{rel}:{line}: writes {table}.{c} — column does not exist")

    print(f"checked {checked_urls} query URLs and {checked_payloads} write payloads")

    problems += check_anon_visibility({t for t in frontend_tables if t in schema})
    if UNCHECKABLE:
        print(f"\n{len(UNCHECKABLE)} reference(s) could NOT be checked — the table name "
              f"is built at runtime:")
        for u in UNCHECKABLE[:10]:
            print(f"    ...{u}...")
        print("  Put the literal /rest/v1/<table> at the call site so it can be verified.")
        problems.append(f"{len(UNCHECKABLE)} dynamically-built /rest/v1/ reference(s) "
                        f"cannot be schema-checked")
    if not problems:
        print("\nNo schema drift found.")
        return 0

    print(f"\n{len(problems)} PROBLEM(S):\n")
    for p in problems:
        print(f"  {p}")
    print("\nSchema problems return 400 at runtime; if the caller does not check the"
          "\nresponse status, they fail silently. RLS gaps are worse -- they return"
          "\n200 with an empty array, so there is no status to check and no error to"
          "\nlog. The page just renders nothing and looks like a quiet day.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
