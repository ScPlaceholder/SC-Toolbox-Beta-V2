"""The Everything Finder's tutorial: what the window is, and the short version of each tool in it.

Shown in shared/qt/tutorial_popup.py's popup, opened from "? Tutorial" in the
window's title bar (window.py). Item Finder and Trade Hub keep their own
tutorial buttons at the top of their tabs. The Star Map's tutorial button lives
in the title bar of the Star Map's own window, which this window does not show,
so the Star Map tab here carries a button that opens it.

Every name in <b> was read out of the source it belongs to, and
tests/test_tutorial.py checks each one is still there:
  window.py                       ITEM FINDER / TRADE HUB / STAR MAP, Shopping List
  shared/shopping/panel.py        Item, Commodity, Add, Clear list, Start, Prefer,
                                  Terminals per entry, Auto-calculate, Plan route,
                                  Show on Star Map
  shared/shopping/shopping_list.py   MIN STOPS, SHORTEST TRIP, BEST PRICE
  market_finder/ui/app.py         Search items..., ? Tutorial, Star Map, Shopping List
  market_finder/ui/detail_panel.py   WHERE TO BUY
  trade_hub_app.py                VEHICLE:, VIEW MODE:, Tutorial, STAR MAP, Show Route
  starmap/panel.py                Home, Route, Cancel, Clear route, In-Game, Market,
                                  Commodities, OVERLAYS, Search system or location...
  starmap/location_dialog.py      Trade routes from here, Commodities, Items
  tools/Assistant/assistant/panel.py, set_route/route_setter.py
                                  In-Game, Calibrate Route, Begin
"""
from __future__ import annotations

from typing import Callable, List, Optional

from shared.qt.theme import P
from shared.qt.tutorial_popup import DIM, YELLOW, Tab, h3, h4, page

ACCENT = "#55ddaa"              # the window's own accent (window.py)
_C_ITEM = P.tool_market
_C_TRADE = P.tool_trade
_C_MAP = P.energy_cyan
_C_LIST = "#cc88ff"
_C_ROUTE = "#ffb347"
_ACC = f"color: {ACCENT};"


def _hotkey(settings_key: str, default: str, fallback: str) -> str:
    """A tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label(settings_key, default)
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return fallback


def _getting_started() -> str:
    hotkey = _hotkey("hotkey_everything_finder", "<ctrl>+6", "Ctrl+6")
    return page(f"""
{h3("Welcome to the Everything Finder", ACCENT)}
<p>The Everything Finder is three tools in one window: find an item, find a
trade, and see where it is on the map. One shopping list is shared by all
three.</p>

{h4("The three tabs", ACCENT)}
<ul>
  <li><b>ITEM FINDER</b> &mdash; where an item is sold and what it costs</li>
  <li><b>TRADE HUB</b> &mdash; what to haul, from where to where</li>
  <li><b>STAR MAP</b> &mdash; the map, with prices and routes on it</li>
</ul>
<p>A tool loads the first time you open its tab, so the first click on a tab
takes a moment. The window opens on the tab you used last.</p>
<p>Press <b>Ctrl+Tab</b> to go to the next tab and <b>Ctrl+Shift+Tab</b> to
go back.</p>

{h4("The shopping list", ACCENT)}
<p><b>Shopping List</b>, at the right of the tab row, opens your shopping
list in a window beside this one. See the <b>Shopping List</b> tab of this
tutorial.</p>

{h4("Opening and closing", ACCENT)}
<p>The launcher's hotkey for this window is
<span style="{_ACC}">{hotkey}</span>. It shows and hides the window, and so
does its tile on the launcher. The <b>x</b> in the title bar closes the
window and the three tools in it.</p>

{h4("Each tool has a longer tutorial", ACCENT)}
<ul>
  <li>Item Finder: <b>? Tutorial</b> at the top of its tab</li>
  <li>Trade Hub: <b>Tutorial</b> at the top of its tab</li>
  <li>Star Map: the button on the <b>Star Map</b> tab of this tutorial</li>
