-- Audit 2 and 4: stop dropping what 03b computes, and record which commit wrote
-- the row.
--
-- THE BOTTLENECK WAS NEVER MISSING FEATURES. 03b computes 129 columns per
-- stock-day. 06_push's row literal carried 36 of them, so 100 were discarded
-- every night -- including adx, zone_age_days, the breadth counts that gate the
-- trade, and the disqualify reason for every row that did not qualify. The last
-- feature analysis concluded "nothing separates" partly from a feature set the
-- pipeline had already thrown away.
--
-- A SECOND TABLE, NOT 54 MORE COLUMNS ON signals. `signals` is the
-- product-facing row: signals.html renders it, mark_signals and update_outcomes
-- resolve it, market_open trades from it. Putting learning-loop inputs there
-- means every future feature change touches the table subscribers read. Here,
-- schema churn costs nothing.
--
-- Keyed (signal_date, symbol, direction) rather than by signals.id, so a feature
-- row can be written before, after or without its signal row -- and so a
-- re-published batch replaces features instead of orphaning them.

CREATE TABLE IF NOT EXISTS public.signals_features (
  signal_date             date    NOT NULL,
  symbol                  text    NOT NULL,
  direction               text    NOT NULL,
  adx                     numeric,
  adx_ranging             integer,
  adx_trending            integer,
  atr                     numeric,
  recent_bos_choch        integer,
  zone_age_days           numeric,
  zone_dist_pct           numeric,
  near_demand_ob          boolean,
  near_supply_ob          boolean,
  price_in_bull_fvg       boolean,
  price_in_bear_fvg       boolean,
  bos_bull                boolean,
  bos_bear                boolean,
  choch_bull              boolean,
  choch_bear              boolean,
  bull_liq_sweep          boolean,
  bear_liq_sweep          boolean,
  active_demand_ob_high   numeric,
  active_demand_ob_low    numeric,
  active_supply_ob_high   numeric,
  active_supply_ob_low    numeric,
  active_bull_fvg_high    numeric,
  active_bull_fvg_low     numeric,
  active_bear_fvg_high    numeric,
  active_bear_fvg_low     numeric,
  advance_count           numeric,
  decline_count           numeric,
  nifty_close             numeric,
  ad_ratio                numeric,
  no_trade_zone           integer,
  rs_5d                   numeric,
  rs_20d                  numeric,
  rs_positive             integer,
  in_discount             integer,
  in_premium              integer,
  equilibrium             numeric,
  delivery_avg            numeric,
  high_delivery           integer,
  institutional_buying    integer,
  vol_avg20               numeric,
  vol_spike               integer,
  vol_confirming          integer,
  accumulation_score      numeric,
  is_accumulation         boolean,
  close                   numeric,
  prev_close              numeric,
  high                    numeric,
  low                     numeric,
  volume                  integer,
  is_warmup               boolean,
  qualifies               boolean,
  entry_valid             boolean,
  disqualify_reason       text,
  reject_reason           text,
  engine_sha              text,
  created_at              timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.signals_features
  ADD CONSTRAINT signals_features_key
  UNIQUE (signal_date, symbol, direction);

-- The join every analysis makes, and the one a per-symbol series needs.
CREATE INDEX IF NOT EXISTS signals_features_join_idx
  ON public.signals_features (signal_date, symbol, direction);
CREATE INDEX IF NOT EXISTS signals_features_series_idx
  ON public.signals_features (symbol, signal_date DESC);

ALTER TABLE public.signals_features ENABLE ROW LEVEL SECURITY;

-- Same reach as signals: the screener is public and these are the inputs behind
-- what it shows. Read-only; writes are service_role, which bypasses RLS.
CREATE POLICY "anon read signals_features"
  ON public.signals_features
  FOR SELECT TO anon, authenticated
  USING (true);

COMMENT ON TABLE public.signals_features IS
  'The 54 columns 03b computes and 06_push used to discard. Separate from `signals` deliberately: that row is read by the page, the resolvers and the trading loop, so it should change rarely, while this one exists to churn as the learning loop wants new inputs. Keyed (signal_date, symbol, direction). Written for EVERY scored row, qualifying or not -- the disqualify reason on a row that did not publish is the negative class''s only explanation and it was being thrown away.';

COMMENT ON COLUMN public.signals_features.disqualify_reason IS
  'Why the 03b disqualifier block rejected this row, or empty if it did not. The entire explanation of the negative class, discarded nightly until 2026-10-02.';

COMMENT ON COLUMN public.signals_features.reject_reason IS
  'Why the zone gate rejected this row (no active zone, wrong side, stop outside the band, no structural stop). Distinct from disqualify_reason: that is the setup failing, this is the geometry failing.';

COMMENT ON COLUMN public.signals_features.zone_dist_pct IS
  'KNOWN DEFECT, carried as-is rather than silently corrected. active_zones computes this to the zone MIDPOINT while its own docstring and the 15% filter use the EDGE, so 975 historical rows exceed a bound the filter applied correctly. Recorded here so the discrepancy is visible in the data rather than only in a code comment.';

COMMENT ON COLUMN public.signals_features.active_demand_ob_high IS
  'The per-side zone families, all eight columns. The signal row records which family was USED (zone_source); these record what the alternatives were, which is what makes "was the chosen family the better one" answerable -- the question the wrong-side-zone defect raised and nothing could answer.';

COMMENT ON COLUMN public.signals_features.is_warmup IS
  'True for the first 50 bars of a symbol''s history, where indicators are not yet meaningful. Recorded rather than filtered so a feature study can exclude them explicitly instead of wondering why early rows behave oddly.';

-- ─────────────────────────────────────────────────────────────────────
-- PROVENANCE. Every era boundary in the audit had to be inferred from which
-- columns are null. That worked only because the deployments happened weeks
-- apart; two in one week and it stops working, with nothing else in the row
-- to say which rules produced it.
--
-- NULL means "written before provenance existed" and is distinguishable from
-- the string 'unknown', which means "the code could not read its own SHA".
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.signals       ADD COLUMN IF NOT EXISTS engine_sha text;
ALTER TABLE public.signals       ADD COLUMN IF NOT EXISTS engine_ran_at timestamptz;
ALTER TABLE public.atlas_trades  ADD COLUMN IF NOT EXISTS engine_sha text;
ALTER TABLE public.atlas_entry_log ADD COLUMN IF NOT EXISTS engine_sha text;

COMMENT ON COLUMN public.signals.engine_sha IS
  'Short git SHA of the code that wrote this row, from engine/provenance.py. NULL = written before 2026-10-02, when provenance did not exist. The literal ''unknown'' = the SHA could not be read at run time. The two are different facts and are stored differently.';

COMMENT ON COLUMN public.signals.engine_ran_at IS
  'When the chain wrote this row, UTC. Distinct from signal_date (the batch''s trading date) and from created_at (the database default): a batch re-run days later has the same signal_date and a later engine_ran_at, which is the only way to tell a replay from an original.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM pg_tables
     WHERE schemaname='public' AND tablename='signals_features')            AS table_exists,
  (SELECT count(*) = 54 + 5 FROM information_schema.columns
     WHERE table_name='signals_features')                                  AS all_columns,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='signals_features_key')                                 AS upsert_key,
  (SELECT count(*) = 2 FROM pg_indexes WHERE schemaname='public'
     AND indexname IN ('signals_features_join_idx','signals_features_series_idx')) AS indexes,
  (SELECT relrowsecurity FROM pg_class
     WHERE oid='public.signals_features'::regclass)                        AS rls_on,
  (SELECT count(*) = 1 FROM pg_policies
     WHERE tablename='signals_features')                                   AS read_policy,
  (SELECT count(*) = 4 FROM information_schema.columns
     WHERE column_name='engine_sha'
       AND table_name IN ('signals','signals_features','atlas_trades',
                          'atlas_entry_log'))                              AS sha_everywhere,
  (SELECT count(*) = 1 FROM information_schema.columns
     WHERE table_name='signals' AND column_name='engine_ran_at')           AS ran_at,
  (SELECT obj_description('public.signals_features'::regclass) IS NOT NULL) AS table_comment;
