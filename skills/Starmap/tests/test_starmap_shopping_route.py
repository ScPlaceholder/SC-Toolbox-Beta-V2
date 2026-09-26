"""Offscreen tests for the standalone Starmap's grocery "Plot shopping route".

The Starmap grocery list pins each item to the location it was added from, so
the calculator's job here is the ORDER: stops must be visited in the shortest
order (checked against brute force) and drawn in that order on the map --
also when the whole route stays inside one system, and again whenever the
list changes.

No network (urllib is blocked), no voice/mic (ears are not built), and HOME
is redirected so ~/.sctoolbox is never read or written.
"""

import itertools
import os
import sys
import tempfile
import time

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

    from starmap import data, distances, grocery, lore
    from starmap import panel as panel_mod
    for mod, attrs in ((data, ("_STATE_DIR",)), (distances, ("_CACHE_DIR",)),
                       (grocery, ("_STORE_DIR",)), (lore, ("_CACHE_DIR",))):
        for a in attrs:
            monkeypatch.setattr(mod, a, str(tmp_path))
    monkeypatch.setattr(data, "_STATE_PATH", str(tmp_path / "starmap_state.json"))
    monkeypatch.setattr(distances, "_CACHE_PATH", str(tmp_path / "distance_cache.json"))
    monkeypatch.setattr(distances, "_cache", {})
    monkeypatch.setattr(grocery, "_STORE_PATH", str(tmp_path / "grocery.json"))
    monkeypatch.setattr(lore, "_CACHE_PATH", str(tmp_path / "lore_cache.json"))
    monkeypatch.setattr(panel_mod.StarmapPanel, "_build_ears", lambda self: None)
    assert os.path.expanduser("~") == _HOME

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


def _order_cost(names, system="Stanton"):
    from starmap.distances import site_distance
    sites = [{"system": system, "places": [n]} for n in names]
    return sum(site_distance(a, b) for a, b in zip(sites, sites[1:]))


def test_single_system_list_is_drawn_in_the_shortest_order(panel):
    from starmap.system_view import SystemView
    # Added in a zig-zag order: Lorville, New Babbage, Area 18, Orison.
    names = ["Lorville", "New Babbage", "Area 18", "Orison"]
    for i, n in enumerate(names):
        panel._grocery.add_item(_item(i, n))
    panel._plot_grocery_route()
    view = panel._stack.currentWidget()
    assert isinstance(view, SystemView), "a Stanton-only list drew nothing"
    drawn = [p[0] for p in view.trade_route_points()]
    assert sorted(drawn) == sorted(names)
    assert [p[4] for p in view.trade_route_points()] == ["stop:1", "stop:2", "stop:3", "stop:4"]
    best = min(_order_cost(list(p)) for p in itertools.permutations(names))
    assert _order_cost(drawn) == pytest.approx(best)
    assert _order_cost(drawn) < _order_cost(names)          # the list order was not the answer


def test_route_updates_when_the_list_changes(panel):
    from starmap.system_view import SystemView
    for i, n in enumerate(["Lorville", "New Babbage", "Area 18"]):
        panel._grocery.add_item(_item(i, n))
    panel._plot_grocery_route()
    assert _pump(lambda: isinstance(panel._stack.currentWidget(), SystemView))
    victim = next(it for it in panel._grocery.items() if it["location"] == "New Babbage")
    panel._grocery.remove_item(victim)

    def drawn():
        v = panel._stack.currentWidget()
        return [p[0] for p in v.trade_route_points()] if isinstance(v, SystemView) else None
    assert _pump(lambda: drawn() is not None and sorted(drawn()) == ["Area 18", "Lorville"]), drawn()
    panel._grocery.clear()
    assert _pump(lambda: not panel.has_shopping_route())
    assert panel._galaxy._route is None


def test_multi_system_list_visits_each_system_once(panel):
    # List order bounces between systems; the shortest order does not.
    for i, (n, s) in enumerate([("Area 18", "Stanton"), ("Ruin Station", "Pyro"),
                                ("Lorville", "Stanton"), ("Checkmate", "Pyro")]):
        panel._grocery.add_item(_item(i, n, s))
    panel._plot_grocery_route()
    g = panel._galaxy
    assert g._route_stops in (["STANTON", "PYRO"], ["PYRO", "STANTON"])
    assert g._route == g._route_stops
    # Inside each system the stops are drawn too, joined to the gateway.
    legs = {}
    for code in ("STANTON", "PYRO"):
        panel._enter_system(code)
        legs[code] = [(p[0], p[4].split(":")[0]) for p in panel._stack.currentWidget().trade_route_points()]
        panel._go_galaxy()
    stanton_first = g._route_stops[0] == "STANTON"
    assert sorted(n for n, r in legs["STANTON"] if r == "stop") == ["Area 18", "Lorville"]
    assert sorted(n for n, r in legs["PYRO"] if r == "stop") == ["Checkmate", "Ruin Station"]
    assert legs["STANTON"][-1 if stanton_first else 0] == ("Pyro Gateway", "jump")
    assert legs["PYRO"][0 if stanton_first else -1] == ("Stanton Gateway", "jump")
