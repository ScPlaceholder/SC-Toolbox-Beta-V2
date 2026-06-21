"""SC Toolbox — Star Map.

Self-contained, render-on-demand star map for the Trade Hub suite.

Scenes (built incrementally): galaxy -> system -> planet globe.
Rendering is pure QPainter (no QtWebEngine, no 3D engine) so the whole
feature adds ~zero installer footprint and idles at ~0% CPU.

Keep this package's ``__init__`` import-light: the standalone harness in
``__main__`` adds the repo root to ``sys.path`` before importing ``shared``.
"""
