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
    W_TOO_TALL, R_ITEM_OVERLAP, R_ITEM_OUTSIDE, R_ITEM_TOO_TALL, OK,
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


def _raw(cls, name, typ, size=1, grid=None, dims=None, scu=None, mfr="TEST",
         own=None):
    """One ship-items.json entry.

    *own* is stdItem's own top-level Width/Height/Length — the item's real size,
    which every entry in the live file carries and which is the reference the
    CargoGrid is checked against. Left out, there is no reference and the older
    Dimensions cross-check stands in; see test_cargo_grid_is_checked_against...
    """
    io = {}
    if grid:
        io["CargoGrid"] = _m(*grid)
    if dims:
        io["Dimensions"] = _m(*dims)
    if scu is not None:
        io["Volume"] = {"SCU": scu}
    std = {
        "ClassName": cls, "Name": name, "Type": typ, "Size": size,
        "Manufacturer": {"Code": mfr, "Name": mfr.title()},
        "InventoryOccupancy": io}
    if own:
        std.update(_m(*own))
    return {"className": cls, "stdItem": std}


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
         own=(1, 1, 10),                                                     # 1x1x8
         grid=(1, 1, 10), dims=(2.3, 2.3, 6.75), scu=10, mfr="FSKI"),
    _raw("KLWE_LaserRepeater_S4", "Long Gun", "WeaponGun.Gun", size=4,
         dims=(0.5, 0.5, 5.0), mfr="KLWE"),                                      # 1x1x4
    _raw("COOL_AEGS_S01_Glacier_SCItem", "Glacier", "Cooler.UNDEFINED",
         own=(0.2233005, 0.801895, 2.211008),                                # 1x1x2
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
    # A grid that cannot hold the item is not a physical box -- and the item is
    # its own stdItem Width/Height/Length, not InventoryOccupancy.Dimensions.
    # Both of these carry a Dimensions box that disagrees with their real size,
    # so both used to be measured by it: the bomb read (2, 2, 6) off 2.3x2.3x6.75
    # and the Glacier (1, 1, 1) off 0.751x0.241x0.513. Their own boxes match
    # their CargoGrids to 2 cm and that is what they are sized by now.
    assert cat["BOMB_S10_FSKI_Colossus"]["dims"] == (1, 1, 8)
    assert cat[LONG]["dims"] == (1, 1, 4)
    assert cat["COOL_AEGS_S01_Glacier_SCItem"]["dims"] == (1, 1, 2)
    for gone in ("Cargo_ShipMining_Pod_Mole_Collapsed", "Cargo_ShipMining_Pod_Template",
                 "MRCK_S02_TEST", "MISL_PH"):
        assert gone not in cat
    assert {d["category"] for d in cat.values()} == {k for k, _ in item_catalog.CATEGORIES}


# ── the CargoGrid is measured against the item's OWN size, not Dimensions ────
#
# Characterised 2026-09-27, from "ship items are not working correctly with the
# grid". footprint() preferred the CargoGrid only when it CONTAINED
# InventoryOccupancy.Dimensions, on the reasoning that a grid too small to hold
# the item is an inventory-UI box. The reference was the wrong field. Measured
# over the whole live file (486 catalogue entries):
#
#     CargoGrid  agrees with stdItem's own Width/Height/Length : 477 of 477
#     Dimensions agrees with it                                :   3 of 480
#
# So Dimensions does not describe the item at all, and comparing the grid to it
# rejected the correct box 350 times: 316 of 486 items were sized off a field
# that contradicts the item's own dimensions. Five were sized past every bay in
# the game -- a size-4 Serac cooler came out 15x6x12 cells (18 m), and three
# size-10 weapons 31x12x86, longer than the Idris that mounts them.
#
# stdItem carries the item's real size as top-level Width/Height/Length, and
# every entry in the live file has it, so it is the reference the grid is
# checked against now. J's stated rule is "CargoGrid, else Dimensions" with the
# junk 0.75^3 grids falling back -- which is what this restores; the Dimensions
# containment test was never part of it.

BESPOKE = [
    # Shapes copied verbatim from ship-items.json 4.10.1-LIVE.12660092. In both,
    # the CargoGrid IS the item's own box to 2 cm and Dimensions is unrelated.
    _raw("COOL_RSI_S04_Polaris_SCItem", "Serac", "Cooler.UNDEFINED", size=4,
         own=(0.8932018, 1.60379, 6.633023),
         grid=(0.89, 1.6, 6.63), dims=(18.065, 7.381, 14.592), scu=2.1, mfr="RSI"),
    _raw("HRST_LaserBeam_Bespoke", "Exodus-10 Laser Beam", "WeaponGun.Gun", size=10,
         own=(3.796108, 4.81137, 30.95411),
         grid=(3.8, 4.81, 30.95), dims=(37.558, 14.875, 106.871), scu=120, mfr="HRST"),
]


def test_the_bespoke_fixture_can_tell_the_two_rules_apart():
    """Guard on the fixture, not on the code.

    These two entries prove nothing unless the old reference and the new one
    actually disagree about them: the Dimensions box must NOT fit inside the
    CargoGrid (so the old rule threw the grid away) while the item's own box
    must (so the new rule keeps it). Without this, both tests below could pass
    on a fixture where the rules never diverge."""
    for e in BESPOKE:
        io = e["stdItem"]["InventoryOccupancy"]
        grid = item_catalog._box(io["CargoGrid"])
        assert not item_catalog._contains(grid, item_catalog._box(io["Dimensions"]))
        assert item_catalog._contains(grid, item_catalog._box(e["stdItem"]))


def test_a_junk_dimensions_box_does_not_beat_the_cargo_grid():
    cat = {d["key"]: d for d in item_catalog.build_catalog(BESPOKE)}
    serac = cat["COOL_RSI_S04_Polaris_SCItem"]
    assert serac["dims"] == (1, 2, 6) and serac["source"] == "grid"
    beam = cat["HRST_LaserBeam_Bespoke"]
    assert beam["dims"] == (4, 4, 25) and beam["source"] == "grid"


def test_a_junk_cargo_grid_still_loses_to_dimensions():
    """The Argo and Enhanced ore pods and the GEO pod carry 0.75^3 in the
    CargoGrid AND in their own stdItem size, and J verified the pod is 8 SCU =
    2x2x2. So the own-box reference must not rescue a junk grid: when both are
    junk the answer is still Dimensions."""
    raw = [_raw("Cargo_ShipMining_Pod_Mole", "Argo Ore Pod", "Container.Cargo",
                own=(0.75, 0.75, 0.75), grid=(0.75, 0.75, 0.75),
                dims=(2.5, 2.5, 2.5), scu=8, mfr="ARGO")]
    (d,) = item_catalog.build_catalog(raw)
    assert d["dims"] == (2, 2, 2) and d["source"] == "dims"


def test_without_an_own_box_dimensions_is_still_the_reference():
    """Coverage for the branch the two fixtures above used to be the only users
    of. With no stdItem Width/Height/Length there is nothing better to check the
    CargoGrid against, so Dimensions stands in and a grid too small to hold it
    still loses -- the pre-2026-09-27 behaviour, kept for a record shaped that
    way. Every entry in the live file carries an own box, so this is the
    fallback, not the path."""
    raw = [_raw("NO_OWN_BOX", "Stub Bomb", "Bomb.Utility", size=10,
                grid=(1, 1, 10), dims=(2.3, 2.3, 6.75), scu=10, mfr="FSKI")]
    (d,) = item_catalog.build_catalog(raw)
    assert d["dims"] == (2, 2, 6) and d["source"] == "dims"


def test_real_datamine_sizes_every_item_by_its_real_cargo_grid():
    """The property, over the live file rather than a fixture: whenever an item
    has a CargoGrid that is not the junk 0.75^3 cube, that grid is what the
    item is measured by."""
    path = item_catalog.default_path()
    if not path or not os.path.isfile(path):
        pytest.skip("ship-items.json not downloaded on this machine")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    by_key = {}
    for e in raw:
        if isinstance(e, dict) and e.get("className"):
            by_key.setdefault(e["className"], e)
    cat = item_catalog.load_catalog(path)
    off = []
    for d in cat:
        io = ((by_key.get(d["key"]) or {}).get("stdItem") or {}).get("InventoryOccupancy") or {}
        grid = item_catalog._box(io.get("CargoGrid"))
        if grid is None or item_catalog._is_junk(grid):
            continue
        want = tuple(item_catalog.cells(v) for v in grid)
        if d["dims"] != want or d["source"] != "grid":
            off.append((d["name"], d["dims"], want, d["source"]))
    assert not off, f"{len(off)} item(s) not sized by their real CargoGrid: {off[:6]}"

    # The five that the Dimensions reference inflated past every bay in the game.
    got = {d["key"]: d["dims"] for d in cat}
    assert got["COOL_RSI_S04_Polaris_SCItem"] == (1, 2, 6)    # was (15, 6, 12)
    assert got["SHLD_RSI_S04_Polaris_SCItem"] == (1, 2, 6)    # was (5, 7, 12)
    assert got["HRST_LaserBeam_Bespoke"] == (4, 4, 25)        # was (31, 12, 86)
    assert got["KLWE_MassDriver_S10"] == (4, 4, 25)           # was (31, 12, 80)
    assert got["BEHR_LaserCannon_S9"] == (4, 4, 25)           # was (5, 5, 25)
    # 25 cells is the longest bay in the corpus (Idris-P). Nothing may exceed it
    # on any axis: an item that fits no grid anywhere cannot be placed at all.
    assert max(max(d["dims"]) for d in cat) <= 25


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
    assert win._items_summary_lbl.toolTip() == "Items: 1"
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


def test_overlapping_item_is_refused_and_names_the_box(win):
    """2026-10-03, J reversed the 09-26 rule: items are solid. This test used
    to be test_overlapping_item_still_places_amber and asserted the opposite."""
    win._renderer._manual_boxes = [(2, 0, 0, 2, 2, 2, 8)]
    win._sync_counts_from_boxes()
    win._render_grid()
    _arm(win, LONG)
    p = _vp(win, 2.5, 0, 3)          # a 1x1x4 gun whose far end runs into the 8 SCU box
    _send(win._view, QEvent.MouseMove, p, Qt.NoButton, Qt.NoButton)
    ghost = win._renderer._ghost
    assert not ghost.ghost_valid                        # red, not amber
    assert "overlaps the 8 SCU container" in win._status_lbl.text()
    _click(win, p)
    assert win._renderer._items == []
    assert "no room: overlaps the 8 SCU container" in win._status_lbl.text()
    assert "room elsewhere" in win._status_lbl.text()
    assert _counts(win) == {8: 1}                       # the container is untouched
    assert not win._move_undo                           # a refusal is not an edit


def test_an_overlap_already_in_the_hold_is_flagged_amber(win):
    """The amber flag is kept as a last line of defence for an overlap that is
    already in the hold state. No user path produces one any more (Load Plan
    refuses phasing items since 2026-10-03, see the old-plan tests below), so
    this sets the state directly to keep the flag/pen rendering covered."""
    win._renderer._manual_boxes = [(2, 0, 0, 2, 2, 2, 8)]
    win._sync_counts_from_boxes()
    item = (2, 0, 1, 1, 1, 4, LONG)
    win._renderer._items = [item]
    win._render_grid()
    assert win._renderer._items == [item]
    assert win._renderer._item_flags[item] == [W_OVERLAP]
    g = _group(win, item)
    assert g.warnings == [W_OVERLAP]
    assert g.item_pen.color().name() == "#ffb000"
    assert "flagged" in win._items_summary_lbl.toolTip()


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


def test_items_do_not_touch_counts_or_optimize(win, monkeypatch):
    """Optimize leaves the item where it is, and (2026-10-03) packs the
    containers AROUND it: before, this test passed with the packer filling
    the pod's cell with containers."""
    monkeypatch.setattr(win, "_ask_reorganise", lambda text: True)
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)]
    win._render_grid()
    win._optimize()
    assert win._renderer._items == [(0, 0, 0, 2, 2, 2, POD)]
    _group(win, (0, 0, 0, 2, 2, 2, POD))                 # still drawn
    assert win._renderer._last_boxes                     # the packer filled the grid
    assert all(not isinstance(b[6], str) for b in win._renderer._last_boxes)
    placed = sum(b[6] for b in win._renderer._last_boxes)
    assert sum(s * n for s, n in _counts(win).items()) == placed
    assert placed == 96 - 8                              # all of the hold but the pod
    assert _no_two_overlap(win._renderer._last_boxes + win._renderer._items)
    assert win._items_summary_lbl.toolTip().startswith("Items: 1")


