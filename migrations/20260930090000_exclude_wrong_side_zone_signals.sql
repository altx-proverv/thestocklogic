-- Exclude wrong-side-zone signals from the accuracy record, visibly.
--
-- 75 signals were published with an entry zone on the WRONG SIDE of price: a
-- LONG whose zone was a supply_ob or bear_fvg (entry ABOVE price), or a SHORT
-- whose zone was a demand_ob or bull_fvg (entry BELOW price). The cause was
-- zone_entry reading active_zone_high/low, which active_zones resolves from
-- structure_trend rather than from the trade direction. Fixed in ac43f53.
--
-- All 75 fall on 2026-08-11 and 2026-08-12, the last two sessions of the 8%
-- entry-distance rule; the 0.30% gate that replaced it on 13 Aug rejected almost
-- all later ones incidentally, by distance rather than by side.
--
-- WHY EXCLUDE RATHER THAN LEAVE. These are not signals that performed badly,
-- they are signals constructed against their own premise: 40 of the 41 resolved
-- wrong-side LONGS hit their stop, a 2.4% win rate against 61.5% for correctly
-- zoned longs on the SAME TWO SESSIONS. A long entered above price on a bear FVG
-- is buying into resistance with the stop below. Leaving them in makes the record
-- measure two different things at once.
--
-- WHY NOT DELETE. The rows are the only evidence of what was published on those
-- days, and the wrong-side/correct-side comparison is the only evidence either
-- way on how much the zone actually matters. v_wrong_side_excluded keeps both
-- queryable.
--
-- NOTHING PUBLISHED IS REWRITTEN. This adds a column and filters the views;
-- every signals row keeps the values it was published with.
--
-- THE 443 BLANKS ARE NOT EXCLUDED. zone_source predates most of the record, so
-- wrong-side is only DETERMINABLE for 385 of 828 signals. 19.5% of those are
-- wrong-side; if that rate held across the blanks there would be ~86 more that
-- cannot be identified. Excluding a population because it might be affected
-- would remove more signal than defect, so NULL means "cannot tell" and is
-- treated as valid. Every filter below is written `IS NOT FALSE`, never
-- `= true`, so a NULL is included by construction rather than by accident.

ALTER TABLE public.signals
  ADD COLUMN IF NOT EXISTS zone_side_valid boolean;

COMMENT ON COLUMN public.signals.zone_side_valid IS
  'FALSE when the entry zone was on the wrong side of price for the direction (a LONG on supply_ob/bear_fvg, a SHORT on demand_ob/bull_fvg). NULL when zone_source is absent and it cannot be determined. Accuracy views filter IS NOT FALSE, so NULL is included.';

UPDATE public.signals SET zone_side_valid =
  CASE
    WHEN zone_source IS NULL OR zone_source = '' THEN NULL
    WHEN upper(direction) = 'LONG'  AND zone_source IN ('supply_ob','bear_fvg') THEN false
    WHEN upper(direction) = 'SHORT' AND zone_source IN ('demand_ob','bull_fvg') THEN false
    ELSE true
  END;

-- signal_marks with wrong-side signals removed. A drop-in replacement wherever
-- accuracy is computed from marks directly -- v_window_summary.accuracy_pct did
-- exactly that, bypassing v_signal_window, so filtering the base view alone would
-- have corrected the P&L figures and left the headline accuracy untouched.
CREATE OR REPLACE VIEW public.v_marks_zone_valid
WITH (security_invoker = true) AS
SELECT m.*
FROM public.signal_marks m
WHERE NOT EXISTS (
  SELECT 1 FROM public.signals s
  WHERE s.signal_date = m.signal_date
    AND s.symbol      = m.symbol
    AND upper(s.direction) = upper(m.direction)
    AND s.zone_side_valid IS FALSE
);

COMMENT ON VIEW public.v_marks_zone_valid IS
  'signal_marks less any mark whose signal had a wrong-side entry zone. NOT EXISTS rather than a join so a duplicate signals row cannot multiply marks.';

-- The excluded population WITH its outcomes, kept deliberately: this is the
-- comparison, and it is the only evidence either way on whether the zone matters.
-- Read it alongside the same two sessions' correctly zoned signals.
CREATE OR REPLACE VIEW public.v_wrong_side_excluded
WITH (security_invoker = true) AS
SELECT DISTINCT ON (s.signal_date, s.symbol, s.direction)
  s.signal_date, s.symbol, s.direction, s.zone_source, s.entry_ref, s.sl,
  s.setup_name, s.score,
  m.resolution, m.resolved_pnl_pct, m.resolved_on, m.cum_move_pct, m.age_days
FROM public.signals s
LEFT JOIN public.signal_marks m
  ON  m.signal_date = s.signal_date
  AND m.symbol      = s.symbol
  AND upper(m.direction) = upper(s.direction)
WHERE s.zone_side_valid IS FALSE
ORDER BY s.signal_date, s.symbol, s.direction, m.mark_date DESC;

COMMENT ON VIEW public.v_wrong_side_excluded IS
  'The 75 signals excluded from the accuracy record for a wrong-side entry zone, with their resolved outcomes. Kept as evidence, not deleted: 40 of 41 resolved wrong-side LONGS hit their stop (2.4% win) against 61.5% for correctly zoned longs on the same two sessions, while wrong-side SHORTS resolved indistinguishably from correct ones (-0.27% vs -0.40%) because every short resolves SAME_DAY before either level is reached.';

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
  -- The exclusion, COUNTED, so the figures above can never quietly
  -- drop 9% of the record. The dashboard reads this and states it.
  (SELECT count(*) FROM public.v_wrong_side_excluded)
                                                             AS n_excluded_wrong_side
FROM v_signal_window;

COMMENT ON VIEW public.v_window_summary IS
  'Aggregate of v_signal_window. n_signals/n_long/n_short count PUBLICATIONS; everything position-level counts FIRST SIGNALS only. indicative_pnl sums pnl_inr at each position real notional. indicative_pnl_all_pub is the all-publications sum, a signal-quality figure and not P&L. accuracy_pct is per-mark across all publications by design.';
