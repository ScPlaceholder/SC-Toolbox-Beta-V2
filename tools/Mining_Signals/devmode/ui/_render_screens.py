"""Render every Dev Mode step to a PNG using the demo backend.

    python devmode/ui/_render_screens.py [out_dir]

Uses the NATIVE platform plugin (offscreen has no system fonts) but never
puts a window on screen (WA_DontShowOnScreen). Output defaults to
tools/Mining_Signals/devmode/_screens/step_<n>_<name>.png.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
SKILL = HERE.parents[2]
ROOT = HERE.parents[4]
for p in (str(ROOT), str(SKILL)):
    if p not in sys.path:
        sys.path.insert(0, p)


def main(out_dir: str | None = None) -> int:
    os.environ["SC_DEVMODE_FAKE"] = "1"
    os.environ.setdefault("SC_DEVMODE_FAKE_DELAY", "0")
    os.environ.setdefault("SC_DEVMODE_ROOT", tempfile.mkdtemp(prefix="sc_devmode_screens_"))

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication

    from shared.qt.theme import apply_theme
    from devmode.ui._fake import FakeBackend
    from devmode.ui.backend import BackendHandle
    from devmode.ui.window import DevModeWindow
    from devmode.ui.workers import drain

    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    out = Path(out_dir) if out_dir else SKILL / "devmode" / "_screens"
    out.mkdir(parents=True, exist_ok=True)

    fake = FakeBackend(delay=0, torch_installed=False)
    win = DevModeWindow(handle=BackendHandle(fake, "SC_DEVMODE_FAKE=1 (demo data)"))
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.resize(1200, 820)
    win.show()
    drain()

    def shot(n: int, name: str, prep=None) -> Path:
        win.select_step(n)
        drain()
        if prep:
            prep()
            drain()
        app.processEvents()
        raw = win.grab()
        # The window is translucent; composite onto a dark "desktop" so the
        # PNG looks like it does over the game.
        pm = QPixmap(raw.size())
        pm.fill(QColor("#101318"))
        p = QPainter(pm)
        p.drawPixmap(0, 0, raw)
        p.end()
        path = out / f"step_{n + 1}_{name}.png"
        pm.save(str(path))
        print(path)
        return path

    shot(0, "engine")
    shot(1, "capture")
    shot(2, "label")
    shot(3, "glyphs", lambda: win.pages[3].set_char("3"))
    shot(4, "synth", lambda: win.pages[4].generate())

    # Train: once locked (no engine), once after installing + training.
    shot(5, "train_locked")

    def trained():
        win.engine_page.install()
        drain()
        tp = win.train_page
        tp.train("signal_rgb")
        tp.train("hud")
        drain()
        tp.select("signal_rgb")
    shot(5, "train", trained)

    def zipped():
        win.export_page.create_zip(str(Path(os.environ["SC_DEVMODE_ROOT"]) / "export"))
    shot(6, "export", zipped)
    win.close()
    drain()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
