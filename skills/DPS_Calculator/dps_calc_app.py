# Rewritten to match erkul.games/live/calculator UI
"""
DPS / Ship-Loadout Calculator — standalone Tkinter GUI for Star Citizen.
Data from erkul.games API (server.erkul.games) + fleetyards.net API.
Launched as a subprocess by the WingmanAI DPS_Calculator skill (main.py).

Tabs:
  ✕ Weapons    — gun/turret slots, per-weapon DPS (raw + sustained)
  ✎ Missiles   — missile launcher racks with missile selection
  ⊙ Defenses   — shield generators + armor stats
  ⚙ Systems    — power plants, coolers, radars (erkul + fleetyards)
  ↑ Propulsion — quantum drives, thrusters, fuel tanks (fleetyards)
  ≡ Overview   — two-column ship summary panel

Usage:
    python dps_calc_app.py <x> <y> <w> <h> <opacity> <cmd_file>
"""

import json
import logging
import math
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional
import webbrowser

import requests

# ── Shared modules (two dirs up from skills/DPS_Calculator/) ─────────────────
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from shared.ipc import ipc_read_incremental  # noqa: E402

_log = logging.getLogger(__name__)

# ── API ───────────────────────────────────────────────────────────────────────
API_BASE    = "https://server.erkul.games"
API_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://www.erkul.games",
    "Referer":         "https://www.erkul.games/",
}
CACHE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".erkul_cache.json")
CACHE_TTL     = 2 * 3600   # BUG 4: reduced from 6h → 2h for fresher data on patch days
CACHE_VERSION = 5           # bumped: three-panel UI + /live/thrusters + /live/paints

# ── Fleetyards API (ship hardpoints — power plants, QD, thrusters, fuel) ──────
FY_BASE    = "https://api.fleetyards.net/v1"
FY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept":     "application/json",
}
FY_HP_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".fy_hardpoints_cache.json"
)
FY_HP_TTL = 6 * 3600   # hardpoints rarely change mid-session

# ── Palette (erkul.games dark theme) ──────────────────────────────────────────
BG           = "#0d1117"
BG2          = "#111827"
BG3          = "#161b25"
BG4          = "#1c2233"
BORDER       = "#252e42"
FG           = "#c8d4e8"
FG_DIM       = "#5a6480"
FG_DIMMER    = "#3a4460"
ACCENT       = "#44aaff"
GREEN        = "#33dd88"
YELLOW       = "#ffaa22"
RED          = "#ff5533"
ORANGE       = "#ff7733"
CYAN         = "#33ccdd"
PURPLE       = "#aa66ff"
PHYS_COL     = "#99aabb"
ENERGY_COL   = "#44ccff"
DIST_COL     = "#bb88ff"
THERM_COL    = "#ff7733"
HEADER_BG    = "#0e1420"
SECT_HDR_BG  = "#131928"
CARD_EVEN    = "#161b25"
CARD_ODD     = "#1c2233"
CARD_BORDER  = "#252e42"
ROW_EVEN     = CARD_EVEN
ROW_ODD      = CARD_ODD
SIZE_COLORS  = {1: "#2a5580", 2: "#2a6677", 3: "#256655", 4: "#446622"}
TRACK_COLORS = {"IR": THERM_COL, "EM": ENERGY_COL, "CrossSection": PHYS_COL}
TYPE_STRIPE  = {
    "WeaponGun":      ENERGY_COL,
    "MissileLauncher": RED,
    "Shield":         DIST_COL,
    "Cooler":         CYAN,
    "Radar":          FG_DIM,
    "PowerPlant":     ORANGE,
    "QuantumDrive":   ACCENT,
    "Thruster":       YELLOW,
}

# ── DPS helpers ───────────────────────────────────────────────────────────────

def _fire_rate_rps(weapon_data: dict) -> float:
    w    = weapon_data.get("weapon", {})
    fa   = w.get("fireActions", [])
    mode = w.get("mode", "")
    if isinstance(fa, list):
        # LOOPING mode (Laser Repeaters, regen/capacitor weapons): erkul uses fireRate/60
        if mode == "Looping":
            return (fa[0].get("fireRate", 0) or 0) / 60.0 if fa else 0.0
        delays = [a["delay"] for a in fa if a.get("delay")]
        delays = [d for d in delays if d > 0]
        if delays:
            # Each delay is one step in a sequential fire cycle.
            # Total cycle time = sum of individual delays (converted to seconds).
            # One round is produced per full cycle.
            cycle_time = sum(d / 60.0 for d in delays)
            return 1.0 / cycle_time if cycle_time > 0 else 0.0
        rates = [a["fireRate"] for a in fa if a.get("fireRate")]
        return sum(rates) / 60.0 if rates else 0.0
    elif isinstance(fa, dict):
        return (fa.get("fireRate") or 0) / 60.0
    return 0.0


def _alpha_max(weapon_data: dict) -> float:
    ammo_d = weapon_data.get("ammo", {}).get("data", {})
    dmg    = ammo_d.get("damage", {})
    total  = sum(v for v in dmg.values() if isinstance(v, (int, float)))
    # Erkul also adds explosion.damage (distortion/rocket weapons)
    expl   = ammo_d.get("explosion", {}).get("damage", {})
    if expl:
        total += sum(v for v in expl.values() if isinstance(v, (int, float)))
    fa     = weapon_data.get("weapon", {}).get("fireActions", [])
    act    = fa[0] if isinstance(fa, list) and fa else (fa if isinstance(fa, dict) else {})
    base   = total * (act.get("pelletCount", 1) or 1) * (act.get("damageMultiplier", 1) or 1)
    # Charge weapons: maxChargeDamageMultiplier (e.g. Destroyer Mass Driver = 2×)
    charge_mult = act.get("maxChargeDamageMultiplier", 1) or 1
    return base * charge_mult


def _dps_sustained(weapon_data: dict, alpha: float, rps: float) -> float:
    w = weapon_data.get("weapon", {})

    # Branch 1: LOOPING / regen (capacitor) weapons — erkul formula:
    # ammos = maxAmmoLoad (at full power, no ship buffs)
    # chargeTime = regenerationCooldown + ammos / maxRegenPerSec
    # continuousFireTime = ammos / fireRate
    # sustained = (ammos * alpha) / (chargeTime + continuousFireTime)
    regen = w.get("regen", {})
    if regen and regen.get("maxAmmoLoad"):
        ammos      = float(regen.get("maxAmmoLoad", 0))
        max_regen  = float(regen.get("maxRegenPerSec", 0) or 1)
        cooldown   = float(regen.get("regenerationCooldown", 0))
        if rps > 0 and ammos > 0:
            fire_time   = ammos / rps
            charge_time = cooldown + ammos / max_regen
            return (ammos * alpha) / (charge_time + fire_time)

    # Branch 2: heat-based weapons (simplifiedHeat present)
    # Erkul formula: accounts for cooling-between-shots and temperatureAfterOverheatFix
    heat = w.get("connection", {}).get("simplifiedHeat", {})
    if not heat:
        # No heat model — check if ammo-limited (pure ballistic, no overheat)
        ac = w.get("ammoContainer", {}) if isinstance(w.get("ammoContainer"), dict) else {}
        max_ammo = ac.get("maxAmmoCount", 0) or 0
        if max_ammo > 0 and rps > 0:
            # ammo-limited sustained = raw DPS (fire until empty, erkul shows raw)
            return alpha * rps
        return alpha * rps
    ot  = (heat.get("overheatTemperature", 100) or 100) - (heat.get("temperatureAfterOverheatFix", 0) or 0)
    ft  = heat.get("overheatFixTime", 0) or 0
    fa  = w.get("fireActions", [])
    # Average heatPerShot across fireActions (erkul does this for burst weapons)
    if isinstance(fa, list) and fa:
        hps = sum(a.get("heatPerShot", 0) or 0 for a in fa) / len(fa)
    elif isinstance(fa, dict):
        hps = fa.get("heatPerShot", 0) or 0
    else:
        hps = 0
    if hps <= 0 or rps <= 0:
        return alpha * rps
    # Erkul: cooling between shots if time_between_shots > timeTillCoolingStarts
    time_between_shots = 1.0 / rps
    ttcs = heat.get("timeTillCoolingStarts", 0) or 0
    cooling_ps = heat.get("coolingPerSecond", 0) or 0
    cooling_between_shots = 0.0
    if time_between_shots > ttcs:
        cooling_between_shots = (time_between_shots - ttcs) * cooling_ps
    # Effective heat per shot after cooling
    effective_hps = hps - cooling_between_shots
    if effective_hps <= 0:
        return alpha * rps   # never overheats
    # Overheat time and shots before overheat (erkul uses Math.ceil)
    oh_time = ot / (effective_hps * rps)
    shots_before_oh = math.ceil(oh_time * rps)
    cycle = oh_time + ft
    return (shots_before_oh * alpha) / cycle if cycle > 0 else 0.0


def _dmg_breakdown(weapon_data: dict) -> dict:
    ammo_d = weapon_data.get("ammo", {}).get("data", {})
    dmg  = ammo_d.get("damage", {})
    expl = ammo_d.get("explosion", {}).get("damage", {})
    result = {}
    for k in ("damagePhysical", "damageEnergy", "damageDistortion", "damageThermal"):
        result[k] = float(dmg.get(k, 0) or 0) + float(expl.get(k, 0) or 0)
    return result


def compute_weapon_stats(raw: dict) -> dict:
    d   = raw.get("data", {})
    rps = _fire_rate_rps(d)
    alp = _alpha_max(d)
    brk = _dmg_breakdown(d)
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
        "dps_sus":   _dps_sustained(d, alp, rps),
        "ammo":      d.get("ammoContainer", {}).get("maxAmmoCount", 0),
        "dmg":       brk,
        "dom":       dom,
    }


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

_FY_SIZE_MAP = {
    "small": 1, "s": 1, "one": 1, "1": 1,
    "medium": 2, "m": 2, "two": 2, "2": 2,
    "large": 3, "l": 3, "three": 3, "3": 3,
    "capital": 4, "xl": 4, "four": 4, "4": 4,
}

def _fy_size(raw) -> int:
    """Convert a Fleetyards size string/int to an integer (1–4)."""
    if isinstance(raw, int):
        return raw
    s = str(raw).lower().strip()
    return _FY_SIZE_MAP.get(s, 1)


