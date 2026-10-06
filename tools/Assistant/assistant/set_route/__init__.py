"""Set route: the one implementation, owned by the AI Assistant.

"Set route to Area 18" resolves a spoken destination and walks Star Citizen's
own star map to plot that route in game. J asked on 2026-10-04 for this to
belong to the Assistant, so the Assistant can set a route with the Star Map
closed. It lived in skills/Starmap/starmap/set_route/ (and before that in
starmap/voice/); the Star Map now reaches THIS copy through
skills/Starmap/starmap/set_route_link.py, and there is no other.

  * destination_engine - J's WingmanAI set_route phonetic engine: turns
    "area eighteen" into a known destination, or a short "which one?" list.
    Pure Python and JSON; no Qt, no game, no map.
  * route_setter       - the in-game macro (F2, clicks, a clipboard paste, R,
    F2 through pynput) and its 3-click calibration dialog. Imports PySide6 at
    module level for the dialog, so it is only imported when a route is
    actually plotted or calibrated.
  * gate               - the In-Game switch. Nothing is sent to the game while
    it is off. One file on disk, shown as an "In-Game" button in both the
    Assistant and the Star Map.
  * service            - RouteService: resolve a destination, and plot() it.
    plot() is the ONLY caller of the macro and reads the gate itself, so no
    caller can skip the switch.
  * phrases            - which utterances are set-route requests.

destination_engine.py and route_setter.py are J's code, moved byte-identical
(decision 2026-09-25: kept as it is). They sit five folders below the toolbox
root, the same depth as before, which their data-path lookups rely on: the
destination list is the live tools/set_route_ai/data (shared with the WingmanAI
skill) where that folder exists, else the copy packaged at
assistant/data/set_route/. The folder names in their path comments are from two
homes ago; the depth is what matters. The click calibration is not packaged:
it is the pilot's own, in ~/.sctoolbox/set_route/ (route_setter.py).

What set-route needs, and does not need: pynput, the Win32 clipboard, the
calibration file and Star Citizen in the foreground. It needs nothing from a
running Star Map: no map state, no current system, no window.

This package's __init__ imports nothing, so importing ``assistant.set_route``
is free; the Star Map does it without loading the rest of the Assistant.
"""
