#!/usr/bin/env python3
"""
Erkul Parity Audit — checks DPS Calculator compute functions against raw erkul cache data.
Reads .erkul_cache.json (no network), imports compute_* from dps_calc_app.py.
Outputs audit_report.txt.
"""

import json
import math
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(SCRIPT_DIR, ".erkul_cache.json")
REPORT_FILE = os.path.join(SCRIPT_DIR, "audit_report.txt")

# ── Import compute functions from dps_calc_app ──────────────────────────────
# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(SCRIPT_DIR, '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)
try:
    from dps_calc_app import (
        compute_weapon_stats,
        compute_shield_stats,
        compute_cooler_stats,
        compute_radar_stats,
        compute_powerplant_stats_erkul,
        compute_qdrive_stats_erkul,
        compute_missile_stats,
        _fire_rate_rps,
        _alpha_max,
        _dps_sustained,
        _dmg_breakdown,
        extract_slots_by_type,
    )
    IMPORT_OK = True
    IMPORT_ERR = ""
except (ImportError, ModuleNotFoundError) as e:
    IMPORT_OK = False
    IMPORT_ERR = str(e)

# ── Cache variables (loaded in main()) ────────────────────────────────────────
CACHE      = None
DATA       = None
SHIPS      = []
WEAPONS    = []
SHIELDS    = []
COOLERS    = []
RADARS     = []
PPS        = []
QDS        = []
MISSILES   = []

# Build lookup dicts  localName -> raw entry (populated in main())
CATALOG = {}

# ── Helpers ──────────────────────────────────────────────────────────────────
from shared.data_utils import pct_diff, safe_float, _sf

# ── Report buffer ────────────────────────────────────────────────────────────
REPORT_LINES = []
def out(line=""):
    REPORT_LINES.append(line)

def section(title):
    out()
    out("=" * 80)
    out(f"  {title}")
    out("=" * 80)

