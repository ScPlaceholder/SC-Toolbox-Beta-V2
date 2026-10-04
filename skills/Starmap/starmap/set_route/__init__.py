"""The set_route port: spoken-destination matching and the in-game route macro.

  * destination_engine - the WingmanAI set_route phonetic engine: turns
    "area eighteen" into a known destination (or a short "which one?" list).
  * route_setter       - walks Star Citizen's own star map to set that route
    in game (moves the mouse, presses keys), plus its 3-click calibration.

J's own set-route code, kept as it is by design (decision 2026-09-25). Both
files were in starmap/voice/ until 2026-10-04; that package went away when
voice-to-text moved into the AI Assistant, and these two stayed with the map
because they are what the map DOES with a destination, not how it hears one.
They sit at the same folder depth as before, which their data-path lookups
(``..``/data/set_route and the five-dirname toolbox root) depend on.
"""
