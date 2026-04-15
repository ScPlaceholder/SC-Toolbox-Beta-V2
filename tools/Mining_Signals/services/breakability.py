"""Star Citizen mining breakability calculator.

Pure-Python port of Mort13's BreakabilityChart calculations
(https://mort13.github.io/BreakabilityChart/, MIT-style fan tool).
All formulas come from that project's ``js/calculations.js``; see the
original Google Drive write-up for the empirical research behind them.

Core relation
-------------
    mass = power * (1 - effective_resistance) / C_MASS
    required_power = (mass * C_MASS) / (1 - effective_resistance)

where ``effective_resistance`` is the rock's base resistance (0..1)
scaled by a resistance-modifier factor that stacks multiplicatively
across laserhead, modules, and gadget.

This module is **data-agnostic**: it takes plain dicts/dataclasses for
laser configuration and knows nothing about UEX or any specific
data source. A future data layer can feed it from UEX or a bundled
JSON without touching the math here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

# ── Constants ──
# Mass coefficient: 0.2 (= 1/5) matches Regolith, PeaceFrog Rock Breaker,
# and the Mining Fracture Analyser — the community-standard formula.
# Mort13's original research used 0.175 but that undershoots required
# power by ~14%, giving false "can break" verdicts on borderline rocks.
C_MASS = 0.2    # Mass coefficient (community standard: ÷5)
C_R = 1.0       # Resistance coefficient


# ─────────────────────────────────────────────────────────────
# Core math
# ─────────────────────────────────────────────────────────────

def effective_resistance(resistance_pct: float, resistance_modifier: float) -> float:
    """Return effective resistance as a 0..1 fraction.

    Parameters
    ----------
    resistance_pct
        Rock's base resistance as displayed in-game (0-100).
    resistance_modifier
        Multiplier already computed by :func:`combine_resistance_modifiers`
        — e.g. 0.75 means total -25%, 1.25 means +25%.
    """
    r = (resistance_pct / 100.0) * resistance_modifier * C_R
    return max(0.0, min(1.0, r))


def mass_at_resistance(
    power: float,
    resistance_pct: float,
    resistance_modifier: float = 1.0,
) -> float:
    """Max rock mass this laser power can crack at the given resistance."""
    eff = effective_resistance(resistance_pct, resistance_modifier)
    return (power * (1.0 - eff)) / C_MASS


def required_power(
    mass: float,
    resistance_pct: float = 0.0,
    resistance_modifier: float = 1.0,
) -> float:
    """Raw laser power required to break a rock of given mass/resistance.

    Returns ``float('inf')`` if the rock is unbreakable (effective
    resistance >= 100%).
    """
    eff = effective_resistance(resistance_pct, resistance_modifier)
    denom = 1.0 - eff
    if denom <= 0.0:
        return float("inf")
    return (mass * C_MASS) / denom


# ─────────────────────────────────────────────────────────────
# Configuration objects
# ─────────────────────────────────────────────────────────────

@dataclass
class GadgetInfo:
    """Lightweight gadget descriptor for breakability calculations."""
    name: str
    resistance: float  # percentage modifier (negative = reduces rock resistance)


@dataclass
class LaserConfig:
    """One configured laserhead.

    Values are the **final** numbers after modules/gadgets have already
    been folded in by the data layer. This keeps the math pure — no
    module attribute lookups live in here.

    Passive stats (``max_power``, ``resistance_modifier``) are the
    always-on baseline. Active stats are what you get when active
    modules are engaged (temporary boost, limited uses).
    """
    name: str
    max_power: float                        # passive-only max power
    min_power: float = 0.0
    resistance_modifier: float = 1.0        # passive-only resistance multiplier
    visible: bool = True
    # Active module boosted stats
    max_power_active: float = 0.0
    resistance_modifier_active: float = 1.0
    active_module_uses: int = 0
    # Ship metadata (populated in fleet mode)
    ship_id: str = ""                       # unique id per fleet ship (source path)
    ship_display: str = ""                  # "mining_mole (MOLE)" display name
    ship_type: str = ""                     # "MOLE" | "Prospector" | "Golem"
    player_count: int = 1                   # crew needed (MOLE=3, others=1)
    turret_index: int = -1                  # index within the ship (for tracking)
    # Team-scope metadata (populated in team mode for substitute reporting)
    team_name: str = ""                     # owning team name
    cluster: str = ""                       # owning cluster letter/name
    player_names: list[str] = field(default_factory=list)  # crew assigned to the ship
    # Consumable tracking (managed by UI, not UEX)
    active_uses_remaining: int = 0          # remaining module activations (decremented per rock)
    active_module_names: str = ""           # comma-separated active module names for display


@dataclass
class ChargeProfile:
    """Charge-decay simulation result.

    Models the dynamic charge mechanics reverse-engineered from
    SCMDB's Mining Solver. The two key constants were derived from
    4 SCMDB screenshots at mass 5789 across different rock
    compositions — decay rate and rock capacity are mass-only
    functions, independent of composition:

        decay_rate  = mass × 0.02   (energy units per second)
        capacity    = mass × 10     (total energy units)

    The charge bar fills at ``net_energy = input - decay`` per second.
    When it enters the optimal window, crack progress accumulates.
    """
    min_throttle_pct: float      # minimum throttle % to overcome decay
    time_to_window_sec: float    # seconds to reach optimal window at 100%
    time_in_window_sec: float    # seconds of green-zone dwell to crack
    est_total_time_sec: float    # total estimated seconds to fracture
    decay_rate: float            # mass × DECAY_COEFF
    net_energy_max: float        # max input rate - decay rate


# Charge-simulation constants (from SCMDB reverse-engineering)
DECAY_COEFF = 0.02      # decay_rate = mass × 0.02
CAPACITY_COEFF = 10.0   # rock_capacity = mass × 10
DEFAULT_WINDOW_CENTER = 0.55   # assume optimal window centered at 55% of capacity
DEFAULT_WINDOW_SIZE = 0.055    # ~5.5% of capacity (SCMDB default)
DEFAULT_TIME_IN_WINDOW = 20.0  # seconds of dwell needed (observed SCMDB average)


@dataclass
class BreakResult:
    """Outcome of a breakability check against a multi-laser setup."""
    percentage: float                # % of total laser power required
    used_lasers: list[str]           # names of lasers in the chosen subset
    insufficient: bool = False       # True if setup cannot break the rock
    unbreakable: bool = False        # True if effective resistance >= 100%
    missing_power: float = 0.0       # MW short (only if insufficient)
    active_modules_needed: int = 0   # 0 = passive only, 1+ = need activations
    turrets_activated: list[str] = field(default_factory=list)  # turret names that needed activation
    gadget_used: str | None = None   # name of gadget recommended (only if needed)
    charge_profile: ChargeProfile | None = None  # charge-decay timing (if computable)


# ─────────────────────────────────────────────────────────────
# Modifier stacking helpers
# ─────────────────────────────────────────────────────────────

def combine_resistance_modifiers(*percent_modifiers: float) -> float:
    """Combine per-item resistance % modifiers into a single multiplier.

    Each argument is a percentage modifier (e.g. -25 for -25%).
    Result is the multiplicative factor — e.g. combining -25 and -10
    yields 0.75 * 0.90 = 0.675.
    """
    factor = 1.0
    for pct in percent_modifiers:
        factor *= 1.0 + (pct / 100.0)
    return factor


def combine_power(*power_factors_pct: float) -> float:
    """Multiplicative stacking of mining-laser-power module factors.

    Upstream uses ``base_power * (mod_value / 100)`` per module (not
    ``1 + mod/100``). Each ``power_factors_pct`` is the raw module
    value (e.g. 110 for a +10% power module).
    """
    factor = 1.0
    for p in power_factors_pct:
        factor *= p / 100.0
    return factor


# ─────────────────────────────────────────────────────────────
# Multi-laser breakability (subset search)
# ─────────────────────────────────────────────────────────────

def _greedy_power_percentage(
    mass: float,
    resistance_pct: float,
    lasers: list[LaserConfig],
) -> BreakResult:
    """Greedy heuristic for large fleets (n>12 turrets).

    Adds turrets one at a time, sorted by max_power descending,
    until the combined setup can break the rock. Not optimal (might
    miss a better resistance-modifier combo) but runs in O(n²) instead
    of O(2^n).
    """
    # Sort by power (strongest first)
    indices = sorted(range(len(lasers)), key=lambda i: -lasers[i].max_power)
    used: list[int] = []

    for idx in indices:
        used.append(idx)
        combined_max = sum(lasers[i].max_power for i in used)
        combined_rmod = 1.0
        for i in used:
            combined_rmod *= lasers[i].resistance_modifier

        needed = required_power(mass, resistance_pct, combined_rmod)
        if needed == float("inf"):
            continue

        pct = needed / combined_max if combined_max > 0 else float("inf")
        if pct <= 1.0:
            return BreakResult(
                percentage=pct * 100.0,
                used_lasers=[lasers[i].name for i in used],
                insufficient=False,
            )

    # All turrets together still can't break
    total = sum(lasers[i].max_power for i in indices)
    combined_rmod = 1.0
    for i in indices:
        combined_rmod *= lasers[i].resistance_modifier
    needed = required_power(mass, resistance_pct, combined_rmod)
    missing = needed - total if needed != float("inf") else 0.0

    return BreakResult(
        percentage=100.0,
        used_lasers=[lasers[i].name for i in indices],
        insufficient=True,
        unbreakable=(needed == float("inf")),
        missing_power=max(0.0, missing),
    )


def power_percentage(
    mass: float,
    resistance_pct: float,
    lasers: list[LaserConfig],
) -> BreakResult:
    """Find the minimal subset of lasers that can break the rock.

    Mirrors ``calculatePowerPercentage`` in the upstream JS: enumerate
    all non-empty laser subsets, sorted smallest-first, return the
    first one whose combined max power is sufficient.
    """
    if not lasers:
        return BreakResult(percentage=0.0, used_lasers=[], insufficient=True)

    n = len(lasers)

    # For large fleets (n>12), use greedy heuristic instead of
    # exhaustive 2^n subset enumeration to avoid combinatorial explosion.
    if n > 12:
        return _greedy_power_percentage(mass, resistance_pct, lasers)

    # Build all non-empty subsets, sorted by size (prefer fewer lasers)
    subsets: list[list[int]] = []
    for mask in range(1, 1 << n):
        subset = [i for i in range(n) if mask & (1 << i)]
        subsets.append(subset)
    subsets.sort(key=len)

    best_insufficient: Optional[BreakResult] = None
    best_actual_pct = float("inf")

    for subset in subsets:
        combined_max = 0.0
        combined_rmod = 1.0
        for i in subset:
            combined_max += lasers[i].max_power
            combined_rmod *= lasers[i].resistance_modifier

        needed = required_power(mass, resistance_pct, combined_rmod)
        if needed == float("inf"):
            continue  # unbreakable with this subset

        pct = needed / combined_max if combined_max > 0 else float("inf")
        if pct <= 1.0:
            return BreakResult(
                percentage=pct * 100.0,
                used_lasers=[lasers[i].name for i in subset],
                insufficient=False,
            )

        if pct < best_actual_pct:
            best_actual_pct = pct
            best_insufficient = BreakResult(
                percentage=100.0,
                used_lasers=[lasers[i].name for i in subset],
                insufficient=True,
                missing_power=needed - combined_max,
            )

    if best_insufficient is not None:
        return best_insufficient

    # Every subset hit 100%+ effective resistance
    return BreakResult(
        percentage=float("inf"),
        used_lasers=[l.name for l in lasers],
        insufficient=True,
        unbreakable=True,
    )


# ─────────────────────────────────────────────────────────────
# Curve generation (for future chart tab)
# ─────────────────────────────────────────────────────────────

def breakability_curve(
    power: float,
    resistance_modifier: float = 1.0,
    step: float = 0.5,
) -> list[tuple[float, float]]:
    """Sample the mass-vs-resistance curve for a single laser.

    Returns a list of ``(resistance%, max_mass)`` points from 0 to 100%.
    """
    pts: list[tuple[float, float]] = []
    r = 0.0
    while r <= 100.0 + 1e-9:
        pts.append((r, mass_at_resistance(power, r, resistance_modifier)))
        r += step
    return pts


def combined_curve(
    lasers: Iterable[LaserConfig],
    step: float = 0.5,
) -> list[tuple[float, float]]:
    """Combined curve for a visible laser set (powers summed, mods multiplied)."""
    visible = [l for l in lasers if l.visible]
    if not visible:
        return []

    total_power = sum(l.max_power for l in visible)
    combined_rmod = 1.0
    for l in visible:
        combined_rmod *= l.resistance_modifier

    return breakability_curve(total_power, combined_rmod, step)


# ─────────────────────────────────────────────────────────────
# Active modules + gadgets composite calculation
# ─────────────────────────────────────────────────────────────

def compute_with_active_modules(
    mass: float,
    resistance_pct: float,
    lasers: list[LaserConfig],
) -> BreakResult:
    """Try passive first, then with active modules if passive fails.

    Returns a BreakResult with ``active_modules_needed`` set to 0
    (passive sufficed) or 1 (one activation cycle needed). Respects
    each turret's ``active_module_uses`` — won't recommend activation
    if the module has 0 uses.
    """
    # 1. Try passive-only
    result = power_percentage(mass, resistance_pct, lasers)
    if not result.insufficient:
        result.active_modules_needed = 0
        return result

    # 2. Check if any turret has active modules WITH remaining uses
    has_actives = any(
        l.active_uses_remaining > 0 and l.max_power_active > 0
        for l in lasers
    )
    if not has_actives:
        return result  # no actives available or all depleted

    # 3. Build boosted laser configs (swap in active stats for turrets with uses left)
    boosted = []
    activated_names: list[str] = []
    for l in lasers:
        if l.active_uses_remaining > 0 and l.max_power_active > 0:
            boosted.append(LaserConfig(
                name=l.name,
                max_power=l.max_power_active,
                min_power=l.min_power,
                resistance_modifier=l.resistance_modifier_active,
                visible=l.visible,
                max_power_active=l.max_power_active,
                resistance_modifier_active=l.resistance_modifier_active,
                active_module_uses=l.active_module_uses,
                active_uses_remaining=l.active_uses_remaining,
                ship_id=l.ship_id,
                ship_display=l.ship_display,
                ship_type=l.ship_type,
                player_count=l.player_count,
                turret_index=l.turret_index,
            ))
            activated_names.append(l.name)
        else:
            boosted.append(l)

    active_result = power_percentage(mass, resistance_pct, boosted)
    if not active_result.insufficient:
        active_result.active_modules_needed = 1
        # Track which turrets actually needed activation (those in the used subset)
        active_result.turrets_activated = [
            n for n in activated_names if n in active_result.used_lasers
        ]
        return active_result

    # Active modules help but still not enough — return the active result
    # so the caller sees the reduced deficit.
    return active_result


def compute_charge_profile(
    mass: float,
    resistance_pct: float,
    combined_max_power: float,
    combined_resistance_modifier: float,
    window_size_modifier: float = 1.0,
    charge_rate_modifier: float = 1.0,
) -> ChargeProfile | None:
    """Estimate charge timing for a given rock + laser combo.

    Parameters
    ----------
    mass : float
        Rock mass in kg.
    resistance_pct : float
        Rock base resistance (0-100%).
    combined_max_power : float
        Sum of max_power of all lasers in the chosen subset.
    combined_resistance_modifier : float
        Product of all resistance modifiers in the chosen subset.
    window_size_modifier : float
        Multiplicative modifier to optimal window size (default 1.0).
    charge_rate_modifier : float
        Multiplicative modifier to optimal charge rate (default 1.0).

    Returns
    -------
    ChargeProfile or None if the setup can't overcome decay.
    """
    if mass <= 0 or combined_max_power <= 0:
        return None

    decay_rate = mass * DECAY_COEFF
    capacity = mass * CAPACITY_COEFF

    eff_res = effective_resistance(resistance_pct, combined_resistance_modifier)
    max_input = combined_max_power * (1.0 - eff_res)

    if max_input <= 0:
        return None

    min_throttle = decay_rate / max_input  # as fraction
    net_energy = max_input - decay_rate

    if net_energy <= 0:
        # Laser can't overcome decay even at 100% throttle
        return ChargeProfile(
            min_throttle_pct=min(min_throttle * 100.0, 999.0),
            time_to_window_sec=float("inf"),
            time_in_window_sec=float("inf"),
            est_total_time_sec=float("inf"),
            decay_rate=decay_rate,
            net_energy_max=net_energy,
        )

    # Optimal window position shifts downward for higher-resistance
    # rocks. Observed from SCMDB: Aluminum(res=0.02)→59%,
    # Agricium(res=0.69)→50%, Beryl(res=0.84)→30%. Linear fit:
    window_center = max(0.10, 0.60 - 0.35 * eff_res)
    window_size = DEFAULT_WINDOW_SIZE * window_size_modifier
    window_start_frac = max(0.0, window_center - window_size / 2)
    window_start_energy = capacity * window_start_frac

    time_to_window = window_start_energy / net_energy

    # Time in window: base dwell scaled inversely by charge_rate_modifier
    # Higher charge rate = faster crack progress = less dwell needed
    time_in_window = DEFAULT_TIME_IN_WINDOW / charge_rate_modifier

    est_total = time_to_window + time_in_window

    return ChargeProfile(
        min_throttle_pct=min_throttle * 100.0,
        time_to_window_sec=time_to_window,
        time_in_window_sec=time_in_window,
        est_total_time_sec=est_total,
        decay_rate=decay_rate,
        net_energy_max=net_energy,
    )


def _attach_charge_profile(
    result: BreakResult,
    mass: float,
    resistance_pct: float,
    lasers: list[LaserConfig],
) -> None:
    """Compute and attach a ChargeProfile to an existing BreakResult.

    Uses the same laser subset that ``power_percentage`` chose
    (identified by ``result.used_lasers``), combining their powers
    and resistance modifiers to run the charge simulation.
    """
    if result.unbreakable or not result.used_lasers:
        return

    # Reconstruct the combined stats for the chosen subset
    used_set = set(result.used_lasers)
    combined_power = 0.0
    combined_rmod = 1.0
    for lc in lasers:
        if lc.name in used_set:
            # Use active stats if active modules were needed
            if result.active_modules_needed and lc.active_module_uses > 0:
                combined_power += lc.max_power_active or lc.max_power
                combined_rmod *= lc.resistance_modifier_active
            else:
                combined_power += lc.max_power
                combined_rmod *= lc.resistance_modifier

    if combined_power <= 0:
        return

    result.charge_profile = compute_charge_profile(
        mass, resistance_pct, combined_power, combined_rmod,
    )


def compute_with_gadgets(
    mass: float,
    resistance_pct: float,
    lasers: list[LaserConfig],
    available_gadgets: list[GadgetInfo],
    always_use_best: bool = False,
) -> BreakResult:
    """Full breakability calculation: passive → active → gadgets.

    If ``always_use_best`` is True, applies the strongest available
    gadget unconditionally. Otherwise gadgets are only tried when
    passive + active modules can't break the rock.

    The gadget's resistance modifier is applied to every turret's
    resistance modifier (matching in-game behavior where one gadget
    affects all lasers on a rock).
    """
    # Sort gadgets by most negative resistance (most helpful first)
    sorted_gadgets = sorted(
        [g for g in available_gadgets if g.resistance is not None],
        key=lambda g: g.resistance,
    )

    if always_use_best and sorted_gadgets:
        # Apply the strongest gadget unconditionally
        gadget = sorted_gadgets[0]
        gadget_factor = 1.0 + gadget.resistance / 100.0
        boosted = [
            LaserConfig(
                name=l.name,
                max_power=l.max_power,
                min_power=l.min_power,
                resistance_modifier=l.resistance_modifier * gadget_factor,
                visible=l.visible,
                max_power_active=l.max_power_active,
                resistance_modifier_active=l.resistance_modifier_active * gadget_factor,
                active_module_uses=l.active_module_uses,
            )
            for l in lasers
        ]
        result = compute_with_active_modules(mass, resistance_pct, boosted)
        result.gadget_used = gadget.name
        _attach_charge_profile(result, mass, resistance_pct, boosted)
        return result

    # Try without gadgets first
    result = compute_with_active_modules(mass, resistance_pct, lasers)
    if not result.insufficient:
        _attach_charge_profile(result, mass, resistance_pct, lasers)
        return result

    # Try each gadget until one works
    for gadget in sorted_gadgets:
        gadget_factor = 1.0 + gadget.resistance / 100.0
        boosted = [
            LaserConfig(
                name=l.name,
                max_power=l.max_power,
                min_power=l.min_power,
                resistance_modifier=l.resistance_modifier * gadget_factor,
                visible=l.visible,
                max_power_active=l.max_power_active,
                resistance_modifier_active=l.resistance_modifier_active * gadget_factor,
                active_module_uses=l.active_module_uses,
            )
            for l in lasers
        ]
        gadget_result = compute_with_active_modules(mass, resistance_pct, boosted)
        if not gadget_result.insufficient:
            gadget_result.gadget_used = gadget.name
            _attach_charge_profile(gadget_result, mass, resistance_pct, boosted)
            return gadget_result

    # Nothing works — return the best result (no gadget, with actives)
    _attach_charge_profile(result, mass, resistance_pct, lasers)
    return result


# ─────────────────────────────────────────────────────────────
# Fleet-aware breakability with substitution + stability scoring
# ─────────────────────────────────────────────────────────────

# Default player counts per ship type
_DEFAULT_CREW: dict[str, int] = {
    "Prospector": 1,
    "Golem": 1,
    "MOLE": 3,
}


def default_player_count(ship_type: str) -> int:
    """Return the default crew count for a ship type."""
    return _DEFAULT_CREW.get(ship_type, 1)


@dataclass
class FleetBreakResult:
    """Result of fleet-aware breakability with substitution analysis."""
    user_can_solo: bool                       # True if fleet ship #1 can break alone
    solo_result: BreakResult | None = None    # user's ship attempt (may be insufficient)
    # "Least Players" tab
    least_players: BreakResult | None = None
    least_players_count: int = 0              # total crew needed
    least_players_ships: list[str] = field(default_factory=list)
    least_players_stability: float = 0.0
    # "Least Ships" tab
    least_ships: BreakResult | None = None
    least_ships_count: int = 0                # number of ships
    least_ships_names: list[str] = field(default_factory=list)
    least_ships_stability: float = 0.0


def stability_score(
    combined_max_power: float,
    needed_power: float,
    combined_rmod: float,
) -> float:
    """Score a laser subset by stability (higher = more stable).

    Combines power headroom (how much buffer over 100%) with a penalty
    for high resistance modifier compounding (which makes the break
    fragile against resistance fluctuations).
    """
    if needed_power <= 0 or needed_power == float("inf"):
        return 0.0
    headroom = combined_max_power / needed_power  # >1 = has buffer
    compounding = abs(combined_rmod - 1.0)
    return headroom - 0.5 * compounding * compounding


def _subset_stats(
    subset: list[int],
    lasers: list[LaserConfig],
    mass: float,
    resistance_pct: float,
) -> tuple[float, float, float, float]:
    """Compute (combined_power, combined_rmod, needed_power, pct) for a subset."""
    combined_max = sum(lasers[i].max_power for i in subset)
    combined_rmod = 1.0
    for i in subset:
        combined_rmod *= lasers[i].resistance_modifier

    needed = required_power(mass, resistance_pct, combined_rmod)
    pct = (needed / combined_max * 100.0) if combined_max > 0 and needed != float("inf") else float("inf")
    return combined_max, combined_rmod, needed, pct


def _subset_ship_info(
    subset: list[int],
    lasers: list[LaserConfig],
) -> tuple[int, int, list[str]]:
    """Return (player_count, ship_count, ship_display_names) for a subset."""
    ships_seen: dict[str, tuple[str, int]] = {}  # ship_id -> (display, players)
    for i in subset:
        sid = lasers[i].ship_id
        if sid and sid not in ships_seen:
            ships_seen[sid] = (lasers[i].ship_display, lasers[i].player_count)

    total_players = sum(p for _, p in ships_seen.values())
    ship_names = [d for d, _ in ships_seen.values()]
    return total_players, len(ships_seen), ship_names


def fleet_breakability(
    mass: float,
    resistance_pct: float,
    lasers: list[LaserConfig],
    user_ship_id: str,
    available_gadgets: list[GadgetInfo] | None = None,
    always_use_best_gadget: bool = False,
) -> FleetBreakResult:
    """Fleet-aware breakability: try user's ship first, then find substitutes.

    Returns a ``FleetBreakResult`` with:
    - ``user_can_solo``: whether ship #1 can break alone
    - ``least_players``: best substitute combo minimizing total crew
    - ``least_ships``: best substitute combo minimizing ship count

    Both substitute results are scored by stability (power headroom +
    low resistance compounding), not just raw power.
    """
    gadgets = available_gadgets or []

    # Separate user's ship turrets from the rest
    user_turrets = [i for i, l in enumerate(lasers) if l.ship_id == user_ship_id]
    user_configs = [lasers[i] for i in user_turrets]

    # 1. Try user's ship solo (with gadgets + active modules)
    solo_result = compute_with_gadgets(
        mass, resistance_pct, user_configs, gadgets, always_use_best_gadget,
    ) if user_configs else BreakResult(
        percentage=0, used_lasers=[], insufficient=True,
    )

    if not solo_result.insufficient:
        return FleetBreakResult(
            user_can_solo=True,
            solo_result=solo_result,
        )

    # 2. User can't solo — find fleet substitutions
    n = len(lasers)
    if n == 0:
        return FleetBreakResult(user_can_solo=False, solo_result=solo_result)

    # Build boosted laser list (active modules engaged where available).
    # Used for subset evaluation so fleet ships with active modules
    # are properly factored in before resorting to substitution.
    boosted_lasers = []
    for l in lasers:
        if l.active_uses_remaining > 0 and l.max_power_active > 0:
            boosted_lasers.append(LaserConfig(
                name=l.name,
                max_power=l.max_power_active,
                min_power=l.min_power,
                resistance_modifier=l.resistance_modifier_active,
                visible=l.visible,
                max_power_active=l.max_power_active,
                resistance_modifier_active=l.resistance_modifier_active,
                active_module_uses=l.active_module_uses,
                active_uses_remaining=l.active_uses_remaining,
                ship_id=l.ship_id,
                ship_display=l.ship_display,
                ship_type=l.ship_type,
                player_count=l.player_count,
                turret_index=l.turret_index,
            ))
        else:
            boosted_lasers.append(l)

    # Build gadget-boosted laser lists: apply the best available gadget's
    # resistance modifier to every turret. This accounts for how gadgets
    # physically reduce rock resistance for all lasers firing at it.
    best_gadget = None
    gadget_boosted = None
    gadget_active_boosted = None
    sorted_gadgets = sorted(
        [g for g in gadgets if g.resistance is not None and g.resistance < 0],
        key=lambda g: g.resistance,
    )
    if sorted_gadgets:
        best_gadget = sorted_gadgets[0]
        gf = 1.0 + best_gadget.resistance / 100.0
        gadget_boosted = [
            LaserConfig(
                name=l.name, max_power=l.max_power, min_power=l.min_power,
                resistance_modifier=l.resistance_modifier * gf,
                visible=l.visible,
                max_power_active=l.max_power_active,
                resistance_modifier_active=l.resistance_modifier_active * gf,
                active_module_uses=l.active_module_uses,
                active_uses_remaining=l.active_uses_remaining,
                ship_id=l.ship_id, ship_display=l.ship_display,
                ship_type=l.ship_type, player_count=l.player_count,
                turret_index=l.turret_index,
            )
            for l in lasers
        ]
        gadget_active_boosted = [
            LaserConfig(
                name=l.name,
                max_power=l.max_power_active if l.active_uses_remaining > 0 and l.max_power_active > 0 else l.max_power,
                min_power=l.min_power,
                resistance_modifier=(l.resistance_modifier_active if l.active_uses_remaining > 0 else l.resistance_modifier) * gf,
                visible=l.visible,
                max_power_active=l.max_power_active,
                resistance_modifier_active=l.resistance_modifier_active * gf,
                active_module_uses=l.active_module_uses,
                active_uses_remaining=l.active_uses_remaining,
                ship_id=l.ship_id, ship_display=l.ship_display,
                ship_type=l.ship_type, player_count=l.player_count,
                turret_index=l.turret_index,
            )
            for l in lasers
        ]

    # Collect all viable subsets. Try in escalating order:
    # 1. Active modules (no gadget)
    # 2. Passive only (no gadget)
    # 3. Active modules + best gadget
    # 4. Passive + best gadget
    viable: list[tuple[list[int], float, float, float, bool, str | None]] = []
    # (subset, pct, stability, needed, uses_actives, gadget_name)

    tiers: list[tuple[list[LaserConfig], bool, str | None]] = [
        (boosted_lasers, True, None),
        (lasers, False, None),
    ]
    if gadget_active_boosted:
        tiers.append((gadget_active_boosted, True, best_gadget.name))
    if gadget_boosted:
        tiers.append((gadget_boosted, False, best_gadget.name))

    for try_lasers, uses_actives, gadget_name in tiers:
        if n <= 12:
            for mask in range(1, 1 << n):
                subset = [i for i in range(n) if mask & (1 << i)]
                cmax, crmod, needed, pct = _subset_stats(subset, try_lasers, mass, resistance_pct)
                if needed == float("inf") or pct > 100.0:
                    continue
                stab = stability_score(cmax, needed, crmod)
                viable.append((subset, pct, stab, needed, uses_actives, gadget_name))
        else:
            indices = sorted(range(n), key=lambda i: -try_lasers[i].max_power)
            used: list[int] = []
            for idx in indices:
                used.append(idx)
                cmax, crmod, needed, pct = _subset_stats(used, try_lasers, mass, resistance_pct)
                if needed != float("inf") and pct <= 100.0:
                    stab = stability_score(cmax, needed, crmod)
                    viable.append((list(used), pct, stab, needed, uses_actives, gadget_name))
                    break
        if viable:
            break

    if not viable:
        return FleetBreakResult(user_can_solo=False, solo_result=solo_result)

    # 3. Score by "least players" and "least ships"
    def _player_key(entry):
        subset, pct, stab, _, _, _ = entry
        players, ships, _ = _subset_ship_info(subset, lasers)
        return (players, -stab)

    def _ship_key(entry):
        subset, pct, stab, _, _, _ = entry
        players, ships, _ = _subset_ship_info(subset, lasers)
        return (ships, -stab)

    # Least players
    viable_lp = sorted(viable, key=_player_key)
    lp_subset, lp_pct, lp_stab, _, lp_actives, lp_gadget = viable_lp[0]
    lp_players, lp_ships, lp_names = _subset_ship_info(lp_subset, lasers)
    lp_result = BreakResult(
        percentage=lp_pct,
        used_lasers=[lasers[i].name for i in lp_subset],
        insufficient=False,
        active_modules_needed=1 if lp_actives else 0,
        gadget_used=lp_gadget,
    )

    # Least ships
    viable_ls = sorted(viable, key=_ship_key)
    ls_subset, ls_pct, ls_stab, _, ls_actives, ls_gadget = viable_ls[0]
    ls_players, ls_ships, ls_names = _subset_ship_info(ls_subset, lasers)
    ls_result = BreakResult(
        percentage=ls_pct,
        used_lasers=[lasers[i].name for i in ls_subset],
        insufficient=False,
        active_modules_needed=1 if ls_actives else 0,
        gadget_used=ls_gadget,
    )

    return FleetBreakResult(
        user_can_solo=False,
        solo_result=solo_result,
        least_players=lp_result,
        least_players_count=lp_players,
        least_players_ships=lp_names,
        least_players_stability=lp_stab,
        least_ships=ls_result,
        least_ships_count=ls_ships,
        least_ships_names=ls_names,
        least_ships_stability=ls_stab,
    )


# ── Team-aware breakability ──────────────────────────────────────────────


@dataclass
class SubstituteInfo:
    """A ship/player substitute from another team or cluster."""
    ship_display: str
    ship_id: str
    team_name: str
    cluster: str
    player_names: list[str] = field(default_factory=list)
    used_turrets: list[str] = field(default_factory=list)  # turret names actually used
    reason: str = ""  # "additional_crew" | "substitute_ship"


@dataclass
class TeamBreakResult:
    """Result of team-aware breakability with escalating search scope."""
    user_can_solo: bool
    solo_result: BreakResult | None = None
    team_can_break: bool = False
    team_result: BreakResult | None = None
    team_ships_used: list[str] = field(default_factory=list)
    needs_additional_crew: list[SubstituteInfo] = field(default_factory=list)
    substitutes: list[SubstituteInfo] = field(default_factory=list)
    substitute_result: BreakResult | None = None
    search_scope: str = ""  # "solo" | "team" | "cluster" | "fleet" | ""


def team_breakability(
    mass: float,
    resistance_pct: float,
    user_ship_id: str,
    team_configs: list[LaserConfig],
    cluster_configs: list[tuple[str, str, list[LaserConfig]]],
    fleet_configs: list[tuple[str, str, list[LaserConfig]]],
    available_gadgets: list[GadgetInfo] | None = None,
    always_use_best_gadget: bool = False,
) -> TeamBreakResult:
    """Team-aware breakability with escalating search scope.

    Search order:
    1. User's ship solo
    2. User's full team
    3. Same cluster (one team at a time, alphabetical)
    4. Other clusters (alphabetical by letter, then by team name)

    Parameters
    ----------
    team_configs : LaserConfigs for all mining ships in the user's team.
    cluster_configs : [(team_name, cluster, configs)] for OTHER teams
        in the same cluster, sorted alphabetically by team_name.
    fleet_configs : [(team_name, cluster, configs)] for teams in OTHER
        clusters, sorted by cluster letter then team name.
    """
    gadgets = available_gadgets or []

    # 1. User solo
    user_turrets = [c for c in team_configs if c.ship_id == user_ship_id]
    if user_turrets:
        solo = compute_with_gadgets(
            mass, resistance_pct, user_turrets, gadgets, always_use_best_gadget,
        )
    else:
        solo = BreakResult(percentage=0, used_lasers=[], insufficient=True)

    if not solo.insufficient:
        return TeamBreakResult(
            user_can_solo=True, solo_result=solo, search_scope="solo",
        )

    # 2. Full team
    if team_configs:
        team_result = compute_with_gadgets(
            mass, resistance_pct, team_configs, gadgets, always_use_best_gadget,
        )
        if not team_result.insufficient:
            used_ships = list({
                c.ship_display for c in team_configs
                if c.name in team_result.used_lasers
            })
            return TeamBreakResult(
                user_can_solo=False, solo_result=solo,
                team_can_break=True, team_result=team_result,
                team_ships_used=used_ships, search_scope="team",
            )

    # 3. Cluster search — add one team at a time from same cluster
    for team_name, cluster, extra_configs in cluster_configs:
        combined = list(team_configs) + extra_configs
        result = compute_with_gadgets(
            mass, resistance_pct, combined, gadgets, always_use_best_gadget,
        )
        if not result.insufficient:
            subs = _extract_substitutes(extra_configs, result, team_name, cluster)
            return TeamBreakResult(
                user_can_solo=False, solo_result=solo,
                substitutes=subs, substitute_result=result,
                search_scope="cluster",
            )

    # 4. Fleet-wide search — single other-cluster team at a time
    for team_name, cluster, extra_configs in fleet_configs:
        combined = list(team_configs) + extra_configs
        result = compute_with_gadgets(
            mass, resistance_pct, combined, gadgets, always_use_best_gadget,
        )
        if not result.insufficient:
            subs = _extract_substitutes(extra_configs, result, team_name, cluster)
            return TeamBreakResult(
                user_can_solo=False, solo_result=solo,
                substitutes=subs, substitute_result=result,
                search_scope="fleet",
            )

    # 5. All same-cluster teams combined. Steps 3/4 only tried ONE
    # substitute team at a time — if no single team supplies enough
    # firepower, combine the whole home cluster before reaching out.
    if cluster_configs:
        all_same_cluster: list[LaserConfig] = []
        for _, _, extra_configs in cluster_configs:
            all_same_cluster.extend(extra_configs)
        if all_same_cluster:
            combined = list(team_configs) + all_same_cluster
            result = compute_with_gadgets(
                mass, resistance_pct, combined, gadgets, always_use_best_gadget,
            )
            if not result.insufficient:
                subs: list[SubstituteInfo] = []
                for team_name, cluster, extra_configs in cluster_configs:
                    subs.extend(_extract_substitutes(
                        extra_configs, result, team_name, cluster,
                    ))
                return TeamBreakResult(
                    user_can_solo=False, solo_result=solo,
                    substitutes=subs, substitute_result=result,
                    search_scope="cluster",
                )

    # 6. Cumulative cross-cluster expansion. Start with the user's
    # team plus the entire home cluster, then pull in other clusters
    # one-by-one in alphabetical order (nearest cluster first), adding
    # each cluster's teams into the pool. Stops at the first cumulative
    # combination that breaks the rock.
    from collections import defaultdict

    accumulated = list(team_configs)
    added_teams: list[tuple[str, str, list[LaserConfig]]] = []

    # Preload the home cluster into the pool (step 5 already proved it
    # alone isn't enough, so any success from here means we added at
    # least one other-cluster team).
    for team_name, cluster, extra_configs in cluster_configs:
        accumulated.extend(extra_configs)
        added_teams.append((team_name, cluster, extra_configs))

    # Group fleet_configs by cluster so we can walk cluster-by-cluster
    # in alphabetical order. Teams within a cluster retain the order
    # the caller provided (already alphabetical).
    fleet_by_cluster: dict[str, list[tuple[str, list[LaserConfig]]]] = defaultdict(list)
    cluster_order: list[str] = []
    for team_name, cluster, extra_configs in fleet_configs:
        if cluster not in fleet_by_cluster:
            cluster_order.append(cluster)
        fleet_by_cluster[cluster].append((team_name, extra_configs))

    # Sort clusters alphabetically. "Unclustered" teams (empty string)
    # sort first; acceptable fallback — they're still cheaper to pull
    # in than reorganizing clusters.
    cluster_order.sort()

    for cluster_name in cluster_order:
        # Add every team in this cluster, then test — one cluster at a
        # time so we don't over-pull from a distant cluster when a
        # nearer one's contribution would have sufficed.
        for team_name, extra_configs in fleet_by_cluster[cluster_name]:
            accumulated.extend(extra_configs)
            added_teams.append((team_name, cluster_name, extra_configs))

        result = compute_with_gadgets(
            mass, resistance_pct, accumulated, gadgets, always_use_best_gadget,
        )
        if not result.insufficient:
            subs: list[SubstituteInfo] = []
            for t_name, cl, cfgs in added_teams:
                subs.extend(_extract_substitutes(cfgs, result, t_name, cl))
            return TeamBreakResult(
                user_can_solo=False, solo_result=solo,
                substitutes=subs, substitute_result=result,
                search_scope="fleet",
            )

    # Nothing works even with every mining ship in the fleet.
    return TeamBreakResult(
        user_can_solo=False, solo_result=solo, search_scope="",
    )


def _extract_substitutes(
    extra_configs: list[LaserConfig],
    result: BreakResult,
    team_name: str,
    cluster: str,
) -> list[SubstituteInfo]:
    """Identify which ships from extra_configs are used in the result.

    Groups used turrets by ship so the break bubble can render a
    "cluster → team → player → ship → lasers" breakdown with all
    of the metadata the caller wanted to verify.
    """
    used_set = set(result.used_lasers)
    by_ship: dict[str, SubstituteInfo] = {}
    for c in extra_configs:
        if c.name not in used_set:
            continue
        if c.ship_id not in by_ship:
            by_ship[c.ship_id] = SubstituteInfo(
                ship_display=c.ship_display,
                ship_id=c.ship_id,
                team_name=c.team_name or team_name,
                cluster=c.cluster or cluster,
                player_names=list(c.player_names),
                used_turrets=[],
                reason="substitute_ship",
            )
        by_ship[c.ship_id].used_turrets.append(c.name)
    return list(by_ship.values())
