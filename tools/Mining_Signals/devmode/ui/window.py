"""The Dev Mode pop-out: a step rail on the left, one page per step."""

from __future__ import annotations

import logging
from typing import Optional

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QScrollArea, QStackedWidget,
    QVBoxLayout, QWidget,
)

from shared.qt.base_window import SCWindow
from shared.qt.theme import P
from shared.qt.title_bar import SCTitleBar

from .backend import BackendHandle, load_backend
from .pages import PAGE_CLASSES
from .pages.base import StepPage
from .style import ACCENT, ACCENT_RGB, HEAD, MONO, Banner

log = logging.getLogger(__name__)

TITLE = "Mining Signals Dev Mode"
START_STEP = 2          # Label: the screen people spend their time on


class RailItem(QPushButton):
    """One step in the left rail: number, name, and a live one-line status."""

    def __init__(self, number: int, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(58)
        self.setObjectName("railItem")
        self.setToolTip(f"{name}  (Ctrl+{number})")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 10, 6)
        lay.setSpacing(10)
        self.num = QLabel(str(number), self)
        self.num.setFixedSize(26, 26)
        self.num.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.num)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.name = QLabel(name.upper(), self)
        self.sub = QLabel("", self)
        col.addWidget(self.name)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        for w in (self.num, self.name, self.sub):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.toggled.connect(self._restyle)
        self._restyle(False)

    def set_summary(self, text: str) -> None:
        self.sub.setText(text)

    def _restyle(self, on: bool) -> None:
        self.setStyleSheet(
            f"QPushButton#railItem {{ background: {'rgba(' + ACCENT_RGB + ', 28)' if on else 'transparent'}; "
            f"border: none; border-left: 3px solid {ACCENT if on else 'transparent'}; "
            f"text-align: left; padding: 0px; min-height: 58px; max-height: 58px; }}"
            f"QPushButton#railItem:hover {{ background: rgba({ACCENT_RGB}, 16); }}")
        self.num.setStyleSheet(
            f"QLabel {{ border: 1px solid {ACCENT if on else P.border_card}; border-radius: 13px; "
            f"color: {P.bg_primary if on else P.fg_dim}; background: {ACCENT if on else 'transparent'}; "
            f"font-family: {HEAD}; font-size: 10pt; font-weight: bold; }}")
        self.name.setStyleSheet(
            f"color: {P.fg_bright if on else P.fg}; font-family: {HEAD}; font-size: 10pt; "
            f"font-weight: bold; letter-spacing: 1px; background: transparent;")
        self.sub.setStyleSheet(
            f"color: {ACCENT if on else P.fg_dim}; font-family: {MONO}; font-size: 8pt; "
            f"background: transparent;")


