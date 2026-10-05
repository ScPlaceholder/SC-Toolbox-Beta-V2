"""ptt_overlay.py - which AI is listening, shown on screen while a push-to-talk key is held.

J, 2026-10-05: "For the assistant and suit Mk 2 can you have individual push to talk buttons which also
auto-route to the right ai?" Each tool has its own key and holding one talks to that tool from anywhere, so the
pilot holding it is in the game and not looking at either tab. This is the one line that tells him which of the
two is hearing him, and afterwards what it heard or why it could not.

A small strip at the top centre of the screen the mouse is on (the game's screen), always on top. It never takes
the keyboard or the mouse: it is shown without being activated, cannot be focused, and clicks pass through it to
whatever is underneath. A game running in exclusive fullscreen draws over every window, this one included; in
windowed or borderless mode it shows.

    show_state(who, what, text, key)

    what          shown                                              for
    listening     ● <who> is listening - release <key> to send       while the key is held
    released      <who>: working...                                  until something below replaces it
    heard         You, to <who>: "<text>"                            a few seconds
    reply         <who>: <text>                                      a few seconds
    note          <who>: <text>                                      a few seconds
    error         <who> did not hear you: <text>                     longer, in the warning colour

Every state times out, the held one included (the ears stop a held key at 12 s): a strip must never be left on
the screen because some later event did not arrive.
"""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from shared.qt.theme import P

# how long each state stays up, ms
_MS = {"listening": 15000, "released": 8000, "heard": 4000, "reply": 8000, "note": 3500, "error": 7000}
_MAX_CHARS = 200


def strip_text(who: str, what: str, text: str = "", key: str = "") -> str:
    """The line for one state; "" for a state this strip does not show."""
    text = " ".join((text or "").split())
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS - 1].rstrip() + "…"
    if what == "listening":
        return "● %s is listening" % who + ("  -  release %s to send" % key if key else "")
    if what == "released":
        return "%s: working..." % who
    if what == "heard":
        return 'You, to %s: "%s"' % (who, text)
    if what in ("reply", "note"):
        return "%s: %s" % (who, text) if text else ""
    if what == "error":
        return "%s did not hear you: %s" % (who, text or "the microphone did not open")
    return ""


class PttOverlay(QWidget):
    def __init__(self) -> None:
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.last: Optional[Tuple[str, str, str]] = None      # (who, what, the line shown), for whoever asks
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel("", self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setWordWrap(True)
        lay.addWidget(self._label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_state(self, who: str, what: str, text: str = "", key: str = "") -> None:
        line = strip_text(who, what, text, key)
        if not line:
            return
        self.last = (who, what, line)
        colour = P.yellow if what == "error" else (P.green if what == "listening" else P.energy_cyan)
        self._label.setStyleSheet(
            f"color: {P.fg_bright}; background: rgba(8, 12, 18, 225); border: 2px solid {colour}; "
            f"border-radius: 6px; padding: 8px 18px; font-family: Consolas; font-size: 12pt;")
        self._label.setText(line)
        self._place()
        self.show()
        self._timer.start(_MS.get(what, 4000))

    def _place(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        g = screen.availableGeometry()
        width = min(900, max(320, int(g.width() * 0.6)))
        self.setFixedWidth(width)
        self.adjustSize()
        self.move(g.x() + (g.width() - width) // 2, g.y() + max(8, int(g.height() * 0.05)))
