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
run the tool from a copy outside the prjgn tree.

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
