# Erkul Parity Fix — Comprehensive Prompt

## Goal
Fix all DPS Calculator calculations so they perfectly match erkul.games for every ship, component, weapon, missile, power pip, shield, and hull value.

## How to Verify
Run `python erkul_parity_audit.py` from the `DPS_Calculator/` directory. It audits all 208 ships and reports every discrepancy. After each fix, re-run the audit to verify. Also open https://www.erkul.games/live/calculator in a browser tab and compare specific ships side-by-side.

## Critical Files
- `services/power_engine.py` — PowerAllocatorEngine (power pips, signatures)
- `services/dps_calculator.py` — Weapon DPS/alpha/sustained calculations
- `services/stat_computation.py` — Shield/cooler/radar/missile/PP/QD stat extraction
- `services/slot_extractor.py` — Weapon/missile slot extraction from loadout tree
- `services/loadout_aggregator.py` — Footer total aggregation
- `data/repository.py` — ComponentRepository lookup/indexing

## Issue 1: Power Plant Output Calculation is Wrong

**Symptom:** Our PP output doesn't match Erkul. Example: Caterpillar has 2× Ginzel PP with `generation.powerSegment = 23`, so we compute 46 total output. But Erkul shows OUTPUT = 30.

**Investigation needed:**
- Open Erkul for Caterpillar and read the exact OUTPUT value shown in the power allocator widget (top-right panel, below signatures)
- Compare with our `_total_pp_output` in `power_engine.py` line ~224
- Check if Erkul uses a different field than `generation.powerSegment` for PP output, or if it applies some scaling/cap
- Check if Erkul's PP output depends on the number of coolers, the ship's power budget, or some other factor
- Look at Erkul's JavaScript source (`erkul.games`) to reverse-engineer the PP output formula

**Where to fix:** `power_engine.py` in the `_count_pp()` inner function of `load_ship()`, around line 186-229.

## Issue 2: Default Power Pip Allocation is Wrong

**Symptom:** Our default pip allocation per category doesn't match Erkul. Example Caterpillar:
- Our pips: WPN=7/8, THR=7/8, SHD=4/5×2, CLR=4/5×2, RDR=5/6, LSP=2/3, UTL=1/2
- Erkul pips: WPN=4, THR=5, SHD=2, RDR=2, CLR=2, QDR=4, UTL=4

**Investigation needed:**
- Open Erkul for multiple ships (Caterpillar, Gladius, Aurora MR, Hammerhead, Constellation Andromeda) and record the exact default pip values for every category
- Compare with our computed `current_seg` and `max_segments` values
- The `max_segments` might be wrong — we use `consumption.powerSegment` from each component as the max, but Erkul might compute max pips differently
- The `default_seg` allocation formula (lines 446-454) scales proportionally when demand > supply, but Erkul may use a different distribution algorithm

**Where to fix:** `power_engine.py` `load_ship()` — the `max_segments` and `default_seg` assignment for each slot, plus the power budget distribution at lines 446-454.

## Issue 3: Consumption Percentage is Wrong

**Symptom:** Our consumption % doesn't match. Caterpillar: ours=67%, Erkul=40%.

**Root cause:** This derives from Issue 1 (wrong PP output) and Issue 2 (wrong pip allocation). Fix those and consumption should automatically correct.

**Where to fix:** `power_engine.py` `recalculate()` — `consumption_pct` at line 636.

## Issue 4: EM and IR Signatures Don't Match Erkul

**Symptom:** Caterpillar: our EM=28,206 vs Erkul=36,200; our IR=10,858 vs Erkul=8,800. CS matches (45.7K).

**Investigation needed:**
- After fixing Issues 1-2, re-check signatures — they depend on pip levels and PP usage ratio
- If still wrong, reverse-engineer Erkul's signature formulas from their JavaScript
- Key formula areas in `recalculate()`: PP EM calculation (lines 582-593), component EM (lines 596-616), IR from coolers (lines 620-631)
- Check if the `_find_range_modifier()` function correctly maps pip counts to power range modifiers
- Check if the cooling ratio calculation (lines 541-567) matches Erkul's formula

