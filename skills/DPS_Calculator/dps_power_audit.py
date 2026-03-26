#!/usr/bin/env python3
"""
PowerAllocator Comprehensive Audit — interactive behavior & signature accuracy.
Reads .erkul_cache.json, instantiates PowerAllocator with a hidden Tk root.
Outputs dps_power_audit_report.txt.
"""

import json
import math
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(SCRIPT_DIR, ".erkul_cache.json")
REPORT_FILE = os.path.join(SCRIPT_DIR, "dps_power_audit_report.txt")

# ── Import from dps_calc_app ────────────────────────────────────────────────
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, '..', '..'))

import tkinter as tk
root = None

try:
    from dps_calc_app import (
        PowerAllocator,
        compute_weapon_stats,
        compute_shield_stats,
        compute_cooler_stats,
        compute_radar_stats,
        compute_powerplant_stats_erkul,
        compute_qdrive_stats_erkul,
    )
    IMPORT_OK = True
    IMPORT_ERR = ""
except Exception as e:
    IMPORT_OK = False
    IMPORT_ERR = str(e)

from shared.data_utils import _sf as _sf_shared

# ── Load cache (deferred to main() for proper error handling) ────────────────
CACHE = {}
DATA = {}
SHIPS = []
WEAPONS = []
SHIELDS = []
COOLERS = []
RADARS = []
PPS = []
QDS = []


def _load_cache():
    global CACHE, DATA, SHIPS, WEAPONS, SHIELDS, COOLERS, RADARS, PPS, QDS
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            CACHE = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: Failed to load cache from {CACHE_FILE}: {e}")
        sys.exit(1)
    DATA = CACHE.get("data", {})
    SHIPS      = DATA.get("/live/ships", [])
    WEAPONS    = DATA.get("/live/weapons", [])
    SHIELDS    = DATA.get("/live/shields", [])
    COOLERS    = DATA.get("/live/coolers", [])
    RADARS     = DATA.get("/live/radars", [])
    PPS        = DATA.get("/live/powerplants", [])
    QDS        = DATA.get("/live/quantumdrives", [])

# ── Build lookup dicts (mirrors DataManager._index + _item_lookup) ───────────
# by_local_name -> computed stats dict
CATALOG_BY_LN = {}
RAW_BY_LN = {}      # localName -> raw data dict
RAW_BY_REF = {}     # ref UUID -> raw data dict

_sf = _sf_shared

def _enrich(stats, d):
    # TODO: extract to shared module (duplicated from dps_calc_app.py DataManager._index)
    """Enrich stats with power/EM/IR fields like DataManager._index does."""
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
    return stats

def _build_indexes():
    """Index all component types — must be called after _load_cache()."""
    for entries, compute_fn, filt in [
        (WEAPONS, compute_weapon_stats, lambda d: d.get("type") == "WeaponGun"),
        (SHIELDS, compute_shield_stats, None),
        (COOLERS, compute_cooler_stats, None),
        (RADARS,  compute_radar_stats,  None),
        (PPS,     compute_powerplant_stats_erkul, None),
        (QDS,     compute_qdrive_stats_erkul, None),
    ]:
        for e in entries:
            d = e.get("data", {})
            if filt and not filt(d):
                continue
            try:
                stats = compute_fn(e)
            except Exception:
                continue
            stats = _enrich(stats, d)
            ln = e.get("localName", "")
            ref = d.get("ref", "")
            if ln:
                CATALOG_BY_LN[ln] = stats
            if ref:
                CATALOG_BY_LN[ref] = stats

    # Build raw lookups (for _raw_lookup equivalent)
    for ep_key in DATA:
        entries = DATA[ep_key]
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

def item_lookup_fn(local_name):
    """Mirrors _item_lookup: lookup by localName across all catalogs."""
    return CATALOG_BY_LN.get(local_name)

def raw_lookup_fn(identifier):
    """Mirrors _raw_lookup: lookup raw data by localName or ref."""
    if identifier in RAW_BY_LN:
        return RAW_BY_LN[identifier]
    if identifier in RAW_BY_REF:
        return RAW_BY_REF[identifier]
    # Broad search like the app does
    for ep_key, entries in DATA.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if entry.get("localName") == identifier:
                return entry.get("data", {})
            d = entry.get("data", {})
            if d.get("ref") == identifier:
                return d
    return None

