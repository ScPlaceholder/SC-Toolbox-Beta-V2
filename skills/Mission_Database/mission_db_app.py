"""
Star Citizen Mission Database -- standalone PySide6 GUI.
Data from scmdb.net static JSON endpoints.
Launched as a subprocess by the WingmanAI Mission_Database skill (main.py).

Usage:
    python mission_db_app.py <x> <y> <w> <h> <opacity> <cmd_file>
"""
import os
import sys

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from PySide6.QtWidgets import QApplication
from shared.qt.theme import apply_theme
from shared.data_utils import parse_cli_args


def main():
    from shared.crash_logger import init_crash_logging
    log = init_crash_logging("missions")
    try:
        d = parse_cli_args(sys.argv[1:], defaults={"w": 1300, "h": 800})

        app = QApplication(sys.argv)
        apply_theme(app)

        from ui.app import MissionDBApp
        window = MissionDBApp(
            d["x"], d["y"], d["w"], d["h"], d["opacity"],
            d["cmd_file"] or os.devnull,
        )
        window.show()
        sys.exit(app.exec())
    except Exception:
        log.critical("FATAL crash in missions main()", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
