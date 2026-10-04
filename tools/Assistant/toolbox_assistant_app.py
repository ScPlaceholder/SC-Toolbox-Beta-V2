"""SC Toolbox - Toolbox Assistant: the Assistant and SuitMk2 in one window, a tab each.

J, 2026-10-04: "Can we combine toolbox assistant and suit mk2 under the same
tool and have a tab for each under the tool".

One launcher tile, one process, one window (assistant/hub.py), two tabs:

    Assistant   the voice-driven copilot      Ctrl+3   (assistant/panel.py, AssistantPanel)
    Suit Mk2    Elah and Montaigne            Ctrl+2   (tools/SuitMk2/ui/suit_window.py, SuitPanel)

Both tools still run on their own from their old entry scripts
(assistant_app.py, tools/SuitMk2/suitmk2_companion_app.py); tests and
development use those. The launcher starts THIS one, and SuitMk2's skill.json
says ``"tab_of": "assistant"`` so its hotkey opens this window on its tab
instead of starting a second SuitMk2.

Started hidden by the launcher (SuitMk2's skill.json still says
``"preload": true``; the launcher starts the window that tool is a tab of, with
env SC_TOOLBOX_PRELOAD), exactly as SuitMk2 was, so the companions follow
Game.log all session. In that state only the Suit Mk2 tab exists: the
Assistant's agent, voice and ears are built the first time its tab is asked for
(Ctrl+3 or the tile), which is when the Assistant used to start as a process of
its own.

A tool the user switched off in the launcher's Settings does not come back as a
tab: the launcher names the disabled ones in env SC_TOOLBOX_TABS_OFF.

Args: <x> <y> <w> <h> <opacity> <cmd_file>

ONE PROCESS, TWO TOOLS: THE MODULE NAMES. SuitMk2 imports its core by bare
top-level names (``import settings``, ``speech``, ``sidecar`` ...) from
tools/SuitMk2/core, and its window as ``ui.suit_window``; the Assistant is a
package (``assistant.*``). Checked before choosing this design (2026-10-04, by
listing what each sys.path root provides and what each side imports):

  ui     tools/SuitMk2/ui and the launcher's <root>/ui. Nothing in this process
         imports the launcher's; tools/SuitMk2 is put ahead of the root on
         sys.path, the same thing SuitMk2's own entry script does. _suit_panel_class
         refuses to go on if some other ``ui`` is already loaded.
  core   <root>/core is a package (the Assistant imports core.skill_registry);
         tools/SuitMk2/core is a plain folder with no __init__.py, and SuitMk2
         never imports it as ``core``. A real package always wins over such a
         folder whatever the order, and check_module_names() says so loudly if
         that ever stops being true (someone adds an __init__.py there).
  none of SuitMk2's bare names (settings, speech, conversation, feedback ...)
  is imported by the Assistant or by shared/, and none of the Assistant's
  modules is top-level.
"""
from __future__ import annotations

import json
import os
import sys

# ── Bootstrap (MUST be first) ──
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402

bootstrap_skill(__file__)

import logging  # noqa: E402

import shared.path_setup  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
SUIT_DIR = os.path.join(BASE_DIR, "tools", "SuitMk2")

# SuitMk2's folder ahead of the toolbox root, so ``ui`` is SuitMk2's (see the docstring).
shared.path_setup.ensure_path(SUIT_DIR, first=True)

log = logging.getLogger("assistant.hub_app")

# The tab keys are the launcher's skill ids: the launcher sends {"type": "show", "tab": <skill id>}.
TAB_ASSISTANT = "assistant"
TAB_SUIT = "suitmk2"
TAB_LABELS = {TAB_ASSISTANT: "Assistant", TAB_SUIT: "Suit Mk2"}

# The window's name is the "name" in skill.json beside this file (the same text as the launcher tile), so
# renaming the tool is that one line. This is only what is shown if that file cannot be read.
FALLBACK_NAME = "Toolbox Assistant"

# Which tab holds the microphone while the launcher has started the window hidden and nobody has opened it yet.
# SuitMk2: that is what was listening in that state before the two were combined (the Assistant was not running
# until its hotkey was pressed).
PRELOAD_TAB = TAB_SUIT

DEFAULT_SIZE = (560, 600)       # used until the window has a saved size of its own


def tool_name() -> str:
    try:
        with open(os.path.join(HERE, "skill.json"), encoding="utf-8") as f:
            name = str(json.load(f).get("name") or "").strip()
        return name or FALLBACK_NAME
    except (OSError, ValueError, AttributeError):
        return FALLBACK_NAME


def _under(path: str, folder: str) -> bool:
    try:
        a, b = os.path.normcase(os.path.abspath(path or "")), os.path.normcase(os.path.abspath(folder))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def check_module_names() -> list:
    """What is wrong with the shared module names in this process, as sentences; [] when nothing is.

    Not import-order luck: this is the check that the two facts the design rests on still hold."""
    problems = []
    try:
        import core
        if not _under(getattr(core, "__file__", "") or "", os.path.join(BASE_DIR, "core")):
            problems.append("'core' is not the toolbox's core package (%r); the Assistant's tool launcher needs "
                            "core.skill_registry" % (getattr(core, "__file__", None),))
    except ImportError as exc:
        problems.append("the toolbox's core package cannot be imported: %s" % exc)
    ui = sys.modules.get("ui")
    if ui is not None and not _under(getattr(ui, "__file__", "") or "", SUIT_DIR):
        problems.append("a 'ui' package that is not SuitMk2's is already loaded (%r)" % (getattr(ui, "__file__", None),))
    return problems


