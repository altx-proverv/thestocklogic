-- THE FINDINGS LEDGER. Every hypothesis ever tested, kept forever.
--
-- WHY IT EXISTS. Without it each analysis starts from nothing and re-tests the same
-- things blind: the 12-feature null was established on 2026-09-30 and nothing in the
-- system records that, so the next analysis would have found the same nothing and
-- called it new. And a hypothesis rejected at n=300 is worth re-running at n=2,000,
-- which is only possible if the rejection was written down with its sample size.
--
-- WHY E-VALUES AND NOT p-VALUES. The loop re-tests after every chain. A fixed-alpha
-- p-value recomputed on accumulating data is not a test under that regime --
-- simulated at four new resolved signals a night over 250 nights, with the null
-- true, a p<0.05 rule crosses in 36.8% of runs. Across eighteen standing hypotheses
-- that is a near-certain false finding every year, on a pipeline whose output is a
-- proposal an operator is asked to approve.
--
-- An e-value is non-negative with expectation at most 1 under the null; its running
-- product is a test martingale, and Ville's inequality bounds
-- P(sup_t E_t >= 1/alpha) <= alpha. So "reject when E >= 1/alpha" is valid AT ANY
-- STOPPING TIME, including one chosen by looking. Measured on the implementation in
-- engine/learning.py: 2.05% at alpha=0.05 against a 5% bound.
--
-- THE THREE INVARIANTS THIS SCHEMA ENFORCES, because all three are easy to lose:
--
--   DELTA-ONLY        learning_results.n_through records how far the product has
--                     consumed. The next run starts there. Re-running over
--                     everything accumulated would multiply the same rows in twice
--                     and the martingale property would be gone with no symptom.
--
--   ONE BASIS         basis_version. Likelihood ratios computed under different
--                     definitions of the data cannot be multiplied together. The
--                     measurement basis changed on 2026-10-03 when 293 verdicts
--                     were re-resolved from entry_ref to the actual fill; an
--                     e-process spanning that is arithmetic without meaning.
--                     Changing it ENDS a lineage and starts a new one.
--
--   PRE-REGISTRATION  registered_at, and seed_from. Observations count only from
--                     seed_from forward. A hypothesis registered after seeing the
--                     data it would explain is not a test, and the ledger would
--                     become a record of rationalisations rather than of findings.

-- ─────────────────────────────────────────────────────────────────────
-- 1. THE REGISTRY
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.learning_hypotheses (
  slug           text        PRIMARY KEY,
  question       text        NOT NULL,
  population     text        NOT NULL,
  statistic      text        NOT NULL,
  null_value     numeric,
  side           text        NOT NULL DEFAULT 'greater',
  family         text        NOT NULL,
  -- The definition of the data this lineage is computed under. A change starts a
  -- NEW lineage; the old results stay, frozen and labelled.
  basis_version  text        NOT NULL,
  -- Observations count from here forward. NOT the earliest available row.
  seed_from      date        NOT NULL,
  status         text        NOT NULL DEFAULT 'standing',
  registered_at  timestamptz NOT NULL DEFAULT now(),
  registered_by  text        NOT NULL DEFAULT 'engine',
  rationale      text,
  closed_at      timestamptz,
  closed_reason  text
);

ALTER TABLE public.learning_hypotheses
  ADD CONSTRAINT learning_hypotheses_side_chk
  CHECK (side IN ('greater', 'less', 'two-sided')) NOT VALID;
ALTER TABLE public.learning_hypotheses VALIDATE CONSTRAINT learning_hypotheses_side_chk;

ALTER TABLE public.learning_hypotheses
  ADD CONSTRAINT learning_hypotheses_status_chk
  CHECK (status IN ('standing', 'closed', 'promoted', 'superseded')) NOT VALID;
ALTER TABLE public.learning_hypotheses VALIDATE CONSTRAINT learning_hypotheses_status_chk;