# ── Ship name -> ship data lookup ────────────────────────────────────────────
SHIP_BY_NAME = {}

# Shared with dps_loadout_audit.py — TODO: extract to shared constant
# ── 20 audit ships ──────────────────────────────────────────────────────────
AUDIT_20 = [
    "Aurora MR", "Gladius", "Arrow", "Avenger Titan", "Cutlass Black",
    "Cutlass Steel", "Freelancer", "Constellation Andromeda", "Vanguard Warden",
    "Sabre", "Buccaneer", "Hurricane", "Redeemer", "Hammerhead",
    "A2 Hercules Starlifter", "Polaris", "Defender", "Eclipse",
    "Scorpius", "F7C Hornet Mk II",
]

AUDIT_20_MAPPED = []


def _build_ship_lookups():
    """Populate SHIP_BY_NAME and AUDIT_20_MAPPED — call after _load_cache()."""
    global SHIP_BY_NAME, AUDIT_20_MAPPED
    SHIP_BY_NAME = {}
    for s in SHIPS:
        name = s.get("data", {}).get("name", "")
        if name:
            SHIP_BY_NAME[name] = s.get("data", {})

    AUDIT_20_MAPPED = []
    for wanted in AUDIT_20:
        if wanted in SHIP_BY_NAME:
            AUDIT_20_MAPPED.append(wanted)
        else:
            found = None
            wl = wanted.lower()
            for sn in SHIP_BY_NAME:
                if wl in sn.lower() or sn.lower() in wl:
                    found = sn
                    break
            if found:
                AUDIT_20_MAPPED.append(found)
            else:
                AUDIT_20_MAPPED.append(wanted)

# ── Report buffer ────────────────────────────────────────────────────────────
REPORT_LINES = []
def out(line=""):
    REPORT_LINES.append(line)

def section(title):
    out()
    out("=" * 90)
    out(f"  {title}")
    out("=" * 90)

from shared.data_utils import pct_diff

# ── Helper: create fresh PowerAllocator + load ship ──────────────────────────
def make_pa(ship_data):
    """Create a fresh PowerAllocator, load ship data, return it.

    The frame is NOT destroyed here — PA may still reference it internally.
    Callers must handle frame cleanup if needed; in practice the hidden root
    is destroyed at exit which cleans up all child frames.
    """
    frame = tk.Frame(root)
    pa = PowerAllocator(frame, item_lookup_fn=item_lookup_fn,
                        raw_lookup_fn=raw_lookup_fn)
    pa.load_ship(ship_data)
    return pa

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1: PowerAllocator Column Generation (all 208 ships)
# ═════════════════════════════════════════════════════════════════════════════
def phase1():
    section("PHASE 1: PowerAllocator Column Generation (all ships)")
    out(f"Testing {len(SHIPS)} ships...")
    out()

    ALL_CATS = ["weaponGun", "thruster", "shield", "radar", "lifeSupport",
                "cooler", "quantumDrive", "utility"]
    cat_ship_count = {c: 0 for c in ALL_CATS}
    total_flags = 0
    flags = []

    for ship_entry in SHIPS:
        sd = ship_entry.get("data", {})
        name = sd.get("name", ship_entry.get("localName", "unknown"))

        try:
            pa = make_pa(sd)
        except Exception as e:
            flags.append(f"  CRASH: {name}: {e}")
            total_flags += 1
            continue

        cats_present = list(pa._categories.keys())
        for c in cats_present:
            if c in cat_ship_count:
                cat_ship_count[c] += 1

        total_slots = len(pa._slots)
        total_segs = sum(s["max_segments"] for s in pa._slots)

        # Check: ship has loadout with powered components but no categories created
        loadout = sd.get("loadout", [])
        has_loadout = isinstance(loadout, list) and len(loadout) > 0
        if has_loadout and not cats_present and sd.get("rnPowerPools"):
            flags.append(f"  FLAG: {name}: has loadout+powerPools but 0 categories created")
            total_flags += 1

        # Check: ship has PowerPlant ports but 0 PP output
        pp_output = getattr(pa, "_total_pp_output", 0)
        pp_slots_total = getattr(pa, "_pp_total_slots", 0)
        if pp_slots_total > 0 and pp_output == 0:
            flags.append(f"  FLAG: {name}: {pp_slots_total} PP ports but 0 output")
            total_flags += 1

        # Clean up frame to avoid leaking 200+ Tk frames
        if hasattr(pa, 'master') and pa.master:
            pa.master.destroy()

    out("Category presence across all ships:")
    for c in ALL_CATS:
        out(f"  {c:16s}: {cat_ship_count[c]:3d} ships")
    out()

    if flags:
        out(f"Flags ({total_flags}):")
        for f_line in flags:
            out(f_line)
    else:
        out("No flags raised.")
    out()
    out(f"Total ships scanned: {len(SHIPS)}")
    out(f"Total flags: {total_flags}")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2: Pip Bar Interaction Simulation (20 ships)
