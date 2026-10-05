-- The indicative P&L measured trades that could not be taken.
--
-- WHAT THE DASHBOARD SHOWED. +Rs2,58,518 over a 20-day window, labelled
-- "Indicative P&L" with sign colouring, beside a 41.8% accuracy figure. A
-- subscriber reads a quarter-lakh profit.
--
-- WHAT IT ACTUALLY MEASURED. Of 202 resolved first signals, only 27 had an entry
-- that was ever achievable. The other 175 were MISSED_GAP_UP or MISSED_GAP_DOWN --
-- price gapped past the entry band at the next open and update_outcomes classified
-- them MISSED. mark_signals performs NO entry check at all: it assumes a fill at
-- entry_ref and scores from there.
--
--     entry never achievable   175 positions   +Rs296,915   mean +1.885%
--     entry FEASIBLE            27 positions   - Rs38,397   mean -2.079%
--     total shown              202 positions   +Rs258,518
--
-- Unenterable trades were 114.9% of the headline. Remove them and the sign flips.
--
-- THE BIAS IS STRUCTURAL, NOT NOISE, and that is why it is this large. A gap UP
-- through the entry band is exactly what makes a long both impossible to enter AND
-- profitable when marked from entry_ref. The measurement selected for favourable
-- gaps: among unenterable rows the split is 76 TARGET to 9 STOP (89% target), among
-- feasible rows it is 3 TARGET to 21 STOP (12.5%). The second is consistent with the
-- 26.2% resolved hit rate; the first is an artefact of scoring a fill that never
-- happened.
--
-- SAME SHAPE AS THE WRONG-SIDE EXCLUSION already in this view: the main view
-- excludes, a companion view keeps the excluded population visible with its
-- outcomes, and the summary COUNTS the exclusion so the figures can never quietly
-- drop 87% of the window.
--
-- NAMED STATES, NOT "NOT FEASIBLE". The filter excludes the four statuses that mean
-- the entry could not be taken, rather than keeping only FEASIBLE. Inverting would
-- also exclude NO_DATA -- a signal published last night whose bars have not arrived
-- -- turning a pending row into a rejected one. A new status value would slip
-- through this filter, so the verify asserts the full set of six is unchanged.

-- ─────────────────────────────────────────────────────────────────────
-- 1. The excluded population, kept as evidence.
-- ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW public.v_entry_missed_excluded
WITH (security_invoker = true) AS
SELECT DISTINCT ON (o.signal_date, o.symbol, o.direction)
  o.signal_date, o.symbol, o.direction, o.entry_status, o.outcome,
  o.entry_ref, o.sl, o.actual_entry,
  m.resolution, m.resolved_pnl_pct, m.resolved_on, m.cum_move_pct
FROM public.signal_outcomes o
LEFT JOIN public.signal_marks m
  ON  m.signal_date = o.signal_date
  AND m.symbol      = o.symbol
  AND upper(m.direction) = upper(o.direction)
WHERE o.entry_status IN ('MISSED_GAP_UP', 'MISSED_GAP_DOWN',
                         'GAPPED_BELOW_SL', 'GAPPED_ABOVE_SL')
ORDER BY o.signal_date, o.symbol, o.direction, m.mark_date DESC;

COMMENT ON VIEW public.v_entry_missed_excluded IS
  'Signals whose entry was never achievable, with the marks that scored them anyway. Kept as evidence rather than deleted: these 175 positions in the 20-day window to 2026-10-05 contributed +Rs296,915 of a +Rs258,518 headline, and the mechanism is self-selecting -- a gap through the entry band is what makes a trade both unenterable and profitable when marked from entry_ref.';