</ul>
""")


_ITEM_FINDER = page(f"""
{h3("Item Finder", _C_ITEM)}
<p>Use the <b>ITEM FINDER</b> tab to find where to buy a piece of gear, a
ship part or a ship.</p>
<ol>
  <li>Type part of a name in <b>Search items...</b></li>
  <li>Click a row. The panel on the right lists <b>WHERE TO BUY</b>, cheapest
      first.</li>
  <li>Double-click a row to keep its details open in a floating bubble.</li>
</ol>

{h4("Working with the other tabs", _C_ITEM)}
<ul>
  <li><b>Star Map</b>, at the top of the tab, takes you to the
      <b>STAR MAP</b> tab.</li>
  <li><b>Shopping List</b>, beside it, opens your shopping list. Drag a row
      from the item table onto the list to add it.</li>
</ul>
<p style="{DIM}">The full tutorial is behind <b>? Tutorial</b> at the top of
the tab.</p>
""")

_TRADE_HUB = page(f"""
{h3("Trade Hub", _C_TRADE)}
<p>Use the <b>TRADE HUB</b> tab to find something to haul for profit.</p>
<ol>
  <li>Choose your ship under <b>VEHICLE:</b></li>
  <li>Read the table: each row is something to buy in one place and sell in
      another, best first.</li>
  <li>Double-click a row for the route's card.</li>
</ol>
<p>The buttons under <b>VIEW MODE:</b> change what the tab shows.</p>

{h4("Two maps", _C_TRADE)}
<p>Trade Hub has a map of its own: its <b>STAR MAP</b> view, and
<b>Show Route</b> on a route's card, use that one. It is not the
<b>STAR MAP</b> tab of this window.</p>

{h4("With the Star Map tab", _C_TRADE)}
<p>Once you have opened both the <b>TRADE HUB</b> and the <b>STAR MAP</b>
tab, the two work together:</p>
<ul>
  <li>The map gets an <b>OVERLAYS</b> list that draws Trade Hub's routes on
      it.</li>
  <li>A place's window on the map gets <b>Trade routes from here</b>, which
      brings you back to this tab with the routes from that place.</li>
</ul>
<p style="{DIM}">The full tutorial is behind <b>Tutorial</b> at the top of
the tab.</p>
""")

_STAR_MAP = page(f"""
{h3("Star Map", _C_MAP)}
<p>Use the <b>STAR MAP</b> tab to look around the universe and see what each
place sells.</p>

{h4("Getting around", _C_MAP)}
<ul>
  <li>Drag with the left button to turn the view, scroll to zoom, drag with
      the right button to slide it.</li>
  <li>Double-click a system, a planet or a moon to go into it.
      <b>&lt; Back</b> goes out one level.</li>
  <li>Type in <b>Search system or location...</b> and pick a place to go
      straight to it.</li>
  <li><b>Home</b> returns to your home system. Right-click it to change which
      system that is.</li>
</ul>

{h4("Prices", _C_MAP)}
<ul>
  <li>Double-click a station, city or outpost that has a terminal to open its
      window, with <b>Commodities</b> and <b>Items</b> tabs.</li>
  <li><b>Market</b> opens a panel to look up what a terminal sells, or where
      one item is sold. <b>Commodities</b> opens a panel of every commodity
      and its prices.</li>
</ul>

{h4("A jump route on the map", _C_MAP)}
<p>Press <b>Route</b>, click the system you start from, then the system you
are going to. The map draws the jumps and tells you how many. The button
reads <b>Cancel</b> while you are picking and <b>Clear route</b> once a route
is drawn. This only draws on the map; to set a route in the game, see the
<b>Set Route</b> tab of this tutorial.</p>
""")

_SHOPPING = page(f"""
{h3("Shopping List", _C_LIST)}
<p>Collect everything you need to buy, then have the trip worked out. There
is one list: Item Finder and the Star Map show the same one, and it is kept
when you close the window.</p>

{h4("Opening it", _C_LIST)}
<p>Press <b>Shopping List</b> at the right of the tab row. The same button in
Item Finder and on the Star Map opens this same list.</p>

