-- REPAIR: statements two migrations reported as applied and did not apply.
--
-- WHAT HAPPENED. Both files were pasted whole into the Supabase SQL editor,
-- which answered "Success. No rows returned", and both applied only a PREFIX of
-- their statements. Verified against the live catalogue on 2026-10-01:
--
--   20260930093000_exclude_wrong_side_zone_signals.sql   3 of 12 statements
--     applied:     zone_side_valid column, its comment, the backfill
--                  (75 rows false, 310 true, 446 NULL -- the NULLs are correct,
--                  they are rows with no zone_source)
--     NOT applied: v_marks_zone_valid, v_wrong_side_excluded, and the REPLACE of
--                  v_signal_window and v_window_summary
--
--   PENDING_publication_kind.sql                         1 of 6 statements
--     applied:     publication_kind column, NOT NULL DEFAULT 'signal'
--     NOT applied: the CHECK constraint, the column comment, v_watchlist
--
-- WHY THE FIRST ONE MATTERS MORE THAN IT LOOKS. v_signal_window and
-- v_window_summary EXIST, so nothing errored and nothing looked wrong -- they
-- just hold their PRE-30-September definitions, with no reference to
-- zone_side_valid. The wrong-side exclusion has therefore never been in force.
-- The column was added, the dashboard was changed to say 75 signals are
-- excluded, and the views that do the excluding were never replaced. A view that
-- is absent announces itself; a view that is silently stale does not.
--
-- The measured effect on the headline is small -- accuracy 44.28% as published
-- against 44.45% filtered, because the 849 wrong-side marks score 42.64%, near
-- the overall rate -- but the page has been claiming an exclusion that was not
-- happening, which is a different fault from the number being wrong.
--
-- HOW TO APPLY THIS. Run it ONE STATEMENT AT A TIME. Do not paste the file.
-- The verification block at the end returns one row of booleans; every column
-- must be true before this is considered applied. "Success" from the editor is
-- not evidence -- that is the whole reason this file exists.

-- ─────────────────────────────────────────────────────────────────────
-- 1. The tail of 20260930093000, verbatim except where noted.
-- ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW public.v_marks_zone_valid
WITH (security_invoker = true) AS
SELECT m.*
FROM public.signal_marks m
WHERE NOT EXISTS (
  SELECT 1 FROM public.signals s
  WHERE s.signal_date = m.signal_date
    AND s.symbol      = m.symbol
    AND upper(s.direction) = upper(m.direction)
    AND s.zone_side_valid IS FALSE
);

COMMENT ON VIEW public.v_marks_zone_valid IS
  'signal_marks less any mark whose signal had a wrong-side entry zone. NOT EXISTS rather than a join so a duplicate signals row cannot multiply marks.';

-- The excluded population WITH its outcomes, kept deliberately: this is the
-- comparison, and it is the only evidence either way on whether the zone matters.
-- Read it alongside the same two sessions' correctly zoned signals.
CREATE OR REPLACE VIEW public.v_wrong_side_excluded
WITH (security_invoker = true) AS
SELECT DISTINCT ON (s.signal_date, s.symbol, s.direction)
  s.signal_date, s.symbol, s.direction, s.zone_source, s.entry_ref, s.sl,
  s.setup_name, s.score,
  m.resolution, m.resolved_pnl_pct, m.resolved_on, m.cum_move_pct, m.age_days
FROM public.signals s
LEFT JOIN public.signal_marks m
  ON  m.signal_date = s.signal_date
  AND m.symbol      = s.symbol
  AND upper(m.direction) = upper(s.direction)
WHERE s.zone_side_valid IS FALSE
ORDER BY s.signal_date, s.symbol, s.direction, m.mark_date DESC;

COMMENT ON VIEW public.v_wrong_side_excluded IS
  'The 75 signals excluded from the accuracy record for a wrong-side entry zone, with their resolved outcomes. Kept as evidence, not deleted: 40 of 41 resolved wrong-side LONGS hit their stop (2.4% win) against 61.5% for correctly zoned longs on the same two sessions, while wrong-side SHORTS resolved indistinguishably from correct ones (-0.27% vs -0.40%) because every short resolves SAME_DAY before either level is reached.';

