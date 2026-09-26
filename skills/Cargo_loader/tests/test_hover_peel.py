"""Hover highlight + hide the boxes on top (J, 2026-09-26).

"Can we have highlight on hover and the option to click and hide boxes on top
for cargo loader".

Runs offscreen. Mouse events are synthesised and delivered straight to the
view's viewport; nothing reaches the OS input queue. No network. Crate data,
the item catalogue and the UEX cache point at a temp dir, never ~/.sctoolbox,
and no plan is saved.
"""

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

SHIP = {
    "name": "Peel Test Ship", "manufacturer": "Test", "capacity": 288,
    "provenance": {"source": "scunpacked-data"},
    "groups": [{"x": 0, "z": 0, "grids": [
        {"x": 0, "z": 0, "width": 6, "height": 6, "length": 8,
         "minSize": 1, "maxSize": 32},
    ]}],
}

C1 = "Carryable_TBO_InventoryContainer_1SCU"

A = (0, 0, 0, 2, 2, 2, 8)        # stack 1: bottom
A2 = (0, 2, 0, 2, 1, 2, 4)       # stack 1: on A
B = (4, 0, 0, 2, 2, 2, 8)        # alone on the floor
C = (4, 0, 4, 2, 2, 2, 8)        # stack 2: bottom
D = (4, 2, 4, 2, 2, 2, 8)        # stack 2: on C


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
    monkeypatch.setattr(cargo_app, "crate_data_dir", lambda: str(tmp_path / "crate-data"),
                        raising=False)
    monkeypatch.setattr(cargo_app, "uex_cache_path", lambda: str(tmp_path / "no-uex.json"),
                        raising=False)
    w = cargo_app.CargoApp(0, 0, 1100, 800, 1.0, None)
    w._commodity_poll.stop()
    monkeypatch.setattr(w._data, "find", lambda name: dict(SHIP))
    w.show()
    w._load_ship("Peel Test Ship")
    QTest.qWait(80)
    # A crate on top of stack 1, so peel covers containers AND crates.
    key = w._new_crate(C1)
    w._renderer._manual_boxes = [A, A2, B, C, D]
    w._renderer._items = [(0.5, 3, 0.5, 1, 1, 1, key)]
    w._sync_counts_from_boxes()
    w._render_grid()
    qapp.processEvents()
    w._crate_box = (0.5, 3, 0.5, 1, 1, 1, key)
    yield w
    for cw in list(getattr(w, "_crate_windows", {}).values()):
        cw.hide()
    w.hide()
    w.deleteLater()
    qapp.processEvents()


def _send(view, etype, pos: QPoint, button, buttons, mods=Qt.NoModifier):
    vp = view.viewport()
    ev = QMouseEvent(etype, QPointF(pos), QPointF(vp.mapToGlobal(pos)),
                     button, buttons, mods)
    QtWidgets.QApplication.sendEvent(vp, ev)


def _settle():
    QTest.qWait(30)
    QtWidgets.QApplication.processEvents()


def _hover(win, pos):
    _send(win._view, QEvent.MouseMove, pos, Qt.NoButton, Qt.NoButton)


def _alt_click(win, pos):
    _send(win._view, QEvent.MouseButtonPress, pos, Qt.LeftButton, Qt.LeftButton,
          Qt.AltModifier)
    _send(win._view, QEvent.MouseButtonRelease, pos, Qt.LeftButton, Qt.NoButton,
          Qt.AltModifier)
    _settle()


def _click(win, pos, button=Qt.LeftButton):
    _send(win._view, QEvent.MouseButtonPress, pos, button, button)
    _send(win._view, QEvent.MouseButtonRelease, pos, button, Qt.NoButton)
    _settle()


def _group(win, box):
    return next(g for g in win._renderer._box_groups if tuple(g.box_data) == tuple(box))


def _vp_scene(win, sx, sy) -> QPoint:
    return win._view.mapFromScene(QPointF(sx, sy))


