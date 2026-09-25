"""SC Toolbox - SuitMk2 companion (Elah, the suit AI, and Montaigne, the ship AI).

Reads Star Citizen's Game.log, decides what is worth saying, has a LOCAL model word it (model service on
127.0.0.1:7790, started by this tool), refuses any line that is not grounded in the game's facts, and speaks it in
two local Piper voices. Nothing leaves the PC.

Launched by skill_launcher with `preload: true`: it starts hidden with the toolbox and runs all session; the window
(Ctrl+2) is a dashboard, and closing it only hides it.

Args: <x> <y> <w> <h> <opacity> <cmd_file>
"""
from __future__ import annotations

import os
import sys

# ── Bootstrap (MUST be first) ──
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402

bootstrap_skill(__file__)


def main() -> None:
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from shared.config_models import WindowGeometry
    from shared.crash_logger import init_crash_logging
    from shared.data_utils import parse_cli_args
    from shared.qt.ipc_thread import IPCWatcher
    from shared.qt.theme import apply_theme

    from ui.suit_window import SuitWindow

    init_crash_logging("suitmk2")
    args = parse_cli_args(sys.argv[1:], defaults={"w": 520, "h": 460})

    app = QApplication(sys.argv)
    app.setApplicationName("SC Toolbox - SuitMk2")
    apply_theme(app)

    geometry = WindowGeometry(x=args["x"], y=args["y"], w=args["w"], h=args["h"], opacity=args["opacity"])
    window = SuitWindow(geometry=geometry, hotkey_text="Ctrl+2", cmd_file=args.get("cmd_file"))

    if not os.environ.get("SC_TOOLBOX_PRELOAD"):
        window.show()          # standalone / explicit launch: show the dashboard; preload: run hidden

    if args.get("cmd_file"):
        watcher = IPCWatcher(args["cmd_file"], poll_ms=150)
        watcher.command_received.connect(window.handle_ipc_command)
        watcher.start(QThread.NormalPriority)

    app.aboutToQuit.connect(window._quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
