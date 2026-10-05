"""
THE STOCK LOGIC — the nightly re-test.

Runs after the chain, extends every standing hypothesis by the observations that
arrived since the last run, writes a result row whether or not anything changed,
and reports ONLY when something crosses or when a closed hypothesis re-opens.

SILENCE IS THE DEFAULT, deliberately. A nightly report nobody reads is the same as
no report, and worse than none, because it trains the operator to ignore the
channel the real finding will arrive on.

WHY EVERY HYPOTHESIS IS ONE-SAMPLE
----------------------------------
The 2026-09-30 study asked "do winners and losers differ on feature X", which is a
two-sample question, and anytime-valid two-sample testing is substantially harder
than one-sample. Rather than hand-wave it, every hypothesis here is re-expressed as
the question an operator would actually act on:

    does this stratum clear breakeven?

At a 2R target, breakeven is 1/(1+2) = 33.3%. "Grade A clears breakeven" is
one-sided, one-sample, has a fixed null that does not move with the data, and is
pre-registrable in a way that "differs from the other group" is not. It is also the
only form of the question that leads anywhere: a stratum that differs from another
but still loses money is not actionable.

WHY THE LOOP STARTS NEARLY EMPTY, AND WHY THAT IS CORRECT
--------------------------------------------------------
Every outcome-based lineage seeds from 2026-10-03, the day the measurement basis
moved from entry_ref to the actual fill and 293 verdicts were re-resolved. The 282
rows that exist were resolved under the old definition and cannot be multiplied
into a product computed under the new one. So tonight most hypotheses have n=0 and
report nothing.

That is the basis rule doing its job, not a defect. The alternative -- seeding from
the earliest available row -- would produce a confident-looking e-value built on
two incompatible definitions of a win, and nothing about it would look wrong.
"""

import os
import sys
import logging
import argparse
from datetime import datetime, timezone, timedelta

import requests

from engine.learning import (bernoulli_log_e, crossed, ebh, wilson)
from engine.provenance import engine_sha

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("LEARNING")

SUPABASE_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
IST = timezone(timedelta(hours=5, minutes=30))
PAGE = 1000
ALPHA = 0.05

# The basis the outcome record is on since the re-resolution of 2026-10-03.
BASIS_OUTCOME = "fill-2R-v2"
BASIS_OUTCOME_FROM = "2026-10-03"
# Marks are scored on the sign of cum_move_pct and were not touched by the target
# repair -- but the wrong-side exclusion changed WHICH marks count, on 2026-10-05.
BASIS_MARKS = "marks-zone-valid-v1"
BASIS_MARKS_FROM = "2026-10-05"

# Breakeven at the 2R measurement yardstick. Fixed, not estimated.
BREAKEVEN_2R = 1.0 / 3.0


def _headers() -> dict:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _get_all(path: str, params: str) -> list:
    out, off = [], 0
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}?{params}"
                         f"&limit={PAGE}&offset={off}",
                         headers=_headers(), timeout=60)
        if r.status_code not in (200, 206):
            raise RuntimeError(f"{path} HTTP {r.status_code}: {r.text[:200]}")
        chunk = r.json() or []
        out += chunk
        if len(chunk) < PAGE:
            return out
        off += PAGE


# ══════════════════════════════════════════════════════════════════
# THE STANDING SET — pre-registered, one-sample, fixed nulls
# ══════════════════════════════════════════════════════════════════
#
# Every stratum below is defined by an ENGINE CATEGORY -- a grade band, the
# configured stop band, the direction, the regime classifier's own output -- and
# never by a cut chosen from the outcomes. A median split computed from the data
# would be a parameter fitted to the thing being tested.

def _stop_band(row) -> str:
    e, s = row.get("actual_entry"), row.get("sl")
    if not e or not s:
        return "?"
    w = 100.0 * abs(float(e) - float(s)) / float(e)
    return "<1%" if w < 1 else ("1-2%" if w < 2 else ("2-3%" if w < 3 else ">=3%"))


def _hit(row) -> int:
    """1 if the row reached its 2R target before its stop. AMBIGUOUS and OPEN are
    not observations: they are rows the record declines to call, and feeding them
    in as either value would be inventing data."""
    return 1 if row.get("outcome") == "WIN_T1" else 0


def _decided(row) -> bool:
    return row.get("outcome") in ("WIN_T1", "LOSS")


REGISTRY = []


def _reg(slug, question, family, population, statistic, null_value, side,
         basis, seed_from, select, rationale):
    REGISTRY.append(dict(slug=slug, question=question, family=family,
                         population=population, statistic=statistic,
                         null_value=null_value, side=side, basis_version=basis,
                         seed_from=seed_from, select=select,
                         rationale=rationale))


