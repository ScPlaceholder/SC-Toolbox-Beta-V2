"""Items tab for the Cargo Loader (J, 2026-09-26).

"add a category tab which lists out those and basically draws them in
appropriately sized boxes. To snap and move around" / "the player will be the
one snapping them down on grid so if they don't match their cargo bay that's
user error not engine error" / "I'd have all the items be able to snap to
other items".

Runs offscreen. A small fixture item list stands in for the 14 MB datamine;
one test reads the real file only if it is on this machine. Nothing is
written to ~/.sctoolbox or to the user's saved plans.
"""

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt, QPoint, QPointF, QEvent  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from cargo_engine import item_catalog  # noqa: E402
from cargo_engine.manual_place import (  # noqa: E402
    PlacementContext, R_OVERLAP, R_TOO_TALL, W_OVERLAP, W_FLOATING, W_OUTSIDE,
)

SHIP = {
    "name": "Items Test Ship", "manufacturer": "Test", "capacity": 96,
    "provenance": {"source": "scunpacked-data"},
    "groups": [{"x": 0, "z": 0, "grids": [
        {"x": 0, "z": 0, "width": 6, "height": 2, "length": 8,
         "minSize": 1, "maxSize": 32},
    ]}],
}


def _m(w, h, l):
    return {"Width": w, "Height": h, "Length": l}


def _raw(cls, name, typ, size=1, grid=None, dims=None, scu=None, mfr="TEST"):
    io = {}
    if grid:
        io["CargoGrid"] = _m(*grid)
    if dims:
        io["Dimensions"] = _m(*dims)
    if scu is not None:
        io["Volume"] = {"SCU": scu}
    return {"className": cls, "stdItem": {
        "ClassName": cls, "Name": name, "Type": typ, "Size": size,
        "Manufacturer": {"Code": mfr, "Name": mfr.title()},
        "InventoryOccupancy": io}}


# Shapes copied from ship-items.json 4.10.1-LIVE.12660092 (metres).
RAW = [
    _raw("Cargo_ShipMining_Pod_Prospector", "MISC Ore Pod", "Container.Cargo",
         grid=(2.5, 2.5, 2.5), dims=(2.5, 2.5, 2.5), scu=8, mfr="MISC"),
    _raw("Cargo_ShipMining_Pod_Mole", "Argo Ore Pod", "Container.Cargo",       # junk grid
         grid=(0.75, 0.75, 0.75), dims=(2.5, 2.5, 2.5), scu=8, mfr="ARGO"),
    _raw("Cargo_ShipMining_Pod_Golem", "Drake Ore Pod", "Container.Cargo",
         grid=(2.5, 2.5, 5), dims=(2.5, 2.5, 5), scu=16, mfr="DRAK"),
    _raw("Cargo_GroundVehicleMining_Pod_ROC", "Greycat ROC Ore Pod", "Container.Cargo",
         scu=2.5, mfr="GRIN"),                                                   # no dims
    _raw("Cargo_ShipMining_Pod_Mole_Collapsed", "MISC Ore Pod", "Container.Cargo",
         grid=(0.75, 0.75, 0.75), dims=(2.5, 2.5, 2.5), scu=2),                 # skipped
    _raw("Cargo_ShipMining_Pod_Template", "MISC Ore Pod", "Container.Cargo",
         grid=(0.75, 0.75, 0.75), scu=50.0365),                                 # skipped
    _raw("MISL_S01_EM_BEHR_Pioneer", "Pioneer I Missile", "Missile.Missile",
         grid=(1, 1, 1), dims=(0.225, 0.225, 1.2), scu=0.0156, mfr="BEHR"),
    _raw("MRCK_S02_TEST", "Test Rack", "MissileLauncher.MissileRack"),          # skipped
    _raw("MISL_PH", "<= PLACEHOLDER =>", "Missile.Missile", dims=(1, 1, 1)),    # skipped
    _raw("BOMB_S10_FSKI_Colossus", "Colossus Bomb", "Bomb.Utility", size=10,
         grid=(1, 1, 10), dims=(2.3, 2.3, 6.75), scu=10, mfr="FSKI"),
    _raw("KLWE_LaserRepeater_S4", "Long Gun", "WeaponGun.Gun", size=4,
         dims=(0.5, 0.5, 5.0), mfr="KLWE"),                                      # 1x1x4
    _raw("COOL_AEGS_S01_Glacier_SCItem", "Glacier", "Cooler.UNDEFINED",
         grid=(0.22, 0.8, 2.21), dims=(0.751, 0.241, 0.513), mfr="AEGS"),
    _raw("POWR_JUST_S01_Fortitude_SCItem", "Fortitude", "PowerPlant.Power",
         dims=(0.741, 0.458, 0.502), mfr="JUST"),
    _raw("SHLD_GODI_S02_FR66", "FR-66", "Shield.UNDEFINED", size=2,
         dims=(1.49, 0.5, 0.51), mfr="GODI"),
    _raw("QDRV_TARS_S01_Expedition_SCItem", "Expedition", "QuantumDrive.UNDEFINED",
         dims=(0.769, 0.54, 0.5), mfr="TARS"),
]
POD = "Cargo_ShipMining_Pod_Prospector"
LONG = "KLWE_LaserRepeater_S4"