def test_brush_paints_an_item(win):
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)]
    win._render_grid()
    win._on_commodity_selected("Mission Cargo 1")
    g = _group(win, (0, 0, 0, 2, 2, 2, POD))
    win._on_box_clicked(g)
    assert win._renderer._assignments[(0, 0, 0, POD)] == "Mission Cargo 1"


# ── items stay inside the confines (J, 2026-09-26) ───────────────────────────
#
# "make sure the ship items stay within the maximum confines of the optimal
# layout." The confines are the slot volumes build_slots() derives from the
# grid data; the optimal layout (greedy_optimize_3d -> assign_slots_from_counts
# -> place_containers_3d) is slot-bounded by construction because the packer
# only ever iterates range(sw - cw + 1), so a CONTAINER could never leave.
# snap_item had no equivalent line, and the pointer that feeds it is unbounded.
#
# The tests in this block are about snap_item, the BOUND: where an item lands.
# ⛔ 2026-10-03 J made items solid, so whether it may STAY there is now
# place_item's job (overlap / outside / too tall refuse; floating still warns).
# snap_item keeps reporting raw warnings, which is why the bound tests below
# still read W_OUTSIDE / W_TOO_TALL; where a docstring used to say "stays a
# warning", the test now also asserts place_item refuses. See the "items are
# solid" block at the end of this file.

