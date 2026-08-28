-- Backtest storage. The test/live seam.
--
-- A backtest run must never write into the live record. Until now a test run
-- wrote into the same signals / signal_marks / signal_outcomes / atlas_trades
-- rows the dashboard reads, which for a nightly batch was a nuisance and for a
-- continuous engine under development is a hazard. These two tables are the
-- only place backtest output goes, and backtest/store.py cannot address any
-- table whose name does not begin with backtest_.
--
-- PER-SIGNAL, NOT JUST AGGREGATES. Every run persists every candidate it
-- produced, not a summary row. A surprising aggregate has to be drillable to
-- the signals that drove it, or it cannot be trusted -- and keeping only the
-- runs that looked interesting is selection on noise.
--
-- RLS: the database auto-enables RLS on new public tables and creates no
-- policy, which is right here. Backtest output is not dashboard data and has
-- no anon reader. The engine reaches it with the service key, which bypasses
-- RLS; no policy is added deliberately.

CREATE TABLE public.backtest_runs (
  run_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at     timestamptz NOT NULL DEFAULT now(),
  git_sha        text        NOT NULL,
  label          text,
  config         jsonb       NOT NULL,
  config_hash    text        NOT NULL,
  -- Period actually replayed, and where the held-out split falls.
  period_start   date,
  period_end     date,
  holdout_start  date,
  -- Rolling-window end this run reports against, so a windowed figure is
  -- reproducible. The dashboard's window moves daily; a baseline pinned to a
  -- date does not.
  window_end     date,
  status         text        NOT NULL DEFAULT 'RUNNING',
  n_candidates   integer,
  n_resolved     integer,
  runtime_sec    numeric,
  notes          text
);

COMMENT ON TABLE public.backtest_runs IS
  'One row per backtest invocation, kept whatever the outcome. config_hash identifies the settings; git_sha identifies the code that ran them.';

CREATE TABLE public.backtest_signals (
  run_id           uuid    NOT NULL REFERENCES public.backtest_runs(run_id) ON DELETE CASCADE,
  signal_date      date    NOT NULL,
  symbol           text    NOT NULL,
  direction        text    NOT NULL,
  setup_name       text,
  entry_ref        numeric,
  sl               numeric,
  target_1         numeric,
  qty              integer,
  notional         numeric,
  -- Resolution comes from mark_signals.resolve_signal, imported unchanged, so
  -- these are directly comparable with the live signal_marks columns.
  resolution       text,
  resolved_pnl_pct numeric,
  resolved_on      date,
  pnl_inr          numeric,
  -- Both reporting bases are recorded rather than chosen: all-publications and
  -- first-signal-only answer different questions and the report labels both.
  is_first_signal  boolean,
  period           text,          -- 'tune' | 'holdout'
  PRIMARY KEY (run_id, signal_date, symbol, direction)
);

COMMENT ON TABLE public.backtest_signals IS
  'Every candidate a run produced, with its resolution. Per-signal by design: an aggregate you cannot drill into is an aggregate you cannot trust.';

CREATE INDEX backtest_signals_run_idx        ON public.backtest_signals (run_id);
CREATE INDEX backtest_signals_resolution_idx ON public.backtest_signals (run_id, resolution);
CREATE INDEX backtest_runs_hash_idx          ON public.backtest_runs (config_hash, created_at DESC);
