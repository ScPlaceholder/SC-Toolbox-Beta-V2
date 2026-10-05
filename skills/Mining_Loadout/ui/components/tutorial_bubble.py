"""Tutorial bubble popup for the Mining Loadout tool — PySide6."""
import re
import textwrap

import shared.path_setup  # noqa: E402

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QFont, QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTextEdit, QPushButton, QFrame,
)

from shared.qt.theme import P

# ── Tab content ──────────────────────────────────────────────────────────────

# The text is written as paragraphs and wrapped here, to the width the bubble
# shows without a horizontal scroll bar. A name the user can read in the tool
# (a button, a heading, a tick box) is written [[like this]]: it is drawn in
# the "ui" colour, and tests/test_tutorial.py checks that every one of them is
# still a string in the tool's source.

_WRAP = 52
_NAME = re.compile(r"\[\[(.+?)\]\]")
_NBSP = " "


def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_mining", "<shift>+4")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Shift+4"


def _fragments(line: str, tag: str = "label") -> list:
    """One line as (tag, text) pairs, with each [[name]] as a "ui" pair."""
    out, pos = [], 0
    for m in _NAME.finditer(line):
        if m.start() > pos:
            out.append((tag, line[pos:m.start()]))
        out.append(("ui", m.group(1).replace(_NBSP, " ")))
        pos = m.end()
    if pos < len(line):
        out.append((tag, line[pos:]))
    return out


def _p(text: str, tag: str = "label", indent: str = "  ") -> list:
    """A wrapped paragraph followed by a blank line. A [[name]] is never split across lines."""
    text = _NAME.sub(lambda m: "[[" + m.group(1).replace(" ", _NBSP) + "]]", " ".join(text.split()))
    # The brackets are not drawn, so they must not count towards the width.
    bare = _NAME.sub(lambda m: "\x01" + m.group(1) + "\x02", text)
    lines = textwrap.wrap(bare, width=_WRAP, initial_indent=indent, subsequent_indent=indent,
                          break_long_words=False, break_on_hyphens=False)
    out = []
    for i, line in enumerate(lines):
        line = line.replace("\x01", "[[").replace("\x02", "]]")
        out += _fragments(line + ("\n\n" if i == len(lines) - 1 else "\n"), tag)
    return out


def _h(text: str) -> list:
    return [("heading", text + "\n"), ("divider", "─" * 52 + "\n\n")]


def _s(text: str) -> list:
    """A sub-heading in the tutorial's own words (not a name on screen)."""
    return [("section", "  " + text + "\n")]


