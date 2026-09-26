"""Personal crates for the Cargo Loader (J, 2026-09-26).

"add personal boxes and then have them each have a number on it and they will
create a tab at the top of the page that you can swap to or pop out which
links to UEX and allows users to fuzzy search and assign any item ingame that
will fit in the crate ... you can't shove a Kraken engine into a handheld
crate".

Runs offscreen on small fixture lists written to a temp dir. Nothing is
written to ~/.sctoolbox, to the Item Finder's UEX cache or to saved plans,
and nothing is downloaded. One test reads the real items.json only when it is
already in the scunpacked cache on this machine.
"""

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt, QPoint, QPointF, QEvent  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

try:                                   # absent before personal crates existed
    from cargo_engine import crate_items  # noqa: E402
except ImportError:
    crate_items = None

SHIP = {
    "name": "Crates Test Ship", "manufacturer": "Test", "capacity": 96,
    "provenance": {"source": "scunpacked-data"},
    "groups": [{"x": 0, "z": 0, "grids": [
        {"x": 0, "z": 0, "width": 6, "height": 2, "length": 8,
         "minSize": 1, "maxSize": 32},
    ]}],
}

C8, C4, C2, C1 = ("Carryable_TBO_InventoryContainer_8SCU", "Carryable_TBO_InventoryContainer_4SCU",
                  "Carryable_TBO_InventoryContainer_2SCU", "Carryable_TBO_InventoryContainer_1SCU")
EIGHTH = "Carryable_2H_FL_05x05x05_DestroyedInventory_BoxExtInventory"
MU = "µSCU"

PANTS_UUID = "d6415769-ff23-430c-a326-d40c6d62ca59"


def _e(cls, name, typ, vol=None, unit="SCU", conv=None, ref="", size=0, sub="", **std):
    io = {}
    if vol is not None:
        io["Volume"] = {"SCU": vol, "SCUConverted": vol if conv is None else conv, "Unit": unit}
    std.setdefault("InventoryOccupancy", {}).update(io)
    return {"className": cls, "reference": ref, "type": typ, "subType": sub, "size": size,
            "name": name, "stdItem": dict(std, Name=name, ClassName=cls)}


def _crate(cls, name, scu, grid, unit="SCU"):
    w, h, l = grid
    return _e(cls, name, "Container", vol=scu, InventoryContainer={"SCU": scu, "UnitName": unit},
              InventoryOccupancy={"CargoGrid": {"Width": w, "Height": h, "Length": l}})


# Shapes and figures copied from scunpacked 4.10.1-LIVE.12660092.
FPS = [
    _e("clothing_pants_emilion", "Emilion Pants", "Char_Clothing_Legs", 0.0073, MU, 7300,
       ref=PANTS_UUID),
    _e("behr_rifle_ballistic_01", "P4-AR Rifle", "WeaponPersonal", 0.013, MU, 13000, sub="Medium"),
    _e("crlf_consumable_healing_01", "MedPen (Hemozal)", "FPS_Consumable", 0.0002, MU, 180),
    _e("test_Gadget", "<= PLACEHOLDER =>", "WeaponPersonal", 0.0, MU, 1),
]
SHIP_ITEMS = [
    _e("POWR_RSI_S04_Polaris_SCItem", "Stellate", "PowerPlant", 2.1, MU, 2100000, size=4),
    _e("Paint_Gladius_Blue", "Gladius Blue Paint", "Paints", 0.0, MU, 1),
    _e("POWR_S04_Template", "<= PLACEHOLDER =>", "PowerPlant", 2.1, MU, 2100000, size=4),
]
ITEMS = [
    _crate(EIGHTH, "Stor*All 1/8 SCU Storage Box", 0.1225, (0.5, 0.5, 0.5)),
    _crate(C1, "Stor*All 1 SCU Self-Storage Container", 1, (1.25, 1.25, 1.25)),
    _crate(C2, "Stor*All 2 SCU Self-Storage Container", 2, (1.25, 1.25, 2.5)),
    _crate(C4, "Stor*All 4 SCU Self-Storage Container", 4, (2.5, 1.25, 2.5)),
    _crate(C8, "Stor*All 8 SCU Self-Storage Container", 8, (2.5, 2.5, 2.5)),
    _crate("Carryable_TBO_InventoryContainer_TEMPLATE", "Stor*All 1 SCU Self-Storage Container",
           1, (1.25, 1.25, 1.25)),                                                   # skipped
    _crate("Carryable_TBO_InventoryContainer_4SCU_Pirate", "Salvaged Skull 4 SCU Container",
           4, (2.5, 1.25, 2.5)),                                                     # not Stor*All
    _e("Hadanite_gem", "Hadanite", "Misc", 0.001, MU, 1000),
    _e("Door_Cupboard", "Cupboard", "Cargo", 0.5),                                   # not carryable
    _e("clothing_pants_emilion", "Emilion Pants", "Char_Clothing_Legs", 0.0073, MU, 7300,
       ref=PANTS_UUID),                                                              # duplicate
]


