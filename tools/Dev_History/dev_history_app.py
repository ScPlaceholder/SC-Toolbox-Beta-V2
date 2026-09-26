"""SC Toolbox — Dev History.

Search Star Citizen's development history (dev-video transcripts + RSI
comm-links), streamed from GitHub by the shared ``sc_dev_history`` engine.
Runs standalone or as a subprocess launched by skill_launcher.

Args: <x> <y> <w> <h> <opacity> <cmd_file>
"""
from __future__ import annotations

import os
import sys

# ── Bootstrap (MUST be first) ──
sys.path.insert(
    0,
    os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    ),
)
from shared.app_bootstrap import bootstrap_skill  # noqa: E402

bootstrap_skill(__file__)

import logging  # noqa: E402

log = logging.getLogger(__name__)


def main() -> None:
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from shared.config_models import WindowGeometry
    from shared.crash_logger import init_crash_logging
    from shared.data_utils import parse_cli_args
    from shared.qt.ipc_thread import IPCWatcher
    from shared.qt.theme import apply_theme

    from ui.dev_history_window import DevHistoryWindow

    init_crash_logging("dev_history")
    args = parse_cli_args(sys.argv[1:], defaults={"w": 1100, "h": 720})

    app = QApplication(sys.argv)
    app.setApplicationName("SC Toolbox - Dev History")
    apply_theme(app)

    geometry = WindowGeometry(
        x=args["x"], y=args["y"], w=args["w"], h=args["h"], opacity=args["opacity"])

    window = DevHistoryWindow(
        geometry=geometry, hotkey_text="Shift+H", cmd_file=args.get("cmd_file"))
    window.show()

    if args.get("cmd_file"):
        watcher = IPCWatcher(args["cmd_file"], poll_ms=150)
        watcher.command_received.connect(window.handle_ipc_command)
        watcher.start(QThread.NormalPriority)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