# ── FAMILY A: does a stratum clear breakeven? ──────────────────────
for _g in ("A+", "A", "B", "C"):
    _reg(f"breakeven.grade.{_g}",
         f"Do grade {_g} signals clear the 33.3% breakeven at a 2R target?",
         "stratum-breakeven", "signal_outcomes, decided rows",
         "hit rate vs 1/(1+2)", BREAKEVEN_2R, "greater",
         BASIS_OUTCOME, BASIS_OUTCOME_FROM,
         lambda r, g=_g: _decided(r) and str(r.get("grade")) == g,
         "Grade is an engine category, not a cut fitted to the outcomes. The "
         "2026-09-30 study found grade non-separating on the pre-repair basis; "
         "this is the actionable form of the question on the new basis.")

for _d in ("LONG", "SHORT"):
    _reg(f"breakeven.direction.{_d}",
         f"Do {_d} signals clear the 33.3% breakeven at a 2R target?",
         "stratum-breakeven", "signal_outcomes, decided rows",
         "hit rate vs 1/(1+2)", BREAKEVEN_2R, "greater",
         BASIS_OUTCOME, BASIS_OUTCOME_FROM,
         lambda r, d=_d: _decided(r) and str(r.get("direction")).upper() == d,
         "Shorts measured -0.554R against longs' -0.221R in the exit study. That "
         "was the whole 282-row population on the old basis; this re-asks it as "
         "an absolute threshold on the new one.")

# SLUGS ARE URL-SAFE IDENTIFIERS. The band labels are "<1%", "1-2%" and so on, and
# a slug containing % would be read as a percent-escape in `slug=eq....` -- a query
# that fails or, worse, silently matches something else. The label stays in the
# question, where it is for a human.
for _b, _slug in (("<1%", "under1"), ("1-2%", "1to2"),
                  ("2-3%", "2to3"), (">=3%", "over3")):
    _reg(f"breakeven.stop.{_slug}",
         f"Do signals with a {_b} stop clear the 33.3% breakeven?",
         "stratum-breakeven", "signal_outcomes, decided rows",
         "hit rate vs 1/(1+2)", BREAKEVEN_2R, "greater",
         BASIS_OUTCOME, BASIS_OUTCOME_FROM,
         lambda r, b=_b: _decided(r) and _stop_band(r) == b,
         "The bands are the configured MIN_STOP_PCT/MAX_STOP_PCT structure, not a "
         "split chosen from the data.")

_reg("breakeven.all",
     "Does the whole resolved set clear the 33.3% breakeven at a 2R target?",
     "stratum-breakeven", "signal_outcomes, decided rows",
     "hit rate vs 1/(1+2)", BREAKEVEN_2R, "greater",
     BASIS_OUTCOME, BASIS_OUTCOME_FROM,
     _decided,
     "The baseline every stratum is read against. The pre-repair record put this "
     "at 26.2% with a CI upper bound of 31.7%, below breakeven.")

# ── FAMILY B: directional accuracy ─────────────────────────────────
_reg("marks.directional",
     "Is per-mark directional accuracy better than a coin?",
     "directional", "v_marks_zone_valid, scored marks",
     "correct_today rate vs 0.50", 0.5, "two-sided",
     BASIS_MARKS, BASIS_MARKS_FROM,
     lambda r: r.get("correct_today") is not None,
     "44.8% on 8,931 marks before the wrong-side exclusion was in force; 54.7% on "
     "181 marks on 2026-10-05, which is p=0.21 and not distinguishable from "
     "chance. Two-sided because the standing record is BELOW 50% and a real "
     "finding could go either way.")

STANDING = {h["slug"]: h for h in REGISTRY}


# ══════════════════════════════════════════════════════════════════
# STATE — where each lineage left off
# ══════════════════════════════════════════════════════════════════

_LEDGER_MISSING = False


def last_result(slug: str, basis: str) -> dict:
    """The most recent result for this lineage, or None.

    SCOPED TO THE BASIS. A result from a previous basis must not be picked up as a
    starting state -- that is precisely the multiplication across a changed
    definition the whole design exists to prevent.

    DEGRADES WHEN THE LEDGER DOES NOT EXIST, so --dry-run is usable before the
    migration is applied and shows what the first night would do. A real run still
    fails at the write, which is where it should fail: reporting a crossing that was
    never recorded would be worse than not running.
    """
    global _LEDGER_MISSING
    if _LEDGER_MISSING:
        return None
    try:
        rows = _get_all("learning_results",
                        f"select=*&slug=eq.{slug}&basis_version=eq.{basis}"
                        f"&order=run_date.desc,id.desc")
    except RuntimeError as e:
        if "PGRST205" in str(e) or "Could not find the table" in str(e):
            _LEDGER_MISSING = True
            log.warning("learning_results does not exist — apply "
                        "migrations/PENDING_learning_ledger.sql. Treating every "
                        "lineage as unstarted; nothing will be written.")
            return None
        raise
    return rows[0] if rows else None


