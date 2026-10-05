"""
THE STOCK LOGIC — Stage 3b: SMC Trade Scoring Engine (Vectorized)
=================================================================
Fully vectorized — no iterrows. Runs on 200K+ rows in <60 seconds.
Memory efficient — processes in chunks if needed.

Run: python3 engine/03b_score.py
"""
import os, sys, logging, warnings
from pathlib import Path
import numpy as np
from collections import OrderedDict
import pandas as pd
from collections import Counter
from tqdm import tqdm

# NOT a blanket ignore. This line used to be warnings.filterwarnings("ignore"),
# which suppressed FutureWarning along with everything else -- so pandas spent a
# year telling us that assigning int64 values into an int16 column would stop
# working, and this module silenced it. On 2026-10-02 pandas 3 on the box turned
# that warning into a TypeError, 03b crashed, and nothing published.
#
# Deprecations are the one category that must stay audible: they are the only
# advance notice that a library upgrade will break the pipeline. Numeric noise is
# suppressed by name instead.
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", message="invalid value encountered")
warnings.filterwarnings("ignore", message="divide by zero encountered")
warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", message="All-NaN slice encountered")
warnings.filterwarnings("ignore", message="numpy.ndarray size changed")
Path("reports").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("reports/03b_score.log"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

SMC_DIR     = Path("data/processed/smc")
# Imported, not retyped: reject_sample had its own copy pointing at a
# different directory, and the artifact it needed was written all along.
from engine.paths import SIGNALS_DIR, ALL_ROWS   # noqa: E402
from engine.paths import PLAYBOOKS               # noqa: E402

TOP_N_LONG    = 5
TOP_N_SHORT   = 2
# MAX_LOSS_INR / MIN_SL_PCT / MAX_SL_PCT / MIN_RR / MIN_SCORE removed -- all
# dead since the zone_entry refactor moved sizing and stop geometry out of this
# module, and since the score gate was retired. engine/zone_entry.py owns the
# risk constants now; grep found no remaining readers.


# ══════════════════════════════════════════════════════════════════
# VECTORIZED SCORING
# ══════════════════════════════════════════════════════════════════

def load_sector_bias() -> dict:
    """Load sector bias from sector momentum parquet."""
    sector_file = Path("data/processed/sector_momentum.parquet")
    if not sector_file.exists():
        return {}
    try:
        sec_df = pd.read_parquet(sector_file)
        return dict(zip(sec_df["sector"], sec_df["trade_bias"]))
    except:
        return {}


def load_symbol_sector() -> dict:
    """Load symbol->sector mapping."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from universe import SYMBOL_SECTOR_MAP
        return SYMBOL_SECTOR_MAP
    except:
        return {}


# Every scoring input is read via df.get(col, <default>), which silently
# substitutes a constant when the column is absent or misspelled. That is how
# the macd_positive / macd_hist_positive mismatch survived unnoticed and put a
# standing +2 on every short. Warn once per missing column so the next typo is
# visible in the log instead of quietly biasing the score.
_EXPECTED_SCORING_COLS = (
    "no_trade_zone", "rvol", "atr_pct", "market_regime", "adx_ranging", "adx",
    "weekly_bullish", "weekly_bearish", "recent_bos_choch", "structure_trend",
    "near_demand_ob", "near_supply_ob", "price_in_bull_fvg", "price_in_bear_fvg",
    "bos_bull", "bos_bear", "choch_bull", "choch_bear",
    "bull_liq_sweep", "bear_liq_sweep", "in_discount", "in_premium",
    "vix_close", "ad_ratio", "rsi", "macd_hist_rising", "macd_positive",
    "price_above_ema20", "ema20_above_ema50", "ema50_above_ema200",
    "institutional_buying", "high_delivery", "rs_positive", "pct_from_52w_high",
)
_warned_missing = set()


def _warn_missing_cols(df: pd.DataFrame):
    missing = [c for c in _EXPECTED_SCORING_COLS
               if c not in df.columns and c not in _warned_missing]
    if missing:
        _warned_missing.update(missing)
        log.warning(f"SCORING INPUTS MISSING -- falling back to defaults for: "
                    f"{', '.join(missing)}. Scores are biased until this is fixed.")


GATE_ORDER = (
    "warmup", "no_trade_zone", "very_low_volume", "atr_too_high",
    "bear_regime_no_reversal", "adq_ranging_market",
    "weekly_structure_misaligned", "no_smc_signal", "no_recent_bos_choch",
)
# Bit i in gates_failed_mask is GATE_ORDER[i]. Bit 9 is added later, after
# compute_zone_entries, for a row whose entry geometry is unusable -- that gate
# lives at a different stage and cannot be evaluated here.
GATE_ZONE_ENTRY_BIT = 9

# ONE DTYPE PAIR FOR BOTH BUILD SITES. score_vectorized built these as int16/int32
# and compute_trade_levels_vectorized rebuilt them as int64, and
# process_direction's propagation then assigned int64 values into an int16 column.
# On pandas 2.x that is a FutureWarning and succeeds; on pandas 3.x -- which the
# box runs and this venv does not -- it is a TypeError, and 03b published nothing.
# int16 holds ten gates with room; int32 holds the mask with room for twenty-two
# more. Both are explicit so neither site can quietly widen.
GATES_FAILED_DTYPE = np.int16
GATES_MASK_DTYPE = np.int32


def gate_conditions(df: pd.DataFrame) -> tuple:
    """(conds, feats).

    conds  {gate name: raw boolean Series}, in funnel order, with NO short-circuit.
    feats  the derived Series the gates are built from, returned rather than
           recomputed because the scoring block below needs the same ones. They
           used to be locals of score_vectorized; moving the gate conditions out
           without returning them is what broke 14 names on the first attempt.

    Each value is the gate's own condition evaluated against every row,
    independent of whether an earlier gate already rejected it. The caller
    short-circuits for disqualify_reason and does not for gates_failed.

    Verbatim from the inline block this replaced, including the .get() defaults --
    a missing column must keep defaulting the way it did (rvol 1.0, atr_pct 2.0,
    regime "unknown", flags 0) or the funnel counts move for reasons that have
    nothing to do with the market.
    """
    def col(name, default):
        return df.get(name, pd.Series(default, index=df.index)).fillna(default)

    direction_col = (df.get("direction", pd.Series("long", index=df.index))
                     if "direction" in df.columns
                     else pd.Series("long", index=df.index))

    rvol        = col("rvol", 1.0)
    atr_pct     = col("atr_pct", 2.0)
    regime      = col("market_regime", "unknown")
    bull_liq    = col("bull_liq_sweep", 0)
    choch_b     = col("choch_bull", 0)
    adx_ranging = col("adx_ranging", 0)
    weekly_bull = col("weekly_bullish", 0)
    weekly_bear = col("weekly_bearish", 0)

    long_smc  = (col("near_demand_ob", 0) + col("price_in_bull_fvg", 0)
                 + col("bos_bull", 0) + col("choch_bull", 0) + col("bull_liq_sweep", 0))
    short_smc = (col("near_supply_ob", 0) + col("price_in_bear_fvg", 0)
                 + col("bos_bear", 0) + col("bear_liq_sweep", 0))
    is_long, is_short = direction_col == "long", direction_col == "short"

    g = OrderedDict()
    g["warmup"]                      = df.get("is_warmup", pd.Series(False, index=df.index)) == True
    g["no_trade_zone"]               = col("no_trade_zone", 0) == 1
    g["very_low_volume"]             = rvol < 0.5
    g["atr_too_high"]                = atr_pct > 8.0
    g["bear_regime_no_reversal"]     = (is_long & (regime == "bear")
                                        & (bull_liq == 0) & (choch_b == 0))
    g["adq_ranging_market"]          = adx_ranging == 1
    g["weekly_structure_misaligned"] = ((is_long & (weekly_bear == 1))
                                        | (is_short & (weekly_bull == 1)))
    g["no_smc_signal"]               = ((is_long & (long_smc == 0))
                                        | (is_short & (short_smc == 0)))
    g["no_recent_bos_choch"]         = col("recent_bos_choch", 0) == 0
    assert tuple(g) == GATE_ORDER, "gate order drifted from GATE_ORDER"

    feats = {
        "direction_col": direction_col, "rvol": rvol, "atr_pct": atr_pct,
        "regime": regime, "bull_liq": bull_liq, "choch_b": choch_b,
        "adx_ranging": adx_ranging, "weekly_bull": weekly_bull,
        "weekly_bear": weekly_bear, "long_smc": long_smc, "short_smc": short_smc,
        "near_ob": col("near_demand_ob", 0), "bull_fvg": col("price_in_bull_fvg", 0),
        "bos_bull": col("bos_bull", 0), "choch_b2": col("choch_bull", 0),
        "sup_ob": col("near_supply_ob", 0), "bear_fvg": col("price_in_bear_fvg", 0),
        "bos_bear": col("bos_bear", 0), "liq_bull": col("bull_liq_sweep", 0),
        "liq_bear": col("bear_liq_sweep", 0),
    }
    return g, feats


def score_vectorized(df: pd.DataFrame, sector_bias: dict, symbol_sector: dict,
                     screen=None) -> pd.DataFrame:
    """
    Scores all rows using vectorized pandas operations.
    No loops. No iterrows. Pure numpy/pandas.

    `screen` is an optional callable applied BETWEEN the disqualifier block and
    the score dimensions. This ordering is load-bearing, not cosmetic -- see the
    call site below.
    """
    n = len(df)
    _warn_missing_cols(df)

    # ── Add sector context ────────────────────────────────────────
    df["sector"]      = df["symbol"].map(symbol_sector).fillna("OTHER")
    df["sector_bias"] = df["sector"].map(sector_bias).fillna("avoid")

    # ── DISQUALIFIERS (vectorized) ────────────────────────────────
    # DECLARED ONCE, CONSUMED TWICE. Every gate's raw condition is built in
    # gate_conditions() and used for two different things:
    #
    #   disqualify_reason  short-circuited, first failure wins. Unchanged -- each
    #                      gate is still masked on (~disqualified), so a row
    #                      reports the first gate it failed and the funnel tally
    #                      reads exactly as it did before.
    #   gates_failed       NOT short-circuited. How many gates this row's features
    #                      fail in total, plus a bitmask of which.
    #
    # The second one exists because the first cannot answer "was this a near
    # miss". A row whose disqualify_reason is `warmup` may also fail nine other
    # gates; a row that fails only `no_recent_bos_choch` is a completely different
    # negative, and for the question "what separates a setup from a non-setup" it
    # is the only interesting kind. Short-circuited, the two are indistinguishable.
    #
    # Both read the same dict so they cannot drift. Writing the conditions out
    # twice -- once to short-circuit, once to count -- is how update_outcomes and
    # trade_review came to hold the same abs() bug in two places.
    conds, _f = gate_conditions(df)
    # Rebound as locals, because the scoring dimensions below were written
    # against these names and reading them from a dict at every use site would be
    # a large diff for no benefit.
    direction_col = _f["direction_col"]; rvol = _f["rvol"]; atr_pct = _f["atr_pct"]
    regime = _f["regime"]; near_ob = _f["near_ob"]; bull_fvg = _f["bull_fvg"]
    bos_bull = _f["bos_bull"]; choch_b2 = _f["choch_b2"]; sup_ob = _f["sup_ob"]
    bear_fvg = _f["bear_fvg"]; bos_bear = _f["bos_bear"]; liq_bull = _f["liq_bull"]
    liq_bear = _f["liq_bear"]

    df["disqualified"]      = False
    df["disqualify_reason"] = ""
    for name, cond in conds.items():
        mask = (~df["disqualified"]) & cond
        df.loc[mask, "disqualified"]      = True
        df.loc[mask, "disqualify_reason"] = name

    # The unconditioned view. int8 is enough for 9 gates and keeps the frame small
    # at ~183k rows per direction.
    failed = np.zeros(len(df), dtype=GATES_FAILED_DTYPE)
    bits   = np.zeros(len(df), dtype=GATES_MASK_DTYPE)
    for i, (name, cond) in enumerate(conds.items()):
        c = cond.to_numpy(dtype=bool, na_value=False) if hasattr(cond, "to_numpy") \
            else np.asarray(cond, dtype=bool)
        failed += c.astype(GATES_FAILED_DTYPE)
        bits   |= (c.astype(GATES_MASK_DTYPE) << i)
    df["gates_failed"]      = failed
    df["gates_failed_mask"] = bits

    # ── SCREEN HOOK ───────────────────────────────────────────────
    # accumulation.py's contract, verbatim: "Call AFTER 03b's disqualifier
    # block, BEFORE scoring." It used to be called after the whole of
    # score_vectorized, which does disqualifiers AND scoring in one pass. Every
    # score dimension below is zeroed via np.where(df["disqualified"], 0.0, ...),
    # so rows the screen recovered had already been zeroed and nothing
    # recomputed them: on the 11 Aug batch, 113 of 154 qualifying signals
    # carried total_score = 0.0 and grade "skip", all five components exactly
    # zero. MIN_SCORE=70 in 06_push_supabase was silently discarding all of
    # them, which is why 4 signals published instead of 154.
    #
    # Running the screen here is safe: it mutates only disqualified /
    # disqualify_reason and appends is_accumulation / accumulation_score. It
    # does not reindex or drop rows, so the Series extracted above stay aligned,
    # and the zeroing below reads df["disqualified"] live.
    if screen is not None:
        df = screen(df)

    # ── SCORE DIMENSIONS (vectorized) ────────────────────────────

    # 1. Market regime score (max 15)
    regime_base = regime.map({"bull":10.0,"sideways":6.0,"bear":3.0,"unknown":5.0}).fillna(5.0)

    # Sector tailwind boost
    is_long_strong  = (direction_col == "long")  & (df["sector_bias"] == "long")
    is_short_strong = (direction_col == "short") & (df["sector_bias"] == "short")
    regime_base = np.where(is_long_strong | is_short_strong,
                           np.minimum(regime_base + 3.0, 12.0), regime_base)

    vix = df.get("vix_close", pd.Series(15.0, index=df.index)).fillna(15.0)
    vix_pts = np.where(vix < 14, 5.0, np.where(vix < 18, 3.0, np.where(vix < 22, 1.0, 0.0)))

    ad = df.get("ad_ratio", pd.Series(1.0, index=df.index)).fillna(1.0)
    ad_pts = np.where(ad >= 2.0, 2.0, np.where(ad >= 1.2, 1.0, 0.0))

    regime_score = np.minimum(regime_base + vix_pts + ad_pts, 15.0)
    regime_score = np.where(df["disqualified"], 0.0, regime_score)

    # 2. SMC structure score (max 30)
    structure = df.get("structure_trend", pd.Series("ranging", index=df.index)).fillna("ranging")
    struct_pts_long  = structure.map({"uptrend":8.0,"ranging":4.0,"downtrend":0.0}).fillna(0.0)
    struct_pts_short = structure.map({"downtrend":8.0,"ranging":4.0,"uptrend":0.0}).fillna(0.0)

    in_discount = df.get("in_discount", pd.Series(0, index=df.index)).fillna(0)
    in_premium  = df.get("in_premium",  pd.Series(0, index=df.index)).fillna(0)
    adx_val     = df.get("adx", pd.Series(20.0, index=df.index)).fillna(20.0)
    adx_boost   = np.where(adx_val > 30, 3.0, np.where(adx_val > 25, 1.5, 0.0))

    smc_long  = struct_pts_long  + near_ob*8 + bull_fvg*7 + liq_bull*7 + bos_bull*5 + choch_b2*8 + in_discount*4 + adx_boost
    smc_short = struct_pts_short + sup_ob*8  + bear_fvg*7 + liq_bear*7 + bos_bear*5 + \
                df.get("choch_bear", pd.Series(0,index=df.index)).fillna(0)*8 + in_premium*4 + adx_boost

    smc_score = np.where(direction_col == "long",
                         np.minimum(smc_long, 30.0),
                         np.minimum(smc_short, 30.0))
    smc_score = np.where(df["disqualified"], 0.0, smc_score)

    # 3. Technical score (max 25)
    rsi = df.get("rsi", pd.Series(50.0, index=df.index)).fillna(50.0)
    p_ema20  = df.get("price_above_ema20",  pd.Series(0,index=df.index)).fillna(0)
    ema20_50 = df.get("ema20_above_ema50",  pd.Series(0,index=df.index)).fillna(0)
    ema50_200= df.get("ema50_above_ema200", pd.Series(0,index=df.index)).fillna(0)
    macd_r   = df.get("macd_hist_rising",   pd.Series(0,index=df.index)).fillna(0)
    # 02b writes this as "macd_positive" (not "macd_hist_positive" -- its two
    # MACD columns are named inconsistently). Reading the wrong name meant
    # .get() returned its all-zero default on every row: longs silently lost
    # the 2-point macd_positive component, and shorts silently GAINED a
    # permanent +2 from (1 - 0) * 2. Read the name the producer actually writes.
    macd_p   = df.get("macd_positive",      pd.Series(0,index=df.index)).fillna(0)

    ema_long  = p_ema20*4 + ema20_50*3 + ema50_200*3
    ema_short = (1-p_ema20)*4 + (1-ema20_50)*3 + (1-ema50_200)*3

    rsi_long  = np.where((rsi>=45)&(rsi<=65), 10.0,
                np.where((rsi>=35)&(rsi<45),   6.0,
                np.where((rsi>65)&(rsi<=70),   4.0,
                np.where(rsi<35,               3.0, 0.0))))
    rsi_short = np.where((rsi>=35)&(rsi<=55), 10.0,
                np.where((rsi>55)&(rsi<=65),   6.0,
                np.where(rsi>70,               3.0, 0.0)))

    macd_long  = macd_r*3 + macd_p*2
    macd_short = (1-macd_r)*3 + (1-macd_p)*2

    tech_long  = np.minimum(ema_long  + rsi_long  + macd_long,  25.0)
    tech_short = np.minimum(ema_short + rsi_short + macd_short, 25.0)
    tech_score = np.where(direction_col=="long", tech_long, tech_short)
    tech_score = np.where(df["disqualified"], 0.0, tech_score)

    # 4. Volume / institutional score (max 20)
    rvol2 = rvol.values
    rvol_pts = np.where(rvol2>=2.0, 10.0,
               np.where(rvol2>=1.5,  7.0,
               np.where(rvol2>=1.2,  4.0,
               np.where(rvol2>=1.0,  2.0, 0.0))))

    inst_buy = df.get("institutional_buying", pd.Series(0,index=df.index)).fillna(0)
    hi_del   = df.get("high_delivery",        pd.Series(0,index=df.index)).fillna(0)
    rs_pos   = df.get("rs_positive",          pd.Series(0,index=df.index)).fillna(0)

    vol_score = np.minimum(rvol_pts + inst_buy*7 + hi_del*4 + rs_pos*3, 20.0)
    vol_score = np.where(df["disqualified"], 0.0, vol_score)

    # 5. Risk/reward score (max 10)
    rr_pts  = np.where((atr_pct>=1.0)&(atr_pct<=3.0), 7.0,
               np.where((atr_pct>=0.5)&(atr_pct<1.0),  4.0,
               np.where((atr_pct>3.0)&(atr_pct<=5.0),  4.0, 0.0)))

    pct52 = df.get("pct_from_52w_high", pd.Series(-5.0,index=df.index)).fillna(-5.0)
    rr_ext = np.where(direction_col=="long",
                      np.where(pct52<-5, 3.0, np.where(pct52<=-0.0, 2.0, 1.0)),
                      np.where((pct52>=-5)&(pct52<=5), 3.0, 1.0))

    rr_score = np.minimum(rr_pts + rr_ext, 10.0)
    rr_score = np.where(df["disqualified"], 0.0, rr_score)

    # ── TOTAL SCORE ───────────────────────────────────────────────
    total = np.round(regime_score + smc_score + tech_score + vol_score + rr_score, 1)
    total = np.where(df["disqualified"], 0.0, total)

    df["regime_score"]    = np.round(regime_score, 1)
    df["smc_score"]       = np.round(smc_score, 1)
    df["technical_score"] = np.round(tech_score, 1)
    df["volume_score"]    = np.round(vol_score, 1)
    df["rr_score"]        = np.round(rr_score, 1)
    df["total_score"]     = total
    # No score gate. Score is non-predictive per validation; the disqualifiers
    # alone decide qualification. This used to be re-derived in
    # process_direction AFTER the screen, which meant the grade below was
    # assigned off the pre-screen value and every recovered row kept "skip".
    df["qualifies"]       = ~df["disqualified"]

    # Assign grade based on score. "C" covers qualifying rows below the old
    # MIN_SCORE floor -- mostly accumulation setups, which score low by
    # construction (quiet tape scores 0 on rvol_pts, ranging scores 0 on
    # adx_boost). They are real signals, not skips.
    # NOTE: signals.html and tsl-dashboard.html style only A+/A/B and fall
    # through to B styling for anything else, so "C" renders as a B-styled
    # chip until a ring-c / g-c class is added. Cosmetic; flagged separately.
    df["grade"] = "C"
    df.loc[~df["qualifies"], "grade"] = "skip"
    df.loc[df["qualifies"] & (total >= 80), "grade"] = "A+"
    df.loc[df["qualifies"] & (total >= 75) & (total < 80), "grade"] = "A"
    df.loc[df["qualifies"] & (total >= 65) & (total < 75), "grade"] = "B"

    return df


def determine_setup_names(df: pd.DataFrame) -> pd.Series:
    """Vectorized setup name assignment."""
    direction = df.get("direction", pd.Series("long", index=df.index))
    choch_b = df.get("choch_bull",        pd.Series(0,index=df.index)).fillna(0)
    choch_s = df.get("choch_bear",        pd.Series(0,index=df.index)).fillna(0)
    liq_b   = df.get("bull_liq_sweep",    pd.Series(0,index=df.index)).fillna(0)
    liq_s   = df.get("bear_liq_sweep",    pd.Series(0,index=df.index)).fillna(0)
    fvg_ob  = df.get("price_in_bull_fvg", pd.Series(0,index=df.index)).fillna(0) & \
               df.get("near_demand_ob",    pd.Series(0,index=df.index)).fillna(0)
    ob_bos  = df.get("near_demand_ob",    pd.Series(0,index=df.index)).fillna(0) & \
               df.get("bos_bull",          pd.Series(0,index=df.index)).fillna(0)
    fvg_b   = df.get("price_in_bull_fvg", pd.Series(0,index=df.index)).fillna(0)
    ob_only = df.get("near_demand_ob",    pd.Series(0,index=df.index)).fillna(0)
    bos_b   = df.get("bos_bull",          pd.Series(0,index=df.index)).fillna(0)
    sup_ob  = df.get("near_supply_ob",    pd.Series(0,index=df.index)).fillna(0)
    bear_fvg= df.get("price_in_bear_fvg", pd.Series(0,index=df.index)).fillna(0)
    bos_s   = df.get("bos_bear",          pd.Series(0,index=df.index)).fillna(0)
    inst_b  = df.get("institutional_buying", pd.Series(0,index=df.index)).fillna(0)

    name = pd.Series("Bullish Momentum Continuation", index=df.index)

    # Long setups (priority order)
    is_long = direction == "long"
    name = np.where(is_long & (bos_b>0),   "Break of Structure — Bullish",      name)
    name = np.where(is_long & (ob_only>0),  "Demand Order Block Retest",         name)
    name = np.where(is_long & (fvg_b>0),    "FVG Fill — Bullish Imbalance",      name)
    name = np.where(is_long & (ob_bos>0),   "Demand OB + BOS Continuation",      name)
    name = np.where(is_long & (fvg_ob>0),   "FVG + Demand OB Confluence",        name)
    name = np.where(is_long & (liq_b>0),    "Liquidity Sweep Reversal",          name)
    name = np.where(is_long & (choch_b>0),  "CHOCH Reversal — Trend Change",     name)
    name = np.where(is_long & (inst_b>0),   "Institutional Accumulation",        name)

    # Short setups
    is_short = direction == "short"
    name = np.where(is_short,                "Bearish Distribution",              name)
    name = np.where(is_short & (bos_s>0),    "Break of Structure — Bearish",      name)
    name = np.where(is_short & (bear_fvg>0), "FVG Fill — Bearish Imbalance",      name)
    name = np.where(is_short & (sup_ob>0),   "Supply Order Block Rejection",      name)
    name = np.where(is_short & (sup_ob>0) & (bos_s>0), "Supply OB + BOS Breakdown", name)
    name = np.where(is_short & (liq_s>0),    "Liquidity Sweep — Short",           name)
    name = np.where(is_short & (choch_s>0),  "CHOCH — Bearish Reversal",          name)

    return pd.Series(name, index=df.index)


def compute_trade_levels_vectorized(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized entry/SL/target computation."""
    close   = df["close"].fillna(0)
    atr     = df.get("atr", close * 0.02).fillna(close * 0.02)
    direction = df.get("direction", pd.Series("long", index=df.index))

    # ── ZONE-BASED ENTRY + STRUCTURAL STOP ─────────────────────────
    # Entry = active zone edge (demand OB / bull FVG for longs, supply side for
    # shorts), forward-filled by engine.active_zones. Stop = last swing extreme
    # forced outside the zone. Sizing = Rs3k risk / Rs1L notional, dual cap.
    # No targets -- winners are trailed. See engine/zone_entry.py.
    try:
        from engine.zone_entry import compute_zone_entries
    except ModuleNotFoundError:
        from zone_entry import compute_zone_entries
    df = compute_zone_entries(df)

    invalid = ~df["entry_valid"]
    df.loc[invalid, "disqualified"]      = True
    df.loc[invalid, "disqualify_reason"] = df.loc[invalid, "reject_reason"]
    df.loc[invalid, "qualifies"]         = False

    # Gate 10, counted at the stage it actually runs at. compute_zone_entries
    # needs the zone and the swing stop, neither of which exists in the
    # disqualifier block, so this cannot be folded into gate_conditions. A row
    # with a usable setup and an unusable stop is the nearest miss there is and
    # must be distinguishable from one that failed nine feature gates.
    if "gates_failed" in df.columns:
        # NUMPY, NOT A SERIES. pandas implements & | ^ on a Series and does NOT
        # implement << or >>, so `series << 9` raises TypeError rather than
        # broadcasting. The mask built in score_vectorized happens to operate on
        # np.ndarray and worked; this site operates on a column and did not.
        bad = invalid.to_numpy(dtype=bool, na_value=False) \
            if hasattr(invalid, "to_numpy") else np.asarray(invalid, dtype=bool)
        base = df["gates_failed"].fillna(0).to_numpy(dtype=GATES_FAILED_DTYPE)
        mask = df["gates_failed_mask"].fillna(0).to_numpy(dtype=GATES_MASK_DTYPE)
        # Cast back to the SAME dtype score_vectorized used. These values travel
        # home through a masked .loc assignment, which on pandas 3 refuses to
        # widen an int16 column to hold int64 values -- even when every value
        # fits.
        df["gates_failed"] = (base + bad.astype(GATES_FAILED_DTYPE)) \
            .astype(GATES_FAILED_DTYPE)
        df["gates_failed_mask"] = (
            mask | (bad.astype(GATES_MASK_DTYPE) << GATE_ZONE_ENTRY_BIT)
        ).astype(GATES_MASK_DTYPE)

    return df


# ══════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════

def load_all_smc() -> pd.DataFrame:
    """
    DEPRECATED -- use smc_batches(). Kept in case another caller exists.

    This read every SMC parquet and concatenated them into ONE frame. At 0.77 MB
    per symbol that is ~383 MB at 500 symbols and ~610 MB at 796, and pd.concat
    peaks at roughly double while the input list and the result are both alive.
    The box is a t2.micro with 1 GB, so the universe expansion would have OOM'd
    right here -- and main()'s 100-symbol batching could not help, because the
    concat happened before any batching did.
    """
    log.warning("load_all_smc() loads the whole universe into memory; "
                "smc_batches() is the bounded version")
    dfs = []
    for f in tqdm(sorted(SMC_DIR.glob("*.parquet")), desc="Loading"):
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            dfs.append(df)
        except Exception as e:
            log.warning(f"Skip {f.stem}: {e}")
    combined = pd.concat(dfs, ignore_index=True)
    log.info(f"Loaded: {len(combined):,} rows, {combined['symbol'].nunique()} symbols")
    return combined


def smc_batches(batch_size: int = 100):
    """
    Yield at most `batch_size` symbols at a time, read from disk on demand.

    Peak memory becomes O(batch) rather than O(universe): ~77 MB per 100 symbols
    whatever the universe size, against 383 MB today and 610 MB at 796.

    Scoring is row-wise -- score_vectorized has no rolling, shift or groupby, and
    the only cross-row step in this file is build_playbooks' groupby(date) --
    so a batch is as valid a unit as the whole frame. Nothing here needs to see
    another symbol's rows.

    Yields (batch_no, total_batches, frame) so the caller can log progress
    without counting files itself.
    """
    files = sorted(SMC_DIR.glob("*.parquet"))
    total = max(1, (len(files) + batch_size - 1) // batch_size)
    log.info(f"{len(files)} SMC file(s), {total} batch(es) of up to {batch_size}")
    for i in range(0, len(files), batch_size):
        dfs = []
        for f in files[i:i + batch_size]:
            try:
                df = pd.read_parquet(f)
                df["date"] = pd.to_datetime(df["date"])
                dfs.append(df)
            except Exception as e:
                log.warning(f"Skip {f.stem}: {e}")
        if not dfs:
            continue
        yield i // batch_size + 1, total, pd.concat(dfs, ignore_index=True)


def process_direction(combined: pd.DataFrame, direction: str,
                      sector_bias: dict, symbol_sector: dict) -> pd.DataFrame:
    """Process one direction (long or short) for all rows."""
    df = combined.copy()
    df["direction"] = direction

    # Accumulation screen -- undoes the momentum-era disqualifiers that reject
    # quiet, consolidating stocks. Longs only; shorts keep the original logic.
    # Passed INTO score_vectorized so it runs between the disqualifier block and
    # the score dimensions, per its own docstring. Calling it afterwards left
    # every recovered row scored 0 / graded "skip".
    try:
        from engine.accumulation import apply_accumulation_screen
    except ModuleNotFoundError:
        from accumulation import apply_accumulation_screen

    df = score_vectorized(df, sector_bias, symbol_sector,
                          screen=apply_accumulation_screen)

    df["setup_name"] = determine_setup_names(df)

    # Compute levels for qualifying signals.
    #
    # THE MASK MUST BE A SNAPSHOT, NOT A VIEW. df["qualifies"] returns a Series
    # backed by the same data as the column, and the loop below WRITES
    # "qualifies" -- zone validation rejects rows, setting some to False. With a
    # view, that write shrinks the mask itself, and then:
    #   - the next column assignment has fewer keys than values and raises
    #     "Must have equal len keys and value when setting with an iterable", or
    #   - worse, when only some rows are invalidated it does not raise: the
    #     columns listed AFTER "qualifies" -- disqualified, disqualify_reason --
    #     get written against the shrunken mask, landing on the wrong rows.
    #
    # It survived because on the box no qualifying row was being invalidated
    # here, so the mask never moved. The first day zone validation rejected one
    # would have been a crash or silent misalignment. to_numpy(copy=True)
    # freezes it.
    qual_mask = df["qualifies"].to_numpy(dtype=bool, copy=True)
    if qual_mask.sum() > 0:
        levels = compute_trade_levels_vectorized(df[qual_mask].copy())
        # entry_zone_source must be in this list or it never leaves
        # process_direction: the frame that goes to the parquet and to 06_push is
        # built from these columns only, so a value written in zone_entry and
        # omitted here is silently discarded.
        for col in ["entry_ref","entry_low","entry_high","sl","stop_pct",
                    "entry_dist_pct","qty","risk_inr","notional","product",
                    "target_1","target_2","rr_1","rr_2",
                    "entry_valid","reject_reason","entry_zone_source","qualifies",
                    "disqualified","disqualify_reason",
                    # Gate 10 is counted inside compute_trade_levels_vectorized and
                    # dies here if it is not listed -- exactly what the comment
                    # above warns about. A row rejected by zone validation is the
                    # nearest miss the funnel produces, and without these two the
                    # reject sampler would record it as failing nothing.
                    "gates_failed","gates_failed_mask"]:
            if col in levels.columns:
                _assign_masked(df, qual_mask, col, levels[col].values)

    return df


def _assign_masked(df: pd.DataFrame, mask, col: str, values) -> None:
    """df.loc[mask, col] = values, widening the column FIRST when it has to.

    pandas 2 emits a FutureWarning and silently upcasts when the values do not fit
    the destination column's dtype. pandas 3 raises TypeError -- "Invalid value
    '[4 4 4 ... 4 1 1]' for dtype 'int16'" -- even when every value is
    representable, because the objection is to the implicit widening, not to the
    data.

    The box runs pandas 3 and this venv runs 2.3.3, so the whole class of defect
    is a passing test here and a crash there. Nineteen columns go through this
    loop; any of them can be the next one. Widening explicitly is what pandas asks
    for and it is a no-op whenever the dtypes already agree.
    """
    if col not in df.columns:
        df[col] = values if np.ndim(values) == 0 else pd.Series(values, index=df.index[mask]) \
            .reindex(df.index)
        return
    cur = df[col].dtype
    try:
        want = np.result_type(cur, np.asarray(values).dtype)
    except TypeError:
        want = object
    if want != cur:
        df[col] = df[col].astype(want)
    df.loc[mask, col] = values


def _latest_slice(df: pd.DataFrame) -> pd.DataFrame:
    """The most recent date's rows from one batch, rejects included.

    A batch covers up to 100 symbols over the whole history, so this is ~100 rows
    out of ~78,000. Taken per batch because the frame is deleted immediately after
    to keep memory flat, and the global maximum date is not known until every
    batch has been read -- _write_all_rows does the final narrowing.
    """
    if df is None or df.empty or "date" not in df.columns:
        return df.head(0) if df is not None else pd.DataFrame()
    return df[df["date"] == df["date"].max()].copy()


def _write_all_rows(parts: list) -> None:
    """all_rows_v2.parquet -- every row for the latest date, qualifying or not.

    This is the ONLY artifact that carries the rejected population, and
    engine/reject_sample refuses to run without it rather than sampling the
    qualifying set and calling the result a control group.
    """
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        log.warning("no rows captured for all_rows_v2 — the reject sampler will "
                    "have nothing to draw from tonight")
        return
    allr = pd.concat(parts, ignore_index=True)
    # Narrow to the single global maximum: batches can disagree if a symbol's
    # history is short, and a frame holding two dates would silently double the
    # sampler's population.
    latest = allr["date"].max()
    allr = allr[allr["date"] == latest]
    allr.to_parquet(ALL_ROWS, index=False)
    log.info(f"All rows for {str(latest)[:10]}: {len(allr):,} "
             f"({int(allr['disqualified'].sum()):,} rejected, "
             f"{int((~allr['disqualified']).sum()):,} qualifying)")


def build_playbooks(scored: pd.DataFrame) -> pd.DataFrame:
    qualifying = scored[scored["qualifies"]].copy()
    log.info(f"Qualifying signals: {len(qualifying):,}")

    all_plays = []
    for d, group in qualifying.groupby("date"):
        # Nearest to entry first, matching market_open and the website. Score
        # is non-predictive and, past the 0.30% publish gate, every survivor is
        # already at its zone -- immediacy is what separates them.
        longs  = group[group["direction"]=="long"].nsmallest(TOP_N_LONG,  "entry_dist_pct")
        shorts = group[group["direction"]=="short"].nsmallest(TOP_N_SHORT, "entry_dist_pct")
        plays  = pd.concat([longs, shorts])
        plays["playbook_date"] = d
        plays["rank"] = range(1, len(plays)+1)
        all_plays.append(plays)

    if not all_plays:
        return pd.DataFrame()
    return pd.concat(all_plays, ignore_index=True)


def _watchlist(df: pd.DataFrame) -> pd.DataFrame:
    """
    Every row carrying a VALID UNMITIGATED ZONE that is not a published signal.

    REDEFINED 2026-10-01, when MAX_ENTRY_DIST_PCT was removed. The old selector
    matched the reject wording "unreachable" -- rows whose ONLY objection was the
    entry distance. No reject emits that any more, so the old definition selects
    nothing and the watchlist would silently empty.

    The new test is structural rather than textual. engine/zone_entry assigns
    entry / entry_low / entry_high ONLY after a row has cleared, in order:

        a zone resolved from the direction's own family   (not "no active zone")
        the side-of-price invariant                       (not "wrong side")
        a structural stop that resolved                   (not "no structural stop")
        the stop on the correct side of entry             (not "stop not below/above")

    So a finite entry_low AND entry_high IS the definition of "valid unmitigated
    zone": the zone exists, price has not passed through it, and the trade is
    geometrically expressible. What such a row may still have failed is the stop
    CAP, the minimum quantity, or the disqualifier block -- and none of those
    makes the zone less real, they make the trade unavailable.

    THESE ARE NEVER ENTERED. The disqualifier block is kept, so a watchlist row
    that the block rejected is information for the page and nothing else;
    market_open filters entries to publication_kind='signal' for exactly this
    reason. Publishing them keeps the record describing what the engine saw.
    """
    if df is None or not len(df):
        return df.head(0) if df is not None else None
    live = df[~df["is_warmup"]] if "is_warmup" in df.columns else df
    if not {"entry_low", "entry_high"} <= set(live.columns):
        log.warning("no entry band columns on the frame — watchlist empty")
        return live.head(0)
    lo = pd.to_numeric(live["entry_low"], errors="coerce")
    hi = pd.to_numeric(live["entry_high"], errors="coerce")
    has_zone = lo.notna() & hi.notna() & (lo > 0) & (hi > 0)
    # not already a signal
    q = (live["qualifies"].fillna(False).astype(bool)
         if "qualifies" in live.columns else pd.Series(False, index=live.index))
    return live[has_zone & ~q].copy()


def tally(df: pd.DataFrame, stats: dict, dq_reasons, direction: str = "") -> None:
    """
    Accumulate report COUNTS from a full scored batch, before it is reduced to
    its qualifying rows.

    THIS EXISTS BECAUSE THE REPORT CANNOT BE COMPUTED FROM `scored`. Only
    qualifying rows survive into it, so every statistic derived from it was
    structurally fixed: "Qualifying: N (100.0%)", "Disqualified: 0", and an
    empty disqualification breakdown, on every run since the engine was written.
    A number that can only ever say one thing is worse than no number, because
    it reads as a healthy result.

    Counts, not rows, so peak memory stays O(batch) and this does not undo the
    batching it sits inside.
    """
    live = df[~df["is_warmup"]] if "is_warmup" in df.columns else df
    if not len(live):
        return
    q = live["qualifies"].fillna(False).astype(bool)
    d = (live["disqualified"].fillna(False).astype(bool)
         if "disqualified" in live.columns
         else pd.Series(False, index=live.index))
    st = stats.setdefault(direction, Counter()) if direction else stats

    def bump(k, v):
        # .get() rather than += so a plain dict works as well as a Counter --
        # tally is called both per-direction (Counter) and aggregate (dict).
        st[k] = st.get(k, 0) + int(v)

    bump("live", len(live))
    bump("qual", int(q.sum()))
    bump("dq", int(d.sum()))

    if "disqualify_reason" in live.columns:
        reasons = live.loc[d, "disqualify_reason"].dropna().astype(str)
        bucket = dq_reasons.setdefault(direction, Counter()) if direction else dq_reasons
        # Zone rejects carry the measured value -- "stop 8.08% too wide (max
        # 7.0%)" -- so counting them verbatim produces thousands of rows that
        # differ only in a decimal and buries the reason under its own detail.
        # Collapsed to the RULE for reporting; the exact per-row reason is
        # untouched in disqualify_reason and goes to the parquet.
        bucket.update(reasons.str.replace(r"\d[\d.,]*", "N", regex=True))
        # WHICH STAGE rejected it. tally() runs after process_direction, so the
        # zone gate's own reasons are already in disqualify_reason -- separating
        # them is what turns "35,839 shorts rejected" into "25 reached the zone
        # gate and every one failed it", which is a different diagnosis.
        try:
            from engine.zone_entry import is_zone_reject
        except ModuleNotFoundError:
            from zone_entry import is_zone_reject
        zone = reasons.map(is_zone_reject)
        bump("zone_rejected", zone.sum())
        bump("block_rejected", (~zone).sum())


def explain_empty_side(side: str, st, reasons, top: int = 3) -> list:
    """
    Lines explaining why a direction published nothing, or [] if it published.

    A ZERO MUST EXPLAIN ITSELF. Shorts stopped reaching the signals table on
    13 Aug 2026 when MAX_ENTRY_DIST_PCT went 8.0 -> 0.30, and six weeks of
    long-only output looked exactly like a market with no short setups. It was
    not: the setups existed, reached the zone gate, and were rejected there --
    a short's structural stop is about twice as wide at the median as a long's,
    so the median short stop falls outside MAX_STOP_PCT while the median long's
    sits inside it. Every one of those rejections was already computed per row.
    Nothing aggregated them by side, so establishing it took a manual trace of
    something the engine already knew.

    The distinction in the first line is the useful part: rejected AT the zone
    gate means the setup was valid but unreachable, rejected BEFORE it means
    there was no setup to begin with. Those call for opposite responses.
    """
    if not st or st.get("qual") or not st.get("live"):
        return []
    at_zone = st.get("zone_rejected", 0)
    out = [f"NO {side.upper()} SIGNALS PUBLISHED. Not an absence of setups:"]
    if at_zone:
        out.append(f"  {at_zone:,} reached the zone gate and every one failed it")
    else:
        out.append("  none reached the zone gate — rejected earlier, in the "
                   "disqualifier block")
    for reason, cnt in (reasons or Counter()).most_common(top):
        out.append(f"    {cnt:>8,}  {reason}")
    return out


def main():
    log.info("THE STOCK LOGIC — Stage 3b: SMC Trade Scoring Engine (Vectorized)")
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    PLAYBOOKS.mkdir(parents=True, exist_ok=True)

    # Load sector context
    sector_bias   = load_sector_bias()
    symbol_sector = load_symbol_sector()
    log.info(f"Sector bias: {sector_bias}")

    # Read and score one batch at a time. The whole universe is never resident:
    # each batch is read from disk, scored, reduced to its qualifying rows, and
    # dropped before the next is read. Peak memory is O(batch), so it does not
    # grow with the universe -- which is what makes 500 -> 796 symbols possible
    # on a 1 GB box.
    log.info("\n── Steps 1+2: Loading and scoring, batch at a time ──")
    batch_size = 100
    all_qualifying = []
    # Report counts, tallied per batch while the full frame is still in hand.
    # See tally() for why the report cannot be derived from `scored`.
    # Per DIRECTION, because a side going to zero is invisible in a merged total.
    # Shorts stopped publishing on 13 Aug 2026 when MAX_ENTRY_DIST_PCT went 8.0 ->
    # 0.30, and establishing that took a trace of the pipeline: every rejection
    # was already computed per row, and nothing aggregated it by side.
    stats = {}
    dq_reasons = {}

    # ── CANDIDATES: valid in every way EXCEPT that price was not at the zone ──
    #
    # A candidate is a row the zone gate rejected with "unreachable" and nothing
    # else: it has an active zone, a structural stop inside the band, and computed
    # levels -- only the DISTANCE failed. That distance was measured at last
    # night's CLOSE, and market_open.near_zone re-measures it against the live
    # price every cycle, so a candidate is a signal whose one failing test the
    # loop is about to redo on better information.
    #
    # Everything else the zone gate rejects is NOT a candidate: no active zone
    # means there is nothing to enter against, and a stop outside 1.5-7% cannot be
    # sized at all. Those are not "watch and see", they are no trade.
    #
    # Levels are already on these rows -- zone_entry assigns entry/stop/stop_pct
    # before it checks the band and the distance -- but qty is not, because sizing
    # happens after. That is correct: enter_trade re-sizes from the live price.
    candidates = []
    # THE REJECTS, LATEST DATE ONLY. process_direction's frame holds every row --
    # qualifying and rejected -- and is then filtered to the qualifying ones and
    # deleted, which is why the negative class has never been persisted. Only the
    # most recent date is kept: that is ~232 symbols x 2 directions = 464 rows,
    # against 365k if the whole rescored history were written. engine/reject_sample
    # draws from this; nothing else reads it.
    all_rows = []

    for n, total, batch in smc_batches(batch_size):
        log.info(f"Batch {n}/{total}: {batch['symbol'].nunique()} stocks, "
                 f"{len(batch):,} rows")

        longs = process_direction(batch, "long", sector_bias, symbol_sector)
        tally(longs, stats, dq_reasons, "long")
        candidates.append(_watchlist(longs))
        all_rows.append(_latest_slice(longs))
        longs_q = longs[longs["qualifies"]].copy()
        del longs

        shorts = process_direction(batch, "short", sector_bias, symbol_sector)
        tally(shorts, stats, dq_reasons, "short")
        candidates.append(_watchlist(shorts))
        all_rows.append(_latest_slice(shorts))
        shorts_q = shorts[shorts["qualifies"]].copy()
        del shorts
        del batch

        batch_q = pd.concat([longs_q, shorts_q], ignore_index=True)
        del longs_q, shorts_q
        all_qualifying.append(batch_q)

    if not all_qualifying:
        log.error("no SMC data scored — is data/processed/smc populated?")
        return
    scored = pd.concat(all_qualifying, ignore_index=True)
    del all_qualifying
    log.info(f"Total qualifying: {len(scored):,}")
    scored.to_parquet(SIGNALS_DIR / "all_scores_v2.parquet", index=False)

    # The candidate artifact, written even when empty so a consumer can tell
    # "nothing was watchable" from "03b did not get this far".
    cand = (pd.concat([c for c in candidates if c is not None], ignore_index=True)
            if any(c is not None and len(c) for c in candidates)
            else scored.head(0))
    cand.to_parquet(SIGNALS_DIR / "candidates_v2.parquet", index=False)
    log.info(f"Candidates (valid but not at the zone at close): {len(cand):,}")

    # The negative class. Written even when empty, for the same reason as
    # candidates_v2: a consumer must be able to tell "nothing was rejected" from
    # "03b did not get this far".
    _write_all_rows(all_rows)

        # Build playbooks
    log.info("\n── Step 3: Building playbooks ──")
    playbooks = build_playbooks(scored)
    if not playbooks.empty:
        playbooks.to_parquet(SIGNALS_DIR / "daily_signals.parquet", index=False)
        log.info(f"Playbooks: {playbooks['playbook_date'].nunique()} days, "
                 f"{len(playbooks)} signals")

    # Report. The COUNTS come from the per-batch tally, which saw every scored
    # row; `q` is the surviving qualifying frame and is used only where the rows
    # themselves are needed (score distribution, setups).
    q = scored[~scored["is_warmup"]] if len(scored) else scored
    tot = Counter()
    for _d in stats.values():
        tot.update(_d)
    n_live, n_q, n_dq = tot["live"], tot["qual"], tot["dq"]

    log.info(f"\n{'='*60}")
    log.info("SIGNAL ENGINE REPORT")
    log.info(f"{'='*60}")
    log.info(f"  Total stock-days  : {n_live:,}")
    log.info(f"  Qualifying signals: {n_q:,} ({n_q/max(n_live,1)*100:.1f}%)")
    log.info(f"  Disqualified      : {n_dq:,} ({n_dq/max(n_live,1)*100:.1f}%)")

    # ── BY DIRECTION, AND THE TWO STAGES SEPARATELY ───────────────
    # "reached zone" is what survived the disqualifier block and was handed to
    # the zone gate; "published" is what came out of it. The gap between them is
    # the gate's own effect, which is where shorts die: their structural stop is
    # about twice as wide at the median as a long's, so the median short stop
    # falls outside MAX_STOP_PCT while the median long's sits inside it.
    log.info(f"\n  BY DIRECTION")
    log.info(f"    {'side':<8}{'scanned':>10}{'reached zone':>14}"
             f"{'published':>11}{'rate':>8}")
    for side in ("long", "short"):
        st = stats.get(side) or Counter()
        reached = st["qual"] + st["zone_rejected"]
        rate = st["qual"] / reached * 100 if reached else 0.0
        log.info(f"    {side:<8}{st['live']:>10,}{reached:>14,}"
                 f"{st['qual']:>11,}{rate:>7.1f}%")

    # A ZERO MUST EXPLAIN ITSELF. Six weeks of long-only output looked identical
    # to a market with no short setups; it was neither -- the setups existed and
    # the gate rejected all of them. Naming the dominant reason turns a silence
    # into a finding.
    for side in ("long", "short"):
        lines = explain_empty_side(side, stats.get(side),
                                   dq_reasons.get(side))
        if lines:
            log.info("")
            for ln in lines:
                log.info(f"  {ln}")
    # The three must account for every scored row. If they do not, a row was
    # neither qualified nor disqualified and the scoring block has a hole in it.
    if n_live and n_q + n_dq != n_live:
        log.warning(f"  ⚠️ {n_live - n_q - n_dq:,} row(s) neither qualified nor "
                    f"disqualified — the scoring block is not exhaustive")

    if len(q):
        log.info(f"\n  Score distribution:")
        log.info(f"    Mean   : {q['total_score'].mean():.1f}")
        log.info(f"    Median : {q['total_score'].median():.1f}")
        log.info(f"    A+/A   : {(q['total_score']>=75).sum()}")
        log.info(f"    B      : {((q['total_score']>=65)&(q['total_score']<75)).sum()}")

    log.info(f"\n  By direction:")
    for d in ["long","short"]:
        sub = q[q["direction"]==d]
        if len(sub):
            log.info(f"    {d}: {len(sub)} signals, avg score {sub['total_score'].mean():.1f}")

    log.info(f"\n  Disqualification breakdown (long / short):")
    _merged = Counter()
    for _d in dq_reasons.values():
        _merged.update(_d)
    _l = dq_reasons.get("long") or Counter()
    _s = dq_reasons.get("short") or Counter()
    for reason, cnt in _merged.most_common():
        log.info(f"    {reason[:44]:<46}{_l[reason]:>9,} /{_s[reason]:>9,}")

    log.info(f"\n  Top setups:")
    for setup, cnt in q["setup_name"].value_counts().head(8).items():
        log.info(f"    {setup:<40}: {cnt}")

    # Sample playbook
    if not playbooks.empty:
        last_date = playbooks["playbook_date"].max()
        last_day  = playbooks[playbooks["playbook_date"]==last_date]
        log.info(f"\n  Sample playbook — {pd.Timestamp(last_date).strftime('%d %b %Y')}:")
        log.info(f"  {'#':>2} {'SYM':<12} {'DIR':<6} {'SCORE':>6} {'GRADE':>5} "
                 f"{'ENTRY':>8} {'T1':>8} {'T2':>8} {'SL':>8} {'RISK₹':>7} {'SETUP'}")
        log.info(f"  {'-'*100}")
        for _, r in last_day.iterrows():
            grade = "A+" if r["total_score"]>=80 else "A" if r["total_score"]>=75 else "B"
            log.info(
                f"  {int(r.get('rank',0)):>2} {r['symbol']:<12} "
                f"{'↑LONG' if r['direction']=='long' else '↓SHORT':<6} "
                f"{r['total_score']:>6.1f} {grade:>5} "
                f"₹{r.get('entry_ref',0):>7.1f} "
                f"₹{r.get('target_1',0):>7.1f} "
                f"₹{r.get('target_2',0):>7.1f} "
                f"₹{r.get('sl',0):>7.1f} "
                f"₹{r.get('risk_inr',0):>6.0f} "
                f"{r.get('setup_name','')}"
            )

    log.info(f"\nSTATUS: {'PASS' if n_q>0 else 'FAIL'}")
    log.info("Next: python3 engine/06_push_supabase.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
