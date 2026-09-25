"""Standalone harness: ``python -m starmap`` (dev).

Puts the toolbox root on ``sys.path`` (so ``shared`` resolves), boots the
skill like the launcher would, then runs the app entry point.
"""
from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(_SKILL_DIR, "..", "..")))

from shared.app_bootstrap import bootstrap_skill  # noqa: E402

bootstrap_skill(os.path.join(_SKILL_DIR, "starmap_app.py"))

if __name__ == "__main__":
    from starmap_app import main
    main()
