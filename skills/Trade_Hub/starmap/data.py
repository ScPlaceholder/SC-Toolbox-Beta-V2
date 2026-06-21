"""Star map data model + crash-safe persistence.

Loads the bundled systems snapshot (``data/systems.json``) and persists view
state atomically under ``~/.sctoolbox/trade_hub/`` so a crash, update, or
move between the embedded tab and the pop-out never loses the user's work.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# UEX location names that differ from the scunpacked body names by more than
# formatting (real renames). Keys + values are norm_loc()-normalised.
LOC_ALIASES = {
    "greenimperialhousingexchange": "grimhex",   # GrimHEX (UEX in-fiction name)
}


def norm_loc(s: str) -> str:
    """Normalise a location name for matching: lowercase, drop parenthetical
    qualifiers like '(Stanton)', keep only alphanumerics — so 'Pyro Gateway
    (Stanton)' -> 'pyrogateway' and 'Area 18' -> 'area18'."""
    s = re.sub(r"\(.*?\)", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


_HERE = os.path.dirname(os.path.abspath(__file__))
_SYSTEMS_PATH = os.path.join(_HERE, "data", "systems.json")

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "trade_hub")
_STATE_PATH = os.path.join(_STATE_DIR, "starmap_state.json")

# The five systems the user may choose as "home" (codes as in systems.json).
HOME_CHOICES: Tuple[str, ...] = ("STANTON", "PYRO", "NYX", "TERRA", "SOL")


@dataclass(frozen=True)
class System:
    code: str
    name: str
    x: float
    y: float
    z: float
    affiliation: str
    in_game: bool
    jump_points: Tuple[str, ...]


@dataclass
class Galaxy:
    """The full ~90-system snapshot, indexed by code."""

    systems: List[System]
    by_code: Dict[str, System] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = _SYSTEMS_PATH) -> "Galaxy":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        systems: List[System] = []
        for s in raw.get("systems", []):
            p = s.get("position", {}) or {}
            systems.append(System(
                code=s["code"],
                name=s.get("name", s["code"]),
                x=float(p.get("x", 0.0)),
                y=float(p.get("y", 0.0)),
                z=float(p.get("z", 0.0)),
                affiliation=s.get("affiliation", "Unclaimed"),
                in_game=bool(s.get("in_game", False)),
                jump_points=tuple(s.get("jump_points", []) or ()),
            ))
        g = cls(systems=systems)
        g.by_code = {s.code: s for s in systems}
        return g

    def get(self, code: Optional[str]) -> Optional[System]:
        if code is None:
            return None
        return self.by_code.get(code)

    def jump_edges(self) -> List[Tuple[System, System]]:
        """Unique undirected jump-point edges (each connected pair once)."""
        edges: List[Tuple[System, System]] = []
        seen = set()
        for s in self.systems:
            for tgt in s.jump_points:
                t = self.by_code.get(tgt)
                if t is None:
                    continue
                key = tuple(sorted((s.code, t.code)))
                if key in seen:
                    continue
                seen.add(key)
                edges.append((s, t))
        return edges

    def shortest_path(self, start: str, goal: str) -> Optional[List[str]]:
        """Dijkstra over the jump-point graph, weighted by inter-system
        distance. Returns the list of system codes start..goal, or None."""
        if start not in self.by_code or goal not in self.by_code:
            return None
        if start == goal:
            return [start]
        dist: Dict[str, float] = {start: 0.0}
        prev: Dict[str, str] = {}
        pq: List[Tuple[float, str]] = [(0.0, start)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == goal:
                break
            if d > dist.get(u, float("inf")):
                continue
            su = self.by_code[u]
            for v in su.jump_points:
                sv = self.by_code.get(v)
                if sv is None:
                    continue
                nd = d + math.dist((su.x, su.y, su.z), (sv.x, sv.y, sv.z))
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if goal not in prev:
            return None
        path = [goal]
        while path[-1] != start:
            path.append(prev[path[-1]])
        path.reverse()
        return path

    def path_distance(self, path: List[str]) -> float:
        total = 0.0
        for a, b in zip(path, path[1:]):
            sa, sb = self.by_code.get(a), self.by_code.get(b)
            if sa and sb:
                total += math.dist((sa.x, sa.y, sa.z), (sb.x, sb.y, sb.z))
        return total


# ── persistence ───────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Return the saved star-map state, or ``{}`` on first run / any error."""
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    """Atomic write (temp + ``os.replace``) so an interrupted write can never
    corrupt or lose the existing state file."""
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, _STATE_PATH)
    except OSError:
        pass


# ── celestial bodies (system-detail view) ──────────────────────────────────────

_BODIES_PATH = os.path.join(_HERE, "data", "bodies.json")


@dataclass(frozen=True)
class Body:
    name: str
    type: str            # Star / Planet / Moon / Manmade / Outpost / Asteroid / ...
    x: float
    y: float
    z: float
    parent: Optional[str]

    @property
    def kind(self) -> str:
        """Normalised category (the source has many type variants)."""
        t = self.type.lower()
        if "star" in t:
            return "star"
        if "planet" in t:
            return "planet"
        if "moon" in t:
            return "moon"
        if "asteroid" in t:
            return "asteroid"
        if "jump" in t:
            return "jumppoint"
        if "manmade" in t or "station" in t:
            return "station"
        if "outpost" in t:
            return "outpost"
        if "landingzone" in t or "landing" in t:
            return "landing"
        return "misc"


def load_bodies() -> Dict[str, List["Body"]]:
    """Return ``{SYSTEM_CODE: [Body, ...]}`` from bodies.json, or ``{}`` if the
    snapshot is absent (the system-detail view degrades gracefully)."""
    try:
        with open(_BODIES_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    out: Dict[str, List[Body]] = {}
    for code, sysobj in (raw.get("systems") or {}).items():
        bodies: List[Body] = []
        for b in (sysobj.get("bodies") or []):
            bodies.append(Body(
                name=b.get("name", "?"),
                type=b.get("type", "?"),
                x=float(b.get("x", 0.0)),
                y=float(b.get("y", 0.0)),
                z=float(b.get("z", 0.0)),
                parent=b.get("parent"),
            ))
        out[code.upper()] = bodies
    return out
