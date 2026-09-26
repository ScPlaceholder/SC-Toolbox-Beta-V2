"""SC Toolbox — Star Map.

Self-contained, render-on-demand star map for Market Finder.

Scenes (built incrementally): galaxy -> system -> planet globe.
Rendering is pure QPainter (no QtWebEngine, no 3D engine) so the whole
feature adds ~zero installer footprint and idles at ~0% CPU.

Keep this package's ``__init__`` import-light: the panel is opened lazily
from ``market_finder.ui.app`` (``from ..starmap.panel import MarketMapPanel``),
by which time the skill entry point (``uex_item_browser.py``) has already
put the toolbox root on ``sys.path``, so ``shared`` resolves without any
work here.
"""
