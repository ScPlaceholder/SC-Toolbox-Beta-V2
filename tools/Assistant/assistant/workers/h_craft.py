"""Craft DB worker: what a blueprint costs to craft, from the datamine.

Same data as the Craft Database window: ``blueprints.json`` of the pinned
scunpacked-data commit (shared/scunpacked.py), indexed by the window's
``data/datamine.py``. The first call downloads it (about 4 MB, pinned
sha256) if the window never has; after that it answers offline.

The datamine names the reward pools a blueprint comes from, not missions
with contractors and drop chances; ``missions_for_blueprint`` (Mission DB)
answers that.
"""
from __future__ import annotations

import _assist_logic as L

from data.datamine import DatamineMissing, DatamineSource, version_string

_state = {"index": None}


def _index() -> dict:
    if _state["index"] is None:
        src = DatamineSource()
        try:
            idx = src.load()
        except DatamineMissing:
            try:
                idx = src.download()
            except Exception as exc:                    # noqa: BLE001
                raise L.ToolFail(f"crafting data could not be downloaded: {exc}")
        _state["index"] = idx
    return _state["index"]


def blueprint_recipe(name: str) -> dict:
    idx = _index()
    version = idx.get("stats", {}).get("version") or version_string(idx.get("build", ""))
    items = idx.get("blueprints") or []
    try:
        best, alts = L.resolve_one(name, items, key=lambda i: i.get("name"), what="blueprint",
                                   min_score=0.3)
    except L.ToolFail:
        return {"query": name, "blueprints": [], "empty": True, "game_version": version,
                "note": f"no blueprint matching '{name}' in {version}"}
    ingredients = []
    for slot in best.get("ingredients") or []:
        opts = [{"name": o.get("name"), "quantity": o.get("quantity"),
                 "unit": o.get("unit") or "cSCU", "min_quality": o.get("min_quality")}
                for o in slot.get("options") or []]
        ing = {"slot": slot.get("slot"), "options": opts}
        if slot.get("choose"):
            ing["choose"] = slot["choose"]
        ingredients.append(ing)
    return {
        "blueprint": best.get("name"),
        "category": best.get("category"),
        "craft_time_seconds": best.get("craft_time_seconds"),
        "ingredients": ingredients,
        "missions": [],
        "reward_pools": list(best.get("sources") or []),
        "obtainable": bool(best.get("obtainable")),
        "default_owned": bool(best.get("default_owned")),
        "game_version": version,
        "version_source": f"datamine {idx.get('source')} @ {str(idx.get('commit'))[:10]}",
        "other_matches": alts,
    }


EXPORTS = {"blueprint_recipe": blueprint_recipe}
