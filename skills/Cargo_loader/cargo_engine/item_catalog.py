"""
Item catalogue for the Cargo Loader's Items tab. Pure logic, no UI.

Source: the pinned scunpacked-data build that shared/scunpacked.py already
downloads and caches (``~/.sctoolbox/scunpacked/<build>/ship-items.json``).
Nothing here downloads; a missing file just means "no items yet".

Footprint rule (1 cell = 1.25 m = one SCU edge)
------------------------------------------------
Each item's ``stdItem.InventoryOccupancy`` carries a ``CargoGrid`` and a
``Dimensions`` box, both in METRES ({Width, Length, Height}). Proof: the MISC
Ore Pod's CargoGrid is 2.5 m a side and its Volume is exactly 8 SCU (2x2x2).

  1. CargoGrid, when it is a real box: not the junk 0.75 m cube some items
     carry (Argo/MOLE and Enhanced ore pods, the GEO pod), and big enough to
     hold the item's OWN size -- stdItem's top-level Width/Height/Length (any
     axis order, 2 cm slack). A grid that cannot contain the item is an
     inventory-UI box, not a physical one.
     ⚠ The reference is the item's own size, NOT ``InventoryOccupancy
       .Dimensions``, which agrees with it for only 3 of 480 entries and is not
       a description of the item. See footprint() for the measurement.
  2. else Dimensions.
  3. else a CargoGrid that is not junk.
  4. else Volume (SCU): the most compact box of at least that many cells,
     flat first. Flagged ``approx`` (Greycat ROC / ROC-DS ore pods).
Every axis rounds UP to whole cells, minimum 1. Width -> w (x),
Height -> h (y, up), Length -> l (z).

This is a SIZE, not a simulation. The engine never models per-item in-game
snapping; the player places items and owns the result (J, 2026-09-26).
"""

from __future__ import annotations

import json
import math
import os
import re

CELL_M = 1.25
JUNK_GRID_M = 0.75
_SLACK_M = 0.02

# (key, label) in display order; the last four are grouped as "Components".
CATEGORIES: list[tuple[str, str]] = [
    ("ore", "Ore Pods"),
    ("missile", "Missiles"),
    ("bomb", "Bombs"),
    ("weapon", "Ship Weapons"),
    ("cooler", "Coolers"),
    ("power", "Power Plants"),
    ("shield", "Shields"),
    ("quantum", "Quantum Drives"),
]
CATEGORY_LABELS = dict(CATEGORIES)
COMPONENT_CATEGORIES = ("cooler", "power", "shield", "quantum")

# Outline / identity colour per category. Kept clear of the SCU container
# colours and of the amber that marks a warning.
CATEGORY_COLORS: dict[str, str] = {
    "ore": "#b08d57",
    "missile": "#ff5c5c",
    "bomb": "#c77dff",
    "weapon": "#7fb2ff",
    "cooler": "#4dd9d0",
    "power": "#b5e550",
    "shield": "#8c9eff",
    "quantum": "#ff8fc7",
}

# stdItem.Type top level -> category. Exact match on purpose: "MissileLauncher"
# (racks) and "BombLauncher" start with the same letters and are not cargo.
_TYPE_CATEGORY = {
    "Missile": "missile",
    "Bomb": "bomb",
    "WeaponGun": "weapon",
    "Cooler": "cooler",
    "PowerPlant": "power",
    "Shield": "shield",
    "QuantumDrive": "quantum",
}

_ORE_POD_RE = re.compile(r"^Cargo_.*Mining_Pod_", re.I)
_GEO_POD_CLASS = "carryable_2h_fl_05x05x05_inventory_argogeo"

_LABEL_DROP = {"missile", "torpedo", "bomb", "ore", "pod", "resource",
               "generator", "the"}


def default_path() -> str | None:
    """Where the toolbox caches ship-items.json (via shared/scunpacked.py)."""
    try:
        from shared import scunpacked
    except Exception:                                   # noqa: BLE001
        return None
    return os.path.join(scunpacked.cache_dir(), "ship-items.json")


def cells(metres: float) -> int:
    """Metres -> whole cells, rounded UP, minimum 1."""
    return max(1, math.ceil(round(float(metres) / CELL_M, 6)))


