"""One shopping list for the whole toolbox (J, 2026-10-04).

There were three - the Everything Finder's pop-out, Item Finder's Grocery List
bubble, the Star Map's Grocery panel - with three stores and three planners.
This file pins what "one" means, and what the survivor had to learn from the
two that were retired:

  * ONE model and file: what one tool adds, every other tool's list shows -
    two widgets in one process, and two processes through the file;
  * the Star Map panel's PIN (buy it where you found it) and its saved list;
  * Item Finder's per-item "where to buy, cheapest first" and drag-and-drop;
  * both retired lists' "the route on the map follows the list";
  * and that none of it brought a second route planner along.

No network: routes, item prices and distances are supplied by the test.
"""
import json
import os
import re
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)
import shared.path_setup  # noqa: E402

import pytest  # noqa: E402

from shared.shopping import shopping_list as sl_mod  # noqa: E402
from shared.shopping.shopping_list import (  # noqa: E402
    Entry, ShoppingList, build_index, item_to_entry_args, pin_matches, plan_routes,
)


# ── the model ────────────────────────────────────────────────────────────────

def test_an_item_from_the_map_is_pinned_and_a_table_row_is_not():
    from_map = item_to_entry_args({"id": 7, "name": "Pembroke Helmet", "category": "Armor",
                                   "price": 5200, "location": "Area 18", "system": "Stanton"})
    assert from_map == {"kind": "item", "name": "Pembroke Helmet", "item_id": 7,
                        "pin": {"location": "Area 18", "system": "Stanton", "price": 5200.0}}
    from_table = item_to_entry_args({"id": "9", "name": "Medpen", "category": "Medical"})
    assert from_table == {"kind": "item", "name": "Medpen", "item_id": 9, "pin": None}
    assert item_to_entry_args({"id": 1}) is None and item_to_entry_args("nope") is None


def test_adding_the_same_item_twice_by_drag_is_not_an_order_for_two(tmp_path):
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    sl.add_item({"id": 9, "name": "Medpen"})
    sl.add_item({"id": 9, "name": "medpen"})
    assert [(e.name, e.qty) for e in sl.entries()] == [("Medpen", 1)]
    sl.add("item", "Medpen", 2)                       # typed with a quantity: that IS an order
    assert sl.entries()[0].qty == 3


def test_a_pin_survives_a_restart_and_can_be_taken_off(tmp_path):
    p = str(tmp_path / "l.json")
    sl = ShoppingList(path=p)
    sl.add_item({"id": 7, "name": "Helmet", "location": "Area 18", "system": "Stanton", "price": 10})
    again = ShoppingList(path=p).load()
    assert again.entries()[0].pin == {"location": "Area 18", "system": "Stanton", "price": 10.0}
    assert again.set_pin("item", "helmet", None)
    assert ShoppingList(path=p).load().entries()[0].pin is None
    assert "pin" not in json.load(open(p, encoding="utf-8"))[0]


def test_two_tools_two_processes_one_list(tmp_path):
    """Item Finder and the Star Map are separate processes. Each holds its own
    ShoppingList object on the same file; what one writes the other must see,
    and neither may wipe out the other's entry by saving a stale copy."""
    p = str(tmp_path / "shopping_list.json")
    item_finder, star_map = ShoppingList(path=p).load(), ShoppingList(path=p).load()
    seen = []
    star_map.subscribe(lambda: seen.append([e.name for e in star_map.entries()]))

    item_finder.add("item", "Medpen", 2)
    assert star_map.refresh() is True, "the Star Map did not notice Item Finder's change"
    assert seen == [["Medpen"]]
    assert star_map.refresh() is False                 # nothing new: no reload, no notification

    # the Star Map adds WITHOUT refreshing first: Item Finder's entry must survive
    time.sleep(0.02)
    item_finder.add("commodity", "Gold", 8)
    star_map.add_item({"id": 7, "name": "Helmet", "location": "Area 18", "system": "Stanton"})
    on_disk = [(d["kind"], d["name"]) for d in json.load(open(p, encoding="utf-8"))]
    assert on_disk == [("item", "Medpen"), ("commodity", "Gold"), ("item", "Helmet")]
    assert item_finder.refresh() is True
    assert [e.name for e in item_finder.entries()] == ["Medpen", "Gold", "Helmet"]

    item_finder.remove("item", "Medpen")
    star_map.clear()                                    # clears what is on disk NOW
    assert json.load(open(p, encoding="utf-8")) == []