def _point_on(win, box) -> QPoint:
    """A viewport point where *box* is the topmost box drawn (any camera)."""
    g = _group(win, box)
    for face in reversed(g._face_items):            # top face first
        poly = face.polygon()
        pts = [poly.at(i) for i in range(poly.count())]
        cx = sum(p.x() for p in pts) / len(pts)
        cy = sum(p.y() for p in pts) / len(pts)
        cands = [(cx, cy)] + [(cx + (p.x() - cx) * t, cy + (p.y() - cy) * t)
                              for t in (0.5, 0.8) for p in pts]
        for sx, sy in cands:
            vp = _vp_scene(win, sx, sy)
            if win._box_group_at(win._view.mapToScene(vp)) is g:
                return vp
    raise AssertionError(f"no visible point on {box}")


def _counts(win):
    return {s: sb.value() for s, sb in win._spinboxes.items() if sb.value()}


def _plan(win):
    return (sorted(win._renderer._last_boxes), sorted(win._renderer._items), _counts(win))


def _hidden(win):
    return {tuple(g.box_data) for g in win._renderer._box_groups if not g.isVisible()}


# ── hover ────────────────────────────────────────────────────────────────────

def test_hover_highlights_and_moves_to_the_next_box(win, monkeypatch):
    import cargo_app
    before = win._status_lbl.text()
    gb = _group(win, B)
    normal = gb._face_items[2].brush().color().name()
    _hover(win, _point_on(win, B))
    assert win._renderer._hover is gb and gb.highlighted
    lit = gb._face_items[2].brush().color()
    assert lit.lightness() > cargo_app.QColor(normal).lightness()      # lighter fill
    assert gb._face_items[2].pen().color().name() == cargo_app.HOVER_EDGE
    assert gb._face_items[2].pen().widthF() >= 2                        # bright outline
    assert win._status_lbl.text().startswith("8 SCU container")

    # Moving to another box restyles exactly two boxes, and never re-renders.
    calls = []
    orig = cargo_app._CargoBoxGroup.recolor
    monkeypatch.setattr(cargo_app._CargoBoxGroup, "recolor",
                        lambda self, c: (calls.append(tuple(self.box_data)), orig(self, c)))
    monkeypatch.setattr(win._renderer, "render",
                        lambda *a, **k: pytest.fail("hover must not re-render"))
    gc = _group(win, D)
    _hover(win, _point_on(win, D))
    assert win._renderer._hover is gc and gc.highlighted and not gb.highlighted
    assert sorted(calls) == sorted([B, D])
    assert gb._face_items[2].brush().color().name() == normal           # restored
    _hover(win, _point_on(win, D))                                      # same box: no work
    assert sorted(calls) == sorted([B, D])

    # Leaving the view ends the hover and puts the status back.
    QtWidgets.QApplication.sendEvent(win._view, QEvent(QEvent.Leave))
    assert win._renderer._hover is None and not gc.highlighted
    assert win._status_lbl.text() == before


def test_hover_names_items_and_crates(win):
    _hover(win, _point_on(win, win._crate_box))
    assert win._status_lbl.text().startswith("Crate 1 (1 SCU)")
    _hover(win, _point_on(win, A2))
    assert win._status_lbl.text().startswith("4 SCU container")
    assert "Alt+click hides the 1 above" in win._status_lbl.text()


def test_hover_is_off_while_a_tool_is_armed(win):
    win._place_btns[1].click()
    _hover(win, _point_on(win, B))
    assert win._renderer._hover is None
    assert not any(g.highlighted for g in win._renderer._box_groups)
    assert win._renderer._ghost is not None            # the ghost still works
    win._set_place_size(None)
    _hover(win, _point_on(win, B))
    assert win._renderer._hover is _group(win, B)
    # a paint brush turns it off too (and un-highlights what was lit)
    win._on_commodity_selected("Mission Cargo 1")
    assert win._renderer._hover is None and not _group(win, B).highlighted
    _hover(win, _point_on(win, D))
    assert win._renderer._hover is None


# ── hide the boxes on top ────────────────────────────────────────────────────

