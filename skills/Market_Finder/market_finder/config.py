"""Centralized configuration for Market Finder."""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Ensure shared is importable
# ---------------------------------------------------------------------------
import shared.path_setup  # noqa: E402  # centralised path config
from shared.qt.theme import P
from shared.api_config import (
    UEX_BASE_URL, MARKET_FINDER_HEADERS, MARKET_FINDER_TIMEOUT,
    CACHE_TTL_DEFAULT, CACHE_TTL_PRICE,
)

# ---------------------------------------------------------------------------
# Paths — Market_Finder root is one level up from this package directory
# ---------------------------------------------------------------------------
BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE: str = os.path.join(BASE_DIR, ".uex_cache.json")
SETTINGS_FILE: str = os.path.join(BASE_DIR, "uex_settings.json")

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
API_BASE: str = UEX_BASE_URL
API_TIMEOUT: int = MARKET_FINDER_TIMEOUT
API_MAX_RETRIES: int = 3
API_BACKOFF_BASE: float = 1.0
API_MAX_WORKERS: int = 8
API_HEADERS: dict[str, str] = MARKET_FINDER_HEADERS

# Config-driven category IDs (replaces hardcoded range(1, 91))
CATEGORY_IDS: list[int] = list(range(1, 91))

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
CACHE_VERSION: int = 3
CACHE_TTL: int = CACHE_TTL_DEFAULT
CACHE_TTL_OPTIONS: dict[str, int] = {
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "8h": 28800,
}

# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------
AUTO_REFRESH_MS: int = 3600 * 1000

# ---------------------------------------------------------------------------
# Item price cache limits
# ---------------------------------------------------------------------------
PRICE_CACHE_MAX_SIZE: int = 500
PRICE_CACHE_TTL: int = CACHE_TTL_PRICE  # 10 minutes

# ---------------------------------------------------------------------------
# Colors — now imported from shared palette
# ---------------------------------------------------------------------------
BG: str = P.bg_primary
BG2: str = P.bg_secondary
BG3: str = P.bg_card
BG4: str = P.bg_input
BORDER: str = P.border
FG: str = P.fg
FG_DIM: str = P.fg_dim
FG_DIMMER: str = P.fg_disabled
ACCENT: str = P.accent
GREEN: str = P.green
YELLOW: str = P.yellow
RED: str = P.red
ORANGE: str = P.orange
CYAN: str = P.energy_cyan
PURPLE: str = P.purple
HEADER_BG: str = P.bg_header
SECT_HDR_BG: str = P.bg_secondary
ROW_EVEN: str = P.bg_card
ROW_ODD: str = P.bg_input
ROW_HOVER: str = "#1e2840"
ROW_SEL: str = P.selection

CAT_COLORS: dict[str, str] = {
    "Armor": "#336699",
    "Weapons": "#993333",
    "Ship Weapons": "#883322",
    "Missiles": "#aa3344",
    "Clothing": "#336633",
    "Sustenance": "#886622",
    "Ship Components": "#334488",
    "Utility": "#554433",
    "Misc": "#443355",
    "Ships": "#335566",
    "Rentals": "#335544",
}

# ---------------------------------------------------------------------------
# Fonts and dimensions
# ---------------------------------------------------------------------------
FONT: str = "Consolas"
ROW_H: int = 26

# ---------------------------------------------------------------------------
# Tab definitions and item classification
# ---------------------------------------------------------------------------
SECTION_TO_TAB: dict[str, str] = {
    "Armor": "Armor",
    "Clothing": "Clothing",
    "Undersuits": "Clothing",
    "Personal Weapons": "Weapons",
    "Vehicle Weapons": "Ship Weapons",
    "Systems": "Ship Components",
    "Utility": "Utility",
    "Liveries": "Misc",
    "Miscellaneous": "Misc",
    "Other": "Misc",
    "Commodities": "Misc",
    "General": "Misc",
}

MISSILE_CATEGORIES: set[str] = {"Missiles", "Missile Racks"}
SUSTENANCE_CATEGORIES: set[str] = {"Foods", "Drinks"}

TAB_DEFS: list[tuple[str, str]] = [
    ("\U0001f50d", "All"),
    ("\U0001f6e1", "Armor"),
    ("\U0001f52b", "Weapons"),
    ("\U0001f455", "Clothing"),
    ("\U0001f4a5", "Ship Weapons"),
    ("\U0001f3af", "Missiles"),
    ("\u2699", "Ship Components"),
    ("\U0001f527", "Utility"),
    ("\U0001f356", "Sustenance"),
    ("\U0001f4e6", "Misc"),
    ("\U0001f6f8", "Ships"),
    ("\U0001f680", "Rentals"),
]

# ---------------------------------------------------------------------------
# Polling intervals (ms)
# ---------------------------------------------------------------------------
POLL_LOADING_MS: int = 200
POLL_COMMANDS_MS: int = 500
SEARCH_DEBOUNCE_MS: int = 300
AUTO_REFRESH_RETRY_MS: int = 30_000

# ---------------------------------------------------------------------------
# Display limits
# ---------------------------------------------------------------------------
SEARCH_BUBBLE_MAX: int = 30
SEARCH_BUBBLE_PER_TAB: int = 8
SHIP_TABLE_BATCH: int = 50
PRICE_DISPLAY_MAX: int = 20
PURCHASE_DISPLAY_MAX: int = 15
RENTAL_DISPLAY_MAX: int = 15

# ---------------------------------------------------------------------------
# Windows dark-mode attribute
# ---------------------------------------------------------------------------
DWMWA_USE_IMMERSIVE_DARK_MODE: int = 20


def item_tab(item: dict) -> str:
    """Determine which UI tab an item belongs to."""
    cat = item.get("category", "")
    if cat in SUSTENANCE_CATEGORIES:
        return "Sustenance"
    tab = SECTION_TO_TAB.get(item.get("section", ""), "Misc")
    if tab == "Ship Weapons" and cat in MISSILE_CATEGORIES:
        tab = "Missiles"
    return tab
