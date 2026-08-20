-- Daily directional marking of the live signal book.
--
-- A signal published in the last 20 trading days is "live" and is marked EVERY
-- evening, previous close to current close. LONG up = correct today. The unit is
-- today's direction, not the outcome of a trade -- a signal down 15% since its
-- call still counts as correct on any day it rises.
--
-- This deliberately does NOT touch signal_outcomes or the trade record. They
-- measure different things (a trade with entry/stop/target vs. a daily
-- directional call) and mixing them makes both meaningless.
--
-- Marking one day at a time also sidesteps the age confound that makes any
-- mark-to-today aggregate unusable: measured from the signal date, accuracy ran
-- 42.6% at 1 day and 71.6% past 60 days, so a blended figure mostly reports how
-- old the window is. Every daily mark is a one-day move, so all marks are
-- comparable regardless of the signal's age. Older signals still contribute MORE
-- marks than recent ones, which the page states.

CREATE TABLE IF NOT EXISTS public.signal_marks (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  signal_date    date    NOT NULL,
  symbol         text    NOT NULL,
  direction      text    NOT NULL,
  mark_date      date    NOT NULL,
  ref_close      numeric,          -- close ON the signal date
  prev_close     numeric,
  mark_close     numeric,
  daily_move_pct numeric,          -- directional: signed so SHORT down is positive
  cum_move_pct   numeric,          -- directional, since ref_close
  correct_today  boolean,          -- NULL when the day's move is exactly zero
  setup_name     text,
  age_days       integer,          -- trading days since signal_date; always >= 1
  created_at     timestamptz DEFAULT now(),
  CONSTRAINT signal_marks_natural_key
    UNIQUE (signal_date, symbol, direction, mark_date)
);

CREATE INDEX IF NOT EXISTS signal_marks_mark_date_idx ON public.signal_marks (mark_date DESC);
ALTER TABLE public.signal_marks ENABLE ROW LEVEL SECURITY;

-- Benchmark. Kept in its own table rather than denormalised onto every mark row.
CREATE TABLE IF NOT EXISTS public.market_marks (
  mark_date      date PRIMARY KEY,
  nifty_close    numeric,
  nifty_move_pct numeric,
  created_at     timestamptz DEFAULT now()
);
ALTER TABLE public.market_marks ENABLE ROW LEVEL SECURITY;

-- ── VIEWS ───────────────────────────────────────────────────────────
-- The book is ~441 signals, so a 20-day chart is ~8,800 rows and PostgREST caps
-- a response at 1000. The page therefore reads aggregates computed in Postgres,
-- never raw marks for more than the current day.
--
-- correct_today IS NULL (an exactly-flat day) is excluded from the denominator
-- rather than counted as wrong -- the same rule as a zero-elapsed signal.

CREATE OR REPLACE VIEW public.v_marks_daily AS
SELECT m.mark_date,
       count(*)                                                        AS n_marks,
       count(*) FILTER (WHERE m.correct_today)                         AS n_correct,
       count(*) FILTER (WHERE m.correct_today IS NOT NULL)             AS n_scored,
       round(100.0 * count(*) FILTER (WHERE m.correct_today)
             / NULLIF(count(*) FILTER (WHERE m.correct_today IS NOT NULL), 0), 1) AS accuracy_pct,
       round(avg(m.daily_move_pct)::numeric, 3)                        AS mean_daily_move,
       round(avg(m.cum_move_pct)::numeric, 3)                          AS mean_cum_move,
       count(*) FILTER (WHERE m.direction = 'LONG')                    AS long_n,
       round(100.0 * count(*) FILTER (WHERE m.direction = 'LONG' AND m.correct_today)
             / NULLIF(count(*) FILTER (WHERE m.direction = 'LONG'
                                        AND m.correct_today IS NOT NULL), 0), 1) AS long_accuracy_pct,
       count(*) FILTER (WHERE m.direction = 'SHORT')                   AS short_n,
       round(100.0 * count(*) FILTER (WHERE m.direction = 'SHORT' AND m.correct_today)
             / NULLIF(count(*) FILTER (WHERE m.direction = 'SHORT'
                                        AND m.correct_today IS NOT NULL), 0), 1) AS short_accuracy_pct,
       count(DISTINCT m.signal_date)                                   AS signal_dates_in_window,
       round((count(*)::numeric / NULLIF(count(DISTINCT m.signal_date), 0)), 1) AS signals_per_day,
       round(sum(m.cum_move_pct / 100.0 * 100000)::numeric, 0)         AS notional_pnl,
       mk.nifty_move_pct
  FROM public.signal_marks m
  LEFT JOIN public.market_marks mk ON mk.mark_date = m.mark_date
 GROUP BY m.mark_date, mk.nifty_move_pct
 ORDER BY m.mark_date DESC;

CREATE OR REPLACE VIEW public.v_marks_by_setup_today AS
SELECT setup_name,
       count(*)                                            AS n,
       round(100.0 * count(*) FILTER (WHERE correct_today)
             / NULLIF(count(*) FILTER (WHERE correct_today IS NOT NULL), 0), 1) AS accuracy_pct,
       round(avg(daily_move_pct)::numeric, 3)              AS mean_daily_move,
       round(avg(cum_move_pct)::numeric, 3)                AS mean_cum_move
  FROM public.signal_marks
 WHERE mark_date = (SELECT max(mark_date) FROM public.signal_marks)
 GROUP BY setup_name
 ORDER BY n DESC;

CREATE OR REPLACE VIEW public.v_open_book AS
SELECT mark_date, signal_date, symbol, direction, setup_name, age_days,
       ref_close, mark_close, daily_move_pct, cum_move_pct, correct_today
  FROM public.signal_marks
 WHERE mark_date = (SELECT max(mark_date) FROM public.signal_marks);

COMMENT ON TABLE public.signal_marks IS
  'One row per live signal per trading day. Directional accuracy only -- no entries, exits, stops or targets. Separate from signal_outcomes by design.';
COMMENT ON COLUMN public.signal_marks.correct_today IS
  'LONG up / SHORT down on the day. NULL when the move is exactly zero -- excluded from accuracy rather than counted wrong.';