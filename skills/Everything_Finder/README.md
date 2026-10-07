# Everything Finder

Item Finder, Trade Hub and Star Map in one window, as three tabs, with the
toolbox's one shopping list beside them. Asked for by J on 2026-10-03. Default hotkey: **Ctrl+6**
(free at the time; Shift+5 / Shift+6 still open Item Finder / Trade Hub on
their own, unchanged).

## How it loads

Each tab is built the first time it is selected (`LazyTabStack`). Opening the
window builds only the tab it opens on (the last one used), so it costs about
what opening that one tool costs.

* **Item Finder** and **Trade Hub** are the real tool windows, constructed the
  way their launchers construct them, with their central widget transplanted
  into the tab (`embed.py` explains why transplant and not re-parent).
* **Star Map** is `skills/Starmap` (the standalone star map), loaded under the
  alias `ef_starmap` so it cannot collide with Trade Hub's own `starmap`
  package. It now carries the features of all three star maps (see below).

## Shopping list

There is **one** shopping list in the toolbox (`shared/shopping/`), and this
window, the standalone Item Finder and the standalone Star Map all show it:
one data model, one widget, one file (`~/.sctoolbox/shopping/shopping_list.json`).
Add something in any of them and it is on the list in the others; tools that
are open at the same time pick the change up within a second or two.

The button beside the tabs pops it out. Inside this window the Item Finder
tab's "Shopping List" button and the Star Map tab's "Shopping List" button
open the same pop-out rather than a second copy each.

What it does:

* takes items AND commodities, by name with a quantity, or by dragging an
  item onto it (a row from Item Finder's table, a Star Map item pop-out);
* an item added from a place on the Star Map is **pinned** to that place
  (click the tag on its row to unpin);
* once prices are in, each entry lists where it is sold, cheapest first;
* routes are planned by **Trade Hub's basket planner** (`basket_engine`),
  never by a copy: items are put into the same terminal index Trade Hub's
  BASKET view builds for commodities, keyed by UEX terminal id. Options: start
  terminal, preferred strategy (MIN STOPS / SHORTEST TRIP / BEST PRICE),
  terminals per entry, auto-calculate;
* "Show on Star Map" draws a plan in the Star Map tab, and the drawn route
  then follows the list as it changes.

It began as this tool's own list (2026-10-03) and became the shared one on
2026-10-04, when Item Finder's Grocery List bubble and the Star Map's Grocery
panel were retired into it. The first launch imports the Star Map's old
`grocery.json` and this tool's old `shopping_list.json` if they exist, and
leaves both files where they are.

Not carried over from the retired lists: Item Finder's "Shorter trip over
cheapest price +N%" option. It was a setting of Item Finder's own route
planner; Trade Hub's planner has SHORTEST TRIP and BEST PRICE as whole
strategies and nothing in between, and adding that here would be a second
route planner. The retired planners' code (`market_finder/route_planner.py`,
`starmap/route_planner.py`) is still on disk because the star maps use its
`visits()` helper to draw; nothing calls its planning functions any more.

## Star Map tab and the microphone

The Star Map tab is the standalone Star Map's panel, and that panel used to
own voice ears: with its mic mode saved as "Always on", selecting the tab
armed the microphone. It has no ears now. Voice-to-text lives in the AI
Assistant, which sends map commands to this window as the IPC command
`map_command`; the window opens the Star Map tab and hands the command to it.
The Item Finder tab's own "MARKET FINDER" header is hidden here (the tab
names the tool).

## Not done yet

* **Quantities are informational.** The planner picks where to buy, not how
  much each terminal has; stock (SCU) is not checked against the quantity.
* **Pickup only.** Trade Hub's planner also has a SELL mode
  (`plan_sell_variants`); the shared list does not use it yet.
* **Trade Hub's in-tool STAR MAP view** (its own view mode) is still Trade
  Hub's original map. Item Finder's Star Map button is redirected to the Star
  Map tab; Trade Hub's is not, because its route-detail "show on map" is wired
  to its internal map.
* Commodity data, when Trade Hub's tab is not open, comes from Trade Hub's own
  fetcher, which also warms its distance cache in the background, just as
  opening Trade Hub does.

## Star Map: union of the three copies

Ported into `skills/Starmap` on 2026-10-03:
from the Trade Hub map - trade overlays (Trade Flows / Top Routes / Activity /
My Runs, shown only when Trade Hub routes are attached), `show_route()` for a
calculated buy -> sell route with jump gateways, and the link-through from a
location card to Trade Hub's routes table; from the Item Finder map - items
grouped into collapsible categories, refill when the items index lands, and
items indexed under a terminal's moon / planet / nickname names.

Not ported: Trade Hub map's "Pop Out" (the map in its own window) and the
Item Finder map's full item-detail bubble and per-session price fallback.