def _suit_panel_class():
    """SuitMk2's SuitPanel. Raises, rather than load the wrong ``ui`` or half a SuitMk2."""
    problems = check_module_names()
    if problems:
        raise ImportError("SuitMk2 cannot share this process: " + "; ".join(problems))
    from ui.suit_window import SuitPanel
    return SuitPanel


def build_tabs(cmd_file):
    """The two tabs, in the order they are shown."""
    from shared.hotkey_label import hotkey_label
    from assistant.hub import TabSpec

    def assistant(mic: bool):
        from assistant.panel import AssistantPanel
        return AssistantPanel(BASE_DIR, mic=mic)

    def suit(mic: bool):
        return _suit_panel_class()(cmd_file=cmd_file, mic=mic)

    def tip(settings_key: str, default: str) -> str:
        try:
            return "Hotkey: " + hotkey_label(settings_key, default)
        except Exception:                          # noqa: BLE001 - a tooltip is not worth a failed start
            return ""

    tabs = [TabSpec(TAB_ASSISTANT, TAB_LABELS[TAB_ASSISTANT], assistant, tip("hotkey_assistant", "<ctrl>+3")),
            TabSpec(TAB_SUIT, TAB_LABELS[TAB_SUIT], suit, tip("hotkey_suitmk2", "<ctrl>+2"))]
    off = {t.strip() for t in os.environ.get("SC_TOOLBOX_TABS_OFF", "").split(",") if t.strip()}
    # Every tab switched off would be a window with nothing in it; something asked for this window, so show both.
    return [t for t in tabs if t.key not in off] or tabs


def first_tab(preload: bool, pending: list) -> str:
    """The tab to build first.

    Hidden start by the launcher: SuitMk2 (PRELOAD_TAB). Otherwise the Assistant, unless the launcher already
    asked for a tab: a hotkey pressed while this tool was not running starts it AND queues
    {"type": "show", "tab": ...}, and building the Assistant first only to leave it would open its microphone
    for a moment for nothing."""
    tab = PRELOAD_TAB if preload else TAB_ASSISTANT
    for cmd in pending or []:
        if isinstance(cmd, dict) and cmd.get("type") == "show" and cmd.get("tab") in TAB_LABELS:
            tab = cmd["tab"]
    return tab


def main() -> int:
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from shared.crash_logger import init_crash_logging
    from shared.data_utils import parse_cli_args
    from shared.ipc import ipc_read_and_clear
    from shared.qt.base_window import load_window_state
    from shared.qt.ipc_thread import IPCWatcher
    from shared.qt.theme import P, apply_theme

    init_crash_logging("assistant")
    args = parse_cli_args(sys.argv[1:], defaults={"w": DEFAULT_SIZE[0], "h": DEFAULT_SIZE[1]})
    cmd_file = args.get("cmd_file")
    standalone = not cmd_file or cmd_file == os.devnull
    # Taken OUT of the environment, not just read: the Assistant starts other tools itself (ipc_bus.spawn_skill)
    # and they inherit this process's environment. Left in, every tool it opened would start hidden.
    preload = bool(os.environ.pop("SC_TOOLBOX_PRELOAD", ""))

    app = QApplication(sys.argv)
    app.setApplicationName("SC Toolbox - " + tool_name())
    app.setQuitOnLastWindowClosed(False)    # hidden is this window's normal state; _quit is the only way out
    apply_theme(app)

    pending = []
    if not standalone:
        try:
            pending = ipc_read_and_clear(cmd_file)
        except (OSError, ValueError):
            pending = []

    # The launcher passes its generic 1300x800 until this window has saved a size of its own.
    w, h = args["w"], args["h"]
    if not load_window_state(os.path.splitext(os.path.basename(__file__))[0]):
        w, h = DEFAULT_SIZE

    from assistant.hub import HubWindow
    window = HubWindow(title=tool_name(), tabs=build_tabs(cmd_file), first=first_tab(preload, pending),
                       width=w, height=h, opacity=args["opacity"], accent=P.energy_cyan,
                       icon_text="🤖", standalone=standalone)
    window.restore_geometry_from_args(args["x"], args["y"], w, h, args["opacity"])
    for line in check_module_names():
        log.error("toolbox assistant: %s", line)

    if not preload:
        window.show()           # by hand, or a hotkey/tile press that had to start it; preload: stay hidden

    if not standalone:
        watcher = IPCWatcher(cmd_file, poll_ms=150)
        watcher.command_received.connect(window.handle_ipc_command)
        watcher.start(QThread.NormalPriority)
    for cmd in pending:         # anything queued before the watcher existed, in order (a "quit" included)
        if isinstance(cmd, dict):
            window.handle_ipc_command(cmd)

    app.aboutToQuit.connect(window._quit)
    log.info("toolbox assistant: up (tab=%s, hidden=%s)", window.current_tab(), preload)
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.getLogger("assistant").critical("FATAL crash", exc_info=True)
        sys.exit(1)
