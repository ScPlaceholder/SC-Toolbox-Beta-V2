"""
Generate exact cargo_layout.json from sc-cargo.space cache data.
Uses grid definitions + reference loadouts for precise 3D positioning.

Now delegates to cargo_engine/ for all logic.

Usage: python generate_layout.py "890 Jump"
       python generate_layout.py "Caterpillar"
"""
import json
import logging
import os
import sys
import uuid

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from cargo_engine.schema import CONTAINER_SIZES, CONTAINER_DIMS
from cargo_engine.placement import packed_to_rotation, max_containers_in_slot
from cargo_engine.packing import place_containers_3d, build_slots
from cargo_engine.optimizer import greedy_optimize_3d, assign_slots_from_counts
from cargo_common import load_reference_loadouts, find_reference_loadout

log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(DIR, ".cargo_cache.json")

REFERENCE_LOADOUTS = load_reference_loadouts(DIR)


def find_ship(ships, name):
    key = name.strip().lower()
    by_name = {s["name"].lower(): s for s in ships}
    if key in by_name:
        return by_name[key]
    for k, v in by_name.items():
        if key in k or k in key:
            return v
    tokens = set(key.split())
    best, best_score = None, 0
    for k, v in by_name.items():
        score = len(tokens & set(k.split()))
        if score > best_score:
            best, best_score = v, score
    if best_score < 2:  # require at least 2 matching tokens to avoid false positives
        return None
    return best


def generate(ship_name):
    # Load cache
    if not os.path.exists(CACHE_FILE):
        return {"error": "Cache file not found. Open cargo_app.py first to download data."}

    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": f"Failed to load cache: {exc}"}

    ships = cache.get("ships", [])
    ship = find_ship(ships, ship_name)
    if not ship:
        names = sorted(s["name"] for s in ships)
        return {"error": f"Ship '{ship_name}' not found.", "available": names[:20]}

    slots, bounds = build_slots(ship)
    x_min, z_min, x_max, z_max = bounds

    # Get reference loadout or greedy optimize
    ref = find_reference_loadout(ship["name"], REFERENCE_LOADOUTS)
    counts = ref if ref else greedy_optimize_3d(slots)

    # Assign containers to slots
    assignment = assign_slots_from_counts(slots, counts)

    # Compute exact 3D positions
    placements = []
    for i, slot in enumerate(slots):
        asgn = assignment[i] if i < len(assignment) else {}
        if not asgn:
            continue
        x0 = slot["x"] - x_min
        y0 = slot.get("y0", 0)
        z0 = slot["z"] - z_min
        for (lx, ly, lz, cw, ch, cl, size) in place_containers_3d(slot, asgn):
            rot = packed_to_rotation(size, cw, ch, cl)
            placements.append({
                "id": str(uuid.uuid4()),
                "scu": size,
                "dims": {"w": cw, "h": ch, "l": cl},
                "pos": {"x": x0 + lx, "y": y0 + ly, "z": z0 + lz},
                "rotation": rot,
            })

    gridW = x_max - x_min
    gridZ = z_max - z_min
    gridH = max((s.get("y0", 0) + s["h"] for s in slots), default=4)

    container_counts = {}
    for s in CONTAINER_SIZES:
        c = counts.get(s, 0)
        if c > 0:
            container_counts[str(s)] = c

    total_scu = sum(counts.get(s, 0) * s for s in CONTAINER_SIZES)

    return {
        "schemaVersion": 1,
        "ship": ship["name"],
        "manufacturer": ship.get("manufacturer", ""),
        "gridW": gridW,
        "gridZ": gridZ,
        "gridH": gridH,
        "totalCapacity": ship.get("capacity", 0),
        "totalUsed": total_scu,
        "containers": container_counts,
        "source": "reference" if ref else "greedy",
        "slots": len(slots),
        "placements": placements,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python generate_layout.py <ship_name>")
        print("       python generate_layout.py --list")
        sys.exit(1)

    if sys.argv[1] == "--list":
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Error loading cache: {exc}")
            sys.exit(1)
        for s in sorted(cache["ships"], key=lambda x: x["name"]):
            print(f"  {s['name']:30s}  {s.get('capacity', 0):>6,} SCU")
        sys.exit(0)

    result = generate(" ".join(sys.argv[1:]))
    print(json.dumps(result, indent=2))
