# -*- coding: utf-8 -*-
"""crafting.py — weapon-crafting DPS modifier for the live DPS calculator.

Weapons carry blueprint DAMAGE slots ("Impact Force") on a quality scale 0-1000.
Each damage slot ramps its per-shot-damage modifier linearly from mod_start (q0,
worst craft) to mod_end (q1000, best craft); q500 == store-bought (modifier ~1.0).
A weapon's damage slots STACK (multiply). Real data (from the erkul cache) shows
every craftable weapon has TWO damage slots — most 0.95->1.05 (best ~+10%), a few
0.8->1.2 (best ~+44%) — so the per-weapon table matters; it is baked into
``crafting_slots.json`` beside this package's parent dir.

Public API (what the UI + aggregator wire to):
    load_table()                        -> {className: {"name","damage_slots":[...]}}
    is_craftable(className)             -> bool
    weapon_damage_mult(className, q)    -> float   (q = scalar 0-1000, or {slot: q})
    craft_options(classnames)           -> UI manifest for the weapons in a loadout

Safe by default: an unset / store-bought quality (500) yields modifier 1.0, so with
no slider moved the DPS is byte-identical to before — erkul parity is untouched.
"""
from __future__ import annotations

import json
import os
from typing import Optional

STORE_Q = 500  # quality at which a damage slot's modifier is ~1.0 (store-bought)

# crafting_slots.json lives in the DPS_Calculator root (parent of services/)
_DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crafting_slots.json")

_TABLE: Optional[dict] = None


def load_table() -> dict:
    """Lazy-load the baked per-weapon damage-slot table (empty dict if missing)."""
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _TABLE = (json.load(f) or {}).get("weapons", {}) or {}
    except Exception:
        _TABLE = {}
    return _TABLE


def is_craftable(className: Optional[str]) -> bool:
    return bool(className) and className in load_table()


def _slot_modifier(slot: dict, quality: float) -> float:
    """Linear-ramp modifier at a given quality, clamped to the slot's range."""
    q0, q1 = slot.get("q_start", 0), slot.get("q_end", 1000)
    q = max(q0, min(q1, quality))
    frac = (q - q0) / (q1 - q0) if q1 > q0 else 0.0
    return slot.get("mod_start", 1.0) + (slot.get("mod_end", 1.0) - slot.get("mod_start", 1.0)) * frac


def weapon_damage_mult(className: Optional[str], quality) -> float:
    """Product of a weapon's damage-slot modifiers at `quality`.

    `quality` is a scalar 0-1000 (one master value for all the weapon's slots) or a
    dict {slot_name: quality}. Unknown / non-craftable weapon -> 1.0 (no-op).
    """
    entry = load_table().get(className)
    if not entry:
        return 1.0
    m = 1.0
    for sl in entry.get("damage_slots", []):
        q = quality.get(sl.get("slot"), STORE_Q) if isinstance(quality, dict) else quality
        m *= _slot_modifier(sl, q)
    return m


def craft_options(classnames) -> dict:
    """UI manifest: one entry per unique craftable weapon in the given loadout.

    classnames: iterable of weapon classNames currently equipped. Returns the
    weapons that are craftable + their damage slots, so the UI can draw sliders.
    """
    table = load_table()
    seen: dict[str, dict] = {}
    for cn in classnames:
        if cn in table and cn not in seen:
            entry = table[cn]
            seen[cn] = {
                "weapon": cn,
                "name": entry.get("name", cn),
                "slots": [
                    {
                        "slot": s.get("slot"),
                        "modifier_range": [s.get("mod_start", 1.0), s.get("mod_end", 1.0)],
                        "quality_range": [s.get("q_start", 0), s.get("q_end", 1000)],
                    }
                    for s in entry.get("damage_slots", [])
                ],
            }
    return {
        "weapon_groups": list(seen.values()),
        "quality_scale": [0, 1000],
        "store_bought_equivalent_quality": STORE_Q,
    }
