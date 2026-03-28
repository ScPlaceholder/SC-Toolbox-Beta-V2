"""DPS calculation functions — pure logic, no UI dependencies."""
import math


def fire_rate_rps(weapon_data: dict) -> float:
    w    = weapon_data.get("weapon", {})
    fa   = w.get("fireActions", [])
    mode = w.get("mode", "")
    if isinstance(fa, list):
        if mode == "Looping":
            return (fa[0].get("fireRate", 0) or 0) / 60.0 if fa else 0.0
        delays = [a["delay"] for a in fa if a.get("delay")]
        delays = [d for d in delays if d > 0]
        if delays:
            cycle_time = sum(d / 60.0 for d in delays)
            return 1.0 / cycle_time if cycle_time > 0 else 0.0
        rates = [a["fireRate"] for a in fa if a.get("fireRate")]
        return sum(rates) / 60.0 if rates else 0.0
    elif isinstance(fa, dict):
        return (fa.get("fireRate") or 0) / 60.0
    return 0.0


def alpha_max(weapon_data: dict) -> float:
    ammo_d = weapon_data.get("ammo", {}).get("data", {})
    dmg    = ammo_d.get("damage", {})
    total  = sum(v for v in dmg.values() if isinstance(v, (int, float)))
    expl   = ammo_d.get("explosion", {}).get("damage", {})
    if expl:
        total += sum(v for v in expl.values() if isinstance(v, (int, float)))
    fa     = weapon_data.get("weapon", {}).get("fireActions", [])
    act    = fa[0] if isinstance(fa, list) and fa else (fa if isinstance(fa, dict) else {})
    base   = total * (act.get("pelletCount", 1) or 1) * (act.get("damageMultiplier", 1) or 1)
    charge_mult = act.get("maxChargeDamageMultiplier", 1) or 1
    return base * charge_mult


def dps_sustained(weapon_data: dict, alpha: float, rps: float) -> float:
    w = weapon_data.get("weapon", {})
    regen = w.get("regen", {})
    if regen and regen.get("maxAmmoLoad"):
        ammos      = float(regen.get("maxAmmoLoad", 0))
        max_regen  = float(regen.get("maxRegenPerSec", 0) or 1)
        cooldown   = float(regen.get("regenerationCooldown", 0))
        if rps > 0 and ammos > 0:
            fire_time   = ammos / rps
            charge_time = cooldown + ammos / max_regen
            return (ammos * alpha) / (charge_time + fire_time)
    heat = w.get("connection", {}).get("simplifiedHeat", {})
    if not heat:
        ac = w.get("ammoContainer", {}) if isinstance(w.get("ammoContainer"), dict) else {}
        max_ammo = ac.get("maxAmmoCount", 0) or 0
        if max_ammo > 0 and rps > 0:
            return alpha * rps
        return alpha * rps
    ot  = (heat.get("overheatTemperature", 100) or 100) - (heat.get("temperatureAfterOverheatFix", 0) or 0)
    ft  = heat.get("overheatFixTime", 0) or 0
    fa  = w.get("fireActions", [])
    if isinstance(fa, list) and fa:
        hps = sum(a.get("heatPerShot", 0) or 0 for a in fa) / len(fa)
    elif isinstance(fa, dict):
        hps = fa.get("heatPerShot", 0) or 0
    else:
        hps = 0
    if hps <= 0 or rps <= 0:
        return alpha * rps
    time_between_shots = 1.0 / rps
    ttcs = heat.get("timeTillCoolingStarts", 0) or 0
    cooling_ps = heat.get("coolingPerSecond", 0) or 0
    cooling_between_shots = 0.0
    if time_between_shots > ttcs:
        cooling_between_shots = (time_between_shots - ttcs) * cooling_ps
    effective_hps = hps - cooling_between_shots
    if effective_hps <= 0:
        return alpha * rps
    oh_time = ot / (effective_hps * rps)
    shots_before_oh = math.ceil(oh_time * rps)
    cycle = oh_time + ft
    return (shots_before_oh * alpha) / cycle if cycle > 0 else 0.0


def dmg_breakdown(weapon_data: dict) -> dict:
    ammo_d = weapon_data.get("ammo", {}).get("data", {})
    dmg  = ammo_d.get("damage", {})
    expl = ammo_d.get("explosion", {}).get("damage", {})
    result = {}
    for k in ("damagePhysical", "damageEnergy", "damageDistortion", "damageThermal"):
        result[k] = float(dmg.get(k, 0) or 0) + float(expl.get(k, 0) or 0)
    return result


def compute_weapon_stats(raw: dict) -> dict:
    d   = raw.get("data", {})
    rps = fire_rate_rps(d)
    alp = alpha_max(d)
    brk = dmg_breakdown(d)
    dom = max(brk, key=brk.get) if any(brk.values()) else "damagePhysical"
    return {
        "name":      d.get("name", "?"),
        "local_name": raw.get("localName", ""),
        "ref":       d.get("ref", ""),
        "size":      d.get("size", 1),
        "group":     d.get("group", ""),
        "alpha":     alp,
        "rps":       rps,
        "dps_raw":   alp * rps,
        "dps_sus":   dps_sustained(d, alp, rps),
        "ammo":      d.get("ammoContainer", {}).get("maxAmmoCount", 0),
        "dmg":       brk,
        "dom":       dom,
    }