def ensure_registered(h: dict) -> None:
    """Upsert the hypothesis. registered_at is set ONCE, by the insert."""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/learning_hypotheses?on_conflict=slug",
        headers={**_headers(),
                 "Prefer": "resolution=ignore-duplicates,return=minimal"},
        json=[{k: h[k] for k in ("slug", "question", "population", "statistic",
                                 "null_value", "side", "family", "basis_version",
                                 "seed_from", "rationale")}],
        timeout=30)
    if r.status_code not in (200, 201, 204, 409):
        raise RuntimeError(f"could not register {h['slug']}: "
                           f"HTTP {r.status_code} {r.text[:200]}")


# ══════════════════════════════════════════════════════════════════
# THE RUN
# ══════════════════════════════════════════════════════════════════

def observations(h: dict, after: str) -> tuple:
    """(stream, high_water) for rows strictly after `after`, in arrival order.

    HIGH WATER IS THE LAST DATE CONSUMED, NOT THE CUTOFF USED. An earlier draft
    stored the cutoff, so a run that consumed three nights of rows recorded the
    boundary it started from and the next run read those same rows again:
    n_through went 5, 15, 30 on five new rows a night. The product grew on repeated
    observations, which is the one thing the e-process may not survive.

    The water mark advances only over rows that were actually read -- including
    rows the stratum filtered OUT, because they were consumed from the source and
    must not be offered again.
    """
    cutoff = max(str(after or ""), h["seed_from"])
    if h["family"] == "directional":
        rows = _get_all("v_marks_zone_valid",
                        f"select=mark_date,correct_today"
                        f"&mark_date=gt.{cutoff}&order=mark_date.asc")
        high = max([str(r["mark_date"])[:10] for r in rows], default=cutoff)
        return ([1 if r.get("correct_today") else 0
                 for r in rows if r.get("correct_today") is not None], high)
    rows = _get_all("signal_outcomes",
                    f"select=signal_date,symbol,direction,grade,outcome,"
                    f"actual_entry,sl&signal_date=gt.{cutoff}"
                    f"&order=signal_date.asc,symbol.asc")
    high = max([str(r["signal_date"])[:10] for r in rows], default=cutoff)
    picked = [r for r in rows if h["select"](r)]
    return ([_hit(r) for r in picked], high)


def run(run_date: str, dry: bool = False) -> dict:
    sha = engine_sha()
    results, evalues = {}, {}

    for slug, h in STANDING.items():
        if not dry:
            ensure_registered(h)
        prev = last_result(slug, h["basis_version"])
        state = None
        after = h["seed_from"]
        if prev:
            # THE DELTA BOUNDARY. `after` must come from the stored cutoff, not
            # from seed_from -- an earlier draft of this read a `last_date` column
            # that does not exist, fell back to seed_from, and would therefore have
            # re-consumed every observation on every run. The product would have
            # grown nightly on the same rows, the martingale property would have
            # been gone, and nothing about the output would have looked wrong.
            state = _unpack(prev)
            after = _last_date(prev) or h["seed_from"]
        try:
            xs, high_water = observations(h, after)
        except Exception as e:
            log.error(f"{slug}: could not read observations ({e})")
            continue
        st = bernoulli_log_e(xs, float(h["null_value"]), h["side"], state)
        p, lo, hi = wilson(st["s"], st["n"])
        results[slug] = dict(
            slug=slug, run_date=run_date, basis_version=h["basis_version"],
            n_through=st["n"], n_new=len(xs), successes=st["s"],
            log_e=round(st["log_e"], 6),
            e_value=round(min(1e30, st["e"]), 6),
            crossed=crossed(st["log_e"], ALPHA),
            point_estimate=round(p, 6) if st["n"] else None,
            ci_lo=round(lo, 6) if st["n"] else None,
            ci_hi=round(hi, 6) if st["n"] else None,
            engine_sha=sha,
            verdict=_pack(st, high_water))
        evalues[slug] = st["e"]

    # ── e-BH ACROSS THE WHOLE STANDING SET ────────────────────────
    # One FDR budget. A hypothesis that clears 1/alpha on its own has not earned a
    # report: eighteen of them looking is eighteen chances.
    decision = ebh(evalues, ALPHA)
    for slug, row in results.items():
        row["crossed_ebh"] = slug in decision["rejected"]
        row["ebh_threshold"] = (round(decision["threshold"], 6)
                                if decision["k"] else None)
    return {"results": results, "ebh": decision, "run_date": run_date}


