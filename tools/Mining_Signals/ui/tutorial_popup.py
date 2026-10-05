"""Tutorial popup for Mining Signals — matches the Craft Database format."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

TOOL_COLOR = "#33dd88"
_BRACKET_LEN = 18

# ── Shared rich-text style fragments ─────────────────────────────────────

_B   = f"font-family: Consolas; color: {P.fg}; font-size: 9pt; line-height: 1.5;"
_DIM = f"color: {P.fg_dim};"
_ACC = f"color: {TOOL_COLOR};"
_GRN = f"color: {P.green};"
_YLW = f"color: {P.yellow};"

_FONT = "font-family: Electrolize, Consolas;"

_C_START  = TOOL_COLOR
_C_SCAN   = "#44aaff"
_C_TABLE  = "#ffb347"
_C_TIPS   = "#cc88ff"
_C_SHIPS  = "#ffaa22"
_C_ROSTER = "#00e7ff"
_C_BREAK  = "#ff5533"
_C_CHART  = "#44ccbb"
_C_REFINERY = "#cc88ff"


def _h3(text: str, color: str) -> str:
    return f'<h3 style="{_FONT} color: {color};">{text}</h3>'


def _h4(text: str, color: str) -> str:
    return f'<h4 style="{_FONT} color: {color};">{text}</h4>'


def _html(body: str) -> str:
    return f'<div style="{_B}">{body}</div>'


# ═══════════════════════════════════════════════════════════════════════════
# Tab content
# ═══════════════════════════════════════════════════════════════════════════

def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_mining_signals", "<ctrl>+1")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Ctrl+1"


def _tab_getting_started() -> str:
    return _html(f"""
{_h3("Welcome to Mining Signals", _C_START)}
<p>Use Mining Signals while you mine. It reads the numbers off your ship's
scanner and mining HUD, tells you what a rock is and whether you can break
it, and keeps track of your refinery jobs and your crew.</p>

{_h4("Quick Setup", _C_START)}
<p>All of this is on the <b>Scanner</b> tab.</p>
<ol>
  <li>Click <b>Set Scanning Region</b> and draw a box round the signature
      panel on your scanner: the location-pin icon <em>and</em> the
      number.</li>
  <li>Click <b>Set Mining HUD Region</b> and draw a box round the SCAN
      RESULTS panel of your mining HUD.</li>
  <li>Click <b>Set Mining Output Display Location</b> and click where you
      want the result to appear.</li>
  <li>Click <b>Start Scan</b>.</li>
</ol>
<p>The first time you press each of the first two, a short tip shows exactly
what to box. Tick <b>Do not show again</b> on a tip when you no longer need
it.</p>

{_h4("The tabs", _C_START)}
<ul>
  <li><b>Mining Chart</b> &mdash; which resource is found where</li>
  <li><b>Scanner</b> &mdash; scanning, the signal table and the break
      check</li>
  <li><b>Mining Ships</b> &mdash; load the loadouts the break check uses</li>
  <li><b>Gadgets</b> &mdash; how many of each gadget you carry</li>
  <li><b>Refinery</b> &mdash; your refinery orders</li>
  <li><b>Mining Roster</b> &mdash; players, teams and ships for a group
      operation</li>
</ul>

{_h4("Data and hotkey", _C_START)}
<p>The signal table comes from a community spreadsheet, is refreshed every
hour, and is kept on this PC so the tool still works offline.</p>
<p>The launcher's hotkey for this tool is
<span style="{_ACC}">{_hotkey()}</span>. It shows and hides the window, and it
is shown in the title bar.</p>
""")


_TAB_CHART = _html(f"""
{_h3("Mining Chart", _C_CHART)}
<p>The <b>Mining Chart</b> tab is a table of locations against resources,
titled <b>Live Mining Chart</b>. Each cell is how much of that resource the
rocks at that location hold on average. The data comes from scmdb.net and is
kept for a day.</p>

{_h4("Reading it", _C_CHART)}
<ul>
  <li>A plain percentage is a resource you can scan for there.</li>
  <li>A dim value starting with <b>~</b> is a trace: the resource turns up
      inside other rocks there, but you cannot scan for it.</li>
  <li><b>Ship Mining</b> and <b>FPS / ROC Mining</b> switch between what a
      mining ship finds and what you find on foot or in a ROC.</li>
