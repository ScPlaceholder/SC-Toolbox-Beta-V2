# COPIED from Battle_Buddy/core/inventory_parser.py (Battle Buddy already reads and stores information for
# loadout, so it was easy to reuse). COPIED, never imported: narrator_seam bans SuitMk2 from importing
# another tool's modules. A loadout is inventory (what the pilot carries), never a position, so it stays on the
# narrator side of the line. Keep in step with the Battle_Buddy original by hand.
"""
Parses Star Citizen Game.log lines relevant to loadout tracking.

Emits structured events for:
  - AttachmentReceived  (item equipped to a player port)
  - ItemRemovedFromPort (item removed from a player port / "removed before timer")
  - SessionJoin         ({Join PU} — player loaded into game)
  - SessionEnd          (SystemQuit / disconnect)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

# ── Port classification ───────────────────────────────────────────────────────

#: Ports that hold medpens and oxypens
PEN_PORTS = frozenset({
    "medPen_attach_1", "medPen_attach_2",
    "oxyPen_attach_1", "oxyPen_attach_2",
})

#: Ports that hold sidearms
SIDEARM_PORTS = frozenset({"wep_sidearm"})

#: Ports that hold primary / secondary weapons (stocked on back)
PRIMARY_PORTS = frozenset({"wep_stocked_1", "wep_stocked_2", "wep_stocked_3", "wep_stocked_4"})

#: Ports that hold utility tools (multitool, med-gun, tractor beam tool, etc.)
UTILITY_PORTS = frozenset({"utility_attach_1", "utility_attach_2", "utility_attach"})

#: Ports for multitool module attachments (tractor beam, healing beam, etc.)
MODULE_PORT = "module_attach"

#: Ports that hold spare magazines on armour (numbered slots only).
#: ``magazine_attach`` (no number) is the mag loaded *in the weapon* and
#: should NOT be counted as a spare.
MAG_PORT_RE = re.compile(r"^magazine_attach_\d+$")

#: Ports that hold grenades / throwables
GRENADE_PORT_RE = re.compile(r"^grenade_attach(_\d+)?$")

# ── Grenade / throwable names ────────────────────────────────────────────────
_GRENADE_KEYWORDS: dict[str, str] = {
    "frag":         "Frag",
    "emp":          "EMP",
    "concussion":   "Concussion",
    "flashbang":    "Flashbang",
    "shutter":      "Flashbang",
    "smoke":        "Smoke",
    "haze":         "Smoke",
    "force":        "Force",
    "mine":         "Mine",
    "lidar":        "LIDAR",
    "radar":        "Radar Scatter",
    "glowstick":    "Glowstick",
    "flare":        "Flare",
}


def classify_grenade(class_name: str) -> str:
    """Return a friendly name for a grenade/throwable class."""
    c = class_name.lower()
    for kw, label in _GRENADE_KEYWORDS.items():
        if kw in c:
            return label
    return "Grenade"

#: Keywords in the class name that identify weapon type
_WEAPON_TYPE_KEYWORDS: dict[str, str] = {
    "pistol":    "Pistol",
    "smg":       "SMG",
    "rifle":     "Rifle",
    "shotgun":   "Shotgun",
    "sniper":    "Sniper",
    "lmg":       "LMG",
    "carbine":   "Carbine",
    "launcher":  "Launcher",
    "multitool": "Multitool",
    "tractor":   "Tractor",
    "medgun":    "Medgun",
    "repair":    "Repair Tool",
}

#: Ammo type from class name
def classify_ammo(class_name: str) -> str:
    c = class_name.lower()
    if "energy" in c:
        return "energy"
    if "ballistic" in c:
        return "ballistic"
    if "distortion" in c:
        return "distortion"
    return "unknown"

def classify_weapon_type(class_name: str) -> str:
    c = class_name.lower()
    for kw, label in _WEAPON_TYPE_KEYWORDS.items():
        if kw in c:
            return label
    return "Weapon"

def base_class(class_name: str) -> str:
    """Normalize a class name by stripping cosmetic/variant suffixes.

    ``volt_sniper_energy_01_store01`` → ``volt_sniper_energy_01``
    ``none_pistol_ballistic_01``      → ``none_pistol_ballistic_01``
    """
    return re.sub(r"_(store|black|tint|iae|grey|yellow|blue|red|green)\w*$", "", class_name)


# ── Consumable pen types ─────────────────────────────────────────────────────
# Maps class name → (short display name, category for the HUD bar)
# Categories: "med", "oxy", "stim", "detox", "other"
_PEN_TYPES: dict[str, tuple[str, str]] = {
    "crlf_consumable_healing_01":           ("MedPen",        "med"),
    "crlf_consumable_oxygen_01":            ("OxyPen",        "oxy"),
    "crlf_consumable_adrenaline_01":        ("AdrenaPen",     "stim"),
    "crlf_consumable_adrenaline_02":        ("AdrenaPen+",    "stim"),
    "crlf_consumable_overdoserevival_01":    ("DetoxPen",      "detox"),
    "crlf_consumable_steroid_01":           ("CorticoPen",    "stim"),
    "crlf_consumable_steroid_02":           ("CorticoPen+",   "stim"),
    "crlf_consumable_painkiller_01":        ("OpioPen",       "med"),
    "crlf_consumable_radiation_01":         ("DeconPen",      "detox"),
    "crlf_consumable_radiation_02":         ("DeconPen+",     "detox"),
    "crlf_consumable_gopill_01":            ("BoostPen",      "stim"),
    "crlf_consumable_gopill_02":            ("BoostPen+",     "stim"),
    "crlf_consumable_tpn_01":              ("VitalityPen",   "med"),
    "rrs_consumable_sedative_01":           ("Drema",         "other"),
}


def classify_pen(class_name: str) -> tuple[str, str]:
    """Return (display_name, category) for a consumable pen class.

    Falls back to the port name heuristic if the class is unknown.
    """
    info = _PEN_TYPES.get(class_name.lower())
    if info:
        return info
    # Fallback: guess from class name keywords
    c = class_name.lower()
    if "healing" in c or "medpen" in c:
        return ("MedPen", "med")
    if "oxygen" in c or "oxypen" in c:
        return ("OxyPen", "oxy")
    if "adrenaline" in c or "adrenapen" in c:
        return ("AdrenaPen", "stim")
    return ("Pen", "other")


# ── Multitool module names ────────────────────────────────────────────────────

_MODULE_NAMES: dict[str, str] = {
    "tractorbeam":  "Tractor Beam",
    "heal":         "Healing",
    "mining":       "Mining",
    "salvage":      "Salvage",
    "repair":       "Repair",
    "cutting":      "Cutting",
}


def classify_module(class_name: str) -> str:
    """Return a friendly name for a multitool module class."""
    c = class_name.lower()
    for key, label in _MODULE_NAMES.items():
        if key in c:
            return label
    return "Module"


# ── Known SC weapon names ────────────────────────────────────────────────────

_WEAPON_NAMES: dict[str, str] = {
    # ── Kastak Arms ──
    "ksar_rifle_ballistic_01":      "Karna",
    "ksar_rifle_ballistic_02":      "Custodian",
    "ksar_smg_ballistic_01":        "Coda",
    "ksar_pistol_ballistic_01":     "Devastator",
    # ── Klaus & Werner ──
    "klwe_pistol_energy_01":        "Arclight",
    "klwe_rifle_energy_01":         "Lumin V",
    "klwe_smg_energy_01":           "Lumin VI",
    "klwe_lmg_energy_01":           "Demeco",
    # ── Behring ──
    "behr_rifle_ballistic_01":      "P4-AR",
    "behr_rifle_ballistic_02":      "P6-LR",
    "behr_smg_ballistic_01":        "P4-CQB",
    "behr_pistol_ballistic_01":     "GP-33",
    "behr_shotgun_ballistic_01":    "GP-33 Scatter",
    "behr_lmg_ballistic_01":        "F55",
    "behr_rifle_ballistic_03":      "S71",
    # ── Gemini ──
    "gmni_pistol_energy_01":        "LH86",
    "gmni_rifle_energy_01":         "A03",
    "gmni_smg_energy_01":           "C54",
    # ── Lightning Bolt Co. ──
    "lbco_pistol_ballistic_01":     "Yubarev",
    "lbco_pistol_energy_01":        "Cutter",
    # ── Hedeby Gunworks ──
    "hdby_pistol_energy_01":        "Salvo Frag",
    # ── Apocalypse Arms ──
    "apar_sniper_ballistic_01":     "Attrition-1",
    "apar_rifle_ballistic_01":      "Attrition-2",
    "apar_sniper_energy_01":        "Animus",
    # ── Preacher Armaments ──
    "prar_shotgun_ballistic_01":    "Devastator",
    # ── VOLT (Verified Offworld Laser Technologies) ──
    "volt_sniper_energy_01":        "Zenith",
    "volt_lmg_energy_01":           "Fresnel",
    "volt_rifle_energy_01":         "Parallax",
    "volt_shotgun_energy_01":       "Prism",
    "volt_pistol_energy_01":        "Pulse",
    "volt_smg_energy_01":           "Quartz",
    # ── None / Generic manufacturer ──
    "none_pistol_ballistic_01":     "TripleDown",
    "none_rifle_ballistic_01":      "Gallant",
    # ── Greycat Industrial ──
    "grin_multitool_energy_01":     "Multitool",
    "grin_multitool_01":            "Multitool",
    # ── RSI / Origin ──
    "rsi_medgun_energy_01":         "MedGun",
}


def pretty_weapon_name(class_name: str) -> str:
    """Return a player-friendly weapon name.

    Checks the known-name table first; falls back to a cleaned-up version
    of the class name.
    """
    base = base_class(class_name)
    # Try exact lookup (lowercase)
    known = _WEAPON_NAMES.get(base.lower())
    if known:
        return known

    # Strip trailing _01, _02, etc. and cosmetic suffixes for fallback
    name = re.sub(r"_\d+$", "", base)
    parts = name.split("_")
    # Drop ammo-type tokens and generic manufacturer 'none'
    drop = {"energy", "ballistic", "distortion", "mag", "none"}
    parts = [p for p in parts if p.lower() not in drop]
    return " ".join(p.capitalize() for p in parts if p) or class_name


# ── Events ────────────────────────────────────────────────────────────────────

@dataclass
class InventoryEvent:
    event_type: str          # "attach" | "remove" | "session_join" | "session_end"
    timestamp: datetime
    raw_line: str
    data: dict[str, Any] = field(default_factory=dict)


# ── Parser ────────────────────────────────────────────────────────────────────

_TS_RE       = re.compile(r"<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_ATTACH_RE   = re.compile(
    r"<AttachmentReceived>\s+Player\[(\w+)\]\s+Attachment\[([^,]+),\s*([^,]+),\s*(\d+)\]\s+Status\[(\w+)\]\s+Port\[([^\]]+)\]"
)
_REMOVE_RE   = re.compile(
    r"in port\[([^\]]+)\] has been removed before timer\.\s+Persistent Entity\[([^,\]]+),\s*([^\]]+)\]"
)


class InventoryParser:
    def __init__(self, player_name: str | None = None) -> None:
        self._player = player_name  # None = accept all
        self._callbacks: list[Callable[[InventoryEvent], None]] = []

    def subscribe(self, callback: Callable[[InventoryEvent], None]) -> None:
        self._callbacks.append(callback)

    def on_line(self, line: str) -> None:
        event = self._parse(line)
        if event:
            for cb in self._callbacks:
                try:
                    cb(event)
                except Exception:
                    logging.getLogger(__name__).exception("InventoryParser callback error")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _ts(self, line: str) -> datetime:
        m = _TS_RE.search(line)
        if m:
            try:
                return datetime.fromisoformat(m.group(1))
            except ValueError:
                pass
        return datetime.now()

    def _parse(self, line: str) -> InventoryEvent | None:
        # Session join
        if "{Join PU}" in line:
            return InventoryEvent("session_join", self._ts(line), line)

        # Session end
        if "SystemQuit" in line or "Disconnecting from Stanton" in line or "Disconnecting from Pyro" in line:
            return InventoryEvent("session_end", self._ts(line), line)

        # Item attached to player port
        if "<AttachmentReceived>" in line and "Player[" in line:
            m = _ATTACH_RE.search(line)
            if m:
                player, entity_name, class_name, entity_id, status, port = m.groups()
                if self._player and player != self._player:
                    return None
                # Ignore placeholder/animation events (Status[local] with
                # generic class names like "Default" or "Inventory_LocalAttach_Item")
                if status == "local":
                    return None
                return InventoryEvent("attach", self._ts(line), line, data={
                    "player":      player,
                    "entity_name": entity_name,
                    "class_name":  class_name,
                    "entity_id":   entity_id,
                    "status":      status,
                    "port":        port,
                })

        # Item removed from player port
        if "has been removed before timer" in line and "Player[" in line:
            # Ignore local-placeholder cleanup events.  These fire when the
            # inventory system replaces a ``Status[local]`` placeholder with
            # the real ``Status[persistent]`` entity (e.g. dragging a mag
            # from backpack to armor).  The text always contains
            # ``Local attached entity[null, null]``.
            if "Local attached entity[null" in line:
                return None

            # Extract player name
            pm = re.search(r"Player\[(\w+)\]", line)
            player = pm.group(1) if pm else ""
            if self._player and player != self._player:
                return None
            m = _REMOVE_RE.search(line)
            if m:
                port, entity_name, class_name = m.groups()
                return InventoryEvent("remove", self._ts(line), line, data={
                    "player":      player,
                    "port":        port.split(":")[-1],   # strip Body_ItemPort:... prefix
                    "entity_name": entity_name.strip(),
                    "class_name":  class_name.strip(),
                })

        return None