CREATE OR REPLACE VIEW public.v_signal_window
WITH (security_invoker = true) AS
WITH win AS (
  SELECT DISTINCT mark_date FROM signal_marks ORDER BY mark_date DESC LIMIT 20
), pub AS (
  SELECT DISTINCT ON (signal_date, symbol, direction)
    signal_date, symbol, direction, setup_name,
    mark_date AS last_mark_date, age_days, daily_move_pct, cum_move_pct,
    correct_today, direction = 'SHORT'::text AS closed_same_day,
    resolved_pnl_pct, resolved_on, resolution
  FROM public.v_marks_zone_valid m
  WHERE signal_date >= (SELECT min(win.mark_date) FROM win)
  ORDER BY signal_date, symbol, direction, mark_date DESC
), sz AS (
  SELECT DISTINCT ON (symbol, direction, signal_date)
         symbol, direction, signal_date,
         COALESCE(notional, qty * entry_ref) AS notional
  FROM public.signals
  ORDER BY symbol, direction, signal_date, id
), j AS (
  SELECT pub.*, sz.notional
  FROM pub LEFT JOIN sz USING (symbol, direction, signal_date)
)
SELECT
  j.signal_date, j.symbol, j.direction, j.setup_name, j.last_mark_date,
  j.age_days, j.daily_move_pct, j.cum_move_pct, j.correct_today,
  j.closed_same_day, j.resolved_pnl_pct, j.resolved_on, j.resolution,
  (row_number() OVER (PARTITION BY j.symbol, j.direction ORDER BY j.signal_date) = 1)
    AS is_first_signal,
  j.notional,
  -- Longs are CNC: full value blocked. Shorts are MIS: ~20% margin.
  CASE WHEN j.direction = 'SHORT' THEN j.notional * 0.20 ELSE j.notional END
    AS capital_required,
  round((j.resolved_pnl_pct * j.notional / 100.0)::numeric, 2) AS pnl_inr
FROM j;

COMMENT ON VIEW public.v_signal_window IS
  'One row per published signal over the 20-trading-day intake window. is_first_signal marks the earliest signal_date per symbol+direction -- the one ATLAS would take under Gate 3b. notional is the position own size (falling back to qty*entry_ref pre-column); capital_required charges shorts 20% MIS margin per position_sizing.py; pnl_inr is P&L at that real size, not a flat Rs1L.';

GRANT SELECT ON public.v_signal_window TO anon, authenticated;

CREATE OR REPLACE VIEW public.v_window_summary
WITH (security_invoker = true) AS
WITH win AS (
  SELECT DISTINCT mark_date FROM signal_marks ORDER BY mark_date DESC LIMIT 20
), marks AS (
  SELECT m.correct_today FROM public.v_marks_zone_valid m
  WHERE m.signal_date >= (SELECT min(win.mark_date) FROM win)
)
SELECT
  (SELECT count(*) FROM win)            AS window_days,
  (SELECT min(win.mark_date) FROM win)  AS window_start,
  (SELECT max(win.mark_date) FROM win)  AS window_end,
  count(*)                                                   AS n_signals,
  count(*) FILTER (WHERE direction = 'LONG')                 AS n_long,
  count(*) FILTER (WHERE direction = 'SHORT')                AS n_short,
  count(*) FILTER (WHERE is_first_signal AND cum_move_pct > 0) AS winners,
  count(*) FILTER (WHERE is_first_signal AND cum_move_pct < 0) AS losers,
  count(*) FILTER (WHERE is_first_signal AND cum_move_pct = 0) AS flat,
  (SELECT symbol FROM v_signal_window WHERE is_first_signal
     ORDER BY cum_move_pct DESC LIMIT 1)                     AS top_gainer_symbol,
  (SELECT round(cum_move_pct, 2) FROM v_signal_window WHERE is_first_signal
     ORDER BY cum_move_pct DESC LIMIT 1)                     AS top_gainer_pct,
  (SELECT symbol FROM v_signal_window WHERE is_first_signal
     ORDER BY cum_move_pct LIMIT 1)                          AS top_loser_symbol,
  (SELECT round(cum_move_pct, 2) FROM v_signal_window WHERE is_first_signal
     ORDER BY cum_move_pct LIMIT 1)                          AS top_loser_pct,
  round(avg(cum_move_pct) FILTER (WHERE is_first_signal), 3) AS mean_cum_move_pct,
  count(*) FILTER (WHERE is_first_signal AND resolution = 'STOP')     AS n_stop,
  count(*) FILTER (WHERE is_first_signal AND resolution = 'TARGET')   AS n_target,
  count(*) FILTER (WHERE is_first_signal AND resolution = 'EXPIRED')  AS n_expired,
  count(*) FILTER (WHERE is_first_signal AND resolution = 'SAME_DAY') AS n_same_day,
  count(*) FILTER (WHERE is_first_signal AND resolution IS NULL)      AS n_unresolved,
  round(avg(resolved_pnl_pct) FILTER (WHERE is_first_signal AND resolution IS NOT NULL), 3)
                                                             AS mean_resolved_pct,
  round(sum(pnl_inr) FILTER (WHERE is_first_signal AND resolution IS NOT NULL))
                                                             AS indicative_pnl,
  (SELECT count(*) FILTER (WHERE marks.correct_today IS NOT NULL) FROM marks)
                                                             AS n_marks_scored,
  (SELECT round(100.0 * count(*) FILTER (WHERE marks.correct_today)::numeric
         / NULLIF(count(*) FILTER (WHERE marks.correct_today IS NOT NULL), 0)::numeric, 1)
     FROM marks)                                             AS accuracy_pct,
  count(*) FILTER (WHERE is_first_signal)                            AS n_first_signals,
  count(*) FILTER (WHERE is_first_signal AND direction = 'LONG')     AS n_first_long,
  count(*) FILTER (WHERE is_first_signal AND direction = 'SHORT')    AS n_first_short,
  round(sum(pnl_inr) FILTER (WHERE resolution IS NOT NULL))  AS indicative_pnl_all_pub,
  -- The exclusion, COUNTED, so the figures above can never quietly
  -- drop 9% of the record. The dashboard reads this and states it.
  (SELECT count(*) FROM public.v_wrong_side_excluded)
                                                             AS n_excluded_wrong_side
