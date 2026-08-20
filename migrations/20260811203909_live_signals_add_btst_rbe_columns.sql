-- live_signals was shaped around orb_engine, the only writer whose payload ever
-- matched it: all 786 existing rows are session='morning'. btst_engine and
-- rbe_engine were added later sending fields that were never created, so every
-- one of their pushes failed with PGRST204 and neither has ever written a row.
--
-- Types mirror the equivalent columns on public.signals. All nullable, so the
-- 786 existing ORB rows are unaffected.

ALTER TABLE public.live_signals
  ADD COLUMN IF NOT EXISTS delivery_pct numeric,
  ADD COLUMN IF NOT EXISTS entry_low    numeric,
  ADD COLUMN IF NOT EXISTS entry_high   numeric,
  ADD COLUMN IF NOT EXISTS grade        text,
  ADD COLUMN IF NOT EXISTS rsi          numeric,
  ADD COLUMN IF NOT EXISTS score        numeric,
  ADD COLUMN IF NOT EXISTS sl_pct       numeric,
  ADD COLUMN IF NOT EXISTS trade_type   text;

COMMENT ON COLUMN public.live_signals.delivery_pct IS
  'Delivery %% on the signal candle. BTST''s primary filter (>= 45).';
COMMENT ON COLUMN public.live_signals.entry_low IS
  'Lower edge of the entry band.';
COMMENT ON COLUMN public.live_signals.entry_high IS
  'Upper edge of the entry band.';
COMMENT ON COLUMN public.live_signals.grade IS
  'A+ | A | B | C. Score-derived; score is non-predictive, treat as cosmetic.';
COMMENT ON COLUMN public.live_signals.rsi IS
  'RSI(14) on the signal candle.';
COMMENT ON COLUMN public.live_signals.score IS
  'Engine-specific conviction score. Not comparable across signal types.';
COMMENT ON COLUMN public.live_signals.sl_pct IS
  'Distance entry->stop, percent.';
COMMENT ON COLUMN public.live_signals.trade_type IS
  'BTST | RBE | ORB. Redundant with session; retained because both writers send it.';