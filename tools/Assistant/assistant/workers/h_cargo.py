"""Cargo Loader worker: container layout for a ship's cargo grid.

Ship grids come from the Cargo Loader's own cache (.cargo_cache.json),
read-only and regardless of its TTL: refreshing it means scraping the
sc-cargo JS bundle, which only the window does. The cache age is reported
so a stale answer is visibly stale.
"""
from __future__ import annotations

import json
import os
import time

import _assist_logic as L

from cargo_engine import build_slots, greedy_optimize_3d
from cargo_common import find_reference_loadout, load_reference_loadouts

_HERE = os.getcwd()
_state = {"ships": None, "ts": 0.0, "refs": None}


def _ships() -> list:
    if _state["ships"] is None:
        path = os.path.join(_HERE, ".cargo_cache.json")
        try:
            with open(path, encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError) as exc:
            raise L.ToolFail("Cargo Loader has no ship grid cache yet; open Cargo "
                             f"Loader once to download it ({exc})")
        _state["ships"] = obj.get("ships") or []
        _state["ts"] = float(obj.get("ts") or 0)
        _state["refs"] = load_reference_loadouts()
    return _state["ships"]


def _fmt(counts: dict) -> list:
    return [{"size_scu": int(k), "count": int(v)}
            for k, v in sorted(((int(k), v) for k, v in (counts or {}).items()), reverse=True)
            if v]


def cargo_layout(ship: str) -> dict:
    ships = _ships()
    s, alts = L.resolve_one(ship, ships, key=lambda x: x.get("name"),
                            what="ship with a cargo grid")
    slots, _ = build_slots(s)
    greedy = {k: v for k, v in greedy_optimize_3d(slots).items() if v}
    ref = find_reference_loadout(s["name"], _state["refs"] or {})
    total = lambda c: sum(int(k) * int(v) for k, v in (c or {}).items())
    res = {
        "ship": s.get("name"),
        "manufacturer": s.get("manufacturer"),
        "capacity_scu": s.get("capacity"),
        "reference_layout": _fmt(ref) if ref else None,
        "reference_scu": total(ref) if ref else None,
        "largest_first_layout": _fmt(greedy),
        "largest_first_scu": total(greedy),
        "grid_cache_age_days": round((time.time() - _state["ts"]) / 86400, 1) if _state["ts"] else None,
        "other_matches": alts,
        "note": ("reference_layout = the known working layout for this ship; "
                 "largest_first_layout = biggest boxes that physically fit each grid"),
    }
    if not greedy and not ref:
        res["empty"] = True
    return res


EXPORTS = {"cargo_layout": cargo_layout}