def _write_sources(d):
    os.makedirs(d, exist_ok=True)
    for name, data in (("fps-items.json", FPS), ("ship-items.json", SHIP_ITEMS),
                       ("items.json", ITEMS)):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)


def _index(tmp_path):
    d = str(tmp_path / "idx-src")
    _write_sources(d)
    return crate_items.build_index({n: os.path.join(d, n) for n in crate_items.SOURCE_FILES})


# ── pure: crates, units, index, search, the fit rule ─────────────────────────

def test_crate_defs_sizes_and_capacities():
    defs = {d["key"]: d for d in crate_items.crate_defs()}
    assert [d["short"] for d in crate_items.crate_defs()] == [
        "1/8 SCU", "1 SCU", "2 SCU", "4 SCU", "8 SCU"]
    assert defs[EIGHTH]["dims"] == (1, 1, 1) and defs[EIGHTH]["capacity_u"] == 122_500
    assert defs[C1]["dims"] == (1, 1, 1) and defs[C1]["capacity_u"] == 1_000_000
    assert defs[C2]["dims"] == (1, 1, 2) and defs[C2]["capacity_u"] == 2_000_000
    assert defs[C4]["dims"] == (2, 1, 2) and defs[C4]["capacity_u"] == 4_000_000
    assert defs[C8]["dims"] == (2, 2, 2) and defs[C8]["capacity_u"] == 8_000_000
    assert all(d["category"] == "crate" for d in defs.values())


def test_units_and_display():
    assert crate_items.volume_u({"SCU": 0.0073, "SCUConverted": 7300, "Unit": MU}) == 7300
    assert crate_items.volume_u({"SCU": 0.0059, "SCUConverted": 5930, "Unit": MU}) == 5930
    assert crate_items.volume_u({"SCU": 0, "SCUConverted": 1, "Unit": "μSCU"}) == 1
    assert crate_items.volume_u({"SCU": 0.01, "SCUConverted": 1, "Unit": "cSCU"}) == 10_000
    assert crate_items.volume_u({"SCU": 2.1, "SCUConverted": 2.1, "Unit": "SCU"}) == 2_100_000
    assert crate_items.volume_u({"SCU": 0, "SCUConverted": 0, "Unit": "SCU"}) is None
    assert crate_items.capacity_u({"SCU": 0.1225, "UnitName": "SCU"}) == 122_500
    assert crate_items.fmt_u(2_100_000) == "2.1 SCU"
    assert crate_items.fmt_u(122_500) == "0.1225 SCU"
    assert crate_items.fmt_u(7300) == "7,300 µSCU"


def test_crates_from_items_json_match_the_pinned_table():
    got = crate_items.crates_from_entries(ITEMS)
    assert [c["cls"] for c in got] == list(crate_items.CRATE_CLASSES)
    want = [dict(c, grid_m=tuple(c["grid_m"])) for c in crate_items.CRATES]
    assert [dict(c, name=w["name"]) for c, w in zip(got, want)] == want