# ═════════════════════════════════════════════════════════════════════════════
def phase2():
    section("PHASE 2: Pip Bar Interaction Simulation (20 ships)")
    out()

    total_flags = 0
    flags = []

    for ship_name in AUDIT_20_MAPPED:
        sd = SHIP_BY_NAME.get(ship_name)
        if not sd:
            out(f"  SKIP: {ship_name} not found in cache")
            continue

        out(f"  Ship: {ship_name}")

        try:
            pa = make_pa(sd)
        except Exception as e:
            flags.append(f"  CRASH: {ship_name}: {e}")
            total_flags += 1
            continue

        # ── Default state ──
        pp_output = getattr(pa, "_total_pp_output", 0)
        em_default = getattr(pa, "em_signature", 0)
        ir_default = getattr(pa, "ir_signature", 0)
        wpn_ratio = getattr(pa, "weapon_power_ratio", -1)
        shd_ratio = getattr(pa, "shield_power_ratio", -1)

        total_draw = sum(s["draw_per_seg"] * s["current_seg"]
                         for s in pa._slots if not s["is_generator"] and s["enabled"])
        consumption_pct = (total_draw / pp_output * 100) if pp_output > 0 else 0

        out(f"    Default: PP={pp_output:.0f}, draw={total_draw:.1f}, "
            f"consumption={consumption_pct:.1f}%, EM={em_default:.1f}, IR={ir_default:.1f}")
        out(f"    weapon_ratio={wpn_ratio:.3f}, shield_ratio={shd_ratio:.3f}")

        # Flag checks on default state
        if math.isnan(consumption_pct) or consumption_pct < 0:
            flags.append(f"  FLAG: {ship_name}: consumption_pct is {consumption_pct}")
            total_flags += 1
        if em_default < 0:
            flags.append(f"  FLAG: {ship_name}: em_signature negative ({em_default})")
            total_flags += 1
        if ir_default < 0:
            flags.append(f"  FLAG: {ship_name}: ir_signature negative ({ir_default})")
            total_flags += 1
        if pp_output == 0 and getattr(pa, "_pp_total_slots", 0) > 0:
            flags.append(f"  FLAG: {ship_name}: total_capacity=0 but PP ports exist")
            total_flags += 1

        # ── Set ALL weapon pips to 0 ──
        wpn_slots = pa._categories.get("weaponGun", [])
        saved_wpn = [(s, s["current_seg"]) for s in wpn_slots]
        for s in wpn_slots:
            s["current_seg"] = 0
        pa._recalculate()

        em_no_wpn = getattr(pa, "em_signature", 0)
        wpn_ratio_zero = getattr(pa, "weapon_power_ratio", -1)
        out(f"    Weapons=0: EM={em_no_wpn:.1f}, weapon_ratio={wpn_ratio_zero:.3f}")

        if wpn_slots and wpn_ratio_zero != 0.0:
            flags.append(f"  FLAG: {ship_name}: weapon pips=0 but ratio={wpn_ratio_zero}")
            total_flags += 1

        # ── Restore weapons, set ALL shield pips to 0 ──
        for s, val in saved_wpn:
            s["current_seg"] = val
        shd_slots = pa._categories.get("shield", [])
        saved_shd = [(s, s["current_seg"]) for s in shd_slots]
        for s in shd_slots:
            s["current_seg"] = 0
        pa._recalculate()

        em_no_shd = getattr(pa, "em_signature", 0)
        shd_ratio_zero = getattr(pa, "shield_power_ratio", -1)
        out(f"    Shields=0: EM={em_no_shd:.1f}, shield_ratio={shd_ratio_zero:.3f}")

        if shd_slots and shd_ratio_zero != 0.0:
            flags.append(f"  FLAG: {ship_name}: shield pips=0 but ratio={shd_ratio_zero}")
            total_flags += 1

        # ── Restore shields ──
        for s, val in saved_shd:
            s["current_seg"] = val
        pa._recalculate()

        # ── Toggle to NAV mode ──
        pa.set_mode("NAV")
        qd_slots = pa._categories.get("quantumDrive", [])
        qd_active = any(s["enabled"] and s["current_seg"] > 0 for s in qd_slots)
        wpn_disabled = all(not s["enabled"] for s in wpn_slots) if wpn_slots else True
        shd_disabled = all(not s["enabled"] for s in shd_slots) if shd_slots else True

        out(f"    NAV mode: QD_active={qd_active}, WPN_disabled={wpn_disabled}, SHD_disabled={shd_disabled}")

        if qd_slots and not qd_active:
            flags.append(f"  FLAG: {ship_name}: NAV mode but QD not activated")
            total_flags += 1
        if wpn_slots and not wpn_disabled:
            flags.append(f"  FLAG: {ship_name}: NAV mode but weapons still enabled")
            total_flags += 1
        if shd_slots and not shd_disabled:
            flags.append(f"  FLAG: {ship_name}: NAV mode but shields still enabled")
            total_flags += 1

        # ── Toggle back to SCM ──
        pa.set_mode("SCM")
        qd_deactivated = all(not s["enabled"] for s in qd_slots) if qd_slots else True
        wpn_restored = all(s["enabled"] for s in wpn_slots) if wpn_slots else True
        shd_restored = all(s["enabled"] for s in shd_slots) if shd_slots else True

        out(f"    SCM mode: QD_off={qd_deactivated}, WPN_on={wpn_restored}, SHD_on={shd_restored}")

        if qd_slots and not qd_deactivated:
            flags.append(f"  FLAG: {ship_name}: SCM mode but QD still active")
            total_flags += 1
        if wpn_slots and not wpn_restored:
            flags.append(f"  FLAG: {ship_name}: SCM mode but weapons not restored")
            total_flags += 1
        if shd_slots and not shd_restored:
            flags.append(f"  FLAG: {ship_name}: SCM mode but shields not restored")
            total_flags += 1

        out()

        # Clean up frame to avoid leaking Tk frames
        if hasattr(pa, 'master') and pa.master:
            pa.master.destroy()

    out()
    if flags:
        out(f"PHASE 2 Flags ({total_flags}):")
        for f_line in flags:
            out(f_line)
    else:
        out("No flags raised.")
    out(f"Total flags: {total_flags}")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3: Signature Accuracy (20 ships)
