"""
Centralized sys.path configuration for the SC_Toolbox project.

Import this module early to ensure all standard project paths are on sys.path.
Each path is added only once (idempotent).

Paths added:
  - PROJECT_ROOT: the top-level SC_Toolbox_Beta_V1.2 directory, enabling
    ``import shared.*``, ``import core.*``, ``import ui.*``, etc.

Individual skill directories are NOT added here because they vary per entry
point.  Use ``ensure_path()`` in your entry-point script for skill-local dirs.

Usage from an entry point that already has the project root on sys.path
(e.g. because skill_launcher adds it before any imports):

    import shared.path_setup          # side-effect: ensures PROJECT_ROOT

Usage from a file that does NOT yet have the project root on sys.path
(i.e. a standalone skill entry point launched via subprocess):

    import os, sys
    sys.path.insert(0, os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
    import shared.path_setup          # finishes the job
"""

import os
import sys

# ---------------------------------------------------------------------------
# Project root — the directory that contains shared/, core/, ui/, skills/
# ---------------------------------------------------------------------------
PROJECT_ROOT: str = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
)


def ensure_path(path: str, *, first: bool = False) -> None:
    """Add *path* to ``sys.path`` if it is not already present.

    If *first* is ``True``, ensure *path* is at index 0 even if it already
    exists elsewhere — this is needed for skill directories that contain a
    ``ui/`` package that must shadow the project-root ``ui/`` launcher package.
    """
    normed = os.path.normpath(path)
    if first:
        if normed in sys.path:
            sys.path.remove(normed)
        sys.path.insert(0, normed)
    elif normed not in sys.path:
        sys.path.insert(0, normed)


# Always ensure the project root is importable.
ensure_path(PROJECT_ROOT)