BOUND_GRIDS = [{"x": 0, "y0": 0, "z": 0, "w": 6, "h": 2, "l": 8}]


def _footprint_inside(pos, dims, grids=BOUND_GRIDS) -> bool:
    """True when the box's whole floor footprint lies inside a single grid."""
    x, _y, z = pos[0], pos[1], pos[2]
    w, _h, l = dims[0], dims[1], dims[2]
    return any((g.get("x") or 0) <= x and x + w <= (g.get("x") or 0) + g["w"]
               and (g.get("z") or 0) <= z and z + l <= (g.get("z") or 0) + g["l"]
               for g in grids)


def test_footprint_predicate_can_return_false():
    """Every bound assertion below is worthless if this predicate is stuck on
    True — an inverted comparison would report "all inside" forever. So: one
    box in, three out, including the rotated footprint the clamp exists for."""
    assert _footprint_inside((0, 0, 0), (2, 1, 2))
    assert not _footprint_inside((20, 0, 20), (1, 1, 1))
    assert not _footprint_inside((3, 0, 0), (4, 1, 1))     # yawed 1x1x4 -> x 3..7
    assert not _footprint_inside((-1, 0, 0), (1, 1, 1))


@pytest.mark.parametrize("dims,target,unclamped", [
    ((1, 1, 1), (20.0, 20.0), (20, 0, 20)),      # dropped off the ship entirely
    ((4, 1, 1), (3.0, 0.0), (3, 0, 0)),          # yawed 1x1x4: footprint ran to x=7
    ((1, 1, 1), (0.0, 8.0), (0, 0, 8)),          # one cell past +Z
    ((1, 1, 1), (-3.0, -3.0), (-3, 0, -3)),      # negative corner
    ((2, 2, 2), (5.0, 7.0), (5, 0, 7)),          # a pod half off the +X/+Z corner
])
def test_item_is_bounded_to_the_grids(dims, target, unclamped):
    ctx = PlacementContext(BOUND_GRIDS, [])
    pos, warns = ctx.snap_item((dims[0], dims[1], dims[2], LONG), target)
    # The fixture is only a test while the old answer was genuinely outside.
    assert not _footprint_inside(unclamped, dims)
    assert _footprint_inside(pos, dims), f"{pos} {dims} escaped {BOUND_GRIDS}"
    assert W_OUTSIDE not in warns


