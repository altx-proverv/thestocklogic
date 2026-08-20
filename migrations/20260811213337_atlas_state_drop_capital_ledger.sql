-- ATLAS no longer tracks capital. These columns were a second copy of a number
-- the broker already knows, and the copies disagreed: atlas_state.capital held
-- 150000 while atlas/config.py held 300000 and atlas.html held 300000. The kill
-- switch derived its daily loss cap from whichever it read, enforcing Rs4,500
-- against a documented Rs9,000.
--
-- Available funds are now read live from kite.margins() at decision time, less
-- the notional of any resting ATLAS GTT triggers (atlas/risk/funds.py), and
-- fail closed when unreadable. The operator manages the money pool.
--
-- No code reads or writes these columns as of this migration: capital_manager
-- is deleted, and kill_switch / daily_report / directives now use mode and P&L
-- only. daily_pnl, weekly_pnl, mode and notes are retained -- they are reporting
-- state, not a capital ledger.

ALTER TABLE public.atlas_state
  DROP COLUMN IF EXISTS capital,
  DROP COLUMN IF EXISTS allocated_capital,
  DROP COLUMN IF EXISTS deployed_capital,
  DROP COLUMN IF EXISTS available_capital,
  DROP COLUMN IF EXISTS total_brokerage;

COMMENT ON TABLE public.atlas_state IS
  'Operator mode and reporting P&L. Deliberately holds NO capital figures -- funds are read live from the broker at decision time. See atlas/risk/funds.py.';