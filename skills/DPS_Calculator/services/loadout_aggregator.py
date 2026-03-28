"""Loadout aggregation — compute footer totals and signatures from selections."""


def compute_footer_totals(selections: dict,
                          find_weapon, find_missile, find_shield,
                          find_cooler, find_radar, find_powerplant,
                          power_sim: bool = False,
                          weapon_power_ratio: float = 1.0,
                          shield_power_ratio: float = 1.0) -> dict:
    """Compute all aggregate stats from current component selections.

    Parameters
    ----------
    selections : dict
        Keys: "weapons", "missiles", "defenses", "components", "propulsion"
        Values: {slot_id: component_name}
    find_* : callable
        Lookup functions that take a name and return stats dict or None
    power_sim : bool
        Whether power simulation is active
    weapon_power_ratio, shield_power_ratio : float
        Power allocation fractions (0-1)

    Returns
    -------
    dict with keys:
        dps_raw, dps_sus, alpha, missile_dmg,
        shield_hp, shield_regen, shield_res (dict), shield_count,
        cooling, power_output, power_draw,
        gun_count, missile_count
    """
    # Weapons
    tot_raw = tot_sus = tot_alp = 0.0
    gun_count = 0
    for sid, nm in selections.get("weapons", {}).items():
        if not nm:
            continue
        s = find_weapon(nm)
        if s:
            tot_raw += s["dps_raw"]
            tot_sus += s["dps_sus"]
            tot_alp += s["alpha"]
            gun_count += 1

    # Missiles
    miss_dmg = 0.0
    miss_count = 0
    for sid, nm in selections.get("missiles", {}).items():
        if not nm:
            continue
        s = find_missile(nm)
        if s:
            miss_dmg += s["total_dmg"]
            miss_count += 1

    # Shields
    tot_hp = tot_regen = 0.0
    shld_res = {"phys": 0.0, "enrg": 0.0, "dist": 0.0}
    shld_count = 0
    for sid, nm in selections.get("defenses", {}).items():
        if not nm:
            continue
        s = find_shield(nm)
        if s:
            tot_hp += s["hp"]
            tot_regen += s["regen"]
            shld_res["phys"] += s["res_phys_max"]
            shld_res["enrg"] += s["res_energy_max"]
            shld_res["dist"] += s["res_dist_max"]
            shld_count += 1

    # Cooling
    tot_cool = 0.0
    for sid, nm in selections.get("components", {}).items():
        if not nm:
            continue
        s = find_cooler(nm)
        if s:
            tot_cool += s["cooling_rate"]

    # Power budget
    tot_pwr_out = 0.0
    tot_pwr_draw = 0.0
    for sid, nm in selections.get("components", {}).items():
        if not nm:
            continue
        if sid.startswith("pp_"):
            s = find_powerplant(nm)
            if s:
                tot_pwr_out += float(s.get("output", 0) or 0)
        else:
            s = find_cooler(nm) or find_radar(nm)
            if s:
                tot_pwr_draw += float(s.get("power_draw", 0) or 0)

    for sid, nm in selections.get("defenses", {}).items():
        if not nm:
            continue
        s = find_shield(nm)
        if s:
            tot_pwr_draw += float(s.get("power_draw", 0) or 0)

    for sid, nm in selections.get("weapons", {}).items():
        if not nm:
            continue
        s = find_weapon(nm)
        if s:
            tot_pwr_draw += float(s.get("power_draw", 0) or 0)

    # Apply power ratio when POWER SIM active
    if power_sim:
        tot_raw *= weapon_power_ratio
        tot_sus *= weapon_power_ratio
        tot_regen *= shield_power_ratio

    return {
        "dps_raw": tot_raw,
        "dps_sus": tot_sus,
        "alpha": tot_alp,
        "missile_dmg": miss_dmg,
        "shield_hp": tot_hp,
        "shield_regen": tot_regen,
        "shield_res": shld_res,
        "shield_count": shld_count,
        "cooling": tot_cool,
        "power_output": tot_pwr_out,
        "power_draw": tot_pwr_draw,
        "gun_count": gun_count,
        "missile_count": miss_count,
    }


def compute_raw_signatures(selections: dict,
                           find_weapon, find_missile, find_shield,
                           find_cooler, find_radar, find_powerplant,
                           find_qdrive) -> tuple:
    """Compute EM and IR signatures in RAW mode (no power sim).

    Returns (em_sig, ir_sig).
    """
    em_sig = 0.0
    ir_sig = 0.0

    find_fns = [
        ("weapons", find_weapon),
        ("missiles", find_missile),
        ("defenses", find_shield),
        ("components", find_cooler),
    ]
    for sel_key, find_fn in find_fns:
        for sid, nm in selections.get(sel_key, {}).items():
            if not nm:
                continue
            s = find_fn(nm)
            if s:
                em_sig += float(s.get("em_max", 0) or 0)
                ir_sig += float(s.get("ir_max", 0) or 0)

    # Power plants
    for sid, nm in selections.get("components", {}).items():
        if not nm or not sid.startswith("pp_"):
            continue
        s = find_powerplant(nm)
        if s:
            em_sig += float(s.get("em_max", s.get("em_idle", 0)) or 0)
            ir_sig += float(s.get("ir_max", 0) or 0)

    # Quantum drives
    for sid, nm in selections.get("propulsion", {}).items():
        if not nm:
            continue
        s = find_qdrive(nm)
        if s:
            em_sig += float(s.get("em_max", s.get("em_idle", 0)) or 0)

    # Radars (in components section)
    for sid, nm in selections.get("components", {}).items():
        if not nm:
            continue
        s = find_radar(nm)
        if s:
            em_sig += float(s.get("em_max", 0) or 0)

    return em_sig, ir_sig
