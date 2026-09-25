# COPIED from Battle_Buddy/core/inventory_tracker.py on 2026-09-24 (J: "battle buddy already reads and stores information for
# loadout so that would be easy to reuse"). COPIED, never imported: narrator_seam bans SuitMk2 from importing
# another tool's modules. A loadout is inventory (what the pilot carries), never a position, so it stays on the
# narrator side of the line. Keep in step with the Battle_Buddy original by hand.
"""
Maintains a live snapshot of the player's loadout from parsed inventory events.

Tracks:
  - Primary weapons (wep_stocked_1/2/3)     — up to 2 shown
  - Sidearm / Medgun  (wep_sidearm)
  - Utility tool      (wep_stocked_3 or mag-bearing tool)
  - Spare magazines per weapon type
  - Medpens / Oxypens (0–2 each)
  - Grenades (count)

Emits a state-changed signal (callable) whenever anything changes.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from loadout_parser import (   # copied from Battle_Buddy, never imported (narrator_seam)
    InventoryEvent,
    PEN_PORTS,
    SIDEARM_PORTS,
    PRIMARY_PORTS,
    UTILITY_PORTS,
    MODULE_PORT,
    MAG_PORT_RE,
    GRENADE_PORT_RE,
    classify_ammo,
    classify_weapon_type,
    classify_module,
    classify_pen,
    classify_grenade,
    pretty_weapon_name,
    base_class,
)

logger = logging.getLogger(__name__)

# How many seconds all pen slots must clear within to be counted as an armour swap
_ARMOUR_SWAP_WINDOW = 6.0
# Minimum pen slots removed in the window to call it an armour swap
_ARMOUR_SWAP_MIN    = 3


@dataclass
class WeaponSlot:
    slot: str           # "primary_1", "primary_2", "sidearm", "utility"
    class_name: str
    display_name: str
    weapon_type: str    # "Pistol", "Rifle", "Sniper", etc.
    ammo_type: str      # "energy", "ballistic", "unknown"
    entity_id: str
    spare_mags: int = 0
    module: str = ""    # attached module name (e.g. "Tractor Beam") — utility only


@dataclass
class PenSlot:
    """A single consumable pen in a body port."""
    port: str           # e.g. "medPen_attach_1"
    class_name: str     # e.g. "crlf_consumable_healing_01"
    display_name: str   # e.g. "MedPen"
    category: str       # "med", "oxy", "stim", "detox", "other"
    entity_id: str


@dataclass
class GrenadeSlot:
    """A single grenade/throwable in a body port."""
    port: str
    class_name: str
    display_name: str   # e.g. "Frag", "EMP", "Glowstick"
    entity_id: str


@dataclass
class LoadoutState:
    """Snapshot of the current loadout.  Thread-safe (copy-on-read)."""
    weapons: dict[str, WeaponSlot] = field(default_factory=dict)
    # slot → class_name  for spare mags on armour
    spare_mag_entities: dict[str, str] = field(default_factory=dict)
    # slot → entity_id  for spare mags (parallel to spare_mag_entities)
    spare_mag_eids: dict[str, str] = field(default_factory=dict)
    # pen tracking: port → PenSlot  (all medPen/oxyPen ports)
    pens: dict[str, PenSlot] = field(default_factory=dict)
    # grenade tracking: port → GrenadeSlot
    grenades: dict[str, GrenadeSlot] = field(default_factory=dict)

    # ── Convenience accessors for the HUD ─────────────────────────────────
    @property
    def medpens(self) -> int:
        return sum(1 for p in self.pens.values() if p.category == "med")

    @property
    def oxypens(self) -> int:
        return sum(1 for p in self.pens.values() if p.category == "oxy")

    @property
    def grenade_count(self) -> int:
        return len(self.grenades)

    def pens_by_category(self) -> dict[str, list[PenSlot]]:
        """Group equipped pens by category."""
        out: dict[str, list[PenSlot]] = {}
        for p in self.pens.values():
            out.setdefault(p.category, []).append(p)
        return out

    def grenades_by_type(self) -> dict[str, int]:
        """Group grenades by display name → count."""
        out: dict[str, int] = {}
        for g in self.grenades.values():
            out[g.display_name] = out.get(g.display_name, 0) + 1
        return out


class InventoryTracker:
    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._state  = LoadoutState()
        self._cbs: list[Callable[[LoadoutState], None]] = []
        # Ring-buffer of recent pen-removal timestamps for armour-swap detection
        self._pen_removals: list[float] = []
        # Ghost-avatar suppression: when Body_ItemPort is attached and we
        # already have a loadout, suppress weapon/consumable overwrites
        # for a short window until a stocked-weapon event proves it's real.
        self._suppress_until: float = 0.0
        self._SUPPRESS_WINDOW: float = 2.0  # seconds

    def on_changed(self, callback: Callable[[LoadoutState], None]) -> None:
        self._cbs.append(callback)

    def on_event(self, event: InventoryEvent) -> None:
        if event.event_type == "session_join":
            with self._lock:
                self._state = LoadoutState()
                self._suppress_until = 0.0
            self._emit()
            return

        if event.event_type not in ("attach", "remove"):
            return

        d    = event.data
        port = d.get("port", "")
        cls  = d.get("class_name", "")
        eid  = d.get("entity_id", "")
        ts   = event.timestamp.timestamp()

        with self._lock:
            # Detect ghost-avatar reload: Body_ItemPort re-attach while we
            # already have weapons → suppress sidearm/consumable overwrites
            # for _SUPPRESS_WINDOW seconds.
            if port == "Body_ItemPort" and self._state.weapons:
                self._suppress_until = ts + self._SUPPRESS_WINDOW
                return

            # A stocked-weapon or utility attach proves this is a real
            # full loadout — cancel suppression.
            if port in PRIMARY_PORTS or port in UTILITY_PORTS:
                self._suppress_until = 0.0

            # While suppressed, only skip sidearm overwrites.
            # Ghost avatars only carry a default sidearm — pens and mags
            # should always be tracked since they're part of legitimate
            # loadout reloads that happen alongside Body_ItemPort events.
            if ts < self._suppress_until:
                if port in SIDEARM_PORTS:
                    return

            changed = False
            if event.event_type == "attach":
                changed = self._handle_attach(port, cls, eid)
            else:
                changed = self._handle_remove(port, cls, eid, ts)

        if changed:
            self._emit()

    def flush(self) -> None:
        """No-op kept for API compatibility with backfill callers."""
        pass

    # ── Attach ───────────────────────────────────────────────────────────────

    def _handle_attach(self, port: str, cls: str, eid: str) -> bool:
        s = self._state

        # Detect pen/grenade use: when an entity appears in the player's
        # hand (weapon_attach_hand_right), it was drawn from an armor slot.
        # Remove it from tracking — if the player cancels and the item
        # re-attaches to its slot, _handle_attach will re-add it.
        if port == "weapon_attach_hand_right":
            # Check pens
            for pen_port, pen in s.pens.items():
                if pen.entity_id == eid:
                    del s.pens[pen_port]
                    return True
            # Check grenades
            for gren_port, gren in s.grenades.items():
                if gren.entity_id == eid:
                    del s.grenades[gren_port]
                    return True
            return False

        # Detect reload: when a mag entity appears on the bare
        # ``magazine_attach`` port (loaded in weapon), check if that same
        # entity was tracked in a numbered spare slot and remove it.
        if port == "magazine_attach":
            consumed = [
                slot for slot, tracked_eid in s.spare_mag_eids.items()
                if tracked_eid == eid
            ]
            if consumed:
                for slot in consumed:
                    s.spare_mag_entities.pop(slot, None)
                    s.spare_mag_eids.pop(slot, None)
                self._recount_mags(s)
                return True
            return False  # not a spare slot event, ignore

        # Pen slots — categorize purely by class name, NOT port name.
        # Players can put any pen type into any pen slot.
        if port in PEN_PORTS:
            display_name, category = classify_pen(cls)
            s.pens[port] = PenSlot(
                port=port, class_name=cls,
                display_name=display_name, category=category,
                entity_id=eid,
            )
            return True

        # Grenade slots
        if GRENADE_PORT_RE.match(port):
            s.grenades[port] = GrenadeSlot(
                port=port, class_name=cls,
                display_name=classify_grenade(cls),
                entity_id=eid,
            )
            return True

        # Multitool module attachment (tractor beam, healing, etc.)
        if port == MODULE_PORT:
            utility = s.weapons.get("utility")
            if utility:
                utility.module = classify_module(cls)
            return bool(utility)

        # Spare magazine slots on armour
        if MAG_PORT_RE.match(port):
            s.spare_mag_entities[port] = cls
            s.spare_mag_eids[port] = eid
            self._recount_mags(s)
            return True

        # Weapon slots
        slot_key = self._port_to_slot(port)
        if slot_key:
            weapon = WeaponSlot(
                slot=slot_key,
                class_name=cls,
                display_name=pretty_weapon_name(cls),
                weapon_type=classify_weapon_type(cls),
                ammo_type=classify_ammo(cls),
                entity_id=eid,
            )
            s.weapons[slot_key] = weapon
            self._recount_mags(s)
            return True

        return False

    # ── Remove ───────────────────────────────────────────────────────────────

    def _handle_remove(self, port: str, cls: str, eid: str, ts: float) -> bool:
        s = self._state

        if port in PEN_PORTS:
            # Record this removal timestamp for armour-swap detection
            self._pen_removals.append(ts)
            # Flush old entries
            cutoff = ts - _ARMOUR_SWAP_WINDOW
            self._pen_removals = [t for t in self._pen_removals if t >= cutoff]

            if len(self._pen_removals) >= _ARMOUR_SWAP_MIN:
                # Armour swap — clear all pen tracking; new attach events will follow
                s.pens.clear()
                self._pen_removals.clear()
            else:
                # Single removal — likely consumed
                s.pens.pop(port, None)
            return True

        if GRENADE_PORT_RE.match(port):
            s.grenades.pop(port, None)
            return True

        if MAG_PORT_RE.match(port):
            s.spare_mag_entities.pop(port, None)
            s.spare_mag_eids.pop(port, None)
            self._recount_mags(s)
            return True

        slot_key = self._port_to_slot(port)
        if slot_key and slot_key in s.weapons:
            del s.weapons[slot_key]
            self._recount_mags(s)
            return True

        return False

    # ── Helpers ──────────────────────────────────────────────────────────────

    # Deterministic port → display-slot mapping.
    # Each game port always maps to the same HUD slot — no overflow logic.
    # When the game re-attaches a weapon to the same port (draw/stow cycle),
    # it simply updates that slot rather than spilling into another.
    _PORT_SLOT = {
        "wep_sidearm":      "sidearm",
        "wep_stocked_1":    "primary_1",
        "wep_stocked_2":    "primary_1",
        "wep_stocked_3":    "primary_2",
        "wep_stocked_4":    "primary_2",
        "utility_attach_1": "utility",
        "utility_attach_2": "utility_2",
        "utility_attach":   "utility",
    }

    def _port_to_slot(self, port: str) -> str | None:
        return self._PORT_SLOT.get(port)

    def _recount_mags(self, s: LoadoutState) -> None:
        """Recompute spare_mags for each weapon based on matching mag class names.

        Matching strategy: strip cosmetic suffixes (``_store01`` etc.) from
        weapon class names before comparing, since mag classes never carry
        those suffixes.  e.g. weapon ``volt_sniper_energy_01_store01`` and
        mag ``volt_sniper_energy_01_mag`` both normalise to
        ``volt_sniper_energy_01``.
        """
        # Reset counts
        for w in s.weapons.values():
            w.spare_mags = 0

        for _port, mag_cls in s.spare_mag_entities.items():
            mc = mag_cls.lower()
            # Strip trailing _mag to get the weapon base from the mag class
            mag_base = mc.rsplit("_mag", 1)[0] if mc.endswith("_mag") else mc

            best: WeaponSlot | None = None
            best_len = 0
            for w in s.weapons.values():
                # Normalise weapon class: strip _store01 etc.
                wc = base_class(w.class_name).lower()
                if wc == mag_base or mag_base.startswith(wc) or wc.startswith(mag_base):
                    if len(wc) > best_len:
                        best = w
                        best_len = len(wc)
            if best:
                best.spare_mags += 1

    def _emit(self) -> None:
        with self._lock:
            snap = self._snapshot()
        for cb in self._cbs:
            try:
                cb(snap)
            except Exception:
                logger.exception("InventoryTracker callback error")

    def _snapshot(self) -> LoadoutState:
        s = self._state
        snap = LoadoutState(
            weapons={k: WeaponSlot(**v.__dict__) for k, v in s.weapons.items()},
            spare_mag_entities=dict(s.spare_mag_entities),
            spare_mag_eids=dict(s.spare_mag_eids),
            pens={k: PenSlot(**v.__dict__) for k, v in s.pens.items()},
            grenades={k: GrenadeSlot(**v.__dict__) for k, v in s.grenades.items()},
        )
        return snap

    def get_state(self) -> LoadoutState:
        with self._lock:
            return self._snapshot()
