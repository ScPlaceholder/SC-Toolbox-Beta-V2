# Starmap

Galaxy, system and planet map for the SC Toolbox, with UEX commodity and
item prices, a grocery list, lore cards and voice navigation.
Built by Red (Kimi) for J, 2026-09-23. Default hotkey: Shift+9.

## Voice

- **Ears** (`starmap/voice/ears.py`): mic + faster-whisper `small.en` on
  the CPU behind a trigger binding. Needs `sounddevice`, `faster-whisper`,
  `numpy`, `pynput`.
- **Destinations** (`starmap/voice/destination_engine.py`): the WingmanAI
  set_route phonetic engine. Reads `tools/set_route_ai/data/` when that
  folder exists (shared learning with the Wingman skill), else the
  bundled copy in `starmap/data/set_route/`.

## In-game route macro (`starmap/voice/route_setter.py`)

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
