"""Loadout stat calculations — single source of truth.

Used by both the GUI and the validation/test suite.
"""
from typing import Dict, List, Optional

from models.items import (
    GadgetItem,
    LaserItem,
    ModuleItem,
    SHIPS,
)


def mult_stack(values: List[float]) -> float:
    """Multiplicative stacking: product(1 + v/100) - 1, result in %.

    Each modifier is treated as an independent multiplicative term.
    E.g. [+25, -10] -> (1.25 * 0.90 - 1) * 100 = +12.5%.
    """
    result = 1.0
    for v in values:
        if v != 0:
            result *= (1.0 + v / 100.0)
    return (result - 1.0) * 100.0


def calc_stats(
    ship: str,
    laser_items: List[Optional[LaserItem]],
    module_items: List[List[Optional[ModuleItem]]],
    gadget_item: Optional[GadgetItem],
) -> Dict[str, float]:
    """Calculate combined loadout stats using multiplicative stacking.

    Power: module deltas are additive within a turret, then applied to laser power.
    E.g. Focus III (95 = -5%) + Surge (150 = +50%) -> +45% (not +42.5%).
    Turret powers are then summed across all turrets.

    Percentage modifiers (resistance, instability, etc.): module stats are additive
    within a turret, then the per-turret sum becomes one multiplicative term.
    """
    min_pwr = 0.0
    max_pwr = 0.0
    ext_pwr = 0.0

    for i, laser in enumerate(laser_items):
        if not laser:
            continue
        mods = module_items[i] if i < len(module_items) else []
        # power_pct is x100 (e.g. 135 = +35%). None means "not applicable".
        pwr_delta = sum(
            (m.power_pct - 100) / 100.0
            for m in mods if m and m.power_pct is not None
        )
        ext_delta = sum(
            (m.ext_power_pct - 100) / 100.0
            for m in mods if m and m.ext_power_pct is not None
        )
        pwr_mult = 1.0 + pwr_delta
        ext_mult = 1.0 + ext_delta
        min_pwr += laser.min_power * pwr_mult
        max_pwr += laser.max_power * pwr_mult
        ext_pwr += (laser.ext_power if laser.ext_power is not None else 0.0) * ext_mult

    # Range from first equipped laser
    first_laser = next((las for las in laser_items if las), None)
    opt_rng = first_laser.opt_range if first_laser and first_laser.opt_range is not None else 0.0
    max_rng = first_laser.max_range if first_laser and first_laser.max_range is not None else 0.0

    # Collect percentage modifiers
    resistances: List[float] = []
    instabilities: List[float] = []
    inerts: List[float] = []
    chrg_windows: List[float] = []
    chrg_rates: List[float] = []
    overcharges: List[float] = []
    shatters: List[float] = []
    clusters: List[float] = []

    for laser in laser_items:
        if not laser:
            continue
        if laser.resistance is not None:
            resistances.append(laser.resistance)
        if laser.instability is not None:
            instabilities.append(laser.instability)
        if laser.inert is not None:
            inerts.append(laser.inert)
        if laser.charge_window is not None:
            chrg_windows.append(laser.charge_window)
        if laser.charge_rate is not None:
            chrg_rates.append(laser.charge_rate)

    for turret_mods in module_items:
        # Module % stats are additive within a turret, then the per-turret
        # sum becomes one multiplicative term (matches Regolith's formula).
        t_res = sum(m.resistance for m in turret_mods if m and m.resistance is not None)
        t_inst = sum(m.instability for m in turret_mods if m and m.instability is not None)
        t_inert = sum(m.inert for m in turret_mods if m and m.inert is not None)
        t_cw = sum(m.charge_window for m in turret_mods if m and m.charge_window is not None)
        t_cr = sum(m.charge_rate for m in turret_mods if m and m.charge_rate is not None)
        t_oc = sum(m.overcharge for m in turret_mods if m and m.overcharge is not None)
        t_shat = sum(m.shatter for m in turret_mods if m and m.shatter is not None)
        if t_res != 0:
            resistances.append(t_res)
        if t_inst != 0:
            instabilities.append(t_inst)
        if t_inert != 0:
            inerts.append(t_inert)
        if t_cw != 0:
            chrg_windows.append(t_cw)
        if t_cr != 0:
            chrg_rates.append(t_cr)
        if t_oc != 0:
            overcharges.append(t_oc)
        if t_shat != 0:
            shatters.append(t_shat)

    if gadget_item:
        if gadget_item.resistance is not None:
            resistances.append(gadget_item.resistance)
        if gadget_item.instability is not None:
            instabilities.append(gadget_item.instability)
        if gadget_item.charge_window is not None:
            chrg_windows.append(gadget_item.charge_window)
        if gadget_item.charge_rate is not None:
            chrg_rates.append(gadget_item.charge_rate)
        if gadget_item.cluster is not None:
            clusters.append(gadget_item.cluster)

    return {
        "min_power": min_pwr,
        "max_power": max_pwr,
        "ext_power": ext_pwr,
        "opt_range": opt_rng,
        "max_range": max_rng,
        "resistance": mult_stack(resistances),
        "instability": mult_stack(instabilities),
        "inert": mult_stack(inerts),
        "charge_window": mult_stack(chrg_windows),
        "charge_rate": mult_stack(chrg_rates),
        "overcharge": mult_stack(overcharges),
        "shatter": mult_stack(shatters),
        "cluster": mult_stack(clusters),
    }


def calc_loadout_price(
    ship: str,
    laser_items: List[Optional[LaserItem]],
    module_items: List[List[Optional[ModuleItem]]],
    gadget_item: Optional[GadgetItem],
) -> float:
    """Calculate total loadout price, excluding the ship's stock laser."""
    ship_cfg = SHIPS.get(ship)
    stock_name = ship_cfg.stock_laser if ship_cfg else ""
    total = 0.0
    for laser in laser_items:
        if laser and laser.name != stock_name:
            total += laser.price
    for turret_mods in module_items:
        for mod in turret_mods:
            if mod:
                total += mod.price
    if gadget_item:
        total += gadget_item.price
    return total
