"""
ATLAS GTT -- read and cancel only
=================================
ATLAS NO LONGER PLACES GTT TRIGGERS. Signals publish only when price is already
within 0.30% of the zone (engine/zone_entry.MAX_ENTRY_DIST_PCT), so the entry is
a MARKET order taken immediately. Nothing rests and nothing waits for a retest.

Why the resting path went: with the old 8% publish gate, triggers sat 4-8% from
price and committed cash for days against a fill that mostly never came. On
2026-08-12 the four resting GTTs were 4.7%, 6.8%, 4.7% and 7.7% away.

What remains is read and cancel:
  - list_active_gtts()  every active GTT, whoever placed it. The funds check
                        needs this: a resting BUY commits cash regardless of
                        origin, including ones the operator placed by hand.
  - list_atlas_gtts()   those matched to an atlas_trades row by trigger id.
  - cancel_gtt()        operator cleanup.

BROKER FACTS worth keeping (verified against Zerodha, Aug 2026)
---------------------------------------------------------------
  - A GTT stays live for ONE YEAR or until triggered. It does NOT expire daily,
    so an abandoned trigger keeps committing cash indefinitely.
  - Kite's get_gtts() response does NOT echo the order `tag` back -- the field
    is absent, not null. Ownership can only be established via a recorded
    trigger id. This cost a day of confusion; do not reintroduce tag matching.
  - Max 500 active GTTs per account.
"""

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("ATLAS-GTT")
IST = timezone(timedelta(hours=5, minutes=30))

GTT_TAG = "ATLAS"   # still sent on any manual placement; Kite discards it


# place_zone_gtt() removed. ATLAS does not place GTT triggers any more --
# signals publish only when price is already within 0.30% of the zone, so the
# entry is a MARKET order via broker.place_order(). Leaving an unused
# order-placing function in the tree is the same hazard as the retired
# trade_executor: no caller today, one import away from being live again.
#
# The read and cancel helpers below stay. Any GTT resting at the broker still
# commits cash and must be visible to the funds check, whoever placed it.


def list_active_gtts() -> list:
    """EVERY active GTT on the account, unfiltered.

    This is what the funds check must use. A resting BUY commits cash whoever
    placed it -- ATLAS, or the operator by hand in Kite -- so "is it ours?" is
    the wrong question when asking what money is spoken for.
    """
    from atlas.execution.broker import get_kite
    kite = get_kite()
    if not kite:
        return []
    try:
        return [g for g in (kite.get_gtts() or []) if g.get("status") == "active"]
    except Exception as e:
        log.warning(f"GTT list failed: {e}")
        return []


def list_atlas_gtts() -> list:
    """Active GTTs ATLAS placed, matched by trigger id recorded in atlas_trades.

    It used to match on the order `tag`. That can never work: Kite's get_gtts()
    response omits the tag field entirely -- not null, absent -- so
    (o.get("tag") or "") was always "" and this returned []. GTT 331263278
    (GRASIM, 2026-08-11) was invisible to ATLAS for exactly this reason while
    resting live at the broker.

    The authoritative link is atlas_trades.gtt_trigger_id.
    """
    import requests
    from atlas.config import SUPABASE_URL, SUPABASE_KEY

    active = list_active_gtts()
    if not active:
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?gtt_trigger_id=not.is.null&select=gtt_trigger_id",
            headers={"apikey": SUPABASE_KEY,
                     "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10)
        if r.status_code != 200:
            log.warning(f"could not read recorded trigger ids: HTTP {r.status_code}")
            return []
        known = {str(x["gtt_trigger_id"]) for x in r.json() if x.get("gtt_trigger_id")}
    except Exception as e:
        log.warning(f"could not read recorded trigger ids: {e}")
        return []

    return [g for g in active if str(g.get("id")) in known]


def cancel_gtt(trigger_id: int) -> dict:
    """Cancel one GTT. Used for manual cleanup, not on a schedule --
    daily expiry is handled by the operator."""
    from atlas.execution.broker import get_kite
    kite = get_kite()
    if not kite:
        return {"success": False, "reason": "Kite not initialized"}
    try:
        kite.delete_gtt(trigger_id=trigger_id)
        log.info(f"GTT cancelled: {trigger_id}")
        return {"success": True, "trigger_id": trigger_id}
    except Exception as e:
        log.error(f"GTT cancel failed for {trigger_id}: {e}")
        return {"success": False, "reason": str(e)}


def gtt_summary() -> str:
    """Telegram-friendly summary of pending ATLAS GTTs."""
    gtts = list_atlas_gtts()
    if not gtts:
        return "No pending ATLAS GTT triggers."
    lines = [f"<b>Pending GTT triggers ({len(gtts)})</b>"]
    for g in gtts:
        cond = g.get("condition", {}) or {}
        sym = cond.get("tradingsymbol", "?")
        trig = (cond.get("trigger_values") or [0])[0]
        qty = sum(int(o.get("quantity", 0)) for o in (g.get("orders") or []))
        lines.append(f"{sym}: {qty} @ trigger Rs{trig}")
    return "\n".join(lines)
