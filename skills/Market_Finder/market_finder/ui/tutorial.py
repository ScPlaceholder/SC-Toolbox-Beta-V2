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


_TAB_GETTING_STARTED = _html(f"""
<h3 style="{_H}">Welcome to Market Finder</h3>
<p>Market Finder lets you search the entire Star Citizen item catalog
powered by <span style="{_ACC}">uexcorp.space</span> data. Find buy/sell
locations, compare prices, and browse ships and rentals.</p>

<h4 style="{_H}">Quick Start</h4>
<ol>
  <li>Type an item name in the <b>search bar</b> at the top</li>
  <li>Click a result from the dropdown, or browse by <b>category tab</b></li>
  <li>Double-click any row to open a <b>detail bubble</b> with prices</li>
</ol>

<h4 style="{_H}">Data Refresh</h4>
<p>Data is cached locally for fast loading. Click the <b>gear icon</b>
(\u2699) in the title bar to open settings, where you can adjust cache
TTL or click <span style="{_ACC}">Refresh Data</span> to fetch the latest
prices from UEX Corp.</p>

<h4 style="{_H}">Auto-Refresh</h4>
<p>The cache refreshes automatically based on your TTL setting (default 2h).
You can change this in Settings to 30m, 1h, 2h, 4h, or 8h.</p>
""")

_TAB_SEARCH = _html(f"""
<h3 style="{_H}">Search &amp; Browse</h3>

<h4 style="{_H}">Search Bar</h4>
<p>The search bar supports fuzzy matching. Start typing an item name and
a <b>search bubble</b> appears with results grouped by category. Click
any result to jump directly to it.</p>
<p>Search is debounced &mdash; results appear after 300ms of idle typing.</p>

<h4 style="{_H}">Category Tabs</h4>
<p>Browse items by category using the tab bar:</p>
<ul>
  <li><b>All</b> &mdash; Every item in the catalog</li>
  <li><b>Armor</b> &mdash; Helmets, chest plates, leg guards</li>
  <li><b>Weapons</b> &mdash; Personal weapons (rifles, pistols, SMGs)</li>
  <li><b>Clothing</b> &mdash; Undersuits, jackets, pants</li>
  <li><b>Ship Weapons</b> &mdash; Guns, cannons, repeaters</li>
  <li><b>Missiles</b> &mdash; Ship-mounted missile systems</li>
  <li><b>Ship Components</b> &mdash; Shields, coolers, power plants, QDs</li>
  <li><b>Utility</b> &mdash; Multitools, tractor beams, gadgets</li>
  <li><b>Sustenance</b> &mdash; Food and drinks</li>
  <li><b>Misc</b> &mdash; Commodities, liveries, misc items</li>
  <li><b>Ships</b> &mdash; Purchasable ships and vehicles</li>
  <li><b>Rentals</b> &mdash; Rentable ships</li>
</ul>
""")

_TAB_DETAILS = _html(f"""
<h3 style="{_H}">Item Details &amp; Prices</h3>

<h4 style="{_H}">Detail Panel</h4>
<p>Click any row in the table to see item details in the right-side
panel. This shows the item name, category, and a quick summary.</p>

<h4 style="{_H}">Detail Bubbles</h4>
<p><b>Double-click</b> a row to open a floating detail bubble. Bubbles
show:</p>
<ul>
  <li>Item name, category, and section</li>
  <li><b>Buy locations</b> &mdash; Terminals where you can purchase, with prices</li>
  <li><b>Sell locations</b> &mdash; Terminals where you can sell, with prices</li>
  <li>Price data fetched live from UEX Corp</li>
</ul>

<h4 style="{_H}">Pinning Bubbles</h4>
<p>Click <b>PIN</b> on a detail bubble to keep it open. Unpinned bubbles
close when you click elsewhere. Pinned bubbles stay visible until you
close them manually with the \u2715 button.</p>

<h4 style="{_H}">Ships &amp; Rentals</h4>
<p>The Ships and Rentals tabs use dedicated tables with columns for
manufacturer, SCU capacity, and price. Double-click for full details
including purchase/rental locations.</p>
""")

_TAB_SETTINGS = _html(f"""
<h3 style="{_H}">Settings &amp; Tips</h3>

<h4 style="{_H}">Settings Panel</h4>
<p>Click the <b>gear icon</b> (\u2699) in the title bar to toggle the
settings panel. Available options:</p>
<ul>
  <li><b>Opacity</b> &mdash; Adjust window transparency (30%-100%)</li>
  <li><b>Always on top</b> &mdash; Keep the window above other apps</li>
  <li><b>Cache TTL</b> &mdash; How long cached data stays fresh</li>
  <li><b>Refresh Data</b> &mdash; Force-fetch latest data from UEX Corp</li>
</ul>

<h4 style="{_H}">Window Controls</h4>
<p>The window is frameless and always-on-top by default, perfect for
use alongside Star Citizen. Drag the title bar to reposition. Window
position and size are saved between sessions.</p>

<h4 style="{_H}">Keyboard Shortcut</h4>
<p>If you launched Market Finder via the SC Toolbox launcher, you can
assign a global hotkey in the launcher's settings panel to toggle the
window from anywhere.</p>
""")

_TABS = [
    ("Getting Started", _TAB_GETTING_STARTED),
    ("Search", _TAB_SEARCH),
    ("Details", _TAB_DETAILS),
    ("Settings", _TAB_SETTINGS),
]


class TutorialBubble(QWidget):
    """Floating, draggable tutorial popup for Market Finder."""

    def __init__(self, parent: QWidget | None = None) -> None:
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

        for title, html in _TABS:
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
