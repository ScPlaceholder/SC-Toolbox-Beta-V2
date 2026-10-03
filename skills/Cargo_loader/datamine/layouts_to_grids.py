"""Convert the hand-made layouts/*.json into the unified groups/grids format.

WHY THIS EXISTS
---------------
The Cargo Loader USED TO carry two grid formats. It no longer does, and this
module is why — so read the past tense below as history, not as a description of
the app. `cargo_app._layout_to_slots`, `SHIP_LAYOUTS` and `_load_ship_layouts`
were all deleted on 2026-09-26; `cargo_app` reads `cargo_grids_layouts.json`
(this module's output) through `merge_layout_grids` and nothing reads
`layouts/*.json` at runtime.

  A. the unified cache format - {manufacturer, name, capacity,
     groups:[{x,z,grids:[{x,y,z,width,height,length,minSize,maxSize}]}]} -
     read by cargo_engine.packing.build_slots. THE only format now.
  B. layouts/<Ship>_cargo_layout.json - {schemaVersion, ship, gridW, gridZ,
     gridH, totalCapacity, containers, placements:[{scu,dims,pos,rotation}]}.
     33 files, 32 of which name a ship. Now a SOURCE format only: this module
     and cargo_grid_editor.html read and write it, the app does not.

Format B was the odd one out and it carried three measured defects that format A
does not have:

  1. FLOOR. _layout_to_slots turned each placement's pos.y into the slot's y0,
     and 29 of the 33 files have placements above y=0 (Starfarer reaches y=4).
     cargo_app._draw_ground's format-B branch drew ONE floor polygon with y
     hardcoded to 0 in all four corners and both gridline loops, so every
     upper-deck box on those ships was drawn with no floor under it. The
     format-A branch draws a floor per slot at that slot's own y0 and is
     immune.
  2. PAINTED DECK WAS A SUPERSET. The format-B branch painted the full
     gridW x gridZ rectangle while PlacementContext(union=True) only accepts
     cells inside some placement volume. Measured coverage of painted deck by
     real volume: Idris-M 19.5%, Caterpillar 24.7%, Freelancer DUR 33.3%,
     890 Jump 33.9%. Four fifths of the Idris' drawn floor refused a box.
  3. EIGHT CONTAINERS COULD NOT BE DROPPED WHERE THEY ALREADY SAT. The slots
     kept the placements' FLOAT positions, so the union of legal cells had
     fractional corners while a dropped box snaps to an integer cell: the
     Idris-M and Idris-P each put four 2 SCU containers at x=29.5 and x=31.5
     and all eight were refused with "outside the cargo grids" by the very
     volume they occupied. They were effectively immovable.

Converting B into A kills all three: the per-slot floor branch draws each grid
at its own y, the painted floor becomes exactly the union of the grids, and the
snapped integer cells are the ones the placement rules test against.

MODES
-----
`volumes`  one grid per placement volume, minSize=maxSize=scu.
           Reproduces the layout EXACTLY - greedy_optimize_3d over these
           grids returns the layout's own container histogram and each
           container lands at its placement's position. Lossless, but every
           grid is one container wide, so manual drag-and-drop can only put
           an identically-shaped container back in each hole.

`merged`   the union of the placement cells decomposed per deck into maximal
           rectangles, then stacked where a footprint repeats. Same cells,
           far fewer grids - but see `columns`: it can put two grids over one
           (x, z) column, a shape no ship in the unified corpus has.

`columns`  (default) the union of the placement cells grouped by COLUMN
           PROFILE: every (x, z) column's occupied y-range is computed first,
           columns with the same (y0, height) are pooled, and each pool's
           footprint is decomposed into maximal rectangles. The point is the
           invariant it guarantees - **at most one grid over any (x, z)
           column** - because `manual_place.PlacementContext._grid_under`
           picks a grid by footprint alone and is blind to y. Measured
           2026-09-26: 0 of the 144 ships in cargo_grids_scunpacked.json
           stack grids over a column, so this is the shape the engine has
           always been fed. `merged` breaks it on 3 ships (Caterpillar,
           Starfarer, Starfarer Gemini) and 38 of their containers then come
           back "sticks out of the top of the grid" from the very grid they
           live in. Same grid COUNT as `merged` on the other 29 ships.

No mode invents a cell: all three reproduce the union of the layout's
placement volumes exactly, which is asserted per ship before writing.

THE ARRANGEMENT
---------------
A layout is not a grid definition, it is a FILLED hold: each placement is one
container at one position, hand-placed because greedy_optimize_3d cannot fill
an irregular hold (measured: the packer reaches 7936 of 8808 SCU over these
ships, short on 20 of 31). So the geometry alone is not enough - converting
the grids and throwing the positions away loses the solved packing.

Every entry therefore carries an optional `arrangement`: the layout's own
placements as [{scu, pos:[x,y,z], dims:[w,h,l]}]. cargo_app loads it into the
renderer's `_manual_boxes`, which has existed for hand-arranged boxes since
drag-and-drop landed. That is ONE format with an optional field, not two
formats - which is the whole point of the exercise.

Measured 2026-09-26, every one of the 1049 placements across the 32 layouts:
each is a legal rotation of its container's schema dims, none exceeds its
maxStackHeight, and all 1049 pass `PlacementContext(union=True).snap()` at
their own position against the converted grids. The path they replace fails
8 of them ("outside the cargo grids": the Idris' four-each x=29.5/31.5
containers, whose float positions no integer cell can hold).

Usage
-----
  python layouts_to_grids.py [--mode columns|merged|volumes] [--out FILE]
  python layouts_to_grids.py --report
  python layouts_to_grids.py --selftest

Attribution: Star Citizen content (c) Cloud Imperium Games.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
LAYOUTS_DIR = os.path.join(SKILL_DIR, "layouts")
DEFAULT_OUT = os.path.join(HERE, "cargo_grids_layouts.json")

# The layout whose `ship` field is a scratch name, not a ship.
SCRATCH_SHIPS = {"custom"}

PROVENANCE_SOURCE = "hand-layout"

# Layout `ship` names the game does not use. The remap is recorded in
# provenance.renamedFrom so the original name is never just lost.
#
# "Zeus CL": the game calls it "Zeus Mk II CL", which is the name in BOTH
# cargo_grids_scunpacked.json and the live .cargo_cache.json, at the same
# 128 SCU the layout states. A previous review called this layout dead; it is
# not - cargo_app.ShipDataLoader.find() synthesises a ship from any unmatched
# layout name, so today the combo box carries BOTH "Zeus CL" (128 SCU, hand
# layout) and "Zeus Mk II CL" (128 SCU, game grids). Remapping collapses the
# duplicate onto the real name AND hands the real name the hand-solved hold:
# greedy_optimize_3d reaches only 72 of its 128 SCU, so this is +56 SCU on a
# ship that has been in the list all along.
RENAMES = {"zeus cl": "Zeus Mk II CL"}


def target_name(layout: dict) -> str:
    ship = (layout.get("ship") or "").strip()
    return RENAMES.get(ship.lower(), ship)


# ── fractional coordinates ───────────────────────────────────────────────────
#
# The layout schema (cargo_engine.validation.validate_placement) accepts floats
# for pos and dims, and eight placements really use them: Idris-M and Idris-P
# each put four 2 SCU containers at x=29.5 and x=31.5. The unified format is
# integer cells and cannot express a half-cell offset, so those eight are
# floored onto the cell below and the conversion is NOT bit-exact for them.
# That is counted, reported, and asserted against a fixed expectation rather
# than quietly absorbed by an int() call - the first draft of this file called
# int() in both the converter and its own checker, so the check compared
# truncated against truncated and passed while the boxes had moved.

def fractional_placements(layout: dict) -> list[tuple[int, str, float]]:
    """(index, "pos.x"-style field, value) for every non-integer coordinate."""
    out = []
    for i, p in enumerate(layout.get("placements", [])):
        for grp, keys in (("pos", "xyz"), ("dims", "whl")):
            for k in keys:
                v = p[grp][k]
                if float(v) != int(v):
                    out.append((i, "%s.%s" % (grp, k), float(v)))
    return out


def _snap(v) -> int:
    """Floor a coordinate onto its cell. Floor, not round: 29.5 and 31.5 are
    0.5 apart from cells 29 and 31, and flooring keeps the two boxes disjoint
    where rounding both up would collide the first into the second."""
    return int(v // 1)


# ── cell sets ────────────────────────────────────────────────────────────────

def layout_cells(layout: dict) -> set[tuple[int, int, int]]:
    """Every (x, y, z) cell covered by the layout's placement volumes."""
    cells: set[tuple[int, int, int]] = set()
    for p in layout.get("placements", []):
        d, pos = p["dims"], p["pos"]
        for dx in range(_snap(d["w"])):
            for dy in range(_snap(d["h"])):
                for dz in range(_snap(d["l"])):
                    cells.add((_snap(pos["x"]) + dx,
                               _snap(pos["y"]) + dy,
                               _snap(pos["z"]) + dz))
    return cells