def test_peel_hides_exactly_the_boxes_above_and_keeps_the_plan(win):
    plan = _plan(win)
    _alt_click(win, _point_on(win, A))
    assert _hidden(win) == {A2, win._crate_box}
    assert _plan(win) == plan                              # counts, boxes, items
    assert win._renderer._manual_boxes is not None and A2 in win._renderer._manual_boxes
    assert win._hidden_lbl.isVisible() and win._show_all_btn.isVisible()
    assert win._hidden_lbl.text().startswith("2 boxes hidden")
    assert win._crate_tab_text(1) in [win._view_tabs.tabText(i)
                                      for i in range(win._view_tabs.count())]
    # A itself is now the box you see, hover and click
    _hover(win, _point_on(win, A))
    assert win._renderer._hover is _group(win, A)
    # Alt+click never removes, moves or places anything
    assert _plan(win) == plan
    # a box with nothing on it: nothing hidden, status says so
    _alt_click(win, _point_on(win, B))
    assert _hidden(win) == {A2, win._crate_box}
    assert "Nothing on top" in win._status_lbl.text()


def test_layer_slider_hides_by_height(win):
    s = win._layer_slider
    assert (s.minimum(), s.maximum(), s.value()) == (1, 6, 6)
    assert not win._hidden_lbl.isVisible()
    s.setValue(2)                                          # base y >= 2 hidden
    assert _hidden(win) == {A2, D, win._crate_box}
    assert win._layer_lbl.text() == "Up to layer 2/6"
    assert win._hidden_lbl.text().startswith("3 boxes hidden")
    s.setValue(3)
    assert _hidden(win) == {win._crate_box}
    s.setValue(1)
    assert _hidden(win) == {A2, D, win._crate_box}
    s.setValue(6)
    assert _hidden(win) == set() and win._layer_lbl.text() == "All layers"


def test_show_all_restores_everything(win):
    plan = _plan(win)
    _alt_click(win, _point_on(win, C))
    win._layer_slider.setValue(3)
    assert _hidden(win) == {D, win._crate_box}
    win._show_all_btn.click()
    assert _hidden(win) == set()
    assert win._layer_slider.value() == win._layer_slider.maximum()
    assert not win._hidden_lbl.isVisible() and not win._show_all_btn.isVisible()
    assert _plan(win) == plan


def test_hidden_boxes_are_not_hit(win):
    at = _point_on(win, D)
    _alt_click(win, _point_on(win, C))
    assert _hidden(win) == {D}
    scene = win._view.mapToScene(at)
    assert win._box_group_at(scene) is not _group(win, D)
    _hover(win, at)
    assert win._renderer._hover is not _group(win, D)
    _click(win, at, Qt.RightButton)                        # manual: right-click removes
    assert D in win._renderer._last_boxes                  # ...but never a hidden box
    _alt_click(win, at)                                    # nor peels through one
    assert D in win._renderer._last_boxes and _group(win, D).isVisible() is False


def test_placing_on_hidden_boxes_shows_them(win):
    # Peeled: stacking on the visible box below lands on the hidden one, so
    # the hidden box it rests on is shown again.
    _alt_click(win, _point_on(win, C))
    assert _hidden(win) == {D}
    win._place_btns[1].click()
    _click(win, _point_on(win, C))
    new = next(b for b in win._renderer._last_boxes if b not in (A, A2, B, C, D))
    assert new[1] >= 4                                     # on top of D
    assert D not in _hidden(win)
    # Height cap: a box placed above the cap drops the cap.
    win._set_place_size(None)
    win._layer_slider.setValue(2)
    assert B not in _hidden(win)
    win._place_btns[1].click()
    _click(win, _point_on(win, B))
    assert win._renderer._layer_cap is None and _hidden(win) == set()


@pytest.mark.parametrize("turns", [0, 1, 2, 3])
def test_works_at_every_camera(win, turns):
    for _ in range(turns):
        win._rotate_cw()
    assert win._renderer._rotation == turns
    _hover(win, _point_on(win, C))
    assert win._renderer._hover is _group(win, C)
    _alt_click(win, _point_on(win, A))
    assert _hidden(win) == {A2, win._crate_box}
    # hidden state survives turning the camera
    win._rotate_cw()
    assert _hidden(win) == {A2, win._crate_box}
    win._show_all()
    assert _hidden(win) == set()
