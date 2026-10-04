"""SC Toolbox — Starmap.

The combined star map: the full galaxy -> system -> planet globe scenes from
the Trade Hub and Market Finder star maps merged into one standalone tool,
with a UEX-backed location browser (commodities + items), a grocery list,
route plotting (single and multi-stop shopping routes) and a command
router. Voice input is the AI Assistant's; this package opens no microphone.

Keep this package's ``__init__`` import-light: the entry script
(``starmap_app.py``) and the standalone harness in ``__main__`` put the
toolbox root on ``sys.path`` before importing ``shared``.
"""
