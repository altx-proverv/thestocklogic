"""
WHICH SECTOR GROUPS MAY BE RANKED (2026-11-10, with the NSE sector map, D-071).

Both heatmaps -- engine/07_sector_momentum.py (nightly, sector_heatmap) and
engine/upstox_ws.py (intraday, the short book's sector step) -- rank every
group they are handed and take the top and bottom three. Two kinds of group
must never be in that ranking:

  UNKNOWN   symbols NSE does not classify. Not a sector: ranking them would
            send a long or short signal to a group with nothing in common.
  thin      fewer than MIN_RANKED_MEMBERS measured this session. NSE's ~20
            industries include one-name groups (Diversified, Forest
            Materials); a one-stock "sector" at the bottom of the table is that
            stock's move, not a sector's.

MIN_RANKED_MEMBERS = 5 is the operator's stated rule (2026-11-10: "the short
spec's minimum-5-members rule"). It was not in the code until now -- the only
size rule was tape-vocabulary.json's min_sector_size (10), which governs one
sentence on the home page.

Unranked groups are returned separately so callers can log them; their stocks
get no sector bias ("avoid"), exactly like a neutral sector.
"""
from __future__ import annotations

UNKNOWN = "UNKNOWN"
NOT_SECTORS = (UNKNOWN, "OTHER", "", None)
MIN_RANKED_MEMBERS = 5


def split_rankable(sector_df, count_col: str = "stock_count", sector_col: str = "sector"):
    """-> (rankable, unranked) DataFrames. Never raises on an empty frame."""
    if sector_df is None or not len(sector_df):
        return sector_df, sector_df
    bad = sector_df[sector_col].isin([x for x in NOT_SECTORS if x is not None]) | \
        sector_df[sector_col].isna() | (sector_df[count_col] < MIN_RANKED_MEMBERS)
    return sector_df[~bad].reset_index(drop=True), sector_df[bad].reset_index(drop=True)