def test_oversized_item_pins_to_the_grid_and_still_warns():
    """An item larger than the bay cannot be contained at all — items are sized
    roughly, so this is a real case, not a bad input. It is pinned to the grid
    instead of following the mouse into empty space, and the columns that hang
    off the end are still reported. That report is the proof the warning path
    did not go quiet when the clamp went in."""
    small = [{"x": 0, "y0": 0, "z": 0, "w": 2, "h": 2, "l": 2}]
    ctx = PlacementContext(small, [])
    pos, warns = ctx.snap_item((4, 1, 1, LONG), (9.0, 9.0))
    assert pos[0] == 0                                    # pinned, overflow on +X
    assert 0 <= pos[2] and pos[2] + 1 <= 2                # the axis that fits, inside
    assert warns == [W_OUTSIDE]
    # 2026-10-03: and since items are solid, it may not be placed there.
    pos2, valid, reason = ctx.place_item((4, 1, 1, LONG), (9.0, 9.0))
    assert pos2 == pos and not valid and reason == R_ITEM_OUTSIDE


def test_no_grid_data_is_not_a_zero_sized_grid():
    """With no grids to measure against there is no bound to apply. The item
    keeps the position it was given and is flagged; it is NOT collapsed onto
    the origin and called in-bounds, which is what clamping an absent grid
    list would do."""
    ctx = PlacementContext([], [])
    pos, warns = ctx.snap_item((1, 1, 1, LONG), (7.0, 9.0))
    assert pos == (7, 0, 9)
    assert warns == [W_OUTSIDE]


# A small side bay and a roomy one far away: the distance is the point, so
# that "relocate to where it fits" and "keep the aim" give visibly different
# answers and a test can tell which rule ran.
FAR_GRIDS = [
    {"x": 0, "y0": 0, "z": 0, "w": 2, "h": 2, "l": 2},
    {"x": 40, "y0": 0, "z": 0, "w": 8, "h": 2, "l": 8},
]


def test_flung_item_lands_in_a_bay_that_can_hold_it():
    """Thrown clear of every grid there is no aim to respect, so a 1x1x4 gun
    goes to the bay that can hold it whole — 40 cells away — rather than being
    pinned in the near 2x2x2 one to overflow and flag."""
    ctx = PlacementContext(FAR_GRIDS, [])
    pos, warns = ctx.snap_item((1, 1, 4, LONG), (-9.0, -9.0))
    assert _footprint_inside(pos, (1, 1, 4), FAR_GRIDS)
    assert warns == []
    # an item the near bay CAN hold still goes to the near bay
    pos1, warns1 = ctx.snap_item((1, 1, 1, LONG), (-9.0, -9.0))
    assert pos1 == (0, 0, 0) and warns1 == []


def test_aiming_at_a_bay_too_small_does_not_teleport():
    """A click is a statement about WHERE. Aimed into the small bay, a 1x1x4
    gun stays in it — pinned, overflowing, amber — instead of reappearing 40
    cells away in the only bay that fits. Over the real corpus the unbounded
    version of that move ran to 86 cells, the length of the Idris-P."""
    ctx = PlacementContext(FAR_GRIDS, [])
    pos, warns = ctx.snap_item((1, 1, 4, LONG), (0.0, 0.0))
    assert pos == (0, 0, 0)
    assert warns == [W_OUTSIDE]


def test_item_across_the_seam_between_two_grids_is_left_alone():
    """The bound is the UNION of the grid floors, not one grid. An item lying
    across two abutting grids is clean by item_warnings' own per-column test,
    and a per-grid clamp dragged it off the seam into one side: 4,056 of
    68,935 already-clean placements across all 144 ships and 33 layouts, some
    of them moved AND then flagged. Containers are different — they must live
    in one grid — which is why copying snap()'s clamp was wrong here."""
    seam = [{"x": 0, "y0": 0, "z": 0, "w": 2, "h": 2, "l": 2},
            {"x": 2, "y0": 0, "z": 0, "w": 2, "h": 2, "l": 2}]
    ctx = PlacementContext(seam, [])
    assert not _footprint_inside((1, 0, 0), (2, 1, 1), seam)   # in no SINGLE grid
    pos, warns = ctx.snap_item((2, 1, 1, LONG), (1.0, 0.0))
    assert pos == (1, 0, 0) and warns == []


def test_half_cell_grid_origin_bounds_to_the_cells_it_covers():
    """8 of the 1,069 placements in the hand-made layouts sit on half cells
    (Idris_M/Idris_P, x = 29.5). The cells such a grid covers are 30 and 31,
    so the bound must not offer 29 — which int(29.5) does, and which the grid
    does not reach."""
    half = [{"x": 29.5, "y0": 0, "z": 0, "w": 2, "h": 1, "l": 1}]
    ctx = PlacementContext(half, [])
    assert sorted(ctx._floor()) == [(30, 0), (31, 0)]
    for aim in (25.0, 29.0, 33.0):
        pos, warns = ctx.snap_item((1, 1, 1, LONG), (aim, 0.0))
        assert pos[0] in (30, 31), f"aim {aim} -> {pos}"
        assert warns == []