**Where to fix:** `power_engine.py` `recalculate()` method.

## Issue 5: Radar Pips = 0 on 10+ Ships

**Symptom:** Ships like 300i, Aurora MR, Hawk, Khartu-al, Mustang Alpha show 0 radar pips even though their radar has `consumption.powerSegment > 0` in the raw data.

**Root cause:** The radar's `localName` (e.g. `radr_grnp_s01_ecouter`) isn't found by `self._lookup(ln)` in `_walk_powered()`. The lookup searches the component catalog but fails because the radar wasn't indexed, or the localName doesn't match the catalog key format.

**Investigation needed:**
- Check if these radar localNames exist in the `/live/radars` cache data
- Check if `ComponentRepository._find()` can locate them
- Add debug logging to `_walk_powered()` to trace why radar slots aren't being created

**Where to fix:** `power_engine.py` `_walk_powered()` inner function, around lines 280-380. May also need fixes in `data/repository.py` `_find()` method for radar lookups.

## Issue 6: Unresolved Weapon References (~20 ships)

**Symptom:** Various non-weapon items are being extracted as weapon slots, then failing lookup:
- UUID turret mount refs (e.g. `8c16ee3d-...` on C1 Spirit, Constellation Taurus)
- EMP devices (`tmbl_emp_device_s1` on Cyclone AA)
- Tractor beams (`grin_tractorbeam_s1` on MPUV Tractor, `grin_tractorbeam_s2` on Reliant Sen)
- Camera mounts (`misc_reliant_mako_camera_mount`)
- Sensor mounts (`misc_reliant_sen_sensor_mount`)
- Rotodomes (`umnt_anvl_s5_rotodome` on F7C-R Hornet Tracker)
- Blanking plates (`umnt_anvl_s5_cap` on F7C-S Ghost)
- Scoops (`cnou_mustang_gamma_scoop_front` on Mustang Gamma/Omega)
- Non-weapon UUIDs that are gimbal mounts without actual weapons

**Root cause:** `slot_extractor.py` `_resolve_weapon_ref()` follows the loadout tree but doesn't filter out non-weapon items. When a port has `itemTypes: ['Turret', 'WeaponGun']` but the `localReference` UUID points to a gimbal mount (not a weapon), it returns the gimbal UUID.

**Where to fix:** `services/slot_extractor.py`:
1. In `_resolve_weapon_ref()`, add skip patterns for known non-weapon localNames: `tmbl_`, `grin_tractorbeam`, `umnt_`, `_scoop_`, `_camera_mount`, `_sensor_mount`
2. When a `localReference` UUID doesn't resolve to an actual weapon in the weapons catalog, treat it as an empty slot (return `""`)
3. Filter out `EMP`, `TractorBeam`, `SensorMount` types from weapon extraction

## Issue 7: Unresolved Missile References (~8 ships)

**Symptom:** Blade rack missiles, torpedo storage UUIDs, and missile cap placeholders fail lookup:
- `vncl_missilerack_blade` (Glaive, Scythe blade wing missiles)
- `b6205a2d-...` UUID (Perseus torpedo storage — 20 slots)
- `misc_reliant_missile_cap_left/right` (Reliant variants)
- `mrck_s04_espr_talon_cap_r` (Talon leg missile caps)

**Root cause:** These are either:
1. Missile rack localNames that aren't in the `/live/missiles` cache (they're rack housings, not individual missiles)
2. UUID refs that don't match any missile in the cache
3. Cap/blanking plate items that fill empty missile slots

**Where to fix:** `services/slot_extractor.py`:
1. Add skip patterns for known missile placeholders: `_cap`, `_blade`, `blanking`
2. For torpedo storage UUIDs, check if they resolve to a missile in the cache; if not, skip
3. For missile racks without child missiles, return `""` (empty slot)

