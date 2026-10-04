"""Inside the Everything Finder there is one shopping list, however you reach it.

Before 2026-10-04 this window had three: its own pop-out, Item Finder's Grocery
List bubble inside the Item Finder tab, and the Star Map's Grocery panel inside
the Star Map tab - and nothing was copied between them (the README said so).
Now all three buttons open the same pop-out on the same list
(shared/shopping), and an item sent from a tab lands in it.

The first tests use stand-in tools (no network). The last one is the real
window with the real Star Map panel, in a clean subprocess with HOME redirected.
"""
import json
import os
import subprocess
import sys
import textwrap

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
EF_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(EF_DIR)

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication  # noqa: E402


def _pump(n=8):
    for _ in range(n):
        QCoreApplication.processEvents()


class _Source:
    def item_catalog(self): return {"Medpen": 11}
    def commodity_names(self): return ["Gold"]
    def start_terminals(self): return {}
    def routes(self): return []
    def item_prices(self, entries): return {}
    def dist_cache(self): return None


class _FakeMap(QtWidgets.QLabel):
    """Stands in for the Star Map panel: records what the window does to it."""

    def __init__(self):
        super().__init__("map")
        self.host, self.plots, self.cleared, self.route = "unset", [], 0, False

    def set_shopping_host(self, toggle):
        self.host = toggle

    def has_shopping_route(self):
        return self.route

    def plot_shopping_route(self, stops):
        self.plots.append(stops)
        self.route = bool(stops)

    def clear_shopping_route(self):
        self.cleared += 1
        self.route = False


def _fake_item_finder():
    from shared.qt.base_window import SCWindow
    win = SCWindow(title="Item Finder", width=700, height=400, min_w=300, min_h=200)
    win.content_layout.addWidget(QtWidgets.QLabel("the item table"), 1)
    win._toggle_shopping_list = lambda: (_ for _ in ()).throw(AssertionError("Item Finder's own list opened"))
    win._shopping_window = lambda: (_ for _ in ()).throw(AssertionError("Item Finder's own list built"))
    return win


@pytest.fixture
def window(monkeypatch, tmp_path):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from everything_finder import tool_loader, window as wmod
    from shared.shopping.shopping_list import ShoppingList
    monkeypatch.setattr(wmod.EverythingFinderWindow, "_state_path",
                        staticmethod(lambda: str(tmp_path / "window.json")))
    made = {}
    monkeypatch.setattr(tool_loader, "load_item_finder_window",
                        lambda *a: made.setdefault("item", _fake_item_finder()))
    monkeypatch.setattr(tool_loader, "load_starmap_panel",
                        lambda: made.setdefault("map", _FakeMap()))
    sl = ShoppingList(path=str(tmp_path / "shopping_list.json"))
    w = wmod.EverythingFinderWindow(x=10, y=10, w=1000, h=700, initial_tab=wmod.TAB_ITEM,
                                    factories={wmod.TAB_TRADE: lambda: QtWidgets.QLabel("hub")},
                                    shopping_list=sl, shopping_source=_Source())
    yield w, wmod, sl, made
    if w.shopping_popout() is not None:
        w.shopping_popout().hide()
    w.hide()
    w.deleteLater()
    _pump()


def test_the_item_finder_tabs_list_button_opens_the_windows_pop_out(window):
    w, wmod, sl, made = window
    w.select_tab(wmod.TAB_ITEM)
    item = made["item"]
    item._toggle_shopping_list()                       # its title-bar "Shopping List" button
    pop = w.shopping_popout()
    assert pop is not None and pop.isVisible(), "the Item Finder tab did not open the shared pop-out"
    assert pop.panel._list is sl
    assert w._btn_shop.isChecked()
    # an item sent from Item Finder's own star-map code path lands in the same list
    assert item._shopping_window() is pop
    item._shopping_window().panel.add_item({"id": 11, "name": "Medpen"})
    assert [e.name for e in sl.entries()] == ["Medpen"]
    item._toggle_shopping_list()
    assert not pop.isVisible() and not w._btn_shop.isChecked()


def test_the_star_map_tab_is_told_to_use_the_windows_pop_out(window):
    w, wmod, sl, made = window
    w.select_tab(wmod.TAB_MAP)
    assert made["map"].host == w.toggle_shopping_list
    made["map"].host()                                 # the map's "Shopping List" button
    assert w.shopping_popout().isVisible() and w.shopping_popout().panel._list is sl