def test_real_items_json_if_present():
    here = []
    try:
        from shared import scunpacked
        here.append(os.path.join(scunpacked.cache_dir(), "items.json"))
    except Exception:                                   # noqa: BLE001
        pass
    path = next((p for p in here if os.path.isfile(p)), None)
    if not path:
        pytest.skip("items.json not downloaded on this machine")
    got = crate_items.crates_from_entries(crate_items.iter_json_array(path))
    assert got == [dict(c, grid_m=tuple(c["grid_m"])) for c in crate_items.CRATES]


def test_index_streams_filters_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(crate_items, "CHUNK", 97)      # force many buffer refills
    idx = _index(tmp_path)
    names = [r[0] for r in idx["items"]]
    assert sorted(names) == sorted(set(names))
    assert set(names) >= {"Emilion Pants", "P4-AR Rifle", "MedPen (Hemozal)", "Stellate",
                          "Hadanite", "Stor*All 4 SCU Self-Storage Container"}
    assert not any("PLACEHOLDER" in n for n in names)
    assert "Gladius Blue Paint" not in names and "Cupboard" not in names
    row = {r[0]: r for r in idx["items"]}
    assert row["Stellate"][1] == 2_100_000 and row["Stellate"][2] == "Power Plant S4"
    assert row["Emilion Pants"][1] == 7300 and row["Emilion Pants"][4] == PANTS_UUID
    assert [c["cls"] for c in idx["crates"]] == list(crate_items.CRATE_CLASSES)
    assert sorted(idx["sources"]) == sorted(crate_items.SOURCE_FILES)


def test_load_index_builds_once_then_reads(tmp_path):
    d = str(tmp_path / "cache")
    assert crate_items.load_index(d) is None            # nothing downloaded
    _write_sources(d)
    idx = crate_items.load_index(d)
    ip = crate_items.index_path(d)
    assert os.path.isfile(ip) and idx["items"]
    os.remove(os.path.join(d, "items.json"))            # the index alone is enough now
    assert crate_items.load_index(d)["items"] == idx["items"]


def test_fuzzy_search(tmp_path):
    rows = _index(tmp_path)["items"]
    assert [r[0] for r in crate_items.search(rows, "pants")] == ["Emilion Pants"]
    assert crate_items.search(rows, "stelate")[0][0] == "Stellate"      # typo
    assert crate_items.search(rows, "p4ar")[0][0] == "P4-AR Rifle"
    assert crate_items.search(rows, "power s4")[0][0] == "Stellate"     # by type
    assert crate_items.search(rows, "") == []


def test_fit_rule_is_strict():
    contents = []
    pants = {"key": "p", "name": "Emilion Pants", "vol_u": 7300}
    assert crate_items.add_item(122_500, contents, pants, 16) == (True, "")
    ok, why = crate_items.add_item(122_500, contents, pants, 1)
    assert not ok and why == "needs 7,300 µSCU, 5,700 µSCU left"
    ok, why = crate_items.set_qty(122_500, contents, "p", 17)
    assert not ok and contents[0]["qty"] == 16
    assert crate_items.set_qty(122_500, contents, "p", 3) == (True, "")
    ok, why = crate_items.add_item(122_500, contents, {"key": "s", "name": "Stellate",
                                                       "vol_u": 2_100_000})
    assert not ok and why == "needs 2.1 SCU, 0.1006 SCU left"