FROM v_signal_window;

COMMENT ON VIEW public.v_window_summary IS
  'Aggregate of v_signal_window. n_signals/n_long/n_short count PUBLICATIONS; everything position-level counts FIRST SIGNALS only. indicative_pnl sums pnl_inr at each position real notional. indicative_pnl_all_pub is the all-publications sum, a signal-quality figure and not P&L. accuracy_pct is per-mark across all publications by design.';

-- ─────────────────────────────────────────────────────────────────────
-- 2. The tail of PENDING_publication_kind.
--
-- The CHECK is written NOT VALID then VALIDATEd separately: 831 existing rows
-- are all 'signal' so it would pass either way, but a constraint that cannot be
-- added because of one bad row should fail at the VALIDATE, where the message
-- names the problem, rather than at the ADD.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.signals
  ADD CONSTRAINT signals_publication_kind_chk
  CHECK (publication_kind IN ('signal', 'candidate')) NOT VALID;

ALTER TABLE public.signals VALIDATE CONSTRAINT signals_publication_kind_chk;

COMMENT ON COLUMN public.signals.publication_kind IS
  'signal = the setup qualified, so it is actionable and is scored by mark_signals. candidate = a valid unmitigated zone that did NOT qualify; published so the record shows what the engine saw, never scored, never entered -- market_open filters entries to publication_kind = ''signal''. Every accuracy view and mark_signals filter on ''signal'' so the record keeps measuring the population it always measured. Reworded 2026-10-01: the original text defined these by distance from the zone, and the entry-distance gate (MAX_ENTRY_DIST_PCT) was removed that day.';

-- The watchlist, for the page and for anyone asking what the loop is watching.
CREATE OR REPLACE VIEW public.v_watchlist
WITH (security_invoker = true) AS
SELECT signal_date, symbol, direction, setup_name, zone_source,
       entry_ref, entry_low, entry_high, sl, stop_pct, entry_dist_pct, score
FROM public.signals
WHERE publication_kind = 'candidate'
ORDER BY signal_date DESC, entry_dist_pct ASC NULLS LAST;

COMMENT ON VIEW public.v_watchlist IS
  'Symbols carrying a valid unmitigated zone that did not qualify as signals: the zone exists, price has not passed through it, and the geometry is expressible, but the setup failed the disqualifier block or the stop cap. Ordered nearest-first by distance to the zone. NOT recommendations, not scored, and NOT enterable -- the loop takes entries only from publication_kind = ''signal''.';

-- ─────────────────────────────────────────────────────────────────────
-- 3. VERIFY. Every column must come back true.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM pg_views
     WHERE schemaname='public' AND viewname='v_marks_zone_valid')      AS v_marks_zone_valid,
  (SELECT count(*) = 1 FROM pg_views
     WHERE schemaname='public' AND viewname='v_wrong_side_excluded')   AS v_wrong_side_excluded,
  (SELECT count(*) = 1 FROM pg_views
     WHERE schemaname='public' AND viewname='v_watchlist')             AS v_watchlist,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='signals_publication_kind_chk')                     AS chk_publication_kind,
  (SELECT col_description('public.signals'::regclass::oid, ordinal_position) IS NOT NULL
     FROM information_schema.columns
     WHERE table_name='signals' AND column_name='publication_kind')    AS publication_kind_comment,
  (SELECT position('zone_side_valid' IN definition) > 0 FROM pg_views
     WHERE schemaname='public' AND viewname='v_signal_window')         AS signal_window_filters,
  (SELECT position('v_marks_zone_valid' IN definition) > 0 FROM pg_views
     WHERE schemaname='public' AND viewname='v_window_summary')        AS window_summary_filters;
