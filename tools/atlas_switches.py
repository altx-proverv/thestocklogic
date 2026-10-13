"""
EVERY SWITCH THAT CAN STOP ATLAS ENTERING A TRADE, IN ONE PLACE
===============================================================

WHY THIS EXISTS. Four controls that disarm the engine were found one at a time,
each after it had already cost a session:

  2026-10-08  the halt file, which 7 of 8 entry points failed open on
  2026-10-09  the kill switch at Gate 7, reading a mode nothing explained
  2026-10-13  ENABLE_EXIT_MANAGEMENT, which returned ENTERED with no stop
  2026-10-13  atlas_state.mode = PAUSED, set by a test suite and reported as
              an operator action for 80 cycles

Each was discovered by reading code after the fact. The common failure is not
any one switch -- it is that there was no list, so there was no way to ask "what
is currently stopping us?" and get an answer.

THIS IS CODE, NOT A DOCUMENT. A table in a markdown file goes stale the first
time a switch is added. Every row below probes the real state, so running it
answers the question for the engine as it is right now, and the 08:30 readiness
message is built from the same rows the operator can print by hand.

READ-ONLY, AND IT NEVER RAISES. A probe that cannot read its state reports
UNKNOWN and counts as blocking -- not knowing whether we are disarmed is not
permission to trade. The 08:30 message must never fail to send because a probe
threw.

    python3 -m tools.atlas_switches            the table
    python3 -m tools.atlas_switches --json     machine-readable
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IST = timezone(timedelta(hours=5, minutes=30))

# scope: SESSION switches disarm every entry; SIGNAL gates shape which
# candidates pass and cannot by themselves mean "ATLAS is disarmed".
SESSION, SIGNAL = "session", "signal"


def _row(name, where, value, blocking, survives, detail="", scope=SESSION,
         advisory=False):
    """One switch.

    advisory=True marks a row that is reported but cannot by itself mean "ATLAS
    is disarmed" -- the systemd timer read from a machine that is not the box,
    or a sentiment source that is merely old. Everything else that cannot be
    read BLOCKS: not knowing whether we are disarmed is not permission to
    trade, and the only way to keep that rule honest is to make the exceptions
    name themselves.
    """
    return {"switch": name, "where": where, "value": str(value),
            "blocking": bool(blocking), "survives_restart": survives,
            "detail": detail, "scope": scope, "advisory": bool(advisory)}


# ── PROBES ─────────────────────────────────────────────────────────

def _halt_file():
    try:
        from atlas.config import halt_file_state
        st = halt_file_state()
        if not st["is_canonical"]:
            return _row("halt sentinel file", st["path"], "WRONG PATH", True, "yes",
                        f"resolved {st['path']}, canonical is {st['canonical']} — "
                        f"a halt written to the canonical path would not stop us")
        return _row("halt sentinel file", st["path"],
                    "HALTED" if st["halted"] else "clear", st["halted"], "yes",
                    (st.get("reason") or "")[:200])
    except Exception as e:
        return _row("halt sentinel file", "atlas.config.HALT_FILE", "UNKNOWN",
                    True, "yes", f"probe failed: {type(e).__name__}: {e}")


def _agent_mode():
    try:
        from atlas.reporting.directives import get_agent_state, mode_source
        from atlas.config import HALT_MODES
        st = get_agent_state()
        mode = str(st.get("mode", "")).upper()
        src, detail = mode_source(st)
        when = str(st.get("updated_at") or "")[:19].replace("T", " ")
        return _row("atlas_state.mode", "Supabase atlas_state id=1", mode,
                    mode in HALT_MODES, "yes (remote)",
                    f"set by {src} at {when or 'unknown time'}"
                    + (f" · {detail[:160]}" if detail else ""))
    except Exception as e:
        return _row("atlas_state.mode", "Supabase atlas_state id=1", "UNREADABLE",
                    True, "yes (remote)",
                    f"unreadable counts as paused: {type(e).__name__}: {e}")


def _flag(name, blocking_when_false=True, note=""):
    try:
        import atlas.config as C
        v = getattr(C, name)
        blocked = (not v) if blocking_when_false else bool(v)
        return _row(name, "atlas/config.py (code)", v, blocked,
                    "yes (constant)", note)
    except Exception as e:
        return _row(name, "atlas/config.py (code)", "UNKNOWN", True,
                    "yes (constant)", f"probe failed: {e}")


def _live_mode():
    try:
        import atlas.config as C
        live = C.LIVE_TRADING_ENABLED
        return _row("LIVE_TRADING_ENABLED", "atlas/config.py + ATLAS_LIVE env",
                    "LIVE" if live else "SHADOW", not live, "yes (re-resolved)",
                    "SHADOW evaluates and records but places no broker order; "
                    f"GO_LIVE_DATE {C.GO_LIVE_DATE}")
    except Exception as e:
        return _row("LIVE_TRADING_ENABLED", "atlas/config.py", "UNKNOWN", True,
                    "yes", f"probe failed: {e}")


def _both_sides_off():
    try:
        import atlas.config as C
        off = (not C.ALLOW_LONG_ENTRIES) and (not C.ALLOW_SHORT_ENTRIES)
        return _row("ALLOW_LONG / ALLOW_SHORT_ENTRIES", "atlas/config.py (code)",
                    f"long={C.ALLOW_LONG_ENTRIES} short={C.ALLOW_SHORT_ENTRIES}",
                    off, "yes (constant)",
                    "shorts are intentionally off; both off would disarm entirely")
    except Exception as e:
        return _row("ALLOW_LONG / ALLOW_SHORT_ENTRIES", "atlas/config.py",
                    "UNKNOWN", True, "yes", f"probe failed: {e}")


def _signal_batch():
    try:
        from atlas.signal.market_open import get_latest_batch_date, batch_is_stale, \
            MAX_BATCH_AGE_DAYS
        d = get_latest_batch_date()
        if not d:
            return _row("published signal batch", "Supabase public.signals",
                        "NONE", True, "yes (remote)",
                        "no published signals at all — the EOD chain has not run")
        stale = batch_is_stale(d)
        return _row("published signal batch", "Supabase public.signals", d,
                    stale, "yes (remote)",
                    f"newest signal_date; stale past {MAX_BATCH_AGE_DAYS} days")
    except Exception as e:
        return _row("published signal batch", "Supabase public.signals",
                    "UNKNOWN", True, "yes (remote)",
                    f"probe failed: {type(e).__name__}: {e}")


def _broker_token():
    try:
        from atlas.execution.zerodha_login import get_stored_token, verify_token
        tok = get_stored_token()
        if not tok:
            return _row("Zerodha access token", "stored token file", "ABSENT",
                        True, "yes (file)", "no token — login required")
        ok = verify_token(tok)
        return _row("Zerodha access token", "stored token file",
                    "valid" if ok else "INVALID", not ok, "yes (file)",
                    "expires daily; the 08:30 job renews it")
    except Exception as e:
        return _row("Zerodha access token", "stored token file", "UNKNOWN", True,
                    "yes (file)", f"probe failed: {type(e).__name__}: {e}")


def _timer():
    try:
        r = subprocess.run(["systemctl", "is-enabled", "atlas-market-hours.timer"],
                           capture_output=True, text=True, timeout=10)
        v = (r.stdout or r.stderr).strip() or "unknown"
        return _row("atlas-market-hours.timer", "systemd (box)", v,
                    v != "enabled", "yes (unit state)",
                    "starts the 09:20-15:20 IST session; disabled means no "
                    "session runs at all")
    except Exception as e:
        return _row("atlas-market-hours.timer", "systemd (box)", "UNKNOWN",
                    False, "yes (unit state)",
                    f"not probeable here ({type(e).__name__}) — run on the box",
                    advisory=True)


def _funds():
    try:
        from atlas.risk.funds import available_funds
        import atlas.config as C
        f = available_funds()
        avail = float(f.get("available") or 0)
        need = float(C.FIXED_NOTIONAL_PER_TRADE)
        return _row("broker funds", "live kite.margins() less resting GTTs",
                    f"Rs{avail:,.0f}", avail < need, "n/a (live read)",
                    f"one trade needs about Rs{need:,.0f}")
    except Exception as e:
        return _row("broker funds", "live kite.margins()", "UNREADABLE", True,
                    "n/a (live read)",
                    f"unreadable blocks at Gate 6: {type(e).__name__}: {e}")


def _regime_source():
    try:
        import pandas as pd
        p = Path("data/processed/market.parquet")
        if not p.exists():
            # NOT ADVISORY. Absent, _market_context falls back to
            # sector_heatmap, which carries no Nifty series and no breadth, so
            # sentiment is unknown and Gate 1 refuses every entry.
            return _row("sentiment source (market.parquet)", str(p), "MISSING",
                        True, "yes (file)",
                        "absent falls back to sector_heatmap, which cannot "
                        "establish sentiment — so every entry is refused")
        m = pd.read_parquet(p, columns=["date"])
        last = pd.to_datetime(m["date"]).max().date()
        age = (datetime.now(IST).date() - last).days
        return _row("sentiment source (market.parquet)", str(p), str(last),
                    False, "yes (file)",
                    f"{age} day(s) old; sentiment decides the permitted side "
                    f"at Gate 1 and an old read means cash, not a block",
                    advisory=True)
    except Exception as e:
        return _row("sentiment source (market.parquet)",
                    "data/processed/market.parquet", "UNKNOWN", False,
                    "yes (file)", f"probe failed: {e}", advisory=True)


def _kill_switch():
    try:
        from atlas.risk.kill_switch import status
        st = status()
        if not st.get("data_available"):
            return _row("kill switch (Gate 7)", "composite: atlas_state + funds",
                        "BLOCKING", True, "n/a (derived)",
                        st.get("note") or st.get("error") or "state unavailable")
        return _row("kill switch (Gate 7)", "composite: atlas_state + funds",
                    "halted" if st.get("halted") else "pass",
                    bool(st.get("halted")), "n/a (derived)",
                    f"mode {st.get('mode')} set by {st.get('mode_source')}")
    except Exception as e:
        return _row("kill switch (Gate 7)", "composite", "UNKNOWN", True,
                    "n/a (derived)", f"probe failed: {type(e).__name__}: {e}")


# ── THE PER-SIGNAL GATES, listed so the inventory is complete ─────
def _signal_gates():
    out = []
    try:
        import atlas.config as C
        out.append(_row("structural stop (Gate 0)", "per signal: sl field",
                        "required > 0", False, "n/a",
                        "REJECTED_NO_STOP; no stop means no entry", SIGNAL))
        out.append(_row("sentiment side (Gate 1)", "market.parquet breadth+20DMA",
                        "bullish required for LONG", False, "n/a",
                        "bearish/mixed/unknown all mean cash", SIGNAL))
        out.append(_row("already holding (Gate 3b)", "Supabase atlas_trades",
                        "one position per symbol", False, "n/a",
                        "blocks a second entry in a held symbol", SIGNAL))
        out.append(_row("entry band + proximity (Gate 4)",
                        f"per signal; ENTRY_PROXIMITY_PCT={C.ENTRY_PROXIMITY_PCT}",
                        f"within {C.ENTRY_PROXIMITY_PCT}% of the zone", False,
                        "yes (constant)",
                        "expect very few entries; zero is a correct outcome",
                        SIGNAL))
        out.append(_row("sizing / stop band (Gate 5)",
                        f"MAX_STOP_PCT={C.MAX_STOP_PCT}, "
                        f"notional Rs{C.FIXED_NOTIONAL_PER_TRADE:,.0f}",
                        "qty >= 1 and stop within band", False, "yes (constant)",
                        "a stop wider than the band is refused", SIGNAL))
        out.append(_row("universe exclusion (upstream)",
                        "engine/universe.EXCLUDED via 06_push_supabase",
                        "40 symbols cannot be published", False, "yes (code)",
                        "an excluded symbol never reaches public.signals, so "
                        "ATLAS never sees it", SIGNAL))
    except Exception as e:
        out.append(_row("per-signal gates", "atlas/config.py", "UNKNOWN", False,
                        "n/a", f"probe failed: {e}", SIGNAL))
    return out


def inventory(include_signal_gates: bool = True, fast: bool = False) -> list:
    """Every switch that can stop an entry, probed. Never raises.

    fast=True skips the probes that talk to the broker (the token, funds, and
    the kill switch's funds leg). The test suite uses it so running the tests
    cannot make a live broker call -- which is the class of accident that
    paused the engine on 2026-10-13 in the first place.
    """
    # EVERY SWITCH IS ALWAYS LISTED. fast mode skips the PROBE, not the row --
    # a mode that silently drops rows is a mode in which a switch can go
    # missing from the inventory, which is the whole failure this file exists
    # to end.
    def _not_probed(name, where, why):
        return _row(name, where, "not probed", False, "see full inventory",
                    f"skipped in fast mode ({why})", advisory=True)

    rows = [_halt_file(), _agent_mode()]
    rows.append(_kill_switch() if not fast else
                _not_probed("kill switch (Gate 7)",
                            "composite: atlas_state + funds",
                            "its funds leg calls the broker"))
    rows += [
        _flag("ENABLE_EXIT_MANAGEMENT",
              note="False refuses every entry at Gate -1b — ATLAS will not "
                   "open a position it cannot protect"),
        _flag("ALLOW_AUTOMATED_STOP_LOSS",
              note="False would mean no stop is placed"),
        _both_sides_off(),
        _live_mode(),
        _signal_batch(),
        _timer(),
        _regime_source(),
    ]
    rows += ([_broker_token(), _funds()] if not fast else
             [_not_probed("Zerodha access token", "stored token file",
                          "it verifies the token against the broker"),
              _not_probed("broker funds", "live kite.margins() less resting GTTs",
                          "it calls the broker")])
    if include_signal_gates:
        rows += _signal_gates()
    return rows


def blocking(rows: list = None) -> list:
    """The switches that would stop an entry right now."""
    rows = rows if rows is not None else inventory()
    return [r for r in rows if r["blocking"]]


def readiness(rows: list = None) -> tuple:
    """(ready, [one line per blocker]) for the operator-facing report."""
    rows = rows if rows is not None else inventory()
    blockers = blocking(rows)
    lines = [f"{r['switch']} = {r['value']}"
             + (f" ({r['detail'][:120]})" if r["detail"] else "")
             for r in blockers]
    return (not blockers), lines


def render(rows: list = None) -> str:
    rows = rows if rows is not None else inventory()
    w = (34, 40, 18, 9, 18)
    out = []
    for scope, title in ((SESSION, "SESSION SWITCHES — any one of these disarms "
                                   "every entry"),
                         (SIGNAL, "PER-SIGNAL GATES — these shape which "
                                  "candidates pass")):
        sel = [r for r in rows if r["scope"] == scope]
        if not sel:
            continue
        out.append("=" * 124)
        out.append(title)
        out.append("=" * 124)
        out.append(f"{'SWITCH':<{w[0]}}{'WHERE THE STATE LIVES':<{w[1]}}"
                   f"{'CURRENT VALUE':<{w[2]}}{'BLOCKS?':<{w[3]}}"
                   f"{'SURVIVES RESTART':<{w[4]}}")
        out.append("-" * 124)
        for r in sel:
            out.append(f"{r['switch']:<{w[0]}}{r['where'][:w[1]-1]:<{w[1]}}"
                       f"{r['value'][:w[2]-1]:<{w[2]}}"
                       f"{('YES' if r['blocking'] else 'no'):<{w[3]}}"
                       f"{r['survives_restart']:<{w[4]}}")
            if r["detail"]:
                out.append(f"{'':<{w[0]}}{r['detail'][:118]}")
        out.append("")
    ready, lines = readiness(rows)
    out.append("=" * 124)
    out.append(f"VERDICT: {'READY — nothing is blocking entries' if ready else 'NOT READY'}")
    for ln in lines:
        out.append(f"  BLOCKED BY  {ln}")
    out.append("=" * 124)
    return "\n".join(out)


def main() -> int:
    rows = inventory()
    if "--json" in sys.argv:
        print(json.dumps({"ready": readiness(rows)[0], "switches": rows}, indent=1))
    else:
        print(render(rows))
    return 0 if readiness(rows)[0] else 1


if __name__ == "__main__":
    sys.exit(main())