class DevModeWindow(SCWindow):
    def __init__(self, handle: Optional[BackendHandle] = None, parent: Optional[QWidget] = None):
        # parent is deliberately NOT passed to SCWindow: SCWindow replaces the
        # window flags, and a parented QMainWindow without Qt.Window would be
        # embedded inside the host instead of floating. Lifetime is tied below.
        super().__init__(title=TITLE, width=1200, height=820, min_w=1000, min_h=620,
                         opacity=0.97, always_on_top=True, accent=ACCENT)
        self.handle = handle or load_backend()
        self._host = parent
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        lay = self.content_layout
        self.title_bar = SCTitleBar(self, title=TITLE, accent_color=ACCENT)
        self.title_bar.minimize_clicked.connect(self.showMinimized)
        self.title_bar.close_clicked.connect(self.close)
        lay.addWidget(self.title_bar)

        if self.handle.is_fake:
            demo = Banner(self, "error")
            demo.set_text(f"<b>DEMO DATA.</b> Using a built-in fake backend because "
                          f"{self.handle.fake_reason}. Nothing shown is real and nothing you do "
                          f"here is saved.")
            wrap = QWidget(self)
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(10, 8, 10, 0)
            wl.addWidget(demo)
            lay.addWidget(wrap)
            self.demo_banner = demo
        else:
            self.demo_banner = None

        body = QWidget(self)
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        lay.addWidget(body, 1)

        rail = QFrame(body)
        rail.setObjectName("dmRail")
        rail.setFixedWidth(214)
        rail.setStyleSheet(f"QFrame#dmRail {{ background: rgba(6, 8, 13, 150); "
                           f"border-right: 1px solid {P.border}; }}")
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(0, 12, 0, 12)
        rl.setSpacing(2)
        cap = QLabel("TRAINING PIPELINE", rail)
        cap.setStyleSheet(f"color: {P.fg_dim}; font-family: {HEAD}; font-size: 8pt; "
                          f"letter-spacing: 2px; padding: 0 0 8px 16px; background: transparent;")
        rl.addWidget(cap)
        bl.addWidget(rail)

        self.stack = QStackedWidget(body)
        bl.addWidget(self.stack, 1)

        self.pages: list[StepPage] = []
        self.rail_items: list[RailItem] = []
        for i, cls in enumerate(PAGE_CLASSES):
            page = cls(self.handle)
            scroll = QScrollArea(self.stack)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setWidget(page)
            scroll.viewport().setStyleSheet("background: transparent;")
            self.stack.addWidget(scroll)
            item = RailItem(i + 1, cls.title, rail)
            item.clicked.connect(lambda _c=False, n=i: self.select_step(n))
            rl.addWidget(item)
            page.summary_changed.connect(item.set_summary)
            page.runner.activity.connect(self._activity)
            page.runner.error.connect(self._job_error)
            page.runner.busy_changed.connect(self._busy_changed)
            self.pages.append(page)
            self.rail_items.append(item)
            sc = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            sc.activated.connect(lambda n=i: self.select_step(n))
        rl.addStretch(1)
        hint = QLabel("Ctrl+1…7 switches step", rail)
        hint.setStyleSheet(f"color: {P.fg_disabled}; font-family: {MONO}; font-size: 8pt; "
                           f"padding-left: 16px; background: transparent;")
        rl.addWidget(hint)

        footer = QFrame(self)
        footer.setObjectName("dmFooter")
        footer.setFixedHeight(26)
        footer.setStyleSheet(f"QFrame#dmFooter {{ background: rgba(6, 8, 13, 200); "
                             f"border-top: 1px solid {P.border}; }}")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(12, 0, 12, 0)
        self.busy_dot = QLabel("●", footer)
        self.activity = QLabel("Ready", footer)
        fl.addWidget(self.busy_dot)
        fl.addWidget(self.activity, 1)
        src = QLabel("demo backend" if self.handle.is_fake else "", footer)
        src.setStyleSheet(f"color: {P.red}; font-family: {MONO}; font-size: 8pt; "
                          f"background: transparent;")
        fl.addWidget(src)
        lay.addWidget(footer)
        self._set_busy_look(False)

        self.engine_page.torch_changed.connect(self.train_page.set_torch)
        self.train_page.go_to_step.connect(self.select_step)

        # Fill the rail's status lines without opening every page.
        for p in (self.engine_page, self.pages[1], self.pages[6]):
            p.refresh()
        self.select_step(START_STEP)
        self._center()

    # ── pages by role ──
    @property
    def engine_page(self):
        return self.pages[0]

    @property
    def label_page(self):
        return self.pages[2]

    @property
    def train_page(self):
        return self.pages[5]

    @property
    def export_page(self):
        return self.pages[6]

    @property
    def current_step(self) -> int:
        return self.stack.currentIndex()

    def select_step(self, n: int) -> None:
        if not 0 <= n < len(self.pages):
            return
        for i, item in enumerate(self.rail_items):
            item.setChecked(i == n)
        self.stack.setCurrentIndex(n)
        self.pages[n].refresh()

    # ── footer ──
    def _activity(self, text: str) -> None:
        self.activity.setText(text)
        self.activity.setStyleSheet(f"color: {P.fg_dim}; font-family: {MONO}; font-size: 8pt; "
                                    f"background: transparent;")

    def _job_error(self, name: str, message: str) -> None:
        log.warning("Dev Mode: %s failed: %s", name, message)
        self.activity.setText(f"{name} failed: {message}")
        self.activity.setStyleSheet(f"color: {P.red}; font-family: {MONO}; font-size: 8pt; "
                                    f"background: transparent;")

    def _busy_changed(self, _busy: bool) -> None:
        self._set_busy_look(self.busy)

    def _set_busy_look(self, busy: bool) -> None:
        self.busy_dot.setStyleSheet(f"color: {ACCENT if busy else P.fg_disabled}; "
                                    f"font-size: 9pt; background: transparent;")
        self.busy_dot.setToolTip("Working in the background" if busy else "Idle")

    @property
    def busy(self) -> bool:
        return any(p.runner.busy for p in self.pages)

    # ── window behaviour ──
    def _center(self) -> None:
        host = self._host if self._host is not None and shiboken6.isValid(self._host) else None
        screen = (host.screen() if host is not None else None) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        g = screen.availableGeometry()
        w, h = min(self.width(), g.width()), min(self.height(), g.height())
        self.resize(w, h)
        self.move(g.x() + (g.width() - w) // 2, g.y() + (g.height() - h) // 2)

    def user_close(self) -> None:
        # SCWindow.user_close may QUIT THE WHOLE APP (SC_TOOLBOX_EXIT_ON_CLOSE);
        # this pop-out lives inside Mining Signals, so X must only close it.
        self.close()

    def closeEvent(self, event) -> None:
        if self.busy:
            # Closing mid-install or mid-training would orphan the work; hide
            # instead, the job finishes, and reopening shows its result.
            self.hide()
            event.ignore()
            return
        # Skip SCWindow.closeEvent on purpose: it saves geometry under the
        # *process's* script name, which inside Mining Signals would overwrite
        # the main Mining Signals window's saved position.
        QMainWindow.closeEvent(self, event)


_INSTANCE: Optional[DevModeWindow] = None


def open_dev_mode(parent: Optional[QWidget] = None,
                  handle: Optional[BackendHandle] = None) -> DevModeWindow:
    """Open the Dev Mode window, or bring the existing one to the front."""
    global _INSTANCE
    win = _INSTANCE
    if win is not None and shiboken6.isValid(win):
        if win.isMinimized():
            win.showNormal()
        win.show()
        win.raise_()
        win.activateWindow()
        return win
    win = DevModeWindow(handle=handle, parent=parent)
    _INSTANCE = win
    win.destroyed.connect(_forget)
    if parent is not None:
        parent.destroyed.connect(win.close)
    win.show()
    win.raise_()
    win.activateWindow()
    return win


def _forget(*_args) -> None:
    global _INSTANCE
    _INSTANCE = None
