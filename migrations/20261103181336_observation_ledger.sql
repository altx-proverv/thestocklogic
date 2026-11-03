-- ════════════════════════════════════════════════════════════════════
-- THE OBSERVATION LEDGER. Append-only, enforced by the database.
-- ════════════════════════════════════════════════════════════════════
--
-- WHAT IT IS. Every symbol-level claim a page renders -- a published signal or
-- candidate, an intraday detection, a BTST pick, an opening-range break, a
-- radar event, a heavy-but-quiet reading -- is written here BEFORE the row
-- that renders it. engine/observations.py is the only writer; each publisher
-- calls it first and refuses to publish when it fails.
--
-- radar_event is ledgered (it renders) but NEVER scored: RADAR describes moves,
-- it does not predict them. The database refuses a score row for one.
--
-- observation_scores holds what happened by T+3: three NSE trading sessions
-- after the observation's session, resolved through engine.trading_calendar,
-- scored net of a 0.23% round-trip floor. Or it holds a skip reason instead of
-- a number, when a number would be invented.
--
-- APPEND-ONLY, AND NOT ON TRUST. Application code can be bypassed by anyone
-- holding the service key and a SQL editor. So:
--
--   1. A BEFORE UPDATE OR DELETE trigger, FOR EACH ROW, raises. It fires for
--      every role, the table owner included -- privileges do not reach it.
--   2. A BEFORE TRUNCATE trigger raises. TRUNCATE fires no row triggers, so
--      (1) alone would leave the fastest way to empty a table open.
--   3. UPDATE, DELETE and TRUNCATE are REVOKEd from anon, authenticated,
--      service_role and PUBLIC. Supabase grants ALL on new public tables to
--      all three by default; this takes it back.
--   4. A BEFORE INSERT trigger stamps recorded_at / scored_at from the
--      database clock. A client can say when it published; it cannot say when
--      the ledger received it.
--
-- WHAT THIS DOES NOT STOP, said plainly: a role that can ALTER the table can
-- drop the triggers, and a superuser can set session_replication_role =
-- replica, which skips triggers. That is the boundary of in-database
-- enforcement. It moves "rewrite history" from a stray UPDATE to a deliberate
-- schema change, which is visible in migration history.
--
-- CORRECTIONS append. A row whose corrects_observation_id is set supersedes
-- the row it names; the original stays. Same for scores.
--
-- `scored` IS NOT A COLUMN. The specification listed it on observations, but
-- a flag that flips from false to true is an UPDATE, which this ledger
-- forbids. It is derived in v_observations from the existence of a score row,
-- so it cannot disagree with the scores and needs no write.
--
-- PaRRVA. CARE Ratings verifies; NSE's PaRRVA Data Centre computes the metrics
-- from exchange data, prospectively. The submission field list was NOT found
-- on 2026-10-26, so these columns are what any such submission must be able
-- to answer and that cannot be recovered after the fact: the instrument
-- (exchange, segment, symbol, isin), the direction, the reference price and
-- what it was, the declared horizon, and two timestamps -- the publisher's and
-- the database's. A column can be added later; a timestamp that was never
-- captured cannot.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE public.observations (
  observation_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  observation_type        text NOT NULL CHECK (observation_type IN (
                            'signal', 'candidate', 'detection', 'btst',
                            'orb_break', 'radar_event', 'heavy_quiet')),
  -- What makes two rows the same observation, per type. See
  -- engine/observations.py natural_key().
  natural_key             text NOT NULL,
  symbol                  text NOT NULL,
  isin                    text,
  exchange                text NOT NULL DEFAULT 'NSE' CHECK (exchange = 'NSE'),
  segment                 text NOT NULL DEFAULT 'CM'  CHECK (segment = 'CM'),
  -- NULL only for a type that claims no direction (heavy_quiet).
  direction               text CHECK (direction IN ('LONG', 'SHORT')),
  -- T0: the NSE session the observation is about.
  session_date            date NOT NULL,
  -- The publisher's clock, and the database's. recorded_at is overwritten on
  -- insert by observations_stamp(); whatever a client sends is discarded.
  published_at            timestamptz NOT NULL,
  recorded_at             timestamptz NOT NULL DEFAULT now(),
  reference_price         numeric CHECK (reference_price > 0),
  reference_basis         text NOT NULL CHECK (reference_basis IN (
                            'close_T0', 'price_at_publication')),
  horizon_sessions        smallint NOT NULL DEFAULT 3 CHECK (horizon_sessions > 0),
  -- Sessions of history the observation's computation needs. Declared per
  -- type in engine/observations.py, each traced to the code that uses it.
  lookback_window         integer NOT NULL CHECK (lookback_window >= 1),
  source_file             text NOT NULL,
  engine_sha              text,
  computed_values         jsonb NOT NULL,
  values_sha256           text NOT NULL,
  corrects_observation_id uuid REFERENCES public.observations (observation_id),
  correction_reason       text,
  CHECK ((corrects_observation_id IS NULL) = (correction_reason IS NULL)),
  CHECK (corrects_observation_id IS DISTINCT FROM observation_id)
);

