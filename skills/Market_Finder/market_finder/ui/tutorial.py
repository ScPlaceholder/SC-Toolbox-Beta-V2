"""Tutorial popup for the Market Finder skill."""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QTabWidget,
)

from shared.qt.theme import P

# Shared rich-text style fragments
_H = f"font-family: Electrolize, Consolas; color: {P.tool_market};"
_B = f"font-family: Consolas; color: {P.fg}; font-size: 9pt; line-height: 1.5;"
_DIM = f"color: {P.fg_dim};"
_ACC = f"color: {P.tool_market};"
_GRN = f"color: {P.green};"


def _html(body: str) -> str:
    return f'<div style="{_B}">{body}</div>'


def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_market", "<shift>+5")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Shift+5"


def _tab_getting_started() -> str:
    return _html(f"""
<h3 style="{_H}">Welcome to Item Finder</h3>
<p>Use Item Finder to find out where an item is sold and what it costs:
armour, weapons, ship parts, food, ships and rentals. The data comes from
<span style="{_ACC}">UEX</span>.</p>

<h4 style="{_H}">Quick Start</h4>
<ol>
  <li>Type part of a name in <b>Search items...</b> The table narrows as you
      type, and a list of matches drops down.</li>
  <li>Click a row. The panel on the right shows <b>WHERE TO BUY</b>,
      cheapest first, and <b>WHERE TO SELL</b>.</li>
  <li>Double-click a row to open the same details in a floating bubble you
      can keep beside the game.</li>
</ol>

<h4 style="{_H}">The buttons at the top</h4>
<ul>
  <li>⚙ &mdash; settings</li>
  <li><b>Shopping List</b> &mdash; your shopping list</li>
  <li><b>? Tutorial</b> &mdash; this window</li>
  <li><b>Star Map</b> &mdash; the map, to see what a place sells</li>
</ul>

<h4 style="{_H}">Opening it</h4>
<p>Item Finder is the <b>ITEM FINDER</b> tab of the Everything Finder. Its
own hotkey is <span style="{_ACC}">{_hotkey()}</span>, which opens it in a
window of its own.</p>
""")


_TAB_SEARCH = _html(f"""
<h3 style="{_H}">Search &amp; Browse</h3>

<h4 style="{_H}">Search</h4>
<p><b>Search items...</b> matches any part of an item's name. Once you have
typed two letters, a list of matches drops down, grouped by kind. Click one
to go to its tab and show its details in the panel on the right.</p>

<h4 style="{_H}">Category tabs</h4>
<p>The tabs under the search box show one kind of item each:</p>
<ul>
  <li><b>All</b> &mdash; every item</li>
  <li><b>Armor</b>, <b>Weapons</b> and <b>Clothing</b> &mdash; what you
      wear and carry</li>
  <li><b>Ship Weapons</b>, <b>Missiles</b> and <b>Ship Components</b>
      &mdash; what you fit to a ship</li>
  <li><b>Utility</b>, <b>Sustenance</b> and <b>Misc</b> &mdash; tools, food
      and drink, and everything else</li>
  <li><b>Ships</b> &mdash; every ship and vehicle. One that cannot be bought
      in the game says so when you select it.</li>
  <li><b>Rentals</b> &mdash; ships you can rent</li>
</ul>

<h4 style="{_H}">Sorting and filtering</h4>
<p>Click a column heading to sort by it. On the <b>Ships</b> tab,
<b>Filter ships...</b> narrows the list by name, and <b>Spaceship</b> and
<b>Ground</b> show one kind of vehicle.</p>
""")

_TAB_DETAILS = _html(f"""
<h3 style="{_H}">Details &amp; Prices</h3>

<h4 style="{_H}">The panel on the right</h4>
<p>Click a row and the panel shows what the item is, then
<b>WHERE TO BUY</b> with the cheapest terminal first, and
<b>WHERE TO SELL</b> with the best price first.</p>

<h4 style="{_H}">Bubbles</h4>
<p><b>Double-click</b> a row to open its details in a floating bubble. You
can open several and drag them where you like. A bubble stays open until you
close it with ✕.</p>

<h4 style="{_H}">Ships and rentals</h4>
<p>For a ship, the details list its specifications, <b>WHERE TO BUY</b> and
<b>RENTAL LOCATIONS</b>, and links to its <b>RSI Store</b> page and its
<b>Brochure</b> when it has them.</p>
""")

