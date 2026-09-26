"""Offscreen tests: Grocery List "Plot Route" -> planner -> Star Map drawing.

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


def _bubble(prices, sink):
    from market_finder.ui.grocery_list import GroceryListBubble

    def on_plot(stops, auto=False):
        sink.append((list(stops), auto))

    b = GroceryListBubble(FakeData(prices), on_plot_route=on_plot)
    _WIDGETS.append(b)
    return b


def _fill(bubble, item_ids):
    for iid in item_ids:
        bubble.add_item({"id": iid, "name": f"Item {iid}"})
    assert _pump(lambda: all(c._loaded for c in bubble._cards.values()))


def _plot(bubble, sink):
    n = len(sink)
    bubble._plot_route()
    assert _pump(lambda: len(sink) > n), "the route never arrived"
    return sink[-1][0]


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


# -- the calculator, driven through the real grocery bubble ---------------------

class TestBubbleRoute:
    def test_visit_order_is_the_shortest_one(self, sandbox, monkeypatch):
        # One item per terminal; terminals on a line at 0, 10, -1.  The old
        # nearest-neighbour-from-the-first-card order went 0 -> -1 -> 10 (12 Gm);
        # the shortest open path is -1 -> 0 -> 10 (11 Gm).
        _line_distances(monkeypatch, sandbox, {1: 0.0, 2: 10.0, 3: -1.0})
        prices = {101: [_price(1, "A", 10)], 102: [_price(2, "B", 10)], 103: [_price(3, "C", 10)]}
        sink = []
        bubble = _bubble(prices, sink)
        _fill(bubble, [101, 102, 103])
        stops = _plot(bubble, sink)
        tids = [s["terminal_id"] for s in stops]
        xs = {1: 0.0, 2: 10.0, 3: -1.0}
        cost = sum(abs(xs[a] - xs[b]) for a, b in zip(tids, tids[1:]))
        assert sorted(tids) == [1, 2, 3]
        assert cost == pytest.approx(11.0)

    def test_terminal_choice_skips_a_needless_stop(self, sandbox, monkeypatch):
        # Item 201 costs the same at terminal 1 (listed first) and terminal 2;
        # item 202 is only sold at terminal 2.  Buying both at 2 is one stop.
        _line_distances(monkeypatch, sandbox, {1: 0.0, 2: 40.0})
        prices = {201: [_price(1, "A", 500), _price(2, "B", 500)], 202: [_price(2, "B", 70)]}
        sink = []
        bubble = _bubble(prices, sink)
        _fill(bubble, [201, 202])
        stops = _plot(bubble, sink)
        assert {s["item_id"] for s in stops} == {201, 202}
        assert {s["terminal_id"] for s in stops} == {2}

    def test_route_follows_the_list_when_it_changes(self, sandbox, monkeypatch):
        _line_distances(monkeypatch, sandbox, {1: 0.0, 2: 10.0, 3: -1.0})
        prices = {101: [_price(1, "A", 10)], 102: [_price(2, "B", 10)], 103: [_price(3, "C", 10)]}
        sink = []
        bubble = _bubble(prices, sink)
        _fill(bubble, [101, 102, 103])
        _plot(bubble, sink)
        n = len(sink)
        bubble._remove_card(bubble._cards[102])
        assert _pump(lambda: len(sink) > n, 5), "removing an item did not re-plan"
        assert sorted(s["item_id"] for s in sink[-1][0]) == [101, 103]
        n = len(sink)
        bubble.add_item({"id": 102, "name": "Item 102"})
        assert _pump(lambda: len(sink) > n and len(sink[-1][0]) == 3, 5), "adding an item did not re-plan"
        n = len(sink)
        bubble.clear()
        assert _pump(lambda: len(sink) > n, 5), "clearing the list did not clear the route"
        assert sink[-1][0] == []

    def test_plot_clicked_before_prices_load_plots_once_they_arrive(self, sandbox, monkeypatch):
        _line_distances(monkeypatch, sandbox, {1: 0.0, 2: 10.0})
        prices = {101: [_price(1, "A", 10)], 102: [_price(2, "B", 10)]}

        class SlowData(FakeData):
            def fetch_item_prices(self, item_id):
                time.sleep(0.3)
                return super().fetch_item_prices(item_id)

        from market_finder.ui.grocery_list import GroceryListBubble
        sink = []
        bubble = GroceryListBubble(SlowData(prices),
                                   on_plot_route=lambda stops, auto=False: sink.append((list(stops), auto)))
        _WIDGETS.append(bubble)
        bubble.add_item({"id": 101, "name": "Item 101"})
        bubble.add_item({"id": 102, "name": "Item 102"})
        bubble._plot_route()                                  # nothing has loaded yet
        assert _pump(lambda: any(s for s, _a in sink), 8), "the click was lost"
        stops, auto = next((s, a) for s, a in sink if s)
        assert auto is False                                  # treated as the user's click
        assert sorted(s["item_id"] for s in stops) == [101, 102]

    def test_app_auto_update_never_pops_the_map_open(self, qapp):
        from market_finder.ui.app import MarketFinderApp

        class Panel:
            def __init__(self, shown):
                self.shown, self.plots = shown, []

            def has_shopping_route(self):
                return self.shown

            def plot_shopping_route(self, stops):
                self.plots.append(stops)

        class Host:
            _starmap_win = None

            def __init__(self, panel):
                self._starmap_panel = panel
                self.opened = 0

            def _open_starmap(self):
                self.opened += 1

        live, cleared = Panel(True), Panel(False)
        h1, h2 = Host(live), Host(cleared)
        MarketFinderApp._plot_grocery_route(h1, [{"system": "Stanton"}], auto=True)
        MarketFinderApp._plot_grocery_route(h1, [], auto=True)
        MarketFinderApp._plot_grocery_route(h2, [{"system": "Stanton"}], auto=True)
        assert live.plots == [[{"system": "Stanton"}], []]
        assert cleared.plots == [] and h1.opened == h2.opened == 0


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

    def test_bubble_to_map_end_to_end_and_clear(self, sandbox, monkeypatch):
        _line_distances(monkeypatch, sandbox, {1: 0.0, 2: 10.0, 3: -1.0})
        prices = {101: [_price(1, "Lorville", 10)], 102: [_price(2, "Area 18", 10)],
                  103: [_price(3, "Orison", 10)]}
        for rows, planet in ((prices[101], "Hurston"), (prices[102], "ArcCorp"), (prices[103], "Crusader")):
            rows[0]["planet_name"] = planet
        panel = _panel()
        sink = []
        from market_finder.ui.grocery_list import GroceryListBubble

        def on_plot(stops, auto=False):
            sink.append((list(stops), auto))
            if not auto or panel.has_shopping_route():
                panel.plot_shopping_route(stops)

        bubble = GroceryListBubble(FakeData(prices), on_plot_route=on_plot)
        _WIDGETS.append(bubble)
        _fill(bubble, [101, 102, 103])
        _plot(bubble, sink)
        view = panel._stack.currentWidget()
        assert [p[0] for p in view.trade_route_points()] == [s["places"][0] for s in sink[-1][0]]
        assert [p[0] for p in view.trade_route_points()] in (
            ["Orison", "Lorville", "Area 18"], ["Area 18", "Lorville", "Orison"])
        n = len(sink)
        bubble.clear()
        assert _pump(lambda: len(sink) > n, 5)
        assert panel.has_shopping_route() is False
        assert view.trade_route_points() == [] and panel._galaxy._route is None


# -- "Shorter trip over cheapest price" option ------------------------------------

def _near_far(monkeypatch, sandbox):
    """Item 301 only at Near (50); item 302 at Far (100) or Near (105, +5 %).
    Near and Far are 100 Gm apart; terminals 1 and 3 share the Near site."""
    _line_distances(monkeypatch, sandbox, {1: 0.0, 2: 100.0, 3: 0.0})
    return {301: [_price(1, "Near", 50)],
            302: [_price(2, "Far", 100), _price(3, "Near", 105)]}


def _state(tmp_path):
    import json
    p = tmp_path / "starmap_state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


class TestShorterTripOption:
    def test_off_by_default_at_ten_percent_and_persists(self, sandbox, tmp_path):
        import json
        (tmp_path / "starmap_state.json").write_text(json.dumps({"galaxy": {"home": "PYRO"}}))
        b1 = _bubble({}, [])
        assert b1._shorter_cb.isChecked() is False
        assert b1._premium_spin.value() == 10
        assert (b1._premium_spin.minimum(), b1._premium_spin.maximum()) == (0, 25)
        assert b1.route_premium() == 0.0
        b1._shorter_cb.setChecked(True)
        b1._premium_spin.setValue(15)
        st = _state(tmp_path)
        assert st["grocery_route"] == {"shorter": True, "premium_pct": 15}
        assert st["galaxy"] == {"home": "PYRO"}               # the star map's state is kept
        b2 = _bubble({}, [])                                  # a later session
        assert b2._shorter_cb.isChecked() is True and b2._premium_spin.value() == 15
        assert b2.route_premium() == pytest.approx(0.15)

    def test_toggling_replans_the_shown_route_immediately(self, sandbox, monkeypatch, tmp_path):
        sink = []
        bubble = _bubble(_near_far(monkeypatch, sandbox), sink)
        _fill(bubble, [301, 302])
        stops = _plot(bubble, sink)
        assert {s["terminal_id"] for s in stops} == {1, 2}    # off: cheapest price, 100 Gm

        n = len(sink)
        bubble._shorter_cb.setChecked(True)
        assert bubble._route_busy, "turning it on did not start a re-plan right away"
        assert _pump(lambda: len(sink) > n, 5), "turning it on did not re-plan"
        assert {s["terminal_id"] for s in sink[-1][0]} == {1, 3}    # +5 % saves the 100 Gm trip
        assert sink[-1][1] is True                                   # an update, never pops the map
        assert _state(tmp_path)["grocery_route"]["shorter"] is True

        n = len(sink)
        bubble._premium_spin.setValue(4)                      # +5 % no longer allowed
        assert _pump(lambda: len(sink) > n, 5), "changing the percentage did not re-plan"
        assert {s["terminal_id"] for s in sink[-1][0]} == {1, 2}

    def test_option_never_plots_a_route_nobody_asked_for(self, sandbox, monkeypatch):
        sink = []
        bubble = _bubble(_near_far(monkeypatch, sandbox), sink)
        _fill(bubble, [301, 302])
        bubble._shorter_cb.setChecked(True)
        _pump(lambda: False, 0.5)
        assert sink == [] and not bubble._route_summary.isVisible()

    def test_summary_line_shows_totals_and_the_difference(self, sandbox, monkeypatch):
        sink = []
        bubble = _bubble(_near_far(monkeypatch, sandbox), sink)
        _fill(bubble, [301, 302])
        _plot(bubble, sink)
        lbl = bubble._route_summary
        assert not lbl.isHidden()
        assert lbl.text() == "150 aUEC · 100 Gm"             # off: totals only
        assert lbl.wordWrap() is False

        n = len(sink)
        bubble._shorter_cb.setChecked(True)
        assert _pump(lambda: len(sink) > n, 5)
        assert lbl.text() == "155 aUEC · 0 Gm  (+5 aUEC, -100 Gm)"
        assert "Cheapest-price route: 150 aUEC" in lbl.toolTip()
        # One fixed line: its height is one text line whatever the bubble width.
        bubble.resize(320, 400)
        lbl.adjustSize()
        assert lbl.sizeHint().height() < 2 * lbl.fontMetrics().height()

        n = len(sink)
        bubble._premium_spin.setValue(4)
        assert _pump(lambda: len(sink) > n, 5)
        assert lbl.text() == "150 aUEC · 100 Gm  (cheapest is shortest)"

        n = len(sink)
        bubble.clear()
        assert _pump(lambda: len(sink) > n, 5)
        assert lbl.isHidden()                                  # no route, no summary

    def test_summary_text_formatting(self):
        from market_finder.ui.grocery_list import route_summary_text
        t = {"auec": 1_523_400.0, "gm": 1877.4, "stops": 5}
        c = {"auec": 1_522_160.0, "gm": 1915.6, "stops": 7}
        assert route_summary_text(t) == "1,523,400 aUEC · 1,877 Gm"
        assert route_summary_text(t, c) == "1,523,400 aUEC · 1,877 Gm  (+1,240 aUEC, -38 Gm)"
        assert route_summary_text({"auec": 10, "gm": None, "stops": 2}, c).endswith(", ? Gm)")