-- ─────────────────────────────────────────────────────────────────────
-- 2. v_signal_window, excluding them.
-- ─────────────────────────────────────────────────────────────────────

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
    -- THE ENTRY MUST HAVE BEEN ACHIEVABLE. NOT EXISTS rather than a join, for the
    -- same reason the zone exclusion uses it: a duplicate signal_outcomes row
    -- would otherwise multiply the marks.
    AND NOT EXISTS (
      SELECT 1 FROM public.signal_outcomes o
      WHERE o.signal_date = m.signal_date
        AND o.symbol      = m.symbol
        AND upper(o.direction) = upper(m.direction)
        AND o.entry_status IN ('MISSED_GAP_UP', 'MISSED_GAP_DOWN',
                               'GAPPED_BELOW_SL', 'GAPPED_ABOVE_SL'))
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
  'One row per published signal over the 20-trading-day intake window, EXCLUDING any whose entry was never achievable (MISSED_GAP_UP/DOWN, GAPPED_BELOW/ABOVE_SL) and any with a wrong-side zone. Before the entry exclusion this view carried 331 rows and a +Rs258,518 indicative P&L of which 114.9% came from trades that could not be taken; after, 33 rows and -Rs38,397. is_first_signal marks the earliest signal_date per symbol+direction. notional is the position own size; capital_required charges shorts 20% MIS margin; pnl_inr is P&L at that real size. The SAME_DAY short convention is covered by the same filter -- 147 of 150 of those were gap-downs that were never short-able.';

GRANT SELECT ON public.v_signal_window TO anon, authenticated;
GRANT SELECT ON public.v_entry_missed_excluded TO anon, authenticated;

-- ─────────────────────────────────────────────────────────────────────
-- 3. v_window_summary — count the exclusion and EXPOSE n.
--
--    The operator's instruction: if n is too small to mean anything the page
--    should state n, not suppress the figure. So n_resolved_positions is surfaced
--    as a first-class column rather than left to be inferred from the counts.
-- ─────────────────────────────────────────────────────────────────────

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
  -- THE SAMPLE SIZE BEHIND indicative_pnl, surfaced so the page can state it.
  -- 27 enterable positions over 20 days is a defensible figure to publish; a
  -- quarter-lakh with no n beside it was not.
  count(*) FILTER (WHERE is_first_signal AND resolution IS NOT NULL)
                                                             AS n_resolved_positions,
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
  -- Both exclusions, COUNTED, so the figures above can never quietly drop part of
  -- the record. The dashboard reads these and states them.
  (SELECT count(*) FROM public.v_wrong_side_excluded)
                                                             AS n_excluded_wrong_side,
  (SELECT count(*) FROM public.v_entry_missed_excluded)
                                                             AS n_excluded_unenterable
FROM v_signal_window;

COMMENT ON VIEW public.v_window_summary IS
  'Aggregate of v_signal_window, which now excludes signals whose entry was never achievable. n_resolved_positions is the sample size behind indicative_pnl and exists because the honest figure is small: 27 enterable positions over the 20 days to 2026-10-05, mean -2.079%, -Rs38,397. The previous +Rs258,518 was 114.9% composed of trades that could not be taken. n_excluded_unenterable counts what was removed.';

GRANT SELECT ON public.v_window_summary TO anon, authenticated;

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true. One statement at a time.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM pg_views WHERE schemaname='public'
     AND viewname='v_entry_missed_excluded')                          AS companion_view,
  -- the filter is in the definition, not merely in a comment
  (SELECT position('MISSED_GAP_UP' IN definition) > 0 FROM pg_views
     WHERE schemaname='public' AND viewname='v_signal_window')        AS window_filters,
  -- the window collapsed, which is the point
  (SELECT count(*) < 60 FROM public.v_signal_window)                  AS window_shrank,
  (SELECT n_resolved_positions BETWEEN 20 AND 40
     FROM public.v_window_summary)                                    AS n_surfaced,
  -- the sign flipped
  (SELECT indicative_pnl < 0 FROM public.v_window_summary)            AS pnl_now_negative,
  (SELECT n_excluded_unenterable > 500
     FROM public.v_window_summary)                                    AS exclusion_counted,
  -- the SAME_DAY shorts went with them
  (SELECT n_same_day < 10 FROM public.v_window_summary)               AS same_day_filtered,
  -- a new entry_status would slip through a named filter, so pin the set
  (SELECT count(DISTINCT entry_status) = 6 FROM public.signal_outcomes) AS six_statuses,
  (SELECT count(*) = 0 FROM public.signal_outcomes
     WHERE entry_status NOT IN ('FEASIBLE','MISSED_GAP_UP','MISSED_GAP_DOWN',
                                'GAPPED_BELOW_SL','GAPPED_ABOVE_SL','NO_DATA'))
                                                                      AS no_new_status;