# ── catalogue (pure) ─────────────────────────────────────────────────────────

def test_catalogue_sizes_from_fixture():
    cat = {d["key"]: d for d in item_catalog.build_catalog(RAW)}
    assert cat[POD]["dims"] == (2, 2, 2) and cat[POD]["source"] == "grid"
    # junk 0.75 m grid -> falls back to Dimensions
    assert cat["Cargo_ShipMining_Pod_Mole"]["dims"] == (2, 2, 2)
    assert cat["Cargo_ShipMining_Pod_Mole"]["source"] == "dims"
    assert cat["Cargo_ShipMining_Pod_Golem"]["dims"] == (2, 2, 4)
    roc = cat["Cargo_GroundVehicleMining_Pod_ROC"]
    assert roc["approx"] and roc["dims"] == (2, 1, 2)
    # a grid that cannot hold the item is not a physical box
    assert cat["BOMB_S10_FSKI_Colossus"]["dims"] == (2, 2, 6)
    assert cat[LONG]["dims"] == (1, 1, 4)
    assert cat["COOL_AEGS_S01_Glacier_SCItem"]["dims"] == (1, 1, 1)
    for gone in ("Cargo_ShipMining_Pod_Mole_Collapsed", "Cargo_ShipMining_Pod_Template",
                 "MRCK_S02_TEST", "MISL_PH"):
        assert gone not in cat
    assert {d["category"] for d in cat.values()} == {k for k, _ in item_catalog.CATEGORIES}


def test_real_datamine_if_present():
    path = item_catalog.default_path()
    if not path or not os.path.isfile(path):
        pytest.skip("ship-items.json not downloaded on this machine")
    cat = {d["key"]: d for d in item_catalog.load_catalog(path)}
    assert cat["Cargo_ShipMining_Pod_Prospector"]["dims"] == (2, 2, 2)
    assert cat["Cargo_ShipMining_Pod_Mole"]["dims"] == (2, 2, 2)
    assert cat["Cargo_ShipMining_Pod_MoleProspector_BTGL"]["dims"] == (2, 2, 2)
    assert cat["Cargo_ShipMining_Pod_Golem"]["dims"] == (2, 2, 4)
    assert cat["Carryable_2H_FL_05x05x05_inventory_argoGeo"]["dims"] == (1, 1, 1)
    assert "Cargo_ShipMining_Pod_Prospector_Collapsed" not in cat
    cats = {d["category"] for d in cat.values()}
    assert cats == {k for k, _ in item_catalog.CATEGORIES}
    assert not any("PLACEHOLDER" in d["name"] for d in cat.values())


