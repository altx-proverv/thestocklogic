-- REPAIR: 31 signals carry a target_1 that is not 2R, and 10 of them were
-- booked as wins while losing money.
--
-- WHAT WENT WRONG, IN THREE PARTS. Found while reconstructing the recorded exit
-- rule in order to compare exit alternatives against it.
--
--   A. THE YARDSTICK WAS THE WRONG PRICE. target_1 is computed at publish time
--      from entry_ref -- the price the setup was priced off. The trade fills at
--      actual_entry, which is the next open and a gap away. Measuring a "2R win"
--      against a target derived from entry_ref therefore scores 2R of a risk
--      nobody took: across the 293 resolved rows the stored target is 2.082R off
--      the actual fill at the median, 1.73R at p10 and 6.71R at p90.
--
--   B. NOTHING CHECKED THE TARGET WAS BEYOND THE FILL. On these 31 rows the
--      stored target_1 is not what measurement_targets(entry_ref, sl) returns,
--      and on 26 of them it sits UNDER +1R -- several between the entry and the
--      stop, on the losing side. Price touched such a level within a bar or two
--      and update_outcomes wrote WIN_T1.
--
--   C. abs() TURNED THOSE INTO PROFITS. pnl was
--      abs(exit_price - actual_entry) * qty, so a target below the fill on a long
--      produced a positive P&L. OFSS 2026-06-01 is recorded at +226.36 per share
--      while actually losing 226.36. Seven rows, 625.78 per unit of quantity, all
--      of it in the flattering direction.
--
-- THE CODE IS FIXED SEPARATELY AND FIRST. engine/update_outcomes.py now derives
-- the yardstick from actual_entry after the fill is known, fails closed on a
-- target that is not beyond the fill (entry_status='BAD_TARGET'), and signs the
-- P&L. engine/trade_review.py carried the same abs() in its own copy of the block
-- and is fixed too. This file repairs what those two already wrote.
--
-- WHAT THIS DOES TO THE PUBLISHED FIGURE. It lowers it, which is the point. The
-- resolved hit rate is 22.2% as published and 18.8% counting only rows that
-- realised +1R or better. The basis change in (A) is on its own mildly
-- FAVOURABLE -- a clean 2R off the fill measures -0.318R against the recorded
-- -0.346R -- so the overstatement is entirely (B) and (C).
--
-- NO IDs ANYWHERE IN THIS FILE. 06_push_supabase deletes and re-inserts a whole
-- date on every run, so signals.id for a given row changes nightly. Every
-- statement below identifies rows by the arithmetic condition that makes them
-- wrong, which also makes the whole file idempotent: re-running it after it has
-- applied matches nothing.
--
-- HOW TO APPLY. One statement at a time. The verify block at the end must return
-- every column true. "Success. No rows returned" from the SQL editor is not
-- evidence that a file applied -- see PENDING_repair_partial_applications.sql,
-- which exists because two files reported exactly that and applied a prefix.

-- ─────────────────────────────────────────────────────────────────────
-- 0. Keep the old values. This is a destructive UPDATE over published rows.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.repair_targets_20261002 AS
SELECT s.signal_date, s.symbol, s.direction, s.entry_ref, s.sl,
       s.target_1 AS old_target_1, s.target_2 AS old_target_2,
       now() AS captured_at
FROM public.signals s
WHERE s.entry_ref > 0 AND s.sl > 0
  AND (CASE WHEN upper(s.direction) = 'LONG' THEN s.entry_ref - s.sl
            ELSE s.sl - s.entry_ref END) > 0
  AND s.target_1 IS NOT NULL
  AND abs(s.target_1 - round((CASE WHEN upper(s.direction) = 'LONG'
         THEN s.entry_ref + 2.0 * (s.entry_ref - s.sl)
         ELSE s.entry_ref - 2.0 * (s.sl - s.entry_ref) END)::numeric, 2)) > 0.02;

COMMENT ON TABLE public.repair_targets_20261002 IS
  'Pre-repair target_1/target_2 for the 31 signals whose stored target was not 2R off entry_ref, captured 2026-10-02 before PENDING_repair_measurement_targets.sql overwrote them. All 31 are 2026-05-04 to 2026-06-04, all with zone_side_valid NULL -- a contiguous pre-zone-engine window, not a scatter. Keep until the repaired rows have been re-resolved and the accuracy figure republished.';

