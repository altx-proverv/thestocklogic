-- SUPERSEDED FOR REPAIR PURPOSES by PENDING_atlas_live_zones_repair.sql.
-- This file opens with CREATE TABLE IF NOT EXISTS and the table already exists
-- with 9 of 16 columns, so re-running it is a no-op that reports success. Apply
-- the repair file instead; this one stays as the record of what the table was
-- meant to be.
-- ─────────────────────────────────────────────────────────────────────
-- STATUS 2026-10-03: PARTIALLY APPLIED, AND THE LIVE VIEW IS DEAD UNTIL IT IS NOT.
--
-- Probed against the live schema today:
--   present  symbol, direction, signal_date, updated_at
--   MISSING  inside, held, sl, setup_name, batch_date, cycle_n
--
-- atlas/signal/market_open.push_live_zones() sends all ten. PostgREST rejects the
-- whole insert on the first unknown column, so atlas_live_zones has never received
-- a row and the live section of signals.html has never populated. It fails
-- silently because push_live_zones is deliberately not routed through
-- breaker.record_read -- a view that cannot fill is not a reason to halt trading.
--
-- This is the same shape as PENDING_repair_partial_applications: a file that
-- reported success and applied a prefix. Run the remaining statements ONE AT A
-- TIME and check the verify block.
-- ─────────────────────────────────────────────────────────────────────

-- The live view: what the market-hours loop is watching, right now.
--
-- WHY A TABLE AND NOT A VIEW OVER signals. The loop already fetches a quote for
-- every symbol in the zone map, once per cycle, in one batched Upstox request --
-- and then discards every quote that is not at a zone. That discarded data is
-- the only live picture of the engine that exists anywhere. signals holds last
-- night's close-time geometry; this holds the price against it.
--
-- WHY A SEPARATE TABLE. Different lifecycle and different consumers. signals is
-- append-per-batch, read by mark_signals and the accuracy views, and is the
-- subscriber-facing record. This is current state only -- overwritten every 60
-- seconds, never appended -- read by the page during market hours and by nothing
-- else. Putting them together would mean the accuracy record's base table
-- changing 360 times a session.
--
-- NO HISTORY, AND THE TABLE IS BOUNDED. The upsert key is (symbol, direction),
-- so there is exactly one row per watched pair and the table can never exceed
-- 2 x the universe -- about 920 rows. A pair that drops out of the zone map on a
-- batch change simply stops being updated and keeps an old cycle_at, which the
-- page already has to detect anyway. That is why there is no DELETE here and no
-- cleanup job: the staleness check the page needs for a dead loop is the same
-- check that handles a dropped symbol.
--
-- dist_pct IS UNSIGNED, AND state CARRIES THE DIRECTION. A signed distance would
-- have to mean different things for a long and a short, and the page would have
-- to re-derive which side counts as "not there yet". state is written from
-- market_open.at_zone(), which is the one place that decides it:
--
--   at_zone     price is inside the band. The entry condition.
--   waiting     price has not reached the band from the correct side.
--   mitigated   price has passed THROUGH the band -- the setup failed rather
--               than cheapened, and it will not be entered today.
--
-- Sorting on dist_pct ASC therefore puts the nearest first regardless of side,
-- and the page never has to know what a demand zone is.

CREATE TABLE IF NOT EXISTS public.atlas_live_zones (
  id               bigserial PRIMARY KEY,
  symbol           text        NOT NULL,
  direction        text        NOT NULL,
  ltp              numeric,
  zone_low         numeric,
  zone_high        numeric,
  dist_pct         numeric,
  state            text        NOT NULL DEFAULT 'waiting',
  inside           boolean     NOT NULL DEFAULT false,
  publication_kind text        NOT NULL DEFAULT 'signal',
  held             boolean     NOT NULL DEFAULT false,
  sl               numeric,
  setup_name       text,
  batch_date       date,
  cycle_n          integer,
  cycle_at         timestamptz NOT NULL DEFAULT now()
);

-- The overwrite target. Upsert resolves on this, which is what makes the table
-- current-state rather than a log.
ALTER TABLE public.atlas_live_zones
  ADD CONSTRAINT atlas_live_zones_symbol_direction_key UNIQUE (symbol, direction);

