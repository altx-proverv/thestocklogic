"""
EVERY ROW, OR AN ERROR — the one paged read (2026-11-10).
==========================================================

PostgREST on this project returns AT MOST 1,000 ROWS per request, and an
explicit `limit` cannot raise it: `signals?limit=20000` returns 1,000 with HTTP
200 and no warning (measured 2026-11-10 against the 8,431-row signals table).

A read that does not page therefore looks complete and is not. That is how
mark_signals and update_outcomes reported success every night while 6,745 of
8,431 published signals got no marks and the outcomes table stopped at
2026-10-12 (audit D-T1D-21/24). radar.store._get was the first instance.

RULES, enforced here rather than remembered by callers:
  * pages of PAGE rows until a page comes back short;
  * an ORDER is required -- offset paging over an unordered result can skip or
    repeat rows between pages, which is a quieter version of the same defect;
  * any non-200 on any page raises ReadFailed: a partial read is never returned
    as if it were the whole table;
  * a runaway (more than max_rows) raises too, rather than truncating.
"""
from __future__ import annotations

import requests

PAGE = 1000


class ReadFailed(RuntimeError):
    """The read did not complete. Never treat this as an empty result."""


def get_all(base_url: str, headers: dict, path: str, params: str,
            timeout: int = 60, max_rows: int = 2_000_000, getter=None) -> list:
    """Every row of /rest/v1/<path>?<params>. `params` must include order=."""
    if "order=" not in params:
        raise ValueError(f"{path}: a paged read needs an order= (got {params!r})")
    if "limit=" in params or "offset=" in params:
        raise ValueError(f"{path}: pass no limit/offset; paging owns them")
    get = getter or requests.get
    out, offset = [], 0
    while True:
        url = f"{base_url}/rest/v1/{path}?{params}&limit={PAGE}&offset={offset}"
        try:
            r = get(url, headers=headers, timeout=timeout)
        except Exception as e:
            raise ReadFailed(f"{path}: {type(e).__name__}: {e} at offset {offset}") from e
        if r.status_code not in (200, 206):
            raise ReadFailed(f"{path}: HTTP {r.status_code} at offset {offset}: "
                             f"{(r.text or '')[:200]}")
        chunk = r.json() or []
        out += chunk
        if len(chunk) < PAGE:
            return out
        offset += PAGE
        if offset >= max_rows:
            raise ReadFailed(f"{path}: more than {max_rows} rows; refusing to truncate")
