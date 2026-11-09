#!/usr/bin/env python3
"""THE SECTOR MAP IS NSE'S, REPRODUCIBLE, AND HONEST ABOUT WHAT IT CANNOT PLACE (D-071).

engine/sector_map.json must be exactly what tools/build_sector_map.py makes
from the NSE files stored beside it; its labels are NSE's Industry verbatim
(Financial Services split BANKING / FINANCE by NSE bank-index membership) or
UNKNOWN; and nothing ranks UNKNOWN or a group under five members. Nothing is
fetched.
"""
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging                                                  # noqa: E402
logging.basicConfig(level=logging.CRITICAL)
import pandas as pd                                             # noqa: E402
from tools import build_sector_map as B                         # noqa: E402
from engine import universe as U                                # noqa: E402
from engine.sector_rank import split_rankable, MIN_RANKED_MEMBERS  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<66} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


stored = json.loads((ROOT / "engine" / "sector_map.json").read_text())
print("\nTHE FILE SAYS WHERE IT CAME FROM")
check("names its generator", stored.get("generator"), "tools/build_sector_map.py")
check("records a fetch time", bool(stored.get("fetched_at")), True)
check("Nifty Total Market was fetched (HTTP 200)",
      stored["sources"].get("ind_niftytotalmarket_list.csv", {}).get("status"), 200)
check("the bank lists were fetched", all(stored["sources"].get(n, {}).get("status") == 200
                                         for n in B.BANK_LISTS), True)

print("\nTHE GENERATOR REPRODUCES IT EXACTLY")
rebuilt = B.build(U.UNIVERSE_SYMBOLS)
for k in ("sectors", "counts", "conflicts", "sources", "fetched_at", "rule"):
    check(f"rebuilt {k} == stored {k}", rebuilt[k] == stored[k], True)

print("\nLABELS ARE NSE'S, OR UNKNOWN")
nse_industries = set()
for n in B.INDUSTRY_LISTS:
    nse_industries |= {r["Industry"] for r in B._read(n) if r.get("Industry")}
allowed = (nse_industries - {B.FINANCIAL}) | {"BANKING", "FINANCE", "UNKNOWN"}
check("every label is an NSE industry, BANKING, FINANCE or UNKNOWN",
      sorted(set(U.SYMBOL_SECTOR_MAP.values()) - allowed), [])
check("every universe symbol has a label", set(U.SYMBOL_SECTOR_MAP) == set(U.UNIVERSE_SYMBOLS), True)
banks = set()
for n in B.BANK_LISTS:
    banks |= {r["Symbol"] for r in B._read(n)}
check("BANKING is exactly the NSE bank-index members we follow",
      sorted(s for s, v in U.SYMBOL_SECTOR_MAP.items() if v == "BANKING"),
      sorted(s for s in U.UNIVERSE_SYMBOLS if s in banks and rebuilt["sectors"][s] != "UNKNOWN"))
check("the folding is gone: VOLTAS is Consumer Durables", U.get_symbol_sector("VOLTAS"), "Consumer Durables")
check("  THELEELA is Consumer Services", U.get_symbol_sector("THELEELA"), "Consumer Services")
check("  COALINDIA is Oil Gas & Consumable Fuels", U.get_symbol_sector("COALINDIA"),
      "Oil Gas & Consumable Fuels")
check("a symbol we do not follow is UNKNOWN, not OTHER", U.get_symbol_sector("NOTASYMBOL"), "UNKNOWN")
src = (ROOT / "engine" / "universe.py").read_text()
check("universe.py no longer claims to be auto-generated",
      "Auto-generated. Do not edit manually" in src, False)
check("universe.py holds no literal sector labels",
      bool(re.search(r'"[A-Z0-9&_-]+":\s*"(FMCG|INFRA|METAL|PHARMA|ENERGY|AUTO|DEFENCE|TELECOM|REALTY|IT|OTHER)"', src)),
      False)

print("\nA CHANGED NSE FILE CHANGES THE MAP (the reproduction check bites)")
tmp = Path(tempfile.mkdtemp())
shutil.copytree(B.SRC_DIR, tmp / "src")
_orig = B.SRC_DIR
try:
    B.SRC_DIR = tmp / "src"
    p = B.SRC_DIR / "ind_niftytotalmarket_list.csv"
    p.write_text(p.read_text().replace("Consumer Durables,VOLTAS", "Capital Goods,VOLTAS"))
    check("a planted relabel in the stored NSE file is caught",
          B.build(U.UNIVERSE_SYMBOLS)["sectors"]["VOLTAS"] != stored["sectors"]["VOLTAS"], True)
finally:
    B.SRC_DIR = _orig

print("\nNOTHING RANKS UNKNOWN OR A THIN GROUP")
# LITERAL COUNTS, not derived from the constant: a test built from the value it
# checks moves with it. The operator's rule is five (2026-11-10).
check("the minimum is the operator's five", MIN_RANKED_MEMBERS, 5)
df = pd.DataFrame({"sector": ["A", "B", "UNKNOWN", "C", "OTHER"],
                   "stock_count": [5, 4, 40, 30, 9]})
r, u = split_rankable(df)
check("ranked: groups with >= 5 members, not UNKNOWN/OTHER", sorted(r.sector), ["A", "C"])
check("unranked: the 4-member group, UNKNOWN, OTHER", sorted(u.sector), ["B", "OTHER", "UNKNOWN"])
for f in ("engine/upstox_ws.py", "engine/07_sector_momentum.py"):
    check(f"{f} ranks only split_rankable's groups", "split_rankable(sector_df)" in (ROOT / f).read_text(), True)
_home = (ROOT / "home.html").read_text()
check("home.html's reading uses the same minimum", "var MIN_RANKED_MEMBERS=5;" in _home
      and "s.counted>=MIN_RANKED_MEMBERS" in _home, True)
from atlas.execution import session_selector as SS                # noqa: E402
import inspect                                                     # noqa: E402
check("session_selector does not treat two UNKNOWNs as one sector",
      'if sec in ("UNKNOWN", "OTHER"):' in inspect.getsource(SS), True)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the sector map is NSE's, reproducible, and UNKNOWN is never ranked")