</ul>

{_h4("Finding the best place", _C_CHART)}
<ul>
  <li>Type in <b>Resource:</b> or <b>Location:</b> to narrow the table.</li>
  <li>Click a resource's column heading, or one of its cells, to sort the
      locations by that resource. Click again to reverse, and a third time
      to stop sorting.</li>
  <li><b>Highest</b> and <b>Lowest</b> set the sort direction. <b>Clear
      Sort</b> puts the table back in its usual order.</li>
  <li>Hold <b>Ctrl</b> and scroll to zoom. <b>Reset Scale</b> undoes it.</li>
</ul>

{_h4("Keeping it in view", _C_CHART)}
<p><b>Pop-out Chart</b> opens a floating copy you can keep beside the game
while you scan. <b>Fullscreen</b> switches the whole window between
fullscreen and normal. <b>Refresh</b> fetches the data again.</p>
""")

_TAB_SCANNING = _html(f"""
{_h3("Scanning", _C_SCAN)}

{_h4("How it works", _C_SCAN)}
<p>Two readers run while a scan is on. One reads the signal number from your
scanner and looks it up in the signal table. The other reads mass, resistance
and instability from the mining HUD and feeds the break check.</p>

{_h4("Set Scanning Region", _C_SCAN)}
<p>Draw the box round the whole signature panel: the location-pin icon on
the left and the number on the right, with a small margin. About 150 by 50
pixels or larger works well. <b>Do not</b> box only the digits: the scanner
needs the icon to find the panel.</p>

{_h4("Set Mining HUD Region", _C_SCAN)}
<p>Draw the box from just above the SCAN RESULTS title to just below the
INSTABILITY row. Leave out the COMPOSITION list under it.</p>

{_h4("Where results appear", _C_SCAN)}
<p><b>Set Mining Output Display Location</b> places the result bubble.
<b>Set Break Bubble Location</b> places the break check's own panel. For
both, click where you want it, or press Esc to cancel.</p>

{_h4("While scanning", _C_SCAN)}
<p><b>Start Scan</b> turns into <b>Stop Scan</b> and the window shrinks to a
small bar. Results can take several seconds. If one looks wrong, look away
from the rock until the bubble clears, then look back.</p>

{_h4("If the numbers read wrongly", _C_SCAN)}
<ul>
  <li><b>Calibrate Mining Crops</b> lets you show the tool exactly where
      each value sits on your screen. It needs a mining HUD region first. The
      window it opens has its own written tutorial and a spoken one.</li>
  <li><b>Game Resolution</b> tells the tool the resolution the game runs
      at, when it cannot work it out by itself.</li>
</ul>

{_h4("Which ship does the break check use?", _C_SCAN)}
<p><b>Choose Mining Ship</b> picks one of the loadouts you loaded on the
<b>Mining Ships</b> tab, or the whole <b>Mining Ops Fleet</b>.
<b>Calc: Fleet</b> and <b>Calc: Team</b> choose between counting every
ship in the fleet and only your team's ships from the <b>Mining Roster</b>.</p>

{_h4("No scan needed", _C_SCAN)}
<p>Type a rock's numbers into <b>Mass:</b> and <b>Resistance %:</b> to run
the break check by hand. A value read from the HUD replaces what you
typed.</p>

{_h4("Requirements", _C_SCAN)}
<ul>
  <li>Star Citizen should run in <b>Borderless Windowed</b> mode. A game in
      exclusive fullscreen is captured as a black picture.</li>
</ul>
""")

_TAB_TABLE = _html(f"""
{_h3("Signal Table", _C_TABLE)}

{_h4("Reading the table", _C_TABLE)}
<p>The table on the <b>Scanner</b> tab lists every known resource with its
signal value for 1 to 6 rocks. Click a column heading to sort.
<b>Double-click</b> a row to open that resource in its own popup, which you
can <b>Pin</b>.</p>

{_h4("Looking one up by hand", _C_TABLE)}
<p>Type a number in <b>Signal value...</b> to find the resources it could
be, or part of a name in <b>Resource name...</b></p>

