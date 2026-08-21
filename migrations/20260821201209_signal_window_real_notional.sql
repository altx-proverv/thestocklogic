-- Per-position money on the base view.
--
-- Two modelling assumptions were doing real damage:
--
--   1. FLAT Rs1L PER POSITION. signals.notional has been stored all along and
--      averages Rs78k, ranging Rs34,983 to Rs99,993 -- size_by_risk takes
--      min(risk_qty, notional_qty) and floors to a multiple of 5, so almost
--      nothing lands on the cap. Scaling resolved_pnl_pct by Rs1L inflated both
--      the capital AND the rupee P&L.
--
--   2. SHORTS COSTED AS CASH. Shorts are MIS. position_sizing.py:102 --
--        capital_required = notional if d == "LONG" else notional * 0.20
--      -- so a short ties up a fifth of its notional. Charging it in full
--      overstated short capital 5x and understated the short return by the same
--      factor: a small loss on a small base is a large negative rate, which is
--      the whole point of splitting the two.
--
-- notional falls back to qty*entry_ref for the 56 of 177 resolved positions
-- predating the notional column. Verified identical where both exist (mean
-- absolute difference 0.00) and covering all 177 with no unjoinable rows, so
-- this is reconstruction, not estimation.
--
-- Columns are appended AFTER is_first_signal: CREATE OR REPLACE VIEW cannot
-- reorder or rename existing columns, and j.* would have pushed notional into
-- position 14 where is_first_signal lives.
--
-- The join is DISTINCT ON despite (symbol, direction, signal_date) being unique
-- in signals today: a future duplicate would otherwise multiply rows silently
-- and inflate every aggregate downstream.
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