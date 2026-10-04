# Starmap

Galaxy, system and planet map for the SC Toolbox, with UEX commodity and
item prices, a grocery list, lore cards and a command bar.
Built by Red (Kimi) for J, 2026-09-23. Default hotkey: Ctrl+5 (was Shift+9, which earlier releases gave Mining Signals).

## Commands (and where voice went)

The Star Map **has no microphone**. Until 2026-10-04 it had its own voice
ears (`starmap/voice/`), and a saved mic mode of "Always on" armed them every
time the map was opened, including as a tab of the Everything Finder. J asked
for voice-to-text to live in one place: the **AI Assistant** (`tools/Assistant`).

- **Command router** (`starmap/commands.py`): turns a line of text into a map
  action - "navigate to Area 18", "route to Pyro", "clear route", "zoom in",
  "take me home", "open the shopping list", "help".
- **Command bar** (`starmap/command_bar.py`): type a command and press Enter;
  also holds Calibrate Star Map and the status line.
- **From the Assistant**: what the Assistant hears for the map arrives as the
  IPC command `{"type": "map_command", "text": ..., "id": ..., "reply_file": ...}`
  (`StarmapPanel.handle_map_command`). The map answers into the reply file and
  the Assistant says the answer; the map itself never speaks, because a second
  voice would be heard by the Assistant's open mic as the pilot. Inside the
  Everything Finder the window forwards the same command to its Star Map tab.
- **Destinations** (`starmap/set_route/destination_engine.py`): the WingmanAI
  set_route phonetic engine. Reads `tools/set_route_ai/data/` when that
  folder exists (shared learning with the Wingman skill), else the
  bundled copy in `starmap/data/set_route/`.
- The old mic settings ("ears" and "voice" in
  `~/.sctoolbox/starmap/starmap_state.json`) are no longer written by the map
  and are left in place; the Assistant reads them once to carry them over.
  The former ears code is in `tools/Assistant/assistant/starmap_ears/`.

## In-game route macro (`starmap/set_route/route_setter.py`)

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
