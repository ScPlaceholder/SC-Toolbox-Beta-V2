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


def _h3(text: str, color: str) -> str:
    return f'<h3 style="{_FONT} color: {color};">{text}</h3>'


def _h4(text: str, color: str) -> str:
    return f'<h4 style="{_FONT} color: {color};">{text}</h4>'


def _html(body: str) -> str:
    return f'<div style="{_B}">{body}</div>'


# ═══════════════════════════════════════════════════════════════════════════
# Tab content
# ═══════════════════════════════════════════════════════════════════════════

_TAB_GETTING_STARTED = _html(f"""
{_h3("Welcome to Mining Signals", _C_START)}
<p>Mining Signals is an all-in-one mining operations tool. It reads
your ship's scanner HUD to identify resources, calculates whether your
fleet can break rocks, and provides a full fleet management roster for
organising large-scale mining operations.</p>

{_h4("Quick Setup", _C_START)}
<ol>
  <li>Click <b style="{_ACC}">Set Region</b> and draw a box around the
      signal number on your mining scanner HUD.</li>
  <li>Click <b style="{_ACC}">Set Mining Output Display Location</b> and
      click where you want the result bubble to appear.</li>
  <li>Click <b style="{_ACC}">Start Scan</b> to begin continuous
      scanning.</li>
</ol>
<p>The tool reads the number every 2-3 seconds and shows the matched
resource in a floating HUD bubble.</p>

{_h4("Tabs Overview", _C_START)}
<ul>
  <li><b>Scanner</b> &mdash; OCR scanning, signal lookup, and
      breakability display</li>
  <li><b>Mining Ships</b> &mdash; Load mining and salvage loadouts</li>
  <li><b>Gadgets</b> &mdash; Manage consumable gadget quantities</li>
  <li><b>Refinery</b> &mdash; Track refinery work orders</li>
  <li><b>Mining Roster</b> &mdash; Full fleet hierarchy management</li>
</ul>

{_h4("Data Source &amp; Hotkey", _C_START)}
<p>Signal data is fetched from a community spreadsheet and refreshed
every hour (works offline with cached data). The default global hotkey
is <b>Shift + 9</b> (reassignable in SC Toolbox settings).</p>
""")

_TAB_SCANNING = _html(f"""
{_h3("Scanning &amp; OCR", _C_SCAN)}

{_h4("How It Works", _C_SCAN)}
<p>The scanner captures a small area of your screen where the mining
signal number appears. It uses OCR to read the digits, then looks up
the value in the signal reference table.</p>

{_h4("Set Region", _C_SCAN)}
<p>Click <b>Set Region</b> to draw a box around the signal number on
your HUD. Draw it <b>tightly around the number</b> &mdash; avoid
including icons or other HUD elements.</p>

{_h4("Set HUD Region", _C_SCAN)}
<p>Click <b>Set HUD Region</b> to capture the mining HUD's mass and
resistance values. This feeds the <b>breakability calculator</b>
automatically while scanning.</p>

{_h4("Display Location", _C_SCAN)}
<p>Click <b>Set Mining Output Display Location</b> to choose where the
result bubble appears. A preview follows your cursor &mdash; click to
lock the position.</p>

{_h4("Choose Mining Ship", _C_SCAN)}
<p>Selects which loaded ship loadout (or the full fleet) feeds the
breakability calculator. Options: individual ships or <b>Fleet</b>
mode (uses all loaded ships combined).</p>

{_h4("Calc: Fleet / Team Toggle", _C_SCAN)}
<p>Switches breakability between:</p>
<ul>
  <li><b>Fleet</b> &mdash; All fleet ships combined (default)</li>
  <li><b>Team</b> &mdash; Only your assigned team's ships (from the
      Mining Roster). Escalates: solo &rarr; team &rarr; cluster
      &rarr; fleet when substitutes are needed.</li>
</ul>

{_h4("Compact Mode", _C_SCAN)}
<p>When scanning is active, the window collapses to a small bar showing
only essential controls. Press your hotkey to hide/show the tool
entirely.</p>

{_h4("Requirements", _C_SCAN)}
<ul>
  <li>Star Citizen must run in <b>Borderless Windowed</b> mode</li>
  <li>Fullscreen exclusive mode shows a black capture</li>
  <li><span style="{_DIM}">Scanning uses ~7-8% of one CPU core at
      2-3 second intervals</span></li>
</ul>
""")

