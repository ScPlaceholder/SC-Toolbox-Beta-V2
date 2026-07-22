# Erkul LIVE reconciliation — per-ship verified findings (#29)
# Verified against live erkul.games 2026-07-11. "live" = actual gun hardpoints seen.
# Buckets: [A] local-undercount (missed nested/module mount) = LOCAL BUG
#          [B] capacity-vs-equipped (local counts hardpoints, ref counted equipped) = DEFINITION
#          [C] TBD

| ship | captured-ref | local | LIVE erkul | verdict |
|------|-----|-----|-----|---------|
| MDC | 1 | 0 | 1 (MRX Torrent via nested Rear module) | [A] local missed module-nested gun; ref right |
| F7C Hornet Mk II | 2 | 4 | 4 hardpoints (2x S4 Revenant + S3 empty + S5 empty) | [B] local=capacity right; ref counted equipped-only |
| Mustang Alpha | 2 | 4 | 4 (Nose Turret 2x Badger equipped + 2x S1 empty) | [B] local=capacity right; ref equipped-only |
| Wildfire Mk I | 3 | 2 | 4 hardpoints (2x Tarantula S3 + Revenant via Specialty VariPuck S5 + 1x S3 empty), 3 equipped | [A]+[B] local undercounts capacity (misses Specialty adapter + empty); ref=equipped(3) |

## SYNTHESIS (4 ships, live-verified)
The 20 divergences resolve into TWO compounding causes, now clear:

1. **The captured reference = DEFAULT-EQUIPPED gun count.** It matches "equipped" on every
   ship checked: MDC 1eq, Hornet Mk II 2eq, Mustang 2eq, Wildfire 3eq. Consistent.

2. **Local = hardpoint CAPACITY — but buggy on non-standard mounts.** It hits capacity (4)
   cleanly when mounts are plain VariPucks (Hornet, Mustang) but UNDERCOUNTS when it meets:
     - nested module mounts (MDC: module->gun, got 0 not 1)
     - specialty/adapter mounts + empty slots (Wildfire: got 2 not 4)

So they diverge because local (capacity) and ref (equipped) MEASURE DIFFERENT THINGS,
AND local's capacity walk breaks on module/adapter mount types.

## THE DECISION (J's call — product intent)
- If toolbox should mirror erkul's DEFAULT loadout -> local should count EQUIPPED, not capacity.
- If toolbox should report CAPACITY (what CAN mount, correct for a DPS planner) -> keep capacity,
  but fix the mount-walk to recurse nested modules + specialty adapters + count empty gun slots.
Recommendation: capacity is the right number for a DPS *planner*; fix the walk. But J decides.

## Batch 2 (individual eyeball of remaining 16, per J)
| Hornet Ghost Mk II | 2 | 4 | 4 hardpoints (2x S4 Revenant + S3 empty + S5 empty), 2 eq | [B] local=capacity; ref=equipped |
| Hornet Heartseeker Mk II | 6 | 4 | 4 hardpoints BUT 6 individual guns (Nose Turret Panther x2 + 2x S4 Revenant + Ball Turret TMSB x2) | ref=6=individual equipped guns; local=4 collapsed turrets to 1 each -> UNDERCOUNT |

## REVISED SYNTHESIS (6 ships) — the real through-line
captured-ref = **count of INDIVIDUAL EQUIPPED GUNS** (each barrel inside a turret counted). Consistent across ALL:
  MDC 1, Hornet Mk II 2, Mustang 2, Wildfire 3, Ghost Mk II 2, Heartseeker Mk II 6. Every one = equipped-gun tally.
local = **count of TOP-LEVEL HARDPOINTS**, but buggy: counts EMPTY hardpoints as +1 (over), COLLAPSES multi-gun
  turrets to 1 (under), MISSES nested-module + specialty-adapter mounts (under). So error DIRECTION flips per ship:
    - has empty hardpoints -> local > ref  (Hornet 4>2, Mustang 4>2, Ghost 4>2)
    - has multi-gun turrets -> local < ref  (Heartseeker 4<6)
    - nested module gun     -> local misses (MDC 0<1)
CORRECTION to my earlier lean: capacity is NOT the right number. For a DPS calc you want INDIVIDUAL EQUIPPED GUNS
  (what actually fires) = exactly what captured-ref measures. captured-ref is RIGHT; local counts the wrong thing.
