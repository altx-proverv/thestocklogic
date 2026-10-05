"""
THE STOCK LOGIC — anytime-valid statistics for the learning loop.

WHY NOT p-VALUES. The loop re-tests every standing hypothesis after every chain.
A fixed-alpha p-value recomputed on accumulating data is not a test under that
regime -- it is a search for a night on which the data happens to look extreme.
Simulated: null true, one hypothesis, four new resolved signals a night, 250
nights.

    fixed alpha = 0.05, re-tested nightly    crossed in 36.8% of runs
    fixed alpha = 0.01, re-tested nightly    crossed in 10.1%
    e-process,  alpha = 0.05                 crossed in  3.2%   (bound: 5%)
    e-process,  alpha = 0.01                 crossed in  1.1%   (bound: 1%)

A 37% false-positive rate per hypothesis per year, across ~18 standing
hypotheses, on a pipeline whose output is a proposal the operator is asked to
approve. That is the failure this module exists to prevent.

WHAT AN E-VALUE IS, briefly. A non-negative statistic with expectation at most 1
under the null. Its running product over a stream is a test martingale, and
Ville's inequality bounds P(sup_t E_t >= 1/alpha) <= alpha -- so "reject when
E >= 1/alpha" is valid AT ANY STOPPING TIME, including a stopping time chosen by
looking. That is exactly the licence nightly re-testing needs, and it is also why
evidence ACCUMULATES: a hypothesis unconvincing at n=300 can cross at n=2,000
because the product carries forward rather than being recomputed.

THREE RULES THAT MAKE IT VALID, and all three are easy to lose:

  DELTA-ONLY. The product may only multiply in observations it has not already
  seen. Re-running over "everything accumulated" each night re-uses rows and the
  martingale property dies.

  PREDICTABLE BETS. lambda_t may depend only on observations before t. Choosing it
  from the current observation is how an e-process becomes a lie.

  ONE BASIS. Likelihood ratios computed under different definitions of the data
  cannot be multiplied together. A change of measurement basis ends the lineage;
  it does not continue it.

Nothing here needs scipy.
"""

import math

# ══════════════════════════════════════════════════════════════════
# E-PROCESS FOR A BERNOULLI MEAN
# ══════════════════════════════════════════════════════════════════


def _lambda_cap(p0: float) -> float:
    """The largest bet that keeps every factor strictly positive.

    A factor is 1 + lam*(x - p0) with x in {0,1}, so it needs
    1 + lam*(1-p0) > 0 and 1 - lam*p0 > 0. Half of the binding limit is used,
    which keeps the process numerically stable and costs little power.
    """
    return 0.5 * min(1.0 / max(p0, 1e-9), 1.0 / max(1.0 - p0, 1e-9))


def bernoulli_log_e(xs, p0: float, side: str = "greater",
                    state: dict = None) -> dict:
    """Extend a Bernoulli e-process by the observations in `xs`.

    xs      new observations only, 0/1, in arrival order
    p0      the null rate
    side    'greater' | 'less' | 'two-sided'
    state   {'log_e','n','s'} from the previous run, or None to start

    -> {'log_e', 'n', 's', 'e', 'log_e_up', 'log_e_dn'}

    ONE-SIDED BY DEFAULT because the questions are one-sided: "does this stratum
    clear breakeven" is not "is it different from breakeven". Two-sided is the
    MEAN of the two one-sided e-values -- a mixture, which is itself an e-value.
    The maximum is not, and using it would quietly double the error rate.

    The bet is a shrunk plug-in: lambda_t from the running mean through t-1 only.
    Before five observations it bets nothing, so a run of three heads cannot
    launch the process.
    """
    st = dict(state or {})
    log_up = float(st.get("log_e_up", 0.0))
    log_dn = float(st.get("log_e_dn", 0.0))
    n = int(st.get("n", 0))
    s = int(st.get("s", 0))
    cap = _lambda_cap(p0)
    var0 = max(p0 * (1.0 - p0), 1e-9)

    for x in xs:
        x = 1.0 if x else 0.0
        if n >= 5:
            edge = (s / n) - p0                      # predictable: through t-1
            lam = max(-cap, min(cap, edge / var0))
        else:
            lam = 0.0
        # 'greater' bets on x > p0; 'less' is the mirror.
        log_up += math.log(max(1e-300, 1.0 + max(0.0, lam) * (x - p0)))
        log_dn += math.log(max(1e-300, 1.0 + max(0.0, -lam) * (p0 - x)))
        n += 1
        s += int(x)

    if side == "greater":
        log_e = log_up
    elif side == "less":
        log_e = log_dn
    else:
        # mixture of the two one-sided e-values: (E_up + E_dn)/2
        m = max(log_up, log_dn)
        log_e = m + math.log(0.5 * (math.exp(log_up - m) + math.exp(log_dn - m)))
    return {"log_e": log_e, "log_e_up": log_up, "log_e_dn": log_dn,
            "n": n, "s": s,
            "e": math.exp(min(700.0, log_e))}


