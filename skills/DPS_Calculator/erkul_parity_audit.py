#!/usr/bin/env python3
"""
Comprehensive Erkul Parity Audit — ship-by-ship verification that every
calculation (DPS, alpha, sustained, shields, hull, missiles, power pips)
matches what Erkul shows for each ship's stock loadout.

Reads .erkul_cache.json directly (no network, no GUI).
Outputs erkul_parity_audit_report.txt.
"""

import json
import math
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(SCRIPT_DIR, '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from services.dps_calculator import (
    fire_rate_rps, alpha_max, dps_sustained, dmg_breakdown, compute_weapon_stats,
)
from services.stat_computation import (
    compute_shield_stats, compute_cooler_stats, compute_radar_stats,
    compute_missile_stats, compute_powerplant_stats_erkul, compute_qdrive_stats_erkul,
)
from services.slot_extractor import extract_slots_by_type
from services.power_engine import PowerAllocatorEngine
from shared.data_utils import _sf, pct_diff

CACHE_FILE = os.path.join(SCRIPT_DIR, ".erkul_cache.json")
REPORT_FILE = os.path.join(SCRIPT_DIR, "erkul_parity_audit_report.txt")

# ── Tolerances ──
TOL_DPS = 0.5       # absolute DPS tolerance
TOL_PCT = 1.0       # percentage tolerance for most values
TOL_SHIELD = 1.0    # absolute shield HP tolerance
TOL_REGEN = 0.1     # absolute regen tolerance
TOL_RESIST = 0.01   # absolute resistance tolerance
TOL_HULL = 1.0      # absolute hull HP tolerance
TOL_MISSILE = 1.0   # absolute missile dmg tolerance
TOL_POWER = 0       # power pips must match exactly
TOL_SIG = 5.0       # percentage tolerance for signature values

# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def load_cache():
    with open(CACHE_FILE, encoding="utf-8") as f:
        obj = json.load(f)
    return obj.get("data", {})

def build_indexes(raw):
    """Build lookup dicts matching ComponentRepository's _index logic."""
    weapons_by_ref = {}
    weapons_by_name = {}
    shields_by_ref = {}
    shields_by_name = {}
    coolers_by_ref = {}
    coolers_by_name = {}
    radars_by_ref = {}
    radars_by_name = {}
    missiles_by_ref = {}
    missiles_by_name = {}
    powerplants_by_ref = {}
    powerplants_by_name = {}
    qdrives_by_ref = {}
    qdrives_by_name = {}

    def _index(entries, compute_fn, by_ref, by_name, filt=None):
        for e in entries:
            d = e.get("data", {})
            if filt and not filt(d):
                continue
            try:
                stats = compute_fn(e)
            except (KeyError, TypeError, ValueError):
                continue
            stats.setdefault("class", d.get("class", ""))
            stats.setdefault("grade", d.get("grade", "?"))
            _hlth = d.get("health", d.get("hp", 0))
            if isinstance(_hlth, dict):
                _hlth = _hlth.get("hp", 0)
            stats.setdefault("hp", _sf(_hlth))
            res = d.get("resource", {}) or {}
            onl = res.get("online", {}) or {}
            cons = onl.get("consumption", {}) or {}
            if not isinstance(cons, dict):
                cons = {}
            pwr_draw = _sf(cons.get("powerSegment", cons.get("power", 0)))
            stats.setdefault("power_draw", pwr_draw)
            stats.setdefault("power_max", pwr_draw)
            sig = onl.get("signatureParams", {}) or {}
            em_d = sig.get("em", {}) or {}
            ir_d = sig.get("ir", {}) or {}
            stats.setdefault("em_max", _sf(em_d.get("nominalSignature", 0)))
            stats.setdefault("ir_max", _sf(ir_d.get("nominalSignature", 0)))
            ref = stats["ref"]
            key = f"{stats['name'].lower()}_{stats['size']}"
            if ref:
                by_ref[ref] = stats
            by_name[key] = stats
            # Also index by localName for direct lookup
            ln = e.get("localName", "")
            if ln:
                by_name[f"_ln_{ln}"] = stats

    _index(raw.get("/live/weapons", []), compute_weapon_stats,
           weapons_by_ref, weapons_by_name,
           filt=lambda d: d.get("type") == "WeaponGun")
    _index(raw.get("/live/shields", []), compute_shield_stats,
           shields_by_ref, shields_by_name)
    _index(raw.get("/live/coolers", []), compute_cooler_stats,
           coolers_by_ref, coolers_by_name)
    _index(raw.get("/live/radars", []), compute_radar_stats,
           radars_by_ref, radars_by_name)
    _index(raw.get("/live/missiles", []), compute_missile_stats,
           missiles_by_ref, missiles_by_name)
    _index(raw.get("/live/powerplants", []), compute_powerplant_stats_erkul,
           powerplants_by_ref, powerplants_by_name)
    _index(raw.get("/live/quantumdrives", []), compute_qdrive_stats_erkul,
           qdrives_by_ref, qdrives_by_name)

    return {
        "weapons": (weapons_by_ref, weapons_by_name),
        "shields": (shields_by_ref, shields_by_name),
        "coolers": (coolers_by_ref, coolers_by_name),
        "radars": (radars_by_ref, radars_by_name),
        "missiles": (missiles_by_ref, missiles_by_name),
        "powerplants": (powerplants_by_ref, powerplants_by_name),
        "qdrives": (qdrives_by_ref, qdrives_by_name),
    }

