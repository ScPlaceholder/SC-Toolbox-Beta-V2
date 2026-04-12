"""
SCTitleBar – holographic HUD-style title bar.

Glowing header strip with accent-colored text, bloom gradient,
drag-to-move, and compact window controls.
"""

from __future__ import annotations
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QFont, QPainter, QPen, QColor, QLinearGradient
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QSlider,
)

from shared.qt.theme import P


class _TitleButton(QPushButton):
    """Tiny borderless button for window chrome."""

    def __init__(self, symbol: str, hover_bg: str, parent=None, is_close: bool = False):
        super().__init__(symbol, parent)
        self._hover_bg = hover_bg
        obj_name = "titleClose" if is_close else "titleMin"
        self.setObjectName(obj_name)
        self.setFixedSize(26, 26)
        self.setCursor(Qt.PointingHandCursor)
        rest_bg = "rgba(255, 60, 60, 0.15)" if is_close else "rgba(200, 200, 200, 0.08)"
        rest_fg = "#cc6666" if is_close else P.fg_dim
        hover_fg = "#ffffff" if is_close else P.fg_bright
        self.setStyleSheet(f"""
            QPushButton#{obj_name} {{
                background: {rest_bg};
                color: {rest_fg};
                border: none;
                border-radius: 3px;
                font-family: Consolas;
                font-size: 13pt;
                font-weight: bold;
                padding: 0px;
                margin: 2px;
                min-height: 0px;
            }}
            QPushButton#{obj_name}:hover {{
                background-color: {hover_bg};
                color: {hover_fg};
            }}
        """)


