-- Per-SIGNAL aggregates over the rolling mark window. The dashboard's existing
-- views cannot supply these: v_marks_daily is per mark_date, and v_open_book is
-- restricted to the single latest mark_date -- which, now that a SHORT has
-- exactly one mark at age 1, would exclude almost every short and make a
-- "notional P&L" silently long-only while still labelled otherwise.
--
-- One row per (signal_date, symbol, direction), carrying that signal's LATEST
-- mark inside the window. A long's cum_move_pct accrues daily; a short's is its
-- single intraday session and stays in the total for as long as that mark is in
-- the window. Both are "move since its own call", which is what the P&L sums.
--
-- security_invoker so it runs under the caller's policies rather than the
-- owner's. signal_marks already has an anon SELECT policy, so anon reads work
-- without this becoming another SECURITY DEFINER view.
CREATE OR REPLACE VIEW public.v_window_summary
WITH (security_invoker = true) AS
WITH win AS (
  SELECT DISTINCT mark_date FROM public.signal_marks
   ORDER BY mark_date DESC LIMIT 20
),
latest AS (
  SELECT DISTINCT ON (m.signal_date, m.symbol, m.direction)
         m.signal_date, m.symbol, m.direction, m.cum_move_pct
    FROM public.signal_marks m
   WHERE m.mark_date IN (SELECT mark_date FROM win)
     AND m.cum_move_pct IS NOT NULL
   ORDER BY m.signal_date, m.symbol, m.direction, m.mark_date DESC
)
SELECT
  (SELECT count(*)      FROM win)                             AS window_days,
  (SELECT min(mark_date) FROM win)                            AS window_start,
  (SELECT max(mark_date) FROM win)                            AS window_end,
  count(*)                                                    AS n_signals,
  count(*) FILTER (WHERE direction = 'LONG')                  AS n_long,
  count(*) FILTER (WHERE direction = 'SHORT')                 AS n_short,
  count(*) FILTER (WHERE cum_move_pct > 0)                    AS winners,
  count(*) FILTER (WHERE cum_move_pct < 0)                    AS losers,
  count(*) FILTER (WHERE cum_move_pct = 0)                    AS flat,
  (SELECT symbol FROM latest ORDER BY cum_move_pct DESC LIMIT 1)              AS top_gainer_symbol,
  (SELECT round(cum_move_pct, 2) FROM latest ORDER BY cum_move_pct DESC LIMIT 1) AS top_gainer_pct,
  (SELECT symbol FROM latest ORDER BY cum_move_pct ASC  LIMIT 1)              AS top_loser_symbol,
  (SELECT round(cum_move_pct, 2) FROM latest ORDER BY cum_move_pct ASC  LIMIT 1) AS top_loser_pct,
  -- Rs 1,00,000 per signal. Hypothetical: nobody held 400 at once.
  round(sum(cum_move_pct) / 100.0 * 100000)                   AS notional_pnl
FROM latest;

GRANT SELECT ON public.v_window_summary TO anon, authenticated;

COMMENT ON VIEW public.v_window_summary IS
  'One row: per-signal aggregates across the rolling 20 mark dates, each signal counted once at its latest in-window mark. Longs accrue daily, shorts carry their single intraday mark. notional_pnl is hypothetical at Rs1L per signal.';