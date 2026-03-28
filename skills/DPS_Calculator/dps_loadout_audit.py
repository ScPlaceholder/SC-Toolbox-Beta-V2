"""
DPS Calculator Loadout Audit — read-only audit of weapon slot extraction
and stock loadout accuracy across all ships in the erkul cache.
"""

import json
import os
import sys
import time

# ── Setup: import from dps_calc_app without launching GUI ────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(SCRIPT_DIR, '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from dps_calc_app import (
    extract_slots_by_type,
    compute_weapon_stats,
    compute_shield_stats,
    compute_cooler_stats,
    compute_radar_stats,
    compute_missile_stats,
    compute_powerplant_stats_erkul,
    compute_qdrive_stats_erkul,
    DataManager,
    CACHE_FILE,
)

REPORT_FILE = os.path.join(SCRIPT_DIR, "dps_loadout_audit_report.txt")

# Spot-check ships for Phase 5
SPOT_CHECK_SHIPS = [
    "Aurora MR", "Gladius", "Arrow", "Avenger Titan", "Cutlass Black",
    "Cutlass Steel", "Freelancer", "Constellation Andromeda", "Vanguard Warden",
    "Sabre", "Buccaneer", "Hurricane", "Redeemer", "Hammerhead",
    "A2 Hercules Starlifter", "Polaris", "Defender", "Eclipse",
    "Scorpius", "F7C Hornet Mk II",
]