def test_item_above_the_ceiling_is_not_clamped_but_is_refused():
    """The bound still never clamps Y. Y comes from the stack underneath or the
    box the player clicked, never from the mouse; clamping it would drop items
    into the box they were stacked on. (Was ..._stays_a_warning.) Since
    2026-10-03 the overshoot is a refusal: an item may not poke through the
    ceiling."""
    ctx = PlacementContext(BOUND_GRIDS, [])
    pos, warns = ctx.snap_item((1, 1, 1, LONG), (0.0, 0.0), y=9)
    assert pos == (0, 9, 0)
    assert W_TOO_TALL in warns
    pos2, valid, reason = ctx.place_item((1, 1, 1, LONG), (0.0, 0.0), y=9)
    assert pos2 == (0, 9, 0) and not valid and reason == R_ITEM_TOO_TALL


def test_click_off_the_bay_lands_the_item_on_it(win):
    """The click path (_place_target): with the pointer outside every grid it
    falls back to unprojecting on the y=0 plane, which is the whole floor
    plane, not the bay."""
    _arm(win, POD)
    _click(win, _vp(win, 9.0, 0, 11.0))
    (item,) = win._renderer._items
    assert _footprint_inside(item[:3], item[3:6], win._grids_world())


def test_dragging_an_item_off_the_bay_keeps_it_on(win):
    """The drag path (_drag_update): the mouse delta is unbounded."""
    start = (2, 0, 3, 2, 2, 2, POD)
    win._renderer._items = [start]
    win._render_grid()
    view, L, NB = win._view, Qt.LeftButton, Qt.NoButton
    grab = _vp(win, 3, 2, 4)                       # top-face centre of the pod
    _send(view, QEvent.MouseButtonPress, grab, L, L)
    _send(view, QEvent.MouseMove, grab + QPoint(3, 3), NB, L)
    _send(view, QEvent.MouseMove, _vp(win, 11.0, 2, 13.0), NB, L)
    QtWidgets.QApplication.processEvents()
    assert win._drag is not None and win._drag["active"]
    pos = win._drag["result"][0]
    assert _footprint_inside(pos, (2, 2, 2), win._grids_world())
    _send(view, QEvent.MouseButtonRelease, _vp(win, 11.0, 2, 13.0), L, NB)
    QTest.qWait(30)
    QtWidgets.QApplication.processEvents()
    (item,) = win._renderer._items
    assert _footprint_inside(item[:3], item[3:6], win._grids_world())


# ── items are solid (J, 2026-10-03) ──────────────────────────────────────────
#
# "make sure that the ship items category honor the actual cargo grid and
# don't stack through existing items or exceed the maximum size of the cargo
# grid. Like we don't need 86 ore pods inside a ship that can only fit 3 and if
# someone puts a 32 scu crate in their the ship items should treat that like a
# physical honest and not phase through it"
#
# This reverses the 2026-09-26 rule for items (overlap / outside / too tall
# were amber warnings). Floating / overhang is still a warning.

from cargo_engine import crate_items  # noqa: E402

SOLID_GRIDS = [{"x": 0, "y0": 0, "z": 0, "w": 6, "h": 2, "l": 8}]
C32 = (0, 0, 0, 2, 2, 8, 32)          # a 32 SCU container yawed along Z: x 0..2
CRATE4 = crate_items.CRATE_CLASSES[3]  # the 4 SCU Stor*All


def _solid(placed, grids=SOLID_GRIDS):
    # No flush magnet: these engine tests aim at exact cells.
    return PlacementContext(grids, placed, flush_threshold=0.0)


def _no_two_overlap(boxes):
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if (a[0] < b[0] + b[3] and b[0] < a[0] + a[3]
                    and a[1] < b[1] + b[4] and b[1] < a[1] + a[4]
                    and a[2] < b[2] + b[5] and b[2] < a[2] + a[5]):
                return False
    return True


def test_item_may_not_phase_through_a_32_scu_container():
    ctx = _solid([C32])
    # A 3x1x1 rests on the floor (its middle cell, x=2, is clear of the
    # container) and its first cell, x=1, is inside the container.
    pos, valid, reason = ctx.place_item((3, 1, 1, LONG), (1.0, 0.0))
    assert pos == (1, 0, 0)
    assert not valid
    assert reason == R_ITEM_OVERLAP.format(what="the 32 SCU container")
    # One cell over, flush against it, is fine.
    pos, valid, reason = ctx.place_item((3, 1, 1, LONG), (2.0, 0.0))
    assert pos == (2, 0, 0) and valid and reason == OK


def test_item_may_not_stack_through_another_item():
    pod = (2, 0, 3, 2, 2, 2, POD)
    ctx = _solid([pod])
    # A 1x1x4 gun along Z: its middle (z 1, 2) is on the floor, its far end
    # (z 3) runs into the pod.
    pos, valid, reason = ctx.place_item((1, 1, 4, LONG), (2.0, 0.0),
                                        name_of=lambda b: "the MISC Ore Pod")
    assert pos == (2, 0, 0)
    assert not valid and reason == R_ITEM_OVERLAP.format(what="the MISC Ore Pod")
    # and the default name, with no caller-supplied namer
    assert (ctx.place_item((1, 1, 4, LONG), (2.0, 0.0))[2]
            == R_ITEM_OVERLAP.format(what="another item"))


