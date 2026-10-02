-- Audit 7: RBE detections are published as calls. They should be observations.
--
-- WHAT IS ACTUALLY HAPPENING. rbe_engine has written 2,001 rows to live_signals
-- since 2026-08-14 -- 1,459 of them since September -- each with an entry, a stop
-- and two targets. signals.html concatenates every live_signals row for today
-- into the displayed set and counts them in the headline long/short counter. So
-- they render as calls.
--
-- Meanwhile mark_signals and update_outcomes read `signals` and nothing else. So
-- not one of those 2,001 has ever been scored.
--
-- AND THE LEVELS THEY CARRY ARE PARTLY INCOHERENT. target_1 is a clean 1.50R on
-- all 2,001 rows. target_2 is not: on 688 of them (34%) it is not beyond
-- target_1 at all, it spans -0.86R to +28.64R, and at the negative end it sits on
-- the LOSING side of entry -- a "second target" reached by the trade going wrong.
-- So these were not merely unscored calls. They were displayed with four price
-- levels, one of which was wrong a third of the time, to subscribers.
--
-- That is the wrong-side-zone problem again in a different place: a published
-- population sitting outside the record, where the record is the subscriber-facing
-- claim. The fix is not to hide them -- the detection logic is sound and worth
-- accumulating -- it is to stop presenting a detection as an instruction, and to
-- start measuring what happens after one.
--
-- WHY A `kind` COLUMN WHEN `session` ALREADY DISTINGUISHES THEM. session says
-- which engine produced the row. kind says whether it is a call or an
-- observation, and those are different questions: a future engine could produce
-- either, and the page must branch on the second, not the first. Deriving one
-- from the other is how 'morning' came to mean "ORB, and therefore a call".

ALTER TABLE public.live_signals ADD COLUMN IF NOT EXISTS kind text;

-- BACKFILL BY SESSION, ONCE, because that is the only evidence available for
-- rows already written.
--   rbe      breakout detections. Observations.
--   morning  the retired ORB engine, same class of thing. Observations.
--   btst     buy-today-sell-tomorrow, published deliberately as a trade. A call.
UPDATE public.live_signals
   SET kind = CASE WHEN session IN ('rbe', 'morning') THEN 'detection'
                   ELSE 'call' END
 WHERE kind IS NULL;

ALTER TABLE public.live_signals
  ALTER COLUMN kind SET DEFAULT 'detection';

-- DEFAULT 'detection', not 'call'. A new engine that forgets to set this should
-- have its output treated as an observation, not published to subscribers as a
-- trade. The safe default is the one that claims less.

ALTER TABLE public.live_signals
  ADD CONSTRAINT live_signals_kind_chk
  CHECK (kind IN ('call', 'detection')) NOT VALID;

ALTER TABLE public.live_signals VALIDATE CONSTRAINT live_signals_kind_chk;

CREATE INDEX IF NOT EXISTS live_signals_kind_idx
  ON public.live_signals (kind, signal_date DESC);

COMMENT ON COLUMN public.live_signals.kind IS
  'call = published as a trade, with levels, counted in the headline. detection = an observation: the engine saw a condition and recorded it, with no instruction attached and no place in the accuracy record. Defaults to detection, because an engine that forgets to set this should claim less rather than more. Distinct from `session`, which says which engine wrote the row -- a future engine could produce either kind.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM information_schema.columns
     WHERE table_name='live_signals' AND column_name='kind')            AS column_exists,
  (SELECT count(*) = 0 FROM public.live_signals WHERE kind IS NULL)     AS all_classified,
  -- Exact counts as at 2026-10-02: 2,001 rbe + 786 morning = 2,787 detections,
  -- 154 btst calls, 2,941 total. Written as >= so a night's new rows do not fail
  -- the verify, but anchored tightly enough to catch a backfill that missed a
  -- session.
  (SELECT count(*) >= 2001 FROM public.live_signals
     WHERE kind='detection' AND session='rbe')                          AS rbe_are_detections,
  (SELECT count(*) >= 786 FROM public.live_signals
     WHERE kind='detection' AND session='morning')                      AS orb_are_detections,
  (SELECT count(*) >= 154 FROM public.live_signals
     WHERE kind='call' AND session='btst')                              AS btst_stay_calls,
  (SELECT count(*) = 0 FROM public.live_signals
     WHERE kind='call' AND session <> 'btst')                           AS nothing_else_is_a_call,
  (SELECT convalidated FROM pg_constraint
     WHERE conname='live_signals_kind_chk')                             AS check_valid,
  (SELECT column_default LIKE '%detection%' FROM information_schema.columns
     WHERE table_name='live_signals' AND column_name='kind')            AS safe_default,
  (SELECT count(*) = 1 FROM pg_indexes
     WHERE indexname='live_signals_kind_idx')                           AS index_exists;