class SCTitleBar(QWidget):
    """Holographic title bar with glow, accent, and controls."""

    minimize_clicked = Signal()
    close_clicked = Signal()
    collapse_clicked = Signal()

    TITLE_HEIGHT = 36

    def __init__(
        self,
        window: QWidget,
        title: str = "SC Toolbox",
        icon_text: str = "",
        accent_color: str = "",
        hotkey_text: str = "",
        show_minimize: bool = True,
        extra_buttons: Optional[List[Tuple[str, Callable]]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent or window)
        self._window = window
        self._drag_pos = QPoint()
        self._dragging = False
        self._accent = accent_color or P.accent
        self._accent_color = QColor(self._accent)

        self.setFixedHeight(self.TITLE_HEIGHT)
        self.setMouseTracking(True)
        # Transparent bg — we paint our own
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 4, 0)
        layout.setSpacing(5)

        # Icon
        if icon_text:
            icon_label = QLabel(icon_text, self)
            icon_label.setStyleSheet(f"""
                font-size: 14pt;
                color: {self._accent};
                background: transparent;
            """)
            layout.addWidget(icon_label)

        # Title
        title_label = QLabel(title.upper(), self)
        title_label.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 11pt;
            font-weight: bold;
            color: {self._accent};
            letter-spacing: 3px;
            background: transparent;
        """)
        layout.addWidget(title_label)

        # Hotkey badge
        if hotkey_text:
            self._hotkey_label = QLabel(hotkey_text, self)
            self._hotkey_label.setStyleSheet(f"""
                font-family: Consolas, monospace;
                font-size: 8pt;
                font-weight: bold;
                color: {P.fg_dim};
                background: transparent;
                padding: 2px 6px;
            """)
            layout.addWidget(self._hotkey_label)
        else:
            self._hotkey_label = None

        layout.addStretch(1)

        # Opacity slider
        opacity_icon = QLabel("\u25C9", self)  # ◉ circle icon
        opacity_icon.setStyleSheet(f"""
            font-size: 8pt;
            color: {P.fg_dim};
            background: transparent;
            padding: 0px 2px;
        """)
        opacity_icon.setToolTip("Window Opacity")
        layout.addWidget(opacity_icon)

        self._opacity_slider = QSlider(Qt.Horizontal, self)
        self._opacity_slider.setRange(30, 100)
        self._opacity_slider.setValue(
            int(window.windowOpacity() * 100) if hasattr(window, "windowOpacity") else 95
        )
        self._opacity_slider.setFixedWidth(55)
        self._opacity_slider.setFixedHeight(18)
        self._opacity_slider.setCursor(Qt.PointingHandCursor)
        self._opacity_slider.setToolTip("Adjust window opacity")
        self._opacity_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: rgba(90, 100, 128, 0.3);
                height: 4px;
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {self._accent};
                width: 10px;
                height: 10px;
                margin: -3px 0;
                border-radius: 5px;
            }}
            QSlider::handle:horizontal:hover {{
                background: {P.fg_bright};
            }}
            QSlider::sub-page:horizontal {{
                background: {self._accent};
                border-radius: 2px;
            }}
        """)
        # Debounce: apply opacity only after the user stops moving the slider
        # for 100 ms.  Calling setWindowOpacity on every valueChanged tick
        # triggers a DWM recomposition on Windows (WA_TranslucentBackground),
        # which causes visible rapid flickering.
        self._opacity_timer = QTimer(self)
        self._opacity_timer.setSingleShot(True)
        self._opacity_timer.setInterval(100)
        self._opacity_timer.timeout.connect(self._apply_opacity)
        self._pending_opacity: float = self._opacity_slider.value() / 100.0

        self._opacity_slider.valueChanged.connect(self._on_opacity_slider_moved)
        layout.addWidget(self._opacity_slider)

        # Extra buttons (e.g. Patreon link)
        for btn_text, btn_cb in (extra_buttons or []):
            eb = QPushButton(btn_text, self)
            eb.setCursor(Qt.PointingHandCursor)
            eb.setStyleSheet(f"""
                QPushButton {{
                    font-family: Consolas, monospace;
                    font-size: 8pt;
                    font-weight: bold;
                    color: {P.accent};
                    background: transparent;
                    border: 1px solid {P.accent};
                    border-radius: 3px;
                    padding: 2px 8px;
                }}
                QPushButton:hover {{
                    background: rgba(68, 170, 255, 0.15);
                }}
            """)
            eb.clicked.connect(btn_cb)
            layout.addWidget(eb)

        # Window controls
        btn_reset = _TitleButton("\u27f2", "rgba(200, 200, 200, 0.18)", self, is_close=False)
        btn_reset.setToolTip("Reset layout to default size")
        btn_reset.clicked.connect(self._on_reset_btn)
        layout.addWidget(btn_reset)

        # Reset scale — resets child QGraphicsView zooms back to 1:1.
        # Useful for canvases like the Mining Signals ledger where
        # wheel-scroll-zoom can leave the view at an odd scale factor.
        btn_reset_scale = _TitleButton("1:1", "rgba(200, 200, 200, 0.18)", self, is_close=False)
        btn_reset_scale.setToolTip("Reset canvas zoom to 1:1")
        btn_reset_scale.clicked.connect(self._on_reset_scale_btn)
        # 1:1 is slightly wider than the other chrome symbols; shrink
        # the font a touch so it fits cleanly in the 26px button.
        btn_reset_scale.setStyleSheet(btn_reset_scale.styleSheet().replace(
            "font-size: 13pt;", "font-size: 9pt;"
        ))
        layout.addWidget(btn_reset_scale)

        # Fullscreen toggle
        self._btn_fullscreen = _TitleButton(
            "\u26f6", "rgba(200, 200, 200, 0.18)", self, is_close=False,
        )
        self._btn_fullscreen.setToolTip("Toggle fullscreen")
        self._btn_fullscreen.clicked.connect(self._on_fullscreen_btn)
        layout.addWidget(self._btn_fullscreen)

        self._btn_collapse = _TitleButton("\u25b2", "rgba(200, 200, 200, 0.18)", self, is_close=False)
        self._btn_collapse.setToolTip("Collapse / Expand")
        self._btn_collapse.clicked.connect(self._on_collapse_btn)
        layout.addWidget(self._btn_collapse)

        if show_minimize:
            btn_min = _TitleButton("-", "rgba(200, 200, 200, 0.18)", self, is_close=False)
            btn_min.clicked.connect(self.minimize_clicked.emit)
            layout.addWidget(btn_min)

        btn_close = _TitleButton("x", "rgba(220, 50, 50, 0.85)", self, is_close=True)
        btn_close.clicked.connect(self._on_close_btn)
        layout.addWidget(btn_close)

    def _on_reset_btn(self) -> None:
        """Reset the window to its default size and centre on screen."""
        if hasattr(self._window, "reset_layout"):
            self._window.reset_layout()

    def _on_reset_scale_btn(self) -> None:
        """Reset canvas zoom on child QGraphicsView(s) to 1:1."""
        if hasattr(self._window, "reset_scale"):
            self._window.reset_scale()

    def _on_fullscreen_btn(self) -> None:
        """Toggle fullscreen on the parent window."""
        if hasattr(self._window, "toggle_fullscreen"):
            self._window.toggle_fullscreen()

    def _on_collapse_btn(self) -> None:
        """Handle collapse button click.  Calls toggle_collapse on the
        window if available (SCWindow provides it), then updates the
        arrow direction.  Also emits the signal for any custom handling."""
        if hasattr(self._window, "toggle_collapse"):
            self._window.toggle_collapse()
            collapsed = getattr(self._window, "_collapsed", False)
            self._btn_collapse.setText("\u25bc" if collapsed else "\u25b2")
        self.collapse_clicked.emit()

    def _on_close_btn(self) -> None:
        """Handle close button click.  If SC_TOOLBOX_EXIT_ON_CLOSE is set,
        quit the process so the launcher can re-show itself.  Otherwise
        emit the normal signal for the skill to handle."""
        import os
        if os.environ.get("SC_TOOLBOX_EXIT_ON_CLOSE") == "1":
            if hasattr(self._window, "user_close"):
                self._window.user_close()
                return
        self.close_clicked.emit()

    def _on_opacity_slider_moved(self, value: int) -> None:
        self._pending_opacity = value / 100.0
        self._opacity_timer.start()

    def _apply_opacity(self) -> None:
        if hasattr(self._window, "set_opacity"):
            self._window.set_opacity(self._pending_opacity)
        else:
            self._window.setWindowOpacity(max(0.3, min(1.0, self._pending_opacity)))

    def set_collapsed(self, collapsed: bool) -> None:
        """Update the collapse button arrow direction."""
        self._btn_collapse.setText("\u25bc" if collapsed else "\u25b2")

    def set_hotkey(self, text: str) -> None:
        if self._hotkey_label:
            self._hotkey_label.setText(text)

    def paintEvent(self, event):
        """Paint the glowing header background."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        # Dark header fill
        bg = QColor(P.bg_header)
        bg.setAlpha(230)
        painter.fillRect(0, 0, w, h, bg)

        # Top edge glow gradient
        glow = QLinearGradient(0, 0, 0, h)
        gc1 = QColor(self._accent_color)
        gc1.setAlpha(25)
        gc2 = QColor(self._accent_color)
        gc2.setAlpha(0)
        glow.setColorAt(0.0, gc1)
        glow.setColorAt(1.0, gc2)
        painter.fillRect(0, 0, w, h, glow)

        # Bottom separator — bright accent line
        accent_line = QColor(self._accent_color)
        accent_line.setAlpha(100)
        painter.setPen(QPen(accent_line, 1))
        painter.drawLine(0, h - 1, w, h - 1)

        # Bottom separator glow
        glow_line = QColor(self._accent_color)
        glow_line.setAlpha(25)
        painter.setPen(QPen(glow_line, 3))
        painter.drawLine(0, h - 2, w, h - 2)

        painter.end()
        super().paintEvent(event)

    # ── Helpers ──

    def _is_on_window_edge(self, event) -> bool:
        """Return True if the mouse position is inside the parent window's
        resize grip zone.  In that case the event should be forwarded to
        the window instead of being handled as a title-bar drag."""
        if not hasattr(self._window, "_edge_at"):
            return False
        # Map the position to the parent window's coordinate space
        win_pos = self.mapTo(self._window, event.position().toPoint())
        return self._window._edge_at(win_pos) is not None

    # ── Drag-to-move ──

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._is_on_window_edge(event):
                event.ignore()  # let SCWindow handle resize
                return
            self._dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self._window.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging and event.buttons() & Qt.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        elif not self._dragging:
            if self._is_on_window_edge(event):
                event.ignore()  # let SCWindow show resize cursor
                return
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self._dragging:
                event.ignore()
                return
            self._dragging = False
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._window.isMaximized():
                self._window.showNormal()
            else:
                self._window.showMaximized()
            event.accept()