def _box(d) -> tuple[float, float, float] | None:
    """(Width, Height, Length) in metres, or None if absent / not positive."""
    if not isinstance(d, dict):
        return None
    try:
        w, h, l = float(d["Width"]), float(d["Height"]), float(d["Length"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or l <= 0:
        return None
    return (w, h, l)


def _is_junk(box) -> bool:
    return box is not None and all(abs(v - JUNK_GRID_M) < 1e-3 for v in box)


def _contains(outer, inner) -> bool:
    return all(i <= o + _SLACK_M for o, i in zip(sorted(outer), sorted(inner)))


def volume_box(scu: float) -> tuple[int, int, int]:
    """Most compact (w, h, l) of at least ceil(scu) cells: shortest longest
    side, then least volume, then flattest."""
    n = max(1, math.ceil(round(float(scu), 6)))
    best = None
    for w in range(1, n + 1):
        for h in range(1, n + 1):
            for l in range(w, n + 1):
                v = w * h * l
                if v < n:
                    continue
                key = (max(w, h, l), v, h, l - w)
                if best is None or key < best[0]:
                    best = (key, (w, h, l))
    return best[1]


def footprint(io: dict, own: tuple[float, float, float] | None = None,
              ) -> tuple[tuple[int, int, int], str] | None:
    """((w, h, l) in cells, source) for an InventoryOccupancy dict, or None.

    source is "grid", "dims" or "volume" (the last is an approximation).

    *own* is the item's own size in metres, off stdItem's top-level
    Width/Height/Length. It is the reference the CargoGrid is checked against.

    ⛔ 2026-09-27. That check used to compare the grid to ``Dimensions``, on the
      reasoning that a grid too small to hold the item is an inventory-UI box
      rather than a physical one. The reasoning is right and the field was
      wrong. Measured over all 486 catalogue entries of
      4.10.1-LIVE.12660092:

          CargoGrid  agrees with the item's own Width/Height/Length : 477 / 477
          Dimensions agrees with it                                :   3 / 480

      ``Dimensions`` does not describe the item, so the containment test was
      comparing the grid to an unrelated box and threw it away 350 times. 316
      of 486 items were sized off a field that contradicts their own
      dimensions, and five landed past every bay in the game: the size-4 Serac
      cooler came out 15x6x12 cells -- 18 m of an 0.89 m part -- and three
      size-10 weapons 31x12x86, longer than the Idris that mounts them. Their
      own boxes and their CargoGrids agree to 2 cm.

      Only the reference moved. A junk 0.75^3 grid still loses to Dimensions
      (the Argo and Enhanced ore pods and the GEO pod carry 0.75^3 in BOTH the
      grid and their own size, and J verified the pod is 8 SCU = 2x2x2), and
      with no own box there is nothing to check against, so Dimensions stands
      in as it did before -- which is why the older fixtures still read the
      same. This restores J's stated rule, "CargoGrid, else Dimensions": the
      containment test was never part of it.
    """
    if not isinstance(io, dict):
        return None
    grid, dims = _box(io.get("CargoGrid")), _box(io.get("Dimensions"))
    ref = own if own is not None else dims
    junk = _is_junk(grid)
    if grid is not None and not junk and (ref is None or _contains(grid, ref)):
        m, src = grid, "grid"
    elif dims is not None:
        m, src = dims, "dims"
    elif grid is not None and not junk:
        m, src = grid, "grid"
    else:
        vol = io.get("Volume") or {}
        try:
            scu = float(vol.get("SCU") or 0)
        except (TypeError, ValueError):
            scu = 0.0
        if scu <= 0:
            return None
        return volume_box(scu), "volume"
    return (cells(m[0]), cells(m[1]), cells(m[2])), src


def category_of(entry: dict) -> str | None:
    cls = entry.get("className") or ""
    std = entry.get("stdItem") or {}
    low = cls.lower()
    if _ORE_POD_RE.match(cls):
        # Pods do not fold in game (J checked: Prospector and MOLE pods are
        # 2x2x2 full or empty), so *_Collapsed is skipped; *_Template is the
        # data's own template with a junk 50 SCU volume.
        if "_collapsed" in low or low.endswith("_template"):
            return None
        return "ore"
    if low == _GEO_POD_CLASS:
        return "ore"
    top = (std.get("Type") or entry.get("type") or "").split(".")[0]
    return _TYPE_CATEGORY.get(top)


def abbrev(name: str, n: int = 7, maker: tuple = ()) -> str:
    """A short on-box label of at most *n* characters.

    Generic words (Missile, Ore, Pod...) go first, then a leading maker word
    (MISC, Greycat...) when something else is left to tell items apart.
    """
    words = [w for w in name.replace("'", "").replace('"', "").split() if w]
    keep = [w for w in words if w.lower() not in _LABEL_DROP] or words
    makers = {m.lower() for m in maker if m}
    if len(keep) > 1 and keep[0].lower() in makers:
        keep = keep[1:]
    if not keep:
        return "?"
    out = keep[0][:n]
    for w in keep[1:]:
        room = n - len(out) - 1
        if room < 2:
            break
        out += " " + w[:room]
    return out


def _is_placeholder(name: str) -> bool:
    return not name or "PLACEHOLDER" in name


def build_catalog(entries: list) -> list[dict]:
    """ship-items.json entries -> sorted, de-duplicated item definitions.

    Each definition: key (className), name, category, size (S#), dims
    (w, h, l) in cells, source ("grid" / "dims" / "volume"), approx, scu,
    label.
    """
    try:
        from shared.scunpacked import is_player_weapon
    except Exception:                                   # noqa: BLE001
        is_player_weapon = None
    order = {k: i for i, (k, _l) in enumerate(CATEGORIES)}
    seen: set = set()
    out: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        cat = category_of(e)
        if cat is None:
            continue
        std = e.get("stdItem") or {}
        name = (std.get("Name") or e.get("name") or "").strip()
        if _is_placeholder(name):
            continue
        cls = e.get("className") or std.get("ClassName") or ""
        if (cat == "weapon" and is_player_weapon is not None
                and not is_player_weapon(cls, name,
                                         (std.get("Manufacturer") or {}).get("Code"))):
            continue
        io = std.get("InventoryOccupancy") or {}
        # stdItem's own top-level Width/Height/Length is the item's real size.
        fp = footprint(io, _box(std))
        if fp is None:
            continue
        dims, src = fp
        dedupe = (cat, name, dims)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        try:
            scu = float((io.get("Volume") or {}).get("SCU"))
        except (TypeError, ValueError):
            scu = None
        try:
            size = int(std.get("Size") if std.get("Size") is not None else e.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        mfr = std.get("Manufacturer") or {}
        maker = (mfr.get("Code") or "", (mfr.get("Name") or "").split(" ")[0])
        out.append({
            "key": cls, "name": name, "category": cat, "size": size,
            "dims": dims, "source": src, "approx": src == "volume",
            "scu": scu, "label": abbrev(name, maker=maker),
        })
    out.sort(key=lambda d: (order[d["category"]], d["size"], d["name"].lower()))
    return out


def load_catalog(path: str | None = None) -> list[dict] | None:
    """Read and build the catalogue; None if the file is not there (yet)."""
    path = path or default_path()
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return build_catalog(json.load(f))