def test_item_may_not_poke_through_the_ceiling_when_stacked():
    pod = (0, 0, 0, 2, 2, 2, POD)
    ctx = _solid([pod])                          # the hold is 2 cells tall
    pos, valid, reason = ctx.place_item((2, 2, 2, POD), (0.0, 0.0))
    assert pos == (0, 2, 0)                      # rests on the pod...
    assert not valid and reason == R_ITEM_TOO_TALL   # ...and would stick out
    # In a 4-tall hold the same stack is honest and allowed.
    tall = [dict(SOLID_GRIDS[0], h=4)]
    pos, valid, reason = _solid([pod], tall).place_item((2, 2, 2, POD), (0.0, 0.0))
    assert pos == (0, 2, 0) and valid and reason == OK


def test_86_ore_pods_in_a_hold_that_fits_3():
    """The literal case J named. A 6x2x2 hold takes exactly three 2x2x2 pods;
    every further aim, on or off the grid, is refused, and the hold reports
    that nothing of that size fits anywhere."""
    hold = [{"x": 0, "y0": 0, "z": 0, "w": 6, "h": 2, "l": 2}]
    placed = []
    for aim in ((0.0, 0.0), (2.0, 0.0), (4.0, 0.0)):
        ctx = PlacementContext(hold, placed)
        assert ctx.item_fits_anywhere((2, 2, 2))
        pos, valid, reason = ctx.place_item((2, 2, 2, POD), aim)
        assert valid, (aim, reason)
        placed.append((*pos, 2, 2, 2, POD))
    attempts = [((i * 7) % 17 * 0.5 - 2.0, (i * 3) % 7 * 0.5 - 1.0) for i in range(86)]
    for aim in attempts:
        ctx = PlacementContext(hold, placed)
        pos, valid, reason = ctx.place_item((2, 2, 2, POD), aim)
        if valid:
            placed.append((*pos, 2, 2, 2, POD))
    assert len(placed) == 3
    assert _no_two_overlap(placed)
    ctx = PlacementContext(hold, placed)
    assert not ctx.item_fits_anywhere((2, 2, 2))
    assert not ctx.item_fits_anywhere((1, 1, 1))     # 24 of 24 cells used


def test_floating_is_still_only_a_warning():
    """J asked for solidity and bounds, not a support model: a gun resting on a
    pod with its far end overhanging places, amber."""
    tall = [dict(SOLID_GRIDS[0], h=4)]
    pod = (0, 0, 0, 2, 2, 2, POD)
    pos, valid, reason = _solid([pod], tall).place_item((1, 1, 4, LONG), (1.0, 0.0))
    assert pos == (1, 2, 0)
    assert valid and reason == W_FLOATING


def test_fits_anywhere_considers_the_yawed_footprint():
    strip = [{"x": 0, "y0": 0, "z": 0, "w": 4, "h": 1, "l": 1}]
    ctx = PlacementContext(strip, [])
    assert not ctx.place_item((1, 1, 4, LONG), (0.0, 0.0))[1]   # as aimed: no
    assert ctx.item_fits_anywhere((1, 1, 4))                     # rotated: yes
    blocked = PlacementContext(strip, [(0, 0, 0, 1, 1, 1, POD)])
    assert not blocked.item_fits_anywhere((1, 1, 4))


def test_clicking_86_times_places_only_what_physically_fits(win):
    """Through the real click path. The 6x2x8 test hold takes twelve 2x2x2
    pods; 86 clicks all over it, including on top of pods, never put more
    than twelve in, never interpenetrate, and the refusal says the hold is
    full rather than silently doing nothing."""
    _arm(win, POD)
    pts = [(x + 0.5, z + 0.5) for x in range(6) for z in range(8)]
    for i in range(86):
        x, z = pts[(i * 5) % len(pts)]
        _click(win, _vp(win, x, 0, z))
    items = win._renderer._items
    # Scattered clicks fragment the floor (9 fit, measured), never exceed 12.
    assert 0 < len(items) <= 12
    assert _no_two_overlap(items)
    ctx = PlacementContext(win._grids_world(), [])
    assert all(ctx.item_blockers(b[:3], b[3:6]) == [] for b in items)
    # Now pack it properly at the twelve pod centres, then click 86 more times.
    win._clear_items()
    for x in (1, 3, 5):
        for z in (1, 3, 5, 7):
            _click(win, _vp(win, x, 0, z))
    items = win._renderer._items
    assert len(items) == 12
    assert _no_two_overlap(items)
    for i in range(86):
        x, z = pts[(i * 5) % len(pts)]
        _click(win, _vp(win, x, 2, z))              # on top of a pod each time
    assert len(win._renderer._items) == 12
    assert len(win._renderer._items) == 12
    status = win._status_lbl.text()
    assert "hold is full" in status and "MISC Ore Pod" in status and "(12 placed)" in status


def test_item_click_into_a_32_scu_container_is_refused(win):
    win._renderer._manual_boxes = [C32]
    win._sync_counts_from_boxes()
    win._render_grid()
    _arm(win, LONG)
    QTest.keyClick(win._view, Qt.Key_R)              # 1x1x4 -> 4x1x1, across X
    p = _vp(win, 3.0, 0, 4.5)                         # footprint x 1..5: x=1 is inside
    t = win._place_target(win._view.mapToScene(p))
    assert t is not None
    pos, dims, valid, reason = t
    assert dims == (4, 1, 1) and pos == (1, 0, 4)
    assert not valid and reason == R_ITEM_OVERLAP.format(what="the 32 SCU container")
    _click(win, p)
    assert win._renderer._items == []
    assert "overlaps the 32 SCU container" in win._status_lbl.text()
    assert win._renderer._manual_boxes == [C32]       # the container did not move