# ── UI ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def win(monkeypatch, tmp_path):
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import cargo_app
    import shared.qt.base_window as bw
    monkeypatch.setattr(cargo_app, "_fetch_uex_commodities", lambda: None)
    monkeypatch.setattr(cargo_app.ShipDataLoader, "load_async", lambda self, cb: None)
    monkeypatch.setattr(bw, "_save_window_state", lambda w: None)
    # Never the real datamine, never a download, in these tests.
    monkeypatch.setattr(cargo_app, "item_catalog_path",
                        lambda: str(tmp_path / "no-ship-items.json"), raising=False)
    w = cargo_app.CargoApp(0, 0, 1000, 800, 1.0, None)
    w._commodity_poll.stop()
    monkeypatch.setattr(w._data, "find", lambda name: dict(SHIP))
    w.show()
    w._load_ship("Items Test Ship")
    if hasattr(w, "_set_item_catalog"):     # absent before the Items tab existed
        w._set_item_catalog(item_catalog.build_catalog(RAW))
    QTest.qWait(80)
    w._render_grid()
    qapp.processEvents()
    yield w
    w.hide()
    w.deleteLater()
    qapp.processEvents()


def _send(view, etype, pos: QPoint, button, buttons):
    vp = view.viewport()
    ev = QMouseEvent(etype, QPointF(pos), QPointF(vp.mapToGlobal(pos)),
                     button, buttons, Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(vp, ev)


def _click(win, pos, button=Qt.LeftButton):
    _send(win._view, QEvent.MouseButtonPress, pos, button, button)
    _send(win._view, QEvent.MouseButtonRelease, pos, button, Qt.NoButton)
    QTest.qWait(30)
    QtWidgets.QApplication.processEvents()


def _vp(win, wx, wy, wz) -> QPoint:
    sx, sy = win._renderer._pt(wx, wy, wz)
    return win._view.mapFromScene(QPointF(sx, sy))


def _leaf(win, key):
    tree = win._items_tree
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        n = stack.pop()
        if n.data(0, Qt.UserRole) == key:
            return n
        stack += [n.child(i) for i in range(n.childCount())]
    raise AssertionError(f"{key} not listed")


def _arm(win, key):
    win._brush_tabs.setCurrentIndex(1)
    win._items_tree.itemClicked.emit(_leaf(win, key), 0)
    assert win._place_item == key and win._place_size is None


def _counts(win):
    return {s: sb.value() for s, sb in win._spinboxes.items() if sb.value()}


def _group(win, box):
    return next(g for g in win._renderer._box_groups if tuple(g.box_data) == box)


def test_items_tab_exists_and_lists_categories(win):
    tabs = win._brush_tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Commodities", "Items"]
    tree = win._items_tree
    tops = [tree.topLevelItem(i).text(0).split(" (")[0] for i in range(tree.topLevelItemCount())]
    assert tops == ["Ore Pods", "Missiles", "Bombs", "Ship Weapons", "Components"]
    comp = tree.topLevelItem(4)
    assert [comp.child(i).text(0).split(" (")[0] for i in range(comp.childCount())] == [
        "Coolers", "Power Plants", "Shields", "Quantum Drives"]
    pod = _leaf(win, POD)
    assert pod.text(0) == "MISC Ore Pod" and pod.text(1).endswith("2×2×2")
    # search narrows the list
    win._items_search.setText("glacier")
    assert tree.topLevelItem(4).text(0) == "Components (1)"
    assert tree.topLevelItem(0).text(0) == "Ore Pods (0)"


def test_missing_item_data_degrades(win):
    win._item_catalog = None
    win._brush_tabs.setCurrentIndex(0)
    win._brush_tabs.setCurrentIndex(1)
    assert win._items_note.text() == "Item data not downloaded yet."


def test_pod_places_at_2x2x2_and_counts_stay(win):
    _arm(win, POD)
    p = _vp(win, 3, 0, 4)
    _send(win._view, QEvent.MouseMove, p, Qt.NoButton, Qt.NoButton)
    assert win._renderer._ghost is not None and win._renderer._ghost.ghost_valid
    _click(win, p)
    assert win._renderer._items == [(2, 0, 3, 2, 2, 2, POD)]
    assert win._renderer._last_boxes == [] and _counts(win) == {}
    assert win._items_summary_lbl.text() == "Items: 1"
    g = _group(win, (2, 0, 3, 2, 2, 2, POD))
    assert g.item_pen is not None and not g.warnings
    # right-click removes it, Ctrl+Z brings it back
    _send(win._view, QEvent.MouseButtonPress, _vp(win, 3, 2, 4), Qt.RightButton, Qt.RightButton)
    QTest.qWait(30)
    QtWidgets.QApplication.processEvents()
    assert win._renderer._items == []
    win._undo_move()
    assert win._renderer._items == [(2, 0, 3, 2, 2, 2, POD)]


def test_rotate_item_with_r(win):
    _arm(win, LONG)
    QTest.keyClick(win._view, Qt.Key_R)
    _click(win, _vp(win, 3, 0, 4))
    (item,) = win._renderer._items
    assert item[3:6] == (4, 1, 1)


def test_overlapping_item_still_places_amber(win):
    win._renderer._manual_boxes = [(2, 0, 0, 2, 2, 2, 8)]
    win._sync_counts_from_boxes()
    win._render_grid()
    _arm(win, LONG)
    p = _vp(win, 2.5, 0, 3)          # a 1x1x4 gun whose far end runs into the 8 SCU box
    _send(win._view, QEvent.MouseMove, p, Qt.NoButton, Qt.NoButton)
    ghost = win._renderer._ghost
    assert ghost.ghost_valid and ghost.ghost_warn
    _click(win, p)
    item = (2, 0, 1, 1, 1, 4, LONG)
    assert win._renderer._items == [item]
    assert win._renderer._item_flags[item] == [W_OVERLAP]
    g = _group(win, item)
    assert g.warnings == [W_OVERLAP]
    assert g.item_pen.color().name() == "#ffb000"
    assert "overlaps another box" in win._status_lbl.text()
    assert "flagged" in win._items_summary_lbl.text()
    assert _counts(win) == {8: 1}                       # the container is untouched


def test_item_flags_floating_and_outside():
    grids = [{"x": 0, "y0": 0, "z": 0, "w": 6, "h": 2, "l": 8}]
    ctx = PlacementContext(grids, [])
    assert ctx.item_warnings((0, 1, 0), (1, 1, 1)) == [W_FLOATING]
    assert ctx.item_warnings((5, 0, 0), (2, 1, 1)) == [W_OUTSIDE]


@pytest.mark.parametrize("neighbour", ["container", "item"])
def test_item_snaps_flush_to_adjacent_box(win, neighbour):
    if neighbour == "container":
        win._renderer._manual_boxes = [(0, 0, 0, 2, 2, 2, 8)]
        win._sync_counts_from_boxes()
    else:
        win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)]
    win._render_grid()
    _arm(win, POD)
    # pointer centre x=3.6: plain rounding gives x=3 (a one-cell gap); the
    # magnet pulls the pod's face onto the neighbour's at x=2
    _click(win, _vp(win, 3.6, 0, 1.0))
    placed = win._renderer._items[-1]
    assert placed == (2, 0, 0, 2, 2, 2, POD)
    assert not win._renderer._item_flags.get(placed)


