"""SC Toolbox — Mouse Blocker.

Fullscreen frameless overlay that absorbs all mouse input so accidental
clicks never reach Star Citizen underneath.  The body is fully
transparent visually with bright glowing red edges and a slowly pulsing
'MOUSE BLOCKER ACTIVE' label in the centre.

Launched as a subprocess by skill_launcher.py.
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

# ── Imports (after bootstrap) ──
import logging  # noqa: E402

from PySide6.QtCore import QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from shared.crash_logger import init_crash_logging  # noqa: E402
from shared.data_utils import parse_cli_args  # noqa: E402
from shared.platform_utils import boost_responsiveness  # noqa: E402
from shared.qt.ipc_thread import IPCWatcher  # noqa: E402
from shared.qt.theme import apply_theme  # noqa: E402

from ui.app import BlockerWindow  # noqa: E402

log = logging.getLogger(__name__)


def main() -> None:
    init_crash_logging("mouse_blocker")
    # Mark this process as latency-sensitive *before* we allocate any Qt
    # objects: bumps priority class to ABOVE_NORMAL and tells Windows
    # not to aggressively trim our working set when other tools (e.g.
    # Mining Signals' OCR) put memory under pressure.  Without this,
    # the first show after a long idle stalls on disk page-faults
    # paging the rendering code back in.
    boost_responsiveness()
    args = parse_cli_args(sys.argv[1:])

    app = QApplication(sys.argv)
    app.setApplicationName("SC Toolbox - Mouse Blocker")
    apply_theme(app)

    window = BlockerWindow(opacity=args["opacity"], hotkey_text="Shift+0")

    if os.environ.get("SC_TOOLBOX_PRELOAD"):
        # Pre-warm path (launcher pre-spawn).  Off-screen show()+hide()
        # so Windows DWM allocates the layered-window backing store
        # now, in the background.  Subsequent IPC-triggered show()s
        # reuse those cached resources and are sub-100ms instead of
        # ~900ms cold.
        #
        # We deliberately use a tiny 200x100 buffer here, NOT a
        # fullscreen-sized one.  An earlier iteration sized the
        # pre-warm window to the full primary screen on the theory
        # that this would avoid a DWM realloc on first real show —
        # but in practice it left the layered window's backing store
        # in a state where the first paint didn't reach the display
        # surface, so the window was "visible" (Qt + Win32 agreed) but
        # the user saw nothing.  The small placeholder pre-warm,
        # combined with _cover_primary_screen resizing on the real
        # show, has been reliable.
        from PySide6.QtCore import QTimer  # noqa: E402
        window.move(-32000, -32000)
        window.resize(200, 100)
        window.show()
        # 250ms is enough for DWM to commit the 200x100 layered-window
        # buffer on Windows 10/11.  After hide(), the buffer stays
        # cached.
        #
        # Critical: only hide if we are STILL in pre-warm mode.  If a
        # user toggle's IPC 'show' arrived during the pre-warm window
        # and already flipped _prewarming to False (via the show's own
        # showEvent path that completed real setup), this timer must
        # be a no-op — otherwise it would hide the window the user
        # just opened.  See BlockerWindow.hideEvent for how
        # _prewarming is cleared on the first hide.
        def _prewarm_hide_if_still_warming() -> None:
            if getattr(window, "_prewarming", False):
                window.hide()
        QTimer.singleShot(250, _prewarm_hide_if_still_warming)
    else:
        # Standalone launch — show immediately at full geometry.
        window.show()

    if args.get("cmd_file"):
        # 50ms poll instead of the 300ms default — average IPC latency
        # drops from ~150ms to ~25ms.  Negligible idle CPU cost (just a
        # file stat plus sleep) and a meaningful win on hotkey-to-show
        # latency for an overlay the user expects to feel instant.
        watcher = IPCWatcher(args["cmd_file"], poll_ms=50)
        watcher.command_received.connect(window.handle_ipc_command)
        # HighPriority ensures the IPC reader gets scheduled ahead of
        # background threads in other processes (e.g. Mining Signals'
        # OCR worker pool) under contention.
        watcher.start(QThread.HighPriority)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
