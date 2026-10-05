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


#: "calibrate star map" (J, 2026-10-04): starts the in-game route setter's
#: calibration. The whole utterance, not its start: "how do I calibrate the
#: star map" is a question for the assistant, not the command.
#: Spellings: Whisper writes the map as "star map", "starmap" or "Star Map"
#: (clean() lowers it and drops the punctuation), and "star" is commonly heard
#: as "start". "Route" is the name on the button (Calibrate Route).
_CALIBRATE = re.compile(
    r"^(?:(?:start|begin|run|do) (?:the |a )?)?"
    r"(?:re ?)?calibrat(?:e|ion)(?: of)?"
    r"(?: (?:the |my )?(?:in game )?"
    r"(?:star ?t? ?maps?(?: route)?|route(?: setter)?|map))?"
    r"(?: please| now)?$")


def is_calibrate(utterance: str) -> bool:
    """True for "calibrate star map" and the ways it is said and heard:

        "calibrate star map"    "calibrate starmap"     "calibrate the star map"
        "calibrate route"       "calibrate the route"   "recalibrate the star map"
        "calibrate start map"   "calibrate"             "start calibration"
        "star map, calibrate"   "please calibrate the star map"
    """
    t = clean(utterance)
    if not t:
        return False
    return bool(_CALIBRATE.match(t) or _CALIBRATE.match(t[_PREFIX.match(t).end():]))


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