def grid_cells(grids: list[dict]) -> set[tuple[int, int, int]]:
    """Every (x, y, z) cell covered by a list of unified-format grids."""
    cells: set[tuple[int, int, int]] = set()
    for g in grids:
        for dx in range(g["width"]):
            for dy in range(g["height"]):
                for dz in range(g["length"]):
                    cells.add((g["x"] + dx, g["y"] + dy, g["z"] + dz))
    return cells


# ── mode: volumes ────────────────────────────────────────────────────────────

def grids_from_volumes(layout: dict) -> list[dict]:
    """One grid per placement volume, pinned to that container's size.

    minSize == maxSize == scu makes greedy_optimize_3d pick exactly that
    container for exactly that hole, so the default view is byte-for-byte the
    arrangement the layout drew.
    """
    out = []
    for p in layout.get("placements", []):
        d, pos = p["dims"], p["pos"]
        out.append({
            "x": _snap(pos["x"]), "y": _snap(pos["y"]), "z": _snap(pos["z"]),
            "width": _snap(d["w"]), "height": _snap(d["h"]),
            "length": _snap(d["l"]),
            # Pinning each hole to its own container is belt-and-braces: today
            # greedy_optimize_3d already picks that container unaided, because
            # a hole exactly one container's size admits nothing larger
            # (measured 2026-09-26: unpinning changes the mix on 0 of 32
            # ships). The pins are here so a future optimiser that prefers
            # many small boxes cannot quietly re-pack a hand-made hold.
            "minSize": int(p["scu"]), "maxSize": int(p["scu"]),
        })
    out.sort(key=lambda g: (g["y"], g["z"], g["x"]))
    return out