-- One ORIGINAL per observation. A re-publish of the same values is a no-op
-- (the writer inserts with ignore-duplicates); different values append a
-- correction, which this index does not constrain.
CREATE UNIQUE INDEX observations_one_original
  ON public.observations (observation_type, natural_key)
  WHERE corrects_observation_id IS NULL;
CREATE INDEX observations_session ON public.observations (session_date, observation_type);
CREATE INDEX observations_corrects ON public.observations (corrects_observation_id)
  WHERE corrects_observation_id IS NOT NULL;


CREATE TABLE public.observation_scores (
  score_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- The ORIGINAL observation this scores...
  observation_id      uuid NOT NULL REFERENCES public.observations (observation_id),
  -- ...and the version whose values were used (the latest correction, or the
  -- original itself).
  scored_version_id   uuid NOT NULL REFERENCES public.observations (observation_id),
  scored_at           timestamptz NOT NULL DEFAULT now(),
  t3_session          date NOT NULL,
  reference_price     numeric,
  exit_price          numeric,
  move_pct            numeric,
  cost_pct            numeric,
  net_pct             numeric,
  outcome             text CHECK (outcome IN ('net_positive', 'net_negative', 'net_flat')),
  skip_reason         text CHECK (skip_reason IN (
                        -- the four the scorer exists to apply
                        'lookback_too_short', 'result_in_window',
                        'gsm_asm', 'price_basis_discontinuity',
                        -- a number would be invented
                        'not_directional', 'no_price_data',
                        'not_a_trading_session',
                        -- the check could not be made, which is not a pass
                        'surveillance_unknown', 'results_calendar_unavailable')),
  skip_detail         text,
  cost_basis          text NOT NULL DEFAULT 'round_trip_floor_0.23pct',
  engine_sha          text,
  corrects_score_id   uuid REFERENCES public.observation_scores (score_id),
  correction_reason   text,
  CHECK ((corrects_score_id IS NULL) = (correction_reason IS NULL)),
  -- A number OR a reason. Never both, never neither.
  CHECK (
    (skip_reason IS NULL
       AND reference_price IS NOT NULL AND exit_price IS NOT NULL
       AND move_pct IS NOT NULL AND cost_pct IS NOT NULL
       AND net_pct IS NOT NULL AND outcome IS NOT NULL)
    OR
    (skip_reason IS NOT NULL
       AND move_pct IS NULL AND net_pct IS NULL AND outcome IS NULL)
  )
);

CREATE UNIQUE INDEX observation_scores_one_original
  ON public.observation_scores (observation_id)
  WHERE corrects_score_id IS NULL;


-- The scorer's own record of running. A run that wrote nothing still writes
-- this row, so "no scores tonight" is distinguishable from "no scorer tonight".
CREATE TABLE public.observation_scorer_runs (
  run_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ran_at            timestamptz NOT NULL DEFAULT now(),
  as_of_session     date NOT NULL,
  considered        integer NOT NULL,
  not_due           integer NOT NULL,
  scored            integer NOT NULL,
  skipped           integer NOT NULL,
  skipped_by_reason jsonb NOT NULL,
  errors            integer NOT NULL,
  engine_sha        text
);


-- ── ENFORCEMENT ─────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.observation_ledger_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'observation ledger is append-only: % on % is refused. '
                  'Append a correction that references the row instead.',
                  TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION public.observation_ledger_stamp()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'observations' THEN
    NEW.recorded_at := now();
  ELSIF TG_TABLE_NAME = 'observation_scores' THEN
    NEW.scored_at := now();
  ELSIF TG_TABLE_NAME = 'observation_scorer_runs' THEN
    NEW.ran_at := now();
  END IF;
  RETURN NEW;
END;
$$;