def test_clear_empties_what_is_on_disk_even_if_this_tool_had_not_looked_yet(tmp_path):
    p = str(tmp_path / "shopping_list.json")
    star_map = ShoppingList(path=p).load()            # opened while the list was empty
    ShoppingList(path=p).load().add("item", "Medpen")  # Item Finder adds something
    star_map.clear()                                   # "Clear list" in the Star Map
    assert json.load(open(p, encoding="utf-8")) == [], \
        "Clear did nothing because this tool still believed the list was empty"


def test_a_rewrite_with_the_same_content_is_not_announced_as_a_change(tmp_path):
    p = tmp_path / "shopping_list.json"
    sl = ShoppingList(path=str(p)).load()
    sl.add("item", "Medpen")
    seen = []
    sl.subscribe(lambda: seen.append(1))
    time.sleep(0.02)
    p.write_text(p.read_text(encoding="utf-8") + " ", encoding="utf-8")   # same entries, new stamp
    assert sl.refresh() is False and seen == [], \
        "every panel would re-render and re-plan for a change that is not one"


def test_a_half_written_file_is_not_mistaken_for_an_empty_list(tmp_path):
    p = tmp_path / "l.json"
    sl = ShoppingList(path=str(p)).load()
    sl.add("item", "Medpen")
    time.sleep(0.02)
    p.write_text('[{"kind": "item", "na', encoding="utf-8")     # another tool, mid-write
    assert sl.refresh() is False
    assert [e.name for e in sl.entries()] == ["Medpen"]


def test_the_retired_lists_files_are_brought_in_once_and_left_alone(tmp_path):
    ef = tmp_path / "ef_shopping_list.json"
    ef.write_text(json.dumps([{"kind": "commodity", "name": "Gold", "qty": 8, "item_id": None},
                              {"kind": "item", "name": "Medpen", "qty": 2, "item_id": 11}]),
                  encoding="utf-8")
    grocery = tmp_path / "grocery.json"
    grocery.write_text(json.dumps([
        {"id": 7, "name": "Helmet", "category": "Armor", "price": 100, "location": "Area 18",
         "system": "Stanton"},
        {"id": 11, "name": "Medpen", "price": 50, "location": "Lorville", "system": "Stanton"},
    ]), encoding="utf-8")
    before = (ef.read_bytes(), grocery.read_bytes())
    new = tmp_path / "shopping" / "shopping_list.json"
    legacy = [("entries", str(ef)), ("grocery", str(grocery)), ("grocery", str(tmp_path / "none.json"))]

    sl = ShoppingList(path=str(new), legacy=legacy).load()
    got = [(e.kind, e.name, e.qty, e.item_id, (e.pin or {}).get("location")) for e in sl.entries()]
    assert got == [("commodity", "Gold", 8, None, None),
                   ("item", "Medpen", 2, 11, "Lorville"),     # same item in both: one entry
                   ("item", "Helmet", 1, 7, "Area 18")]
    assert new.exists(), "the imported list was not saved to the shared file"
    assert (ef.read_bytes(), grocery.read_bytes()) == before

    # once: the shared file exists now, so the old files are not read again
    sl.remove("item", "Helmet")
    again = ShoppingList(path=str(new), legacy=legacy).load()
    assert [e.name for e in again.entries()] == ["Gold", "Medpen"]