FIX: make local count individual equipped guns — recurse into turrets (count each barrel), recurse nested
  modules + specialty adapters, and DON'T count empty hardpoints. One fix, re-run confirms all 20.
| Hornet Heartseeker Mk I | 5 | 6 | 5 individual guns (Nose Turret Bulldog x2 + 2 Mantis + 1 M6A); 4 hardpoints | ref=5=equipped guns (RIGHT); local=6 OVERcounts (double-counts turret/adapter) |

## STATUS: 7/20 verified, mechanism AIRTIGHT
captured-ref = individual equipped guns on ALL 7 (MDC1, HornetMkII2, Mustang2, Wildfire3, Ghost2, HSMkII6, HSMkI5).
Every remaining divergence is explained by this + local's buggy counting. Remaining 13 will be confirmatory.

## Batch 3 (J chose A - full table)
| Ironclad | 10 | 13 | 10 guns (5x dual gun-turrets) + 3 tractor-beam turrets (SureGrip) | ref=10=guns (RIGHT, excludes tractor beams); local=13 MISCOUNTS 3 tractor beams as guns |
| Ironclad Assault | 22 | 24 | 22 guns (8 turrets: 4+2+2+4+4+2+2+2), no tractor beams | ref=22=guns (RIGHT); local=24 over by 2 (phantom/mount miscount) |
| Mustang Beta | 2 | 4 | 2 guns (Nose Turret Badger x2) + 2x S1 empty | ref=2=guns (RIGHT); local=4 counts 2 empty hardpoints |
| Nomad | 3 | 4 | 3 guns (3x Panther) + 1 SureGrip tractor beam | ref=3=guns (RIGHT); local=4 counts tractor beam as gun (same as Ironclad) |
| C1 Spirit | 4 | 5 | 4 guns (Nose Turret M5A x2 + 2x M5A) + 1 SureGrip tractor beam | ref=4=guns (RIGHT); local=5 counts tractor beam as gun |
| Constellation Taurus | 6 | 8 | 6 guns (4x Galdereen + Upper Turret Panther x2) + 1 lower tractor-beam turret | ref=6=guns (RIGHT); local=8 over by 2 (tractor beam + mount miscount) |
| Cyclone AA | 0 | 1 | 0 guns (turret = 2 EM missiles + TroMag distortion burst, no guns) | ref=0=guns (RIGHT); local=1 miscounts distortion/missile mount as gun |
| Cyclone MT | 1 | 2 | 1 gun (9-Series Longsword ballistic) + 2 EM missile mounts | ref=1=guns (RIGHT); local=2 miscounts a missile mount as gun |
| Cyclone TR | 1 | 2 | 1 gun (single YellowJacket turret, S1) | ref=1=guns (RIGHT); local=2 double-counts turret mount + gun |
| Nova | 3 | 1 | 3 guns (S5 Slayer cannon + Remote Gun Badger x2) + Ignite missile pods | ref=3=guns (RIGHT); local=1 MISSED the Remote Gun turret's 2 Badgers (undercount) |
| Storm | 1 | 0 | 1 gun (S3 Reign-3 in Remote Main turret) | ref=1=guns (RIGHT); local=0 MISSED the Remote Main turret gun (undercount) |
| Nox | 0 | 2 | 0 guns (2x S1 empty - racing hoverbike) | ref=0=guns (RIGHT); local=2 counts 2 empty hardpoints |
| Nox Kue | 0 | 2 | 0 guns (2x S1 empty - racing hoverbike) | ref=0=guns (RIGHT); local=2 counts 2 empty hardpoints |

## ====== COMPLETE: ALL 20/20 LIVE-VERIFIED ======
RESULT: captured-ref = INDIVIDUAL EQUIPPED GUNS on all 20 ships. Erkul is correct every time.
The SC Toolbox local extractor is wrong on all 20, via SIX distinct counting bugs:
  1. Counts EMPTY hardpoints as guns        -> Nox, Nox Kue, Hornet Mk II, Ghost Mk II, Mustang A, Mustang B
  2. Counts TRACTOR BEAMS as guns           -> Ironclad (+3), Nomad (+1), C1 Spirit (+1), Const Taurus (+1)
  3. COLLAPSES multi-gun turrets to 1       -> Heartseeker Mk II (4 vs 6)
  4. MISSES whole turret types (undercount) -> Nova (missed Remote Gun), Storm (missed Remote Main), MDC (missed module)
  5. DOUBLE-COUNTS turret mount + gun/adapter-> Heartseeker Mk I (6 vs 5), Cyclone TR (2 vs 1), Wildfire (partial)
  6. Counts MISSILE / distortion mounts as guns -> Cyclone MT (2 vs 1), Cyclone AA (1 vs 0)

