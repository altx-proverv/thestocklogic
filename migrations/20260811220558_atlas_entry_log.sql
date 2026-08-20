-- One row per signal evaluated by market_open, recorded at decision time.
--
-- atlas_trades only ever receives ENTERED / GTT_PENDING / SHADOW rows via
-- _log_intent, so every SKIPPED_* and BLOCKED_* outcome was discarded after
-- being sent to Telegram. The daily report needs those reasons, and
-- re-deriving them at 19:05 would report the state of the gates at 19:05 --
-- different regime, different prices, different funds -- not the decision the
-- agent actually made at 09:37.
--
-- Deliberately separate from atlas_trades: writing skips there would make them
-- count against get_today_entry_count(), which gates MAX_TRADES_PER_DAY.

CREATE TABLE IF NOT EXISTS public.atlas_entry_log (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_date    date        NOT NULL,
  run_at      timestamptz NOT NULL DEFAULT now(),
  symbol      text        NOT NULL,
  direction   text,
  status      text        NOT NULL,
  reason      text,
  qty         integer,
  entry_price numeric,
  stop_price  numeric,
  risk_inr    numeric,
  agent_mode  text
);

CREATE INDEX IF NOT EXISTS atlas_entry_log_run_date_idx
  ON public.atlas_entry_log (run_date DESC);

ALTER TABLE public.atlas_entry_log ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.atlas_entry_log IS
  'Per-signal entry decisions from market_open, recorded at decision time. Includes skips and blocks, which atlas_trades never stores. Read by daily_report.';
COMMENT ON COLUMN public.atlas_entry_log.status IS
  'enter_trade() status: ENTERED | GTT_PLACED | SHADOW_INTENT | SHADOW_GTT | SKIPPED_* | BLOCKED_* | REJECTED_*';