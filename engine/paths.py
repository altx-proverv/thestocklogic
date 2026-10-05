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
