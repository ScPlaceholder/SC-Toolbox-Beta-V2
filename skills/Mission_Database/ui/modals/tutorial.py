"""Tutorial popup for the Mission Database skill."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel, QTabWidget, QScrollArea, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from ui.modals.base import ModalBase

# Shared rich-text style fragments
_H = f"font-family: Electrolize, Consolas; color: {P.accent};"
_B = f"font-family: Consolas; color: {P.fg}; font-size: 9pt; line-height: 1.5;"
_DIM = f"color: {P.fg_dim};"
_ACC = f"color: {P.accent};"
_GRN = f"color: {P.green};"
_YLW = f"color: {P.yellow};"


def _html(body: str) -> str:
    """Wrap body HTML in a styled container."""
    return f"""
    <div style="{_B}">
    {body}
    </div>
    """


def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_missions", "<shift>+3")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Shift+3"


def _tab_getting_started() -> str:
    return _html(f"""
<h3 style="{_H}">Welcome to the Mission Database</h3>
<p>Use this tool to look up contracts, crafting blueprints and mining
locations, and to keep a list of the blueprints you own. The data comes from
<span style="{_ACC}">scmdb.net</span>.</p>

<h4 style="{_H}">The pages</h4>
<p>The buttons under the header switch between four pages:</p>
<ul>
  <li><b>Missions</b> &mdash; every contract, with filters</li>
  <li><b>Fabricator</b> &mdash; crafting blueprints and what they need</li>
  <li><b>Resources</b> &mdash; where each ore and material is found</li>
  <li><b>Owned Blueprints</b> &mdash; the blueprints you have, kept up to date
      from your game log</li>
</ul>
<p><b>Rank Planner</b>, on the right of the same row, works out which
missions take you from one reputation rank to another.</p>

<h4 style="{_H}">LIVE / PTU</h4>
<p>Use the <b>LIVE</b> and <b>PTU</b> buttons in the header to switch
between the live game and the Public Test Universe. The version in use is
shown beside them. If you open <b>Fabricator</b> on LIVE and the LIVE data
has no crafting blueprints, the tool switches to PTU by itself and the status
line says so.</p>

<h4 style="{_H}">Data</h4>
<p>The data is kept on this PC after it is first loaded. While the window is
open the tool checks for a new game version every <b>30 minutes</b> and
reloads by itself. With no connection it uses what it saved last, and the
status line in the header says so.</p>

<h4 style="{_H}">Hotkey</h4>
<p>The launcher's hotkey for this tool is
<span style="{_ACC}">{_hotkey()}</span>. It shows and hides the window. Change
it in the launcher's settings.</p>
<p style="{_DIM}">The <b>Set hotkey</b> button in the header only records a
key and shows it on the button. It does not bind that key to anything.</p>
""")


_TAB_MISSIONS = _html(f"""
<h3 style="{_H}">Missions Page</h3>
<p>Narrow the list with the <b>FILTERS</b> on the left. The count above the
cards shows how many missions are left. <b>Clear all</b> resets every
filter.</p>

<h4 style="{_H}">Search</h4>
<p><b>SEARCH</b> matches the mission's name and description. The list
updates a moment after you stop typing.</p>

<h4 style="{_H}">Category, system, type and faction</h4>
<ul>
  <li><b>CATEGORY</b> &mdash; <b>Career</b>, <b>Story</b>, <b>Wikelo</b>,
      <b>Asd</b> and <b>ACE</b>. <b>Blueprints</b> keeps only missions that
      can reward a blueprint. More than one can be on at once; click a button
      again to turn it off.</li>
  <li><b>STAR SYSTEM</b> &mdash; <b>Stanton</b>, <b>Pyro</b>, <b>Nyx</b>, or
      <b>Multi</b> for missions that span systems.</li>
  <li><b>MISSION TYPE</b> &mdash; a dropdown of the kinds of job, such as
      delivery or bounty.</li>
  <li><b>FACTION</b> &mdash; a dropdown of who offers the mission.</li>
</ul>

<h4 style="{_H}">Legality, sharing and availability</h4>
<ul>
  <li><b>LEGALITY</b> &mdash; <b>All</b>, <b>Legal</b> or <b>Illegal</b>.</li>
  <li><b>SHARING</b> &mdash; <b>Sharable</b> for missions you can share with
      a party, <b>Solo</b> for the ones you cannot.</li>
  <li><b>AVAILABILITY</b> &mdash; <b>Unique</b> for missions you can do
      once, <b>Repeatable</b> for the rest.</li>
