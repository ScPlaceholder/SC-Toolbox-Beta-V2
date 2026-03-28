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


_TAB_GETTING_STARTED = _html(f"""
<h3 style="{_H}">Welcome to the Mission Database</h3>
<p>This tool lets you browse <b>missions</b>, <b>crafting blueprints</b>,
and <b>mining/salvage resources</b> from Star Citizen, powered by
<span style="{_ACC}">scmdb.net</span> data.</p>

<h4 style="{_H}">LIVE / PTU</h4>
<p>Use the <span style="{_GRN}">LIVE</span> and
<span style="{_YLW}">PTU</span> buttons in the header to switch between
the current live build and the Public Test Universe. The data set and
game version update automatically.</p>

<h4 style="{_H}">Data Refresh</h4>
<p>Data is fetched from scmdb.net on launch and cached locally.
The tool checks for updates every <b>30 minutes</b> automatically.
If a new game version is detected, data refreshes in the background.</p>

<h4 style="{_H}">Navigation</h4>
<p>Three tabs at the top switch between pages:</p>
<ul>
  <li><b>Missions</b> &mdash; Browse and filter all available contracts</li>
  <li><b>Fabricator</b> &mdash; Search crafting blueprints and recipes</li>
  <li><b>Resources</b> &mdash; Find mining and salvage locations</li>
</ul>
""")

_TAB_MISSIONS = _html(f"""
<h3 style="{_H}">Missions Page</h3>

<h4 style="{_H}">Search</h4>
<p>The search bar at the top of the sidebar filters missions by name,
description, and faction. Typing is debounced &mdash; results update
after you stop typing for 300ms.</p>

<h4 style="{_H}">Category Filters</h4>
<p>Toggle mission categories like <b>Delivery</b>, <b>Combat</b>,
<b>Bounty Hunt</b>, <b>Salvage</b>, etc. Multiple categories can be
active at once. Click a category again to deselect it.</p>

<h4 style="{_H}">System Filter</h4>
<p>Filter by star system: <b>Stanton</b>, <b>Pyro</b>, <b>Nyx</b>,
or <b>Multi</b> (missions spanning multiple systems).</p>

<h4 style="{_H}">Legality &amp; Sharing</h4>
<p><b>Legality:</b> Show only Legal, Illegal, or All missions.<br>
<b>Sharing:</b> Filter by chain missions (multi-step), one-time missions,
or all.</p>

<h4 style="{_H}">Pay Range</h4>
<p>Use the slider to set a minimum payout threshold. Only missions
paying at least that amount will be shown.</p>

<h4 style="{_H}">Mission Cards</h4>
<p>Click any mission card to open a <b>detail popup</b> with four tabs:</p>
<ul>
  <li><b>Overview</b> &mdash; Full description and location</li>
  <li><b>Requirements</b> &mdash; Prerequisites and conditions</li>
  <li><b>Calculator</b> &mdash; Pay breakdown and efficiency</li>
  <li><b>Community</b> &mdash; Tips and notes from players</li>
</ul>
""")

_TAB_FABRICATOR = _html(f"""
<h3 style="{_H}">Fabricator Page</h3>

<h4 style="{_H}">Blueprint Search</h4>
<p>Search by item name to find crafting blueprints. Results include
weapons, armour, ammo, and other craftable items.</p>

<h4 style="{_H}">Type &amp; Subtype Filters</h4>
<p>Filter blueprints by type (<b>Weapons</b>, <b>Armour</b>, <b>Ammo</b>)
and subtype (e.g. Rifle, Pistol, SMG for weapons). Use the multi-select
dropdown to pick multiple subtypes at once.</p>

<h4 style="{_H}">Blueprint Cards</h4>
<p>Click a blueprint card to open a detail popup showing:</p>
<ul>
  <li><b>Product stats</b> &mdash; Item properties and stats</li>
  <li><b>Crafting recipe</b> &mdash; Required materials per tier</li>
  <li><b>Quality slider</b> &mdash; See how quality affects modifiers</li>
  <li><b>Source missions</b> &mdash; Which missions reward this blueprint</li>
</ul>
""")

_TAB_RESOURCES = _html(f"""
<h3 style="{_H}">Resources Page</h3>

<h4 style="{_H}">Overview</h4>
<p>Browse mining and salvage data across all locations. Each card shows
a location with its available resources and maximum yield percentages.</p>

<h4 style="{_H}">System Filter</h4>
<p>Filter locations by star system (Stanton, Pyro, etc.).</p>

<h4 style="{_H}">Group Type</h4>
<p>Filter by mining/salvage method:</p>
<ul>
  <li><b>Ship Mining</b> &mdash; Standard ship-based mining</li>
  <li><b>Ship Mining (Rare)</b> &mdash; Rare deposit locations</li>
  <li><b>FPS Mining</b> &mdash; Hand-mining with multitool</li>
  <li><b>ROC Mining</b> &mdash; Ground vehicle mining</li>
  <li><b>Harvesting</b> &mdash; Collectible plants and items</li>
  <li><b>Salvage</b> &mdash; Derelicts and debris fields</li>
</ul>

<h4 style="{_H}">Resource Filter</h4>
<p>Use the multi-select dropdown to filter by specific resources
(e.g. Quantanium, Laranite, Agricium). Only locations containing
at least one selected resource will be shown.</p>

<h4 style="{_H}">Location Cards</h4>
<p>Click a location card to see a detailed breakdown of all resources
available there, including type, group, and max yield percentage.</p>
""")

_TAB_TIPS = _html(f"""
<h3 style="{_H}">Tips &amp; Shortcuts</h3>

<h4 style="{_H}">Custom Hotkey</h4>
<p>Click the <span style="{_DIM}">\u2328 Set hotkey</span> button in the
header bar to assign a global keyboard shortcut. Press your desired
key combination (e.g. <b>Ctrl+Shift+M</b>) and click Save. The hotkey
will toggle the window from anywhere.</p>

<h4 style="{_H}">Popup Management</h4>
<p>Up to <b>5 detail popups</b> can be open at once. When the limit is
reached, the oldest unpinned popup is automatically closed.</p>
<p>Click <span style="{_GRN}">Pin</span> on any popup to prevent it
from being auto-closed. Pinned popups stay open until you close them
manually.</p>

<h4 style="{_H}">Window Controls</h4>
<p>The window is always-on-top so you can use it alongside Star Citizen.
Drag the title bar to reposition. The window remembers its position
between sessions.</p>

<h4 style="{_H}">Discord</h4>
<p>Click <span style="color: #7289da;">Discord: SCMDB</span> in the header
to join the SCMDB community for data updates and discussion.</p>
""")

_TABS = [
    ("Getting Started", _TAB_GETTING_STARTED),
    ("Missions", _TAB_MISSIONS),
    ("Fabricator", _TAB_FABRICATOR),
    ("Resources", _TAB_RESOURCES),
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

        for title, html in _TABS:
            tabs.addTab(_make_tab_content(html, tabs), title)

        self.body_layout.addWidget(tabs)
        self.show()
