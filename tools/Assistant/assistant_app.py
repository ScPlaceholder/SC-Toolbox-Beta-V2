"""SC Toolbox — AI Assistant.

The voice-driven LLM copilot for the whole toolbox. Launched as a
subprocess by the launcher like any other skill.

Args: <x> <y> <w> <h> <opacity> <cmd_file>

The LLM endpoint is not hardcoded anywhere: it is read from
~/.sctoolbox/assistant_llm.json (see assistant/config.py), so Elah plugs
in whatever server by editing that file or the Settings dialog.
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

from PySide6.QtWidgets import QApplication  # noqa: E402

from shared.crash_logger import init_crash_logging  # noqa: E402
from shared.qt.base_window import SCWindow  # noqa: E402
from shared.qt.ipc_thread import IPCWatcher  # noqa: E402
from shared.qt.theme import apply_theme  # noqa: E402

BASE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _safe_arg(argv, i, default, type_fn):
    try:
        return type_fn(argv[i])
    except (IndexError, ValueError, TypeError):
        return default


def main() -> int:
    log = init_crash_logging("assistant")
    argv = sys.argv[1:]
    # The launcher appends the IPC command file as the LAST argument; with
    # no custom_args configured for this skill it is argv[0]. Detect it by
    # name rather than a fixed index so custom_args can be added later.
    cmd_file = ""
    if argv and (argv[-1].endswith(".jsonl") or "sc_toolbox" in argv[-1]):
        cmd_file = argv[-1]
        argv = argv[:-1]
    opacity = _safe_arg(argv, 4, 0.95, float)

    app = QApplication(sys.argv)
    apply_theme(app)

    from assistant.panel import AssistantWindow

    win = AssistantWindow(BASE_DIR, opacity=opacity)

    # ── launcher IPC (show / hide / toggle / quit) ───────────────────────
    if cmd_file and cmd_file != os.devnull:
        watcher = IPCWatcher(cmd_file)

        def _dispatch(cmd: dict) -> None:
            t = cmd.get("type", "")
            if t == "quit":
                app.quit()
            elif t == "show":
                win.show()
                win.raise_()
            elif t == "hide":
                win.hide()
            elif t == "toggle":
                win.show() if win.isHidden() else win.hide()

        watcher.command_received.connect(_dispatch)
        watcher.start()

    win.show()
    log.info("assistant: up (base_dir=%s)", BASE_DIR)
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.getLogger("assistant").critical("FATAL crash", exc_info=True)
        sys.exit(1)
