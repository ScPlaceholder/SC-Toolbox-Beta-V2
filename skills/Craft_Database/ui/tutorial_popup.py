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
from ui.constants import TOOL_COLOR

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
_C_DETAIL  = "#cc88ff"           # purple — Detail Popup
_C_TIPS    = "#44dd88"           # green  — Tips

_FONT = "font-family: Electrolize, Consolas;"


def _h3(text: str, color: str) -> str:
    return f'<h3 style="{_FONT} color: {color};">{text}</h3>'


def _h4(text: str, color: str) -> str:
    return f'<h4 style="{_FONT} color: {color};">{text}</h4>'


def _html(body: str) -> str:
    return f'<div style="{_B}">{body}</div>'


_TAB_GETTING_STARTED = _html(f"""
{_h3("Welcome to the Craft Database", _C_START)}
<p>This tool lets you browse and filter all <b>crafting blueprints</b>
available in Star Citizen, powered by
<span style="{_ACC}">sc-craft.tools</span> data.</p>

{_h4("Data Loading", _C_START)}
<p>On launch the tool fetches blueprint data from sc-craft.tools and caches it
locally for <b>1 hour</b>. The stats bar at the top shows the total blueprint
count, unique ingredient count, and current game version once loading is complete.</p>

{_h4("Layout", _C_START)}
<p>The window is split into two areas:</p>
<ul>
  <li><b>Left panel</b> &mdash; Filter controls (category, resource, mission type,
      location, contractor, ownable toggle)</li>
  <li><b>Center</b> &mdash; Search bar, blueprint grid, and pagination</li>
</ul>

{_h4("Hotkey", _C_START)}
<p>The default global hotkey to show / hide this window is
<b>Shift + 7</b>. You can reassign it in the SC Toolbox settings.</p>
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
  <li>Up to four <b>ingredient pills</b> with resource name and quantity</li>
  <li>Number of <b>source missions</b> that reward this blueprint</li>
</ul>
<p>Click anywhere on a card (or the <span style="{_ACC}">\u2197</span> button)
to open a <b>detail popup</b> with full crafting information.</p>

{_h4("Pagination", _C_BROWSE)}
<p>Results are paged in sets of 50. Use the <b>Prev</b> / <b>Next</b>
buttons at the bottom to navigate. The result count shows how many
blueprints match the current filters.</p>
""")

_TAB_FILTERS = _html(f"""
{_h3("Filter Panel", _C_FILTER)}

{_h4("Ownable Only", _C_FILTER)}
<p>Check this box to show only blueprints that can be <b>owned</b>
(i.e. learnable by the player rather than faction-locked).</p>

{_h4("Category", _C_FILTER)}
<p>Filter by item type + subtype, e.g. <b>Weapons / Sniper</b> or
<b>Armour / Combat / Heavy</b>. Type to fuzzy-search the dropdown.</p>

{_h4("Resource", _C_FILTER)}
<p>Show only blueprints that require a specific crafting material,
e.g. <b>Tungsten</b> or <b>Taranite</b>.</p>

{_h4("Mission Type", _C_FILTER)}
<p>Filter blueprints by the type of mission that drops them,
e.g. <b>Mercenary</b>, <b>Bounty Hunter</b>, or <b>Delivery</b>.</p>

{_h4("Location", _C_FILTER)}
<p>Show only blueprints dropped by missions available in a specific
star system or location, e.g. <b>Pyro</b> or <b>Stanton</b>.</p>

{_h4("Contractor", _C_FILTER)}
<p>Filter by the mission-giving faction, e.g. <b>BHG</b>, <b>Shubin</b>,
or <b>CfP</b>.</p>

{_h4("Clear Filters", _C_FILTER)}
<p>Click the <span style="{_DIM}">Clear Filters</span> button at the bottom
of the filter panel to reset all dropdowns and the ownable checkbox at once.</p>
""")

_TAB_DETAIL = _html(f"""
{_h3("Blueprint Detail Popup", _C_DETAIL)}

{_h4("Opening a Popup", _C_DETAIL)}
<p>Click any blueprint card or its <span style="{_ACC}">\u2197</span> button.
Up to <b>5 detail popups</b> can be open simultaneously.</p>

{_h4("Global Quality Slider", _C_DETAIL)}
<p>Drag the slider (or type in the spinbox) to set a quality value from
<b>0 to 1000</b>. Every ingredient card and the stat summary table update
in real time &mdash; no reload needed.</p>

{_h4("Parts", _C_DETAIL)}
<p>Each ingredient slot shows the resource name, quantity in <b>cSCU</b>,
and a read-only quality indicator that tracks the global slider. Quality
effect tags (<span style="{_GRN}">+%</span> / <span style="{_YLW}">&minus;%</span>)
show how quality affects each stat for that slot.</p>

{_h4("Stat Summary", _C_DETAIL)}
<p>A table below the parts lists every affected stat with its crafted
modifier at the current quality value.</p>

{_h4("Source Missions", _C_DETAIL)}
<p>If the blueprint is rewarded by missions, they are listed at the
bottom grouped by lawfulness and mission type. Each entry shows the
mission name, contractor, location, and drop chance.</p>

{_h4("Pin &amp; Close", _C_DETAIL)}
<p>Click <span style="{_GRN}">Pin</span> to lock a popup in place so it
is never auto-closed when you open a new one. Click <b>Unpin</b> to
release it. The red <b>x</b> closes a popup immediately.</p>
<p>Drag the title bar to reposition any popup anywhere on screen.</p>
""")

_TAB_TIPS = _html(f"""
{_h3("Tips &amp; Shortcuts", _C_TIPS)}

{_h4("Popup Overflow", _C_TIPS)}
<p>When you open a 6th popup, the oldest <b>unpinned</b> one is automatically
closed to keep the screen tidy. Pinned popups are never auto-closed.</p>

{_h4("Combined Filters", _C_TIPS)}
<p>All filters work together. For example, set <b>Resource = Tungsten</b> and
<b>Location = Pyro</b> to find blueprints that need Tungsten <em>and</em>
are rewarded by Pyro missions.</p>

{_h4("Fuzzy Search in Dropdowns", _C_TIPS)}
<p>Every filter dropdown supports fuzzy matching &mdash; you don't need to
type the exact name. Typing <b>sni</b> will find <b>Weapons / Sniper</b>.</p>

{_h4("Always-on-Top", _C_TIPS)}
<p>The window stays above Star Citizen so you can reference it in-game.
Drag the title bar to move it out of the way.</p>

{_h4("Data Refresh", _C_TIPS)}
<p>Cached data expires after 1 hour. Restart the tool or send a
<b>refresh</b> IPC command to force a fresh fetch from sc-craft.tools.</p>
""")

_TABS = [
    ("Getting Started", _TAB_GETTING_STARTED),
    ("Browsing", _TAB_BROWSING),
    ("Filters", _TAB_FILTERS),
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
        self.setStyleSheet("""
            QPushButton#tutClose {
                background: rgba(255, 60, 60, 0.15);
                color: #cc6666;
                border: none;
                border-radius: 3px;
                font-family: Consolas;
                font-size: 13pt;
                font-weight: bold;
                padding: 0px;
                margin: 2px;
                min-height: 0px;
            }
            QPushButton#tutClose:hover {
                background-color: rgba(220, 50, 50, 0.85);
                color: #ffffff;
            }
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

        for tab_title, html in _TABS:
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

        bl = 14
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
