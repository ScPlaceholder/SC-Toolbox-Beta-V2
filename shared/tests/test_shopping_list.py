# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
# Moved with the code from skills/Everything_Finder/tests/ to shared/tests/ on 2026-10-04.
"""The shared shopping list: both kinds addable, one route, Trade Hub's math.

These are the Everything Finder's original tests for the list, which is now the
toolbox's one list (shared/shopping). What the merge added - pins, the per-entry
offers, drag and drop, following the map, sharing across tools - is tested in
test_shopping_one_list.py.

No network: routes, item prices and distances are all supplied by the test.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)
import shared.path_setup  # noqa: E402

import pytest  # noqa: E402

from shared.shopping.shopping_list import (  # noqa: E402
    Entry, ShoppingList, build_index, plan_routes, plan_stops_for_map, plan_summary,
)


# ── the list ─────────────────────────────────────────────────────────────────

def test_items_and_commodities_both_addable_and_kept_apart(tmp_path):
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    sl.add("item", "Medpen", 2)
    sl.add("commodity", "Laranite", 32)
    sl.add("commodity", "Medpen", 1)          # same name, other kind: a separate entry
    sl.add("item", "medpen", 1)               # same kind, case-insensitive: bumps qty
    kinds = [(e.kind, e.name, e.qty) for e in sl.entries()]
    assert kinds == [("item", "Medpen", 3), ("commodity", "Laranite", 32), ("commodity", "Medpen", 1)]
    assert len(sl.of_kind("item")) == 1 and len(sl.of_kind("commodity")) == 2


def test_list_persists_and_reloads(tmp_path):
    p = str(tmp_path / "l.json")
    sl = ShoppingList(path=p)
    sl.add("item", "Pembroke Helmet", 1, item_id=4242)
    sl.add("commodity", "Gold", 8)
    again = ShoppingList(path=p).load()
    assert [(e.kind, e.name, e.qty, e.item_id) for e in again.entries()] == \
        [("item", "Pembroke Helmet", 1, 4242), ("commodity", "Gold", 8, None)]
    assert again.remove("commodity", "gold")
    assert ShoppingList(path=p).load().of_kind("commodity") == []


def test_bad_entries_are_refused(tmp_path):
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    with pytest.raises(ValueError):
        sl.add("ship", "Kraken")
    with pytest.raises(ValueError):
        sl.add("item", "   ")


# ── planning with Trade Hub's basket planner ─────────────────────────────────

def _route(commodity, tid, tname, loc, price, sys_="Stanton"):
    from shared.shopping.paths import ensure_trade_hub_path
    ensure_trade_hub_path()
    from trade_hub_data import Route
    return Route(commodity=commodity, buy_terminal=tname, buy_location=loc, buy_system=sys_,
                 sell_terminal="Sink", sell_location="Sink", sell_system=sys_,
                 scu_available=100, scu_demand=100, price_buy=price, price_sell=price * 2,
                 margin=price, id_terminal_buy=tid, id_terminal_sell=999)


def _price_row(tid, tname, price, station):
    return {"id_terminal": tid, "terminal_name": tname, "price_buy": price,
            "star_system_name": "Stanton", "space_station_name": station}


class _Dist:
    """A DistanceCache stand-in: symmetric, all pairs known, records fetches."""

    def __init__(self, d):
        self.d = d
        self.fetched = None

    def get(self, a, b):
        return self.d.get((min(a, b), max(a, b)), 50.0)

    def fetch_missing(self, pairs, on_progress=None):
        self.fetched = set(pairs)


ENTRIES = [Entry("commodity", "Laranite", 32), Entry("item", "Medpen", 2)]
ROUTES = [_route("Laranite", 1, "Admin - Port Tressler", "Port Tressler", 10.0),
          _route("Laranite", 2, "TDD - Area18", "Area18", 9.0)]
# Terminal 1 deliberately carries a DIFFERENT display name in the item feed than in
# the commodity routes (the UEX endpoints do disagree), so only the terminal id can
# tell the planner they are one stop.
PRICES = {"Medpen": [_price_row(1, "Admin Office (Port Tressler)", 300.0, "Port Tressler"),
                     _price_row(3, "CuraLife - Lorville", 250.0, "Lorville")]}


def test_one_index_holds_both_kinds_keyed_by_terminal_id():
    pi = build_index(ENTRIES, ROUTES, PRICES)
    assert sorted(pi.selected) == ["commodity:Laranite", "item:Medpen"]
    by_tid = {tk.terminal_id: sorted(offers) for tk, offers in pi.index.items()}
    # terminal 1 sells BOTH: one key, two offers (not two stops at the same terminal)
    assert by_tid[1] == ["commodity:Laranite", "item:Medpen"], by_tid
    assert by_tid[2] == ["commodity:Laranite"] and by_tid[3] == ["item:Medpen"]


def test_per_entry_limit_keeps_the_cheapest_terminals():
    pi = build_index(ENTRIES, ROUTES, PRICES, per_entry_limit=1)
    by_tid = {tk.terminal_id: sorted(o) for tk, o in pi.index.items()}
    assert by_tid == {2: ["commodity:Laranite"], 3: ["item:Medpen"]}, by_tid


def test_entry_nobody_sells_is_reported_not_dropped_silently():
    pi = build_index(ENTRIES + [Entry("item", "Unobtainium")], ROUTES, PRICES)
    assert pi.no_offers == ["item:Unobtainium"]


def test_min_stops_route_buys_both_kinds_at_the_shared_terminal():
    dist = _Dist({})
    plans, pi = plan_routes(ENTRIES, ROUTES, PRICES, dist, prefer="MIN STOPS")
    assert plans, "no plan produced"
    first = plans[0]
    assert first.label == "MIN STOPS"
    assert [s.terminal.terminal_id for s in first.stops] == [1]
    assert sorted(o.commodity for o in first.stops[0].picks) == ["commodity:Laranite", "item:Medpen"]
    assert dist.fetched, "distances must be warmed through the cache before planning"
    assert any("Medpen (item)" in ln and "Laranite (commodity)" in ln for ln in plan_summary(first))
    stops = plan_stops_for_map(first)
    assert {s["name"] for s in stops} == {"Laranite", "Medpen"}
    assert all(s["places"] == ["Port Tressler"] for s in stops)


def test_preferred_strategy_is_listed_first():
    plans, _pi = plan_routes(ENTRIES, ROUTES, PRICES, _Dist({}), prefer="BEST PRICE")
    assert plans[0].label == "BEST PRICE"
    # best price: cheapest Laranite (T2, 9) and cheapest Medpen (T3, 250)
    assert sorted(s.terminal.terminal_id for s in plans[0].stops) == [2, 3]


def test_planning_is_delegated_to_trade_hubs_basket_engine(monkeypatch):
    """J's directive: the route math is Trade Hub's. Spy on it and prove the
    shopping list hands it the combined index rather than planning itself."""
    from shared.shopping import shopping_list as sl_mod
    be = sl_mod._basket_engine()
    seen = {}
    real = be.plan_variants

    def spy(index, selected, start, dist_cache, max_variants=5):
        seen["selected"] = set(selected)
        seen["labels"] = {lbl for offers in index.values() for lbl in offers}
        return real(index, selected, start, dist_cache, max_variants=max_variants)
    monkeypatch.setattr(be, "plan_variants", spy)
    plans, _ = plan_routes(ENTRIES, ROUTES, PRICES, _Dist({}))
    assert seen.get("selected") == {"commodity:Laranite", "item:Medpen"}, seen
    assert seen["labels"] == {"commodity:Laranite", "item:Medpen"}
    assert plans


# ── the pop-out ──────────────────────────────────────────────────────────────

class _FakeSource:
    def item_catalog(self):
        return {"Medpen": 11, "Pembroke Helmet": 12}

    def commodity_names(self):
        return ["Laranite", "Gold"]

    def start_terminals(self):
        return {"Admin - Port Tressler": 1}

    def routes(self):
        return ROUTES

    def item_prices(self, entries):
        return PRICES

    def dist_cache(self):
        return _Dist({})


def _pump(cond, timeout=5.0):
    import time
    from PySide6.QtCore import QCoreApplication
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_popout_adds_items_and_commodities_and_auto_plans(tmp_path):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from shared.shopping.panel import ShoppingListWindow
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    win = ShoppingListWindow(sl, _FakeSource())
    pop = win.panel
    win.show()                 # a panel that is not on screen loads and plans nothing
    assert _pump(lambda: pop._names["item"] and True)
    pop.set_kind("commodity")
    assert _pump(lambda: pop._names["commodity"] and True)
    assert pop.add_entry("item", "medpen", 2)          # case-insensitive match to the catalogue
    assert pop.add_entry("commodity", "Laranite", 32)
    assert not pop.add_entry("commodity", "Not A Real Rock")
    assert [(e.kind, e.name, e.item_id) for e in sl.entries()] == \
        [("item", "Medpen", 11), ("commodity", "Laranite", None)]
    # auto-calculate is on: the list change schedules a plan without a click
    assert _pump(lambda: bool(pop.plans()), timeout=8.0), pop._status.text()
    assert pop.plans()[0].label == "MIN STOPS"
    win.hide()
    win.deleteLater()
