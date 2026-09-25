"""Starmap worker: jump route between two star systems.

Galaxy.shortest_path is Dijkstra over the full ~90-system jump graph,
which includes systems not yet in the game. A second route restricted to
in-game systems is computed here from the same data, so the answer can
say which route you can actually fly today.
"""
from __future__ import annotations

import heapq
import math

import _assist_logic as L

from starmap.data import Galaxy

_g = {"galaxy": None}


def _galaxy() -> Galaxy:
    if _g["galaxy"] is None:
        _g["galaxy"] = Galaxy.load()
    return _g["galaxy"]


def _resolve(g: Galaxy, q: str):
    code = (q or "").strip().upper()
    if code in g.by_code:
        return g.by_code[code]
    s, _ = L.resolve_one(q, g.systems, key=lambda s: s.name, what="star system", min_score=0.6)
    return s


def _path_in_game(g: Galaxy, start: str, goal: str):
    ok = {c for c, s in g.by_code.items() if s.in_game}
    if start not in ok or goal not in ok:
        return None
    dist, prev, pq = {start: 0.0}, {}, [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == goal:
            break
        if d > dist.get(u, math.inf):
            continue
        su = g.by_code[u]
        for v in su.jump_points:
            if v not in ok:
                continue
            sv = g.by_code[v]
            nd = d + math.dist((su.x, su.y, su.z), (sv.x, sv.y, sv.z))
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    if start == goal:
        return [start]
    if goal not in prev:
        return None
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    return path[::-1]


def jump_route(from_system: str, to_system: str) -> dict:
    g = _galaxy()
    a, b = _resolve(g, from_system), _resolve(g, to_system)
    full = g.shortest_path(a.code, b.code)
    live = _path_in_game(g, a.code, b.code)
    name = lambda c: g.by_code[c].name
    res = {
        "from": a.name, "to": b.name,
        "route": [name(c) for c in full] if full else None,
        "jumps": len(full) - 1 if full else None,
        "route_systems_in_game": ([bool(g.by_code[c].in_game) for c in full] if full else None),
        "in_game_route": [name(c) for c in live] if live else None,
        "in_game_jumps": len(live) - 1 if live else None,
        "note": "route = shortest over the whole lore jump map; in_game_route uses only systems in the game today",
    }
    if not full:
        res["empty"] = True
        res["note"] = "no jump-point chain connects those systems in the map data"
    return res


EXPORTS = {"jump_route": jump_route}
