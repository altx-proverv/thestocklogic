#!/usr/bin/env python3
"""EVERY DATASET WE READ HAS A ROW IN THE DATA-RIGHTS REGISTER.

compliance/data_rights.csv records, per dataset, what the source's terms let us
compute and show, and how sure we are. This file enumerates every place the code
pulls data from outside -- a URL, an API path on a vendor's base URL, a broker
SDK call -- and fails if the dataset behind it has no row.

Same shape as the writer registry in tests/test_feed_freshness.py: a new
ingestion path fails this suite until somebody classifies it. Classifying it
means writing a row, and a row whose every field says "unconfirmed" is a
legitimate row. Silence is the only thing this refuses.

THREE WAYS THIS COULD GO QUIET, EACH CLOSED BELOW:
  1. A new endpoint on a host we already know. A host allow-list would wave it
     through, so every URL and every base-URL path must match a DATASET
     detector or a NOT_A_DATASET pattern -- not just a known host.
  2. A detector whose code changed shape. A pattern that matches no file
     passes forever; each detector must match at least one module.
  3. A coverage check aimed at the wrong object. The bite-proof at the bottom
     deletes a row that is in use and plants a module with a new URL, and
     requires both to be reported.

NOT COVERED: lineage from an ingested dataset to the Supabase tables and pages
that publish what was derived from it. A row here is about the source; whether a
given page may show a value computed from it is the row's permitted_display.
"""
import ast
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._srcutil import executable_source          # noqa: E402

REGISTER = ROOT / "compliance" / "data_rights.csv"
COLUMNS = ["source", "dataset", "permitted_processing", "permitted_display",
           "user_count_limit", "update_frequency", "retention",
           "commercial_distribution", "terms_url", "verified_on", "status"]

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:24]:<26} {'ok' if ok else 'FAIL':<6} {note}")


def head(title):
    print()
    print(title)
    print("-" * 112)


# ══════════════════════════════════════════════════════════════════
# WHAT COUNTS AS READING A DATASET
# ══════════════════════════════════════════════════════════════════
# (source, dataset) must equal a register row's columns exactly; that pair is
# the key. Patterns run over EXECUTABLE source -- comments and docstrings are
# stripped, so a module explaining an endpoint it no longer calls is not a user.
DATASETS = [
    ("NSE", "Daily bhavcopy (CM bhavcopy and sec_bhavdata_full)",
     r"bhavcopy_save|sec_bhavdata_full"),
    ("NSE", "Trading holiday master (api/holiday-master)",
     r"api/holiday-master"),
    ("NSE", "Corporate announcements (api/corporate-announcements)",
     r"api/corporate-announcements"),
    ("NSE", "Shareholding pattern master (api/corporate-share-holdings-master)",
     r"api/corporate-share-holdings-master"),
    ("NSE", "Financial results listing (api/corporates-financial-results)",
     r"api/corporates-financial-results"),
    ("NSE", "Corporate XBRL filings (nsearchives corporate/xbrl)",
     r"\[['\"]xbrl['\"]\]"),
    ("NSE", "Index constituent lists (content/indices/*.csv)",
     r"content/indices/"),
    ("NSE", "Equity securities list (content/equities/sec_list.csv)",
     r"content/equities/sec_list"),
    ("NSE", "ETF list (api/etf)",
     r"nseindia\.com/api/etf"),
    ("NSE", "F&O market lots (content/fo/fo_mktlots.csv)",
     r"fo_mktlots"),
    ("NSE", "Event calendar: board meetings (api/event-calendar)",
     r"api/event-calendar"),
    ("NSE", "ASM surveillance list (api/reportASM)",
     r"api/reportASM"),
    ("NSE", "GSM surveillance list (api/reportGSM)",
     r"api/reportGSM"),
    ("Upstox", "NSE real-time market quotes (market-quote/quotes and market-quote/ohlc)",
     r"market-quote/(?:quotes|ohlc|ltp)"),
    ("Upstox", "Historical candles, daily and intraday (historical-candle)",
     r"historical-candle"),
    ("Upstox", "Option chain (option/chain)",
     r"option/chain"),
    ("Upstox", "Instrument master (assets.upstox.com market-quote/instruments)",
     r"market-quote/instruments"),
    ("Zerodha", "Kite instrument dump (kite.instruments / api.kite.trade/instruments)",
     r"\b(?:kite|_k)\.instruments\(|api\.kite\.trade/instruments"),
    ("Zerodha", "NSE market quotes via Kite (ltp / quote / ohlc / historical_data)",
     r"\b(?:kite|_k)\.(?:ltp|quote|ohlc|historical_data)\("),
    ("Zerodha", "NSE real-time tick stream via KiteTicker",
     r"\bKiteTicker\("),
    ("Zerodha", "Kite account data (orders, positions, holdings, margins, GTTs)",
     r"\b(?:kite|_k)\.(?:orders|positions|holdings|margins|get_gtts|order_history|trades)\("),
]

