"""SC Toolbox - Toolbox Assistant: the Assistant and SuitMk2 in one window, a tab each.

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

A PUSH-TO-TALK KEY EACH. Each tab has its own key (the Assistant's
"Mic key", SuitMk2's "Talk key"; defaults in shared/ptt_keys.py), and holding
one talks to that tool whichever tab is showing and with the window closed. A
tab has to exist to hear its key, so a few seconds after the window is up every
tab that was not built yet is built behind the one in front (warm_tabs). That
is the Assistant in the hidden start described above: its agent, voice and ears
now exist from about WARM_MS after launch instead of from its first Ctrl+3.

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

# How long after start-up the tabs nobody has opened are built, so their push-to-talk keys work. Late enough that
# the first tab's own start-up (SuitMk2 finding Game.log and waking its model service) is not competing with it.
WARM_MS = 4000

# THE SIZE. The window first shipped opening at 560x600, and at UI scale 1.5 the Suit Mk2
# tab was unreadable at that size: nine status rows 4 px high and drawn over each other, six button labels cut, and
# the title cut to "TOOLBOX ASSISTANT / A". SuitMk2's dashboard is a plain column with no scroll area, and a window's
# explicit minimum size overrides what its layout needs, so nothing stopped the window being smaller than its content.
#
# MIN_SIZE is the smallest window at which that tab can be read, MEASURED: the real SuitPanel laid out with the real
# fonts and the widest row texts seen live, at each size from 520 wide upward, checking every label, button, checkbox
# and combo for being narrower or shorter than it needs and for overlapping a neighbour:
#       UI scale 1.5    830 x 650          UI scale 1.0    860 x 640
# (800x740 still cuts "Keep training screenshots" and "Export training screenshots" at both scales.) 860x650 is
# clean at both, and the whole title fits from 800 up. The window cannot be made smaller, and a saved size that is
# smaller is raised when it is loaded. SCWindow lowers a minimum that does not fit the screen (1920x1080 at scale
# 1.5 is 1280x720 of these units).
MIN_SIZE = (860, 650)
DEFAULT_SIZE = (860, 740)       # used until the window has a saved size of its own; the extra height is the log's

# THE PLACE. Until it has a position the user chose, the window opens beside the launcher: both default to the
# same corner (100,100), and it first opened exactly on top of the launcher. What the first version saved when it
# was closed where it first appeared is that same corner at the old size, and is not a choice either.
OLD_FIRST_OPEN = (100, 100, 560, 600)
LAUNCHER_GAP = 12


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def opening_size(saved) -> tuple:
    """(w, h) to open at: the saved size, each side raised to MIN_SIZE; DEFAULT_SIZE when none was saved."""
    w, h = _int((saved or {}).get("w")), _int((saved or {}).get("h"))
    if w is None or h is None:
        return DEFAULT_SIZE
    return max(w, MIN_SIZE[0]), max(h, MIN_SIZE[1])


def position_is_the_users(saved) -> bool:
    """False while the window has never been put anywhere by the user: nothing saved, or what the first version
    saved at its first-open corner (OLD_FIRST_OPEN, give or take the few pixels a frame adds)."""
    x, y, w, h = (_int((saved or {}).get(k)) for k in ("x", "y", "w", "h"))
    if x is None or y is None:
        return False
    ox, oy, ow, oh = OLD_FIRST_OPEN
    return not (abs(x - ox) <= 4 and abs(y - oy) <= 4 and (w, h) == (ow, oh))


def as_rect(value):
    """(x, y, w, h) from what the launcher sent, or None if it is not a rectangle on a screen (the launcher parks
    itself at -32000,-32000 while a tool is open, when that setting is on)."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    r = tuple(_int(v) for v in value)
    if None in r or r[2] <= 0 or r[3] <= 0 or r[0] <= -30000 or r[1] <= -30000:
        return None
    return r


def launcher_rect(pending):
    """Where the launcher says it is, from commands queued before this window existed (a hotkey that had to start
    the tool); None when none says."""
    found = None
    for cmd in pending or []:
        if isinstance(cmd, dict) and cmd.get("type") in ("show", "toggle"):
            found = as_rect(cmd.get("launcher")) or found
    return found


