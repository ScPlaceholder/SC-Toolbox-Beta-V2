"""Centralized API configuration for all SC Toolbox skills.

Every external service URL, User-Agent string, default timeout, and cache TTL
lives here so that a single edit propagates everywhere.
"""

# ---------------------------------------------------------------------------
# UEX Corp  (Trade Hub, Market Finder, Cargo Loader, Mining Loadout)
# ---------------------------------------------------------------------------
UEX_BASE_URL: str = "https://api.uexcorp.space/2.0"

UEX_USER_AGENT: str = "WingmanAI-TradeHub/1.0"
UEX_HEADERS: dict[str, str] = {
    "User-Agent": UEX_USER_AGENT,
    "Accept": "application/json",
}

UEX_TIMEOUT: int = 30          # seconds — used by Trade Hub, Market Finder
UEX_MINING_TIMEOUT: int = 25   # seconds — Mining Loadout requests

# ---------------------------------------------------------------------------
# Erkul / DPS Calculator
# ---------------------------------------------------------------------------
ERKUL_BASE_URL: str = "https://server.erkul.games"

ERKUL_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
ERKUL_HEADERS: dict[str, str] = {
    "User-Agent":      ERKUL_USER_AGENT,
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://www.erkul.games",
    "Referer":         "https://www.erkul.games/",
}
ERKUL_TIMEOUT: int = 20        # seconds — ErkulApiClient default
ERKUL_LOADOUT_TIMEOUT: int = 15  # seconds — ship loadout fetch
ERKUL_VERSION_TIMEOUT: int = 10  # seconds — game version check

# ---------------------------------------------------------------------------
# FleetYards
# ---------------------------------------------------------------------------
FLEETYARDS_BASE_URL: str = "https://api.fleetyards.net/v1"

FLEETYARDS_HEADERS: dict[str, str] = {
    "User-Agent": ERKUL_USER_AGENT,
    "Accept":     "application/json",
}
FLEETYARDS_TIMEOUT: int = 15   # seconds — FleetyardsApiClient default

# ---------------------------------------------------------------------------
# SC Cargo Space  (Cargo Loader)
# ---------------------------------------------------------------------------
SC_CARGO_BASE_URL: str = "https://sc-cargo.space"

SC_CARGO_USER_AGENT: str = ERKUL_USER_AGENT  # browser-style UA required

SC_CARGO_HEADERS: dict[str, str] = {
    "User-Agent": SC_CARGO_USER_AGENT,
}
SC_CARGO_HOMEPAGE_TIMEOUT: int = 10   # seconds — homepage fetch
SC_CARGO_BUNDLE_TIMEOUT: int = 25     # seconds — JS bundle fetch
SC_CARGO_UEX_TIMEOUT: int = 15        # seconds — UEX commodities from cargo

# ---------------------------------------------------------------------------
# SCMDB  (Mission Database)
# ---------------------------------------------------------------------------
SCMDB_BASE_URL: str = "https://scmdb.net"

SCMDB_HEADERS: dict[str, str] = {
    "User-Agent": ERKUL_USER_AGENT,
    "Accept":     "application/json, text/plain, */*",
}
SCMDB_TIMEOUT: int = 30        # seconds

# ---------------------------------------------------------------------------
# Market Finder  (UEX-based, but has its own User-Agent)
# ---------------------------------------------------------------------------
MARKET_FINDER_USER_AGENT: str = "MarketFinder/1.0"
MARKET_FINDER_HEADERS: dict[str, str] = {
    "User-Agent": MARKET_FINDER_USER_AGENT,
    "Accept": "application/json",
}
MARKET_FINDER_TIMEOUT: int = 30  # seconds

# ---------------------------------------------------------------------------
# Mining Loadout  (UEX-based, own User-Agent)
# ---------------------------------------------------------------------------
MINING_LOADOUT_USER_AGENT: str = "WingmanAI-MiningLoadout/1.0"

# ---------------------------------------------------------------------------
# Cache TTL defaults  (seconds)
# ---------------------------------------------------------------------------
CACHE_TTL_SHORT: int = 300          # 5 min  — Trade Hub in-memory cache
CACHE_TTL_PRICE: int = 600          # 10 min — Market Finder price cache
CACHE_TTL_DEFAULT: int = 3600       # 1 h    — Market Finder disk cache
CACHE_TTL_ERKUL: int = 2 * 3600     # 2 h    — DPS Calculator / Mission DB
CACHE_TTL_CARGO: int = 6 * 3600     # 6 h    — Cargo Loader / FY hardpoints
CACHE_TTL_MINING: int = 86400       # 24 h   — Mining Loadout API cache