def _tabs() -> list:
    """The tabs, built when the bubble opens so the hotkey shown is the current one."""
    return [
        {
            "label": "Overview",
            "content": (
                _h("Mining Loadout")
                + _p("Use this tool to build a mining ship's loadout and see what "
                     "it does to laser power, resistance, instability and the rest, "
                     "before you buy the parts.", "value")
                + _s("LEFT")
                + _p("Pick your ship under [[MINING SHIP:]]. [[RESET LOADOUT]] and "
                     "[[COPY STATS]] are below the ship buttons.")
                + _s("CENTER")
                + _p("One panel for each turret, with a laser and its modules. The "
                     "[[INVENTORY — GADGET]] strip is under the turrets.")
                + _s("RIGHT")
                + _p("[[LOADOUT STATS]] changes as you change parts. "
                     "[[LOADOUT PRICE]] is at the bottom of it.")
                + _s("HOTKEY")
                + _p("The launcher's hotkey for this tool is " + _hotkey() + ". It "
                     "shows and hides the window.")
            ),
        },
        {
            "label": "Turrets",
            "content": (
                _h("Ship")
                + _p("Click [[PROSPECTOR]], [[MOLE]] or [[GOLEM]]. The MOLE has "
                     "three turrets; the other two have one. Changing ship puts "
                     "that ship's stock laser in every turret and clears the "
                     "modules.")
                + _h("Turret panels")
                + _p("[[LASER HEAD]] is the mining laser. The dropdown lists only "
                     "lasers of the turret's size, shown as [[SIZE]] in the panel "
                     "header.")
                + _p("[[MODULE SLOT]] 1, 2 and 3 take the laser's modules. A laser "
                     "with fewer slots shows fewer of them.")
                + _p("Click [[Details]] beside a laser or a module to open a card "
                     "with all of its numbers and [[WHERE TO BUY]] it.")
                + _h("Gadget")
                + _p("Pick one gadget in the [[INVENTORY — GADGET]] strip. The "
                     "ⓘ beside it opens the gadget's card.")
            ),
        },
        {
            "label": "Crafted",
            "content": (
                _h("Crafted lasers")
                + _p("If you crafted the laser yourself, tick [[CRAFTED]] under "
                     "it. A row appears for each part of the laser. Enter the "
                     "quality of the material you used for each one.")
                + _p("The tool then shows what the parts do, as [[Laser power]] "
                     "and a percentage, and the stats on the right use it.")
                + _p("Choosing a different laser clears the tick.")
                + _s("WHEN YOU CANNOT TICK IT")
                + _p("The tick box is greyed out when the game has no crafting "
                     "blueprint for that laser. It is not shown at all when the "
                     "crafting data is not on this PC; the Craft Database tool "
                     "downloads it.")
                + _s("HOW THE PARTS ADD UP")
                + _p("[[CRAFTED PARTS COMBINE:]] chooses between [[Multiplied]] "
                     "and [[Averaged]]. The game does not say which is right and "
                     "it has not been measured, so this is a guess you can "
                     "change. Hover it for the details.")
                + _s("IN MINING SIGNALS")
                + _p("A loadout you save here keeps its crafted lasers, and "
                     "Mining Signals uses them when it loads the file.")
            ),
        },
        {
            "label": "Stats",
            "content": (
                _h("Stats panel")
                + _p("[[LOADOUT STATS]] adds up the whole loadout: [[Min Power]], "
                     "[[Max Power]], [[Resistance]], [[Instability]], "
                     "[[Opt Chrg Wnd]], [[Opt Chrg Rate]] and more.")
                + [("positive", "  Green"), ("label", " means the change helps you.\n"),
                   ("negative", "  Red"), ("label", " means it works against you.\n\n")]
                + _h("Price")
                + _p("[[LOADOUT PRICE]] is the total in aUEC. The laser a ship "
                     "comes with costs nothing. The same total is in the bar "
                     "along the bottom as [[Loadout Price:]].")
                + _h("Detail cards")
                + _p("A card opened with [[Details]] stays open and can be "
                     "dragged, so you can put two side by side. Up to 5 can be "
                     "open. Opening a sixth closes the oldest. Tick the ⚲ on "
                     "a card to keep it; if all five are kept, a new one does "
                     "not open.")
            ),
        },
        {
            "label": "Save & Tips",
            "content": (
                _h("Keeping a loadout")
                + _p("[[SAVE]] in the title bar writes the ship, lasers, modules, "
                     "gadget and crafted parts to a file. [[LOAD]] opens one and "
                     "switches to its ship.")
                + _p("Save a loadout you want to keep. Do not count on the tool "
                     "to bring back what you had when you closed it.")
                + _h("Other buttons")
                + _p("[[COPY STATS]] copies the loadout and its stats to the "
                     "clipboard as text, to paste to your crew.")
                + _p("[[RESET LOADOUT]] puts the stock laser back in every "
                     "turret and clears the modules, the gadget and the crafted "
                     "ticks.")
                + _p("⟳ in the title bar fetches the items and prices again "
                     "from UEX. The tool does this by itself once the data is a "
                     "day old.")
            ),
        },
    ]


def tutorial_markup() -> str:
    """The whole tutorial as markup with each on-screen name in <b>: what the tests read."""
    import html
    return "".join(("<b>%s</b>" % html.escape(text)) if tag == "ui" else html.escape(text)
                   for tab in _tabs() for tag, text in tab["content"])


_TAG_COLORS = {
    "heading": P.tool_mining,
    "label": P.fg_dim,
    "value": P.fg_bright,
    "positive": P.green,
    "negative": P.red,
    "neutral": P.yellow,
    "ui": P.yellow,             # a name the user can read in the tool; see _fragments
    "divider": P.separator,
    "section": P.accent,
}


def _make_format(tag: str) -> QTextCharFormat:
    fmt = QTextCharFormat()
    color = _TAG_COLORS.get(tag, P.fg)
    fmt.setForeground(QColor(color))
    font = QFont("Consolas", 9)
    if tag in ("heading", "value", "positive", "negative", "section"):
        font.setBold(True)
    if tag == "heading":
        font.setPointSize(10)
    fmt.setFont(font)
    return fmt


# ── Bubble widget ────────────────────────────────────────────────────────────

