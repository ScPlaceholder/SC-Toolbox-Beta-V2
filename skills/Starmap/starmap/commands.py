r"""CommandRouter - turns a line of text into a map action.

The text comes from the command bar, or from the AI Assistant, which owns the
microphone and relays what it heard (IPC ``map_command``). The Star Map itself
does not listen: this module was starmap/voice/commands.py until 2026-10-04,
when voice-to-text moved into the Assistant.

Built-in commands cover map navigation, routing and the shopping list.
``register()`` is the public extension point: repurposed WingmanAI /
set_route logic plugs in there, e.g.::

    router.register(r"scan(?:\s+the)?\s+(.+)", handler, "scan <target>")

Handlers receive the regex match object and may return a string, which is
shown on the status line and handed back to whoever sent the command.
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

#: Reply files the Assistant asks the map to answer into. A command arrives
#: over a file any local process can write, so the path it names is only
#: honoured when it is one of these, in the temp folder.
REPLY_PREFIX = "sc_toolbox_reply_"
REPLY_SUFFIX = ".jsonl"


def safe_reply_file(path) -> str:
    """*path* if it is a reply file in the temp folder, else ""."""
    if not isinstance(path, str) or not path:
        return ""
    norm = os.path.normcase(os.path.abspath(path))
    base = os.path.basename(norm)
    tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    if os.path.dirname(norm) != tmp:
        return ""
    if not (base.startswith(REPLY_PREFIX) and base.endswith(REPLY_SUFFIX)):
        return ""
    return path


class CommandRouter(QObject):
    unrecognized = Signal(str)

    def __init__(self, panel: QObject, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._panel = panel
        self._handlers: List[Tuple[re.Pattern, Callable, str]] = []
        self._register_builtin()

    # ── public extension point ─────────────────────────────────────────────
    def register(self, pattern: str, handler: Callable,
                 help_text: str = "") -> None:
        """Add (or replace) a command. *handler* receives the match."""
        rx = re.compile(pattern, re.IGNORECASE)
        self._handlers = [h for h in self._handlers if h[0].pattern != rx.pattern]
        self._handlers.append((rx, handler, help_text))

    def help_lines(self) -> List[str]:
        return [h[2] for h in self._handlers if h[2]]

    # ── dispatch ───────────────────────────────────────────────────────────
    def run(self, text: str) -> Tuple[bool, str]:
        """Run *text*. Returns ``(understood, message)``; never raises.

        ``understood`` is False only when no command matched (``unrecognized``
        is emitted too). A handler that fails is still "understood": the
        message says what went wrong."""
        text = (text or "").strip()
        if not text:
            return False, ""
        norm = re.sub(r"[^\w\s]", " ", text.lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        for rx, handler, _help in self._handlers:
            m = rx.search(norm)
            if m:
                try:
                    msg = handler(m)
                except Exception as exc:
                    return True, "command error: %s" % exc
                return True, msg if isinstance(msg, str) else ""
        self.unrecognized.emit(text)
        return False, "did not understand: '%s'" % text

    def dispatch(self, text: str) -> bool:
        """Run *text* and put the outcome on the panel's status line."""
        ok, msg = self.run(text)
        if ok and msg:
            self._panel.voice_status(msg)
        return ok

    # ── built-ins ──────────────────────────────────────────────────────────
    def _register_builtin(self) -> None:
        p = self._panel
        # set_route_ai port: registered FIRST so "set route to X" wins over
        # the plain "route to X" map-routing command below.
        self.register(r"\bset\s+(?:the\s+)?route\s+to\s+(.+)",
                      lambda m: p.cmd_set_route(m.group(1)), "set route to <destination>")
        # Destination -> full in-game route via the set_route port:
        # "navigate to Area 18" mirrors the map, confirms and walks the
        # in-game macro when In-Game is toggled on.
        self.register(r"\b(?:navigate\s+to|set\s+course\s+to|plot\s+(?:a\s+)?course\s+to)\s+(.+)$",
                      lambda m: p.cmd_set_route(m.group(1)),
                      "navigate to <destination> (plots it in game)")
        # Side views.
        self.register(r"\bcommodities?\b",
                      lambda m: p.cmd_toggle_commodities(), "commodities view")
        self.register(r"\bmarket(?:\s+finder)?\b",
                      lambda m: p.cmd_toggle_market(), "market finder view")
        self.register(r"\bhelp\b",
                      lambda m: p.cmd_help(), "help")
        self.register(r"\b(?:set\s+)?route\s+to\s+(.+)",
                      lambda m: p.cmd_route_to(m.group(1)), "route to <system>")
        self.register(r"\bclear(?:\s+the)?\s+route\b",
                      lambda m: p.cmd_clear_route(), "clear route")
        self.register(r"\b(?:take\s+me\s+)?home\b",
                      lambda m: p.cmd_home(), "home")
        self.register(r"\bback\s+to\s+(?:the\s+)?galaxy\b|\bgalaxy\b|\bstar\s*map\b",
                      lambda m: p.cmd_galaxy(), "back to galaxy")
        self.register(r"\bzoom\s+(in|out)\b",
                      lambda m: p.cmd_zoom(m.group(1) == "in"), "zoom in / out")
        self.register(r"\b(?:go\s+)?back\b|\bup\s+one\b",
                      lambda m: p.cmd_back(), "back")
        # "grocery" is the list's old name; both words reach the one shopping list.
        self.register(r"\b(?:open|close|toggle|show|hide)(?:\s+the)?\s+(?:grocery|shopping)\b",
                      lambda m: p.cmd_toggle_grocery(), "toggle shopping list")
        self.register(r"\b(?:go(?:\s+to)?|show(?:\s+me)?|find|navigate\s+to|"
                      r"take\s+me\s+to)\s+([a-z0-9][\w .]*?)\s*$",
                      lambda m: p.cmd_goto(m.group(1)),
                      "go to <system or location>")
