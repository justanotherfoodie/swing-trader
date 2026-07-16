"""S&P 500 universe.

The full list is embedded statically so a scan ALWAYS covers ~500 stocks regardless
of network conditions. Wikipedia is used only as an optional refresh; if it fails or
gets rate-limited (it 403s aggressively), we silently keep the baked-in list. The
constituents change only a few times a year, so a static snapshot is plenty fresh.
"""

import requests
from functools import lru_cache

# Snapshot of S&P 500 constituents (refreshed June 2026). ~500 tickers.
SP500_STATIC = [
    "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE", "ADI", "ADM",
    "ADP", "ADSK", "AEE", "AEP", "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM",
    "ALB", "ALGN", "ALL", "ALLE", "AMAT", "AMCR", "AMD", "AME", "AMGN", "AMP",
    "AMT", "AMZN", "ANET", "AON", "AOS", "APA", "APD", "APH", "APO", "APP",
    "APTV", "ARE", "ARES", "ATO", "AVB", "AVGO", "AVY", "AWK", "AXON", "AXP",
    "AZO", "BA", "BAC", "BALL", "BAX", "BBY", "BDX", "BEN", "BF-B", "BG",
    "BIIB", "BKNG", "BKR", "BLDR", "BLK", "BMY", "BNY", "BR", "BRK-B", "BRO",
    "BSX", "BX", "BXP", "C", "CAG", "CAH", "CARR", "CASY", "CAT", "CB",
    "CBOE", "CBRE", "CCI", "CCL", "CDNS", "CDW", "CEG", "CF", "CFG", "CHD",
    "CHRW", "CHTR", "CI", "CIEN", "CINF", "CL", "CLX", "CMCSA", "CME", "CMG",
    "CMI", "CMS", "CNC", "CNP", "COF", "COHR", "COIN", "COO", "COP", "COR",
    "COST", "CPAY", "CPRT", "CPT", "CRH", "CRL", "CRM", "CRWD", "CSCO", "CSGP",
    "CSX", "CTAS", "CTSH", "CTVA", "CVNA", "CVS", "CVX", "D", "DAL", "DASH",
    "DD", "DDOG", "DE", "DECK", "DELL", "DG", "DGX", "DHI", "DHR", "DIS",
    "DLR", "DLTR", "DOC", "DOV", "DOW", "DPZ", "DRI", "DTE", "DUK", "DVA",
    "DVN", "DXCM", "EA", "EBAY", "ECL", "ED", "EFX", "EG", "EIX", "EL",
    "ELV", "EME", "EMR", "EOG", "EQIX", "EQR", "EQT", "ERIE", "ES", "ESS",
    "ETN", "ETR", "EVRG", "EW", "EXC", "EXE", "EXPD", "EXPE", "EXR", "F",
    "FANG", "FAST", "FCX", "FDS", "FDX", "FE", "FFIV", "FICO", "FIS", "FITB",
    "FIX", "FLEX", "FOX", "FOXA", "FRT", "FSLR", "FTNT", "FTV", "GD", "GDDY",
    "GE", "GEHC", "GEN", "GEV", "GILD", "GIS", "GL", "GLW", "GM", "GNRC",
    "GOOG", "GOOGL", "GPC", "GPN", "GRMN", "GS", "GWW", "HAL", "HAS", "HBAN",
    "HCA", "HD", "HIG", "HII", "HLT", "HON", "HOOD", "HPE", "HPQ", "HRL",
    "HSIC", "HST", "HSY", "HUBB", "HUM", "HWM", "IBKR", "IBM", "ICE", "IDXX",
    "IEX", "IFF", "INCY", "INTC", "INTU", "INVH", "IP", "IQV", "IR", "IRM",
    "ISRG", "IT", "ITW", "IVZ", "J", "JBHT", "JBL", "JCI", "JKHY", "JNJ",
    "JPM", "KDP", "KEY", "KEYS", "KHC", "KIM", "KKR", "KLAC", "KMB", "KMI",
    "KO", "KR", "KVUE", "L", "LDOS", "LEN", "LH", "LHX", "LII", "LIN",
    "LITE", "LLY", "LMT", "LNT", "LOW", "LRCX", "LULU", "LUV", "LVS", "LYB",
    "LYV", "MA", "MAA", "MAR", "MAS", "MCD", "MCHP", "MCK", "MCO", "MDLZ",
    "MDT", "MET", "META", "MGM", "MKC", "MLM", "MMM", "MNST", "MO", "MOS",
    "MPC", "MPWR", "MRK", "MRNA", "MRVL", "MS", "MSCI", "MSFT", "MSI", "MTB",
    "MTD", "MU", "NCLH", "NDAQ", "NDSN", "NEE", "NEM", "NFLX", "NI", "NKE",
    "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS", "NUE", "NVDA", "NVR", "NWS",
    "NWSA", "NXPI", "O", "ODFL", "OKE", "OMC", "ON", "ORCL", "ORLY", "OTIS",
    "OXY", "PANW", "PAYX", "PCAR", "PCG", "PEG", "PEP", "PFE", "PFG", "PG",
    "PGR", "PH", "PHM", "PKG", "PLD", "PLTR", "PM", "PNC", "PNR", "PNW",
    "PODD", "PPG", "PPL", "PRU", "PSA", "PSX", "PTC", "PWR", "PYPL", "QCOM",
    "RCL", "REG", "REGN", "RF", "RJF", "RL", "RMD", "ROK", "ROL", "ROP",
    "ROST", "RSG", "RTX", "RVTY", "SBAC", "SBUX", "SCHW", "SHW", "SJM", "SLB",
    "SMCI", "SNA", "SNPS", "SO", "SOLV", "SPG", "SPGI", "SRE", "STE", "STLD",
    "STT", "STX", "STZ", "SWK", "SWKS", "SYF", "SYK", "SYY", "T", "TAP",
    "TDG", "TDY", "TECH", "TEL", "TER", "TFC", "TGT", "TJX", "TKO", "TMO",
    "TMUS", "TPL", "TPR", "TRGP", "TRMB", "TROW", "TRV", "TSCO", "TSLA", "TSN",
    "TT", "TTD", "TTWO", "TXN", "TXT", "TYL", "UAL", "UBER", "UDR", "UHS",
    "ULTA", "UNH", "UNP", "UPS", "URI", "USB", "V", "VEEV", "VICI", "VLO",
    "VLTO", "VMC", "VRSK", "VRSN", "VRT", "VRTX", "VST", "VTR", "VTRS", "VZ",
    "WAB", "WAT", "WBD", "WDAY", "WDC", "WEC", "WELL", "WFC", "WM", "WMB",
    "WMT", "WRB", "WSM", "WST", "WTW", "WY", "WYNN", "XEL", "XOM", "XYL",
    "YUM", "ZBH", "ZBRA", "ZTS",
]

