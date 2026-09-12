-- Two-phase entry write: atlas_entry now commits a PENDING row BEFORE placing
-- the order, so no death between the two can leave a position that Gate 3b
-- cannot see. That needs two things from the schema.
--
-- 1. order_id as a real column.
--
--    _log_intent has always written the broker order id into the free-text
--    `notes` field and nothing has ever read it back. Reconcile has to join a
--    PENDING row to the order that may or may not exist behind it, and parsing
--    that out of prose is not a join.
--
--    Without this column the new writers name a field the schema does not
--    have, PostgREST answers 400 PGRST204, and we are back at the failure this
--    directory's README already lists three times -- gtt_trigger_id,
--    delivery_pct, live_prices.volume. The breaker halts on a 4xx rather than
--    retrying it, so the failure would be loud rather than silent this time,
--    but it would still be the same failure.
--
--    The order tag is ATLAS:<atlas_trades.id>. id is a bigint, so the tag fits
--    Kite's 20-character limit and the join is exact. That is the primary
--    link; order_id is what we store once the order is known, so a position
--    can be traced from the ledger without re-reading the broker.
--
-- 2. status accepting 'PENDING'.
--
--    atlas_trades predates this directory and its DDL is not in the repo, so
--    whether status carries a CHECK constraint could not be determined from
--    the code. The sibling tables created by migration -- atlas_entry_log and
--    backtest_runs -- both declare status as plain text with a COMMENT and no
--    CHECK, which is this project's style, so most likely there is nothing to
--    change. The block below finds out rather than assumes: it extends an
--    ARRAY-style constraint in place if one exists, does nothing if none does,
--    and refuses loudly if it finds a constraint in a shape it should not
--    rewrite blind.

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS order_id text;

CREATE INDEX IF NOT EXISTS atlas_trades_order_id_idx
  ON public.atlas_trades (order_id)
  WHERE order_id IS NOT NULL;

COMMENT ON COLUMN public.atlas_trades.order_id IS
  'Zerodha order id for the entry. Written when the order is confirmed placed; NULL while status is PENDING. The order carries tag ATLAS:<id>, which is the authoritative link -- this column is the convenience copy.';

DO $$
DECLARE
  con     record;
  newdef  text;
BEGIN
  FOR con IN
    SELECT c.conname, pg_get_constraintdef(c.oid) AS def
    FROM   pg_constraint c
    JOIN   pg_class t ON t.oid = c.conrelid
    JOIN   pg_namespace n ON n.oid = t.relnamespace
    WHERE  n.nspname = 'public'
      AND  t.relname = 'atlas_trades'
      AND  c.contype = 'c'
      AND  pg_get_constraintdef(c.oid) ILIKE '%status%'
  LOOP
    IF con.def ILIKE '%''PENDING''%' THEN
      RAISE NOTICE 'constraint % already permits PENDING', con.conname;
      CONTINUE;
    END IF;

    -- Only rewrite the shape we can rewrite safely: status = ANY (ARRAY[...]).
    IF con.def ~* 'ANY\s*\(\s*ARRAY\s*\[' THEN
      newdef := regexp_replace(con.def, '(ARRAY\s*\[)',
                               '\1''PENDING''::text, ', 'i');
      EXECUTE format('ALTER TABLE public.atlas_trades DROP CONSTRAINT %I',
                     con.conname);
      EXECUTE format('ALTER TABLE public.atlas_trades ADD CONSTRAINT %I %s',
                     con.conname, newdef);
      RAISE NOTICE 'constraint % extended to permit PENDING', con.conname;
    ELSE
      RAISE EXCEPTION
        'atlas_trades has CHECK constraint % in a shape this migration will '
        'not rewrite blind: %. Add PENDING by hand, then re-run.',
        con.conname, con.def;
    END IF;
  END LOOP;
END $$;

COMMENT ON COLUMN public.atlas_trades.status IS
  'PENDING | OPEN | GTT_PENDING | CLOSED | CANCELLED | SHADOW. PENDING is written before the order is placed and cleared once its outcome is known -- it means an order for this symbol may exist at the broker right now, and atlas.config.BLOCKING_STATUSES makes Gate 3b treat it as a holding. A PENDING row older than ~2 minutes is unresolved and is reconcile.settle()''s work.';