-- ─────────────────────────────────────────────────────────────────────
-- 2. EVERY RESULT, EVERY NIGHT, PASS OR FAIL
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.learning_results (
  id             bigserial   PRIMARY KEY,
  slug           text        NOT NULL REFERENCES public.learning_hypotheses(slug),
  run_date       date        NOT NULL,
  basis_version  text        NOT NULL,
  -- how far the product has consumed, and what arrived this run
  n_through      integer     NOT NULL,
  n_new          integer     NOT NULL DEFAULT 0,
  successes      integer,
  -- the evidence. log_e is stored because e overflows a double long before the
  -- evidence stops being interesting.
  log_e          numeric     NOT NULL,
  e_value        numeric,
  crossed        boolean     NOT NULL DEFAULT false,
  -- survived e-BH across the whole standing set, which is the only thing that
  -- licenses a report
  crossed_ebh    boolean     NOT NULL DEFAULT false,
  ebh_threshold  numeric,
  -- reporting only, never deciding: a fixed-sample interval read after a nightly
  -- look has no coverage guarantee
  point_estimate numeric,
  ci_lo          numeric,
  ci_hi          numeric,
  verdict        text,
  engine_sha     text,
  computed_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.learning_results
  ADD CONSTRAINT learning_results_run_key
  UNIQUE (slug, run_date, basis_version);

CREATE INDEX IF NOT EXISTS learning_results_slug_idx
  ON public.learning_results (slug, run_date DESC);
CREATE INDEX IF NOT EXISTS learning_results_crossed_idx
  ON public.learning_results (crossed_ebh, run_date DESC) WHERE crossed_ebh;

-- ─────────────────────────────────────────────────────────────────────
-- 3. PROPOSALS — nothing acts on its own
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.learning_findings (
  id              bigserial   PRIMARY KEY,
  slug            text        NOT NULL REFERENCES public.learning_hypotheses(slug),
  proposed_at     timestamptz NOT NULL DEFAULT now(),
  what_changed    text        NOT NULL,
  evidence        text        NOT NULL,
  n_at_proposal   integer     NOT NULL,
  e_at_proposal   numeric,
  ci_lo           numeric,
  ci_hi           numeric,
  proposed_action text        NOT NULL,
  status          text        NOT NULL DEFAULT 'proposed',
  -- THE GATE. Automatic only after a held-out period AND 200+ live trades, earned
  -- per finding. ATLAS has 19 trades lifetime and is paused, so this is advisory
  -- for the foreseeable future -- which is stated here so the table cannot be
  -- mistaken for one that acts.
  held_out_until  date,
  live_trades_at_approval integer,
  approved_at     timestamptz,
  approved_by     text,
  reverted_at     timestamptz,
  notes           text
);

ALTER TABLE public.learning_findings
  ADD CONSTRAINT learning_findings_status_chk
  CHECK (status IN ('proposed', 'approved', 'live', 'rejected', 'reverted'))
  NOT VALID;
ALTER TABLE public.learning_findings VALIDATE CONSTRAINT learning_findings_status_chk;

CREATE INDEX IF NOT EXISTS learning_findings_status_idx
  ON public.learning_findings (status, proposed_at DESC);

-- ─────────────────────────────────────────────────────────────────────
-- RLS — read-only to the page, writes are service_role
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.learning_hypotheses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.learning_results    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.learning_findings   ENABLE ROW LEVEL SECURITY;

-- anon AND authenticated on all three. The ledger is the honest counterpart to a
-- published accuracy figure: what was tested, and what failed. A record of
-- rejections that only the operator can see is worth less than one anybody can.
CREATE POLICY "read learning_hypotheses" ON public.learning_hypotheses
  FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY "read learning_results" ON public.learning_results
  FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY "read learning_findings" ON public.learning_findings
  FOR SELECT TO anon, authenticated USING (true);

COMMENT ON TABLE public.learning_hypotheses IS
  'Every hypothesis the learning loop tests, pre-registered. seed_from is the date observations start counting -- never the earliest available row, because a hypothesis registered after seeing the data it would explain is not a test. basis_version identifies the definition of the data the lineage is computed under; a change ends the lineage rather than continuing it.';

COMMENT ON TABLE public.learning_results IS
  'One row per hypothesis per nightly run, pass or fail, kept forever. log_e is a running test martingale extended by NEW observations only (n_through records how far it has consumed) -- so evidence accumulates and a hypothesis rejected at n=300 can cross at n=2,000 without the penalty that re-computing a p-value nightly would incur. crossed_ebh is the only field that licenses a report: it means the e-value survived Benjamini-Hochberg across the whole standing set.';

COMMENT ON COLUMN public.learning_results.ci_lo IS
  'Wilson or confidence-sequence bound, for REPORTING. A fixed-sample interval read after a nightly look has no coverage guarantee; nothing is decided on it. crossed_ebh decides.';

-- ─────────────────────────────────────────────────────────────────────
-- VERIFY. All true. One statement at a time.
-- ─────────────────────────────────────────────────────────────────────

SELECT
  (SELECT count(*) = 3 FROM pg_tables WHERE schemaname='public'
     AND tablename IN ('learning_hypotheses','learning_results',
                       'learning_findings'))                          AS tables_exist,
  (SELECT count(*) = 1 FROM pg_constraint
     WHERE conname='learning_results_run_key')                        AS result_key,
  (SELECT count(*) = 3 FROM pg_constraint
     WHERE conname IN ('learning_hypotheses_side_chk',
                       'learning_hypotheses_status_chk',
                       'learning_findings_status_chk')
       AND convalidated)                                              AS checks_valid,
  (SELECT count(*) = 4 FROM pg_indexes WHERE schemaname='public'
     AND indexname IN ('learning_results_slug_idx',
                       'learning_results_crossed_idx',
                       'learning_findings_status_idx',
                       'learning_hypotheses_pkey'))                   AS indexes,
  (SELECT count(*) = 3 FROM pg_class WHERE relrowsecurity
     AND relname IN ('learning_hypotheses','learning_results',
                     'learning_findings'))                            AS rls_on,
  (SELECT count(*) = 3 FROM pg_policies WHERE schemaname='public'
     AND tablename IN ('learning_hypotheses','learning_results',
                       'learning_findings'))                          AS policies,
  (SELECT count(*) = 2 FROM information_schema.columns
     WHERE table_name='learning_hypotheses'
       AND column_name IN ('basis_version','seed_from'))              AS invariant_columns,
  (SELECT count(*) = 2 FROM information_schema.columns
     WHERE table_name='learning_results'
       AND column_name IN ('n_through','log_e'))                      AS delta_columns;
