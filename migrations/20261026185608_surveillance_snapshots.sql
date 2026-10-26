-- ════════════════════════════════════════════════════════════════════
-- GSM / ASM SURVEILLANCE SNAPSHOTS. Append-only, enforced by the database.
-- ════════════════════════════════════════════════════════════════════
--
-- WHY THIS IS A TABLE AND NOT A LOG. NSE's api/reportGSM and api/reportASM
-- answer with TODAY's list and nothing else. A session nobody captured is gone
-- permanently; no source backfills it. The observation scorer skips every
-- window with an uncaptured session as `surveillance_unknown`, so until this
-- table holds a full T+3 window, nothing scores.
--
-- INDEPENDENT OF PENDING_observation_ledger.sql. It defines its own functions,
-- so it can be applied first and on its own -- every day it waits is a day
-- that cannot be recovered.
--
-- TWO TABLES:
--   surveillance_snapshots  one row per (symbol, framework, stage, session):
--                           what NSE said that day. Three captures a session
--                           write the same rows; the unique key makes the
--                           second and third a no-op (ON CONFLICT DO NOTHING,
--                           which is not an UPDATE). A stage that CHANGES
--                           during the day is a new row -- both are true.
--   surveillance_captures   one row per capture ATTEMPT, success or failure.
--                           "Was this session captured" is answered by an ok
--                           row here, never by counting snapshot rows -- a
--                           capture that found nothing and a capture that
--                           never ran must not look alike.
--
-- STAGE IS NSE'S CODE, NOT A LABEL. "Stage I" exists under both long-term and
-- short-term ASM, which are different measures; survCode ('LTASM - I (13)',
-- 'STASM - I (11)', 'IBC I & GSM 0 (58)') is what distinguishes them, so
-- `stage` holds survCode and the human label is kept beside it.
--
-- SESSION IS NSE'S STAMP, NOT OUR CLOCK. engine/surveillance_snapshot.py
-- refuses to label a capture session S unless every row's asmTime / gsmTime
-- says S -- the same rule as a bhavcopy that is not the session it claims to
-- be. The stamp is stored in nse_stamp so the check can be re-done.
--
-- APPEND-ONLY, same four layers as the observation ledger: row trigger
-- refusing UPDATE/DELETE for every role, statement trigger refusing TRUNCATE,
-- REVOKE of UPDATE/DELETE/TRUNCATE from service_role, and recorded_at stamped
-- by the database. captured_at is the CAPTURE time, from the job's clock: a
-- capture saved to disk while Supabase was down and replayed later must keep
-- the time NSE was actually asked.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE public.surveillance_snapshots (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_date  date NOT NULL,
  symbol        text NOT NULL,
  framework     text NOT NULL CHECK (framework IN ('GSM', 'ASM')),
  stage         text NOT NULL,           -- NSE survCode
  -- asmSurvIndicator for ASM. NULL for GSM: NSE's gsmStage is the survCode
  -- number in Roman numerals ('... (62)' -> 'LXII'), not a stage.
  stage_label   text,
  description   text,                    -- NSE survDesc
  isin          text,
  nse_stamp     text NOT NULL,           -- asmTime / gsmTime as NSE sent it
  captured_at   timestamptz NOT NULL,
  recorded_at   timestamptz NOT NULL DEFAULT now(),
  source_url    text NOT NULL,
  UNIQUE (symbol, framework, stage, session_date)
);
CREATE INDEX surveillance_snapshots_session ON public.surveillance_snapshots (session_date);

CREATE TABLE public.surveillance_captures (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_date  date NOT NULL,
  captured_at   timestamptz NOT NULL,
  recorded_at   timestamptz NOT NULL DEFAULT now(),
  run_label     text NOT NULL,           -- morning | afternoon | close | replay
  ok            boolean NOT NULL,
  asm_rows      integer,
  gsm_rows      integer,
  error         text,
  CHECK (ok = (error IS NULL)),
  CHECK (NOT ok OR (asm_rows IS NOT NULL AND gsm_rows IS NOT NULL))
);
CREATE INDEX surveillance_captures_session ON public.surveillance_captures (session_date, ok);


CREATE OR REPLACE FUNCTION public.surveillance_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'surveillance record is append-only: % on % is refused. '
                  'A snapshot is what NSE said that day; it is never corrected.',
                  TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION public.surveillance_stamp()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.recorded_at := now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER surveillance_snapshots_no_update_delete
  BEFORE UPDATE OR DELETE ON public.surveillance_snapshots
  FOR EACH ROW EXECUTE FUNCTION public.surveillance_append_only();
CREATE TRIGGER surveillance_snapshots_no_truncate
  BEFORE TRUNCATE ON public.surveillance_snapshots
  FOR EACH STATEMENT EXECUTE FUNCTION public.surveillance_append_only();
CREATE TRIGGER surveillance_snapshots_stamp
  BEFORE INSERT ON public.surveillance_snapshots
  FOR EACH ROW EXECUTE FUNCTION public.surveillance_stamp();

CREATE TRIGGER surveillance_captures_no_update_delete
  BEFORE UPDATE OR DELETE ON public.surveillance_captures
  FOR EACH ROW EXECUTE FUNCTION public.surveillance_append_only();
CREATE TRIGGER surveillance_captures_no_truncate
  BEFORE TRUNCATE ON public.surveillance_captures
  FOR EACH STATEMENT EXECUTE FUNCTION public.surveillance_append_only();
CREATE TRIGGER surveillance_captures_stamp
  BEFORE INSERT ON public.surveillance_captures
  FOR EACH ROW EXECUTE FUNCTION public.surveillance_stamp();


ALTER TABLE public.surveillance_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.surveillance_captures  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.surveillance_snapshots FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.surveillance_captures  FROM PUBLIC, anon, authenticated;
REVOKE UPDATE, DELETE, TRUNCATE ON public.surveillance_snapshots FROM service_role;
REVOKE UPDATE, DELETE, TRUNCATE ON public.surveillance_captures  FROM service_role;
GRANT SELECT, INSERT ON public.surveillance_snapshots TO service_role;
GRANT SELECT, INSERT ON public.surveillance_captures  TO service_role;

COMMENT ON TABLE public.surveillance_snapshots IS
  'NSE GSM/ASM lists as captured on each session. NSE serves only the current list, so this is the only record. Append-only by trigger; unique on (symbol, framework, stage, session_date).';
COMMENT ON TABLE public.surveillance_captures IS
  'Every capture attempt, ok or not. A session counts as captured only with an ok row here.';
