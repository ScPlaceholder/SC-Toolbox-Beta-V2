"""How the Star Map reaches set-route: it is the AI Assistant's code.

Set route (destination matching, the in-game macro, its calibration and the
In-Game switch) lives in ONE place, ``tools/Assistant/assistant/set_route/``
(J, 2026-10-04). The Star Map keeps its typed "navigate to ..." command and
its In-Game button, and both call that copy through this module. Calibration
is not reachable from here on purpose: its button moved to the Assistant's
window with the macro it calibrates. The Star Map has no set-route code of its own, and
must not grow any: a second copy is the one that drifts unwatched.

The Assistant's folder is appended to ``sys.path`` (last, so it can shadow
nothing) and only ``assistant.set_route`` is imported. ``assistant/__init__``
is stdlib-only and imports none of its submodules, so this loads no voice,
no model and no window into the map's process.

Everything here raises :class:`SetRouteUnavailable` with a sentence fit for
the status line when the Assistant tool is not installed beside the map.
"""
from __future__ import annotations

import importlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
#: set_route_link.py -> starmap -> Starmap -> skills -> toolbox root
TOOLBOX_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
ASSISTANT_DIR = os.path.join(TOOLBOX_ROOT, "tools", "Assistant")
PACKAGE = "assistant.set_route"


class SetRouteUnavailable(RuntimeError):
    """Set route cannot be reached from this Star Map; str() says why."""


def _module(name: str):
    if not os.path.isdir(os.path.join(ASSISTANT_DIR, "assistant", "set_route")):
        raise SetRouteUnavailable(
            "set route lives in the AI Assistant tool, which is not installed here")
    if ASSISTANT_DIR not in sys.path:
        sys.path.append(ASSISTANT_DIR)
    try:
        return importlib.import_module(PACKAGE + "." + name)
    except ImportError as exc:
        raise SetRouteUnavailable("set route could not be loaded from the AI Assistant (%s)"
                                  % exc) from exc


def service():
    """A new RouteService (resolve a destination; plot it in game)."""
    return _module("service").RouteService()


def gate():
    """The In-Game switch module (in_game_enabled / set_in_game)."""
    return _module("gate")

