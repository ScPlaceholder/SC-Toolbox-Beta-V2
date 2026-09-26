"""Crafting blueprints from the pinned scunpacked-data datamine.

Source: ``blueprints.json`` of github.com/StarCitizenWiki/scunpacked-data at
the commit pinned in ``shared/scunpacked.py`` (the same build the toolbox
uses for ships and items). It is fetched once through
``scunpacked.fetch_raw`` into ``~/.sctoolbox/scunpacked/<build>/`` and its
sha256 is checked against ``scunpacked.PINNED_SHA256``.

From it a compact ``craft_index.json`` is built once (a few seconds, off the
UI thread) in the shape ``domain.models.Blueprint.from_dict`` reads, so the
window, the inventory and the filters keep working unchanged. After that,
startup only reads the index. No network is needed once the file is cached.

Raw record (one per blueprint, all ``Kind == "creation"``, one tier each)::

    Key            BP_CRAFT_AMRS_LaserCannon_S1
    Output         {Class, Type, Subtype, Grade, Name}  ([] on one broken record)
    Availability   {Default: bool, RewardPools: [{Key}]}
    Tiers[0]       {CraftTimeSeconds, Requirements: root -> group* -> leaf}
        group      {Name (slot), RequiredCount, Modifiers[], Children[]}
        leaf       resource {Name, QuantityScu, MinQuality}
                   item     {Name, Quantity, MinQuality}
        a group may hold groups ("ASPECTS": any 2 of 3 sub-slots)
    Modifiers[]    {Name, QualityRange{Min,Max}, ModifierRange{AtMinQuality,
                    AtMaxQuality}, ValueRangeType, ValueSegments[]?}
    Dismantle      {TimeSeconds, Efficiency, Returns[]}

Not in the datamine (sc-craft.tools had them): which missions drop a
blueprint, with contractor, location and drop chance. The datamine only
names the reward pool (``BP_REWARDS_NyxFoxwellEasy``); those are shown as
the blueprint's sources. Categories are derived from the output item's
type (the datamine's own category is an unnamed UUID).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from shared import scunpacked

log = logging.getLogger(__name__)

RAW_FILE = "blueprints.json"
INDEX_FILE = "craft_index.json"
INDEX_VERSION = 1


class DatamineMissing(Exception):
    """The blueprint data has not been downloaded yet."""


def game_label(build: str = scunpacked.BUILD) -> str:
    """'4.10.1-LIVE.12660092' -> 'Game data: 4.10.1-LIVE'."""
    return "Game data: " + build.rsplit(".", 1)[0]


def version_string(build: str = scunpacked.BUILD) -> str:
    """'4.10.1-LIVE.12660092' -> 'LIVE-4.10.1-12660092' (sc-craft style)."""
    m = re.match(r"^([\d.]+)-([A-Z]+)\.(\d+)$", build)
    return f"{m.group(2)}-{m.group(1)}-{m.group(3)}" if m else build


# ── categories ───────────────────────────────────────────────────────────

_COMPONENT = {"Cooler": "Cooler", "PowerPlant": "Power Plant", "Shield": "Shield",
              "QuantumDrive": "Quantum Drive", "Radar": "Radar"}
_UTILITY = {"WeaponMining": "Mining Laser", "MiningModifier": "Mining Module",
            "SalvageHead": "Salvage Head", "SalvageModifier": "Salvage Module",
            "TractorBeam": "Tractor Beam", "DockingCollar": "Fuel Nozzle",
            "Container": "Ore Pod"}
_FPS = {"pistol": "Pistol", "rifle": "Rifle", "sniper": "Sniper", "smg": "SMG",
        "shotgun": "Shotgun", "lmg": "LMG", "hmg": "HMG", "crossbow": "Crossbow"}
_ARMOUR_PART = {"Helmet": "Helmet", "Torso": "Core", "Arms": "Arms", "Legs": "Legs",
                "Backpack": "Backpack"}


def _camel_words(s: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s).replace("Scatter Gun", "Scattergun")


def categorize(out: dict, key: str) -> str:
    """'Top / Sub' category for one blueprint, from its output item."""
    typ = str(out.get("Type") or "")
    sub = str(out.get("Subtype") or "")
    cls = str(out.get("Class") or "").lower()
    if typ == "WeaponGun":
        parts = key.split("_")
        fam = parts[3] if len(parts) > 3 else ""
        if not re.search(r"(Cannon|Repeater|Gatling|ScatterGun|Driver)$", fam):
            return "Ship Weapons / Other"
        return "Ship Weapons / " + _camel_words(fam)
    if typ in _COMPONENT:
        return "Ship Components / " + _COMPONENT[typ]
    if typ in _UTILITY:
        return "Ship Utility / " + _UTILITY[typ]
    if typ == "WeaponPersonal":
        tok = cls.split("_")[1] if "_" in cls else ""
        return "Weapons / " + _FPS.get(tok, "Other")
    if typ == "WeaponAttachment":
        return "Ammo / " + (_camel_words(sub) if sub and sub != "UNDEFINED" else "Attachment")
    if typ == "Char_Armor_Undersuit":
        return "Armour / Undersuit"
    if typ.startswith("Char_Armor_"):
        part = _ARMOUR_PART.get(typ[len("Char_Armor_"):], typ[len("Char_Armor_"):])
        weight = sub if sub in ("Light", "Medium", "Heavy") else ""
        if not weight:
            weight = next((w.title() for w in ("light", "medium", "heavy") if w in cls), "")
        return "Armour / " + (f"{weight} / {part}" if weight else part)
    if typ.startswith("Char_Clothing"):
        return "Clothing"
    if typ:
        return "Misc"
    # the one record with an empty Output: use the key (BP_CRAFT_COOL_...)
    k = key.upper()
    for tag, name in (("_COOL_", "Cooler"), ("_POWR_", "Power Plant"),
                      ("_SHLD_", "Shield"), ("_QDRV_", "Quantum Drive"), ("_RADR_", "Radar")):
        if tag in k:
            return "Ship Components / " + name
    return "Misc"


# ── normalisation ────────────────────────────────────────────────────────

def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _name_from_key(key: str) -> str:
    k = re.sub(r"^BP_CRAFT_", "", key)
    k = re.sub(r"_SCItem$", "", k, flags=re.I)
    return k.replace("_", " ")


def pool_label(pool_key: str) -> str:
    """'BP_REWARDS_NyxFoxwellEasy' -> 'Nyx Foxwell Easy'."""
    k = re.sub(r"^BP_(MISSIONREWARD|REWARDS?|REWARDPOOL)_", "", pool_key or "")
    k = k.replace("_", " ")
    k = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", k)
    k = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", k)
    return re.sub(r"\s+", " ", k).strip() or pool_key


def _effect(m: dict) -> Optional[dict]:
    qr, mr = m.get("QualityRange"), m.get("ModifierRange")
    segs = m.get("ValueSegments") or []
    additive = m.get("ValueRangeType") == "linear_integer_additive"
    if additive:
        segments = [[int(_num(s.get("QualityMin"))), int(_num(s.get("QualityMax"), 1000)),
                     _num(s.get("AdditiveAtStart")), _num(s.get("AdditiveAtEnd"))]
                    for s in segs]
        if not segments:
            return None
        return {"stat": m.get("Name") or m.get("Key") or "?", "additive": True,
                "quality_min": segments[0][0], "quality_max": segments[-1][1],
                "modifier_at_min": segments[0][2], "modifier_at_max": segments[-1][3],
                "segments": segments}
    if not isinstance(qr, dict) or not isinstance(mr, dict) or not mr:
        return None                  # the datamine leaves some ranges empty: no effect
    segments = [[int(_num(s.get("QualityMin"))), int(_num(s.get("QualityMax"), 1000)),
                 _num(s.get("ModifierAtStart"), 1.0), _num(s.get("ModifierAtEnd"), 1.0)]
                for s in segs]
    eff = {"stat": m.get("Name") or m.get("Key") or "?",
           "quality_min": int(_num(qr.get("Min"))), "quality_max": int(_num(qr.get("Max"), 1000)),
           "modifier_at_min": _num(mr.get("AtMinQuality"), 1.0),
           "modifier_at_max": _num(mr.get("AtMaxQuality"), 1.0)}
    if segments:
        eff["segments"] = segments
        eff["quality_min"], eff["quality_max"] = segments[0][0], segments[-1][1]
        eff["modifier_at_min"], eff["modifier_at_max"] = segments[0][2], segments[-1][3]
    return eff


def _option(leaf: dict) -> dict:
    if leaf.get("Kind") == "item" or ("Quantity" in leaf and "QuantityScu" not in leaf):
        qty, unit = _num(leaf.get("Quantity")), "pcs"
    else:                                      # resources are in SCU; shown as cSCU
        qty, unit = round(_num(leaf.get("QuantityScu")) * 100.0, 4), "cSCU"
    return {"guid": leaf.get("UUID") or "", "name": leaf.get("Name") or "?",
            "quantity": qty, "unit": unit, "min_quality": int(_num(leaf.get("MinQuality")))}


def _slots(group: dict, choose: str = "") -> list:
    kids = group.get("Children") or []
    sub = [k for k in kids if k.get("Kind") == "group"]
    if sub:
        n = int(_num(group.get("RequiredCount"), len(sub)))
        note = f"any {n} of {len(sub)}" if n < len(sub) else ""
        out = []
        for g in sub:
            out.extend(_slots(g, note))
        return out
    opts = [_option(k) for k in kids if k.get("Kind") in ("resource", "item")]
    if not opts:
        return []
    effects = [e for e in (_effect(m) for m in group.get("Modifiers") or []) if e]
    return [{"slot": group.get("Name") or group.get("Key") or "?",
             "name": opts[0]["name"], "quantity": opts[0]["quantity"],
             "unit": opts[0]["unit"], "choose": choose,
             "options": opts, "quality_effects": effects}]


def normalize(raw: dict, idx: int = 0, version: str = "") -> Optional[dict]:
    """One raw blueprints.json record -> the dict Blueprint.from_dict reads."""
    key = raw.get("Key") or ""
    if not key:
        return None
    out = raw.get("Output") if isinstance(raw.get("Output"), dict) else {}
    tiers = raw.get("Tiers") or []
    tier = tiers[0] if tiers else {}
    avail = raw.get("Availability") or {}
    pools = [p.get("Key") for p in avail.get("RewardPools") or [] if p.get("Key")]
    default = bool(avail.get("Default"))
    ingredients = []
    for g in (tier.get("Requirements") or {}).get("Children") or []:
        if g.get("Kind") == "group":
            ingredients.extend(_slots(g))
    dis = raw.get("Dismantle") or {}
    return {
        "id": idx,
        "blueprint_id": key,
        "name": out.get("Name") or _name_from_key(key),
        "category": categorize(out, key),
        "craft_time_seconds": int(_num(tier.get("CraftTimeSeconds"))),
        "tiers": max(1, len(tiers)),
        "default_owned": 1 if default else 0,
        "obtainable": default or bool(pools),
        "sources": [pool_label(p) for p in pools],
        "output_class": out.get("Class") or "",
        "grade": out.get("Grade") or "",
        "dismantle_seconds": int(_num(dis.get("TimeSeconds"))),
        "dismantle_efficiency": _num(dis.get("Efficiency")),
        "version": version,
        "ingredients": ingredients,
        "missions": [],
    }


def build_index(raw_list: list, build: str = scunpacked.BUILD,
                commit: str = scunpacked.COMMIT, sha256: str = "") -> dict:
    version = version_string(build)
    bps = []
    for i, r in enumerate(raw_list or []):
        n = normalize(r, i, version) if isinstance(r, dict) else None
        if n:
            bps.append(n)
    bps.sort(key=lambda b: b["name"].lower())
    for i, b in enumerate(bps):
        b["id"] = i
    resources = sorted({o["name"] for b in bps for s in b["ingredients"] for o in s["options"]})
    categories = sorted({b["category"] for b in bps})
    return {"index_version": INDEX_VERSION, "build": build, "commit": commit,
            "source": scunpacked.REPO, "sha256": sha256,
            "stats": {"totalBlueprints": len(bps), "uniqueIngredients": len(resources),
                      "version": version},
            "hints": {"category": categories,
                      "resource": [{"name": r} for r in resources]},
            "blueprints": bps}


# ── cache access ─────────────────────────────────────────────────────────

class DatamineSource:
    """Pinned, cached blueprint data. ``cache_dir`` is for tests."""

    def __init__(self, cache_dir: Optional[str] = None, build: str = scunpacked.BUILD,
                 commit: str = scunpacked.COMMIT) -> None:
        self.build = build
        self.commit = commit
        self.dir = cache_dir or scunpacked.cache_dir(build)

    @property
    def raw_path(self) -> str:
        return os.path.join(self.dir, RAW_FILE)

    @property
    def index_path(self) -> str:
        return os.path.join(self.dir, INDEX_FILE)

    def label(self) -> str:
        return game_label(self.build)

    def has_raw(self) -> bool:
        return os.path.isfile(self.raw_path)

    def _read_index(self) -> Optional[dict]:
        try:
            with open(self.index_path, encoding="utf-8") as f:
                idx = json.load(f)
        except (OSError, ValueError):
            return None
        if idx.get("index_version") != INDEX_VERSION or idx.get("build") != self.build:
            return None
        return idx

    def status(self) -> str:
        """'ready' (index built), 'raw' (downloaded, not indexed) or 'missing'."""
        if self._read_index() is not None:
            return "ready"
        return "raw" if self.has_raw() else "missing"

    def load(self) -> dict:
        """The index; builds it from the cached raw file if needed. Never fetches."""
        idx = self._read_index()
        if idx is not None:
            return idx
        if not self.has_raw():
            raise DatamineMissing(f"crafting data for {self.build} is not downloaded yet")
        sha = scunpacked._sha256_file(self.raw_path)
        want = scunpacked.PINNED_SHA256.get(RAW_FILE)
        if want and self.build == scunpacked.BUILD and sha != want:
            raise scunpacked.ScunpackedError(
                f"{RAW_FILE} in the cache does not match the pinned sha256; download it again")
        with open(self.raw_path, encoding="utf-8") as f:
            raw = json.load(f)
        idx = build_index(raw, self.build, self.commit, sha)
        del raw
        try:
            scunpacked._atomic_write(self.index_path,
                                     json.dumps(idx, separators=(",", ":")).encode("utf-8"))
        except OSError:
            log.warning("could not write %s; will rebuild next time", self.index_path)
        log.info("Craft index built: %d blueprints (%s)", len(idx["blueprints"]), self.build)
        return idx

    def download(self, timeout: float = 120.0) -> dict:
        """Fetch blueprints.json (pinned commit + sha256), then build the index."""
        pinned = scunpacked.PINNED_SHA256 if self.build == scunpacked.BUILD else None
        scunpacked.fetch_raw(build=self.build, commit=self.commit, timeout=timeout,
                             files_wanted=scunpacked.CRAFT_FILES, dest=self.dir,
                             expect_sha256=pinned)
        return self.load()