{h4("Adding things", _C_LIST)}
<ul>
  <li>In the list, choose <b>Item</b> or <b>Commodity</b>, type a name, set
      how many, and press <b>Add</b>.</li>
  <li>Or drag a row from Item Finder's table and drop it on the list.</li>
  <li>Or, on the Star Map, open a place's window and drag a row from its
      <b>Items</b> tab. An item added that way is tied to that place; click
      the tag on its row to let the planner choose.</li>
</ul>
<p>The <b>x</b> on a row removes it. <b>Clear list</b> empties the list.</p>

{h4("Planning the trip", _C_LIST)}
<ol>
  <li>Set <b>Start</b> to where you are setting off from, or leave it
      empty.</li>
  <li>Choose what matters most under <b>Prefer</b>: <b>MIN STOPS</b>,
      <b>SHORTEST TRIP</b> or <b>BEST PRICE</b>.</li>
  <li>Press <b>Plan route</b>. With <b>Auto-calculate</b> ticked this
      happens by itself a moment after the list changes.</li>
  <li>Press <b>Show on Star Map</b> on a plan to see it drawn on the
      <b>STAR MAP</b> tab, in the order you would visit.</li>
</ol>
<p style="{DIM}"><b>Terminals per entry</b> is how many of the cheapest
places for each thing the planner may choose between.</p>
""")

_SET_ROUTE = page(f"""
{h3("Setting a route in the game", _C_ROUTE)}
<p>The toolbox can set your route inside Star Citizen for you: it opens the
game's own map, types the destination and sets the route, using your mouse
and keyboard.</p>

{h4("Set it up once", _C_ROUTE)}
<ol>
  <li>On the <b>STAR MAP</b> tab, turn on <b>In-Game</b>. It is the same
      switch as <b>In-Game</b> in the Toolbox Assistant. While it is off,
      nothing is ever sent to the game.</li>
  <li>With Star Citizen open, click on the game and say
      <em>calibrate star map</em> to the Toolbox Assistant. If you have no
      microphone, press <b>Calibrate Route</b> in the Assistant, then
      <b>Begin</b>.</li>
  <li>Do the three steps it tells you: press F2 and zoom out, left-click the
      map's search bar and press Enter; type a destination in your current
      system and left-click its result; left-click the centre of the map.</li>
</ol>

{h4("Setting a route by voice", _C_ROUTE)}
<ol>
  <li>Say <em>navigate to Area 18</em> or <em>set route to Area 18</em> to
      the Toolbox Assistant.</li>
  <li>It names the place and asks whether to set the route. Say
      <em>yes</em>.</li>
  <li>Leave the mouse and keyboard alone while it works, until it tells you
      the route is plotted.</li>
</ol>
<p style="{DIM}">It pastes the destination's name into the game, so whatever
you had copied is replaced.</p>

{h4("Typing it instead", _C_ROUTE)}
<p>Type <em>navigate to Area 18</em> in the command box of the
<b>STAR MAP</b> tab and press Enter.
<span style="{YELLOW}">Typed, it does not ask first.</span> With
<b>In-Game</b> on it starts at once. With <b>In-Game</b> off the map only
goes to the place.</p>
""")


def tabs(open_star_map_tutorial: Optional[Callable[[], None]] = None) -> List[Tab]:
    """The tutorial's tabs, built when it opens so the hotkey shown is the current one.

    *open_star_map_tutorial* opens the Star Map tool's own tutorial; the button for it is left
    out when there is no way to open it."""
    star_map = Tab("Star Map", _STAR_MAP)
    if open_star_map_tutorial is not None:
        star_map = Tab("Star Map", _STAR_MAP, "Open the Star Map tutorial", open_star_map_tutorial)
    return [
        Tab("Start", _getting_started()),
        Tab("Item Finder", _ITEM_FINDER),
        Tab("Trade Hub", _TRADE_HUB),
        star_map,
        Tab("Shopping List", _SHOPPING),
        Tab("Set Route", _SET_ROUTE),
    ]


def markup() -> str:
    """All the text, for the tests."""
    return "".join(t.html for t in tabs())
