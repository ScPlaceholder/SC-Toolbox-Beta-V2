"""Floating pop-out window that mirrors the Mining Chart tab.

Used so miners can keep the chart on top of the Star Citizen window
while the main Mining Signals UI is collapsed for scanning.  The
caller is expected to treat this as a singleton: call
``show_singleton(parent, data)`` instead of constructing directly.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QMouseEvent, QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QTabBar,
)

from shared.qt.theme import P
from shared.qt.search_bar import SCSearchBar

from .mining_chart import MiningChartGrid, VIEW_SHIP, VIEW_FPS
from services.mining_chart_data import MiningChartData

log = logging.getLogger(__name__)

# One live instance at a time — enforced via a module-level reference.
_instance: "ChartBubble | None" = None


class ChartBubble(QWidget):
    """Frameless, always-on-top popup showing the mining chart."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setMinimumSize(600, 320)
        self.resize(1100, 700)

        self._drag_origin: QPoint | None = None
        self._build_ui()

    # ── UI ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        # Outer frame for a visible border
        frame = QFrame(self)
        frame.setObjectName("bubbleFrame")
        frame.setStyleSheet(f"""
            QFrame#bubbleFrame {{
                background: {P.bg_primary};
                border: 1px solid {P.accent};
            }}
        """)
        root.addWidget(frame, 1)

        inner = QVBoxLayout(frame)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        # ── Title bar (draggable) ──
        self._title_bar = QWidget(frame)
        self._title_bar.setFixedHeight(28)
        self._title_bar.setStyleSheet(
            f"background: {P.bg_header}; border-bottom: 1px solid {P.border};"
        )
        tbl = QHBoxLayout(self._title_bar)
        tbl.setContentsMargins(10, 0, 4, 0)
        tbl.setSpacing(8)

        title = QLabel("Mining Chart — Pop-out", self._title_bar)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; "
            f"font-size: 9pt; font-weight: bold; "
            f"color: {P.accent}; background: transparent;"
        )
        tbl.addWidget(title)
        tbl.addStretch(1)

        hdr_btn_style = f"""
            QPushButton {{
                background: transparent;
                color: {P.accent};
                border: 1px solid {P.accent};
                border-radius: 3px;
                font-family: Consolas, monospace;
                font-size: 8pt; font-weight: bold;
                padding: 2px 8px;
            }}
            QPushButton:hover {{ background: rgba(68, 170, 255, 0.15); }}
            QPushButton:disabled {{
                color: {P.fg_dim}; border-color: {P.border};
            }}
        """

        self._btn_sort_dir = QPushButton("\u25bc Highest", self._title_bar)
        self._btn_sort_dir.setCursor(Qt.PointingHandCursor)
        self._btn_sort_dir.setToolTip(
            "Toggle sort direction (highest / lowest percentage first)"
        )
        self._btn_sort_dir.setStyleSheet(hdr_btn_style)
        self._btn_sort_dir.clicked.connect(self._on_toggle_sort_dir)
        tbl.addWidget(self._btn_sort_dir)

        self._btn_clear_sort = QPushButton("Clear", self._title_bar)
        self._btn_clear_sort.setCursor(Qt.PointingHandCursor)
        self._btn_clear_sort.setToolTip("Clear the focused column or row")
        self._btn_clear_sort.setStyleSheet(hdr_btn_style)
        self._btn_clear_sort.clicked.connect(self._on_clear_sort)
        tbl.addWidget(self._btn_clear_sort)

        self._btn_reset_scale = QPushButton("Reset Scale", self._title_bar)
        self._btn_reset_scale.setCursor(Qt.PointingHandCursor)
        self._btn_reset_scale.setToolTip(
            "Reset the chart zoom to 100% (Ctrl+Wheel to zoom)"
        )
        self._btn_reset_scale.setStyleSheet(hdr_btn_style)
        self._btn_reset_scale.clicked.connect(self._on_reset_scale)
        tbl.addWidget(self._btn_reset_scale)

        self._btn_fullscreen = QPushButton("\u26f6 Fullscreen", self._title_bar)
        self._btn_fullscreen.setCursor(Qt.PointingHandCursor)
        self._btn_fullscreen.setToolTip(
            "Toggle this pop-out between fullscreen and normal"
        )
        self._btn_fullscreen.setStyleSheet(hdr_btn_style)
        self._btn_fullscreen.clicked.connect(self._on_toggle_fullscreen)
        tbl.addWidget(self._btn_fullscreen)

        close_btn = QPushButton("\u2715", self._title_bar)   # ✕
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("Close pop-out")
        close_btn.setFixedSize(24, 22)
        close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 60, 60, 0.15);
                color: #cc6666;
                border: none; border-radius: 3px;
                font-family: Consolas; font-size: 11pt; font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: rgba(220, 50, 50, 0.85);
                color: #ffffff;
            }
        """)
        close_btn.clicked.connect(self.close)
        tbl.addWidget(close_btn)

        inner.addWidget(self._title_bar)

        # ── View-mode sub-tab bar ──
        self._view_tabs = QTabBar(frame)
        self._view_tabs.setDrawBase(False)
        self._view_tabs.setExpanding(False)
        self._view_tabs.setStyleSheet(f"""
            QTabBar {{
                background: {P.bg_primary};
                border: none;
            }}
            QTabBar::tab {{
                background: {P.bg_card};
                color: {P.fg_dim};
                border: 1px solid {P.border};
                border-bottom: none;
                padding: 4px 12px;
                font-family: Consolas, monospace;
                font-size: 9pt;
                font-weight: bold;
                margin: 4px 2px 0 0;
            }}
            QTabBar::tab:selected {{
                background: {P.bg_primary};
                color: {P.accent};
                border-bottom: 2px solid {P.accent};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)
        self._view_tabs.addTab("Ship Mining")
        self._view_tabs.addTab("FPS / ROC Mining")
        self._view_tabs.currentChanged.connect(self._on_view_tab_changed)
        inner.addWidget(self._view_tabs)

        # ── Search row: resource + location fuzzy search ──
        search_holder = QWidget(frame)
        search_holder.setStyleSheet(f"background: {P.bg_primary};")
        search_row = QHBoxLayout(search_holder)
        search_row.setContentsMargins(8, 4, 8, 4)
        search_row.setSpacing(8)

        def _mk_label(text: str) -> QLabel:
            lbl = QLabel(text, search_holder)
            lbl.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 8pt; "
                f"font-weight: bold; color: {P.fg_dim}; background: transparent;"
            )
            return lbl

        search_row.addWidget(_mk_label("Resource:"))
        self._resource_search = SCSearchBar(
            placeholder="Filter resources...",
            debounce_ms=150, parent=search_holder,
        )
        self._resource_search.setFixedHeight(24)
        self._resource_search.search_changed.connect(self._on_resource_search)
        search_row.addWidget(self._resource_search, 1)

        search_row.addWidget(_mk_label("Location:"))
        self._location_search = SCSearchBar(
            placeholder="Filter locations...",
            debounce_ms=150, parent=search_holder,
        )
        self._location_search.setFixedHeight(24)
        self._location_search.search_changed.connect(self._on_location_search)
        search_row.addWidget(self._location_search, 1)

        inner.addWidget(search_holder)

        # ── Chart inside a scroll area ──
        self._scroll = QScrollArea(frame)
        self._scroll.setWidgetResizable(False)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {P.bg_primary};
                border: none;
            }}
        """)
        self._grid = MiningChartGrid(self._scroll)
        self._grid.focus_state_changed.connect(self._sync_sort_button)
        self._scroll.setWidget(self._grid)
        inner.addWidget(self._scroll, 1)

        # Initial sync so disabled state is correct before first click.
        self._sync_sort_button()

    # ── handlers for the inner controls ──

    def _on_view_tab_changed(self, index: int) -> None:
        self._grid.set_view_mode(VIEW_SHIP if index == 0 else VIEW_FPS)

    def _on_resource_search(self, text: str) -> None:
        self._grid.set_resource_filter(text)

    def _on_location_search(self, text: str) -> None:
        self._grid.set_location_filter(text)

    def _on_toggle_sort_dir(self) -> None:
        self._grid.toggle_sort_direction()

    def _on_clear_sort(self) -> None:
        self._grid.clear_focus()

    def _on_reset_scale(self) -> None:
        self._grid.reset_scale()

    def _on_toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            saved = getattr(self, "_pre_fullscreen_geom", None)
            if saved is not None:
                self.setGeometry(saved)
        else:
            self._pre_fullscreen_geom = self.geometry()
            self.showFullScreen()

    def _sync_sort_button(self) -> None:
        direction = self._grid.sort_direction()
        if direction == "desc":
            self._btn_sort_dir.setText("\u25bc Highest")
        else:
            self._btn_sort_dir.setText("\u25b2 Lowest")
        has_focus = (self._grid.focused_column() is not None
                     or self._grid.focused_row() is not None)
        self._btn_sort_dir.setEnabled(has_focus)
        self._btn_clear_sort.setEnabled(has_focus)

    # ── drag-to-move via the title bar ──

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton and self._title_bar.geometry().contains(ev.position().toPoint()):
            self._drag_origin = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        if self._drag_origin is not None and ev.buttons() & Qt.LeftButton:
            target = ev.globalPosition().toPoint() - self._drag_origin
            self.move(self._clamp_to_screen(target))
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        self._drag_origin = None
        super().mouseReleaseEvent(ev)

    # ── keep the title bar reachable on every monitor ──

    def _clamp_to_screen(self, top_left: QPoint) -> QPoint:
        """Restrict a proposed top-left position so the draggable title
        bar is always reachable.  Picks the screen nearest the proposed
        position and clamps the Y coordinate so the title bar can't go
        above the screen (off the top) and clamps X so at least a sliver
        of the window remains visible on the left and right.
        """
        screen = QGuiApplication.screenAt(top_left) or self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return top_left
        avail = screen.availableGeometry()

        title_h = self._title_bar.height() if self._title_bar is not None else 28
        w = self.width()
        h = self.height()
        # Keep at least a small handle visible on every edge.
        min_visible_x = 80

        x = top_left.x()
        y = top_left.y()
        # Clamp Y: top of window can't go above the screen, and the title
        # bar can't be dragged off the bottom.
        y = max(avail.top(), min(y, avail.bottom() - title_h))
        # Clamp X: leave at least min_visible_x pixels on-screen.
        x = max(avail.left() - (w - min_visible_x),
                min(x, avail.right() - min_visible_x))
        return QPoint(x, y)

    def showEvent(self, ev) -> None:  # noqa: N802
        """When the pop-out is shown, make sure it sits on-screen.

        Fixes the case where the user dragged the previous instance off
        the top of the monitor before closing Mining Signals: the next
        time they open the pop-out, it would otherwise reappear at the
        bad saved position.
        """
        super().showEvent(ev)
        self.move(self._clamp_to_screen(self.pos()))

    # ── data ──

    def set_data(self, data: MiningChartData | None) -> None:
        if data is not None:
            self._grid.set_data(data)

    # ── singleton teardown ──

    def closeEvent(self, ev) -> None:  # noqa: N802
        global _instance
        if _instance is self:
            _instance = None
        super().closeEvent(ev)


# ─────────────────────────────────────────────────────────────────────────────
# Singleton factory — the only entry point the UI layer should use.
# ─────────────────────────────────────────────────────────────────────────────


def show_singleton(parent, data: MiningChartData | None) -> ChartBubble:
    """Create (or raise) the single ``ChartBubble`` instance and show it."""
    global _instance
    if _instance is not None:
        try:
            _instance.set_data(data)
            _instance.show()
            _instance.raise_()
            _instance.activateWindow()
            return _instance
        except RuntimeError:
            # Instance was deleted behind our back — fall through to recreate.
            _instance = None

    _instance = ChartBubble(parent)
    _instance.set_data(data)
    _instance.show()
    _instance.raise_()
    _instance.activateWindow()
    return _instance


def close_singleton() -> None:
    """Close the pop-out if it exists.  Safe to call even if there is none."""
    global _instance
    if _instance is not None:
        try:
            _instance.close()
        except RuntimeError:
            pass
        _instance = None
