"""Pure helpers for the grocery list's "Plot Optimal Route" feature.

No Qt imports here — this module only turns the grocery cards' buy rows
into an ordered list of shopping stops.  Distance between two stops comes
from UEX terminal telemetry (see :mod:`market_finder.starmap.distances`);
when a pair has no telemetry we fall back to the galaxy jump-graph so the
route still orders sensibly.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple


def collect_stops(cards: List[Any]) -> List[dict]:
    """One stop per grocery card: its cheapest buy row's terminal + system.

    Cards whose prices have not loaded yet (or have no buy rows) are
    skipped — the caller flashes a hint for those.
    """
    stops: List[dict] = []
    for card in cards:
        rows = getattr(card, "_buy_rows", None) or []
        if not rows or not getattr(card, "_loaded", False):
            continue
        best = rows[0]
        stops.append({
            "item_id": card.item_id(),
            "name": (card._item.get("name") or "Unknown"),
            "terminal": best.get("terminal") or "",
            "terminal_id": best.get("terminal_id") or 0,
            "system": best.get("system") or "",
            "price": best.get("price") or 0,
        })
    return stops


def missing_price_cards(cards: List[Any]) -> List[Any]:
    """Cards that could not contribute a stop (prices still loading / none)."""
    return [c for c in cards
            if not getattr(c, "_loaded", False) or not (getattr(c, "_buy_rows", None) or [])]


def unique_terminals(stops: List[dict]) -> List[Tuple[int, str]]:
    """Deduplicated (terminal_id, system) pairs, order-preserving."""
    seen: Dict[int, str] = {}
    for s in stops:
        tid = int(s.get("terminal_id") or 0)
        if tid and tid not in seen:
            seen[tid] = s.get("system") or ""
    return list(seen.items())


def order_stops(stops: List[dict],
                dist_fn: Callable[[dict, dict], Optional[float]],
                start: Optional[dict] = None) -> List[dict]:
    """Nearest-neighbour ordering of the shopping stops.

    *dist_fn(a, b)* returns the travel distance between two stop dicts, or
    None when unknown (treated as infinite — those pairs are avoided but
    never dropped).  *start* (e.g. the pilot's home system) is visited
    first when given.  Returns a new list; empty/one-stop inputs are
    returned as-is.
    """
    remaining = list(stops)
    if len(remaining) < 2:
        return remaining
    ordered: List[dict] = []
    if start is not None:
        current = start
        # The start is a synthetic stop (e.g. home); find the nearest real one.
        remaining.sort(key=lambda s: (_dist_or_inf(dist_fn, current, s),
                                      (s.get("name") or "")))
        ordered.append(remaining.pop(0))
        current = ordered[-1]
    else:
        ordered.append(remaining.pop(0))
        current = ordered[-1]
    while remaining:
        remaining.sort(key=lambda s: (_dist_or_inf(dist_fn, current, s),
                                      (s.get("name") or "")))
        current = remaining.pop(0)
        ordered.append(current)
    return ordered


def _dist_or_inf(dist_fn: Callable[[dict, dict], Optional[float]],
                 a: dict, b: dict) -> float:
    try:
        d = dist_fn(a, b)
    except Exception:
        d = None
    return d if (d is not None and d >= 0) else float("inf")