-- The page's only ordering, and the only index it needs. cycle_at is included so
-- the freshness read -- max(cycle_at) -- does not scan the table.
CREATE INDEX IF NOT EXISTS atlas_live_zones_dist_idx
  ON public.atlas_live_zones (dist_pct ASC NULLS LAST);
CREATE INDEX IF NOT EXISTS atlas_live_zones_cycle_at_idx
  ON public.atlas_live_zones (cycle_at DESC);

ALTER TABLE public.atlas_live_zones ENABLE ROW LEVEL SECURITY;

-- Roles: anon AND authenticated, for the same reason live_prices needed both --
-- signals.html swaps Authorization to the user's access_token once signed in, so
-- a policy scoped to anon alone breaks the page for exactly the operator who is
-- signed in. Read-only; writes stay with service_role, which bypasses RLS.
CREATE POLICY "anon read atlas_live_zones"
  ON public.atlas_live_zones
  FOR SELECT
  TO anon, authenticated
  USING (true);

COMMENT ON TABLE public.atlas_live_zones IS
  'Current state of every symbol the market-hours loop is watching, overwritten each 60s cycle. One row per (symbol, direction); never appended, no history, bounded at 2x the universe. Written by atlas/signal/market_open.py AFTER the entry decisions, so a failure here cannot delay an order, and deliberately NOT routed through atlas/risk/breaker -- a reporting write that cannot reach Supabase must not count toward a trading halt. Read by signals.html during market hours only.';

COMMENT ON COLUMN public.atlas_live_zones.state IS
  'at_zone = price is inside the band, the entry condition. waiting = has not reached the band from the correct side. mitigated = price passed THROUGH the band, so the setup failed rather than cheapened and will not be entered today. Written from market_open.at_zone(), the single place that decides side-of-price.';

COMMENT ON COLUMN public.atlas_live_zones.dist_pct IS
  'Unsigned distance from the live price to the nearest band edge, percent; 0 when inside. Unsigned so that ORDER BY dist_pct ASC puts the nearest first for longs and shorts alike -- the direction lives in `state`, not in the sign.';

COMMENT ON COLUMN public.atlas_live_zones.publication_kind IS
  'signal = ATLAS-actionable, it can be entered this session. candidate = watchlist-only; a valid unmitigated zone that did not qualify, shown so the page describes what the engine watches rather than only what it may trade. market_open filters entries to signal.';

COMMENT ON COLUMN public.atlas_live_zones.cycle_at IS
  'UTC instant the cycle wrote this row. The page computes age as Date.now() - new Date(cycle_at) so no timezone enters the arithmetic; only wall-clock DISPLAY formats to Asia/Kolkata. Written UTC-aware deliberately -- this repo has rendered an IST date as the previous day before, and an age computed through a local-time string is how that returns.';

COMMENT ON COLUMN public.atlas_live_zones.held IS
  'ATLAS holds or has committed a position in this symbol and direction today. Free to record: committed_today() already builds this set every cycle for the duplicate-entry guard.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. Every column must come back true. Apply one statement at a time;
-- "Success" from the SQL editor is not evidence -- see migrations/README.md.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 1 FROM pg_tables
     WHERE schemaname='public' AND tablename='atlas_live_zones')          AS table_exists,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='atlas_live_zones_symbol_direction_key')              AS upsert_key,
  (SELECT count(*) = 1 FROM pg_indexes
     WHERE schemaname='public' AND indexname='atlas_live_zones_dist_idx') AS dist_index,
  (SELECT count(*) = 1 FROM pg_indexes
     WHERE schemaname='public' AND indexname='atlas_live_zones_cycle_at_idx') AS cycle_index,
  (SELECT relrowsecurity FROM pg_class
     WHERE oid='public.atlas_live_zones'::regclass)                      AS rls_on,
  (SELECT count(*) = 1 FROM pg_policies
     WHERE schemaname='public' AND tablename='atlas_live_zones'
       AND policyname='anon read atlas_live_zones')                      AS read_policy,
  (SELECT 'anon' = ANY(roles) AND 'authenticated' = ANY(roles) FROM pg_policies
     WHERE schemaname='public' AND tablename='atlas_live_zones'
       AND policyname='anon read atlas_live_zones')                      AS both_roles,
  (SELECT obj_description('public.atlas_live_zones'::regclass) IS NOT NULL) AS table_comment;
