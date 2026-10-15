"""
IS THIS POSITION PROTECTED? ASK THE BROKER.
===========================================

ONE SOURCE OF TRUTH for "protected", shared by the entry path, the pre-close
handover, the per-cycle repair and the four scheduled reports. It existed twice
before this file -- once in atlas/reporting/session_report.py and once as the
`exit_legs` column -- and the two disagreed about ATHERENERG on 2026-10-15.

THE RECORDED exit_legs COLUMN IS NOT EVIDENCE. On 2026-10-15 ATHERENERG's
exit_legs was `{}` for exactly the reason the position needed protecting: the
placement failed, so nothing was recorded. A repair that reads exit_legs to
decide whether to act concludes "nothing to do" from the fingerprint of the
failure it is supposed to fix. So every decision here reads orders() and
get_gtts() and nothing else.

THREE VERDICTS, AND "UNKNOWN" IS NOT "PROTECTED":

    CONFIRMED   read back from the broker -- a resting SL order, or an active
                GTT trigger on the correct side of the entry
    NONE        the broker was readable and there is nothing there
    UNKNOWN     the broker could not be read

UNKNOWN never authorises inaction. A repair refuses to place into an unreadable
broker (it cannot tell a missing stop from an unreadable one, and placing a
second stop on top of a live one is its own incident) -- but it alerts, and the
reports count UNKNOWN as unprotected.

WHICH LEG IS WHICH is decided by where a trigger sits relative to the ENTRY,
never by leg order: a long's stop is below and its target above, and the trail
modifies one leg of a two-leg GTT, so position in the list means nothing.

NOTHING HERE DECIDES WHETHER TO TRADE. It reports, and it repairs when asked.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# An SL order resting at the broker is one of these. ANYTHING ELSE IS NOT
# PROTECTION: COMPLETE means it already fired, CANCELLED and REJECTED mean it
# is not there -- and REJECTED is what 2026-10-15 produced four times while
# every mechanism reported success.
LIVE_ORDER_STATUS = ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")

CONFIRMED, NONE, UNKNOWN = "CONFIRMED", "NONE", "UNKNOWN"

# HOW OFTEN AN UNPROTECTED POSITION RE-ALERTS. 900s = 15 minutes, the same
# value as market_open.ALERT_COOLDOWN_SECONDS, deliberately: the operator
# already knows that cadence from every other repeating alert in the system.
# A single alert that then goes quiet is how ATHERENERG sat naked for a day.
UNPROTECTED_REPEAT_SECONDS = 900

REASON_UNPROTECTED = "OPEN_WITHOUT_PROTECTION"

_STATE_DIR = Path(os.environ.get("ATLAS_STATE_DIR", "/var/lib/atlas"))
UNPROTECTED_FILE = Path(os.environ.get(
    "ATLAS_UNPROTECTED_FILE", str(_STATE_DIR / "unprotected.json")))


def _now_ist():
    return datetime.now(IST)


# ══════════════════════════════════════════════════════════════════
# WHAT THE BROKER SAYS
# ══════════════════════════════════════════════════════════════════

def broker_protection(kite=None, orders=None, gtts=None) -> dict:
    """-> {"ok", "why", "orders": {sym: [...]}, "gtts": {sym: [...]}}

    `orders` and `gtts` may be passed in by a caller that has already fetched
    them this cycle -- reconcile_exits does -- so a repair costs no extra
    broker request.
    """
    out = {"ok": False, "why": "", "orders": {}, "gtts": {}}

    if orders is None or gtts is None:
        if kite is None:
            try:
                from atlas.execution.broker import get_kite
                kite = get_kite()
            except Exception as e:
                out["why"] = f"could not reach the broker: {type(e).__name__}: {e}"
                return out
        if kite is None:
            out["why"] = "no broker session (token missing or expired)"
            return out

    try:
        book = orders if orders is not None else kite.orders()
        for o in book or []:
            if str(o.get("status", "")).upper() not in LIVE_ORDER_STATUS:
                continue
            if str(o.get("order_type", "")).upper() not in ("SL", "SL-M"):
                continue
            out["orders"].setdefault(o.get("tradingsymbol"), []).append({
                "order_id": o.get("order_id"),
                "trigger": o.get("trigger_price"),
                "price": o.get("price"),
                "qty": o.get("quantity"),
                "type": o.get("order_type"),
                "status": o.get("status"),
                "side": o.get("transaction_type")})
    except Exception as e:
        out["why"] = f"orders unreadable: {type(e).__name__}: {e}"
        return out

    try:
        tl = gtts if gtts is not None else kite.get_gtts()
        for g in tl or []:
            if str(g.get("status", "")).lower() != "active":
                continue
            cond = g.get("condition") or {}
            out["gtts"].setdefault(cond.get("tradingsymbol"), []).append({
                "trigger_id": g.get("id"),
                "type": g.get("type"),
                "triggers": cond.get("trigger_values") or [],
                "last_price": cond.get("last_price"),
                "legs": [{"price": l.get("price"), "qty": l.get("quantity"),
                          "side": l.get("transaction_type")}
                         for l in (g.get("orders") or [])]})
    except Exception as e:
        out["why"] = f"GTTs unreadable: {type(e).__name__}: {e}"
        return out

    out["ok"] = True
    return out


def confirm_legs(trade: dict, prot: dict) -> dict:
    """Is this trade's stop confirmed live, and its target?

    -> {"stop", "target", "detail", "gtt_ids", "stop_at", "target_at",
        "has_sl_order", "has_gtt"}
    """
    sym = trade.get("symbol")
    entry = float(trade.get("entry_price") or 0)
    long_ = str(trade.get("direction") or "").upper() == "LONG"

    if not prot.get("ok"):
        return {"stop": UNKNOWN, "target": UNKNOWN, "detail": prot.get("why", ""),
                "gtt_ids": [], "stop_at": None, "target_at": None,
                "has_sl_order": False, "has_gtt": False}

    stop_at = target_at = None
    ids, bits = [], []

    sl_orders = prot["orders"].get(sym, [])
    for o in sl_orders:
        if stop_at is None:
            stop_at = o["trigger"]
        bits.append(f"SL order {o['order_id']} trigger Rs{o['trigger']}")

    gtts = prot["gtts"].get(sym, [])
    for g in gtts:
        ids.append(g["trigger_id"])
        for t in g["triggers"]:
            t = float(t)
            # WHERE IT SITS RELATIVE TO THE ENTRY, not which slot it is in.
            is_stop = (t < entry) if long_ else (t > entry)
            if is_stop:
                if stop_at is None:
                    stop_at = t
                bits.append(f"GTT {g['trigger_id']} stop trigger Rs{t}")
            else:
                if target_at is None:
                    target_at = t
                bits.append(f"GTT {g['trigger_id']} target trigger Rs{t}")

    return {"stop": CONFIRMED if stop_at is not None else NONE,
            "target": CONFIRMED if target_at is not None else NONE,
            "detail": "; ".join(bits), "gtt_ids": ids,
            "stop_at": stop_at, "target_at": target_at,
            "has_sl_order": bool(sl_orders), "has_gtt": bool(gtts)}


def is_protected(trade: dict, prot: dict) -> bool:
    """A stop CONFIRMED at the broker. UNKNOWN is not protected."""
    return confirm_legs(trade, prot)["stop"] == CONFIRMED


# ══════════════════════════════════════════════════════════════════
# THE REGISTER OF UNPROTECTED POSITIONS -- and the repeating alert
# ══════════════════════════════════════════════════════════════════
#
# WHY A FILE. The alert has to repeat across processes: the entry that could
# not be protected happens in one cycle, and the repeat has to still fire an
# hour later in a cycle that knows nothing about it -- and after a restart.
# Keyed to the session date so a fresh morning starts with an empty register:
# no stand-down and no alert state survives the close.

def _load() -> dict:
    day = _now_ist().date().isoformat()
    blank = {"session_date": day, "positions": {}, "seen_today": 0}
    try:
        if UNPROTECTED_FILE.exists():
            d = json.loads(UNPROTECTED_FILE.read_text())
            if d.get("session_date") == day:
                return {**blank, **d}
            log.info(f"unprotected register is from {d.get('session_date')}, "
                     f"today is {day} — starting fresh")
    except Exception as e:
        # FAIL EMPTY, and say so. An unreadable register cannot prove a
        # position is unprotected; the next broker read re-derives the truth
        # and re-registers it. Losing the COUNT is the cost, not the alert.
        log.error(f"could not read {UNPROTECTED_FILE} ({e}) — register empty "
                  f"this pass; the broker read will repopulate it")
    return blank


def _save(d: dict) -> None:
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = False, ""
    if drilling and str(UNPROTECTED_FILE) == "/var/lib/atlas/unprotected.json":
        log.warning(f"DRILL ({why}) — refusing to write the live unprotected "
                    f"register at {UNPROTECTED_FILE}")
        return
    try:
        UNPROTECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = UNPROTECTED_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(UNPROTECTED_FILE)
    except Exception as e:
        log.error(f"could not persist the unprotected register: {e}")


def register(trade: dict, why: str, wanted_stop=None) -> dict:
    """Record that this position is open with no protection. -> the entry.

    The COUNT ("1 of 1 today") is the operator's signal, so it counts every
    position registered today and is never decremented by a later repair: two
    positions that both failed and were both fixed is still a day on which
    protection failed twice, and hiding that is the opposite of the point.
    """
    d = _load()
    key = str(trade.get("id") or trade.get("symbol"))
    if key not in d["positions"]:
        d["seen_today"] = int(d.get("seen_today") or 0) + 1
        d["positions"][key] = {
            "symbol": trade.get("symbol"),
            "direction": trade.get("direction"),
            "qty": trade.get("qty"),
            "entry_price": trade.get("entry_price"),
            "wanted_stop": wanted_stop if wanted_stop is not None
                           else trade.get("stop_price"),
            "why": str(why)[:400],
            "since": _now_ist().isoformat(),
            "last_alert": 0,
            "ordinal": d["seen_today"],
        }
        _save(d)
    return d["positions"][key]


def resolve(key) -> bool:
    """Protection is now confirmed -- stop repeating for this one."""
    d = _load()
    k = str(key)
    if k in d.get("positions", {}):
        d["positions"].pop(k, None)
        _save(d)
        return True
    return False


def outstanding() -> dict:
    return dict(_load().get("positions", {}))


def seen_today() -> int:
    return int(_load().get("seen_today") or 0)


def due_alerts(now=None) -> list:
    """Which registered positions are due to re-alert. Marks them sent."""
    now_ts = (now or _now_ist()).timestamp()
    d = _load()
    due = []
    changed = False
    for k, p in (d.get("positions") or {}).items():
        if now_ts - float(p.get("last_alert") or 0) >= UNPROTECTED_REPEAT_SECONDS:
            p["last_alert"] = now_ts
            due.append((k, p))
            changed = True
    if changed:
        _save(d)
    return due


def alert_text(p: dict, total: int) -> str:
    """The deviation alert. Names everything an operator needs to act."""
    return (
        f"🔴 <b>UNPROTECTED POSITION — MANUAL STOP NEEDED</b>\n"
        f"<b>{p.get('symbol')}</b> {p.get('direction')} {p.get('qty')} "
        f"@ Rs{float(p.get('entry_price') or 0):,.2f}\n"
        f"wanted stop  Rs{float(p.get('wanted_stop') or 0):,.2f}\n"
        f"why it failed  <code>{str(p.get('why'))[:300]}</code>\n"
        f"open since  {str(p.get('since'))[11:19]} IST\n\n"
        f"<b>unprotected position {p.get('ordinal')} of {total} today.</b>\n"
        f"ATLAS took the trade deliberately and is NOT standing the day down. "
        f"Place a stop by hand. This repeats every "
        f"{UNPROTECTED_REPEAT_SECONDS // 60} minutes until a stop is confirmed "
        f"at the broker. Send /pause if you want ATLAS to stop taking entries."
    )


def sweep(open_trades: list, prot: dict = None, send=None, now=None) -> dict:
    """Re-check the register against the broker, then fire what is due.

    CALLED EVERY CYCLE. Two jobs: drop anything now protected (the operator
    placed a stop by hand, or a repair worked), and re-alert the rest.
    """
    out = {"checked": 0, "resolved": 0, "alerted": 0, "outstanding": 0}
    if send is None:
        from atlas.reporting.telegram import send as _s
        send = _s
    reg = outstanding()
    if not reg:
        return out
    if prot is None:
        prot = broker_protection()

    by_key = {str(t.get("id") or t.get("symbol")): t for t in (open_trades or [])}
    for key, p in list(reg.items()):
        out["checked"] += 1
        t = by_key.get(key) or {"symbol": p.get("symbol"),
                                "direction": p.get("direction"),
                                "entry_price": p.get("entry_price")}
        # NOT IN THE OPEN SET AT ALL means it was closed -- by the operator, or
        # by a stop they placed that then fired. Either way it is no longer an
        # unprotected open position.
        if key not in by_key:
            resolve(key)
            out["resolved"] += 1
            continue
        if prot.get("ok") and is_protected(t, prot):
            resolve(key)
            out["resolved"] += 1
            log.warning(f"{p.get('symbol')}: protection is now confirmed at the "
                        f"broker — no longer alerting")
            continue

    total = seen_today()
    for key, p in due_alerts(now):
        try:
            send(alert_text(p, total))
            out["alerted"] += 1
        except Exception as e:
            log.error(f"could not send the unprotected alert for "
                      f"{p.get('symbol')}: {e}")
    out["outstanding"] = len(outstanding())
    return out


# ══════════════════════════════════════════════════════════════════
# THE REPAIR — place protection on an OPEN position that has none
# ══════════════════════════════════════════════════════════════════
#
# THE HOLE THIS CLOSES. Before 2026-10-16 protection was placed in exactly two
# places: the entry path, at the instant of a fill, and preclose_gtt.handover(),
# gated on `entry_date == today`. A position whose handover failed was `older`
# the next morning, so it routed to trail(), which found no recorded leg and
# returned {"ok": True} -- a silent success. ATHERENERG, entered 15 October,
# spent a full session and a night naked because every path reported fine.
#
# So the question a repair asks is not "how old is this row" but "does the
# BROKER show a stop". Age decides only the MECHANISM:
#
#   entered today, session still running  -> day-0 SL (a regular order; it
#       checks the ORDER BOOK, which is all that exists before the shares
#       reach demat at T+1)
#   entered earlier, or we are at the pre-close  -> GTT (it checks DEMAT, and
#       a day order placed at 15:20 is flushed at 16:00 anyway)
#
# The pre-close case is why `at_preclose` exists rather than being inferred
# from the clock inside this function: the 15:20 job must get the GTT for a
# position entered TODAY as well, because its day-0 SL is about to die.

def ensure_protected(trade: dict, kite=None, prot: dict = None,
                     now_ist=None, at_preclose: bool = False) -> dict:
    """Place protection if the broker shows none. -> a result dict.

    action is one of: "none" (already protected), "placed_day0_sl",
    "placed_gtt", "refused_unreadable", "failed".
    """
    from atlas.execution import exits as X

    now = now_ist or _now_ist()
    sym = str(trade.get("symbol") or "")
    direction = str(trade.get("direction") or "LONG").upper()
    qty = int(float(trade.get("qty") or 0))
    entry = float(trade.get("entry_price") or 0)
    stop = float(trade.get("stop_price") or 0)
    out = {"symbol": sym, "action": "none", "ok": True, "reason": "",
           "mechanism": "", "legs": {}, "target": None}

    if prot is None:
        prot = broker_protection(kite=kite)

    if not prot.get("ok"):
        # REFUSE TO PLACE INTO AN UNREADABLE BROKER. It cannot tell a missing
        # stop from an unreadable one, and stacking a second stop on a live one
        # is its own incident. It is still reported as not-ok so the caller
        # alerts rather than recording a success.
        out.update({"action": "refused_unreadable", "ok": False,
                    "reason": f"broker unreadable ({prot.get('why')}) — "
                              f"cannot tell a missing stop from an unreadable "
                              f"one, so nothing was placed"})
        return out

    legs = confirm_legs(trade, prot)
    if legs["stop"] == CONFIRMED:
        out["reason"] = f"stop already confirmed at the broker ({legs['detail']})"
        return out

    if not (qty and entry and stop):
        out.update({"action": "failed", "ok": False,
                    "reason": f"incomplete row: qty={qty} entry={entry} "
                              f"stop={stop} — cannot price protection"})
        return out

    ed = trade.get("entry_date")
    if isinstance(ed, str):
        try:
            from datetime import date as _d
            ed = _d.fromisoformat(ed)
        except ValueError:
            ed = None
    entered_today = (ed == now.date())

    # THE RECORDED STOP IS NOT NECESSARILY A PLACEABLE PRICE. ATHERENERG's
    # 1418.86 is not a multiple of its 0.10 tick, which is what the broker
    # refused four times. protect_day0 and place_overnight_gtts each resolve
    # the instrument's own tick and round through price_for, so the raw
    # recorded value is handed in and the tick path owns the correction.
    if entered_today and not at_preclose:
        r = X.protect_day0(symbol=sym, direction=direction, qty=qty,
                           fill_price=entry, stop_price=stop, kite=kite)
        out.update({"action": "placed_day0_sl", "ok": bool(r.get("ok")),
                    "reason": r.get("reason", ""),
                    "mechanism": r.get("mechanism", ""),
                    "legs": r.get("legs") or {}})
        return out

    r = X.place_overnight_gtts(symbol=sym, direction=direction, qty=qty,
                               fill_price=entry, stop_price=stop, kite=kite)
    out.update({"action": "placed_gtt", "ok": bool(r.get("ok")),
                "reason": r.get("reason", ""),
                "mechanism": "GTT_SPLIT" if r.get("ok") else "",
                "legs": r.get("legs") or {}, "target": r.get("target")})
    return out