</ul>

<h4 style="{_H}">Rank and reward</h4>
<p><b>RANK INDEX</b> has a <b>Min</b> and a <b>Max</b> slider. <b>REWARD
UEC</b> has a <b>Min</b> and a <b>Max</b> box: type the lowest and highest
payout you want to see.</p>

<h4 style="{_H}">Mission cards</h4>
<p>Click a card to open the mission in its own popup. It has four tabs:</p>
<ul>
  <li><b>OVERVIEW</b> &mdash; who offers it, the description, the
      <b>REWARD</b>, any <b>BUY-IN</b>, <b>HAULING ORDERS</b> and
      <b>BLUEPRINT REWARDS</b></li>
  <li><b>REQUIREMENTS</b> &mdash; the <b>MISSION CHAIN</b> it belongs to,
      the <b>REQUIRED STANDING</b>, the <b>COOLDOWN</b>, and whether it can
      be shared or repeated</li>
  <li><b>CALCULATOR</b> &mdash; the rewards, and how many runs of this
      mission each reputation rank takes</li>
  <li><b>COMMUNITY</b> &mdash; empty for now</li>
</ul>
""")

_TAB_FABRICATOR = _html(f"""
<h3 style="{_H}">Fabricator Page</h3>

<h4 style="{_H}">Finding a blueprint</h4>
<p>Type an item name in the search box, or narrow the list with the filters
on the left:</p>
<ul>
  <li><b>Obtainable</b> &mdash; on by default. It hides blueprints that no
      mission gives out. Untick it to see every recipe in the data.</li>
  <li><b>TYPE</b> &mdash; <b>Weapons</b>, <b>Armour</b> or <b>Ammo</b>.</li>
  <li><b>ARMOR CLASS</b> and <b>ARMOR SLOT</b> &mdash; weight and body part,
      for armour.</li>
  <li><b>SUBTYPE</b>, <b>MANUFACTURER</b> and <b>MATERIAL</b> &mdash; each
      lets you tick several at once.</li>
</ul>
<p><b>Clear</b> resets the filters and turns <b>Obtainable</b> back on.</p>

<h4 style="{_H}">Quality</h4>
<p>The <b>GLOBAL QUALITY</b> slider on this page sets the quality a
blueprint opens at. You can change it again inside the popup.</p>

<h4 style="{_H}">Blueprint cards</h4>
<p>Click a card to open the blueprint. The popup shows:</p>
<ul>
  <li><b>PRODUCT STATS</b> &mdash; what the finished item is</li>
  <li><b>CRAFTING RECIPE</b> &mdash; the materials, each with its own
      <b>QUALITY</b> slider. <b>GLOBAL QUALITY</b> moves them all.</li>
  <li><b>STAT SUMMARY</b> &mdash; what the chosen quality does to the
      item</li>
  <li><b>DISMANTLE</b> &mdash; what you get back for taking it apart</li>
  <li>The missions that reward this blueprint</li>
</ul>
<p>Press <b>+ Own</b> to add the blueprint to your <b>Owned Blueprints</b>.
The button then reads <b>Owned</b>; press it again to take the blueprint
off the list (it asks first). <b>Move</b> puts an owned blueprint in one of your folders.</p>
""")

_TAB_RESOURCES = _html(f"""
<h3 style="{_H}">Resources Page</h3>
<p>Each card is a location, with the resources found there and the highest
yield of each.</p>

<h4 style="{_H}">Filters</h4>
<ul>
  <li><b>SEARCH</b> &mdash; matches location names and resource names.</li>
  <li><b>SYSTEM</b> &mdash; <b>Stanton</b>, <b>Pyro</b> or <b>Nyx</b>.</li>
  <li><b>LOCATION TYPE</b> &mdash; <b>Planet</b>, <b>Moon</b>, <b>Belt</b>,
      <b>Lagrange</b>, <b>Cluster</b>, <b>Event</b> or <b>Special</b>.</li>
  <li><b>DEPOSIT TYPE</b> &mdash; <b>Ship</b>, <b>FPS</b>, <b>ROC</b> or
      <b>Harvest</b>: how the resource is collected.</li>
  <li><b>RESOURCES</b> &mdash; tick the resources you are after. <b>Any</b>
      keeps locations that have at least one of them; <b>All</b> keeps only
      locations that have every one.</li>
