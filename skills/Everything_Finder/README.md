<!-- Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
     written 2026-10-03T21:47-0400, parent: session:7bee459a -->
# Everything Finder

Item Finder, Trade Hub and Star Map in one window, as three tabs, with one
shared shopping list. Asked for by J on 2026-10-03. Default hotkey: **Ctrl+6**
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

The button beside the tabs pops out a list that takes items AND commodities.
Routes are planned by **Trade Hub's basket planner** (`basket_engine`), never
by a copy: items are put into the same terminal index Trade Hub's BASKET view
builds for commodities, keyed by UEX terminal id. Options: start terminal,
preferred strategy (MIN STOPS / SHORTEST TRIP / BEST PRICE), terminals per
entry, auto-calculate. "Show on Star Map" draws a plan in the Star Map tab.

## Star Map tab and the microphone

The Star Map tab is the standalone Star Map's panel, and that panel used to
own voice ears: with its mic mode saved as "Always on", selecting the tab
armed the microphone. It has no ears now. Voice-to-text lives in the AI
Assistant, which sends map commands to this window as the IPC command
`map_command`; the window opens the Star Map tab and hands the command to it.
The Item Finder tab's own "MARKET FINDER" header is hidden here (the tab
names the tool).

## Not done yet

* **Three lists, not one.** Item Finder's own Grocery List and the Star Map's
  own Grocery panel still exist inside their tabs and are not merged with the
  shared list. Nothing is copied between them.
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