{_h4("Rarity", _C_TABLE)}
<ul>
  <li><span style="color:#8cc63f;"><b>Common</b></span> &mdash; most
      frequently found</li>
  <li><span style="color:#00bcd4;"><b>Uncommon</b></span></li>
  <li><span style="color:#ffc107;"><b>Rare</b></span></li>
  <li><span style="color:#aa66ff;"><b>Epic</b></span></li>
  <li><span style="color:#ff9800;"><b>Legendary</b></span> &mdash; the
      rarest</li>
</ul>
<p>The table also has rows for ROC, FPS and salvage signals.</p>

{_h4("When two resources share a number", _C_TABLE)}
<p>Some resources give the same signal value at different rock counts. The
result bubble lists <b>all possible matches</b>, so you can tell them apart
from what you see.</p>
""")

_TAB_SHIPS = _html(f"""
{_h3("Mining Ships and Gadgets", _C_SHIPS)}

{_h4("Mining", _C_SHIPS)}
<p>On the <b>Mining Ships</b> tab, the <b>Mining</b> page has a slot each
for <b>Golem</b>, <b>Prospector</b> and <b>Mole</b>. Press <b>Load</b> on a
slot and pick a loadout you saved in the Mining Loadout tool. <b>Clear</b>
empties the slot. A laser saved as crafted keeps its crafted power here.</p>

{_h4("Mining Ops Fleet", _C_SHIPS)}
<p><b>Add Ship</b> adds a loadout to the fleet. The first ship in the fleet
is yours. <b>Expand Fleet</b> lists the fleet so you can remove ships one at
a time, and <b>Clear Fleet</b> empties it.</p>

{_h4("Salvage", _C_SHIPS)}
<p>On the <b>Salvage</b> page, <b>Add Salvage Ship</b> loads a loadout saved
in the DPS Calculator. Salvage ships show up in the <b>Mining Roster</b>.
They are not counted in the break check.</p>

{_h4("Gadgets tab", _C_SHIPS)}
<p>Set how many of each gadget you carry. The break check uses a gadget
only when lasers and modules are not enough. Tick
<b>Always use best gadget</b> to have it use the strongest one every
time.</p>
<p><b>Mining Foreman Console</b> shows what the fleet has left: gadgets, and
the remaining uses of each turret's active modules. <b>Refresh All
Modules</b> and <b>Refresh All Gadgets</b> set them back to full.</p>
""")

_TAB_REFINERY = _html(f"""
{_h3("Refinery", _C_REFINERY)}
<p>The <b>Refinery</b> tab keeps a list of the refinery orders you have
placed, so you know what is ready and where.</p>

{_h4("Adding orders", _C_REFINERY)}
<ol>
  <li>Click <b>Set Refinery Region</b> and draw a box round the part of
      the screen where the refinery terminal shows your order.</li>
  <li>Click <b>Scan Now</b> to read it once, or turn on <b>Auto-Scan</b> to
      read it every few seconds while you place orders.</li>
</ol>
<p style="{_DIM}">Scan Now does nothing while the mining scan is
running.</p>

{_h4("The lists", _C_REFINERY)}
<ul>
  <li><b>Orders In Process</b> &mdash; still refining, with <b>Rename</b>
      and <b>Delete</b>.</li>
  <li><b>Orders Complete</b> &mdash; ready to collect. An order moves here
      by itself when your game log says it finished. Press <b>Mark Picked
      Up</b> when you have collected it.</li>
  <li><b>Picked Up</b> &mdash; your history.</li>
</ul>
<p>Double-click an order to open it in its own popup. If orders never
complete, press <b>Set Log Path</b> and choose your LIVE folder.</p>

{_h4("Where to refine", _C_REFINERY)}
<ul>
  <li><b>Locations</b> lists the refineries. <b>Near me</b> sorts them from
      where the game log last placed you. Click one to see its yields.</li>
  <li><b>Yields</b> compares every refinery for each mineral.</li>
</ul>
""")

_TAB_ROSTER = _html(f"""
{_h3("Mining Roster", _C_ROSTER)}
<p>The <b>Mining Roster</b> tab is for organising a group: who is in it,
which team they are in, and which ship they are on.</p>

