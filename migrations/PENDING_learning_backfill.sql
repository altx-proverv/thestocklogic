-- BACKFILL: what we already know, so the loop does not re-discover it blind.
--
-- APPLY AFTER PENDING_learning_ledger.sql.
--
-- These are CLOSED results on a PRE-REPAIR basis, and the basis_version says so.
-- None of them can be continued: the measurement basis moved from entry_ref to the
-- actual fill on 2026-10-03 and 293 verdicts were re-resolved, so an e-process
-- spanning that boundary would be multiplying likelihood ratios computed under two
-- different definitions of a win. They are here to stop the next analysis
-- re-testing the same things and calling the same nothing new.
--
-- No e-values: these were fixed-sample studies with p-values and q-values, which is
-- what the ledger exists to replace. log_e is recorded as 0 and crossed as false,
-- with the actual finding in the verdict text. A zero is honest here -- it means
-- "this lineage carries no anytime-valid evidence", which is exactly true.

-- ─────────────────────────────────────────────────────────────────────
-- 1. THE TWELVE-FEATURE NULL — 2026-09-30
-- ─────────────────────────────────────────────────────────────────────

INSERT INTO public.learning_hypotheses
  (slug, question, population, statistic, null_value, side, family,
   basis_version, seed_from, status, registered_by, rationale,
   closed_at, closed_reason)
VALUES
  ('closed.features12.2026-09-30',
   'Does any of twelve recorded features separate winners from losers?',
   '293 resolved signals, 2025-05 to 2026-09, pre-repair basis',
   'Mann-Whitney AUC and chi-square, Benjamini-Hochberg across 12',
   NULL, 'two-sided', 'closed-study', 'entry_ref-2R-v1', '2025-05-12', 'closed',
   'backfill-2026-10-06',
   'setup_name, grade, score, RVOL, delivery %, ATR percentile, zone source, '
   'structure trend, stop width, direction, sector, regime. Four of the twelve were '
   'untestable as recorded and the five sub-scores are one quantity six ways '
   '(score equals their sum on all 845 rows, mean gap 0.00).',
   '2026-09-30 00:00:00+00',
   'NOTHING SEPARATES. Zero of twelve survive BH; best adjusted q = 0.062 and it '
   'belongs to a 45-row artefact of a known defect. Fifty-five pairs: zero survive, '
   'and the best is beaten by pure noise in 5 of 25 shuffled replicas. The sample '
   'had 96% power to detect AUC 0.65, so this is evidence of absence at that effect '
   'size rather than an inconclusive result. Two findings DID clear correction and '
   'neither is tradeable: structure trend (q=0.003) dissolves into "shorts stopped '
   'working after June", and wrong-side zones (q=0.025, 0 wins in 21) confirm a bug '
   'since fixed.')
ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────
-- 2. THE EXIT STUDY — 2026-10-02. CLOSED AND NOT RE-TESTED.
-- ─────────────────────────────────────────────────────────────────────
-- The operator's instruction on 2026-10-03 was explicit: the exits are settled,
-- keep the recorder running so the question stays answerable, and stop proposing
-- exit changes. So this is in the ledger and deliberately NOT in the standing set --
-- which is the distinction the ledger exists to make possible.

INSERT INTO public.learning_hypotheses
  (slug, question, population, statistic, null_value, side, family,
   basis_version, seed_from, status, registered_by, rationale,
   closed_at, closed_reason)
VALUES
  ('closed.exits14.2026-10-02',
   'Does any exit rule make the resolved set profitable?',
   '284 rebuilt 20-day price paths, same fill and same initial stop',
   'mean R per trade, paired bootstrap against the recorded rule, BH across 13',
   0.0, 'greater', 'closed-study', 'paths-substitute-bars-v1', '2025-05-12',
   'closed', 'backfill-2026-10-06',
   'Trailing stops at four aggressions, 3R and 4R targets, time stops at 5, 10 and '
   '20 days. Bars were rebuilt from an independent source because the local series '
   'were stale; replaying the identical rule against the box''s authoritative '
   'verdicts agreed on 278 of 279 (99.6%).',
   '2026-10-02 00:00:00+00',
   'NO EXIT RULE HELPS. All fourteen have a 95% CI entirely below zero. Best is a 1R '
   'trailing stop at -0.221R against the recorded rule''s -0.318R, and it fails every '
   'robustness check: BH q = 0.32, a 56/44 split against the baseline, and WORSE '
   '(-0.536R) under the adverse reading of the bars. The decisive measurement is not '
   'the rule table but the payoff curve: at every target from +1R to +6R the hit rate '
   'falls short of its own breakeven by 8 to 14 points, so there is no target where '
   'the curve crosses. Holding longer is strictly worse: -0.376R / -0.403R / -0.462R '
   'at 5 / 10 / 20 days. THE ENTRY IS THE PROBLEM, not the payoff.')
ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────
-- 3. THE WRONG-SIDE ZONE DEFECT — resolved by exclusion
-- ─────────────────────────────────────────────────────────────────────

