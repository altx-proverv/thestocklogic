-- ATLAS WRITES TO THE OBSERVATION LEDGER (2026-11-10, the long pilot).
--
-- atlas_entry     a real long-book fill: the price paid and where it came from,
--                 the size and why, the ladder, and the published stop and
--                 target (published only -- nothing is placed at the broker).
--                 Directional and SCORED at T+3 like any other call.
-- atlas_decision  every refusal (status + reason) and every watch-only
--                 would-enter, one row per symbol per status per session.
--                 NEVER SCORED: a refusal is not a call, and a would-enter in a
--                 paused session is not a position -- scoring either would
--                 invent a claim, the same reason radar_event is unscored.
--
-- The CHECK and the never-score list must equal engine/observations.TYPES and
-- UNSCORED_TYPES; tests/test_observations.py reads the NEWEST migration that
-- defines each.

ALTER TABLE public.observations DROP CONSTRAINT observations_observation_type_check;
ALTER TABLE public.observations ADD CONSTRAINT observations_observation_type_check
  CHECK (observation_type IN (
    'signal', 'candidate', 'detection', 'btst',
    'orb_break', 'radar_event', 'heavy_quiet',
    'atlas_entry', 'atlas_decision'));

CREATE OR REPLACE FUNCTION public.observation_scores_scorable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.observations o
             WHERE o.observation_id IN (NEW.observation_id, NEW.scored_version_id)
               AND o.observation_type IN ('radar_event', 'atlas_decision')) THEN
    RAISE EXCEPTION 'observation % is not a call (radar_event / atlas_decision) '
                    'and is never scored', NEW.observation_id
          USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