# Nasdaq-100 (June 2026). Most overlap S&P 500; merge dedupes. Adds names like
# MELI, PDD, ASML, ARM, MSTR, SHOP, ZS that aren't in the S&P 500.
NASDAQ100_STATIC = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALNY", "AMAT", "AMD",
    "AMGN", "AMZN", "APP", "ARM", "ASML", "AVGO", "AXON", "BKNG", "BKR", "CCEP",
    "CDNS", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO", "CSX", "CTAS",
    "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG", "FAST", "FER", "FTNT",
    "GEHC", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INSM", "INTC", "INTU", "ISRG",
    "KDP", "KHC", "KLAC", "LIN", "LITE", "LRCX", "MAR", "MCHP", "MDLZ", "MELI",
    "META", "MNST", "MPWR", "MRVL", "MSFT", "MSTR", "MU", "NFLX", "NVDA", "NXPI",
    "ODFL", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM",
    "REGN", "ROP", "ROST", "SBUX", "SHOP", "SNDK", "SNPS", "STX", "TMUS", "TRI",
    "TSLA", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "WDC", "WMT", "XEL", "ZS",
]

# S&P MidCap 400 (June 2026). The quality mid-cap tier - liquid, real businesses,
# more swing-trade opportunities without the penny-stock junk of the full market.
MIDCAP400_STATIC = [
    "AA", "AAL", "AAON", "ACI", "ACM", "ADC", "AEIS", "AFG", "AGCO", "AHR",
    "AIT", "ALGM", "ALK", "ALLY", "ALV", "AM", "AMG", "AMH", "AMKR", "AN",
    "ANF", "APG", "APPF", "AR", "ARMK", "ARW", "ARWR", "ASB", "ASH", "ATI",
    "ATR", "AVAV", "AVNT", "AVT", "AVTR", "AXTA", "AYI", "BAH", "BBWI", "BC",
    "BCO", "BDC", "BHF", "BILL", "BIO", "BJ", "BKH", "BLD", "BMRN", "BRKR",
    "BROS", "BRX", "BSY", "BURL", "BWA", "BWXT", "BYD", "CACI", "CAR", "CART",
    "CAVA", "CBSH", "CBT", "CCK", "CDE", "CDP", "CELH", "CFR", "CG", "CGNX",
    "CHDN", "CHE", "CHH", "CHRD", "CHWY", "CLF", "CLH", "CMC", "CNH", "CNM",
    "CNO", "CNX", "COKE", "COLB", "COLM", "CPRI", "CR", "CRBG", "CROX", "CRS",
    "CRUS", "CSL", "CTRE", "CUBE", "CUZ", "CVLT", "CW", "CXT", "CYTK", "DAR",
    "DBX", "DCI", "DINO", "DKS", "DLB", "DOCN", "DOCS", "DOCU", "DT", "DTM",
    "DUOL", "DY", "EEFT", "EGP", "EHC", "ELAN", "ELF", "ELS", "ENS", "ENSG",
    "ENTG", "EPR", "EQH", "ESAB", "ESNT", "EVR", "EWBC", "EXEL", "EXLS", "EXP",
    "EXPO", "FAF", "FBIN", "FCFS", "FCN", "FFIN", "FHI", "FHN", "FIVE", "FLG",
    "FLR", "FLS", "FN", "FNB", "FND", "FNF", "FOUR", "FR", "FTI", "G",
    "GAP", "GATX", "GBCI", "GEF", "GGG", "GHC", "GLPI", "GME", "GMED", "GNTX",
    "GPK", "GT", "GTLS", "GWRE", "GXO", "H", "HAE", "HALO", "HGV", "HIMS",
    "HL", "HLI", "HLNE", "HOG", "HOMB", "HQY", "HR", "HRB", "HWC", "HXL",
    "IBOC", "IDA", "IDCC", "ILMN", "INGR", "IPGP", "IRT", "ITT", "JAZZ", "JEF",
    "JHG", "JLL", "KBH", "KBR", "KD", "KEX", "KNF", "KNSL", "KNX", "KRC",
    "KRG", "KTOS", "LAD", "LAMR", "LEA", "LECO", "LFUS", "LIVN", "LNTH", "LOPE",
    "LPX", "LSCC", "LSTR", "M", "MANH", "MAT", "MEDP", "MIDD", "MKSI", "MLI",
    "MMS", "MOG-A", "MORN", "MP", "MSA", "MSM", "MTDR", "MTG", "MTN", "MTSI",
    "MTZ", "MUR", "MUSA", "NBIX", "NEU", "NFG", "NJR", "NLY", "NNN", "NOV",
    "NOVT", "NSA", "NTNX", "NVST", "NVT", "NWE", "NXST", "NXT", "NYT", "OC",
    "OGE", "OGS", "OHI", "OKTA", "OLED", "OLLI", "OLN", "ONB", "ONTO", "OPCH",
    "ORA", "ORI", "OSK", "OVV", "OZK", "P", "PAG", "PATH", "PB", "PBF",
    "PCTY", "PEGA", "PEN", "PFGC", "PII", "PINS", "PK", "PLNT", "PNFP", "POR",
    "POST", "PPC", "PR", "PRI", "PSN", "PVH", "QLYS", "R", "RBA", "RBC",
    "REXR", "RGA", "RGEN", "RGLD", "RH", "RLI", "RMBS", "RNR", "ROIV", "ROKU",
    "RPM", "RRC", "RRX", "RS", "RYAN", "RYN", "SAIA", "SAIC", "SAM", "SANM",
    "SBRA", "SCI", "SEIC", "SF", "SFM", "SHC", "SIGI", "SIRI", "SITM", "SLAB",
    "SLGN", "SLM", "SMG", "SMTC", "SN", "SNX", "SON", "SPXC", "SR", "SSB",
    "SSD", "ST", "STAG", "STRL", "STWD", "SWX", "SYNA", "TCBI", "TEX", "THC",
    "THG", "THO", "TKR", "TLN", "TMHC", "TNL", "TOL", "TREX", "TRU", "TTC",
    "TTEK", "TTMI", "TWLO", "TXRH", "UBSI", "UFPI", "UGI", "UMBF", "UNM", "USFD",
    "UTHR", "VAL", "VC", "VFC", "VIAV", "VICR", "VLY", "VMI", "VNO", "VNOM",
    "VNT", "VOYA", "VVV", "WAL", "WBS", "WCC", "WEX", "WFRD", "WH", "WHR",
    "WING", "WLK", "WMG", "WMS", "WPC", "WSO", "WTFC", "WTRG", "WTS", "WWD",
    "XPO", "XRAY", "YETI", "ZION",
]