def test_the_real_store_knows_where_the_retired_lists_were(monkeypatch, tmp_path):
    monkeypatch.setattr(sl_mod, "_root", lambda: str(tmp_path))
    assert sl_mod.store_path() == os.path.join(str(tmp_path), "shopping", "shopping_list.json")
    assert sl_mod.legacy_stores() == [
        ("entries", os.path.join(str(tmp_path), "everything_finder", "shopping_list.json")),
        ("grocery", os.path.join(str(tmp_path), "starmap", "grocery.json"))]
    assert ShoppingList()._legacy == sl_mod.legacy_stores()
    assert ShoppingList(path=str(tmp_path / "x.json"))._legacy == [], \
        "a caller's own file must not pull in the user's old lists"


def test_every_tool_in_a_process_gets_the_same_object(monkeypatch, tmp_path):
    monkeypatch.setattr(sl_mod, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(sl_mod, "_shared", None)
    assert sl_mod.shared_list() is sl_mod.shared_list()
    assert sl_mod.shared_list().path == sl_mod.store_path()


# ── planning: pins and offers are inputs, the math is Trade Hub's ─────────────

def _route(commodity, tid, tname, loc, price, sys_="Stanton"):
    from shared.shopping.paths import ensure_trade_hub_path
    ensure_trade_hub_path()
    from trade_hub_data import Route
    return Route(commodity=commodity, buy_terminal=tname, buy_location=loc, buy_system=sys_,
                 sell_terminal="Sink", sell_location="Sink", sell_system=sys_,
                 scu_available=100, scu_demand=100, price_buy=price, price_sell=price * 2,
                 margin=price, id_terminal_buy=tid, id_terminal_sell=999)


def _price_row(tid, tname, price, city, system="Stanton"):
    return {"id_terminal": tid, "terminal_name": tname, "price_buy": price,
            "star_system_name": system, "city_name": city}


class _Dist:
    def get(self, a, b):
        return 50.0

    def fetch_missing(self, pairs, on_progress=None):
        pass


PRICES = {"Helmet": [_price_row(1, "Casaba - Lorville", 80.0, "Lorville"),
                     _price_row(2, "Casaba - Area18", 100.0, "Area18"),
                     _price_row(3, "Casaba - New Babbage", 120.0, "New Babbage")]}
PINNED = Entry("item", "Helmet", 1, pin={"location": "Area 18", "system": "Stanton"})


def test_pin_matching_forgives_spelling_but_not_the_place():
    from shared.shopping.shopping_list import _basket_engine
    TK = _basket_engine().TerminalKey
    area18 = TK(2, "Casaba - Area18", "Area18", "Stanton")
    assert pin_matches({"location": "Area 18", "system": "Stanton"}, area18)
    assert pin_matches({"location": "area18"}, area18)
    assert pin_matches({"location": "ArcCorp"}, area18, places=["Area18", "ArcCorp"])
    assert not pin_matches({"location": "Lorville"}, area18)
    assert not pin_matches({"location": "Area 18", "system": "Pyro"}, area18)
    assert not pin_matches({"location": ""}, area18)


def test_a_pinned_item_is_bought_at_its_pin_not_at_the_cheapest_terminal():
    free = build_index([Entry("item", "Helmet")], [], PRICES)
    assert sorted(tk.terminal_id for tk in free.index) == [1, 2, 3]
    pinned = build_index([PINNED], [], PRICES)
    assert [tk.terminal_id for tk in pinned.index] == [2], "the pin did not narrow the candidates"
    assert pinned.pin_missed == []
    plans, _ = plan_routes([PINNED], [], PRICES, _Dist(), prefer="BEST PRICE")
    assert [s.terminal.terminal_id for s in plans[0].stops] == [2]
    plans, _ = plan_routes([Entry("item", "Helmet")], [], PRICES, _Dist(), prefer="BEST PRICE")
    assert [s.terminal.terminal_id for s in plans[0].stops] == [1]


def test_a_pin_nobody_sells_at_is_reported_and_planned_anywhere():
    lost = Entry("item", "Helmet", 1, pin={"location": "Orison", "system": "Stanton"})
    pi = build_index([lost], [], PRICES)
    assert pi.pin_missed == ["item:Helmet"]
    assert sorted(tk.terminal_id for tk in pi.index) == [1, 2, 3]


def test_a_commodity_can_be_pinned_too():
    routes = [_route("Gold", 5, "TDD - Area18", "Area18", 9.0),
              _route("Gold", 6, "Admin - Lorville", "Lorville", 7.0)]
    pi = build_index([Entry("commodity", "Gold", 8, pin={"location": "Area 18"})], routes, {})
    assert [tk.terminal_id for tk in pi.index] == [5]


def test_every_offer_is_kept_cheapest_first_for_the_where_to_buy_list():
    pi = build_index([Entry("item", "Helmet")], [], PRICES, per_entry_limit=1)
    assert [tk.terminal_id for tk in pi.index] == [1]                # the planner gets one
    listed = [(tk.terminal_id, off.price_buy) for tk, off in pi.offers["item:Helmet"]]
    assert listed == [(1, 80.0), (2, 100.0), (3, 120.0)]            # the pilot sees them all


def test_no_second_route_planner_came_along():
    """J: route math stays Trade Hub's; a duplicate is exactly what he does not want.
    The two retired lists planned with market_finder.route_planner / starmap.route_planner.
    Nothing in the shared list may import either, or carry an ordering routine of its own."""
    pkg = os.path.join(ROOT, "shared", "shopping")
    banned = re.compile(r"route_planner|order_stops|plan_shopping|held_karp|two_opt|"
                        r"itertools\.permutations|commodities_routes|uex\.space|urlopen", re.I)
    hits = []
    for name in sorted(os.listdir(pkg)):
        if name.endswith(".py"):
            with open(os.path.join(pkg, name), encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    code = line.split("#", 1)[0]
                    if banned.search(code) and '"""' not in line and not _in_docstring(pkg, name, i):
                        hits.append(f"{name}:{i}: {line.strip()}")
    assert hits == [], "\n".join(hits)
    # and the retired UIs are really gone, not just unused
    for gone in ("skills/Market_Finder/market_finder/ui/grocery_list.py",
                 "skills/Starmap/starmap/grocery.py",
                 "skills/Everything_Finder/everything_finder/shopping_list.py",
                 "skills/Everything_Finder/everything_finder/shopping_popout.py"):
        assert not os.path.exists(os.path.join(ROOT, *gone.split("/"))), gone + " is back"


def _in_docstring(pkg, name, lineno):
    import ast
    with open(os.path.join(pkg, name), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                if body[0].lineno <= lineno <= body[0].end_lineno:
                    return True
    return False


def test_the_tools_no_longer_call_their_own_planners():
    """The retired planners' modules still exist (the star maps use their `visits`
    helper to draw). Their PLANNING entry points must have no caller left."""
    callers = []
    planning = re.compile(r"\b(plan_shopping|order_stops|plan_route|collect_wants|candidate_sites)\s*\(")
    for skill in ("Market_Finder", "Starmap", "Everything_Finder"):
        for root, _dirs, files in os.walk(os.path.join(ROOT, "skills", skill)):
            if "tests" in root.split(os.sep) or "__pycache__" in root:
                continue
            for name in files:
                if not name.endswith(".py") or name == "route_planner.py":
                    continue
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if planning.search(line.split("#", 1)[0]):
                            callers.append(f"{os.path.relpath(os.path.join(root, name), ROOT)}:{i}")
    assert callers == [], callers


# ── the widget ───────────────────────────────────────────────────────────────

class _Source:
    def __init__(self):
        self.calls = []

    def item_catalog(self):
        self.calls.append("item_catalog")
        return {"Helmet": 7, "Medpen": 11}

    def commodity_names(self):
        self.calls.append("commodity_names")
        return ["Gold"]

    def start_terminals(self):
        return {"Casaba - Lorville": 1}

    def routes(self):
        return [_route("Gold", 1, "Casaba - Lorville", "Lorville", 7.0)]

    def item_prices(self, entries):
        self.calls.append("item_prices")
        return {**PRICES, "Medpen": [_price_row(1, "Casaba - Lorville", 30.0, "Lorville")]}

    def dist_cache(self):
        return _Dist()


def _pump(cond, timeout=6.0):
    from PySide6.QtCore import QCoreApplication
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def qt():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    made = []
    yield made
    from PySide6.QtCore import QCoreApplication, QEvent
    for w in made:
        w.hide()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()


def _panel(qt, sl, source=None, **kw):
    from shared.shopping.panel import ShoppingListPanel
    p = ShoppingListPanel(sl, source or _Source(), **kw)
    qt.append(p)
    return p


def test_a_panel_that_is_not_on_screen_fetches_nothing_and_plans_nothing(qt, tmp_path):
    """The Star Map builds its docked list hidden. Opening the map must not start
    loading Item Finder's catalogue or planning routes behind the pilot's back."""
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    src = _Source()
    p = _panel(qt, sl, src)
    sl.add("item", "Helmet")
    _pump(lambda: False, 1.2)                       # longer than the auto-plan delay
    assert src.calls == [] and p.plans() == []
    p.show()
    assert _pump(lambda: bool(p.plans())), "showing the panel did not catch up"
    assert "item_catalog" in src.calls and "item_prices" in src.calls


def test_two_widgets_on_one_list_show_the_same_thing(qt, tmp_path):
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    item_finder, star_map = _panel(qt, sl), _panel(qt, sl)
    star_map.add_item({"id": 7, "name": "Helmet", "location": "Area 18", "system": "Stanton"})
    assert item_finder.row("item", "Helmet") is not None
    assert "Area 18" in item_finder.row("item", "Helmet").pin_label.text()
    item_finder._list.remove("item", "Helmet")
    assert star_map.row("item", "Helmet") is None and "0 item(s)" in star_map._count.text()


def test_an_open_panel_picks_up_what_another_process_added(qt, tmp_path):
    path = str(tmp_path / "l.json")
    here, elsewhere = ShoppingList(path=path).load(), ShoppingList(path=path).load()
    p = _panel(qt, here)
    p._auto.setChecked(False)
    p.show()
    elsewhere.add("commodity", "Gold", 8)            # e.g. typed into Item Finder's window
    assert _pump(lambda: p.row("commodity", "Gold") is not None, 5.0), \
        "the open panel never noticed the other tool's change"


def test_a_dragged_item_can_be_dropped_on_the_list(qt, tmp_path):
    from PySide6.QtCore import QMimeData, QPoint, Qt
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from shared.qt.data_table import SC_ITEM_MIME
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    p = _panel(qt, sl)
    assert p.acceptDrops()

    def drop(payload, fmt=SC_ITEM_MIME):
        mime = QMimeData()
        mime.setData(fmt, payload)
        enter = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        p.dragEnterEvent(enter)
        ev = QDropEvent(QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        p.dropEvent(ev)
        return enter.isAccepted(), ev.isAccepted()

    # a row dragged out of Item Finder's table
    assert drop(json.dumps({"id": 11, "name": "Medpen", "category": "Medical"}).encode()) == (True, True)
    # a Star Map item pop-out: carries its place, so it is pinned there
    assert drop(json.dumps({"id": 7, "name": "Helmet", "price": 100, "location": "Area 18",
                            "system": "Stanton"}).encode()) == (True, True)
    got = [(e.name, e.item_id, (e.pin or {}).get("location")) for e in sl.entries()]
    assert got == [("Medpen", 11, None), ("Helmet", 7, "Area 18")]
    # the same thing dropped again: said, not doubled
    drop(json.dumps({"id": 11, "name": "Medpen"}).encode())
    assert [e.qty for e in sl.entries()] == [1, 1] and "already on the list" in p.status()
    # not an item, not our format: refused, nothing added
    assert drop(b"not json")[1] is False
    assert drop(b"{}", fmt="text/plain") == (False, False)
    assert len(sl) == 2


def test_each_entry_lists_where_it_is_sold_and_the_pin_can_be_clicked_off(qt, tmp_path):
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    p = _panel(qt, sl)
    p.show()
    sl.add_item({"id": 7, "name": "Helmet", "location": "Area 18", "system": "Stanton"})
    assert _pump(lambda: bool(p.plans()))
    row = p.row("item", "Helmet")
    # collapsed: the best price only, and a way to see the other two
    assert len(row.offer_labels) == 1 and "Casaba - Lorville" in row.offer_labels[0].text()
    assert "2 other places" in row.toggle_label.text()
    p._toggle_offers("item:Helmet")
    row = p.row("item", "Helmet")
    assert [l.text().strip().split("  ")[0] for l in row.offer_labels] == \
        ["Casaba - Lorville", "Casaba - Area18", "Casaba - New Babbage"]
    assert "hide" in row.toggle_label.text()
    # pinned to Area 18: the plan buys there, not at the cheaper Lorville
    assert [s.terminal.terminal_id for s in p.plans()[0].stops] == [2]
    row.pin_label._on_click()                          # click the pin tag: unpin
    assert sl.entries()[0].pin is None
    assert _pump(lambda: p.plans() and p.plans()[0].stops[0].terminal.terminal_id == 1), \
        "unpinning did not re-plan to the cheapest terminal"
    assert p.row("item", "Helmet").pin_label is None


def test_the_route_on_the_map_follows_the_list_and_leaves_with_it(qt, tmp_path):
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    drawn, cleared = [], []
    p = _panel(qt, sl,
               on_show_on_map=lambda plan, auto: drawn.append(
                   (sorted(o.commodity for s in plan.stops for o in s.picks), auto)),
               on_clear_map=lambda: cleared.append(1))
    p.show()
    sl.add("item", "Helmet")
    assert _pump(lambda: bool(p.plans()))
    assert drawn == [], "a route was drawn that nobody asked for"
    p.show_on_map(p.plans()[0])
    assert drawn == [(["item:Helmet"], False)]
    sl.add("item", "Medpen")                           # the list changes ...
    assert _pump(lambda: len(drawn) == 2), "the drawn route did not follow the list"
    assert drawn[1] == (["item:Helmet", "item:Medpen"], True)      # ... and says it is a redraw
    sl.clear()
    assert cleared == [1], "emptying the list left its route on the map"
    sl.add("item", "Medpen")                           # a fresh list starts with a clean map
    assert _pump(lambda: bool(p.plans()))
    assert len(drawn) == 2


def test_the_map_is_left_alone_until_asked(qt, tmp_path):
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    drawn = []
    p = _panel(qt, sl, on_show_on_map=lambda plan, auto: drawn.append(auto))
    p.show()
    sl.add("item", "Helmet")
    assert _pump(lambda: bool(p.plans()))
    p.show_on_map(p.plans()[0])
    p.map_route_cleared()                              # the host says the pilot cleared it
    sl.add("item", "Medpen")
    assert _pump(lambda: len(p.plans()[0].stops[0].picks) == 2 or len(p.plans()[0].stops) == 2)
    assert drawn == [False]


def test_the_pop_out_is_the_same_panel_in_a_frame(qt, tmp_path):
    from shared.shopping.panel import ShoppingListPanel, ShoppingListWindow
    sl = ShoppingList(path=str(tmp_path / "l.json"))
    win = ShoppingListWindow(sl, _Source())
    qt.append(win)
    assert isinstance(win.panel, ShoppingListPanel) and win.panel._list is sl
