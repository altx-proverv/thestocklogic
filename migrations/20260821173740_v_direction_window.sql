-- Gross profit and gross loss by direction, resolved signals only, same
-- 20-day intake window and same basis as v_setup_window.
--
-- A net figure hides the ratio. Shorts at -Rs31,836 read as a small bleed;
-- +Rs30,274 against -Rs62,110 says they lose two rupees for every one made,
-- which is the more useful statement and a different conclusion.
--
-- The TOTAL row is a ROLLUP of the same aggregate, not a separately computed
-- sum, so it cannot drift from the rows above it -- and since both it and
-- v_window_summary.indicative_pnl sum resolved_pnl_pct over the resolved rows
-- of v_signal_window, it reconciles with the Indicative P&L card by
-- construction.
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
  COALESCE(round(sum(resolved_pnl_pct) FILTER (WHERE resolved_pnl_pct > 0) / 100.0 * 100000), 0) AS gross_profit,
  COALESCE(round(sum(resolved_pnl_pct) FILTER (WHERE resolved_pnl_pct < 0) / 100.0 * 100000), 0) AS gross_loss,
  round(sum(resolved_pnl_pct) / 100.0 * 100000)                            AS net_pnl
FROM public.v_signal_window
WHERE resolution IS NOT NULL
GROUP BY ROLLUP(direction);

GRANT SELECT ON public.v_direction_window TO anon, authenticated;

COMMENT ON VIEW public.v_direction_window IS
  'Resolved P&L split into gross profit and gross loss by direction, over the 20-trading-day intake window. TOTAL is a ROLLUP of the same aggregate and reconciles with v_window_summary.indicative_pnl by construction.';