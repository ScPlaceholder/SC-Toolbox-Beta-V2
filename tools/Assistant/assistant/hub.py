"""HubWindow - one window, one tab per tool.

J, 2026-10-04: "Can we combine toolbox assistant and suit mk2 under the same
tool and have a tab for each under the tool". This is the window; the tabs are
the two tools' own HUDs, built without their title bars (AssistantPanel in
panel.py, SuitPanel in tools/SuitMk2/ui/suit_window.py). What goes in which tab
is decided by the entry script (toolbox_assistant_app.py): this module knows
nothing about either tool, which is also what lets its tests run with stand-in
tabs and no microphone, model or game log.

Rules the window keeps, each with a test in tests/test_hub_window.py:

  one microphone   Both tools have ears of their own. Only the tab that is
                   showing may listen: selecting a tab takes the microphone
                   from every other tab (mic_release) BEFORE it gives it to
                   the new one (mic_take), so there is no moment with two.
                   A hidden window does not move the microphone: it stays
                   with the tab that was last in front, which is what each
                   tool did on its own (both kept listening while hidden).
  lazy tabs        A tab is built the first time it is selected. The launcher
                   starts this window hidden so SuitMk2 can follow Game.log;
                   the Assistant's agent, voice and ears are not built until
                   its tab is first asked for.
  show / hide      Every tab that exists is told when the window is shown or
                   hidden (host_visibility_changed). SuitMk2's companions use
                   it to be silent while the window is not open; a tab that
                   is not in front gets no show/hide event of its own.
  the launcher     {"type": "show", "tab": "<key>"} selects that tab and
                   shows the window (core/process_manager.py show_tab).
                   A hotkey or the tile sends {"type": "toggle", "tab":
                   "<key>"} (toggle_tab; Ctrl+3 is tab "assistant", Ctrl+2
                   is tab "suitmk2"): hidden, or on another tab, the window
                   shows that tab; already showing it, the window hides.
                   The choice is made HERE, from what the window really is,
                   because the launcher is not told about the X or about a
                   click on a tab.
  closing          The X hides the window when the launcher started it (the
                   companions keep following the game), and quits when it was
                   started by hand.
  quit             Once only. Every tab that exists is shut down (shutdown),
                   then Qt is asked to exit.

A tab is any QWidget. The methods above are optional: a tab that has no
microphone simply does not define mic_take / mic_release.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QWidget,
)

from shared.qt.base_window import SCWindow, _save_window_state
from shared.qt.theme import P
from shared.qt.title_bar import SCTitleBar

log = logging.getLogger(__name__)

# The window of this process, if it is a HubWindow. Tools running inside one of
# its tabs use show_tab() to bring another tab forward ("open the suit" said to
# the Assistant) instead of starting that tool a second time.
_LIVE: Optional["HubWindow"] = None


class TabSpec:
    """One tab: its key (what the launcher sends), the text on its button, and
    how to build it. ``build(mic)`` returns the tab's widget; ``mic`` is True
    when the tab is the one in front as it is built, so it may listen."""

    def __init__(self, key: str, label: str, build: Callable[[bool], QWidget],
                 tooltip: str = "") -> None:
        self.key = key
        self.label = label
        self.build = build
        self.tooltip = tooltip


def hosted_tabs() -> List[str]:
    """Keys of the tabs of this process's window; [] when it has no HubWindow."""
    return list(_LIVE.tab_keys()) if _LIVE is not None else []


def show_tab(key: str) -> bool:
    """Bring tab *key* of this process's window forward. Safe from any thread
    (the request crosses to the GUI thread as a queued signal). False when
    this process has no such tab."""
    if _LIVE is None or key not in _LIVE.tab_keys():
        return False
    _LIVE.tabRequested.emit(key)
    return True


def _tab_ss(accent: str) -> str:
    return (
        f"QPushButton {{ background: transparent; color: {P.fg_dim}; border: none; "
        f"border-bottom: 2px solid transparent; padding: 5px 16px; "
        f"font-family: Consolas; font-size: 10pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; }} "
        f"QPushButton:checked {{ color: {accent}; border-bottom: 2px solid {accent}; }}"
    )


