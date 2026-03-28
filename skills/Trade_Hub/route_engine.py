"""
Trade route filtering, sorting, and calculation engine for Trade Hub.
"""
import os
import sys
from dataclasses import dataclass
from typing import List, Optional

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from shared.ships import scu_for_ship  # noqa: E402
from uex_client import RouteData  # noqa: E402

# Column keys for the table (includes legacy keys for sort-command compatibility)
COLUMN_KEYS = (
    "commodity",
    "buy_location",
    "buy_terminal",
    "buy_system",
    "sell_location",
    "sell_terminal",
    "sell_system",
    "available_scu",
    "scu_demand",
    "effective_scu",
    "margin_scu",
    "est_profit",
)


@dataclass
class FilterState:
    system: str = ""
    location: str = ""
    commodity: str = ""
    search: str = ""
    min_margin_scu: float = 0.0
    same_system_only: bool = False
    allow_illegal: bool = True


## scu_for_ship is imported from shared.ships


def apply_filters(routes: List[RouteData], filters: FilterState) -> List[RouteData]:
    """Return routes that satisfy all active filter criteria."""
    result = routes

    if not filters.allow_illegal:
        result = [r for r in result if not getattr(r, 'is_illegal', False)]

    if filters.system:
        s = filters.system.lower()
        result = [
            r for r in result
            if s in r.buy_system.lower() or s in r.sell_system.lower()
        ]

    if filters.location:
        loc = filters.location.lower()
        result = [
            r for r in result
            if (loc in r.buy_location.lower()
                or loc in r.buy_terminal.lower()
                or loc in r.buy_system.lower()
                or loc in r.sell_location.lower()
                or loc in r.sell_terminal.lower()
                or loc in r.sell_system.lower())
        ]

    if filters.commodity:
        c = filters.commodity.lower()
        result = [r for r in result if c in r.commodity.lower()]

    if filters.search:
        q = filters.search.lower()
        result = [
            r for r in result
            if any(q in x.lower() for x in [
                r.commodity, r.buy_location, r.buy_terminal,
                r.sell_location, r.sell_terminal,
                r.buy_system, r.sell_system,
            ])
        ]

    if filters.min_margin_scu > 0:
        result = [r for r in result if r.margin >= filters.min_margin_scu]

    if filters.same_system_only:
        result = [
            r for r in result
            if r.buy_system and r.buy_system == r.sell_system
        ]

    return result


def sort_routes(
    routes: List[RouteData],
    column: str,
    reverse: bool,
    ship_scu: int = 0,
) -> List[RouteData]:
    """Sort routes by the given column key."""
    key_map = {
        "commodity":     lambda r: r.commodity.lower(),
        "buy_location":  lambda r: r.buy_location.lower(),
        "buy_terminal":  lambda r: r.buy_terminal.lower(),
        "buy_system":    lambda r: r.buy_system.lower(),
        "sell_location": lambda r: r.sell_location.lower(),
        "sell_terminal": lambda r: r.sell_terminal.lower(),
        "sell_system":   lambda r: r.sell_system.lower(),
        "available_scu": lambda r: r.scu_available,
        "scu_demand":    lambda r: r.scu_demand,
        "effective_scu": lambda r: r.effective_scu(ship_scu),
        "margin_scu":    lambda r: r.margin,
        "est_profit":    lambda r: r.estimated_profit(ship_scu),
    }
    key_fn = key_map.get(column, lambda r: r.score)
    return sorted(routes, key=key_fn, reverse=reverse)


def get_unique_systems(routes: List[RouteData]) -> List[str]:
    systems: set = set()
    for r in routes:
        if r.buy_system:
            systems.add(r.buy_system)
        if r.sell_system:
            systems.add(r.sell_system)
    return sorted(systems)


def get_unique_commodities(routes: List[RouteData]) -> List[str]:
    return sorted({r.commodity for r in routes if r.commodity})


def profit_tier(margin: float) -> str:
    """Return 'high' (≥1000), 'med' (≥300), or 'low' (<300)."""
    if margin >= 1000:
        return "high"
    if margin >= 300:
        return "med"
    return "low"


## format_number removed — was dead code


def top_routes(
    routes: List[RouteData],
    ship_scu: int = 0,
    n: int = 10,
) -> List[RouteData]:
    """Return the top N routes ranked by estimated profit for the given ship."""
    ranked = sorted(routes, key=lambda r: r.estimated_profit(ship_scu), reverse=True)
    return ranked[:n]
