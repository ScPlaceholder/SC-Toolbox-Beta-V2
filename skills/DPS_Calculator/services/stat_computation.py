"""Component stat computation functions — pure logic, no UI dependencies."""


# ── Fleetyards helpers (inlined to avoid services → ui dependency) ────────────

_FY_SIZE_MAP = {
    "small": 1, "s": 1, "one": 1, "1": 1,
    "medium": 2, "m": 2, "two": 2, "2": 2,
    "large": 3, "l": 3, "three": 3, "3": 3,
    "capital": 4, "xl": 4, "four": 4, "4": 4,
}


def _fy_size(raw) -> int:
    if isinstance(raw, int):
        return raw
    s = str(raw).lower().strip()
    return _FY_SIZE_MAP.get(s, 1)


def _fy_comp_name(hp: dict) -> str:
    comp = hp.get("component") or {}
    return comp.get("name") or hp.get("loadoutIdentifier") or "\u2014"


def _fy_comp_mfr(hp: dict) -> str:
    comp = hp.get("component") or {}
    mfr = comp.get("manufacturer") or {}
    return mfr.get("name") or mfr.get("code") or ""


def compute_shield_stats(raw: dict) -> dict:
    d  = raw.get("data", {})
    sh = d.get("shield", {})
    res = sh.get("resistance", {})
    ab  = sh.get("absorption", {})
    return {
        "name":             d.get("name", "?"),
        "local_name":       raw.get("localName", ""),
        "ref":              d.get("ref", ""),
        "size":             d.get("size", 1),
        "hp":               sh.get("maxShieldHealth", 0),
        "regen":            sh.get("maxShieldRegen", 0),
        "dmg_delay":        sh.get("damagedRegenDelay", 0),
        "down_delay":       sh.get("downedRegenDelay", 0),
        "res_phys_min":     res.get("physicalMin", 0),
        "res_phys_max":     res.get("physicalMax", 0),
        "res_energy_min":   res.get("energyMin", 0),
        "res_energy_max":   res.get("energyMax", 0),
        "res_dist_min":     res.get("distortionMin", 0),
        "res_dist_max":     res.get("distortionMax", 0),
        "abs_phys_min":     ab.get("physicalMin", 0),
        "abs_phys_max":     ab.get("physicalMax", 0),
        "abs_energy_min":   ab.get("energyMin", 0),
        "abs_energy_max":   ab.get("energyMax", 0),
        "abs_dist_min":     ab.get("distortionMin", 0),
        "abs_dist_max":     ab.get("distortionMax", 0),
        "class":            d.get("class", ""),
    }


def compute_cooler_stats(raw: dict) -> dict:
    d  = raw.get("data", {})
    co = d.get("cooler", {})
    return {
        "name":          d.get("name", "?"),
        "local_name":    raw.get("localName", ""),
        "ref":           d.get("ref", ""),
        "size":          d.get("size", 1),
        "cooling_rate":  co.get("coolingRate", 0),
        "suppression_heat": co.get("suppressionHeatFactor", 0),
        "suppression_ir":   co.get("suppressionIRFactor", 0),
    }


def compute_radar_stats(raw: dict) -> dict:
    d  = raw.get("data", {})
    rd = d.get("radar", {}) or {}
    return {
        "name":         d.get("name", "?"),
        "local_name":   raw.get("localName", ""),
        "ref":          d.get("ref", ""),
        "size":         d.get("size", 1),
        "detection_min": rd.get("detectionLifetimeMin", 0),
        "detection_max": rd.get("detectionLifetimeMax", 0),
        "cross_section": rd.get("crossSectionOcclusionFactor", 0),
        "scan_speed":    rd.get("azimuthScanSpeed", 0) or d.get("radar", {}).get("scanSpeed", 0) if rd else 0,
    }


def compute_missile_stats(raw: dict) -> dict:
    d  = raw.get("data", {})
    ms = d.get("missile", {}) or {}
    dmg = ms.get("damage", {}) or {}
    total_dmg = sum(v for v in dmg.values() if isinstance(v, (int, float)))
    return {
        "name":       d.get("name", "?"),
        "local_name": raw.get("localName", ""),
        "ref":        d.get("ref", ""),
        "size":       d.get("size", 1),
        "total_dmg":  total_dmg,
        "dmg_phys":   float(dmg.get("damagePhysical", 0) or 0),
        "dmg_energy": float(dmg.get("damageEnergy", 0) or 0),
        "dmg_dist":   float(dmg.get("damageDistortion", 0) or 0),
        "tracking":   ms.get("trackingSignalType", "?"),
        "lock_range": ms.get("lockRangeMax", 0),
        "lock_time":  ms.get("lockTime", 0),
        "speed":      ms.get("linearSpeed", 0),
        "lifetime":   ms.get("maxLifetime", 0),
        "lock_angle": ms.get("lockingAngle", 0),
    }


# ── erkul power-plant / quantum-drive stat helpers ────────────────────────────

