# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
"""The features ported INTO this Star Map from the other two copies (2026-10-03).

The Everything Finder's Star Map tab is this tool, so it must carry the union
of the three star maps. Each test pins one ported feature:

  from the Item Finder map (market_finder/starmap):
    * items are indexed under moon / planet / nickname names of the TERMINAL
      record, never under a price row's own "name"
    * the items card groups rows into collapsible categories, collapsed when
      the location is big, and refill() works while the card is hidden
  from the Trade Hub map (Trade_Hub/starmap):
    * the OVERLAYS sidebar exists, hidden until a routes provider is attached
    * attached routes add their buy/sell locations to the terminal badges
    * show_route() draws a cross-system route with BOTH jump gateways injected

No network (urlopen blocked), no ears, HOME redirected.
"""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HOME = tempfile.mkdtemp(prefix="starmap_efport_test_")
os.environ["HOME"] = os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402


def _pump(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


# ── Item Finder map: terminal name keys ───────────────────────────────────────

def test_items_index_files_rows_under_the_terminals_moon_and_planet():
    from starmap.items_index import build_index
    terminals = {7: {"id": 7, "star_system_name": "Stanton", "outpost_name": "Shubin SAL-2",
                     "moon_name": "Daymar", "planet_name": "Crusader", "nickname": "SAL-2 Mining"}}
    rows = [{"id_item": 1, "id_terminal": 7, "price_buy": 50, "item_name": "Pickaxe"}]
    idx = build_index(rows, terminals, {1: {"name": "Pickaxe", "category": "Tools"}})
    keys = {k for k in idx}
    assert ("stanton", "daymar") in keys, keys
    assert ("stanton", "crusader") in keys, keys
    from starmap.data import norm_loc
    assert ("stanton", norm_loc("SAL-2 Mining")) in keys, keys


def test_items_index_never_uses_a_price_rows_own_name_as_a_place():
    from starmap.items_index import build_index
    # Terminal unknown: only the row's own location fields may be used.
    rows = [{"id_item": 2, "id_terminal": 999, "price_buy": 10, "star_system_name": "Stanton",
             "city_name": "Area18", "name": "Medpen"}]
    idx = build_index(rows, {}, {})
    bodies = {b for _s, b in idx}
    assert not any("medpen" in b for b in bodies), bodies
    assert any("area" in b for b in bodies), bodies


# ── Item Finder map: categories + refill ──────────────────────────────────────

def _rows(n_per_cat, cats=("Armor", "Weapons")):
    out = []
    for c in cats:
        for i in range(n_per_cat):
            out.append({"item": {"id": f"{c}{i}", "name": f"{c} {i}", "category": c},
                        "price": 100 + i})
    return out


def _headers(dlg):
    from starmap.items_dialog import _CategoryHeader
    return dlg._inner.findChildren(_CategoryHeader)


def test_items_card_groups_rows_by_category_and_collapses_big_locations():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from starmap.items_dialog import ItemsDialog, _AUTO_EXPAND_MAX
    big = _rows(_AUTO_EXPAND_MAX)                  # 2 x MAX rows -> over the auto-expand limit
    dlg = ItemsDialog("Area18", "Stanton", lambda: big, lambda _i: None)
    dlg.refill()
    hs = _headers(dlg)
    assert len(hs) == 2, [h._name for h in hs]
    assert sorted(h._name for h in hs) == ["Armor", "Weapons"]
    assert all("▸" in h._label.text() for h in hs), "big location should open collapsed"
    dlg.deleteLater()


def test_items_card_refills_while_hidden_as_an_embedded_tab():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from starmap.items_dialog import ItemsDialog
    rows = []
    dlg = ItemsDialog("Area18", "Stanton", lambda: rows, lambda _i: None)
    assert not dlg.isVisible()
    dlg.refill()
    assert "No item data" in dlg._status.text()
    rows.extend(_rows(3))                           # the index landed
    dlg.refill()                                    # still hidden, must fill anyway
    assert len(_headers(dlg)) == 2
    assert "6 items" in dlg._status.text(), dlg._status.text()
    dlg.deleteLater()


# ── Trade Hub map: overlays, route terminals, show_route ──────────────────────

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
    # the shared shopping list: this test's own file, never ~/.sctoolbox
    monkeypatch.setattr(shop_mod, "_shared", shop_mod.ShoppingList(path=str(tmp_path / "shopping.json")))
    monkeypatch.setattr(data, "_STATE_PATH", str(tmp_path / "starmap_state.json"))
    monkeypatch.setattr(distances, "_CACHE_PATH", str(tmp_path / "distance_cache.json"))
    monkeypatch.setattr(distances, "_cache", {})
    monkeypatch.setattr(lore, "_CACHE_PATH", str(tmp_path / "lore_cache.json"))
    p = panel_mod.StarmapPanel()
    assert p._galaxy is not None, "star map failed to build"
    p._galaxy.set_home("STANTON")
    p.resize(1100, 700)
    p.show()
    _pump(lambda: False, 0.05)
    yield p
    p.hide()
    p.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()


class _Route:
    def __init__(self, buy_location, sell_location, buy_system="Stanton", sell_system="Stanton"):
        self.buy_location, self.sell_location = buy_location, sell_location
        self.buy_system, self.sell_system = buy_system, sell_system
        self.commodity = "Laranite"
        self.buy_terminal, self.sell_terminal = buy_location, sell_location
        self.id_terminal_buy, self.id_terminal_sell = 1, 2
        self.margin = self.profit = self.score = 100.0
        self.price_buy, self.price_sell = 10.0, 20.0
        self.scu_available = self.scu_demand = 100
        self.distance = 0.0


def test_overlay_sidebar_is_hidden_until_routes_are_attached(panel):
    sb = panel._overlay_sidebar
    assert sb is not None and sb.isHidden(), "standalone map must not show trade overlays"
    assert set(panel._ov_buttons) == {"flows", "top", "activity", "career"}
    panel.set_routes_provider(lambda: [])
    assert not sb.isHidden()
    panel.set_routes_provider(None)
    assert sb.isHidden()


def test_attached_routes_add_their_locations_to_the_terminal_badges(panel):
    before = panel._terminal_names()
    panel.set_routes_provider(lambda: [_Route("Everus Harbor", "Baijini Point")])
    after = panel._terminal_names()
    added = after - before
    assert any("everus" in n for n in added), added
    assert any("baijini" in n for n in added), added


def test_show_route_cross_system_injects_both_gateways(panel):
    panel._show_route_now([("Everus Harbor", "Stanton", "buy"),
                           ("Ruin Station", "Pyro", "sell")])
    roles = [(c, r) for (c, _n, x, _y, _z, r) in panel._trade_route_pts if x is not None]
    assert ("STANTON", "buy") in roles and ("PYRO", "sell") in roles, roles
    jumps = {c for c, r in roles if r == "jump"}
    assert jumps == {"STANTON", "PYRO"}, panel._trade_route_pts
    assert panel.has_trade_route()
    panel.clear_trade_route()
    assert not panel.has_trade_route()
