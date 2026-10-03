# datamine/ - Cargo grid rebuild from scunpacked-data

Rebuilds the Cargo Loader ship grids from datamined game files
(github.com/StarCitizenWiki/scunpacked-data) instead of the sc-cargo.space
scrape, per brief on branch assistant-headless-tools (baseline tag
cargo-grids-baseline).

## Files

- `rebuild_grids.js` - the converter/differ. Node, stdlib only.
- `ships.json` - scunpacked-data source (build 4.10.1-LIVE.12660092,
  commit e96132078ae6a1a5f62a183fb1523dc006dcfddb). 41 MB, gitignored,
  fetched/copied in, never committed.
- `cargo_grids_scunpacked.json` - the rebuilt loader-format cache
  (144 ships, axis map XZY). Produced by `convert`.
- `report.md` - the reviewed diff vs the 2026-06-18 baseline
  (cargo_grids_baseline_2026-06-18.json).
- `fixtures/` - synthetic ships.json + cache pairs used to regression-test
  the tool before real data was available.

## Real-data results (2026-09-25)

- Axis map: **XZY** (width=X, height=Z, length=Y, file axes). Evidence:
  63/87 matched ships exact-shape under XZY vs 16 for the runner-up.
  The pre-data YZX hypothesis (from the Caterpillar hand-check) was wrong;
  under XZY the Caterpillar's 6x4x4 / 5x4x1 / 1x2x4 modules match exactly.
