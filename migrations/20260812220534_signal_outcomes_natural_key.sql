-- The accuracy record must survive a signal re-push. It did not.
--
-- signal_outcomes.signal_id carried a FOREIGN KEY to signals.id, and
-- 06_push_supabase deletes and re-inserts a whole date's signals on every run --
-- so ids are not stable, and re-running a date destroyed that date's measured
-- outcomes via the FK. This record is what the subscriber product is sold on.
--
-- Fix: key outcomes on what actually identifies a signal -- the day, the
-- instrument and the side -- and cut the dependency on a surrogate id that
-- churns.
--
-- Also collapses 10.3x duplication. update_outcomes POSTed plain inserts and
-- only skipped keys already DECIDED, so every OPEN outcome was re-inserted
-- nightly; the worst key had 29 copies. Verified safe before deleting: of 601
-- unique keys, ZERO have two different decided outcomes, so "keep the
-- most-resolved row" cannot discard a real result.

-- 0. keep a copy of the pre-migration table
CREATE TABLE IF NOT EXISTS public.signal_outcomes_backup_20260813 AS
  SELECT * FROM public.signal_outcomes;

-- 1. deduplicate, keeping the most-resolved row per natural key
WITH ranked AS (
  SELECT id, row_number() OVER (
           PARTITION BY signal_date, symbol, direction
           ORDER BY CASE outcome
                      WHEN 'WIN_T2'      THEN 5
                      WHEN 'WIN_T1'      THEN 5
                      WHEN 'LOSS'        THEN 4
                      WHEN 'MISSED'      THEN 3
                      WHEN 'INVALIDATED' THEN 3
                      WHEN 'AMBIGUOUS'   THEN 2
                      WHEN 'OPEN'        THEN 1
                      ELSE 0 END DESC,
                    updated_at DESC NULLS LAST,
                    id DESC
         ) AS rn
  FROM public.signal_outcomes
)
DELETE FROM public.signal_outcomes
 WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- 2. cut the cascade. signal_id stays as a soft pointer to the signals row that
--    was current when the outcome was scored; it is NOT authoritative and goes
--    stale the moment that date is re-pushed.
ALTER TABLE public.signal_outcomes
  DROP CONSTRAINT IF EXISTS signal_outcomes_signal_id_fkey;

-- 3. the natural key
ALTER TABLE public.signal_outcomes
  ADD CONSTRAINT signal_outcomes_natural_key
  UNIQUE (signal_date, symbol, direction);

-- 4. make the row self-contained. The planned levels lived only on signals, so
--    reading the accuracy record meant joining a table whose ids churn. With
--    these present an outcome row is complete on its own and survives any
--    re-push of the signal it came from.
ALTER TABLE public.signal_outcomes
  ADD COLUMN IF NOT EXISTS entry_ref numeric,
  ADD COLUMN IF NOT EXISTS sl        numeric,
  ADD COLUMN IF NOT EXISTS target_1  numeric;

-- 5. backfill levels where the soft pointer still resolves
UPDATE public.signal_outcomes o
   SET entry_ref = s.entry_ref, sl = s.sl, target_1 = s.target_1
  FROM public.signals s
 WHERE s.id = o.signal_id AND o.entry_ref IS NULL;

COMMENT ON CONSTRAINT signal_outcomes_natural_key ON public.signal_outcomes IS
  'signal_date + symbol + direction identifies a signal. signals.id does not -- 06_push deletes and re-inserts a date, changing every id.';
COMMENT ON COLUMN public.signal_outcomes.signal_id IS
  'Soft pointer to the signals row current when scored. NOT authoritative; goes stale when that date is re-pushed. Join on the natural key instead.';