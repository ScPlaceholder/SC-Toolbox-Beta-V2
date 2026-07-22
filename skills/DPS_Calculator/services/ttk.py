# -*- coding: utf-8 -*-
"""ttk.py — time-to-kill: how long your loadout takes to kill a target ship.

Erkul gives you a DPS number and stops. This turns it into the question players
actually ask: how long is the fight, and can you even win it? Built on the same
live engine that already carries your erkul parity — the attacker's sustained DPS,
and the target ship's real shield HP + hull HP.

Model (v0):
  effective HP = target's installed shield HP + hull HP
  TTK seconds  = effective HP / attacker sustained DPS
  Plus the insight erkul never gives: if sustained DPS <= the target's shield
  regen, you CANNOT break the shield under sustained fire — the kill is
  impossible with that loadout (short of alpha bursts).
"""
from __future__ import annotations

from typing import Optional


def ship_hull_hp(ship: dict) -> float:
    """Hull HP the way erkul shows it: armor.data.health.hp, falling back to hull.totalHp."""
    ship = ship.get("data", ship) if isinstance(ship, dict) else {}
    armor = ship.get("armor", {})
    ad = armor.get("data", armor) if isinstance(armor, dict) else {}
    armor_hp = (ad.get("health", {}) or {}).get("hp", 0) or 0
    hull = ship.get("hull", {})
    hull_total = hull.get("totalHp", 0) if isinstance(hull, dict) else 0
    return float(armor_hp or hull_total or 0)


def target_defense(ship: dict, find_shield, extract_slots_by_type) -> dict:
    """Compute a target ship's shield HP/regen (from its default shields) + hull HP.

    find_shield: fn(ref) -> shield stats dict with 'hp'/'regen' (the live repository's).
    extract_slots_by_type: the loadout slot extractor (injected to avoid a hard import).
    """
    d = ship.get("data", ship) if isinstance(ship, dict) else {}
    hull = ship_hull_hp(ship)
    shp = 0.0
    sregen = 0.0
    try:
        for s in extract_slots_by_type(d.get("loadout", []) or [], {"Shield"}):
            ref = s.get("local_ref", "")
            if not ref:
                continue
            st = find_shield(ref)
            if st:
                shp += float(st.get("hp", 0) or 0)
                sregen += float(st.get("regen", 0) or 0)
    except Exception:
        pass
    return {
        "hull_hp": hull,
        "shield_hp": shp,
        "shield_regen": sregen,
        "effective_hp": hull + shp,
    }


def time_to_kill(attacker_dps_sus: float, defense: dict) -> dict:
    """TTK seconds + winnability verdict for a given sustained DPS vs a target's defense."""
    eff = float(defense.get("effective_hp", 0) or 0)
    regen = float(defense.get("shield_regen", 0) or 0)
    shp = float(defense.get("shield_hp", 0) or 0)
    dps = float(attacker_dps_sus or 0)
    if dps <= 0:
        return {"ttk": None, "possible": False, "reason": "no sustained DPS"}
    # Can't break a shield you can't out-damage its regen (sustained).
    if shp > 0 and dps <= regen:
        return {"ttk": None, "possible": False,
                "reason": f"sustained DPS ({dps:,.0f}) <= shield regen ({regen:,.0f}) - cannot break shield"}
    return {"ttk": eff / dps, "possible": True, "reason": ""}


def format_ttk(result: dict) -> str:
    if not result.get("possible"):
        return f"Unwinnable — {result.get('reason', '')}"
    t = result["ttk"]
    if t < 60:
        return f"{t:.1f} s"
    return f"{int(t // 60)}m {int(t % 60)}s"
