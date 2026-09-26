"""Pure helpers for the Grocery List feature.

No Qt imports here — this module only normalizes the raw price payload
returned by :meth:`DataService.fetch_item_prices` into the rows the
grocery list bubble renders.  Keeping it Qt-free makes it unit-testable.
"""

from __future__ import annotations

from typing import Any

# Location fields, ordered from coarse (system) to fine (station), exactly
# as the detail panel joins them with " > ".
LOCATION_FIELDS: tuple[str, ...] = (
    "star_system_name",
    "planet_name",
    "moon_name",
    "city_name",
    "space_station_name",
)


# Where a terminal physically is, finest first.  The route planner keys a
# "site" on the first populated one (terminals at the same station / city /
# outpost are one stop) and resolves it to map coordinates, so unlike
# LOCATION_FIELDS this includes the outpost.
PLACE_FIELDS: tuple[str, ...] = (
    "space_station_name",
    "outpost_name",
    "city_name",
    "moon_name",
    "planet_name",
)


def place_names(price: dict[str, Any]) -> list[str]:
    """The populated place names of a price row, finest first, deduplicated."""
    out: list[str] = []
    for f in PLACE_FIELDS:
        v = str(price.get(f) or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def format_location(price: dict[str, Any]) -> str:
    """Join the populated location fields of a price row with ``>``."""
    parts = [str(price[f]) for f in LOCATION_FIELDS if price.get(f)]
    return " > ".join(parts)


def format_price(value: Any) -> str:
    """Format an aUEC amount for display (e.g. ``1,234 aUEC``)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "—"
    return f"{n:,.0f} aUEC"


def buy_locations(prices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the terminals that *sell* an item, cheapest first.

    The grocery list is a shopping list, so we surface buy prices —
    what the player pays at a terminal (``price_buy``).  Each returned
    row is a small normalized dict: ``{"terminal", "terminal_id", "system",
    "location", "places", "price"}`` (``places`` = :func:`place_names`).
    Rows without a positive buy price are dropped.
    """
    rows: list[dict[str, Any]] = []
    for p in prices:
        raw = p.get("price_buy")
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        rows.append({
            "terminal": p.get("terminal_name") or "Unknown",
            "terminal_id": p.get("id_terminal") or 0,
            "system": p.get("star_system_name") or "",
            "location": format_location(p),
            "places": place_names(p),
            "price": price,
        })
    rows.sort(key=lambda r: r["price"])
    return rows
