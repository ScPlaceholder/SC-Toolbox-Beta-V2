"""
Shared constants and algorithms for Cargo Loader tools.

This module now delegates to cargo_engine/ for all logic, providing
backward-compatible re-exports so existing consumers (cargo_app.py,
generate_layout.py) continue to work without changes.

The single source of truth for container dimensions lives in
container_schema.json, loaded by cargo_engine.schema.
"""
import json
import logging
import os

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
DIR = os.path.dirname(os.path.abspath(__file__))

# ── Re-export everything from cargo_engine ────────────────────────────────────
from cargo_engine.schema import (
    CONTAINER_SIZES,
    CONTAINER_DIMS as EDITOR_BASE_DIMS,   # (w, h, l) format — editor coords
    CONTAINER_MAX_STACK_HEIGHT as CONTAINER_MAX_CH,
    CONTAINER_COLORS,
)

# Legacy CONTAINER_DIMS in (length, width, height) format for backward compat.
# The old code used L×W×H ordering; new schema uses W×H×L (editor coords).
# Since the schema now stores (w, h, l), we re-map to (l, w, h) for the old name.
CONTAINER_DIMS = {
    scu: (dims[2], dims[0], dims[1])  # (l, w, h) = old (length, width, height)
    for scu, dims in EDITOR_BASE_DIMS.items()
}

from cargo_engine.placement import best_rotation, max_containers_in_slot
from cargo_engine.packing import place_containers_3d, build_slots
from cargo_engine.optimizer import greedy_optimize_3d, assign_slots_from_counts

# ── Reference loadouts ───────────────────────────────────────────────────────
_LOADOUTS_FILE = os.path.join(DIR, "reference_loadouts.json")


def load_reference_loadouts(script_dir: str | None = None) -> dict[str, dict[int, int]]:
    """Load reference loadouts from JSON. *script_dir* overrides the default directory."""
    path = os.path.join(script_dir, "reference_loadouts.json") if script_dir else _LOADOUTS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: {int(sz): cnt for sz, cnt in v.items()} for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        log.warning("Failed to load reference loadouts from %s: %s", path, e)
        return {}


def find_reference_loadout(
    ship_name: str, loadouts: dict[str, dict[int, int]]
) -> dict[int, int] | None:
    """Return the reference loadout for *ship_name*, or None if not found.

    Matching strategy (case-insensitive):
    1. Exact match after lowercasing.
    2. Longest key that is a substring of the ship name.
    3. Longest ship-name fragment that is a substring of any key.
    """
    needle = ship_name.lower().strip()
    # 1. Exact
    if needle in loadouts:
        return loadouts[needle]
    # 2. Longest key contained in needle
    best_key: str | None = None
    best_len = 0
    for key in loadouts:
        if key in needle and len(key) > best_len:
            best_key = key
            best_len = len(key)
    if best_key:
        return loadouts[best_key]
    # 3. Longest needle fragment contained in any key
    best_key = None
    best_len = 0
    for key in loadouts:
        if needle in key and len(key) > best_len:
            best_key = key
            best_len = len(key)
    if best_key:
        return loadouts[best_key]
    return None
