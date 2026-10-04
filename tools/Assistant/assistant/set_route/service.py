"""RouteService: resolve a destination, and plot it in game. No Qt, no window.

The Assistant's tools and the Star Map's command bar both call this, so what
"set route to X" means, and what stands between a request and the game, is
written once.

Two steps, kept apart on purpose:

  * :meth:`RouteService.resolve` - a spoken or typed name to a known
    destination. Reads JSON; touches nothing.
  * :meth:`RouteService.plot`    - run the in-game macro. This is the only
    place the macro is started, and it checks the In-Game switch (gate.py)
    itself, every time, before anything else. A caller cannot pass the answer
    in, so a caller cannot get it wrong.

The engine and the macro are built lazily through factories. Tests hand in
fakes; nothing here imports pynput or PySide6 until a real route is plotted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from . import gate as _gate

log = logging.getLogger(__name__)

#: Status texts. The Star Map shows them on its command bar; the Assistant
#: says them. Kept as the Star Map worded them before the move.
ENGINE_UNAVAILABLE = "destination engine unavailable"
SETTER_UNAVAILABLE = "route setter unavailable"
NEEDS_PYNPUT = "in-game plotting needs pynput (pip install pynput)"
BUSY = "already setting a route"


def in_game_off(dest: str) -> str:
    return "route to %s (toggle 'In-Game' on to plot it in the game)" % dest


def started(dest: str) -> str:
    return "setting route to %s in game" % dest


def character_line(key: str, default: str, **fields) -> str:
    """A fixed line in the pilot's chosen character's words; *default* without them."""
    try:
        from shared.character_voice import line
        return line(key, **fields)
    except ImportError as exc:
        log.debug("set route: shared.character_voice unavailable (%s); plain wording for %r", exc, key)
        return default
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        log.warning("set route: character line %r failed to render (%s: %s); plain wording",
                    key, type(exc).__name__, exc)
        return default


@dataclass
class Resolution:
    """What a name resolved to. Exactly one of the three is set, or ``error``."""
    phrase: str
    destination: str = ""
    alternatives: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def unknown(self) -> bool:
        return not (self.destination or self.alternatives or self.error)


def _default_engine():
    from .destination_engine import DestinationPhoneticEngine
    return DestinationPhoneticEngine()


def _default_setter():
    from .route_setter import InGameRouteSetter
    return InGameRouteSetter()


class RouteService:
    def __init__(self, engine_factory: Optional[Callable] = None,
                 setter_factory: Optional[Callable] = None, gate=None) -> None:
        self._engine_factory = engine_factory or _default_engine
        self._setter_factory = setter_factory or _default_setter
        self._gate = gate if gate is not None else _gate
        self._engine = None
        self._setter = None

    # ── the parts, built on first use ────────────────────────────────────
    def engine(self):
        if self._engine is None:
            try:
                self._engine = self._engine_factory()
            except Exception as exc:                     # noqa: BLE001 - reported to the pilot by the caller
                log.warning("set route: the destination engine could not be built (%s: %s)",
                            type(exc).__name__, exc, exc_info=True)
                self._engine = None
        return self._engine

    def setter(self):
        if self._setter is None:
            try:
                self._setter = self._setter_factory()
            except Exception as exc:                     # noqa: BLE001 - reported to the pilot by the caller
                log.warning("set route: the in-game route setter could not be built (%s: %s)",
                            type(exc).__name__, exc, exc_info=True)
                self._setter = None
        return self._setter

    # ── the switch ───────────────────────────────────────────────────────
    def in_game(self) -> bool:
        return bool(self._gate.in_game_enabled())

    # ── step 1: a name to a destination (touches nothing) ────────────────
    def resolve(self, phrase: str) -> Resolution:
        phrase = (phrase or "").strip()
        engine = self.engine()
        if engine is None:
            return Resolution(phrase, error=ENGINE_UNAVAILABLE)
        dest, alts = engine.find_destination(phrase)
        if alts:
            return Resolution(phrase, alternatives=list(alts))
        if not dest:
            return Resolution(phrase)
        return Resolution(phrase, destination=dest)

    def known(self, dest: str) -> bool:
        """Is *dest* exactly a destination in the list (not merely close to one)?"""
        engine = self.engine()
        if engine is None or not dest:
            return False
        try:
            return engine.find_exact(dest) is not None
        except AttributeError:
            got, alts = engine.find_destination(dest)
            return bool(got) and not alts and got.lower() == dest.lower()

    # ── step 2: the game ─────────────────────────────────────────────────
    def why_not(self, dest: str) -> str:
        """Why *dest* cannot be plotted right now; "" when it can."""
        if not self.in_game():
            return in_game_off(dest)
        setter = self.setter()
        if setter is None:
            return SETTER_UNAVAILABLE
        if not setter.available():
            return NEEDS_PYNPUT
        if setter.busy():
            return BUSY
        return ""

    def plot(self, dest: str,
             status_cb: Optional[Callable[[str], None]] = None,
             done_cb: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
        """Start the in-game macro for *dest*. Returns ``(started, message)``.

        Nothing is sent to the game unless the In-Game switch is on."""
        problem = self.why_not(dest)
        if problem:
            return False, problem
        if not self.setter().set_route(dest, status_cb=status_cb, done_cb=done_cb):
            return False, BUSY
        return True, started(dest)
