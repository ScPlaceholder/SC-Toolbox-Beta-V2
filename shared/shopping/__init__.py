"""The toolbox's ONE shopping list (J, 2026-10-04).

There were three: the Everything Finder's "Shopping List" pop-out, Item
Finder's "Grocery List" bubble and the Star Map's docked Grocery panel. Three
stores, three widgets and three route planners, none of which saw what was
added in another. This package is the one that is left, used by all three
tools:

  * shopping_list - the data model (:class:`ShoppingList`, one file on disk,
    shared across processes) and route planning, which is Trade Hub's basket
    planner and nothing else.
  * source        - where prices come from: Item Finder's data service for
    items, Trade Hub's routes for commodities, Trade Hub's distance cache.
  * panel         - the one widget (:class:`ShoppingListPanel`, embeddable) and
    its pop-out frame (:class:`ShoppingListWindow`).

Importing this package pulls in no Qt and neither tool; ``panel`` does.
"""
from .shopping_list import (  # noqa: F401
    KINDS, STRATEGIES, Entry, ShoppingList, shared_list,
    build_index, plan_routes, plan_stops_for_map, plan_summary,
)
