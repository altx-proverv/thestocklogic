-- The 52-week LOW, the missing half of a pair.
--
-- daily_closes carried high_52w, high_20d and low_20d and no 52-week low, so
-- the Losers side of the /home filters had a chip with no detector behind it.
-- Rather than label a 20-day low as a 52-week one, the chip was absent.
--
-- publish_closes fills this from the SAME window high_52w uses: the last 252
-- sessions of data/processed/stocks/<SYM>.parquet up to and including the
-- session. 252 was not guessed -- it is the only tail length that reproduces
-- the stored high_52w on all 40 symbols sampled (tail(250) missed one,
-- tail(240) missed six, a 365-calendar-day window missed three).
ALTER TABLE public.daily_closes ADD COLUMN IF NOT EXISTS low_52w numeric;

COMMENT ON COLUMN public.daily_closes.low_52w IS
  'Lowest low over the trailing 252 sessions including this one. Same window as high_52w. Written by engine/publish_closes.py.';
