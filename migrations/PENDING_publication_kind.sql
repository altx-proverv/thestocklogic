-- Distinguish an actionable signal from a watched candidate, in one column.
--
-- WHY THE WATCHLIST HAS TO BE PUBLISHED AT ALL. The loop watches only what reaches
-- the signals table, and it was watching 3 symbols out of 706 because a signal is
-- published only when price is already within 0.30% of its zone AT LAST NIGHT'S
-- CLOSE. market_open.near_zone re-measures that against the live price every
-- cycle, so the close-time test throws away symbols the live test would have
-- caught. Watching them means publishing them -- a symbol ATLAS can ENTER that was
-- never published gives you two populations that disagree about what happened,
-- which is exactly what the wrong-side signals created.
--
-- WHY IT IS NOT ENOUGH TO JUST PUBLISH THEM. mark_signals scores every row in
-- signals with no direction or kind filter. Dropping ~90 candidates a session into
-- the same table would silently change what the accuracy record MEASURES: from
-- "signals we published as actionable" to "everything we watched". The number
-- would move for a reason that has nothing to do with signal quality, and nobody
-- reading it would know. So the population is named in the data rather than
-- inferred.
--
--   signal     price was at the zone at the close. Actionable, scored, shown as a
--              trade on the page. This is the existing population, unchanged.
--   candidate  valid in every way except distance, and within one ATR of its zone
--              so the gap is plausibly closable in a session. Watched by the loop,
--              NOT scored, and not a call to act on.
--
-- DEFAULT 'signal' AND A BACKFILL, both, so no row is ever NULL: a NULL would make
-- publication_kind=eq.signal quietly exclude every historical row, which is the
-- same shape as the filter bug that would have excluded the 443 blanks.

ALTER TABLE public.signals
  ADD COLUMN IF NOT EXISTS publication_kind text NOT NULL DEFAULT 'signal';

UPDATE public.signals SET publication_kind = 'signal'
  WHERE publication_kind IS NULL;

ALTER TABLE public.signals
  ADD CONSTRAINT signals_publication_kind_chk
  CHECK (publication_kind IN ('signal', 'candidate'));

COMMENT ON COLUMN public.signals.publication_kind IS
  'signal = price was within MAX_ENTRY_DIST_PCT of the zone at the close; actionable and scored by mark_signals. candidate = valid except for distance and within one ATR of the zone; watched by the market-hours loop, never scored, not a call. Every accuracy view and mark_signals filter on publication_kind = ''signal'' so the record keeps measuring the population it always measured.';

-- The watchlist, for the page and for anyone asking what the loop is watching.
CREATE OR REPLACE VIEW public.v_watchlist
WITH (security_invoker = true) AS
SELECT signal_date, symbol, direction, setup_name, zone_source,
       entry_ref, entry_low, entry_high, sl, stop_pct, entry_dist_pct, score
FROM public.signals
WHERE publication_kind = 'candidate'
ORDER BY signal_date DESC, entry_dist_pct ASC NULLS LAST;

COMMENT ON VIEW public.v_watchlist IS
  'Candidates the market-hours loop is watching: valid setups whose price was not at the zone at the close but is within one ATR of it. Ordered nearest-first. NOT recommendations and not scored -- an entry happens only if the live price reaches the zone during the session.';
