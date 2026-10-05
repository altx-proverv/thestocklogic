-- ATLAS restarts live at a fixed Rs10,000 notional. The trade record needs to say
-- what was actually taken, under which rule, and what it cost.
--
-- WHY EACH COLUMN. atlas_trades records entry_price and qty, so notional is
-- derivable -- but nothing stored the TARGET or which sizing rule produced the
-- quantity. When real sizing returns the table will hold two regimes and nothing
-- would tell them apart, which is precisely the gap the measurement-basis change
-- left in signal_outcomes: 293 verdicts computed one way and the rest another, with
-- no column saying which.
--
-- A KNOWN PROPERTY, NOT A FINDING. Quantity is round(10000/price), minimum one
-- share, no price ceiling. That makes realised notional a function of share price,
-- and two bands are biased in one direction only:
--
--     Rs6,666-10,000   can NEVER reach target (quantity is 1, price is below the
--                      target) -- median 77% of target, ceiling 99%
--     above Rs10,000   can NEVER be under -- median 142%, worst 1,293% (MRF)
--
-- Everything cheaper straddles 100%: 79% of signals sit under Rs2,000 a share and
-- land within 9% of target. So any RUPEE-denominated analysis of this record will
-- show a size bias by price band, and it is arithmetic rather than a result. R
-- multiples are unaffected, which is why the learning loop reads those.

ALTER TABLE public.atlas_trades ADD COLUMN IF NOT EXISTS notional numeric;
ALTER TABLE public.atlas_trades ADD COLUMN IF NOT EXISTS target_notional numeric;
ALTER TABLE public.atlas_trades ADD COLUMN IF NOT EXISTS notional_pct numeric;
ALTER TABLE public.atlas_trades ADD COLUMN IF NOT EXISTS oversized boolean NOT NULL DEFAULT false;
ALTER TABLE public.atlas_trades ADD COLUMN IF NOT EXISTS sizing_basis text;
ALTER TABLE public.atlas_trades ADD COLUMN IF NOT EXISTS risk_inr numeric;
ALTER TABLE public.atlas_trades ADD COLUMN IF NOT EXISTS costs_inr numeric;
ALTER TABLE public.atlas_trades ADD COLUMN IF NOT EXISTS pnl_net numeric;

-- The 19 trades already in the table were sized the old way. Labelled rather than
-- left NULL, so "no basis recorded" and "the previous basis" are different states.
UPDATE public.atlas_trades
   SET sizing_basis = 'risk-3000-notional-100000-mult5-v1'
 WHERE sizing_basis IS NULL;

CREATE INDEX IF NOT EXISTS atlas_trades_basis_idx
  ON public.atlas_trades (sizing_basis, entry_date DESC);

COMMENT ON COLUMN public.atlas_trades.sizing_basis IS
  'Which rule produced the quantity. fixed-notional-10000-mult1-v1 = the measuring instrument from 2026-10-06: quantity is round(10000/price), minimum one share, no price ceiling, and risk is whatever the stop implies (about Rs205 at the median). risk-3000-notional-100000-mult5-v1 = the previous rule, quantity derived from a Rs3,000 risk budget. Never compare rupee P&L across the two without grouping on this.';

COMMENT ON COLUMN public.atlas_trades.notional_pct IS
  'Realised notional as a percentage of target. Structurally biased by share price and that is arithmetic, not a finding: the Rs6,666-10,000 band can never reach 100% and the above-Rs10,000 band can never fall below it.';

COMMENT ON COLUMN public.atlas_trades.oversized IS
  'The share price exceeded the whole notional target, so one share was taken regardless. Deliberate -- the instrument must not be blind to MRF, MARUTI or ABBOTINDIA because of its own test size -- and flagged so the record knows. 19 symbols in the published universe trade above Rs10,000; the worst carries about 6x the median trade''s risk.';

COMMENT ON COLUMN public.atlas_trades.pnl IS
  'GROSS. (exit - entry) * qty, no costs. This is what the learning loop measures, deliberately: folding costs in would mean the hit-rate hypotheses were testing the broker''s fee schedule as well as the engine''s edge. See pnl_net.';

COMMENT ON COLUMN public.atlas_trades.pnl_net IS
  'Gross less modelled costs. THE RATES ARE UNVERIFIED -- the published discount-broker structure as understood on 2026-10-06, not figures read off a contract note. At Rs10,000 costs are about 5% of 1R on an intraday short and 11% on a delivery long, and the asymmetry is the opposite of intuition: delivery STT is 0.1% on both legs where intraday is 0.025% on the sell leg only. See atlas/risk/costs.py.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 8 FROM information_schema.columns
     WHERE table_name='atlas_trades'
       AND column_name IN ('notional','target_notional','notional_pct','oversized',
                           'sizing_basis','risk_inr','costs_inr','pnl_net'))  AS eight_columns,
  (SELECT count(*) = 0 FROM public.atlas_trades WHERE sizing_basis IS NULL)   AS all_labelled,
  (SELECT count(*) = 19 FROM public.atlas_trades
     WHERE sizing_basis = 'risk-3000-notional-100000-mult5-v1')               AS old_trades_labelled,
  (SELECT is_nullable = 'NO' FROM information_schema.columns
     WHERE table_name='atlas_trades' AND column_name='oversized')            AS oversized_not_null,
  (SELECT count(*) = 1 FROM pg_indexes
     WHERE indexname='atlas_trades_basis_idx')                                AS basis_index,
  (SELECT col_description('public.atlas_trades'::regclass::oid, ordinal_position)
            LIKE 'GROSS%' FROM information_schema.columns
     WHERE table_name='atlas_trades' AND column_name='pnl')                   AS pnl_marked_gross;