def _fy_slug(name: str) -> str:
    """Derive Fleetyards model slug from a ship display name."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s


def _fy_hp_group(fy_list: list) -> dict:
    """Group a flat Fleetyards hardpoints array by type key."""
    groups: dict = {}
    for hp in (fy_list or []):
        t = hp.get("type", "unknown")
        groups.setdefault(t, []).append(hp)
    return groups


def _fy_comp_name(hp: dict) -> str:
    """Return the fitted component name from a FY hardpoint entry."""
    comp = hp.get("component") or {}
    return comp.get("name") or hp.get("loadoutIdentifier") or "—"


def _fy_comp_mfr(hp: dict) -> str:
    comp = hp.get("component") or {}
    mfr  = comp.get("manufacturer") or {}
    return mfr.get("name") or mfr.get("code") or ""


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


# ── Label helpers ─────────────────────────────────────────────────────────────

_LABEL_STRIP = re.compile(
    r"(^hardpoint_|_weapon$|_gun$|^hardpoint_class_\d+$|^weapon_)",
    re.IGNORECASE,
)
_TURRET_HOUSING_SUBTYPES = {
    "TopTurret", "MannedTurret", "BallTurret", "NoseTurret",
    "RemoteTurret", "UpperTurret", "LowerTurret",
}
_GROUP_SHORT = {
    "laser repeater":        "LR", "laser cannon":          "LC",
    "laser gatling":         "LG", "laser scattergun":      "LS",
    "laser beam":            "LB", "ballistic repeater":    "BR",
    "ballistic cannon":      "BC", "ballistic gatling":     "BG",
    "ballistic scattergun":  "BS", "distortion cannon":     "DC",
    "distortion repeater":   "DR", "distortion scattergun": "DS",
    "plasma cannon":         "PC", "tachyon cannon":        "TC",
    "neutron cannon":        "NC", "rocket pod":            "RP",
}
_DOM_COL = {
    "damagePhysical":    PHYS_COL,
    "damageEnergy":      ENERGY_COL,
    "damageDistortion":  DIST_COL,
    "damageThermal":     THERM_COL,
}
_TRACK_COL = {"IR": THERM_COL, "EM": ENERGY_COL, "CrossSection": PHYS_COL}

# Voice-command tab → data-access + UI-change mapping
_TAB_FIND = {
    "weapons":  "find_weapon",
    "missiles": "find_missile",
    "defenses": "find_shield",
}
_TAB_CHANGE = {
    "weapons":  "_weapon_on_change",
    "missiles": "_missile_on_change",
    "defenses": "_shield_on_change",
}


def _port_label(name: str) -> str:
    s = re.sub(r"hardpoint_|_weapon$|weapon_", "", name, flags=re.I)
    s = re.sub(r"_+", " ", s).strip()
    return s.title() if s else name.replace("_", " ").title()


def group_short(g: str) -> str:
    return _GROUP_SHORT.get(g.lower(), g[:3].upper() if g else "—")


def pct(v: float) -> str:
    return f"{v*100:+.0f}%" if v is not None else "0%"


# ── Ship slot extraction ──────────────────────────────────────────────────────

def extract_slots_by_type(loadout: list, accept_types: set) -> list:
    """
    Walk the loadout tree and return slots whose itemTypes match accept_types.
    For turret housings that contain weapon/gun ports, recurse into them.
    Returns list of { id, label, max_size, editable, local_ref }.
    """
    slots = []

    def _resolve_weapon_ref(port, depth=0):
        """Resolve the actual weapon/missile ref from a gun, turret, or missile port.
        Recursively searches up to 3 levels deep for the innermost weapon ref.

        Hierarchy examples:
          Gun port → hardpoint_class_2 → localReference = weapon UUID
          Turret → turret_left → hardpoint_class_2 → localReference = weapon UUID
          Missile rack → missile_01_attach → localName = missile localName
        """
        if depth > 4:
            return ""

        ln = port.get("localName", "")
        lr = port.get("localReference", "")
        children = port.get("loadout", [])

        # Missile racks: localName starts with 'mrck_', missile is in children
        if ln and ln.startswith("mrck_") and children:
            for child in children:
                child_ln = child.get("localName", "")
                if child_ln and child_ln.startswith("misl_"):
                    return child_ln
            return ln

        # If this port has localName that looks like a weapon/missile, use it
        # Skip names that are gimbal mounts, controllers, bomb racks, or other non-weapons
        _SKIP_PREFIXES = ("controller_", "bmbrck_", "mount_gimbal_", "mount_fixed_",
                          "turret_", "relay_", "vehicle_screen", "radar_display")
        if ln and not any(ln.startswith(pfx) for pfx in _SKIP_PREFIXES):
            # Also skip if it has children (it's a housing, not a weapon)
            if not children:
                return ln

        # Search children recursively for the deepest weapon ref
        for child in children:
            child_ipn = child.get("itemPortName", "")
            child_ln = child.get("localName", "")
            child_lr = child.get("localReference", "")
            child_children = child.get("loadout", [])

            # If child has its own children (deeper nesting), recurse
            if child_children:
                result = _resolve_weapon_ref(child, depth + 1)
                if result:
                    return result

            # Child has a localName (weapon/missile) — skip non-weapon names
            if child_ln and not any(child_ln.startswith(pfx) for pfx in _SKIP_PREFIXES):
                return child_ln

            # Child has a localReference (weapon UUID on hardpoint_class_*,
            # hardpoint_left/right, turret_weapon, etc.)
            is_weapon_port = ("class" in child_ipn or "weapon" in child_ipn
                              or "gun" in child_ipn or "turret" in child_ipn
                              or "missile" in child_ipn
                              or child_ipn in ("hardpoint_left", "hardpoint_right",
                                               "hardpoint_upper", "hardpoint_lower"))
            if is_weapon_port:
                if child_lr:
                    return child_lr
                else:
                    # Found the weapon port but it's empty — no stock weapon equipped.
                    # Return "" to prevent falling back to parent's mount UUID.
                    return ""

        # Fall back to this port's localReference
        return lr

    # Port names to skip entirely — not real weapon/missile slots
    _SKIP_PORT_PATTERNS = ("camera", "tractor", "self_destruct", "landing",
                            "fuel_port", "docking", "air_traffic", "relay",
                            "salvage", "mining", "scan")

    def walk(ports, parent_label="", inherited_size=None):
        for port in (ports or []):
            pname     = port.get("itemPortName", "")
            pname_lower = pname.lower()

            # Skip non-weapon ports
            if any(pat in pname_lower for pat in _SKIP_PORT_PATTERNS):
                continue

            types     = port.get("itemTypes", [])
            editable  = port.get("editable", False)
            max_sz    = port.get("maxSize") or inherited_size or 1
            local_ref = port.get("localName", port.get("localReference", ""))
            children  = port.get("loadout", [])

            type_names = {t.get("type", "")  for t in types}
            sub_names  = {t.get("subType", "") for t in types}

            label = _port_label(pname)
            if parent_label:
                label = f"{parent_label} / {label}"

            # Determine what this port actually is
            is_gun         = "WeaponGun" in type_names
            is_missile     = "MissileLauncher" in type_names
            is_bomb        = "BombLauncher" in type_names
            is_gun_turret  = "Turret" in type_names and bool(sub_names & {"Gun", "GunTurret"})
            is_housing     = ("Turret" in type_names or "TurretBase" in type_names) and bool(
                sub_names & (_TURRET_HOUSING_SUBTYPES - {"GunTurret"})
            )
            is_inner_gun   = (
                pname.startswith("turret_")
                or pname.startswith("hardpoint_class")
                or pname.startswith("hardpoint_weapon")
            ) and not types and inherited_size is not None

            # Skip bomb launchers from weapon extraction (they're not guns)
            if is_bomb and "WeaponGun" in accept_types and "BombLauncher" not in accept_types:
                continue

            # Skip missile turrets from gun extraction (PDS/CIWS turrets)
            is_missile_turret = "Turret" in type_names and "MissileTurret" in sub_names
            if is_missile_turret and "WeaponGun" in accept_types:
                continue

            is_match = bool(type_names & accept_types)

            if "WeaponGun" in accept_types or "MissileLauncher" in accept_types:
                want_guns = "WeaponGun" in accept_types
                want_missiles = "MissileLauncher" in accept_types

                # Skip missile-named ports when extracting guns
                if want_guns and not want_missiles:
                    if ("missile" in pname_lower or "missilerack" in pname_lower
                            or "bombrack" in pname_lower or "bomb_" in pname_lower):
                        if not is_gun or is_missile:
                            continue

                # For missile-only extraction: only extract direct MissileLauncher
                # ports. Don't recurse into turret housings or extract inner gun ports.
                missile_only = want_missiles and not want_guns

                if is_match or (is_gun_turret and not missile_only):
                    weapon_ref = _resolve_weapon_ref(port)
                    slots.append({
                        "id":        f"{parent_label}:{pname}",
                        "label":     label,
                        "max_size":  max_sz,
                        "editable":  editable,
                        "local_ref": weapon_ref,
                    })
                elif is_housing and not missile_only:
                    # Only recurse into turret housings for gun extraction
                    walk(children, label, max_sz)
                elif is_inner_gun and not missile_only:
                    # Only extract inner gun ports for gun extraction
                    weapon_ref = _resolve_weapon_ref(port)
                    slots.append({
                        "id":        f"{parent_label}:{pname}_{len(slots)}",
                        "label":     label,
                        "max_size":  inherited_size,
                        "editable":  True,
                        "local_ref": weapon_ref,
                    })
                else:
                    if children:
                        walk(children, parent_label, inherited_size)
            else:
                # Component tab logic (Shield, Cooler, Radar, PowerPlant, QuantumDrive…)
                if is_match:
                    slots.append({
                        "id":        f"{pname}",
                        "label":     label,
                        "max_size":  max_sz,
                        "editable":  editable,
                        "local_ref": local_ref,
                    })
                elif children:
                    walk(children, parent_label, inherited_size)

    walk(loadout)
    return slots


# ── Component picker popup ────────────────────────────────────────────────────
#
# Column spec: list of (header, key, char_width, fg_color, fmt_fn)
#   fmt_fn(value, item_dict) -> str
#
# Usage:
#   ComponentPickerPopup(root, anchor_btn, items, columns, current_name,
#                        on_select=lambda item_or_None: ...)

_INF = float('inf')   # erkul sentinel for "unlimited"

QD_COLS = [
    ("Name",       "name",       16, FG,         lambda v, it: it["name"]),
    ("Class",      "class",       9, FG_DIM,     lambda v, it: str(v) if v else "—"),
    ("Grade",      "grade",       5, FG_DIM,     lambda v, it: str(v) if v else "—"),
    ("Speed km/s", "speed",      10, GREEN,      lambda v, it: f"{v/1000:,.0f}" if v else "—"),
    ("Max Dist Gm","jump_range", 10, FG,         lambda v, it: "∞" if v >= _INF else (f"{v/1e9:.1f}" if v else "—")),
    ("Spool s",    "spool",       7, YELLOW,     lambda v, it: f"{v:.1f}" if v else "—"),
    ("Cooldown s", "cooldown",    9, FG_DIM,     lambda v, it: f"{v:.1f}" if v else "—"),
    ("Fuel/Mm",    "fuel_rate",   8, ENERGY_COL, lambda v, it: f"{v:.2f}" if v else "—"),
    ("Power kW",   "power_draw",  8, ORANGE,     lambda v, it: f"{v/1000:.1f}" if v else "—"),
    ("EM",         "em_max",      8, YELLOW,     lambda v, it: f"{v:,.0f}" if v else "—"),
    ("HP",         "hp",          6, PHYS_COL,   lambda v, it: f"{v:.0f}" if v else "—"),
]

PP_COLS = [
    ("Name",       "name",       20, FG,     lambda v, it: it["name"]),
    ("Class",      "class",      10, FG_DIM, lambda v, it: str(v) if v else "—"),
    ("Grade",      "grade",       5, FG_DIM, lambda v, it: str(v) if v else "—"),
    ("Output",     "output",     10, ORANGE, lambda v, it: f"{v:,.0f}" if v else "—"),
    ("IR",         "ir_max",      8, THERM_COL, lambda v, it: f"{v:,.0f}" if v else "—"),
    ("EM",         "em_max",      8, YELLOW, lambda v, it: f"{v:,.0f}" if v else "—"),
    ("HP",         "hp",          6, PHYS_COL,   lambda v, it: f"{v:.0f}" if v else "—"),
]

# ── Inline ComponentTable column specs (for erkul-style table rows) ──────────
# Format matches QD_COLS/PP_COLS: (header, key, char_width, fg_color, fmt_fn)

WEAPON_TABLE_COLS = [
    ("Name",    "name",    12, FG,      lambda v, it: it["name"]),
    ("Type",    "group",    3, FG_DIM,  lambda v, it: group_short(it.get("group", ""))),
    ("DPS↓",   "dps_sus",  7, GREEN,   lambda v, it: f"{v:,.0f}" if v else "—"),
    ("Raw",     "dps_raw",  6, YELLOW,  lambda v, it: f"{v:,.0f}" if v else "—"),
    ("Alpha",   "alpha",    6, ACCENT,  lambda v, it: f"{v:.1f}" if v else "—"),
    ("RPS",     "rps",      5, FG_DIM,  lambda v, it: f"{v:.2f}" if v else "—"),
    ("Ammo",    "ammo",     5, FG,      lambda v, it: f"{int(v)}" if v else "—"),
]

MISSILE_TABLE_COLS = [
    ("Name",    "name",       12, FG,      lambda v, it: it["name"]),
    ("Track",   "tracking",    3, FG_DIM,  lambda v, it: str(v)[:2].upper() if v else "—"),
    ("Dmg↓",   "total_dmg",   7, RED,     lambda v, it: f"{v:,.0f}" if v else "—"),
    ("Speed",   "speed",       6, FG_DIM,  lambda v, it: f"{v:.0f}" if v else "—"),
    ("Range",   "lock_range",  6, YELLOW,  lambda v, it: f"{v/1000:.1f}k" if v else "—"),
    ("Lock",    "lock_time",   5, FG_DIM,  lambda v, it: f"{v:.1f}s" if v else "—"),
]

SHIELD_TABLE_COLS = [
    ("Name",    "name",           12, FG,         lambda v, it: it["name"]),
    ("Class",   "class",           5, FG_DIM,     lambda v, it: str(v) if v else "—"),
    ("HP↓",    "hp",               7, PURPLE,     lambda v, it: f"{v:,.0f}" if v else "—"),
    ("Reg/s",   "regen",           6, GREEN,      lambda v, it: f"{v:.1f}" if v else "—"),
    ("Phys",    "res_phys_max",    5, PHYS_COL,   lambda v, it: pct(v)),
    ("Enrg",    "res_energy_max",  5, ENERGY_COL, lambda v, it: pct(v)),
    ("Dist",    "res_dist_max",    5, DIST_COL,   lambda v, it: pct(v)),
    ("Power",   "power_draw",      6, ORANGE,     lambda v, it: f"{v/1000:.1f}" if v else "—"),
    ("EM",      "em_max",          5, YELLOW,     lambda v, it: f"{v:,.0f}" if v else "—"),
]

COOLER_TABLE_COLS = [
    ("Name",    "name",         12, FG,       lambda v, it: it["name"]),
    ("Class",   "class",         5, FG_DIM,   lambda v, it: str(v) if v else "—"),
    ("Cool↓",  "cooling_rate",   7, GREEN,    lambda v, it: f"{v:,.0f}" if v else "—"),
    ("Pwr",     "power_draw",    6, ORANGE,   lambda v, it: f"{v/1000:.1f}" if v else "—"),
    ("IR",      "ir_max",        5, THERM_COL, lambda v, it: f"{v:.0f}" if v else "—"),
    ("EM",      "em_max",        5, YELLOW,    lambda v, it: f"{v:.0f}" if v else "—"),
    ("HP",      "hp",            5, PHYS_COL,  lambda v, it: f"{v:.0f}" if v else "—"),
]

RADAR_TABLE_COLS = [
    ("Name",    "name",          12, FG,       lambda v, it: it["name"]),
    ("Class",   "class",          5, FG_DIM,   lambda v, it: str(v) if v else "—"),
    ("Det↓",   "detection_min",   6, GREEN,    lambda v, it: f"{v:.0f}" if v else "—"),
    ("Max",     "detection_max",   6, FG,       lambda v, it: f"{v:.0f}" if v else "—"),
    ("Power",   "power_draw",      6, ORANGE,   lambda v, it: f"{v/1000:.1f}" if v else "—"),
    ("EM",      "em_max",          5, YELLOW,   lambda v, it: f"{v:.0f}" if v else "—"),
    ("HP",      "hp",              5, PHYS_COL, lambda v, it: f"{v:.0f}" if v else "—"),
]


# ── Inline ComponentTable (replaces Combobox for slot selection) ─────────────

class ComponentTable(tk.Frame):
    """Lightweight component slot row. Shows the currently selected component
    as a single erkul-style data row. Click to open ComponentPickerPopup for
    changing. This replaces the old full-table approach that created ~7000+
    widgets and caused severe lag.

    Layout (one row, ~28px):
      [3px accent stripe] [Sz badge] [Name (orange)] [stat1] [stat2] ... [▼]

    Click the row → opens ComponentPickerPopup with all compatible items.
    """

    _SEL_BG  = "#1e2840"
    _HOVER   = "#222840"
    _EMPTY_BG = CARD_ODD

    def __init__(self, parent, columns, items, on_select, *,
                 current_ref="", type_color=ACCENT, max_rows=6):
        super().__init__(parent, bg=BG)
        self._cols      = columns     # [(header, key, cw, color, fmt_fn), ...]
        self._items     = list(items)
        self._on_select = on_select
        self._sel_ref   = current_ref
        self._type_col  = type_color
        self._max_rows  = max_rows  # kept for API compat
        self._sel_item  = None       # currently selected item dict
        self._root_win  = None       # resolved lazily

        # Find selected item
        if current_ref:
            for it in self._items:
                if it.get("ref") == current_ref:
                    self._sel_item = it
                    break
        self._build_row()

    # ── single-row layout ─────────────────────────────────────────────────────

    def _build_row(self):
        for w in self.winfo_children():
            w.destroy()

        item = self._sel_item
        bg   = self._SEL_BG if item else self._EMPTY_BG

        fr = tk.Frame(self, bg=bg, cursor="hand2")
        fr.pack(fill="x")

        # Left accent stripe
        tk.Frame(fr, bg=self._type_col if item else FG_DIMMER,
                 width=3).pack(side="left", fill="y")

        if item:
            # Size badge
            sz = item.get("size", 1)
            tk.Label(fr, text=f"S{sz}", width=3, font=("Consolas", 8),
                     bg=bg, fg=FG_DIM, anchor="c", pady=2).pack(side="left")

            # Data columns
            for header, key, cw, color, fmt_fn in self._cols:
                val = item.get(key, 0) or 0
                try:
                    text = fmt_fn(val, item)
                except Exception:
                    text = str(val) if val else "—"
                fg_c   = ORANGE if key == "name" else color
                anchor = "w" if key == "name" else "e"
                tk.Label(fr, text=text, width=cw, font=("Consolas", 8),
                         bg=bg, fg=fg_c, anchor=anchor, padx=2,
                         pady=2).pack(side="left")
        else:
            tk.Label(fr, text="  (empty — click to select)",
                     font=("Consolas", 8), bg=bg, fg=FG_DIM,
                     pady=2).pack(side="left", padx=4)

        # Dropdown arrow
        tk.Label(fr, text="▼", font=("Consolas", 7), bg=bg, fg=FG_DIM,
                 padx=4).pack(side="right")

        # Click → open picker popup
        def _open(e=None):
            root = self._get_root()
            if root:
                ComponentPickerPopup(
                    root, fr, self._items, self._cols,
                    self._sel_item.get("name", "") if self._sel_item else "",
                    self._on_picked,
                )

        # Hover
        def _enter(e, f=fr, h=self._HOVER):
            f.configure(bg=h)
            for c in f.winfo_children():
                try: c.configure(bg=h)
                except Exception: print(f"[DPS] Hover enter configure failed for widget {c}")
        def _leave(e, f=fr, o=bg):
            f.configure(bg=o)
            for c in f.winfo_children():
                try: c.configure(bg=o)
                except Exception: print(f"[DPS] Hover leave configure failed for widget {c}")

        for widget in [fr] + list(fr.winfo_children()):
            widget.bind("<Button-1>", lambda e: _open())
            widget.bind("<Enter>", _enter)
            widget.bind("<Leave>", _leave)

    def _get_root(self):
        """Walk up to find the Tk root window."""
        if self._root_win:
            return self._root_win
        w = self
        while w.master:
            w = w.master
        self._root_win = w
        return w

    def _on_picked(self, item):
        """Called by ComponentPickerPopup when user picks a component."""
        if item is None:
            self._sel_ref  = ""
            self._sel_item = None
            self._on_select(None)
        else:
            self._sel_ref  = item.get("ref", "")
            self._sel_item = item
            self._on_select(item)
        self._build_row()

    # ── sort / API (kept for compatibility) ───────────────────────────────────

    def _sort_by(self, key):
        pass  # sorting happens inside the popup now

    def set_selected(self, ref):
        """Programmatically select a component by ref."""
        self._sel_ref = ref
        self._sel_item = None
        if ref:
            for it in self._items:
                if it.get("ref") == ref:
                    self._sel_item = it
                    break
        self._build_row()

    def refresh(self, items, selected_ref=""):
        """Replace items and selection."""
        self._items   = list(items)
        self._sel_ref = selected_ref
        self._sel_item = None
        if selected_ref:
            for it in self._items:
                if it.get("ref") == selected_ref:
                    self._sel_item = it
                    break
        self._build_row()


class ComponentPickerPopup(tk.Toplevel):
    """Erkul-style component picker: scrollable table with filter + leave-empty."""

    def __init__(self, root, anchor_widget, items, columns, current_name, on_select):
        super().__init__(root)
        self._root       = root
        self._items      = list(items)
        self._filtered   = list(items)
        self._columns    = columns
        self._on_select  = on_select
        self._cur_name   = current_name
        self._sort_key   = None    # column key to sort by
        self._sort_rev   = True    # descending by default
        self._hdr_labels = []      # for updating sort arrows

        # Frameless dark window
        self.overrideredirect(True)
        self.configure(bg=BORDER)
        self.attributes("-topmost", True)

        self._build_ui()
        self._position(anchor_widget)

        # Close on click outside
        self._bind_id = root.bind("<Button-1>", self._on_root_click, add="+")
        self.bind("<Escape>", lambda _: self._close())
        # Clean up root binding when popup is destroyed (prevent binding leak)
        self.bind("<Destroy>", lambda e: (
            self._root.unbind("<Button-1>", self._bind_id) if self._bind_id and e.widget == self else None
        ), add="+")
        self._filter_entry.focus_set()

    # ── Build ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="both", expand=True)

        # ── Top bar ──
        top = tk.Frame(outer, bg=BG2, pady=6)
        top.pack(fill="x")

        tk.Label(top, text="Select component or", font=("Consolas", 9),
                 bg=BG2, fg=FG_DIM, padx=8).pack(side="left")

        tk.Button(top, text="leave empty",
                  font=("Consolas", 9, "bold"), bg=BG3, fg=FG_DIM,
                  relief="flat", bd=0, cursor="hand2",
                  activebackground=BORDER, activeforeground=FG,
                  padx=10, pady=3,
                  command=lambda: self._select(None)).pack(side="left", padx=4)

        tk.Label(top, text="Filter", font=("Consolas", 9),
                 bg=BG2, fg=FG_DIM).pack(side="left", padx=(16, 4))

        self._filter_var = tk.StringVar()
        self._filter_entry = tk.Entry(
            top, textvariable=self._filter_var,
            font=("Consolas", 9), bg=BG3, fg=FG,
            insertbackground=FG, relief="flat", bd=1, width=22,
        )
        self._filter_entry.pack(side="left", ipady=4, padx=(0, 8))
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())

        # ── Column header (clickable for sorting) ──
        hdr = tk.Frame(outer, bg=HEADER_BG, height=22)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        self._hdr_labels = []
        tk.Label(hdr, text="Sz", width=3, font=("Consolas", 8, "bold"),
                 bg=HEADER_BG, fg=FG_DIM, anchor="c").pack(side="left", padx=(4, 0))
        for header, key, cw, color, _ in self._columns:
            anchor = "w" if key == "name" else "e"
            lbl = tk.Label(hdr, text=header, width=cw, font=("Consolas", 8, "bold"),
                           bg=HEADER_BG, fg=FG_DIM, anchor=anchor,
                           padx=3, cursor="hand2")
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, k=key: self._sort_by(k))
            self._hdr_labels.append((lbl, header, key))

        # ── Scrollable list ──
        scroll_outer = tk.Frame(outer, bg=BG)
        scroll_outer.pack(fill="both", expand=True)

        vbar = ttk.Scrollbar(scroll_outer, orient="vertical")
        vbar.pack(side="right", fill="y")

        self._canvas = tk.Canvas(
            scroll_outer, bg=BG, highlightthickness=0,
            yscrollcommand=vbar.set,
        )
        self._canvas.pack(fill="both", expand=True)
        vbar.configure(command=self._canvas.yview)

        self._list_frame = tk.Frame(self._canvas, bg=BG)
        self._cwin = self._canvas.create_window(
            (0, 0), window=self._list_frame, anchor="nw"
        )
        self._list_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfig(self._cwin, width=e.width),
        )
        self._canvas.bind(
            "<MouseWheel>",
            lambda e: self._canvas.yview_scroll(-1 * (int(e.delta / 120) or (1 if e.delta > 0 else (-1 if e.delta < 0 else 0))), "units"),
        )

        self._populate()

    def _sort_by(self, key):
        """Sort filtered items by the clicked column."""
        if self._sort_key == key:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_key = key
            self._sort_rev = True  # new column → descending first
        # Update header labels to show sort arrow
        for lbl, header, k in self._hdr_labels:
            if k == key:
                arrow = " ↓" if self._sort_rev else " ↑"
                lbl.configure(text=header + arrow, fg=ACCENT)
            else:
                lbl.configure(text=header, fg=FG_DIM)
        self._populate()

    def _populate(self):
        for w in self._list_frame.winfo_children():
            w.destroy()

        items = self._filtered
        if self._sort_key:
            try:
                items = sorted(items,
                               key=lambda x: (x.get(self._sort_key) or 0),
                               reverse=self._sort_rev)
            except TypeError:
                items = sorted(items,
                               key=lambda x: str(x.get(self._sort_key, "")),
                               reverse=self._sort_rev)

        for i, item in enumerate(items):
            is_cur = item.get("name", "") == self._cur_name
            bg     = "#1e2840" if is_cur else (ROW_EVEN if i % 2 == 0 else ROW_ODD)
            fr     = tk.Frame(self._list_frame, bg=bg, cursor="hand2")
            fr.pack(fill="x")

            sz     = item.get("size", 1)
            sz_col = ACCENT if sz >= 3 else (GREEN if sz == 2 else YELLOW)
            tk.Label(fr, text=f"S{sz}", width=3, font=("Consolas", 9, "bold"),
                     bg=bg, fg=sz_col, anchor="c", pady=3).pack(side="left", padx=(4, 0))

            for header, key, cw, color, fmt_fn in self._columns:
                val    = item.get(key, 0) or 0
                text   = fmt_fn(val, item)
                anchor = "w" if key == "name" else "e"
                tk.Label(fr, text=text, width=cw, font=("Consolas", 9),
                         bg=bg, fg=color, anchor=anchor, padx=3,
                         pady=3).pack(side="left")

            # Hover highlight + click
            def _enter(e, f=fr, orig=bg):
                f.configure(bg=BG3)
                for c in f.winfo_children(): c.configure(bg=BG3)

            def _leave(e, f=fr, orig=bg):
                f.configure(bg=orig)
                for c in f.winfo_children(): c.configure(bg=orig)

            def _click(e, it=item): self._select(it)

            fr.bind("<Enter>",    _enter)
            fr.bind("<Leave>",    _leave)
            fr.bind("<Button-1>", _click)
            for child in fr.winfo_children():
                child.bind("<Enter>",    _enter)
                child.bind("<Leave>",    _leave)
                child.bind("<Button-1>", _click)

    def _apply_filter(self):
        q = self._filter_var.get().lower().strip()
        self._filtered = (
            [it for it in self._items if q in it.get("name", "").lower()]
            if q else list(self._items)
        )
        self._populate()

    # ── Interaction ──────────────────────────────────────────────────────────

    def _select(self, item):
        self._close()
        self._on_select(item)

    def _position(self, anchor_widget):
        self.update_idletasks()
        ax  = anchor_widget.winfo_rootx()
        ay  = anchor_widget.winfo_rooty() + anchor_widget.winfo_height() + 2
        sw  = self._root.winfo_screenwidth()
        sh  = self._root.winfo_screenheight()
        # Use a sensible fixed width (columns are ~700px wide typically)
        pw  = max(self.winfo_reqwidth(), 580)
        pw  = min(pw, sw - 20)
        ph  = min(max(self.winfo_reqheight(), 320), 480)
        # Clamp to screen — ensure popup is fully visible
        ax  = max(0, min(ax, sw - pw - 10))
        ay  = max(0, min(ay, sh - ph - 10))
        # If popup would overlap the bottom, show it above the anchor instead
        if ay + ph > sh - 40:
            ay = max(0, anchor_widget.winfo_rooty() - ph - 2)
        self.geometry(f"{pw}x{ph}+{ax}+{ay}")

    def _on_root_click(self, event):
        try:
            wx, wy = self.winfo_x(), self.winfo_y()
            ww, wh = self.winfo_width(), self.winfo_height()
            if not (wx <= event.x_root <= wx + ww and wy <= event.y_root <= wy + wh):
                self._close()
        except Exception:
            pass

    def _close(self):
        try:
            self._root.unbind("<Button-1>", self._bind_id)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


def _picker_btn(parent, bg, text="Select…", width=28):
    """Styled button that mimics a combobox for opening ComponentPickerPopup."""
    btn = tk.Button(
        parent, text=text, width=width,
        font=("Consolas", 9), bg=BG3, fg=FG,
        relief="flat", bd=1, cursor="hand2",
        activebackground=BORDER, activeforeground=FG,
        anchor="w", padx=6, pady=3,
    )
    btn.pack(side="left", padx=(0, 6))
    return btn


# ── Data manager ──────────────────────────────────────────────────────────────

class _IndexSnapshot:
    """Immutable-ish container for all DataManager index dicts.

    Built in the background thread and swapped in via a single atomic
    reference assignment (``self._idx = snap``) so that readers never
    see a half-populated state — no lock required on the read path.
    """
    __slots__ = ('weapons_by_ref', 'weapons_by_name', 'shields_by_ref', 'shields_by_name',
                 'coolers_by_ref', 'coolers_by_name', 'radars_by_ref', 'radars_by_name',
                 'missiles_by_ref', 'missiles_by_name', 'powerplants_by_ref', 'powerplants_by_name',
                 'qdrives_by_ref', 'qdrives_by_name', 'ships_by_name')
    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, {})


class DataManager:
    def __init__(self):
        self.raw: dict = {}          # endpoint → raw list
        self.loaded  = False
        self.loading = False
        self.error: Optional[str] = None
        self._lock = threading.Lock()

        # BUG 4/6: track cached game version for auto-update detection
        self.cached_game_version: str = ""

        # Indexed dicts live inside an _IndexSnapshot so readers can access
        # a consistent set without acquiring _lock.  The background thread
        # builds a new snapshot and swaps it in atomically.
        # BUG 1 FIX: by_name is now keyed f"{name.lower()}_{size}" so ALL size variants
        # are preserved — no longer overwritten by the largest size.
        self._idx = _IndexSnapshot()

        # Fleetyards per-ship hardpoints: slug → {ts, hardpoints: list}
        self._fy_hp_mem: dict = {}   # in-memory cache (session lifetime)
        self._fy_hp_disk_loaded = False  # sentinel: True after first disk load

    # ── Backward-compatible property accessors into _IndexSnapshot ────────────
    # External code (audit scripts, etc.) reads dm.weapons_by_ref etc. directly.
    # These properties delegate to the current snapshot, which is safe because
    # the snapshot reference swap is atomic.

    @property
    def weapons_by_ref(self):     return self._idx.weapons_by_ref
    @weapons_by_ref.setter
    def weapons_by_ref(self, v):  self._idx.weapons_by_ref = v
    @property
    def weapons_by_name(self):    return self._idx.weapons_by_name
    @weapons_by_name.setter
    def weapons_by_name(self, v): self._idx.weapons_by_name = v
    @property
    def shields_by_ref(self):     return self._idx.shields_by_ref
    @shields_by_ref.setter
    def shields_by_ref(self, v):  self._idx.shields_by_ref = v
    @property
    def shields_by_name(self):    return self._idx.shields_by_name
    @shields_by_name.setter
    def shields_by_name(self, v): self._idx.shields_by_name = v
    @property
    def coolers_by_ref(self):     return self._idx.coolers_by_ref
    @coolers_by_ref.setter
    def coolers_by_ref(self, v):  self._idx.coolers_by_ref = v
    @property
    def coolers_by_name(self):    return self._idx.coolers_by_name
    @coolers_by_name.setter
    def coolers_by_name(self, v): self._idx.coolers_by_name = v
    @property
    def radars_by_ref(self):      return self._idx.radars_by_ref
    @radars_by_ref.setter
    def radars_by_ref(self, v):   self._idx.radars_by_ref = v
    @property
    def radars_by_name(self):     return self._idx.radars_by_name
    @radars_by_name.setter
    def radars_by_name(self, v):  self._idx.radars_by_name = v
    @property
    def missiles_by_ref(self):    return self._idx.missiles_by_ref
    @missiles_by_ref.setter
    def missiles_by_ref(self, v): self._idx.missiles_by_ref = v
    @property
    def missiles_by_name(self):   return self._idx.missiles_by_name
    @missiles_by_name.setter
    def missiles_by_name(self, v):self._idx.missiles_by_name = v
    @property
    def powerplants_by_ref(self):     return self._idx.powerplants_by_ref
    @powerplants_by_ref.setter
    def powerplants_by_ref(self, v):  self._idx.powerplants_by_ref = v
    @property
    def powerplants_by_name(self):    return self._idx.powerplants_by_name
    @powerplants_by_name.setter
    def powerplants_by_name(self, v): self._idx.powerplants_by_name = v
    @property
    def qdrives_by_ref(self):     return self._idx.qdrives_by_ref
    @qdrives_by_ref.setter
    def qdrives_by_ref(self, v):  self._idx.qdrives_by_ref = v
    @property
    def qdrives_by_name(self):    return self._idx.qdrives_by_name
    @qdrives_by_name.setter
    def qdrives_by_name(self, v): self._idx.qdrives_by_name = v
    @property
    def ships_by_name(self):      return self._idx.ships_by_name
    @ships_by_name.setter
    def ships_by_name(self, v):   self._idx.ships_by_name = v

    # ── Fleetyards hardpoints ──────────────────────────────────────────────────

    def _fy_load_disk_cache(self) -> dict:
        """Load FY hardpoints disk cache. Returns {slug: {ts, hardpoints}}."""
        try:
            if os.path.isfile(FY_HP_CACHE_FILE):
                with open(FY_HP_CACHE_FILE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            _log.warning("FY disk cache load failed: %s", e)
        return {}

    def _fy_save_disk_cache(self):
        try:
            with open(FY_HP_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._fy_hp_mem, f)
        except Exception as e:
            _log.warning("FY disk cache save failed: %s", e)

    def fetch_fy_hardpoints(self, ship_name: str, on_done=None):
        """
        Fetch Fleetyards hardpoints for ship_name in a background thread.
        Calls on_done(grouped_dict) on the calling thread (via root.after if
        a root is passed — callers must schedule it themselves via on_done).
        Uses both an in-memory and a disk cache keyed by slug.
        """
        slug = _fy_slug(ship_name)

        # 1. Check in-memory cache
        mem = self._fy_hp_mem.get(slug)
        if mem and time.time() - mem.get("ts", 0) < FY_HP_TTL:
            if on_done:
                on_done(_fy_hp_group(mem["hardpoints"]))
            return

        # 2. Check disk cache (populated from previous session)
        if not self._fy_hp_disk_loaded:
            self._fy_hp_mem = self._fy_load_disk_cache()
            self._fy_hp_disk_loaded = True
            mem = self._fy_hp_mem.get(slug)
            if mem and time.time() - mem.get("ts", 0) < FY_HP_TTL:
                if on_done:
                    on_done(_fy_hp_group(mem["hardpoints"]))
                return

        def _run():
            try:
                r = requests.get(
                    f"{FY_BASE}/models/{slug}/hardpoints",
                    headers=FY_HEADERS, timeout=15,
                )
                try:
                    if r.ok:
                        data = r.json()
                        self._fy_hp_mem[slug] = {"ts": time.time(), "hardpoints": data}
                        self._fy_save_disk_cache()
                        if on_done:
                            on_done(_fy_hp_group(data))
                        return
                finally:
                    r.close()
            except Exception as e:
                _log.warning("FY hardpoints fetch failed for %s: %s", slug, e)
            if on_done:
                on_done({})   # failed — caller gets empty dict

        threading.Thread(target=_run, daemon=True).start()

    # ── Cache ─────────────────────────────────────────────────────────────────

    def _save_cache(self, data: dict):
        # BUG 4: write CACHE_VERSION and game_version alongside data
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "ts":           time.time(),
                    "version":      CACHE_VERSION,
                    "game_version": self.cached_game_version,
                    "data":         data,
                }, f)
        except Exception as e:
            _log.warning("erkul cache save failed: %s", e)

    def _load_cache(self) -> Optional[dict]:
        # BUG 4: reject cache whose version != CACHE_VERSION
        try:
            if not os.path.isfile(CACHE_FILE):
                return None
            with open(CACHE_FILE, encoding="utf-8") as f:
                obj = json.load(f)
            if obj.get("version") != CACHE_VERSION:
                return None          # stale schema — force re-fetch
            if time.time() - obj.get("ts", 0) < CACHE_TTL:
                return obj.get("data", {})
        except Exception as e:
            _log.warning("erkul cache load failed: %s", e)
        return None

    def _load_cached_game_version(self) -> str:
        """Return the game_version string stored in the cache file, or ''."""
        try:
            if not os.path.isfile(CACHE_FILE):
                return ""
            with open(CACHE_FILE, encoding="utf-8") as f:
                obj = json.load(f)
            return obj.get("game_version", "")
        except Exception:
            return ""

    # ── Fetch helpers ─────────────────────────────────────────────────────────

    def _fetch(self, path: str) -> list:
        r = requests.get(API_BASE + path, headers=API_HEADERS, timeout=20)
        try:
            r.raise_for_status()
            return r.json()
        finally:
            r.close()

    def _fetch_safe(self, path: str, warn_cb=None) -> list:
        """Like _fetch but returns [] on error (Improvement D: error resilience)."""
        try:
            return self._fetch(path)
        except Exception as exc:
            if warn_cb:
                warn_cb(f"⚠ {path}: {exc}")
            return []

    def _fetch_all_ships(self, warn_cb=None) -> list:
        """
        BUG 2 FIX: Fetch the complete ship roster from erkul.games.
        Strategy:
          1. /live/ships?limit=500          (broadest single call)
          2. /live/ships?type=ground        (ground vehicles)
          3. /live/ships?type=capital       (capital ships)
          4. /live/ships                    (base endpoint, may have different set)
          5. Fleetyards.net fallback if erkul returns < 30 ships total
        All results are deduplicated by ship name.
        """
        seen: dict = {}  # name → ship entry

        def merge(entries):
            for e in (entries or []):
                n = e.get("data", {}).get("name", "")
                if n and n not in seen:
                    seen[n] = e

        merge(self._fetch_safe("/live/ships?limit=500",      warn_cb))
        merge(self._fetch_safe("/live/ships?type=ground",    warn_cb))
        merge(self._fetch_safe("/live/ships?type=capital",   warn_cb))
        merge(self._fetch_safe("/live/ships",                warn_cb))

        ships = list(seen.values())

        # Fleetyards.net fallback when erkul is too sparse
        if len(ships) < 30:
            if warn_cb:
                warn_cb("erkul returned < 30 ships — trying fleetyards.net fallback…")
            try:
                for page in range(1, 10):
                    r = requests.get(
                        f"https://fleetyards.net/api/v1/models?perPage=240&page={page}",
                        headers=API_HEADERS, timeout=20,
                    )
                    try:
                        chunk = r.json()
                        if not chunk:
                            break
                        for m in chunk:
                            n = m.get("name", "")
                            if n and n not in seen:
                                seen[n] = {
                                    "data": {
                                        "name":    n,
                                        "ref":     m.get("slug", ""),
                                        "loadout": [],
                                    }
                                }
                        if len(chunk) < 240:
                            break
                    finally:
                        r.close()
                ships = list(seen.values())
            except Exception as exc:
                if warn_cb:
                    warn_cb(f"fleetyards fallback failed: {exc}")

        return ships

    # ── Main load ─────────────────────────────────────────────────────────────

    def load(self, on_done=None):
        with self._lock:
            if self.loading:
                return
            self.loading = True

        def _run():
            try:
                cached = self._load_cache()
                if cached:
                    raw = cached
                    self.cached_game_version = self._load_cached_game_version()
                else:
                    raw = {}
                    # Improvement D: each endpoint is fetched independently;
                    # a single failure does not abort the whole load.
                    raw["/live/weapons"]      = self._fetch_safe("/live/weapons")
                    raw["/live/shields"]      = self._fetch_safe("/live/shields")
                    raw["/live/coolers"]      = self._fetch_safe("/live/coolers")
                    raw["/live/missiles"]     = self._fetch_safe("/live/missiles")
                    raw["/live/radars"]       = self._fetch_safe("/live/radars")
                    raw["/live/powerplants"]  = self._fetch_safe("/live/power-plants")
                    raw["/live/quantumdrives"]= self._fetch_safe("/live/qdrives")
                    raw["/live/thrusters"]    = self._fetch_safe("/live/thrusters")
                    raw["/live/paints"]       = self._fetch_safe("/live/paints")
                    # BUG 2 FIX: use robust multi-endpoint ship fetch
                    raw["/live/ships"]        = self._fetch_all_ships()
                    self._save_cache(raw)

                # ── Indexer ───────────────────────────────────────────────────
                def _index(entries, compute_fn, by_ref, by_name, filt=None):
                    for e in entries:
                        d = e.get("data", {})
                        if filt and not filt(d):
                            continue
                        try:
                            stats = compute_fn(e)
                        except Exception:
                            continue   # Improvement D: skip malformed entries
                        # Enrich with common fields for ComponentTable display
                        # (uses setdefault so compute_fn values are NOT overwritten)
                        def _sf(v):
                            if isinstance(v, (int, float)): return float(v)
                            if isinstance(v, str):
                                try: return float(v)
                                except ValueError: return 0.0
                            return 0.0
                        stats.setdefault("class", d.get("class", ""))
                        stats.setdefault("grade", d.get("grade", "?"))
                        _hlth = d.get("health", d.get("hp", 0))
                        if isinstance(_hlth, dict):
                            _hlth = _hlth.get("hp", 0)
                        stats.setdefault("hp", _sf(_hlth))
                        # Power draw from resource.online.consumption
                        res  = d.get("resource", {}) or {}
                        onl  = res.get("online", {}) or {}
                        cons = onl.get("consumption", {}) or {}
                        if not isinstance(cons, dict):
                            cons = {}
                        # powerSegment (shields/coolers/radars) or power (weapons)
                        pwr_draw = _sf(cons.get("powerSegment",
                                       cons.get("power", 0)))
                        stats.setdefault("power_draw", pwr_draw)
                        stats.setdefault("power_max", pwr_draw)
                        # EM / IR from resource.online.signatureParams
                        sig  = onl.get("signatureParams", {}) or {}
                        em_d = sig.get("em", {}) or {}
                        ir_d = sig.get("ir", {}) or {}
                        stats.setdefault("em_max", _sf(em_d.get("nominalSignature", 0)))
                        stats.setdefault("ir_max", _sf(ir_d.get("nominalSignature", 0)))
                        ref = stats["ref"]
                        # BUG 1 FIX: key = "name_size" so S1/S2/S3 variants all survive.
                        # No more "keep only largest" — every (name, size) pair is kept.
                        key = f"{stats['name'].lower()}_{stats['size']}"
                        if ref:
                            by_ref[ref] = stats
                        by_name[key] = stats

                # BUG 3 FIX: filter only on type=="WeaponGun"; remove subType=="Gun"
                # restriction so turret weapons and newer subtypes are included.
                # Thread-safety: build a complete _IndexSnapshot first, then
                # swap it in via a single atomic reference assignment so readers
                # never see a half-populated state.
                snap = _IndexSnapshot()

                _index(raw.get("/live/weapons", []), compute_weapon_stats,
                       snap.weapons_by_ref, snap.weapons_by_name,
                       filt=lambda d: d.get("type") == "WeaponGun")

                _index(raw.get("/live/shields", []),  compute_shield_stats,
                       snap.shields_by_ref, snap.shields_by_name)

                _index(raw.get("/live/coolers", []),  compute_cooler_stats,
                       snap.coolers_by_ref, snap.coolers_by_name)

                _index(raw.get("/live/radars", []),   compute_radar_stats,
                       snap.radars_by_ref, snap.radars_by_name)

                _index(raw.get("/live/missiles", []), compute_missile_stats,
                       snap.missiles_by_ref, snap.missiles_by_name)

                _index(raw.get("/live/powerplants", []),  compute_powerplant_stats_erkul,
                       snap.powerplants_by_ref, snap.powerplants_by_name)

                _index(raw.get("/live/quantumdrives", []), compute_qdrive_stats_erkul,
                       snap.qdrives_by_ref, snap.qdrives_by_name)

                sbn = {}
                for e in raw.get("/live/ships", []):
                    d = e.get("data", {})
                    n = d.get("name", "")
                    if n:
                        sbn[n]         = d
                        sbn[n.lower()] = d
                snap.ships_by_name = sbn

                with self._lock:
                    self.raw     = raw
                    self._idx    = snap
                    self.loaded  = True
                    self.loading = False

            except Exception as exc:
                with self._lock:
                    self.error   = str(exc)
                    self.loading = False

            # NOTE: on_done is called from this background thread.
            # Callers that touch the GUI must wrap via root.after(), e.g.:
            #   dm.load(on_done=lambda: root.after(0, my_callback))
            if on_done:
                on_done()

        threading.Thread(target=_run, daemon=True).start()

    # ── Ship accessors ────────────────────────────────────────────────────────

    def get_ship_names(self) -> list:
        seen, names = set(), []
        for e in self.raw.get("/live/ships", []):
            n = e.get("data", {}).get("name", "")
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        return sorted(names)

    def get_ship_data(self, name: str) -> Optional[dict]:
        idx = self._idx  # snapshot read
        return idx.ships_by_name.get(name) or idx.ships_by_name.get(name.lower())

    # ── Component lookup ──────────────────────────────────────────────────────

    def _find(self, by_ref: dict, by_name: dict,
              query: str, max_size: int = None) -> Optional[dict]:
        """
        BUG 1 FIX: Search by_name values (not keys) so all size variants are
        considered. When max_size is given, only return items whose size <=
        max_size; among those return the largest-size match. When max_size is
        None return the overall largest-size match (previous behaviour).
        """
        q = query.strip()
        if not q:
            return None

        # 1. Direct ref lookup (UUID)
        if q in by_ref:
            s = by_ref[q]
            if max_size is None or s["size"] <= max_size:
                return s

        ql = q.lower()

        def size_ok(v: dict) -> bool:
            return max_size is None or v["size"] <= max_size

        # 1b. local_name match (erkul localName e.g. "shld_godi_s01_allstop_scitem")
        # This is critical for stock loadout matching — loadout stores localName,
        # not the display name or UUID ref.
        for v in by_name.values():
            ln = v.get("local_name", "")
            if ln and ln.lower() == ql and size_ok(v):
                return v
        for v in by_ref.values():
            ln = v.get("local_name", "")
            if ln and ln.lower() == ql and size_ok(v):
                return v

        candidates: list = []

        # 2. Exact name match (all size variants)
        for v in by_name.values():
            if v["name"].lower() == ql and size_ok(v):
                candidates.append(v)

        # 3. Prefix match
        if not candidates:
            for v in by_name.values():
                if v["name"].lower().startswith(ql) and size_ok(v):
                    candidates.append(v)

        # 4. Substring match
        if not candidates:
            for v in by_name.values():
                if ql in v["name"].lower() and size_ok(v):
                    candidates.append(v)

        if candidates:
            # Return largest size within constraint
            return max(candidates, key=lambda x: x["size"])

        return None

    # BUG 1 FIX: all find_* now accept max_size for size-constrained slot lookup.
    # Each method captures self._idx once so the entire lookup uses a single
    # consistent snapshot even if a background load swaps _idx mid-call.
    def find_weapon(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.weapons_by_ref,  idx.weapons_by_name,  q, max_size)

    def find_shield(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.shields_by_ref,  idx.shields_by_name,  q, max_size)

    def find_cooler(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.coolers_by_ref,  idx.coolers_by_name,  q, max_size)

    def find_radar(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.radars_by_ref,   idx.radars_by_name,   q, max_size)

    def find_missile(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.missiles_by_ref, idx.missiles_by_name, q, max_size)

    def find_powerplant(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.powerplants_by_ref, idx.powerplants_by_name, q, max_size)

    def find_qdrive(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.qdrives_by_ref, idx.qdrives_by_name, q, max_size)

    def _list_for_size(self, by_name: dict, max_size: int) -> list:
        # Improvement B: with the new name+size keying, by_name.values() already
        # contains ALL size variants — no dedup needed. All that fit are shown.
        return sorted(
            [v for v in by_name.values() if v["size"] <= max_size],
            key=lambda x: (-x["size"], x["name"]),
        )

    def weapons_for_size(self, sz):      return self._list_for_size(self._idx.weapons_by_name,      sz)
    def shields_for_size(self, sz):      return self._list_for_size(self._idx.shields_by_name,      sz)
    def coolers_for_size(self, sz):      return self._list_for_size(self._idx.coolers_by_name,      sz)
    def radars_for_size(self, sz):       return self._list_for_size(self._idx.radars_by_name,       sz)
    def missiles_for_size(self, sz):     return self._list_for_size(self._idx.missiles_by_name,     sz)
    def powerplants_for_size(self, sz):  return self._list_for_size(self._idx.powerplants_by_name,  sz)
    def qdrives_for_size(self, sz):      return self._list_for_size(self._idx.qdrives_by_name,      sz)


# ── Power Allocator Widget ──────────────────────────────────────────────────

class PowerAllocator(tk.Frame):
    """Interactive power allocation panel matching erkul.games' power widget.

    Shows stacked pip bars for each powered component category,
    consumption bar, signature readouts, and SCM/NAV mode toggle.
    """

    # Column order matching erkul (no power plant column — PP just provides budget)
    CATEGORY_ORDER = [
        ("weaponGun",    "WPN",  "\u2694",  ORANGE),
        ("thruster",     "THR",  "\u2708",  CYAN),
        ("shield",       "SHD",  "\U0001f6e1", PURPLE),
        ("radar",        "RDR",  "\U0001f4e1", FG_DIM),
        ("lifeSupport",  "LSP",  "\u2764",  "#5a9a70"),
        ("cooler",       "CLR",  "\u2744",  CYAN),
        ("quantumDrive", "QDR",  "\U0001f680", ACCENT),
        ("utility",      "UTL",  "\U0001f527", YELLOW),
    ]

    PIP_W       = 18      # pixel width of each pip bar column (wider for easier clicking)
    PIP_H       = 7       # pixel height of one pip segment
    PIP_GAP     = 2       # vertical gap between pips
    GREEN_PIP   = GREEN
    ORANGE_PIP  = ORANGE
    DARK_PIP    = "#2a3040"   # visible dark empty pip (brighter than bg)
    GREY_PIP    = FG_DIMMER

    def __init__(self, parent, item_lookup_fn, raw_lookup_fn=None,
                 on_change=None, **kwargs):
        """
        Parameters
        ----------
        parent : tk widget
            Parent container.
        item_lookup_fn : callable(localName: str) -> dict | None
            Returns the erkul catalog entry for a component by its localName.
        raw_lookup_fn : callable(localName: str) -> dict | None
            Returns the raw erkul data dict (with resource/consumption fields).
        on_change : callable() -> None
            Called whenever power allocation changes (pip click, mode switch).
        """
        super().__init__(parent, bg=BG2, **kwargs)
        self._lookup = item_lookup_fn
        self._lookup_raw = raw_lookup_fn or (lambda ln: None)
        self._on_change = on_change
        self._slots: list[dict] = []
        self._categories: dict[str, list[dict]] = {}
        self._mode = "SCM"          # "SCM" or "NAV"
        self._canvases: list[tuple[tk.Canvas, dict]] = []  # (canvas, slot)
        self._build_static_ui()

    # ── public API ───────────────────────────────────────────────────────────

    def load_ship(self, ship_data: dict):
        """Build power slots from ship data. Erkul power system:

        1. Weapon pool: ship.rnPowerPools.weaponGun.poolSize = number of weapon segments
        2. Engine: ifcs.resource.online.consumption.powerSegment = engine segments
        3. Shields: each shield's consumption.powerSegment
        4. Power plants: each PP's generation.powerSegment (generators)
        5. Coolers/Radar/LifeSupport/QD: consumption.powerSegment per component
        """
        self._slots.clear()
        self._categories.clear()
        self._canvases.clear()

        if not isinstance(ship_data, dict):
            self._rebuild_columns()
            self._recalculate()
            return

        loadout = ship_data.get("loadout", [])
        if not isinstance(loadout, list):
            loadout = []

        # Store cross-section for CS signature
        cs_raw = ship_data.get("crossSection", 0)
        if isinstance(cs_raw, dict):
            # erkul stores {x, y, z} — use max face area
            cs_x = float(cs_raw.get("x", 0) or 0)
            cs_y = float(cs_raw.get("y", 0) or 0)
            cs_z = float(cs_raw.get("z", 0) or 0)
            self._ship_cs = max(cs_x, cs_y, cs_z)
        elif isinstance(cs_raw, (int, float)):
            self._ship_cs = float(cs_raw)
        else:
            self._ship_cs = 0

        slot_id = 0

        # Store armor signal multipliers from ship data
        armor_d = ship_data.get("armor", {})
        if isinstance(armor_d, dict):
            armor_d = armor_d.get("data", armor_d)
        arm = armor_d.get("armor", {}) if isinstance(armor_d, dict) else {}
        self._armor_sig_em = float(arm.get("signalElectromagnetic", 1) or 1)
        self._armor_sig_ir = float(arm.get("signalInfrared", 1) or 1)
        self._armor_sig_cs = float(arm.get("signalCrossSection", 1) or 1)

        def _add_slot(name, cat_key, max_seg, default_seg, draw_per_seg,
                       em_per_seg=0, ir_per_seg=0, is_gen=False, output=0,
                       em_total=0, ir_total=0, power_ranges=None):
            nonlocal slot_id
            if max_seg <= 0:
                return
            slot = {
                "id":           f"slot_{slot_id}",
                "name":         name,
                "category":     cat_key,
                "max_segments": max_seg,
                "default_seg":  min(default_seg, max_seg),
                "current_seg":  min(default_seg, max_seg),
                "enabled":      True,
                "draw_per_seg": draw_per_seg,
                "em_per_seg":   em_per_seg,
                "ir_per_seg":   ir_per_seg,
                "em_total":     em_total if em_total else em_per_seg * max_seg,
                "ir_total":     ir_total if ir_total else ir_per_seg * max_seg,
                "cooling_gen":  0,   # set for coolers only
                "power_ranges": power_ranges,  # [{start, modifier}, ...] from erkul
                "is_generator": is_gen,
                "output":       output,
            }
            self._slots.append(slot)
            self._categories.setdefault(cat_key, []).append(slot)
            slot_id += 1

        # ── 1. WEAPON POOL ──
        rn_pools = ship_data.get("rnPowerPools", {})
        wpn_pool = rn_pools.get("weaponGun", {})
        wpn_pool_size = wpn_pool.get("poolSize", 0)
        if wpn_pool_size and wpn_pool_size > 0:
            # Count actual equipped weapons to get total weapon power draw
            wpn_draw_total = 0
            wpn_em_total = 0
            def _count_weapons(ports):
                nonlocal wpn_draw_total, wpn_em_total
                for p in (ports if isinstance(ports, list) else []):
                    itypes = [it.get("type","") for it in p.get("itemTypes", [])]
                    ln = p.get("localName", "")
                    if ("WeaponGun" in itypes or "Turret" in itypes) and ln:
                        cat = self._lookup(ln)
                        if cat:
                            wpn_draw_total += float(cat.get("power_draw", 0) or 0)
                            wpn_em_total += float(cat.get("em_max", 0) or 0)
                    _count_weapons(p.get("loadout", []))
            _count_weapons(loadout)
            _add_slot("Weapons", "weaponGun", wpn_pool_size, wpn_pool_size,
                       wpn_draw_total / wpn_pool_size if wpn_pool_size else 0,
                       em_per_seg=wpn_em_total / wpn_pool_size if wpn_pool_size else 0,
                       em_total=wpn_em_total)

        # ── 2. ENGINE / THRUSTERS ──
        ifcs = ship_data.get("ifcs", {})
        ifcs_res = ifcs.get("resource", {}).get("online", {})
        engine_seg = ifcs_res.get("consumption", {}).get("powerSegment", 0)
        engine_min_frac = ifcs_res.get("powerConsumptionMinimumFraction", 0.1)
        if engine_seg and engine_seg > 0:
            min_seg = max(1, int(engine_seg * engine_min_frac))
            _add_slot("Thrusters", "thruster", int(engine_seg), int(engine_seg),
                       1.0)  # 1 segment = 1 power unit

        # ── 3. Compute total power plant output + EM (no pips — PP just provides budget) ──
        self._total_pp_output = 0
        self._pp_count = 0
        self._pp_total_slots = 0
        self._pp_em_total = 0.0       # sum of PP EM nominalSignature
        self._pp_power_ranges = []    # list of powerRanges per PP
        pp_size_sum = 0

        def _count_pp(ports):
            for port in (ports if isinstance(ports, list) else []):
                itypes = [it.get("type","") for it in port.get("itemTypes", [])]
                ln = port.get("localName", "")
                lr = port.get("localReference", "")
                if "PowerPlant" in itypes:
                    self._pp_total_slots += 1
                    output_found = 0
                    pp_em = 0.0
                    pp_pr = None
                    # Try catalog lookup by localName
                    cat = self._lookup(ln) if ln else None
                    if cat:
                        output_found = float(cat.get("output", 0) or 0)
                        pp_em = float(cat.get("em_max", cat.get("em_idle", 0)) or 0)
                    # Try raw lookup by localName or localReference UUID
                    raw = self._lookup_raw(ln) if ln else None
                    if not raw and lr:
                        raw = self._lookup_raw(lr)
                    if raw:
                        res = raw.get("resource", {}).get("online", {})
                        gen = res.get("generation", {})
                        if not output_found:
                            output_found = float(gen.get("powerSegment", 0) or 0)
                        # Get EM from raw if not from catalog
                        sig = res.get("signatureParams", {})
                        raw_em = float((sig.get("em", {}) or {}).get("nominalSignature", 0) or 0)
                        if raw_em:
                            pp_em = raw_em
                        # Get powerRanges
                        pr_data = res.get("powerRanges", {})
                        if pr_data:
                            pp_pr = []
                            for rk in ("low", "medium", "high"):
                                rd = pr_data.get(rk, {})
                                if rd:
                                    pp_pr.append({"start": rd.get("start", 0), "modifier": rd.get("modifier", 1)})
                    if output_found > 0:
                        self._total_pp_output += output_found
                        self._pp_count += 1
                        self._pp_em_total += pp_em
                        self._pp_power_ranges.append(pp_pr)
                _count_pp(port.get("loadout", []))
        _count_pp(loadout)

        # ── 4-8. Walk loadout for shields, coolers, radar, life support, QD, utility ──
        POWERED_TYPES = {
            "Shield": "shield",
            "Cooler": "cooler",
            "Radar": "radar",
            "LifeSupportGenerator": "lifeSupport",
            "QuantumDrive": "quantumDrive",
        }
        # Utility types: mining, salvage, tractor beam, utility turrets
        UTILITY_TYPES = {"MiningLaser", "SalvageHead", "TractorBeam", "UtilityTurret", "ToolArm"}

        # Skip patterns for non-powered placeholder components (blanking plates,
        # missile rack caps, blade racks, fuel intakes) that have no catalog entry.
        _SKIP_SUBSTRINGS = ("_cap", "blanking", "blade_rack", "missilerack_blade",
                            "missile_cap", "fuel_intake", "intk_", "_remote_top_turret")

        def _walk_powered(ports):
            for port in (ports if isinstance(ports, list) else []):
                itypes = [it.get("type","") for it in port.get("itemTypes", [])]
                ln = port.get("localName", "")
                lr = port.get("localReference", "")
                # Identifier: prefer localName, fall back to localReference UUID
                ident = ln or lr

                # Skip known non-powered placeholders
                if ident and any(s in ident.lower() for s in _SKIP_SUBSTRINGS):
                    _walk_powered(port.get("loadout", []))
                    continue

                for pt in itypes:
                    # Check utility types
                    if pt in UTILITY_TYPES and ident:
                        catalog = self._lookup(ln) if ln else None
                        if not catalog and lr:
                            # Try raw lookup by UUID
                            raw = self._lookup_raw(lr)
                            if raw:
                                catalog = {"name": raw.get("name", pt), "power_draw": 1.0,
                                           "em_max": 0, "ir_max": 0}
                        name = catalog.get("name", ident) if catalog else ident
                        draw = float(catalog.get("power_draw", 0) or 0) if catalog else 1.0
                        em = float(catalog.get("em_max", 0) or 0) if catalog else 0
                        seg = max(1, int(draw)) if draw > 0 else 1
                        _add_slot(name, "utility", seg, seg, draw / seg if seg else 0,
                                  em_per_seg=em / seg if seg else 0, em_total=em)
                        break

                    # Check powered types
                    cat_key = POWERED_TYPES.get(pt)
                    if cat_key and ident:
                        # Try localName first, then localReference UUID
                        catalog = self._lookup(ln) if ln else None
                        if not catalog and lr:
                            raw = self._lookup_raw(lr)
                            if raw:
                                res = raw.get("resource", {}).get("online", {})
                                cons = res.get("consumption", {})
                                sig = res.get("signatureParams", {})
                                em_d = (sig.get("em", {}) or {}).get("nominalSignature", 0)
                                ir_d = (sig.get("ir", {}) or {}).get("nominalSignature", 0)
                                draw_seg = cons.get("powerSegment", cons.get("power", 0))
                                catalog = {
                                    "name": raw.get("name", ident[:20]),
                                    "power_draw": float(draw_seg or 0),
                                    "em_max": float(em_d or 0),
                                    "ir_max": float(ir_d or 0),
                                }
                        # Fallback: broad search across ALL raw cache endpoints
                        # Covers placeholder/fake components (e.g. radr_s02_fake)
                        # and UUID-only refs that standard lookups miss.
                        if not catalog and ln:
                            raw = self._lookup_raw(ln)  # searches all endpoints by localName
                            if raw:
                                res = raw.get("resource", {}).get("online", {})
                                cons = res.get("consumption", {})
                                sig = res.get("signatureParams", {})
                                em_d = (sig.get("em", {}) or {}).get("nominalSignature", 0)
                                ir_d = (sig.get("ir", {}) or {}).get("nominalSignature", 0)
                                draw_seg = cons.get("powerSegment", cons.get("power", 0))
                                catalog = {
                                    "name": raw.get("name", ln),
                                    "power_draw": float(draw_seg or 0),
                                    "em_max": float(em_d or 0),
                                    "ir_max": float(ir_d or 0),
                                }
                        # Final fallback: placeholder/fake components (e.g. radr_s02_fake)
                        # that don't exist in any catalog or raw cache — treat as 1-segment dummy.
                        if not catalog and ident and ("_fake" in ident or "fake_" in ident):
                            catalog = {
                                "name": ident,
                                "power_draw": 1.0,
                                "em_max": 0,
                                "ir_max": 0,
                            }
                        if catalog:
                            name = catalog.get("name", ln)
                            draw = float(catalog.get("power_draw", 0) or 0)
                            em = float(catalog.get("em_max", 0) or 0)
                            ir = float(catalog.get("ir_max", 0) or 0)

                            # QD special case: power_draw=0 in catalog stats but
                            # raw erkul data has consumption.power (2-3 segments).
                            # Look up from raw cache if draw is 0 for QD.
                            if cat_key == "quantumDrive" and draw == 0:
                                raw = self._lookup_raw(ln)
                                if raw:
                                    raw_res = raw.get("resource", {}).get("online", {})
                                    raw_cons = raw_res.get("consumption", {})
                                    draw = float(raw_cons.get("power", raw_cons.get("powerSegment", 0)) or 0)
                                    raw_sig = raw_res.get("signatureParams", {})
                                    raw_em = (raw_sig.get("em", {}) or {}).get("nominalSignature", 0)
                                    if raw_em:
                                        em = float(raw_em)
                                if draw == 0:
                                    draw = 2  # fallback: QDs typically draw 2-3 segments

                            seg = max(1, int(draw)) if draw > 0 else 0
                            if seg > 0:
                                is_qd = cat_key == "quantumDrive"
                                # Get powerRanges from raw data
                                pr = None
                                raw_pr = self._lookup_raw(ident)
                                if raw_pr:
                                    pr_data = raw_pr.get("resource", {}).get("online", {}).get("powerRanges", {})
                                    if pr_data:
                                        pr = []
                                        for rk in ("low", "medium", "high"):
                                            rd = pr_data.get(rk, {})
                                            if rd:
                                                pr.append({"start": rd.get("start", 0), "modifier": rd.get("modifier", 1)})
                                _add_slot(name, cat_key, seg, seg,
                                          draw / seg,
                                          em_per_seg=em / seg if seg else 0,
                                          ir_per_seg=ir / seg if seg else 0,
                                          em_total=em, ir_total=ir,
                                          power_ranges=pr)
                                # Set cooling generation for cooler slots
                                if cat_key == "cooler":
                                    raw_c = self._lookup_raw(ident)
                                    if raw_c:
                                        cg = raw_c.get("resource", {}).get("online", {}).get("generation", {}).get("cooling", 0)
                                        self._slots[-1]["cooling_gen"] = float(cg or 0)
                                    else:
                                        # Fallback: use catalog cooling_rate if available
                                        self._slots[-1]["cooling_gen"] = float(catalog.get("cooling_rate", 0) or 0) / 1000
                                if is_qd:
                                    # QD starts OFF in SCM mode
                                    self._slots[-1]["enabled"] = False
                                    self._slots[-1]["current_seg"] = 0
                        break

                _walk_powered(port.get("loadout", []))

        _walk_powered(loadout)

        # ── Also check items section for life support + utilities ──
        items_dict = ship_data.get("items", {})
        if isinstance(items_dict, dict):
            for grp_name, grp_list in items_dict.items():
                if not isinstance(grp_list, list):
                    continue
                for item in grp_list:
                    if not isinstance(item, dict):
                        continue
                    idata = item.get("data", {})
                    itype = idata.get("type", "")

                    if itype == "LifeSupportGenerator" and "lifeSupport" not in self._categories:
                        res = idata.get("resource", {}).get("online", {})
                        cons = res.get("consumption", {})
                        seg = cons.get("powerSegment", 0)
                        sig = res.get("signatureParams", {})
                        em = (sig.get("em", {}) or {}).get("nominalSignature", 0)
                        name = idata.get("name", "Life Support")
                        if seg and seg > 0:
                            _add_slot(name, "lifeSupport", int(seg), int(seg),
                                      1.0, em_per_seg=float(em)/seg if seg else 0,
                                      em_total=float(em))

                    elif itype in ("SalvageHead", "MiningLaser", "TractorBeam"):
                        res = idata.get("resource", {}).get("online", {})
                        cons = res.get("consumption", {})
                        seg = cons.get("powerSegment", cons.get("power", 0))
                        sig = res.get("signatureParams", {})
                        em = (sig.get("em", {}) or {}).get("nominalSignature", 0)
                        name = idata.get("name", itype)
                        if seg and seg > 0:
                            _add_slot(name, "utility", int(seg), int(seg),
                                      1.0, em_per_seg=float(em)/seg if seg else 0,
                                      em_total=float(em))

        # ── Check rnPowerPools for utility pools not yet accounted for ──
        if "utility" not in self._categories:
            util_pool_keys = ("tractorBeam", "towingBeam", "weaponMining", "salvageHead")
            has_util_pool = any(
                rn_pools.get(k, {}).get("type") in ("dynamic", "fixed")
                for k in util_pool_keys
            )
            if has_util_pool:
                # Ship has utility power pools — add a utility slot
                # Use pool size if fixed, otherwise default 2 segments
                for k in util_pool_keys:
                    pool = rn_pools.get(k, {})
                    if pool.get("type") == "fixed" and pool.get("poolSize", 0) > 0:
                        _add_slot(k.replace("weapon", "").title(), "utility",
                                  pool["poolSize"], pool["poolSize"], 1.0)
                        break
                else:
                    # Dynamic pools — create a generic 2-segment utility slot
                    _add_slot("Utility", "utility", 2, 0, 1.0)

        # ── Power budget distribution ──
        # If total segment demand > PP output, scale default segments proportionally
        # This matches erkul's default allocation behavior
        total_demand = sum(s["max_segments"] for s in self._slots if not s["is_generator"])
        pp_output = self._total_pp_output
        if total_demand > 0 and pp_output > 0 and total_demand > pp_output:
            scale = pp_output / total_demand
            for s in self._slots:
                if s["is_generator"]:
                    continue
                scaled = max(1, int(s["max_segments"] * scale))
                s["default_seg"] = min(scaled, s["max_segments"])
                s["current_seg"] = s["default_seg"]

        self._rebuild_columns()
        self._recalculate()

    def set_level_by_type(self, category: str, slot_idx: int, level: int):
        """Set a slot's pip level by category key and index within that category."""
        slots = self._categories.get(category, [])
        if 0 <= slot_idx < len(slots):
            s = slots[slot_idx]
            s["current_seg"] = max(0, min(level, s["max_segments"]))
            self._recalculate()

    def toggle_by_type(self, category: str, slot_idx: int):
        """Toggle a slot's enabled state by category key and index."""
        slots = self._categories.get(category, [])
        if 0 <= slot_idx < len(slots):
            slots[slot_idx]["enabled"] = not slots[slot_idx]["enabled"]
            self._recalculate()

    def set_mode(self, mode: str):
        """Switch between 'SCM' and 'NAV' flight modes.

        SCM mode: weapons + shields ON, QD OFF
        NAV mode: weapons + shields OFF, QD ON
        """
        mode = mode.upper()
        if mode not in ("SCM", "NAV") or mode == self._mode:
            return
        self._mode = mode
        self._update_mode_buttons()

        if mode == "NAV":
            # Turn OFF weapons and shields, turn ON QD
            for cat_key in ("weaponGun", "shield"):
                for slot in self._categories.get(cat_key, []):
                    slot["enabled"] = False
                    slot["current_seg"] = 0
            for slot in self._categories.get("quantumDrive", []):
                slot["enabled"] = True
                slot["current_seg"] = slot["max_segments"]
        else:  # SCM
            # Turn ON weapons and shields, turn OFF QD
            for cat_key in ("weaponGun", "shield"):
                for slot in self._categories.get(cat_key, []):
                    slot["enabled"] = True
                    slot["current_seg"] = slot["default_seg"]
            for slot in self._categories.get("quantumDrive", []):
                slot["enabled"] = False
                slot["current_seg"] = 0

        self._recalculate()

    # ── internal: recalculate ────────────────────────────────────────────────

    @staticmethod
    def _find_range_modifier(power_ranges, segment_count):
        """Erkul findRangeObject: find which power range applies for a segment count.
        power_ranges = [{"start": N, "modifier": M}, ...] sorted by start.
        Returns the modifier for the range where segment_count >= start < next_start.
        """
        if not power_ranges:
            return 1.0
        result = 1.0
        for i, rng in enumerate(power_ranges):
            start = rng.get("start", 0)
            next_start = power_ranges[i + 1]["start"] if i + 1 < len(power_ranges) else float("inf")
            if segment_count >= start and segment_count < next_start:
                result = rng.get("modifier", 1.0)
                break
        return result

    def _recalculate(self):
        """Recompute totals using erkul-exact formulas and redraw all visuals."""
        total_capacity = getattr(self, "_total_pp_output", 0)
        total_draw     = 0.0
        em_sig         = 0.0
        ir_sig         = 0.0

        pp_online = getattr(self, "_pp_count", 0)
        pp_total  = getattr(self, "_pp_total_slots", 0)
        armor_em  = getattr(self, "_armor_sig_em", 1.0)
        armor_ir  = getattr(self, "_armor_sig_ir", 1.0)
        armor_cs  = getattr(self, "_armor_sig_cs", 1.0)

        # ── Step 1: Cooling generation (from coolers, scaled by powerRange) ──
        cooling_gen = 0.0
        for s in self._slots:
            if s["category"] != "cooler" or not s["enabled"] or s["current_seg"] <= 0:
                continue
            raw_gen = s.get("cooling_gen", 0)
            if raw_gen and s["max_segments"] > 0:
                # erkul: (segment / consumption.powerSegment) × generation.cooling × modifier
                frac = s["current_seg"] / s["max_segments"]
                modifier = self._find_range_modifier(s.get("power_ranges"), s["current_seg"])
                cooling_gen += raw_gen * frac * modifier

        # ── Step 2: Cooling consumption (erkul formula) ──
        # Part 1 (h): sum of all active segments (including cooler segments)
        cooling_cons = 0.0
        for s in self._slots:
            if not s["enabled"] or s["current_seg"] <= 0 or s["is_generator"]:
                continue
            cooling_cons += s["current_seg"]
        # Part 2: shields, lsp, radar, QD add EXTRA = segment × powerRangeModifier
        for s in self._slots:
            if not s["enabled"] or s["current_seg"] <= 0 or s["is_generator"]:
                continue
            if s["category"] in ("shield", "lifeSupport", "radar", "quantumDrive"):
                modifier = self._find_range_modifier(s.get("power_ranges"), s["current_seg"])
                cooling_cons += s["current_seg"] * modifier

        cooling_ratio = min(1.0, cooling_cons / cooling_gen) if cooling_gen > 0 else 0.5

        # ── Step 3: Power draw ──
        for s in self._slots:
            if not s["enabled"] or s["current_seg"] <= 0 or s["is_generator"]:
                continue
            total_draw += s["draw_per_seg"] * s["current_seg"]

        # ── Step 4: EM signature (erkul-exact formula) ──
        # ppUsageRatio = sum of all non-generator active segments / totalAvailablePowerSegments
        total_active_seg = sum(
            s["current_seg"] for s in self._slots
            if s["enabled"] and s["current_seg"] > 0 and not s["is_generator"])
        pp_usage_ratio = min(1.0, total_active_seg / total_capacity) if total_capacity > 0 else 0

        # Power plant EM (not in slots — tracked separately)
        # erkul: sum(pp.em × modifier) × ppUsageRatio
        pp_em = getattr(self, "_pp_em_total", 0)
        pp_ranges = getattr(self, "_pp_power_ranges", [])
        pp_count = getattr(self, "_pp_count", 0)
        if pp_em and pp_count > 0:
            # PP segment per plant = totalOutput × ppUsageRatio / num_plants
            pp_seg_per = (total_capacity * pp_usage_ratio / pp_count) if pp_count else 0
            pp_modifier = 1.0
            if pp_ranges and pp_ranges[0]:
                pp_modifier = self._find_range_modifier(pp_ranges[0], pp_seg_per)
            em_sig += pp_em * pp_modifier * pp_usage_ratio

        # Component EM
        for s in self._slots:
            if not s["enabled"] or s["current_seg"] <= 0 or s["is_generator"]:
                continue

            cat = s["category"]
            em_total = s.get("em_total", 0)
            if not em_total:
                continue

            frac = s["current_seg"] / s["max_segments"] if s["max_segments"] else 0
            modifier = self._find_range_modifier(s.get("power_ranges"), s["current_seg"])

            if cat == "weaponGun":
                # Weapons: sum(em) × (usage/100) — weapon usage = pips/pool fraction
                em_sig += em_total * frac
            elif cat in ("shield", "cooler", "lifeSupport", "radar", "quantumDrive"):
                # em × selectedFraction × powerRangeModifier
                em_sig += em_total * frac * modifier
            else:
                # Other (thruster, utility): fraction-based
                em_sig += em_total * frac

        em_sig *= armor_em

        # ── Step 5: IR signature (erkul: only from coolers) ──
        for s in self._slots:
            if s["category"] != "cooler" or not s["enabled"] or s["current_seg"] <= 0:
                continue
            ir_total = s.get("ir_total", 0)
            if not ir_total:
                continue
            frac = s["current_seg"] / s["max_segments"] if s["max_segments"] else 0
            modifier = self._find_range_modifier(s.get("power_ranges"), s["current_seg"])
            ir_sig += ir_total * frac * cooling_ratio * modifier

        ir_sig *= armor_ir

        # ── Step 6: CS signature (fixed per hull × armor multiplier) ──
        cs_sig = getattr(self, "_ship_cs", 0) * armor_cs

        consumption_pct = (total_draw / total_capacity * 100) if total_capacity > 0 else 0

        # Store as attributes so parent can read them via callback
        self.em_signature = em_sig
        self.ir_signature = ir_sig
        self.cs_signature = cs_sig

        # Format signatures with K suffix
        def _fmt_sig(val):
            if val >= 1000:
                return f"{val/1000:.1f}K"
            return f"{val:.0f}"

        # Update header labels
        self._lbl_em.config(text=_fmt_sig(em_sig))
        self._lbl_ir.config(text=_fmt_sig(ir_sig))
        self._lbl_cs.config(text=_fmt_sig(cs_sig))
        self._lbl_output.config(text=f"{pp_online} / {int(total_capacity)}")
        self._lbl_draw.config(text=f"{total_draw:.0f} / {total_capacity:.0f}")
        pct_text = f"{consumption_pct:.0f}%"
        self._lbl_pct.config(text=pct_text)

        # Consumption bar color
        if consumption_pct > 100:
            bar_color = RED
        elif consumption_pct >= 80:
            bar_color = YELLOW
        else:
            bar_color = GREEN

        self._draw_consumption_bar(consumption_pct, bar_color)

        # Redraw all pip canvases
        for canvas, slot in self._canvases:
            self._draw_pip_bar(canvas, slot)

        # Store computed ratios for parent to read
        # Weapon ratio = current_seg / max_segments for weapon slots
        # This represents how much of the weapon power budget is allocated
        wpn_slots = self._categories.get("weaponGun", [])
        if wpn_slots:
            wpn_current = sum(s["current_seg"] for s in wpn_slots if s["enabled"])
            wpn_max = sum(s["max_segments"] for s in wpn_slots)
            self.weapon_power_ratio = wpn_current / wpn_max if wpn_max > 0 else 0.0
            # If weapons are disabled (NAV mode), ratio = 0
            if not any(s["enabled"] for s in wpn_slots):
                self.weapon_power_ratio = 0.0
        else:
            self.weapon_power_ratio = 1.0

        # Shield ratio = current_seg / max_segments for shield slots
        shd_slots = self._categories.get("shield", [])
        if shd_slots:
            shd_current = sum(s["current_seg"] for s in shd_slots if s["enabled"])
            shd_max = sum(s["max_segments"] for s in shd_slots)
            self.shield_power_ratio = shd_current / shd_max if shd_max > 0 else 0.0
            if not any(s["enabled"] for s in shd_slots):
                self.shield_power_ratio = 0.0
        else:
            self.shield_power_ratio = 1.0

        # Also factor in overconsumption: if total draw > capacity, scale everything down
        if total_capacity > 0 and total_draw > total_capacity:
            overload_factor = total_capacity / total_draw
            self.weapon_power_ratio *= overload_factor
            self.shield_power_ratio *= overload_factor

        # Notify parent of changes
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    # ── internal: UI construction ────────────────────────────────────────────

    def _build_static_ui(self):
        """Build the static layout (header, column frame, mode toggle)."""
        # Header row: signatures + consumption
        hdr = tk.Frame(self, bg=HEADER_BG)
        hdr.pack(fill="x", padx=4, pady=(4, 2))

        sig_frame = tk.Frame(hdr, bg=HEADER_BG)
        sig_frame.pack(side="left", padx=6)

        for icon, color, attr in [
            ("\u26a1", ENERGY_COL, "_lbl_em"),
            ("\U0001f525", THERM_COL, "_lbl_ir"),
            ("\u25ce", PHYS_COL, "_lbl_cs"),
        ]:
            tk.Label(sig_frame, text=icon, bg=HEADER_BG, fg=color,
                     font=("Segoe UI", 9)).pack(side="left")
            lbl = tk.Label(sig_frame, text="0", bg=HEADER_BG, fg=FG,
                           font=("Consolas", 9))
            lbl.pack(side="left", padx=(0, 8))
            setattr(self, attr, lbl)

        # Output label
        self._lbl_output = tk.Label(hdr, text="0 pwr", bg=HEADER_BG, fg=GREEN,
                                    font=("Consolas", 9, "bold"))
        self._lbl_output.pack(side="left", padx=(10, 4))

        # Consumption bar area
        cons_frame = tk.Frame(hdr, bg=HEADER_BG)
        cons_frame.pack(side="right", padx=6)

        self._lbl_pct = tk.Label(cons_frame, text="0%", bg=HEADER_BG, fg=FG,
                                 font=("Consolas", 9, "bold"))
        self._lbl_pct.pack(side="right", padx=(4, 0))

        self._consumption_canvas = tk.Canvas(cons_frame, width=120, height=10,
                                             bg=BG4, highlightthickness=0)
        self._consumption_canvas.pack(side="right")

        self._lbl_draw = tk.Label(cons_frame, text="0 / 0", bg=HEADER_BG, fg=FG_DIM,
                                  font=("Consolas", 8))
        self._lbl_draw.pack(side="right", padx=(0, 6))

        # Column grid frame (populated by _rebuild_columns)
        self._col_frame = tk.Frame(self, bg=BG2)
        self._col_frame.pack(fill="both", expand=True, padx=4, pady=2)

        # Category icon row (populated by _rebuild_columns)
        self._icon_row = tk.Frame(self, bg=BG2)
        self._icon_row.pack(fill="x", padx=4)

        # SCM / NAV toggle
        mode_frame = tk.Frame(self, bg=BG2)
        mode_frame.pack(fill="x", padx=4, pady=(2, 4))

        self._btn_scm = tk.Button(mode_frame, text="SCM", width=6,
                                  bg=ACCENT, fg=BG, relief="flat",
                                  font=("Segoe UI", 8, "bold"),
                                  command=lambda: self.set_mode("SCM"))
        self._btn_scm.pack(side="left", padx=(0, 2))

        self._btn_nav = tk.Button(mode_frame, text="NAV", width=6,
                                  bg=BG4, fg=FG_DIM, relief="flat",
                                  font=("Segoe UI", 8, "bold"),
                                  command=lambda: self.set_mode("NAV"))
        self._btn_nav.pack(side="left")

    def _update_mode_buttons(self):
        if self._mode == "SCM":
            self._btn_scm.config(bg=ACCENT, fg=BG)
            self._btn_nav.config(bg=BG4, fg=FG_DIM)
        else:
            self._btn_scm.config(bg=BG4, fg=FG_DIM)
            self._btn_nav.config(bg=ACCENT, fg=BG)

    def _rebuild_columns(self):
        """Destroy and recreate the column grid from current categories.
        Each category gets a single combined column containing:
          - Category label at top
          - Pip bar canvases in the middle
          - Icon at bottom (clickable to toggle category)
        This ensures bars and icons are always vertically aligned.
        """
        for w in self._col_frame.winfo_children():
            w.destroy()
        for w in self._icon_row.winfo_children():
            w.destroy()
        self._canvases.clear()

        for cat_key, label, icon, color in self.CATEGORY_ORDER:
            slots = self._categories.get(cat_key, [])
            if not slots:
                continue

            # Column structure (top to bottom):
            #   1. Label (top)
            #   2. Spacer (fills vertical space, pushes bars down)
            #   3. Bars (tightly stacked, bottom-aligned)
            #   4. Icon (very bottom)
            col = tk.Frame(self._col_frame, bg=BG2)
            col.pack(side="left", anchor="s", padx=1, fill="y")

            # 1. Category label
            tk.Label(col, text=label, bg=BG2, fg=color,
                     font=("Consolas", 6, "bold")).pack(side="top", pady=(0, 1))

            # 4. Icon at very bottom (pack first so it claims bottom space)
            icon_lbl = tk.Label(col, text=icon, bg=BG2, fg=color,
                                font=("Segoe UI", 9), cursor="hand2")
            icon_lbl.pack(side="bottom", pady=(1, 0))
            icon_lbl.bind("<Button-1>",
                          lambda e, ck=cat_key: self._toggle_category(ck))
            icon_lbl.bind("<Enter>",
                          lambda e, lbl=icon_lbl: lbl.configure(bg="#2a3050"))
            icon_lbl.bind("<Leave>",
                          lambda e, lbl=icon_lbl, c=color: lbl.configure(bg=BG2))

            # 3. Bars stacked tightly above icon
            # Pack each canvas with side="bottom" so they stack upward from icon
            # Use consistent 1px gap between ALL bars regardless of category
            for si, slot in enumerate(slots):
                max_seg = slot["max_segments"]
                canvas_h = max_seg * (self.PIP_H + self.PIP_GAP)
                if canvas_h < 9:
                    canvas_h = 9  # minimum 1 pip visible

                c = tk.Canvas(col, width=self.PIP_W, height=canvas_h,
                              bg=BG3, highlightthickness=0, bd=0,
                              cursor="hand2")
                c.configure(width=self.PIP_W, height=canvas_h)
                # Exactly 1px gap between bars (same for all columns)
                c.pack(side="bottom", pady=(0, 1) if si < len(slots)-1 else 0)
                c.bind("<Button-1>", lambda e, s=slot, cv=c: self._on_pip_click(e, s, cv))
                c.bind("<Button-3>", lambda e, s=slot: self._on_right_click(s))
                self._canvases.append((c, slot))

    # ── internal: drawing ────────────────────────────────────────────────────

    def _draw_pip_bar(self, canvas: tk.Canvas, slot: dict):
        """Redraw a single pip bar on its canvas."""
        canvas.delete("all")
        max_seg    = slot["max_segments"]
        current    = slot["current_seg"]
        default    = slot["default_seg"]
        enabled    = slot["enabled"]
        w          = self.PIP_W
        pip_h      = self.PIP_H
        gap        = self.PIP_GAP

        for i in range(max_seg):
            seg_idx = max_seg - 1 - i  # draw top-down, index 0 = bottom
            y = i * (pip_h + gap)

            if not enabled:
                fill = self.GREY_PIP
            elif seg_idx < current and seg_idx < default:
                fill = self.GREEN_PIP
            elif seg_idx < current and seg_idx >= default:
                fill = self.ORANGE_PIP
            else:
                fill = self.DARK_PIP

            canvas.create_rectangle(1, y, w - 1, y + pip_h, fill=fill, outline="")

    def _draw_consumption_bar(self, pct: float, color: str):
        """Redraw the consumption bar canvas."""
        c = self._consumption_canvas
        c.delete("all")
        bar_w = c.winfo_width() or 120
        bar_h = c.winfo_height() or 10
        fill_w = min(bar_w, bar_w * pct / 100)
        c.create_rectangle(0, 0, fill_w, bar_h, fill=color, outline="")

    # ── internal: interaction ────────────────────────────────────────────────

    def _on_pip_click(self, event, slot: dict, canvas: tk.Canvas):
        """Left-click on a pip: set the level to the clicked segment."""
        max_seg = slot["max_segments"]
        pip_h   = self.PIP_H + self.PIP_GAP
        total_h = canvas.winfo_height()
        clicked_row = int(event.y / pip_h) if pip_h else 0
        new_level   = max_seg - clicked_row  # top row = highest level
        slot["current_seg"] = max(0, min(new_level, max_seg))
        self._recalculate()

    def _on_right_click(self, slot: dict):
        """Right-click: toggle enabled state."""
        slot["enabled"] = not slot["enabled"]
        self._recalculate()

    def _toggle_category(self, cat_key: str):
        """Toggle all slots in a category on/off (click on category icon)."""
        slots = self._categories.get(cat_key, [])
        if not slots:
            return
        # If any slot is enabled, turn all off. Otherwise turn all on.
        any_on = any(s["enabled"] for s in slots)
        for s in slots:
            s["enabled"] = not any_on
            if s["enabled"]:
                s["current_seg"] = s["default_seg"]
            else:
                s["current_seg"] = 0
        self._recalculate()


