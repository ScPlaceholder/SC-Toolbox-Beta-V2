# Starmap

Galaxy, system and planet map for the SC Toolbox, with UEX commodity and
item prices, the shared shopping list, lore cards and a command bar.
Built by Red (Kimi) for J, 2026-09-23. Default hotkey: Ctrl+5 (was Shift+9, which earlier releases gave Mining Signals).

## Search goes all the way in

Typing a place in the search box takes the map all the way to it (J,
2026-10-04), through `StarmapPanel.goto`. The map is four scenes deep: galaxy >
system > planet & moons > globe.

- A **system** is entered (what double-clicking it does).
- A **planet or moon** ends on its own globe.
- A **place on a planet or moon** (landing zone, outpost, station in its orbit)
  ends on that body's globe, turned so the place faces you at the centre,
  zoomed in, ringed and named. The globe is the deepest scene there is: a place
  is a pin on it.
- **Anything else** (a Lagrange station, a gateway, a jump point, an asteroid
  base) lives in the system scene: centred there at the close zoom, ringed.

Nothing animates, like every other move on this map. The scenes on the way are
pushed on the nav stack as if walked by hand, so Back, Home and the wheel work
as usual. A name found in two systems (each end of a jump has a "Pyro Gateway")
means the one in the system on screen, else the first in the data's order.
"area 18" finds the data's "Area18". Enter on text that names no one place goes
nowhere and says so on the status line (`PlaceSearch` in `panel.py`).

The same code moves the map for "go to X" and "navigate to X" in the command
bar, the Assistant's `map_goto`, and the Trade Hub's buy / sell pickers. It
never reaches the in-game route code; only "navigate to" / "set route to" do.

## Shopping list

The **Shopping List** button docks the toolbox's one shopping list
(`shared/shopping/`) beside the map - the same list, widget and file that Item
Finder and the Everything Finder show. An item added from a place on the map
(an item pop-out, or **+ Shopping List** in the Market panel) is pinned to
that place. Routes are planned by Trade Hub's basket planner; **Show on Star
Map** draws one here in the planner's order, and the drawn route follows the
list. The map's own grocery panel (`starmap/grocery.py`, with its own file and
its own stop ordering) was retired on 2026-10-04; an old
`~/.sctoolbox/starmap/grocery.json` is imported once and left in place.

## Commands (and where voice went)

The Star Map **has no microphone**. Until 2026-10-04 it had its own voice
ears (`starmap/voice/`), and a saved mic mode of "Always on" armed them every
time the map was opened, including as a tab of the Everything Finder. J asked
for voice-to-text to live in one place: the **AI Assistant** (`tools/Assistant`).

- **Command router** (`starmap/commands.py`): turns a line of text into a map
  action - "navigate to Area 18", "route to Pyro", "clear route", "zoom in",
  "take me home", "open the shopping list", "help".
- **Command bar** (`starmap/command_bar.py`): type a command and press Enter;
  also holds the status line. (Calibrate moved to the Assistant, see below.)
- **From the Assistant**: what the Assistant hears for the map arrives as the
  IPC command `{"type": "map_command", "text": ..., "id": ..., "reply_file": ...}`
  (`StarmapPanel.handle_map_command`). The map answers into the reply file and
  the Assistant says the answer; the map itself never speaks, because a second
  voice would be heard by the Assistant's open mic as the pilot. Inside the
  Everything Finder the window forwards the same command to its Star Map tab.
- **Set route is not the map's code** (2026-10-04). Destination matching, the
  in-game macro, its calibration and the In-Game switch live in
  `tools/Assistant/assistant/set_route/`, so the Assistant sets a route with
  the map closed. The map reaches that one copy through
  `starmap/set_route_link.py` for the two things it kept: the typed
  "navigate to ..." command and the In-Game button (the same saved switch as
  the Assistant's). Said aloud, "navigate to ..." is handled by the Assistant
  and never relayed here; the Assistant only sends `{"type": "map_goto",
  "name": ...}` so an open map shows the place.
- The old mic settings ("ears" and "voice" in
  `~/.sctoolbox/starmap/starmap_state.json`) are no longer written by the map
  and are left in place; the Assistant reads them once to carry them over.
  The former ears code is in `tools/Assistant/assistant/starmap_ears/`.

## In-game route macro (`tools/Assistant/assistant/set_route/route_setter.py`)

Calibration (three clicks on the game's own star map) is opened from the
Assistant's window, **Calibrate Route**. The Star Map has no Calibrate button
and no right-click "Calibrate in-game route setter..." any more.

With **In-Game** toggled on, "navigate to <destination>" plots the route
in Star Citizen itself: F2, click the map, search, paste the destination,
click the result, R x6, F2. It moves the mouse and presses keys in the
game window, using the click positions in `mouse_calibration.json`.

This is J's own set-route code, kept as it is by design (decision
2026-09-25).

**It has never been run end to end in game.** Every test so far replaced
the setter with a stub (no input is ever sent from a test), so the click
positions, timings and the result click are unverified in a live client.
Try it once with the game open and the map closed before relying on it.
