#!/usr/bin/env python3
"""
SYSTEMD DIRECTIVES IN THE SECTION SYSTEMD READS THEM FROM
=========================================================
StartLimitIntervalSec and StartLimitBurst were in [Service]. systemd does not
recognise them there:

    Unknown key 'StartLimitIntervalSec' in section [Service], ignoring.

It logged that and started anyway, so the rate limit meant to catch a crash
loop was itself silently absent -- the failure it was added to prevent. They
moved to [Unit] in systemd 230; only RestartSec stays in [Service].

Nothing refused, nothing broke, and the unit looked installed. That is the same
shape as every other bug this directory exists for: a value that is ignored
rather than rejected.

install.sh now runs `systemd-analyze verify` before installing, which is the
real check and catches keys this file has never heard of. This one runs
anywhere -- there is no systemd on the machine these units are usually edited
on, which is how the misplaced key survived review in the first place.
"""

import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

# Where systemd actually reads each key from. Only keys these units use.
PLACEMENT = {
    "StartLimitIntervalSec": "Unit",
    "StartLimitBurst":       "Unit",
    "Description":           "Unit",
    "After":                 "Unit",
    "Wants":                 "Unit",
    "Requires":              "Unit",
    "Type":                  "Service",
    "User":                  "Service",
    "WorkingDirectory":      "Service",
    "EnvironmentFile":       "Service",
    "Environment":           "Service",
    "ExecStart":             "Service",
    "Restart":               "Service",
    "RestartSec":            "Service",
    "KillSignal":            "Service",
    "TimeoutStopSec":        "Service",
    "StandardOutput":        "Service",
    "StandardError":         "Service",
    "RuntimeDirectory":      "Service",
    "StateDirectory":        "Service",
    "OnCalendar":            "Timer",
    "AccuracySec":           "Timer",
    "Persistent":            "Timer",
    "Unit":                  "Timer",
    "WantedBy":              "Install",
}


def parse(path: Path) -> list:
    """[(section, key, value, lineno)] — comments and blanks dropped."""
    out, section = [], None
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = re.match(r"^\[([A-Za-z]+)\]$", line)
        if m:
            section = m.group(1)
            continue
        if "=" in line and section:
            key, _, val = line.partition("=")
            out.append((section, key.strip(), val.strip(), n))
    return out


def _report_after_close(rpt) -> bool:
    """The report must fire after WINDOW_END, or it reports a session still
    running and calls a live window an early death."""
    sys.path.insert(0, str(ROOT))
    from atlas.signal.market_open import WINDOW_END
    cal = next((v for _, k, v, _ in rpt if k == "OnCalendar"), "")
    m = re.search(r"(\d{2}):(\d{2})", cal)
    if not m:
        return False
    mins = int(m.group(1)) * 60 + int(m.group(2))
    return mins > WINDOW_END.hour * 60 + WINDOW_END.minute


