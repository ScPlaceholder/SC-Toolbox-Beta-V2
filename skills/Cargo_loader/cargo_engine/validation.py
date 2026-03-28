"""
Validation layer — schema checking and placement validation.

Validates layout JSON before loading, rejects invalid placements,
and enforces grid bounds.
"""

from cargo_engine.schema import CONTAINER_SIZES, CONTAINER_DIMS


class ValidationError(Exception):
    """Raised when layout or placement data is invalid."""
    pass


def validate_layout(data: dict) -> list[str]:
    """Validate a layout JSON dict. Returns list of error strings (empty = valid)."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["Layout must be a JSON object"]

    # Schema version
    sv = data.get("schemaVersion")
    if sv != 1:
        errors.append(f"Unsupported schemaVersion: {sv} (expected 1)")

    # Grid dimensions
    for key, label in [("gridW", "Grid width"), ("gridZ", "Grid depth"), ("gridH", "Grid height")]:
        val = data.get(key)
        if val is None:
            errors.append(f"Missing required field: {key}")
        elif not isinstance(val, int) or val < 1:
            errors.append(f"{label} ({key}) must be a positive integer, got {val}")
        elif val > 256:
            errors.append(f"{label} ({key}) exceeds maximum of 256, got {val}")

    # Ship name
    if not data.get("ship"):
        errors.append("Missing or empty ship name")

    # Placements
    placements = data.get("placements")
    if placements is None:
        errors.append("Missing placements array")
    elif not isinstance(placements, list):
        errors.append("placements must be an array")
    else:
        grid_w = data.get("gridW", 256)
        grid_z = data.get("gridZ", 256)
        grid_h = data.get("gridH", 64)
        for i, p in enumerate(placements):
            p_errors = validate_placement(p, grid_w, grid_z, grid_h)
            for err in p_errors:
                errors.append(f"placements[{i}]: {err}")

    return errors


def validate_placement(
    p: dict,
    grid_w: int = 256,
    grid_z: int = 256,
    grid_h: int = 64,
) -> list[str]:
    """Validate a single placement dict. Returns list of error strings."""
    errors: list[str] = []

    if not isinstance(p, dict):
        return ["Placement must be a JSON object"]

    # SCU
    scu = p.get("scu")
    if scu not in CONTAINER_SIZES:
        errors.append(f"Invalid scu: {scu} (must be one of {CONTAINER_SIZES})")
        return errors  # can't validate further without valid SCU

    # Dims
    dims = p.get("dims")
    if not isinstance(dims, dict):
        errors.append("Missing or invalid dims object")
    else:
        for axis in ("w", "h", "l"):
            v = dims.get(axis)
            if not isinstance(v, (int, float)) or v < 1:
                errors.append(f"dims.{axis} must be a positive number, got {v}")

        # Verify dims are a valid rotation of the base dims
        if all(isinstance(dims.get(a), (int, float)) and dims[a] >= 1 for a in ("w", "h", "l")):
            base = CONTAINER_DIMS[scu]
            actual = (dims["w"], dims["h"], dims["l"])
            base_sorted = tuple(sorted(base))
            actual_sorted = tuple(sorted(actual))
            if base_sorted != actual_sorted:
                errors.append(
                    f"dims {actual} are not a valid rotation of "
                    f"base dims {base} for {scu} SCU"
                )

    # Position
    pos = p.get("pos")
    if not isinstance(pos, dict):
        errors.append("Missing or invalid pos object")
    else:
        for axis in ("x", "y", "z"):
            v = pos.get(axis)
            if not isinstance(v, (int, float)) or v < 0:
                errors.append(f"pos.{axis} must be a non-negative number, got {v}")

        # Bounds check
        if isinstance(dims, dict) and isinstance(pos, dict):
            pw = dims.get("w", 0)
            ph = dims.get("h", 0)
            pl = dims.get("l", 0)
            px = pos.get("x", 0)
            py = pos.get("y", 0)
            pz = pos.get("z", 0)
            if isinstance(px, (int, float)) and isinstance(pw, (int, float)) and px + pw > grid_w:
                errors.append(f"Exceeds grid width: x={px} + w={pw} > gridW={grid_w}")
            if isinstance(py, (int, float)) and isinstance(ph, (int, float)) and py + ph > grid_h:
                errors.append(f"Exceeds grid height: y={py} + h={ph} > gridH={grid_h}")
            if isinstance(pz, (int, float)) and isinstance(pl, (int, float)) and pz + pl > grid_z:
                errors.append(f"Exceeds grid depth: z={pz} + l={pl} > gridZ={grid_z}")

    # Rotation
    rot = p.get("rotation", 0)
    if rot not in (0, 90, 180, 270):
        errors.append(f"Invalid rotation: {rot} (must be 0, 90, 180, or 270)")

    return errors
