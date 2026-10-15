-- atlas_trades: whether the protection is LIVE AT THE BROKER, recorded
-- separately from what it was meant to be.
--
-- WHY. On 2026-10-15 row 22 (ATHERENERG) had target_price NULL, because the
-- OCO placement failed before anything was written. The record could not say
-- what the target had been, let alone whether it existed. Two different facts
-- had been collapsed into one nullable column:
--
--     what ATLAS intended      -> stop_price, target_price
--     what the exchange holds  -> stop_confirmed, target_confirmed
--
-- The intended levels are now always written, whether or not placement
-- succeeded. These two booleans carry the other half.
--
-- CONFIRMED MEANS READ BACK from orders() and get_gtts(), never from what
-- ATLAS recorded attempting -- see atlas/execution/protection.py. Four
-- mechanisms reported placing a stop on 2026-10-15 and all four orders were
-- rejected; a column written from intent would have said true for every one.
--
-- NULL IS NOT FALSE. A NULL means nobody has asked the broker about this row
-- (every row written before this migration). False means the broker was asked
-- and said no. Keeping them distinct is why there is no DEFAULT.
--
-- APPLY ONE STATEMENT AT A TIME. The Supabase editor has twice applied only a
-- prefix of a pasted file and reported success. The catalogue query at the end
-- is the verification; read it rather than the editor's message.

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS stop_confirmed boolean;

COMMENT ON COLUMN public.atlas_trades.stop_confirmed IS
  'Was a stop CONFIRMED resting at the broker for this position -- read back from orders()/get_gtts(), never from what ATLAS recorded attempting. NULL = never checked (rows predating 2026-10-16). false = the broker was readable and there was nothing there.';

ALTER TABLE public.atlas_trades
  ADD COLUMN IF NOT EXISTS target_confirmed boolean;

COMMENT ON COLUMN public.atlas_trades.target_confirmed IS
  'Was the target leg CONFIRMED live at the broker. Distinct from target_price, which is now always written with the INTENDED 2R level whether or not placement succeeded.';

-- VERIFY. Expect two rows, both ok = true.
SELECT c.column_name,
       count(*)     AS found,
       count(*) = 1 AS ok,
       string_agg(c.data_type, '') AS detail
  FROM information_schema.columns c
 WHERE c.table_schema = 'public'
   AND c.table_name = 'atlas_trades'
   AND c.column_name IN ('stop_confirmed', 'target_confirmed')
 GROUP BY c.column_name
 ORDER BY c.column_name;
