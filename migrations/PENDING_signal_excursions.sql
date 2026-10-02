-- Audit 6: record the path, not just the verdict.
--
-- WHY THIS EXISTS. Every resolved signal in signal_outcomes exits at exactly
-- -1.00R or +2.00R -- 228 of 228 losses at `sl`, 65 of 65 wins at `target_1`,
-- which is exactly 2R. That is not what the market did, it is how resolution was
-- built: update_outcomes walks five days, takes whichever level is touched first,
-- and writes that level as the exit price. There is no slippage, no partial fill
-- and no broker anywhere in the 293.
--
-- The consequence is that NO EXIT RULE IS TESTABLE from that table. A 3R target,
-- a trailing stop, a time stop, a wider stop -- each needs to know where price
-- actually went after entry, and the record only says which of two pre-set lines
-- it crossed first. The one exit question that was answerable took a bespoke
-- replay off the parquets, which does not scale and is not reproducible by anyone
-- who does not have them.
--
-- DAILY ROWS, NOT A SUMMARY. A summary answers one exit rule. Two numbers -- max
-- favourable and max adverse over the window -- cannot tell you whether a trade
-- went to +3R on day 2 and came back, or crawled there by day 19, and those are
-- different trades under every trailing rule. The path answers any rule; a
-- summary answers the one it was computed for.
--
-- NO SWING DATA HERE, DELIBERATELY. The trailing analysis that produced the
-- +0.371R figure had to shift swing confirmations by five bars, because
-- detect_swing_points reads LB bars either side and the forward-filled
-- last_swing_low is therefore not causal mid-series. This table carries only
-- high/low/close, which are known at the close of the day they describe, so
-- nothing computed from it can inherit that look-ahead.

CREATE TABLE IF NOT EXISTS public.signal_excursions (
  signal_date       date        NOT NULL,
  symbol            text        NOT NULL,
  direction         text        NOT NULL,
  day_offset        integer     NOT NULL,
  bar_date          date        NOT NULL,
  -- the bar itself, so any rule can be recomputed without the parquet
  high              numeric,
  low               numeric,
  close             numeric,
  -- entry and risk, repeated on every row so a single row is self-contained
  entry_price       numeric,
  stop_price        numeric,
  risk_per_share    numeric,
  -- the path, cumulative from entry to and including this bar
  mfe_r             numeric,
  mae_r             numeric,
  mfe_pct           numeric,
  mae_pct           numeric,
  close_r           numeric,
  -- what happened on THIS bar
  stop_touched      boolean     NOT NULL DEFAULT false,
  target_2r_touched boolean     NOT NULL DEFAULT false,
  target_3r_touched boolean     NOT NULL DEFAULT false,
  both_same_bar     boolean     NOT NULL DEFAULT false,
  engine_sha        text,
  computed_at       timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.signal_excursions
  ADD CONSTRAINT signal_excursions_key
  UNIQUE (signal_date, symbol, direction, day_offset);

-- The query every exit study makes: one signal's whole path, in order.
CREATE INDEX IF NOT EXISTS signal_excursions_path_idx
  ON public.signal_excursions (signal_date, symbol, direction, day_offset);
-- And the cross-sectional one: "what did everything do by day 5".
CREATE INDEX IF NOT EXISTS signal_excursions_offset_idx
  ON public.signal_excursions (day_offset, signal_date DESC);

ALTER TABLE public.signal_excursions ENABLE ROW LEVEL SECURITY;

-- Same reach as signals and signals_features: these are the inputs behind a
-- public accuracy record. Read-only; writes are service_role, which bypasses RLS.
CREATE POLICY "anon read signal_excursions"
  ON public.signal_excursions
  FOR SELECT TO anon, authenticated
  USING (true);

COMMENT ON TABLE public.signal_excursions IS
  'The daily price path after entry, 20 trading days per signal, written by engine/excursions.py. Exists because every row in signal_outcomes exits at exactly -1R or +2R by construction, which makes no exit rule testable from it: a 3R target, a trailing stop or a time stop each need to know where price actually went, and the record only says which of two pre-set lines it crossed first. Carries high/low/close only -- no swing data -- so nothing computed from it can inherit the five-bar look-ahead that swing detection has mid-series.';

COMMENT ON COLUMN public.signal_excursions.day_offset IS
  '1 = the first session after signal_date, which is the entry session. Counts trading days present in the bar series, so a holiday does not consume an offset and offset 5 is always the fifth session traded.';

COMMENT ON COLUMN public.signal_excursions.mfe_r IS
  'Maximum favourable excursion from the fill, in R, cumulative to and including this bar. Signed so that favourable is positive for both sides: (high - entry) / risk for a long, (entry - low) / risk for a short. Cumulative rather than per-bar because the question an exit rule asks is "how far has this been in profit by now", not "how far did it move today".';

COMMENT ON COLUMN public.signal_excursions.mae_r IS
  'Maximum adverse excursion from the fill, in R, cumulative, reported as a POSITIVE magnitude. A value above 1.0 means price went further against the trade than the stop, i.e. the stop would have filled with slippage -- which is how the only three real stop-outs ATLAS has taken came in at -1.45R against a modelled -1.00R.';

COMMENT ON COLUMN public.signal_excursions.both_same_bar IS
  'The stop and the 2R target were both touched within this daily bar, so their order is unknowable from daily OHLC. update_outcomes has an AMBIGUOUS branch for this and the resolver that actually wrote signal_outcomes does not -- it checks the stop first, assigning every tie to LOSS. Recorded here so the magnitude of that assumption is measurable rather than argued.';

COMMENT ON COLUMN public.signal_excursions.risk_per_share IS
  'abs(entry_price - stop_price) at the fill. Stored so every R figure on the row can be re-derived from the raw bar, and so a row remains interpretable if the sizing rule changes.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true. One statement at a time -- see migrations/README.md.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM pg_tables
     WHERE schemaname='public' AND tablename='signal_excursions')          AS table_exists,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='signal_excursions_key')                                AS upsert_key,
  (SELECT count(*) = 2 FROM pg_indexes WHERE schemaname='public'
     AND indexname IN ('signal_excursions_path_idx',
                       'signal_excursions_offset_idx'))                    AS indexes,
  (SELECT relrowsecurity FROM pg_class
     WHERE oid='public.signal_excursions'::regclass)                       AS rls_on,
  (SELECT count(*) = 1 FROM pg_policies
     WHERE schemaname='public' AND tablename='signal_excursions')          AS read_policy,
  (SELECT count(*) = 4 FROM information_schema.columns
     WHERE table_name='signal_excursions'
       AND column_name IN ('mfe_r','mae_r','both_same_bar','engine_sha'))  AS key_columns,
  (SELECT obj_description('public.signal_excursions'::regclass) IS NOT NULL) AS table_comment;