def crossed(log_e: float, alpha: float = 0.05) -> bool:
    """Ville: reject when E >= 1/alpha, at any stopping time."""
    return log_e >= math.log(1.0 / alpha)


# ══════════════════════════════════════════════════════════════════
# CONFIDENCE SEQUENCE FOR A BOUNDED MEAN
# ══════════════════════════════════════════════════════════════════


def mean_cs(xs, lo: float, hi: float, alpha: float = 0.05,
            state: dict = None, t_target: int = 500) -> dict:
    """Time-uniform confidence interval for the mean of a bounded stream.

    Normal-mixture (Howard et al.) boundary on the centred sum, with the
    sub-Gaussian variance proxy a bounded variable always admits, sigma =
    (hi-lo)/2. Valid SIMULTANEOUSLY at every t, so it may be read after every
    update without penalty -- which is the same licence the e-process has and for
    the same reason.

    Used where the quantity is not a rate: expectancy in R, and the paired
    stratum differences the membership test produces.

    t_target only tunes where the interval is tightest. It does not affect
    validity.
    """
    st = dict(state or {})
    n = int(st.get("n", 0))
    tot = float(st.get("sum", 0.0))
    for x in xs:
        tot += float(x)
        n += 1
    if n == 0:
        return {"n": 0, "sum": 0.0, "mean": None, "lo": None, "hi": None,
                "width": None}
    sigma2 = ((hi - lo) / 2.0) ** 2
    rho = max(1e-9, sigma2 * max(1, t_target))
    v = n * sigma2
    # boundary on the SUM; divide by n for the mean
    b = math.sqrt(2.0 * (v + rho) * math.log(math.sqrt((v + rho) / rho) / alpha + 1.0))
    mean = tot / n
    w = b / n
    return {"n": n, "sum": tot, "mean": mean,
            "lo": max(lo, mean - w), "hi": min(hi, mean + w), "width": w}


# ══════════════════════════════════════════════════════════════════
# e-BH — ONE FDR BUDGET ACROSS THE STANDING SET
# ══════════════════════════════════════════════════════════════════


def ebh(evalues: dict, alpha: float = 0.05) -> dict:
    """Which hypotheses are rejected, controlling FDR at alpha.

    Wang & Ramdas: sort e-values decreasing, find the largest k with
    e_(k) >= K/(alpha*k), reject that top k. Composes with anytime-validity --
    the e-values may each have been collected under optional stopping, which a
    p-value-based BH could not tolerate.

    -> {'rejected': [slug...], 'k': int, 'threshold': float, 'ranked': [...]}
    """
    items = sorted(((s, e) for s, e in evalues.items()),
                   key=lambda kv: -kv[1])
    K = len(items)
    if K == 0:
        return {"rejected": [], "k": 0, "threshold": float("inf"), "ranked": []}
    k = 0
    for i, (_s, e) in enumerate(items, start=1):
        if e >= K / (alpha * i):
            k = i
    return {"rejected": [s for s, _ in items[:k]], "k": k,
            "threshold": (K / (alpha * k)) if k else float("inf"),
            "ranked": items}


# ══════════════════════════════════════════════════════════════════
# WILSON — for REPORTING only, never for deciding
# ══════════════════════════════════════════════════════════════════


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """(point, lo, hi). A fixed-sample interval, shown beside a finding so a
    reader has a familiar number. It is NOT what decides anything: read after a
    nightly look it has no coverage guarantee, which is the whole reason the
    e-process exists."""
    if n <= 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, c - h), min(1.0, c + h))
