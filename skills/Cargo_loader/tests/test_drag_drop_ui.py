"""In-process drag-and-drop test for the Cargo Loader iso view.

Runs offscreen (QT_QPA_PLATFORM=offscreen). Mouse and key events are
synthesised and delivered straight to the widget with QApplication.sendEvent
/ QTest — nothing reaches the OS input queue. No network, no settings writes.

Set CARGO_DND_SHOTS=<dir> to also save the green- and red-ghost screenshots.
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
    "name": "DnD Test Ship", "manufacturer": "Test", "capacity": 96,
    "provenance": {"source": "scunpacked-data"},
    "groups": [{"x": 0, "z": 0, "grids": [
        {"x": 0, "z": 0, "width": 6, "height": 2, "length": 8,
         "minSize": 1, "maxSize": 32},
    ]}],
}


@pytest.fixture
def app_window(monkeypatch):
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import cargo_app
    import shared.qt.base_window as bw
    # No network, no geometry/state files written.
    monkeypatch.setattr(cargo_app, "_fetch_uex_commodities", lambda: None)
    monkeypatch.setattr(cargo_app.ShipDataLoader, "load_async", lambda self, cb: None)
    monkeypatch.setattr(bw, "_save_window_state", lambda w: None)
    win = cargo_app.CargoApp(0, 0, 1000, 700, 1.0, None)
    win._commodity_poll.stop()
    monkeypatch.setattr(win._data, "find", lambda name: dict(SHIP))
    win.show()
    win._load_ship("DnD Test Ship")
    # One 16 SCU box (2x2x4, not a cube, so rotation is visible) and nothing else
    for s, sb in win._spinboxes.items():
        sb.setValue(0)
    win._spinboxes[16].setValue(1)
    QTest.qWait(80)
    win._render_grid()
    qapp.processEvents()
    yield win, qapp
    win.hide()
    win.deleteLater()
    qapp.processEvents()


def _send(view, etype, pos: QPoint, button, buttons):
    vp = view.viewport()
    ev = QMouseEvent(etype, QPointF(pos), QPointF(vp.mapToGlobal(pos)),
                     button, buttons, Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(vp, ev)


def _view_point(win, wx, wy, wz) -> QPoint:
    sx, sy = win._renderer._pt(wx, wy, wz)
    return win._view.mapFromScene(QPointF(sx, sy))


def _shot(win, name):
    d = os.environ.get("CARGO_DND_SHOTS")
    if d:
        os.makedirs(d, exist_ok=True)
        win.grab().save(os.path.join(d, name))


def test_drag_box_to_new_cell(app_window):
    win, qapp = app_window
    boxes = win._renderer._last_boxes
    assert len(boxes) == 1
    x, y, z, w, h, l, size = boxes[0]
    assert size == 16 and (w, h, l) == (2, 2, 4)

    view = win._view
    L, NB = Qt.LeftButton, Qt.NoButton
    grab = _view_point(win, x + w / 2, y + h, z + l / 2)       # top-face centre
    _send(view, QEvent.MouseButtonPress, grab, L, L)
    # Move 3 cells along +X on the same (top) plane: legal -> green ghost
    tgt = _view_point(win, x + w / 2 + 3, y + h, z + l / 2)
    _send(view, QEvent.MouseMove, grab + QPoint(3, 3), NB, L)
    _send(view, QEvent.MouseMove, tgt, NB, L)
    qapp.processEvents()
    ghost = win._renderer._ghost
    assert ghost is not None and ghost.ghost_valid is True
    assert win._drag["result"][0] == (x + 3, 0, z)
    _shot(win, "cargo_dnd_green_ghost.png")

    # R rotates the box 90 deg about the vertical while dragging
    QTest.keyClick(view, Qt.Key_R)
    assert win._drag is not None and win._drag["active"]
    assert win._drag["dims"] == (4, 2, 2)
    # right-click rotates it back
    _send(view, QEvent.MouseButtonPress, tgt, Qt.RightButton, L | Qt.RightButton)
    assert win._drag["dims"] == (2, 2, 4)
    QTest.keyClick(view, Qt.Key_R)

    # Off the side of the grid -> red ghost
    out = _view_point(win, x + w / 2 - 4, y + h, z + l / 2)   # left of the grid, still on screen
    _send(view, QEvent.MouseMove, out, NB, L)
    qapp.processEvents()
    assert win._renderer._ghost.ghost_valid is False
    _shot(win, "cargo_dnd_red_ghost.png")

    # Back to the legal spot and drop
    _send(view, QEvent.MouseMove, tgt, NB, L)
    _send(view, QEvent.MouseButtonRelease, tgt, L, NB)
    QTest.qWait(30)
    qapp.processEvents()
    # rotated about its centre: centre (4, 2) with a 4x2 footprint -> (2, 0, 1)
    assert win._renderer._manual_boxes == [(2, 0, 1, 4, 2, 2, 16)]
    assert win._renderer._ghost is None

    # Saved plan carries the moved position; counts unchanged
    payload = win._loadout_payload()
    assert payload["boxes"] == [{"scu": 16, "pos": [2, 0, 1], "dims": [4, 2, 2]}]
    assert payload["counts"]["16"] == 1

    # Undo puts it back to the auto-packed arrangement
    win._undo_move()
    assert win._renderer._manual_boxes is None
    assert win._renderer._last_boxes == [(x, y, z, w, h, l, 16)]


def test_invalid_drop_and_escape_leave_box_home(app_window):
    win, qapp = app_window
    x, y, z, w, h, l, _ = win._renderer._last_boxes[0]
    view = win._view
    L, NB = Qt.LeftButton, Qt.NoButton
    grab = _view_point(win, x + w / 2, y + h, z + l / 2)

    # Drop outside the grid: rejected, box returns
    _send(view, QEvent.MouseButtonPress, grab, L, L)
    out = _view_point(win, x + w / 2 - 4, y + h, z + l / 2)   # left of the grid, still on screen
    _send(view, QEvent.MouseMove, grab + QPoint(4, 4), NB, L)
    _send(view, QEvent.MouseMove, out, NB, L)
    _send(view, QEvent.MouseButtonRelease, out, L, NB)
    QTest.qWait(30)
    assert win._renderer._manual_boxes is None
    assert "cancelled" in win._status_lbl.text().lower()

    # Esc mid-drag cancels a legal move too
    grab = _view_point(win, x + w / 2, y + h, z + l / 2)
    tgt = _view_point(win, x + w / 2 + 2, y + h, z + l / 2 + 2)
    _send(view, QEvent.MouseButtonPress, grab, L, L)
    _send(view, QEvent.MouseMove, grab + QPoint(4, 4), NB, L)
    _send(view, QEvent.MouseMove, tgt, NB, L)
    assert win._renderer._ghost is not None
    QTest.keyClick(view, Qt.Key_Escape)
    assert win._drag is None and win._renderer._ghost is None
    _send(view, QEvent.MouseButtonRelease, tgt, L, NB)
    QTest.qWait(30)
    assert win._renderer._manual_boxes is None


def test_saved_boxes_restore_on_load(app_window):
    win, qapp = app_window
    x, y, z, w, h, l, _ = win._renderer._last_boxes[0]
    payload = {"version": 1, "ship": SHIP["name"], "rotation": 0,
               "counts": {"8": 1}, "assignments": [],
               "boxes": [{"scu": 8, "pos": [4, 0, 6], "dims": [2, 2, 2]}]}
    win._pending_loadout = payload
    win._load_ship(SHIP["name"])
    QTest.qWait(30)
    assert win._renderer._last_boxes == [(4, 0, 6, 2, 2, 2, 8)]
