"""Pico Pals: the launcher's entry point for Pico (sprite_pal.py is the window itself).

The launcher starts every tool the same way (core/process_manager.py):

    <python> <script> <x> <y> <w> <h> [custom_args...] <opacity> <cmd_file>

and afterwards talks to it through <cmd_file>, a JSONL file it appends {"type": "show"},
{"type": "hide"} and {"type": "quit"} to. sprite_pal.py has its own argparse CLI (--log, --loops,
--demo, --mood) and rejects those positionals with exit code 2 ("unrecognized arguments"), so a tile
pointed straight at it would die on start and never answer show / hide / quit. This file is the
adapter, and sprite_pal.py keeps its CLI exactly as it was for a terminal.

What is done with what the launcher passes:

  x y w h, opacity   IGNORED. They are the launcher's default for a 1300x800 tool window. Pico is a
                     280px sprite that remembers where it was dragged to and how big it is in its own
                     settings (%APPDATA%/PicoPal/settings.json), and it is drawn at full opacity.
  custom_args        passed to sprite_pal.py unchanged, so a skill.json can say
                     "custom_args": ["--demo"] or ["--loops", "<folder>"].
  cmd_file           watched: show -> Pico appears, hide -> Pico disappears (still running, still
                     tailing the log), quit -> Pico exits. If the file vanishes the launcher is gone
                     and Pico exits too, like every other tool (shared/qt/ipc_thread.py).

Game.log: the install folder the toolbox already knows (shared/sc_install.py, the one the first-launch
prompt saves) is used when it has a Game.log; otherwise sprite_pal.py falls back to its own default
paths. Nothing is detected or saved here, only read.

If Pico cannot start, this exits non-zero with one plain sentence on stderr. The launcher captures a
tool's stderr in logs/pico.log and shows that log when a tool dies with a non-zero code, so the reason
reaches the user instead of a tile that silently does nothing:

    3  no animation loops on disk (the art is not part of the install, see sprites.DEFAULT_DIR)
    4  an import failed on this interpreter (PySide6, or a file missing from the tool)

By hand, flags go straight through:   py pico_pals_app.py --demo
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))

EXIT_NO_ART = 3
EXIT_IMPORT = 4


def split_launcher_argv(argv: list[str]) -> tuple[list[str], str | None]:
    """(arguments for sprite_pal.py, command file or None) from what this script was started with.

    The launcher's form is recognised by its four leading integers; anything else (no arguments, or
    flags typed by hand) is handed to sprite_pal.py untouched with no command file."""
    lead = argv[:4]
    if len(lead) < 4 or not all(_is_int(a) for a in lead):
        return list(argv), None
    rest = argv[4:]
    if len(rest) >= 2:                      # [custom_args...] opacity cmd_file
        return list(rest[:-2]), rest[-1]
    return [], None                         # geometry (and maybe opacity) only: no command file


def _is_int(text: str) -> bool:
    try:
        int(text)
        return True
    except (TypeError, ValueError):
        return False


def toolbox_game_log() -> str | None:
    """The newest Game.log under the install folder saved in the toolbox's shared settings, or None."""
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from shared import sc_install
        return sc_install.newest_game_log(sc_install.get_sc_root())
    except Exception:                       # noqa: BLE001 - optional lookup; sprite_pal has its own defaults
        return None


def _fail(code: int, message: str) -> int:
    print("Pico Pals cannot start: " + message, file=sys.stderr, flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pal_args, cmd_file = split_launcher_argv(argv)

    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import sprite_pal
        from pico import sprites
    except ImportError as exc:
        return _fail(EXIT_IMPORT, "this Python (%s) cannot import what Pico needs: %s"
                     % (sys.executable, exc))

    wants_log = not any(a in ("--demo", "--mood", "--log") or a.startswith(("--mood=", "--log="))
                        for a in pal_args)
    if wants_log:
        log_path = toolbox_game_log()
        if log_path:
            pal_args += ["--log", log_path]

    def on_ready(app, pal) -> None:
        if not cmd_file:
            return
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from PySide6.QtCore import QObject, Slot
        from shared.qt.ipc_thread import IPCWatcher

        class Commands(QObject):
            """Lives on the GUI thread (parent: the window), so the watcher thread's signal is
            queued to it and the window is only ever touched from the thread that owns it."""

            @Slot(dict)
            def handle(self, cmd: dict) -> None:
                kind = cmd.get("type")
                if kind == "show":
                    pal.show()
                    pal.raise_()
                elif kind == "hide":
                    pal.hide()
                elif kind == "quit":
                    app.quit()

        commands = Commands(pal)
        watcher = IPCWatcher(cmd_file, poll_ms=150)
        watcher.command_received.connect(commands.handle)
        watcher.start()
        pal._ipc_watcher = watcher          # keep a reference: a collected QThread stops watching

    try:
        return sprite_pal.main(pal_args, on_ready=on_ready)
    except sprites.SpriteError as exc:
        return _fail(EXIT_NO_ART, "%s. Pico's animation loops are not installed on this machine "
                     "(they are expected under %s)." % (exc, sprites.DEFAULT_DIR))


if __name__ == "__main__":
    raise SystemExit(main())