</ul>
<p><b>Clear</b> resets the filters.</p>

<h4 style="{_H}">Cards or table</h4>
<p><b>Cards</b> and <b>Table</b> switch between the two layouts. In the
table, click a column heading to sort by it and click again to reverse.</p>

<h4 style="{_H}">A location's resources</h4>
<p>Click a location card to list everything found there. <b>SORT BY</b>
orders the list by <b>% Desc</b>, <b>Name</b> or <b>Type</b>.</p>
""")

_TAB_OWNED = _html(f"""
<h3 style="{_H}">Owned Blueprints</h3>
<p>This page lists the blueprints your character has. You do not have to
fill it in.</p>

<h4 style="{_H}">It fills in by itself</h4>
<p>The tool reads your Star Citizen game logs while it is open and adds each
blueprint the game says you received. A status line on the page tells you
what it is doing and how many it has found.</p>
<ul>
  <li><b>Scan Game Log</b> &mdash; read all the logs again now.</li>
  <li><b>SC Folder</b> &mdash; choose your Star Citizen channel folder (for
      example LIVE) if the tool has the wrong one or none.</li>
  <li><b>Clear All</b> &mdash; empty the list, after asking. Blueprints you
      clear are not added back by themselves.</li>
</ul>

<h4 style="{_H}">Adding and removing by hand</h4>
<p>On the <b>Fabricator</b> page, open a blueprint and press <b>+ Own</b>.
Here, right-click a blueprint and choose <b>Remove from owned</b> to take it
off.</p>

<h4 style="{_H}">Folders</h4>
<ul>
  <li><b>+ New Folder</b> makes a folder. Click a folder to open it; the
      trail above the cards takes you back out.</li>
  <li>Drag a blueprint onto a folder to put it in, or right-click it and
      choose <b>Move to folder...</b></li>
  <li>Right-click a folder to rename or delete it. What was in a deleted
      folder moves up one level.</li>
</ul>
""")

_TAB_TIPS = _html(f"""
<h3 style="{_H}">Tips</h3>

<h4 style="{_H}">Rank Planner</h4>
<p><b>Rank Planner</b> is marked <b>EXPERIMENTAL FEATURE</b>. Pick a
<b>FACTION</b>, a <b>SYSTEM</b>, and the ranks to go <b>FROM</b> and
<b>TO</b>. For each rank step it names the best repeatable mission and how
many runs it takes. Click a mission's name to open it.</p>

<h4 style="{_H}">Popups</h4>
<p>Up to <b>5</b> popups can be open at once, and this tutorial counts as
one. Opening another closes the oldest one that is not pinned. Press
<b>Pin</b> on a popup to keep it; <b>Unpin</b> releases it. If all five are
pinned, the oldest of them is closed.</p>

<h4 style="{_H}">Window</h4>
<p>The window stays on top of Star Citizen. Drag the title bar to move it,
drag an edge to resize it, and use the slider in the title bar to make it
more or less see-through.</p>

<h4 style="{_H}">Discord</h4>
<p><b>Discord: SCMDB</b> in the header opens the SCMDB community's Discord
invite in your browser.</p>
""")


def _tabs() -> list:
    """(title, html) per tab. Built when the tutorial opens, so the hotkey shown is the current one."""
    return [
        ("Getting Started", _tab_getting_started()),
        ("Missions", _TAB_MISSIONS),
        ("Fabricator", _TAB_FABRICATOR),
        ("Resources", _TAB_RESOURCES),
        ("Owned", _TAB_OWNED),
        ("Tips", _TAB_TIPS),
    ]


def _make_tab_content(html: str, parent: QWidget) -> QScrollArea:
    """Create a scrollable label for one tutorial tab."""
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


class TutorialModal(ModalBase):
    """Tabbed tutorial popup for the Mission Database."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, title="Tutorial", width=600, height=480, accent=P.tool_mission)

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
                background: #1a2a30;
                color: {P.tool_mission};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)

        for title, html in _tabs():
            tabs.addTab(_make_tab_content(html, tabs), title)

        self.body_layout.addWidget(tabs)
        self.show()