INSERT INTO public.learning_hypotheses
  (slug, question, population, statistic, null_value, side, family,
   basis_version, seed_from, status, registered_by, rationale,
   closed_at, closed_reason)
VALUES
  ('closed.wrongside.2026-09-30',
   'Do signals whose entry zone was resolved on the wrong side of price perform '
   'differently?',
   '75 signals with zone_side_valid = false',
   'win rate against correctly zoned signals on the same two sessions',
   NULL, 'two-sided', 'closed-defect', 'entry_ref-2R-v1', '2026-05-01', 'closed',
   'backfill-2026-10-06',
   'Not a market finding. A defect: the zone was resolved by trend rather than by '
   'the direction''s own family, so a long could be priced off a supply zone.',
   '2026-09-30 00:00:00+00',
   'CONFIRMED DEFECT, NOT AN EDGE. 40 of 41 resolved wrong-side LONGS hit their stop '
   '(2.4% win) against 61.5% for correctly zoned longs on the same two sessions. '
   'Wrong-side SHORTS resolved indistinguishably (-0.27% vs -0.40%) because every '
   'short resolves SAME_DAY before either level is reached. Fixed on 2026-09-30; the '
   'exclusion views went live 2026-10-05. Kept as evidence rather than deleted.')
ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────
-- 4. THE DETECTION MEASUREMENT — first night, 2026-10-05
-- ─────────────────────────────────────────────────────────────────────
-- STANDING, not closed: 2,832 detections have been published and none was ever
-- scored. The first 91 excursion rows landed on 2026-10-05. It seeds from there
-- because that is when the measurement began, not when the detections began.

INSERT INTO public.learning_hypotheses
  (slug, question, population, statistic, null_value, side, family,
   basis_version, seed_from, status, registered_by, rationale)
VALUES
  ('detections.reach2r',
   'Do intraday breakout detections reach +2R before their stop more often than the '
   '33.3% a 2R target needs to break even?',
   'signal_excursions where source = detection',
   'first-touch rate at +2R vs 1/(1+2)', 0.3333333333, 'greater',
   'detection-path-v1', '2026-10-05', 'standing', 'backfill-2026-10-06',
   '2,832 detections published since 2026-08-14 with entry, stop and two targets, '
   'and not one scored until now. On 688 of the 2,001 RBE rows target_2 is not even '
   'beyond target_1, so the published levels cannot be the yardstick -- R multiples '
   'off entry and stop are used instead. Its R is filled at the detection price on '
   'the detection day, a different convention from a signal''s next-open fill, which '
   'is why source exists on the table.')
ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 3 FROM public.learning_hypotheses
     WHERE status = 'closed')                                      AS three_closed,
  (SELECT count(*) = 1 FROM public.learning_hypotheses
     WHERE slug = 'detections.reach2r' AND status = 'standing')    AS detection_standing,
  (SELECT count(*) = 0 FROM public.learning_hypotheses
     WHERE basis_version = 'fill-2R-v2' AND status = 'closed')     AS no_closed_on_new_basis,
  (SELECT count(*) = 4 FROM public.learning_hypotheses
     WHERE registered_by = 'backfill-2026-10-06')                  AS all_four_backfilled,
  (SELECT bool_and(closed_reason IS NOT NULL) FROM public.learning_hypotheses
     WHERE status = 'closed')                                      AS every_closure_explained,
  (SELECT bool_and(rationale IS NOT NULL) FROM public.learning_hypotheses)
                                                                   AS every_one_justified;
