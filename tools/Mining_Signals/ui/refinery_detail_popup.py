"""Small popup card shown when a refinery is clicked in the Locations tab.

Displays the refinery name, system, location type, yield bonuses /
penalties for every mineral, and a Copy Name button.  Only one instance
is shown at a time — clicking a different refinery replaces the current
popup.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea,
)

from shared.qt.theme import P

from services.refinery_locations import RefineryLocation
from services.refinery_yields import RefineryYieldData

ACCENT = "#33dd88"
GREEN = "#33dd88"
RED = "#ff4444"

_instance: "RefineryDetailPopup | None" = None


class RefineryDetailPopup(QWidget):
    """Frameless, always-on-top popup showing a refinery's yield profile."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setFixedWidth(320)
        self.setStyleSheet(f"background: {P.bg_primary};")

        self._drag_origin: QPoint | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        frame = QFrame(self)
        frame.setObjectName("popFrame")
        frame.setStyleSheet(f"""
            QFrame#popFrame {{
                background: {P.bg_primary};
                border: 1px solid {ACCENT};
            }}
        """)
        root.addWidget(frame)

        inner = QVBoxLayout(frame)
        inner.setContentsMargins(12, 8, 12, 10)
        inner.setSpacing(4)

        # Title row (name + close)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        self._name_lbl = QLabel("", frame)
        self._name_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; "
            f"font-size: 11pt; font-weight: bold; "
            f"color: {P.fg_bright}; background: transparent;"
        )
        self._name_lbl.setWordWrap(True)
        title_row.addWidget(self._name_lbl, 1)

        close_btn = QPushButton("\u2715", frame)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 60, 60, 0.15);
                color: #cc6666; border: none; border-radius: 3px;
                font-family: Consolas; font-size: 10pt; font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: rgba(220, 50, 50, 0.85);
                color: #ffffff;
            }
        """)
        close_btn.clicked.connect(self.close)
        title_row.addWidget(close_btn)
        inner.addLayout(title_row)

        # Sub-info (system + type)
        self._sub_lbl = QLabel("", frame)
        self._sub_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        inner.addWidget(self._sub_lbl)

        # Separator
        sep = QFrame(frame)
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {P.border};")
        inner.addWidget(sep)

        # Yield header
        yield_hdr = QLabel("YIELD PROFILE", frame)
        yield_hdr.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 7pt; "
            f"font-weight: bold; letter-spacing: 1px; "
            f"color: {P.fg_dim}; background: transparent; padding-top: 4px;"
        )
        inner.addWidget(yield_hdr)

        # Scrollable yield rows
        self._yield_scroll = QScrollArea(frame)
        self._yield_scroll.setWidgetResizable(True)
        self._yield_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._yield_scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background: {P.bg_primary}; }}"
        )
        self._yield_container = QWidget()
        self._yield_container.setStyleSheet(f"background: {P.bg_primary};")
        self._yield_layout = QVBoxLayout(self._yield_container)
        self._yield_layout.setContentsMargins(4, 2, 4, 2)
        self._yield_layout.setSpacing(1)
        self._yield_scroll.setWidget(self._yield_container)
        inner.addWidget(self._yield_scroll, 1)

        # Copy button
        self._copy_btn = QPushButton("Copy Name to Clipboard", frame)
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.setStyleSheet(f"""
            QPushButton {{
                font-family: Consolas, monospace; font-size: 8pt;
                font-weight: bold; color: {ACCENT}; background: transparent;
                border: 1px solid {ACCENT}; border-radius: 3px;
                padding: 5px 10px; margin-top: 6px;
            }}
            QPushButton:hover {{ background: rgba(51, 221, 136, 0.15); }}
        """)
        inner.addWidget(self._copy_btn)

        # Status flash
        self._status = QLabel("", frame)
        self._status.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 7pt; "
            f"color: {ACCENT}; background: transparent;"
        )
        self._status.setAlignment(Qt.AlignCenter)
        inner.addWidget(self._status)

    # ── drag-to-move (frameless window) ──

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.LeftButton:
            self._drag_origin = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self._drag_origin is not None and ev.buttons() & Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag_origin)
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        self._drag_origin = None
        super().mouseReleaseEvent(ev)

    # ── data ──

    def populate(
        self,
        ref: RefineryLocation,
        yield_data: RefineryYieldData | None,
    ) -> None:
        """Fill the popup with a refinery's info and yield profile."""
        self._name_lbl.setText(ref.name)
        self._sub_lbl.setText(f"{ref.system}  \u00b7  {ref.loc_type}")
        self._status.setText("")

        # Wire copy button (disconnect any prior connection first)
        try:
            self._copy_btn.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._copy_btn.clicked.connect(lambda: self._copy(ref.name))

        # Clear old yield rows
        while self._yield_layout.count() > 0:
            item = self._yield_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        # Find yield profile
        profile: dict[str, int] = {}
        if yield_data is not None:
            for r in yield_data.refineries:
                if r.name == ref.name:
                    profile = yield_data.profiles.get(r.profile_id, {})
                    break

        if not profile:
            no_data = QLabel("No yield data available", self._yield_container)
            no_data.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 8pt; "
                f"color: {P.fg_dim}; background: transparent; padding: 8px;"
            )
            self._yield_layout.addWidget(no_data)
        else:
            for mineral in sorted(profile.keys()):
                pct = profile[mineral]
                row = QWidget(self._yield_container)
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(4)

                name_lbl = QLabel(mineral, row)
                name_lbl.setStyleSheet(
                    f"font-family: Consolas, monospace; font-size: 8pt; "
                    f"color: {P.fg}; background: transparent;"
                )
                rl.addWidget(name_lbl, 1)

                if pct > 0:
                    val_text, val_color = f"+{pct}%", GREEN
                elif pct < 0:
                    val_text, val_color = f"{pct}%", RED
                else:
                    val_text, val_color = "0%", P.fg_dim
                val_lbl = QLabel(val_text, row)
                val_lbl.setStyleSheet(
                    f"font-family: Consolas, monospace; font-size: 9pt; "
                    f"font-weight: bold; color: {val_color}; "
                    f"background: transparent;"
                )
                val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                val_lbl.setFixedWidth(50)
                rl.addWidget(val_lbl)

                self._yield_layout.addWidget(row)

        self._yield_layout.addStretch(1)

        # Size to content (clamp height)
        n_rows = max(len(profile), 1)
        body_h = min(n_rows * 22 + 40, 400)
        self.setFixedHeight(body_h + 160)

    def _copy(self, name: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(name)
        self._status.setText(f"Copied '{name}'")

    def closeEvent(self, ev) -> None:
        global _instance
        if _instance is self:
            _instance = None
        super().closeEvent(ev)


def show_refinery_popup(
    parent: QWidget,
    ref: RefineryLocation,
    yield_data: RefineryYieldData | None,
    anchor: QWidget | None = None,
) -> RefineryDetailPopup:
    """Show (or replace) the singleton refinery detail popup."""
    global _instance
    if _instance is not None:
        try:
            _instance.close()
        except RuntimeError:
            pass
        _instance = None

    popup = RefineryDetailPopup(parent)
    popup.populate(ref, yield_data)

    if anchor is not None:
        pos = anchor.mapToGlobal(anchor.rect().topRight())
        popup.move(pos.x() + 8, pos.y())
    else:
        popup.move(parent.mapToGlobal(parent.rect().center()))

    popup.show()
    popup.raise_()
    _instance = popup
    return popup
