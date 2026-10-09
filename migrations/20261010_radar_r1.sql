-- TSL FLASH — RADAR R1. Six tables, three read views, one config seed.
--
-- SEE docs/TSL_FLASH.md. This file is the schema for the deterministic Radar
-- slice: event detection, the extremes scanner, heat and the Hot 10, levels
-- computed by code, and forward-outcome recording for every detected event.
--
-- SHARES NOTHING WITH signals_* OR atlas_*. Not a column, not a view, not a
-- policy. Radar reads the signals chain's bhavcopy output from disk and writes
-- only radar_* tables. ATLAS is halted pending a live broker test and nothing
-- here touches it.
--
-- ══ APPLY ONE STATEMENT AT A TIME ══════════════════════════════════════════
--
-- The Supabase SQL editor has twice applied only a PREFIX of a pasted file and
-- reported success -- 3 of 12 statements once, 1 of 6 another time -- and the
-- cut-off is not at a consistent position. Every statement below is therefore
-- independently runnable and idempotent: IF NOT EXISTS on tables and indexes,
-- DROP ... IF EXISTS before each policy and view, ON CONFLICT on the seed.
-- Re-running any single statement is safe.
--
-- §13 at the end is a catalogue query that proves each object exists. "No
-- error" is not evidence; that query is.
--
-- ══ RLS ════════════════════════════════════════════════════════════════════
--
-- Base tables: anon has NO access and NO policy. Writes are service-role only.
-- The page reads three narrow views granted to anon, carrying display columns
-- only. docs/TSL_FLASH.md §11 records why, and the one-line change to make
-- Flash fully private instead.
--
-- Note the database has an event trigger (ensure_rls -> rls_auto_enable) that
-- enables RLS on every new public table and creates no policy. The ENABLE
-- statements below are belt-and-braces: explicit beats inherited when the
-- consequence of the trigger not firing is a public table.


