"""Tutorial bubble popup for the Mining Loadout tool — PySide6."""
import shared.path_setup  # noqa: E402

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QFont, QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTextEdit, QPushButton, QFrame,
)

from shared.qt.theme import P

# ── Tab content ──────────────────────────────────────────────────────────────

_TABS = [
    {
        "label": "Overview",
        "content": [
            ("heading", "Mining Loadout Tool\n"),
            ("divider", "\u2500" * 52 + "\n\n"),
            ("value", "Plan and compare mining configurations\n"),
            ("value", "for Star Citizen mining ships.\n\n"),
            ("label", "The interface has three main areas:\n\n"),
            ("section", "  LEFT SIDEBAR\n"),
            ("label", "  Select your mining ship and access\n"),
            ("label", "  utility buttons (reset, copy stats).\n\n"),
            ("section", "  CENTER PANELS\n"),
            ("label", "  Configure each turret with a laser\n"),
            ("label", "  head and up to two modules. A gadget\n"),
            ("label", "  slot sits below the turrets.\n\n"),
            ("section", "  RIGHT STATS PANEL\n"),
            ("label", "  Live statistics update as you change\n"),
            ("label", "  your loadout. The total price is shown\n"),
            ("label", "  at the bottom.\n\n"),
            ("neutral", "  Use the tabs above to learn more \u2192\n"),
        ],
    },
    {
        "label": "Ship & Turrets",
        "content": [
            ("heading", "Ship Selection\n"),
            ("divider", "\u2500" * 52 + "\n\n"),
            ("label", "  Use the "),
            ("section", "sidebar ship buttons"),
            ("label", " to switch\n"),
            ("label", "  between mining ships (Prospector,\n"),
            ("label", "  MOLE, etc.). Each ship has a different\n"),
            ("label", "  number of turrets and laser sizes.\n\n"),
            ("heading", "Turret Panels\n"),
            ("divider", "\u2500" * 52 + "\n\n"),
            ("label", "  Each turret panel contains:\n\n"),
            ("section", "  LASER HEAD\n"),
            ("label", "  Select a mining laser from the\n"),
            ("label", "  dropdown. Lasers are filtered by the\n"),
            ("label", "  turret's size requirement.\n\n"),
            ("section", "  MODULE SLOTS (x2)\n"),
            ("label", "  Each laser supports up to 2 modules.\n"),
            ("label", "  The number of available slots depends\n"),
            ("label", "  on the selected laser. Passive modules\n"),
            ("label", "  are always active. Active modules have\n"),
            ("label", "  limited uses and duration.\n\n"),
            ("positive", "  Tip: "),
            ("label", "Click the "),
            ("neutral", "\u24d8 Details"),
            ("label", " link next to any\n"),
            ("label", "  dropdown to pin a detail card with\n"),
            ("label", "  full stats for that item.\n"),
        ],
    },
    {
        "label": "Stats & Gadgets",
        "content": [
            ("heading", "Stats Panel\n"),
            ("divider", "\u2500" * 52 + "\n\n"),
            ("label", "  The right panel shows live stats for\n"),
            ("label", "  your current loadout configuration:\n\n"),
            ("positive", "  Green"),
            ("label", " values = beneficial modifier\n"),
            ("negative", "  Red"),
            ("label", "   values = detrimental modifier\n"),
            ("neutral", "  Yellow"),
            ("label", " values = neutral/informational\n\n"),
            ("label", "  Stats include laser power, resistance,\n"),
            ("label", "  instability, charge window, charge\n"),
            ("label", "  rate, and more. All values update\n"),
            ("label", "  instantly when you change equipment.\n\n"),
            ("heading", "Gadget Slot\n"),
            ("divider", "\u2500" * 52 + "\n\n"),
            ("label", "  The "),
            ("section", "INVENTORY \u2014 GADGET"),
            ("label", " strip below\n"),
            ("label", "  the turret panels lets you equip one\n"),
            ("label", "  gadget. Gadgets apply ship-wide\n"),
            ("label", "  modifiers to your loadout.\n\n"),
            ("heading", "Loadout Price\n"),
            ("divider", "\u2500" * 52 + "\n\n"),
            ("label", "  Total cost in aUEC is shown at the\n"),
            ("label", "  bottom of the stats panel. Stock\n"),
            ("label", "  lasers are free.\n"),
        ],
    },
    {
        "label": "Tips",
        "content": [
            ("heading", "Useful Tips\n"),
            ("divider", "\u2500" * 52 + "\n\n"),
            ("section", "  DETAIL CARDS\n"),
            ("label", "  Click "),
            ("neutral", "\u24d8 Details"),
            ("label", " next to any laser, module,\n"),
            ("label", "  or gadget to pop out a floating card\n"),
            ("label", "  with full stats. Pin multiple cards to\n"),
            ("label", "  compare items side by side. Use the\n"),
            ("value", "  \u26b2 lock"),
            ("label", " icon to prevent auto-eviction.\n\n"),
            ("section", "  COPY STATS\n"),
            ("label", "  The "),
            ("positive", "\U0001f4cb COPY STATS"),
            ("label", " button copies your\n"),
            ("label", "  full loadout to the clipboard in a\n"),
            ("label", "  formatted text block \u2014 great for\n"),
            ("label", "  sharing builds with org mates.\n\n"),
            ("section", "  RESET LOADOUT\n"),
            ("label", "  Reverts all turrets to the ship's\n"),
            ("label", "  stock laser and clears all modules\n"),
            ("label", "  and gadgets.\n\n"),
            ("section", "  REFRESH DATA\n"),
            ("label", "  Click the "),
            ("neutral", "\u27f3"),
            ("label", " icon in the title bar to\n"),
            ("label", "  force-refresh pricing and item data\n"),
            ("label", "  from the UEX Corp API.\n\n"),
            ("section", "  CONFIGURATION\n"),
            ("label", "  Your loadout is saved automatically\n"),
            ("label", "  and restored when you reopen the tool.\n"),
        ],
    },
]

_TAG_COLORS = {
    "heading": P.tool_mining,
    "label": P.fg_dim,
    "value": P.fg_bright,
    "positive": P.green,
    "negative": P.red,
    "neutral": P.yellow,
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

        for i, tab in enumerate(_TABS):
            btn = QPushButton(tab["label"])
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
        tab_data = _TABS[idx]
        self._text_edit.clear()
        cursor = self._text_edit.textCursor()
        for tag, text in tab_data["content"]:
            cursor.insertText(text, _make_format(tag))
        self._text_edit.setTextCursor(cursor)
        self._text_edit.moveCursor(QTextCursor.Start)
