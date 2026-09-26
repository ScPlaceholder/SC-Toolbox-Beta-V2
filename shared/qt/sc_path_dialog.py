"""First-launch popup: link the Star Citizen install folder once for every tool.

Shown by the launcher on first run (see ``shared.sc_install``).  It
auto-detects first, so most people only click Confirm; Browse accepts the
install root, a channel folder (LIVE/PTU...) or a Game.log; Skip leaves every
tool auto-detecting on its own, as before, and the popup does not return.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from shared import sc_install
from shared.i18n import _ as _t
from shared.qt.theme import P


def _button(text: str, primary: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setMinimumHeight(34)
    border = P.energy_cyan if primary else P.border
    color = P.fg_bright if primary else P.fg
    btn.setStyleSheet(
        f"QPushButton {{ background: {P.bg_input}; color: {color}; "
        f"border: 1px solid {border}; border-radius: 6px; padding: 6px 18px; "
        f"font-family: Consolas; font-size: 10pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {P.energy_cyan}; }} "
        f"QPushButton:disabled {{ color: {P.fg_dim}; border-color: {P.border}; }}")
    return btn


class ScPathDialog(QDialog):
    """Ask once where Star Citizen lives.  ``self.root`` holds the saved root."""

    def __init__(self, parent=None, detected: Optional[str] = None) -> None:
        super().__init__(parent)
        self.root: Optional[str] = None
        self._candidate: Optional[str] = detected
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setObjectName("scPathCard")
        card.setStyleSheet(
            f"#scPathCard {{ background: {P.bg_card}; border: 1px solid {P.energy_cyan};"
            f" border-radius: 8px; }}")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(12)

        title = QLabel(_t("Where is Star Citizen installed?"))
        title.setStyleSheet(
            f"color: {P.energy_cyan}; font-family: Consolas; font-size: 13pt; font-weight: bold;")
        sub = QLabel(_t("Several tools read the game's logs (Battle Buddy, PlayTime, "
                        "Suit companion, Mission Database). Link the folder once and "
                        "they all use it. Change it any time in Settings \u2192 Tools."))
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        lay.addWidget(title)
        lay.addWidget(sub)

        self._path = QLabel()
        self._path.setWordWrap(True)
        self._path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._path.setStyleSheet(
            f"background: {P.bg_input}; color: {P.fg_bright}; border: 1px solid {P.border};"
            f" border-radius: 6px; padding: 8px 10px; font-family: Consolas; font-size: 10pt;")
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet("font-size: 9pt;")
        lay.addWidget(self._path)
        lay.addWidget(self._status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._skip = _button(_t("Skip"))
        self._browse = _button(_t("Browse…"))
        self._confirm = _button(_t("Confirm"), primary=True)
        self._skip.clicked.connect(self._on_skip)
        self._browse.clicked.connect(self._on_browse)
        self._confirm.clicked.connect(self._on_confirm)
        row.addWidget(self._skip)
        row.addStretch(1)
        row.addWidget(self._browse)
        row.addWidget(self._confirm)
        lay.addLayout(row)

        self._refresh()

    # ── state ──

    def _refresh(self) -> None:
        if self._candidate:
            self._path.setText(self._candidate)
            self._status.setText(_t("Found it. Confirm if this is right, or Browse to pick another."))
            self._status.setStyleSheet(f"color: {P.green}; font-size: 9pt;")
            self._confirm.setEnabled(True)
        else:
            self._path.setText(_t("(not found automatically)"))
            self._status.setText(_t("Browse to the StarCitizen folder (the one containing "
                                    "LIVE), or to the LIVE folder itself."))
            self._status.setStyleSheet(f"color: {P.yellow}; font-size: 9pt;")
            self._confirm.setEnabled(False)

    def _on_browse(self) -> None:
        start = self._candidate or "C:/"
        picked = QFileDialog.getExistingDirectory(
            self, _t("Select your StarCitizen folder"), start)
        if not picked:
            return
        root = sc_install.normalise_root(picked)
        if root:
            self._candidate = root
            self._refresh()
        else:
            self._path.setText(picked)
            self._status.setText(_t("That folder doesn't contain LIVE, PTU or any other game "
                                    "channel. Pick the StarCitizen folder that does."))
            self._status.setStyleSheet(f"color: {P.red}; font-size: 9pt;")
            self._confirm.setEnabled(False)

    def _on_confirm(self) -> None:
        if not self._candidate:
            return
        try:
            self.root = sc_install.set_sc_root(self._candidate)
        except (ValueError, OSError) as exc:
            self._status.setText(str(exc))
            self._status.setStyleSheet(f"color: {P.red}; font-size: 9pt;")
            return
        self.accept()

    def _on_skip(self) -> None:
        try:
            sc_install.mark_prompt_done()
        except OSError:
            pass            # worst case: the popup asks again next launch
        self.reject()


def maybe_prompt_for_sc_path(parent=None) -> Optional[str]:
    """Show the popup if it has never been answered; returns the saved root."""
    if not sc_install.first_launch_prompt_needed():
        return sc_install.get_sc_root()
    dlg = ScPathDialog(parent, detected=sc_install.detect_sc_root())
    # Centre on the launcher's SCREEN, not the launcher: the launcher can sit in a
    # corner, and a popup centred on it could hang off the edge.
    dlg.adjustSize()
    screen = parent.screen() if parent is not None else None
    if screen is None:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
    if screen is not None:
        g = screen.availableGeometry()
        dlg.move(g.center().x() - dlg.width() // 2, g.center().y() - dlg.height() // 2)
    dlg.exec()
    return dlg.root