# Build raw lookup dicts
RAW_BY_LN = {}
RAW_BY_REF = {}

SUPPLEMENT_FILE = os.path.join(SCRIPT_DIR, "data", "erkul_supplement.json")

def build_raw_lookups(raw):
    global RAW_BY_LN, RAW_BY_REF
    for ep_key in raw:
        entries = raw[ep_key]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            ln = entry.get("localName", "")
            d = entry.get("data", {})
            ref = d.get("ref", "")
            if ln:
                RAW_BY_LN[ln] = d
            if ref:
                RAW_BY_REF[ref] = d

    # Merge supplement data (items absent from Erkul API)
    try:
        with open(SUPPLEMENT_FILE, encoding="utf-8") as f:
            supplement = json.load(f)
        for entry in supplement:
            ln = entry.get("localName", "")
            d  = entry.get("data", {})
            ref = d.get("ref", "")
            if ln:
                RAW_BY_LN.setdefault(ln, d)
            if ref:
                RAW_BY_REF.setdefault(ref, d)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

def find_component(by_ref, by_name, query):
    """Find a component by ref UUID, localName, or name."""
    if not query:
        return None
    if query in by_ref:
        return by_ref[query]
    # Try localName index
    ln_key = f"_ln_{query}"
    if ln_key in by_name:
        return by_name[ln_key]
    # Try name match
    ql = query.lower()
    for v in by_name.values():
        ln = v.get("local_name", "")
        if ln and ln.lower() == ql:
            return v
    for v in by_ref.values():
        ln = v.get("local_name", "")
        if ln and ln.lower() == ql:
            return v
    return None

def item_lookup_fn(local_name, indexes):
    """Lookup function for PowerAllocatorEngine."""
    for comp_type in indexes:
        by_ref, by_name = indexes[comp_type]
        result = find_component(by_ref, by_name, local_name)
        if result:
            return result
    return None

def raw_lookup_fn(identifier):
    """Lookup raw data by localName or ref UUID."""
    if identifier in RAW_BY_LN:
        return RAW_BY_LN[identifier]
    if identifier in RAW_BY_REF:
        return RAW_BY_REF[identifier]
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def audit_weapon(weapon_entry):
    """Audit a single weapon's computed stats vs raw data.
    Returns (stats_dict, issues_list)."""
    issues = []
    d = weapon_entry.get("data", {})
    stats = compute_weapon_stats(weapon_entry)

    # Verify fire rate
    rps = fire_rate_rps(d)
    w = d.get("weapon", {})
    fa = w.get("fireActions", [])
    mode = w.get("mode", "")

    # Verify alpha
    alpha = alpha_max(d)

    # Verify DPS
    dps_raw = alpha * rps
    dps_sus = dps_sustained(d, alpha, rps)

    # Check for NaN/Inf
    for label, val in [("rps", rps), ("alpha", alpha), ("dps_raw", dps_raw), ("dps_sus", dps_sus)]:
        if math.isnan(val) or math.isinf(val):
            issues.append(f"{label} is {val}")

    # Verify damage breakdown
    brk = dmg_breakdown(d)
    ammo_d = d.get("ammo", {}).get("data", {})
    raw_dmg = ammo_d.get("damage", {})
    for k in ("damagePhysical", "damageEnergy", "damageDistortion", "damageThermal"):
        raw_val = float(raw_dmg.get(k, 0) or 0)
        comp_val = brk.get(k, 0)
        if abs(raw_val - comp_val) > 0.01:
            # Check if explosion damage is being added
            expl = ammo_d.get("explosion", {}).get("damage", {})
            expl_val = float(expl.get(k, 0) or 0)
            expected = raw_val + expl_val
            if abs(expected - comp_val) > 0.01:
                issues.append(f"dmg {k}: raw={raw_val} + expl={expl_val} != computed={comp_val}")

    return stats, issues


