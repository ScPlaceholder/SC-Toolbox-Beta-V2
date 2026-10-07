"""chat_offer.py - the pop-up that asks whether to get Gemma, the heavier model for talking with the companions.

The words are chat_models.offer()'s; this file only puts them in a box. The default button, and Escape, and closing
the box, are all "no": nothing is downloaded and nothing is chosen unless the yes button itself is pressed. When
there is nothing to offer (the PC has no room for the model right now, or Ollama does not answer) the box has one
button and says why.

    if chat_offer.ask(window, chat_models.offer(models, free)):
        ...                                    # the player said yes
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

CORE = Path(__file__).resolve().parent.parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import chat_models                                    # noqa: E402

try:
    from shared.qt.theme import P                     # the toolbox palette
except Exception:                                     # standalone / tests
    class P:                                          # type: ignore[no-redef]
        fg, fg_dim, bg_input, border, bg_deepest, bg_primary = "#ddd", "#999", "#222", "#444", "#111", "#161a20"

ACCENT = "#7fd1b9"


def _style() -> str:
    """The toolbox theme gives every widget a transparent background and light text, which on a bare message box is
    light text on the system's own window colour. So the box is given the theme's background itself."""
    return (f"QMessageBox {{ background: {P.bg_primary}; }}"
            f"QMessageBox QLabel {{ color: {P.fg}; font-size: 10pt; }}"
            f"QPushButton {{ background: {P.bg_input}; color: {P.fg}; border: 1px solid {P.border};"
            f" border-radius: 4px; padding: 6px 14px; }}"
            f"QPushButton:default {{ border-color: {ACCENT}; }}"
            f"QPushButton:hover {{ border-color: {ACCENT}; }}")


def build(parent, offer) -> QMessageBox:
    """The box, not yet shown. box.yes_button is None when nothing is offered."""
    box = QMessageBox(parent)
    box.setStyleSheet(_style())
    box.setWindowTitle(chat_models.OFFER_TITLE)
    box.setIcon(QMessageBox.Question if offer.yes else QMessageBox.Information)
    box.setText(offer.headline)
    box.setInformativeText(offer.body)
    no = box.addButton(offer.no, QMessageBox.RejectRole)
    yes = box.addButton(offer.yes, QMessageBox.AcceptRole) if offer.yes else None
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    box.yes_button, box.no_button = yes, no
    return box


def said_yes(box: QMessageBox) -> bool:
    return box.yes_button is not None and box.clickedButton() is box.yes_button


def ask(parent, offer) -> bool:
    """Show the box and wait. True only when the yes button was pressed."""
    box = build(parent, offer)
    box.exec()
    return said_yes(box)


def confirm(parent, title: str, text: str, yes: str, no: str = "Cancel") -> bool:
    """A yes/no question in the same box, default no. True only when the yes button was pressed."""
    box = QMessageBox(parent)
    box.setStyleSheet(_style())
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning)
    box.setText(text)
    no_button = box.addButton(no, QMessageBox.RejectRole)
    yes_button = box.addButton(yes, QMessageBox.AcceptRole)
    box.setDefaultButton(no_button)
    box.setEscapeButton(no_button)
    box.exec()
    return box.clickedButton() is yes_button
