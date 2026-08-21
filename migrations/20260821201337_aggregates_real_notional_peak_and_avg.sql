-- Rebuild every aggregate on v_signal_window.pnl_inr / capital_required, so
-- the whole chain speaks real rupees at real position size. See the
-- signal_window_real_notional migration for why.
--
-- PEAK AND AVERAGE ARE BOTH REPORTED, and neither is sufficient alone. Peak is
-- the capital you must have to never be blocked; average is what is actually
-- working across the window. Return on peak is the conservative read, return on
-- average the optimistic one. A lumpy book -- and this one is very lumpy, with
-- most sessions idle and one holding the entire concentration -- has a large
-- gap between them, and quoting either by itself misstates the result.

CREATE OR REPLACE VIEW public.v_window_summary
WITH (security_invoker = true) AS
WITH win AS (
  SELECT DISTINCT mark_date FROM signal_marks ORDER BY mark_date DESC LIMIT 20
), marks AS (
  SELECT m.correct_today FROM signal_marks m
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
  round(sum(pnl_inr) FILTER (WHERE resolution IS NOT NULL))  AS indicative_pnl_all_pub
FROM v_signal_window;

COMMENT ON VIEW public.v_window_summary IS
  'Aggregate of v_signal_window. n_signals/n_long/n_short count PUBLICATIONS; everything position-level counts FIRST SIGNALS only. indicative_pnl sums pnl_inr at each position real notional. indicative_pnl_all_pub is the all-publications sum, a signal-quality figure and not P&L. accuracy_pct is per-mark across all publications by design.';

CREATE OR REPLACE VIEW public.v_direction_window
WITH (security_invoker = true) AS
SELECT
  CASE WHEN GROUPING(direction) = 1 THEN 'TOTAL' ELSE direction END        AS direction,
  CASE WHEN GROUPING(direction) = 1 THEN 3
       WHEN direction = 'LONG' THEN 1 ELSE 2 END                           AS sort_ord,
  count(*)                                                                 AS n_signals,
  COALESCE(round(sum(resolved_pnl_pct) FILTER (WHERE resolved_pnl_pct > 0)::numeric, 3), 0) AS gross_profit_pct,
  COALESCE(round(sum(resolved_pnl_pct) FILTER (WHERE resolved_pnl_pct < 0)::numeric, 3), 0) AS gross_loss_pct,
  round(sum(resolved_pnl_pct)::numeric, 3)                                 AS net_pct,
  COALESCE(round(sum(pnl_inr) FILTER (WHERE pnl_inr > 0)), 0)              AS gross_profit,
  COALESCE(round(sum(pnl_inr) FILTER (WHERE pnl_inr < 0)), 0)              AS gross_loss,
  round(sum(pnl_inr))                                                      AS net_pnl
FROM public.v_signal_window
WHERE resolution IS NOT NULL AND is_first_signal
GROUP BY ROLLUP(direction);

COMMENT ON VIEW public.v_direction_window IS
  'Resolved P&L by direction at each position real notional, first signal per symbol+direction only. TOTAL is a ROLLUP of the same aggregate and reconciles with v_window_summary.indicative_pnl by construction.';

CREATE OR REPLACE VIEW public.v_setup_window
WITH (security_invoker = true) AS
SELECT
  COALESCE(setup_name, '—')                                    AS setup_name,
  count(*)                                                     AS n_signals,
  count(*) FILTER (WHERE resolution IS NOT NULL)               AS n_resolved,
  count(*) FILTER (WHERE resolution IS NULL)                   AS n_unresolved,
  count(*) FILTER (WHERE resolution = 'TARGET')                AS n_target,
  count(*) FILTER (WHERE resolution = 'STOP')                  AS n_stop,
  count(*) FILTER (WHERE resolution = 'SAME_DAY')              AS n_same_day,
  count(*) FILTER (WHERE resolution = 'EXPIRED')               AS n_expired,
  round(100.0 * count(*) FILTER (WHERE resolution = 'TARGET')
        / NULLIF(count(*) FILTER (WHERE resolution IS NOT NULL), 0), 1) AS hit_rate_pct,
  round(sum(resolved_pnl_pct) FILTER (WHERE resolution IS NOT NULL)::numeric, 3) AS resolved_pct,
  round(sum(pnl_inr) FILTER (WHERE resolution IS NOT NULL))    AS resolved_pnl
FROM public.v_signal_window
WHERE is_first_signal
GROUP BY COALESCE(setup_name, '—');

COMMENT ON VIEW public.v_setup_window IS
  'Per-setup aggregates over the 20-trading-day intake window, resolved basis, first signal per symbol+direction only, at real notional. n_target + n_stop need not equal n_resolved: SAME_DAY shorts are resolved but are neither.';

DROP VIEW IF EXISTS public.v_capital_window;
CREATE VIEW public.v_capital_window
WITH (security_invoker = true) AS
WITH win AS (
  SELECT DISTINCT mark_date AS d FROM public.signal_marks ORDER BY mark_date DESC LIMIT 20
), cal AS (
  SELECT DISTINCT mark_date AS d FROM public.signal_marks
), pos AS (
  SELECT symbol, direction, signal_date, resolved_on::date AS resolved_on,
         notional, capital_required, pnl_inr
  FROM public.v_signal_window
  WHERE is_first_signal AND resolution IS NOT NULL AND resolved_on IS NOT NULL
), occ AS (
  SELECT p.direction, p.symbol, p.signal_date, p.capital_required, c.d AS held_on
  FROM pos p
  JOIN cal c ON c.d > p.signal_date AND c.d <= p.resolved_on
), dirs AS (
  SELECT 'LONG' AS direction UNION ALL SELECT 'SHORT' UNION ALL SELECT 'TOTAL'
), grid AS (
  -- Every direction x every window session, so sessions holding nothing count
  -- as zero in the average rather than being dropped from the denominator.
  SELECT dirs.direction, win.d FROM dirs CROSS JOIN win
), daily AS (
  SELECT g.direction, g.d, COALESCE(sum(o.capital_required), 0) AS cap
  FROM grid g
  LEFT JOIN occ o ON o.held_on = g.d
                 AND (g.direction = 'TOTAL' OR o.direction = g.direction)
  GROUP BY g.direction, g.d
), peak AS (
  SELECT DISTINCT ON (direction) direction, cap AS peak_cap, d AS peak_date
  FROM daily ORDER BY direction, cap DESC, d
), avgcap AS (
  SELECT direction, avg(cap) AS avg_cap,
         count(*) FILTER (WHERE cap > 0) AS sessions_active,
         count(*) AS sessions_total
  FROM daily GROUP BY direction
), holdlen AS (
  SELECT direction, symbol, signal_date, count(*) AS hold_days
  FROM occ GROUP BY direction, symbol, signal_date
), med AS (
  SELECT direction, percentile_cont(0.5) WITHIN GROUP (ORDER BY hold_days) AS median_hold
  FROM holdlen GROUP BY direction
  UNION ALL
  SELECT 'TOTAL', percentile_cont(0.5) WITHIN GROUP (ORDER BY hold_days) FROM holdlen
), agg AS (
  SELECT direction, count(*) AS n_resolved,
         sum(capital_required) AS dep, sum(pnl_inr) AS pnl
  FROM pos GROUP BY direction
  UNION ALL
  SELECT 'TOTAL', count(*), sum(capital_required), sum(pnl_inr) FROM pos
)
SELECT
  a.direction,
  CASE a.direction WHEN 'LONG' THEN 1 WHEN 'SHORT' THEN 2 ELSE 3 END AS sort_ord,
  a.n_resolved,
  round(a.dep)                                            AS total_deployed,
  round(p.peak_cap)                                       AS peak_capital,
  p.peak_date,
  round(v.avg_cap)                                        AS avg_capital,
  v.sessions_active,
  v.sessions_total,
  round(m.median_hold::numeric, 1)                        AS median_hold_days,
  round(a.pnl)                                            AS net_pnl,
  -- Conservative: the capital you must hold to never be blocked.
  round(100.0 * a.pnl / NULLIF(p.peak_cap, 0), 2)         AS return_on_peak_pct,
  -- Optimistic: the capital actually working, averaged across the window.
  round(100.0 * a.pnl / NULLIF(v.avg_cap, 0), 2)          AS return_on_avg_pct,
  round(100.0 * a.pnl / NULLIF(a.dep, 0), 2)              AS return_on_deployed_pct
FROM agg a
JOIN peak   p USING (direction)
JOIN avgcap v USING (direction)
JOIN med    m USING (direction);

COMMENT ON VIEW public.v_capital_window IS
  'Capital and return, resolved first-signal positions, at real notional with shorts charged 20% MIS margin. Occupancy is (signal_date, resolved_on], matching resolve_signal. PEAK is the capital needed never to be blocked (conservative return); AVERAGE is what is working across all 20 window sessions including idle ones (optimistic return). Neither is honest alone. LONG, SHORT and TOTAL peaks fall on different dates and do not sum.';

GRANT SELECT ON public.v_window_summary, public.v_direction_window,
                public.v_setup_window, public.v_capital_window
  TO anon, authenticated;