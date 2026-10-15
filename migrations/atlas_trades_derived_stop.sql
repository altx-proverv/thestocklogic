-- atlas_trades: the published stop, the derived stop, and where it came from.
--
-- WHY. On 2026-10-15, 103 long candidates carried a valid zone and NOT ONE had
-- a published stop inside [MIN_STOP_PCT, MAX_STOP_PCT] = [0.5%, 5.0%] -- 86 too
-- wide, 17 too tight. HEXT's was 16.56% from entry, TATAELXSI's 17.72%. The
-- stop band, not publication_kind, was what locked every candidate out.
--
-- ATLAS now derives its own stop from the SMC levels the nightly chain already
-- computes when the published one does not fit. BOTH are recorded, because the
-- comparison is the whole point: if the derived stops get hit far more often
-- than the published ones would have, that is the finding, and it cannot be
-- recovered after the fact from a single stop_price column.
--
-- APPLY ONE STATEMENT AT A TIME. The Supabase editor has twice applied only a
-- prefix of a pasted file and reported success. §5 is the catalogue query; read
-- it rather than the editor's message.

-- ─────────────────────────────────────────────────────────────────────
-- 1. published_stop -- the stop as the batch published it, untouched.
--
--    SEPARATE FROM stop_price, which already exists and holds the stop
--    ACTUALLY USED. Writing the published value into stop_price would destroy
--    the only record of what the engine was handed.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS published_stop numeric;

COMMENT ON COLUMN public.atlas_trades.published_stop IS
  'The stop as the signal batch published it, before ATLAS decided whether it fits [MIN_STOP_PCT, MAX_STOP_PCT]. stop_price holds the stop actually used. Equal when the published stop fit.';

-- ─────────────────────────────────────────────────────────────────────
-- 2. stop_source -- WHERE the stop used came from.
--
--    Text, not an enum: the level vocabulary comes from
--    atlas/execution/derived_stop.py and has already changed once this month.
--    Values: published | demand_ob_low | bull_fvg_low | last_swing_low |
--            supply_ob_high | bear_fvg_high | last_swing_high |
--            clamp:min | clamp:max
--
--    clamp:* IS NOT A STRUCTURAL LEVEL and is named so it cannot be mistaken
--    for one -- it is the band edge, arithmetic with no structure behind it,
--    used when no level sits on the correct side of entry inside the band.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS stop_source text;

COMMENT ON COLUMN public.atlas_trades.stop_source IS
  'Where stop_price came from: published, or an SMC level (demand_ob_low, bull_fvg_low, last_swing_low and the short-side mirrors), or clamp:min / clamp:max. A clamp is the band edge, not structure.';

-- ─────────────────────────────────────────────────────────────────────
-- 3. publication_kind -- signal or candidate.
--
--    Candidates became enterable on 2026-10-16 on exactly the same gates as
--    signals. The two populations must stay separable in the record or the
--    accuracy figures stop describing what was traded -- which is the same
--    class of problem as the 22.2%-vs-18.8% hit rate.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS publication_kind text;

COMMENT ON COLUMN public.atlas_trades.publication_kind IS
  'signal | candidate, as published in public.signals. Candidates became enterable 2026-10-16; kept so the two populations can be reported separately.';

-- ─────────────────────────────────────────────────────────────────────
-- 4. stop_pct_used -- the stop distance actually taken, in percent.
--
--    Derivable from entry_price and stop_price, and stored anyway: every
--    analysis of this will group by it, and recomputing it from two columns
--    invites the same off-by-a-rounding drift that put three different
--    sizing numbers in three different files.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS stop_pct_used numeric;

COMMENT ON COLUMN public.atlas_trades.stop_pct_used IS
  'abs(entry_price - stop_price) / entry_price * 100, as computed at entry.';

-- ─────────────────────────────────────────────────────────────────────
-- 5. VERIFY. Run this LAST and read it. "Success" from the editor describes
--    the last statement it chose to run, not the file.
--
--    Expect four rows, all ok = true.
-- ─────────────────────────────────────────────────────────────────────

SELECT c.column_name,
       count(*)      AS found,
       1             AS expected,
       count(*) = 1  AS ok,
       string_agg(c.data_type, '') AS detail
  FROM information_schema.columns c
 WHERE c.table_schema = 'public'
   AND c.table_name = 'atlas_trades'
   AND c.column_name IN ('published_stop', 'stop_source',
                         'publication_kind', 'stop_pct_used')
 GROUP BY c.column_name
 ORDER BY c.column_name;
