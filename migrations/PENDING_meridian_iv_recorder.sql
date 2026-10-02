-- MERIDIAN — the IV recorder's three tables.
--
-- WHY THIS EXISTS BEFORE ANYTHING ELSE IN MERIDIAN. IV percentile is the only
-- input in the design with a clock on it: it needs a year of daily history and no
-- API sells that history retrospectively. Every other part of MERIDIAN can be
-- built in a week whenever; this one cannot be caught up. So the recorder ships
-- first and alone, and starts accumulating while the rest is still a document.
--
-- SEPARATE FROM ATLAS, DELIBERATELY. Nothing here is read or written by any ATLAS
-- module. The two verticals share this Postgres database, the Supabase service
-- key, and the Upstox access token FILE -- which meridian reads and never
-- refreshes, because a bad rewrite of that file would take out the equity price
-- feed. That is the full extent of the coupling and it is worth stating rather
-- than claiming isolation we do not have.
--
-- PRIVATE, AND STRICTER THAN THE REST OF THIS PROJECT BY INTENT. Every other
-- table here grants SELECT to anon -- the screener is a public page. These grant
-- to `authenticated` AND require an active subscriber row. A signed-in account
-- that is not a subscriber reads nothing. Operator decision, 2026-10-02: this is
-- the first thing in the project where private has to mean private.

-- ─────────────────────────────────────────────────────────────────────
-- 1. The gate. SECURITY DEFINER so the policy can read `subscribers`
--    without the caller needing rights on it, and so a policy on
--    subscribers itself cannot recurse. Mirrors public.is_admin().
-- ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.is_active_subscriber()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.subscribers s
     WHERE s.user_id = auth.uid()
       AND s.status  = 'active'
  );
$$;

REVOKE ALL ON FUNCTION public.is_active_subscriber() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.is_active_subscriber() FROM anon;
GRANT EXECUTE ON FUNCTION public.is_active_subscriber() TO authenticated, service_role;

COMMENT ON FUNCTION public.is_active_subscriber() IS
  'True when the caller is a signed-in user with an active subscribers row. Not callable by anon. Used by the MERIDIAN policies, which are deliberately stricter than the public screener tables: signed in is not sufficient, subscribed is required.';

