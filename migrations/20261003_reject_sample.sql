-- The negative class: a stratified daily sample of stock-days that did NOT qualify.
--
-- THE QUESTION. Twelve gates reject about 460 symbol-days a night to pass roughly
-- 3.6. Nobody knows whether those 3.6 behave any differently from 3.6 drawn at
-- random from the 460, because the rejected rows have never been persisted. The
-- 2026-09-30 feature study compared winners against losers and found nothing; the
-- 2026-10-02 exit study found no exit rule that rescues them. Neither could ask
-- whether qualifying means anything at all -- which is the engine's premise.
--
-- WHY THIS IS THE BEST-POWERED QUESTION ON THIS RECORD. The winner/loser question
-- has a positive class of ~19% of 293 resolved signals growing at ~71/month. The
-- membership question has a negative class 128x larger than its positive one that
-- costs nothing to enlarge. At k=10 controls per case, three months of sampling
-- detects AUC 0.56; six months, 0.54.
--
-- WHY A SAMPLE, AND WHY k=10. Not storage -- the "370k rejected stock-days" is the
-- whole 788-day history that 03b rescores on every run; one night's NEW rejects
-- are 232 symbols x 2 directions = 464 rows, about 70 MB/year stored whole. It is
-- that a 128:1 control ratio buys almost nothing. The standard error of a
-- two-group difference goes as sqrt(1/n1 + 1/n2) = sqrt(1/n1)*sqrt(1 + 1/k):
--
--     k=1   1.414x the width of infinite controls
--     k=5   1.095x
--     k=10  1.049x     <- 4.9% wider, for 1/13th the rows
--     k=20  1.025x
--
-- Past k=10 the curve is flat. ~36 rows a night, ~9,100/year, ~5.5 MB/year.
--
-- WHY THE STRATA LOOK LIKE THAT. Stratifying on disqualify_reason alone is a trap.
-- Every gate in 03b is masked on (~disqualified), so the reason is the FIRST gate
-- a row failed, and failure count is confounded with gate POSITION: the last
-- gate's rejects have necessarily passed the other eight and are ALL near misses,
-- while the first gate's fail a median of three. Sampling on reason alone buys a
-- near-miss rate that is an artefact of the funnel's ordering. So the stratum is
-- (first failed gate) x (near miss) x direction, and gates_failed plus its bitmask
-- ride on every row so the distinction is recoverable rather than inferred.

