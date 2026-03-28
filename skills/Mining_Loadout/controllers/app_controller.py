"""Application controller — orchestrates startup, IPC, and shutdown."""
import argparse
import logging
import os
import queue
import sys
import threading
from logging.handlers import RotatingFileHandler

import shared.path_setup  # noqa: E402  # centralised path config
from shared.platform_utils import set_dpi_awareness

from services.ipc_service import start_ipc_reader

_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mining_loadout.log",
)


def setup_logging() -> logging.Logger:
    """Configure rotating file handler for the application."""
    lg = logging.getLogger("MiningLoadout")
    lg.setLevel(logging.DEBUG)
    if not lg.handlers:
        fh = RotatingFileHandler(
            _LOG_PATH, maxBytes=1_500_000, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        lg.addHandler(fh)
    return lg


def parse_args(argv: list) -> argparse.Namespace:
    """Parse CLI arguments with argparse (fixes the float-parsing bug)."""
    parser = argparse.ArgumentParser(description="Mining Loadout GUI")
    parser.add_argument("x", type=int, nargs="?", default=80, help="Window X position")
    parser.add_argument("y", type=int, nargs="?", default=80, help="Window Y position")
    parser.add_argument("w", type=int, nargs="?", default=1200, help="Window width")
    parser.add_argument("h", type=int, nargs="?", default=720, help="Window height")
    parser.add_argument("opacity", type=float, nargs="?", default=0.95, help="Window opacity")
    parser.add_argument("cmd_file", type=str, nargs="?", default=None, help="IPC command file path")
    return parser.parse_args(argv)


def run_app(argv: list) -> None:
    """Main application entry point."""
    log = setup_logging()
    log.info("=" * 60)
    log.info("Mining Loadout starting — Python %s", sys.version.split()[0])
    log.info("Script: %s", os.path.abspath(__file__))

    # DPI awareness before any window creation
    set_dpi_awareness()

    # Parse CLI args (argparse handles cmd_file as string, not float)
    args = parse_args(argv)
    log.info("Args: x=%s y=%s w=%s h=%s opacity=%s cmd_file=%s",
             args.x, args.y, args.w, args.h, args.opacity, args.cmd_file)

    cmd_queue = queue.Queue()
    stop_evt = threading.Event()

    if args.cmd_file:
        start_ipc_reader(args.cmd_file, cmd_queue, stop_evt)

    # PySide6 application
    from PySide6.QtWidgets import QApplication
    from shared.qt.theme import apply_theme

    app = QApplication(sys.argv)
    apply_theme(app)

    from ui.main_window import MiningLoadoutWindow

    window = MiningLoadoutWindow(
        cmd_queue=cmd_queue,
        win_x=args.x,
        win_y=args.y,
        win_w=args.w,
        win_h=args.h,
        refresh_interval=86400.0,
        opacity=args.opacity,
    )
    window.run()

    exit_code = app.exec()
    stop_evt.set()
    sys.exit(exit_code)
