"""Manual mode for the Cargo Loader (J, 2026-09-26): obvious, default, blank grid.

Runs offscreen. Mouse events are synthesised and delivered straight to the
view's viewport; nothing reaches the OS input queue. No network, no settings.
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
    "name": "Manual Test Ship", "manufacturer": "Test", "capacity": 96,
    "provenance": {"source": "scunpacked-data"},
    "groups": [{"x": 0, "z": 0, "grids": [
        {"x": 0, "z": 0, "width": 6, "height": 2, "length": 8,
         "minSize": 1, "maxSize": 32},
    ]}],
}


@pytest.fixture
def win(monkeypatch):
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import cargo_app
    import shared.qt.base_window as bw
    monkeypatch.setattr(cargo_app, "_fetch_uex_commodities", lambda: None)
    monkeypatch.setattr(cargo_app.ShipDataLoader, "load_async", lambda self, cb: None)
    monkeypatch.setattr(bw, "_save_window_state", lambda w: None)
    w = cargo_app.CargoApp(0, 0, 1000, 700, 1.0, None)
    w._commodity_poll.stop()
    monkeypatch.setattr(w._data, "find", lambda name: dict(SHIP))
    w.show()
    w._load_ship("Manual Test Ship")
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


def _counts(win):
    return {s: sb.value() for s, sb in win._spinboxes.items() if sb.value()}


def test_manual_is_default_and_ship_starts_blank(win):
    assert win._mode == "manual"
    assert win._mode_btns["manual"].isChecked() and not win._mode_btns["auto"].isChecked()
    assert win._mode_hint.text()                       # the instructions are on screen
    assert win._renderer._last_boxes == []
    assert _counts(win) == {}


def test_click_grid_places_box_and_counts_follow(win):
    win._place_btns[16].click()
    assert win._place_size == 16
    _click(win, _vp(win, 3, 0, 4))
    boxes = win._renderer._last_boxes
    assert len(boxes) == 1 and boxes[0][6] == 16
    assert _counts(win) == {16: 1}
    assert win._mode == "manual" and win._renderer._manual_boxes is not None


def test_hover_shows_ghost(win):
    win._place_btns[8].click()
    p = _vp(win, 1, 0, 1)
    _send(win._view, QEvent.MouseMove, p, Qt.NoButton, Qt.NoButton)
    assert win._renderer._ghost is not None and win._renderer._ghost.ghost_valid


def test_click_outside_grid_places_nothing(win):
    win._place_btns[16].click()
    _click(win, _vp(win, 40, 0, 40))
    assert win._renderer._last_boxes == []


def test_right_click_removes_and_undo_restores(win):
    win._place_btns[16].click()
    _click(win, _vp(win, 3, 0, 4))
    x, y, z, w, h, l, _s = win._renderer._last_boxes[0]
    on_box = _vp(win, x + w / 2, y + h, z + l / 2)    # top face centre
    _send(win._view, QEvent.MouseButtonPress, on_box, Qt.RightButton, Qt.RightButton)
    QTest.qWait(30)
    QtWidgets.QApplication.processEvents()
    assert win._renderer._last_boxes == [] and _counts(win) == {}
    win._undo_move()
    assert len(win._renderer._last_boxes) == 1 and _counts(win) == {16: 1}


def test_optimize_in_manual_fills_and_stays_editable(win):
    win._optimize()
    assert win._mode == "manual"
    assert win._renderer._last_boxes
    assert win._renderer._manual_boxes == win._renderer._last_boxes
    placed = sum(b[6] for b in win._renderer._last_boxes)
    assert sum(s * n for s, n in _counts(win).items()) == placed


def test_typing_a_count_switches_to_auto(win):
    win._spinboxes[16].setValue(1)
    assert win._mode == "auto" and win._mode_btns["auto"].isChecked()
    assert len(win._renderer._last_boxes) == 1


def test_mission_cargo_brush(win):
    import cargo_app
    items = win._commodity_combo._all_items
    assert items[:10] == [f"Mission Cargo {i}" for i in range(1, 11)]
    colours = {cargo_app.commodity_color(n) for n in items[:10]}
    assert len(colours) == 10