# Every external URL or base-URL path that is NOT a dataset, with the reason.
# An entry here is a decision and has to say why.
NOT_A_DATASET = [
    (r"\.supabase\.co|^\{[^}]*\}/rest/v1/",
     "our own database; what it holds was ingested through a row above"),
    (r"api\.telegram\.org|^\{[^}]*\}/(sendMessage|getUpdates|answerCallbackQuery)\b",
     "Telegram bot API: outbound alerts and operator commands"),
    (r"api\.anthropic\.com",
     "outbound model call. It is SENT derived market data -- a processing "
     "question for the source rows, not a dataset we read"),
    (r"fonts\.googleapis\.com|fonts\.gstatic\.com", "web fonts"),
    (r"unpkg\.com/@supabase/", "supabase-js client library"),
    (r"thestocklogic\.com", "our own site"),
    (r"^https?://kite\.trade/docs/", "documentation link"),
    (r"api\.upstox\.com/v2/?$|^https?://assets\.upstox\.com/market-quote/instruments/?$",
     "base URL constant; each path built on it is classified on its own"),
    (r"api\.upstox\.com/v2/login/|api\.upstox\.com/v2/user/profile$"
     r"|^\{[^}]*\}/user/profile$",
     "Upstox OAuth and token check: account auth, no market data"),
    (r"kite\.zerodha\.com/api/?$|^\{KITE_API\}/\{\}$",
     "Kite web login (login, twofa) used by the token refresh: account auth, "
     "no market data"),
    (r"^https?://(www\.)?nseindia\.com/?$|^https?://(www\.)?nseindia\.com/resources/?$",
     "NSE home page fetched only for session cookies before an API call"),
    (r"nseindia\.com/resources/exchange-communication-holidays$",
     "sent as a Referer header on the holiday-master call; never fetched"),
]

# Where an f-string's leading {name} looks like a base URL, the path after it is
# an endpoint and must be classified. A log line like f"{i}/{n} done" is not.
# The endpoint is reported as "{BASE_NAME}/path", so an exemption can name the
# base it trusts rather than every path shaped like it.
BASE_NAME = re.compile(r"url|base|api", re.I)
URL = re.compile(r"""https?://[^\s"'`<>()\\]+""")

SKIP_DIRS = {"venv", "tests", "node_modules", "legacy", "__pycache__", ".git",
             "data", "reports", "migrations", "docs"}
EXTS = {".py", ".js", ".html", ".sh"}


