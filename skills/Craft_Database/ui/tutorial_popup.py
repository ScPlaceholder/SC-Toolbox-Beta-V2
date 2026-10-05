"""Tutorial popup for the Craft Database skill."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from ui.constants import (
    TOOL_COLOR,
    POPUP_BRACKET_LEN,
    CLOSE_BTN_BG,
    CLOSE_BTN_COLOR,
    CLOSE_BTN_HOVER_BG,
)

# ── Shared rich-text style fragments ─────────────────────────────────────

_B   = f"font-family: Consolas; color: {P.fg}; font-size: 9pt; line-height: 1.5;"
_DIM = f"color: {P.fg_dim};"
_ACC = f"color: {TOOL_COLOR};"
_GRN = f"color: {P.green};"
_YLW = f"color: {P.yellow};"

# Per-tab accent colors for h3 / h4 sub-headers
_C_START   = TOOL_COLOR          # teal   — Getting Started
_C_BROWSE  = "#44aaff"           # blue   — Browsing
_C_FILTER  = "#ffb347"           # amber  — Filters
_C_INV     = "#33dd88"           # green  — Inventory
_C_DETAIL  = "#cc88ff"           # purple — Detail Popup
_C_TIPS    = "#44dd88"           # green  — Tips

_FONT = "font-family: Electrolize, Consolas;"


def _h3(text: str, color: str) -> str:
    return f'<h3 style="{_FONT} color: {color};">{text}</h3>'


def _h4(text: str, color: str) -> str:
    return f'<h4 style="{_FONT} color: {color};">{text}</h4>'


def _html(body: str) -> str:
    return f'<div style="{_B}">{body}</div>'


def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_craft_db", "<shift>+7")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Shift+7"


_RED = f"color: {P.red};"


def _tab_getting_started() -> str:
    return _html(f"""
{_h3("Welcome to the Craft Database", _C_START)}
<p>Use this tool to look up any <b>crafting blueprint</b> in Star Citizen:
what it needs, how long it takes, and how you get it. The data is read from
the game's own files, as datamined by
<span style="{_ACC}">StarCitizenWiki/scunpacked-data</span>.</p>

{_h4("Opening it", _C_START)}
<p>Craft Database has no tile on the launcher. Open it with its hotkey,
<span style="{_ACC}">{_hotkey()}</span>. The same key hides the window
again, and it is shown in this window's title bar.</p>
<p>To change the key, or to switch the tool on, open the launcher's
<b>SETTINGS</b> and find the row marked <b>(no tile)</b> on the <b>Tools</b>
tab. The left toggle switches the tool on or off, the right toggle switches
its key on or off. A tool that is switched off has no hotkey.</p>

{_h4("Data Loading", _C_START)}
<p>The blueprints come from one pinned game build (shown at the top right,
e.g. <b>Game data: 4.10.1-LIVE</b>). They are downloaded once (about 4 MB)
with the <b>Download</b> button and kept on disk, so the tool then works
fully offline. The bar at the top shows the <b>BLUEPRINTS</b> and
<b>INGREDIENTS</b> counts.</p>

{_h4("Layout", _C_START)}
<p>The window is split into two areas:</p>
<ul>
  <li><b>Left panel</b> &mdash; <b>Filters</b>: <b>Obtainable</b>,
      <b>BLUEPRINT TYPE</b> and <b>RESOURCE NEEDED</b></li>
  <li><b>Center</b> &mdash; Search bar, blueprint grid, and pagination</li>
</ul>
""")


_TAB_BROWSING = _html(f"""
{_h3("Browsing Blueprints", _C_BROWSE)}

{_h4("Search Bar", _C_BROWSE)}
<p>Type any text to search across blueprint <b>name</b>, <b>category</b>,
and <b>ingredient names</b>. Results update automatically after a short
debounce delay.</p>

{_h4("Blueprint Cards", _C_BROWSE)}
<p>Each card shows:</p>
<ul>
  <li><b>Blueprint name</b> and <b>craft time</b></li>
  <li>An <b>Own</b> button to add the blueprint to your inventory</li>
  <li>Up to four <b>ingredient pills</b> with resource name and quantity.
      Materials are measured in <b>cSCU</b>; gems and parts are counted as
      whole pieces (<b>pcs</b>)</li>
  <li>How the blueprint is obtained: the number of <b>missions</b> that drop
      it, or <b>Known by default</b>, or the number of
      <b>mission reward pools</b> that give it when the individual missions
      are not known</li>
</ul>
<p>Click anywhere on a card (or the <span style="{_ACC}">↗</span> button)
to open a <b>detail popup</b> with full crafting information.</p>

