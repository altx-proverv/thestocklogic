"""
BACKTEST — rolling-window replication
=====================================
Reproduces the dashboard's window exactly, so a backtest figure and a dashboard
figure mean the same thing.

THE DEFINITION IS NOT THE OBVIOUS ONE, and getting it wrong is how the same
data reads as 239, 278 or 289 resolved. From v_signal_window:

    win  = SELECT DISTINCT mark_date FROM signal_marks ORDER BY DESC LIMIT 20
    pub  = SELECT DISTINCT ON (signal_date, symbol, direction) ...
             WHERE signal_date >= (SELECT min(mark_date) FROM win)
             ORDER BY signal_date, symbol, direction, mark_date DESC

Three traps, each of which cost a wrong number today:

  1. Membership is `signal_date >= window_start` with NO upper bound. Bounding
     it on both sides drops signals that are still being marked.
  2. The row taken per signal is the LATEST mark, not the earliest.
     `resolution` is backfilled onto a signal's mark rows, so the earliest row
     reads NULL for signals that are in fact resolved -- 41 of them.
  3. The window is 20 distinct MARK dates, not 20 calendar or signal dates.

BOTH BASES ARE REPORTED, never one. all-publications answers "of everything we
published, how did it do". first-signal-only answers "what would the book have
made", because Gate 3b hard-skips a symbol already held, so republications are
positions the agent is built not to take. Reporting either alone invites the
reader to treat it as the other.
"""

import pandas as pd

KEY = ["signal_date", "symbol", "direction"]


def window_bounds(mark_dates: list, window_end: str, window_days: int = 20):
    """(window_start, window_end) for the 20 distinct mark dates ending there."""
    md = sorted(mark_dates)
    if window_end not in md:
        raise ValueError(f"{window_end} is not a mark date; "
                         f"available {md[0]}..{md[-1]}")
    i = md.index(window_end)
    if i + 1 < window_days:
        raise ValueError(f"only {i+1} mark dates on or before {window_end}, "
                         f"need {window_days}")
    win = md[i - window_days + 1: i + 1]
    return win[0], win[-1]


def publications(marks: pd.DataFrame, window_start: str, window_end: str) -> pd.DataFrame:
    """One row per published signal in the window, carrying its LATEST mark."""
    hist = marks[marks["mark_date"] <= window_end]
    inwin = hist[hist["signal_date"] >= window_start]
    return (inwin.sort_values("mark_date")
                 .groupby(KEY, as_index=False)
                 .last())


def add_first_signal_flag(pub: pd.DataFrame) -> pd.DataFrame:
    """
    is_first_signal: earliest signal_date per (symbol, direction) IN THE WINDOW.

    Boundary caveat carried over from the live view: a signal whose earlier
    publication has already aged out is flagged first here, because the window
    cannot see past its own edge. It resolves itself as the window rolls.
    """
    pub = pub.copy()
    first = (pub.sort_values("signal_date")
                .drop_duplicates(subset=["symbol", "direction"], keep="first")
                .set_index(KEY).index)
    pub["is_first_signal"] = pd.MultiIndex.from_frame(pub[KEY]).isin(first)
    return pub


def summarise(pub: pd.DataFrame, first_only: bool = False) -> dict:
    """The figures the dashboard shows, for one basis."""
    base = pub[pub["is_first_signal"]] if first_only else pub
    res = base[base["resolution"].notna()]
    counts = res["resolution"].value_counts().to_dict()

    out = {
        "n_signals":  int(len(base)),
        "resolved":   int(len(res)),
        "unresolved": int(len(base) - len(res)),
        "STOP":       int(counts.get("STOP", 0)),
        "TARGET":     int(counts.get("TARGET", 0)),
        "SAME_DAY":   int(counts.get("SAME_DAY", 0)),
        "EXPIRED":    int(counts.get("EXPIRED", 0)),
    }
    out["hit_rate_pct"] = (round(100.0 * out["TARGET"] / out["resolved"], 1)
                           if out["resolved"] else 0.0)
    out["total_sum_pct"] = round(float(res["resolved_pnl_pct"].sum()), 3)

    for d, label in (("LONG", "long"), ("SHORT", "short")):
        g = res[res["direction"] == d]
        out[f"{label}_n"] = int(len(g))
        out[f"{label}_sum_pct"] = round(float(g["resolved_pnl_pct"].sum()), 3)
    return out


def by_setup(pub: pd.DataFrame, first_only: bool = False) -> pd.DataFrame:
    """Per-setup breakdown, same shape as v_setup_window."""
    base = pub[pub["is_first_signal"]] if first_only else pub
    rows = []
    for setup, g in base.groupby("setup_name", dropna=False):
        r = g[g["resolution"].notna()]
        c = r["resolution"].value_counts().to_dict()
        rows.append({
            "setup_name": setup,
            "n_signals":  len(g),
            "n_resolved": len(r),
            "n_target":   c.get("TARGET", 0),
            "n_stop":     c.get("STOP", 0),
            "n_same_day": c.get("SAME_DAY", 0),
            "hit_rate_pct": round(100.0 * c.get("TARGET", 0) / len(r), 1) if len(r) else 0.0,
            "resolved_pct": round(float(r["resolved_pnl_pct"].sum()), 3),
        })
    return pd.DataFrame(rows).sort_values("n_signals", ascending=False)
