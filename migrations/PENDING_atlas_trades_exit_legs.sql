-- atlas_trades: the three columns the day-0 -> day-1 handover needs.
--
-- WHY THIS BLOCKS CLEARING THE HALT. atlas/execution/preclose_gtt.py is the job
-- that converts a day-0 SL order into the two overnight GTTs at 15:20, and it
-- cannot run at all:
--
--   open longs read failed: HTTP 400
--   {"code":"42703","message":"column atlas_trades.exit_legs does not exist"}
--
-- PostgREST rejects the WHOLE select when one named column is absent, so the
-- job cannot even read open positions. It fails closed and alerts PRECLOSE
-- BLIND -- correct behaviour, and useless protection. Without these columns:
--
--   entry fills       -> day-0 SL placed            (works today)
--   15:20             -> preclose_gtt 400s, alerts, exits 1
--   16:00             -> the exchange flushes the SL
--   next open         -> NO overnight GTT. Position unprotected.
--
-- That is the 2026-10-07 failure class. The halt stays until this is applied.
--
-- FOUND BY RUNNING THE JOB, not by reading it -- `preclose_gtt --dry-run` on the
-- box, after installing its cron line. The fourth instance in this project of a
-- writer naming a column the schema does not have (gtt_trigger_id,
-- delivery_pct, live_prices.volume were the others), which is why
-- migrations/README.md says to verify the objects rather than the absence of an
-- error.
--
-- APPLY ONE STATEMENT AT A TIME. The Supabase editor has twice applied only a
-- prefix of a pasted file and reported success. §4 is the catalogue query; read
-- it rather than the editor's message.

-- ─────────────────────────────────────────────────────────────────────
-- 1. exit_legs -- the broker ids of what is protecting the position.
--
--    JSONB, NOT COLUMNS, because the shape differs by phase and by half:
--      day 0      {"day0_sl_order_id": "..."}
--      overnight  {"oco_trigger_id": 70001, "oco_qty": 5,
--                  "trail_trigger_id": 70002, "trail_qty": 5}
--    A column per leg would need six, and five of them null at any moment.
--
--    LOAD-BEARING FOR THE TRAIL, not just for the record: preclose_gtt.trail()
--    reads trail_trigger_id and trail_qty out of this to know WHICH GTT to
--    modify. With no exit_legs the stop can never move, so the whole
--    move-up-only trail is inert.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS exit_legs jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.atlas_trades.exit_legs IS
  'Broker ids of the protection resting against this position. day 0: {"day0_sl_order_id"}. overnight: {"oco_trigger_id","oco_qty","trail_trigger_id","trail_qty"}. preclose_gtt.trail() reads trail_trigger_id and trail_qty from here to decide which GTT to modify, so an empty object means the stop cannot be trailed.';

-- ─────────────────────────────────────────────────────────────────────
-- 2. exit_mechanism -- which of the mechanisms is actually in force.
--
--    Text rather than an enum: the vocabulary has changed three times in a
--    week (SLM -> SL -> GTT_OCO -> SL_DAY0/GTT_SPLIT) and an enum would have
--    needed a migration each time. The values are documented in
--    atlas/execution/exits.py ORDER_SPECS.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS exit_mechanism text;

COMMENT ON COLUMN public.atlas_trades.exit_mechanism IS
  'SL_DAY0 (day-0 stop-loss order) | GTT_SPLIT (overnight OCO + stop-only pair) | NONE. Text and not an enum because the vocabulary changed three times in one week.';

-- ─────────────────────────────────────────────────────────────────────
-- 3. target_price -- the 2R level the OCO half is booked at.
--
--    SEPARATE FROM target_1 AND target_2, which already exist and are NOT the
--    same thing: those came from the signal's published levels, this is 2R
--    measured from the ACTUAL FILL. Writing this into target_1 would overwrite
--    a published figure with a derived one and make the record unable to say
--    which it held.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS target_price numeric;

COMMENT ON COLUMN public.atlas_trades.target_price IS
  'The 2R target on the OCO half, measured from the ACTUAL fill. Distinct from target_1/target_2, which are the signal''s published levels -- a MARKET or LIMIT fill slips, so 2R of the risk actually taken is not 2R of the published entry.';

-- ─────────────────────────────────────────────────────────────────────
-- 4. VERIFY. Run this LAST and read it. "Success" from the editor describes
--    the last statement it chose to run, not the file.
--
--    Expect three rows, all ok = true.
-- ─────────────────────────────────────────────────────────────────────

SELECT 'exit_legs' AS column_name,
       count(*) AS found, 1 AS expected, count(*) = 1 AS ok,
       string_agg(data_type || ' default ' ||
                  coalesce(column_default, 'none'), '') AS detail
  FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'atlas_trades'
   AND column_name = 'exit_legs'
UNION ALL
SELECT 'exit_mechanism', count(*), 1, count(*) = 1,
       string_agg(data_type, '')
  FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'atlas_trades'
   AND column_name = 'exit_mechanism'
UNION ALL
SELECT 'target_price', count(*), 1, count(*) = 1,
       string_agg(data_type, '')
  FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'atlas_trades'
   AND column_name = 'target_price';
