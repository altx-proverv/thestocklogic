-- The market flash: one short desk note per session, generated after the EOD
-- chain and served from here.
--
-- WHY A TABLE. The text is generated ONCE per session by a model call. A page
-- view must never trigger that call -- it would be a cost and a latency per
-- reader, and 500 readers would get 500 different sentences about the same day.
-- So it is generated server-side at the end of the chain and stored.
--
-- WHY THE INPUTS ARE STORED ALONGSIDE. The generator is forbidden to invent a
-- number, and a prompt cannot enforce that. The validator checks every numeric
-- token in the text against the inputs before storing, and keeping the inputs
-- means any figure in a published flash can still be checked against the
-- snapshot that produced it months later. Without this the rule is aspirational.
--
-- source DISTINGUISHES THE TWO PATHS, permanently. 'template' is not a
-- placeholder or a degraded mode -- it is what runs whenever the API is down, the
-- key is missing, or the validator rejects the generated text, and it is built
-- from the same inputs. A flash is always available; the column says which path
-- produced the one on screen.

CREATE TABLE IF NOT EXISTS public.market_flash (
  session_date timestamptz  NOT NULL,
  flash_text   text         NOT NULL,
  source       text         NOT NULL DEFAULT 'template',
  model        text,
  inputs       jsonb        NOT NULL DEFAULT '{}'::jsonb,
  reject_reason text,
  generated_at timestamptz  NOT NULL DEFAULT now()
);

-- One flash per session. Upsert on it, so a re-run of the chain replaces rather
-- than appends -- the page reads the newest row and two rows for one date is an
-- ambiguity nothing resolves.
ALTER TABLE public.market_flash
  ADD CONSTRAINT market_flash_session_date_key UNIQUE (session_date);

ALTER TABLE public.market_flash
  ADD CONSTRAINT market_flash_source_chk
  CHECK (source IN ('llm', 'template')) NOT VALID;

ALTER TABLE public.market_flash VALIDATE CONSTRAINT market_flash_source_chk;

CREATE INDEX IF NOT EXISTS market_flash_session_idx
  ON public.market_flash (session_date DESC);

ALTER TABLE public.market_flash ENABLE ROW LEVEL SECURITY;

-- anon AND authenticated, for the same reason live_prices needed both: the page
-- swaps Authorization to the user's access_token once signed in, so anon alone
-- breaks it for the signed-in reader. Read-only; writes are service_role.
CREATE POLICY "anon read market_flash"
  ON public.market_flash
  FOR SELECT
  TO anon, authenticated
  USING (true);

COMMENT ON TABLE public.market_flash IS
  'One two-to-three sentence market note per session, written by engine/market_flash.py at the end of the EOD chain. Describes conditions; never advises. Every number in flash_text is validated against `inputs` before the row is written, and a failed validation or a failed API call stores the deterministic template instead -- so there is always a flash and it is never stale prose from a previous session. The page refuses to show a row whose session_date is older than the current signal batch.';

COMMENT ON COLUMN public.market_flash.source IS
  'llm = the model wrote it and it passed validation. template = the deterministic line from the same inputs, which runs whenever the API is unreachable, the key is absent, or validation rejected the generated text. The template is permanent infrastructure, not a placeholder.';

COMMENT ON COLUMN public.market_flash.inputs IS
  'The exact figures the text was generated from, stored so that "never invent a number" stays auditable after the fact rather than being taken on trust.';

COMMENT ON COLUMN public.market_flash.reject_reason IS
  'Set when source=template because generated text was REJECTED rather than unavailable -- names which rule failed (an unaccounted number, advisory language, or shape). Kept because a validator that silently falls back cannot be tuned.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true. One statement at a time -- see migrations/README.md.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM pg_tables
     WHERE schemaname='public' AND tablename='market_flash')            AS table_exists,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='market_flash_session_date_key')                     AS upsert_key,
  (SELECT convalidated FROM pg_constraint
     WHERE conname='market_flash_source_chk')                           AS source_check,
  (SELECT count(*) = 1 FROM pg_indexes
     WHERE schemaname='public' AND indexname='market_flash_session_idx') AS session_index,
  (SELECT relrowsecurity FROM pg_class
     WHERE oid='public.market_flash'::regclass)                         AS rls_on,
  (SELECT 'anon' = ANY(roles) AND 'authenticated' = ANY(roles) FROM pg_policies
     WHERE schemaname='public' AND tablename='market_flash'
       AND policyname='anon read market_flash')                         AS both_roles,
  (SELECT obj_description('public.market_flash'::regclass) IS NOT NULL) AS table_comment;