class TutorialBubble(QWidget):
    """Floating tutorial popup with tabbed content."""

    def __init__(self, parent_window: QWidget, opacity: float = 0.95):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setObjectName("tutorialBubble")
        self.setWindowOpacity(opacity)
        self.setStyleSheet(f"QWidget#tutorialBubble {{ background-color: {P.bg_header}; }}")
        self.setFixedSize(440, 480)

        self._parent_window = parent_window
        self._current_tab = 0
        self._tab_btns: list[QPushButton] = []

        self._tabs = _tabs()
        self._build_ui()
        self._select_tab(0)
        self._position_near_parent()

    def _position_near_parent(self):
        pw = self._parent_window
        pos = pw.pos()
        size = pw.size()
        cx = pos.x() + (size.width() - self.width()) // 2
        cy = pos.y() + (size.height() - self.height()) // 2
        self.move(cx, cy)

    def _build_ui(self):
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # ── Title bar ────────────────────────────────────────────────────
        bar = QWidget()
        bar.setObjectName("tutorialBar")
        bar.setFixedHeight(36)
        bar.setStyleSheet(f"QWidget#tutorialBar {{ background-color: {P.bg_header}; }}")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 0, 6, 0)
        bar_lay.setSpacing(4)

        # Drag support
        bar._drag_pos = QPoint()

        def drag_press(e):
            if e.button() == Qt.LeftButton:
                bar._drag_pos = e.globalPosition().toPoint() - self.pos()
                e.accept()

        def drag_move(e):
            if e.buttons() & Qt.LeftButton:
                self.move(e.globalPosition().toPoint() - bar._drag_pos)
                e.accept()

        bar.mousePressEvent = drag_press
        bar.mouseMoveEvent = drag_move

        title_lbl = QLabel("  TUTORIAL")
        title_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 10pt;
            font-weight: bold;
            color: {P.tool_mining};
            background: transparent;
        """)
        bar_lay.addWidget(title_lbl)

        sub_lbl = QLabel("Mining Loadout")
        sub_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 7pt;
            color: {P.fg_dim};
            background: transparent;
        """)
        bar_lay.addWidget(sub_lbl)
        bar_lay.addStretch(1)

        close_btn = QPushButton("x")
        close_btn.setObjectName("cardClose")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton#cardClose {
                background: rgba(255, 60, 60, 0.15);
                color: #cc6666;
                border: none;
                font-family: Consolas;
                font-size: 13pt;
                font-weight: bold;
                border-radius: 3px;
                padding: 0px;
                margin: 2px;
                min-height: 0px;
            }
            QPushButton#cardClose:hover {
                background-color: rgba(220, 50, 50, 0.85);
                color: #ffffff;
            }
        """)
        close_btn.clicked.connect(self.close)
        bar_lay.addWidget(close_btn)

        main_lay.addWidget(bar)

        # ── Accent line ──────────────────────────────────────────────────
        accent = QFrame()
        accent.setFixedHeight(1)
        accent.setStyleSheet(f"background-color: {P.tool_mining};")
        main_lay.addWidget(accent)

        # ── Tab bar ──────────────────────────────────────────────────────
        tab_bar = QWidget()
        tab_bar.setObjectName("tutorialTabBar")
        tab_bar.setStyleSheet(f"QWidget#tutorialTabBar {{ background-color: {P.bg_secondary}; }}")
        tab_lay = QHBoxLayout(tab_bar)
        tab_lay.setContentsMargins(6, 4, 6, 4)
        tab_lay.setSpacing(4)

        for i, tab in enumerate(self._tabs):
            btn = QPushButton(tab["label"].replace("&", "&&"))     # a lone & is Qt's shortcut mark
            btn.setObjectName(f"tutTab_{i}")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda checked=False, idx=i: self._select_tab(idx))
            tab_lay.addWidget(btn)
            self._tab_btns.append(btn)

        tab_lay.addStretch(1)
        main_lay.addWidget(tab_bar)

        # ── Content area ─────────────────────────────────────────────────
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: {P.bg_secondary};
                color: {P.fg};
                border: none;
                font-family: Consolas;
                font-size: 9pt;
                padding: 14px 10px;
                selection-background-color: {P.selection};
            }}
        """)
        main_lay.addWidget(self._text_edit, 1)

    def _select_tab(self, idx: int):
        self._current_tab = idx

        # Update tab button styles
        for i, btn in enumerate(self._tab_btns):
            obj = f"tutTab_{i}"
            if i == idx:
                btn.setStyleSheet(f"""
                    QPushButton#{obj} {{
                        background-color: {P.bg_input};
                        color: {P.tool_mining};
                        border: 1px solid {P.tool_mining};
                        border-radius: 3px;
                        font-family: Consolas;
                        font-size: 8pt;
                        font-weight: bold;
                        padding: 2px 8px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton#{obj} {{
                        background-color: {P.bg_card};
                        color: {P.fg_dim};
                        border: 1px solid {P.border};
                        border-radius: 3px;
                        font-family: Consolas;
                        font-size: 8pt;
                        padding: 2px 8px;
                    }}
                    QPushButton#{obj}:hover {{
                        background-color: {P.bg_input};
                        color: {P.fg_bright};
                        border-color: {P.fg_dim};
                    }}
                """)

        # Fill content
        tab_data = self._tabs[idx]
        self._text_edit.clear()
        cursor = self._text_edit.textCursor()
        for tag, text in tab_data["content"]:
            cursor.insertText(text, _make_format(tag))
        self._text_edit.setTextCursor(cursor)
        self._text_edit.moveCursor(QTextCursor.Start)
