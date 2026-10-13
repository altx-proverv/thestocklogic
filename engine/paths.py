"""
Where the pipeline's artifacts live. One definition, imported.

WHY THIS EXISTS. engine/03b_score.py writes all_rows_v2.parquet to
data/processed/signals_v2/ and engine/reject_sample.py read it from data/signals/.
The file was written correctly every night and the sampler reported it missing --
with an error message that told the operator to add a flag to the crontab that does
not exist, so the suggested fix would have been a second wrong turn.

Four modules referenced this directory as a string literal: 03b, 06_push,
reject_sample and the playbook writer. 03b cannot be imported by name (a module
starting with a digit), which is why the constant was retyped rather than shared.
That is not a good enough reason -- this module can be imported by all of them.
"""

from pathlib import Path

# Everything 03b produces, and everything downstream of it reads.
SIGNALS_DIR = Path("data/processed/signals_v2")

ALL_SCORES = SIGNALS_DIR / "all_scores_v2.parquet"      # qualifying rows only
CANDIDATES = SIGNALS_DIR / "candidates_v2.parquet"      # valid zone, did not qualify
# Every row for the LATEST DATE, qualifying or not -- the only artifact carrying the
# rejected population. ~464 rows a night, against 365k if the whole rescored history
# were kept. engine/reject_sample draws its control group from this and refuses to
# run without it rather than sampling the qualifying set.
ALL_ROWS = SIGNALS_DIR / "all_rows_v2.parquet"
PLAYBOOKS = SIGNALS_DIR / "playbooks"


# ── THE SYMBOL LIST IS THE UNIVERSE, NOT THE FOLDER ────────────────
#
# WHY THIS EXISTS. Eleven modules took their symbol list from
# `STOCKS_DIR.glob("*.parquet")`, which on 2026-10-13 was 765 files for a
# 746-symbol universe. The 19 extra are old renames and delistings whose
# parquets stayed on disk after the symbol left SYMBOL_SECTOR_MAP --
# TATAMOTORS frozen at 2025-10-23, INFOSYS at 2025-05-09, GMRINFRA at
# 2024-12-10, and 16 more. Nothing removed them, because nothing owned them.
#
# They were not harmless. 02b logged all of them as STALE every night (59
# symbols: these 19 plus the 40 then-unwritten exclusions) and any surface
# built by globbing this folder -- a heatmap tile per file, a breadth
# denominator, a manifest count -- counted a symbol that has not traded under
# that name for a year.
#
# THE UNIVERSE IS THE AUTHORITY. SYMBOL_SECTOR_MAP is what a symbol being in
# the universe MEANS; the folder is a cache of it. A symbol dropped from the
# map leaves every computation on the next run instead of lingering until
# someone notices the file.
#
# NOT EXCLUDED-FILTERED, DELIBERATELY. This returns the whole map, exclusions
# included. EXCLUDED is a TRADING filter and is enforced at the publish stage
# (engine/06_push_supabase.py); enforcing it here is what froze 37 parquets
# for a month. Data for every universe member, trading for the subset.
def universe_symbol_files(data_dir, suffix: str = ".parquet") -> list:
    """Paths under `data_dir` for symbols in SYMBOL_SECTOR_MAP, sorted.

    Only files that exist are returned -- a universe member with no file yet
    is a gap for the caller to notice, not an error here.
    """
    from pathlib import Path
    try:
        from engine.universe import SYMBOL_SECTOR_MAP
    except ModuleNotFoundError:          # run by file path, engine/ on sys.path
        from universe import SYMBOL_SECTOR_MAP
    d = Path(data_dir)
    return sorted((d / f"{s}{suffix}" for s in SYMBOL_SECTOR_MAP
                   if (d / f"{s}{suffix}").exists()),
                  key=lambda p: p.stem)


def orphan_symbol_files(data_dir, suffix: str = ".parquet") -> list:
    """Files under `data_dir` whose symbol is NOT in the universe, sorted.

    The complement of universe_symbol_files. Used to report what the folder is
    carrying that the universe does not claim, rather than silently including
    it in a computation.
    """
    from pathlib import Path
    try:
        from engine.universe import SYMBOL_SECTOR_MAP
    except ModuleNotFoundError:
        from universe import SYMBOL_SECTOR_MAP
    d = Path(data_dir)
    return sorted((p for p in d.glob(f"*{suffix}")
                   if p.stem not in SYMBOL_SECTOR_MAP),
                  key=lambda p: p.stem)
