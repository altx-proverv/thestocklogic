-- Surface resolved P&L alongside the mark-to-market move, and make the
-- headline P&L the resolved one.
--
-- "Since call" answers where price is now. "Resolved" answers whether the trade
-- as designed worked -- it stops moving the moment the stop or target is hit,
-- so it cannot be flattered by a later rally. Both are shown; only the second
-- feeds the P&L card.
DROP VIEW IF EXISTS public.v_window_summary;

CREATE OR REPLACE VIEW public.v_signal_window
WITH (security_invoker = true) AS
WITH win AS (
  SELECT DISTINCT mark_date FROM public.signal_marks
   ORDER BY mark_date DESC LIMIT 20
)
SELECT DISTINCT ON (m.signal_date, m.symbol, m.direction)
       m.signal_date,
       m.symbol,
       m.direction,
       m.setup_name,
       m.mark_date            AS last_mark_date,
       m.age_days,
       m.daily_move_pct,
       m.cum_move_pct,
       m.correct_today,
       -- A short is MIS: one session, so its single mark IS the whole trade.
       (m.direction = 'SHORT') AS closed_same_day,
       m.resolved_pnl_pct,
       m.resolved_on,
       m.resolution
  FROM public.signal_marks m
 WHERE m.signal_date >= (SELECT min(mark_date) FROM win)
 ORDER BY m.signal_date, m.symbol, m.direction, m.mark_date DESC;

GRANT SELECT ON public.v_signal_window TO anon, authenticated;

CREATE VIEW public.v_window_summary
WITH (security_invoker = true) AS
WITH win AS (
  SELECT DISTINCT mark_date FROM public.signal_marks
   ORDER BY mark_date DESC LIMIT 20
),
marks AS (
  SELECT m.correct_today
    FROM public.signal_marks m
   WHERE m.signal_date >= (SELECT min(mark_date) FROM win)
)
SELECT
  (SELECT count(*)       FROM win)                            AS window_days,
  (SELECT min(mark_date) FROM win)                            AS window_start,
  (SELECT max(mark_date) FROM win)                            AS window_end,
  count(*)                                                    AS n_signals,
  count(*) FILTER (WHERE direction = 'LONG')                  AS n_long,
  count(*) FILTER (WHERE direction = 'SHORT')                 AS n_short,
  count(*) FILTER (WHERE cum_move_pct > 0)                    AS winners,
  count(*) FILTER (WHERE cum_move_pct < 0)                    AS losers,
  count(*) FILTER (WHERE cum_move_pct = 0)                    AS flat,
  (SELECT symbol FROM public.v_signal_window ORDER BY cum_move_pct DESC LIMIT 1)              AS top_gainer_symbol,
  (SELECT round(cum_move_pct,2) FROM public.v_signal_window ORDER BY cum_move_pct DESC LIMIT 1) AS top_gainer_pct,
  (SELECT symbol FROM public.v_signal_window ORDER BY cum_move_pct ASC  LIMIT 1)              AS top_loser_symbol,
  (SELECT round(cum_move_pct,2) FROM public.v_signal_window ORDER BY cum_move_pct ASC  LIMIT 1) AS top_loser_pct,
  round(avg(cum_move_pct)::numeric, 3)                        AS mean_cum_move_pct,
  -- Resolution breakdown. This distribution is the finding; the total is a
  -- consequence of it.
  count(*) FILTER (WHERE resolution = 'STOP')                 AS n_stop,
  count(*) FILTER (WHERE resolution = 'TARGET')               AS n_target,
  count(*) FILTER (WHERE resolution = 'EXPIRED')              AS n_expired,
  count(*) FILTER (WHERE resolution = 'SAME_DAY')             AS n_same_day,
  count(*) FILTER (WHERE resolution IS NULL)                  AS n_unresolved,
  round(avg(resolved_pnl_pct) FILTER (WHERE resolution IS NOT NULL)::numeric, 3) AS mean_resolved_pct,
  -- Rs 1,00,000 per signal, over the RESOLVED ones only. An unresolved signal
  -- has no result yet; counting it as zero would be a claim, not a measurement.
  round(sum(resolved_pnl_pct) FILTER (WHERE resolution IS NOT NULL) / 100.0 * 100000) AS indicative_pnl,
  (SELECT count(*) FILTER (WHERE correct_today IS NOT NULL) FROM marks)  AS n_marks_scored,
  (SELECT round(100.0 * count(*) FILTER (WHERE correct_today)
                / NULLIF(count(*) FILTER (WHERE correct_today IS NOT NULL), 0), 1)
     FROM marks)                                              AS accuracy_pct
FROM public.v_signal_window;

GRANT SELECT ON public.v_window_summary TO anon, authenticated;

COMMENT ON VIEW public.v_window_summary IS
  'One row: aggregates of v_signal_window, the 20-trading-day intake population. indicative_pnl sums RESOLVED P&L only, at Rs1L per signal; unresolved signals are counted, not zeroed.';