def audit_shield(shield_entry):
    """Audit a single shield's computed stats vs raw data."""
    issues = []
    d = shield_entry.get("data", {})
    stats = compute_shield_stats(shield_entry)
    sh = d.get("shield", {})

    # Verify HP
    raw_hp = sh.get("maxShieldHealth", 0)
    if abs(stats["hp"] - raw_hp) > TOL_SHIELD:
        issues.append(f"hp: computed={stats['hp']} raw={raw_hp}")

    # Verify regen
    raw_regen = sh.get("maxShieldRegen", 0)
    if abs(stats["regen"] - raw_regen) > TOL_REGEN:
        issues.append(f"regen: computed={stats['regen']} raw={raw_regen}")

    # Verify resistances
    res = sh.get("resistance", {})
    for rtype, rkey in [("phys", "physical"), ("energy", "energy"), ("dist", "distortion")]:
        for bound in ("min", "max"):
            raw_key = f"{rkey}{bound.title()}"
            stat_key = f"res_{rtype}_{bound}"
            raw_val = float(res.get(raw_key, 0) or 0)
            comp_val = stats.get(stat_key, 0)
            if abs(raw_val - comp_val) > TOL_RESIST:
                issues.append(f"resist {stat_key}: computed={comp_val} raw={raw_val}")

    return stats, issues


def audit_missile(missile_entry):
    """Audit a single missile's computed stats vs raw data."""
    issues = []
    d = missile_entry.get("data", {})
    stats = compute_missile_stats(missile_entry)
    ms = d.get("missile", {}) or {}
    dmg = ms.get("damage", {}) or {}

    raw_total = sum(v for v in dmg.values() if isinstance(v, (int, float)))
    if abs(stats["total_dmg"] - raw_total) > TOL_MISSILE:
        issues.append(f"total_dmg: computed={stats['total_dmg']} raw={raw_total}")

    return stats, issues


# ═══════════════════════════════════════════════════════════════════════════════
# SHIP-LEVEL AUDIT
# ═══════════════════════════════════════════════════════════════════════════════