# ── UI ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def win(monkeypatch, tmp_path):
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import cargo_app
    import shared.qt.base_window as bw
    monkeypatch.setattr(cargo_app, "_fetch_uex_commodities", lambda: None)
    monkeypatch.setattr(cargo_app.ShipDataLoader, "load_async", lambda self, cb: None)
    monkeypatch.setattr(bw, "_save_window_state", lambda w: None)
    monkeypatch.setattr(cargo_app, "item_catalog_path",
                        lambda: str(tmp_path / "no-ship-items.json"), raising=False)
    # Crate data and the UEX cache: temp paths only, never ~/.sctoolbox.
    monkeypatch.setattr(cargo_app, "crate_data_dir", lambda: str(tmp_path / "crate-data"),
                        raising=False)
    monkeypatch.setattr(cargo_app, "uex_cache_path", lambda: str(tmp_path / "no-uex.json"),
                        raising=False)
    opened = []
    w = cargo_app.CargoApp(0, 0, 1100, 800, 1.0, None)
    w._commodity_poll.stop()
    monkeypatch.setattr(w, "crate_open_url", opened.append, raising=False)
    w._opened_urls = opened
    monkeypatch.setattr(w._data, "find", lambda name: dict(SHIP))
    w.show()
    w._load_ship("Crates Test Ship")
    QTest.qWait(80)
    w._render_grid()
    qapp.processEvents()
    yield w
    for cw in list(getattr(w, "_crate_windows", {}).values()):
        cw.hide()
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


def _wait(cond, ms=3000):
    end = time.time() + ms / 1000
    while time.time() < end:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        QTest.qWait(20)
    return cond()


def _place(win, cls, at):
    win._brush_tabs.setCurrentIndex(1)
    win._crate_btns[cls].click()
    assert win._place_item == cls
    _click(win, _vp(win, *at))
    return win._renderer._items[-1]


def _tabs(win):
    t = win._view_tabs
    return [t.tabText(i) for i in range(t.count())]


def _group(win, box):
    return next(g for g in win._renderer._box_groups if tuple(g.box_data) == box)


def _three(win):
    a = _place(win, C4, (1, 0, 1))
    b = _place(win, C1, (4.5, 0, 0.5))
    c = _place(win, EIGHTH, (0.5, 0, 6.5))
    return a, b, c


def test_crates_listed_in_items_tab(win):
    btns = win._crate_btns
    assert [btns[k].text() for k in crate_items.CRATE_CLASSES] == ["1/8", "1", "2", "4", "8"]
    d = win._renderer._item_defs[C4]
    assert d["dims"] == (2, 1, 2) and d["capacity_u"] == 4_000_000
    assert "4 SCU" in btns[C4].toolTip() and "2×1×2" in btns[C4].toolTip()
    assert not win._view_tabs.tabBar().isVisible()      # no crates yet: hold only
    assert _tabs(win) == ["Hold"]


def test_placed_crates_number_1_to_n_and_stay_stable(win):
    a, b, c = _three(win)
    assert [x[6] for x in (a, b, c)] == [C4 + "#1", C1 + "#2", EIGHTH + "#3"]
    assert (a[3:6], b[3:6], c[3:6]) == ((2, 1, 2), (1, 1, 1), (1, 1, 1))
    assert _group(win, a)._label_item.toPlainText() == "1"
    assert _group(win, c)._label_item.toPlainText() == "3"
    assert _tabs(win) == ["Hold", "Crate 1 (4 SCU)", "Crate 2 (1 SCU)", "Crate 3 (1/8 SCU)"]
    assert win._view_tabs.tabBar().isVisible()
    assert "Crates: 3" in win._items_summary_lbl.text()
    # remove crate 2: crate 3 keeps its number, its tab and its label
    win._crate_btns[C4].click()                         # stop placing (toggle off)
    win._set_place_item(None)
    _send(win._view, QEvent.MouseButtonPress, _vp(win, b[0] + 0.5, 1, b[2] + 0.5),
          Qt.RightButton, Qt.RightButton)
    QTest.qWait(30)
    QtWidgets.QApplication.processEvents()
    assert [x[6] for x in win._renderer._items] == [C4 + "#1", EIGHTH + "#3"]
    assert _tabs(win) == ["Hold", "Crate 1 (4 SCU)", "Crate 3 (1/8 SCU)"]
    assert _group(win, c)._label_item.toPlainText() == "3"
    # the next crate is 4, never a reused 2
    d = _place(win, C2, (4.5, 0, 4))
    assert d[6] == C2 + "#4"
    # Ctrl+Z takes crate 4 away, again brings crate 2 back (same number)
    win._undo_move()
    win._undo_move()
    assert sorted(x[6] for x in win._renderer._items) == sorted(
        [C4 + "#1", C1 + "#2", EIGHTH + "#3"])
    assert _tabs(win) == ["Hold", "Crate 1 (4 SCU)", "Crate 2 (1 SCU)", "Crate 3 (1/8 SCU)"]