-- ─────────────────────────────────────────────────────────────────────
-- 01. radar_events — one row per detected event
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.radar_events (
  id            bigserial    PRIMARY KEY,
  symbol        text         NOT NULL,
  sector        text,
  trigger_type  text         NOT NULL,
  detected_at   timestamptz  NOT NULL DEFAULT now(),
  -- The SESSION the event was detected ON, which is not the same as the
  -- timestamp it was written at: detection runs at 19:10 IST against the
  -- 15:30 close, and a replay writes historical sessions at today's clock.
  -- Heat decay and the outcome window both count from this, never from
  -- detected_at.
  event_date    date         NOT NULL,
  status        text         NOT NULL DEFAULT 'live',
  heat          numeric      NOT NULL DEFAULT 0,
  severity      numeric,
  -- What the trigger actually measured, kept so a card can be re-rendered and
  -- a threshold change can be re-scored without re-reading bhavcopy.
  move_pct      numeric,
  vol_multiple  numeric,
  direction     text,
  outcome       jsonb        NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz  NOT NULL DEFAULT now(),
  updated_at    timestamptz  NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_events_status_chk') THEN
    ALTER TABLE public.radar_events ADD CONSTRAINT radar_events_status_chk CHECK (status IN ('live', 'displaced', 'closed'));
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_events_direction_chk') THEN
    ALTER TABLE public.radar_events ADD CONSTRAINT radar_events_direction_chk CHECK (direction IS NULL OR direction IN ('up', 'down'));
  END IF;
END $$;

-- ONE EVENT PER SYMBOL PER SESSION. The dedup rule in docs/TSL_FLASH.md §03
-- keeps a continuing slide from producing five cards, and this constraint is
-- what makes the nightly run idempotent: a re-run upserts rather than appends.
-- Two rows for one symbol-session would double-count in the base-rate library,
-- which is the one consumer that cannot tolerate it.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_events_symbol_date_key') THEN
    ALTER TABLE public.radar_events ADD CONSTRAINT radar_events_symbol_date_key UNIQUE (symbol, event_date);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS radar_events_hot_idx
  ON public.radar_events (status, heat DESC);

CREATE INDEX IF NOT EXISTS radar_events_symbol_idx
  ON public.radar_events (symbol, event_date DESC);

-- The outcome sweep asks "every event whose window is still open", which is
-- this order and not heat order.
CREATE INDEX IF NOT EXISTS radar_events_open_window_idx
  ON public.radar_events (event_date DESC)
  WHERE status <> 'closed';

ALTER TABLE public.radar_events ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.radar_events IS
  'TSL Flash Radar: one row per detected event. status is a DISPLAY state (live|displaced) except closed, which means the outcome window finished. A displaced event keeps accumulating outcome rows -- see docs/TSL_FLASH.md section 07; the base-rate library depends on it and ten survivors a day would be a biased sample.';

COMMENT ON COLUMN public.radar_events.heat IS
  'severity decayed by session age, halving every heat_half_life_sessions. Recomputed every run, so it is a snapshot and not a fact about the event.';

COMMENT ON COLUMN public.radar_events.severity IS
  'max(|move%|/move_threshold%, vol_multiple/vol_threshold). Dimensionless: both terms are multiples over the bar, so the max picks whichever was more extreme.';


-- ─────────────────────────────────────────────────────────────────────
-- 02. radar_documents — every filing seen, with its citation
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.radar_documents (
  doc_id       bigserial    PRIMARY KEY,
  source       text         NOT NULL,
  url          text,
  published_at timestamptz,
  symbols      text[]       NOT NULL DEFAULT '{}',
  -- The hard rule is no cause without a cited document, so the TEXT is kept,
  -- not just a pointer: a URL that 404s a year later must not take the
  -- evidence with it.
  title        text,
  body         text,
  category     text,
  text_hash    text         NOT NULL,
  fetched_at   timestamptz  NOT NULL DEFAULT now()
);

-- text_hash IS THE IDENTITY, not the URL. NSE serves the same announcement at
-- different URLs and re-publishes corrections, so deduping on url would store
-- one filing several times and let the card cite whichever copy sorted first.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_documents_hash_key') THEN
    ALTER TABLE public.radar_documents ADD CONSTRAINT radar_documents_hash_key UNIQUE (text_hash);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS radar_documents_symbols_idx
  ON public.radar_documents USING gin (symbols);

CREATE INDEX IF NOT EXISTS radar_documents_published_idx
  ON public.radar_documents (published_at DESC);

ALTER TABLE public.radar_documents ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.radar_documents IS
  'Exchange filings only -- NSE corporate-announcements, the same endpoint engine/tier1_fetch.py uses. NO scraped news sites, by hard rule. Used for cause TEXT and never for a number; every number on a card traces to bhavcopy.';


-- ─────────────────────────────────────────────────────────────────────
-- 03. radar_card_versions — what a reader was shown, per day
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.radar_card_versions (
  id                bigserial    PRIMARY KEY,
  event_id          bigint       NOT NULL
                                 REFERENCES public.radar_events(id)
                                 ON DELETE CASCADE,
  trade_date        date         NOT NULL,
  payload           jsonb        NOT NULL,
  validation_status text         NOT NULL DEFAULT 'pending',
  validation_notes  text,
  created_at        timestamptz  NOT NULL DEFAULT now()
);

-- VERSIONED BY DAY, NOT OVERWRITTEN. The card for an event changes as sessions
-- pass -- heat decays, distances move, outcomes fill in -- and the record of
-- what was on screen on a given date is the only way to audit a claim after
-- the fact.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_card_versions_event_date_key') THEN
    ALTER TABLE public.radar_card_versions ADD CONSTRAINT radar_card_versions_event_date_key UNIQUE (event_id, trade_date);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_card_versions_status_chk') THEN
    ALTER TABLE public.radar_card_versions ADD CONSTRAINT radar_card_versions_status_chk CHECK (validation_status IN ('pending', 'passed', 'failed'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS radar_card_versions_lookup_idx
  ON public.radar_card_versions (event_id, trade_date DESC);

ALTER TABLE public.radar_card_versions ENABLE ROW LEVEL SECURITY;

COMMENT ON COLUMN public.radar_card_versions.validation_status IS
  'failed means the payload broke a hard rule (banned action language, a cause with no cited document, or a missing delivery-%% disclaimer) and must not be served. The row is KEPT rather than discarded so the failure is inspectable.';


-- ─────────────────────────────────────────────────────────────────────
-- 04. radar_levels — one row per (event, level_type)
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.radar_levels (
  id           bigserial    PRIMARY KEY,
  event_id     bigint       NOT NULL
                            REFERENCES public.radar_events(id)
                            ON DELETE CASCADE,
  level_type   text         NOT NULL,
  price        numeric,
  -- Distance from the price current at computed_at. Stored rather than derived
  -- on read so the card version and its levels agree about which day they
  -- describe.
  dist_pct     numeric,
  ref_date     date,
  computed_at  timestamptz  NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_levels_event_type_key') THEN
    ALTER TABLE public.radar_levels ADD CONSTRAINT radar_levels_event_type_key UNIQUE (event_id, level_type);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS radar_levels_event_idx
  ON public.radar_levels (event_id);

ALTER TABLE public.radar_levels ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.radar_levels IS
  'Reference prices computed by pure functions over bhavcopy -- see docs/TSL_FLASH.md section 05. NO model touches these, now or later. level_type is deliberately descriptive (w52_high, event_vwap, dma_50): there is no "entry" or "exit" type and there must never be one.';


-- ─────────────────────────────────────────────────────────────────────
-- 05. radar_scanner_daily — the extremes sweep
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.radar_scanner_daily (
  id             bigserial   PRIMARY KEY,
  trade_date     date        NOT NULL,
  symbol         text        NOT NULL,
  side           text        NOT NULL,
  sector         text,
  close          numeric,
  extreme_price  numeric,
  pct_from_extreme numeric,
  vol_multiple   numeric,
  volume         bigint,
  delivery_pct   numeric,
  -- A filing inside filing_lookback_sessions. A FLAG, not a cause: the scanner
  -- makes no claim about why price is where it is.
  filing_recent  boolean     NOT NULL DEFAULT false,
  -- True when this row also met a Mode A trigger and was promoted to a full
  -- event card. Lets the page link the two without a second query.
  promoted       boolean     NOT NULL DEFAULT false,
  rank_in_side   integer,
  created_at     timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_scanner_daily_side_chk') THEN
    ALTER TABLE public.radar_scanner_daily ADD CONSTRAINT radar_scanner_daily_side_chk CHECK (side IN ('high', 'low'));
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'radar_scanner_daily_key') THEN
    ALTER TABLE public.radar_scanner_daily ADD CONSTRAINT radar_scanner_daily_key UNIQUE (trade_date, symbol, side);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS radar_scanner_daily_date_idx
  ON public.radar_scanner_daily (trade_date DESC, side, rank_in_side);

ALTER TABLE public.radar_scanner_daily ENABLE ROW LEVEL SECURITY;


-- ─────────────────────────────────────────────────────────────────────
-- 06. radar_config — the knobs, editable without a deploy
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.radar_config (
  key         text        PRIMARY KEY,
  value       text        NOT NULL,
  note        text,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.radar_config ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.radar_config IS
  'Read at the start of every Radar run. A key missing here falls back to the default in radar/config.py, and the run logs which values it actually used -- so an edit that does not take effect is visible in the log rather than inferred from output.';


-- ─────────────────────────────────────────────────────────────────────
-- 07. The config seed. ON CONFLICT so re-running does not clobber an
--     operator edit -- the point of this table is editing without a deploy,
--     and a migration that resets it would defeat that.
-- ─────────────────────────────────────────────────────────────────────

INSERT INTO public.radar_config (key, value, note) VALUES
  ('universe',                 'nifty500', 'resolves to engine/universe.ALL_SYMBOLS'),
  ('shock_1d_pct',             '8',        '1-day move threshold, either way'),
  ('shock_1d_vol_mult',        '3.0',      'x the 20-day average volume'),
  ('shock_5d_pct',             '15',       '5-day move threshold, either way'),
  ('shock_5d_vol_mult',        '2.0',      'x 5 x the 20-day average volume'),
  ('scanner_vol_mult',         '2.0',      'Mode B volume floor'),
  ('scanner_top_n_each_side',  '10',       'highs and lows separately'),
  ('hot_cards_n',              '10',       'the page always shows exactly this many'),
  ('heat_half_life_sessions',  '5',        'severity halves every n sessions'),
  ('outcome_window_trading_days', '60',    'after which status becomes closed'),
  ('base_rate_min_n',          '20',       'below this, no base rate is displayed'),
  ('swing_fractal_bars',       '5',        'fractal width for swing pivots'),
  -- Added beyond the brief. Each is listed in docs/TSL_FLASH.md section 12 with
  -- its reason, separately from the briefed keys so the addition is visible.
  ('event_dedup_sessions',     '5',        'ADDED: a continuing slide must not fill the Hot 10'),
  ('vol_avg_window',           '20',       'ADDED: the window the thresholds are stated against'),
  ('filing_lookback_sessions', '5',        'ADDED: window for the scanner filing flag'),
  ('circuit_bands_pct',        '2,5,10,20','ADDED: band widths a locked session is matched against'),
  ('ipo_max_age_years',        '5',        'ADDED: beyond this, ipo_price is dropped'),
  ('rsi_period',               '14',       'ADDED: explicit rather than implied'),
  ('dma_periods',              '50,200',   'ADDED: explicit rather than implied')
ON CONFLICT (key) DO NOTHING;


-- ─────────────────────────────────────────────────────────────────────
-- 08. v_radar_hot10 — what the Hot 10 list renders
--
--     DROP then CREATE, not CREATE OR REPLACE. A REPLACE that does not run
--     leaves the view present, queryable and holding its OLD definition --
--     which is how the wrong-side exclusion shipped as a dashboard claim with
--     nothing behind it for a day. A DROP that does not run makes the CREATE
--     fail loudly instead.
-- ─────────────────────────────────────────────────────────────────────

DROP VIEW IF EXISTS public.v_radar_hot10;

CREATE VIEW public.v_radar_hot10
WITH (security_invoker = true)
AS
SELECT e.id              AS event_id,
       e.symbol,
       e.sector,
       e.trigger_type,
       e.event_date,
       e.direction,
       e.move_pct,
       e.vol_multiple,
       e.severity,
       e.heat,
       e.status,
       -- SESSIONS SINCE THE EVENT COMES FROM THE CARD PAYLOAD, not from SQL.
       -- Postgres has no NSE trading calendar, so any expression here would be
       -- counting calendar days or scanner rows and calling them sessions -- a
       -- card detected on Friday would read three sessions old on Monday.
       -- radar/heat.py computes it against engine/trading_calendar.py, which is
       -- the same calendar the decay uses, so the number on screen and the
       -- number in the arithmetic cannot disagree.
       (c.payload ->> 'sessions_since')::int AS sessions_since,
       -- THE CAUSE IS NESTED UNDER q01.why, NOT AT THE TOP LEVEL. The first
       -- draft of this view read payload->>'cause_text', which exists nowhere
       -- in the payload radar/cards.py builds: every Hot 10 row would have
       -- rendered with a null cause and the page would have shown ten
       -- "no disclosed trigger found" lines on a night when half of them had
       -- a filing. tests/test_radar.py now walks every JSON path this view
       -- names against a real payload, so the two cannot drift again.
       c.payload -> 'q01' -> 'why' ->> 'cause_text'  AS cause_text,
       c.payload -> 'q01' -> 'why' ->> 'cause_url'   AS cause_url,
       (c.payload -> 'q01' -> 'why' -> 'sourced')::boolean AS cause_sourced,
       c.trade_date                      AS card_trade_date,
       c.validation_status
  FROM public.radar_events e
  LEFT JOIN LATERAL (
       SELECT cv.payload, cv.trade_date, cv.validation_status
         FROM public.radar_card_versions cv
        WHERE cv.event_id = e.id
          AND cv.validation_status = 'passed'
        ORDER BY cv.trade_date DESC
        LIMIT 1
  ) c ON true
 WHERE e.status = 'live'
 ORDER BY e.heat DESC;

COMMENT ON VIEW public.v_radar_hot10 IS
  'Display columns for the Hot 10 list. security_invoker so the caller role is what RLS sees -- a view owned by a superuser would otherwise bypass the base-table policies it exists to narrow.';


-- ─────────────────────────────────────────────────────────────────────
-- 09. v_radar_card_latest — the newest PASSED card per event
-- ─────────────────────────────────────────────────────────────────────

DROP VIEW IF EXISTS public.v_radar_card_latest;

CREATE VIEW public.v_radar_card_latest
WITH (security_invoker = true)
AS
SELECT DISTINCT ON (cv.event_id)
       cv.event_id,
       e.symbol,
       e.sector,
       e.trigger_type,
       e.event_date,
       e.direction,
       e.heat,
       e.severity,
       e.status,
       e.outcome,
       cv.trade_date,
       cv.payload,
       cv.validation_status
  FROM public.radar_card_versions cv
  JOIN public.radar_events e ON e.id = cv.event_id
 -- FAILED CARDS ARE NOT SERVED. The row is kept for inspection; the view is
 -- what the page reads, so a payload that broke a hard rule cannot reach it.
 WHERE cv.validation_status = 'passed'
 ORDER BY cv.event_id, cv.trade_date DESC;


-- ─────────────────────────────────────────────────────────────────────
-- 10. v_radar_scanner_today — the latest sweep, both sides
-- ─────────────────────────────────────────────────────────────────────

DROP VIEW IF EXISTS public.v_radar_scanner_today;

CREATE VIEW public.v_radar_scanner_today
WITH (security_invoker = true)
AS
SELECT s.trade_date, s.symbol, s.side, s.sector, s.close,
       s.extreme_price, s.pct_from_extreme, s.vol_multiple,
       s.volume, s.delivery_pct, s.filing_recent, s.promoted,
       s.rank_in_side
  FROM public.radar_scanner_daily s
 WHERE s.trade_date = (SELECT max(trade_date) FROM public.radar_scanner_daily)
 ORDER BY s.side, s.rank_in_side;


-- ─────────────────────────────────────────────────────────────────────
-- 11. GRANTS. Base tables stay closed to anon with no policy; the three
--     views above are the only public surface.
-- ─────────────────────────────────────────────────────────────────────

REVOKE ALL ON public.radar_events        FROM anon;
REVOKE ALL ON public.radar_documents     FROM anon;
REVOKE ALL ON public.radar_card_versions FROM anon;
REVOKE ALL ON public.radar_levels        FROM anon;
REVOKE ALL ON public.radar_scanner_daily FROM anon;
REVOKE ALL ON public.radar_config        FROM anon;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.radar_events        TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.radar_documents     TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.radar_card_versions TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.radar_levels        TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.radar_scanner_daily TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.radar_config        TO service_role;

GRANT USAGE, SELECT ON SEQUENCE public.radar_events_id_seq            TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.radar_documents_doc_id_seq     TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.radar_card_versions_id_seq     TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.radar_levels_id_seq            TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.radar_scanner_daily_id_seq     TO service_role;

-- THE PUBLIC SURFACE. security_invoker is set on all three, so a view read as
-- anon still meets the base-table policies -- and there are none for anon.
-- These policies are what makes the views readable, scoped to exactly the rows
-- the views already filter to.
DROP POLICY IF EXISTS "anon read live radar events" ON public.radar_events;
CREATE POLICY "anon read live radar events"
  ON public.radar_events FOR SELECT
  TO anon, authenticated
  USING (status = 'live');

DROP POLICY IF EXISTS "anon read passed radar cards" ON public.radar_card_versions;
CREATE POLICY "anon read passed radar cards"
  ON public.radar_card_versions FOR SELECT
  TO anon, authenticated
  USING (validation_status = 'passed');

DROP POLICY IF EXISTS "anon read radar levels" ON public.radar_levels;
CREATE POLICY "anon read radar levels"
  ON public.radar_levels FOR SELECT
  TO anon, authenticated
  USING (true);

DROP POLICY IF EXISTS "anon read radar scanner" ON public.radar_scanner_daily;
CREATE POLICY "anon read radar scanner"
  ON public.radar_scanner_daily FOR SELECT
  TO anon, authenticated
  USING (true);

-- NO ANON POLICY ON radar_documents OR radar_config, deliberately. The cause
-- TEXT and URL a card cites travel inside the card payload, which is already
-- public; the full filing corpus and the operator's knobs are not a public
-- surface. RLS with no policy answers anon with 200 and an empty array, so
-- this is also the one place where that silence is the intended behaviour.

GRANT SELECT ON public.v_radar_hot10          TO anon, authenticated, service_role;
GRANT SELECT ON public.v_radar_card_latest    TO anon, authenticated, service_role;
GRANT SELECT ON public.v_radar_scanner_today  TO anon, authenticated, service_role;


-- ─────────────────────────────────────────────────────────────────────
-- 12. updated_at trigger on radar_events. The outcome sweep touches rows
--     every night and "when did this last change" is the first question
--     asked of a row that looks stale.
-- ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.radar_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS radar_events_touch ON public.radar_events;
CREATE TRIGGER radar_events_touch
  BEFORE UPDATE ON public.radar_events
  FOR EACH ROW EXECUTE FUNCTION public.radar_touch_updated_at();


-- ─────────────────────────────────────────────────────────────────────
-- 13. VERIFY. Run this LAST and read it. "Success" from the editor
--     describes the last statement it chose to run, not the file.
--
--     Expect: 6 tables, 3 views, 19 config rows, 4 anon policies,
--     5 unique constraints, 1 trigger,
--     and every `ok` column true.
-- ─────────────────────────────────────────────────────────────────────

SELECT 'tables' AS object_class,
       count(*) AS found,
       6        AS expected,
       count(*) = 6 AS ok,
       string_agg(tablename, ', ' ORDER BY tablename) AS names
  FROM pg_tables
 WHERE schemaname = 'public' AND tablename LIKE 'radar_%'
UNION ALL
SELECT 'views', count(*), 3, count(*) = 3,
       string_agg(viewname, ', ' ORDER BY viewname)
  FROM pg_views
 WHERE schemaname = 'public' AND viewname LIKE 'v_radar_%'
UNION ALL
SELECT 'config rows', count(*), 19, count(*) = 19, NULL
  FROM public.radar_config
UNION ALL
SELECT 'rls enabled', count(*), 6, count(*) = 6, NULL
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relname LIKE 'radar_%'
   AND c.relkind = 'r' AND c.relrowsecurity
UNION ALL
SELECT 'anon select policies', count(*), 4, count(*) = 4,
       string_agg(tablename || ':' || policyname, ', ' ORDER BY tablename)
  FROM pg_policies
 WHERE schemaname = 'public' AND tablename LIKE 'radar_%'
   AND 'anon' = ANY(roles)
UNION ALL
-- DOCUMENTS AND CONFIG MUST HAVE NO ANON POLICY. This row is the one that
-- proves the private half stayed private; a passing count above says nothing
-- about it.
SELECT 'radar_documents+config anon policies', count(*), 0, count(*) = 0, NULL
  FROM pg_policies
 WHERE schemaname = 'public'
   AND tablename IN ('radar_documents', 'radar_config')
   AND 'anon' = ANY(roles)
UNION ALL
-- FIVE, not six: radar_config is keyed by a PRIMARY KEY, which is contype 'p'
-- and deliberately not counted here.
SELECT 'unique constraints', count(*), 5, count(*) = 5,
       string_agg(conname, ', ' ORDER BY conname)
  FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname = 'public' AND t.relname LIKE 'radar_%' AND c.contype = 'u'
UNION ALL
SELECT 'updated_at trigger', count(*), 1, count(*) = 1, NULL
  FROM pg_trigger
 WHERE tgname = 'radar_events_touch' AND NOT tgisinternal
UNION ALL
-- THE VIEWS MUST ACTUALLY BE THE NEW ONES. A DROP that silently did not run
-- leaves a stale view that still answers queries, so check for a string only
-- the new definition contains rather than for the view's existence.
SELECT 'v_radar_hot10 is the new definition', count(*), 1, count(*) = 1, NULL
  FROM pg_views
 WHERE schemaname = 'public' AND viewname = 'v_radar_hot10'
   AND definition LIKE '%cause_sourced%'
UNION ALL
SELECT 'v_radar_card_latest filters to passed', count(*), 1, count(*) = 1, NULL
  FROM pg_views
 WHERE schemaname = 'public' AND viewname = 'v_radar_card_latest'
   AND definition LIKE '%passed%';
