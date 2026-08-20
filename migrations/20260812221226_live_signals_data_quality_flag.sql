-- btst_engine.get_live_prices() selected a `volume` column live_prices does not
-- have, so PostgREST returned 400, the status check returned {}, and every BTST
-- candidate was scored against the PREVIOUS CLOSE instead of a live price --
-- for the whole life of the module. score_btst's ltp fell back to close on
-- every row.
--
-- Those signals are not a track record. They are the output of a scorer running
-- on stale inputs, and the accuracy record is what the subscriber product is
-- sold on, so the caveat has to live with the data rather than in a commit
-- message.
--
-- Marked rather than deleted: the rows are evidence of what the engine did, and
-- deleting them would lose that. ltp IS NULL is the fingerprint -- a genuine
-- live price is always populated.

ALTER TABLE public.live_signals
  ADD COLUMN IF NOT EXISTS data_quality text;

COMMENT ON COLUMN public.live_signals.data_quality IS
  'NULL = normal. STALE_PRICES = scored without a live price feed; not a valid track record. Set for the BTST rows produced while get_live_prices() was returning 400.';

UPDATE public.live_signals
   SET data_quality = 'STALE_PRICES'
 WHERE session = 'btst'
   AND (ltp IS NULL OR ltp = 0)
   AND data_quality IS NULL;