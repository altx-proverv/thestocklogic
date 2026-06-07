"""
ATLAS Risk Engine — Position Sizing
=====================================
CNC LONG: Fixed INR 50,000 per trade (capital/3)
          Qty = floor(50,000 / entry_price)
          Never blocks — sizes down to fit
          Only blocks if qty = 0 (stock too expensive)

MIS SHORT: Risk-capped sizing
           Qty = floor(MAX_RISK_PER_TRADE / risk_per_share)
           Capital deployed = margin (not real capital)
"""

import sys, math, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    INITIAL_CAPITAL, CAPITAL_PER_TRADE, MAX_RISK_PER_TRADE,
    MIN_CONVICTION_SCORE, ELITE_CONVICTION,
    AGENT_MODES, DEFAULT_AGENT_MODE
)

log = logging.getLogger(__name__)


def conviction_multiplier(conviction: float) -> float:
    if conviction >= 90: return 1.0   # No oversize — keep it clean
    elif conviction >= 85: return 1.0
    elif conviction >= 80: return 0.8
    else: return 0.6


def calculate(
    entry_price: float,
    sl_price: float,
    target_price: float,
    conviction: float = 75,
    agent_mode: str = DEFAULT_AGENT_MODE,
    capital: float = None,
    win_rate: float = 0.5,
    direction: str = "LONG",
) -> dict:
    """
    Calculate position size.
    CNC LONG: fixed INR 50K per trade, size down if needed
    MIS SHORT: risk-capped, margin trade
    """
    if not entry_price or not sl_price or entry_price == sl_price:
        return {"qty": 0, "error": "Invalid entry or SL price"}

    cap        = capital or INITIAL_CAPITAL
    product    = "MIS" if str(direction).upper() == "SHORT" else "CNC"
    mode_config= AGENT_MODES.get(agent_mode, AGENT_MODES[DEFAULT_AGENT_MODE])

    risk_per_share   = abs(entry_price - sl_price)
    reward_per_share = abs(target_price - entry_price) if target_price else risk_per_share * 2
    rr_ratio         = reward_per_share / risk_per_share if risk_per_share > 0 else 0

    if product == "CNC":
        # Fixed INR 50K per trade — size down never block
        trade_capital = CAPITAL_PER_TRADE * mode_config["size_pct"]
        qty = math.floor(trade_capital / entry_price)

        if qty <= 0:
            return {"qty": 0, "error": f"Stock too expensive — ₹{entry_price:,.0f} exceeds ₹{trade_capital:,.0f} per trade budget"}

        capital_deployed = qty * entry_price
        risk_inr         = qty * risk_per_share
        reward_inr       = qty * reward_per_share

    else:  # MIS SHORT
        # Risk-capped — qty based on SL distance
        conv_mult    = conviction_multiplier(conviction)
        max_risk_inr = min(MAX_RISK_PER_TRADE * mode_config["size_pct"] * conv_mult, MAX_RISK_PER_TRADE)
        qty          = math.floor(max_risk_inr / risk_per_share) if risk_per_share > 0 else 0

        if qty <= 0:
            return {"qty": 0, "error": "Position size too small"}

        capital_deployed = risk_inr  = qty * risk_per_share  # Only risk counts for MIS
        reward_inr       = qty * reward_per_share

    result = {
        "qty":               qty,
        "product":           product,
        "direction":         direction.upper(),
        "entry_price":       entry_price,
        "sl_price":          sl_price,
        "target_1":          target_price,
        "target_price":      target_price,
        "capital_deployed":  round(capital_deployed, 2),
        "risk_inr":          round(risk_inr, 2),
        "reward_inr":        round(reward_inr, 2),
        "rr_ratio":          round(rr_ratio, 2),
        "size_pct":          round(capital_deployed / cap * 100, 2),
        "conviction":        conviction,
        "agent_mode":        agent_mode,
    }

    log.info(
        f"Sizing [{product}] {direction}: {qty} shares @ ₹{entry_price:,.0f} | "
        f"Capital: ₹{capital_deployed:,.0f} | Risk: ₹{risk_inr:,.0f} | RR: {rr_ratio:.1f}:1"
    )
    return result


def validate(sizing: dict, capital: float = None) -> tuple:
    """Validate position sizing. Returns (is_valid, reason)."""
    if sizing.get("qty", 0) <= 0:
        return False, sizing.get("error", "Zero quantity")
    if sizing.get("rr_ratio", 0) < 1.5:
        return False, f"RR too low: {sizing['rr_ratio']:.1f}:1 (min 1.5:1)"
    if sizing.get("product") == "MIS" and sizing.get("risk_inr", 0) > MAX_RISK_PER_TRADE:
        return False, f"MIS risk exceeds cap: ₹{sizing['risk_inr']:,.0f}"
    return True, "Valid"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s [ATLAS-SIZING] %(message)s")
    print("=== ATLAS POSITION SIZING ===\n")

    # SUNTV LONG CNC — the trade that was blocked
    r1 = calculate(entry_price=521.0, sl_price=510.0, target_price=542.0,
                   conviction=78, agent_mode="NORMAL", capital=150000, direction="LONG")
    v1, reason1 = validate(r1)
    print(f"SUNTV LONG CNC (was blocked before):")
    print(f"  Qty:      {r1['qty']} shares")
    print(f"  Capital:  ₹{r1['capital_deployed']:,.0f} ({r1['size_pct']:.1f}%)")
    print(f"  Risk:     ₹{r1['risk_inr']:,.0f}")
    print(f"  RR:       {r1['rr_ratio']:.1f}:1")
    print(f"  Valid:    {v1} — {reason1}\n")

    # NHPC SHORT MIS
    r2 = calculate(entry_price=75.1, sl_price=76.5, target_price=73.0,
                   conviction=79, agent_mode="NORMAL", capital=150000, direction="SHORT")
    v2, reason2 = validate(r2)
    print(f"NHPC SHORT MIS:")
    print(f"  Qty:      {r2['qty']} shares")
    print(f"  Risk:     ₹{r2['risk_inr']:,.0f} (hard cap)")
    print(f"  RR:       {r2['rr_ratio']:.1f}:1")
    print(f"  Valid:    {v2} — {reason2}")