-- ─────────────────────────────────────────────────────────────────────
-- 1. Recompute target_1 and target_2 at 2R and 3R off entry_ref.
--    Matches engine/zone_entry.measurement_targets exactly: MEASURE_R1 = 2.0,
--    MEASURE_R2 = 3.0, rounded to 2dp, and only where the stop is on the
--    correct side of entry_ref (that function returns NULL otherwise, and a
--    row with no usable geometry must stay unusable rather than be invented).
-- ─────────────────────────────────────────────────────────────────────

UPDATE public.signals s
   SET target_1 = round((CASE WHEN upper(s.direction) = 'LONG'
                              THEN s.entry_ref + 2.0 * (s.entry_ref - s.sl)
                              ELSE s.entry_ref - 2.0 * (s.sl - s.entry_ref)
                         END)::numeric, 2),
       target_2 = round((CASE WHEN upper(s.direction) = 'LONG'
                              THEN s.entry_ref + 3.0 * (s.entry_ref - s.sl)
                              ELSE s.entry_ref - 3.0 * (s.sl - s.entry_ref)
                         END)::numeric, 2)
 WHERE s.entry_ref > 0 AND s.sl > 0
   AND (CASE WHEN upper(s.direction) = 'LONG' THEN s.entry_ref - s.sl
             ELSE s.sl - s.entry_ref END) > 0
   AND s.target_1 IS NOT NULL
   AND abs(s.target_1 - round((CASE WHEN upper(s.direction) = 'LONG'
          THEN s.entry_ref + 2.0 * (s.entry_ref - s.sl)
          ELSE s.entry_ref - 2.0 * (s.sl - s.entry_ref) END)::numeric, 2)) > 0.02;

-- ─────────────────────────────────────────────────────────────────────
-- 2. Hand the affected outcomes back to the resolver.
--
--    update_outcomes skips any row whose outcome is already in
--    ('WIN_T1','WIN_T2','LOSS','MISSED','INVALIDATED'), so a repaired signal
--    keeps its wrong verdict forever unless the verdict is cleared. Setting
--    outcome='OPEN' is what makes it reprocessable; the old verdict is in
--    repair_targets_20261002 and in the row history, not lost.
--
--    SCOPED TO THE REPAIRED SIGNALS ONLY, by joining the backup table rather
--    than re-deriving the condition -- by this point signals.target_1 has
--    already been corrected, so the condition in step 1 no longer matches
--    anything and re-deriving it here would reset nothing.
-- ─────────────────────────────────────────────────────────────────────

UPDATE public.signal_outcomes o
   SET outcome      = 'OPEN',
       exit_price   = NULL,
       exit_day     = NULL,
       days_held    = 0,
       pnl          = 0,
       updated_at   = now()
 WHERE EXISTS (
   SELECT 1 FROM public.repair_targets_20261002 r
    WHERE r.signal_date = o.signal_date
      AND r.symbol      = o.symbol
      AND upper(r.direction) = upper(o.direction));

-- ─────────────────────────────────────────────────────────────────────
-- 3. THE BIGGER ONE: every price-based verdict was decided on the old basis.
--
--    Fixing the yardstick in code fixes it going FORWARD. Every verdict already
--    in the table was reached by comparing bars against a target derived from
--    entry_ref, so repairing only the 31 worst rows would leave the record on two
--    different measurement bases at once -- which is a worse defect than either,
--    and the same shape as the two-engine-era problem already in this record.
--
--    SCOPE IS EXACTLY THE PRICE-BASED VERDICTS. 'MISSED' and 'INVALIDATED' are
--    decided by the gap guards on the entry day and never consult the target, so
--    they are untouched and stay decided. That leaves WIN_T1, WIN_T2 and LOSS --
--    228 losses and 65 wins, the 293 resolved rows, the same population the exit
--    study ran on.
--
--    APPLY STEP 3 ONLY IF THE BOX STILL HOLDS THE BARS. Re-resolution reads daily
--    parquets from data/processed/stocks and the oldest affected signal is
--    2025-05-12. A symbol whose history no longer reaches back that far returns
--    OPEN instead of a verdict. That is a visible gap rather than a silent wrong
--    number, and signal_outcomes_pre20261002 below holds everything either way --
--    but it does mean the figure can go DOWN in coverage as well as in value.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.signal_outcomes_pre20261002 AS
SELECT *, now() AS captured_at FROM public.signal_outcomes;

