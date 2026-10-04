"""Crafted mining lasers: what material quality does to laser power.

One place for the maths, imported by BOTH Mining Loadout (``calc_stats``) and
Mining Signals (``loadout_loader``), so the two tools cannot disagree about a
crafted laser.

What the game data says (``blueprints.json`` of the pinned scunpacked build,
the same file the Craft Database reads): a mining laser blueprint has ingredient
groups, and a group can carry ``Modifiers`` that scale with the quality of the
material put in it. In 4.10.1 every craftable mining laser has::

    FRAME     -> health_maxhealth  "Integrity"     0.8 at quality 0 .. 1.2 at 1000
    EMITTER   -> weapon_damage     "Laser Power"   0.8 .. 1.2
    BUS BARS  -> weapon_damage     "Laser Power"   0.8 .. 1.2

Nothing here hardcodes those names or numbers; they are read from the file, so a
patch that changes the range or adds a group is picked up.

What the game data does NOT say, and nobody has measured in game yet:

* how the two "Laser Power" groups combine. Multiplied gives 0.64 .. 1.44,
  averaged gives 0.8 .. 1.2. Both rules are offered; ``DEFAULT_COMBINE`` is the
  current GUESS. Change that one constant once a crafted laser has been measured.
* whether ``weapon_damage`` also scales the extraction beam. The callers apply
  the factor to mining laser power only and leave extraction power alone.

No Qt, no network. A missing or unreadable blueprint file yields an empty index,
which the callers treat as "crafting not offered".
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

COMBINE_MULTIPLY = "multiply"
COMBINE_AVERAGE = "average"
COMBINE_RULES = (COMBINE_MULTIPLY, COMBINE_AVERAGE)

# The game data does not specify how two Laser Power groups combine. This is the
# current guess; it is the ONE line to change after an in-game measurement.
DEFAULT_COMBINE = COMBINE_MULTIPLY

# Blueprint modifier keys.
POWER_KEY = "weapon_damage"
INTEGRITY_KEY = "health_maxhealth"

# Output.Type of a mining laser head in blueprints.json.
LASER_OUTPUT_TYPE = "WeaponMining"

BLUEPRINT_FILE = "blueprints.json"

# Keys of the optional per-turret "crafted" object in a loadout file, and of the
# optional loadout-wide combine rule next to "ship"/"gadget".
FIELD_CRAFTED = "crafted"
FIELD_QUALITIES = "qualities"
FIELD_POWER_FACTOR = "power_factor"
FIELD_COMBINE = "crafted_combine"

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {}        # path -> (mtime, size, index)


def default_blueprint_path() -> str:
    """Where the Craft Database keeps the pinned build's blueprints.json."""
    from shared import scunpacked
    return os.path.join(scunpacked.cache_dir(), BLUEPRINT_FILE)


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _parse_group(group: dict) -> List[dict]:
    """One ingredient group -> one entry per usable modifier on it."""
    out: List[dict] = []
    key = group.get("Key")
    if not isinstance(key, str) or not key:
        return out
    materials = [c.get("Name") for c in (group.get("Children") or [])
                 if isinstance(c, dict) and isinstance(c.get("Name"), str)]
    for mod in group.get("Modifiers") or []:
        if not isinstance(mod, dict):
            continue
        qr = mod.get("QualityRange") or {}
        mr = mod.get("ModifierRange") or {}
        q_min, q_max = _num(qr.get("Min")), _num(qr.get("Max"))
        at_min, at_max = _num(mr.get("AtMinQuality")), _num(mr.get("AtMaxQuality"))
        if None in (q_min, q_max, at_min, at_max) or q_max <= q_min:
            raise ValueError(f"group {key!r}: modifier has no usable range")
        if mod.get("ValueRangeType") != "linear":
            # Only the linear shape is understood here. Guessing at another one
            # would put an invented number on screen.
            raise ValueError(f"group {key!r}: unsupported ValueRangeType "
                             f"{mod.get('ValueRangeType')!r}")
        out.append({
            "key": key,
            "name": str(group.get("Name") or key),
            "material": ", ".join(materials),
            "modifier_key": str(mod.get("Key") or ""),
            "label": str(mod.get("Name") or ""),
            "q_min": q_min, "q_max": q_max,
            "at_min": at_min, "at_max": at_max,
        })
    return out


def build_index(records: Any) -> Dict[str, dict]:
    """Raw blueprints.json records -> {laser name: {"power": [...], "integrity": [...]}}.

    Only mining lasers that have at least one Laser Power group are kept. A laser
    whose blueprint cannot be understood is left out (so it is simply not offered
    as crafted) rather than given a made-up factor.
    """
    index: Dict[str, dict] = {}
    if not isinstance(records, list):
        return index
    for rec in records:
        if not isinstance(rec, dict):
            continue
        output = rec.get("Output") or {}
        if not isinstance(output, dict) or output.get("Type") != LASER_OUTPUT_TYPE:
            continue
        name = output.get("Name")
        tiers = rec.get("Tiers") or []
        if not isinstance(name, str) or not name or not tiers:
            continue
        try:
            children = ((tiers[0] or {}).get("Requirements") or {}).get("Children") or []
            groups: List[dict] = []
            for g in children:
                if isinstance(g, dict):
                    groups.extend(_parse_group(g))
        except (ValueError, AttributeError, TypeError) as exc:
            log.warning("mining_crafting: blueprint for %r skipped: %s", name, exc)
            continue
        power = [g for g in groups if g["modifier_key"] == POWER_KEY]
        if not power:
            continue
        index[name] = {
            "power": power,
            "integrity": [g for g in groups if g["modifier_key"] == INTEGRITY_KEY],
        }
    return index