def test_crate_tab_opens_pops_out_and_docks(win):
    a, b, c = _three(win)
    win._set_place_item(None)
    # clicking crate 2's lid in the hold opens its tab
    _click(win, _vp(win, b[0] + 0.5, b[1] + b[4], b[2] + 0.5))
    assert win._view_tabs.currentWidget() is win._crate_panels[2]
    panel = win._crate_panels[2]
    panel.pop_btn.click()
    QtWidgets.QApplication.processEvents()
    assert 2 in win._crate_windows and win._crate_windows[2].isVisible()
    assert panel.window() is win._crate_windows[2]
    assert _tabs(win) == ["Hold", "Crate 1 (4 SCU)", "Crate 3 (1/8 SCU)"]
    assert panel.pop_btn.text() == "Dock"
    # closing the window docks it back, in number order
    win._crate_windows[2].close()
    QtWidgets.QApplication.processEvents()
    assert 2 not in win._crate_windows
    assert _tabs(win) == ["Hold", "Crate 1 (4 SCU)", "Crate 2 (1 SCU)", "Crate 3 (1/8 SCU)"]
    assert panel.window() is win and win._view_tabs.currentWidget() is panel
    # pop out again and dock with the button
    panel.pop_btn.click()
    panel.pop_btn.click()
    assert 2 not in win._crate_windows and win._view_tabs.indexOf(panel) == 2
    # a popped-out crate that is removed from the hold closes its window
    panel.pop_btn.click()
    cw = win._crate_windows[2]
    win._remove_box(b)
    QtWidgets.QApplication.processEvents()
    assert 2 not in win._crate_windows and not cw.isVisible()
    assert _tabs(win) == ["Hold", "Crate 1 (4 SCU)", "Crate 3 (1/8 SCU)"]


def test_item_that_fits_is_added_and_fill_updates(win, tmp_path):
    a = _place(win, C4, (1, 0, 1))
    win._set_place_item(None)
    win._set_crate_index(_index(tmp_path))
    win._open_crate(1)
    panel = win._crate_panels[1]
    assert panel.bar._pct == 0 and panel.fill_lbl.text().startswith("0 µSCU / 4 SCU")
    panel.search.setText("stellate")
    assert panel.results.topLevelItemCount() == 1
    assert panel.results.currentItem().text(0) == "Stellate"
    panel.add_btn.click()
    st = win.crate_state(1)
    assert [(c["name"], c["qty"], c["vol_u"]) for c in st["contents"]] == [("Stellate", 1, 2_100_000)]
    assert panel.contents.topLevelItemCount() == 1
    assert panel.contents.topLevelItem(0).text(0) == "1"
    assert panel.fill_lbl.text() == "2.1 SCU / 4 SCU  (52%)"
    assert abs(panel.bar._pct - 0.525) < 1e-9
    assert "Added 1 × Stellate" in panel.msg_lbl.text()
    # quantity controls
    panel.search.setText("pants")
    panel.qty.setValue(10)
    panel.add_btn.click()
    panel.contents.setCurrentItem(panel.contents.topLevelItem(1))
    panel.plus_btn.click()
    assert win.crate_state(1)["contents"][1]["qty"] == 11
    panel.contents.setCurrentItem(panel.contents.topLevelItem(1))
    panel.remove_btn.click()
    assert [c["name"] for c in win.crate_state(1)["contents"]] == ["Stellate"]


