"""
ATLAS Reporting — Daily Report
==============================
Runs at 19:05 IST (13:35 UTC), after the EOD chain.

Reads regime and broker funds from the SAME sources the entry path uses --
atlas_entry.get_market_context() and risk/funds.available_funds() -- so the
report cannot disagree with the agent about the state of the world. Both fail
loudly rather than substituting a default.

Per-position detail comes from atlas_trades; today's decisions, including
skips, come from atlas_entry_log, which market_open writes at decision time.

Live P&L is marked against live_prices. If that feed is stale the P&L is NOT
shown -- a number computed from yesterday's closes looks exactly like a real
one and is worse than no number.

Trailing stops are RECOMMENDATIONS. ATLAS places no stop-loss orders.
"""

import sys, requests, logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    DEFAULT_AGENT_MODE, MAX_RISK_PER_TRADE, MAX_TRADES_PER_DAY,
    OPEN_STATUSES,
)
from atlas.reporting.telegram import send

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ATLAS-REPORT] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# live_prices is refreshed by upstox_ws at each session update; the last of the
# day lands at 15:15 IST. Anything older than this at report time means the
# feed did not run, not that the market was quiet.
MAX_PRICE_AGE_HOURS = 8

HTTP_TIMEOUT = 15


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def _get(query: str, what: str):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{query}",
                         headers=_headers(), timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        log.warning(f"{what}: HTTP {r.status_code}")
    except requests.RequestException as e:
        log.warning(f"{what}: {e}")
    return None


# ── FORMATTING ────────────────────────────────────────────────────

def _inr(n, dec: int = 0) -> str:
    """Indian digit grouping: 4135653 -> '41,35,653', 3115.7 -> '3,115.7'.

    Formats the whole value FIRST so rounding carries into the integer part.
    Splitting int/frac before rounding dropped the carry: 1323.95 at dec=1 gave
    whole=1323 and f"{0.95:.1f}" = "1.0", of which only ".0" was kept -- so it
    printed 1,323.0 and lost a rupee. On a report of prices and P&L that is not
    cosmetic.
    """
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    neg = v < 0
    full = f"{abs(v):.{dec}f}"
    if "." in full:
        s, frac_digits = full.split(".")
        frac = "." + frac_digits
    else:
        s, frac = full, ""
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + s + frac


