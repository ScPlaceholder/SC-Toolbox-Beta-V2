from __future__ import annotations
from dataclasses import dataclass, field, asdict


@dataclass
class WeaponStats:
    name: str = "?"
    local_name: str = ""
    ref: str = ""
    size: int = 1
    group: str = ""
    alpha: float = 0.0
    rps: float = 0.0
    dps_raw: float = 0.0
    dps_sus: float = 0.0
    ammo: int = 0
    dmg: dict = field(default_factory=dict)
    dom: str = "damagePhysical"
    # Extended fields populated by indexer
    class_: str = ""
    grade: str = "?"
    hp: float = 0.0
    power_draw: float = 0.0
    power_max: float = 0.0
    em_max: float = 0.0
    ir_max: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass
class ShieldStats:
    name: str = "?"
    local_name: str = ""
    ref: str = ""
    size: int = 1
    hp: float = 0.0
    regen: float = 0.0
    dmg_delay: float = 0.0
    down_delay: float = 0.0
    res_phys_min: float = 0.0
    res_phys_max: float = 0.0
    res_energy_min: float = 0.0
    res_energy_max: float = 0.0
    res_dist_min: float = 0.0
    res_dist_max: float = 0.0
    abs_phys_min: float = 0.0
    abs_phys_max: float = 0.0
    abs_energy_min: float = 0.0
    abs_energy_max: float = 0.0
    abs_dist_min: float = 0.0
    abs_dist_max: float = 0.0
    class_: str = ""
    grade: str = "?"
    power_draw: float = 0.0
    power_max: float = 0.0
    em_max: float = 0.0
    ir_max: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass
class CoolerStats:
    name: str = "?"
    local_name: str = ""
    ref: str = ""
    size: int = 1
    cooling_rate: float = 0.0
    suppression_heat: float = 0.0
    suppression_ir: float = 0.0
    class_: str = ""
    grade: str = "?"
    hp: float = 0.0
    power_draw: float = 0.0
    power_max: float = 0.0
    em_max: float = 0.0
    ir_max: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass
class RadarStats:
    name: str = "?"
    local_name: str = ""
    ref: str = ""
    size: int = 1
    detection_min: float = 0.0
    detection_max: float = 0.0
    cross_section: float = 0.0
    scan_speed: float = 0.0
    class_: str = ""
    grade: str = "?"
    hp: float = 0.0
    power_draw: float = 0.0
    power_max: float = 0.0
    em_max: float = 0.0
    ir_max: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass
class MissileStats:
    name: str = "?"
    local_name: str = ""
    ref: str = ""
    size: int = 1
    total_dmg: float = 0.0
    dmg_phys: float = 0.0
    dmg_energy: float = 0.0
    dmg_dist: float = 0.0
    tracking: str = "?"
    lock_range: float = 0.0
    lock_time: float = 0.0
    speed: float = 0.0
    lifetime: float = 0.0
    lock_angle: float = 0.0
    class_: str = ""
    grade: str = "?"
    hp: float = 0.0
    power_draw: float = 0.0
    power_max: float = 0.0
    em_max: float = 0.0
    ir_max: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass
class PowerPlantStats:
    name: str = "?"
    local_name: str = ""
    ref: str = ""
    size: int = 1
    class_: str = ""
    grade: str = "?"
    output: float = 0.0
    power_draw: float = 0.0
    power_max: float = 0.0
    overclocked: float = 0.0
    em_idle: float = 0.0
    em_max: float = 0.0
    ir_max: float = 0.0
    hp: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass
class QDriveStats:
    name: str = "?"
    local_name: str = ""
    ref: str = ""
    size: int = 1
    class_: str = ""
    grade: str = "?"
    speed: float = 0.0
    spool: float = 0.0
    cooldown: float = 0.0
    fuel_rate: float = 0.0
    jump_range: float = 0.0
    efficiency: float = 0.0
    power_draw: float = 0.0
    power_max: float = 0.0
    em_idle: float = 0.0
    em_max: float = 0.0
    ir_max: float = 0.0
    hp: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass
class ThrusterStats:
    name: str = "?"
    size: int = 1
    category: str = ""
    mfr: str = ""
    thrust: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PowerPlantStatsFY:
    """Fleetyards power plant stats."""
    name: str = "?"
    size: int = 1
    grade: str = "?"
    class_: str = ""
    mfr: str = ""
    power_output: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("class_")
        return d


@dataclass
class QDriveStatsFY:
    """Fleetyards quantum drive stats."""
    name: str = "?"
    size: int = 1
    grade: str = "?"
    mfr: str = ""
    speed: float = 0.0
    spool: float = 0.0
    cooldown: float = 0.0
    fuel_rate: float = 0.0
    jump_range: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PowerSlot:
    id: str = ""
    name: str = ""
    category: str = ""
    max_segments: int = 0
    default_seg: int = 0
    current_seg: int = 0
    enabled: bool = True
    draw_per_seg: float = 0.0
    em_per_seg: float = 0.0
    ir_per_seg: float = 0.0
    em_total: float = 0.0
    ir_total: float = 0.0
    cooling_gen: float = 0.0
    power_ranges: list = field(default_factory=list)
    is_generator: bool = False
    output: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)
