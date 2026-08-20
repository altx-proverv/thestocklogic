-- live_prices had RLS enabled and ZERO policies since it was created, so every
-- frontend read (atlas.html, signals.html) got 200 + [] -- indistinguishable
-- from an empty table. The feed was healthy the whole time: 500 symbols,
-- written twice daily by engine/upstox_ws.py push_live_prices since 2026-06-03.
--
-- Roles: anon AND authenticated. atlas.html swaps Authorization to the user's
-- access_token once signed in (hdr(), atlas.html), so a policy scoped to anon
-- alone would break the page for exactly the operator who is recording exits.
-- atlas_trades already covers both roles for this reason.
--
-- Read-only. Writes stay with service_role, which bypasses RLS.
CREATE POLICY "anon read live_prices"
  ON public.live_prices
  FOR SELECT
  TO anon, authenticated
  USING (true);