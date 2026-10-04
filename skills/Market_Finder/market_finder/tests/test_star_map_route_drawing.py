"""Offscreen tests: a shopping route's stops -> Item Finder's Star Map drawing.

Until 2026-10-04 this file (test_shopping_route_ui.py) also drove Item Finder's
own Grocery List bubble and its route planner. That list is retired - there is
one shopping list now (shared/shopping, tested in shared/tests), planned by
Trade Hub's basket planner - so the bubble tests went with it. What is left is
what Item Finder still does itself: DRAW an ordered stop list on its star map,
and hand a shared-list plan to that map without ever popping it open unasked.

Runs offscreen (QT_QPA_PLATFORM=offscreen).  No network: the UEX distance
fetch is stubbed and every on-disk path (star-map state, distance cache,
items cache, lore cache) is redirected to a temp dir, so ~/.sctoolbox is
never read or written.

Set MF_ROUTE_SHOTS=<dir> to also save the rendered maps as PNGs.
"""

import math
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402

from shared.errors import Result  # noqa: E402

ROUTE_RGB = (0xFF, 0xCC, 0x00)          # P.tool_trade, the route colour


# -- fixtures -------------------------------------------------------------------

_WIDGETS: list = []        # top-level widgets a test made; destroyed after it


@pytest.fixture
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    # Destroy everything now, not at interpreter exit (Qt teardown at exit
    # crashes intermittently when Python-overridden widgets are still alive).
    while _WIDGETS:
        w = _WIDGETS.pop()
        w.hide()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()


