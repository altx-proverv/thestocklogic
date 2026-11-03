-- ════════════════════════════════════════════════════════════════════
-- index_quotes.price_changed_at: WHEN THE VALUE LAST MOVED, NOT THE ROW.
-- ════════════════════════════════════════════════════════════════════
--
-- WHY. The page's freshness rule reads updated_at, and updated_at is the
-- WRITER's heartbeat, not the price's. USDINR showed one price at +0.00% for a
-- week at a time -- the weekly contracts do not trade -- under a perfectly
-- fresh updated_at. That is live_sentiment's 2026-10-20 defect in a new place:
-- a check aimed at the row, not the value (atlas/freshness.py).
--
-- WHAT. A BEFORE trigger stamps price_changed_at from the DATABASE clock, and
-- only when ltp is DISTINCT FROM the stored ltp. No writer can forget it, no
-- vendor clock is involved, and a write that repeats a value leaves the stamp
-- where it was -- so (now - price_changed_at) is how long the price has
-- actually been unchanged, whatever updated_at says.
--
-- UPSERTS. PostgREST's merge-duplicates is INSERT ... ON CONFLICT DO UPDATE:
-- the BEFORE INSERT branch stamps the proposed row, and on conflict the BEFORE
-- UPDATE branch sees OLD and decides. A payload never carries
-- price_changed_at, so OLD's value is kept unless ltp moved.
--
-- EXISTING ROWS START NULL. When their price last changed is not known, and
-- the page treats an unknown as not-live rather than inventing a time.
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE public.index_quotes ADD COLUMN price_changed_at timestamptz;

COMMENT ON COLUMN public.index_quotes.price_changed_at IS
  'Database time at which ltp last took a different value. Set only by trigger. '
  'NULL = not yet observed to change since this column was added.';

CREATE OR REPLACE FUNCTION public.index_quotes_stamp_price_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.price_changed_at := now();
  ELSIF NEW.ltp IS DISTINCT FROM OLD.ltp THEN
    NEW.price_changed_at := now();
  ELSE
    NEW.price_changed_at := OLD.price_changed_at;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER index_quotes_stamp_price_change
  BEFORE INSERT OR UPDATE ON public.index_quotes
  FOR EACH ROW EXECUTE FUNCTION public.index_quotes_stamp_price_change();
