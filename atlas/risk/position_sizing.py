"""
ATLAS Risk Engine — Position Sizing
=====================================
Kelly-adjusted, volatility-aware position sizing.
Never risks more than MAX_RISK_PER_TRADE per trade.
Adjusts size based on agent mode and conviction score.
"""

import sys, math, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    INITIAL_CAPITAL, MAX_RISK_PER_TRADE,
    MIN_CONVICTION_SCORE, ELITE_CONVICTION,
    AGENT_MODES, DEFAULT_AGENT_MODE
)

log = logging.getLogger(__name__)


def get_capital(state: dict = None) -> float:
    """Get current available capital."""
    if state:
        return float(state.get("capital", INITIAL_CAPITAL))
    return INITIAL_CAPITAL


def kelly_fraction(win_rate: float, rr_ratio: float) -> float:
    """
    Kelly Criterion: f = (p*b - q) / b
    p = win rate, q = 1-p, b = risk/reward ratio
    Returns fraction of capital to risk (capped at 25%).
    """
    if rr_ratio <= 0 or win_rate <= 0:
        return 0.02  # Default 2% risk
    p = win_rate
    q = 1 - p
    b = rr_ratio
    kelly = (p * b - q) / b
    # Half-Kelly for safety, cap at 25%
    return max(0.01, min(kelly * 0.5, 0.25))


