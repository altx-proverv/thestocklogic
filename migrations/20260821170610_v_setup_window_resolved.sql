-- Per-setup performance across the 20-trading-day INTAKE window, on the
-- resolved basis.
--
-- Replaces v_marks_by_setup_today, which was filtered to marks dated today.
-- Two things were wrong with that. It showed a single day's moves split into
-- small groups, which is noise. And it caught almost no shorts at all -- a
-- short has exactly one mark, on day one, so it only appeared if it happened to
-- be called the previous session. The header above it described a 20-day
-- population while the table described one day.
--
-- Resolved basis, not daily direction: daily direction across the whole book
-- tracks the index closely enough that it cannot separate one setup from
-- another. Resolution can -- 2R targets against 1R stops, with a real hit rate.
--
-- SAME_DAY shorts are counted in n_resolved and in the P&L. They are resolved
-- outcomes, reached by a different rule, and dropping them would quietly make
-- every setup long-only. They are deliberately NOT in n_target or n_stop, so
-- those two need not sum to n_resolved.
--
-- Unresolved signals are carried as their own count and contribute NOTHING to
-- the P&L. A signal with no outcome yet is not a zero.
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
  -- target / resolved, so SAME_DAY sits in the denominator: it is an outcome
  -- that was not a target, which is exactly what the rate should reflect.
  round(100.0 * count(*) FILTER (WHERE resolution = 'TARGET')
        / NULLIF(count(*) FILTER (WHERE resolution IS NOT NULL), 0), 1) AS hit_rate_pct,
  round(sum(resolved_pnl_pct) FILTER (WHERE resolution IS NOT NULL)::numeric, 3) AS resolved_pct,
  round(sum(resolved_pnl_pct) FILTER (WHERE resolution IS NOT NULL) / 100.0 * 100000) AS resolved_pnl
FROM public.v_signal_window
GROUP BY COALESCE(setup_name, '—');

GRANT SELECT ON public.v_setup_window TO anon, authenticated;

COMMENT ON VIEW public.v_setup_window IS
  'Per-setup aggregates over the 20-trading-day intake window, resolved basis. n_target + n_stop need not equal n_resolved: SAME_DAY shorts are resolved but are neither. Unresolved signals are counted separately and contribute nothing to resolved_pnl.';