{_h4("Pagination", _C_BROWSE)}
<p>Results are paged in sets of 50. Use the <b>Prev</b> / <b>Next</b>
buttons at the bottom to navigate. The result count shows how many
blueprints match the current filters.</p>
""")

_TAB_FILTERS = _html(f"""
{_h3("Filter Panel", _C_FILTER)}

{_h4("Obtainable", _C_FILTER)}
<p><b>On by default.</b> It keeps only blueprints a player can actually get:
known from the start, or given by at least one mission reward pool. The game
files also hold recipes nothing hands out yet &mdash; untick it to see those
too.</p>

{_h4("Blueprint type", _C_FILTER)}
<p><b>BLUEPRINT TYPE</b> filters by item type, e.g. <b>Weapons / Sniper</b>,
<b>Armour / Heavy / Core</b> or <b>Ship Components / Shield</b>. Picking a
top level (<b>Armour</b>) includes everything under it. Type to fuzzy-search
the dropdown.</p>

{_h4("Resource needed", _C_FILTER)}
<p><b>RESOURCE NEEDED</b> shows only blueprints that require a specific
crafting material, e.g. <b>Tungsten</b> or <b>Taranite</b>.</p>

{_h4("Clear all filters", _C_FILTER)}
<p>The red <b>Clear all filters</b> button at the bottom of the panel resets
both dropdowns at once and turns <b>Obtainable</b> back on.</p>

{_h4("Filtering by mission", _C_FILTER)}
<p>There is no filter for mission type, location or contractor here. To
start from a mission and see which blueprints it gives, use the
<b>Mission Database</b>.</p>
""")

_TAB_INVENTORY = _html(f"""
{_h3("Inventory", _C_INV)}

{_h4("Marking Blueprints as Owned", _C_INV)}
<p>Each blueprint card has an <b>Own</b> button in the header row. Click it
to add that blueprint to your personal inventory. Once owned, the button
changes to a greyed-out <b>Owned</b> label so you can see at a glance
which blueprints you already have.</p>

{_h4("Viewing Your Inventory", _C_INV)}
<p>Click the <b>INVENTORY (N)</b> button in the bar at the top of the
window. The number in parentheses shows how many blueprints you currently
own. When active, the button highlights and the grid switches to show only
your owned blueprints.</p>

{_h4("Filtering Your Inventory", _C_INV)}
<p>The search bar and the <b>BLUEPRINT TYPE</b> and <b>RESOURCE NEEDED</b>
filters work in inventory mode too, so you can find one blueprint among
your collection. <b>Obtainable</b> is not applied to your inventory.</p>

{_h4("Removing a Blueprint", _C_INV)}
<p>While viewing your inventory, each card shows an <b>Unown</b> button.
Clicking it opens a confirmation prompt &mdash; press <b>Yes</b> to
remove the blueprint from your inventory, or <b>No</b> to keep it.</p>

{_h4("Persistence", _C_INV)}
<p>Your inventory is saved locally and persists between sessions. You
do not need to re-mark blueprints each time you launch the tool.</p>
""")

_TAB_DETAIL = _html(f"""
{_h3("Blueprint Detail Popup", _C_DETAIL)}

{_h4("Opening a Popup", _C_DETAIL)}
<p>Click any blueprint card or its <span style="{_ACC}">↗</span> button.
Up to <b>5 detail popups</b> can be open simultaneously.</p>

{_h4("Global Quality Slider", _C_DETAIL)}
<p>Under <b>GLOBAL QUALITY</b>, drag the slider (or type in the spinbox) to
set a quality value from <b>0 to 1000</b>. Moving the global slider sets
<em>all</em> ingredient sliders to the same value at once.</p>

{_h4("Parts &amp; Per-Slot Quality", _C_DETAIL)}
<p>Under <b>PARTS</b>, each ingredient slot shows the resource name, the
amount, and its own <b>QUALITY</b> slider. Raw materials are given in
<b>cSCU</b>; gems and parts are counted as whole pieces (<b>pcs</b>). A slot
marked <b>(any 2 of 3)</b> means the recipe accepts a choice &mdash; you do
not need every material listed under it.</p>
<p>Adjust each ingredient's quality individually to see how different
combinations change the result. Quality effect tags
(<span style="{_GRN}">+%</span> / <span style="{_YLW}">&minus;%</span>)
update in real time as you move each slider.</p>

{_h4("Craft &amp; Dismantle Time", _C_DETAIL)}
<p>The row under the title shows <b>CRAFT TIME</b>, <b>TIERS</b> and, when
the item can be taken apart, <b>DISMANTLE</b>. Hover <b>DISMANTLE</b> to see
what share of the materials you get back.</p>