def test_too_big_item_is_refused_with_the_reason(win, tmp_path):
    _place(win, EIGHTH, (1, 0, 1))
    win._set_place_item(None)
    win._set_crate_index(_index(tmp_path))
    win._open_crate(1)
    panel = win._crate_panels[1]
    panel.search.setText("stellate")
    # shown red before trying: it cannot fit what is left
    assert panel.results.currentItem().foreground(0).color().name() == \
        __import__("shared.qt.theme", fromlist=["P"]).P.red.lower()
    panel.add_btn.click()
    assert win.crate_state(1)["contents"] == []
    assert panel.msg_lbl.text() == "Won't fit: needs 2.1 SCU, 0.1225 SCU left"
    ok, why = win.crate_add(1, {"key": "x", "name": "Stellate", "vol_u": 2_100_000})
    assert not ok and why == "needs 2.1 SCU, 0.1225 SCU left"


def test_quantity_overflow_is_refused(win, tmp_path):
    _place(win, EIGHTH, (1, 0, 1))
    win._set_place_item(None)
    win._set_crate_index(_index(tmp_path))
    win._open_crate(1)
    panel = win._crate_panels[1]
    panel.search.setText("pants")
    panel.qty.setValue(17)                           # 17 x 7,300 = 124,100 > 122,500
    panel.add_btn.click()
    assert win.crate_state(1)["contents"] == []
    assert panel.msg_lbl.text() == "Won't fit: needs 0.1241 SCU, 0.1225 SCU left"
    panel.qty.setValue(16)
    panel.add_btn.click()
    assert win.crate_state(1)["contents"][0]["qty"] == 16
    panel.contents.setCurrentItem(panel.contents.topLevelItem(0))
    panel.plus_btn.click()                           # the 17th does not fit either
    assert win.crate_state(1)["contents"][0]["qty"] == 16
    assert panel.msg_lbl.text() == "Won't fit: needs 7,300 µSCU, 5,700 µSCU left"
    assert panel.fill_lbl.text() == "0.1168 SCU / 0.1225 SCU  (95%)"


def test_save_load_round_trips_crates(win, tmp_path):
    a, b, c = _three(win)
    win._set_place_item(None)
    win._remove_box(b)                               # numbers 1 and 3 remain
    idx = _index(tmp_path)
    rows = {r[0]: r for r in idx["items"]}
    win.crate_add(1, crate_items.entry_from_row(rows["Stellate"]), 1)
    win.crate_add(1, crate_items.entry_from_row(rows["Emilion Pants"]), 5)
    win.crate_add(3, crate_items.entry_from_row(rows["MedPen (Hemozal)"]), 4)
    win._renderer._assignments[(c[0], c[1], c[2], c[6])] = "Mission Cargo 2"
    payload = json.loads(json.dumps(win._loadout_payload()))   # what a file holds
    assert "items" not in payload                    # crates are not plain items
    assert [(k["no"], k["cls"], k["size"]) for k in payload["crates"]] == [
        (1, C4, "4 SCU"), (3, EIGHTH, "1/8 SCU")]
    assert payload["crates"][0]["pos"] == list(a[:3]) and payload["crates"][0]["dims"] == [2, 1, 2]
    assert [(i["name"], i["qty"], i["vol_u"]) for i in payload["crates"][0]["contents"]] == [
        ("Stellate", 1, 2_100_000), ("Emilion Pants", 5, 7300)]

    win._pending_loadout = payload
    win._load_ship(payload["ship"])
    assert sorted(x[6] for x in win._renderer._items) == sorted([C4 + "#1", EIGHTH + "#3"])
    assert [(c["name"], c["qty"]) for c in win.crate_state(3)["contents"]] == [
        ("MedPen (Hemozal)", 4)]
    assert win.crate_state(1)["contents"][1]["qty"] == 5
    assert _tabs(win) == ["Hold", "Crate 1 (4 SCU)", "Crate 3 (1/8 SCU)"]
    assert win._renderer._assignments.get((c[0], c[1], c[2], c[6])) == "Mission Cargo 2"
    assert _place(win, C1, (4.5, 0, 4.5))[6] == C1 + "#4"      # after the highest saved