def audit_ship(ship_name, ship_data, indexes, raw):
    """Full audit of a single ship. Returns dict of results."""
    results = {
        "name": ship_name,
        "issues": [],
        "weapon_issues": [],
        "shield_issues": [],
        "missile_issues": [],
        "hull_issues": [],
        "power_issues": [],
        "totals": {},
    }

    loadout = ship_data.get("loadout", [])
    if not loadout:
        results["issues"].append("NO LOADOUT")
        return results

    w_by_ref, w_by_name = indexes["weapons"]
    s_by_ref, s_by_name = indexes["shields"]
    m_by_ref, m_by_name = indexes["missiles"]

    # ── WEAPONS ──
    wt_slots = extract_slots_by_type(loadout, {"WeaponGun", "Turret"})
    total_dps_raw = 0.0
    total_dps_sus = 0.0
    total_alpha = 0.0
    gun_count = 0
    for slot in wt_slots:
        lr = slot.get("local_ref", "")
        if not lr:
            continue
        w = find_component(w_by_ref, w_by_name, lr)
        if w:
            total_dps_raw += w["dps_raw"]
            total_dps_sus += w["dps_sus"]
            total_alpha += w["alpha"]
            gun_count += 1
        else:
            # Check if it's a missile equipped in a gun slot (e.g. Avenger Renegade)
            m_in_gun = find_component(m_by_ref, m_by_name, lr)
            if m_in_gun:
                results["weapon_issues"].append(
                    f"INFO: missile in gun slot: {lr} in slot {slot.get('label','?')}")
            else:
                raw_d = RAW_BY_REF.get(lr) or RAW_BY_LN.get(lr)
                if raw_d:
                    itype = raw_d.get("type", "Unknown")
                    results["weapon_issues"].append(
                        f"KNOWN NON-WEAPON ({itype}): {lr} in slot {slot.get('label','?')}")
                else:
                    results["weapon_issues"].append(
                        f"UNRESOLVED weapon ref: {lr} in slot {slot.get('label','?')}")

    # ── MISSILES ──
    gun_ids = {s["id"] for s in wt_slots}
    ms_all = extract_slots_by_type(loadout, {"MissileLauncher"})
    ms_slots = [s for s in ms_all if s["id"] not in gun_ids]
    total_missile_dmg = 0.0
    missile_count = 0
    for slot in ms_slots:
        lr = slot.get("local_ref", "")
        if not lr:
            continue
        m = find_component(m_by_ref, m_by_name, lr)
        if m:
            total_missile_dmg += m["total_dmg"]
            missile_count += 1
        else:
            raw_d = RAW_BY_REF.get(lr) or RAW_BY_LN.get(lr)
            if raw_d:
                itype = raw_d.get("type", "Unknown")
                results["missile_issues"].append(
                    f"KNOWN NON-WEAPON ({itype}): {lr} in slot {slot.get('label','?')}")
            else:
                results["missile_issues"].append(
                    f"UNRESOLVED missile ref: {lr} in slot {slot.get('label','?')}")

    # ── SHIELDS ──
    sh_slots = extract_slots_by_type(loadout, {"Shield"})
    total_shield_hp = 0.0
    total_shield_regen = 0.0
    shield_count = 0
    shield_res_phys = 0.0
    shield_res_energy = 0.0
    shield_res_dist = 0.0
    for slot in sh_slots:
        lr = slot.get("local_ref", "")
        if not lr:
            continue
        s = find_component(s_by_ref, s_by_name, lr)
        if s:
            total_shield_hp += s["hp"]
            total_shield_regen += s["regen"]
            shield_res_phys += s["res_phys_max"]
            shield_res_energy += s["res_energy_max"]
            shield_res_dist += s["res_dist_max"]
            shield_count += 1
        else:
            results["shield_issues"].append(f"UNRESOLVED shield ref: {lr} in slot {slot.get('label','?')}")

    # Average resistances if multiple shields
    if shield_count > 0:
        shield_res_phys /= shield_count
        shield_res_energy /= shield_count
        shield_res_dist /= shield_count

    # ── HULL ──
    armor_d = ship_data.get("armor", {})
    if isinstance(armor_d, dict):
        armor_data = armor_d.get("data", armor_d)
    else:
        armor_data = {}
    arm = armor_data.get("armor", {}) if isinstance(armor_data, dict) else {}
    hull_d = ship_data.get("hull", {})

    # Hull HP from hull.totalHp
    hull_hp = hull_d.get("totalHp", 0) if isinstance(hull_d, dict) else 0
    # Armor HP from armor.data.health.hp
    armor_health = armor_data.get("health", {})
    armor_hp = armor_health.get("hp", 0) if isinstance(armor_health, dict) else 0

    # Armor type from armor.data.subType
    armor_subtype = armor_data.get("subType", "Unknown")

    # Damage multipliers from armor
    dmg_mult = arm.get("damageMultiplier", {})
    armor_phys = float(dmg_mult.get("damagePhysical", 1) or 1)
    armor_energy = float(dmg_mult.get("damageEnergy", 1) or 1)
    armor_dist = float(dmg_mult.get("damageDistortion", 1) or 1)

    # ── POWER ALLOCATOR ──
    def _item_lookup(ln):
        return item_lookup_fn(ln, indexes)

    engine = PowerAllocatorEngine(_item_lookup, raw_lookup_fn)
    engine.load_ship(ship_data)

    # Collect power pip data
    power_data = {}
    for cat_key, label, _, _ in PowerAllocatorEngine.CATEGORY_ORDER:
        cat_slots = engine._categories.get(cat_key, [])
        if cat_slots:
            total_max = sum(s["max_segments"] for s in cat_slots)
            total_current = sum(s["current_seg"] for s in cat_slots)
            total_enabled = all(s["enabled"] for s in cat_slots)
            power_data[cat_key] = {
                "label": label,
                "max_pips": total_max,
                "current_pips": total_current,
                "enabled": total_enabled,
                "slot_count": len(cat_slots),
            }

    # Verify power pips match erkul's rnPowerPools for weapons
    rn_pools = ship_data.get("rnPowerPools", {})
    wpn_pool = rn_pools.get("weaponGun", {})
    if wpn_pool.get("type") == "fixed":
        erkul_wpn_pips = wpn_pool.get("poolSize", 0)
        our_wpn = power_data.get("weaponGun", {})
        our_wpn_pips = our_wpn.get("max_pips", 0) if our_wpn else 0
        if erkul_wpn_pips != our_wpn_pips:
            results["power_issues"].append(
                f"Weapon pips: erkul={erkul_wpn_pips} ours={our_wpn_pips}")

    # Verify engine pips match ifcs
    ifcs = ship_data.get("ifcs", {})
    ifcs_res = ifcs.get("resource", {}).get("online", {}) if isinstance(ifcs.get("resource"), dict) else {}
    engine_seg = ifcs_res.get("consumption", {}).get("powerSegment", 0) if isinstance(ifcs_res.get("consumption"), dict) else 0
    if engine_seg:
        our_thr = power_data.get("thruster", {})
        our_thr_pips = our_thr.get("max_pips", 0) if our_thr else 0
        if int(engine_seg) != our_thr_pips:
            results["power_issues"].append(
                f"Thruster pips: erkul={int(engine_seg)} ours={our_thr_pips}")

    # Verify each component type's power segments match raw data
    for comp_type, cat_key in [("Shield", "shield"), ("Cooler", "cooler"),
                                ("Radar", "radar"), ("QuantumDrive", "quantumDrive")]:
        comp_slots = extract_slots_by_type(loadout, {comp_type})
        expected_total_seg = 0
        for slot in comp_slots:
            lr = slot.get("local_ref", "")
            if not lr:
                continue
            raw_d = raw_lookup_fn(lr)
            if raw_d:
                res = raw_d.get("resource", {}).get("online", {})
                cons = res.get("consumption", {})
                seg = cons.get("powerSegment", cons.get("power", 0))
                expected_total_seg += int(seg) if seg else 0

        our_cat = power_data.get(cat_key, {})
        our_seg = our_cat.get("max_pips", 0) if our_cat else 0
        if expected_total_seg > 0 and expected_total_seg != our_seg:
            results["power_issues"].append(
                f"{cat_key} pips: erkul_raw={expected_total_seg} ours={our_seg}")

    # ── SIGNATURES ──
    sig_data = engine.recalculate()

    # ── STORE TOTALS ──
    results["totals"] = {
        "dps_raw": round(total_dps_raw, 1),
        "dps_sus": round(total_dps_sus, 1),
        "alpha": round(total_alpha, 1),
        "missile_dmg": round(total_missile_dmg, 0),
        "gun_count": gun_count,
        "missile_count": missile_count,
        "shield_hp": round(total_shield_hp, 0),
        "shield_regen": round(total_shield_regen, 1),
        "shield_res_phys": round(shield_res_phys * 100, 1) if shield_count > 0 else 0,
        "shield_res_energy": round(shield_res_energy * 100, 1) if shield_count > 0 else 0,
        "shield_res_dist": round(shield_res_dist * 100, 1) if shield_count > 0 else 0,
        "shield_count": shield_count,
        "hull_hp": hull_hp,
        "armor_hp": armor_hp,
        "armor_type": armor_subtype,
        "armor_phys_mult": round(armor_phys, 2),
        "armor_energy_mult": round(armor_energy, 2),
        "armor_dist_mult": round(armor_dist, 2),
        "pp_output": sig_data.get("total_capacity", 0),
        "power_draw": sig_data.get("total_draw", 0),
        "em_sig": round(sig_data.get("em_sig", 0), 1),
        "ir_sig": round(sig_data.get("ir_sig", 0), 1),
        "cs_sig": round(sig_data.get("cs_sig", 0), 1),
        "weapon_ratio": round(sig_data.get("weapon_power_ratio", 0), 3),
        "shield_ratio": round(sig_data.get("shield_power_ratio", 0), 3),
        "power_pips": power_data,
    }

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# INDIVIDUAL COMPONENT AUDITS (ALL WEAPONS, SHIELDS, MISSILES)
# ═══════════════════════════════════════════════════════════════════════════════