def beside(launcher, size, screen, gap: int = LAUNCHER_GAP) -> tuple:
    """(x, y) for a window of *size* on *screen* (x, y, w, h: the part of the launcher's monitor a window may use).

    To the launcher's right with the tops level; to its left when the right has no room; fully on the screen
    either way. Centred on the screen when the launcher's place is not known, or when neither side has room (a
    small screen: the two cannot both be seen whole there, and centred is where a window is looked for)."""
    sx, sy, sw, sh = screen
    w, h = min(size[0], sw), min(size[1], sh)
    centre = (sx + (sw - w) // 2, sy + (sh - h) // 2)
    if not launcher:
        return centre
    lx, ly, lw, _lh = launcher
    y = max(sy, min(ly, sy + sh - h))
    if lx + lw + gap + w <= sx + sw:
        return lx + lw + gap, y
    if lx - gap - w >= sx:
        return lx - gap - w, y
    return centre


def _screen_of(launcher) -> tuple:
    """The usable rectangle of the monitor the launcher is on (the primary one when that is not known)."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication
    screen = None
    if launcher:
        screen = QGuiApplication.screenAt(QPoint(launcher[0] + launcher[2] // 2, launcher[1] + launcher[3] // 2))
    g = (screen or QGuiApplication.primaryScreen()).availableGeometry()
    return g.x(), g.y(), g.width(), g.height()


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

    def tip(settings_key: str, default: str, tool_dir: str) -> str:
        # What the tab's tool does (the "summary" in its skill.json, the launcher tile's text), then its hotkey.
        try:
            from shared.tool_tips import Section, read_summary, tooltip_text
            return tooltip_text([Section("", read_summary(tool_dir), "Hotkey: " + hotkey_label(settings_key, default))])
        except Exception:                          # noqa: BLE001 - a tooltip is not worth a failed start
            return ""

    tabs = [TabSpec(TAB_ASSISTANT, TAB_LABELS[TAB_ASSISTANT], assistant, tip("hotkey_assistant", "<ctrl>+3", HERE)),
            TabSpec(TAB_SUIT, TAB_LABELS[TAB_SUIT], suit, tip("hotkey_suitmk2", "<ctrl>+2", SUIT_DIR))]
    off = {t.strip() for t in os.environ.get("SC_TOOLBOX_TABS_OFF", "").split(",") if t.strip()}
    # Every tab switched off would be a window with nothing in it; something asked for this window, so show both.
    return [t for t in tabs if t.key not in off] or tabs


def warm_tabs(window) -> list:
    """Build every tab of *window* that does not exist yet, behind the one in front. Returns the keys it built.
    One tab failing to build is the window's business (it shows why on that tab) and does not stop the next."""
    built = []
    for key in window.tab_keys():
        if window.page(key) is None and window.warm(key):
            built.append(key)
    if built:
        log.info("toolbox assistant: built behind the front tab, for their push-to-talk keys: %s", ", ".join(built))
    return built


def show_tutorial(window):
    """The window's tutorial (assistant/tutorial.py), with a tab for each tool that has a tab in *window*."""
    from assistant import tutorial
    from shared.qt.theme import P
    from shared.qt.tutorial_popup import TutorialPopup
    return TutorialPopup.open("toolbox_assistant", window, title="Toolbox Assistant", accent=P.energy_cyan,
                              tabs=tutorial.tabs(window.tab_keys()))


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
    from PySide6.QtCore import QThread, QTimer
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

    # The launcher passes its generic 1300x800 until this window has saved a size of its own, so the size is taken
    # from the saved state itself (see THE SIZE and THE PLACE above).
    saved = load_window_state(os.path.splitext(os.path.basename(__file__))[0])
    w, h = opening_size(saved)

    from assistant.hub import HubWindow
    holder = {}                 # the button is made with the window; it needs the window when it is pressed
    window = HubWindow(title=tool_name(), tabs=build_tabs(cmd_file), first=first_tab(preload, pending),
                       width=w, height=h, min_width=MIN_SIZE[0], min_height=MIN_SIZE[1],
                       opacity=args["opacity"], accent=P.energy_cyan, icon_text="🤖", standalone=standalone,
                       extra_buttons=[("? Tutorial", lambda: show_tutorial(holder["window"]))])
    holder["window"] = window
    window.restore_geometry_from_args(args["x"], args["y"], w, h, args["opacity"])
    if not position_is_the_users(saved):
        def place(raw):
            launcher = as_rect(raw)
            return beside(launcher, (window.width(), window.height()), _screen_of(launcher))
        window.place_on_first_show(place)
    for line in check_module_names():
        log.error("toolbox assistant: %s", line)

    if not preload:
        # by hand, or a hotkey/tile press that had to start it; preload: stay hidden
        window.place_for_first_show(launcher_rect(pending))
        window.show()

    if not standalone:
        watcher = IPCWatcher(cmd_file, poll_ms=150)
        watcher.command_received.connect(window.handle_ipc_command)
        watcher.start(QThread.NormalPriority)
    for cmd in pending:         # anything queued before the watcher existed, in order (a "quit" included)
        if isinstance(cmd, dict):
            window.handle_ipc_command(cmd)

    app.aboutToQuit.connect(window._quit)
    QTimer.singleShot(WARM_MS, lambda: warm_tabs(window))
    log.info("toolbox assistant: up (tab=%s, hidden=%s)", window.current_tab(), preload)
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.getLogger("assistant").critical("FATAL crash", exc_info=True)
        sys.exit(1)