def test_dragging_an_item_into_a_box_is_refused_and_it_stays(win):
    win._renderer._manual_boxes = [(0, 0, 0, 2, 2, 2, 8)]
    win._sync_counts_from_boxes()
    start = (3, 0, 5, 2, 2, 2, POD)
    win._renderer._items = [start]
    win._render_grid()
    view, L, NB = win._view, Qt.LeftButton, Qt.NoButton
    grab = _vp(win, 4, 2, 6)
    _send(view, QEvent.MouseButtonPress, grab, L, L)
    _send(view, QEvent.MouseMove, grab + QPoint(3, 3), NB, L)
    tgt = _vp(win, 1.0, 2, 1.0)                       # straight onto the 8 SCU box
    _send(view, QEvent.MouseMove, tgt, NB, L)
    QtWidgets.QApplication.processEvents()
    pos, valid, reason = win._drag["result"]
    assert not valid and reason.startswith("no room")
    assert not win._renderer._ghost.ghost_valid
    _send(view, QEvent.MouseButtonRelease, tgt, L, NB)
    QTest.qWait(30)
    QtWidgets.QApplication.processEvents()
    assert win._renderer._items == [start]
    assert win._status_lbl.text().startswith("Move cancelled: no room")


def test_personal_crate_and_item_block_each_other(win):
    win._brush_tabs.setCurrentIndex(1)
    win._crate_btns[CRATE4].click()
    _click(win, _vp(win, 1, 0, 1))
    (crate,) = win._renderer._items
    assert crate[3:6] == (2, 1, 2)
    assert win._obstacle_name(crate) == "Crate 1 (4 SCU)"
    # the crate refuses an item laid through it, and names itself
    ctx = PlacementContext(win._grids_world(), list(win._renderer._items),
                           flush_threshold=0.0)
    pos, valid, reason = ctx.place_item((4, 1, 1, LONG),
                                        (float(crate[0] + 1), float(crate[2])),
                                        name_of=win._obstacle_name)
    assert not valid and reason == R_ITEM_OVERLAP.format(what="Crate 1 (4 SCU)")
    # an ore pod on top of the 1-tall crate would poke through the 2-tall hold
    _arm(win, POD)
    _click(win, _vp(win, crate[0] + 1, 1, crate[2] + 1))
    assert len(win._renderer._items) == 1
    assert "Can't place" in win._status_lbl.text()
    # a second crate stacks on it honestly (1 + 1 = 2), and takes number 2:
    # the refused attempts above did not use a crate number up
    win._brush_tabs.setCurrentIndex(1)
    win._crate_btns[CRATE4].click()
    _click(win, _vp(win, crate[0] + 1, 1, crate[2] + 1))
    assert len(win._renderer._items) == 2
    assert win._renderer._items[-1][6] == CRATE4 + "#2"
    assert win._renderer._items[-1][1] == 1


# ── objects are objects: Optimize / Auto ask first, and pack around items ────
#
# J, 2026-10-03: "should treat objects as objects. If there are manually placed
# objects and the user hits optimize it should pop up a warning asking 'are you
# sure' before reorganizing existing objects and then adding additional
# objects". conftest.py makes any prompt a test did not answer FAIL, so the
# tests that do not expect one prove it does not appear.


def _asker(answer):
    asked = []

    def ask(text):
        asked.append(text)
        return answer
    return ask, asked


def _hand_box(win, size=16):
    win._place_btns[size].click()
    _click(win, _vp(win, 3, 0, 4))
    win._place_btns[size].click()                  # toggle placing off
    assert len(win._renderer._manual_boxes) == 1
    return list(win._renderer._manual_boxes)


def test_optimize_on_an_empty_hold_does_not_ask(win):
    win._optimize()                                # a prompt would fail here
    assert win._renderer._last_boxes


def test_optimize_asks_and_cancel_moves_nothing(win, monkeypatch):
    before = _hand_box(win)
    ask, asked = _asker(False)
    monkeypatch.setattr(win, "_ask_reorganise", ask)
    win._optimize()
    assert len(asked) == 1
    assert "1 container(s) you placed will be rearranged" in asked[0]
    assert win._renderer._manual_boxes == before
    assert _counts(win) == {16: 1}
    assert "cancelled" in win._status_lbl.text()


def test_optimize_asks_then_reorganises_and_undo_brings_it_back(win, monkeypatch):
    before = _hand_box(win)
    ask, asked = _asker(True)
    monkeypatch.setattr(win, "_ask_reorganise", ask)
    win._optimize()
    assert len(asked) == 1 and "Ctrl+Z" in asked[0]
    assert sum(b[6] for b in win._renderer._last_boxes) == 96
    win._undo_move()
    assert win._renderer._manual_boxes == before


def test_optimize_with_an_item_names_it_in_the_question(win, monkeypatch):
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)]
    win._render_grid()
    ask, asked = _asker(False)
    monkeypatch.setattr(win, "_ask_reorganise", ask)
    win._optimize()
    assert "1 item(s)/crate(s) stay where they are" in asked[0]
    assert win._renderer._last_boxes == []         # cancelled: nothing packed