_TAB_TABLE = _html(f"""
{_h3("Signal Table", _C_TABLE)}

{_h4("Reading the Table", _C_TABLE)}
<p>The table shows all known mining resources with their signal values
for 1 to 6 rocks. Click column headers to sort by any column.
<b>Double-click</b> a row to open a detail popup with pin/close.</p>

{_h4("Manual Search", _C_TABLE)}
<p>Type a signal value in the search bar to identify a resource without
scanning. You can also search by resource name. When multiple resources
share the same value, all matches are shown.</p>

{_h4("Rarity Tiers", _C_TABLE)}
<ul>
  <li><span style="color:#8cc63f;"><b>Common</b></span> &mdash; Most
      frequently found</li>
  <li><span style="color:#00bcd4;"><b>Uncommon</b></span> &mdash;
      Moderate value</li>
  <li><span style="color:#ffc107;"><b>Rare</b></span> &mdash; High
      value resources</li>
  <li><span style="color:#aa66ff;"><b>Epic</b></span> &mdash; Very
      valuable</li>
  <li><span style="color:#ff9800;"><b>Legendary</b></span> &mdash;
      Extremely rare and valuable</li>
</ul>

{_h4("Overlapping Values", _C_TABLE)}
<p>Some resources share the same signal value at different rock counts.
The result bubble lists <b>all possible matches</b> so you can narrow
it down based on context.</p>
""")

_TAB_SHIPS = _html(f"""
{_h3("Mining Ships &amp; Salvage", _C_SHIPS)}

{_h4("Mining Sub-Tab", _C_SHIPS)}
<p>Load a saved <b>Mining Loadout</b> (.json) for each ship type:</p>
<ul>
  <li><b>Golem</b>, <b>Prospector</b>, <b>MOLE</b> &mdash; individual
      loadout slots</li>
  <li>Each shows turret hierarchy with lasers, modules, and gadget</li>
  <li>The active ship selection feeds the breakability calculator</li>
</ul>

{_h4("Mining Ops Fleet", _C_SHIPS)}
<p>Add multiple ship loadouts to form a fleet. In <b>Fleet</b> mode
all ships are combined for breakability analysis. The first ship is
treated as "yours" for solo-check priority.</p>

{_h4("Salvage Sub-Tab", _C_SHIPS)}
<p>Load <b>DPS Calculator</b> loadout files (.json) for salvage ships
(Vulture, Reclaimer, etc.). Salvage ships appear in the Mining Roster
fleet panel and can be dragged onto teams and strike groups. They
don't affect mining breakability calculations.</p>

{_h4("Gadgets Tab", _C_SHIPS)}
<p>Set quantities for each consumable gadget type. The breakability
calculator uses gadgets as a last resort when passive + active modules
can't crack a rock. Toggle <b>"Always use best gadget"</b> to apply
the strongest one automatically.</p>
""")

