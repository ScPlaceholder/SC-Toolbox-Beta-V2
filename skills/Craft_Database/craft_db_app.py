"""
SC Toolbox — Craft Database
Star Citizen crafting blueprint browser cloned from sc-craft.tools.
"""
import os
import sys

# ── Bootstrap (MUST be first) ──
sys.path.insert(
    0,
    os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    ),
)
from shared.app_bootstrap import bootstrap_skill

bootstrap_skill(__file__)

# ── Imports (after bootstrap) ──
from shared.crash_logger import init_crash_logging
from shared.platform_utils import set_dpi_awareness
from shared.data_utils import parse_cli_args
from shared.qt.theme import apply_theme

from PySide6.QtWidgets import QApplication

from ui.app import CraftDatabaseApp


def main():
    log = init_crash_logging("craft_db")
    try:
        set_dpi_awareness()
        parsed = parse_cli_args(sys.argv[1:], {"w": 1300, "h": 800})

        app = QApplication(sys.argv)
        app.setApplicationName("SC Toolbox \u2014 Craft Database")
        apply_theme(app)

        window = CraftDatabaseApp(
            x=parsed["x"],
            y=parsed["y"],
            w=parsed["w"],
            h=parsed["h"],
            opacity=parsed["opacity"],
            cmd_file=parsed["cmd_file"],
        )
        window.show()
        sys.exit(app.exec())
    except Exception:
        log.critical("FATAL crash", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