@lru_cache(maxsize=1)
def get_universe() -> list[str]:
    """Return the full tradeable universe: S&P 500 + Nasdaq-100 + S&P MidCap 400,
    deduplicated and sorted. ~1,000 quality, liquid names - no penny-stock junk.

    Tries a live Wikipedia refresh of the S&P 500 portion; the Nasdaq-100 and
    MidCap-400 always come from the embedded snapshots (they change rarely and
    Wikipedia rate-limits aggressively). Always returns ~1,000 tickers.
    """
    sp500 = SP500_STATIC
    try:
        from io import StringIO
        import pandas as pd
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=headers, timeout=10,
        )
        resp.raise_for_status()
        df = pd.read_html(StringIO(resp.text))[0]
        live = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        if len(live) >= 400:
            sp500 = live
    except Exception:
        pass  # keep embedded S&P 500 snapshot

    merged = sorted(set(sp500) | set(NASDAQ100_STATIC) | set(MIDCAP400_STATIC))
    print(f"[universe] Loaded {len(merged)} tickers "
          f"(S&P 500 + Nasdaq-100 + MidCap 400, deduplicated)")
    return merged


# Backwards-compatible alias: existing callers import get_sp500_tickers.
@lru_cache(maxsize=1)
def get_sp500_tickers() -> list[str]:
    return get_universe()


SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AMZN": "Consumer Discretionary", "META": "Communication Services",
    "GOOGL": "Communication Services", "TSLA": "Consumer Discretionary",
    "JPM": "Financials", "BAC": "Financials", "GS": "Financials",
    "XOM": "Energy", "CVX": "Energy", "UNH": "Healthcare",
    "LLY": "Healthcare", "JNJ": "Healthcare",
}
