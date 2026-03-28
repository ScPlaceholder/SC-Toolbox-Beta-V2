"""Pure domain models for the Craft Database skill.

No Qt imports — these are plain dataclasses suitable for caching and
serialisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Ingredient / Slot models ────────────────────────────────────────────────


@dataclass
class IngredientOption:
    guid: str
    name: str
    quantity_scu: float
    min_quality: int = 0
    unit: str = "scu"
    loc_key: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> IngredientOption:
        return cls(
            guid=d.get("guid", ""),
            name=d.get("name", ""),
            quantity_scu=float(d.get("quantity_scu", 0)),
            min_quality=int(d.get("min_quality", 0)),
            unit=d.get("unit", "scu"),
            loc_key=d.get("loc_key", ""),
        )


@dataclass
class QualityEffect:
    stat: str
    quality_min: int = 0
    quality_max: int = 1000
    modifier_at_min: float = 1.0
    modifier_at_max: float = 1.0

    @classmethod
    def from_dict(cls, d: dict) -> QualityEffect:
        return cls(
            stat=d.get("stat", ""),
            quality_min=int(d.get("quality_min", 0)),
            quality_max=int(d.get("quality_max", 1000)),
            modifier_at_min=float(d.get("modifier_at_min", 1.0)),
            modifier_at_max=float(d.get("modifier_at_max", 1.0)),
        )

    def modifier_at(self, quality: int) -> float:
        if self.quality_max == self.quality_min:
            return self.modifier_at_max
        t = (quality - self.quality_min) / (self.quality_max - self.quality_min)
        t = max(0.0, min(1.0, t))
        return self.modifier_at_min + t * (self.modifier_at_max - self.modifier_at_min)

    def pct_at(self, quality: int) -> float:
        mod = self.modifier_at(quality)
        return (mod - 1.0) * 100.0


@dataclass
class IngredientSlot:
    slot: str
    options: list[IngredientOption] = field(default_factory=list)
    quality_effects: list[QualityEffect] = field(default_factory=list)
    name: str = ""
    quantity_scu: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> IngredientSlot:
        return cls(
            slot=d.get("slot", ""),
            options=[IngredientOption.from_dict(o) for o in d.get("options", [])],
            quality_effects=[QualityEffect.from_dict(q) for q in d.get("quality_effects", [])],
            name=d.get("name", ""),
            quantity_scu=float(d.get("quantity_scu", 0)),
        )


# ── Mission models ──────────────────────────────────────────────────────────


@dataclass
class MissionDifficulty:
    mechanical_skill: str = ""
    time_commitment: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> MissionDifficulty:
        return cls(
            mechanical_skill=d.get("mechanicalSkill", ""),
            time_commitment=d.get("timeCommitment", ""),
        )


@dataclass
class Mission:
    name: str
    contractor: str = ""
    mission_type: str = ""
    category: str = ""
    lawful: int = 1
    not_for_release: int = 0
    drop_chance: float = 1.0
    locations: str = ""
    description: str = ""
    time_to_complete_minutes: int = 0
    difficulty: MissionDifficulty = field(default_factory=MissionDifficulty)

    @classmethod
    def from_dict(cls, d: dict) -> Mission:
        diff = d.get("difficulty")
        return cls(
            name=d.get("name", ""),
            contractor=d.get("contractor", ""),
            mission_type=d.get("mission_type", ""),
            category=d.get("category", ""),
            lawful=int(d.get("lawful", 1)),
            not_for_release=int(d.get("not_for_release", 0)),
            drop_chance=float(d.get("drop_chance", 1.0)),
            locations=d.get("locations", ""),
            description=d.get("description", ""),
            time_to_complete_minutes=int(d.get("time_to_complete_minutes", 0)),
            difficulty=MissionDifficulty.from_dict(diff) if isinstance(diff, dict) else MissionDifficulty(),
        )

    @property
    def drop_pct(self) -> str:
        return f"{self.drop_chance * 100:.0f}%"


# ── Item stats ───────────────────────────────────────────────────────────────


@dataclass
class SpreadInfo:
    min_val: float = 0
    max_val: float = 0
    first_attack: float = 0
    attack: float = 0
    decay: float = 0

    @classmethod
    def from_dict(cls, d: dict) -> SpreadInfo:
        return cls(
            min_val=float(d.get("min", 0)),
            max_val=float(d.get("max", 0)),
            first_attack=float(d.get("first_attack", 0)),
            attack=float(d.get("attack", 0)),
            decay=float(d.get("decay", 0)),
        )


@dataclass
class FireMode:
    name: str = ""
    fire_rate: float = 0
    heat_per_shot: float = 0
    wear_per_shot: float = 0
    ammo_cost: int = 1
    pellet_count: int = 1
    damage_multiplier: float = 1.0
    spread: SpreadInfo = field(default_factory=SpreadInfo)

    @classmethod
    def from_dict(cls, d: dict) -> FireMode:
        sp = d.get("spread")
        return cls(
            name=d.get("name", ""),
            fire_rate=float(d.get("fire_rate", 0)),
            heat_per_shot=float(d.get("heat_per_shot", 0)),
            wear_per_shot=float(d.get("wear_per_shot", 0)),
            ammo_cost=int(d.get("ammo_cost", 1)),
            pellet_count=int(d.get("pellet_count", 1)),
            damage_multiplier=float(d.get("damage_multiplier", 1.0)),
            spread=SpreadInfo.from_dict(sp) if isinstance(sp, dict) else SpreadInfo(),
        )


@dataclass
class DamageResistance:
    physical: float = 0
    energy: float = 0
    distortion: float = 0
    thermal: float = 0
    biochemical: float = 0
    stun: float = 0
    impact_force: float = 0
    profile: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> DamageResistance:
        return cls(
            physical=float(d.get("physical", 0)),
            energy=float(d.get("energy", 0)),
            distortion=float(d.get("distortion", 0)),
            thermal=float(d.get("thermal", 0)),
            biochemical=float(d.get("biochemical", 0)),
            stun=float(d.get("stun", 0)),
            impact_force=float(d.get("impact_force", 0)),
            profile=d.get("profile", ""),
        )


@dataclass
class TemperatureResistance:
    min_temp: float = 0
    max_temp: float = 0

    @classmethod
    def from_dict(cls, d: dict) -> TemperatureResistance:
        return cls(
            min_temp=float(d.get("min", 0)),
            max_temp=float(d.get("max", 0)),
        )


@dataclass
class ItemStats:
    type: str = ""
    fire_modes: list[FireMode] = field(default_factory=list)
    damage_resistance: DamageResistance | None = None
    temperature_resistance: TemperatureResistance | None = None
    mass_kg: float = 0
    overheat_temperature: float = 0

    @classmethod
    def from_dict(cls, d: dict) -> ItemStats:
        dr = d.get("damage_resistance")
        tr = d.get("temperature_resistance")
        return cls(
            type=d.get("type", ""),
            fire_modes=[FireMode.from_dict(f) for f in d.get("fire_modes", [])],
            damage_resistance=DamageResistance.from_dict(dr) if isinstance(dr, dict) else None,
            temperature_resistance=TemperatureResistance.from_dict(tr) if isinstance(tr, dict) else None,
            mass_kg=float(d.get("mass_kg", 0)),
            overheat_temperature=float(d.get("overheat_temperature", 0)),
        )


# ── Blueprint (top-level) ───────────────────────────────────────────────────


@dataclass
class Blueprint:
    id: int
    blueprint_id: str
    name: str
    category: str = ""
    craft_time_seconds: int = 0
    tiers: int = 1
    default_owned: int = 0
    item_stats: ItemStats = field(default_factory=ItemStats)
    version: str = ""
    ingredients: list[IngredientSlot] = field(default_factory=list)
    missions: list[Mission] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> Blueprint:
        stats = d.get("item_stats")
        return cls(
            id=int(d.get("id", 0)),
            blueprint_id=d.get("blueprint_id", ""),
            name=d.get("name", ""),
            category=d.get("category", ""),
            craft_time_seconds=int(d.get("craft_time_seconds", 0)),
            tiers=int(d.get("tiers", 1)),
            default_owned=int(d.get("default_owned", 0)),
            item_stats=ItemStats.from_dict(stats) if isinstance(stats, dict) else ItemStats(),
            version=d.get("version", ""),
            ingredients=[IngredientSlot.from_dict(i) for i in d.get("ingredients", [])],
            missions=[Mission.from_dict(m) for m in d.get("missions", [])],
        )

    @property
    def craft_time_display(self) -> str:
        s = self.craft_time_seconds
        if s < 60:
            return f"{s}s"
        m, sec = divmod(s, 60)
        h, m = divmod(m, 60)
        parts: list[str] = []
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        if sec:
            parts.append(f"{sec}s")
        return " ".join(parts)

    @property
    def category_type(self) -> str:
        return self.category.split(" / ")[0] if self.category else ""

    @property
    def category_subtype(self) -> str:
        parts = self.category.split(" / ")
        return " / ".join(parts[1:]) if len(parts) > 1 else ""

    @property
    def ingredient_names(self) -> list[str]:
        return [slot.name for slot in self.ingredients if slot.name]

    @property
    def mission_count(self) -> int:
        return len(self.missions)


# ── Filter hints ─────────────────────────────────────────────────────────────


@dataclass
class FilterHints:
    locations: list[str] = field(default_factory=list)
    mission_types: list[str] = field(default_factory=list)
    contractors: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> FilterHints:
        return cls(
            locations=d.get("location", []),
            mission_types=d.get("mission_type", []),
            contractors=d.get("contractor", []),
            resources=d.get("resource", []),
            categories=d.get("category", []),
        )


# ── Stats ────────────────────────────────────────────────────────────────────


@dataclass
class CraftStats:
    total_blueprints: int = 0
    unique_ingredients: int = 0
    version: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> CraftStats:
        return cls(
            total_blueprints=int(d.get("totalBlueprints", 0)),
            unique_ingredients=int(d.get("uniqueIngredients", 0)),
            version=d.get("version", ""),
        )


# ── Pagination ───────────────────────────────────────────────────────────────


@dataclass
class Pagination:
    page: int = 1
    limit: int = 50
    total: int = 0
    pages: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> Pagination:
        return cls(
            page=int(d.get("page", 1)),
            limit=int(d.get("limit", 50)),
            total=int(d.get("total", 0)),
            pages=int(d.get("pages", 1)),
        )
