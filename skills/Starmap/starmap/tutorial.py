"""Tutorial popup for the Starmap skill.

Same shape as the Craft Database and Mission Database popups: a frameless,
draggable, always-on-top tabbed dialog with corner brackets, opened from a
"? Tutorial" button in the title bar.

Every button label, menu entry and mouse gesture named below was read out of
the source, not a commit message:
  panel.py            Home / Route / In-Game / Grocery / Market / Commodities,
                      "< Back", the "   >   " breadcrumb, the context menus
  voice_control.py    Mic: Push-to-talk | Always on, Set Mic Keybind,
                      Calibrate Star Map, Voice Replies
  grocery.py          X, clear, Plot shopping route
  market_view.py      Filter terminals..., Search items everywhere...,
                      Pop out, + Grocery
  location_dialog.py  the Commodities / Items tabs
  galaxy_view.py etc. drag rotate / wheel zoom / right-drag pan /
                      double-click enter / right-click lore
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

TOOL_COLOR = P.energy_cyan
POPUP_BRACKET_LEN = 14
CLOSE_BTN_BG = "rgba(255, 60, 60, 0.15)"
CLOSE_BTN_COLOR = "#cc6666"
CLOSE_BTN_HOVER_BG = "rgba(220, 50, 50, 0.85)"

# ── Shared rich-text style fragments ─────────────────────────────────────

_B = f"font-family: Consolas; color: {P.fg}; font-size: 9pt; line-height: 1.5;"
_DIM = f"color: {P.fg_dim};"
_ACC = f"color: {TOOL_COLOR};"
_GRN = f"color: {P.green};"
_YLW = f"color: {P.yellow};"

# Per-tab accent colors for h3 / h4 sub-headers
_C_START = TOOL_COLOR       # cyan   — Getting Started
_C_MOVE = "#44aaff"         # blue   — Moving Around
_C_ROUTE = "#ffb347"        # amber  — Jump Routes
_C_MARKET = "#33dd88"       # green  — Market & Items
_C_GROC = "#cc88ff"         # purple — Grocery List
_C_VOICE = "#ff8fc7"        # pink   — Voice

_FONT = "font-family: Electrolize, Consolas;"


def _h3(text: str, color: str) -> str:
    return f'<h3 style="{_FONT} color: {color};">{text}</h3>'


def _h4(text: str, color: str) -> str:
    return f'<h4 style="{_FONT} color: {color};">{text}</h4>'


def _html(body: str) -> str:
    return f'<div style="{_B}">{body}</div>'


_TAB_GETTING_STARTED = _html(f"""
{_h3("Welcome to the Star Map", _C_START)}
<p>A map of the whole Star Citizen universe you can fly around with the
mouse, plus what everything <b>costs</b> and <b>where to buy it</b>: terminal
item prices, commodity prices, a shopping list, and jump routes you can push
into the game.</p>

{_h4("The map has four levels", _C_START)}
<p>You start looking at the <b>galaxy</b>, and go inwards:</p>
<ul>
  <li><b>Galaxy</b> &mdash; every system</li>
  <li><b>System</b> &mdash; its star, planets and moons</li>
  <li><b>Planet &amp; Moons</b> &mdash; one planet's own little system</li>
  <li><b>Planet</b> &mdash; its surface locations</li>
</ul>
<p><b>Double-click</b> anything to go in. <b>&lt; Back</b> goes out one level,
and the trail beside it (<span style="{_DIM}">Galaxy &gt; Stanton &gt;
&hellip;</span>) is clickable &mdash; click any step to jump straight there.</p>

{_h4("Finding something by name", _C_START)}
<p>Use <b>Search system or location&hellip;</b> in the top bar. It matches
loosely, so you do not need the exact spelling, and it takes you there.</p>

{_h4("The three side panels", _C_START)}
<p>Three buttons on the right of the top bar open and close panels beside the
map. They stay how you leave them:</p>
<ul>
  <li><b>Grocery</b> &mdash; your shopping list</li>
  <li><b>Market</b> &mdash; the Item Finder: what a terminal sells, and where
      to find one item anywhere</li>
  <li><b>Commodities</b> &mdash; every commodity and its UEX prices</li>
</ul>

{_h4("Offline and online", _C_START)}
<p>The systems, planets and moons are <b>built in</b>, so the map itself
always works. Prices come from <b>UEX</b> over the internet and are cached on
disk, so a recent price list survives going offline. There is nothing to
download by hand.</p>
""")

_TAB_MOVING = _html(f"""
{_h3("Flying the camera", _C_MOVE)}
<p>It is all mouse, and it is the same at every level of the map. The hint
line along the bottom of the view says so too.</p>

