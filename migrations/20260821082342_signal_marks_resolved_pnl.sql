-- Resolved P&L: each signal measured against its OWN stop and target, walked
-- forward on daily OHLC, rather than marked to wherever price happened to be on
-- an arbitrary date. Mark-to-market answers "how has the market moved since we
-- called it"; this answers "did the trade as designed work".
--
-- Per signal, not per mark. The value repeats across a long's daily rows the
-- way setup_name does; the views collapse to one row per signal anyway.
--
-- resolution:
--   STOP      low breached sl        -> exit at sl
--   TARGET    high reached target_1  -> exit at target_1
--   EXPIRED   neither within 20 trading days -> exit at that day's close
--   SAME_DAY  a SHORT, which is MIS and closes on its single session
--   NULL      not resolved yet: young signal, still inside the horizon, or
--             missing sl/target
--
-- When a bar breaches the stop AND reaches the target, the STOP wins. Daily
-- bars carry no intraday sequence, so which came first is unknowable; taking
-- the loss is the conservative reading.
ALTER TABLE public.signal_marks
  ADD COLUMN IF NOT EXISTS resolved_pnl_pct numeric,
  ADD COLUMN IF NOT EXISTS resolved_on      date,
  ADD COLUMN IF NOT EXISTS resolution       text;

COMMENT ON COLUMN public.signal_marks.resolved_pnl_pct IS
  'Directional % from entry to the resolving exit. LONG entry is the signal-date close; SHORT entry is its single mark''s open. Fixed once resolved.';
COMMENT ON COLUMN public.signal_marks.resolved_on IS
  'Trading date the signal resolved on. NULL while still unresolved.';
COMMENT ON COLUMN public.signal_marks.resolution IS
  'STOP | TARGET | EXPIRED | SAME_DAY, or NULL if not yet resolved. STOP wins a same-bar stop-and-target: daily bars carry no intraday sequence.';

CREATE INDEX IF NOT EXISTS signal_marks_resolution_idx
  ON public.signal_marks (resolution) WHERE resolution IS NOT NULL;