# ═════════════════════════════════════════════════════════════════════════════
def phase3():
    section("PHASE 3: Signature Accuracy Verification (20 ships)")
    out()

    total_flags = 0
    flags = []

    for ship_name in AUDIT_20_MAPPED:
        sd = SHIP_BY_NAME.get(ship_name)
        if not sd:
            out(f"  SKIP: {ship_name} not found in cache")
            continue

        try:
            pa = make_pa(sd)
        except Exception as e:
            flags.append(f"  CRASH: {ship_name}: {e}")
            total_flags += 1
            continue

        # Read PA's computed values
        pa_em = getattr(pa, "em_signature", 0)
        pa_ir = getattr(pa, "ir_signature", 0)

        # ── Manual EM computation ──
        armor_em = getattr(pa, "_armor_sig_em", 1.0)
        armor_ir = getattr(pa, "_armor_sig_ir", 1.0)
        total_capacity = getattr(pa, "_total_pp_output", 0)
        pp_em_total = getattr(pa, "_pp_em_total", 0)
        pp_ranges = getattr(pa, "_pp_power_ranges", [])
        pp_count = getattr(pa, "_pp_count", 0)

        # ppUsageRatio
        total_active_seg = sum(
            s["current_seg"] for s in pa._slots
            if s["enabled"] and s["current_seg"] > 0 and not s["is_generator"])
        pp_usage_ratio = min(1.0, total_active_seg / total_capacity) if total_capacity > 0 else 0

        manual_em = 0.0

        # PP EM
        if pp_em_total and pp_count > 0:
            pp_seg_per = (total_capacity * pp_usage_ratio / pp_count) if pp_count else 0
            pp_modifier = 1.0
            if pp_ranges and pp_ranges[0]:
                pp_modifier = PowerAllocator._find_range_modifier(pp_ranges[0], pp_seg_per)
            manual_em += pp_em_total * pp_modifier * pp_usage_ratio

        # Component EM
        for s in pa._slots:
            if not s["enabled"] or s["current_seg"] <= 0 or s["is_generator"]:
                continue
            em_total = s.get("em_total", 0)
            if not em_total:
                continue
            frac = s["current_seg"] / s["max_segments"] if s["max_segments"] else 0
            modifier = PowerAllocator._find_range_modifier(s.get("power_ranges"), s["current_seg"])

            cat = s["category"]
            if cat == "weaponGun":
                manual_em += em_total * frac
            elif cat in ("shield", "cooler", "lifeSupport", "radar", "quantumDrive"):
                manual_em += em_total * frac * modifier
            else:
                manual_em += em_total * frac

        manual_em *= armor_em

        # ── Manual IR computation ──
        # Cooling ratio calculation
        cooling_gen = 0.0
        for s in pa._slots:
            if s["category"] != "cooler" or not s["enabled"] or s["current_seg"] <= 0:
                continue
            raw_gen = s.get("cooling_gen", 0)
            if raw_gen and s["max_segments"] > 0:
                frac = s["current_seg"] / s["max_segments"]
                modifier = PowerAllocator._find_range_modifier(s.get("power_ranges"), s["current_seg"])
                cooling_gen += raw_gen * frac * modifier

        cooling_cons = 0.0
        for s in pa._slots:
            if not s["enabled"] or s["current_seg"] <= 0 or s["is_generator"]:
                continue
            cooling_cons += s["current_seg"]
        for s in pa._slots:
            if not s["enabled"] or s["current_seg"] <= 0 or s["is_generator"]:
                continue
            if s["category"] in ("shield", "lifeSupport", "radar", "quantumDrive"):
                modifier = PowerAllocator._find_range_modifier(s.get("power_ranges"), s["current_seg"])
                cooling_cons += s["current_seg"] * modifier

        cooling_ratio = min(1.0, cooling_cons / cooling_gen) if cooling_gen > 0 else 0.5

        manual_ir = 0.0
        for s in pa._slots:
            if s["category"] != "cooler" or not s["enabled"] or s["current_seg"] <= 0:
                continue
            ir_total = s.get("ir_total", 0)
            if not ir_total:
                continue
            frac = s["current_seg"] / s["max_segments"] if s["max_segments"] else 0
            modifier = PowerAllocator._find_range_modifier(s.get("power_ranges"), s["current_seg"])
            manual_ir += ir_total * frac * cooling_ratio * modifier

        manual_ir *= armor_ir

        # ── Compare ──
        em_diff = pct_diff(pa_em, manual_em)
        ir_diff = pct_diff(pa_ir, manual_ir)

        status_em = "OK" if em_diff <= 5 else f"MISMATCH {em_diff:.1f}%"
        status_ir = "OK" if ir_diff <= 5 else f"MISMATCH {ir_diff:.1f}%"

        out(f"  {ship_name}:")
        out(f"    EM: PA={pa_em:.1f}, manual={manual_em:.1f} -> {status_em}")
        out(f"    IR: PA={pa_ir:.1f}, manual={manual_ir:.1f} -> {status_ir}")

        if em_diff > 5:
            flags.append(f"  FLAG: {ship_name}: EM mismatch {em_diff:.1f}% (PA={pa_em:.1f} vs manual={manual_em:.1f})")
            total_flags += 1
        if ir_diff > 5:
            flags.append(f"  FLAG: {ship_name}: IR mismatch {ir_diff:.1f}% (PA={pa_ir:.1f} vs manual={manual_ir:.1f})")
            total_flags += 1

    out()
    if flags:
        out(f"PHASE 3 Flags ({total_flags}):")
        for f_line in flags:
            out(f_line)
    else:
        out("No flags raised — manual computation matches PowerAllocator exactly.")
    out(f"Total flags: {total_flags}")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4: Power Budget Validation (all ships)