{_h4("The four gestures", _C_MOVE)}
<ul>
  <li><b>Left-drag</b> &mdash; rotate. Swing around to see depth.</li>
  <li><b>Wheel</b> &mdash; zoom in and out.</li>
  <li><b>Right-drag</b> &mdash; pan, to slide the whole view sideways.</li>
  <li><b>Double-click</b> &mdash; go into whatever you clicked.</li>
</ul>

{_h4("Right-click for the lore", _C_ROUTE)}
<p><b>Right-click a system without dragging</b> and you get a card about it:
who holds it and what happened there. Easy to miss, because right-drag is
also pan &mdash; the difference is whether you move the mouse.</p>
<p>Not every system is a place you can fly to. The line under a system's name
tells you which: <span style="{_ACC}">in-game &mdash; double-click to
enter</span>, or <span style="{_YLW}">lore system &mdash; double-click to look
around</span>. A lore system has no detailed map data yet.</p>

{_h4("Home", _C_MOVE)}
<ul>
  <li><b>Left-click Home</b> &mdash; snap the view back to your home system,
      wherever you have wandered off to.</li>
  <li><b>Right-click Home</b> &mdash; <b>Change home system&hellip;</b>, if you
      move base. The map asks you to pick one the first time.</li>
  <li>Your home is marked on the galaxy view with a
      <span style="{_ACC}">&#8962;</span>.</li>
</ul>
""")

_TAB_ROUTES = _html(f"""
{_h3("Plotting a jump route", _C_ROUTE)}

{_h4("Two clicks", _C_ROUTE)}
<ol>
  <li>Press <b>Route</b>. It changes to <b>Cancel</b> and the hint line reads
      <span style="{_DIM}">click route points</span>.</li>
  <li><b>Click the system you are starting from</b>, then
      <b>click the destination</b>.</li>
</ol>
<p>The route is drawn across the galaxy and summarised at the bottom with the
number of <b>jumps</b> and the distance in <b>light years</b>. If there is no
chain of jump points between the two, it says so rather than guessing.</p>

{_h4("Clearing it", _C_ROUTE)}
<p>Once a route is drawn the button reads <b>Clear route</b>; press it to wipe
it. While you are still picking points it reads <b>Cancel</b> and backs out
without drawing anything.</p>

{_h4("Setting the route inside the game", _C_MARKET)}
<p>Turn on <b>In-Game</b> and a route you ask for <b>by voice</b> is also
punched into Star Citizen's own map for you &mdash; it opens the map, types
the destination and sets it.</p>
<p><span style="{_YLW}">It has to be calibrated first</span>, because it works
by clicking your screen and every screen is different.
<b>Right-click Route</b> and choose
<b>Calibrate in-game route setter&hellip;</b>, or press
<b>Calibrate Star Map</b> on the voice bar. It walks you through three clicks
and remembers them.</p>
<p style="{_DIM}">Until it is calibrated, In-Game has nothing to aim at.
Plotting on this map, on its own, needs no calibration at all.</p>
""")

_TAB_MARKET = _html(f"""
{_h3("Prices, and where to buy things", _C_MARKET)}

{_h4("Clicking a place on the map", _C_MARKET)}
<p>Click a station, city or outpost and you get its own window with two tabs:
<b>Commodities</b> (what it buys and sells, at what price) and <b>Items</b>
(the gear on its shelves).</p>

{_h4("Market &mdash; the Item Finder", _C_MARKET)}
<p>The <b>Market</b> panel answers it from the other end. Two search boxes,
and they do different jobs:</p>
<ul>
  <li><b>Filter terminals&hellip;</b> &mdash; narrow the list of terminals,
      then pick one to see everything it stocks.</li>
  <li><b>Search items everywhere&hellip;</b> &mdash; name one item and find
      <em>every</em> place in the universe that sells it, with prices.</li>
</ul>
<p><b>Double-click a row</b> (or press <b>Pop out</b>) for the full detail
bubble on that item. <b>+ Grocery</b> puts the selected item on your shopping
list.</p>

