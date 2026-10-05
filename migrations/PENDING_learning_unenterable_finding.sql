-- A CLOSED FINDING: the indicative P&L measured trades that could not be taken.
--
-- APPLY AFTER PENDING_learning_ledger.sql.
--
-- Registered as evidence of how a measurement failed, not merely as a bug that was
-- fixed. The mechanism is the interesting part and it generalises: scoring a signal
-- from a price that was never paid selects for the moves that made the price
-- unreachable.

INSERT INTO public.learning_hypotheses
  (slug, question, population, statistic, null_value, side, family,
   basis_version, seed_from, status, registered_by, rationale,
   closed_at, closed_reason)
VALUES
  ('closed.unenterable-pnl.2026-10-06',
   'Did the published indicative P&L measure trades that could actually be taken?',
   '202 resolved first signals in the 20 trading days to 2026-10-05',
   'sum of resolved_pnl_pct x notional, split by signal_outcomes.entry_status',
   NULL, 'two-sided', 'closed-defect', 'marks-entry_ref-v1', '2026-08-25', 'closed',
   'reconciliation-2026-10-06',
   'Raised because the dashboard showed +Rs2,58,518 over the window while the '
   'resolved record stood at -0.21R per trade and 26.2% against a 33.3% breakeven. '
   'A quarter-lakh profit is not possible from a negative-expectancy set, so one of '
   'the two was measuring something else.',
   '2026-10-06 00:00:00+00',
   'NO. ONLY 27 OF 202 RESOLVED POSITIONS HAD AN ENTRY THAT WAS EVER ACHIEVABLE. '
   'The other 175 were MISSED_GAP_UP or MISSED_GAP_DOWN: price gapped past the entry '
   'band at the next open and update_outcomes classified them MISSED. mark_signals '
   'performs no entry check at all -- it assumes a fill at entry_ref -- so those 175 '
   'contributed +Rs296,915 while the 27 enterable positions contributed -Rs38,397. '
   'Unenterable trades were 114.9% of the headline and removing them flips the sign. '
   'THE MECHANISM IS SELF-SELECTING, which is why the effect is this large rather '
   'than noise: a gap UP through a long''s entry band is exactly what makes the trade '
   'both impossible to enter and profitable when marked from entry_ref. Among '
   'unenterable rows the split is 76 TARGET to 9 STOP (89% target); among feasible '
   'rows it is 3 TARGET to 21 STOP (12.5%), which is consistent with the 26.2% '
   'resolved hit rate. The SAME_DAY short convention carried the same defect in a '
   'different form -- 147 of 150 were gap-downs that were never short-able. FIXED by '
   'PENDING_exclude_unenterable.sql: v_signal_window excludes the four unenterable '
   'statuses, v_entry_missed_excluded keeps the population as evidence, and '
   'v_window_summary surfaces n_resolved_positions so the page states its sample '
   'size (27 over 20 days) instead of publishing a figure with no n beside it. '
   'GENERALISES: scoring a signal from a price that was never paid selects for the '
   'moves that made the price unreachable. Any future measure that assumes a fill '
   'needs the same check.')
ON CONFLICT (slug) DO NOTHING;

SELECT
  (SELECT count(*) = 1 FROM public.learning_hypotheses
     WHERE slug = 'closed.unenterable-pnl.2026-10-06')                AS recorded,
  (SELECT status = 'closed' AND family = 'closed-defect'
     FROM public.learning_hypotheses
     WHERE slug = 'closed.unenterable-pnl.2026-10-06')                AS closed_defect,
  (SELECT closed_reason LIKE '%114.9%%' FROM public.learning_hypotheses
     WHERE slug = 'closed.unenterable-pnl.2026-10-06')                AS keeps_the_number,
  (SELECT count(*) = 0 FROM public.learning_results
     WHERE slug = 'closed.unenterable-pnl.2026-10-06')                AS no_eprocess;
