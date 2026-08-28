"""
BACKTEST — the fixed symbol sample
==================================
A correct as-of replay costs 1875 ms per symbol per date, so the full universe
over 17 months is ~97 hours per config. Cutting SESSIONS would have been the
easier saving and is the wrong one: it thins the held-out month, which is the
only thing separating a real effect from a coincidence. Cutting SYMBOLS costs
much less, because zone detection behaves the same on 100 stocks as on 539 --
it is per-symbol structure, not a cross-sectional property.

THE SAMPLE MUST BE THE SAME POPULATION FOR EVERY CONFIG.
Comparing config A on one 100-stock draw against config B on another measures
the draw, not the config. So the sample is:

  * drawn once from a fixed seed,
  * stored in the run config and therefore in backtest_runs.config, and
  * reproducible from the seed alone -- sorted symbol list in, same 100 out,
    regardless of filesystem ordering.

WHAT THE SAMPLE CANNOT ANSWER. Absolute signal COUNTS scale with universe
size: 100 of 539 symbols produces roughly a fifth of the candidates, and the
per-day publication cap (TOP_N_LONG / TOP_N_SHORT in 03b) bites differently on
a smaller pool. Rates -- hit rate, mean R, the STOP/TARGET split -- are
comparable across configs on the same sample; counts are not comparable to the
live record. A config that looks good here is re-run once on the full universe
before it is acted on. That confirmation run is the point of the sample, not
an afterthought.
"""

import hashlib
import logging
from pathlib import Path

log = logging.getLogger("BACKTEST-SAMPLE")

DEFAULT_SEED = 20260829
DEFAULT_SIZE = 100

ROOT = Path(__file__).resolve().parent.parent
STOCKS_DIR = ROOT / "data/processed/stocks"


def available_symbols(stocks_dir: Path = None) -> list:
    """Every symbol with a price parquet, sorted. Sorted matters: it is the
    input to the draw, and glob order is filesystem-dependent."""
    stocks_dir = stocks_dir or STOCKS_DIR
    return sorted(p.stem for p in stocks_dir.glob("*.parquet"))


def draw(seed: int = DEFAULT_SEED, size: int = DEFAULT_SIZE,
         population: list = None) -> list:
    """
    Deterministic sample of `size` symbols.

    Uses a SHA-256 keyed sort rather than random.sample: the result depends
    only on (seed, symbol), so adding or removing an unrelated symbol from the
    universe does not reshuffle the whole draw the way a sequential RNG would.
    A universe that gains a stock next month keeps the sample it already had,
    minus nothing, plus possibly the new one -- which keeps old runs
    comparable with new ones instead of silently changing the population.
    """
    pop = sorted(population if population is not None else available_symbols())
    if not pop:
        raise RuntimeError("no symbols available to sample")
    if size >= len(pop):
        log.warning(f"sample size {size} >= universe {len(pop)} — using all")
        return pop

    def key(sym: str) -> str:
        return hashlib.sha256(f"{seed}:{sym}".encode()).hexdigest()

    return sorted(sorted(pop, key=key)[:size])


def fingerprint(symbols: list) -> str:
    """Short hash of the exact member list, stored beside the seed so a run
    can be checked against the population it actually used rather than the one
    the seed would produce today."""
    return hashlib.sha256("|".join(sorted(symbols)).encode()).hexdigest()[:16]


def describe(symbols: list, population: list = None) -> str:
    pop = population if population is not None else available_symbols()
    return (f"{len(symbols)} of {len(pop)} symbols "
            f"(fingerprint {fingerprint(symbols)})")
