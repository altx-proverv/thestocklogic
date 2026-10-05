-- A KNOWN PROPERTY OF THE INSTRUMENT, recorded so no analysis mistakes it for a
-- finding.
--
-- APPLY AFTER PENDING_learning_ledger.sql.
--
-- Registered as status='closed' with no e-process, because it is not a hypothesis
-- about the market at all -- it is arithmetic about the sizing rule, settled in
-- advance and not capable of being refuted by more data. The ledger holds it so that
-- the next person to plot rupee P&L by price band and find a pattern can see it was
-- already explained.

INSERT INTO public.learning_hypotheses
  (slug, question, population, statistic, null_value, side, family,
   basis_version, seed_from, status, registered_by, rationale,
   closed_at, closed_reason)
VALUES
  ('known.size-bias-by-price-band',
   'Does realised position size vary systematically with share price?',
   'every trade sized under fixed-notional-10000-mult1-v1',
   'realised notional as a percentage of the Rs10,000 target, by price band',
   NULL, 'two-sided', 'known-property', 'fixed-notional-10000-mult1-v1',
   '2026-10-06', 'closed', 'sizing-restart-2026-10-06',
   'Quantity is round(10000/price), minimum one share, no price ceiling. Realised '
   'notional is therefore a step function of share price, and two bands are biased '
   'in one direction only. Recorded before the restart rather than discovered '
   'afterwards.',
   '2026-10-06 00:00:00+00',
   'YES, AND IT IS ARITHMETIC, NOT A RESULT. Measured across 1,484 published '
   'signals: under Rs500 lands at 100% of target (523 signals, range 98-102%); '
   'Rs500-2,000 at 100% (651, 91-109%); Rs2,000-4,000 at 101% (150, 88-120%); '
   'Rs4,000-6,666 at 93% (65, 80-131%); Rs6,666-10,000 at 77% (47, 68-99%) and '
   'CANNOT reach target because quantity is 1 and the price is below it; above '
   'Rs10,000 at 142% (48, 101-1293%) and CANNOT fall below it. The worst case is MRF '
   'at Rs129,290 for one share, 1293% of target. 79% of signals sit under Rs2,000 a '
   'share and land within 9% of target, so the effect is confined to 10.8% of the '
   'population. CONSEQUENCE: any rupee-denominated analysis of this record will show '
   'a size effect by price band and it is the sizing rule, not the market. R '
   'multiples are scale-invariant and unaffected, which is why every standing '
   'hypothesis is expressed in R. Risk per trade follows the same shape: Rs205 at '
   'the median for prices under Rs10,000, up to Rs1,148 for a forced single share -- '
   'about 6x, not the 13x the notional ratio suggests, because the expensive names '
   'carry tighter percentage stops.')
ON CONFLICT (slug) DO NOTHING;

SELECT
  (SELECT count(*) = 1 FROM public.learning_hypotheses
     WHERE slug = 'known.size-bias-by-price-band')                    AS recorded,
  (SELECT status = 'closed' FROM public.learning_hypotheses
     WHERE slug = 'known.size-bias-by-price-band')                    AS closed_not_standing,
  (SELECT family = 'known-property' FROM public.learning_hypotheses
     WHERE slug = 'known.size-bias-by-price-band')                    AS marked_as_property,
  (SELECT count(*) = 0 FROM public.learning_results
     WHERE slug = 'known.size-bias-by-price-band')                    AS no_eprocess;
