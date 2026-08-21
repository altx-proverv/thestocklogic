-- Move the P&L basis from "every publication" to "one position per symbol".
--
-- A demand zone that survives more than one session republishes, so the same
-- symbol reappears in consecutive batches. v_signal_window counted each
-- publication as an independent position: 424 rows over 243 distinct
-- symbol+direction pairs, 42.7% republished. Summing resolved_pnl_pct over all
-- of them counted the same move once per republication, over overlapping
-- holding periods, and the dashboard then multiplied it by Rs1,00,000 and
-- called it Indicative P&L -- a rupee figure implying a book nobody held.
--
-- ATLAS settled the question. Gate 3b (atlas_entry.enter_trade) hard-skips any
-- symbol already held, so the agent takes the FIRST signal of a run and skips
-- every republication. Measuring on all publications was measuring positions
-- the agent is built not to take.
--
-- is_first_signal marks the earliest signal_date per (symbol, direction) IN
-- THE WINDOW. Boundary caveat: a signal whose earlier publication has already
-- aged out is flagged first here, because the window cannot see past its own
-- edge. It resolves itself as the window rolls forward.
--
-- The all-publications sum is kept as indicative_pnl_all_pub and belongs under
-- a signal-quality heading, never a P&L one: "of everything we published, how
-- did it do" is a real question, but it is not what the book would have made.

-- ---------------------------------------------------------------------------
-- 1. Base view gains the flag. CREATE OR REPLACE, so the existing columns keep
--    their names, order and types and is_first_signal is appended -- required
--    because v_direction_window and v_setup_window depend on this view.
-- ---------------------------------------------------------------------------
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
  FROM signal_marks m
  WHERE signal_date >= (SELECT min(win.mark_date) FROM win)
  ORDER BY signal_date, symbol, direction, mark_date DESC
)
SELECT pub.*,
       (row_number() OVER (PARTITION BY symbol, direction ORDER BY signal_date) = 1)
         AS is_first_signal
FROM pub;

COMMENT ON VIEW public.v_signal_window IS
  'One row per published signal over the 20-trading-day intake window. is_first_signal marks the earliest signal_date per symbol+direction -- the one ATLAS would take under Gate 3b. P&L aggregates filter on it; publication counts do not.';

-- ---------------------------------------------------------------------------
-- 2. Summary. Position-level metrics move to the first-signal basis;
--    publication counts stay as they are, because "how many signals did we put
--    out" is a genuinely different question from "how many positions was that".
-- ---------------------------------------------------------------------------
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
  -- PUBLICATION counts. Unchanged basis: these drive signals-per-day and the
  -- long/short split, which describe output volume, not positions held.
  count(*)                                                   AS n_signals,
  count(*) FILTER (WHERE direction = 'LONG')                 AS n_long,
  count(*) FILTER (WHERE direction = 'SHORT')                AS n_short,
  -- POSITION-level from here down: first signal only.
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
  round(sum(resolved_pnl_pct) FILTER (WHERE is_first_signal AND resolution IS NOT NULL)
        / 100.0 * 100000::numeric)                           AS indicative_pnl,
  (SELECT count(*) FILTER (WHERE marks.correct_today IS NOT NULL) FROM marks)
                                                             AS n_marks_scored,
  -- Per MARK, deliberately across all publications: every day a long is held
  -- is its own directional call. Republication reweights the sample but does
  -- not bias the percentage -- the same stock on the same day resolves the
  -- same way whichever publication it is counted under.
  (SELECT round(100.0 * count(*) FILTER (WHERE marks.correct_today)::numeric
         / NULLIF(count(*) FILTER (WHERE marks.correct_today IS NOT NULL), 0)::numeric, 1)
     FROM marks)                                             AS accuracy_pct,
  -- Appended columns.
  count(*) FILTER (WHERE is_first_signal)                            AS n_first_signals,
  count(*) FILTER (WHERE is_first_signal AND direction = 'LONG')     AS n_first_long,
  count(*) FILTER (WHERE is_first_signal AND direction = 'SHORT')    AS n_first_short,
  -- SIGNAL QUALITY, not P&L. Every publication summed, including repeats.
  round(sum(resolved_pnl_pct) FILTER (WHERE resolution IS NOT NULL)
        / 100.0 * 100000::numeric)                           AS indicative_pnl_all_pub
FROM v_signal_window;

COMMENT ON VIEW public.v_window_summary IS
  'Aggregate of v_signal_window. n_signals/n_long/n_short count PUBLICATIONS; everything position-level (winners, losers, extremes, resolution counts, indicative_pnl) counts FIRST SIGNALS only, matching what ATLAS would hold under Gate 3b. indicative_pnl_all_pub is the all-publications sum and is a signal-quality figure, not P&L. accuracy_pct is per-mark across all publications by design.';

-- ---------------------------------------------------------------------------
-- 3. By direction. Same basis, so the TOTAL row still reconciles with the
--    Indicative P&L card by construction -- both now sum resolved_pnl_pct over
--    the resolved FIRST-signal rows of v_signal_window.
-- ---------------------------------------------------------------------------
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
  AND is_first_signal
GROUP BY ROLLUP(direction);

COMMENT ON VIEW public.v_direction_window IS
  'Resolved P&L split into gross profit and gross loss by direction, first signal per symbol+direction only. TOTAL is a ROLLUP of the same aggregate and reconciles with v_window_summary.indicative_pnl by construction.';

-- ---------------------------------------------------------------------------
-- 4. By setup. Same basis. n_signals here means positions, not publications --
--    a setup that republishes a lot was previously credited for the repeats.
-- ---------------------------------------------------------------------------
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
  round(sum(resolved_pnl_pct) FILTER (WHERE resolution IS NOT NULL) / 100.0 * 100000) AS resolved_pnl
FROM public.v_signal_window
WHERE is_first_signal
GROUP BY COALESCE(setup_name, '—');

COMMENT ON VIEW public.v_setup_window IS
  'Per-setup aggregates over the 20-trading-day intake window, resolved basis, first signal per symbol+direction only. n_target + n_stop need not equal n_resolved: SAME_DAY shorts are resolved but are neither. Unresolved signals are counted separately and contribute nothing to resolved_pnl.';

GRANT SELECT ON public.v_signal_window, public.v_window_summary,
                public.v_direction_window, public.v_setup_window
  TO anon, authenticated;
