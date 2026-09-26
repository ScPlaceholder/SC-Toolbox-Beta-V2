"""
Options popup for Battle Buddy.
Provides: log path text input, orientation toggle, auto-show toggle.
Settings are saved to ~/.sctoolbox/battle_buddy/settings.json.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QWidget, QButtonGroup, QRadioButton,
    QFileDialog,
)

from ui.theme import (
    BG, BG2, BG3, BG4, BG_INPUT, BORDER, FG, FG_DIM, ACCENT,
    HEADER_BG, FONT_TITLE, FONT_BODY,
)

# Settings live OUTSIDE the install folder: a Velopack update replaces that
# folder wholesale, which silently reset the log path and HUD position on every
# update.  The old in-folder file is still read once, as a migration.
_SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".sctoolbox", "battle_buddy", "settings.json")
_LEGACY_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                     "battle_buddy_settings.json")


def _shared_game_log() -> str:
    """Newest Game.log under the folder linked in the launcher's first-launch popup."""
    try:
        from shared.sc_install import get_sc_root, newest_game_log
    except ImportError:            # launched without the toolbox root on sys.path
        return ""
    return newest_game_log(get_sc_root()) or ""


def _auto_detect_game_log() -> str:
    """Search common Star Citizen install locations for the most recently
    modified Game.log.  Returns the path if found, or the legacy default."""
    import string

    candidates: list[str] = []
    # Check all available drive letters
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        # Common SC install patterns (with and without space)
        for base in (
            f"{drive}Star Citizen/StarCitizen",
            f"{drive}StarCitizen",
            f"{drive}Program Files/Roberts Space Industries/StarCitizen",
            f"{drive}Games/StarCitizen",
            f"{drive}Games/Star Citizen/StarCitizen",
        ):
            for channel in ("LIVE", "HOTFIX", "PTU", "EPTU", "TECH-PREVIEW"):
                path = os.path.join(base, channel, "Game.log")
                if os.path.isfile(path):
                    candidates.append(path)

    if not candidates:
        return "C:/StarCitizen/LIVE/Game.log"

    # Pick the most recently modified
    best = max(candidates, key=lambda p: os.path.getmtime(p))
    return best.replace("\\", "/")


def load_settings() -> dict:
    defaults = {
        "log_path":          "C:/StarCitizen/LIVE/Game.log",
        "orientation":       "horizontal",
        "opacity":           0.92,
    }
    migrated = False
    for path in (_SETTINGS_FILE, _LEGACY_SETTINGS_FILE):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    defaults.update(json.load(fh))
                migrated = path == _LEGACY_SETTINGS_FILE
                break
        except (OSError, json.JSONDecodeError):
            continue
    if migrated:
        save_settings(defaults)

    # If the configured log path doesn't exist, use the launcher's shared
    # folder (not persisted, so a later change there still reaches us),
    # then fall back to scanning drives.
    if not os.path.isfile(defaults["log_path"]):
        shared = _shared_game_log()
        if shared:
            defaults["log_path"] = shared
            return defaults
        detected = _auto_detect_game_log()
        if os.path.isfile(detected):
            defaults["log_path"] = detected
            # Persist so we don't re-scan every launch
            save_settings(defaults)

    return defaults