-- RADAR IS NEVER SCORED. Its events describe a move that already happened, in
-- either direction; they feed the base-rate library (radar/outcomes.py), not
-- the accuracy record. The scorer skips them, and this makes a stray insert
-- impossible rather than merely unlikely. The list must equal
-- engine/observations.UNSCORED_TYPES -- tests/test_observations.py checks.
CREATE OR REPLACE FUNCTION public.observation_scores_scorable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.observations o
             WHERE o.observation_id IN (NEW.observation_id, NEW.scored_version_id)
               AND o.observation_type IN ('radar_event')) THEN
    RAISE EXCEPTION 'observation % is a radar_event: radar events are not '
                    'predictions and are never scored', NEW.observation_id
          USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER observations_no_update_delete
  BEFORE UPDATE OR DELETE ON public.observations
  FOR EACH ROW EXECUTE FUNCTION public.observation_ledger_append_only();
CREATE TRIGGER observations_no_truncate
  BEFORE TRUNCATE ON public.observations
  FOR EACH STATEMENT EXECUTE FUNCTION public.observation_ledger_append_only();
CREATE TRIGGER observations_stamp
  BEFORE INSERT ON public.observations
  FOR EACH ROW EXECUTE FUNCTION public.observation_ledger_stamp();

CREATE TRIGGER observation_scores_no_update_delete
  BEFORE UPDATE OR DELETE ON public.observation_scores
  FOR EACH ROW EXECUTE FUNCTION public.observation_ledger_append_only();
CREATE TRIGGER observation_scores_no_truncate
  BEFORE TRUNCATE ON public.observation_scores
  FOR EACH STATEMENT EXECUTE FUNCTION public.observation_ledger_append_only();
CREATE TRIGGER observation_scores_stamp
  BEFORE INSERT ON public.observation_scores
  FOR EACH ROW EXECUTE FUNCTION public.observation_ledger_stamp();
CREATE TRIGGER observation_scores_scorable
  BEFORE INSERT ON public.observation_scores
  FOR EACH ROW EXECUTE FUNCTION public.observation_scores_scorable();

CREATE TRIGGER observation_scorer_runs_no_update_delete
  BEFORE UPDATE OR DELETE ON public.observation_scorer_runs
  FOR EACH ROW EXECUTE FUNCTION public.observation_ledger_append_only();
CREATE TRIGGER observation_scorer_runs_no_truncate
  BEFORE TRUNCATE ON public.observation_scorer_runs
  FOR EACH STATEMENT EXECUTE FUNCTION public.observation_ledger_append_only();
CREATE TRIGGER observation_scorer_runs_stamp
  BEFORE INSERT ON public.observation_scorer_runs
  FOR EACH ROW EXECUTE FUNCTION public.observation_ledger_stamp();


-- ── DERIVED VIEW: `scored`, and the version in force ────────────────

CREATE VIEW public.v_observations AS
SELECT o.*,
       EXISTS (SELECT 1 FROM public.observation_scores s
               WHERE s.observation_id = o.observation_id) AS scored,
       COALESCE((SELECT c.observation_id FROM public.observations c
                 WHERE c.corrects_observation_id = o.observation_id
                 ORDER BY c.recorded_at DESC, c.observation_id DESC LIMIT 1),
                o.observation_id) AS current_version_id
FROM public.observations o
WHERE o.corrects_observation_id IS NULL;


-- ── ACCESS. Not public. Pages render the publish tables, not this. ──

ALTER TABLE public.observations            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.observation_scores      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.observation_scorer_runs ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.observations            FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.observation_scores      FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.observation_scorer_runs FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.v_observations          FROM PUBLIC, anon, authenticated;

REVOKE UPDATE, DELETE, TRUNCATE ON public.observations            FROM service_role;
REVOKE UPDATE, DELETE, TRUNCATE ON public.observation_scores      FROM service_role;
REVOKE UPDATE, DELETE, TRUNCATE ON public.observation_scorer_runs FROM service_role;
GRANT SELECT, INSERT ON public.observations            TO service_role;
GRANT SELECT, INSERT ON public.observation_scores      TO service_role;
GRANT SELECT, INSERT ON public.observation_scorer_runs TO service_role;
GRANT SELECT ON public.v_observations TO service_role;

COMMENT ON TABLE public.observations IS
  'Every symbol-level observation a page renders, written before it renders. Append-only by trigger; corrections append with corrects_observation_id. scored is derived in v_observations.';
COMMENT ON TABLE public.observation_scores IS
  'One original score per observation at T+3 NSE sessions, net of a 0.23% round-trip floor, or a skip_reason instead of a number. Append-only by trigger.';