def compute_powerplant_stats_erkul(raw: dict) -> dict:
    d    = raw.get("data", {})
    # Power output lives at resource.online.generation.powerSegment (erkul 4.x)
    res  = d.get("resource", {}) or {}
    onl  = res.get("online", {}) or {}
    gen  = onl.get("generation", {}) or {}
    sig  = onl.get("signatureParams", {}) or {}
    em_d = sig.get("em", {}) or {}
    ir_d = sig.get("ir", {}) or {}
    # health is a dict {"hp":N, ...} in erkul data
    hlth = d.get("health", {})
    hp_val = hlth.get("hp", 0) if isinstance(hlth, dict) else (hlth or 0)
    return {
        "name":          d.get("name", "?"),
        "local_name":    raw.get("localName", ""),
        "ref":           d.get("ref", ""),
        "size":          d.get("size", 1),
        "class":         d.get("class", ""),
        "grade":         d.get("grade", "?"),
        "output":        float(gen.get("powerSegment", 0) or 0),
        "power_draw":    0.0,   # PPs generate, not consume
        "power_max":     0.0,
        "overclocked":   0.0,
        "em_idle":       float(em_d.get("nominalSignature", 0) or 0),
        "em_max":        float(em_d.get("nominalSignature", 0) or 0),
        "ir_max":        float(ir_d.get("nominalSignature", 0) or 0),
        "hp":            float(hp_val or 0),
    }


def compute_qdrive_stats_erkul(raw: dict) -> dict:
    d  = raw.get("data", {})
    # erkul uses "qdrive" key (not "quantumDrive")
    qd = d.get("qdrive", d.get("quantumDrive", d.get("quantumdrive", {}))) or {}
    # Speed/spool are inside qdrive.params (erkul 4.x)
    params = qd.get("params", qd.get("standardJump", {})) or {}
    # Resource for EM/power
    res  = d.get("resource", {}) or {}
    onl  = res.get("online", {}) or {}
    sig  = onl.get("signatureParams", {}) or {}
    em_d = sig.get("em", {}) or {}
    # health is a dict {"hp":N, ...}
    hlth = d.get("health", {})
    hp_val = hlth.get("hp", 0) if isinstance(hlth, dict) else (hlth or 0)
    return {
        "name":       d.get("name", "?"),
        "local_name": raw.get("localName", ""),
        "ref":        d.get("ref", ""),
        "size":       d.get("size", 1),
        "class":      d.get("class", ""),
        "grade":      d.get("grade", "?"),
        "speed":      float(params.get("driveSpeed", qd.get("speed", 0)) or 0),
        "spool":      float(params.get("spoolUpTime", qd.get("spoolUpTime", 0)) or 0),
        "cooldown":   float(params.get("cooldownTime", qd.get("cooldown", 0)) or 0),
        "fuel_rate":  float(qd.get("quantumFuelRequirement", qd.get("fuelRate", 0)) or 0),
        "jump_range": float(qd.get("jumpRange", qd.get("maxRange", 0)) or 0),
        "efficiency": float(qd.get("quantumFuelRequirement", 0) or 0),
        "power_draw": 0.0,
        "power_max":  0.0,
        "em_idle":    float(em_d.get("nominalSignature", 0) or 0),
        "em_max":     float(em_d.get("nominalSignature", 0) or 0),
        "ir_max":     0.0,
        "hp":         float(hp_val or 0),
    }


# ── Fleetyards component helpers ──────────────────────────────────────────────

def compute_powerplant_stats(hp: dict) -> dict:
    """Extract power plant info from a Fleetyards hardpoint entry."""
    comp = hp.get("component") or {}
    td   = comp.get("typeData") or {}
    return {
        "name":       _fy_comp_name(hp),
        "size":       _fy_size(comp.get("size", hp.get("size", 1))),
        "grade":      comp.get("grade", "?"),
        "class":      comp.get("class", ""),
        "mfr":        _fy_comp_mfr(hp),
        "power_output": float(td.get("output", td.get("powerOutput", 0)) or 0),
    }


def compute_qdrive_stats(hp: dict) -> dict:
    """Extract quantum drive info from a Fleetyards hardpoint entry."""
    comp = hp.get("component") or {}
    td   = comp.get("typeData") or {}
    sj   = td.get("standardJump") or {}
    return {
        "name":        _fy_comp_name(hp),
        "size":        _fy_size(comp.get("size", hp.get("size", 1))),
        "grade":       comp.get("grade", "?"),
        "mfr":         _fy_comp_mfr(hp),
        "speed":       float(sj.get("speed", 0) or 0),          # m/s
        "spool":       float(sj.get("spoolUpTime", 0) or 0),    # s
        "cooldown":    float(sj.get("cooldown", 0) or 0),       # s
        "fuel_rate":   float(td.get("fuelRate", 0) or 0),
        "jump_range":  float(td.get("jumpRange", 0) or 0),
    }


def compute_thruster_stats(hp: dict) -> dict:
    """Extract thruster info from a Fleetyards hardpoint entry."""
    comp     = hp.get("component") or {}
    td       = comp.get("typeData") or {}
    category = hp.get("category") or hp.get("categoryLabel") or hp.get("type", "")
    return {
        "name":     _fy_comp_name(hp),
        "size":     _fy_size(comp.get("size", hp.get("size", 1))),
        "category": category,
        "mfr":      _fy_comp_mfr(hp),
        "thrust":   float(td.get("thrustCapacity", td.get("thrust", 0)) or 0),
    }
