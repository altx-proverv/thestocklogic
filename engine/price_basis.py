"""
THE STOCK LOGIC — PRICE BASIS: when a series stops being comparable
====================================================================

ONE DETECTOR, TWO CONSUMERS. radar/levels.py gates every multi-session level on
this, and engine/02b_smc_signals.py + engine/03b_score.py gate the published
levels and the enterable universe on it. It lives in engine/ because that is the
lower layer -- radar reads engine, never the reverse.

THE PARQUETS ARE NOT SPLIT-ADJUSTED, and that makes every multi-session level
wrong across a corporate action rather than slightly off. INDIAGLYCO split 1:5
on 2026-09-02: its 52-week high is a pre-split 1219.00 against a post-split
close of 382.70, and radar would have put "52-week high, +218.53% from here" on
a card. That is not a rounding error. It is a fabricated number with exactly the
right shape to be believed.

── WHAT THIS REPLACED, AND WHY IT WAS WRONG ──────────────────────

The old test compared prev_close[i] against close[i-1] and called the result a
corporate action, on the premise that "NSE ADJUSTS PREV_CLOSE on the ex-date
while the previous row's CLOSE stays unadjusted". That premise is false. The
audit of 2026-10-22 measured it across the whole universe:

    split/bonus events in the last 252 sessions                    31
    of those, where NSE adjusted prev_close on the ex-date          0

NSE's PrvsClsgPric is the previous session's traded close, full stop. On
INDIAGLYCO's ex-date it read 1111.70 against a prior close of 1111.70 --
identical, so the test could not fire, and `corporate_actions()` returned [] for
every real split in the book.

It appeared to work for exactly one symbol, and by accident. Eleven sessions
(02-16 Sep, while INDIAGLYCO sat in the BE trade-to-trade series) were missing
from the parquet until INGEST_SERIES accepted BE on 15 October. The test was
comparing 17 Sep's correct prev_close (266.20) against 01 Sep's close (1111.70)
across an eleven-session hole, and reading the hole as an adjustment. Filling
the hole removed the only detection it ever had, which is how this surfaced.

prev_close vs the prior close is a FEED-CONTINUITY test. It is kept, because it
is a good one, under a name that says what it does.

── THE MECHANISM THAT DOES SEPARATE THEM ─────────────────────────

A corporate action changes the price BASIS overnight, so the entire move is
already in the FIRST PRINT of the ex-date. A crash happens during the session:
it opens near the prior close and falls. The difference is WHERE the gap sits,
and it needs no list of expected ratios -- a whitelist of 1:2, 1:5, 1:10 would
miss the next unusual one, and the realised ratio is approximate anyway because
the stock trades freely from the opening tick.

THE THRESHOLD IS THE PRICE BAND, not a chosen number. NSE's widest band is 20%:
an OPENING print more than 20% away from the prior close cannot be reached by
trading, because the band would have stopped it. Measured against the field:

    the 31 known splits, shallowest gap   TRENT      open -33.5%  (2:3)
    the one in-window crash              POLICYBZR  open -10.0%,
                                         falling intraday to -36.0% at the close
    whole-history up-gaps beyond +25%    2 (FORCEMOT +31.3%, IFCI +29.0%)

At 20% all 31 are caught and POLICYBZR is not, because POLICYBZR's gap is not in
its opening print. The margin is wide either side: the nearest split sits 13
points beyond the threshold and the crash 10 points inside it.

WHAT THIS STILL CANNOT SEE, stated plainly rather than discovered later: an
action whose ratio is SHALLOWER than the band is indistinguishable from an
ordinary overnight gap by this method. A 1:10 bonus moves price about -9% and is
NOT detected. Closing that needs NSE's corporate-actions feed, where the ratio
is a known fact instead of an inference. This detector is the floor.

AND IT OVER-FLAGS BY DESIGN. Two F&O names, BRIGADE (-22.3%) and CUB (-23.1%),
gap beyond the cash band because F&O stocks have dynamic bands rather than the
20% cap. Both held their new basis for the following ten sessions, so both are
treated as basis changes. If either is in fact an ordinary gap the cost is one
symbol suppressed; the cost of the opposite error is a fabricated level on a
public page and a corrupt score. Over-flagging is the safe direction and is
chosen deliberately.
"""

import logging

import pandas as pd

log = logging.getLogger(__name__)

# NSE's widest price band. See the derivation above: this is a mechanism, not a
# tuned parameter, and it is a module constant rather than a config key so that
# nobody can turn it off to make a card render.
CORP_ACTION_GAP_PCT = 20.0

