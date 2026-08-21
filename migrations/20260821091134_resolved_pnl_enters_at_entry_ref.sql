-- Resolved P&L now enters at entry_ref, for BOTH directions. The daily marks
-- still enter at the signal-date close. This is deliberate: two metrics, two
-- questions, two entry conventions.
--
--   marks        "what did the market do since we called it" -> signal-date
--                close, because that is where the call was made
--   resolved P&L "did the trade as designed work"            -> entry_ref,
--                because sl and target_1 are defined against entry_ref and
--                nothing else
--
-- Entering resolution at the close silently destroyed the measurement. Every
-- signal is built at exactly 2R, but the close has already drifted from
-- entry_ref by publication: TARGET-resolvers had closed +4.0% above it (target
-- 1.3% away, stop 6.3% away -> 0.2R) and STOP-resolvers -1.4% below it (stop
-- 1.4% away, target 7.2% away -> 5R). The counts then measured drift, not
-- design. Under entry_ref every STOP is exactly -1R and every TARGET +2R.
--
-- Do not unify the two conventions. See resolve_signal() in
-- engine/mark_signals.py.
COMMENT ON COLUMN public.signal_marks.resolved_pnl_pct IS
  'Directional % from entry_ref to the resolving exit, BOTH directions. Not the signal-date close: sl and target_1 are defined against entry_ref, so any other entry changes the trade''s R before it is measured. The daily marks deliberately use the close instead. Assumes the entry_ref limit fills.';

COMMENT ON COLUMN public.signal_marks.resolution IS
  'STOP (-1R) | TARGET (+2R) | EXPIRED (day-20 close) | SAME_DAY (short, exits at day-one close, no R convention), or NULL if unresolved. STOP wins a same-bar stop-and-target: daily bars carry no intraday sequence.';