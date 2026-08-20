-- The entry log records the MORNING check (is LTP inside the zone right now).
-- It did not record the PUBLICATION distance (how far price sat from the zone
-- at the close the signal was scored on), so the two measurements could not be
-- compared after the fact. A signal can publish at 0.28% off Tuesday's close
-- and open 2% away on Wednesday; ATLAS correctly skips it, and the operator
-- needs both numbers to see that the skip was right rather than a gate bug.
ALTER TABLE atlas_entry_log
  ADD COLUMN IF NOT EXISTS entry_dist_pct numeric,
  ADD COLUMN IF NOT EXISTS signal_date    date;

COMMENT ON COLUMN atlas_entry_log.entry_dist_pct IS
  'Publication distance: |close - zone| as %% of close, measured by 03b_score at '
  'signal time. Gated by MAX_ENTRY_DIST_PCT. NOT the morning check.';
COMMENT ON COLUMN atlas_entry_log.signal_date IS
  'Batch date the candidate came from -- the close entry_dist_pct was measured against.';