{_h4("Stat Summary", _C_DETAIL)}
<p>The <b>STAT SUMMARY</b> table below the parts lists every affected stat.
The <b>CRAFTED</b> column is its modifier at the quality you have set. When
several parts change the same stat, the table shows their combined
effect.</p>

{_h4("Drops &mdash; which missions give it", _C_DETAIL)}
<p>When the drops are known, the bottom of the popup has a
<b>DROPS (N MISSIONS)</b> section, split into
<span style="{_GRN}">LAWFUL</span> and <span style="{_RED}">UNLAWFUL</span>
and grouped by mission type. Each row gives the mission name, the
<b>contractor</b> who offers it, the <b>location</b>, and the
<b>drop chance</b>.</p>
<p><span style="{_YLW}">Two honest caveats.</span> The chances in one reward
pool can add up to less than 100% &mdash; some pools hold an empty slot that
consumes probability, and it is left in rather than inflating the real
numbers. And the mission lists come from the Mission Database's data, which
can be from a different game build than the blueprints, so treat a mission
list as a good guide, not a guarantee.</p>

{_h4("Obtained From &mdash; when drops are not known", _C_DETAIL)}
<p>Not every blueprint has known drops. For the rest you get
<b>OBTAINED FROM</b> instead: <b>Known by default</b>, or the reward pools
that give it as named in the game files, or <b>Not given by any mission in
this game build</b>.</p>
<p>An empty list means <b>no data</b>, never "nothing drops it".</p>

{_h4("Pin &amp; Close", _C_DETAIL)}
<p>Click <b>Pin</b> to keep a popup when you open new ones. Click
<b>Unpin</b> to release it. The red <b>x</b> closes a popup immediately.</p>
<p>Drag a popup anywhere on its surface to move it.</p>
""")

_TAB_TIPS = _html(f"""
{_h3("Tips &amp; Shortcuts", _C_TIPS)}

{_h4("Popup Overflow", _C_TIPS)}
<p>When you open a 6th popup, the oldest <b>unpinned</b> one is closed to
keep the screen tidy. If all five are pinned, the oldest pinned one is
closed instead.</p>

{_h4("Combined Filters", _C_TIPS)}
<p>All filters work together. For example, set
<b>RESOURCE NEEDED</b> to <b>Tungsten</b> and
<b>BLUEPRINT TYPE</b> to <b>Ship Components</b> to find components that need
Tungsten.</p>

{_h4("Fuzzy Search in Dropdowns", _C_TIPS)}
<p>Every filter dropdown supports fuzzy matching &mdash; you don't need to
type the exact name. Typing <b>sni</b> will find <b>Weapons / Sniper</b>.</p>

{_h4("Always-on-Top", _C_TIPS)}
<p>The window stays above Star Citizen so you can reference it in-game.
Drag the title bar to move it out of the way.</p>

{_h4("Data Refresh", _C_TIPS)}
<p>The data is pinned to one game build and does not expire. A toolbox
update moves it to a newer build; the tool then offers the download again.</p>

{_h4("No mission lists at all?", _C_TIPS)}
<p>The blueprints are downloaded by this tool, but the <b>mission drop
lists are not</b>. They are taken from the <b>Mission Database</b>'s own
downloaded data, once, when this tool first prepares its blueprint data. If
Mission Database had downloaded nothing on this PC at that moment, every
popup shows <b>OBTAINED FROM</b> and none lists missions.</p>
""")


def _tabs() -> list:
    """(title, html) per tab. Built when the popup opens, so the hotkey shown is the current one."""
    return [
        ("Getting Started", _tab_getting_started()),
        ("Browsing", _TAB_BROWSING),
        ("Filters", _TAB_FILTERS),
        ("Inventory", _TAB_INVENTORY),
        ("Detail Popup", _TAB_DETAIL),
        ("Tips", _TAB_TIPS),
    ]


# ── Close button ─────────────────────────────────────────────────────────


class _CloseBtn(QPushButton):
    def __init__(self, parent=None):
        super().__init__("x", parent)
        self.setObjectName("tutClose")
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton#tutClose {{
                background: {CLOSE_BTN_BG};
                color: {CLOSE_BTN_COLOR};
                border: none;
                border-radius: 3px;
                font-family: Consolas;
                font-size: 13pt;
                font-weight: bold;
                padding: 0px;
                margin: 2px;
                min-height: 0px;
            }}
            QPushButton#tutClose:hover {{
                background-color: {CLOSE_BTN_HOVER_BG};
                color: #ffffff;
            }}
        """)


# ── Scrollable tab content ───────────────────────────────────────────────


