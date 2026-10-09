"""
TSL FLASH — Radar.

See docs/TSL_FLASH.md. Two modes over one facts engine; this package is the
Radar mode and the facts engine both.

IMPORTS NOTHING FROM atlas/. The only cross-package dependency is
engine/universe.py (the Nifty 500 list and sector map) and engine/
trading_calendar.py, both read-only. Radar consumes the signals chain's
bhavcopy output from disk and writes only radar_* tables.
"""
