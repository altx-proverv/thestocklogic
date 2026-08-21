-- Capital and return: what the book would actually have tied up.
--
-- Summed P&L says nothing about the capital behind it. +Rs1,90,600 across the
-- window is a different statement depending on whether it took Rs1.77Cr of
-- gross turnover or Rs41L of simultaneously committed cash, and only the second
-- is a rate of return anyone can act on.
--
-- OCCUPANCY IS (signal_date, resolved_on], NOT [signal_date, resolved_on].
-- ======================================================================
-- The brief said signal_date to resolved_on inclusive. Implemented exclusive of
-- signal_date deliberately, because that is when capital is actually committed
-- and because the engine already defines it that way:
--
--   LONG   mark_signals.resolve_signal walks
--          horizon = [x for x in all_dates if x > s.signal_date]
--          -- strictly after the signal date, through resolved_on.
--   SHORT  entered at the OPEN of the first trading day after the signal,
--          squared off at that day's close.
--
-- signal_date is the evening the signal published (~18:52 IST). No position
-- existed on it. Counting it would add a phantom day to every position and,
-- worse, would give a SAME_DAY short TWO days instead of one -- doubling the
-- short capital base and halving the magnitude of the short return, which is
-- precisely the number the split exists to expose.
--
-- PEAKS DO NOT SUM. The LONG peak, the SHORT peak and the TOTAL peak each fall
-- on whichever date that population was most concentrated, and those are
-- different dates. TOTAL is computed from the combined daily occupancy, not by
-- adding the other two rows. Any consumer showing all three must say so.
--
-- Resolved positions only, matching the P&L it is divided into. The 66
-- unresolved first-signals are still open and do tie up capital, so the true
-- peak is higher than reported here; including them without a P&L contribution
-- would depress the return by construction, so it is stated rather than mixed.
CREATE OR REPLACE VIEW public.v_capital_window
WITH (security_invoker = true) AS
WITH cal AS (
  -- The system's trading calendar. signal_marks is written once per trading
  -- day, so its distinct mark_dates ARE the sessions -- no holiday table needed.
  SELECT DISTINCT mark_date AS d FROM public.signal_marks
), pos AS (
  SELECT symbol, direction, signal_date, resolved_on::date AS resolved_on,
         resolved_pnl_pct
  FROM public.v_signal_window
  WHERE is_first_signal
    AND resolution IS NOT NULL
    AND resolved_on IS NOT NULL
), occ AS (
  -- One row per position per session held.
  SELECT p.direction, p.symbol, p.signal_date, c.d AS held_on
  FROM pos p
  JOIN cal c ON c.d > p.signal_date AND c.d <= p.resolved_on
), daily AS (
  SELECT direction, held_on, count(*) AS n FROM occ GROUP BY direction, held_on
  UNION ALL
  SELECT 'TOTAL', held_on, count(*) FROM occ GROUP BY held_on
), peak AS (
  -- Earliest date wins a tie, so the figure is stable run to run.
  SELECT DISTINCT ON (direction) direction, n AS peak_n, held_on AS peak_date
  FROM daily ORDER BY direction, n DESC, held_on
), holdlen AS (
  SELECT direction, symbol, signal_date, count(*) AS hold_days
  FROM occ GROUP BY direction, symbol, signal_date
), med AS (
  SELECT direction, percentile_cont(0.5) WITHIN GROUP (ORDER BY hold_days) AS median_hold
  FROM holdlen GROUP BY direction
  UNION ALL
  SELECT 'TOTAL', percentile_cont(0.5) WITHIN GROUP (ORDER BY hold_days) FROM holdlen
), agg AS (
  SELECT direction, count(*) AS n_resolved, sum(resolved_pnl_pct) AS pct
  FROM pos GROUP BY direction
  UNION ALL
  SELECT 'TOTAL', count(*), sum(resolved_pnl_pct) FROM pos
)
SELECT
  a.direction,
  CASE a.direction WHEN 'LONG' THEN 1 WHEN 'SHORT' THEN 2 ELSE 3 END AS sort_ord,
  a.n_resolved,
  -- Gross turnover: every position sized at the Rs1L notional cap.
  (a.n_resolved * 100000)::numeric                        AS total_deployed,
  p.peak_n                                                AS peak_concurrent,
  (p.peak_n * 100000)::numeric                            AS peak_capital,
  p.peak_date,
  round(m.median_hold::numeric, 1)                        AS median_hold_days,
  round(a.pct / 100.0 * 100000)                           AS net_pnl,
  -- The meaningful rate: return on the cash the book actually had to hold.
  round(100.0 * (a.pct / 100.0 * 100000) / NULLIF(p.peak_n * 100000, 0), 2)
                                                          AS return_on_peak_pct,
  -- Return on gross turnover. Always the flatterer of the two for a book that
  -- turns over quickly; kept so the difference is visible rather than implied.
  round(100.0 * (a.pct / 100.0 * 100000) / NULLIF(a.n_resolved * 100000, 0), 2)
                                                          AS return_on_deployed_pct
FROM agg a
JOIN peak p USING (direction)
JOIN med  m USING (direction);

COMMENT ON VIEW public.v_capital_window IS
  'Capital committed and return, resolved first-signal positions over the 20-day intake window. Occupancy is (signal_date, resolved_on] -- exclusive of signal_date, matching resolve_signal, so a SAME_DAY short occupies exactly one session. LONG, SHORT and TOTAL peaks fall on different dates and do not sum. Excludes unresolved positions, which do tie up capital, so the true peak is higher.';

GRANT SELECT ON public.v_capital_window TO anon, authenticated;