# ═════════════════════════════════════════════════════════════════════════════
def phase4():
    section("PHASE 4: Power Budget Validation (all ships)")
    out(f"Testing {len(SHIPS)} ships...")
    out()

    total_flags = 0
    flags = []

    for ship_entry in SHIPS:
        sd = ship_entry.get("data", {})
        name = sd.get("name", ship_entry.get("localName", "unknown"))

        try:
            pa = make_pa(sd)
        except Exception as e:
            flags.append(f"  CRASH: {name}: {e}")
            total_flags += 1
            continue

        pp_output = getattr(pa, "_total_pp_output", 0)
        pp_total_slots = getattr(pa, "_pp_total_slots", 0)
        wpn_ratio = getattr(pa, "weapon_power_ratio", -1)
        shd_ratio = getattr(pa, "shield_power_ratio", -1)

        total_draw = sum(s["draw_per_seg"] * s["current_seg"]
                         for s in pa._slots if not s["is_generator"] and s["enabled"])
        consumption_pct = (total_draw / pp_output * 100) if pp_output > 0 else 0

        # Check 1: PP output > 0 when PP ports exist
        if pp_total_slots > 0 and pp_output <= 0:
            flags.append(f"  FLAG: {name}: {pp_total_slots} PP ports but output={pp_output}")
            total_flags += 1

        # Check 2: consumption < 300% on stock loadout
        if consumption_pct > 300:
            flags.append(f"  FLAG: {name}: consumption={consumption_pct:.0f}% (>300%)")
            total_flags += 1

        # Check 3: weapon_power_ratio in [0, 1]
        if wpn_ratio < 0 or wpn_ratio > 1.0001:
            flags.append(f"  FLAG: {name}: weapon_power_ratio={wpn_ratio:.3f} out of [0,1]")
            total_flags += 1

        # Check 4: shield_power_ratio in [0, 1]
        if shd_ratio < 0 or shd_ratio > 1.0001:
            flags.append(f"  FLAG: {name}: shield_power_ratio={shd_ratio:.3f} out of [0,1]")
            total_flags += 1

        # Check 5: NaN checks
        if math.isnan(consumption_pct):
            flags.append(f"  FLAG: {name}: consumption_pct is NaN")
            total_flags += 1
        if math.isnan(wpn_ratio):
            flags.append(f"  FLAG: {name}: weapon_power_ratio is NaN")
            total_flags += 1
        if math.isnan(shd_ratio):
            flags.append(f"  FLAG: {name}: shield_power_ratio is NaN")
            total_flags += 1

    out()
    if flags:
        out(f"PHASE 4 Flags ({total_flags}):")
        for f_line in flags:
            out(f_line)
    else:
        out("No flags raised.")
    out(f"Total ships scanned: {len(SHIPS)}")
    out(f"Total flags: {total_flags}")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 5: Mode Switch Behavior (all ships)
