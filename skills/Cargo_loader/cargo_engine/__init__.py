"""
cargo_engine — Pure logic module for cargo container placement, collision
detection, and bin-packing. No UI code. Deterministic. Unit-testable.

All container definitions are loaded from container_schema.json
(single source of truth shared with the JS editor).
"""

from cargo_engine.schema import (
    CONTAINER_SIZES,
    CONTAINER_DIMS,
    CONTAINER_EDITOR_DIMS,
    CONTAINER_MAX_STACK_HEIGHT,
    CONTAINER_COLORS,
    SHADING,
    load_schema,
)
from cargo_engine.placement import best_rotation, max_containers_in_slot
from cargo_engine.collision import OccupancyGrid
from cargo_engine.packing import place_containers_3d, build_slots
from cargo_engine.optimizer import greedy_optimize_3d, assign_slots_from_counts
from cargo_engine.validation import validate_layout, validate_placement, ValidationError

__all__ = [
    "CONTAINER_SIZES",
    "CONTAINER_DIMS",
    "CONTAINER_EDITOR_DIMS",
    "CONTAINER_MAX_STACK_HEIGHT",
    "CONTAINER_COLORS",
    "SHADING",
    "load_schema",
    "best_rotation",
    "max_containers_in_slot",
    "OccupancyGrid",
    "place_containers_3d",
    "build_slots",
    "greedy_optimize_3d",
    "assign_slots_from_counts",
    "validate_layout",
    "validate_placement",
    "ValidationError",
]
