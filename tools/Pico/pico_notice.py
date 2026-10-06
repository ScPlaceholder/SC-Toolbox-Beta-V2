"""Pico Pals: the legal notice, and the two places it is shown.

THE TEXT LIVES HERE AND NOWHERE ELSE. CHARACTER_LINE and SHORT_FORM below are
the only copy in the toolbox. Anything that needs the notice (the Customise
box, the About window, a script that publishes Pico Pals somewhere else) reads
it from this file:

    from pico_notice import CHARACTER_LINE, SHORT_FORM, text
    py pico_notice.py            prints the two lines
    py pico_notice.py --json     the same as JSON, for a script that is not Python

DO NOT TRANSLATE IT AND DO NOT REWORD IT. SHORT_FORM is legal wording: the
trademark sentence and the "not endorsed by or affiliated with" sentence are
taken from Cloud Imperium's fan kit documents and have to appear as written.
So neither constant is ever passed through _(), s_(), _t() or N_(), which is
what tools/extract_strings.py looks for when it builds the translators'
template; a translated toolbox still shows this text in English.
tests/test_pico_notice.py fails if a character of it changes, if it is wrapped
in a translation call, or if it stops being shown.

WHERE A USER SEES IT
  Customise Pico     at the bottom of the box. That box opens every time Pico
                     starts, so nobody has to look for the notice.
  About Pico Pals... in his right-click menu, a small window with the same text.
Never on the penguin himself, and never over him.

Cloud Imperium asks for the notice to be visible, legible and no smaller than
10 point: MIN_POINT_SIZE, applied in notice_label().

Text only. Cloud Imperium's "Made By The Community" logo has usage rules of its
own and is not used in the toolbox.

No Qt at import, so the text can be read by something with no display.
"""
from __future__ import annotations

CHARACTER_LINE = "Pico is a character from Star Citizen."

SHORT_FORM = (
    "Made By The Community. Star Citizen®, Roberts Space Industries® and Cloud Imperium® are "
    "registered trademarks of Cloud Imperium Rights LLC. Pico Pals is unofficial, free fan art, "
    "not endorsed by or affiliated with the Cloud Imperium or Roberts Space Industries group of "
    "companies. Not for sale."
)

MIN_POINT_SIZE = 10.0

TITLE = "About Pico Pals"
MENU_TEXT = "About Pico Pals..."
LABEL_NAME = "pico_notice"          # objectName of every label that shows the notice
WIDTH = 440                         # px the text wraps at: five or six lines at 10 point


def text() -> str:
    """The whole notice as shown: the character line, a blank line, the short form."""
    return CHARACTER_LINE + "\n\n" + SHORT_FORM


def notice_label(parent=None):
    """A label showing the whole notice: plain text, wrapped, selectable, at least MIN_POINT_SIZE."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    lbl = QLabel(text(), parent)
    lbl.setObjectName(LABEL_NAME)
    lbl.setTextFormat(Qt.PlainText)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
    font = lbl.font()
    if font.pointSizeF() < MIN_POINT_SIZE:      # also true (-1) when the font was given in pixels
        font.setPointSizeF(MIN_POINT_SIZE)
        lbl.setFont(font)
    lbl.setMinimumWidth(WIDTH)
    return lbl


def About(parent=None):
    """The About window (a QDialog): the notice and a Close button."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

    dlg = QDialog(parent)
    dlg.setWindowTitle(TITLE)
    # His window is always on top; a window of his that was not would open underneath the game.
    dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    lay = QVBoxLayout(dlg)
    dlg.notice = notice_label(dlg)
    lay.addWidget(dlg.notice)
    buttons = QDialogButtonBox(QDialogButtonBox.Close, dlg)
    buttons.rejected.connect(dlg.reject)
    buttons.accepted.connect(dlg.accept)
    lay.addWidget(buttons)
    return dlg


def main(argv=None) -> int:
    import json
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv:
        out = json.dumps({"character_line": CHARACTER_LINE, "short_form": SHORT_FORM}, ensure_ascii=False)
    else:
        out = text()
    sys.stdout.buffer.write((out + "\n").encode("utf-8"))      # the console's own code page has no (R)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