# ── App (three-panel erkul.games layout) ──────────────────────────────────────

class DpsCalcApp:
    def __init__(self, x, y, w, h, opacity, cmd_file):
        self.cmd_file    = cmd_file
        self._data       = DataManager()
        self._ship_name: Optional[str] = None
        self._current_ship: Optional[dict] = None
        self._power_sim = False
        self._weapon_power_ratio = 1.0
        self._shield_power_ratio = 1.0
        self._flight_mode = "scm"
        self._ship_data: Optional[dict] = None
        self._pending_ship: Optional[str] = None

        # selections: section → { slot_id → component_name }
        self._sel: dict = {
            "weapons": {}, "missiles": {}, "defenses": {}, "components": {},
            "propulsion": {},
        }

        # UI refs: section → [(slot_dict, ComponentTable), ...]
        self._slot_tables: dict = {}
        # Legacy _rows kept for compat shim (voice commands that still reference it)
        self._rows: dict = {
            k: [] for k in ("weapons", "missiles", "defenses", "components", "propulsion")
        }
        self._cooler_rows:     list = []
        self._radar_rows:      list = []
        self._powerplant_rows: list = []
        self._qdrive_rows:     list = []
        self._thruster_rows:   list = []
        self._fy_groups: dict = {}

        # Panel content frames (filled on ship load)
        self._left_table:   Optional[tk.Frame] = None
        self._center_table: Optional[tk.Frame] = None

        self._build_ui(x, y, w, h, opacity)
        self._start_cmd_watcher()
        self._data.load(on_done=lambda: self.root.after(0, self._on_data_loaded))

    # ── UI (three-panel erkul.games layout) ───────────────────────────────────

    def _build_ui(self, x, y, w, h, opacity):
        self.root = tk.Tk()
        self.root.title("#DPS Calculator")
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.configure(bg=BG)
        self.root.attributes("-alpha", opacity)
        self.root.attributes("-topmost", True)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        for widget in ("TCombobox",):
            style.configure(widget,
                fieldbackground=BG4, background=BG4, foreground=FG,
                arrowcolor=ACCENT, selectbackground=BG4, selectforeground=FG,
                bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
            )
            style.map(widget,
                fieldbackground=[("readonly", BG4)],
                selectbackground=[("readonly", BG4)],
                foreground=[("readonly", FG)],
            )
        style.configure("TScrollbar", troughcolor=BG2, background=BORDER,
                         arrowcolor=FG_DIM)
        style.configure("TPanedwindow", background=BORDER)

        # ── Header bar (44px) ──────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=HEADER_BG, height=44)
        hdr.pack(fill="x", side="top")
        hdr.pack_propagate(False)

        tk.Label(hdr, text="#DPS", font=("Consolas", 13, "bold"),
                 bg=HEADER_BG, fg=ACCENT).pack(side="left", padx=(10, 0), pady=6)
        tk.Label(hdr, text="Calculator", font=("Consolas", 13),
                 bg=HEADER_BG, fg=FG).pack(side="left", padx=(2, 4))

        # LIVE badge
        self._version_var = tk.StringVar(value="LIVE")
        self._version_lbl = tk.Label(hdr, textvariable=self._version_var,
                                     font=("Consolas", 8, "bold"), bg="#1a3a2a",
                                     fg=GREEN, padx=4, pady=1)
        self._version_lbl.pack(side="left", padx=(0, 12))

        # Ship selector — fuzzy search entry with dropdown
        tk.Label(hdr, text="Ship", font=("Consolas", 9),
                 bg=HEADER_BG, fg=FG_DIM).pack(side="left", padx=(0, 4))
        self._ship_var = tk.StringVar(value="Loading…")
        self._ship_names: list = []
        self._ship_entry = tk.Entry(
            hdr, textvariable=self._ship_var, width=28,
            font=("Consolas", 9), bg=BG4, fg=FG,
            insertbackground="white", relief="flat",
            highlightthickness=1, highlightcolor=ACCENT,
            highlightbackground=BORDER, state="disabled")
        self._ship_entry.pack(side="left", padx=(0, 8))
        self._ship_popup = None
        self._ship_popup_sel = -1
        self._ship_var.trace_add("write", self._on_ship_search)
        self._ship_entry.bind("<Return>", self._on_ship_enter)
        self._ship_entry.bind("<Escape>", lambda e: self._close_ship_popup())
        self._ship_entry.bind("<Down>", lambda e: self._ship_popup_nav(1))
        self._ship_entry.bind("<Up>", lambda e: self._ship_popup_nav(-1))
        self._ship_entry.bind("<FocusOut>",
                               lambda e: self.root.after(150, self._close_ship_popup))
        # Keep reference for configure calls
        self._ship_cb = self._ship_entry

        # Right side buttons
        btn_s = dict(font=("Consolas", 8), bg=BG3, fg=FG_DIM, relief="flat",
                     cursor="hand2", bd=0, activebackground=BORDER,
                     activeforeground=FG, padx=6, pady=3)
        tk.Button(hdr, text="⟳ Refresh", command=self._do_refresh,
                  **btn_s).pack(side="right", padx=(0, 10))
        tk.Button(hdr, text="— Min", command=self.root.iconify,
                  **btn_s).pack(side="right", padx=(0, 4))

        # Patreon link for erkul
        patreon_lbl = tk.Label(
            hdr, text="Patreon: erkul", font=("Consolas", 8),
            bg=HEADER_BG, fg="#f96854", cursor="hand2")
        patreon_lbl.pack(side="right", padx=(0, 10))
        patreon_lbl.bind("<Button-1>",
                         lambda e: webbrowser.open("https://www.patreon.com/erkul"))
        patreon_lbl.bind("<Enter>", lambda e: patreon_lbl.configure(fg="#ff8a73"))
        patreon_lbl.bind("<Leave>", lambda e: patreon_lbl.configure(fg="#f96854"))

        self._status_var = tk.StringVar(value="Fetching data from erkul.games…")
        tk.Label(hdr, textvariable=self._status_var, font=("Consolas", 8),
                 bg=HEADER_BG, fg=FG_DIM).pack(side="right", padx=10)

        # ── Three-panel PanedWindow ────────────────────────────────────────
        self._paned = tk.PanedWindow(self.root, orient="horizontal",
                                     bg=BORDER, sashwidth=1, sashpad=0,
                                     opaqueresize=True)
        self._paned.pack(fill="both", expand=True)

        # Left panel (weapons + missiles)
        left_outer = tk.Frame(self._paned, bg=BG, width=420)
        self._paned.add(left_outer, minsize=300, stretch="always")
        self._left_canvas, self._left_table = self._make_scroll_panel(left_outer)

        # Center panel (two sub-tabs: Defenses/Systems + Power/Propulsion)
        center_outer = tk.Frame(self._paned, bg=BG, width=360)
        self._paned.add(center_outer, minsize=260, stretch="always")

        # ── Center sub-tab bar (32px) ──
        self._ctab_bar = tk.Frame(center_outer, bg=BG2, height=32)
        self._ctab_bar.pack(fill="x", side="top")
        self._ctab_bar.pack_propagate(False)
        self._ctab_active = 0  # 0 = Defenses, 1 = Power

        self._ctab_btns = []
        for idx, text in enumerate(["⊙  Defenses / Systems", "⚙  Power & Propulsion"]):
            btn = tk.Button(
                self._ctab_bar, text=text, font=("Consolas", 9, "bold"),
                relief="flat", bd=0, cursor="hand2", padx=12, pady=4,
                command=lambda i=idx: self._switch_center_tab(i),
            )
            btn.pack(side="left", fill="both", expand=True)
            self._ctab_btns.append(btn)

        # Two content containers — each gets its own scroll panel
        self._ctab_frames = []

        # Tab 0: Defenses / Systems
        cf0 = tk.Frame(center_outer, bg=BG)
        self._center_canvas_0, self._center_table_0 = self._make_scroll_panel(cf0)
        self._ctab_frames.append(cf0)

        # Tab 1: Power & Propulsion
        cf1 = tk.Frame(center_outer, bg=BG)
        self._center_canvas_1, self._center_table_1 = self._make_scroll_panel(cf1)
        self._ctab_frames.append(cf1)

        # Show tab 0 by default
        self._switch_center_tab(0)

        # Legacy alias for any code referencing _center_table
        self._center_canvas = self._center_canvas_0
        self._center_table = self._center_table_0

        # Right panel (ship info + stats)
        right_outer = tk.Frame(self._paned, bg=BG, width=380)
        self._paned.add(right_outer, minsize=280, stretch="always")
        self._right_canvas, self._right_table = self._make_scroll_panel(right_outer)
        self._build_right_panel_placeholder()

        # ── Footer ─────────────────────────────────────────────────────────
        footer = tk.Frame(self.root, bg=HEADER_BG, height=36)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)

        self._footer_vars: dict = {}
        row = tk.Frame(footer, bg=HEADER_BG)
        row.pack(fill="x", padx=10, pady=6)

        for key, label, color in [
            ("dps_raw", "DPS:",       GREEN),
            ("dps_sus", "Sustained:", YELLOW),
            ("alpha",   "Alpha:",     ACCENT),
            ("shld_hp", "Shield:",    DIST_COL),
            ("hull_hp", "Hull:",      PHYS_COL),
            ("cooling", "Cooling:",   CYAN),
        ]:
            tk.Label(row, text=label, font=("Consolas", 8), bg=HEADER_BG,
                     fg=FG_DIM).pack(side="left")
            var = tk.StringVar(value="—")
            self._footer_vars[key] = var
            tk.Label(row, textvariable=var, font=("Consolas", 9, "bold"),
                     bg=HEADER_BG, fg=color).pack(side="left", padx=(2, 12))

    # ── Scrollable panel helper ────────────────────────────────────────────

    def _make_scroll_panel(self, parent):
        """Create a scrollable frame inside parent. Returns (canvas, inner_frame)."""
        vbar = ttk.Scrollbar(parent, orient="vertical")
        vbar.pack(side="right", fill="y")
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0,
                           yscrollcommand=vbar.set)
        canvas.pack(fill="both", expand=True)
        vbar.configure(command=canvas.yview)
        table = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=table, anchor="nw")
        table.bind("<Configure>",
                   lambda e, c=canvas: c.configure(scrollregion=c.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e, c=canvas, wi=win: c.itemconfig(wi, width=e.width))
        canvas.bind("<MouseWheel>",
                    lambda e, c=canvas: c.yview_scroll(-1*(int(e.delta/120) or (1 if e.delta > 0 else (-1 if e.delta < 0 else 0))), "units"))
        # Propagate mousewheel to inner frames
        def _bind_wheel(widget):
            widget.bind("<MouseWheel>",
                        lambda e, c=canvas: c.yview_scroll(-1*(int(e.delta/120) or (1 if e.delta > 0 else (-1 if e.delta < 0 else 0))), "units"))
            for child in widget.winfo_children():
                _bind_wheel(child)
        table.bind("<Map>", lambda e, t=table: _bind_wheel(t))
        return canvas, table

    # ── Center sub-tab switching ────────────────────────────────────────────

    def _switch_center_tab(self, idx):
        """Show center sub-tab idx (0 or 1), hide the other."""
        self._ctab_active = idx
        for i, fr in enumerate(self._ctab_frames):
            if i == idx:
                fr.pack(fill="both", expand=True)
            else:
                fr.pack_forget()
        # Style tab buttons
        for i, btn in enumerate(self._ctab_btns):
            if i == idx:
                btn.configure(bg="#1e2840", fg=ACCENT,
                              activebackground="#1e2840", activeforeground=ACCENT)
            else:
                btn.configure(bg=BG2, fg=FG_DIM,
                              activebackground=BG2, activeforeground=FG_DIM)
        # Reset scroll to top
        canvas = self._center_canvas_0 if idx == 0 else self._center_canvas_1
        canvas.yview_moveto(0)

    # ── Compat shim: _build_scroll_table (used by old _rebuild_tab) ────────

    def _build_scroll_table(self, parent):
        canvas, table = self._make_scroll_panel(parent)
        placeholder = tk.Label(table, text="\n  Select a ship.",
                               font=("Consolas", 10), bg=BG, fg=FG_DIM,
                               anchor="w")
        placeholder.pack(fill="x", padx=10, pady=10)
        return table, (canvas, None, placeholder)

    # ── Right panel placeholder (before ship loaded) ─────────────────────────

    def _build_right_panel_placeholder(self):
        table = self._right_table
        tk.Label(table, text="Select a ship to view stats.",
                 font=("Consolas", 10), bg=BG, fg=FG_DIM,
                 anchor="w").pack(fill="x", padx=10, pady=20)

    # ── Right panel builder (ship info + stats) ───────────────────────────

    def _build_right_panel(self, ship: dict):
        table = self._right_table
        for w in table.winfo_children():
            w.destroy()
        self._ov_vars: dict = {}
        self._sig_vars: dict = {}   # signature bar variables

        def section(title, color=ACCENT):
            sh = tk.Frame(table, bg=SECT_HDR_BG)
            sh.pack(fill="x", pady=(6, 0))
            tk.Label(sh, text=f"  ■ {title}", font=("Consolas", 9, "bold"),
                     bg=SECT_HDR_BG, fg=color, anchor="w",
                     pady=4).pack(side="left")

        def stat_row(label, key, color=FG, font_size=9, bold=False):
            fr = tk.Frame(table, bg=BG)
            fr.pack(fill="x", padx=8, pady=1)
            tk.Label(fr, text=label, width=20, font=("Consolas", 8),
                     bg=BG, fg=FG_DIM, anchor="w").pack(side="left")
            var = tk.StringVar(value="—")
            self._ov_vars[key] = var
            f = ("Consolas", font_size, "bold") if bold else ("Consolas", font_size)
            tk.Label(fr, textvariable=var, font=f,
                     bg=BG, fg=color, anchor="w").pack(side="left")

        def big_stat(label, key, color, size=16):
            fr = tk.Frame(table, bg=BG)
            fr.pack(fill="x", padx=8, pady=2)
            var = tk.StringVar(value="—")
            self._ov_vars[key] = var
            tk.Label(fr, textvariable=var, font=("Consolas", size, "bold"),
                     bg=BG, fg=color, anchor="w").pack(side="left")
            tk.Label(fr, text=label, font=("Consolas", 9),
                     bg=BG, fg=FG_DIM, anchor="w").pack(side="left", padx=(4, 0))

        # ── Signature bar (IR / EM / CS) ──
        sig_bar = tk.Frame(table, bg=HEADER_BG, height=32)
        sig_bar.pack(fill="x", pady=(0, 2))
        sig_bar.pack_propagate(False)

        sig_inner = tk.Frame(sig_bar, bg=HEADER_BG)
        sig_inner.pack(expand=True)

        for sig_key, icon_text, icon_color, label in [
            ("ir",  "⫶",  THERM_COL,  "IR"),
            ("em",  "⚡", YELLOW,     "EM"),
            ("cs",  "◆",  ORANGE,     "CS"),
        ]:
            # Icon
            tk.Label(sig_inner, text=icon_text, font=("Consolas", 11, "bold"),
                     bg=HEADER_BG, fg=icon_color).pack(side="left", padx=(8, 2))
            # Value
            var = tk.StringVar(value="—")
            self._sig_vars[sig_key] = var
            tk.Label(sig_inner, textvariable=var, font=("Consolas", 11, "bold"),
                     bg=HEADER_BG, fg=FG).pack(side="left")
            # Separator (except after last)
            if sig_key != "cs":
                sep = tk.Frame(sig_inner, bg=BORDER, width=1, height=18)
                sep.pack(side="left", padx=8, fill="y")

        # Ship name header
        name = ship.get("name", "?")
        tk.Label(table, text=name, font=("Consolas", 14, "bold"),
                 bg=BG, fg=FG, anchor="w",
                 padx=8, pady=6).pack(fill="x")

        # ── Power Sim Toggle ──
        toggle_fr = tk.Frame(table, bg=BG)
        toggle_fr.pack(fill="x", padx=8, pady=(2, 2))

        self._power_sim = getattr(self, "_power_sim", False)

        self._raw_btn = tk.Button(
            toggle_fr, text="RAW", font=("Consolas", 9, "bold"),
            relief="flat", bd=0, cursor="hand2", padx=10, pady=3,
            command=lambda: self._set_power_mode(False))
        self._raw_btn.pack(side="left", padx=(0, 2))

        self._sim_btn = tk.Button(
            toggle_fr, text="\u26a1 POWER SIM", font=("Consolas", 9, "bold"),
            relief="flat", bd=0, cursor="hand2", padx=10, pady=3,
            command=lambda: self._set_power_mode(True))
        self._sim_btn.pack(side="left")

        self._update_power_toggle_style()

        # ── PowerAllocator widget (shown when POWER SIM active) ──
        # TODO: pre-build by_local_name index in DataManager for O(1) lookup
        def _item_lookup(local_name):
            """Look up component stats from erkul catalog by localName."""
            idx = self._data._idx  # snapshot read — consistent across all categories
            for cat_name, by_ref, by_name in [
                ("weapon", idx.weapons_by_ref, idx.weapons_by_name),
                ("shield", idx.shields_by_ref, idx.shields_by_name),
                ("cooler", idx.coolers_by_ref, idx.coolers_by_name),
                ("radar", idx.radars_by_ref, idx.radars_by_name),
                ("pp", idx.powerplants_by_ref, idx.powerplants_by_name),
                ("qd", idx.qdrives_by_ref, idx.qdrives_by_name),
            ]:
                for ref, stats in by_ref.items():
                    if stats.get("local_name") == local_name:
                        return stats
            return None

        def _raw_lookup(identifier):
            """Look up raw erkul data dict by localName or ref UUID."""
            raw = getattr(self._data, "raw", {}) or {}
            for ep_key, entries in raw.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if entry.get("localName") == identifier:
                        return entry.get("data", {})
                    # Also check by ref UUID
                    d = entry.get("data", {})
                    if d.get("ref") == identifier:
                        return d
            return None

        def _on_power_change():
            """Called when power allocator pips change — update DPS/shield/sig stats."""
            pa = getattr(self, "_power_allocator", None)
            if pa:
                self._weapon_power_ratio = getattr(pa, "weapon_power_ratio", 1.0)
                self._shield_power_ratio = getattr(pa, "shield_power_ratio", 1.0)

                em = getattr(pa, "em_signature", 0)
                ir = getattr(pa, "ir_signature", 0)

                def _fmt(v):
                    if not v: return "—"
                    if v >= 1000: return f"{v/1000:.1f}K"
                    return f"{v:.0f}"

                # Always update sig bars when allocator changes (regardless of mode)
                if hasattr(self, "_sig_vars"):
                    self._sig_vars["em"].set(_fmt(em))
                    self._sig_vars["ir"].set(_fmt(ir))

                if hasattr(self, "_ov_vars"):
                    if "sig_em" in self._ov_vars:
                        self._ov_vars["sig_em"].set(_fmt(em))
                    if "sig_ir" in self._ov_vars:
                        self._ov_vars["sig_ir"].set(_fmt(ir))

            if not self._power_sim:
                self._weapon_power_ratio = 1.0
                self._shield_power_ratio = 1.0
            self._update_footer()

        self._power_allocator = PowerAllocator(
            table, item_lookup_fn=_item_lookup, raw_lookup_fn=_raw_lookup,
            on_change=_on_power_change)
        if self._power_sim:
            self._power_allocator.pack(fill="x", padx=4, pady=(0, 4))

        # ── Ship placeholder (when NOT in power sim) ──
        self._ship_placeholder = tk.Frame(table, bg=BG3, height=60)
        if not self._power_sim:
            self._ship_placeholder.pack(fill="x", padx=8, pady=(0, 4))
            self._ship_placeholder.pack_propagate(False)
            tk.Label(self._ship_placeholder, text=name, font=("Consolas", 10),
                     bg=BG3, fg=FG_DIM).pack(expand=True)

        # ── Anchor for power allocator positioning ──
        self._stats_anchor = tk.Frame(table, bg=BG, height=0)
        self._stats_anchor.pack(fill="x")

        # ── Weapon DPS ──
        section("WEAPON DPS", GREEN)
        big_stat("dps", "dps_raw", GREEN, 16)
        big_stat("sustained", "dps_sus", YELLOW, 12)
        big_stat("alpha", "alpha", ACCENT, 12)
        big_stat("missile dmg", "missile_dmg", RED, 12)
        stat_row("Weapon slots:", "gun_slots")
        stat_row("Missile racks:", "miss_slots")

        # ── Shields ──
        section("SHIELDS", DIST_COL)
        big_stat("hp", "shld_hp", DIST_COL, 14)
        stat_row("Regen/s:", "shld_regen", GREEN)
        stat_row("Phys resist:", "shld_phys", PHYS_COL)
        stat_row("Energy resist:", "shld_enrg", ENERGY_COL)
        stat_row("Dist resist:", "shld_dist", DIST_COL)

        # ── Hull ──
        section("HULL", PHYS_COL)
        big_stat("hp", "hull_hp", PHYS_COL, 14)
        stat_row("Armor type:", "armor_type")
        stat_row("Phys dmg:", "armor_phys", PHYS_COL)
        stat_row("Energy dmg:", "armor_enrg", ENERGY_COL)
        stat_row("Dist dmg:", "armor_dist", DIST_COL)

        # ── Ship specs ──
        section("SHIP SPECS", FG)
        stat_row("Cargo (SCU):", "cargo")
        stat_row("Crew:", "crew")
        stat_row("SCM speed:", "scm_speed", GREEN)
        stat_row("AB speed:", "ab_speed", YELLOW)
        stat_row("QT speed:", "qt_speed")
        stat_row("H2 fuel:", "h2_fuel")
        stat_row("QT fuel:", "qt_fuel")

        # ── Power ──
        section("POWER", ORANGE)
        stat_row("Power output:", "pwr_output", ORANGE)
        stat_row("Power draw:", "pwr_draw", ENERGY_COL)
        stat_row("Power margin:", "pwr_margin", GREEN)

        # ── Cooling ──
        section("COOLING", CYAN)
        stat_row("Total cooling:", "cooling", CYAN)

        # ── Signatures ──
        section("SIGNATURES", YELLOW)
        stat_row("EM signature:", "sig_em", YELLOW)
        stat_row("IR signature:", "sig_ir", THERM_COL)
        stat_row("CS signature:", "sig_cs", ORANGE)

    # ── Section header helper ──────────────────────────────────────────────

    def _section_header(self, parent, title, type_color=ACCENT, reset_fn=None):
        """Render a section header bar with optional RESET button."""
        sh = tk.Frame(parent, bg=SECT_HDR_BG, height=28)
        sh.pack(fill="x", pady=(4, 0))
        sh.pack_propagate(False)
        tk.Label(sh, text=f"  ■ {title}", font=("Consolas", 9, "bold"),
                 bg=SECT_HDR_BG, fg=type_color, anchor="w").pack(side="left", fill="y")
        if reset_fn:
            tk.Button(sh, text="RESET", font=("Consolas", 7), bg=SECT_HDR_BG,
                      fg=ACCENT, relief="flat", bd=0, cursor="hand2",
                      activebackground=BORDER, activeforeground=FG,
                      padx=6, command=reset_fn).pack(side="right", padx=6)

    # ── Component card helper ──────────────────────────────────────────────

    def _make_card(self, parent, idx, size, slot_label, type_color):
        """
        Create a two-line component card with left accent stripe + size badge.
        Returns (content_frame, line1_frame, line2_frame).
        """
        bg = CARD_EVEN if idx % 2 == 0 else CARD_ODD

        outer = tk.Frame(parent, bg=CARD_BORDER)
        outer.pack(fill="x", padx=0, pady=0)

        inner = tk.Frame(outer, bg=bg)
        inner.pack(fill="x", padx=1, pady=1)

        # Left accent stripe
        stripe = tk.Frame(inner, bg=type_color, width=3)
        stripe.pack(side="left", fill="y")

        content = tk.Frame(inner, bg=bg, padx=6, pady=3)
        content.pack(side="left", fill="x", expand=True)

        # Line 1: size badge + slot label
        line1 = tk.Frame(content, bg=bg)
        line1.pack(fill="x")

        sz_bg = SIZE_COLORS.get(size, SIZE_COLORS[1])
        tk.Label(line1, text=f"S{size}", font=("Consolas", 8, "bold"),
                 bg=sz_bg, fg="white", width=3, padx=2).pack(side="left")
        tk.Label(line1, text=slot_label, font=("Consolas", 8),
                 bg=bg, fg=FG_DIM).pack(side="left", padx=(6, 0))

        # Line 2: caller fills this
        line2 = tk.Frame(content, bg=bg)
        line2.pack(fill="x", pady=(2, 0))

        return content, line1, line2, bg

    # ── Data loaded ────────────────────────────────────────────────────────────

    def _on_data_loaded(self):
        if self._data.error:
            self._status_var.set(f"Error: {self._data.error}")
            return
        names = self._data.get_ship_names()
        self._ship_names = names
        self._ship_entry.configure(state="normal")
        self._ship_var.set("")

        # Improvement A: show detailed counts in status bar
        nw = len(self._data.weapons_by_name)
        ns = len(self._data.shields_by_name)
        nc = len(self._data.coolers_by_name)
        nm = len(self._data.missiles_by_name)
        self._status_var.set(
            f"Ready — {len(names)} ships · {nw} weapons · {ns} shields "
            f"· {nc} coolers · {nm} missiles | erkul.games"
        )

        if self._pending_ship:
            self._ship_var.set(self._pending_ship)
            self._load_ship(self._pending_ship)
            self._pending_ship = None

        # BUG 6: fire one-shot version-check thread after data is ready
        self._start_version_check()

    # ── Fuzzy ship search popup ──────────────────────────────────────────────

    def _on_ship_search(self, *_args):
        """Called when the ship search entry text changes."""
        if not self._ship_names:
            return
        query = self._ship_var.get().strip().lower()
        if len(query) < 1:
            self._close_ship_popup()
            return
        # Fuzzy match: contains query anywhere in name
        matches = [n for n in self._ship_names if query in n.lower()]
        if not matches:
            self._close_ship_popup()
            return
        self._show_ship_popup(matches[:20])

    def _show_ship_popup(self, matches: list):
        """Show or update the ship search dropdown popup."""
        if self._ship_popup and self._ship_popup.winfo_exists():
            # Update existing popup
            for w in self._ship_popup_inner.winfo_children():
                w.destroy()
        else:
            self._ship_popup = tk.Toplevel(self.root)
            self._ship_popup.overrideredirect(True)
            self._ship_popup.configure(bg=BORDER)
            self._ship_popup.attributes("-topmost", True)
            self._ship_popup_inner = tk.Frame(self._ship_popup, bg=BG3)
            self._ship_popup_inner.pack(fill="both", expand=True, padx=1, pady=1)

        # Position below the entry
        ex = self._ship_entry.winfo_rootx()
        ey = self._ship_entry.winfo_rooty() + self._ship_entry.winfo_height()
        ew = self._ship_entry.winfo_width()
        row_h = 24
        popup_h = min(len(matches) * row_h, 400)
        self._ship_popup.geometry(f"{ew}x{popup_h}+{ex}+{ey}")

        self._ship_popup_sel = -1
        self._ship_popup_labels = []

        for i, name in enumerate(matches):
            bg = ROW_EVEN if i % 2 == 0 else ROW_ODD
            lbl = tk.Label(
                self._ship_popup_inner, text=f"  {name}", font=("Consolas", 9),
                bg=bg, fg=FG, anchor="w", padx=4, pady=1, cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, n=name: self._select_ship_from_popup(n))
            lbl.bind("<Enter>", lambda e, l=lbl: l.configure(bg=ACCENT, fg=BG))
            lbl.bind("<Leave>", lambda e, l=lbl, b=bg: l.configure(bg=b, fg=FG))
            self._ship_popup_labels.append((lbl, name, bg))

    def _close_ship_popup(self):
        if self._ship_popup and self._ship_popup.winfo_exists():
            self._ship_popup.destroy()
        self._ship_popup = None
        self._ship_popup_sel = -1

    def _select_ship_from_popup(self, name: str):
        self._close_ship_popup()
        # Temporarily unbind trace to avoid re-triggering search
        info = self._ship_var.trace_info()
        if info:
            self._ship_var.trace_remove("write", info[0][1])
        self._ship_var.set(name)
        self._ship_var.trace_add("write", self._on_ship_search)
        self._load_ship(name)

    def _on_ship_enter(self, event):
        """Handle Enter key in ship search — select first match or highlighted."""
        if self._ship_popup and self._ship_popup.winfo_exists() and self._ship_popup_labels:
            idx = max(0, self._ship_popup_sel)
            if idx < len(self._ship_popup_labels):
                _, name, _ = self._ship_popup_labels[idx]
                self._select_ship_from_popup(name)
        else:
            # Try exact match
            query = self._ship_var.get().strip()
            matches = [n for n in self._ship_names if query.lower() in n.lower()]
            if matches:
                self._select_ship_from_popup(matches[0])

    def _ship_popup_nav(self, delta: int):
        """Navigate up/down in the ship popup list."""
        if not self._ship_popup or not self._ship_popup.winfo_exists():
            return
        if not self._ship_popup_labels:
            return
        # Unhighlight current
        if 0 <= self._ship_popup_sel < len(self._ship_popup_labels):
            lbl, _, bg = self._ship_popup_labels[self._ship_popup_sel]
            lbl.configure(bg=bg, fg=FG)
        # Move
        self._ship_popup_sel += delta
        self._ship_popup_sel = max(0, min(self._ship_popup_sel,
                                          len(self._ship_popup_labels) - 1))
        # Highlight new
        lbl, _, _ = self._ship_popup_labels[self._ship_popup_sel]
        lbl.configure(bg=ACCENT, fg=BG)

    # ── Ship loading ───────────────────────────────────────────────────────────

    def _load_ship(self, name: str):
        ship = self._data.get_ship_data(name)
        if not ship:
            return
        self._ship_name = ship.get("name", name)
        self._ship_var.set(self._ship_name)
        self._sel = {k: {} for k in self._sel}

        loadout = ship.get("loadout") or ship.get("_fetched_loadout")

        if loadout:
            self._apply_ship_loadout(ship, loadout)
        else:
            # BUG 5 FIX: fetch per-ship loadout from /live/ships/{ref}/loadout
            # in a background thread so the UI stays responsive.
            ref = ship.get("ref", "")
            if ref:
                self._status_var.set(f"Loading {self._ship_name} loadout…")
                def _fetch_loadout(s=ship, r=ref):
                    result = []
                    try:
                        resp = requests.get(
                            f"{API_BASE}/live/ships/{r}/loadout",
                            headers=API_HEADERS, timeout=15,
                        )
                        try:
                            if resp.ok:
                                result = resp.json()
                                s["_fetched_loadout"] = result  # cache for session
                        finally:
                            resp.close()
                    except Exception:
                        pass
                    self.root.after(0, self._apply_ship_loadout, s, result)
                threading.Thread(target=_fetch_loadout, daemon=True).start()
            else:
                self._apply_ship_loadout(ship, [])

    def _apply_ship_loadout(self, ship: dict, loadout: list):
        """Populate all three panels from the resolved loadout."""
        self._ship_data = ship
        self._current_ship = ship
        self._slot_tables = {}  # clear all table refs

        # ── LEFT PANEL: weapons + missiles ──
        left = self._left_table
        for w in left.winfo_children():
            w.destroy()
        self._rows["weapons"] = []
        self._rows["missiles"] = []

        all_weapon_slots = extract_slots_by_type(loadout, {"WeaponGun", "Turret"})
        # Validate: clear local_ref if it doesn't resolve to an actual weapon
        # (filters tractor beams, special mounts, EMP devices, sensors, etc.)
        for s in all_weapon_slots:
            lr = s.get("local_ref", "")
            if lr:
                found = self._data.find_weapon(lr, max_size=s["max_size"])
                if not found:
                    found = self._data.find_weapon(lr)
                if not found:
                    s["local_ref"] = ""  # not a weapon — treat as empty slot
        gun_slots   = [s for s in all_weapon_slots if " / " not in s["label"]]
        turret_slots = [s for s in all_weapon_slots if " / " in s["label"]]
        self._rebuild_weapons_section(left, gun_slots, turret_slots)

        missile_slots_raw = extract_slots_by_type(loadout, {"MissileLauncher"})
        # Filter out gun slots that also have MissileLauncher type
        gun_ids = {s["id"] for s in gun_slots + turret_slots}
        missile_slots = [s for s in missile_slots_raw if s["id"] not in gun_ids]
        # Validate: clear local_ref if it doesn't resolve to an actual missile
        for s in missile_slots:
            lr = s.get("local_ref", "")
            if lr:
                found = self._data.find_missile(lr, max_size=s["max_size"])
                if not found:
                    found = self._data.find_missile(lr)
                if not found:
                    s["local_ref"] = ""  # not a missile — treat as empty slot
        self._rebuild_missiles_section(left, missile_slots)

        # ── CENTER PANEL TAB 0: Defenses / Systems (shields, coolers, radars) ──
        tab0 = self._center_table_0
        for w in tab0.winfo_children():
            w.destroy()
        self._rows["defenses"] = []
        self._rows["components"] = []
        self._cooler_rows = []
        self._radar_rows = []

        shield_slots = extract_slots_by_type(loadout, {"Shield"})
        self._rebuild_shields_section(tab0, shield_slots)

        cooler_slots = extract_slots_by_type(loadout, {"Cooler"})
        self._rebuild_coolers_section(tab0, cooler_slots, ship, loadout)

        radar_slots = extract_slots_by_type(loadout, {"Radar"})
        self._rebuild_radars_section(tab0, radar_slots, ship, loadout)

        # ── CENTER PANEL TAB 1: Power & Propulsion (PP, QD, thrusters) ──
        tab1 = self._center_table_1
        for w in tab1.winfo_children():
            w.destroy()
        self._powerplant_rows = []

        # PP placeholder (filled by FY data)
        self._pp_placeholder = tk.Frame(tab1, bg=BG)
        self._pp_placeholder.pack(fill="x")

        # QD placeholder (filled by FY data)
        self._qd_placeholder = tk.Frame(tab1, bg=BG)
        self._qd_placeholder.pack(fill="x")

        # Thruster placeholder (filled by FY data)
        self._thruster_placeholder = tk.Frame(tab1, bg=BG)
        self._thruster_placeholder.pack(fill="x")

        # Reset to Tab 0 on new ship
        self._switch_center_tab(0)

        # ── RIGHT PANEL: ship info + stats ──
        self._build_right_panel(ship)
        self._update_overview(ship)
        # Load power allocator with ship data
        if hasattr(self, "_power_allocator"):
            self._power_allocator.load_ship(ship)
        self._compute_power_stats(ship)
        self._update_footer()

        ship_name = ship.get("name", "?")
        self._status_var.set(f"Loaded: {ship_name} — fetching Fleetyards…")

        self._fy_groups = {}

        def _fy_done(groups: dict):
            self._fy_groups = groups
            self._rebuild_powerplants_section(groups)
            self._rebuild_qd_section(groups)
            self._rebuild_thrusters_section(groups)
            self._status_var.set(f"Loaded: {ship_name}")

        self._data.fetch_fy_hardpoints(
            ship_name,
            on_done=lambda g: self.root.after(0, _fy_done, g),
        )

    # ── Weapons tab builder (gun/turret split) ───────────────────────────────

    # ── Table-based section builders (three-panel layout) ────────────────────

    def _build_table_slot(self, parent, section_key, slot, list_fn, find_fn,
                          table_cols, type_color):
        """Build a slot header + ComponentTable. Returns ComponentTable."""
        sid    = slot["id"]
        max_sz = slot["max_size"] or 1

        # Compact slot header
        sh = tk.Frame(parent, bg=BG)
        sh.pack(fill="x", pady=(4, 0))
        sz_bg = SIZE_COLORS.get(max_sz, SIZE_COLORS[1])
        tk.Label(sh, text=f"S{max_sz}", font=("Consolas", 8, "bold"),
                 bg=sz_bg, fg="white", width=3, padx=2).pack(side="left", padx=(4, 0))
        tk.Label(sh, text=slot["label"], font=("Consolas", 8),
                 bg=BG, fg=FG_DIM).pack(side="left", padx=(6, 0))

        items = list_fn(max_sz)

        def _on_sel(item, _sid=sid, _key=section_key):
            name = item["name"] if item else ""
            self._sel[_key][_sid] = name
            self._update_footer()

        # Find stock component ref
        # Try with size constraint first; fall back to unconstrained
        # (stock loadout weapons are always valid for their slot,
        #  even if size seems wrong — e.g. S4 fixed on S3 gimbal port)
        stock_ref = ""
        if slot.get("local_ref"):
            st = find_fn(slot["local_ref"], max_size=max_sz)
            if not st:
                st = find_fn(slot["local_ref"])  # unconstrained fallback
            if st:
                stock_ref = st.get("ref", "")
                self._sel[section_key][sid] = st["name"]
                # Ensure stock weapon is in the items list even if size exceeds slot max
                if not any(i.get("ref") == stock_ref for i in items):
                    items = [st] + items

        tbl = ComponentTable(parent, table_cols, items, _on_sel,
                             current_ref=stock_ref, type_color=type_color,
                             max_rows=6)
        tbl.pack(fill="x")

        # Store for voice commands
        self._slot_tables.setdefault(section_key, []).append((slot, tbl, find_fn))
        return tbl

    def _rebuild_weapons_section(self, parent, gun_slots, turret_slots):
        """Build weapons + turrets sections in the left panel."""
        key = "weapons"
        all_slots = gun_slots + turret_slots
        if not all_slots:
            tk.Label(parent, text="  No weapon slots.",
                     font=("Consolas", 9), bg=BG, fg=FG_DIM).pack(fill="x", pady=8)
            return

        if gun_slots:
            self._section_header(parent, "WEAPONS", ENERGY_COL)
            for slot in gun_slots:
                self._build_table_slot(
                    parent, key, slot,
                    self._data.weapons_for_size, self._data.find_weapon,
                    WEAPON_TABLE_COLS, TYPE_STRIPE["WeaponGun"])

        if turret_slots:
            self._section_header(parent, "TURRETS", ENERGY_COL)
            for slot in turret_slots:
                self._build_table_slot(
                    parent, key, slot,
                    self._data.weapons_for_size, self._data.find_weapon,
                    WEAPON_TABLE_COLS, TYPE_STRIPE["WeaponGun"])

    def _rebuild_missiles_section(self, parent, slots):
        """Build missile racks section in the left panel."""
        key = "missiles"
        if not slots:
            return
        self._section_header(parent, "MISSILE & BOMB RACKS", RED)
        for slot in slots:
            self._build_table_slot(
                parent, key, slot,
                self._data.missiles_for_size, self._data.find_missile,
                MISSILE_TABLE_COLS, TYPE_STRIPE["MissileLauncher"])

    def _rebuild_shields_section(self, parent, slots):
        """Build shields section in the center panel."""
        key = "defenses"
        if not slots:
            return
        self._section_header(parent, "SHIELDS", DIST_COL)
        for slot in slots:
            self._build_table_slot(
                parent, key, slot,
                self._data.shields_for_size, self._data.find_shield,
                SHIELD_TABLE_COLS, TYPE_STRIPE["Shield"])

    def _rebuild_coolers_section(self, parent, slots, ship, loadout):
        """Build coolers section in the center panel."""
        if not slots:
            return
        self._section_header(parent, "COOLERS", CYAN,
                             reset_fn=lambda: self._reset_section("coolers"))
        for slot in slots:
            self._build_table_slot(
                parent, "components", slot,
                self._data.coolers_for_size, self._data.find_cooler,
                COOLER_TABLE_COLS, TYPE_STRIPE["Cooler"])

    def _rebuild_radars_section(self, parent, slots, ship, loadout):
        """Build radars section in the center panel."""
        if not slots:
            return
        self._section_header(parent, "RADARS", FG_DIM,
                             reset_fn=lambda: self._reset_section("radars"))
        for slot in slots:
            self._build_table_slot(
                parent, "components", slot,
                self._data.radars_for_size, self._data.find_radar,
                RADAR_TABLE_COLS, TYPE_STRIPE["Radar"])

    # ── FY-powered sections (power plants, QD, thrusters) ──────────────────

    def _rebuild_powerplants_section(self, groups: dict):
        """Fill PP placeholder in center panel using ComponentTable."""
        container = getattr(self, "_pp_placeholder", None)
        if not container:
            return
        for w in container.winfo_children():
            w.destroy()
        self._powerplant_rows = []

        pp_list = groups.get("power_plants", [])
        if not pp_list:
            return

        self._section_header(container, "POWER PLANTS", ORANGE,
                             reset_fn=lambda: self._reset_section("powerplants"))
        for i, hp in enumerate(pp_list):
            fy_st = compute_powerplant_stats(hp)
            sz = fy_st["size"]
            sid = f"pp_{i}"
            lbl = re.sub(r"hardpoint_", "", hp.get("name", f"PP {i+1}"), flags=re.I)
            lbl = lbl.replace("_", " ").title()

            # Slot header
            sh = tk.Frame(container, bg=BG)
            sh.pack(fill="x", pady=(4, 0))
            sz_bg = SIZE_COLORS.get(sz, SIZE_COLORS[1])
            tk.Label(sh, text=f"S{sz}", font=("Consolas", 8, "bold"),
                     bg=sz_bg, fg="white", width=3, padx=2).pack(side="left", padx=(4, 0))
            tk.Label(sh, text=lbl, font=("Consolas", 8),
                     bg=BG, fg=FG_DIM).pack(side="left", padx=(6, 0))

            items = self._data.powerplants_for_size(sz)

            def _on_sel(item, _sid=sid):
                name = item["name"] if item else ""
                self._sel["components"][_sid] = name
                self._update_footer()

            stock_ref = ""
            stock_name = fy_st["name"]
            if stock_name and stock_name != "—":
                st = self._data.find_powerplant(stock_name, max_size=sz)
                if st:
                    stock_ref = st.get("ref", "")
                    self._sel["components"][sid] = st["name"]

            tbl = ComponentTable(container, PP_COLS, items, _on_sel,
                                 current_ref=stock_ref, type_color=TYPE_STRIPE["PowerPlant"],
                                 max_rows=6)
            tbl.pack(fill="x")

            slot_dict = {"id": sid, "label": lbl, "max_size": sz, "local_ref": ""}
            self._slot_tables.setdefault("powerplants", []).append(
                (slot_dict, tbl, self._data.find_powerplant))
            self._powerplant_rows.append((sid, tbl))

    def _rebuild_qd_section(self, groups: dict):
        """Fill QD placeholder in center panel using ComponentTable."""
        container = getattr(self, "_qd_placeholder", None)
        if not container:
            return
        for w in container.winfo_children():
            w.destroy()
        self._qdrive_rows = []

        qd_list = groups.get("quantum_drives", [])
        if not qd_list:
            return

        self._section_header(container, "QUANTUM DRIVES", ACCENT)
        for i, hp in enumerate(qd_list):
            fy_st = compute_qdrive_stats(hp)
            sz = fy_st["size"]
            sid = f"qd_{i}"
            lbl = re.sub(r"hardpoint_", "", hp.get("name", f"QD {i+1}"), flags=re.I)
            lbl = lbl.replace("_", " ").title()

            # Slot header
            sh = tk.Frame(container, bg=BG)
            sh.pack(fill="x", pady=(4, 0))
            sz_bg = SIZE_COLORS.get(sz, SIZE_COLORS[1])
            tk.Label(sh, text=f"S{sz}", font=("Consolas", 8, "bold"),
                     bg=sz_bg, fg="white", width=3, padx=2).pack(side="left", padx=(4, 0))
            tk.Label(sh, text=lbl, font=("Consolas", 8),
                     bg=BG, fg=FG_DIM).pack(side="left", padx=(6, 0))

            items = self._data.qdrives_for_size(sz)

            def _on_sel(item, _sid=sid):
                name = item["name"] if item else ""
                self._sel["propulsion"][_sid] = name
                self._update_footer()

            stock_ref = ""
            stock_name = fy_st["name"]
            if stock_name and stock_name != "—":
                st = self._data.find_qdrive(stock_name, max_size=sz)
                if st:
                    stock_ref = st.get("ref", "")
                    self._sel["propulsion"][sid] = st["name"]

            tbl = ComponentTable(container, QD_COLS, items, _on_sel,
                                 current_ref=stock_ref, type_color=TYPE_STRIPE["QuantumDrive"],
                                 max_rows=6)
            tbl.pack(fill="x")

            slot_dict = {"id": sid, "label": lbl, "max_size": sz, "local_ref": ""}
            self._slot_tables.setdefault("qdrives", []).append(
                (slot_dict, tbl, self._data.find_qdrive))
            self._qdrive_rows.append((sid, tbl))

    def _rebuild_thrusters_section(self, groups: dict):
        """Fill thruster placeholder in center panel with display-only cards."""
        container = getattr(self, "_thruster_placeholder", None)
        if not container:
            return
        for w in container.winfo_children():
            w.destroy()
        self._thruster_rows = []

        for grp_key, title in [("main_thrusters", "MAIN THRUSTERS"),
                                ("retro_thrusters", "RETRO THRUSTERS"),
                                ("maneuvering_thrusters", "MANEUVERING")]:
            items = groups.get(grp_key, [])
            if not items:
                continue
            self._section_header(container, title, YELLOW)
            for i, hp in enumerate(items):
                st = compute_thruster_stats(hp)
                lbl = re.sub(r"hardpoint_", "", hp.get("name", f"T {i+1}"), flags=re.I)
                lbl = lbl.replace("_", " ").title()
                content, line1, line2, bg = self._make_card(
                    container, i, st["size"], lbl, TYPE_STRIPE["Thruster"])
                tk.Label(line2, text=st["name"], font=("Consolas", 8),
                         bg=bg, fg=FG).pack(side="left")
                tk.Label(line2, text=st["mfr"], font=("Consolas", 7),
                         bg=bg, fg=FG_DIM).pack(side="left", padx=(6, 0))
                self._thruster_rows.append(st)

    # ── (Old per-type stat-label helpers removed — ComponentTable shows stats inline) ──


    def _update_overview(self, ship: dict):
        v = self._ov_vars
        name = ship.get("name", "?")

        # Hull — totalHp is the sum of all hull parts
        hull = ship.get("hull", {})
        hull_hp = hull.get("totalHp", 0) if isinstance(hull, dict) else 0

        # Armor
        armor = ship.get("armor", {})
        if isinstance(armor, dict):
            armor_d = armor.get("data", armor)
        else:
            armor_d = {}
        a_health = armor_d.get("health", {}) or {}
        hull_hp  = hull_hp or a_health.get("hp", 0)
        a_resist = a_health.get("damageResistanceMultiplier", {}) or {}
        # resistance stored as multipliers (0.85 = 15% reduction = +15% phys resist)
        a_phys = (1 - a_resist.get("physical",  1)) * 100
        a_enrg = (1 - a_resist.get("energy",    1)) * 100
        a_dist = (1 - a_resist.get("distortion",1)) * 100
        a_type = armor_d.get("subType", "?")

        # Flight — scmSpeed is the speed-controlled mode speed; maxAfterburnSpeed for AB
        ifcs = ship.get("ifcs", {}) or {}
        scm  = ifcs.get("scmSpeed", 0)
        ab   = ifcs.get("maxAfterburnSpeed", 0)
        if isinstance(scm, dict): scm = 0
        if isinstance(ab, dict):  ab  = 0

        # QT
        qt    = ship.get("qtFuelCapacity", 0) or 0
        h2    = ship.get("fuelCapacity", 0) or 0
        cargo = ship.get("cargo", 0)
        if isinstance(cargo, (int, float)):
            cargo_scu = int(cargo)
        elif isinstance(cargo, dict):
            cargo_scu = int(cargo.get("capacity", 0) or 0)
        else:
            cargo_scu = 0

        # Crew
        vehicle = ship.get("vehicle", {}) or {}
        crew    = vehicle.get("crewSize", "?")

        v["hull_hp"].set(f"{hull_hp:,.0f}")
        v["armor_type"].set(a_type)
        v["armor_phys"].set(f"{a_phys:+.0f}%")
        v["armor_enrg"].set(f"{a_enrg:+.0f}%")
        v["armor_dist"].set(f"{a_dist:+.0f}%")
        v["cargo"].set(str(cargo_scu))
        v["crew"].set(str(crew))
        v["scm_speed"].set(f"{scm:,.0f}" if scm else "?")
        v["ab_speed"].set(f"{ab:,.0f}"  if ab  else "?")
        v["qt_speed"].set("?")
        v["h2_fuel"].set(f"{h2:,.0f}"  if h2  else "?")
        v["qt_fuel"].set(f"{qt:,.0f}"  if qt  else "?")

        # Update footer hull HP
        self._footer_vars["hull_hp"].set(f"{hull_hp:,.0f}" if hull_hp else "—")

        # ── Cross-section signature — only CS is set here; EM/IR are computed
        # by _update_signatures (called from _update_footer) which overwrites them.
        cs_raw = ship.get("crossSection", 0)
        if isinstance(cs_raw, dict):
            cs_x = float(cs_raw.get("x", 0) or 0)
            cs_y = float(cs_raw.get("y", 0) or 0)
            cs_z = float(cs_raw.get("z", 0) or 0)
            cs_sig = max(cs_x, cs_y, cs_z)
        elif isinstance(cs_raw, (int, float)):
            cs_sig = float(cs_raw)
        else:
            cs_sig = 0

        def _fmt_sig(val):
            if not val:
                return "—"
            if val >= 1000:
                return f"{val/1000:.1f}K"
            return f"{val:.0f}"

        if hasattr(self, "_sig_vars"):
            self._sig_vars["cs"].set(_fmt_sig(cs_sig))
        if "sig_cs" in v:
            v["sig_cs"].set(_fmt_sig(cs_sig))

    # ── Footer / totals ────────────────────────────────────────────────────────

    def _update_footer(self):
        # Weapons
        tot_raw = tot_sus = tot_alp = 0.0
        for sid, nm in self._sel["weapons"].items():
            if not nm: continue
            s = self._data.find_weapon(nm)
            if s:
                tot_raw += s["dps_raw"]
                tot_sus += s["dps_sus"]
                tot_alp += s["alpha"]

        # Missiles (volley damage)
        miss_dmg = 0.0
        for sid, nm in self._sel["missiles"].items():
            if not nm: continue
            s = self._data.find_missile(nm)
            if s:
                miss_dmg += s["total_dmg"]

        # Shields
        tot_hp = tot_regen = 0.0
        shld_res = {"phys": 0.0, "enrg": 0.0, "dist": 0.0}
        shld_count = 0
        for sid, nm in self._sel["defenses"].items():
            if not nm: continue
            s = self._data.find_shield(nm)
            if s:
                tot_hp    += s["hp"]
                tot_regen += s["regen"]
                shld_res["phys"] += s["res_phys_max"]
                shld_res["enrg"] += s["res_energy_max"]
                shld_res["dist"] += s["res_dist_max"]
                shld_count += 1

        # Cooling
        tot_cool = 0.0
        for sid, nm in self._sel["components"].items():
            if not nm: continue
            s = self._data.find_cooler(nm)
            if s:
                tot_cool += s["cooling_rate"]

        # Power budget — sum PP output and all components' power draw
        tot_pwr_out = 0.0
        tot_pwr_draw = 0.0
        for sid, nm in self._sel["components"].items():
            if not nm:
                continue
            if sid.startswith("pp_"):
                s = self._data.find_powerplant(nm)
                if s:
                    tot_pwr_out += float(s.get("output", 0) or 0)
            else:
                # Cooler, radar — draw power
                s = self._data.find_cooler(nm) or self._data.find_radar(nm)
                if s:
                    tot_pwr_draw += float(s.get("power_draw", 0) or 0)
        # Shields draw power
        for sid, nm in self._sel["defenses"].items():
            if not nm: continue
            s = self._data.find_shield(nm)
            if s:
                tot_pwr_draw += float(s.get("power_draw", 0) or 0)
        # Weapons draw power
        for sid, nm in self._sel["weapons"].items():
            if not nm: continue
            s = self._data.find_weapon(nm)
            if s:
                tot_pwr_draw += float(s.get("power_draw", 0) or 0)

        # Apply power ratio when POWER SIM is active
        if self._power_sim:
            wr = self._weapon_power_ratio
            sr = self._shield_power_ratio
            tot_raw *= wr
            tot_sus *= wr
            tot_regen *= sr

        # Footer
        self._footer_vars["dps_raw"].set(f"{tot_raw:,.0f}" if tot_raw else "—")
        self._footer_vars["dps_sus"].set(f"{tot_sus:,.0f}" if tot_sus else "—")
        self._footer_vars["alpha"].set(f"{tot_alp:,.1f}"   if tot_alp else "—")
        self._footer_vars["shld_hp"].set(f"{tot_hp:,.0f}"  if tot_hp  else "—")
        self._footer_vars["cooling"].set(f"{tot_cool/1000:,.0f}k" if tot_cool else "—")

        # Overview combat section
        if hasattr(self, "_ov_vars"):
            self._ov_vars["dps_raw"].set(f"{tot_raw:,.0f}" if tot_raw else "—")
            self._ov_vars["dps_sus"].set(f"{tot_sus:,.0f}" if tot_sus else "—")
            self._ov_vars["alpha"].set(f"{tot_alp:,.1f}"   if tot_alp else "—")
            self._ov_vars["missile_dmg"].set(f"{miss_dmg:,.0f}" if miss_dmg else "—")
            n_guns = sum(1 for v in self._sel["weapons"].values() if v)
            n_miss = sum(1 for v in self._sel["missiles"].values() if v)
            self._ov_vars["gun_slots"].set(f"{n_guns} equipped")
            self._ov_vars["miss_slots"].set(f"{n_miss} equipped")
            # Defenses
            self._ov_vars["shld_hp"].set(f"{tot_hp:,.0f}" if tot_hp else "—")
            regen_display = tot_regen
            self._ov_vars["shld_regen"].set(f"{regen_display:.1f}" if regen_display else "—")
            avg = lambda v: v / shld_count if shld_count else 0
            self._ov_vars["shld_phys"].set(pct(avg(shld_res["phys"])))
            self._ov_vars["shld_enrg"].set(pct(avg(shld_res["enrg"])))
            self._ov_vars["shld_dist"].set(pct(avg(shld_res["dist"])))
            self._ov_vars["cooling"].set(f"{tot_cool/1000:,.0f}k" if tot_cool else "—")
            # Power budget
            self._ov_vars["pwr_output"].set(f"{tot_pwr_out:,.0f}" if tot_pwr_out else "—")
            self._ov_vars["pwr_draw"].set(f"{tot_pwr_draw:,.0f}" if tot_pwr_draw else "—")
            margin = tot_pwr_out - tot_pwr_draw
            self._ov_vars["pwr_margin"].set(f"{margin:+,.0f}" if tot_pwr_out else "—")

        # ── Recompute signatures from selected components ──
        self._update_signatures()

    def _update_signatures(self):
        """Recompute EM/IR/CS signatures from all currently selected components.
        When POWER SIM is active, use the allocator's values (power-adjusted).
        """
        def _fmt_sig(val):
            if not val:
                return "—"
            if val >= 1000:
                return f"{val/1000:.1f}K"
            return f"{val:.0f}"

        # When POWER SIM is active, use the allocator's computed values
        if self._power_sim and hasattr(self, "_power_allocator"):
            pa = self._power_allocator
            em_sig = getattr(pa, "em_signature", 0)
            ir_sig = getattr(pa, "ir_signature", 0)
            if hasattr(self, "_sig_vars"):
                self._sig_vars["ir"].set(_fmt_sig(ir_sig))
                self._sig_vars["em"].set(_fmt_sig(em_sig))
            if hasattr(self, "_ov_vars"):
                if "sig_em" in self._ov_vars:
                    self._ov_vars["sig_em"].set(_fmt_sig(em_sig))
                if "sig_ir" in self._ov_vars:
                    self._ov_vars["sig_ir"].set(_fmt_sig(ir_sig))
            return

        # RAW mode: sum from all selected components
        em_sig = 0.0
        ir_sig = 0.0

        find_fns = [
            ("weapons",    self._data.find_weapon),
            ("missiles",   self._data.find_missile),
            ("defenses",   self._data.find_shield),
            ("components", self._data.find_cooler),
        ]
        for sel_key, find_fn in find_fns:
            for sid, nm in self._sel.get(sel_key, {}).items():
                if not nm:
                    continue
                s = find_fn(nm)
                if s:
                    em_sig += float(s.get("em_max", 0) or 0)
                    ir_sig += float(s.get("ir_max", 0) or 0)

        for sid, nm in self._sel.get("components", {}).items():
            if not nm or not sid.startswith("pp_"):
                continue
            s = self._data.find_powerplant(nm)
            if s:
                em_sig += float(s.get("em_max", s.get("em_idle", 0)) or 0)
                ir_sig += float(s.get("ir_max", 0) or 0)

        for sid, nm in self._sel.get("propulsion", {}).items():
            if not nm:
                continue
            s = self._data.find_qdrive(nm)
            if s:
                em_sig += float(s.get("em_max", s.get("em_idle", 0)) or 0)

        for sid, nm in self._sel.get("components", {}).items():
            if not nm:
                continue
            s = self._data.find_radar(nm)
            if s:
                em_sig += float(s.get("em_max", 0) or 0)

        if hasattr(self, "_sig_vars"):
            self._sig_vars["ir"].set(_fmt_sig(ir_sig))
            self._sig_vars["em"].set(_fmt_sig(em_sig))

        if hasattr(self, "_ov_vars"):
            self._ov_vars.get("sig_em", tk.StringVar()).set(_fmt_sig(em_sig))
            self._ov_vars.get("sig_ir", tk.StringVar()).set(_fmt_sig(ir_sig))

    # ── Power simulation ────────────────────────────────────────────────────

    def _set_power_mode(self, sim_on: bool):
        """Toggle between RAW and POWER SIM modes."""
        self._power_sim = sim_on
        self._update_power_toggle_style()
        # Show/hide power allocator and ship placeholder — always BEFORE stats
        if hasattr(self, "_power_allocator"):
            if sim_on:
                anchor = getattr(self, "_stats_anchor", None)
                if anchor:
                    self._power_allocator.pack(fill="x", padx=4, pady=(0, 4),
                                                before=anchor)
                else:
                    self._power_allocator.pack(fill="x", padx=4, pady=(0, 4))
                if hasattr(self, "_ship_placeholder"):
                    self._ship_placeholder.pack_forget()
                # Force redraw after packing (canvases need size)
                self.root.update_idletasks()
                self._power_allocator._recalculate()
            else:
                self._power_allocator.pack_forget()
                if hasattr(self, "_ship_placeholder"):
                    anchor = getattr(self, "_stats_anchor", None)
                    if anchor:
                        self._ship_placeholder.pack(fill="x", padx=8, pady=(0, 4),
                                                     before=anchor)
                    else:
                        self._ship_placeholder.pack(fill="x", padx=8, pady=(0, 4))
        # Recalculate with or without power adjustment
        if hasattr(self, "_current_ship") and self._current_ship:
            self._compute_power_stats(self._current_ship)
        self._update_footer()

    def _update_power_toggle_style(self):
        """Style the RAW/POWER SIM toggle buttons."""
        if not hasattr(self, "_raw_btn"):
            return
        if self._power_sim:
            self._raw_btn.configure(bg=BG3, fg=FG_DIM,
                                    activebackground=BG3, activeforeground=FG_DIM)
            self._sim_btn.configure(bg="#3a2a00", fg=YELLOW,
                                    activebackground="#4a3a10", activeforeground=YELLOW)
        else:
            self._raw_btn.configure(bg="#1a3a2a", fg=GREEN,
                                    activebackground="#2a4a3a", activeforeground=GREEN)
            self._sim_btn.configure(bg=BG3, fg=FG_DIM,
                                    activebackground=BG3, activeforeground=FG_DIM)

    def _compute_power_stats(self, ship: dict):
        """Delegate power computation to the PowerAllocator widget."""
        # The PowerAllocator handles its own recalculation internally
        # Sync the power ratios for DPS/shield adjustment from the allocator
        if hasattr(self, "_power_allocator") and self._power_allocator._slots:
            pa = self._power_allocator
            self._weapon_power_ratio = getattr(pa, "weapon_power_ratio", 1.0)
            self._shield_power_ratio = getattr(pa, "shield_power_ratio", 1.0)

    # ── Game-version auto-update (BUG 6) ──────────────────────────────────────

    def _start_version_check(self):
        """
        One-shot daemon thread: fetch the live game version from erkul and
        automatically invalidate the cache + reload data when the game updates.
        Runs once after the initial data load completes.
        """
        def _check():
            version = ""
            for path in ("/live/gameVersion", "/live/version"):
                try:
                    r = requests.get(API_BASE + path, headers=API_HEADERS, timeout=10)
                    try:
                        if r.ok:
                            obj = r.json()
                            version = (
                                obj.get("gameVersion") or
                                obj.get("version")     or
                                obj.get("live")        or ""
                            )
                            if version:
                                break
                    finally:
                        r.close()
                except Exception:
                    continue   # endpoint doesn't exist — try next

            if not version:
                return   # no version endpoint on this server — skip silently

            # Show version badge in header
            self.root.after(0, self._show_version_badge, version)

            cached_v = self._data.cached_game_version

            if cached_v == version:
                return   # version unchanged — nothing to do

            # Version differs (or was never stored)
            self._data.cached_game_version = version

            if cached_v:
                # A previous version was stored and it changed → patch detected
                self.root.after(
                    0, self._status_var.set,
                    f"Game updated to v{version} — refreshing data…"
                )
                try:
                    if os.path.isfile(CACHE_FILE):
                        os.remove(CACHE_FILE)
                except Exception:
                    pass
                with self._data._lock:
                    self._data.loaded  = False
                    self._data.loading = False
                    self._data.error   = None
                self._data.load(
                    on_done=lambda: self.root.after(0, self._on_data_loaded)
                )
            else:
                # First run — just persist the version into the cache
                self._data._save_cache(self._data.raw)

        threading.Thread(target=_check, daemon=True).start()

    def _show_version_badge(self, version: str):
        """Display the game version badge in the header."""
        self._version_var.set(f"v{version}")
        self._version_lbl.pack(side="right", padx=(0, 8))

    # ── Section RESET helper (generic) ───────────────────────────────────────

    def _reset_section(self, section_name):
        """Reset slots in a section by reloading the ship."""
        if self._ship_name:
            self._load_ship(self._ship_name)

    # ── Voice helpers (unified for ComponentTable) ────────────────────────────

    def _voice_set_slot(self, section_key, slot_query, comp_name, find_fn=None):
        """Generic voice command: set a slot in any section via ComponentTable."""
        tables = self._slot_tables.get(section_key, [])
        if not tables:
            return
        q = slot_query.lower().strip()

        # Resolve targets
        if q == "all":
            targets = tables
        elif q.isdigit():
            idx = int(q) - 1
            targets = [tables[idx]] if 0 <= idx < len(tables) else []
        else:
            targets = [t for t in tables if q in t[0]["label"].lower()]
            if not targets:
                targets = tables  # fallback to all

        for slot_dict, tbl, slot_find_fn in targets:
            fn = find_fn or slot_find_fn
            max_sz = slot_dict.get("max_size")
            stats = fn(comp_name, max_size=max_sz)
            if stats:
                sid = slot_dict["id"]
                # Determine which sel dict to use
                sel_key = section_key
                if section_key in ("coolers", "radars", "powerplants"):
                    sel_key = "components"
                elif section_key in ("qdrives",):
                    sel_key = "propulsion"
                self._sel.setdefault(sel_key, {})[sid] = stats["name"]
                tbl.set_selected(stats.get("ref", ""))
        self._update_footer()

    def _set_by_slot(self, tab_key: str, slot_query: str, comp_name: str):
        """Find the slot matching slot_query and update via ComponentTable."""
        find_map = {
            "weapons": self._data.find_weapon,
            "missiles": self._data.find_missile,
            "defenses": self._data.find_shield,
        }
        self._voice_set_slot(tab_key, slot_query, comp_name,
                             find_fn=find_map.get(tab_key))

    def _set_component_slot(self, comp_type: str, slot_query: str, name: str):
        """Set a cooler or radar slot by slot number/label or 'all'."""
        section_map = {"cooler": "coolers", "radar": "radars"}
        find_map = {"cooler": self._data.find_cooler, "radar": self._data.find_radar}
        section = section_map.get(comp_type)
        if not section:
            return
        # Coolers/radars are stored under "components" in _slot_tables
        # but _build_table_slot stores them under "components" section_key
        # Try the section_key used by _build_table_slot
        tables = self._slot_tables.get("components", [])
        if not tables:
            return
        q = slot_query.lower().strip()
        fn = find_map.get(comp_type, self._data.find_cooler)

        # Filter tables to only cooler or radar slots
        type_filter = comp_type.lower()
        filtered = []
        for slot_dict, tbl, slot_fn in tables:
            if slot_fn == fn:
                filtered.append((slot_dict, tbl, slot_fn))

        if q == "all":
            targets = filtered
        elif q.isdigit():
            idx = int(q) - 1
            targets = [filtered[idx]] if 0 <= idx < len(filtered) else []
        else:
            targets = [t for t in filtered if q in t[0]["label"].lower()]

        for slot_dict, tbl, slot_fn in targets:
            max_sz = slot_dict.get("max_size")
            stats = fn(name, max_size=max_sz)
            if stats:
                self._sel["components"][slot_dict["id"]] = stats["name"]
                tbl.set_selected(stats.get("ref", ""))
        self._update_footer()

    def _set_powerplant_slot(self, slot_query: str, name: str):
        """Set a power plant slot by voice command."""
        self._voice_set_slot("powerplants", slot_query, name,
                             find_fn=self._data.find_powerplant)

    def _set_qdrive_slot(self, slot_query: str, name: str):
        """Set a quantum drive slot by voice command."""
        self._voice_set_slot("qdrives", slot_query, name,
                             find_fn=self._data.find_qdrive)

    def _reset_all(self):
        if self._ship_name:
            self._load_ship(self._ship_name)
        else:
            self._sel = {k: {} for k in self._sel}

    def _do_refresh(self):
        self._status_var.set("Refreshing from erkul.games…")
        try:
            if os.path.isfile(CACHE_FILE):
                os.remove(CACHE_FILE)
        except Exception:
            pass
        with self._data._lock:
            self._data.loaded  = False
            self._data.loading = False
            self._data.error   = None
        self._data.load(on_done=lambda: self.root.after(0, self._on_data_loaded))

    # ── Command file watcher ───────────────────────────────────────────────────

    def _start_cmd_watcher(self):
        self._cmd_offset = 0
        threading.Thread(target=self._watch_cmds, daemon=True).start()

    def _watch_cmds(self):
        while True:
            try:
                if self.cmd_file and os.path.isfile(self.cmd_file):
                    commands, self._cmd_offset = ipc_read_incremental(
                        self.cmd_file, self._cmd_offset)
                    for cmd in commands:
                        self.root.after(0, self._dispatch, cmd)
            except Exception as e:
                _log.warning("_watch_cmds error: %s", e)
            time.sleep(0.2)

    def _dispatch(self, cmd: dict):
        t = cmd.get("type", "")
        if t == "quit":
            self.root.destroy()
        elif t == "show":
            self.root.deiconify(); self.root.lift()
        elif t == "hide":
            self.root.withdraw()
        elif t == "set_ship":
            ship = cmd.get("ship", "")
            if ship:
                if self._data.loaded:
                    self._ship_var.set(ship); self._load_ship(ship)
                else:
                    self._pending_ship = ship
        elif t == "set_weapon":
            slot = str(cmd.get("slot", "1"))
            name = cmd.get("name", "")
            tab  = cmd.get("tab", "weapons")   # weapons | missiles | defenses
            if name and self._data.loaded:
                self._set_by_slot(tab, slot, name)
        elif t == "set_component":
            comp_type = cmd.get("component_type", "cooler")
            slot      = str(cmd.get("slot", "1"))
            name      = cmd.get("name", "")
            if name and self._data.loaded:
                self._set_component_slot(comp_type, slot, name)
        elif t == "set_powerplant":
            slot = str(cmd.get("slot", "1"))
            name = cmd.get("name", "")
            if name and self._data.loaded:
                self._set_powerplant_slot(slot, name)
        elif t == "set_quantumdrive":
            slot = str(cmd.get("slot", "1"))
            name = cmd.get("name", "")
            if name and self._data.loaded:
                self._set_qdrive_slot(slot, name)
        elif t == "reset":
            self._reset_all()
        elif t == "refresh":
            self._do_refresh()

    # ── Run ────────────────────────────────────────────────────────────────────

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)

        def _check():
            if self._pending_ship and self._data.loaded:
                self._ship_var.set(self._pending_ship)
                self._load_ship(self._pending_ship)
                self._pending_ship = None
            self.root.after(500, _check)

        self.root.after(500, _check)
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    from shared.platform_utils import set_dpi_awareness
    from shared.data_utils import parse_cli_args
    set_dpi_awareness()
    p = parse_cli_args(sys.argv[1:])
    DpsCalcApp(p["x"], p["y"], p["w"], p["h"], p["opacity"], p["cmd_file"]).run()


if __name__ == "__main__":
    main()