def test_drag_item_onto_container_flush(win):
    win._renderer._manual_boxes = [(0, 0, 0, 2, 2, 2, 8)]
    win._sync_counts_from_boxes()
    win._renderer._items = [(3, 0, 5, 2, 2, 2, POD)]
    win._render_grid()
    view = win._view
    L, NB = Qt.LeftButton, Qt.NoButton
    grab = _vp(win, 4, 2, 6)                      # pod's top-face centre
    _send(view, QEvent.MouseButtonPress, grab, L, L)
    _send(view, QEvent.MouseMove, grab + QPoint(3, 3), NB, L)
    tgt = _vp(win, 3.6, 2, 1.0)
    _send(view, QEvent.MouseMove, tgt, NB, L)
    _send(view, QEvent.MouseButtonRelease, tgt, L, NB)
    QTest.qWait(30)
    QtWidgets.QApplication.processEvents()
    assert win._renderer._items == [(2, 0, 0, 2, 2, 2, POD)]
    assert _counts(win) == {8: 1}


def test_save_load_round_trips_items(win):
    win._renderer._manual_boxes = [(0, 0, 0, 2, 2, 2, 8)]
    win._sync_counts_from_boxes()
    win._renderer._items = [(2, 0, 0, 2, 2, 2, POD), (0, 0, 4, 1, 1, 4, LONG)]
    win._renderer._assignments[(2, 0, 0, POD)] = "Mission Cargo 3"
    win._render_grid()
    payload = json.loads(json.dumps(win._loadout_payload()))   # what a file holds
    assert payload["counts"]["8"] == 1
    assert [it["key"] for it in payload["items"]] == [POD, LONG]
    assert payload["items"][0]["pos"] == [2, 0, 0] and payload["items"][0]["dims"] == [2, 2, 2]
    assert payload["items"][0]["name"] == "MISC Ore Pod"

    win._renderer._items = []
    win._pending_loadout = payload
    win._load_ship(payload["ship"])
    assert sorted(win._renderer._items) == sorted(
        [(2, 0, 0, 2, 2, 2, POD), (0, 0, 4, 1, 1, 4, LONG)])
    assert win._renderer._manual_boxes == [(0, 0, 0, 2, 2, 2, 8)]
    assert win._renderer._assignments.get((2, 0, 0, POD)) == "Mission Cargo 3"

    # an older plan (no "items") still loads, with no items
    old = {k: v for k, v in payload.items() if k != "items"}
    win._pending_loadout = old
    win._load_ship(old["ship"])
    assert win._renderer._items == [] and win._renderer._manual_boxes == [(0, 0, 0, 2, 2, 2, 8)]