@pytest.fixture
def sandbox(monkeypatch, tmp_path, qapp):
    """Every star-map / distance cache path -> tmp_path; UEX fetch -> no-op."""
    from market_finder.starmap import data as sm_data
    from market_finder.starmap import distances, items_index, lore
    monkeypatch.setattr(sm_data, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sm_data, "_STATE_PATH", str(tmp_path / "starmap_state.json"))
    monkeypatch.setattr(distances, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(distances, "_CACHE_PATH", str(tmp_path / "distance_cache.json"))
    monkeypatch.setattr(distances, "_cache", {})
    monkeypatch.setattr(items_index, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(items_index, "_CACHE_PATH", str(tmp_path / "items_prices_all.json"))
    monkeypatch.setattr(lore, "_CACHE_DIR", str(tmp_path))
    fetched = []
    monkeypatch.setattr(distances, "fetch_missing",
                        lambda pairs, on_progress=None: fetched.append(set(pairs)))
    return distances


class FakeData:
    items: list = []
    terminals: dict = {}
    _api = None

    def __init__(self, prices):
        self._prices = prices

    def is_loaded(self):
        return True

    def fetch_item_prices(self, item_id):
        return Result.success([dict(r) for r in self._prices.get(item_id, [])])


def _pump(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _price(tid, city, price, system="Stanton"):
    return {"id_terminal": tid, "terminal_name": f"Shop {tid} - {city}", "price_buy": price,
            "star_system_name": system, "planet_name": "Planet " + city, "city_name": city}


def _line_distances(monkeypatch, distances, xs):
    """Terminal telemetry on a line: |x_o - x_d| Gm."""
    monkeypatch.setattr(distances, "get_distance",
                        lambda o, d: (0.0 if o == d else abs(xs[o] - xs[d]))
                        if (o in xs and d in xs) else None)


def _panel():
    from market_finder.starmap.panel import MarketMapPanel
    panel = MarketMapPanel(FakeData({}))
    _WIDGETS.append(panel)
    panel._galaxy.set_home("STANTON")         # no first-run home picker dialog
    panel.resize(1100, 700)
    panel.show()
    _pump(lambda: False, 0.05)
    return panel


def _shot(widget, name):
    img = widget.grab().toImage()
    out = os.environ.get("MF_ROUTE_SHOTS")
    if out:
        os.makedirs(out, exist_ok=True)
        img.save(os.path.join(out, name))
    return img


def _is_route_px(img, x, y, r=2):
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            xx, yy = int(round(x)) + dx, int(round(y)) + dy
            if 0 <= xx < img.width() and 0 <= yy < img.height():
                c = QColor(img.pixel(xx, yy))
                if c.red() > 200 and c.green() > 150 and c.blue() < 90:
                    return True
    return False


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    L = (bx - ax) ** 2 + (by - ay) ** 2
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / L))
    return math.hypot(px - (ax + t * (bx - ax)), py - (ay + t * (by - ay)))


def _stanton_stops():
    """A real optimal route (plan_route over UEX rows): 4 sites in Stanton."""
    def s(stop, place, planet, name, tid):
        return {"item_id": tid, "name": name, "terminal": f"T{tid}", "terminal_id": tid,
                "system": "Stanton", "location": f"Stanton > {planet} > {place}",
                "places": [place, planet], "price": 100, "stop": stop}
    return [s(1, "Orison", "Crusader", "Undersuit", 1),
            s(2, "CRU-L4 Shallow Fields Station", "Crusader", "Pants", 2),
            s(3, "Pyro Gateway (Stanton)", "Stanton", "Helmet", 3),
            s(4, "Area 18", "ArcCorp", "Torpedo", 4),
            s(4, "Area 18", "ArcCorp", "Flash", 5)]


# -- the shared list's plan, handed to the map by the app ----------------------------

class _Offer:
    def __init__(self, label, price):
        self.commodity, self.price_buy, self.scu_available = label, price, 0


class _Stop:
    def __init__(self, tid, name, location, picks):
        from types import SimpleNamespace
        self.terminal = SimpleNamespace(terminal_id=tid, terminal_name=name,
                                        location=location, system="Stanton")
        self.picks = picks


class _Plan:
    """The shape of Trade Hub's BasketPlan, as the shared list hands it over."""
    label = "MIN STOPS"

    def __init__(self, stops):
        self.stops = stops


class TestSharedListPlanOnTheMap:
    def _hosts(self):
        class Panel:
            def __init__(self, shown):
                self.shown, self.plots = shown, []

            def has_shopping_route(self):
                return self.shown

            def plot_shopping_route(self, stops):
                self.plots.append([s["places"][0] for s in stops])

        class ListPanel:
            cleared = 0

            def map_route_cleared(self):
                self.cleared += 1

        class Host:
            _starmap_win = None

            def __init__(self, panel):
                from types import SimpleNamespace
                self._starmap_panel = panel
                self._shopping_win = SimpleNamespace(panel=ListPanel())
                self.opened = 0

            def _open_starmap(self):
                self.opened += 1
        return Panel, Host

    def test_the_plan_is_drawn_in_the_planners_order(self, qapp):
        from market_finder.ui.app import MarketFinderApp
        Panel, Host = self._hosts()
        plan = _Plan([_Stop(2, "Shop - Orison", "Orison", [_Offer("item:Undersuit", 10)]),
                      _Stop(1, "Shop - Area 18", "Area 18", [_Offer("item:Pants", 5),
                                                             _Offer("commodity:Gold", 7)])])
        h = Host(Panel(False))
        MarketFinderApp._show_shopping_plan(h, plan)
        assert h.opened == 1, "a click on Show on Star Map opens the map"
        # one stop dict per pick, in the plan's own order - nothing re-sorted here
        assert h._starmap_panel.plots == [["Orison", "Area 18", "Area 18"]]

    def test_an_auto_update_never_pops_the_map_open(self, qapp):
        from market_finder.ui.app import MarketFinderApp
        Panel, Host = self._hosts()
        plan = _Plan([_Stop(1, "Shop - Area 18", "Area 18", [_Offer("item:Pants", 5)])])
        live, cleared = Host(Panel(True)), Host(Panel(False))
        MarketFinderApp._show_shopping_plan(live, plan, auto=True)
        MarketFinderApp._show_shopping_plan(cleared, plan, auto=True)
        assert live._starmap_panel.plots == [["Area 18"]]
        assert cleared._starmap_panel.plots == [], "a route the user cleared came back"
        assert live.opened == cleared.opened == 0
        # ... and the list is told to stop following a route that is no longer drawn
        assert cleared._shopping_win.panel.cleared == 1 and live._shopping_win.panel.cleared == 0

    def test_emptying_the_list_takes_its_route_off_an_open_map(self, qapp):
        from market_finder.ui.app import MarketFinderApp
        Panel, Host = self._hosts()
        live, cleared = Host(Panel(True)), Host(Panel(False))
        MarketFinderApp._clear_shopping_route(live)
        MarketFinderApp._clear_shopping_route(cleared)
        assert live._starmap_panel.plots == [[]] and cleared._starmap_panel.plots == []


# -- Item Finder's list button opens THE list, not a list of its own ---------------

class TestItemFinderUsesTheSharedList:
    @pytest.fixture
    def host(self, qapp, monkeypatch, tmp_path):
        """A widget carrying MarketFinderApp's real list methods (the app itself
        starts a UEX load when constructed, so it is not built here)."""
        from market_finder.ui.app import MarketFinderApp
        from shared.shopping import shopping_list as shop_mod
        monkeypatch.setattr(shop_mod, "_shared",
                            shop_mod.ShoppingList(path=str(tmp_path / "shopping_list.json")))

        class Host(QtWidgets.QWidget):
            _shopping_window = MarketFinderApp._shopping_window
            _toggle_shopping_list = MarketFinderApp._toggle_shopping_list
            _add_to_shopping_from_map = MarketFinderApp._add_to_shopping_from_map
            _show_shopping_plan = MarketFinderApp._show_shopping_plan
            _clear_shopping_route = MarketFinderApp._clear_shopping_route

        h = Host()
        h.data = FakeData({})
        h._shopping_win = None
        h._starmap_panel = None
        h._starmap_win = None
        _WIDGETS.append(h)
        return h

    def test_the_button_opens_the_shared_list_priced_by_this_windows_data(self, host):
        from shared.shopping import shared_list
        from shared.shopping.panel import ShoppingListWindow
        host._toggle_shopping_list()
        win = host._shopping_win
        _WIDGETS.append(win)
        assert isinstance(win, ShoppingListWindow) and win.isVisible()
        assert win.panel._list is shared_list(), "Item Finder opened a list of its own"
        assert win.panel._source.item_service() is host.data
        host._toggle_shopping_list()
        assert not win.isVisible() and host._shopping_win is win       # hidden, list kept

    def test_an_item_sent_from_item_finders_map_opens_the_list_and_lands_on_it(self, host):
        from shared.shopping import shared_list
        host._add_to_shopping_from_map({"id": 7, "name": "Helmet", "price": 100,
                                        "location": "Area 18", "system": "Stanton"})
        _WIDGETS.append(host._shopping_win)
        assert host._shopping_win.isVisible()
        [e] = shared_list().entries()
        assert (e.name, e.item_id, e.pin["location"]) == ("Helmet", 7, "Area 18")

    def test_item_finder_has_no_list_or_planner_of_its_own_left(self):
        import market_finder.ui.app as app_mod
        src = open(app_mod.__file__, encoding="utf-8").read()
        assert "GroceryListBubble" not in src and "grocery_list" not in src
        assert "route_planner" not in src, "Item Finder's window still reaches for its own planner"
        assert not os.path.exists(os.path.join(os.path.dirname(app_mod.__file__), "grocery_list.py"))


# -- drawing -----------------------------------------------------------------------

class TestStarMapDrawing:
    def test_single_system_route_is_drawn_stop_by_stop_in_order(self, sandbox):
        from market_finder.starmap.system_view import SystemView
        panel = _panel()
        stops = _stanton_stops()
        panel.plot_shopping_route(stops)
        view = panel._stack.currentWidget()
        assert isinstance(view, SystemView), "a Stanton-only route drew nothing"
        pts = view.trade_route_points()
        assert [(p[0], p[4]) for p in pts] == [
            ("Orison", "stop:1"), ("CRU-L4 Shallow Fields Station", "stop:2"),
            ("Pyro Gateway (Stanton)", "stop:3"), ("Area 18", "stop:4")]

        img = _shot(view, "mf_route_stanton.png")
        w, h = view.width(), view.height()
        scale = view._scale(w, h)
        xy = [view._cam.project(p[1], p[2], p[3], w / 2.0, h / 2.0, scale)[:2] for p in pts]
        for (ax, ay), (bx, by) in zip(xy, xy[1:]):          # consecutive stops are joined
            hits = sum(_is_route_px(img, ax + t * (bx - ax), ay + t * (by - ay)) for t in (0.3, 0.5, 0.7))
            assert hits >= 2, f"no route line between {(ax, ay)} and {(bx, by)}"
        # ...and a non-consecutive pair is not (probe away from lines and labels).
        segs = list(zip(xy, xy[1:]))
        probed = 0
        for i, j in ((0, 2), (0, 3), (1, 3)):
            mx, my = (xy[i][0] + xy[j][0]) / 2.0, (xy[i][1] + xy[j][1]) / 2.0
            if min(_seg_dist((mx, my), a, b) for a, b in segs) < 12:
                continue
            if any(abs(my - y) < 16 and -30 < mx - x < 320 for x, y in xy):
                continue
            probed += 1
            assert not _is_route_px(img, mx, my, r=1), f"stops {i + 1} and {j + 1} are joined directly"
        assert probed >= 1

    def test_route_that_returns_to_a_system_keeps_every_visit(self, sandbox):
        panel = _panel()
        stops = [
            {"item_id": 1, "name": "a", "terminal_id": 1, "system": "Stanton",
             "location": "Stanton > ArcCorp > Area 18", "places": ["Area 18", "ArcCorp"]},
            {"item_id": 2, "name": "b", "terminal_id": 2, "system": "Pyro",
             "location": "Pyro > Terminus > Ruin Station", "places": ["Ruin Station", "Terminus"]},
            {"item_id": 3, "name": "c", "terminal_id": 3, "system": "Stanton",
             "location": "Stanton > Hurston > Lorville", "places": ["Lorville", "Hurston"]},
        ]
        panel.plot_shopping_route(stops)
        g = panel._galaxy
        assert g._route == ["STANTON", "PYRO", "STANTON"]
        assert g._route_stops == ["STANTON", "PYRO", "STANTON"]
        assert g.route_stop_labels() == {"STANTON": "1,3", "PYRO": "2"}
        _shot(g, "mf_route_galaxy_return.png")

    def test_in_system_legs_run_through_the_gateways(self, sandbox):
        from market_finder.starmap.system_view import SystemView
        panel = _panel()
        stops = [
            {"item_id": 1, "name": "a", "terminal_id": 1, "system": "Nyx",
             "location": "Nyx > Levski", "places": ["Levski"]},
            {"item_id": 2, "name": "b", "terminal_id": 2, "system": "Pyro",
             "location": "Pyro > Terminus > Ruin Station", "places": ["Ruin Station", "Terminus"]},
            {"item_id": 3, "name": "c", "terminal_id": 3, "system": "Stanton",
             "location": "Stanton > ArcCorp > Area 18", "places": ["Area 18", "ArcCorp"]},
        ]
        panel.plot_shopping_route(stops)
        assert panel._galaxy._route == ["NYX", "PYRO", "STANTON"]
        _shot(panel._galaxy, "mf_route_galaxy.png")
        legs = {}
        for code in ("NYX", "PYRO", "STANTON"):
            panel._enter_system(code)
            view = panel._stack.currentWidget()
            assert isinstance(view, SystemView)
            legs[code] = [(p[0], p[4]) for p in view.trade_route_points()]
            view.frame_trade_route()
            _shot(view, f"mf_route_{code.lower()}.png")
            panel._go_galaxy()
        assert legs == {
            "NYX": [("Levski", "stop:1"), ("Pyro Gateway", "jump")],
            "PYRO": [("Nyx Gateway", "jump"), ("Ruin Station", "stop:2"), ("Stanton Gateway", "jump")],
            "STANTON": [("Pyro Gateway", "jump"), ("Area 18", "stop:3")],
        }
