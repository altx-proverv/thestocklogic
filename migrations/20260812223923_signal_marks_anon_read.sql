-- The dashboard reads with the anon key. RLS is enabled on both new tables, and
-- without a policy anon gets an empty result rather than an error -- which is
-- exactly the silent-empty-page failure this rebuild exists to fix.
-- Mirrors the existing "anon read outcomes" policy on signal_outcomes.
--
-- Read-only. Writes stay with the service role used by engine/mark_signals.py.

CREATE POLICY "anon read signal_marks" ON public.signal_marks
  FOR SELECT TO anon USING (true);

CREATE POLICY "anon read market_marks" ON public.market_marks
  FOR SELECT TO anon USING (true);

-- Views are queried through PostgREST as the invoking role, so grant explicitly.
GRANT SELECT ON public.v_marks_daily          TO anon;
GRANT SELECT ON public.v_marks_by_setup_today TO anon;
GRANT SELECT ON public.v_open_book            TO anon;
GRANT SELECT ON public.signal_marks           TO anon;
GRANT SELECT ON public.market_marks           TO anon;