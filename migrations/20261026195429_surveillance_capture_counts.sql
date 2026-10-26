-- ════════════════════════════════════════════════════════════════════
-- surveillance_captures: rows NSE SENT and rows we STORED, as two numbers.
-- ════════════════════════════════════════════════════════════════════
--
-- WHY. NSE's 26-Oct-2026 long-term ASM list carries AAATECH twice (srno 3 and
-- 4): same ISIN, same survCode, differing only in company-name casing, which is
-- not stored. 266 rows arrive; 265 distinct listings exist. A single count
-- either reports NSE's 266 against 265 stored rows -- which reads as one row
-- lost -- or reports 265 and hides that NSE sent a duplicate. Both are facts,
-- so both are kept:
--
--   rows_received  rows in NSE's payload, duplicates included
--   rows_stored    distinct (symbol, framework, stage) listings in this
--                  capture, every one READ BACK from surveillance_snapshots
--                  after the insert. On the second and third run of a session
--                  nothing new is inserted, and rows_stored still counts the
--                  listings confirmed present.
--
-- asm_rows / gsm_rows keep their columns and are now DEFINED as the stored,
-- distinct count per framework, so for an ok capture
-- rows_stored = asm_rows + gsm_rows, and the CHECK below holds every ok row to
-- that. The raw payload with NSE's own count stays on the box under
-- data/reference/surveillance/raw/.
--
-- Applied while both tables were empty (verified 2026-10-26 19:53 UTC), so
-- the new CHECK constrains every row this table will ever hold.
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE public.surveillance_captures
  ADD COLUMN rows_received integer,
  ADD COLUMN rows_stored   integer;

ALTER TABLE public.surveillance_captures
  ADD CONSTRAINT surveillance_captures_ok_counts CHECK (
    NOT ok OR (
      rows_received IS NOT NULL AND rows_stored IS NOT NULL
      AND rows_stored <= rows_received
      AND rows_stored = asm_rows + gsm_rows));

COMMENT ON COLUMN public.surveillance_captures.rows_received IS
  'Rows in NSE''s payload for this capture, duplicates included.';
COMMENT ON COLUMN public.surveillance_captures.rows_stored IS
  'Distinct (symbol, framework, stage) listings in this capture, each read back from surveillance_snapshots after the insert.';
COMMENT ON COLUMN public.surveillance_captures.asm_rows IS
  'Distinct ASM listings stored (not NSE''s raw ASM row count -- see rows_received).';
COMMENT ON COLUMN public.surveillance_captures.gsm_rows IS
  'Distinct GSM listings stored (not NSE''s raw GSM row count -- see rows_received).';
