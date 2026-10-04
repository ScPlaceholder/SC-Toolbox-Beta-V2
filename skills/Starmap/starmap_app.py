"""SC Toolbox — Starmap.

The combined standalone star map: the full Trade Hub galaxy -> system ->
planet scenes plus the Market Finder terminal items browsing, grocery list
and multi-stop shopping routes, with a command bar. It has no microphone:
voice input lives in the AI Assistant, which relays map commands here.

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

from PySide6.QtWidgets import QApplication  # noqa: E402

from shared.crash_logger import init_crash_logging  # noqa: E402
from shared.data_utils import parse_cli_args  # noqa: E402
from shared.platform_utils import boost_responsiveness  # noqa: E402
from shared.qt.base_window import SCWindow  # noqa: E402

from shared.qt.theme import P, apply_theme  # noqa: E402
from shared.qt.title_bar import SCTitleBar  # noqa: E402

from starmap.panel import StarmapPanel  # noqa: E402

log = logging.getLogger(__name__)


def main() -> None:
    init_crash_logging("starmap")
    boost_responsiveness()
    parsed = parse_cli_args(sys.argv[1:], {"w": 1280, "h": 820})

    app = QApplication(sys.argv)
    app.setApplicationName("SC Toolbox - Starmap")
    apply_theme(app)

    win = SCWindow(title="SC Toolbox - Starmap",
                   width=parsed["w"], height=parsed["h"],
                   min_w=640, min_h=420,
                   opacity=parsed["opacity"],
                   accent=P.energy_cyan)
    def _show_tutorial() -> None:
        # Imported here so a tutorial import error can never stop the map
        # itself from opening.
        from starmap.tutorial import TutorialPopup
        TutorialPopup(win)

    tb = SCTitleBar(win, title="STAR MAP", icon_text="\u2726",
                    accent_color=P.energy_cyan, show_minimize=True,
                    extra_buttons=[("? Tutorial", _show_tutorial)])
    tb.minimize_clicked.connect(win.showMinimized)
    tb.close_clicked.connect(win.close)
    win.content_layout.addWidget(tb)

    panel = StarmapPanel(cmd_file=parsed["cmd_file"] or "")
    win.content_layout.addWidget(panel, 1)
    win.move(parsed["x"], parsed["y"])
    win.show()

    # Persist map state and close its bubbles / watchers on exit.
    app.aboutToQuit.connect(panel.shutdown)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