# The feed-continuity tolerance. Unchanged in value from the constant it
# replaces (CORP_ACTION_TOLERANCE_PCT); only its name and meaning are corrected.
# prev_close and the prior close agree exactly on an ordinary session, so
# anything this large means our own record is wrong.
FEED_CONTINUITY_TOLERANCE_PCT = 15.0


def _col(df, name):
    return df[name].astype(float).to_numpy()


def corporate_actions(df: pd.DataFrame) -> list:
    """Dates where the PRICE BASIS changed overnight. [] when clean.

    Two conditions, both required, and together they are the mechanism:

      the OPEN gapped beyond the band    the move was there before a single
                                         share traded, so trading cannot have
                                         produced it
      the CLOSE stayed outside it        the new basis held for the session, so
                                         it was not one bad opening print

    SYMMETRIC, because a reverse split or consolidation gaps UP by the same
    mechanism. The universe's whole history holds two up-gaps beyond +25% and
    none beyond +50%, so the upward arm costs almost nothing, and declining to
    look would be a blind spot by choice.
    """
    if df is None or len(df) < 2 or not {"open", "close"} <= set(df.columns):
        return []
    close, open_ = _col(df, "close"), _col(df, "open")
    band = CORP_ACTION_GAP_PCT / 100.0
    out = []
    # Plain loop with explicit zero guards. The vectorised form needed
    # mode.use_inf_as_na to stop a division by a zero prior close becoming inf,
    # and pandas 3 removed that option -- the box runs pandas 3.0.3, so a clever
    # one-liner here is a local pass and a production crash. Three of those have
    # shipped from this repo already.
    for i in range(1, len(df)):
        prior, o, c = close[i - 1], open_[i], close[i]
        if not prior or pd.isna(prior) or pd.isna(o) or pd.isna(c) or not o:
            continue
        if abs(o / prior - 1.0) > band and abs(c / prior - 1.0) > band:
            out.append(df["date"].iloc[i])
    return out


def feed_discontinuities(df: pd.DataFrame) -> list:
    """Dates where OUR SERIES disagrees with the feed about the prior close.

    NOT a corporate-action test -- see the module docstring for the month that
    cost. prev_close[i] and close[i-1] are the same quantity twice: the previous
    session's close as the feed reports it, and as we stored it. They agree on
    an ordinary session and on an ex-date alike.

    They disagree when OUR record is wrong, which is worth catching on its own:

      a session missing from the parquet   INDIAGLYCO's 11 BE-series days
      a mis-dated bhavcopy                 cm14Sep2026bhav.csv holds 11-Sep
                                           data, so 2026-09-14 is a duplicate
                                           bar carrying another session's
                                           prev_close

    A level computed across either is unusable, so this gates levels too.
    """
    if df is None or len(df) < 2 or "prev_close" not in df.columns:
        return []
    close, prev = _col(df, "close"), _col(df, "prev_close")
    out = []
    for i in range(1, len(df)):
        prior, p = close[i - 1], prev[i]
        if not prior or pd.isna(prior) or pd.isna(p):
            continue
        if abs(p / prior - 1.0) * 100.0 > FEED_CONTINUITY_TOLERANCE_PCT:
            out.append(df["date"].iloc[i])
    return out


def discontinuities(df: pd.DataFrame) -> list:
    """Every date across which this series cannot be compared.

    The union of the two tests, because a level is equally meaningless over a
    changed price basis and over a hole in the record. Callers gate on this;
    nothing should gate on one test and forget the other, which is the shape of
    the original defect.
    """
    seen, out = set(), []
    for d in list(corporate_actions(df)) + list(feed_discontinuities(df)):
        if d not in seen:
            seen.add(d)
            out.append(d)
    return sorted(out)


def spans_discontinuity(df: pd.DataFrame, sessions: int) -> bool:
    """Does the trailing `sessions` window cross a discontinuity? Never raises.

    True means every level over that window must be SUPPRESSED -- published as
    NULL, not scaled and not replaced by a neutral stand-in. Adjusting the
    series ourselves would mean inventing a ratio from a price gap, and
    substituting a middle value is still a number the scorer would act on.
    """
    try:
        if df is None or len(df) < 2:
            return False
        window = df.iloc[-int(sessions):] if sessions < len(df) else df
        if len(window) < 2:
            return False
        first = window["date"].iloc[0]
        return any(d >= first for d in discontinuities(df))
    except Exception as e:
        # FAILS CLOSED. If the series cannot be examined we cannot certify the
        # window as comparable, and the whole point of this module is to refuse
        # rather than to guess.
        log.error(f"could not examine the series for discontinuities ({e}) — "
                  f"treating the window as NOT comparable")
        return True
