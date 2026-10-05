-- REPAIR: atlas_live_zones exists with 9 of its 16 columns and has never
-- received a row.
--
-- WHY THE ORIGINAL FILE CANNOT FIX THIS. PENDING_atlas_live_zones.sql opens with
-- CREATE TABLE IF NOT EXISTS. The table exists, so that statement is a no-op and
-- re-running the file adds nothing -- it will report success and change nothing,
-- which is the third time this project has seen that exact shape. Columns have to
-- be added explicitly.
--
-- PROBED AGAINST THE LIVE SCHEMA 2026-10-05:
--   present  symbol, direction, ltp, zone_low, zone_high, dist_pct, state,
--            publication_kind, cycle_at
--   MISSING  id, inside, held, sl, setup_name, batch_date, cycle_n
--
-- WHAT IT COSTS. market_open.push_live_zones sends all of them. PostgREST rejects
-- the whole insert on the first unknown column, so every cycle of every session
-- since the live view shipped has posted a payload that was thrown away whole:
-- atlas_live_zones has 0 rows, and the live section of signals.html has never
-- populated. It failed silently because the write is deliberately not routed
-- through the breaker -- a reporting table must not halt trading -- and nothing
-- else was watching. breaker.record_reporting_write now alerts on it without
-- halting, so a repeat is loud instead of invisible.

ALTER TABLE public.atlas_live_zones
  ADD COLUMN IF NOT EXISTS id bigserial;

-- `inside` and `held` are the two booleans the page branches on to tell "price is
-- in the zone" from "it got there and stayed". NOT NULL with a false default so a
-- row written by an older build cannot read as held.
ALTER TABLE public.atlas_live_zones
  ADD COLUMN IF NOT EXISTS inside boolean NOT NULL DEFAULT false;

ALTER TABLE public.atlas_live_zones
  ADD COLUMN IF NOT EXISTS held boolean NOT NULL DEFAULT false;

-- The stop, so the page can show what the risk would be at the live price rather
-- than re-deriving it from last night's signals row.
ALTER TABLE public.atlas_live_zones
  ADD COLUMN IF NOT EXISTS sl numeric;

ALTER TABLE public.atlas_live_zones
  ADD COLUMN IF NOT EXISTS setup_name text;

-- batch_date and cycle_n are the staleness evidence. cycle_at alone says when the
-- row was written; these say which batch it belongs to and which loop iteration
-- produced it, so a row left behind by a batch change is identifiable rather than
-- merely old.
ALTER TABLE public.atlas_live_zones
  ADD COLUMN IF NOT EXISTS batch_date date;

ALTER TABLE public.atlas_live_zones
  ADD COLUMN IF NOT EXISTS cycle_n integer;

-- The upsert target. push_live_zones resolves on (symbol, direction) to keep the
-- table current-state rather than a log; without this the upsert has nothing to
-- conflict on and every cycle would append.
ALTER TABLE public.atlas_live_zones
  ADD CONSTRAINT atlas_live_zones_symbol_direction_key UNIQUE (symbol, direction);

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true. One statement at a time -- see migrations/README.md.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 16 FROM information_schema.columns
     WHERE table_schema='public' AND table_name='atlas_live_zones')      AS all_16_columns,
  (SELECT count(*) = 7 FROM information_schema.columns
     WHERE table_schema='public' AND table_name='atlas_live_zones'
       AND column_name IN ('id','inside','held','sl','setup_name',
                           'batch_date','cycle_n'))                      AS the_seven_added,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='atlas_live_zones_symbol_direction_key')              AS upsert_key,
  -- the two booleans must not be nullable, or an old row reads as held
  (SELECT count(*) = 2 FROM information_schema.columns
     WHERE table_name='atlas_live_zones' AND is_nullable='NO'
       AND column_name IN ('inside','held'))                             AS booleans_not_null,
  (SELECT relrowsecurity FROM pg_class
     WHERE oid='public.atlas_live_zones'::regclass)                      AS rls_on,
  (SELECT count(*) >= 1 FROM pg_policies
     WHERE schemaname='public' AND tablename='atlas_live_zones')         AS read_policy;

-- AFTER APPLYING: the next market-hours cycle should write one row per watched
-- symbol. Confirm with
--   SELECT count(*), max(cycle_at) FROM public.atlas_live_zones;
-- and expect a non-zero count within one cycle of the loop running. If it is
-- still 0, the alert from breaker.record_reporting_write will now say why.
