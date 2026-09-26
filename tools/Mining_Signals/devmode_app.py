"""Mining Signals Dev Mode — train the OCR on your own monitor.

Standalone:   python devmode_app.py          (SC_DEVMODE_FAKE=1 for demo data)
From a tool:  from devmode_app import open_dev_mode; open_dev_mode(parent=self)
"""

from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_SKILL_DIR, "..", ".."))
for _p in (_PROJECT_ROOT, _SKILL_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def open_dev_mode(parent=None):
    """Open (or raise) the single Dev Mode window. Needs a running QApplication."""
    from devmode.ui.window import open_dev_mode as _open
    return _open(parent=parent)


def main() -> int:
    from shared.app_bootstrap import bootstrap_skill
    bootstrap_skill(__file__)
    from shared.crash_logger import init_crash_logging
    log = init_crash_logging("mining_signals_devmode")

    from PySide6.QtWidgets import QApplication
    from shared.qt.theme import apply_theme

    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    try:
        win = open_dev_mode()
    except Exception:
        log.critical("Dev Mode failed to open", exc_info=True)
        return 1
    win.destroyed.connect(app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
