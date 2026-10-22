"""
TSL FLASH — Radar.

See docs/TSL_FLASH.md. Two modes over one facts engine; this package is the
Radar mode and the facts engine both.

IMPORTS NOTHING FROM atlas/. The cross-package dependencies are
engine/universe.py (the Nifty 500 list and sector map), engine/
trading_calendar.py, and engine/price_basis.py (the one corporate-action and
feed-continuity detector, shared with the signals chain so a split cannot be
refused on a card and acted on in a signal) -- all three read-only. Radar
consumes the signals chain's bhavcopy output from disk and writes only radar_*
tables.
"""