def test_containers_still_refused_over_items(win):
    _arm(win, POD)
    _click(win, _vp(win, 3, 0, 4))
    pod = (2, 0, 3, 2, 2, 2, POD)
    assert win._renderer._items == [pod]
    win._place_btns[8].click()
    assert win._place_size == 8 and win._place_item is None
    on_pod = _vp(win, 3, 2, 4)                    # pointing at the pod's top
    _, _, valid, reason = win._place_target(win._view.mapToScene(on_pod))
    assert not valid and reason == R_TOO_TALL
    _click(win, on_pod)
    assert win._renderer._last_boxes == [] and _counts(win) == {}
    # and dropped straight into it: the container rule is still a wall
    ctx = PlacementContext(win._grids_world(), [pod])
    _, ok, why = ctx.snap((2, 2, 2, 8), (2, 0, 3))
    assert not ok and why == R_OVERLAP


def test_items_do_not_touch_counts_or_optimize(win):
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)]
    win._render_grid()
    win._optimize()
    assert win._renderer._items == [(0, 0, 0, 2, 2, 2, POD)]
    _group(win, (0, 0, 0, 2, 2, 2, POD))                 # still drawn
    assert win._renderer._last_boxes                     # the packer filled the grid
    assert all(not isinstance(b[6], str) for b in win._renderer._last_boxes)
    placed = sum(b[6] for b in win._renderer._last_boxes)
    assert sum(s * n for s, n in _counts(win).items()) == placed
    assert win._items_summary_lbl.text().startswith("Items: 1")


def test_brush_paints_an_item(win):
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)]
    win._render_grid()
    win._on_commodity_selected("Mission Cargo 1")
    g = _group(win, (0, 0, 0, 2, 2, 2, POD))
    win._on_box_clicked(g)
    assert win._renderer._assignments[(0, 0, 0, POD)] == "Mission Cargo 1"
