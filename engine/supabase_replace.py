"""
REPLACE A SCOPE OF ROWS WITHOUT EVER LEAVING IT EMPTY (2026-11-11, D-083).
=========================================================================

The shape this replaces, found in five publishers:

    DELETE ...?signal_date=eq.D          # the old rows are gone
    POST   new rows                      # ...and if this fails, so are the new

PostgREST gives no transaction across two requests, so a failed or refused
insert after the delete leaves the scope EMPTY, and nothing says so. On
2026-11-10 that emptied the 9 Nov watchlist: 167 rows gone, the replacement
refused, the session the next morning watching 78 symbols instead of 245.

The order here is the one 06_push_supabase already used for signals:

  1. list the ids currently in scope       -- unreadable: change nothing
  2. insert the replacement                -- fails: change nothing
  3. delete exactly the ids from step 1    -- fails: both sets present, logged

Step 3 deletes BY ID inside the scope, never by the scope alone, so it cannot
remove a row another writer added in between, and the worst outcome is a
visible duplicate instead of a silent hole. A single statement is atomic, so a
bulk POST either stores every row or none.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


def replace_rows(base_url: str, headers: dict, table: str, scope: str,
                 records: list, timeout: int = 60, http=requests) -> bool:
    """Replace every row of `table` matching `scope` with `records`.

    `scope` is a PostgREST filter string, e.g. "signal_date=eq.2026-11-09".
    True only when the new rows are stored. The old rows are removed only then.
    """
    if not scope or "=" not in scope:
        raise ValueError(f"{table}: a replace needs a scope filter (got {scope!r})")
    url = f"{base_url}/rest/v1/{table}"
    try:
        r = http.get(f"{url}?{scope}&select=id", headers=headers, timeout=timeout)
    except Exception as e:
        log.error(f"{table} [{scope}]: could not list existing rows ({e}) — "
                  f"nothing published, nothing deleted")
        return False
    if r.status_code != 200:
        log.error(f"{table} [{scope}]: could not list existing rows "
                  f"(HTTP {r.status_code} {r.text[:160]}) — nothing published, "
                  f"nothing deleted")
        return False
    old_ids = [x["id"] for x in (r.json() or [])]

    # AN EMPTY REPLACEMENT DELETES NOTHING. "Nothing computed" and "the scope
    # should now be empty" look identical from here, and the first is far more
    # common; a caller that really means to clear a scope must say so itself.
    if not records:
        log.warning(f"{table} [{scope}]: empty replacement — the "
                    f"{len(old_ids)} existing row(s) are left in place")
        return False
    try:
        ins = http.post(url, headers=headers, json=records, timeout=timeout)
    except Exception as e:
        log.error(f"{table} [{scope}]: insert raised ({e}) — the "
                  f"{len(old_ids)} existing row(s) are intact")
        return False
    if ins.status_code not in (200, 201, 204):
        log.error(f"{table} [{scope}]: insert refused HTTP {ins.status_code} "
                  f"{ins.text[:200]} — the {len(old_ids)} existing row(s) "
                  f"are intact")
        return False

    if old_ids:
        try:
            d = http.delete(f"{url}?{scope}&id=in.({','.join(map(str, old_ids))})",
                            headers=headers, timeout=timeout)
            ok = d.status_code in (200, 204)
            why = f"HTTP {d.status_code} {d.text[:120]}"
        except Exception as e:
            ok, why = False, str(e)
        if not ok:
            log.error(f"DUPLICATE ROWS in {table} [{scope}]: the new rows are in "
                      f"but {len(old_ids)} previous row(s) could not be deleted "
                      f"({why})")
    return True