def main() -> int:
    ok = True
    print("DIRECTIVE PLACEMENT")
    print("-" * 78)

    for name in ("atlas-market-hours.service", "atlas-market-hours.timer",
                 "atlas-engine-report.service", "atlas-engine-report.timer"):
        path = DEPLOY / name
        if not path.exists():
            print(f"  ** {name} missing")
            ok = False
            continue
        entries = parse(path)
        bad = [(s, k, n) for s, k, _, n in entries
               if k in PLACEMENT and PLACEMENT[k] != s]
        unknown = sorted({k for _, k, _, _ in entries if k not in PLACEMENT})
        if bad:
            ok = False
            for s, k, n in bad:
                print(f"  ** {name}:{n} {k} is in [{s}], systemd reads it "
                      f"from [{PLACEMENT[k]}]")
        else:
            print(f"  ok {name:<32}{len(entries)} directive(s), all in place")
        if unknown:
            # Not a failure: this table only knows the keys these units use.
            # systemd-analyze verify in install.sh is what covers the rest.
            print(f"     (not in this table, unchecked here: {', '.join(unknown)})")

    print()
    print("THE SHAPE OF THE PAIR")
    print("-" * 78)
    svc = parse(DEPLOY / "atlas-market-hours.service")
    tmr = parse(DEPLOY / "atlas-market-hours.timer")
    rep = parse(DEPLOY / "atlas-engine-report.service")
    rpt = parse(DEPLOY / "atlas-engine-report.timer")

    checks = [
        ("service has NO [Install] (timer-activated, not boot-started)",
         not any(s == "Install" for s, _, _, _ in svc)),
        ("timer has [Install]",
         any(s == "Install" for s, _, _, _ in tmr)),
        ("service restarts on-failure, not always",
         any(k == "Restart" and v == "on-failure" for _, k, v, _ in svc)),
        ("start-limit present, so a crash loop gives up",
         any(k == "StartLimitBurst" for _, k, _, _ in svc)),
        ("timer schedule names its timezone",
         any(k == "OnCalendar" and ("Asia/" in v or "UTC" in v)
             for _, k, v, _ in tmr)),
        ("timer points at the service",
         any(k == "Unit" and v.startswith("atlas-market-hours")
             for _, k, v, _ in tmr)),
        ("report service is oneshot, not a daemon",
         any(k == "Type" and v == "oneshot" for _, k, v, _ in rep)),
        ("report service has NO [Install] (timer-activated)",
         not any(s_ == "Install" for s_, _, _, _ in rep)),
        ("report timer has [Install]",
         any(s_ == "Install" for s_, _, _, _ in rpt)),
        ("report fires AFTER the window closes",
         _report_after_close(rpt)),
    ]
    for label, good in checks:
        ok &= good
        print(f"  {label:<62}{'ok' if good else '** WRONG **'}")

    # The schedule and the code must agree about when the window opens, or the
    # timer fires at a time the service refuses to run at.
    print()
    print()
    print("THE MIS SQUARE-OFF UNIT")
    print("-" * 78)
    sq_t = (DEPLOY / "atlas-mis-squareoff.timer").read_text()
    sq_s = (DEPLOY / "atlas-mis-squareoff.service").read_text()
    sqt = parse(DEPLOY / "atlas-mis-squareoff.timer")
    sqs = parse(DEPLOY / "atlas-mis-squareoff.service")
    sq_checks = [
        ("timer fires at 15:15 IST",
         any(k == "OnCalendar" and "15:15" in v and "Asia/Kolkata" in v
             for _, k, v, _ in sqt)),
        ("  ahead of the broker's 15:20, and says so", "15:20" in sq_t),
        # Persistent=false is right HERE and nowhere else. A missed 15:15 cannot be
        # made up: by the time the box is back the exchange has already squared the
        # position off, so running late would place a closing order for a position
        # that no longer exists -- a new trade, not a square-off.
        ("Persistent is FALSE, unlike every other ATLAS timer",
         any(k == "Persistent" and v.lower() == "false" for _, k, v, _ in sqt)),
        ("  and the reason is written down, not just the value",
         "not a square-off" in sq_t or "new trade" in sq_t),
        ("service is oneshot",
         any(k == "Type" and v == "oneshot" for _, k, v, _ in sqs)),
        # [Install] on a timer-activated service makes it run at BOOT, which here
        # means placing closing orders at an arbitrary time of day.
        ("service has NO [Install]",
         not any(sec == "Install" for sec, _, _, _ in sqs)),
        ("timer has [Install]",
         any(sec == "Install" for sec, _, _, _ in sqt)),
        ("timer points at its own service",
         any(k == "Unit" and v.startswith("atlas-mis-squareoff")
             for _, k, v, _ in sqt)),
        ("runs regardless of ENABLE_EXIT_MANAGEMENT",
         "ENABLE_EXIT_MANAGEMENT" in sq_s),
    ]
    for label, good in sq_checks:
        ok &= good
        print(f"  {label:<62}{'ok' if good else '** WRONG **'}")


    print("SCHEDULE AGREES WITH THE CODE")
    print("-" * 78)
    sys.path.insert(0, str(ROOT))
    from atlas.signal.market_open import WINDOW_START      # noqa: E402
    cal = next((v for _, k, v, _ in tmr if k == "OnCalendar"), "")
    m = re.search(r"(\d{2}):(\d{2})", cal)
    good = bool(m) and (int(m.group(1)), int(m.group(2))) == (
        WINDOW_START.hour, WINDOW_START.minute)
    ok &= good
    print(f"  OnCalendar {cal!r}")
    print(f"  WINDOW_START {WINDOW_START.strftime('%H:%M')} IST")
    print(f"  {'they match:':<62}{'ok' if good else '** DRIFTED **'}")

    print("-" * 78)
    print("UNIT FILES:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
