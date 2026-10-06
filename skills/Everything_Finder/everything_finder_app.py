"""SC Toolbox -- Everything Finder (entry point).

Item Finder, Trade Hub and Star Map as three lazily-loaded tabs in one window,
plus a shared shopping list for items and commodities.

Launched as a subprocess by skill_launcher.py.
Args: <x> <y> <w> <h> <opacity> <cmd_file>
"""
from __future__ import annotations

import os
import sys

# Bootstrap (MUST be first)
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402

bootstrap_skill(__file__)

from PySide6.QtWidgets import QApplication  # noqa: E402

from shared.crash_logger import init_crash_logging  # noqa: E402
from shared.data_utils import parse_cli_args  # noqa: E402
from shared.platform_utils import set_dpi_awareness  # noqa: E402
from shared.qt.theme import apply_theme  # noqa: E402

from everything_finder.window import EverythingFinderWindow  # noqa: E402


def main() -> None:
    log = init_crash_logging("everything_finder")
    try:
        set_dpi_awareness()
        parsed = parse_cli_args(sys.argv[1:], {"w": 1400, "h": 900})
        app = QApplication(sys.argv)
        app.setApplicationName("SC Toolbox - Everything Finder")
        apply_theme(app)
        win = EverythingFinderWindow(
            x=parsed["x"], y=parsed["y"], w=parsed["w"], h=parsed["h"],
            opacity=parsed["opacity"], cmd_file=parsed["cmd_file"])
        win.show()
        sys.exit(app.exec())
    except Exception:
        log.critical("FATAL crash in everything_finder main()", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