def test_show_on_star_map_draws_on_the_tab_and_follows_without_stealing_focus(window):
    from types import SimpleNamespace
    w, wmod, sl, made = window
    term = SimpleNamespace(terminal_id=1, terminal_name="Shop - Area 18", location="Area 18",
                           system="Stanton")
    plan = SimpleNamespace(label="MIN STOPS", stops=[SimpleNamespace(
        terminal=term, picks=[SimpleNamespace(commodity="item:Medpen", price_buy=30.0)])])
    w.toggle_shopping_list()
    # an auto-redraw before any map exists: nothing is built, nothing switches
    w.show_plan_on_map(plan, auto=True)
    assert "map" not in made and w.tabs.current_key() != wmod.TAB_MAP
    # the click: opens the Star Map tab and draws there
    w.show_plan_on_map(plan)
    _pump()
    fake = made["map"]
    assert w.tabs.current_key() == wmod.TAB_MAP
    assert [[s["places"][0] for s in stops] for stops in fake.plots] == [["Area 18"]]
    # the list changes while the pilot is on another tab: redraw, but stay put
    w.select_tab(wmod.TAB_TRADE)
    w.show_plan_on_map(plan, auto=True)
    assert len(fake.plots) == 2 and w.tabs.current_key() == wmod.TAB_TRADE
    # the pilot cleared the route on the map: an auto-redraw must not bring it back
    fake.route = False
    w.show_plan_on_map(plan, auto=True)
    assert len(fake.plots) == 2
    # the list is emptied: its route comes off the map
    w.clear_plan_on_map()
    assert fake.cleared == 1


# ── the real window and the real Star Map panel ──────────────────────────────

_PROBE = textwrap.dedent(r'''
    import json, os, sys, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, ROOT)
    import urllib.request
    def _no_network(*a, **k): raise OSError("network disabled in tests")
    urllib.request.urlopen = _no_network

    # A home system on file, or the Star Map opens its first-run home picker - a
    # modal dialog that would wait for a click forever.
    sdir = os.path.join(TMP, ".sctoolbox", "starmap")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "starmap_state.json"), "w", encoding="utf-8") as fh:
        json.dump({"galaxy": {"selected": "STANTON", "home": "STANTON"}}, fh)

    from shared.app_bootstrap import bootstrap_skill
    bootstrap_skill(os.path.join(EF_DIR, "everything_finder_app.py"))
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication
    app = QApplication([])
    from everything_finder import window as wmod
    wmod.EverythingFinderWindow._state_path = staticmethod(lambda: os.path.join(TMP, "w.json"))

    class Source:
        def item_catalog(self): return {}
        def commodity_names(self): return []
        def start_terminals(self): return {}
        def routes(self): return []
        def item_prices(self, entries): return {}
        def dist_cache(self): return None

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)

    from shared.shopping import shared_list
    out = {"store": shared_list().path}
    w = wmod.EverythingFinderWindow(initial_tab=wmod.TAB_MAP, shopping_source=Source())
    w.show()
    pump(1.0)
    panel = w.inner(wmod.TAB_MAP)
    out["map_list_is_shared"] = panel._grocery._list is shared_list()
    out["map_button"] = panel._btn_grocery.text()
    panel._btn_grocery.click()                      # the Star Map tab's own list button
    pump(0.3)
    pop = w.shopping_popout()
    out["popout_opened"] = bool(pop is not None and pop.isVisible())
    out["popout_list_is_shared"] = bool(pop is not None and pop.panel._list is shared_list())
    out["docked_copy_hidden"] = panel._grocery.isHidden()
    # an item added from the map's item pop-out shows up in the window's list
    panel.add_to_shopping({"id": 7, "name": "Pembroke Helmet", "price": 5200,
                           "location": "Area 18", "system": "Stanton"})
    pump(0.2)
    row = pop.panel.row("item", "Pembroke Helmet") if pop is not None else None
    out["row_in_popout"] = row is not None
    out["row_pinned"] = bool(row is not None and row.pin_label is not None
                             and "Area 18" in row.pin_label.text())
    with open(shared_list().path, encoding="utf-8") as fh:
        out["on_disk"] = json.load(fh)
    print("PROBE " + json.dumps(out), flush=True)
    os._exit(0)
''')


def test_real_window_star_map_tab_and_pop_out_share_one_list_and_one_file(tmp_path):
    code = f"ROOT = {ROOT!r}\nEF_DIR = {EF_DIR!r}\nTMP = {str(tmp_path)!r}\n" + _PROBE
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=60, env=env, encoding="utf-8", errors="replace")
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line is not None, f"probe printed nothing (rc={proc.returncode}):\n{proc.stderr[-3000:]}"
    got = json.loads(line[6:])
    want_store = os.path.join(str(tmp_path), ".sctoolbox", "shopping", "shopping_list.json")
    assert os.path.normcase(got["store"]) == os.path.normcase(want_store), got["store"]
    assert got["map_button"] == "Shopping List"
    assert got["map_list_is_shared"] and got["popout_list_is_shared"]
    assert got["popout_opened"], "the Star Map tab's list button did not open the pop-out"
    assert got["docked_copy_hidden"], "a second copy of the list was docked beside the map"
    assert got["row_in_popout"] and got["row_pinned"]
    assert got["on_disk"] == [{"kind": "item", "name": "Pembroke Helmet", "qty": 1, "item_id": 7,
                               "pin": {"location": "Area 18", "system": "Stanton", "price": 5200.0}}]
