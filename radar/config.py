"""
Radar's knobs: defaults here, overrides from radar_config.

WHY DEFAULTS LIVE IN CODE AS WELL AS IN THE TABLE. The table is the point --
thresholds change without a deploy -- but a run that cannot reach Supabase must
still be runnable, and a replay over historical sessions must be reproducible
without depending on what the table happens to hold tonight. So: code holds the
shipped values, the table overrides them, and load() logs every value it
actually used along with where it came from.

That last part is not decoration. A config edit that silently did not take
effect is indistinguishable, from the output alone, from a threshold that does
not do what you think it does.
"""

from __future__ import annotations

import os
import logging

log = logging.getLogger("RADAR-CONFIG")

# ── SHIPPED DEFAULTS ──────────────────────────────────────────────
#
# The twelve from the brief, then the seven added in docs/TSL_FLASH.md §12.
# Kept in two blocks so the addition stays visible here too.
DEFAULTS = {
    "universe":                     "nifty500",
    "shock_1d_pct":                 8.0,
    "shock_1d_vol_mult":            3.0,
    "shock_5d_pct":                 15.0,
    "shock_5d_vol_mult":            2.0,
    "scanner_vol_mult":             2.0,
    "scanner_top_n_each_side":      10,
    "hot_cards_n":                  10,
    "heat_half_life_sessions":      5.0,
    "outcome_window_trading_days":  60,
    "base_rate_min_n":              20,
    "swing_fractal_bars":           5,

    # Added beyond the brief -- see docs/TSL_FLASH.md §12 for why each one.
    "event_dedup_sessions":         5,
    "vol_avg_window":               20,
    "filing_lookback_sessions":     5,
    "circuit_bands_pct":            "2,5,10,20",
    "ipo_max_age_years":            5,
    "rsi_period":                   14,
    "dma_periods":                  "50,200",
}

# Which keys are which type. A table holds text; without this, "10" would be
# compared against 10 and `vol >= mult` would be a string comparison that is
# silently always False.
_INT_KEYS = {"scanner_top_n_each_side", "hot_cards_n",
             "outcome_window_trading_days", "base_rate_min_n",
             "swing_fractal_bars", "event_dedup_sessions", "vol_avg_window",
             "filing_lookback_sessions", "ipo_max_age_years", "rsi_period"}
_FLOAT_KEYS = {"shock_1d_pct", "shock_1d_vol_mult", "shock_5d_pct",
               "shock_5d_vol_mult", "scanner_vol_mult",
               "heat_half_life_sessions"}


def _coerce(key: str, raw):
    """Text from the table -> the type the arithmetic needs. Unparseable keeps
    the default and says so; it does not become 0, which would turn a threshold
    into 'everything qualifies'."""
    if key in _INT_KEYS:
        try:
            return int(float(str(raw).strip()))
        except Exception:
            log.warning(f"radar_config.{key}={raw!r} is not an integer — "
                        f"using the default {DEFAULTS[key]!r}")
            return DEFAULTS[key]
    if key in _FLOAT_KEYS:
        try:
            return float(str(raw).strip())
        except Exception:
            log.warning(f"radar_config.{key}={raw!r} is not a number — "
                        f"using the default {DEFAULTS[key]!r}")
            return DEFAULTS[key]
    return str(raw).strip()


def csv_floats(value) -> list:
    """'2,5,10,20' -> [2.0, 5.0, 10.0, 20.0]. Used by circuit_bands_pct and
    dma_periods, both of which are lists in a text column."""
    out = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            log.warning(f"ignoring unparseable list element {part!r}")
    return out


class Config(dict):
    """Attribute access over the loaded values, plus the provenance of each."""

    def __init__(self, values: dict, sources: dict):
        super().__init__(values)
        self.sources = sources

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    @property
    def circuit_bands(self) -> list:
        return csv_floats(self["circuit_bands_pct"])

    @property
    def dma_list(self) -> list:
        return [int(x) for x in csv_floats(self["dma_periods"])]

    def log_table(self) -> str:
        lines = ["config in force this run:"]
        for k in sorted(self):
            lines.append(f"    {k:<30} {str(self[k]):<12} "
                         f"({self.sources.get(k, 'default')})")
        return "\n".join(lines)


def load(use_table: bool = True) -> Config:
    """DEFAULTS, overridden by radar_config. Never raises.

    use_table=False is what the replay and the tests run with, so a historical
    re-score is reproducible from the code alone rather than from whatever the
    operator last typed into the table.
    """
    values = dict(DEFAULTS)
    sources = {k: "default" for k in values}

    if not use_table:
        return Config(values, sources)

    url = os.environ.get("SUPABASE_URL", "https://eibdlcanpudjgmkjxrga.supabase.co")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        log.warning("no SUPABASE_SERVICE_KEY — running on shipped defaults, "
                    "radar_config was NOT consulted")
        return Config(values, sources)

    try:
        import requests
        r = requests.get(f"{url}/rest/v1/radar_config?select=key,value",
                         headers={"apikey": key, "Authorization": f"Bearer {key}"},
                         timeout=20)
        if r.status_code != 200:
            log.warning(f"radar_config read failed: HTTP {r.status_code} "
                        f"{r.text[:120]} — running on shipped defaults")
            return Config(values, sources)
        rows = r.json() or []
    except Exception as e:
        log.warning(f"radar_config unreachable ({type(e).__name__}: {e}) — "
                    f"running on shipped defaults")
        return Config(values, sources)

    for row in rows:
        k = str(row.get("key", "")).strip()
        if k not in DEFAULTS:
            # A key in the table that the code does not know about. Named, not
            # ignored: it is usually a typo in the key, which would otherwise
            # look like an edit that had no effect.
            log.warning(f"radar_config.{k!r} is not a key Radar reads — ignored")
            continue
        values[k] = _coerce(k, row.get("value"))
        sources[k] = "radar_config"

    missing = [k for k in DEFAULTS if sources[k] == "default"]
    if missing:
        log.info(f"{len(missing)} key(s) not in radar_config, using defaults: "
                 f"{', '.join(sorted(missing))}")
    return Config(values, sources)
