"""Restore a saved loadout's per-slot selections onto the slots a ship has now.

Saved loadouts store ``{"selections": {section: {slot_id: component_name}}}``.
Released builds up to 2.3.1 built weapon slot ids from erkul's port tree in
``services/slot_extractor.py``: ``"<parent label>:<itemPortName>"``, e.g.
``":hardpoint_weapon_left"`` for a top-level hardpoint or
``"Turret Top:hardpoint_weapon_left_3"`` for a gun inside a turret (the
``_3`` is a running slot counter appended to inner-gun ports). 3.0 builds
weapon rows from scunpacked-data (``data/scunpacked_provider._gun_slots``) and
keys them ``"sc:" + <HardpointName path>``, e.g.
``"sc:hardpoint_turret_top/hardpoint_weapon_left/hardpoint_class_2"``. Without
a translation every old weapon selection silently fails to restore.

The translation only maps when the answer is certain:

* ``":<port>"`` maps to the one row whose slot path STARTS at ``<port>``.
* ``"<Label>:<port>"`` maps to the one row whose top hardpoint renders as
  ``<Label>`` (the old and new label functions are the same rule) and whose
  path below it contains ``<port>``, or ``<port>`` minus a trailing ``_<n>``
  counter.
* Zero or several candidate rows: unmapped. Several old slots landing on one
  row with DIFFERENT components (3.0 groups a turret's guns into one row):
  none of them is applied.

Anything not applied is counted, so the caller can say so instead of
restoring a partial loadout in silence.
"""
from __future__ import annotations

import re

_COUNTER_RE = re.compile(r"_\d+$")


def _label(hp: str) -> str:
    # Same rule as data/scunpacked_provider._label and the old
    # services/slot_extractor._port_label (kept local: this module is pure).
    s = re.sub(r"hardpoint_|_weapon$|weapon_", "", hp or "", flags=re.I)
    s = re.sub(r"_+", " ", s).strip()
    return s.title() if s else (hp or "?")


def _row_matches(old_id: str, row: dict) -> bool:
    label, sep, port = old_id.rpartition(":")
    if not sep or not port:
        return False
    port_l = port.lower()
    stripped = _COUNTER_RE.sub("", port_l)
    for sl in row.get("sc_slots") or []:
        parts = [p.lower() for p in str(sl.get("id", "")).split("/")]
        if not parts or not parts[0]:
            continue
        if label == "":
            if port_l == parts[0]:
                return True
        elif label == _label(parts[0]) and (
                port_l in parts[1:] or (stripped != port_l and stripped in parts[1:])):
            return True
    return False


def map_legacy_id(old_id: str, slots: list) -> str | None:
    """The current id of ``old_id`` among ``slots``, or None if not certain."""
    if not old_id or old_id.startswith("sc:"):
        return None
    hits = [s for s in slots if s.get("sc_slots") and _row_matches(old_id, s)]
    return hits[0]["id"] if len(hits) == 1 else None


def resolve_selections(saved: dict, slots_by_section: dict) -> tuple[dict, int]:
    """Plan a restore.

    ``saved``: the file's ``selections`` dict. ``slots_by_section``:
    ``{section: [slot dict, ...]}`` for the ship as loaded now.
    Returns ``({section: {current_slot_id: name}}, unplaced)`` where
    ``unplaced`` counts saved, non-empty selections that have no slot to go to.
    """
    plan: dict = {}
    unplaced = 0
    for section, entries in (saved or {}).items():
        if not isinstance(entries, dict):
            continue
        slots = slots_by_section.get(section) or []
        current = {s.get("id") for s in slots}
        direct: dict = {}
        mapped: dict = {}          # target id -> [names]
        for sid, name in entries.items():
            if not name:
                continue           # "" = the user cleared the slot; nothing to restore
            if sid in current:
                direct[sid] = name
                continue
            target = map_legacy_id(sid, slots)
            if target is None:
                unplaced += 1
            else:
                mapped.setdefault(target, []).append(name)
        out = dict(direct)
        for target, names in mapped.items():
            wanted = set(names) | ({direct[target]} if target in direct else set())
            if len(wanted) == 1:
                out[target] = names[0]
            else:
                unplaced += len(names)   # conflicting choices for one row: apply none
        if out:
            plan[section] = out
    return plan, unplaced


def not_restored_message(n: int) -> str:
    if n <= 0:
        return ""
    return (f"{n} saved selection could not be restored" if n == 1
            else f"{n} saved selections could not be restored")
