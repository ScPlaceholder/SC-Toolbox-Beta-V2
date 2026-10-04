"""Offscreen tests: the shared shopping list in the standalone Star Map.

Until 2026-10-04 the Star Map had its own grocery panel (starmap/grocery.py,
its own file on disk) and ordered that list's stops itself
(route_planner.order_stops). There is one shopping list now (shared/shopping),
planned by Trade Hub's basket planner, and the map's part is:

  * dock THAT list (not a copy) beside the map;
  * add items found on the map to it, pinned to the place they were found at;
  * draw a planned route exactly in the planner's order - in one system,
    across systems, and again whenever the list changes - and take it off the
    map when the list is emptied or the pilot clears it.

No network (urllib is blocked), no microphone (the map has none), HOME is
redirected, and the shared list is pointed at a temp file.
"""

import os
import sys
import tempfile
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HOME = tempfile.mkdtemp(prefix="starmap_route_test_")
os.environ["HOME"] = os.environ["USERPROFILE"] = _HOME      # before any starmap import
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402


def _pump(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def panel(monkeypatch, tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import urllib.request

    def _no_network(*_a, **_k):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    from starmap import data, distances, lore
    from starmap import panel as panel_mod
    from shared.shopping import shopping_list as shop_mod
    for mod, attrs in ((data, ("_STATE_DIR",)), (distances, ("_CACHE_DIR",)),
                       (lore, ("_CACHE_DIR",))):
        for a in attrs:
            monkeypatch.setattr(mod, a, str(tmp_path))
    monkeypatch.setattr(data, "_STATE_PATH", str(tmp_path / "starmap_state.json"))
    monkeypatch.setattr(distances, "_CACHE_PATH", str(tmp_path / "distance_cache.json"))
    monkeypatch.setattr(distances, "_cache", {})
    monkeypatch.setattr(lore, "_CACHE_PATH", str(tmp_path / "lore_cache.json"))
    # the one shared list, in this test's own file
    monkeypatch.setattr(shop_mod, "_shared",
                        shop_mod.ShoppingList(path=str(tmp_path / "shopping_list.json")))

    p = panel_mod.StarmapPanel()
    assert p._galaxy is not None, "star map failed to build"
    p._galaxy.set_home("STANTON")
    p.resize(1100, 700)
    p.show()
    _pump(lambda: False, 0.05)
    yield p
    # Destroy now, not at interpreter exit (Qt teardown at exit can crash).
    p.hide()
    p.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()


def _item(i, location, system="Stanton"):
    return {"id": i, "name": f"Item {i}", "category": "", "price": 100,
            "location": location, "system": system}


def _plan(places, label="MIN STOPS"):
    """A Trade Hub BasketPlan's shape: stops in the planner's order, one pick each."""
    stops = []
    for i, (place, system) in enumerate(places):
        term = SimpleNamespace(terminal_id=100 + i, terminal_name=f"Shop - {place}",
                               location=place, system=system)
        pick = SimpleNamespace(commodity=f"item:Item {i}", price_buy=100.0, scu_available=0)
        stops.append(SimpleNamespace(terminal=term, picks=[pick], distance_from_prev_gm=1.0))
    return SimpleNamespace(stops=stops, label=label, total_distance_gm=float(len(stops)),
                           unresolved=[])


# ── it is the shared list, not a list of the map's own ────────────────────────

def test_the_docked_list_is_the_shared_list(panel):
    from shared.shopping import shared_list
    from shared.shopping.panel import ShoppingListPanel
    assert isinstance(panel._grocery, ShoppingListPanel)
    assert panel._grocery._list is shared_list()
    assert panel._btn_grocery.text() == "Shopping List"
    assert not hasattr(panel, "_plot_grocery_route"), "the map still plans grocery routes itself"
    import starmap
    assert not os.path.exists(os.path.join(os.path.dirname(starmap.__file__), "grocery.py"))


def test_an_item_added_from_the_map_lands_on_the_shared_list_pinned_to_its_place(panel):
    from shared.shopping import shared_list
    panel.add_to_shopping(_item(7, "Area 18"))
    panel.add_to_shopping(_item(7, "Area 18"))            # the same pop-out button, twice
    [e] = shared_list().entries()
    assert (e.kind, e.name, e.item_id, e.qty) == ("item", "Item 7", 7, 1)
    assert e.pin == {"location": "Area 18", "system": "Stanton", "price": 100.0}
    assert "Item 7" in panel._voicebar.status()
    # ... and it is what the docked panel shows, with the pin on the row
    row = panel._grocery.row("item", "Item 7")
    assert row is not None and row.pin_label is not None and "Area 18" in row.pin_label.text()


def test_the_market_panels_add_button_uses_the_same_path(panel, monkeypatch):
    from shared.shopping import shared_list
    monkeypatch.setattr(panel._market, "_selected_item", lambda: _item(3, "Lorville"))
    panel._market._add_grocery()
    assert [(e.name, e.pin["location"]) for e in shared_list().entries()] == [("Item 3", "Lorville")]


def test_borrowing_trade_hubs_planner_does_not_hand_it_the_starmap_package(panel):
    """Trade Hub ships a top-level package that is also called ``starmap``, and
    importing its data module puts Trade Hub's folder FIRST on sys.path. The list
    imports that module to plan a route. The Star Map's own package must already
    be the one in sys.modules, and stay it - including for a submodule the map
    only imports later (its tutorial)."""
    import importlib
    from shared.shopping.shopping_list import _basket_engine
    be = _basket_engine()                              # imports trade_hub_data + basket_engine
    assert hasattr(be, "plan_variants")
    here = os.path.normcase(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
    import starmap
    assert os.path.normcase(os.path.abspath(starmap.__file__)).startswith(here), starmap.__file__
    sys.modules.pop("starmap.tutorial", None)
    tut = importlib.import_module("starmap.tutorial")
    assert os.path.normcase(os.path.abspath(tut.__file__)).startswith(here), tut.__file__
    assert type(panel).__module__ == "starmap.panel"


# ── drawing a plan: the planner's order, untouched ───────────────────────────

def test_a_single_system_plan_is_drawn_in_the_planners_order(panel):
    from starmap.system_view import SystemView
    # Deliberately a zig-zag: if the map still re-ordered stops for the shortest trip
    # (the retired order_stops), this is the order it would not keep.
    names = ["Lorville", "New Babbage", "Area 18", "Orison"]
    panel._show_shopping_plan(_plan([(n, "Stanton") for n in names]))
    view = panel._stack.currentWidget()
    assert isinstance(view, SystemView), "a Stanton-only plan drew nothing"
    assert [p[0] for p in view.trade_route_points()] == names
    assert [p[4] for p in view.trade_route_points()] == ["stop:1", "stop:2", "stop:3", "stop:4"]
    assert panel.has_shopping_route()
    assert panel._btn_route.text() == "Clear route"


def test_a_multi_system_plan_draws_the_jump_route_and_each_systems_leg(panel):
    panel._show_shopping_plan(_plan([("Area 18", "Stanton"), ("Lorville", "Stanton"),
                                     ("Ruin Station", "Pyro"), ("Checkmate", "Pyro")]))
    g = panel._galaxy
    assert g._route_stops == ["STANTON", "PYRO"]
    assert g._route == g._route_stops
    legs = {}
    for code in ("STANTON", "PYRO"):
        panel._enter_system(code)
        legs[code] = [(p[0], p[4].split(":")[0]) for p in panel._stack.currentWidget().trade_route_points()]
        panel._go_galaxy()
    assert [n for n, r in legs["STANTON"] if r == "stop"] == ["Area 18", "Lorville"]
    assert [n for n, r in legs["PYRO"] if r == "stop"] == ["Ruin Station", "Checkmate"]
    assert legs["STANTON"][-1] == ("Pyro Gateway", "jump")
    assert legs["PYRO"][0] == ("Stanton Gateway", "jump")


# ── the drawn route follows the list ─────────────────────────────────────────

class _Source:
    """Prices and routes for the docked panel: nothing sells anything, no network."""

    def item_catalog(self): return {}
    def commodity_names(self): return []
    def start_terminals(self): return {}
    def routes(self): return []
    def item_prices(self, entries): return {}
    def dist_cache(self): return None


def test_a_replan_redraws_a_route_that_is_on_the_map(panel):
    from starmap.system_view import SystemView
    lst = panel._grocery
    first = _plan([("Lorville", "Stanton"), ("Area 18", "Stanton")])
    lst._plans = [first]
    lst.show_on_map(first)                       # the pilot clicks "Show on Star Map"
    assert isinstance(panel._stack.currentWidget(), SystemView)
    # the list changed and the planner came back with a different MIN STOPS route
    lst._plans = [_plan([("Orison", "Stanton")])]
    lst._follow_on_map()
    assert [p[0] for p in panel._stack.currentWidget().trade_route_points()] == ["Orison"]


def test_a_route_the_pilot_cleared_is_not_brought_back(panel):
    lst = panel._grocery
    first = _plan([("Lorville", "Stanton"), ("Area 18", "Stanton")])
    lst._plans = [first]
    lst.show_on_map(first)
    panel._route_clicked()                       # "Clear route" on the map itself
    assert not panel.has_shopping_route()
    lst._plans = [_plan([("Orison", "Stanton")])]
    lst._follow_on_map()
    assert not panel.has_shopping_route(), "a re-plan redrew a route the pilot had cleared"
    assert lst._map_live is None


def test_emptying_the_list_takes_its_route_off_the_map(panel, monkeypatch):
    from shared.shopping import shared_list
    lst = panel._grocery
    monkeypatch.setattr(lst, "_source", _Source())
    shared_list().add_item(_item(1, "Lorville"))
    first = _plan([("Lorville", "Stanton")])
    lst._plans = [first]
    lst.show_on_map(first)
    assert panel.has_shopping_route()
    shared_list().clear()
    assert not panel.has_shopping_route()
    assert panel._galaxy._route is None
    assert panel._btn_route.text() == "Route"


# ── inside a host that shows the list itself (the Everything Finder) ──────────

def test_with_a_host_the_button_opens_the_hosts_list_and_docks_nothing(panel):
    calls = []
    panel.set_shopping_host(lambda: calls.append("toggle"))
    panel._btn_grocery.click()
    assert calls == ["toggle"]
    assert panel._grocery.isHidden(), "a second copy of the list was docked beside the map"
    assert not panel._btn_grocery.isChecked()
    assert panel.cmd_toggle_grocery() == "shopping list toggled" and calls == ["toggle", "toggle"]


def test_without_a_host_the_button_docks_the_list(panel):
    panel._btn_grocery.click()
    assert not panel._grocery.isHidden()
    panel._btn_market.click()                    # at most one side view at a time
    assert panel._grocery.isHidden()
    assert panel.cmd_toggle_grocery() == "shopping list shown"
