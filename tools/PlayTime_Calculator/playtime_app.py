"""SC Toolbox — PlayTime Calculator.

Scans Star Citizen's Game.log + logbackups to compute total play time and an
interactive breakdown (highlights, time-of-day, per-day/month/year trends,
session log).  Runs standalone or as a subprocess launched by skill_launcher.

Args: <x> <y> <w> <h> <opacity> <cmd_file>

Pass ``--headless`` to scan and refresh the launcher summary cache without
opening a window (used by the launcher to keep the main-menu total fresh).
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


def run_headless() -> int:
    """Scan + recompute + write the summary cache, no GUI.  Pure-Python engine."""
    from core import log_scanner, analytics, settings as st
    folder = log_scanner.get_or_detect_folder()
    if not folder:
        return 0
    sessions = log_scanner.scan(folder)
    s = st.load_settings()
    cap = float(s.get("session_cap_hours", 0) or 0)
    if cap > 0:
        sessions = log_scanner.apply_cap(sessions, cap)
    a = analytics.build_analytics(sessions)
    st.write_summary(analytics.build_summary(a, s.get("time_format", "hours"), folder))
    return 0


def main() -> None:
    if "--headless" in sys.argv[1:]:
        from shared.crash_logger import init_crash_logging
        init_crash_logging("playtime")
        sys.exit(run_headless())

    # ── GUI path ──
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from shared.config_models import WindowGeometry
    from shared.crash_logger import init_crash_logging
    from shared.data_utils import parse_cli_args
    from shared.qt.ipc_thread import IPCWatcher
    from shared.qt.theme import apply_theme

    from ui.playtime_window import PlayTimeWindow

    init_crash_logging("playtime")
    args = parse_cli_args(sys.argv[1:], defaults={"w": 1100, "h": 760})

    app = QApplication(sys.argv)
    app.setApplicationName("SC Toolbox - PlayTime Calculator")
    apply_theme(app)

    geometry = WindowGeometry(
        x=args["x"], y=args["y"], w=args["w"], h=args["h"], opacity=args["opacity"])

    window = PlayTimeWindow(
        geometry=geometry, hotkey_text="Ctrl+4", cmd_file=args.get("cmd_file"))
    window.show()

    if args.get("cmd_file"):
        watcher = IPCWatcher(args["cmd_file"], poll_ms=150)
        watcher.command_received.connect(window.handle_ipc_command)
        watcher.start(QThread.NormalPriority)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
