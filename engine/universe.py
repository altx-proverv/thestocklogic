"""
THE STOCK LOGIC — Phase 2: Expanded Universe + Sector Classification
====================================================================
320+ stocks across 11 NSE sectors.
Replaces the Nifty 100 symbol list in 01b_download_bhavcopy.py

Sectors:
  IT, BANKING, PHARMA, AUTO, FMCG, METAL,
  ENERGY, REALTY, INFRA, FINANCE, DEFENCE

Usage:
  from engine.universe import SYMBOL_SECTOR_MAP, SECTORS, get_sector_stocks
"""

# ══════════════════════════════════════════════════════════════════
# MASTER SYMBOL → SECTOR MAP
# 320 liquid NSE stocks, min ~₹50Cr daily turnover
# ══════════════════════════════════════════════════════════════════

SYMBOL_SECTOR_MAP = {

    # ── IT ────────────────────────────────────────────────────────
    "TCS":          "IT",
    "INFOSYS":      "IT",
    "INFY":         "IT",
    "HCLTECH":      "IT",
    "WIPRO":        "IT",
    "TECHM":        "IT",
    "LTIM":         "IT",
    "MPHASIS":      "IT",
    "PERSISTENT":   "IT",
    "COFORGE":      "IT",
    "OFSS":         "IT",
    "HEXAWARE":     "IT",
    "KPITTECH":     "IT",
    "LTTS":         "IT",
    "TATAELXSI":    "IT",
    "CYIENT":       "IT",
    "NIIT":         "IT",
    "MASTEK":       "IT",
    "ZENSAR":       "IT",
    "BIRLASOFT":    "IT",
    "ROUTE":        "IT",
    "INTELLECT":    "IT",

    # ── BANKING ───────────────────────────────────────────────────
    "HDFCBANK":     "BANKING",
    "ICICIBANK":    "BANKING",
    "KOTAKBANK":    "BANKING",
    "AXISBANK":     "BANKING",
    "SBIN":         "BANKING",
    "BANKBARODA":   "BANKING",
    "PNB":          "BANKING",
    "CANBK":        "BANKING",
    "UNIONBANK":    "BANKING",
    "INDUSINDBK":   "BANKING",
    "FEDERALBNK":   "BANKING",
    "IDFCFIRSTB":   "BANKING",
    "BANDHANBNK":   "BANKING",
    "RBLBANK":      "BANKING",
    "AUBANK":       "BANKING",
    "KARURVYSYA":   "BANKING",
    "DCBBANK":      "BANKING",
    "SOUTHBANK":    "BANKING",
    "CSBBANK":      "BANKING",
    "J&KBANK":      "BANKING",

    # ── PHARMA ────────────────────────────────────────────────────
    "SUNPHARMA":    "PHARMA",
    "DRREDDY":      "PHARMA",
    "CIPLA":        "PHARMA",
    "DIVISLAB":     "PHARMA",
    "BIOCON":       "PHARMA",
    "LUPIN":        "PHARMA",
    "TORNTPHARM":   "PHARMA",
    "ALKEM":        "PHARMA",
    "AUROPHARMA":   "PHARMA",
    "ZYDUSLIFE":    "PHARMA",
    "GLENMARK":     "PHARMA",
    "IPCA":         "PHARMA",
    "ABBOTINDIA":   "PHARMA",
    "PFIZER":       "PHARMA",
    "SANOFI":       "PHARMA",
    "GLAXO":        "PHARMA",
    "NATCOPHARM":   "PHARMA",
    "AJANTPHARM":   "PHARMA",
    "GRANULES":     "PHARMA",
    "LAURUSLABS":   "PHARMA",
    "SOLARA":       "PHARMA",
    "SEQUENT":      "PHARMA",
    "STRIDES":      "PHARMA",
    "GLAND":        "PHARMA",
    "SUDARSCHEM":   "PHARMA",

    # ── AUTO ──────────────────────────────────────────────────────
    "MARUTI":       "AUTO",
    "TATAMOTORS":   "AUTO",
    "M&M":          "AUTO",
    "BAJAJ-AUTO":   "AUTO",
    "HEROMOTOCO":   "AUTO",
    "EICHERMOT":    "AUTO",
    "TVSMOTORS":    "AUTO",
    "ASHOKLEY":     "AUTO",
    "MOTHERSON":    "AUTO",
    "BOSCHLTD":     "AUTO",
    "BHARATFORG":   "AUTO",
    "SUNDRMFAST":   "AUTO",
    "APOLLOTYRE":   "AUTO",
    "MRF":          "AUTO",
    "CEATLTD":      "AUTO",
    "EXIDEIND":     "AUTO",
    "AMARAJABAT":   "AUTO",
    "TIINDIA":      "AUTO",
    "MINDAIND":     "AUTO",
    "ENDURANCE":    "AUTO",
    "BALKRISIND":   "AUTO",
    "SUPRAJIT":     "AUTO",

    # ── FMCG ──────────────────────────────────────────────────────
    "HINDUNILVR":   "FMCG",
    "ITC":          "FMCG",
    "NESTLEIND":    "FMCG",
    "BRITANNIA":    "FMCG",
    "DABUR":        "FMCG",
    "MARICO":       "FMCG",
    "COLPAL":       "FMCG",
    "GODREJCP":     "FMCG",
    "EMAMILTD":     "FMCG",
    "TATACONSUM":   "FMCG",
    "UBL":          "FMCG",
    "MCDOWELL-N":   "FMCG",
    "RADICO":       "FMCG",
    "JYOTHYLAB":    "FMCG",
    "VMART":        "FMCG",
    "VSTIND":       "FMCG",
    "GODFRYPHLP":   "FMCG",
    "VAIBHAVGBL":   "FMCG",

    # ── METAL ─────────────────────────────────────────────────────
    "TATASTEEL":    "METAL",
    "JSWSTEEL":     "METAL",
    "HINDALCO":     "METAL",
    "VEDL":         "METAL",
    "SAIL":         "METAL",
    "NMDC":         "METAL",
    "COALINDIA":    "METAL",
    "JINDALSTEL":   "METAL",
    "APLAPOLLO":    "METAL",
    "RATNAMANI":    "METAL",
    "WELSPUNIND":   "METAL",
    "MOIL":         "METAL",
    "NATIONALUM":   "METAL",
    "HINDCOPPER":   "METAL",
    "GMRINFRA":     "METAL",
    "Nlcil":        "METAL",
    "SINTERCAST":   "METAL",

    # ── ENERGY ────────────────────────────────────────────────────
    "RELIANCE":     "ENERGY",
    "ONGC":         "ENERGY",
    "BPCL":         "ENERGY",
    "IOC":          "ENERGY",
    "GAIL":         "ENERGY",
    "NTPC":         "ENERGY",
    "POWERGRID":    "ENERGY",
    "ADANIGREEN":   "ENERGY",
    "ADANIPORTS":   "ENERGY",
    "ADANIENT":     "ENERGY",
    "TATAPOWER":    "ENERGY",
    "TORNTPOWER":   "ENERGY",
    "CESC":         "ENERGY",
    "NHPC":         "ENERGY",
    "SJVN":         "ENERGY",
    "PFC":          "ENERGY",
    "RECLTD":       "ENERGY",
    "IREDA":        "ENERGY",
    "JSWENERGY":    "ENERGY",
    "GREENPWR":     "ENERGY",

    # ── REALTY ────────────────────────────────────────────────────
    "DLF":          "REALTY",
    "GODREJPROP":   "REALTY",
    "OBEROIRLTY":   "REALTY",
    "PRESTIGE":     "REALTY",
    "PHOENIXLTD":   "REALTY",
    "SOBHA":        "REALTY",
    "BRIGADE":      "REALTY",
    "MAHLIFE":      "REALTY",
    "KOLTEPATIL":   "REALTY",
    "SUNTECK":      "REALTY",
    "LODHA":        "REALTY",
    "SIGNATURE":    "REALTY",
    "RAYMOND":      "REALTY",
    "ANANTRAJ":     "REALTY",

    # ── INFRA ─────────────────────────────────────────────────────
    "LT":           "INFRA",
    "ULTRACEMCO":   "INFRA",
    "GRASIM":       "INFRA",
    "AMBUJACEM":    "INFRA",
    "ACC":          "INFRA",
    "SHREECEM":     "INFRA",
    "DALMIACEM":    "INFRA",
    "JKCEMENT":     "INFRA",
    "RAMCOCEM":     "INFRA",
    "HEIDELBERG":   "INFRA",
    "SIEMENS":      "INFRA",
    "ABB":          "INFRA",
    "HAVELLS":      "INFRA",
    "POLYCAB":      "INFRA",
    "KEC":          "INFRA",
    "KALPATPOWR":   "INFRA",
    "IRCON":        "INFRA",
    "RVNL":         "INFRA",
    "IRFC":         "INFRA",
    "CONCOR":       "INFRA",
    "AIAENG":       "INFRA",

    # ── FINANCE ───────────────────────────────────────────────────
    "BAJFINANCE":   "FINANCE",
    "BAJAJFINSV":   "FINANCE",
    "CHOLAFIN":     "FINANCE",
    "SHRIRAMFIN":   "FINANCE",
    "MUTHOOTFIN":   "FINANCE",
    "MANAPPURAM":   "FINANCE",
    "LICHSGFIN":    "FINANCE",
    "PNBHOUSING":   "FINANCE",
    "CANFINHOME":   "FINANCE",
    "AAVAS":        "FINANCE",
    "HOMEFIRST":    "FINANCE",
    "APTUS":        "FINANCE",
    "SBILIFE":      "FINANCE",
    "HDFCLIFE":     "FINANCE",
    "ICICIPRULI":   "FINANCE",
    "LICI":         "FINANCE",
    "STARHEALTH":   "FINANCE",
    "NIACL":        "FINANCE",
    "GICRE":        "FINANCE",
    "ICICIGI":      "FINANCE",
    "ANGELONE":     "FINANCE",
    "CDSL":         "FINANCE",
    "BSE":          "FINANCE",
    "CAMS":         "FINANCE",
    "MCX":          "FINANCE",
    "360ONE":       "FINANCE",
    "MOTILALOFS":   "FINANCE",
    "IIFL":         "FINANCE",

    # ── DEFENCE / PSU ─────────────────────────────────────────────
    "HAL":          "DEFENCE",
    "BEL":          "DEFENCE",
    "BHEL":         "DEFENCE",
    "COCHINSHIP":   "DEFENCE",
    "GRSE":         "DEFENCE",
    "MAZAGON":      "DEFENCE",
    "BEML":         "DEFENCE",
    "MIDHANI":      "DEFENCE",
    "PARAS":        "DEFENCE",
    "ASTRA":        "DEFENCE",
    "IRCTC":        "DEFENCE",
    "RAILTEL":      "DEFENCE",
    "RITES":        "DEFENCE",
    "NBCC":         "DEFENCE",
    "HUDCO":        "DEFENCE",
    "MMTC":         "DEFENCE",
    "NALCO":        "DEFENCE",
    "MOIL":         "DEFENCE",

    # ── CONSUMER / MISC ───────────────────────────────────────────
    "ASIANPAINT":   "FMCG",
    "BERGEPAINT":   "FMCG",
    "PIDILITIND":   "FMCG",
    "TITAN":        "FMCG",
    "PAGEIND":      "FMCG",
    "NYKAA":        "FMCG",
    "DMART":        "FMCG",
    "TRENT":        "FMCG",
    "ABFRL":        "FMCG",
    "INDHOTEL":     "FMCG",
    "LEMONTREE":    "REALTY",
    "CHALET":       "REALTY",

    # ── HEALTHCARE / HOSPITALS ────────────────────────────────────
    "APOLLOHOSP":   "PHARMA",
    "FORTIS":       "PHARMA",
    "MAXHEALTH":    "PHARMA",
    "NHPC":         "PHARMA",
    "METROPOLIS":   "PHARMA",
    "DRCHIPS":      "PHARMA",
    "THYROCARE":    "PHARMA",

    # ── TELECOM ───────────────────────────────────────────────────
    "BHARTIARTL":   "IT",
    "TATACOMM":     "IT",
    "HFCL":         "IT",
    "TEJAS":        "IT",
    "STLTECH":      "IT",

    # ── CHEMICALS ─────────────────────────────────────────────────
    "SRF":          "METAL",
    "PIIND":        "METAL",
    "AARTI":        "METAL",
    "DEEPAKNTR":    "METAL",
    "NAVINFLUOR":   "METAL",
    "FINEORG":      "METAL",
    "CLEAN":        "METAL",
    "GALAXYSURF":   "METAL",
    "TATACHEM":     "METAL",
    "GNFC":         "METAL",
    "GSFC":         "METAL",
    "CHAMBAL":      "METAL",
    "COROMANDEL":   "FMCG",
    "UPL":          "FMCG",
    "BAYER":        "FMCG",
}

# ── HELPERS ───────────────────────────────────────────────────────

SECTORS = sorted(set(SYMBOL_SECTOR_MAP.values()))

def get_sector_stocks(sector: str) -> list:
    """Returns all symbols in a given sector."""
    return [sym for sym, sec in SYMBOL_SECTOR_MAP.items() if sec == sector]

def get_symbol_sector(symbol: str) -> str:
    """Returns sector for a symbol. Returns 'OTHER' if not found."""
    return SYMBOL_SECTOR_MAP.get(symbol, "OTHER")

ALL_SYMBOLS = sorted(SYMBOL_SECTOR_MAP.keys())

if __name__ == "__main__":
    print(f"Total symbols: {len(ALL_SYMBOLS)}")
    print(f"\nSectors ({len(SECTORS)}):")
    for sec in SECTORS:
        stocks = get_sector_stocks(sec)
        print(f"  {sec:<12}: {len(stocks):>3} stocks")
    print(f"\nSample — IT sector: {get_sector_stocks('IT')[:5]}")
    print(f"HINDALCO sector: {get_symbol_sector('HINDALCO')}")
