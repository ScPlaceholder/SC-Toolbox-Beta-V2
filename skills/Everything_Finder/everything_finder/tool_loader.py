# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
"""Import the three existing tools, each only when its tab is first opened.

Nothing here runs at import time: every loader is a function, and each one
imports its tool inside the function body. That is the "streamed in as normal"
J asked for - opening the Everything Finder imports exactly one tool.

Module-name hygiene (the reason this file exists rather than three one-liners):

* Item Finder lives in the ``market_finder`` package - unique, no clash.
* Trade Hub uses flat top-level modules (``trade_hub_data``, ``basket_engine``,
  ``basket_view``...) AND a top-level package called ``starmap`` (its own star
  map, Trade_Hub/starmap). It must keep owning that name: trade_hub_app imports
  ``starmap.panel`` at load time and ``starmap.price_log`` lazily later.
* The standalone Star Map tool (skills/Starmap) ALSO calls its package
  ``starmap``. Importing it by that name would hand Trade Hub the wrong package
  (or the reverse, depending on which tab opened first). So the Star Map tab
  loads skills/Starmap/starmap under the alias ``ef_starmap``; the package uses
  only relative imports internally, so it works unchanged under any name.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from types import ModuleType

SKILLS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
ITEM_FINDER_DIR = os.path.join(SKILLS_DIR, "Market_Finder")
TRADE_HUB_DIR = os.path.join(SKILLS_DIR, "Trade_Hub")
STARMAP_PKG_DIR = os.path.join(SKILLS_DIR, "Starmap", "starmap")
STARMAP_ALIAS = "ef_starmap"


# One implementation of "put Trade Hub / Item Finder on sys.path": the shared
# shopping list needs the same two folders in every tool that hosts it.
from shared.shopping.paths import ensure_item_finder_path, ensure_trade_hub_path  # noqa: E402,F401


def load_item_finder_window(x: int, y: int, w: int, h: int, opacity: float):
    """Construct Item Finder exactly as its entry script does (minus the QApplication)."""
    ensure_item_finder_path()
    from market_finder.service import DataService
    from market_finder.ui.app import MarketFinderApp
    data = DataService()
    return MarketFinderApp(data, x=x, y=y, w=w, h=h, opacity=opacity, cmd_file=None)


def load_trade_hub_window(x: int, y: int, w: int, h: int, opacity: float):
    """Construct Trade Hub exactly as its entry script does, with the launcher's
    custom args (refresh 300 s, 500 routes) and no IPC command file."""
    ensure_trade_hub_path()
    mod = importlib.import_module("trade_hub_app")
    return mod.TradeHubWindow(cmd_file="", x=x, y=y, w=w, h=h,
                              refresh_interval=300.0, max_routes=500, opacity=opacity)


def import_starmap_package() -> ModuleType:
    """skills/Starmap/starmap imported as ``ef_starmap`` (see module docstring)."""
    mod = sys.modules.get(STARMAP_ALIAS)
    if mod is not None:
        return mod
    init = os.path.join(STARMAP_PKG_DIR, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        STARMAP_ALIAS, init, submodule_search_locations=[STARMAP_PKG_DIR])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the Star Map package from {STARMAP_PKG_DIR}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[STARMAP_ALIAS] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(STARMAP_ALIAS, None)
        raise
    return mod


def load_starmap_panel():
    """The one Star Map: skills/Starmap's StarmapPanel (see the report / README)."""
    import_starmap_package()
    panel_mod = importlib.import_module(STARMAP_ALIAS + ".panel")
    return panel_mod.StarmapPanel(cmd_file="")