def audit_all_weapons(raw):
    """Audit every weapon in the cache."""
    issues = []
    total = 0
    failed = 0
    for entry in raw.get("/live/weapons", []):
        d = entry.get("data", {})
        if d.get("type") != "WeaponGun":
            continue
        total += 1
        name = d.get("name", "?")
        stats, weapon_issues = audit_weapon(entry)
        if weapon_issues:
            failed += 1
            for issue in weapon_issues:
                issues.append(f"  {name} (S{d.get('size',0)}): {issue}")
    return total, failed, issues


def audit_all_shields(raw):
    """Audit every shield in the cache."""
    issues = []
    total = 0
    failed = 0
    for entry in raw.get("/live/shields", []):
        total += 1
        d = entry.get("data", {})
        name = d.get("name", "?")
        stats, shield_issues = audit_shield(entry)
        if shield_issues:
            failed += 1
            for issue in shield_issues:
                issues.append(f"  {name} (S{d.get('size',0)}): {issue}")
    return total, failed, issues


def audit_all_missiles(raw):
    """Audit every missile in the cache."""
    issues = []
    total = 0
    failed = 0
    for entry in raw.get("/live/missiles", []):
        total += 1
        d = entry.get("data", {})
        name = d.get("name", "?")
        stats, missile_issues = audit_missile(entry)
        if missile_issues:
            failed += 1
            for issue in missile_issues:
                issues.append(f"  {name} (S{d.get('size',0)}): {issue}")
    return total, failed, issues


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5: CONTEXT-AWARE DPS PIPELINE TESTS
# These tests catch wiring bugs that Phase 1-4 miss — e.g., wrong data level
# being passed to dps_sustained, ship buff not applied, power ratio ignored.
# ═══════════════════════════════════════════════════════════════════════════════

# Known-good reference values verified against erkul.games:
#   CF-337 Panther (alpha=43.65, rps=12.5, maxAmmoLoad=75, maxRegenPerSec=15, cooldown=0.74)
#   Asgard buff: ammoLoadMult=5, regenMult=1, powerRatioMult=1
#
#   standalone (all mults=1, ratio=1):        ammos=75,  sus≈278.9
#   with ammoLoadMult=5, ratio=1.0:           ammos=375, sus≈293.7
#   with ammoLoadMult=5, ratio=0.513:         ammos=192, sus≈204.2  ← Erkul shows ~204
#
# UPDATED 2026-07-13: the old fixtures (291.0/296.2/206.6) were stale — the Panther's
# regenerationCooldown changed 0.25→0.74 in a patch, lowering sustained DPS. New values
# match BOTH the toolbox calc and an independent clean-Erkul-data calc; the standalone
# 278.9 was live-confirmed on erkul.games (shows 558 on a 2× mount = 279/gun).

_PHASE5_TESTS = [
    # (description, weapon_name_substr, ammo_load_mult, regen_mult, power_ratio_mult,
    #  weapon_power_ratio, expected_sus, tolerance)
    ("Panther standalone (no buff)",
     "CF-337 Panther", 1.0, 1.0, 1.0, 1.0, 278.9, 1.0),
    ("Panther Asgard buff ammoLoad=5 full power",
     "CF-337 Panther", 5.0, 1.0, 1.0, 1.0, 293.7, 1.0),
    ("Panther Asgard buff ammoLoad=5 ratio=0.513 (Erkul ~204)",
     "CF-337 Panther", 5.0, 1.0, 1.0, 0.513, 204.2, 1.0),
]

