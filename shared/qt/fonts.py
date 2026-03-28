"""
Font loader – bundles and registers Electrolize (Star Citizen MobiGlas font).
"""

from __future__ import annotations
import os
import logging
from PySide6.QtGui import QFontDatabase

log = logging.getLogger(__name__)

_loaded = False

FONTS_DIR = os.path.join(os.path.dirname(__file__), "fonts")


def load_fonts() -> None:
    """Register bundled fonts with Qt.  Safe to call multiple times."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    electrolize = os.path.join(FONTS_DIR, "Electrolize-Regular.ttf")
    if os.path.isfile(electrolize):
        fid = QFontDatabase.addApplicationFont(electrolize)
        if fid >= 0:
            families = QFontDatabase.applicationFontFamilies(fid)
            log.debug("Loaded font: %s (families: %s)", electrolize, families)
        else:
            log.warning("Failed to load font: %s", electrolize)
    else:
        log.warning("Font file not found: %s", electrolize)
