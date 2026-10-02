# The 03b disqualifier block as it stood before gate_conditions() was
# extracted, captured 2026-10-02 and dedented to module level. Kept in the
# repo so tests/test_gate_audit.py can prove the refactor changed no funnel
# count on any machine, not just the one it was done on. Do not edit: its only
# job is to be the old behaviour.

# ── DISQUALIFIERS (vectorized) ────────────────────────────────
df["disqualified"]       = False
df["disqualify_reason"]  = ""

# Warmup
mask = df["is_warmup"] == True
df.loc[mask, "disqualified"]      = True
df.loc[mask, "disqualify_reason"] = "warmup"

# No trade zone
mask = (~df["disqualified"]) & (df.get("no_trade_zone", pd.Series(0, index=df.index)) == 1)
df.loc[mask, "disqualified"]      = True
df.loc[mask, "disqualify_reason"] = "no_trade_zone"

# Very low volume
rvol = df.get("rvol", pd.Series(1.0, index=df.index)).fillna(1.0)
mask = (~df["disqualified"]) & (rvol < 0.5)
df.loc[mask, "disqualified"]      = True
df.loc[mask, "disqualify_reason"] = "very_low_volume"

# ATR too high
atr_pct = df.get("atr_pct", pd.Series(2.0, index=df.index)).fillna(2.0)
mask = (~df["disqualified"]) & (atr_pct > 8.0)
df.loc[mask, "disqualified"]      = True
df.loc[mask, "disqualify_reason"] = "atr_too_high"

# Bear regime no reversal (long only)
regime   = df.get("market_regime", pd.Series("unknown", index=df.index)).fillna("unknown")
bull_liq = df.get("bull_liq_sweep", pd.Series(0, index=df.index)).fillna(0)
choch_b  = df.get("choch_bull", pd.Series(0, index=df.index)).fillna(0)
direction_col = df.get("direction", pd.Series("long", index=df.index)) if "direction" in df.columns else pd.Series("long", index=df.index)
mask = (~df["disqualified"]) & (direction_col == "long") & \
       (regime == "bear") & (bull_liq == 0) & (choch_b == 0)
df.loc[mask, "disqualified"]      = True
df.loc[mask, "disqualify_reason"] = "bear_regime_no_reversal"

# ADX ranging disqualifier
adx_ranging = df.get("adx_ranging", pd.Series(0, index=df.index)).fillna(0)
mask = (~df["disqualified"]) & (adx_ranging == 1)
df.loc[mask, "disqualified"]      = True
df.loc[mask, "disqualify_reason"] = "adq_ranging_market"

# Weekly structure misalignment disqualifier
weekly_bull = df.get("weekly_bullish", pd.Series(0, index=df.index)).fillna(0)
weekly_bear = df.get("weekly_bearish", pd.Series(0, index=df.index)).fillna(0)
mask_long_weak  = (~df["disqualified"]) & (direction_col=="long")  & (weekly_bear==1)
mask_short_weak = (~df["disqualified"]) & (direction_col=="short") & (weekly_bull==1)
df.loc[mask_long_weak | mask_short_weak, "disqualified"]      = True
df.loc[mask_long_weak | mask_short_weak, "disqualify_reason"] = "weekly_structure_misaligned"

# SMC signal columns
near_ob  = df.get("near_demand_ob",    pd.Series(0, index=df.index)).fillna(0)
bull_fvg = df.get("price_in_bull_fvg", pd.Series(0, index=df.index)).fillna(0)
bos_bull = df.get("bos_bull",          pd.Series(0, index=df.index)).fillna(0)
choch_b2 = df.get("choch_bull",        pd.Series(0, index=df.index)).fillna(0)
sup_ob   = df.get("near_supply_ob",    pd.Series(0, index=df.index)).fillna(0)
bear_fvg = df.get("price_in_bear_fvg", pd.Series(0, index=df.index)).fillna(0)
bos_bear = df.get("bos_bear",          pd.Series(0, index=df.index)).fillna(0)
liq_bull = df.get("bull_liq_sweep",    pd.Series(0, index=df.index)).fillna(0)
liq_bear = df.get("bear_liq_sweep",    pd.Series(0, index=df.index)).fillna(0)

long_smc  = near_ob + bull_fvg + bos_bull + choch_b2 + liq_bull
short_smc = sup_ob  + bear_fvg + bos_bear + liq_bear

# Hard gate 1: no SMC signal at all
mask_long  = (~df["disqualified"]) & (direction_col == "long")  & (long_smc  == 0)
mask_short = (~df["disqualified"]) & (direction_col == "short") & (short_smc == 0)
df.loc[mask_long | mask_short, "disqualified"]      = True
df.loc[mask_long | mask_short, "disqualify_reason"] = "no_smc_signal"

# Hard gate 2: BOS or CHOCH must have occurred within last 9 trading days
# Without structure confirmation, OB alone is insufficient
recent_bos = df.get("recent_bos_choch", pd.Series(0, index=df.index)).fillna(0)
mask_no_struct = (~df["disqualified"]) & (recent_bos == 0)
df.loc[mask_no_struct, "disqualified"]      = True
df.loc[mask_no_struct, "disqualify_reason"] = "no_recent_bos_choch"