_TAB_SHOPPING = _html(f"""
<h3 style="{_H}">Shopping List</h3>
<p>Press <b>Shopping List</b> at the top to open your list. There is one
shopping list in the toolbox: the Star Map and the Everything Finder show the
same one, and it is still there next time.</p>

<h4 style="{_H}">Adding to it</h4>
<ul>
  <li><b>Drag a row</b> from the item table and drop it on the list. Rows on
      the <b>Ships</b> and <b>Rentals</b> tabs cannot be dragged.</li>
  <li>Or, in the list itself, pick <b>Item</b> or <b>Commodity</b>, type a
      name, set how many, and press <b>Add</b>.</li>
</ul>

<h4 style="{_H}">Where to buy it all</h4>
<p><b>Plan route</b> works out where to buy everything on the list. Press
<b>Show on Star Map</b> on a plan to see it drawn on the map.
<b>Clear list</b> empties the list.</p>
""")

_TAB_SETTINGS = _html(f"""
<h3 style="{_H}">Settings &amp; Tips</h3>

<h4 style="{_H}">Settings</h4>
<p>Click ⚙ at the top to open or close the settings:</p>
<ul>
  <li><b>Opacity:</b> &mdash; how see-through the window is, from 30% to
      100%</li>
  <li><b>Always on top</b> &mdash; keep the window above the game</li>
  <li><b>Cache TTL:</b> &mdash; how long downloaded data is used before it
      is fetched again</li>
  <li><b>Refresh Data</b> &mdash; fetch everything again now</li>
</ul>
<p style="{_DIM}">Always on top and Cache TTL are not remembered. They go
back to how they started the next time the tool opens.</p>

<h4 style="{_H}">Fresh prices</h4>
<p>While the window is open the data is fetched again about once an hour.
Use <b>Refresh Data</b> if you want it sooner.</p>

<h4 style="{_H}">Window</h4>
<p>Drag the title bar to move the window. Where it is and how big it is are
remembered.</p>
""")


def _tabs() -> list:
    """(title, html) per tab. Built when the tutorial opens, so the hotkey shown is the current one."""
    return [
        ("Getting Started", _tab_getting_started()),
        ("Search", _TAB_SEARCH),
        ("Details", _TAB_DETAILS),
        ("Shopping List", _TAB_SHOPPING),
        ("Settings", _TAB_SETTINGS),
    ]


class TutorialBubble(QWidget):
    """Floating, draggable tutorial popup for Market Finder.

    Singleton: a second call just raises the existing window.
    """

    _instance: "TutorialBubble | None" = None

    def __new__(cls, parent: QWidget | None = None):
        if cls._instance is not None and cls._instance.isVisible():
            cls._instance.raise_()
            cls._instance.activateWindow()
            return cls._instance
        instance = super().__new__(cls)
        cls._instance = instance
        return instance

    def __init__(self, parent: QWidget | None = None) -> None:
        if getattr(self, "_initialised", False):
            return
        self._initialised = True
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._drag_pos: QPoint | None = None

        self.setFixedSize(560, 460)
        self.setStyleSheet(f"""
            TutorialBubble {{
                background-color: {P.bg_secondary};
                border: 2px solid {P.tool_market};
                border-radius: 6px;
            }}
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Title bar
        title_bar = QWidget()
        title_bar.setFixedHeight(28)
        title_bar.setStyleSheet(f"""
            background-color: {P.bg_header};
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
        """)
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(10, 2, 4, 2)
        tb_lay.setSpacing(6)

        title_lbl = QLabel("TUTORIAL")
        title_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas;
            font-size: 10pt; font-weight: bold;
            color: {P.tool_market};
            letter-spacing: 2px;
            background: transparent;
        """)
        tb_lay.addWidget(title_lbl, 1)

        close_btn = QLabel("\u2715")
        close_btn.setFixedSize(20, 20)
        close_btn.setAlignment(Qt.AlignCenter)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            font-size: 10pt; color: {P.fg_dim}; background: transparent;
        """)
        close_btn.mousePressEvent = lambda _: self.close()
        tb_lay.addWidget(close_btn)

        outer.addWidget(title_bar)

        # Tabbed content
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
                padding: 5px 12px;
                font-family: Consolas;
                font-size: 9pt;
                font-weight: bold;
            }}
            QTabBar::tab:selected {{
                background: #1a2a30;
                color: {P.tool_market};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)

        for title, html in _tabs():
            scroll = QScrollArea()
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
            lbl.setStyleSheet(f"background: transparent; padding: 14px; color: {P.fg};")
            scroll.setWidget(lbl)
            tabs.addTab(scroll, title)

        outer.addWidget(tabs, 1)

        # Position near parent
        if parent:
            pg = parent.geometry()
            x = pg.x() + (pg.width() - self.width()) // 2
            y = pg.y() + (pg.height() - self.height()) // 2
            self.move(max(0, x), max(0, y))

        self.show()

    def closeEvent(self, event):
        TutorialBubble._instance = None
        self._initialised = False
        super().closeEvent(event)

    # Drag support
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
