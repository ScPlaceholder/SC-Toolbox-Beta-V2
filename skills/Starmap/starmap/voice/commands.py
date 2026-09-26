r"""CommandRouter — turns transcripts into panel actions.

Built-in commands cover map navigation, routing, the grocery list and
ears control. ``register()`` is the public extension point: repurposed
WingmanAI / set_route voice logic plugs in there, e.g.::

    router.register(r"scan(?:\s+the)?\s+(.+)", handler, "scan <target>")

Handlers receive the regex match object and may return a string, which
the panel shows as a status message.
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal


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
        """Add (or replace) a voice command. *handler* receives the match."""
        rx = re.compile(pattern, re.IGNORECASE)
        self._handlers = [h for h in self._handlers if h[0].pattern != rx.pattern]
        self._handlers.append((rx, handler, help_text))

    def help_lines(self) -> List[str]:
        return [h[2] for h in self._handlers if h[2]]

    # ── dispatch ───────────────────────────────────────────────────────────
    def dispatch(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        norm = re.sub(r"[^\w\s]", " ", text.lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        for rx, handler, _help in self._handlers:
            m = rx.search(norm)
            if m:
                try:
                    msg = handler(m)
                    if isinstance(msg, str) and msg:
                        self._panel.voice_status(msg)
                except Exception as exc:
                    self._panel.voice_status("command error: %s" % exc)
                return True
        self.unrecognized.emit(text)
        return False

    # ── built-ins ──────────────────────────────────────────────────────────
    def _register_builtin(self) -> None:
        p = self._panel
        # set_route_ai port: registered FIRST so "set route to X" wins over
        # the plain "route to X" map-routing command below.
        self.register(r"\bset\s+(?:the\s+)?route\s+to\s+(.+)",
                      lambda m: p.cmd_set_route(m.group(1)), "set route to <destination>")
        # Spoken destination -> full in-game route via the set_route port:
        # "navigate to Area 18" mirrors the map, speaks the confirmation and
        # walks the in-game macro when In-Game is toggled on.
        self.register(r"\b(?:navigate\s+to|set\s+course\s+to|plot\s+(?:a\s+)?course\s+to)\s+(.+)$",
                      lambda m: p.cmd_set_route(m.group(1)),
                      "navigate to <destination> (plots it in game)")
        # Side views.
        self.register(r"\bcommodities?\b",
                      lambda m: p.cmd_toggle_commodities(), "commodities view")
        self.register(r"\bmarket(?:\s+finder)?\b",
                      lambda m: p.cmd_toggle_market(), "market finder view")
        self.register(r"\b(ears?\s+off|stop\s+listening|go\s+to\s+sleep)\b",
                      lambda m: p.cmd_ears_off(), "ears off / stop listening")
        self.register(r"\bhelp\b",
                      lambda m: p.cmd_voice_help(), "help")
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
        self.register(r"\b(?:open|close|toggle)(?:\s+the)?\s+grocery\b",
                      lambda m: p.cmd_toggle_grocery(), "toggle grocery list")
        self.register(r"\b(?:go(?:\s+to)?|show(?:\s+me)?|find|navigate\s+to|"
                      r"take\s+me\s+to)\s+([a-z0-9][\w .]*?)\s*$",
                      lambda m: p.cmd_goto(m.group(1)),
                      "go to <system or location>")