def strip_non_python(text):
    """Comments out of JS, HTML and shell. Crude, and enough: a URL that only
    lives in a comment is not a read."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?<![:\\'\"])//[^\n]*", "", text)
    text = re.sub(r"(?m)^\s*#(?!!).*$", "", text)
    return text


def source_of(path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix != ".py":
        return strip_non_python(raw), []
    try:
        src = executable_source(path)
    except SyntaxError:
        return raw, []
    paths = []
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.JoinedStr) and n.values
                and isinstance(n.values[0], ast.FormattedValue)
                and BASE_NAME.search(ast.unparse(n.values[0].value))):
            tail = "".join(v.value if isinstance(v, ast.Constant) else "{}"
                           for v in n.values[1:])
            if tail.startswith("/"):
                base = ast.unparse(n.values[0].value)
                paths.append("{" + base + "}" + tail.split("?")[0])
    return src, paths


def scan(files):
    """(usage, unclassified). usage: {(source, dataset): {module}}.
    unclassified: [(module, endpoint)] no detector and no exemption explains."""
    usage, unclassified = {}, []
    for rel, (src, paths) in files.items():
        for source, dataset, pat in DATASETS:
            if re.search(pat, src):
                usage.setdefault((source, dataset), set()).add(rel)
        for ep in URL.findall(src) + paths:
            ep = ep.rstrip(".,;")
            if any(re.search(p, ep) for _, _, p in DATASETS):
                continue
            if any(re.search(p, ep) for p, _ in NOT_A_DATASET):
                continue
            unclassified.append((rel, ep))
    return usage, sorted(set(unclassified))


def load_register(path=REGISTER):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


def unregistered(usage, rows):
    keys = {(r["source"], r["dataset"]) for r in rows}
    return sorted(k for k in usage if k not in keys)


files = {}
for f in sorted(ROOT.rglob("*")):
    rel = f.relative_to(ROOT)
    if (f.is_file() and f.suffix in EXTS
            and not any(p in SKIP_DIRS for p in rel.parts)):
        files[str(rel)] = source_of(f)

usage, unclassified = scan(files)
fieldnames, rows = load_register()


# ══════════════════════════════════════════════════════════════════
head("THE REGISTER IS WELL FORMED")
# ══════════════════════════════════════════════════════════════════
check("columns are exactly the register's columns", fieldnames, COLUMNS)
keys = [(r["source"], r["dataset"]) for r in rows]
check("no (source, dataset) appears twice",
      sorted(k for k in set(keys) if keys.count(k) > 1), [])
check("status is verified | provisional | unconfirmed",
      sorted({r["status"] for r in rows} - {"verified", "provisional", "unconfirmed"}), [])
check("commercial_distribution is yes | no | unconfirmed",
      sorted({r["commercial_distribution"] for r in rows} - {"yes", "no", "unconfirmed"}), [])
# A row cannot claim more certainty than its fields carry. "verified" with a
# blank URL, or with an unconfirmed field, is the optimistic yes this register
# exists to prevent.
check("a verified row has a terms_url, a date, and nothing unconfirmed",
      sorted(r["dataset"] for r in rows if r["status"] == "verified"
             and (not r["terms_url"] or not r["verified_on"]
                  or any("unconfirmed" in r[c].lower() for c in COLUMNS))), [])
check("  and a terms_url always carries the date it was read",
      sorted(r["dataset"] for r in rows if r["terms_url"] and not r["verified_on"]), [])
check("every row says something in every column but the URL and date",
      sorted(f"{r['dataset']}:{c}" for r in rows for c in COLUMNS
             if c not in ("terms_url", "verified_on") and not r[c].strip()), [])


# ══════════════════════════════════════════════════════════════════
head("THE ENUMERATION — EVERY DATASET IN USE, AND WHO READS IT")
# ══════════════════════════════════════════════════════════════════
for (source, dataset), mods in sorted(usage.items()):
    print(f"  {source:<8} {dataset}")
    for m in sorted(mods):
        print(f"             {m}")

# A DETECTOR THAT MATCHES NOTHING IS A TEST THAT HAS GONE QUIET. If the code
# reading a dataset changes shape, the pattern stops matching and the dataset
# drops out of the coverage check without anything failing.
print()
check("every detector matches at least one module",
      sorted(d for s, d, _ in DATASETS if (s, d) not in usage), [],
      "repoint it, or delete it if the read is gone")
check("no external endpoint is unclassified", unclassified, [],
      "add a DATASETS detector or a NOT_A_DATASET reason")


# ══════════════════════════════════════════════════════════════════
head("THE GATE — EVERY DATASET IN USE HAS A REGISTER ROW")
# ══════════════════════════════════════════════════════════════════
missing = unregistered(usage, rows)
for source, dataset in missing:
    print(f"  NO ROW  {source:<8} {dataset}")
    for m in sorted(usage[(source, dataset)]):
        print(f"            read by {m}")
check("every dataset in use has a register row", len(missing), 0,
      f"{len(usage)} datasets in use")

idle = sorted(k for k in keys if k not in usage)
for source, dataset in idle:
    print(f"  (row, no reader in code)  {source:<8} {dataset}")


# ══════════════════════════════════════════════════════════════════
head("BITE-PROOF — THE CHECKS ABOVE CAN FAIL")
# ══════════════════════════════════════════════════════════════════
# Delete, in memory, a row whose dataset IS in use. The coverage function has to
# name it. If it does not, the gate is aimed at something other than the rows.
in_use_rows = [r for r in rows if (r["source"], r["dataset"]) in usage]
check("the register has at least one row whose dataset is in use",
      bool(in_use_rows), True, "otherwise the next check proves nothing")
if in_use_rows:
    victim = in_use_rows[0]
    rest = [r for r in rows if r is not victim]
    check("deleting an in-use row is reported as unregistered",
          (victim["source"], victim["dataset"]) in unregistered(usage, rest), True,
          victim["dataset"][:40])

# A planted module that fetches from a host nothing knows about, and one that
# hits a NEW path on a known vendor base. Both must surface.
planted = {"engine/planted.py": (
    "import requests\n"
    "requests.get('https://data.example-vendor.com/v1/eod.csv')\n", []),
    "engine/planted2.py": ("", ["{BASE_URL}/market-quote/greeks"])}
_, caught = scan(planted)
check("a new external host is unclassified",
      ("engine/planted.py", "https://data.example-vendor.com/v1/eod.csv") in caught, True)
check("a new path on a known vendor base is unclassified",
      ("engine/planted2.py", "{BASE_URL}/market-quote/greeks") in caught, True)


print()
print("=" * 112)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASSED — {len(usage)} datasets in use, every one has a register row")
