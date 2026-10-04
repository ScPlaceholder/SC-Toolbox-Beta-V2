"""Where the shopping list finds the two tools whose data and math it borrows.

The list plans routes with Trade Hub's planner and reads item prices through
Item Finder's data service. Both live in skill folders that are only on
``sys.path`` inside their own process, so whichever tool hosts the list (Item
Finder, the Star Map, the Everything Finder) adds them here, on first use.

Appended, never inserted: a host's own modules keep winning any name they
already own. Trade Hub also ships a top-level package called ``starmap``; the
standalone Star Map's package has the same name, and it must stay the one that
is imported in the Star Map's process. Nothing the list imports from Trade Hub
(``trade_hub_data``, ``basket_engine``) touches that package.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SKILLS_DIR = os.path.join(ROOT, "skills")
ITEM_FINDER_DIR = os.path.join(SKILLS_DIR, "Market_Finder")
TRADE_HUB_DIR = os.path.join(SKILLS_DIR, "Trade_Hub")


def _ensure(path: str) -> None:
    norm = os.path.normpath(path)
    if norm not in sys.path:
        sys.path.append(norm)


def ensure_trade_hub_path() -> None:
    """Make Trade Hub's flat modules importable (trade_hub_data, basket_engine)."""
    _ensure(TRADE_HUB_DIR)


def ensure_item_finder_path() -> None:
    """Make the ``market_finder`` package importable."""
    _ensure(ITEM_FINDER_DIR)