-- ─────────────────────────────────────────────────────────────────────
-- 2. The ATM series. This is what IV percentile is computed from.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.meridian_iv_daily (
  trade_date        date        NOT NULL,
  underlying_symbol text        NOT NULL,
  underlying_key    text        NOT NULL,
  kind              text        NOT NULL,
  spot              numeric,
  expiry_date       date,
  days_to_expiry    integer,
  atm_strike        numeric,
  atm_iv            numeric,
  atm_call_iv       numeric,
  atm_put_iv        numeric,
  india_vix         numeric,
  pcr               numeric,
  total_call_oi     numeric,
  total_put_oi      numeric,
  strikes_recorded  integer     NOT NULL DEFAULT 0,
  expiry_keyword    text,
  snapshot_taken_at timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- One reading per underlying per day. Upsert on it, so re-running the recorder
-- replaces rather than appends -- two rows for one underlying-day is an
-- ambiguity that a percentile calculation would silently average.
ALTER TABLE public.meridian_iv_daily
  ADD CONSTRAINT meridian_iv_daily_key UNIQUE (trade_date, underlying_symbol);

-- The percentile query is "this underlying, last 252 sessions", so the index is
-- on that order and not on trade_date alone.
CREATE INDEX IF NOT EXISTS meridian_iv_daily_series_idx
  ON public.meridian_iv_daily (underlying_symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS meridian_iv_daily_date_idx
  ON public.meridian_iv_daily (trade_date DESC);

-- ─────────────────────────────────────────────────────────────────────
-- 3. Strike-level detail, BOUNDED TO ATM +/- 10.
--
--    The full chain would be 8,000-12,000 rows a night and 2-3 million a
--    year, against a database that currently holds about 20,000 rows in
--    total. ATM +/- 10 keeps the smile and the skew -- which is what the
--    strike detail is for -- and cuts it to roughly a third. The tails are
--    data that would rarely be queried, and a row never read is a row that
--    only costs.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.meridian_iv_strikes (
  trade_date        date        NOT NULL,
  underlying_symbol text        NOT NULL,
  expiry_date       date        NOT NULL,
  strike            numeric     NOT NULL,
  strike_offset     integer     NOT NULL,
  spot              numeric,
  call_iv           numeric,
  call_oi           numeric,
  call_volume       numeric,
  call_ltp          numeric,
  call_delta        numeric,
  call_gamma        numeric,
  call_theta        numeric,
  call_vega         numeric,
  put_iv            numeric,
  put_oi            numeric,
  put_volume        numeric,
  put_ltp           numeric,
  put_delta         numeric,
  put_gamma         numeric,
  put_theta         numeric,
  put_vega          numeric,
  snapshot_taken_at timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.meridian_iv_strikes
  ADD CONSTRAINT meridian_iv_strikes_key
  UNIQUE (trade_date, underlying_symbol, expiry_date, strike);

CREATE INDEX IF NOT EXISTS meridian_iv_strikes_lookup_idx
  ON public.meridian_iv_strikes (underlying_symbol, trade_date DESC, strike_offset);

-- ─────────────────────────────────────────────────────────────────────
-- 4. Run accounting. EVERY run writes here, success or failure.
--
--    The failure this table exists to prevent is a recorder that writes
--    nothing for three months. That failure is NOT caught by the recorder's
--    own error handling, because the case which produces it is the recorder
--    never running at all -- so there are two mechanisms: this table, which
--    records what each run did, and an independent staleness check in the
--    15:30 engine report, which fires when the newest trade_date is stale
--    whether or not the recorder ran.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.meridian_recorder_runs (
  id                   bigserial PRIMARY KEY,
  run_label            text        NOT NULL,
  trade_date           date,
  started_at           timestamptz NOT NULL,
  finished_at          timestamptz,
  status               text        NOT NULL,
  underlyings_expected integer     NOT NULL DEFAULT 0,
  underlyings_written  integer     NOT NULL DEFAULT 0,
  strikes_written      integer     NOT NULL DEFAULT 0,
  india_vix            numeric,
  api_calls            integer     NOT NULL DEFAULT 0,
  duration_s           numeric,
  failures             jsonb       NOT NULL DEFAULT '[]'::jsonb,
  list_diff            jsonb       NOT NULL DEFAULT '{}'::jsonb,
  notes                text,
  created_at           timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.meridian_recorder_runs
  ADD CONSTRAINT meridian_recorder_runs_status_chk
  CHECK (status IN ('OK', 'PARTIAL', 'FAILED')) NOT VALID;

ALTER TABLE public.meridian_recorder_runs
  VALIDATE CONSTRAINT meridian_recorder_runs_status_chk;

CREATE INDEX IF NOT EXISTS meridian_recorder_runs_recent_idx
  ON public.meridian_recorder_runs (started_at DESC);

-- ─────────────────────────────────────────────────────────────────────
-- 5. RLS. authenticated AND subscribed. No anon grant anywhere.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.meridian_iv_daily      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meridian_iv_strikes    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meridian_recorder_runs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "subscriber read meridian_iv_daily"
  ON public.meridian_iv_daily
  FOR SELECT TO authenticated
  USING (public.is_active_subscriber());

CREATE POLICY "subscriber read meridian_iv_strikes"
  ON public.meridian_iv_strikes
  FOR SELECT TO authenticated
  USING (public.is_active_subscriber());

-- Run accounting is operator diagnostics, not product. Admins only.
CREATE POLICY "admin read meridian_recorder_runs"
  ON public.meridian_recorder_runs
  FOR SELECT TO authenticated
  USING (public.is_admin());

-- Writes are service_role throughout, which bypasses RLS. No INSERT or UPDATE
-- policy is created on purpose: there is no path by which a signed-in user can
-- write to any of these.

-- ─────────────────────────────────────────────────────────────────────
-- 6. Comments
-- ─────────────────────────────────────────────────────────────────────

COMMENT ON TABLE public.meridian_iv_daily IS
  'MERIDIAN. One ATM implied-volatility reading per underlying per session, written nightly by meridian/iv_recorder.py from the Upstox option chain. This is the series IV percentile is computed from, and it is the reason the recorder was built before the rest of MERIDIAN: a year of history cannot be bought retrospectively. Reads nothing from and writes nothing to any ATLAS table. Private: authenticated AND an active subscriber.';

COMMENT ON COLUMN public.meridian_iv_daily.kind IS
  'index or stock. Determines the expiry keyword: NIFTY is the only NSE index with weeklies, so it uses current_week; the other three indices and every stock are monthly-only and use current_month. Stored because a wrong keyword silently records the wrong contract, which would poison a year of history before anyone noticed.';

COMMENT ON COLUMN public.meridian_iv_daily.snapshot_taken_at IS
  'When the chain was actually fetched, UTC. Recorded from day one deliberately: the right run time is not yet established -- 15:45 IST reflects the close, 18:35 IST follows the EOD chain, and open interest settles somewhere between them. The recorder runs at BOTH for one session to compare. Without this column a year of history taken at a drifting time would be uninterpretable rather than merely imperfect.';

COMMENT ON COLUMN public.meridian_iv_daily.atm_iv IS
  'Mean of atm_call_iv and atm_put_iv where both are present, otherwise whichever exists. Both legs are stored separately so the blend can be recomputed differently later without re-fetching a year of chains.';

COMMENT ON TABLE public.meridian_iv_strikes IS
  'MERIDIAN. Strike-level IV, OI and greeks, BOUNDED TO ATM +/- 10 strikes by operator decision 2026-10-02. The full chain would be 2-3 million rows a year against a database holding ~20k; this band keeps the smile and the skew, which is what strike detail is for, and drops tails that would rarely be queried. strike_offset is signed: 0 is ATM, negative below, positive above.';

COMMENT ON TABLE public.meridian_recorder_runs IS
  'MERIDIAN. One row per recorder invocation, written whether it succeeded or not. Half of the loud-failure design: this says what each run did, and an independent staleness check in the 15:30 engine report says when no run has happened at all. The second half is the one that matters, because a recorder that never starts cannot report its own absence.';

COMMENT ON COLUMN public.meridian_recorder_runs.list_diff IS
  'Symbols where the Upstox instrument master and the NSE F&O list disagree, reported rather than reconciled. The F&O list is revised by the exchange periodically and is never hardcoded here; a symbol present in one source and absent from the other is a fact the operator should see, not something for the recorder to resolve on its own.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. Every column must come back true. Apply ONE STATEMENT AT A TIME --
-- "Success" from the SQL editor is not evidence, see migrations/README.md.
-- Three of the last five migrations applied only a prefix of their statements.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname = 'is_active_subscriber')      AS gate_fn,
  (SELECT count(*) = 3 FROM pg_tables WHERE schemaname = 'public'
     AND tablename IN ('meridian_iv_daily','meridian_iv_strikes',
                       'meridian_recorder_runs'))                           AS three_tables,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname = 'meridian_iv_daily_key')                               AS daily_key,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname = 'meridian_iv_strikes_key')                             AS strikes_key,
  (SELECT convalidated FROM pg_constraint
     WHERE conname = 'meridian_recorder_runs_status_chk')                   AS status_check,
  (SELECT count(*) = 4 FROM pg_indexes WHERE schemaname = 'public'
     AND indexname IN ('meridian_iv_daily_series_idx','meridian_iv_daily_date_idx',
                       'meridian_iv_strikes_lookup_idx',
                       'meridian_recorder_runs_recent_idx'))                AS four_indexes,
  (SELECT bool_and(relrowsecurity) FROM pg_class
     WHERE oid IN ('public.meridian_iv_daily'::regclass,
                   'public.meridian_iv_strikes'::regclass,
                   'public.meridian_recorder_runs'::regclass))              AS rls_all_on,
  (SELECT count(*) = 3 FROM pg_policies WHERE schemaname = 'public'
     AND tablename LIKE 'meridian_%')                                       AS three_policies,
  (SELECT bool_and(NOT ('anon' = ANY(roles))) FROM pg_policies
     WHERE schemaname = 'public' AND tablename LIKE 'meridian_%')           AS no_anon_grant,
  (SELECT bool_and(obj_description(c.oid) IS NOT NULL) FROM pg_class c
     WHERE c.oid IN ('public.meridian_iv_daily'::regclass,
                     'public.meridian_iv_strikes'::regclass,
                     'public.meridian_recorder_runs'::regclass))            AS table_comments;