## Issue 8: Shield Regen Display (Cosmetic)

**Symptom:** Erkul's displayed shield regen accounts for power pip level. E.g., Golem OX Bulwark shield has `maxShieldRegen=257`, but Erkul shows `85.7` = 257 × (1/3) because shield is at 1/3 pips.

**Expected behavior:** When power sim is active, displayed regen should be `maxShieldRegen × shield_power_ratio`. When power sim is OFF, show raw `maxShieldRegen`.

**Where to fix:** `services/loadout_aggregator.py` `compute_footer_totals()` — the shield regen scaling at line 114 already does `tot_regen *= shield_power_ratio` when `power_sim=True`. Verify this is being applied correctly in the UI (`dps_ui/app.py`).

## Issue 9: Armor Damage Display Format

**Symptom:** Erkul shows armor damage as "PEN. PHYSICAL DMG REDUCTION: 30%" meaning `(1 - damageMultiplier.damagePhysical) × 100`. Our UI may show the raw multiplier (0.7) instead of the reduction percentage (+30%).

**Where to check:** `dps_ui/app.py` or wherever hull/armor stats are displayed. Ensure the display converts `damageMultiplier` to percentage reduction: `reduction% = (1 - multiplier) × 100`.

## Verification Checklist

After implementing fixes, verify these specific ships match Erkul exactly:

1. **Caterpillar** — Power pips, PP output, consumption %, EM/IR/CS signatures
2. **Golem OX** — DPS (should be total weapon DPS, affected by weapon power ratio), shield regen (should reflect power ratio), missile dmg = 1600
3. **Gladius** — All weapons resolved (3 guns), 4 missiles, 2 shields, all pips correct
4. **Aurora MR** — Radar pips should be non-zero (4 pips)
5. **Hammerhead** — Large ship with many weapon/shield/cooler slots
6. **Constellation Andromeda** — Multi-turret ship, verify turret weapon resolution
7. **F7C Hornet Mk I** — Class 4 center hardpoint UUID resolution
8. **Perseus** — Torpedo storage missile resolution
9. **Mustang Gamma** — Scoop in nose slot should not count as weapon
10. **MPUV Tractor** — Tractor beam should not count as weapon

For each ship, compare against Erkul (https://www.erkul.games/live/calculator):
- [ ] Power pip counts per category (WPN, THR, SHD, RDR, LSP, CLR, QDR, UTL)
- [ ] PP output total
- [ ] Consumption %
- [ ] EM, IR, CS signatures
- [ ] Total weapon DPS (raw and sustained)
- [ ] Alpha damage
- [ ] Total missile damage
- [ ] Shield HP and regen
- [ ] Shield resistances (phys, energy, distortion)
- [ ] Hull HP
- [ ] Armor type and damage reductions

## Priority Order

1. **Fix PP output** (Issue 1) — root cause of most pip/signature problems
2. **Fix default pip allocation** (Issue 2) — needed for correct DPS/regen display
3. **Fix radar pips** (Issue 5) — catalog lookup issue
4. **Fix unresolved weapons** (Issue 6) — filter non-weapons from extraction
5. **Fix unresolved missiles** (Issue 7) — filter placeholders
6. **Verify signatures** (Issue 4) — should auto-fix after Issues 1-2
7. **Verify consumption** (Issue 3) — should auto-fix after Issues 1-2
8. **Verify display formats** (Issues 8-9) — cosmetic

## Audit Script

Run `python erkul_parity_audit.py` after each fix. The script:
- Phase 1: Tests all 136 weapons, 64 shields, 61 missiles individually
- Phase 2: Audits all 208 ships for weapon/missile/shield resolution and power pip mismatches
- Phase 3: Shows power pip table for all ships
- Phase 4: Shows DPS/shield/hull summary table for all ships

Target: **0 issues** in Phase 2.