def _pack(st: dict, high_water: str) -> str:
    """The one-sided halves and the HIGH WATER MARK, carried in `verdict`.

    They have to persist or the lineage cannot be resumed, and adding two more
    columns for internal state would invite a reader to interpret them. Packed as
    a short machine string, documented here and nowhere else relied upon.
    """
    return (f"up={st['log_e_up']:.6f};dn={st['log_e_dn']:.6f};"
            f"through={high_water}")


def _unpack(prev: dict) -> dict:
    v = str(prev.get("verdict") or "")
    up = dn = 0.0
    for part in v.split(";"):
        if part.startswith("up="):
            up = float(part[3:] or 0)
        elif part.startswith("dn="):
            dn = float(part[3:] or 0)
    return {"log_e_up": up, "log_e_dn": dn,
            "n": int(prev["n_through"]), "s": int(prev.get("successes") or 0)}


def _last_date(prev: dict) -> str:
    v = str(prev.get("verdict") or "")
    for part in v.split(";"):
        if part.startswith("through="):
            return part[len("through="):]
    return ""


def write(results: dict) -> int:
    rows = list(results.values())
    if not rows:
        return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/learning_results"
        f"?on_conflict=slug,run_date,basis_version",
        headers={**_headers(),
                 "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows, timeout=60)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"ledger write failed: HTTP {r.status_code} "
                           f"{r.text[:300]}")
    return len(rows)


def report(out: dict) -> str:
    """The message, or "" for silence."""
    crossing = [r for r in out["results"].values() if r["crossed_ebh"]]
    if not crossing:
        return ""
    lines = [f"🔬 <b>Learning loop — {len(crossing)} crossing(s)</b>",
             f"e-BH at alpha={ALPHA} across {len(out['results'])} standing "
             f"hypotheses.", ""]
    for r in sorted(crossing, key=lambda x: -float(x["log_e"])):
        h = STANDING[r["slug"]]
        lines.append(f"<b>{r['slug']}</b>")
        lines.append(f"  {h['question']}")
        lines.append(f"  n={r['n_through']}  rate={100*float(r['point_estimate']):.1f}%"
                     f"  null={100*float(h['null_value']):.1f}%")
        lines.append(f"  e={float(r['e_value']):.1f}  "
                     f"95% CI {100*float(r['ci_lo']):.1f}–{100*float(r['ci_hi']):.1f}%")
        lines.append("")
    lines.append("Nothing has changed in the engine. This is a proposal.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="run date (default: today IST)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.dry_run and not SUPABASE_KEY:
        log.error("SUPABASE_SERVICE_KEY is not set")
        return 2
    run_date = a.date or datetime.now(IST).date().isoformat()

    out = run(run_date, dry=a.dry_run)
    res = out["results"]
    live = sum(1 for r in res.values() if r["n_through"] > 0)
    newobs = sum(r["n_new"] for r in res.values())
    log.info(f"{len(res)} standing hypothes(es), {live} with any observations, "
             f"{newobs} new observation(s) this run")
    if not live:
        log.info("every lineage is still empty — outcome hypotheses seed from "
                 f"{BASIS_OUTCOME_FROM} and marks from {BASIS_MARKS_FROM}, so "
                 f"evidence starts accumulating from there forward.")

    if a.dry_run:
        for slug, r in sorted(res.items()):
            log.info(f"  {slug:34} n={r['n_through']:5} +{r['n_new']:4} "
                     f"log_e={float(r['log_e']):+8.3f} "
                     f"{'CROSSED' if r['crossed_ebh'] else ''}")
        msg = report(out)
        log.info("report:\n" + (msg or "  (silence — nothing crossed)"))
        return 0

    n = write(res)
    log.info(f"wrote {n} result row(s) to the ledger")
    msg = report(out)
    if msg:
        log.warning("A HYPOTHESIS CROSSED — reporting")
        try:
            from atlas.reporting.telegram import send
            send(msg)
        except Exception as e:
            log.error(f"could not send the finding ({e}) — it is in the ledger")
    else:
        log.info("nothing crossed; staying silent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