def load_cache_raw() -> dict:
    """Load the raw cache data dict (not through DataManager.load which needs threading)."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            obj = json.load(f)
        return obj.get("data", {})
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Failed to load cache from {CACHE_FILE}: {e}") from e


def build_data_manager(raw: dict) -> DataManager:
    """Build a DataManager and populate its indexes from raw cache data (no network)."""
    dm = DataManager()
    dm.raw = raw

    from shared.data_enrichment import enrich_component_stats

    # Reproduce the indexing logic from DataManager.load()
    def _index(entries, compute_fn, by_ref, by_name, filt=None):
        for e in entries:
            d = e.get("data", {})
            if filt and not filt(d):
                continue
            try:
                stats = compute_fn(e)
            except (KeyError, TypeError, ValueError) as e_err:
                print(f"[AUDIT] Skipping entry: {e_err}")
                continue
            enrich_component_stats(stats, d)
            ref = stats["ref"]
            key = f"{stats['name'].lower()}_{stats['size']}"
            if ref:
                by_ref[ref] = stats
            by_name[key] = stats

    _index(raw.get("/live/weapons", []), compute_weapon_stats,
           dm.weapons_by_ref, dm.weapons_by_name,
           filt=lambda d: d.get("type") == "WeaponGun")
    _index(raw.get("/live/shields", []), compute_shield_stats,
           dm.shields_by_ref, dm.shields_by_name)
    _index(raw.get("/live/coolers", []), compute_cooler_stats,
           dm.coolers_by_ref, dm.coolers_by_name)
    _index(raw.get("/live/radars", []), compute_radar_stats,
           dm.radars_by_ref, dm.radars_by_name)
    _index(raw.get("/live/missiles", []), compute_missile_stats,
           dm.missiles_by_ref, dm.missiles_by_name)
    _index(raw.get("/live/powerplants", []), compute_powerplant_stats_erkul,
           dm.powerplants_by_ref, dm.powerplants_by_name)
    _index(raw.get("/live/quantumdrives", []), compute_qdrive_stats_erkul,
           dm.qdrives_by_ref, dm.qdrives_by_name)

    sbn = {}
    for e in raw.get("/live/ships", []):
        d = e.get("data", {})
        n = d.get("name", "")
        if n:
            sbn[n] = d
            sbn[n.lower()] = d
    dm.ships_by_name = sbn
    dm.loaded = True
    return dm


def get_all_ships(raw: dict) -> list:
    """Return list of (name, ship_data_dict) for all ships."""
    ships = []
    seen = set()
    for e in raw.get("/live/ships", []):
        d = e.get("data", {})
        n = d.get("name", "")
        if n and n not in seen:
            seen.add(n)
            ships.append((n, d))
    return sorted(ships, key=lambda x: x[0])


def main():
    out_lines = []
    def out(s=""):
        out_lines.append(s)

    t0 = time.time()
    out("=== DPS CALCULATOR LOADOUT AUDIT ===")
    out(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out()

    # Load data
    raw = load_cache_raw()
    dm = build_data_manager(raw)
    all_ships = get_all_ships(raw)
    out(f"Ships in cache: {len(all_ships)}")
    out(f"Weapons indexed: {len(dm.weapons_by_ref)} by ref, {len(dm.weapons_by_name)} by name")
    out(f"Missiles indexed: {len(dm.missiles_by_ref)} by ref, {len(dm.missiles_by_name)} by name")
    out(f"Shields indexed: {len(dm.shields_by_ref)} by ref, {len(dm.shields_by_name)} by name")
    out(f"Coolers indexed: {len(dm.coolers_by_ref)} by ref, {len(dm.coolers_by_name)} by name")
    out(f"Radars indexed: {len(dm.radars_by_ref)} by ref, {len(dm.radars_by_name)} by name")
    out(f"PowerPlants indexed: {len(dm.powerplants_by_ref)} by ref, {len(dm.powerplants_by_name)} by name")
    out(f"QuantumDrives indexed: {len(dm.qdrives_by_ref)} by ref, {len(dm.qdrives_by_name)} by name")
    out()

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 1: WEAPON SLOT EXTRACTION
    # ═══════════════════════════════════════════════════════════════════════════
    out("--- PHASE 1: WEAPON SLOT EXTRACTION ---")

    total_guns = 0
    guns_resolved = 0
    total_turrets = 0
    turrets_resolved = 0
    total_missiles = 0
    missiles_resolved = 0
    gun_failures = []       # (ship, slot_id, local_ref)
    missile_failures = []

    # Per-ship data for later phases
    ship_gun_slots = {}     # name -> list of slot dicts
    ship_missile_slots = {} # name -> list of slot dicts

    for ship_name, ship_data in all_ships:
        loadout = ship_data.get("loadout", [])
        if not loadout:
            continue

        # Weapon + turret slots
        wt_slots = extract_slots_by_type(loadout, {"WeaponGun", "Turret"})
        ship_gun_slots[ship_name] = wt_slots

        # Missile slots — collect gun slot IDs for dedup
        gun_ids = {s["id"] for s in wt_slots}
        ms_all = extract_slots_by_type(loadout, {"MissileLauncher"})
        ms_slots = [s for s in ms_all if s["id"] not in gun_ids]
        ship_missile_slots[ship_name] = ms_slots

        for slot in wt_slots:
            lr = slot.get("local_ref", "")
            label = slot.get("label", "")
            is_turret = "turret" in label.lower() or "Turret" in slot.get("id", "")

            if is_turret:
                total_turrets += 1
            else:
                total_guns += 1

            if lr:
                resolved = dm.find_weapon(lr)
                if resolved:
                    if is_turret:
                        turrets_resolved += 1
                    else:
                        guns_resolved += 1
                else:
                    gun_failures.append((ship_name, slot["id"], lr, label))
            # Empty local_ref = empty slot (no stock weapon), not a failure

        for slot in ms_slots:
            total_missiles += 1
            lr = slot.get("local_ref", "")
            if lr:
                resolved = dm.find_missile(lr)
                if resolved:
                    missiles_resolved += 1
                else:
                    missile_failures.append((ship_name, slot["id"], lr, slot.get("label", "")))

    out(f"Total gun slots: {total_guns}")
    out(f"  With stock weapon (non-empty local_ref): resolved {guns_resolved}")
    out(f"Total turret slots: {total_turrets}")
    out(f"  With stock weapon (non-empty local_ref): resolved {turrets_resolved}")
    out(f"Total missile slots: {total_missiles}")
    out(f"  With stock missile (non-empty local_ref): resolved {missiles_resolved}")
    out()

    if gun_failures:
        out(f"GUN/TURRET RESOLUTION FAILURES ({len(gun_failures)}):")
        for ship, sid, lr, label in gun_failures[:50]:
            out(f"  {ship:30s} | {label:30s} | ref={lr[:50]}")
        if len(gun_failures) > 50:
            out(f"  ... and {len(gun_failures) - 50} more")
        out()

    if missile_failures:
        out(f"MISSILE RESOLUTION FAILURES ({len(missile_failures)}):")
        for ship, sid, lr, label in missile_failures[:50]:
            out(f"  {ship:30s} | {label:30s} | ref={lr[:50]}")
        if len(missile_failures) > 50:
            out(f"  ... and {len(missile_failures) - 50} more")
        out()

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 2: STOCK LOADOUT COMPLETENESS
    # ═══════════════════════════════════════════════════════════════════════════
    out("--- PHASE 2: STOCK LOADOUT COMPLETENESS ---")

    comp_types = {
        "Shield":       (dm.find_shield,      {"Shield"}),
        "Cooler":       (dm.find_cooler,       {"Cooler"}),
        "Radar":        (dm.find_radar,        {"Radar"}),
        "PowerPlant":   (dm.find_powerplant,   {"PowerPlant"}),
        "QuantumDrive": (dm.find_qdrive,       {"QuantumDrive"}),
    }

    comp_totals = {k: {"total": 0, "resolved": 0, "failures": []} for k in comp_types}

    for ship_name, ship_data in all_ships:
        loadout = ship_data.get("loadout", [])
        if not loadout:
            continue

        for comp_name, (find_fn, type_set) in comp_types.items():
            slots = extract_slots_by_type(loadout, type_set)
            for slot in slots:
                comp_totals[comp_name]["total"] += 1
                lr = slot.get("local_ref", "")
                if lr:
                    resolved = find_fn(lr)
                    if resolved:
                        comp_totals[comp_name]["resolved"] += 1
                    else:
                        comp_totals[comp_name]["failures"].append(
                            (ship_name, slot["id"], lr, slot.get("label", ""))
                        )

    for comp_name in comp_types:
        t = comp_totals[comp_name]["total"]
        r = comp_totals[comp_name]["resolved"]
        pct = (r / t * 100) if t > 0 else 0
        out(f"{comp_name:15s}: {r}/{t} resolved ({pct:.1f}%)")

    out()
    for comp_name in comp_types:
        failures = comp_totals[comp_name]["failures"]
        if failures:
            out(f"{comp_name} RESOLUTION FAILURES ({len(failures)}):")
            for ship, sid, lr, label in failures[:30]:
                out(f"  {ship:30s} | {label:30s} | ref={lr[:60]}")
            if len(failures) > 30:
                out(f"  ... and {len(failures) - 30} more")
            out()

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 3: DUPLICATE SLOT DETECTION
    # ═══════════════════════════════════════════════════════════════════════════
    out("--- PHASE 3: DUPLICATE SLOTS ---")

    dup_count = 0
    overlap_count = 0

    for ship_name, ship_data in all_ships:
        loadout = ship_data.get("loadout", [])
        if not loadout:
            continue

        gun_slots = ship_gun_slots.get(ship_name, [])
        ms_slots = ship_missile_slots.get(ship_name, [])

        # Check for duplicate IDs within weapon+turret slots
        gun_ids_list = [s["id"] for s in gun_slots]
        gun_id_set = set()
        for gid in gun_ids_list:
            if gid in gun_id_set:
                out(f"  DUPLICATE gun slot: {ship_name} -> {gid}")
                dup_count += 1
            gun_id_set.add(gid)

        # Check for duplicate IDs within missile slots
        ms_ids_list = [s["id"] for s in ms_slots]
        ms_id_set = set()
        for mid in ms_ids_list:
            if mid in ms_id_set:
                out(f"  DUPLICATE missile slot: {ship_name} -> {mid}")
                dup_count += 1
            ms_id_set.add(mid)

        # Check for overlap between gun and missile slots
        # (should already be deduped, but verify)
        overlap = gun_id_set & ms_id_set
        if overlap:
            for oid in overlap:
                out(f"  OVERLAP gun/missile: {ship_name} -> {oid}")
                overlap_count += 1

    out(f"Duplicate slot IDs found: {dup_count}")
    out(f"Gun/missile overlaps found: {overlap_count}")
    out()

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 4: SLOT SIZE VALIDATION
    # ═══════════════════════════════════════════════════════════════════════════
    out("--- PHASE 4: SIZE VALIDATION ---")

    size_violations = 0
    size_details = []

    for ship_name, ship_data in all_ships:
        loadout = ship_data.get("loadout", [])
        if not loadout:
            continue

        # Weapon slots
        for slot in ship_gun_slots.get(ship_name, []):
            lr = slot.get("local_ref", "")
            max_sz = slot.get("max_size", 99)
            if lr:
                resolved = dm.find_weapon(lr)
                if resolved and resolved["size"] > max_sz:
                    # Exception: stock weapons from child localReference can exceed
                    # parent gimbal max_size — this is expected (gimbal reduces size)
                    # We flag but note it's a gimbal child exception
                    size_details.append((
                        ship_name, slot.get("label", ""), "weapon",
                        resolved["name"], resolved["size"], max_sz
                    ))
                    size_violations += 1

        # Missile slots
        for slot in ship_missile_slots.get(ship_name, []):
            lr = slot.get("local_ref", "")
            max_sz = slot.get("max_size", 99)
            if lr:
                resolved = dm.find_missile(lr)
                if resolved and resolved["size"] > max_sz:
                    size_details.append((
                        ship_name, slot.get("label", ""), "missile",
                        resolved["name"], resolved["size"], max_sz
                    ))
                    size_violations += 1

        # Component slots
        for comp_name, (find_fn, type_set) in comp_types.items():
            slots = extract_slots_by_type(loadout, type_set)
            for slot in slots:
                lr = slot.get("local_ref", "")
                max_sz = slot.get("max_size", 99)
                if lr:
                    resolved = find_fn(lr)
                    if resolved and resolved["size"] > max_sz:
                        size_details.append((
                            ship_name, slot.get("label", ""), comp_name,
                            resolved["name"], resolved["size"], max_sz
                        ))
                        size_violations += 1

    out(f"Size violations found: {size_violations}")
    if size_details:
        out(f"{'Ship':30s} | {'Slot':30s} | {'Type':12s} | {'Component':25s} | Sz | Max")
        out("-" * 140)
        for ship, label, ctype, cname, csz, msz in size_details[:60]:
            out(f"{ship:30s} | {label:30s} | {ctype:12s} | {cname:25s} | {csz:2d} | {msz:2d}")
        if len(size_details) > 60:
            out(f"... and {len(size_details) - 60} more")
    out()

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 5: DPS TOTALS SPOT CHECK
    # ═══════════════════════════════════════════════════════════════════════════
    out("--- PHASE 5: DPS TOTALS SPOT CHECK ---")
    out(f"{'Ship':30s} | {'Raw DPS':>10s} | {'Sus DPS':>10s} | {'Msl Dmg':>10s} | {'Shield HP':>10s} | Guns | Msls | Shds")
    out("-" * 125)

    for target_name in SPOT_CHECK_SHIPS:
        ship_data = dm.ships_by_name.get(target_name) or dm.ships_by_name.get(target_name.lower())
        if not ship_data:
            out(f"{target_name:30s} | {'NOT FOUND':>10s} |")
            continue

        loadout = ship_data.get("loadout", [])
        if not loadout:
            out(f"{target_name:30s} | {'NO LOADOUT':>10s} |")
            continue

        # Weapons
        wt_slots = extract_slots_by_type(loadout, {"WeaponGun", "Turret"})
        sum_raw_dps = 0.0
        sum_sus_dps = 0.0
        gun_count = 0
        for slot in wt_slots:
            lr = slot.get("local_ref", "")
            if lr:
                w = dm.find_weapon(lr)
                if w:
                    sum_raw_dps += w.get("dps_raw", 0)
                    sum_sus_dps += w.get("dps_sus", 0)
                    gun_count += 1

        # Missiles
        gun_ids = {s["id"] for s in wt_slots}
        ms_all = extract_slots_by_type(loadout, {"MissileLauncher"})
        ms_slots = [s for s in ms_all if s["id"] not in gun_ids]
        sum_msl_dmg = 0.0
        msl_count = 0
        for slot in ms_slots:
            lr = slot.get("local_ref", "")
            if lr:
                m = dm.find_missile(lr)
                if m:
                    sum_msl_dmg += m.get("total_dmg", 0)
                    msl_count += 1

        # Shields
        sh_slots = extract_slots_by_type(loadout, {"Shield"})
        sum_shield_hp = 0.0
        sh_count = 0
        for slot in sh_slots:
            lr = slot.get("local_ref", "")
            if lr:
                s = dm.find_shield(lr)
                if s:
                    sum_shield_hp += s.get("hp", 0)
                    sh_count += 1

        out(f"{target_name:30s} | {sum_raw_dps:>10,.0f} | {sum_sus_dps:>10,.0f} | {sum_msl_dmg:>10,.0f} | {sum_shield_hp:>10,.0f} | {gun_count:>4d} | {msl_count:>4d} | {sh_count:>4d}")

    out()

    # ═══════════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════
    elapsed = time.time() - t0
    out("=== SUMMARY ===")
    out(f"Ships audited: {len(all_ships)}")
    out(f"Phase 1 - Gun slots: {total_guns}, Turret slots: {total_turrets}, Missile slots: {total_missiles}")
    out(f"  Gun resolution failures: {len(gun_failures)}")
    out(f"  Missile resolution failures: {len(missile_failures)}")
    out(f"Phase 2 - Component resolution:")
    for comp_name in comp_types:
        t = comp_totals[comp_name]["total"]
        r = comp_totals[comp_name]["resolved"]
        f_count = len(comp_totals[comp_name]["failures"])
        out(f"  {comp_name:15s}: {r}/{t} resolved, {f_count} failures")
    out(f"Phase 3 - Duplicate slots: {dup_count}, Gun/missile overlaps: {overlap_count}")
    out(f"Phase 4 - Size violations: {size_violations}")
    out(f"Phase 5 - Spot-checked {len(SPOT_CHECK_SHIPS)} ships")
    out(f"Audit completed in {elapsed:.2f}s")

    # Write report
    report_text = "\n".join(out_lines)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(report_text)
    print(f"\nReport saved to: {REPORT_FILE}")


if __name__ == "__main__":
    main()