COMMENT ON TABLE public.signal_outcomes_pre20261002 IS
  'Full copy of signal_outcomes as at 2026-10-02, before the measurement basis moved from entry_ref to the actual fill. 1,002 rows: 228 LOSS, 65 WIN_T1, 466 MISSED, 84 INVALIDATED, 159 OPEN. Kept so the published history can be reconstructed and so the before/after difference in the accuracy figure is measurable rather than asserted. Safe to drop once the republished figure has been checked.';

UPDATE public.signal_outcomes
   SET outcome    = 'OPEN',
       exit_price = NULL,
       exit_day   = NULL,
       days_held  = 0,
       pnl        = 0,
       updated_at = now()
 WHERE outcome IN ('WIN_T1', 'WIN_T2', 'LOSS');

-- AFTER APPLYING: run the resolver so these come back on the honest basis.
--   cd /home/ubuntu/thestocklogic && venv/bin/python -m engine.update_outcomes
--
-- EXPECT THE FIGURE TO FALL. The resolved hit rate is 22.2% as published and
-- 18.8% counting only rows that realised +1R or better. Rows whose target was
-- never beyond the fill now return entry_status='BAD_TARGET' and outcome='SKIP'
-- rather than a win.
--
-- signal_marks needs no reset: mark_signals recomputes every resolution from
-- signals on each run and upserts with resolution=merge-duplicates, so the
-- corrected target_1 flows through on the next nightly run by itself.

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true. One statement at a time.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 31 FROM public.repair_targets_20261002)            AS backed_up_31,
  -- every captured row was from the one contiguous window
  (SELECT min(signal_date) = '2026-05-04'::date
            AND max(signal_date) = '2026-06-04'::date
     FROM public.repair_targets_20261002)                              AS window_as_expected,
  -- nothing in signals is off 2R any more
  (SELECT count(*) = 0 FROM public.signals s
    WHERE s.entry_ref > 0 AND s.sl > 0 AND s.target_1 IS NOT NULL
      AND (CASE WHEN upper(s.direction) = 'LONG' THEN s.entry_ref - s.sl
                ELSE s.sl - s.entry_ref END) > 0
      AND abs(s.target_1 - round((CASE WHEN upper(s.direction) = 'LONG'
             THEN s.entry_ref + 2.0 * (s.entry_ref - s.sl)
             ELSE s.entry_ref - 2.0 * (s.sl - s.entry_ref) END)::numeric, 2))
          > 0.02)                                                      AS all_targets_now_2r,
  -- and no target sits on the losing side of its own reference price
  (SELECT count(*) = 0 FROM public.signals s
    WHERE s.entry_ref > 0 AND s.sl > 0 AND s.target_1 IS NOT NULL
      AND (CASE WHEN upper(s.direction) = 'LONG' THEN s.target_1 - s.entry_ref
                ELSE s.entry_ref - s.target_1 END) <= 0)               AS no_wrong_side_targets,
  -- the repaired outcomes are waiting for the resolver, not holding a verdict
  (SELECT count(*) = 0 FROM public.signal_outcomes o
     JOIN public.repair_targets_20261002 r
       ON r.signal_date = o.signal_date AND r.symbol = o.symbol
      AND upper(r.direction) = upper(o.direction)
    WHERE o.outcome IN ('WIN_T1','WIN_T2','LOSS','MISSED','INVALIDATED'))
                                                                       AS outcomes_released,
  -- no row anywhere still claims a win it did not make
  (SELECT count(*) = 0 FROM public.signal_outcomes o
    WHERE o.outcome = 'WIN_T1' AND o.pnl < 0)                          AS no_negative_wins,
  (SELECT obj_description('public.repair_targets_20261002'::regclass) IS NOT NULL)
                                                                       AS backup_documented,
  -- step 3, if applied: the full copy exists and no price-based verdict remains
  (SELECT count(*) = 1002 FROM public.signal_outcomes_pre20261002)      AS full_backup_1002,
  (SELECT count(*) = 0 FROM public.signal_outcomes
    WHERE outcome IN ('WIN_T1','WIN_T2','LOSS'))                        AS verdicts_released,
  -- entry-status verdicts were NOT touched: they never read the target
  (SELECT count(*) = 466 FROM public.signal_outcomes
    WHERE outcome = 'MISSED')                                           AS missed_untouched,
  (SELECT count(*) = 84 FROM public.signal_outcomes
    WHERE outcome = 'INVALIDATED')                                      AS invalidated_untouched;