def save_settings(settings: dict) -> None:
    try:
        path = os.path.abspath(_SETTINGS_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
    except OSError as exc:
        logger.warning("Failed to save settings: %s", exc)


class OptionsPopup(QDialog):
    """Options window for Battle Buddy.  Draggable, non-modal."""

    _instance: "OptionsPopup | None" = None

    @classmethod
    def show_options(cls, parent=None, on_save: Callable[[dict], None] | None = None) -> "OptionsPopup":
        if cls._instance is None or not cls._instance.isVisible():
            cls._instance = cls(parent, on_save)
        cls._instance.show()
        cls._instance.raise_()
        cls._instance.activateWindow()
        return cls._instance

    def __init__(self, parent=None, on_save: Callable[[dict], None] | None = None):
        super().__init__(parent)
        self._on_save = on_save
        self._settings = load_settings()

        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedWidth(480)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {BG};
                border: 1px solid {ACCENT};
                border-radius: 4px;
            }}
        """)
        self._drag_pos = QPoint()
        self._dragging = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_body())
        root.addWidget(self._build_footer())

        self.adjustSize()

    # ── Sections ─────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        hdr = QWidget(self)
        hdr.setFixedHeight(34)
        hdr.setCursor(QCursor(Qt.OpenHandCursor))
        hdr.setStyleSheet(f"background-color: {HEADER_BG}; border-bottom: 1px solid {BORDER};")
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(12, 0, 6, 0)

        title = QLabel("\u2699\ufe0f  Battle Buddy \u2014 Options", hdr)
        title.setStyleSheet(f"""
            font-family: {FONT_TITLE}; font-size: 10pt;
            font-weight: bold; color: {ACCENT}; background: transparent;
        """)
        lay.addWidget(title)
        lay.addStretch(1)

        btn_close = QPushButton("\u2715", hdr)
        btn_close.setFixedSize(26, 22)
        btn_close.setCursor(QCursor(Qt.PointingHandCursor))
        btn_close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {FG_DIM}; border: none; font-size: 11pt; }}
            QPushButton:hover {{ color: #ff5533; }}
        """)
        btn_close.clicked.connect(self.close)
        lay.addWidget(btn_close)

        self._hdr = hdr
        return hdr

    def _build_body(self) -> QWidget:
        body = QWidget(self)
        body.setStyleSheet(f"background-color: {BG};")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 14, 18, 8)
        lay.setSpacing(16)

        lay.addWidget(self._section_log_path())
        lay.addWidget(self._divider())
        lay.addWidget(self._section_orientation())
        lay.addWidget(self._divider())
        lay.addWidget(self._section_behavior())

        return body

    def _section_log_path(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        lbl = QLabel("GAME.LOG PATH", w)
        lbl.setStyleSheet(f"font-family: {FONT_TITLE}; font-size: 9pt; font-weight: bold; color: {ACCENT};")
        lay.addWidget(lbl)

        hint = QLabel("Full path to your Star Citizen Game.log file.", w)
        hint.setStyleSheet(f"font-family: {FONT_BODY}; font-size: 8pt; color: {FG_DIM}; font-style: italic;")
        lay.addWidget(hint)

        row = QWidget(w)
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(6)

        self._log_path_edit = QLineEdit(row)
        self._log_path_edit.setText(self._settings.get("log_path", ""))
        self._log_path_edit.setPlaceholderText("C:/StarCitizen/LIVE/Game.log")
        self._log_path_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {BG_INPUT};
                color: {FG};
                border: 1px solid {BORDER};
                font-family: {FONT_BODY}; font-size: 9pt;
                padding: 4px 8px;
                border-radius: 2px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
        """)
        row_lay.addWidget(self._log_path_edit, 1)

        btn_browse = QPushButton("Browse…", row)
        btn_browse.setFixedHeight(28)
        btn_browse.setCursor(QCursor(Qt.PointingHandCursor))
        btn_browse.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG};
                border: 1px solid {BORDER};
                font-family: {FONT_BODY}; font-size: 8pt;
                padding: 2px 10px; border-radius: 2px;
            }}
            QPushButton:hover {{ background-color: {BG4}; border-color: {ACCENT}; }}
        """)
        btn_browse.clicked.connect(self._browse_log)
        row_lay.addWidget(btn_browse)

        lay.addWidget(row)
        return w

    def _section_orientation(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        lbl = QLabel("HUD ORIENTATION", w)
        lbl.setStyleSheet(f"font-family: {FONT_TITLE}; font-size: 9pt; font-weight: bold; color: {ACCENT};")
        lay.addWidget(lbl)

        hint = QLabel("Choose how weapon slots are arranged on screen.", w)
        hint.setStyleSheet(f"font-family: {FONT_BODY}; font-size: 8pt; color: {FG_DIM}; font-style: italic;")
        lay.addWidget(hint)

        radio_row = QWidget(w)
        radio_lay = QHBoxLayout(radio_row)
        radio_lay.setContentsMargins(0, 4, 0, 0)
        radio_lay.setSpacing(24)

        radio_style = f"""
            QRadioButton {{
                font-family: {FONT_BODY}; font-size: 9pt; color: {FG};
                spacing: 8px;
            }}
            QRadioButton::indicator {{
                width: 14px; height: 14px;
                border-radius: 7px;
                border: 1px solid {BORDER};
                background: {BG2};
            }}
            QRadioButton::indicator:checked {{
                background: {ACCENT};
                border-color: {ACCENT};
            }}
            QRadioButton:hover {{ color: {ACCENT}; }}
        """

        self._radio_h = QRadioButton("Horizontal  (wide bar)", radio_row)
        self._radio_h.setStyleSheet(radio_style)
        self._radio_v = QRadioButton("Vertical  (narrow column)", radio_row)
        self._radio_v.setStyleSheet(radio_style)

        orientation = self._settings.get("orientation", "horizontal")
        self._radio_h.setChecked(orientation == "horizontal")
        self._radio_v.setChecked(orientation == "vertical")

        grp = QButtonGroup(self)
        grp.addButton(self._radio_h)
        grp.addButton(self._radio_v)

        radio_lay.addWidget(self._radio_h)
        radio_lay.addWidget(self._radio_v)
        radio_lay.addStretch(1)
        lay.addWidget(radio_row)
        return w

    def _section_behavior(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        lbl = QLabel("BEHAVIOUR", w)
        lbl.setStyleSheet(f"font-family: {FONT_TITLE}; font-size: 9pt; font-weight: bold; color: {ACCENT};")
        lay.addWidget(lbl)

        cb_style = f"""
            QCheckBox {{
                font-family: {FONT_BODY}; font-size: 9pt; color: {FG};
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 14px; height: 14px;
                border: 1px solid {BORDER};
                background: {BG2}; border-radius: 2px;
            }}
            QCheckBox::indicator:checked {{
                background: {ACCENT};
                border-color: {ACCENT};
            }}
            QCheckBox:hover {{ color: {ACCENT}; }}
        """

        return w

    def _build_footer(self) -> QWidget:
        footer = QWidget(self)
        footer.setStyleSheet(f"background-color: {HEADER_BG}; border-top: 1px solid {BORDER};")
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(8)
        lay.addStretch(1)

        btn_cancel = QPushButton("Cancel", footer)
        btn_cancel.setFixedSize(80, 28)
        btn_cancel.setCursor(QCursor(Qt.PointingHandCursor))
        btn_cancel.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG_DIM};
                border: 1px solid {BORDER}; border-radius: 2px;
                font-family: {FONT_BODY}; font-size: 9pt;
            }}
            QPushButton:hover {{ color: {FG}; border-color: {FG_DIM}; }}
        """)
        btn_cancel.clicked.connect(self.close)
        lay.addWidget(btn_cancel)

        btn_save = QPushButton("Save", footer)
        btn_save.setFixedSize(80, 28)
        btn_save.setCursor(QCursor(Qt.PointingHandCursor))
        btn_save.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT}; color: #000;
                border: none; border-radius: 2px;
                font-family: {FONT_BODY}; font-size: 9pt; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #00e0ff; }}
        """)
        btn_save.clicked.connect(self._save)
        lay.addWidget(btn_save)

        return footer

    def _divider(self) -> QWidget:
        d = QWidget()
        d.setFixedHeight(1)
        d.setStyleSheet(f"background-color: {BORDER};")
        return d

    # ── Actions ──────────────────────────────────────────────────────────────

    def _browse_log(self) -> None:
        current = self._log_path_edit.text().strip()
        start_dir = os.path.dirname(current) if current else "C:/"
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Game.log", start_dir, "Log files (*.log);;All files (*)"
        )
        if path:
            self._log_path_edit.setText(path.replace("\\", "/"))

    def _save(self) -> None:
        self._settings["log_path"]          = self._log_path_edit.text().strip()
        self._settings["orientation"]       = "horizontal" if self._radio_h.isChecked() else "vertical"
        save_settings(self._settings)
        if self._on_save:
            self._on_save(dict(self._settings))
        self.close()

    # ── Drag-to-move ─────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._hdr.geometry().contains(event.position().toPoint()):
            self._dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            self._hdr.setCursor(QCursor(Qt.ClosedHandCursor))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._hdr.setCursor(QCursor(Qt.OpenHandCursor))
            event.accept()
        else:
            super().mouseReleaseEvent(event)
