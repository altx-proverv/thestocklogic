-- WINDOW = 20 trading days of INTAKE, not of marking.
--
-- v_window_summary selected on mark_date, which is not the same thing: a signal
-- stays live for 20 trading days, so marks landing inside the last 20 mark
-- dates belong to signals called up to ~38 trading days ago. 74 of the 499
-- signals it counted were called BEFORE the window opened -- oldest 2026-06-30
-- against a window starting 2026-07-24 -- and each carried its full cumulative
-- move since its own call. That is where the +21.5% top gainer came from.
--
-- Now: a signal counts only while its signal_date is inside the window, and
-- ages out completely on day 21. Longs accrue daily marks up to 20 days;
-- shorts contribute their single day-one mark and stay in the population.
--
-- v_signal_window defines the population ONCE. v_window_summary aggregates it
-- rather than re-deriving it, so the cards and the table cannot drift apart.
--
-- Dropped rather than replaced: CREATE OR REPLACE cannot rename a view column,
-- and notional_pnl becomes indicative_pnl here.
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
       (m.direction = 'SHORT') AS closed_same_day
  FROM public.signal_marks m
 WHERE m.signal_date >= (SELECT min(mark_date) FROM win)
 ORDER BY m.signal_date, m.symbol, m.direction, m.mark_date DESC;

GRANT SELECT ON public.v_signal_window TO anon, authenticated;

COMMENT ON VIEW public.v_signal_window IS
  'One row per signal called within the last 20 trading days, carrying its latest mark. Longs accrue daily; shorts have exactly one, flagged closed_same_day. Ages out entirely at day 21.';

CREATE VIEW public.v_window_summary
WITH (security_invoker = true) AS
WITH win AS (
  SELECT DISTINCT mark_date FROM public.signal_marks
   ORDER BY mark_date DESC LIMIT 20
),
-- Every mark belonging to a signal in the intake window. Accuracy is per mark
-- (each day a long is held is its own directional call); the rest are per
-- signal and come from v_signal_window.
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
  -- Rs 1,00,000 per signal. Indicative: nobody held 400 at once.
  round(sum(cum_move_pct) / 100.0 * 100000)                   AS indicative_pnl,
  (SELECT count(*) FILTER (WHERE correct_today IS NOT NULL) FROM marks)  AS n_marks_scored,
  (SELECT round(100.0 * count(*) FILTER (WHERE correct_today)
                / NULLIF(count(*) FILTER (WHERE correct_today IS NOT NULL), 0), 1)
     FROM marks)                                              AS accuracy_pct
FROM public.v_signal_window;

GRANT SELECT ON public.v_window_summary TO anon, authenticated;

COMMENT ON VIEW public.v_window_summary IS
  'One row: aggregates of v_signal_window, the 20-trading-day intake population. accuracy_pct is per mark across those signals; everything else is per signal. indicative_pnl is hypothetical at Rs1L per signal.';