_TAB_ROSTER = _html(f"""
{_h3("Mining Roster", _C_ROSTER)}
<p>The Roster is a three-panel interactive fleet management tool for
organising large-scale mining operations.</p>

{_h4("Left Panel: Player Roster", _C_ROSTER)}
<ul>
  <li><b>Add Player</b> &mdash; type a name and press Enter or click
      Add</li>
  <li><b>Import / Export</b> &mdash; save/load player lists as JSON</li>
  <li><b>Search</b> &mdash; fuzzy filter players by name</li>
  <li>Players are grouped by team with collapsible headers. Unassigned
      players fall into an "Unassigned" section.</li>
</ul>

{_h4("Right-Click a Player", _C_ROSTER)}
<ul>
  <li><b>Set as User</b> &mdash; marks "you"; the canvas centres on your
      team on launch</li>
  <li><b>Set as Foreman</b> &mdash; makes them the fleet foreman (top
      node on the canvas)</li>
  <li><b>Promote to Leader</b> &mdash; creates a new Team node on the
      canvas for them</li>
  <li><b>Promote to Strike Group Leader</b> &mdash; (inside a strike
      group) marks them as the SG leader</li>
  <li><b>Assign Profession</b> &mdash; choose from 23 professions; an
      icon appears by their name and on their canvas badge</li>
  <li><b>Remove Player</b> &mdash; removes from the roster</li>
</ul>

{_h4("Profession Key Tab", _C_ROSTER)}
<p>Switch to the <b>Key</b> tab to see all profession icons with
descriptions. Drag a profession row directly onto a player's name to
assign it. Use the search box to filter professions.</p>

{_h4("Right Panel: Ship Fleet", _C_ROSTER)}
<p>Shows all loaded mining ships, salvage ships, and fleet support
ships in collapsible categories. <b>Drag</b> any ship onto the canvas
to place it.</p>

{_h4("Fleet Support Ships", _C_ROSTER)}
<p>Add support ships via the buttons at the bottom: Hauling, Repair,
Refuel, Escort, Mothership, Medical. Each opens a <b>fuzzy search</b>
of 270+ Star Citizen ships with crew counts from the UEX database.</p>
""")

_TAB_CANVAS = _html(f"""
{_h3("Canvas &amp; Hierarchy", _C_ROSTER)}

{_h4("The Node Graph", _C_ROSTER)}
<p>The centre panel is an interactive canvas showing the fleet
hierarchy as a node graph (like Blender's geometry nodes):</p>
<ul>
  <li><b>Foreman</b> &mdash; top node (double-click to rename)</li>
  <li><b>Teams</b> &mdash; created when a player is promoted to Leader
      (double-click to rename)</li>
  <li><b>Ships</b> &mdash; dragged from the fleet panel or assigned via
      right-click</li>
  <li><b>Player Badges</b> &mdash; crew assigned to each ship, with
      colour-coded connector lines back to the ship</li>
</ul>

{_h4("Interactions", _C_ROSTER)}
<ul>
  <li><b>Left-click drag</b> on empty canvas &mdash; pan the view</li>
  <li><b>Middle-click drag</b> &mdash; also pans</li>
  <li><b>Scroll wheel</b> &mdash; zoom in/out</li>
  <li><b>Drag a team</b> &mdash; moves the entire sub-tree (all its
      ships, strike groups, and child teams) together</li>
  <li><b>Right-click a ship</b> &mdash; delete, assign to team/foreman,
      unassign, manage crew, add strike group (on motherships)</li>
  <li><b>Right-click a team</b> &mdash; assign to cluster (A-Z)</li>
  <li><b>Drag a ship near a team</b> &mdash; snaps into that team
      (120px proximity)</li>
  <li><b>Drag a team near another team</b> &mdash; nests as a
      sub-team (200px proximity)</li>
</ul>

{_h4("Motherships &amp; Strike Groups", _C_ROSTER)}
<ul>
  <li>Add a <b>Mothership</b> from Fleet Support, then <b>drag ships
      onto it</b> &mdash; creates a "Strike Group 1" with the ships
      snapped in a column</li>
  <li><b>Right-click a mothership</b> &rarr; "Add Strike Group" to
      create additional groups (Strike Group 2, 3, etc.)</li>
  <li>Double-click a strike group to <b>rename</b> it</li>
  <li>Players on strike group ships can be promoted to
      <b>Strike Group Leader</b> via right-click</li>
  <li>Strike groups appear as nested sub-sections in the left
      panel player list</li>
</ul>

{_h4("Clusters", _C_ROSTER)}
<ul>
  <li><b>Right-click a team</b> &rarr; "Assign to Cluster" (A-Z)</li>
  <li>Cluster label appears on the team node (e.g. "Cluster B")</li>
  <li>The <b>Cluster filter bar</b> above the canvas lets you check/
      uncheck clusters. Unchecked clusters dim to 30% opacity.</li>
  <li>Your cluster's checkbox is highlighted in green</li>
  <li>Team-mode breakability searches your cluster first before trying
      other clusters (alphabetically nearest)</li>
</ul>
""")