def load_index(path: Optional[str] = None) -> Dict[str, dict]:
    """The crafted-laser index from the blueprint cache. Never raises, never downloads.

    Returns {} when the file is missing or unreadable.
    """
    try:
        p = path or default_blueprint_path()
        st = os.stat(p)
    except (OSError, ImportError) as exc:
        log.info("mining_crafting: no blueprint data (%s); crafted lasers not offered", exc)
        return {}
    with _cache_lock:
        hit = _cache.get(p)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    try:
        with open(p, "r", encoding="utf-8") as fh:
            index = build_index(json.load(fh))
    except (OSError, ValueError) as exc:
        log.warning("mining_crafting: could not read %s (%s); crafted lasers not offered", p, exc)
        return {}
    with _cache_lock:
        _cache[p] = (st.st_mtime, st.st_size, index)
    return index


def is_craftable(laser_name: str, index: Optional[Dict[str, dict]]) -> bool:
    return bool(index) and laser_name in index


def default_quality(group: dict) -> int:
    """The middle of the group's quality range (500 for 0..1000)."""
    return int(round((group["q_min"] + group["q_max"]) / 2.0))


def clamp_quality(group: dict, quality: Any) -> float:
    """A quality inside the group's range; the middle if it is not a number."""
    q = _num(quality)
    if q is None:
        q = float(default_quality(group))
    return min(max(q, group["q_min"]), group["q_max"])


def modifier_at(group: dict, quality: Any) -> float:
    """The group's modifier at a material quality (clamped to its range)."""
    q = clamp_quality(group, quality)
    t = (q - group["q_min"]) / (group["q_max"] - group["q_min"])
    return group["at_min"] + t * (group["at_max"] - group["at_min"])


def normalize_combine(combine: Any) -> str:
    return combine if combine in COMBINE_RULES else DEFAULT_COMBINE


def combine_values(values: List[float], combine: Any = None) -> float:
    """Combine the per-group modifiers into one factor under the chosen rule."""
    if not values:
        return 1.0
    if normalize_combine(combine) == COMBINE_AVERAGE:
        return sum(values) / len(values)
    out = 1.0
    for v in values:
        out *= v
    return out


def power_factor(laser_name: str, qualities: Any, combine: Any = None,
                 index: Optional[Dict[str, dict]] = None) -> float:
    """Laser power factor for a crafted laser, from the blueprint data.

    ``qualities`` maps a group key ("EMITTER", "BUS BARS") to the material
    quality used. A group with no entry counts at the middle of its range.
    Returns 1.0 for a laser with no blueprint.
    """
    entry = (index or {}).get(laser_name)
    if not entry:
        return 1.0
    q = qualities if isinstance(qualities, dict) else {}
    return combine_values([modifier_at(g, q.get(g["key"])) for g in entry["power"]], combine)


def integrity_factor(laser_name: str, qualities: Any,
                     index: Optional[Dict[str, dict]] = None) -> Optional[float]:
    """Integrity factor (information only; neither tool has an integrity stat)."""
    entry = (index or {}).get(laser_name)
    if not entry or not entry["integrity"]:
        return None
    q = qualities if isinstance(qualities, dict) else {}
    return combine_values([modifier_at(g, q.get(g["key"])) for g in entry["integrity"]],
                          COMBINE_MULTIPLY)


def resolve_power_factor(laser_name: str, crafted: Any, combine: Any = None,
                         index: Optional[Dict[str, dict]] = None) -> float:
    """The factor a reader of a loadout file should apply to one turret's laser.

    ``crafted`` is the turret's optional "crafted" object (or None).

    Which number wins:
      * blueprint data available  -> recomputed from the stored INPUTS (qualities
        + combine rule). The stored "power_factor" is ignored, so a change of rule
        or of the game's ranges takes effect. A laser the data says is not
        craftable gets 1.0 whatever the file claims.
      * no blueprint data at all  -> the stored "power_factor", if it is a sane
        positive number; otherwise 1.0.
    """
    if not isinstance(crafted, dict):
        return 1.0
    if index:
        if laser_name not in index:
            return 1.0
        return power_factor(laser_name, crafted.get(FIELD_QUALITIES), combine, index)
    stored = _num(crafted.get(FIELD_POWER_FACTOR))
    if stored is not None and stored > 0:
        return stored
    return 1.0


def crafted_entry(laser_name: str, qualities: Any, combine: Any = None,
                  index: Optional[Dict[str, dict]] = None) -> Optional[dict]:
    """The "crafted" object to write for a turret, or None if not craftable.

    Stores the user's inputs (one quality per Laser Power group) plus the factor
    they produced, as a fallback for a reader without the blueprint data.
    """
    entry = (index or {}).get(laser_name)
    if not entry:
        return None
    q = qualities if isinstance(qualities, dict) else {}
    clean: Dict[str, int] = {}
    for g in entry["power"]:
        clean[g["key"]] = int(round(clamp_quality(g, q.get(g["key"]))))
    return {
        FIELD_QUALITIES: clean,
        FIELD_POWER_FACTOR: round(power_factor(laser_name, clean, combine, index), 6),
    }


def clean_crafted(raw: Any) -> Optional[dict]:
    """Validate a "crafted" object read from disk. None if it is not usable."""
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Any] = {FIELD_QUALITIES: {}}
    q = raw.get(FIELD_QUALITIES)
    if isinstance(q, dict):
        for k, v in q.items():
            n = _num(v)
            if isinstance(k, str) and n is not None:
                out[FIELD_QUALITIES][k] = int(round(n))
    stored = _num(raw.get(FIELD_POWER_FACTOR))
    if stored is not None and stored > 0:
        out[FIELD_POWER_FACTOR] = stored
    return out
