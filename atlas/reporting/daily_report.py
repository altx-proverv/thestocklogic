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

# Individually-detailed skips. The rest are carried by the per-status counts.
SKIP_DETAIL = 10

# Telegram rejects any message over 4096 characters outright -- the send fails,
# nothing is delivered, and the only trace is a 400 in the log. That is exactly
# how the 2026-08-12 report was lost: 143 skip lines, 7,959 characters, "Bad
# Request: message is too long". Bounding the skip list fixes the cause; this
# is the backstop, so a report can be degraded but never silently dropped.
TELEGRAM_LIMIT = 4096

FOOTER = ("REMINDER: ATLAS places no stop-losses. Set them manually.\n"
          "Trailing levels above are recommendations only — nothing is placed.")


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def _get(query: str, what: str):
    try:
        r = requests.get(f"{SUPABASE_URL}{query}",
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
    rows = _get(f"/rest/v1/live_prices?symbol={quote(f'in.({vals})', safe='')}"
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
    return _get(f"/rest/v1/atlas_entry_log?run_date=eq.{today}"
                f"&order=run_at.asc&select=*", "entry log") or []


def get_positions() -> list:
    return _get(f"/rest/v1/atlas_trades?status=in.({','.join(OPEN_STATUSES)})"
                f"&order=entry_date.asc&select=*", "positions") or []


def get_agent_state() -> dict:
    rows = _get("/rest/v1/atlas_state?limit=1&order=updated_at.desc", "agent state")
    return rows[0] if rows else {"mode": DEFAULT_AGENT_MODE}


# ── SECTIONS ──────────────────────────────────────────────────────

def _pub_line(d: dict) -> str:
    """The PUBLICATION distance behind a decision, if it was recorded.

    Deliberately shown next to the morning check, because the two answer
    different questions and the difference looks like a bug when only one is
    visible. entry_dist_pct is how far price sat from the zone at the close
    03b scored -- that is what MAX_ENTRY_DIST_PCT gates. The reason string
    beside it is the morning check: is LTP inside the zone RIGHT NOW. A signal
    that published at 0.28% off Tuesday's close can open 2% away on Wednesday,
    and skipping it is correct behaviour, not a gate that failed to apply.
    """
    dist = d.get("entry_dist_pct")
    if dist is None:
        return ""
    try:
        dist = float(dist)
    except (TypeError, ValueError):
        return ""
    when = ""
    if d.get("signal_date"):
        try:
            when = date.fromisoformat(str(d["signal_date"])).strftime(" %d %b")
        except ValueError:
            pass
    return f"published {dist:.2f}% from the{when or ''} close"


def _sample_skips(skipped: list, n: int = None) -> list:
    """Up to n skips, spread ACROSS statuses rather than taken off the top.

    Taking the first n gave ten consecutive SKIPPED_REGIME rows carrying one
    identical reason string -- ten lines that say what the status count already
    said. Round-robin means a 123/20 split shows both kinds, and the rarest
    status (usually the interesting one) is never buried under the commonest.
    Order within each status is preserved, so it is still a sample of what ran
    first, not a reshuffle.
    """
    n = SKIP_DETAIL if n is None else n
    groups = {}
    for d in skipped:
        groups.setdefault(d["status"], []).append(d)
    out, buckets = [], list(groups.values())
    i = 0
    while len(out) < n and any(buckets):
        b = buckets[i % len(buckets)]
        if b:
            out.append(b.pop(0))
        if not b:
            buckets.remove(b)
            i -= 1
        i += 1
    return out


def _fmt_today(decisions: list) -> str:
    entered = [d for d in decisions if d["status"] in ("ENTERED", "SHADOW_INTENT")]
    gtts    = [d for d in decisions if d["status"] in ("GTT_PLACED", "SHADOW_GTT")]
    failed  = [d for d in decisions if d["status"] == "ORDER_FAILED"]
    skipped = [d for d in decisions
               if d not in entered and d not in gtts and d not in failed]

    counts = [f"Entered: {len(entered)}"]
    if gtts:
        counts.append(f"GTT placed: {len(gtts)}")
    if failed:
        counts.append(f"ORDER FAILED: {len(failed)}")
    counts.append(f"Skipped: {len(skipped)}")

    out = ["TODAY", " · ".join(counts), ""]
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
        pub = _pub_line(d)
        if pub:
            out.append(f"  {' ' * 11} {pub}")
        out.append("")

    # An order that reached the broker and was refused is never summarised away.
    # It means every gate passed and execution still did not happen, which is
    # the one outcome that needs the operator's eyes tonight.
    if failed:
        out.append("  ORDERS REFUSED BY THE BROKER")
        for d in failed:
            out.append(f"  {_pad(d['symbol'], 11)} {_clip(d.get('reason') or '', 72)}")
            pub = _pub_line(d)
            if pub:
                out.append(f"  {' ' * 11} {pub}")
        out.append("")

    if skipped:
        # Summarise by status, then detail a SAMPLE. One line per skip is what
        # blew the Telegram limit on 2026-08-12: 143 skips built a 7,959-char
        # message against a 4,096 ceiling and the whole report was lost. The
        # counts carry the information; the detail lines are illustration.
        from collections import Counter
        by_status = Counter(d["status"] for d in skipped)
        for status, n in by_status.most_common():
            out.append(f"  {_pad(status, 18)} {n}")
        out.append("")
        for d in _sample_skips(skipped):
            reason = (d.get("reason") or d["status"]).strip()
            out.append(f"  {_pad(d['symbol'], 11)} skipped — {_clip(reason)}")
            pub = _pub_line(d)
            if pub:
                out.append(f"  {' ' * 11} {pub}")
        if len(skipped) > SKIP_DETAIL:
            out.append(f"  … and {len(skipped) - SKIP_DETAIL} more "
                       f"(full detail in atlas_entry_log)")
    return "\n".join(out).rstrip()


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
    ]
    return "\n".join(parts)


def _wrap(body: str) -> str:
    return f"<pre>{_esc(body)}</pre>"


def _fit(body: str, footer: str, limit: int = TELEGRAM_LIMIT) -> str:
    """Trim `body` until body+footer fits Telegram's limit. Footer is kept.

    Measured on the ESCAPED, WRAPPED payload -- what actually gets sent. A raw
    length check would pass and the send would still 400, because _esc expands
    every & into &amp;. M&M is a real NSE symbol, so that is not hypothetical.

    Truncation is announced in the message. A report that quietly drops its tail
    is the same failure as one that never arrives: the operator cannot tell.
    """
    full = f"{body}\n\n{footer}"
    if len(_wrap(full)) <= limit:
        return full

    note = "  … report truncated to fit Telegram's 4096-character limit"
    lines = body.split("\n")
    while lines:
        candidate = "\n".join(lines) + f"\n{note}\n\n{footer}"
        if len(_wrap(candidate)) <= limit:
            log.warning(f"report truncated: {len(lines)} of "
                        f"{len(body.splitlines())} body lines kept")
            return candidate
        lines.pop()
    return f"{note}\n\n{footer}"


def generate_and_send() -> bool:
    body = _fit(build_report(), FOOTER)
    log.info("\n" + body)
    ok = send(_wrap(body))
    if ok:
        log.info(f"Daily report sent ({len(_wrap(body))} chars)")
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