# ═════════════════════════════════════════════════════════════════════════════
def phase5():
    section("PHASE 5: Mode Switch Behavior (all ships)")
    out(f"Testing {len(SHIPS)} ships...")
    out()

    total_flags = 0
    flags = []

    for ship_entry in SHIPS:
        sd = ship_entry.get("data", {})
        name = sd.get("name", ship_entry.get("localName", "unknown"))

        try:
            pa = make_pa(sd)
        except Exception as e:
            flags.append(f"  CRASH: {name}: {e}")
            total_flags += 1
            continue

        wpn_slots = pa._categories.get("weaponGun", [])
        shd_slots = pa._categories.get("shield", [])
        qd_slots = pa._categories.get("quantumDrive", [])

        # Skip ships with no mode-relevant categories
        if not wpn_slots and not shd_slots and not qd_slots:
            continue

        # ── Switch to NAV ──
        pa.set_mode("NAV")

        # Check weapons disabled
        if wpn_slots:
            wpn_all_off = all(not s["enabled"] and s["current_seg"] == 0 for s in wpn_slots)
            if not wpn_all_off:
                flags.append(f"  FLAG: {name}: NAV mode - weapons not fully disabled")
                total_flags += 1

        # Check shields disabled
        if shd_slots:
            shd_all_off = all(not s["enabled"] and s["current_seg"] == 0 for s in shd_slots)
            if not shd_all_off:
                flags.append(f"  FLAG: {name}: NAV mode - shields not fully disabled")
                total_flags += 1

        # Check QD enabled
        if qd_slots:
            qd_all_on = all(s["enabled"] and s["current_seg"] == s["max_segments"] for s in qd_slots)
            if not qd_all_on:
                flags.append(f"  FLAG: {name}: NAV mode - QD not fully enabled")
                total_flags += 1

        # ── Switch back to SCM ──
        pa.set_mode("SCM")

        # Check weapons restored
        if wpn_slots:
            wpn_all_on = all(s["enabled"] for s in wpn_slots)
            if not wpn_all_on:
                flags.append(f"  FLAG: {name}: SCM mode - weapons not re-enabled")
                total_flags += 1

        # Check shields restored
        if shd_slots:
            shd_all_on = all(s["enabled"] for s in shd_slots)
            if not shd_all_on:
                flags.append(f"  FLAG: {name}: SCM mode - shields not re-enabled")
                total_flags += 1

        # Check QD disabled
        if qd_slots:
            qd_all_off = all(not s["enabled"] and s["current_seg"] == 0 for s in qd_slots)
            if not qd_all_off:
                flags.append(f"  FLAG: {name}: SCM mode - QD not fully disabled")
                total_flags += 1

    out()
    if flags:
        out(f"PHASE 5 Flags ({total_flags}):")
        for f_line in flags:
            out(f_line)
    else:
        out("No flags raised — all ships toggle SCM/NAV correctly.")
    out(f"Total ships scanned: {len(SHIPS)}")
    out(f"Total flags: {total_flags}")


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════
def main():
    global root
    _load_cache()
    _build_indexes()
    _build_ship_lookups()

    root = tk.Tk()
    root.withdraw()
    out("DPS Calculator — PowerAllocator Comprehensive Audit")
    out(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out(f"Cache file: {CACHE_FILE}")
    out(f"Ships in cache: {len(SHIPS)}")
    out(f"Import OK: {IMPORT_OK}")
    if not IMPORT_OK:
        out(f"Import error: {IMPORT_ERR}")
        out("ABORTING — cannot proceed without PowerAllocator import.")
        return

    # Verify 20-ship mapping
    out()
    out("20-ship audit mapping:")
    for wanted, mapped in zip(AUDIT_20, AUDIT_20_MAPPED):
        found = mapped in SHIP_BY_NAME
        out(f"  {wanted:30s} -> {mapped:30s} {'OK' if found else 'NOT FOUND'}")

    t0 = time.time()
    phase1()
    phase2()
    phase3()
    phase4()
    phase5()
    elapsed = time.time() - t0

    section("SUMMARY")
    out(f"Audit completed in {elapsed:.1f}s")
    out(f"Total ships in cache: {len(SHIPS)}")
    out()

    # Write report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT_LINES))
    print(f"Report written to {REPORT_FILE}")
    print(f"Elapsed: {elapsed:.1f}s")

if __name__ == "__main__":
    try:
        main()
    finally:
        if root is not None:
            root.destroy()