{_h4("Commodities", _C_MARKET)}
<p>The <b>Commodities</b> panel lists every commodity in the game &mdash;
search by <b>name, code or category</b>. A <span style="{_YLW}">(!)</span>
badge means it is illegal to haul. Click a card for its own page, with the
price history and a <b>Wiki</b> link, and <b>&#10547; Routes</b> to trade it.</p>
<p style="{_DIM}">Price trend lines need several days of UEX snapshots before
they have anything to draw, so a brand-new install shows none.</p>
""")

_TAB_GROCERY = _html(f"""
{_h3("The shopping list", _C_GROC)}
<p>Planning a kit-up run: collect everything you need to buy, then have the
map draw the shortest trip that visits all of it.</p>

{_h4("Getting things onto the list", _C_GROC)}
<p>Open it with the <b>Grocery</b> button, then either:</p>
<ul>
  <li><b>Drag a row</b> from the Market panel, or a popped-out item bubble,
      and drop it on the list. The bubble says
      <span style="{_DIM}">drag me onto the Grocery List</span>.</li>
  <li>Press <b>+ Grocery</b> in the Market panel with a row selected.</li>
  <li>Press <b>&#65291; add to Grocery List</b> on an item's pop-out.</li>
</ul>
<p>The list header counts what is on it, e.g.
<span style="{_ACC}">GROCERY LIST (6)</span>.</p>

{_h4("Taking things off", _C_GROC)}
<ul>
  <li><b>X</b> on a row removes that one item.</li>
  <li><b>clear</b> in the header empties the whole list.</li>
</ul>

{_h4("Plot shopping route", _C_GROC)}
<p>Press it and the map draws the shortest route through every location on the
list, <b>in visit order</b>. It follows the list live &mdash; add or remove
something and the route redraws itself, so you can shuffle the list and watch
the trip get shorter.</p>
""")

_TAB_VOICE = _html(f"""
{_h3("Talking to the map", _C_VOICE)}
<p>You can drive the map by voice, hands on the stick: <em>navigate to
Daymar</em>, <em>clear route</em>, <em>show Hurston</em>. Speech is recognised
<b>on this PC</b>, not in the cloud.</p>

{_h4("How the mic opens", _C_VOICE)}
<p>The <b>Mic:</b> pair on the voice bar chooses:</p>
<ul>
  <li><b>Push-to-talk</b> &mdash; hold a key while you speak. Quieter, and it
      cannot be set off by the game's own audio.</li>
  <li><b>Always on</b> &mdash; the mic stays open; just talk.</li>
</ul>

{_h4("Set Mic Keybind", _C_VOICE)}
<p>Press it, then press whatever you want to use: a <b>key, mouse button,
joystick or gamepad button</b>. Bind it to something on your HOTAS and you
never take a hand off the controls.</p>
<p><span style="{_YLW}">Push-to-talk with no key bound does nothing</span>,
so set one, or switch to Always on.</p>

{_h4("Voice Replies", _C_VOICE)}
<p>Toggle it to have the map <b>say confirmations out loud</b>
(<em>Navigate to&hellip;</em>) instead of only printing them. Handy when you
are looking at the game and not at this window.</p>

{_h4("What can I say?", _C_VOICE)}
<p><b>Right-click the voice bar</b> for <b>Voice commands help</b> &mdash; it
prints the whole list of commands into the status line. The same menu also
has <b>Set trigger</b>, the <b>Mode</b> choice, and <b>Whisper model</b>
(<b>tiny.en</b>, <b>base.en</b>, <b>small.en</b>, <b>medium.en</b> &mdash;
bigger hears better and costs more time).</p>
<p style="{_DIM}">The status line beside the buttons is where the map answers:
what it heard, what it did, or that it did not understand. If it says voice
ears need a package installed, that is why nothing is listening.</p>

{_h4("Voice and the game together", _C_GROC)}
<p>With <b>In-Game</b> on, a spoken route is set in Star Citizen too &mdash;
see the <b>Jump Routes</b> tab, and calibrate it first.</p>
""")

_TABS = [
    ("Getting Started", _TAB_GETTING_STARTED),
    ("Moving Around", _TAB_MOVING),
    ("Jump Routes", _TAB_ROUTES),
    ("Market & Items", _TAB_MARKET),
    ("Grocery List", _TAB_GROCERY),
    ("Voice", _TAB_VOICE),
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
    """Tabbed tutorial popup for the Star Map.

    Singleton: a second call just raises the existing window.
    """

    _instance: Optional["TutorialPopup"] = None

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

        self.setWindowTitle("Star Map — Tutorial")
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

        title_lbl = QLabel("STAR MAP  —  TUTORIAL", title_bar)
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
                background: #0e1e26;
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