def test_old_plans_still_load(win):
    old = {"version": 1, "ship": "Crates Test Ship", "rotation": 0,
           "counts": {"1": 0, "2": 0, "4": 0, "8": 1, "16": 0, "24": 0, "32": 0},
           "assignments": [], "boxes": [{"scu": 8, "pos": [0, 0, 0], "dims": [2, 2, 2]}]}
    _place(win, C4, (4, 0, 5))                        # a crate from before the load
    win._pending_loadout = old
    win._load_ship(old["ship"])
    assert win._renderer._items == [] and win._crates == {}
    assert win._renderer._manual_boxes == [(0, 0, 0, 2, 2, 2, 8)]
    assert _tabs(win) == ["Hold"] and not win._view_tabs.tabBar().isVisible()
    # an Items-tab plan (b74f1e1) with its "items" list loads its item as before
    items_plan = dict(old, items=[{"key": "Cargo_ShipMining_Pod_Prospector", "name": "MISC Ore Pod",
                                   "category": "ore", "pos": [3, 0, 3], "dims": [2, 2, 2]}])
    win._pending_loadout = items_plan
    win._load_ship(old["ship"])
    assert win._renderer._items == [(3, 0, 3, 2, 2, 2, "Cargo_ShipMining_Pod_Prospector")]
    assert _place(win, C1, (5.5, 0, 7.5))[6] == C1 + "#1"


def test_missing_data_degrades_and_says_so(win):
    _place(win, C1, (1, 0, 1))
    win._set_place_item(None)
    win._open_crate(1)
    panel = win._crate_panels[1]
    assert win._crate_index_state == "missing"
    assert panel.data_lbl.text() == "Item lists not downloaded yet."
    assert not panel.dl_btn.isHidden()
    panel.search.setText("pants")
    assert panel.results.topLevelItemCount() == 0
    panel.add_btn.click()
    assert "Search for an item" in panel.msg_lbl.text()


def test_index_loads_lazily_off_the_ui_thread(win, tmp_path):
    d = str(tmp_path / "crate-data")
    _write_sources(d)
    assert win._crate_index_state == "idle"          # nothing read at start-up
    _place(win, C1, (1, 0, 1))                       # first use of the crates
    assert win._crate_index_state in ("loading", "ready")
    assert _wait(lambda: win._crate_index_state == "ready")
    assert os.path.isfile(crate_items.index_path(d))
    assert win._uex_state == "idle"                  # UEX waits for a crate tab
    win._set_place_item(None)
    win._open_crate(1)
    assert _wait(lambda: win._uex_state == "none")
    panel = win._crate_panels[1]
    assert panel.data_lbl.text().endswith("no UEX cache, names only")
    panel.search.setText("medpen")
    assert panel.results.topLevelItem(0).text(0) == "MedPen (Hemozal)"
    assert panel.uex_lbl.text() == "No UEX cache: name only"
    assert not panel.uex_btn.isEnabled()


def test_uex_info_from_the_item_finder_cache(win, tmp_path, monkeypatch):
    import cargo_app
    uex = tmp_path / "uex.json"
    uex.write_text(json.dumps({"version": 3, "timestamp": 1, "items": [
        {"id": 665, "name": "Emilion Pants", "slug": "emilion-pants", "uuid": PANTS_UUID,
         "section": "Clothing", "category": "Legwear", "company_name": "Derion"}]}),
        encoding="utf-8")
    monkeypatch.setattr(cargo_app, "uex_cache_path", lambda: str(uex))
    _place(win, C1, (1, 0, 1))
    win._set_place_item(None)
    win._set_crate_index(_index(tmp_path))
    win._open_crate(1)
    assert _wait(lambda: win._uex_state == "ready")
    panel = win._crate_panels[1]
    assert panel.data_lbl.text().endswith("UEX linked")
    panel.search.setText("emilion")
    assert panel.uex_lbl.text() == "UEX: Clothing / Legwear  ·  Derion"
    assert panel.uex_btn.isEnabled()
    panel.uex_btn.click()
    assert win._opened_urls == ["https://uexcorp.space/items/info?name=emilion-pants"]
    panel.search.setText("hadanite")                 # not in this UEX cache
    assert panel.uex_lbl.text() == "Not listed on UEX" and not panel.uex_btn.isEnabled()
