"""
Reads trade route data directly from the UEXCorp skill's local SQLite database.
This mirrors the same data already cached by the UEXCorp skill — no extra API calls.
Falls back gracefully if the DB is not found.
"""
import contextlib
import glob
import logging
import os
import sqlite3
from typing import List, Optional

from uex_client import RouteData

logger = logging.getLogger(__name__)


def find_uexcorp_db() -> Optional[str]:
    """Locate the most recently modified UEXCorp SQLite database file."""
    appdata = os.environ.get("APPDATA", "") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    base = os.path.join(appdata, "ShipBit", "WingmanAI")

    patterns = [
        # Version-specific paths (e.g. 2_0_0, 1_8_1)
        os.path.join(base, "*", "skills", "uexcorp", "data", "*.db"),
        # Flat paths
        os.path.join(base, "skills", "uexcorp", "data", "*.db"),
        # Custom skills path variant
        os.path.join(base, "*", "custom_skills", "uexcorp", "data", "*.db"),
    ]

    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))

    if not candidates:
        return None

    # Return the most recently modified (most current data)
    return max(candidates, key=os.path.getmtime)


def read_routes_from_db(db_path: str) -> List[RouteData]:
    """
    Query the commodity_route table and return normalized RouteData objects.
    Returns an empty list on any error (caller falls back to API).
    """
    try:
        # Open read-only via PRAGMA (simpler, avoids pathlib.Path.as_uri() issues on Windows).
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute("""
                SELECT *
                FROM   commodity_route
                WHERE  is_profitable = 1
                ORDER  BY score DESC
            """)
            rows = cur.fetchall()

        routes = []
        for r in rows:
            rd = _row_to_route(r)
            if rd:
                routes.append(rd)
        return routes

    except sqlite3.Error as exc:
        logger.warning("[TradeHub] DB read error: %s", exc)
        return []


# ── Internal helpers ──────────────────────────────────────────────────────────

def _row_to_route(r: sqlite3.Row) -> Optional[RouteData]:
    """Map one commodity_route row to a RouteData object."""
    try:
        rd = RouteData()
        rd.commodity    = _col(r, "commodity_name")
        rd.buy_system   = _col(r, "terminal_origin_star_system_name")
        rd.buy_location = _best_location(r, "origin")
        rd.buy_terminal = _col(r, "terminal_origin_name")
        rd.sell_system  = _col(r, "terminal_destination_star_system_name")
        rd.sell_location= _best_location(r, "destination")
        rd.sell_terminal= _col(r, "terminal_destination_name")

        rd.price_buy  = float(_col(r, "price_buy")  or 0)
        rd.price_sell = float(_col(r, "price_sell") or 0)

        # scu_sell_stock = available to buy at origin terminal
        rd.scu_available = int(_col(r, "scu_sell_stock") or 0)
        rd.scu_demand    = int(_col(r, "scu_buy_stock")  or 0)

        rd.margin = float(_col(r, "profit_margin") or 0)
        if rd.margin == 0 and rd.price_sell > rd.price_buy:
            rd.margin = rd.price_sell - rd.price_buy

        if rd.price_buy > 0:
            rd.margin_pct = (rd.margin / rd.price_buy) * 100

        rd.score = float(_col(r, "score") or 0)
        rd.is_illegal = bool(int(_col(r, "commodity_is_illegal") or _col(r, "is_illegal") or 0))

        if rd.commodity and rd.margin > 0:
            return rd
        return None

    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("[TradeHub] Failed to parse route row: %s", exc)
        return None


def _col(row: sqlite3.Row, name: str) -> str:
    """Safe column access — returns '' if column doesn't exist."""
    try:
        val = row[name]
        return val if val is not None else ""
    except (IndexError, KeyError):
        return ""


def _best_location(row: sqlite3.Row, side: str) -> str:
    """Return the most specific non-empty location name for origin/destination."""
    prefix = f"terminal_{side}"
    for suffix in ("outpost_name", "city_name", "space_station_name",
                   "moon_name", "planet_name", "star_system_name"):
        val = _col(row, f"{prefix}_{suffix}").strip()
        if val:
            return val
    return ""