CREATE TABLE IF NOT EXISTS public.reject_sample (
  sample_date        date        NOT NULL,
  symbol             text        NOT NULL,
  direction          text        NOT NULL,

  -- WHY it was rejected, and how nearly it was not
  disqualify_reason  text,
  -- The number of gates this row's features fail IN TOTAL, not short-circuited.
  -- 1 = a near miss: acceptable everywhere except one place. These are the only
  -- informative negatives, and disqualify_reason cannot identify them.
  gates_failed       smallint    NOT NULL DEFAULT 0,
  -- Bit i = GATE_ORDER[i] in engine/03b_score.py. Bit 9 is the zone-entry gate,
  -- which runs at a later stage and cannot be evaluated with the other nine.
  -- REORDERING GATE_ORDER SILENTLY REINTERPRETS EVERY HISTORICAL ROW HERE.
  gates_failed_mask  integer     NOT NULL DEFAULT 0,

  -- the sampling frame, so a consumer can reweight to the population
  stratum            text,
  stratum_population integer,
  sample_weight      numeric,

  -- features, named EXACTLY as signals_features names them. A column that exists
  -- on only one side of the comparison is worse than useless: it looks comparable
  -- and is not.
  rvol               numeric,
  atr_pct            numeric,
  adx                numeric,
  adx_ranging        boolean,
  rsi                numeric,
  delivery_pct       numeric,
  market_regime      text,
  sector             text,
  sector_bias        text,
  weekly_bullish     boolean,
  weekly_bearish     boolean,
  near_demand_ob     boolean,
  near_supply_ob     boolean,
  price_in_bull_fvg  boolean,
  price_in_bear_fvg  boolean,
  bos_bull           boolean,
  bos_bear           boolean,
  choch_bull         boolean,
  choch_bear         boolean,
  bull_liq_sweep     boolean,
  bear_liq_sweep     boolean,
  recent_bos_choch   boolean,
  active_zone_high   numeric,
  active_zone_low    numeric,
  active_zone_source text,
  zone_age_days      numeric,
  zone_dist_pct      numeric,
  structure_trend    text,
  accumulation_score numeric,
  is_accumulation    boolean,
  rs_5d              numeric,
  rs_20d             numeric,
  advance_count      integer,
  decline_count      integer,
  nifty_close        numeric,
  open               numeric,
  high               numeric,
  low                numeric,
  close              numeric,
  volume             numeric,

  engine_sha         text,
  created_at         timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.reject_sample
  ADD CONSTRAINT reject_sample_key
  UNIQUE (sample_date, symbol, direction);

-- A row that qualified is not a reject. The sampler draws only from
-- disqualified == True, and this is the assertion that it stayed that way.
ALTER TABLE public.reject_sample
  ADD CONSTRAINT reject_sample_is_a_reject
  CHECK (gates_failed >= 1) NOT VALID;

ALTER TABLE public.reject_sample VALIDATE CONSTRAINT reject_sample_is_a_reject;

ALTER TABLE public.reject_sample
  ADD CONSTRAINT reject_sample_direction_chk
  CHECK (upper(direction) IN ('LONG', 'SHORT')) NOT VALID;

ALTER TABLE public.reject_sample VALIDATE CONSTRAINT reject_sample_direction_chk;

-- The two queries the membership test makes: one date's whole sample, and the
-- near-miss subset across all dates.
CREATE INDEX IF NOT EXISTS reject_sample_date_idx
  ON public.reject_sample (sample_date DESC, direction);
CREATE INDEX IF NOT EXISTS reject_sample_near_idx
  ON public.reject_sample (gates_failed, sample_date DESC);
CREATE INDEX IF NOT EXISTS reject_sample_reason_idx
  ON public.reject_sample (disqualify_reason, sample_date DESC);

ALTER TABLE public.reject_sample ENABLE ROW LEVEL SECURITY;

-- AUTHENTICATED ONLY, not anon. Unlike signals and signal_excursions, this is not
-- the inputs behind a published claim -- it is the engine's rejection logic laid
-- out row by row, which is the part a subscriber is paying for. MERIDIAN set the
-- precedent for authenticated-only on this project and the same reasoning applies.
CREATE POLICY "authenticated read reject_sample"
  ON public.reject_sample
  FOR SELECT TO authenticated
  USING (true);

COMMENT ON TABLE public.reject_sample IS
  'A stratified daily sample of stock-days that did NOT qualify, at 10 controls per qualifying case, written by engine/reject_sample.py. Exists to test the engine''s premise: whether the ~3.6 symbol-days a night that pass twelve gates behave any differently from 3.6 drawn at random from the ~460 that do not. Every analysis before this one compared winners against losers and so could not ask that question. Sampled rather than stored whole because a 128:1 control ratio gives a confidence interval only 0.4% narrower than 10:1 -- not because the rows are expensive. Read alongside signal_excursions where source=''reject'' for the forward paths.';

-- ONE SEMANTIC TRAP, WRITTEN DOWN BEFORE ANYONE ANALYSES THIS. gates_failed means
-- "this row's features failed a gate", NOT "this row was rejected". The
-- accumulation screen runs after 03b's gate block and can un-disqualify a row,
-- clearing disqualified and disqualify_reason while gates_failed keeps its count --
-- so a QUALIFYING row can carry gates_failed >= 1. Cases must therefore be defined
-- by `qualifies`, never by gates_failed = 0; doing the latter puts every
-- screen-recovered row in the control group and measures the screen instead of the
-- filters. Nothing in this table is a recovered row (the sampler draws only from
-- disqualified == True), but the comparison's other side is full of them.

COMMENT ON COLUMN public.reject_sample.gates_failed IS
  'How many of 03b''s gates this row''s features fail IN TOTAL, computed without short-circuiting. 1 means a near miss -- acceptable everywhere except one place -- and those are the only informative negatives. NOT recoverable from disqualify_reason, which is the FIRST gate failed and is confounded with gate position: the last gate''s rejects are all near misses by construction, the first gate''s fail a median of three.';

COMMENT ON COLUMN public.reject_sample.gates_failed_mask IS
  'Bitmask of WHICH gates failed. Bit i corresponds to GATE_ORDER[i] in engine/03b_score.py; bit 9 is the zone-entry gate, evaluated at a later stage. Reordering GATE_ORDER silently reinterprets every row already written here -- the order is asserted inside gate_conditions() for exactly that reason.';

COMMENT ON COLUMN public.reject_sample.sample_weight IS
  'stratum_population / rows drawn from that stratum. Allocation is proportional, but every stratum present gets at least one row, so rare gates are over-represented relative to the population by design. Multiply by this to recover population-level marginals.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true. One statement at a time -- see migrations/README.md.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM pg_tables
     WHERE schemaname='public' AND tablename='reject_sample')            AS table_exists,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='reject_sample_key')                                  AS upsert_key,
  (SELECT convalidated FROM pg_constraint
     WHERE conname='reject_sample_is_a_reject')                           AS reject_check,
  (SELECT convalidated FROM pg_constraint
     WHERE conname='reject_sample_direction_chk')                         AS direction_check,
  (SELECT count(*) = 3 FROM pg_indexes WHERE schemaname='public'
     AND indexname IN ('reject_sample_date_idx','reject_sample_near_idx',
                       'reject_sample_reason_idx'))                       AS indexes,
  (SELECT relrowsecurity FROM pg_class
     WHERE oid='public.reject_sample'::regclass)                          AS rls_on,
  -- authenticated only: anon must have NO policy here
  (SELECT count(*) = 1 FROM pg_policies
     WHERE schemaname='public' AND tablename='reject_sample')             AS one_policy,
  (SELECT roles::text NOT LIKE '%anon%' FROM pg_policies
     WHERE schemaname='public' AND tablename='reject_sample'
     LIMIT 1)                                                            AS anon_excluded,
  (SELECT count(*) = 4 FROM information_schema.columns
     WHERE table_name='reject_sample'
       AND column_name IN ('gates_failed','gates_failed_mask','stratum',
                           'sample_weight'))                             AS audit_columns,
  (SELECT obj_description('public.reject_sample'::regclass) IS NOT NULL)  AS table_comment;
