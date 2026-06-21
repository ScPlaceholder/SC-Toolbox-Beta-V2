"""Standalone harness for the star map — lets us verify rendering, idle CPU and
interaction in isolation, before any Trade Hub wiring.

Run from the Trade_Hub directory:

    python -m starmap                 # live window
    python -m starmap --selftest      # offscreen render -> starmap/selftest.png
    python -m starmap --smoke         # drive synthetic input, catch crashes
    python -m starmap --auto-close 8000
"""
import faulthandler
import os
import sys
import traceback

# Repo root is three levels up: starmap -> Trade_Hub -> skills -> <root>.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer   # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent            # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

from shared.qt.theme import P, apply_theme                       # noqa: E402

from .data import Galaxy, load_state, save_state                 # noqa: E402
from .galaxy_view import GalaxyView                              # noqa: E402

_CRASH_LOG = os.path.join(_HERE, "starmap_crash.log")


def _install_crash_log() -> None:
    """Persist any uncaught exception / fatal fault so a vanished window leaves a trail."""
    try:
        faulthandler.enable(open(_CRASH_LOG, "a", encoding="utf-8"))
    except OSError:
        faulthandler.enable()

    def _hook(et, ev, tb):
        try:
            with open(_CRASH_LOG, "w", encoding="utf-8") as f:
                traceback.print_exception(et, ev, tb, file=f)
        except OSError:
            pass
        traceback.print_exception(et, ev, tb)

    sys.excepthook = _hook


def _build_view() -> GalaxyView:
    view = GalaxyView(Galaxy.load())
    view.apply_state(load_state().get("galaxy"))
    return view


def _selftest(out_path: str) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    view = _build_view()
    w, h = 1280, 820
    view.resize(w, h)
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor(P.bg_deepest))
    view.render(img)
    ok = img.save(out_path)
    print(f"selftest render {'OK' if ok else 'FAILED'} -> {out_path}")
    print(f"systems={len(view._galaxy.systems)} edges={len(view._edges)}")


def _smoke() -> None:
    """Drive every interaction path through synthetic events to catch crashes
    the render-only selftest can't reach."""
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    W, H = 1000, 700
    view = _build_view()
    view.resize(W, H)
    img = QImage(W, H, QImage.Format_ARGB32)

    def repaint():
        view.render(img)

    def mev(t, x, y, btn, btns):
        return QMouseEvent(t, QPointF(x, y), QPointF(x, y), btn, btns, Qt.NoModifier)

    steps = []
    try:
        steps.append("initial-paint"); repaint()

        steps.append("hover")
        view.mouseMoveEvent(mev(QEvent.MouseMove, 500, 350, Qt.NoButton, Qt.NoButton)); repaint()

        steps.append("rotate-drag")
        view.mousePressEvent(mev(QEvent.MouseButtonPress, 500, 350, Qt.LeftButton, Qt.LeftButton))
        view.mouseMoveEvent(mev(QEvent.MouseMove, 560, 400, Qt.LeftButton, Qt.LeftButton)); repaint()
        view.mouseReleaseEvent(mev(QEvent.MouseButtonRelease, 560, 400, Qt.LeftButton, Qt.NoButton))

        steps.append("right-drag-pan")
        view.mousePressEvent(mev(QEvent.MouseButtonPress, 500, 350, Qt.RightButton, Qt.RightButton))
        view.mouseMoveEvent(mev(QEvent.MouseMove, 520, 360, Qt.RightButton, Qt.RightButton)); repaint()
        view.mouseReleaseEvent(mev(QEvent.MouseButtonRelease, 520, 360, Qt.RightButton, Qt.NoButton))

        steps.append("zoom-extremes")
        view._cam.zoom = 11.0; repaint()
        view._cam.zoom = 0.16; repaint()
        view._cam.zoom = 1.0; repaint()

        steps.append("click-select-stanton")
        st = view._galaxy.get("STANTON")
        base = view._base_scale(W, H)
        sx, sy, _ = view._cam.project(st.x, st.y, st.z, W / 2.0, H / 2.0, base)
        view.mousePressEvent(mev(QEvent.MouseButtonPress, sx, sy, Qt.LeftButton, Qt.LeftButton))
        view.mouseReleaseEvent(mev(QEvent.MouseButtonRelease, sx, sy, Qt.LeftButton, Qt.NoButton)); repaint()

        steps.append("state-roundtrip")
        view.apply_state(view.get_state())

        app.processEvents()
        print("SMOKE PASS: " + " -> ".join(steps))
        print(f"selected={view._selected!r} zoom={view._cam.zoom:.3f}")
    except Exception:
        print("SMOKE FAIL at step: " + (steps[-1] if steps else "init"))
        traceback.print_exc()
        sys.exit(1)


def _live(auto_close_ms: int = 0) -> None:
    _install_crash_log()
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    view = _build_view()
    view.setWindowTitle("SC Toolbox — Star Map (M1 galaxy)")
    view.resize(1280, 820)
    view.show()

    def _persist() -> None:
        st = load_state()
        st["galaxy"] = view.get_state()
        save_state(st)

    app.aboutToQuit.connect(_persist)
    if auto_close_ms > 0:
        QTimer.singleShot(auto_close_ms, app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        i = args.index("--selftest")
        out = args[i + 1] if len(args) > i + 1 else os.path.join(_HERE, "selftest.png")
        _selftest(out)
    elif "--smoke" in args:
        _smoke()
    elif "--auto-close" in args:
        i = args.index("--auto-close")
        ms = int(args[i + 1]) if len(args) > i + 1 else 8000
        _live(auto_close_ms=ms)
    else:
        _live()