# ── mode: merged ─────────────────────────────────────────────────────────────

def _max_rects(plane: set[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    """Greedy decomposition of a 2-D cell set into (x, z, w, l) rectangles.

    Deterministic: scans in (z, x) order, grows X as far as the run allows,
    then grows Z while every row of that width is still free.
    """
    todo = set(plane)
    rects: list[tuple[int, int, int, int]] = []
    while todo:
        x0, z0 = min(todo, key=lambda c: (c[1], c[0]))
        w = 0
        while (x0 + w, z0) in todo:
            w += 1
        l = 0
        while all((x0 + dx, z0 + l) in todo for dx in range(w)):
            l += 1
        for dz in range(l):
            for dx in range(w):
                todo.discard((x0 + dx, z0 + dz))
        rects.append((x0, z0, w, l))
    return rects


def _merge_up(boxes: list[dict]) -> list[dict]:
    """Stack boxes that share a footprint and touch in Y into one box."""
    by_foot: dict[tuple[int, int, int, int], list[dict]] = {}
    for b in boxes:
        by_foot.setdefault((b["x"], b["z"], b["width"], b["length"]), []).append(b)
    out = []
    for foot, group in by_foot.items():
        group.sort(key=lambda b: b["y"])
        cur = dict(group[0])
        for b in group[1:]:
            if b["y"] == cur["y"] + cur["height"]:
                cur["height"] += b["height"]
            else:
                out.append(cur)
                cur = dict(b)
        out.append(cur)
    out.sort(key=lambda g: (g["y"], g["z"], g["x"]))
    return out


def grids_from_merged(layout: dict) -> list[dict]:
    """The union of the placement volumes as maximal axis-aligned boxes."""
    cells = layout_cells(layout)
    planes: dict[int, set[tuple[int, int]]] = {}
    for (x, y, z) in cells:
        planes.setdefault(y, set()).add((x, z))
    boxes = []
    for y in sorted(planes):
        for (x0, z0, w, l) in _max_rects(planes[y]):
            boxes.append({"x": x0, "y": y, "z": z0,
                          "width": w, "height": 1, "length": l,
                          "minSize": None, "maxSize": None})
    return _merge_up(boxes)


# ── mode: columns ────────────────────────────────────────────────────────────
#
# WHY A THIRD MODE. manual_place.PlacementContext._grid_under(cx, cz) chooses
# the grid a dropped container belongs to from its footprint centre ALONE - it
# never looks at y. That is safe only while no two grids sit over the same
# (x, z) column, and measured 2026-09-26 that holds for 0 of 144 ships in
# cargo_grids_scunpacked.json: the invariant is universal in the real corpus,
# so nothing has ever exercised the ambiguous case.
#
# `merged` breaks it. Decomposing per deck and stacking repeated footprints
# puts a tall grid and a short grid over one column on the Caterpillar, the
# Starfarer and the Starfarer Gemini, and `_grid_under` then hands a container
# the wrong one: 38 of their containers are rejected from the grid they
# already occupy, with "sticks out of the top of the grid".
#
# Grouping by column profile makes the invariant structural instead of lucky:
# a column belongs to exactly one pool, so exactly one grid covers it.

def column_runs(layout: dict) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """(x, z) -> the maximal contiguous y-runs occupied in that column.

    A column with two runs is a deck floating over a gap. No real layout has
    one (measured: 0 of 32), and such a column is the ONE case that would
    force two grids over one footprint; `convert_layout` refuses it rather
    than silently emitting the ambiguous shape.
    """
    cols: dict[tuple[int, int], set[int]] = {}
    for (x, y, z) in layout_cells(layout):
        cols.setdefault((x, z), set()).add(y)
    out = {}
    for xz, ys in cols.items():
        runs: list[list[int]] = []
        for v in sorted(ys):
            if runs and v == runs[-1][1] + 1:
                runs[-1][1] = v
            else:
                runs.append([v, v])
        out[xz] = [(lo, hi) for lo, hi in runs]
    return out


def gapped_columns(layout: dict) -> list[tuple[int, int]]:
    """Columns whose occupied cells are not one contiguous run."""
    return sorted(xz for xz, runs in column_runs(layout).items() if len(runs) > 1)


def grids_from_columns(layout: dict) -> list[dict]:
    """Cells pooled by column profile, each pool decomposed into rectangles."""
    pools: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for xz, runs in column_runs(layout).items():
        for (lo, hi) in runs:
            pools.setdefault((lo, hi - lo + 1), set()).add(xz)
    out = []
    for (y0, h) in sorted(pools):
        for (x0, z0, w, l) in _max_rects(pools[(y0, h)]):
            out.append({"x": x0, "y": y0, "z": z0,
                        "width": w, "height": h, "length": l,
                        "minSize": None, "maxSize": None})
    out.sort(key=lambda g: (g["y"], g["z"], g["x"]))
    return out


MODES = {"volumes": grids_from_volumes, "merged": grids_from_merged,
         "columns": grids_from_columns}
DEFAULT_MODE = "columns"


def stacked_columns(grids: list[dict]) -> list[tuple[int, int]]:
    """(x, z) columns covered by more than one grid.

    Must be empty. `_grid_under` is y-blind, so a second grid over a column is
    a container the engine can reject from its own home.
    """
    seen: dict[tuple[int, int], int] = {}
    for g in grids:
        for dx in range(g["width"]):
            for dz in range(g["length"]):
                xz = (g["x"] + dx, g["z"] + dz)
                seen[xz] = seen.get(xz, 0) + 1
    return sorted(xz for xz, n in seen.items() if n > 1)


# ── the arrangement ──────────────────────────────────────────────────────────

def arrangement_boxes(layout: dict) -> list[dict]:
    """The layout's placements, snapped, in the renderer's box order.

    This is the hand-solved packing the grids cannot carry. cargo_app turns it
    into `_manual_boxes`; the tuple order there is (x, y, z, w, h, l, scu).
    """
    out = []
    for p in layout.get("placements", []):
        d, pos = p["dims"], p["pos"]
        out.append({
            "scu": int(p["scu"]),
            "pos": [_snap(pos["x"]), _snap(pos["y"]), _snap(pos["z"])],
            "dims": [_snap(d["w"]), _snap(d["h"]), _snap(d["l"])],
        })
    out.sort(key=lambda b: (b["pos"][1], b["pos"][2], b["pos"][0], b["scu"]))
    return out


# ── conversion ───────────────────────────────────────────────────────────────

class ConversionError(Exception):
    """A layout could not be converted without losing or inventing cells."""


def convert_layout(layout: dict, mode: str = DEFAULT_MODE) -> dict:
    """Convert one layout dict into a unified-format ship entry.

    Raises ConversionError if the produced grids do not cover exactly the same
    cells as the layout's placements, if they overlap, or - in `columns` mode -
    if two grids end up over one (x, z) column. Those checks are the whole
    guarantee: they are what makes "lossless" a measurement rather than a claim.
    """
    if mode not in MODES:
        raise ConversionError("unknown mode %r" % mode)
    grids = MODES[mode](layout)
    want = layout_cells(layout)
    got = grid_cells(grids)
    if got != want:
        raise ConversionError(
            "%s: cell mismatch in mode %s - %d missing, %d invented"
            % (layout.get("ship", "?"), mode,
               len(want - got), len(got - want)))
    vol = sum(g["width"] * g["height"] * g["length"] for g in grids)
    if vol != len(want):
        raise ConversionError(
            "%s: grids overlap - volume %d over %d distinct cells"
            % (layout.get("ship", "?"), vol, len(want)))
    if mode == "columns":
        gapped = gapped_columns(layout)
        if gapped:
            raise ConversionError(
                "%s: %d column(s) occupied with a gap, e.g. %r - a floating "
                "deck cannot be covered by one grid per column"
                % (layout.get("ship", "?"), len(gapped), gapped[0]))
        stacked = stacked_columns(grids)
        if stacked:
            raise ConversionError(
                "%s: %d (x,z) column(s) covered by more than one grid, e.g. "
                "%r - _grid_under is y-blind and would reject a container "
                "from its own grid" % (layout.get("ship", "?"),
                                       len(stacked), stacked[0]))
    frac = fractional_placements(layout)
    ship = (layout.get("ship") or "").strip()
    name = target_name(layout)
    prov = {
        "source": PROVENANCE_SOURCE,
        "mode": mode,
        "schemaVersion": layout.get("schemaVersion"),
        "statedCapacity": layout.get("totalCapacity"),
        # Non-empty means this ship is NOT bit-exact: these coordinates
        # were floored onto a cell because the unified format has no
        # sub-cell positions.
        "snappedFromFractional": [
            {"placement": i, "field": f, "was": v} for i, f, v in frac],
    }
    if name != ship:
        prov["renamedFrom"] = ship
    return {
        "name": name,
        "capacity": len(want),
        "groups": [{"x": 0, "z": 0, "grids": grids}],
        # The hand-solved packing. Optional by design: a ship without one
        # falls through to the optimiser exactly as the other 113 do.
        "arrangement": arrangement_boxes(layout),
        "provenance": prov,
    }


def validate_all(layouts: list[dict]) -> list[str]:
    """Schema errors across the layouts, as "<ship>: <error>" lines.

    cargo_app used to run this at import and log the first three per file. It
    no longer reads layouts/ at all, so the check moved here - to the one place
    that still does - rather than being dropped with the code that called it.
    Reporting only: a schema warning has never blocked a layout and does not
    block a conversion, which has its own cell-exact assertions.
    """
    try:
        sys.path.insert(0, SKILL_DIR)
        from cargo_engine.validation import validate_layout
    except ImportError:
        return []
    out = []
    for layout in layouts:
        for err in validate_layout(layout) or []:
            out.append("%s: %s" % (layout.get("ship", "?"), err))
    return out


def load_layouts(directory: str = LAYOUTS_DIR) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


def convert_all(mode: str = DEFAULT_MODE,
                directory: str = LAYOUTS_DIR) -> tuple[list[dict], list[str]]:
    ships, skipped = [], []
    for layout in load_layouts(directory):
        ship = (layout.get("ship") or "").strip()
        if not ship or ship.lower() in SCRATCH_SHIPS:
            skipped.append(ship or "<unnamed>")
            continue
        ships.append(convert_layout(layout, mode))
    ships.sort(key=lambda s: s["name"].lower())
    return ships, skipped


GAME_GRIDS = os.path.join(HERE, "cargo_grids_scunpacked.json")


def _game_capacities(path: str = GAME_GRIDS) -> dict[str, int]:
    """name.lower() -> capacity from the game-file grids, for the report.

    Read-only and tolerant: the conversion itself never depends on it. It is
    here so a capacity that disagrees with the game is VISIBLE rather than
    something a human has to happen to notice.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    out = {}
    for s in obj.get("ships") or []:
        name = (s.get("name") or "").strip().lower()
        if name:
            out[name] = s.get("capacity")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default=DEFAULT_MODE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--report", action="store_true",
                    help="print a per-ship table and write nothing")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv[1:])

    if args.selftest:
        return selftest()

    ships, skipped = convert_all(args.mode)
    for err in validate_all(load_layouts()):
        print("schema warning: %s" % err, file=sys.stderr)
    if args.report:
        game = _game_capacities()
        print("%-26s %6s %8s %8s %8s %7s %s"
              % ("ship", "grids", "SCU", "stated", "game", "boxes", "flags"))
        conflicts = []
        for s in ships:
            g = s["groups"][0]["grids"]
            stated = s["provenance"]["statedCapacity"]
            gcap = game.get(s["name"].lower())
            flags = []
            if stated != s["capacity"]:
                flags.append("stated!=grids")
            if gcap is not None and gcap != s["capacity"]:
                flags.append("GAME!=grids")
                conflicts.append((s["name"], s["capacity"], stated, gcap))
            elif gcap is None:
                flags.append("no-game-entry")
            if s["provenance"]["snappedFromFractional"]:
                flags.append("snapped=%d"
                             % len(s["provenance"]["snappedFromFractional"]))
            if "renamedFrom" in s["provenance"]:
                flags.append("renamedFrom=%s" % s["provenance"]["renamedFrom"])
            print("%-26s %6d %8d %8s %8s %7d %s"
                  % (s["name"], len(g), s["capacity"], stated,
                     "-" if gcap is None else gcap,
                     len(s.get("arrangement") or []), " ".join(flags)))
        print("\n%d converted, skipped: %s"
              % (len(ships), ", ".join(skipped) or "none"))
        print("grids %d, arrangement boxes %d, SCU %d"
              % (sum(len(s["groups"][0]["grids"]) for s in ships),
                 sum(len(s.get("arrangement") or []) for s in ships),
                 sum(s["capacity"] for s in ships)))
        if conflicts:
            print("\nCAPACITY CONFLICTS (grids vs the game files) - %d:"
                  % len(conflicts))
            for n, cells, stated, gcap in conflicts:
                print("  %-26s grids=%-5d stated=%-5s game=%-5s"
                      % (n, cells, stated, gcap))
        return 0

    payload = {"ts": 0, "mode": args.mode, "ships": ships}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print("wrote %s: %d ships, mode=%s (skipped %s)"
          % (args.out, len(ships), args.mode, ", ".join(skipped) or "none"))
    return 0


def selftest() -> int:
    """Exercise both modes against a shape neither can fudge."""
    fake = {
        "schemaVersion": 1, "ship": "Selftest", "gridW": 4, "gridZ": 4,
        "gridH": 2, "totalCapacity": 6,
        "placements": [
            # two 1x1x2 volumes side by side on the deck, one on top of the
            # left-hand pair -> merged must find an L, not a cuboid.
            {"scu": 2, "dims": {"w": 1, "h": 1, "l": 2},
             "pos": {"x": 0, "y": 0, "z": 0}, "rotation": 0},
            {"scu": 2, "dims": {"w": 1, "h": 1, "l": 2},
             "pos": {"x": 1, "y": 0, "z": 0}, "rotation": 0},
            {"scu": 2, "dims": {"w": 1, "h": 1, "l": 2},
             "pos": {"x": 0, "y": 1, "z": 0}, "rotation": 0},
        ],
    }
    fails = []
    for mode in sorted(MODES):
        try:
            got = convert_layout(fake, mode)
        except ConversionError as exc:
            fails.append("%s raised %s" % (mode, exc))
            continue
        if got["capacity"] != 6:
            fails.append("%s: capacity %d != 6" % (mode, got["capacity"]))
    # merged must not paint the empty cell above (1,1,0)
    merged = convert_layout(fake, "merged")
    if (1, 1, 0) in grid_cells(merged["groups"][0]["grids"]):
        fails.append("merged invented the cell above the right-hand volume")
    if len(merged["groups"][0]["grids"]) != 2:
        fails.append("merged produced %d grids, expected 2"
                     % len(merged["groups"][0]["grids"]))

    # THE SHAPE `columns` EXISTS FOR, and the one `merged` gets wrong: an L
    # where a tall column stands beside a short one over the same footprint
    # run. merged stacks a 1-high grid on top of a 1-high grid over cells
    # (0,*,0..1); columns must not.
    for mode in ("merged", "columns"):
        grids = convert_layout(fake, mode)["groups"][0]["grids"]
        st = stacked_columns(grids)
        if mode == "columns" and st:
            fails.append("columns put %d grid(s) over one column: %r" % (len(st), st))
        if mode == "merged" and not st:
            fails.append(
                "merged no longer stacks a column on the selftest fixture - the "
                "fixture stopped exercising the difference `columns` exists for")

    # the arrangement must carry every placement, at its own position
    cols = convert_layout(fake, "columns")
    arr = cols["arrangement"]
    if len(arr) != len(fake["placements"]):
        fails.append("arrangement has %d boxes for %d placements"
                     % (len(arr), len(fake["placements"])))
    if sum(b["scu"] for b in arr) != 6:
        fails.append("arrangement totals %d SCU, expected 6"
                     % sum(b["scu"] for b in arr))
    want_pos = {(0, 0, 0), (1, 0, 0), (0, 1, 0)}
    if {tuple(b["pos"]) for b in arr} != want_pos:
        fails.append("arrangement positions %r != %r"
                     % (sorted(tuple(b["pos"]) for b in arr), sorted(want_pos)))

    # columns must REFUSE a floating deck rather than emit the ambiguous shape
    floating = {
        "schemaVersion": 1, "ship": "SelftestGapped", "gridW": 2, "gridZ": 2,
        "gridH": 4, "totalCapacity": 2,
        "placements": [
            {"scu": 1, "dims": {"w": 1, "h": 1, "l": 1},
             "pos": {"x": 0, "y": 0, "z": 0}, "rotation": 0},
            {"scu": 1, "dims": {"w": 1, "h": 1, "l": 1},
             "pos": {"x": 0, "y": 2, "z": 0}, "rotation": 0},
        ],
    }
    if gapped_columns(floating) != [(0, 0)]:
        fails.append("gapped_columns missed the floating deck: %r"
                     % gapped_columns(floating))
    try:
        convert_layout(floating, "columns")
        fails.append("columns accepted a gapped column instead of refusing it")
    except ConversionError:
        pass
    # ...and the other modes must still handle it, so the refusal is scoped
    for mode in ("volumes", "merged"):
        try:
            convert_layout(floating, mode)
        except ConversionError as exc:
            fails.append("%s refused the gapped fixture: %s" % (mode, exc))

    if fails:
        for f in fails:
            print("FAIL %s" % f)
        return 1
    print("PASS layouts_to_grids selftest (%d modes)" % len(MODES))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