{_h4("Left panel: Player Roster", _C_ROSTER)}
<ul>
  <li>Type a name and press Enter or click <b>Add Player</b>.</li>
  <li><b>Import</b> and <b>Export</b> load and save the list of players as
      a file.</li>
  <li>The search box filters the players by name.</li>
</ul>

{_h4("Right-click a player", _C_ROSTER)}
<ul>
  <li><b>Set as User</b> &mdash; marks which player is you. The canvas
      moves to your ship or team.</li>
  <li><b>Set as Foreman</b> &mdash; puts them in charge of the fleet.</li>
  <li><b>Promote to Leader</b> &mdash; makes them a team leader.</li>
  <li><b>Promote to Strike Group Leader</b> &mdash; for a player in a
      strike group.</li>
  <li><b>Assign Profession</b> &mdash; choose one of 23 professions.</li>
  <li><b>Remove Player</b> &mdash; takes them off the roster.</li>
</ul>
<p>Right-click the player again to undo any of the first four.</p>

{_h4("Key", _C_ROSTER)}
<p>The <b>Key</b> page beside <b>Players</b> lists every profession and
its icon.</p>

{_h4("Right panel: Ship Fleet", _C_ROSTER)}
<p><b>Ship Fleet</b> lists the mining and salvage ships you loaded and the
support ships you added. <b>Drag</b> a ship onto the canvas to place
it.</p>

{_h4("Add Fleet Support", _C_ROSTER)}
<p>The buttons under <b>Add Fleet Support</b> add a ship that does not
mine: <b>Hauling</b>, <b>Repair</b>, <b>Refuel</b>, <b>Escort</b>,
<b>Mothership</b> or <b>Medical</b>. Each asks you to pick the ship model.
The list of models comes from Item Finder's data, so open Item Finder once
if it is empty.</p>
""")

_TAB_CANVAS = _html(f"""
{_h3("The canvas", _C_ROSTER)}
<p>The middle of the <b>Mining Roster</b> tab draws the group as boxes
joined by lines: the foreman at the top, teams under them, ships under
teams, and the crew on each ship.</p>

{_h4("Moving around", _C_ROSTER)}
<ul>
  <li><b>Drag empty canvas</b>, with the left or the middle button, to
      slide the view.</li>
  <li><b>Scroll</b> to zoom.</li>
</ul>

{_h4("Arranging", _C_ROSTER)}
<ul>
  <li><b>Drag a team</b> and everything under it moves with it.</li>
  <li><b>Drop a ship near a team</b> and it joins that team.</li>
  <li><b>Drop a team near another team</b>, or near the foreman, and it
      goes under it.</li>
  <li><b>Double-click</b> the foreman, a team or a strike group to rename
      it.</li>
  <li><b>Right-click a ship</b> to delete it, assign it to a team or take
      it out of one.</li>
</ul>

{_h4("Motherships and strike groups", _C_ROSTER)}
<ul>
  <li>Add a <b>Mothership</b> from <b>Add Fleet Support</b>, then
      right-click it and choose <b>Add Strike Group</b>.</li>
  <li>Drag a ship from <b>Ship Fleet</b> onto a strike group to put it in
      that group.</li>
</ul>

{_h4("Clusters", _C_ROSTER)}
<ul>
  <li>Right-click a team and choose <b>Assign to Cluster</b>, then a
      letter from A to Z.</li>
  <li>The <b>Clusters:</b> bar above the canvas has a tick box for each
      cluster. Untick one to dim its teams. <b>All</b> and <b>None</b> tick
      or untick every one.</li>
</ul>
""")

_TAB_BREAK = _html(f"""
{_h3("Can I break it?", _C_BREAK)}
<p>The break check works out whether your lasers can crack a rock, from the
rock's mass and resistance and the loadouts you loaded.</p>

{_h4("How it calculates", _C_BREAK)}
<p>Power needed = mass &times; 0.2 / (1 &minus; effective resistance).
Effective resistance is the rock's resistance after your lasers, modules
and gadgets have changed it.</p>

{_h4("What it tries, in order", _C_BREAK)}
<p>Lasers alone first, then with active modules, then with a gadget. The
result says which of those it took.</p>