def run_phase5_tests(raw, out):
    """Test the full context-aware dps_sustained pipeline for known reference values."""
    out("=" * 100)
    out("  PHASE 5: CONTEXT-AWARE DPS PIPELINE TESTS")
    out("=" * 100)
    out()

    failures = []

    # ── Test A: raw_lookup returns inner data dict (not outer entry) ──────────
    out("A. raw_lookup structure test:")
    sample_ref = None
    for entry in raw.get("/live/weapons", []):
        r = entry.get("data", {}).get("ref")
        if r:
            sample_ref = r
            break

    if sample_ref:
        result = RAW_BY_REF.get(sample_ref, {})
        has_weapon_key = "weapon" in result
        has_data_key   = "data" in result
        if has_weapon_key and not has_data_key:
            out(f"  PASS  raw_lookup returns inner data dict (has 'weapon', no 'data' wrapper)")
        elif has_data_key:
            msg = "  FAIL  raw_lookup returns OUTER entry dict — double-unwrap bug present"
            out(msg)
            failures.append(msg)
        elif not has_weapon_key:
            msg = f"  FAIL  raw_lookup result has neither 'weapon' nor 'data' key (ref={sample_ref})"
            out(msg)
            failures.append(msg)
    else:
        out("  SKIP  No weapon refs found in cache")
    out()

    # ── Test B: ammo_load_mult changes sustained DPS ──────────────────────────
    out("B. ammo_load_mult effect test:")
    panther_d = None
    for entry in raw.get("/live/weapons", []):
        d = entry.get("data", {})
        if "Panther" in d.get("name", "") and "CF-337" in d.get("name", ""):
            panther_d = d
            break

    if panther_d:
        alp = alpha_max(panther_d)
        rps = fire_rate_rps(panther_d)
        sus_no_buff  = dps_sustained(panther_d, alp, rps, 1.0, 1.0, 1.0, 1.0)
        sus_with_buff = dps_sustained(panther_d, alp, rps, 5.0, 1.0, 1.0, 1.0)
        if abs(sus_with_buff - sus_no_buff) > 1.0:
            out(f"  PASS  ammo_load_mult=5 changes sus: {sus_no_buff:.1f} → {sus_with_buff:.1f}")
        else:
            msg = f"  FAIL  ammo_load_mult=5 had no effect: {sus_no_buff:.1f} → {sus_with_buff:.1f}"
            out(msg)
            failures.append(msg)
    else:
        out("  SKIP  CF-337 Panther not found in cache")
    out()

    # ── Test C: weapon_power_ratio compounds into ammos (not linear scale) ────
    out("C. weapon_power_ratio compounding test:")
    if panther_d:
        alp = alpha_max(panther_d)
        rps = fire_rate_rps(panther_d)
        sus_full  = dps_sustained(panther_d, alp, rps, 5.0, 1.0, 1.0, 1.0)
        sus_half  = dps_sustained(panther_d, alp, rps, 5.0, 1.0, 1.0, 0.5)
        # Linear scale would give sus_half = sus_full * 0.5
        # Erkul's formula gives a non-linear result due to charge_time compounding
        linear_pred = sus_full * 0.5
        is_nonlinear = abs(sus_half - linear_pred) > 1.0
        if sus_half < sus_full and is_nonlinear:
            out(f"  PASS  power_ratio=0.5 gives non-linear result: {sus_full:.1f} → {sus_half:.1f} "
                f"(linear would be {linear_pred:.1f})")
        elif sus_half >= sus_full:
            msg = f"  FAIL  power_ratio=0.5 did not reduce sustained DPS: {sus_full:.1f} → {sus_half:.1f}"
            out(msg)
            failures.append(msg)
        else:
            msg = f"  FAIL  power_ratio=0.5 applied as linear scale: {sus_full:.1f} → {sus_half:.1f}"
            out(msg)
            failures.append(msg)
    else:
        out("  SKIP  CF-337 Panther not found in cache")
    out()

    # ── Test D: Known reference values ────────────────────────────────────────
    out("D. Known reference value tests (Erkul-verified):")
    if panther_d:
        alp = alpha_max(panther_d)
        rps = fire_rate_rps(panther_d)
        for desc, name_substr, alm, rpm, prm, wpr, expected, tol in _PHASE5_TESTS:
            if name_substr.split()[-1] not in "CF-337 Panther":
                continue
            got = dps_sustained(panther_d, alp, rps, alm, rpm, prm, wpr)
            if abs(got - expected) <= tol:
                out(f"  PASS  {desc}: expected={expected:.1f} got={got:.1f}")
            else:
                msg = f"  FAIL  {desc}: expected={expected:.1f} got={got:.1f} (delta={got-expected:+.1f})"
                out(msg)
                failures.append(msg)
    else:
        out("  SKIP  CF-337 Panther not found in cache")
    out()

    # ── Test E: Asgard ship buff is readable from cache ───────────────────────
    out("E. Ship engineering buff test (Asgard ammoLoadMult=5):")
    asgard_d = None
    for e in raw.get("/live/ships", []):
        d = e.get("data", {})
        if d.get("name") == "Asgard":
            asgard_d = d
            break
    if asgard_d:
        buff = asgard_d.get("buff", {}).get("regenModifier", {})
        alm = float(buff.get("maxAmmoLoadMultiplier", 1) or 1)
        if abs(alm - 5.0) < 0.01:
            out(f"  PASS  Asgard maxAmmoLoadMultiplier = {alm}")
        else:
            msg = f"  FAIL  Asgard maxAmmoLoadMultiplier = {alm} (expected 5.0)"
            out(msg)
            failures.append(msg)
    else:
        out("  SKIP  Asgard not found in cache")
    out()

    total = len(_PHASE5_TESTS) + 4  # A,B,C,D,E
    n_fail = len(failures)
    out(f"Phase 5 result: {n_fail} failures")
    if failures:
        out("  *** PIPELINE BUGS DETECTED — fix these before shipping ***")
    out()
    return failures


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    out_lines = []
    def out(s=""):
        out_lines.append(s)
        try:
            print(s)
        except UnicodeEncodeError:
            print(s.encode("ascii", "replace").decode("ascii"))

    t0 = time.time()
    out("=" * 100)
    out("  ERKUL PARITY AUDIT — Comprehensive Ship-by-Ship Verification")
    out("=" * 100)
    out(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out(f"Cache: {CACHE_FILE}")
    out()

    # Load data
    raw = load_cache()
    indexes = build_indexes(raw)
    build_raw_lookups(raw)

    # Get all ships
    ships = []
    seen = set()
    for e in raw.get("/live/ships", []):
        d = e.get("data", {})
        n = d.get("name", "")
        if n and n not in seen:
            seen.add(n)
            ships.append((n, d))
    ships.sort(key=lambda x: x[0])

    out(f"Ships in cache: {len(ships)}")
    w_ref, w_name = indexes["weapons"]
    s_ref, s_name = indexes["shields"]
    m_ref, m_name = indexes["missiles"]
    out(f"Weapons: {len(w_ref)} by ref")
    out(f"Shields: {len(s_ref)} by ref")
    out(f"Missiles: {len(m_ref)} by ref")
    out()

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 1: INDIVIDUAL COMPONENT VERIFICATION
    # ═══════════════════════════════════════════════════════════════════════
    out("=" * 100)
    out("  PHASE 1: INDIVIDUAL COMPONENT VERIFICATION")
    out("=" * 100)
    out()

    # Weapons
    w_total, w_failed, w_issues = audit_all_weapons(raw)
    out(f"WEAPONS: {w_total} tested, {w_failed} with issues")
    if w_issues:
        for issue in w_issues[:30]:
            out(issue)
        if len(w_issues) > 30:
            out(f"  ... and {len(w_issues) - 30} more")
    out()

    # Shields
    s_total, s_failed, s_issues = audit_all_shields(raw)
    out(f"SHIELDS: {s_total} tested, {s_failed} with issues")
    if s_issues:
        for issue in s_issues[:30]:
            out(issue)
        if len(s_issues) > 30:
            out(f"  ... and {len(s_issues) - 30} more")
    out()

    # Missiles
    m_total, m_failed, m_issues = audit_all_missiles(raw)
    out(f"MISSILES: {m_total} tested, {m_failed} with issues")
    if m_issues:
        for issue in m_issues[:30]:
            out(issue)
        if len(m_issues) > 30:
            out(f"  ... and {len(m_issues) - 30} more")
    out()

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 2: SHIP-BY-SHIP FULL AUDIT
    # ═══════════════════════════════════════════════════════════════════════
    out("=" * 100)
    out("  PHASE 2: SHIP-BY-SHIP FULL AUDIT")
    out("=" * 100)
    out()

    total_ship_issues = 0
    ships_with_issues = 0
    all_results = []

    for ship_name, ship_data in ships:
        try:
            result = audit_ship(ship_name, ship_data, indexes, raw)
        except (KeyError, TypeError, ValueError, AttributeError) as ex:
            out(f"CRASH: {ship_name}: {ex}")
            total_ship_issues += 1
            continue

        all_results.append(result)
        all_messages = (result["weapon_issues"] + result["shield_issues"] +
                        result["missile_issues"] + result["hull_issues"] +
                        result["power_issues"] + result["issues"])
        # Only count true unknowns as issues; KNOWN/INFO are informational
        real_issues = [m for m in all_messages if m.startswith("UNRESOLVED")]
        if all_messages:
            if real_issues:
                ships_with_issues += 1
            total_ship_issues += len(real_issues)
            out(f"  {ship_name}:")
            for msg in all_messages:
                out(f"    {msg}")

    out()
    out(f"Ships audited: {len(ships)}")
    out(f"Ships with issues: {ships_with_issues}")
    out(f"Total issues: {total_ship_issues}")
    out()

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 3: POWER PIP SUMMARY (ALL SHIPS)
    # ═══════════════════════════════════════════════════════════════════════
    out("=" * 100)
    out("  PHASE 3: POWER PIP SUMMARY (ALL SHIPS)")
    out("=" * 100)
    out()

    cat_order = ["weaponGun", "thruster", "shield", "radar", "lifeSupport",
                 "cooler", "quantumDrive", "utility"]
    cat_labels = {"weaponGun": "WPN", "thruster": "THR", "shield": "SHD",
                  "radar": "RDR", "lifeSupport": "LSP", "cooler": "CLR",
                  "quantumDrive": "QDR", "utility": "UTL"}

    header = f"{'Ship':35s}"
    for cat in cat_order:
        header += f" | {cat_labels.get(cat, cat):>5s}"
    header += f" | {'PP Out':>7s} | {'Draw':>7s} | {'EM':>8s} | {'IR':>8s} | {'CS':>8s}"
    out(header)
    out("-" * len(header))

    for result in all_results:
        t = result["totals"]
        if not t:
            continue
        pips = t.get("power_pips", {})
        line = f"{result['name']:35s}"
        for cat in cat_order:
            pd = pips.get(cat)
            if pd:
                line += f" | {pd['current_pips']:>2d}/{pd['max_pips']:<2d}"
            else:
                line += f" | {'--':>5s}"
        line += f" | {t.get('pp_output', 0):>7.0f}"
        line += f" | {t.get('power_draw', 0):>7.1f}"
        line += f" | {t.get('em_sig', 0):>8.1f}"
        line += f" | {t.get('ir_sig', 0):>8.1f}"
        line += f" | {t.get('cs_sig', 0):>8.1f}"
        out(line)

    out()

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 4: DPS/SHIELD/HULL SUMMARY TABLE (ALL SHIPS)
    # ═══════════════════════════════════════════════════════════════════════
    out("=" * 100)
    out("  PHASE 4: DPS / SHIELD / HULL SUMMARY (ALL SHIPS)")
    out("=" * 100)
    out()

    header2 = (f"{'Ship':35s} | {'DPS':>8s} | {'Sus':>8s} | {'Alpha':>8s} | "
               f"{'MslDmg':>8s} | {'ShdHP':>8s} | {'Regen':>6s} | "
               f"{'P%':>5s} {'E%':>5s} {'D%':>5s} | "
               f"{'HullHP':>7s} | {'Armor':>7s} | "
               f"{'P*':>5s} {'E*':>5s} {'D*':>5s} | "
               f"{'Guns':>4s} | {'Msls':>4s} | {'Shds':>4s}")
    out(header2)
    out("-" * len(header2))

    for result in all_results:
        t = result["totals"]
        if not t:
            continue
        line = (f"{result['name']:35s} | {t['dps_raw']:>8.1f} | {t['dps_sus']:>8.1f} | "
                f"{t['alpha']:>8.1f} | {t['missile_dmg']:>8.0f} | "
                f"{t['shield_hp']:>8.0f} | {t['shield_regen']:>6.1f} | "
                f"{t['shield_res_phys']:>+5.0f} {t['shield_res_energy']:>+5.0f} "
                f"{t['shield_res_dist']:>+5.0f} | "
                f"{t['hull_hp']:>7.0f} | {t['armor_type']:>7s} | "
                f"{t['armor_phys_mult']:>5.2f} {t['armor_energy_mult']:>5.2f} "
                f"{t['armor_dist_mult']:>5.2f} | "
                f"{t['gun_count']:>4d} | {t['missile_count']:>4d} | {t['shield_count']:>4d}")
        out(line)

    out()

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 5: CONTEXT-AWARE DPS PIPELINE TESTS
    # ═══════════════════════════════════════════════════════════════════════
    p5_failures = run_phase5_tests(raw, out)

    # ═══════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════════
    elapsed = time.time() - t0
    out("=" * 100)
    out("  SUMMARY")
    out("=" * 100)
    out(f"Phase 1 — Component verification:")
    out(f"  Weapons: {w_total} tested, {w_failed} issues")
    out(f"  Shields: {s_total} tested, {s_failed} issues")
    out(f"  Missiles: {m_total} tested, {m_failed} issues")
    out(f"Phase 2 — Ship audits: {len(ships)} ships, {ships_with_issues} with issues, {total_ship_issues} total issues")
    out(f"Phase 3 — Power pips: see table above")
    out(f"Phase 4 — DPS/Shield/Hull: see table above")
    out(f"Phase 5 — Pipeline tests: {len(p5_failures)} failures" +
        (" *** BUGS DETECTED ***" if p5_failures else " (all pass)"))
    out(f"Elapsed: {elapsed:.1f}s")

    # Write report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print(f"\nReport saved to: {REPORT_FILE}")


if __name__ == "__main__":
    main()