# ── PHASE 1: Power Allocator Category Coverage ──────────────────────────────
def phase1():
    section("PHASE 1: Power Allocator Category Coverage")
    out(f"Scanning {len(SHIPS)} ships for component resolution against catalog...")
    out()

    # NOTE: Must stay in sync with dps_calc_app.py POWERED_TYPES / UTILITY_TYPES
    COMPONENT_TYPES = {
        "Shield", "Cooler", "Radar", "LifeSupportGenerator", "QuantumDrive",
        "PowerPlant", "WeaponGun", "Turret", "MiningLaser", "SalvageHead",
        "TractorBeam", "UtilityTurret", "ToolArm", "MissileLauncher",
        "MainThruster", "ManneuverThruster", "WeaponDefensive",
    }

    total_ships = 0
    ships_with_issues = 0
    total_unresolved = 0
    category_presence = {}  # type -> count of ships having it

    for ship_entry in SHIPS:
        total_ships += 1
        ship_name = ship_entry.get("localName", "unknown")
        sd = ship_entry.get("data", {})
        loadout = sd.get("loadout", [])
        rn_pools = sd.get("rnPowerPools", {})
        items = sd.get("items", {})
        ifcs = sd.get("ifcs", {})

        # Collect all component types present
        found_types = set()
        unresolved = []

        def walk_loadout(ports, depth=0):
            for port in (ports or []):
                ptypes = port.get("itemTypes") or []
                type_names = {t.get("type", "") for t in ptypes} if ptypes else set()
                for tn in type_names:
                    if tn in COMPONENT_TYPES:
                        found_types.add(tn)

                ln = port.get("localName", "")
                lr = port.get("localReference", "")
                ref_to_check = ln or lr

                # Try to resolve against catalog
                if ref_to_check and type_names & {"Shield", "Cooler", "Radar",
                    "PowerPlant", "QuantumDrive", "WeaponGun", "MissileLauncher"}:
                    if ref_to_check not in CATALOG:
                        # Check children too — maybe the weapon is nested
                        children = port.get("loadout", [])
                        child_resolved = False
                        for child in children:
                            cln = child.get("localName", "")
                            clr = child.get("localReference", "")
                            cref = cln or clr
                            if cref and cref in CATALOG:
                                child_resolved = True
                                break
                        if not child_resolved and ref_to_check:
                            # Skip known non-component prefixes
                            skip_prefixes = ("controller_", "mount_", "turret_",
                                           "relay_", "vehicle_screen", "radar_display",
                                           "bmbrck_", "seat_", "dashboard_")
                            # Skip non-powered placeholders: caps, blanking plates,
                            # blade racks, fuel intakes, remote turret mounts
                            skip_substrings = ("_cap", "blanking", "blade_rack",
                                              "missilerack_blade", "missile_cap",
                                              "fuel_intake", "intk_",
                                              "_remote_top_turret")
                            ref_lower = ref_to_check.lower()
                            is_fake = "_fake" in ref_lower or "fake_" in ref_lower
                            is_skip_prefix = any(ref_to_check.startswith(p) for p in skip_prefixes)
                            is_skip_sub = any(s in ref_lower for s in skip_substrings)
                            # UUID-only gimbal/turret/torpedo mount refs: long UUID with
                            # no catalog entry — these are mount points, not actual components
                            is_uuid_mount = (len(ref_to_check) > 30
                                           and bool(re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-', ref_to_check))
                                           and type_names & {"WeaponGun", "Turret",
                                                            "BombLauncher", "MissileLauncher"})
                            if not (is_skip_prefix or is_skip_sub or is_fake or is_uuid_mount):
                                unresolved.append((port.get("itemPortName", "?"),
                                                 list(type_names), ref_to_check))

                # Recurse into children
                children = port.get("loadout", [])
                if children and depth < 5:
                    walk_loadout(children, depth + 1)

        walk_loadout(loadout)

        # Check rnPowerPools
        wpn_pool = rn_pools.get("weaponGun", {})
        if wpn_pool.get("poolSize", 0) > 0:
            found_types.add("weaponGun_pool")

        # Check ifcs thruster power
        ifcs_res = ifcs.get("resource", {}).get("online", {}).get("consumption", {})
        if ifcs_res.get("powerSegment", 0) > 0:
            found_types.add("thruster_power")

        # Check items dict
        if items.get("lifeSupports"):
            found_types.add("LifeSupportGenerator")
        if items.get("utilities"):
            found_types.add("utility_items")
        for pool_key in ["tractorBeam", "towingBeam", "weaponMining", "salvageHead"]:
            pool = rn_pools.get(pool_key, {})
            if pool.get("type") == "fixed" and pool.get("poolSize", 0) > 0:
                found_types.add(f"{pool_key}_pool")

        for ft in found_types:
            category_presence[ft] = category_presence.get(ft, 0) + 1

        if unresolved:
            ships_with_issues += 1
            total_unresolved += len(unresolved)
            if len(unresolved) <= 5:  # limit verbosity
                out(f"  [{ship_name}] {len(unresolved)} unresolved component(s):")
                for port_name, types, ref in unresolved[:3]:
                    out(f"    port={port_name} types={types} ref={ref}")
                if len(unresolved) > 3:
                    out(f"    ... and {len(unresolved)-3} more")

    out()
    out("--- Category Presence Summary ---")
    for cat, count in sorted(category_presence.items(), key=lambda x: -x[1]):
        out(f"  {cat:30s}: {count:4d} / {total_ships} ships")

    out()
    out(f"PHASE 1 SUMMARY: {total_ships} ships scanned, "
        f"{ships_with_issues} with unresolved components, "
        f"{total_unresolved} total unresolved refs")

    return ships_with_issues, total_unresolved


# ── PHASE 2: Component Stat Accuracy ────────────────────────────────────────
def phase2():
    section("PHASE 2: Component Stat Accuracy")
    if not IMPORT_OK:
        out(f"SKIPPED — import error: {IMPORT_ERR}")
        return 0

    THRESHOLD = 2.0  # percent
    discrepancies = []

    # --- Weapons ---
    out(f"\n  Checking {len(WEAPONS)} weapons...")
    for raw in WEAPONS:
        d = raw.get("data", {})
        name = d.get("name", "?")
        try:
            stats = compute_weapon_stats(raw)
        except (KeyError, TypeError, ValueError) as e:
            discrepancies.append(("weapon", name, f"compute error: {e}"))
            continue

        # Cross-check alpha from raw
        rps_raw = _fire_rate_rps(d)
        alpha_raw = _alpha_max(d)
        dps_raw_calc = alpha_raw * rps_raw
        sus_raw = _dps_sustained(d, alpha_raw, rps_raw)

        checks = [
            ("alpha", stats["alpha"], alpha_raw),
            ("rps", stats["rps"], rps_raw),
            ("dps_raw", stats["dps_raw"], dps_raw_calc),
            ("dps_sus", stats["dps_sus"], sus_raw),
        ]
        for label, computed, expected in checks:
            diff = pct_diff(computed, expected)
            if diff > THRESHOLD:
                discrepancies.append(("weapon", name,
                    f"{label}: computed={computed:.4f} vs raw={expected:.4f} ({diff:.1f}%)"))

    # --- Shields ---
    out(f"  Checking {len(SHIELDS)} shields...")
    for raw in SHIELDS:
        d = raw.get("data", {})
        name = d.get("name", "?")
        sh = d.get("shield", {})
        try:
            stats = compute_shield_stats(raw)
        except (KeyError, TypeError, ValueError) as e:
            discrepancies.append(("shield", name, f"compute error: {e}"))
            continue

        checks = [
            ("hp", stats["hp"], safe_float(sh.get("maxShieldHealth", 0))),
            ("regen", stats["regen"], safe_float(sh.get("maxShieldRegen", 0))),
            ("res_phys_min", stats["res_phys_min"],
             safe_float(sh.get("resistance", {}).get("physicalMin", 0))),
            ("res_phys_max", stats["res_phys_max"],
             safe_float(sh.get("resistance", {}).get("physicalMax", 0))),
            ("res_energy_min", stats["res_energy_min"],
             safe_float(sh.get("resistance", {}).get("energyMin", 0))),
            ("res_energy_max", stats["res_energy_max"],
             safe_float(sh.get("resistance", {}).get("energyMax", 0))),
            ("res_dist_min", stats["res_dist_min"],
             safe_float(sh.get("resistance", {}).get("distortionMin", 0))),
            ("res_dist_max", stats["res_dist_max"],
             safe_float(sh.get("resistance", {}).get("distortionMax", 0))),
        ]
        for label, computed, expected in checks:
            diff = pct_diff(safe_float(computed), expected)
            if diff > THRESHOLD:
                discrepancies.append(("shield", name,
                    f"{label}: computed={computed} vs raw={expected} ({diff:.1f}%)"))

    # --- Coolers ---
    out(f"  Checking {len(COOLERS)} coolers...")
    for raw in COOLERS:
        d = raw.get("data", {})
        name = d.get("name", "?")
        co = d.get("cooler", {})
        try:
            stats = compute_cooler_stats(raw)
        except (KeyError, TypeError, ValueError) as e:
            discrepancies.append(("cooler", name, f"compute error: {e}"))
            continue

        checks = [
            ("cooling_rate", stats["cooling_rate"], safe_float(co.get("coolingRate", 0))),
            ("suppression_heat", stats["suppression_heat"],
             safe_float(co.get("suppressionHeatFactor", 0))),
            ("suppression_ir", stats["suppression_ir"],
             safe_float(co.get("suppressionIRFactor", 0))),
        ]
        for label, computed, expected in checks:
            diff = pct_diff(safe_float(computed), expected)
            if diff > THRESHOLD:
                discrepancies.append(("cooler", name,
                    f"{label}: computed={computed} vs raw={expected} ({diff:.1f}%)"))

    # --- Radars ---
    out(f"  Checking {len(RADARS)} radars...")
    for raw in RADARS:
        d = raw.get("data", {})
        name = d.get("name", "?")
        rd = d.get("radar", {}) or {}
        try:
            stats = compute_radar_stats(raw)
        except (KeyError, TypeError, ValueError) as e:
            discrepancies.append(("radar", name, f"compute error: {e}"))
            continue

        # Radar data uses signatureDtection sub-keys, not detectionLifetimeMin/Max
        # The compute function reads rd.get("detectionLifetimeMin") etc.
        checks = [
            ("detection_min", stats["detection_min"],
             safe_float(rd.get("detectionLifetimeMin", 0))),
            ("detection_max", stats["detection_max"],
             safe_float(rd.get("detectionLifetimeMax", 0))),
        ]
        for label, computed, expected in checks:
            diff = pct_diff(safe_float(computed), expected)
            if diff > THRESHOLD:
                discrepancies.append(("radar", name,
                    f"{label}: computed={computed} vs raw={expected} ({diff:.1f}%)"))

    # --- Power Plants ---
    out(f"  Checking {len(PPS)} power plants...")
    for raw in PPS:
        d = raw.get("data", {})
        name = d.get("name", "?")
        res = d.get("resource", {}).get("online", {}).get("generation", {})
        sig = d.get("resource", {}).get("online", {}).get("signatureParams", {})
        try:
            stats = compute_powerplant_stats_erkul(raw)
        except (KeyError, TypeError, ValueError) as e:
            discrepancies.append(("powerplant", name, f"compute error: {e}"))
            continue

        checks = [
            ("output", stats["output"], safe_float(res.get("powerSegment", 0))),
            ("em_max", stats["em_max"],
             safe_float(sig.get("em", {}).get("nominalSignature", 0))),
            ("ir_max", stats["ir_max"],
             safe_float(sig.get("ir", {}).get("nominalSignature", 0))),
        ]
        for label, computed, expected in checks:
            diff = pct_diff(safe_float(computed), expected)
            if diff > THRESHOLD:
                discrepancies.append(("powerplant", name,
                    f"{label}: computed={computed} vs raw={expected} ({diff:.1f}%)"))

    # --- Quantum Drives ---
    out(f"  Checking {len(QDS)} quantum drives...")
    for raw in QDS:
        d = raw.get("data", {})
        name = d.get("name", "?")
        qd = d.get("qdrive", d.get("quantumDrive", d.get("quantumdrive", {}))) or {}
        params = qd.get("params", qd.get("standardJump", {})) or {}
        try:
            stats = compute_qdrive_stats_erkul(raw)
        except (KeyError, TypeError, ValueError) as e:
            discrepancies.append(("qdrive", name, f"compute error: {e}"))
            continue

        raw_speed = safe_float(params.get("driveSpeed", qd.get("speed", 0)))
        raw_spool = safe_float(params.get("spoolUpTime", qd.get("spoolUpTime", 0)))

        checks = [
            ("speed", stats["speed"], raw_speed),
            ("spool", stats["spool"], raw_spool),
        ]
        for label, computed, expected in checks:
            diff = pct_diff(safe_float(computed), expected)
            if diff > THRESHOLD:
                discrepancies.append(("qdrive", name,
                    f"{label}: computed={computed} vs raw={expected} ({diff:.1f}%)"))

    # --- Missiles ---
    out(f"  Checking {len(MISSILES)} missiles...")
    for raw in MISSILES:
        d = raw.get("data", {})
        name = d.get("name", "?")
        ms = d.get("missile", {}) or {}
        dmg = ms.get("damage", {}) or {}
        total_raw = sum(v for v in dmg.values() if isinstance(v, (int, float)))
        try:
            stats = compute_missile_stats(raw)
        except (KeyError, TypeError, ValueError) as e:
            discrepancies.append(("missile", name, f"compute error: {e}"))
            continue

        checks = [
            ("total_dmg", stats["total_dmg"], total_raw),
        ]
        for label, computed, expected in checks:
            diff = pct_diff(safe_float(computed), expected)
            if diff > THRESHOLD:
                discrepancies.append(("missile", name,
                    f"{label}: computed={computed} vs raw={expected} ({diff:.1f}%)"))

    # Report
    out()
    if discrepancies:
        out(f"--- {len(discrepancies)} Discrepancies Found (>{THRESHOLD}% threshold) ---")
        by_type = {}
        for cat, name, detail in discrepancies:
            by_type.setdefault(cat, []).append((name, detail))
        for cat in sorted(by_type):
            items = by_type[cat]
            out(f"\n  [{cat.upper()}] {len(items)} issue(s):")
            for name, detail in items[:20]:
                out(f"    {name}: {detail}")
            if len(items) > 20:
                out(f"    ... and {len(items)-20} more")
    else:
        out("  No discrepancies found. All compute functions match raw data within 2%.")

    out()
    out(f"PHASE 2 SUMMARY: Checked {len(WEAPONS)} weapons, {len(SHIELDS)} shields, "
        f"{len(COOLERS)} coolers, {len(RADARS)} radars, {len(PPS)} power plants, "
        f"{len(QDS)} quantum drives, {len(MISSILES)} missiles. "
        f"{len(discrepancies)} discrepancies.")

    return len(discrepancies)


# ── PHASE 3: Missing Table Columns ──────────────────────────────────────────
def phase3():
    section("PHASE 3: Missing Table Columns")
    out("Checking if column specs cover all stats erkul exposes...\n")

    missing = []

    # Check cooler: does COOLER_TABLE_COLS include ir_max?
    # From raw data: cooler has resource.online.signatureParams.ir.nominalSignature
    # compute_cooler_stats does NOT return ir_max or em_max
    # COOLER_TABLE_COLS references: name, class, cooling_rate, power_draw, em_max, hp
    # But compute_cooler_stats only returns: name, local_name, ref, size, cooling_rate,
    #   suppression_heat, suppression_ir
    out("  COOLER_TABLE_COLS:")
    # Check what erkul has for coolers
    if COOLERS:
        sample = COOLERS[0]["data"]
        sig = sample.get("resource", {}).get("online", {}).get("signatureParams", {})
        ir_sig = sig.get("ir", {}).get("nominalSignature", 0)
        em_sig = sig.get("em", {}).get("nominalSignature", 0)
        power_draw = sample.get("resource", {}).get("online", {}).get(
            "consumption", {}).get("powerSegment", 0)
        if ir_sig:
            out(f"    MISSING: ir_max (erkul has ir.nominalSignature={ir_sig} "
                f"but COOLER_TABLE_COLS references ir not in compute_cooler_stats output)")
            missing.append("COOLER: ir_max")
        # Check if em_max is in compute output
        # compute_cooler_stats does NOT return em_max — but COOLER_TABLE_COLS references it
        out(f"    NOTE: COOLER_TABLE_COLS references 'em_max' and 'power_draw' — "
            f"these are NOT in compute_cooler_stats() return dict")
        out(f"      erkul raw: em={em_sig}, power_draw={power_draw}")
        missing.append("COOLER: em_max not in compute_cooler_stats (but in column spec)")
        missing.append("COOLER: power_draw not in compute_cooler_stats (but in column spec)")
        missing.append("COOLER: hp not in compute_cooler_stats (but in column spec)")

    # Check radar: does RADAR_TABLE_COLS include power_draw?
    out("\n  RADAR_TABLE_COLS:")
    if RADARS:
        sample = RADARS[0]["data"]
        power_draw = sample.get("resource", {}).get("online", {}).get(
            "consumption", {}).get("powerSegment", 0)
        sig = sample.get("resource", {}).get("online", {}).get("signatureParams", {})
        em_sig = sig.get("em", {}).get("nominalSignature", 0)
        # RADAR_TABLE_COLS has: name, class, detection_min, detection_max, hp
        # Missing: power_draw, em
        if power_draw:
            out(f"    MISSING: power_draw (erkul has powerSegment={power_draw})")
            missing.append("RADAR: power_draw")
        if em_sig:
            out(f"    MISSING: em_max (erkul has em.nominalSignature={em_sig})")
            missing.append("RADAR: em_max")

    # Check shield: does SHIELD_TABLE_COLS include power_draw, em_max?
    out("\n  SHIELD_TABLE_COLS:")
    if SHIELDS:
        sample = SHIELDS[0]["data"]
        power_draw = sample.get("resource", {}).get("online", {}).get(
            "consumption", {}).get("powerSegment", 0)
        sig = sample.get("resource", {}).get("online", {}).get("signatureParams", {})
        em_sig = sig.get("em", {}).get("nominalSignature", 0)
        # SHIELD_TABLE_COLS has: name, class, hp, regen, res_phys_max, res_energy_max, res_dist_max
        # Missing: power_draw, em_max
        if power_draw:
            out(f"    MISSING: power_draw (erkul has powerSegment={power_draw})")
            missing.append("SHIELD: power_draw")
        if em_sig:
            out(f"    MISSING: em_max (erkul has em.nominalSignature={em_sig})")
            missing.append("SHIELD: em_max")

    # Check PP: does PP_COLS include ir_max?
    out("\n  PP_COLS:")
    if PPS:
        sample = PPS[0]["data"]
        sig = sample.get("resource", {}).get("online", {}).get("signatureParams", {})
        ir_sig = sig.get("ir", {}).get("nominalSignature", 0)
        # PP_COLS has: name, class, grade, output, em_max, hp
        # Missing: ir_max
        if ir_sig:
            out(f"    MISSING: ir_max (erkul has ir.nominalSignature={ir_sig})")
            missing.append("PP: ir_max")
        # PP does have ir_max in compute_powerplant_stats_erkul but PP_COLS doesn't show it
        out(f"    NOTE: compute_powerplant_stats_erkul returns ir_max={ir_sig} "
            f"but PP_COLS does not display it")

    # Check QD_COLS for completeness
    out("\n  QD_COLS:")
    if QDS:
        sample = QDS[0]["data"]
        sig = sample.get("resource", {}).get("online", {}).get("signatureParams", {})
        em_sig = sig.get("em", {}).get("nominalSignature", 0)
        # QD_COLS has: name, class, grade, speed, jump_range, spool, cooldown, fuel_rate, power_draw, hp
        # em is not shown but is in compute function
        if em_sig:
            out(f"    NOTE: compute_qdrive_stats_erkul returns em_max={em_sig} "
                f"but QD_COLS does not display it")
            missing.append("QD: em_max (in compute, not in QD_COLS)")

    out()
    out(f"PHASE 3 SUMMARY: {len(missing)} missing/mismatched column(s):")
    for m in missing:
        out(f"  - {m}")

    return len(missing)


# ── PHASE 4: Power Allocator Math ────────────────────────────────────────────
def phase4():
    section("PHASE 4: Power Allocator Math")
    out("Computing power budget for 10 target ships...\n")

    # (search_key, display_name) — search_key matched against ship display name
    TARGET_SHIPS = [
        ("Gladius",),
        ("Avenger Titan",),
        ("Cutlass Black",),
        ("Constellation Andromeda",),
        ("Hammerhead",),
        ("Carrack",),
        ("Polaris",),
        ("Reclaimer",),
        ("Caterpillar",),
        ("Aurora MR",),
    ]

    # Build ship lookup by display name match
    ship_lookup = {}
    for target_tuple in TARGET_SHIPS:
        search = target_tuple[0].lower()
        for s in SHIPS:
            sd = s.get("data", {})
            display = sd.get("name", "").lower()
            if display == search:
                ship_lookup[search] = s
                break
        # Fallback: partial match on localName
        if search not in ship_lookup:
            search_parts = search.replace(" ", "_")
            for s in SHIPS:
                ln = s.get("localName", "").lower()
                if search_parts in ln:
                    ship_lookup[search] = s
                    break

    issues = []

    for target_tuple in TARGET_SHIPS:
        target = target_tuple[0].lower()
        ship_entry = ship_lookup.get(target)
        if not ship_entry:
            out(f"  [{target}] NOT FOUND in cache")
            issues.append((target, "ship not found"))
            continue

        sd = ship_entry.get("data", {})
        ship_name = sd.get("name", ship_entry.get("localName", target))
        loadout = sd.get("loadout", [])

        out(f"  [{ship_name}] (localName={ship_entry.get('localName','')})")

        # Collect all component localNames/refs from loadout
        pp_output = 0.0
        total_draw = 0.0
        total_em = 0.0
        total_ir = 0.0
        total_cs = 0.0
        component_count = 0

        def walk_for_power(ports, depth=0):
            nonlocal pp_output, total_draw, total_em, total_ir, total_cs, component_count
            for port in (ports or []):
                ptypes = port.get("itemTypes") or []
                type_names = {t.get("type", "") for t in ptypes} if ptypes else set()

                ln = port.get("localName", "")
                lr = port.get("localReference", "")
                ref = ln or lr

                if ref and ref in CATALOG:
                    cat_type, cat_entry = CATALOG[ref]
                    cd = cat_entry.get("data", {})
                    res = cd.get("resource", {}).get("online", {})
                    sig = res.get("signatureParams", {})
                    cons = res.get("consumption", {})
                    gen = res.get("generation", {})

                    # Power generation (power plants)
                    if cat_type == "powerplant":
                        pp_out = safe_float(gen.get("powerSegment", 0))
                        pp_output += pp_out
                        em_val = safe_float(sig.get("em", {}).get("nominalSignature", 0))
                        ir_val = safe_float(sig.get("ir", {}).get("nominalSignature", 0))
                        total_em += em_val
                        total_ir += ir_val
                        component_count += 1

                    # Power consumption (everything else)
                    elif cat_type in ("shield", "cooler", "radar", "qdrive", "weapon"):
                        draw = safe_float(cons.get("powerSegment", 0))
                        total_draw += draw
                        em_val = safe_float(sig.get("em", {}).get("nominalSignature", 0))
                        ir_val = safe_float(sig.get("ir", {}).get("nominalSignature", 0))
                        total_em += em_val
                        total_ir += ir_val
                        component_count += 1

                # Recurse
                children = port.get("loadout", [])
                if children and depth < 5:
                    walk_for_power(children, depth + 1)

        walk_for_power(loadout)

        # Also add IFCS (thruster) power draw
        ifcs = sd.get("ifcs", {})
        ifcs_draw = safe_float(
            ifcs.get("resource", {}).get("online", {}).get(
                "consumption", {}).get("powerSegment", 0))
        total_draw += ifcs_draw

        # Cross-section from ship data
        cs_data = sd.get("crossSection", {})
        if isinstance(cs_data, dict):
            total_cs = safe_float(cs_data.get("value", cs_data.get("crossSection", 0)))

        # Compute consumption %
        if pp_output > 0:
            consumption_pct = (total_draw / pp_output) * 100.0
        else:
            consumption_pct = float('inf') if total_draw > 0 else 0.0

        out(f"    PP output:       {pp_output:>10,.0f}")
        out(f"    Total draw:      {total_draw:>10,.0f}")
        out(f"    Consumption:     {consumption_pct:>10.1f}%")
        out(f"    EM signature:    {total_em:>10,.0f}")
        out(f"    IR signature:    {total_ir:>10,.0f}")
        out(f"    CS:              {total_cs:>10,.1f}")
        out(f"    Components:      {component_count:>10d}")

        # Flag issues
        ship_issues = []
        if math.isnan(consumption_pct):
            ship_issues.append("NaN consumption percentage")
        if math.isinf(consumption_pct):
            ship_issues.append("Division by zero: PP output is 0 but draw > 0")
        if consumption_pct > 200:
            ship_issues.append(f"Consumption >200%: {consumption_pct:.1f}%")
        if component_count > 0 and total_em == 0:
            ship_issues.append("Zero EM signature despite having components")
        if pp_output == 0 and component_count > 0:
            ship_issues.append("Zero PP output but components present (PP not resolved?)")

        if ship_issues:
            for issue in ship_issues:
                out(f"    ** FLAG: {issue}")
                issues.append((ship_name, issue))
        else:
            out(f"    OK")
        out()

    out(f"PHASE 4 SUMMARY: {len(issues)} issue(s) flagged across "
        f"{len(TARGET_SHIPS)} ships checked")
    for name, issue in issues:
        out(f"  - {name}: {issue}")

    return len(issues)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    global CACHE, DATA, SHIPS, WEAPONS, SHIELDS, COOLERS, RADARS, PPS, QDS, MISSILES, CATALOG
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            CACHE = json.load(f)
        DATA = CACHE["data"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"ERROR: Failed to load cache from {CACHE_FILE}: {e}")
        sys.exit(1)
    SHIPS      = DATA.get("/live/ships", [])
    WEAPONS    = DATA.get("/live/weapons", [])
    SHIELDS    = DATA.get("/live/shields", [])
    COOLERS    = DATA.get("/live/coolers", [])
    RADARS     = DATA.get("/live/radars", [])
    PPS        = DATA.get("/live/powerplants", [])
    QDS        = DATA.get("/live/quantumdrives", [])
    MISSILES   = DATA.get("/live/missiles", [])
    CATALOG.clear()
    for label, lst in [("weapon", WEAPONS), ("shield", SHIELDS), ("cooler", COOLERS),
                       ("radar", RADARS), ("powerplant", PPS), ("qdrive", QDS), ("missile", MISSILES)]:
        for entry in lst:
            ln = entry.get("localName", "")
            ref = (entry.get("data", {}) or {}).get("ref", "")
            if ln:
                CATALOG[ln] = (label, entry)
            if ref:
                CATALOG[ref] = (label, entry)

    out("ERKUL PARITY AUDIT REPORT")
    out(f"Cache: {CACHE_FILE}")
    out(f"Game version: {CACHE.get('game_version', '?')}")
    out(f"Cache timestamp: {CACHE.get('ts', '?')}")
    out(f"Ships: {len(SHIPS)}, Weapons: {len(WEAPONS)}, Shields: {len(SHIELDS)}, "
        f"Coolers: {len(COOLERS)}, Radars: {len(RADARS)}, "
        f"PPs: {len(PPS)}, QDs: {len(QDS)}, Missiles: {len(MISSILES)}")

    if not IMPORT_OK:
        out(f"\nWARNING: Could not import from dps_calc_app: {IMPORT_ERR}")
        out("Phase 2 will be skipped. Other phases use raw data only.")

    p1_issues, p1_unresolved = phase1()
    p2_discrepancies = phase2()
    p3_missing = phase3()
    p4_issues = phase4()

    section("OVERALL SUMMARY")
    out(f"  Phase 1 (Category Coverage):   {p1_issues} ships with unresolved components "
        f"({p1_unresolved} total refs)")
    out(f"  Phase 2 (Stat Accuracy):       {p2_discrepancies} discrepancies")
    out(f"  Phase 3 (Missing Columns):     {p3_missing} missing column(s)")
    out(f"  Phase 4 (Power Allocator):     {p4_issues} issue(s)")
    total = p1_issues + p2_discrepancies + p3_missing + p4_issues
    out(f"\n  TOTAL ISSUES: {total}")

    # Write report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT_LINES) + "\n")
    print(f"Report written to {REPORT_FILE}")
    print(f"Total issues: {total}")

    # Print summary to console
    print("\n--- QUICK SUMMARY ---")
    print(f"  Phase 1: {p1_issues} ships with unresolved refs ({p1_unresolved} refs)")
    print(f"  Phase 2: {p2_discrepancies} stat discrepancies")
    print(f"  Phase 3: {p3_missing} missing columns")
    print(f"  Phase 4: {p4_issues} power math issues")


if __name__ == "__main__":
    main()
