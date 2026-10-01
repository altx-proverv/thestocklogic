-- atlas_trades.product — the column every exit path dispatches on, and which has
-- never existed.
--
-- THE FAULT. market_open.open_positions_for_reconcile selects
-- id,symbol,direction,qty,product,gtt_trigger_id. PostgREST answers the whole
-- select with 400 when one column is absent, the function returns [], and
-- reconcile_exits therefore has never had a position to reconcile. Resting legs
-- were never cancelled. reconcile_exits' own docstring names the consequence: an
-- orphaned trigger can re-enter a symbol that is already closed.
--
-- It was invisible because the log line dropped the response body -- "open
-- positions read failed: HTTP 400", once a cycle, for a session, with the column
-- name sitting unread in r.text.
--
-- WHY THE COLUMN RATHER THAN DROPPING IT FROM THE SELECT. product is what decides
-- how a position is exited: a CNC long gets GTT legs that persist past the close,
-- an MIS short gets an SL-M regular order and the 15:15 square-off. exits.py
-- dispatches on it in protect() and filters on it in squareoff_mis(). Without it
-- squareoff_mis sees product=None on every row, matches none of them as MIS, and
-- an intraday short would be closed by the exchange at 15:20 with no order of
-- ours behind the fill.
--
-- THE BACKFILL IS EXACT, NOT A GUESS. position_sizing.size_by_risk assigns
-- product = "CNC" if LONG else "MIS", with no other branch, so direction
-- determines it for every row ATLAS has written. All 19 existing rows are LONG.

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS product text;

UPDATE public.atlas_trades
   SET product = CASE WHEN upper(direction) = 'LONG' THEN 'CNC' ELSE 'MIS' END
 WHERE product IS NULL;

ALTER TABLE public.atlas_trades
  ADD CONSTRAINT atlas_trades_product_chk
  CHECK (product IS NULL OR product IN ('CNC', 'MIS')) NOT VALID;

ALTER TABLE public.atlas_trades VALIDATE CONSTRAINT atlas_trades_product_chk;

COMMENT ON COLUMN public.atlas_trades.product IS
  'CNC for a delivery long, MIS for an intraday short. Written at reserve time from position_sizing.size_by_risk. Load-bearing for exits: protect() places GTT legs for CNC and an SL-M regular order for MIS, and squareoff_mis() filters on it for the 15:15 timer. A NULL here means an MIS short would not be squared off by ATLAS.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true. One statement at a time -- see migrations/README.md.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM information_schema.columns
     WHERE table_name='atlas_trades' AND column_name='product')      AS column_exists,
  (SELECT count(*) = 0 FROM public.atlas_trades WHERE product IS NULL) AS backfilled,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='atlas_trades_product_chk')                        AS check_exists,
  (SELECT convalidated FROM pg_constraint
     WHERE conname='atlas_trades_product_chk')                        AS check_valid,
  (SELECT col_description('public.atlas_trades'::regclass::oid, ordinal_position) IS NOT NULL
     FROM information_schema.columns
     WHERE table_name='atlas_trades' AND column_name='product')       AS has_comment;
