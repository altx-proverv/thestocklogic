-- atlas.html swaps Authorization to the operator's access_token once signed in
-- (hdr(), atlas.html), so its reads arrive as `authenticated`, not `anon`.
-- "anon read signal_marks" was scoped to anon alone, so the close-price
-- fallback returned [] for exactly the signed-in operator recording exits --
-- the same 200-and-empty failure as live_prices, one role over.
--
-- Extends the existing policy rather than adding a second one; atlas_trades
-- already carries two overlapping SELECT policies from this same pattern.
-- No new exposure: anon can already read this table.
ALTER POLICY "anon read signal_marks"
  ON public.signal_marks
  TO anon, authenticated;