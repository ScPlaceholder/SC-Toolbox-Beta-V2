"""
Schema loader — reads container_schema.json and provides typed constants.

This is the single source of truth for container dimensions, colors,
and constraints. Both Python and JS consumers read from this file.
"""

import json
import os

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHEMA_PATH = os.path.join(_DIR, "container_schema.json")
_SCHEMA_CACHE: dict | None = None


def load_schema() -> dict:
    """Load and cache the container schema from disk."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        _SCHEMA_CACHE = json.load(f)
    return _SCHEMA_CACHE


def _build_constants():
    """Derive all constants from the schema file."""
    schema = load_schema()
    ct = schema["containerTypes"]

    sizes = sorted(int(k) for k in ct)

    # CONTAINER_DIMS: {scu: (w, h, l)} — editor base dims (w=X, h=Y, l=Z)
    # The long axis defaults to Z (l). This matches EDITOR_BASE_DIMS in the old code.
    dims = {}
    for k, v in ct.items():
        d = v["dimensions"]
        dims[int(k)] = (d["w"], d["h"], d["l"])

    # CONTAINER_MAX_STACK_HEIGHT: {scu: max_h} — only for constrained containers
    max_ch = {}
    for k, v in ct.items():
        if v["maxStackHeight"] is not None:
            max_ch[int(k)] = v["maxStackHeight"]

    # Colors
    colors = {int(k): v for k, v in schema["colors"].items()}

    # Shading factors
    shading = schema["shading"]

    return sizes, dims, max_ch, colors, shading


CONTAINER_SIZES, CONTAINER_DIMS, CONTAINER_MAX_STACK_HEIGHT, CONTAINER_COLORS, SHADING = _build_constants()

# Alias for backward compatibility — editor dims are the same as CONTAINER_DIMS
# in the new schema (w, h, l format with long axis on Z).
CONTAINER_EDITOR_DIMS = CONTAINER_DIMS
