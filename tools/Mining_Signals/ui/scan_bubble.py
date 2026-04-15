"""Floating HUD bubble that shows the detected mining resource.

Small frameless always-on-top popup positioned near the scan region.
Auto-fades after a configurable duration, or stays until the next
scan updates it.  Supports showing multiple matches when signal
values overlap across resources.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

from shared.qt.theme import P
from services.signal_matcher import SignalMatch

log = logging.getLogger(__name__)

RARITY_COLORS: dict[str, str] = {
    "Common": "#8cc63f",
    "Uncommon": "#00bcd4",
    "Rare": "#ffc107",
    "Epic": "#aa66ff",
    "Legendary": "#ff9800",
    "ROC": "#33ccdd",
    "FPS": "#44aaff",
    "Salvage": "#66ccff",
}

_FADE_DURATION_MS = 8000  # stay visible between scan ticks
_ANIMATION_MS = 300
_WIDTH = 280
_ROW_HEIGHT = 24
_PADDING = 20  # top + bottom padding


class ScanBubble(QWidget):
    """Floating HUD popup showing detected mining resource(s)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._accent = QColor(P.green)
        self._matches: list[SignalMatch] = []

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 10, 14, 10)
        self._layout.setSpacing(2)

        # Dynamic labels — created per show_matches call
        self._labels: list[QLabel] = []

        # Auto-fade timer
        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self._start_fade_out)

    def show_scanning(self, anchor_x: int, anchor_y: int) -> None:
        """Display a 'Scanning — Please Wait' placeholder bubble."""
        self._matches = []
        self._accent = QColor(P.green)

        for lbl in self._labels:
            self._layout.removeWidget(lbl)
            lbl.deleteLater()
        self._labels.clear()

        header = QLabel("Scanning", self)
        header.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 14pt; font-weight: bold;
            color: {P.green}; background: transparent;
        """)
        self._layout.addWidget(header)
        self._labels.append(header)

        sub = QLabel("Please Wait", self)
        sub.setStyleSheet(f"""
            font-family: Consolas, monospace;
            font-size: 9pt; color: {P.fg_dim}; background: transparent;
        """)
        self._layout.addWidget(sub)
        self._labels.append(sub)

        self.setFixedSize(_WIDTH, _PADDING + 44)

        anim = getattr(self, "_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass

        self.move(anchor_x, anchor_y)
        self.setWindowOpacity(1.0)
        if not self.isVisible():
            self.show()
        self.raise_()

        try:
            import ctypes
            hwnd = int(self.winId())
            ctypes.windll.user32.SetWindowPos(
                hwnd, -1, 0, 0, 0, 0,
                0x0002 | 0x0001 | 0x0040 | 0x0010,
            )
        except Exception:
            pass

        # No fade timer — stays until a result replaces it
        self._fade_timer.stop()
        self.update()

    def show_matches(
        self,
        matches: list[SignalMatch],
        anchor_x: int,
        anchor_y: int,
        scanned_value: int | None = None,
    ) -> None:
        """Display one or more match results.

        *scanned_value* is the raw OCR number so the user can visually
        confirm it matches the in-game signature.
        """
        if not matches:
            return

        self._matches = matches
        self._accent = QColor(RARITY_COLORS.get(matches[0].rarity, P.fg))

        # Clear old labels
        for lbl in self._labels:
            self._layout.removeWidget(lbl)
            lbl.deleteLater()
        self._labels.clear()

        extra_rows = 0

        if len(matches) == 1:
            # Single match — show name large, detail below
            m = matches[0]
            accent = RARITY_COLORS.get(m.rarity, P.fg)

            name_lbl = QLabel(m.name, self)
            name_lbl.setStyleSheet(f"""
                font-family: Electrolize, Consolas, monospace;
                font-size: 14pt; font-weight: bold;
                color: {accent}; background: transparent;
            """)
            self._layout.addWidget(name_lbl)
            self._labels.append(name_lbl)

            rock_word = "Rock" if m.rock_count == 1 else "Rocks"
            detail_lbl = QLabel(f"{m.rarity}  \u00b7  {m.rock_count} {rock_word}", self)
            detail_lbl.setStyleSheet(f"""
                font-family: Consolas, monospace;
                font-size: 9pt; color: {P.fg_dim}; background: transparent;
            """)
            self._layout.addWidget(detail_lbl)
            self._labels.append(detail_lbl)

            total_height = _PADDING + 44  # name + detail
        else:
            # Multiple matches — compact list
            for m in matches:
                accent = RARITY_COLORS.get(m.rarity, P.fg)
                rock_word = "Rock" if m.rock_count == 1 else "Rocks"
                line = f"{m.name}  \u00b7  {m.rarity}  \u00b7  {m.rock_count} {rock_word}"
                lbl = QLabel(line, self)
                lbl.setStyleSheet(f"""
                    font-family: Consolas, monospace;
                    font-size: 9pt; font-weight: bold;
                    color: {accent}; background: transparent;
                """)
                self._layout.addWidget(lbl)
                self._labels.append(lbl)

            total_height = _PADDING + len(matches) * _ROW_HEIGHT

        # Signature confirmation line — shows the scanned value so the
        # user can eyeball-verify it against the in-game number.
        if scanned_value is not None:
            sig_lbl = QLabel(f"Signature: {scanned_value:,}", self)
            sig_lbl.setStyleSheet(f"""
                font-family: Consolas, monospace;
                font-size: 8pt; color: {P.fg_dim}; background: transparent;
                padding-top: 2px;
            """)
            self._layout.addWidget(sig_lbl)
            self._labels.append(sig_lbl)
            extra_rows += 1

        total_height += extra_rows * _ROW_HEIGHT

        self.setFixedSize(_WIDTH, total_height)

        # Stop any in-progress fade-out animation so we can't fade
        # out the bubble while it's supposed to be showing again.
        anim = getattr(self, "_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass

        # Position and show (force visible even if previously hidden)
        self.move(anchor_x, anchor_y)
        self.setWindowOpacity(1.0)
        if not self.isVisible():
            self.show()
        self.raise_()

        # Force topmost via Win32 API — fights borderless fullscreen games
        try:
            import ctypes
            hwnd = int(self.winId())
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_SHOWWINDOW = 0x0040
            SWP_NOACTIVATE = 0x0010
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE,
            )
        except Exception:
            pass

        log.info("show_matches: %d match(es) at (%d,%d)",
                 len(matches), anchor_x, anchor_y)

        # Persistent — no auto-fade. The scan loop in ui/app.py hides
        # the bubble when the HUD panel isn't visible (the user looked
        # away from the rock), so the bubble stays up until then.
        self._fade_timer.stop()
        self.update()

    # Keep backward compat
    def show_match(self, match: SignalMatch, anchor_x: int, anchor_y: int) -> None:
        """Display a single match result."""
        self.show_matches([match], anchor_x, anchor_y)

    def _start_fade_out(self) -> None:
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(_ANIMATION_MS)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.OutQuad)
        self._anim.finished.connect(self.hide)
        self._anim.start()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        # Dark translucent background
        bg = QColor(P.bg_primary)
        bg.setAlpha(220)
        painter.fillRect(0, 0, w, h, bg)

        # Top glow gradient
        glow = QLinearGradient(0, 0, 0, 8)
        gc = QColor(self._accent)
        gc.setAlpha(60)
        glow.setColorAt(0.0, gc)
        gc2 = QColor(self._accent)
        gc2.setAlpha(0)
        glow.setColorAt(1.0, gc2)
        painter.fillRect(0, 0, w, 8, glow)

        # Border
        border = QColor(self._accent)
        border.setAlpha(140)
        painter.setPen(QPen(border, 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        # Glow bloom
        bloom = QColor(self._accent)
        bloom.setAlpha(20)
        painter.setPen(QPen(bloom, 3))
        painter.drawRect(1, 1, w - 3, h - 3)

        painter.end()
        super().paintEvent(event)