_TAB_BREAK = _html(f"""
{_h3("Breakability Calculator", _C_BREAK)}
<p>The breakability system determines whether your ship(s) can crack
a mining rock based on its mass and resistance.</p>

{_h4("How It Calculates", _C_BREAK)}
<p>Power required = mass &times; 0.2 / (1 - effective resistance).
Effective resistance is the rock's base resistance modified by your
laser's resistance modifier, modules, and gadgets.</p>

{_h4("Escalation Order (Team Mode)", _C_BREAK)}
<ol>
  <li><b>Solo</b> &mdash; your ship's turrets only</li>
  <li><b>Team</b> &mdash; all mining ships in your team</li>
  <li><b>Cluster</b> &mdash; adds one team at a time from your cluster
      (alphabetical by team name)</li>
  <li><b>Fleet</b> &mdash; tries other clusters (alphabetical by
      cluster letter, nearest first)</li>
</ol>
<p>The HUD bubble shows the scope that succeeded (Solo/Team/Cluster/
Fleet) next to the power percentage.</p>

{_h4("Fleet Mode", _C_BREAK)}
<p>In Fleet mode, all fleet ships are combined. If your ship can't
solo, the bubble shows two tabs: <b>Least Players</b> (fewest crew)
and <b>Least Ships</b> (fewest ships needed).</p>

{_h4("Substitute Info", _C_BREAK)}
<p>When substitutes are needed, the bubble shows which ships from which
teams/clusters can assist, with their crew requirements.</p>

{_h4("Gadgets &amp; Active Modules", _C_BREAK)}
<p>The calculator tries passive-only first, then active modules, then
gadgets. It tracks remaining uses per turret and gadget quantities.
The result shows how many activations are needed.</p>
""")

_TAB_PERSISTENCE = _html(f"""
{_h3("Saving &amp; Persistence", _C_TIPS)}

{_h4("Roster File Location", _C_TIPS)}
<p>Your roster saves to <b>Documents/SC Loadouts/mining_roster.json</b>.
This location <b>survives SC Toolbox updates</b> (the tool folder gets
replaced, but Documents is never touched).</p>

{_h4("Export &amp; Load", _C_TIPS)}
<ul>
  <li><b style="{_ACC}">Export</b> &mdash; saves the entire roster
      (players, teams, ships, clusters, strike groups, professions,
      canvas positions) to a shareable JSON file</li>
  <li><b style="{_ACC}">Load</b> &mdash; imports a previously exported
      roster file (replaces the current roster after confirmation)</li>
  <li>Share exported files with org members so everyone has the same
      fleet structure</li>
</ul>

{_h4("What Persists Automatically", _C_TIPS)}
<ul>
  <li>All team assignments, ship placements, and player roles</li>
  <li>Canvas node positions (drag layout is preserved)</li>
  <li>Cluster assignments (A-Z)</li>
  <li>Strike group names and leaders</li>
  <li>Profession assignments</li>
  <li>Assigned user selection (centres on your team on launch)</li>
  <li>Player roster import/export files</li>
</ul>

{_h4("Clear Roster", _C_TIPS)}
<p>The <b>Clear</b> button wipes all teams, ships, and assignments but
keeps your player roster and fleet support ship definitions. A
confirmation dialog prevents accidental clearing.</p>

{_h4("Tips &amp; Troubleshooting", _C_TIPS)}
<ul>
  <li>Draw the OCR scan region <b>tightly</b> around just the number</li>
  <li>Star Citizen must run in <b>Borderless Windowed</b> mode</li>
  <li>Signal data refreshes every hour (restart to force refresh)</li>
  <li>All windows stay on top of Star Citizen</li>
  <li>Drag the title bar to reposition any popup</li>
</ul>
""")

_TABS = [
    ("Getting Started", _TAB_GETTING_STARTED),
    ("Scanning",        _TAB_SCANNING),
    ("Signal Table",    _TAB_TABLE),
    ("Ships & Salvage", _TAB_SHIPS),
    ("Roster",          _TAB_ROSTER),
    ("Canvas",          _TAB_CANVAS),
    ("Breakability",    _TAB_BREAK),
    ("Save & Tips",     _TAB_PERSISTENCE),
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