def _esc(s) -> str:
    """HTML-escape for a Telegram <pre> block. M&M is a real symbol."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _pad(s, n: int) -> str:
    s = str(s)
    return s + " " * max(0, n - len(s))


def _clip(s, n: int = 62) -> str:
    """Trim to n chars on a word boundary. Gate reasons are verbose --
    'hedge shorts require extreme_bearish (200DMA-3%, 50<200DMA, VIX>18)' --
    and a hard slice cut mid-token leaving a dangling space."""
    s = " ".join(str(s).split())
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return (cut or s[:n]).rstrip(" ,;-") + "…"


# ── DATA ──────────────────────────────────────────────────────────

def get_regime() -> dict:
    """Same source the entry gate uses, staleness guard included."""
    try:
        from atlas.execution.atlas_entry import get_market_context
        return get_market_context()
    except Exception as e:
        log.error(f"regime read failed: {e}")
        return {"regime": "unknown", "allow_accumulation": False, "source": "error"}


def get_funds() -> tuple:
    """(funds_dict_or_None, error_string). Same source the entry gate uses."""
    try:
        from atlas.risk.funds import available_funds
        return available_funds(), ""
    except Exception as e:
        return None, str(e)


def get_live_prices(symbols: list) -> tuple:
    """({symbol: ltp}, stale_bool, as_of_string).

    Stale means the feed did not update today -- callers must suppress P&L
    rather than mark positions against old closes.
    """
    if not symbols:
        return {}, False, ""
    # Quote and percent-encode the symbol list. Bare `in.(M&M,GRASIM)` ends the
    # query parameter at the ampersand, and PostgREST answers
    # 400 PGRST100 "failed to parse filter (in.(M)". M&M is a real NSE symbol
    # and holding it silently suppressed P&L for EVERY position, because the
    # whole request failed and the feed was reported stale.
    from urllib.parse import quote
    vals = ",".join('"' + s.replace('"', '""') + '"' for s in symbols)
    rows = _get(f"live_prices?symbol={quote(f'in.({vals})', safe='')}"
                f"&select=symbol,ltp,updated_at", "live prices")
    if not rows:
        return {}, True, "no price rows"

    prices, newest = {}, None
    for r in rows:
        try:
            prices[r["symbol"]] = float(r["ltp"])
        except (TypeError, ValueError):
            continue
        ts = r.get("updated_at")
        if ts:
            try:
                t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(IST)
                newest = t if newest is None or t > newest else newest
            except Exception:
                pass

    if newest is None:
        return prices, True, "no timestamp on price rows"
    age_h = (datetime.now(IST) - newest).total_seconds() / 3600.0
    return prices, age_h > MAX_PRICE_AGE_HOURS, newest.strftime("%d %b %H:%M IST")


def get_today_decisions() -> list:
    today = datetime.now(IST).date().isoformat()
    return _get(f"atlas_entry_log?run_date=eq.{today}"
                f"&order=run_at.asc&select=*", "entry log") or []


def get_positions() -> list:
    return _get(f"atlas_trades?status=in.({','.join(OPEN_STATUSES)})"
                f"&order=entry_date.asc&select=*", "positions") or []


def get_agent_state() -> dict:
    rows = _get("atlas_state?limit=1&order=updated_at.desc", "agent state")
    return rows[0] if rows else {"mode": DEFAULT_AGENT_MODE}


# ── SECTIONS ──────────────────────────────────────────────────────

def _fmt_today(decisions: list) -> str:
    entered = [d for d in decisions if d["status"] in ("ENTERED", "SHADOW_INTENT")]
    gtts    = [d for d in decisions if d["status"] in ("GTT_PLACED", "SHADOW_GTT")]
    skipped = [d for d in decisions if d not in entered and d not in gtts]

    out = ["TODAY",
           f"Entered: {len(entered)} · GTT placed: {len(gtts)} · Skipped: {len(skipped)}",
           ""]
    if not decisions:
        out.append("  no signals evaluated — market_open did not run, or the")
        out.append("  batch was empty")
        return "\n".join(out)

    for d in entered + gtts:
        verb = ("entered @" if d["status"] in ("ENTERED", "SHADOW_INTENT")
                else "GTT resting @")
        out.append(f"  {_pad(d['symbol'], 11)} {verb} Rs{_inr(d.get('entry_price'), 1)}")
        stop, qty, risk = d.get("stop_price"), d.get("qty"), d.get("risk_inr")
        if stop:
            pct = abs(float(d["entry_price"]) - float(stop)) / float(d["entry_price"]) * 100
            out.append(f"  {' ' * 11} stop Rs{_inr(stop, 1)} ({pct:.2f}%) · "
                       f"{qty or '—'} qty · Rs{_inr(risk)} risk")
        out.append("")

    for d in skipped:
        reason = (d.get("reason") or d["status"]).strip()
        out.append(f"  {_pad(d['symbol'], 11)} skipped — {_clip(reason)}")
    return "\n".join(out)


def _fmt_positions(positions: list, prices: dict, stale: bool, as_of: str) -> str:
    from atlas.risk.trailing import recommend_trail

    open_pos = [p for p in positions if p.get("status") == "OPEN"]
    out = [f"OPEN POSITIONS ({len(open_pos)})"]
    if not open_pos:
        out.append("  none")
        return "\n".join(out)

    for p in open_pos:
        sym   = p.get("symbol", "?")
        dirn  = (p.get("direction") or "LONG").upper()
        qty   = int(p.get("qty") or 0)
        entry = float(p.get("entry_price") or 0)
        stop  = p.get("stop_price")
        out.append(f"  {_pad(sym, 11)} {dirn} · {qty} qty @ Rs{_inr(entry, 1)} · "
                   f"Rs{_inr(entry * qty)} notional")

        ltp = prices.get(sym)
        if stale or ltp is None:
            why = f"last {as_of}" if as_of else "no price"
            out.append(f"  {' ' * 11} price feed STALE ({why}) — P&L not shown")
        else:
            pnl = (ltp - entry) * qty if dirn == "LONG" else (entry - ltp) * qty
            pct = (pnl / (entry * qty) * 100) if entry and qty else 0.0
            out.append(f"  {' ' * 11} now Rs{_inr(ltp, 1)} · "
                       f"P&L {'+' if pnl >= 0 else '-'}Rs{_inr(abs(pnl))} ({pct:+.2f}%)")

        if stop:
            risk = abs(entry - float(stop)) * qty
            out.append(f"  {' ' * 11} stop Rs{_inr(stop, 1)} (manual) · "
                       f"Rs{_inr(risk)} risk")
            t = recommend_trail(sym, dirn, float(stop), p.get("entry_date"))
            if t["ok"]:
                out.append(f"  {' ' * 11} trail → Rs{_inr(t['price'], 1)} "
                           f"({t['reason']}, {t['tighten_pct']:.2f}% tighter)")
            else:
                out.append(f"  {' ' * 11} trail → none ({t['reason']})")
        else:
            out.append(f"  {' ' * 11} NO STOP RECORDED — set one manually")
        out.append("")
    return "\n".join(out).rstrip()


def _fmt_gtts(positions: list) -> str:
    resting = [p for p in positions if p.get("status") == "GTT_PENDING"]
    out = [f"RESTING GTTs ({len(resting)})"]
    if not resting:
        out.append("  none")
        return "\n".join(out)
    for p in resting:
        sym  = p.get("symbol", "?")
        trig = p.get("trigger_price") or p.get("entry_price") or 0
        qty  = int(p.get("qty") or 0)
        out.append(f"  {_pad(sym, 11)} trigger Rs{_inr(trig, 1)} · "
                   f"Rs{_inr(float(trig) * qty)} committed")
    return "\n".join(out)


# ── REPORT ────────────────────────────────────────────────────────

def build_report() -> str:
    now = datetime.now(IST)
    ctx = get_regime()
    funds, funds_err = get_funds()
    decisions = get_today_decisions()
    positions = get_positions()

    symbols = sorted({p["symbol"] for p in positions if p.get("symbol")})
    prices, stale, as_of = get_live_prices(symbols)

    regime = str(ctx.get("regime", "unknown")).upper()
    if ctx.get("regime") == "unknown":
        regime_line = f"Regime: UNKNOWN ({ctx.get('source', '?')}) · no trades permitted"
    else:
        stance = ("accumulation permitted" if ctx.get("allow_accumulation")
                  else "accumulation blocked")
        if ctx.get("extreme_bearish"):
            stance += " · extreme bear (hedge shorts open)"
        regime_line = f"Regime: {regime} · {stance}"

    funds_line = (f"Broker funds: Rs{_inr(funds['available'])} available"
                  if funds else
                  f"Broker funds: UNAVAILABLE ({funds_err[:60]}) — entries blocked")

    mode = get_agent_state().get("mode", DEFAULT_AGENT_MODE)
    mode_line = f"Mode: {mode}" if mode != DEFAULT_AGENT_MODE else ""

    parts = [
        f"ATLAS DAILY — {now.strftime('%d %b %Y, %H:%M IST')}",
        "",
        regime_line,
        funds_line,
    ]
    if mode_line:
        parts.append(mode_line)
    parts += [
        "",
        _fmt_today(decisions),
        "",
        _fmt_positions(positions, prices, stale, as_of),
        "",
        _fmt_gtts(positions),
        "",
        "REMINDER: ATLAS places no stop-losses. Set them manually.",
        "Trailing levels above are recommendations only — nothing is placed.",
    ]
    return "\n".join(parts)


def generate_and_send() -> bool:
    body = build_report()
    log.info("\n" + body)
    ok = send(f"<pre>{_esc(body)}</pre>")
    if ok:
        log.info("Daily report sent")
    else:
        log.error("Failed to send daily report")
    return ok


# NOTE: exactly ONE __main__ guard, at end of file.
#
# There used to be two. The first called generate_and_send(); run() was defined
# below it and a second guard at EOF called run(), which called
# generate_and_send() again -- so the report was sent twice, three seconds
# apart. Confirmed in production on 2026-08-11.
#
# run()'s directive poll went with it. It called directives.poll(300), which
# long-polls Telegram getUpdates while bot_listener.py already long-polls the
# same bot token 24/7. getUpdates has one logical consumer: whichever polls
# first takes the update and the other never sees it. That is why the evening
# prompt logged "No directive received" with a healthy listener.


if __name__ == "__main__":
    generate_and_send()
