-- Shorts are MIS: entered and squared off in one session. Marking one 12
-- trading days after the call measured a position that could not have existed,
-- and 1093 of the 1180 SHORT marks in this table were exactly that.
--
-- A SHORT now gets ONE mark, at age_days = 1: the first trading day AFTER the
-- signal publishes, open to close of that same day. Not the signal date --
-- the EOD chain publishes after that close, so a signal-date entry would be
-- trading on information that did not exist yet. Not close-to-close either --
-- that holds the overnight gap, which MIS cannot.
--
-- LONGS are unchanged: CNC, held, marked daily and cumulatively.
--
-- Snapshot first. The rebuild is deterministic from signals + closes, but only
-- on the box and only while those parquets still hold the range.
CREATE TABLE IF NOT EXISTS public.signal_marks_backup_20260821 AS
  SELECT * FROM public.signal_marks;

COMMENT ON TABLE public.signal_marks_backup_20260821 IS
  'Pre-rebuild copy of signal_marks, 2026-08-21. Taken before SHORT marks were reduced to a single intraday mark at age_days = 1.';

-- Drop every carried-forward SHORT mark. Age 1 survives and is recomputed
-- open-to-close by the next mark_signals run; until that runs it still holds
-- the old close-to-close number.
DELETE FROM public.signal_marks
 WHERE direction = 'SHORT' AND age_days >= 2;

-- Record what the columns mean now that they differ by direction.
COMMENT ON COLUMN public.signal_marks.ref_close IS
  'LONG: close on the signal date, the level cum_move_pct is measured from. SHORT: the entry, i.e. the open of the single mark date -- a short is one intraday session, so there is nothing to accumulate from.';
COMMENT ON COLUMN public.signal_marks.prev_close IS
  'LONG: previous trading day close. SHORT: same as ref_close (the entry open) -- the daily move IS the whole trade.';
COMMENT ON COLUMN public.signal_marks.cum_move_pct IS
  'LONG: directional move since ref_close. SHORT: equal to daily_move_pct by construction -- the position does not survive the session.';
COMMENT ON COLUMN public.signal_marks.age_days IS
  'Trading days since signal_date, always >= 1. LONG accrues one mark per day. SHORT has exactly one mark, at age 1.';