def test_switching_to_auto_asks_and_cancel_stays_manual(win, monkeypatch):
    before = _hand_box(win)
    ask, asked = _asker(False)
    monkeypatch.setattr(win, "_ask_reorganise", ask)
    win._mode_btns["auto"].click()
    assert len(asked) == 1
    assert win._mode == "manual" and win._mode_btns["manual"].isChecked()
    assert win._renderer._manual_boxes == before


def test_typing_a_count_asks_and_cancel_puts_it_back(win, monkeypatch):
    before = _hand_box(win)
    ask, asked = _asker(False)
    monkeypatch.setattr(win, "_ask_reorganise", ask)
    win._spinboxes[8].setValue(3)
    assert len(asked) == 1
    assert win._spinboxes[8].value() == 0 and win._spinboxes[16].value() == 1
    assert win._mode == "manual"
    assert win._renderer._manual_boxes == before


def test_auto_counts_that_do_not_fit_around_an_item_are_reported(win, monkeypatch):
    monkeypatch.setattr(win, "_ask_reorganise", lambda text: True)
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)]
    win._set_mode("auto")
    win._spinboxes[32].setValue(3)                 # 3 x 32 = the whole empty hold
    drawn = win._renderer._last_boxes
    assert len(drawn) == 2                         # one has nowhere to go...
    assert _no_two_overlap(list(drawn) + win._renderer._items)
    assert "1 container(s), 32 SCU, do not fit" in win._status_lbl.text()   # ...and says so


def test_a_ship_layout_is_not_drawn_through_an_item(win):
    win._current_ship["arrangement"] = [
        {"pos": [0, 0, 0], "dims": [2, 2, 2], "scu": 8},
        {"pos": [2, 0, 0], "dims": [2, 2, 2], "scu": 8},
    ]
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)]
    assert win._apply_arrangement()
    assert win._renderer._manual_boxes == [(2, 0, 0, 2, 2, 2, 8)]
    assert "1 container(s) of the ship's layout left out" in win._status_lbl.text()


# ── old plans: phasing items are refused on load, and listed ─────────────────
#
# J, 2026-10-03: "Refuse item phasing". A plan saved before items were solid
# can carry items inside containers or each other; they are not loaded, and a
# notice says exactly which and why, so nothing disappears silently.

def _reload(win, payload):
    win._pending_loadout = json.loads(json.dumps(payload))
    win._load_ship(payload["ship"])


def test_old_plan_refuses_phasing_items_and_lists_them(win):
    box8 = (0, 0, 0, 2, 2, 2, 8)
    win._renderer._manual_boxes = [box8]
    win._sync_counts_from_boxes()
    good = (2, 0, 0, 2, 2, 2, POD)
    floating = (0, 1, 4, 1, 1, 4, LONG)            # overhangs: still allowed
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD)] * 3 + [good, floating,
                                                             (9, 0, 0, 1, 1, 1, LONG)]
    payload = win._loadout_payload()
    _reload(win, payload)
    assert win._renderer._manual_boxes == [box8]               # containers as saved
    assert sorted(win._renderer._items) == sorted([good, floating])
    assert win._renderer._item_flags[floating] == [W_FLOATING]  # amber, not refused
    status = win._status_lbl.text()
    assert "4 items not loaded" in status
    assert "MISC Ore Pod x3 - no room: overlaps the 8 SCU container" in status
    assert "Long Gun - no room: sticks out of the cargo grid" in status
    ((title, text),) = win._notices
    assert "MISC Ore Pod x3" in text and "Long Gun" in text


def test_two_saved_items_in_one_place_keep_the_first(win):
    a = (0, 0, 0, 2, 2, 2, POD)
    b = (1, 0, 1, 2, 2, 2, POD)
    win._renderer._items = [a, b]
    _reload(win, win._loadout_payload())
    assert win._renderer._items == [a]
    assert "1 item not loaded: MISC Ore Pod - no room: overlaps the MISC Ore Pod" \
        in win._status_lbl.text()


def test_a_clean_plan_loads_without_a_notice(win):
    win._renderer._items = [(0, 0, 0, 2, 2, 2, POD), (2, 0, 0, 2, 2, 2, POD)]
    _reload(win, win._loadout_payload())
    assert len(win._renderer._items) == 2
    assert not getattr(win, "_notices", [])


def test_a_refused_crate_is_named_with_its_contents_and_gets_no_tab(win):
    win._brush_tabs.setCurrentIndex(1)
    win._crate_btns[CRATE4].click()
    _click(win, _vp(win, 1, 0, 1))
    (crate,) = win._renderer._items
    no = int(crate[6].rsplit("#", 1)[1])
    win._crates[no]["contents"] = [{"key": "k", "name": "Medpen", "qty": 2,
                                    "vol_u": 1000, "kind": "", "uuid": ""}]
    win._crate_btns[CRATE4].click()
    payload = win._loadout_payload()
    # The old plan: an 8 SCU container saved in the very cells the crate is in.
    payload["counts"]["8"] = 1
    payload["boxes"] = [{"scu": 8, "pos": [crate[0], 0, crate[2]], "dims": [2, 2, 2]}]
    _reload(win, payload)
    assert win._renderer._items == []
    assert "Crate 1 (4 SCU) (holding 1 item(s)) - no room: overlaps the 8 SCU container" \
        in win._status_lbl.text()
    assert no not in win._live_crates()
    assert no not in win._crates          # not left registered as a dead crate