- 64 MATCH_ALL, 22 SCU_ONLY (same volume, grids reshuffled), 1 DIFF
  (Vulture 13 -> 12; the files have no 1x1x1 grid), 18 carried over
  from the baseline (concepts/unreleased: Hull-D/E, Merchantman, Pioneer,
  Galaxy, Liberator, Arrastra, Aurora Mk I variants, Retaliator, ...),
  39 new file-only variants (mostly Wikelo/Teach's/BIS/paint editions).
- ships.json has no grid positions: convert lays a ship's grids out
  left-to-right along X with a 1-cell gap inside one group at (0,0).
  Positions are display-only; packing (cargo_engine/packing.py build_slots)
  treats each grid as its own slot, so layout does not affect packing.
- minSize/maxSize are not carried: file MinSize/MaxSize are per-axis
  objects and packing.py intentionally hardcodes them to None.
- Duplicate file entries (paints/tiers/BIS, same Name + same grids) are
  deduped. Same Name with different grids happens once (Aegis Hammerhead,
  40 vs 64 SCU); resolveDuplicates keeps the set matching the baseline
  (AEGS_Hammerhead_GS, 64) and drops the other, logging to stderr.
- Name matching: scunpacked Names carry the manufacturer prefix
  ("Crusader A2 Hercules Starlifter" vs loader "A2 Hercules"). Matching is
  exact on the prefix-stripped name first, then exact, then containment
  (longer key wins, min length 3); each loader ship is claimed once.

## Pipeline

1. `node rebuild_grids.js inspect <ships.json>`
   Prints the real field names of the first entries so casing/nesting of
   CargoGrids can be confirmed against the tolerant reader.
2. `node rebuild_grids.js derive <ships.json> <cargo_cache.json>`
   Scores all 6 axis permutations across every ship present in both
   sources (exact shape multiset, then per-grid partial credit, then SCU).
3. `node rebuild_grids.js diff <ships.json> <cargo_cache.json> [report.md]`
   Ship-by-ship report: MATCH_ALL / SCU_ONLY / DIFF / only-in-loader /
   only-in-files, with per-grid shapes and rounding flags for dims that
   are not clean multiples of 1.25 m. Review this before switching.
4. `node rebuild_grids.js convert <ships.json> <cargo_cache.json> [XYZmap] [out.json]`
   Emits loader-format cache JSON (groups/grids in cells, provenance per
   ship). Loader ships with no files counterpart are carried over
   unchanged, so the rebuild never loses ships. Nothing is written into
   the live .cargo_cache.json by this tool.

Note: node cannot resolve module paths under this tree (AppData symlink);
run the tool from a copy outside your user profile folder.

## Loader-side notes (from cargo_app.py / cargo_engine)

- Cache format: { ts, ships: [{ manufacturer, name, capacity,
  groups: [{ x, z, grids: [{ x, y, z, width, height, length }] }], labels }] }.
- build_slots (cargo_engine/packing.py) reads per-grid keys
  maxSize/minSize if present, but hardcodes them to None because the
  sc-cargo.space values were unreliable. The optimizer honors them.
- Refresh today only happens when the window opens (ShipDataLoader in
  cargo_app.py, _refresh deletes the cache and re-scrapes). Loading the
  converted file and refreshing without a window still need the switch
  wired up after the diff is reviewed.

## Credit line (for the tool UI once switched)

Ship data: StarCitizenWiki/scunpacked-data. Star Citizen content (c) Cloud Imperium Games.

## Unifying the hand-made layouts (2026-09-26)

J asked for one grid format for every ship. The split is **not** inside this
directory: everything here is already the unified cache format. The odd one out
is `../layouts/*.json`, 33 files in a different schema
(`{schemaVersion, ship, gridW, gridZ, gridH, containers, placements}`) read by
`cargo_app._layout_to_slots`. `layouts_to_grids.py` converts them; see its
docstring for the two modes and `../tests/test_layout_migration.py` for what is
proven.

### Coverage, measured

- 33 layout files. **`Custom`** is a scratch file (`totalCapacity: 0`), and
  **`Zeus CL`** is already dead: no ship in either data source carries that
  name (the game calls it `Zeus Mk II CL`), so `SHIP_LAYOUTS` holds it and
  `_load_ship` never looks it up. **31 live.**
- Of those 31, **30** have a `cargo_grids_scunpacked.json` counterpart from
  the game files; **Retaliator** matches only a carried-over sc-cargo.space
  entry. So coverage is 31/31 with no name work needed beyond Zeus CL.
- `cargo_grids_scunpacked.json` covers **144 ships: 126 datamined, 18 carried
  over** (concepts the game files lack).

### What the layouts carry that the datamine does not

1. **A solved packing.** A layout is not a grid definition, it is a *filled*
   hold: each `placements[]` entry is one container at one position, and
   `_layout_to_slots` turns each into a "slot". That is the whole reason the
   files exist - the greedy optimiser cannot fill an irregular hold. Measured
   over the 31 live ships: the datamine grids + `greedy_optimize_3d` reach
   **7936 of 8808 SCU**, short on 20 of 31 (M2 Hercules 522 -> 322, Starfarer
   291 -> 187, Constellation Taurus 174 -> 118). Deleting the layouts and
   leaning on the packer is therefore **not** lossless.
   - This is not unique to the layout ships: **24 of the 113 ships already on
     the datamine path show the same shortfall today** (Zeus Mk II CL
     128 -> 72, Liberator 400 -> 256, Valkyrie 90 -> 50). The layouts are a
     hand-fix for a general packer limitation, applied to 31 ships.
2. **Deck height.** `pos.y` > 0 on **29 of the 33** files (Starfarer and
   Starfarer Gemini reach y=4). The unified format has always had a per-grid
   `y` key - and **nothing read it**: `build_slots` hardcoded `"y0": 0`, so
   `.cargo_cache.json`'s Caterpillar grids at `y=2` were packed and drawn on
   the floor. Fixed 2026-09-26; `build_slots` now reads `grid["y"]` (plus an
   optional `group["y"]`) into `y0`.
3. **`gridW`/`gridZ`/`gridH`** - a bounding box with no equivalent. It is only
   used to paint the floor, and see the defect below.
4. Nothing else. `containers` is reproduced exactly by
   `../reference_loadouts.json` for **30 of the 31** (MOTH differs: ref
   `{8:20,16:4}`, layout `{8:24,16:2}`, both 224 SCU).

### What the datamine carries that the layouts do not

`minSize`/`maxSize` per grid (from the files' per-axis MinSize/MaxSize),
`manufacturer`, `labels`, and `provenance`. A layout has no manufacturer and
no per-grid size rules - `cargo_app._grids_world` explicitly nulls the ones
`_layout_to_slots` invents, because they only echo the box drawn there.

### Two defects the split causes, both re-verified on the live tree

1. **No floor on an upper deck.** `cargo_app._draw_ground`'s layout branch
   draws ONE polygon with `y` hardcoded to 0 in all four corners and in both
   gridline loops, while `_collect_boxes` honours `slot["y0"]`. On the 29
   files with `pos.y > 0` every raised box is drawn with nothing under it. The
   non-layout branch draws a floor per slot at that slot's own `y0` and is
   immune, which is why only these 31 ships show it.
2. **The painted deck is a superset of where a box may go.** The layout branch
   paints the full `gridW x gridZ` rectangle; `PlacementContext(union=True)`
   only accepts cells inside some placement volume. Coverage of painted deck by
   real volume: **Idris-M 19.5%, Idris-P 20.6%, Caterpillar 24.7%,
   Freelancer DUR/MIS 33.3%, 890 Jump 33.9%**. Four fifths of the Idris' drawn
   floor refuses a container. **Converting to either mode dissolves this**: the
   painted floor becomes the per-grid floors, which are exactly the grids the
   placement rules use, so drawn and legal become the same set by construction.

### The fork, which is J's to settle

`volumes` mode is lossless and `merged` mode is correct geometry, and today you
cannot have both:

Measured over the **31 live** layout ships (Custom and Zeus CL excluded):

| variant | grids | SCU filled | short on | manual drag-and-drop |
|---|---|---|---|---|
| today, two formats (== `volumes`) | 1023 | **8786 / 8786** | 0 ships | permissive: `union=True`, a box may straddle volumes |
| `volumes` | 1023 | **8786 / 8786** | 0 ships | each grid is one container's hole, so only an identical container fits back |
| `merged` + greedy | 141 | 7802 / 8786 | 23 ships | correct per-grid containment |
| `merged` + the layout's own mix | 141 | 8538 / 8786 | 8 ships (-248) | correct per-grid containment |
| datamine grids + greedy (delete the layouts) | 141 | 7936 / 8808 | 20 ships | correct per-grid containment |

(The datamine's 8808 differs from the layouts' 8786 on two ships only:
Hammerhead 64 vs 40 - the layout is a partial hand-solve of the game's 64 SCU
hold, see the capacity section below - and Freelancer 66 vs 68, where the layout over-fills past the game's own
capacity by 2 SCU. Where they disagree, trust the datamine.)

The `merged` decomposition independently recovers almost exactly the datamine's
grid structure - **141 grids against the datamine's 141** across the 31 ships,
and ship by ship: Caterpillar 14 vs 14, Carrack 9 vs 9, Reclaimer 12 vs 12,
Starfarer 5 vs 5, 890 Jump 7 vs 7 - which is good evidence that the
layouts and the game files describe the same holds, and that the layouts were
simply written as a filled loadout instead of as volumes.

**The way to get both** is `merged` grids for the geometry plus the layout's
placements as the ship's starting arrangement (the renderer already has
`_manual_boxes` for exactly that). That is one format with an optional field,
not two formats, and it is the recommendation. It is a `cargo_app.py` change,
so it was not made unilaterally.

---

## DONE (2026-09-26/27): one format, and the numbers it moved

The recommendation above was taken, with one correction to it — see
"`merged` was the wrong decomposition" below. `cargo_app.py` no longer reads
`layouts/*.json`; `SHIP_LAYOUTS`, `_load_ship_layouts` and `_layout_to_slots`
are gone, and so are the `has_layout` branches in `_collect_boxes` and
`_update_assignment` and the per-ship size-tag nulling in `_grids_world`.

**Pipeline.** `python layouts_to_grids.py` writes
`cargo_grids_layouts.json` (mode `columns`), which `cargo_app.merge_layout_grids`
overlays by name onto whichever ship source loaded — game files, cache, or
scrape, because a hand-solved hold is not a property of one source. Each entry
is an ordinary unified entry plus an optional `arrangement`
(`[{scu, pos:[x,y,z], dims:[w,h,l]}]`) that `CargoApp._apply_arrangement` loads
into the renderer's `_manual_boxes`. `layouts/` is kept: it is the source the
converter and `cargo_grid_editor.html` read and write.

### Measured through the app, not through a re-implementation

`tests/test_layout_unification.py` boots a live `CargoApp`, loads all 32 ships
and compares the renderer's own `_last_boxes`, in ship coordinates, against the
layout files. **All 32 match exactly.** Totals on the Reset path:

| | capacity | drawn | short |
|---|---|---|---|
| before | 8936 | 8874 | 62 SCU on 2 ships |
| after | 8912 | **8912** | **0** |

Only four ships draw anything different, and each is accounted for:

- **Freelancer 28 -> 66 SCU.** The worst live defect this replaced, and it was
  not the format: `_update_fill` computes `used` **once** and then clamps each
  spinbox against `min(physical, remaining // size)`, so a hold whose boxes
  totalled 68 in a 66 SCU ship came out 1 SCU 2->0, 2 SCU 9->8, 4 SCU 4->3,
  32 SCU 1->0. The ship had been drawing 28 of 66 SCU for as long as the layout
  existed. `_apply_arrangement` goes through `_sync_counts_from_boxes`, which
  RAISES each maximum to what is placed instead of clamping down.
- **Idris-M and Idris-P, 4 boxes each.** The documented fractional snap:
  x=29.5 -> 29 and x=31.5 -> 31. Nothing else moved on either ship.
- **Caterpillar, every box shifted z-6.** Not a box change — a bounds change.
  The old path forced `bounds = (0, 0, gridW, gridZ)`, the layout's declared
  bounding box; bounds now come from the grids, so six rows of empty painted
  deck in front of the hold are gone. Identical in ship coordinates.
- **Hammerhead: the hold stays 64 SCU and the 40 SCU hand arrangement is drawn
  in it.** (The first version of the merge shrank the hold to 40 so that drawn
  equalled capacity; corrected 2026-09-27.) See the capacity section. The
  "after" row above counts the converted layouts' own capacity (Hammerhead 40);
  in the app the Hammerhead hold is 64, so the app-side capacity is 24 SCU
  higher, and those 24 SCU are free cells by design, not a shortfall.

### `merged` was the wrong decomposition — `columns` is the third mode

`manual_place.PlacementContext._grid_under(cx, cz)` picks the grid a dropped
container belongs to from its footprint centre and **never looks at y**. That is
safe only while no two grids sit over the same (x, z) column, and measured over
`cargo_grids_scunpacked.json` that holds for **0 of 144 ships** — the invariant
is universal in the real corpus, so nothing has ever exercised the ambiguous
case.

`merged` breaks it on the **Caterpillar, Starfarer and Starfarer Gemini**, and
under `union=False` **38 of their containers are then rejected from the grid
they already occupy** with "sticks out of the top of the grid". `columns` groups
cells by column profile first, so a column belongs to exactly one pool and the
invariant is structural. Grid counts are identical to `merged` on the other 29
ships (143 total against `merged`'s 144 after the Freelancer fix), and it
refuses a floating deck rather than emit the ambiguous shape — one synthetic
fixture produces that, no real layout does.

### `union` is a property of the DATA, not a second format

`union=self._union_grids` stayed, renamed and re-derived: it is now
`has_union_grids(ship)`, i.e. `provenance.source == "hand-layout"`. The reason is
not legacy. A hand-made hold is one irregular volume cut into rectangles by a
converter, and those rectangle boundaries are an artifact of the decomposition,
not walls — the Caterpillar's 24 SCU container legitimately spans two of them.
The game files list real bays with real walls and real per-grid MinSize/MaxSize,
and there a container must fit inside one. Requiring per-grid containment on a
decomposed hold would be over-constraining, which the numbers say plainly: with
`columns` grids and `union=True`, **0 of 1049 containers are refused at their own
position; with per-grid containment, 8 are.**

**And the path this replaces refused 8 too.** `_layout_to_slots` kept the
placements' float positions, so the union of legal cells had fractional corners
while a dropped box snaps to an integer cell: all eight Idris x=29.5 / x=31.5
containers were rejected with "outside the cargo grids" by the very volume they
occupied. They were effectively immovable. That is now fixed, and pinned by
`test_the_format_it_replaces_rejected_eight`.

### The three capacity claims, and which number is right

Every layout has three independent claims: the cells its boxes occupy, its own
`totalCapacity`, and the game files. **30 of 32 agree exactly.** Each of the
three disagreements has a different odd one out, so each is settled by majority
*and* by geometry rather than by preference:

- **Freelancer — 66 is right** (cells 68, stated 66, game 66). Two of three said
  66, and the geometry agreed: removing the two 1 SCU boxes at (0,0,13) and
  (0,1,13) leaves 54 SCU in the main hold and 12 in the side racks, which is
  exactly the game files' `2x3x9 + 2x3x1 + 2x3x1`. Fixed in the layout file —
  those two boxes were 2 SCU the ship cannot carry.
  ⚠ **The same 2 SCU were in a THIRD file.** `reference_loadouts.json` also had
  the Freelancer at 68 (`{"32":1,"4":4,"2":9,"1":2}`), and `_optimize()` prefers
  a reference loadout over the greedy packer — so pressing Optimize would have
  asked 68 into 66 and been clamped all over again, by a path the arrangement
  never touches. Fixing the layout and stopping there would have fixed the
  default view and left the button broken. One over-fill, three files.
- **Mercury Star Runner — 114 is right** (cells 114, stated 14, game 114). A
  dropped digit. Inert while capacity came from the ship source, and a 100 SCU
  error the moment it did not. Fixed in the layout file.
- **Hammerhead — the game's 64 is kept; the layout supplies only its
  arrangement** (cells 40, stated 40, game 64). Corrected 2026-09-27. An earlier
  draft of this section said "UNRESOLVED, 40 used" and called the 64 one half of
  a same-name duplicate. That premise is false for what the app loads: the
  ship source carries exactly ONE Hammerhead entry, one 4 x 2 x 8 grid = 64
  cells with capacity 64 (provenance scunpacked 4.10.1), and it is internally
  consistent. (The 40-vs-64 choice in `rebuild_grids.js` `resolveDuplicates`,
  noted under "Real-data results" above, happens upstream when this file is
  built and is already settled on `AEGS_Hammerhead_GS`, 64.) The hand layout
  is simply an older, partial solve covering 40 of those 64 cells.
  So `cargo_app.merge_layout_grids` compares GEOMETRY, not just capacity: a
  layout may contribute a hand-solved decomposition and arrangement, but it may
  never remove cells the ship source knows about. Where the layout covers fewer
  cells than the source (the Hammerhead is the only such ship, 40 vs 64), the
  source's hold and capacity are kept and only the arrangement is taken, with a
  warning logged. The app therefore shows a 64 SCU hold with the 40 SCU
  hand arrangement drawn in it and 24 SCU of real, reachable cells free.
  Pinned by `test_a_partial_hand_solve_does_not_shrink_the_hold` (drawn 40,
  capacity 64); the layout-vs-game cell mismatch itself stays listed in
  `EXPECTED_GAME_MISMATCH = {"Hammerhead": (40, 64)}` so the list cannot grow
  quietly.

### Zeus CL — remapped to `Zeus Mk II CL`, and it was never dead

⛔ **The analysis above says "`Zeus CL` is already dead: `_load_ship` never looks
it up." That is wrong.** `ShipDataLoader.find()` synthesised a ship from any
unmatched layout name and `get_ship_names()` injected it, so the combo box
carried **both** "Zeus CL" (128 SCU, hand layout, reachable and working) and
"Zeus Mk II CL" (128 SCU, game grids, packer reaching only 72). Two entries, one
ship. The claim was checked against `by_name` and not against the fallback
twenty lines below it.

Remapping collapses the duplicate onto the game's name and hands that name the
hand-solved hold: **+56 SCU on a ship that has been in the list all along.**
Recorded as `provenance.renamedFrom`. The ship list goes from 145 names to 144:
`Zeus CL` removed, nothing added, nothing else lost. `Freelancer Dur` and
`Freelancer Mis` were the same duplication in miniature (the game files spell
them `DUR`/`MIS`) and collapse the same way.

### Still true, still not fixed by this

The packer shortfall is untouched and is a separate piece of work: **24 of the
113 ships on the datamine path still under-fill** (Liberator 400 -> 256,
Valkyrie 90 -> 50). The layouts were a hand-fix applied to 31 ships for a
general `greedy_optimize_3d` limitation. Unifying the format neither fixes nor
worsens that; it does mean a future fix has one code path to improve.

### Known non-bit-exactness

Idris-M and Idris-P each place four 2 SCU containers at **x=29.5 and x=31.5**.
The unified format is integer cells, so those eight are floored onto the cell
below - a half-cell shift on 16 of 2700 SCU. `provenance.snappedFromFractional`
records each one and `test_fractional_inventory` asserts these eight are the
only ones, so the tolerance cannot widen quietly; a mutation of `_snap` from
floor to round is caught. (The first draft of the converter called `int()` in
both the converter and its own checker, so the check compared truncated against
truncated and passed while the boxes moved.)

### Not done, on purpose

- `cargo_grids_from_layouts_{volumes,merged}.json` are the earlier review
  candidates. They are superseded by `cargo_grids_layouts.json`, are not read
  by anything, and are not committed.
- History is not rewritten.
