-- _log_intent has been POSTing gtt_trigger_id and trigger_price since 3b37815.
-- Neither column existed, so PostgREST rejected every write with
-- 400 PGRST204 "Could not find the 'gtt_trigger_id' column". requests.post does
-- not raise on 4xx and _log_intent only caught exceptions, so the rejection was
-- discarded silently: no row, no warning, no traceback.
--
-- Consequence, observed 2026-08-11: GTT 331263278 (GRASIM, 30 qty, trigger
-- 3115.70) was really placed at Zerodha and is still resting, while
-- atlas_trades has no record of it -- so /atlas, the daily report and the funds
-- check all believe nothing is committed.
--
-- gtt_trigger_id is also the ONLY reliable way to recognise an ATLAS GTT: Kite's
-- get_gtts() response omits the order `tag` field entirely, so tag-based
-- matching can never work.

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS gtt_trigger_id text,
  ADD COLUMN IF NOT EXISTS trigger_price  numeric;

CREATE INDEX IF NOT EXISTS atlas_trades_gtt_trigger_id_idx
  ON public.atlas_trades (gtt_trigger_id)
  WHERE gtt_trigger_id IS NOT NULL;

COMMENT ON COLUMN public.atlas_trades.gtt_trigger_id IS
  'Zerodha GTT trigger id for a resting entry. The only reliable link between a broker GTT and the ATLAS trade that placed it -- Kite does not echo order tags back from get_gtts().';
COMMENT ON COLUMN public.atlas_trades.trigger_price IS
  'Price the GTT triggers at (the zone edge). Distinct from entry_price, which is the intended fill.';