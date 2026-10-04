"""Which utterances ask for a route to be set in game.

    "set route to Area 18"       "navigate to Port Tressler"
    "set a course to Lorville"   "plot a course to Everus Harbor"
    "route to Area 18"           "star map, navigate to Area 18"

Each is anchored at the START of the utterance: "what is the best trade route
to Pyro" must not become "route to Pyro".

One phrase is deliberately NOT a set-route request: "route to <star system>"
("route to Pyro"). That is the Star Map drawing a jump route across its own
galaxy view, which is the map's business and stays there. Star systems are not
destinations the in-game search knows either. "Clear route" likewise only
clears what the Star Map drew; there is no in-game clear to move.
"""
from __future__ import annotations

import re
from typing import Callable, Iterable, Union

_CLEAN = re.compile(r"[^\w\s]")

#: "star map, navigate to X" used to be relayed to the map. A route is the
#: Assistant's now, with or without the map's name in front.
_PREFIX = re.compile(r"^(?:please )?(?:(?:on |tell |ask )?(?:the )?star ?map(?: to)? )?(?:please )?")

_SET = re.compile(r"^(?:set (?:the |a )?route to|navigate to|set (?:a )?course to|"
                  r"plot (?:a )?course to) (\S.*)$")
_BARE = re.compile(r"^route to (\S.*)$")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", _CLEAN.sub(" ", (text or "").lower())).strip()


def destination(utterance: str,
                systems: Union[Iterable[str], Callable[[], Iterable[str]]] = ()) -> str:
    """The destination a set-route utterance names, or "" when it is not one.

    *systems* are star-system names (or a callable returning them, only called
    for a bare "route to X"); "route to <one of them>" is left to the Star Map
    (returns "")."""
    t = clean(utterance)
    if not t:
        return ""
    t = t[_PREFIX.match(t).end():]
    m = _SET.match(t)
    if m:
        return m.group(1).strip()
    m = _BARE.match(t)
    if m:
        dest = m.group(1).strip()
        names = systems() if callable(systems) else systems
        if dest in {clean(s) for s in names}:
            return ""
        return dest
    return ""
