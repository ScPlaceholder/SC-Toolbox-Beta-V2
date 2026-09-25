"""Craft DB worker: what a blueprint costs to craft, from sc-craft.tools.

The Craft Database window is pinned to SC_CRAFT_VERSION in
shared/api_config.py (LIVE-4.7.0). That file is shared by every tool and
is not ours to edit, so the Assistant picks the version itself, here only:
the newest ``channel == live`` and ``active == 1`` entry of /api/versions.
If /versions fails, it falls back to the pinned version and says so.
"""
from __future__ import annotations

import time

import _assist_logic as L

from data.api_client import CraftApiClient
from shared.api_config import SC_CRAFT_VERSION

_state = {"client": None, "version": "", "source": "", "ts": 0.0}
_TTL = 6 * 3600.0


def pick_live_version(versions: list) -> str:
    live = [v for v in versions or []
            if str(v.get("channel", "")).lower() == "live" and int(v.get("active") or 0) == 1]
    if not live:
        live = [v for v in versions or [] if str(v.get("channel", "")).lower() == "live"]
    live.sort(key=lambda v: (str(v.get("created_at") or ""), int(v.get("id") or 0)), reverse=True)
    return str(live[0]["version"]) if live else ""


def _client() -> CraftApiClient:
    if _state["client"] is None or time.time() - _state["ts"] > _TTL:
        probe = CraftApiClient()
        r = probe._http.get_json("/versions")
        ver = pick_live_version(r.data if r.ok and isinstance(r.data, list) else [])
        if ver:
            _state.update(version=ver, source="sc-craft.tools /api/versions (active LIVE)")
        else:
            _state.update(version=SC_CRAFT_VERSION,
                          source=f"fallback to pinned {SC_CRAFT_VERSION}; /versions: "
                                 f"{r.error if not r.ok else 'no live entry'}")
        _state["client"] = CraftApiClient(version=_state["version"])
        _state["ts"] = time.time()
    return _state["client"]


def blueprint_recipe(name: str) -> dict:
    c = _client()
    r = c.fetch_blueprints(search=name, limit=25)
    items = (r.data or {}).get("items", []) if r.ok and isinstance(r.data, dict) else []
    if not r.ok:
        raise L.ToolFail(f"sc-craft.tools search failed: {r.error}")
    if not items:   # the site's search is substring; retry on the longest word
        words = sorted(L.norm(name).split(), key=len, reverse=True)
        if words and words[0] != L.norm(name):
            r = c.fetch_blueprints(search=words[0], limit=50)
            items = (r.data or {}).get("items", []) if r.ok and isinstance(r.data, dict) else []
    if not items:
        return {"query": name, "blueprints": [], "empty": True, "game_version": _state["version"],
                "note": f"no blueprint matching '{name}' in {_state['version']}"}
    best, alts = L.resolve_one(name, items, key=lambda i: i.get("name"), what="blueprint",
                               min_score=0.3)
    d = c.fetch_blueprint_detail(best["id"])
    if not d.ok:
        raise L.ToolFail(f"sc-craft.tools detail failed for {best.get('name')}: {d.error}")
    dd = d.data or {}
    ingredients = []
    for slot in dd.get("ingredients") or []:
        opts = [{"name": o.get("name"), "quantity": o.get("quantity_scu"),
                 "unit": o.get("unit") or "scu", "min_quality": o.get("min_quality")}
                for o in slot.get("options") or []]
        ingredients.append({"slot": slot.get("slot"), "options": opts})
    missions, seen = [], set()
    for m in dd.get("missions") or []:
        k = (m.get("name"), m.get("contractor"))
        if k in seen:
            continue
        seen.add(k)
        try:
            chance = round(float(m.get("drop_chance")), 3)
        except (TypeError, ValueError):
            chance = m.get("drop_chance")
        missions.append({"name": k[0], "contractor": k[1], "drop_chance": chance})
    mission_count = len(missions)
    missions = missions[:10]
    return {
        "blueprint": dd.get("name"),
        "category": dd.get("category"),
        "craft_time_seconds": dd.get("craft_time_seconds"),
        "ingredients": ingredients,
        "missions": missions,
        "distinct_missions": mission_count,
        "default_owned": bool(dd.get("default_owned")),
        "game_version": _state["version"],
        "version_source": _state["source"],
        "other_matches": alts,
    }


EXPORTS = {"blueprint_recipe": blueprint_recipe}