{_h4("Who it counts", _C_BREAK)}
<ol>
  <li><b>Solo</b> &mdash; your ship only</li>
  <li><b>Team</b> &mdash; the mining ships in your team</li>
  <li><b>Cluster</b> &mdash; teams from your cluster</li>
  <li><b>Fleet</b> &mdash; everyone</li>
</ol>
<p>When it needed more than your own ship, the result names how far it had
to go.</p>

{_h4("Substitute", _C_BREAK)}
<p>In fleet mode, when your ship cannot break the rock alone but the fleet
can, a <b>Substitute</b> button appears on the <b>Scanner</b> tab. It opens
<b>Substitute Ships Needed</b>, which lists who to call in: <b>Least
Players</b> for the fewest crew, <b>Least Ships</b> for the fewest
ships.</p>
""")

_TAB_PERSISTENCE = _html(f"""
{_h3("Saving", _C_TIPS)}

{_h4("Where the roster is kept", _C_TIPS)}
<p>Your roster saves by itself to
<b>Documents/SC Loadouts/mining_roster.json</b>, which a toolbox update does
not touch.</p>

{_h4("Sharing a roster", _C_TIPS)}
<ul>
  <li><b>Export</b>, in the bar above the canvas, writes the whole roster
      to a file: players, teams, ships, clusters, strike groups,
      professions and where everything sits on the canvas.</li>
  <li><b>Load</b>, beside it, opens such a file. It replaces the roster you
      have, after asking.</li>
</ul>
<p style="{_DIM}">The <b>Export</b> button in the player panel is a
different one: it saves only the list of players.</p>

{_h4("Clearing", _C_TIPS)}
<p>Three buttons in the same bar, each asking first: <b>Players</b>,
<b>Ships</b> and <b>All</b>.</p>

{_h4("Tips", _C_TIPS)}
<ul>
  <li>Box the whole signature panel, icon included, not only the
      number.</li>
  <li>Star Citizen should run in <b>Borderless Windowed</b> mode.</li>
  <li>The main window stays on top of Star Citizen. Use the slider in the
      title bar to make it more or less see-through.</li>
</ul>
""")


def _tabs() -> list:
    """(title, html) per tab. Built when the popup opens, so the hotkey shown is the current one.
    Short titles: ten of them have to fit across the popup without scroll arrows."""
    return [
        ("Start",    _tab_getting_started()),
        ("Chart",    _TAB_CHART),
        ("Scanning", _TAB_SCANNING),
        ("Signals",  _TAB_TABLE),
        ("Ships",    _TAB_SHIPS),
        ("Refinery", _TAB_REFINERY),
        ("Roster",   _TAB_ROSTER),
        ("Canvas",   _TAB_CANVAS),
        ("Breaking", _TAB_BREAK),
        ("Saving",   _TAB_PERSISTENCE),
    ]


# ── Close button ─────────────────────────────────────────────────────────


class _CloseBtn(QPushButton):
    def __init__(self, parent=None):
        super().__init__("✕", parent)  # ✕
        self.setObjectName("tutClose")
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton#tutClose {{
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
            }}
            QPushButton#tutClose:hover {{
                background-color: rgba(220, 50, 50, 0.85);
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
    """Tabbed tutorial popup for Mining Signals.

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
        if getattr(self, "_initialised", False):
            return
        self._initialised = True

        super().__init__(parent)
        self._drag_pos: QPoint | None = None

        self.setWindowTitle("Mining Signals — Tutorial")
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(640, 520)
        self.setMinimumSize(480, 360)

        # Centre near parent
        if parent:
            pg = parent.geometry()
            x = pg.x() + (pg.width() - 640) // 2
            y = pg.y() + (pg.height() - 520) // 2
            self.move(max(0, x), max(0, y))

        self._build()
        self.show()

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

        title_lbl = QLabel("MINING SIGNALS  \u2014  TUTORIAL", title_bar)
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
                padding: 5px 10px;
                font-family: Consolas;
                font-size: 8pt;
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

        bl = _BRACKET_LEN
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
        self._initialised = False
        super().closeEvent(event)
