"""
Lightweight loader for DPS Calculator salvage ship loadouts.

The DPS Calculator saves loadouts as JSON with shape:
    {"version": 1, "ship": "<ship name>", "selections": {...}}

We only need the ship name and file path for display in the
Mining Roster — salvage loadouts don't contribute to mining
breakability calculations, so we don't resolve the ``selections``
dict to numeric stats.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SalvageSnapshot:
    """Parsed DPS Calculator loadout — ship name + source path only."""
    ship: str                               # e.g. "Vulture", "Reclaimer"
    source_path: str                        # absolute path of the file
    version: int = 1
    salvage_heads: list[str] = field(default_factory=list)


def load_salvage_file(path: str) -> SalvageSnapshot | None:
    """Read and parse a DPS Calculator JSON file.

    Returns ``None`` if the file is missing or not a valid DPS payload.
    Accepts any DPS-format file; the caller chooses whether it's a salvage
    ship, so we don't enforce a role check.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("salvage_loader: failed to read %s: %s", path, exc)
        return None

    if not isinstance(raw, dict):
        return None

    ship = raw.get("ship") or ""
    if not ship:
        return None

    selections = raw.get("selections") or {}
    heads_dict = selections.get("salvage_heads") or {}
    salvage_heads: list[str] = []
    if isinstance(heads_dict, dict):
        salvage_heads = [str(v) for v in heads_dict.values() if v]

    return SalvageSnapshot(
        ship=str(ship),
        source_path=os.path.abspath(path),
        version=int(raw.get("version", 1)) if isinstance(raw.get("version"), int) else 1,
        salvage_heads=salvage_heads,
    )


def describe_salvage_snapshot(snap: SalvageSnapshot) -> str:
    """Short human-readable summary for tooltips."""
    lines = [f"Ship: {snap.ship}"]
    if snap.salvage_heads:
        lines.append("Salvage heads:")
        for h in snap.salvage_heads:
            lines.append(f"  + {h}")
    return "\n".join(lines)