def conviction_multiplier(conviction: float) -> float:
    """
    Scale position size based on conviction score.
    75-79: 0.6x (minimum threshold)
    80-84: 0.8x
    85-89: 1.0x (full size)
    90+:   1.2x (elite — slight oversize)
    """
    if conviction >= 90:
        return 1.2
    elif conviction >= 85:
        return 1.0
    elif conviction >= 80:
        return 0.8
    else:
        return 0.6


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
    Calculate position size for a trade.

    Args:
        entry_price:  Entry price
        sl_price:     Stop loss price
        target_price: Target 1 price
        conviction:   Signal conviction score (0-100)
        agent_mode:   Current agent mode
        capital:      Available capital (defaults to config)
        win_rate:     Historical win rate (default 50%)

    Returns dict with:
        qty:          Number of shares to buy/sell
        capital_deployed: Total capital used
        risk_inr:     Maximum loss if SL hit
        reward_inr:   Maximum gain if T1 hit
        rr_ratio:     Risk-reward ratio
        size_pct:     Position as % of capital
    """
    if not entry_price or not sl_price or entry_price == sl_price:
        return {"qty": 0, "error": "Invalid entry or SL price"}

    cap = capital or INITIAL_CAPITAL
    mode_config = AGENT_MODES.get(agent_mode, AGENT_MODES[DEFAULT_AGENT_MODE])
    mode_size_pct = mode_config["size_pct"]

    # Risk per share
    risk_per_share = abs(entry_price - sl_price)
    if risk_per_share <= 0:
        return {"qty": 0, "error": "Risk per share is zero"}

    # Reward per share
    reward_per_share = abs(target_price - entry_price) if target_price else risk_per_share * 2
    rr_ratio = reward_per_share / risk_per_share

    # Kelly fraction
    kelly = kelly_fraction(win_rate, rr_ratio)

    # Max risk in INR (absolute cap)
    max_risk_inr = min(MAX_RISK_PER_TRADE, cap * kelly)

    # Apply agent mode scaling
    max_risk_inr *= mode_size_pct

    # Apply conviction multiplier
    conv_mult = conviction_multiplier(conviction)
    max_risk_inr *= conv_mult

    # Hard cap — never risk more than MAX_RISK_PER_TRADE
    max_risk_inr = min(max_risk_inr, MAX_RISK_PER_TRADE)

    # Calculate quantity
    qty = math.floor(max_risk_inr / risk_per_share)

    if qty <= 0:
        return {"qty": 0, "error": "Position size too small"}

    # Capital deployed
    capital_deployed = qty * entry_price
    actual_risk      = qty * risk_per_share
    actual_reward    = qty * reward_per_share

    # For SHORT MIS — override capital_deployed to show only risk
    product = "MIS" if str(direction).upper() == "SHORT" else "CNC"
    if product == "MIS":
        capital_deployed = risk_inr  # Only risk counts for MIS

    result = {
        "qty":               qty,
        "product":           product,
        "entry_price":       entry_price,
        "sl_price":          sl_price,
        "target_price":      target_price,
        "capital_deployed":  round(capital_deployed, 2),
        "risk_inr":          round(actual_risk, 2),
        "reward_inr":        round(actual_reward, 2),
        "rr_ratio":          round(rr_ratio, 2),
        "size_pct":          round(capital_deployed / cap * 100, 2),
        "conviction":        conviction,
        "agent_mode":        agent_mode,
        "kelly_fraction":    round(kelly, 4),
        "conv_multiplier":   conv_mult,
    }

    log.info(
        f"Position sizing: {qty} shares | "
        f"Capital: ₹{capital_deployed:,.0f} | "
        f"Risk: ₹{actual_risk:,.0f} | "
        f"RR: {rr_ratio:.1f}:1 | "
        f"Conv: {conviction}/100 | "
        f"Mode: {agent_mode}"
    )

    return result


def validate(sizing: dict, capital: float = None) -> tuple:
    """
    Validate position sizing result.
    Returns (is_valid, reason)
    """
    cap = capital or INITIAL_CAPITAL

    if sizing.get("qty", 0) <= 0:
        return False, sizing.get("error", "Zero quantity")

    if sizing.get("rr_ratio", 0) < 1.5:
        return False, f"RR ratio too low: {sizing['rr_ratio']:.1f}:1 (min 1.5:1)"

    if sizing.get("risk_inr", 0) > MAX_RISK_PER_TRADE:
        return False, f"Risk too high: ₹{sizing['risk_inr']:,.0f} (max ₹{MAX_RISK_PER_TRADE:,.0f})"

    # Capital deployed check removed — capital_manager handles the fence
    # MIS shorts use margin, CNC longs bounded by capital_manager.can_deploy()

    return True, "Valid"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s [ATLAS-SIZING] %(message)s")

    print("=== ATLAS POSITION SIZING TEST ===\n")

    # Test 1 — Grade A signal NORMAL mode
    result = calculate(
        entry_price=1543.0,
        sl_price=1512.0,
        target_price=1605.0,
        conviction=81,
        agent_mode="NORMAL",
        capital=100000,
        win_rate=0.50,
    )
    is_valid, reason = validate(result, 100000)
    print(f"TECHM LONG (Conv:81, NORMAL mode):")
    print(f"  Qty:      {result['qty']} shares")
    print(f"  Capital:  INR {result['capital_deployed']:,.0f} ({result['size_pct']:.1f}%)")
    print(f"  Risk:     INR {result['risk_inr']:,.0f}")
    print(f"  Reward:   INR {result['reward_inr']:,.0f}")
    print(f"  RR:       {result['rr_ratio']:.1f}:1")
    print(f"  Valid:    {is_valid} — {reason}")

    print()

    # Test 2 — Elite signal AGGRESSIVE mode
    result2 = calculate(
        entry_price=833.0,
        sl_price=825.0,
        target_price=843.0,
        conviction=91,
        agent_mode="AGGRESSIVE",
        capital=100000,
        win_rate=0.55,
    )
    is_valid2, reason2 = validate(result2, 100000)
    print(f"CANFINHOME LONG (Conv:91, AGGRESSIVE mode):")
    print(f"  Qty:      {result2['qty']} shares")
    print(f"  Capital:  INR {result2['capital_deployed']:,.0f} ({result2['size_pct']:.1f}%)")
    print(f"  Risk:     INR {result2['risk_inr']:,.0f}")
    print(f"  Reward:   INR {result2['reward_inr']:,.0f}")
    print(f"  RR:       {result2['rr_ratio']:.1f}:1")
    print(f"  Valid:    {is_valid2} — {reason2}")

    print()

    # Test 3 — CAUTIOUS mode
    result3 = calculate(
        entry_price=1812.0,
        sl_price=1849.0,
        target_price=1740.0,
        conviction=73,
        agent_mode="CAUTIOUS",
        capital=100000,
        win_rate=0.45,
    )
    is_valid3, reason3 = validate(result3, 100000)
    print(f"SBILIFE SHORT (Conv:73, CAUTIOUS mode):")
    print(f"  Qty:      {result3['qty']} shares")
    print(f"  Capital:  INR {result3['capital_deployed']:,.0f} ({result3['size_pct']:.1f}%)")
    print(f"  Risk:     INR {result3['risk_inr']:,.0f}")
    print(f"  Reward:   INR {result3['reward_inr']:,.0f}")
    print(f"  RR:       {result3['rr_ratio']:.1f}:1")
    print(f"  Valid:    {is_valid3} — {reason3}")