class HubWindow(SCWindow):
    """One window with a row of tabs; see the module docstring for the rules."""

    tabRequested = Signal(str)

    def __init__(self, title: str, tabs: Sequence[TabSpec], first: str = "",
                 width: int = 560, height: int = 560, opacity: float = 0.95,
                 accent: str = "", icon_text: str = "", standalone: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(title=title, width=width, height=height, min_w=420, min_h=360,
                         opacity=opacity, accent=accent or P.energy_cyan, parent=parent)
        if not tabs:
            raise ValueError("a HubWindow needs at least one tab")
        self._specs: Dict[str, TabSpec] = {t.key: t for t in tabs}
        self._order = [t.key for t in tabs]
        self._pages: Dict[str, QWidget] = {}
        self._current = ""
        self._standalone = bool(standalone)
        self._quitting = False
        accent = accent or P.energy_cyan

        tb = SCTitleBar(window=self, title=title.upper(), icon_text=icon_text,
                        accent_color=accent, show_minimize=True)
        tb.minimize_clicked.connect(self.showMinimized)
        tb.close_clicked.connect(self._on_close)
        self.content_layout.addWidget(tb)

        row = QHBoxLayout()
        row.setContentsMargins(10, 2, 10, 0)
        row.setSpacing(2)
        self._buttons: Dict[str, QPushButton] = {}
        for spec in tabs:
            b = QPushButton(spec.label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_tab_ss(accent))
            if spec.tooltip:
                b.setToolTip(spec.tooltip)
            b.clicked.connect(lambda _c=False, k=spec.key: self.select(k))
            row.addWidget(b)
            self._buttons[spec.key] = b
        row.addStretch(1)
        self.content_layout.addLayout(row)

        self._stack = QStackedWidget(self)
        self.content_layout.addWidget(self._stack, 1)

        self.tabRequested.connect(self._show_tab)
        global _LIVE
        _LIVE = self
        self.select(first if first in self._specs else self._order[0])

    # ── tabs ─────────────────────────────────────────────────────────────
    def tab_keys(self) -> List[str]:
        return list(self._order)

    def current_tab(self) -> str:
        return self._current

    def page(self, key: str) -> Optional[QWidget]:
        """The tab's widget, or None while it has not been built yet."""
        return self._pages.get(key)

    def select(self, key: str) -> bool:
        """Bring tab *key* to the front and give it the microphone. Builds the
        tab if this is the first time. False for a key this window has no tab
        for (the window is left as it was)."""
        if key not in self._specs:
            log.warning("hub: no tab %r (tabs: %s)", key, ", ".join(self._order))
            return False
        for k, b in self._buttons.items():
            b.setChecked(k == key)
        if key == self._current:
            return True
        # Take the microphone from every other tab FIRST, so there is never a
        # moment with two tabs listening.
        for k, page in self._pages.items():
            if k != key:
                self._call(page, "mic_release", k)
        page = self._pages.get(key)
        if page is None:
            page = self._build(key)             # built already holding the mic
        else:
            self._call(page, "mic_take", key)
        self._current = key
        self._stack.setCurrentWidget(page)
        return True

    def _build(self, key: str) -> QWidget:
        try:
            page = self._specs[key].build(True)
        except Exception as exc:                # noqa: BLE001 - one tab failing must not take the other down
            log.exception("hub: tab %r could not be built", key)
            page = QLabel("%s could not be opened:\n%s: %s\n\nThe log has the details."
                          % (self._specs[key].label, type(exc).__name__, exc))
            page.setWordWrap(True)
            page.setAlignment(Qt.AlignCenter)
            page.setStyleSheet(f"color: {P.yellow}; font-family: Consolas; font-size: 10pt; "
                               f"background: transparent; padding: 20px;")
        self._pages[key] = page
        self._stack.addWidget(page)
        return page

    @staticmethod
    def _call(page: QWidget, method: str, key: str) -> None:
        """Call an optional method of a tab. A tab that raises is logged, and
        the others still get their call."""
        fn = getattr(page, method, None)
        if fn is None:
            return
        try:
            fn()
        except Exception:                       # noqa: BLE001 - see the docstring
            log.exception("hub: tab %r failed in %s()", key, method)

    def _tell_tabs(self, method: str) -> None:
        for key, page in list(self._pages.items()):
            self._call(page, method, key)

    # ── shown / hidden ───────────────────────────────────────────────────
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._tell_tabs("host_visibility_changed")

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._tell_tabs("host_visibility_changed")

    def _show_tab(self, key: str = "") -> None:
        if key:
            self.select(key)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ── launcher IPC ─────────────────────────────────────────────────────
    def handle_ipc_command(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        tab = str(cmd.get("tab") or "")
        if t == "show":
            self._show_tab(tab)
        elif t == "hide":
            self.hide()
        elif t == "toggle":
            if self.isHidden() or (tab and tab != self._current):
                self._show_tab(tab)
            else:
                self.hide()
        elif t == "quit":
            self._quit()

    # ── lifecycle ────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        if self._standalone:
            self._quit()
        else:
            _save_window_state(self)    # hide is not close: SCWindow only saves on closeEvent
            self.hide()             # the tabs keep running; the launcher's quit ends them

    def _quit(self) -> None:
        # Once. This is wired from the launcher's "quit", from the X when
        # standalone, and from QApplication.aboutToQuit; it ends by asking Qt
        # to quit, and Qt answers that by emitting aboutToQuit again (see
        # SuitMk2's _quit for what that loop did when it was not guarded).
        if self._quitting:
            return
        self._quitting = True
        _save_window_state(self)
        try:
            self._tell_tabs("shutdown")
        finally:
            global _LIVE
            if _LIVE is self:
                _LIVE = None
            QApplication.quit()