THE FIX (slot_extractor.py): count a hardpoint toward "guns" IFF it holds an equipped item whose
  type is a GUN (ballistic/energy/laser cannon/repeater/gatling). Specifically:
   - RECURSE into every turret/mount/module, counting each equipped gun barrel (turret xN -> N guns)
   - EXCLUDE empty hardpoints (no equipped item -> 0)
   - EXCLUDE tractor beams (SureGrip), missiles (Dominator/Ignite/etc), distortion (TroMag), tractor/utility
   - Do NOT double-count the mount AND its gun; count the leaf weapon only
  Then re-run erkul_truth_parity.py -> expect 0 divergences.

## ====== CRITICAL CORRECTION (2026-07-11, after reading the code + truth JSON) ======
My "all 20 = local wrong, erkul right" was an OVER-REACH. It splits two ways:

TRUTH-FILE ARTIFACTS (local is CORRECT, do NOT change the calculator) — 6 ships:
  Nox, Nox Kue, Mustang Alpha, Mustang Beta, F7C-S Ghost Mk II, F7C Hornet Mk II.
  Cause: erkul_slot_truth.json stores EMPTY slots with category="" (the scrape loses the
  type of an unfilled hardpoint). So truth_counts can't tell an empty GUN slot from empty
  paint -> under-counts guns. Each of these 6 ships' divergence == its # of empty weapon-zone
  slots (verified). local reads real port types and correctly shows the configurable empties.
  FIX BELONGS IN truth_counts/scrape, NOT slot_extractor.

GENUINE LOCAL BUGS (0 empty weapon-zone slots; real miscount) — 14 ships:
  OVER (tractor/mount): Ironclad(+3), Ironclad Assault(+2), Const Taurus(+2), Nomad(+1),
    C1 Spirit(+1), Cyclone TR(+1), Cyclone MT(+1), Cyclone AA(+1), Heartseeker Mk I(+1)
  UNDER (missed guns): Nova(-2), Heartseeker Mk II(-2), Storm(-1), MDC(-1), Wildfire(-1)
  FIX BELONGS IN slot_extractor (tractor UUID detection, missing turret-gun recursion,
    turret grouping) — carefully, regression-tested vs the 197 passing ships.

LESSON: I compared live eyeballing to the truth-FILE numbers and pattern-matched all 20 to one
story. The arbiter itself is blind to empty slots. Measure the real thing; don't reach.