def _make_tab(html: str, parent: QWidget) -> QScrollArea:
    scroll = QScrollArea(parent)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet(f"""
        QScrollArea {{ background: transparent; border: none; }}
        QScrollBar:vertical {{
            background: {P.scrollbar_bg}; width: 6px; border: none;
        }}
        QScrollBar::handle:vertical {{
            background: {P.scrollbar_handle}; min-height: 20px; border-radius: 3px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    """)
    lbl = QLabel(html)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.RichText)
    lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    lbl.setStyleSheet(f"background: transparent; padding: 16px; color: {P.fg};")
    lbl.setOpenExternalLinks(True)
    scroll.setWidget(lbl)
    return scroll


# ── Tutorial popup ───────────────────────────────────────────────────────


class TutorialPopup(QDialog):
    """Tabbed tutorial popup for the Craft Database.

    Singleton: a second call just raises the existing window.
    """

    _instance: Optional[TutorialPopup] = None

    def __new__(cls, parent: Optional[QWidget] = None):
        if cls._instance is not None and cls._instance.isVisible():
            cls._instance.raise_()
            cls._instance.activateWindow()
            return cls._instance
        instance = super().__new__(cls)
        cls._instance = instance
        return instance

    def __init__(self, parent: Optional[QWidget] = None):
        # Avoid re-running __init__ on repeated calls (singleton)
        if getattr(self, "_initialised", False):
            return
        self._initialised = True

        super().__init__(parent)
        self._drag_pos: QPoint | None = None

        self.setWindowTitle("Craft Database — Tutorial")
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(620, 500)
        self.setMinimumSize(480, 360)

        # Centre near parent
        if parent:
            pg = parent.geometry()
            x = pg.x() + (pg.width() - 620) // 2
            y = pg.y() + (pg.height() - 500) // 2
            self.move(max(0, x), max(0, y))

        self._build()
        self.show()

    # ── Build ────────────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        frame = QWidget(self)
        frame.setStyleSheet("background-color: rgba(11, 14, 20, 230);")
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(0, 0, 0, 0)
        frame_lay.setSpacing(0)

        # ── Title bar
        title_bar = QWidget(frame)
        title_bar.setFixedHeight(34)
        title_bar.setStyleSheet(f"background-color: {P.bg_header};")
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(12, 0, 4, 0)
        tb_lay.setSpacing(8)

        title_lbl = QLabel("CRAFT DATABASE  \u2014  TUTORIAL", title_bar)
        title_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace;"
            f"font-size: 11pt; font-weight: bold;"
            f"color: {TOOL_COLOR}; letter-spacing: 2px; background: transparent;"
        )
        tb_lay.addWidget(title_lbl)
        tb_lay.addStretch(1)

        close_btn = _CloseBtn(title_bar)
        close_btn.clicked.connect(self.close)
        tb_lay.addWidget(close_btn)

        frame_lay.addWidget(title_bar)

        # ── Tabbed content
        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background: transparent;
            }}
            QTabBar::tab {{
                background: {P.bg_secondary};
                color: {P.fg_dim};
                border: none;
                padding: 6px 14px;
                font-family: Consolas;
                font-size: 9pt;
                font-weight: bold;
            }}
            QTabBar::tab:selected {{
                background: #0e2220;
                color: {TOOL_COLOR};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)

        for tab_title, html in _tabs():
            tabs.addTab(_make_tab(html, tabs), tab_title)

        frame_lay.addWidget(tabs, 1)
        outer.addWidget(frame)

    # ── Paint: border + corner brackets ─────────────────────────────────

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        edge = QColor(TOOL_COLOR)
        edge.setAlpha(100)
        painter.setPen(QPen(edge, 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        bl = POPUP_BRACKET_LEN
        bracket = QColor(TOOL_COLOR)
        bracket.setAlpha(200)
        painter.setPen(QPen(bracket, 2))
        painter.drawLine(0, 0, bl, 0)
        painter.drawLine(0, 0, 0, bl)
        painter.drawLine(w - 1, 0, w - 1 - bl, 0)
        painter.drawLine(w - 1, 0, w - 1, bl)
        painter.drawLine(0, h - 1, bl, h - 1)
        painter.drawLine(0, h - 1, 0, h - 1 - bl)
        painter.drawLine(w - 1, h - 1, w - 1 - bl, h - 1)
        painter.drawLine(w - 1, h - 1, w - 1, h - 1 - bl)
        painter.end()

    # ── Drag support ─────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    # ── Cleanup ──────────────────────────────────────────────────────────

    def closeEvent(self, event):
        TutorialPopup._instance = None
        super().closeEvent(event)