## ====== SESSION STATE (2026-07-11, end of work block) ======
KEPT: Phase 1 truth-arbiter fix in erkul_truth_parity.py (truth_counts now counts empty
  weapon-zone slots). Audit-only, validated live (Hornet Mk I = 4 slots). Divergences: 20 -> 19
  (fixed 6 empty-slot false-positives; exposed 5 false-passes: Hornet Mk I / Ghost Mk I /
  Tracker Mk I-II / Wildfire were passing only because audit AND calc were both wrong at same #).
REVERTED: my slot_extractor.py gun-position-exclusion fix (correct in isolation, fixed
  Ironclad 13->10 + both Cyclones) — backed out to hold zero-regression because it UNMASKED
  compensating errors on Starlancer TAC (14->12) and Ironclad Assault (24->25) that need
  per-ship live verification. Backup used: services/slot_extractor.py.bak_elah_20260711.
KEY INSIGHT (why this resisted for weeks): the extractor is full of COMPENSATING errors —
  an over-count (counting turret shells/shrouds as guns) cancels an under-count (missed empty
  mounts / turret types) so the TOTAL matches truth by luck. Any single fix unmasks its partner
  bug on some other ship. Correct approach = fix the whole cluster together, re-run all 218 after
  each change, live-verify every newly-shifted ship. Ready gun-position fix (enumerated safe
  exclusion set) is documented above; apply WITH the empty-mount + missed-turret fixes.

## ====== CONVERGENCE PROGRESS (2026-07-11, ship-by-ship per J greenlight) ======
20 -> 14 divergences, zero regressions. Applied+kept:
  EXTRACTOR (slot_extractor.py): _is_nongun_child filter on (1) _gun_position_count,
    (2) housing inner_guns, (3) is_inner_gun branch. Excludes turret shells/shrouds/
    screens/missile/tractor children from gun counts. Fixes Ironclad 13->10,
    Ironclad Assault ->22, Cyclones TR/MT/AA.
  ARBITER (erkul_truth_parity.py truth_counts): a gimbal-turret holding a MISSILE rack
    (or tractor) is not an empty gun slot. Fixes Starlancer TAC (truth 14->12=local).
REMAINING 14 = two clusters:
  (a) UNDER — empty gun mounts / missed turret guns: Hornet Mk I, Ghost Mk I, Tracker
      Mk I/II, Wildfire (2v4); Idris P (35v36); Nova (1v3), Storm (0v1), MDC (0v1);
      Heartseeker Mk II (4v6).
  (b) OVER — leftover tractor/mount: Const Taurus (8v6), Nomad (4v3), C1 Spirit (5v4);
      Heartseeker Mk I (6v5).

## TRACTOR CLUSTER DONE (2026-07-11): 14 -> 11
Added _ref_is_tractor() to slot_extractor.py: a bare-UUID installed item is a tractor if the
erkul resolver says is_nonweapon_utility, ELSE if it's a known SureGrip UUID (8c16ee3d = S2 on
Ironclad/C1/Taurus; 34f8a503 = Nomad). Wired into _holds_tractor_beam via localReference check.
Converged Nomad, C1 Spirit, Constellation Taurus. Zero regressions.
REMAINING 11 (all UNDER except Heartseeker Mk I) — the delicate cluster:
  empty gun mounts not counted: Hornet Mk I, Ghost Mk I, Tracker Mk I/II, Wildfire (2v4), Idris P (35v36)
  missed equipped turret guns: Nova (1v3), Storm (0v1), MDC (0v1), Heartseeker Mk II (4v6)
  double-count: Heartseeker Mk I (6v5)
  RISK: adding gun counts can create phantom guns on OTHER ships — must distinguish empty GUN
  mount from empty module/missile/tractor mount, and recurse special turret types without
  over-reaching. Do fresh + regression-test each.

## EMPTY-MOUNT CLUSTER: attempted + reverted (2026-07-11)
Tried counting empty gun-family turret mounts (subtype whitelist GunTurret/BallTurret/
CanardTurret/NoseMounted + non-gun port-name blacklist camera/scanner/module/cargo/missile/...).
Regression: OVER-reached -> phantom guns on Hornet Mk II (4->5), Ghost Mk II (4->5), Mustang
Gamma/Omega (2->3). Net 11->13, WORSE. Reverted (removed constants + is_empty_gun_mount + 2
emission branches). ROOT DIFFICULTY: erkul shows the SAME slot total (4) for Hornet Mk I AND Mk II
but maps it to DIFFERENT physical mounts (Mk I: nose+center empties counted; Mk II: reaches 4 via
center's 2 gun children, nose NOT counted). So no per-mount subtype rule can be correct for both -
it needs erkul-grouping-aware logic (understand how erkul buckets a ship's mounts into displayed
slots), which is a larger design. PARKED honestly rather than ship phantom guns. State: clean 11.

## MISSED-GUN (weapon-leaf) FIX: 11 -> 9, zero regressions (2026-07-11)
Added _is_weapon_leaf() to slot_extractor.py: a child port ending in '_weapon'
(hardpoint_primary_weapon, hardpoint_left/right_weapon on Nova/Storm turrets) with
an installed item, no itemTypes, not a controller/nongun-child = a real gun position.
Wired into _gun_position_count + housing inner_guns. General rule, NO per-ship exceptions.
Converged Storm (0->1) and Nova (1->3). REMAINING 9: Hornet Mk I family empty-mounts (5,
2v4), Heartseeker Mk II (4v6), MDC (module-nested gun 0v1), Idris P (empty spinal 35v36),
Heartseeker Mk I (double-count 6v5). Session total: 20 -> 9, all systematic + missed-gun
errors fixed via general rules, zero regressions.
