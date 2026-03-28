"""
Mixed-freight solver for Trade Hub Beta V1.2.

Builds mixed-cargo loads that combine multiple commodities into a single
cargo bay when no single commodity can fill the ship.  Supports greedy
single-leg packing and multi-leg chaining with stop penalties.

DO NOT import any UI (tkinter, PySide6) code here.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from trade_hub_data import Route, calc_profit, get_calc_mode

log = logging.getLogger("TradeHub.mixed_freight")


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class CargoSlot:
    """A single commodity occupying part of a cargo bay."""
    commodity:    str   = ""
    scu_loaded:   int   = 0
    price_buy:    float = 0.0
    price_sell:   float = 0.0
    margin:       float = 0.0
    profit:       float = 0.0
    buy_terminal:  str  = ""
    sell_terminal: str  = ""
    is_primary:   bool  = False
    is_illegal:   bool  = False
    distance:     float = 0.0


@dataclass
class MixedLeg:
    """One leg of a mixed-freight route with multiple cargo slots."""
    cargo_slots:    List[CargoSlot] = field(default_factory=list)
    buy_terminal:   str = ""
    buy_location:   str = ""
    buy_system:     str = ""
    sell_terminal:  str = ""
    sell_location:  str = ""
    sell_system:    str = ""

    def total_scu(self) -> int:
        return sum(s.scu_loaded for s in self.cargo_slots)

    def total_profit(self) -> float:
        return sum(s.profit for s in self.cargo_slots)

    def total_investment(self) -> float:
        return sum(s.scu_loaded * s.price_buy for s in self.cargo_slots)

    def fill_pct(self, ship_scu: int) -> float:
        if ship_scu <= 0:
            return 0.0
        return (self.total_scu() / ship_scu) * 100.0

    def primary_commodity(self) -> str:
        for s in self.cargo_slots:
            if s.is_primary:
                return s.commodity
        if self.cargo_slots:
            return max(self.cargo_slots, key=lambda s: s.scu_loaded).commodity
        return ""

    def total_distance(self) -> float:
        if self.cargo_slots:
            return max(s.distance for s in self.cargo_slots)
        return 0.0


@dataclass
class MixedRoute:
    """Complete mixed-freight route composed of one or more MixedLegs."""
    legs:     List[MixedLeg] = field(default_factory=list)
    ship_scu: int = 0

    def total_profit(self) -> float:
        return sum(leg.total_profit() for leg in self.legs)

    def total_investment(self) -> float:
        return sum(leg.total_investment() for leg in self.legs)

    def roi_pct(self) -> float:
        inv = self.total_investment()
        return (self.total_profit() / inv * 100.0) if inv > 0 else 0.0

    def fill_efficiency(self) -> float:
        if not self.legs or self.ship_scu <= 0:
            return 0.0
        return sum(leg.fill_pct(self.ship_scu) for leg in self.legs) / len(self.legs)

    def num_legs(self) -> int:
        return len(self.legs)

    def commodity_summary(self) -> str:
        parts: List[str] = []
        for leg in self.legs:
            names = [s.commodity for s in leg.cargo_slots]
            parts.append("+".join(names) if names else "?")
        return " \u203a ".join(parts)

    def min_primary_avail(self) -> int:
        vals: List[int] = []
        for leg in self.legs:
            for s in leg.cargo_slots:
                if s.is_primary:
                    vals.append(s.scu_loaded)
        return min(vals) if vals else 0

    def total_distance(self) -> float:
        return sum(leg.total_distance() for leg in self.legs)

    @property
    def start_terminal(self) -> str:
        return self.legs[0].buy_terminal if self.legs else ""

    @property
    def start_system(self) -> str:
        return self.legs[0].buy_system if self.legs else ""

    @property
    def end_terminal(self) -> str:
        return self.legs[-1].sell_terminal if self.legs else ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_illegal(route: Route) -> bool:
    """Safely check the is_illegal flag (field may not exist yet)."""
    return getattr(route, "is_illegal", False)


def _available_scu(route: Route) -> int:
    """Effective supply for mixed-freight purposes (min of available, demand)."""
    avail = route.scu_available if route.scu_available > 0 else 0
    demand = route.scu_demand if route.scu_demand > 0 else 0
    if avail > 0 and demand > 0:
        return min(avail, demand)
    return max(avail, demand)


# ── Calc-mode aware profit ────────────────────────────────────────────────────

def calc_slot_profit(slot: CargoSlot) -> float:
    """Calculate profit for a single cargo slot using the active calc mode.

    Builds a temporary Route from the slot's parameters so the full
    calc_profit pipeline (Monte Carlo, Risk Adjusted, etc.) is applied
    per-commodity rather than just simple margin * scu.
    """
    mode = get_calc_mode()
    mode_id = mode.get("id", "standard")
    if mode_id == "standard" or not mode.get("params"):
        return slot.scu_loaded * slot.margin

    # Build a lightweight Route so calc_profit can run its simulation
    tmp = Route(
        commodity=slot.commodity,
        buy_terminal=slot.buy_terminal,
        sell_terminal=slot.sell_terminal,
        price_buy=slot.price_buy,
        price_sell=slot.price_sell,
        margin=slot.margin,
        scu_available=slot.scu_loaded,
        scu_demand=slot.scu_loaded,
    )
    return calc_profit(tmp, slot.scu_loaded)


def calc_mixed_leg_profit(leg: MixedLeg) -> float:
    """Calculate total profit for a mixed leg using the active calc mode."""
    return sum(calc_slot_profit(s) for s in leg.cargo_slots)


def calc_mixed_route_profit(route: MixedRoute) -> float:
    """Calculate total profit for a mixed route using the active calc mode."""
    return sum(calc_mixed_leg_profit(leg) for leg in route.legs)


# ── Single-leg builder ────────────────────────────────────────────────────────

def build_single_mixed_leg(
    routes: List[Route],
    buy_terminal: str,
    sell_terminal: str,
    ship_scu: int,
    allow_illegal: bool = True,
) -> Optional[MixedLeg]:
    """Pack the cargo bay for one (buy_terminal -> sell_terminal) leg.

    Filters *routes* to the given terminal pair, sorts by margin descending,
    and greedily fills the bay respecting per-commodity scu_available/demand.
    Returns ``None`` if no positive-margin commodities are loadable.
    """
    pair_routes = [
        r for r in routes
        if r.buy_terminal == buy_terminal
        and r.sell_terminal == sell_terminal
        and r.margin > 0
        and (allow_illegal or not _is_illegal(r))
    ]
    if not pair_routes:
        return None

    pair_routes.sort(key=lambda r: r.margin, reverse=True)

    remaining = ship_scu
    slots: List[CargoSlot] = []
    first = True

    for r in pair_routes:
        if remaining <= 0:
            break
        loadable = min(remaining, _available_scu(r))
        if loadable <= 0:
            continue
        slots.append(CargoSlot(
            commodity=r.commodity,
            scu_loaded=loadable,
            price_buy=r.price_buy,
            price_sell=r.price_sell,
            margin=r.margin,
            profit=loadable * r.margin,
            buy_terminal=r.buy_terminal,
            sell_terminal=r.sell_terminal,
            is_primary=first,
            is_illegal=_is_illegal(r),
            distance=r.distance,
        ))
        remaining -= loadable
        first = False

    if not slots:
        return None

    # Grab location metadata from the first route in the pair
    ref = pair_routes[0]
    return MixedLeg(
        cargo_slots=slots,
        buy_terminal=buy_terminal,
        buy_location=ref.buy_location,
        buy_system=ref.buy_system,
        sell_terminal=sell_terminal,
        sell_location=ref.sell_location,
        sell_system=ref.sell_system,
    )


# ── Main solver ───────────────────────────────────────────────────────────────

def find_mixed_routes(
    routes: List[Route],
    ship_scu: int,
    allow_illegal: bool = True,
    min_fill_pct: float = 70.0,
    stop_penalty_pct: float = 5.0,
    max_stops_route: int = 3,
    max_stops_loop: int = 7,
    top_k: int = 300,
) -> List[MixedRoute]:
    """Find mixed-freight routes that combine multiple commodities per leg.

    Algorithm
    ---------
    1. Index routes by (buy_terminal, sell_terminal) pair and by buy_terminal.
       Filter illegal routes when *allow_illegal* is False.
    2. At each buy terminal identify **anchors**: routes in the top-25 % by
       margin where ``scu_available < ship_scu`` (i.e. a single commodity
       cannot fill the bay).
    3. For each anchor, build a single-leg mixed load: load the anchor first,
       then greedily fill the remaining bay with fillers from the same terminal
       pair sorted by margin descending (respecting per-commodity supply/demand).
    4. Chain single-leg loads into multi-leg routes via greedy extension,
       avoiding terminal revisits except when returning to the origin (loops).
    5. Apply a cumulative stop penalty, filter by *min_fill_pct*, and rank by
       adjusted profit.

    Parameters
    ----------
    routes : list[Route]
        Full set of single-commodity routes from the data layer.
    ship_scu : int
        Total cargo capacity of the player's ship (SCU).
    allow_illegal : bool
        Whether to include routes flagged as illegal cargo.
    min_fill_pct : float
        Minimum average cargo-bay fill percentage to keep a route.
    stop_penalty_pct : float
        Profit penalty applied per additional stop beyond the first leg.
    max_stops_route : int
        Maximum legs in a point-to-point route.
    max_stops_loop : int
        Maximum legs when building loop routes (return to origin).
    top_k : int
        Number of top routes to return.
    """
    if not routes or ship_scu <= 0:
        return []

    # ── 1. Build indexes ──────────────────────────────────────────────────
    filtered = [
        r for r in routes
        if r.buy_terminal and r.sell_terminal and r.margin > 0
        and (allow_illegal or not _is_illegal(r))
    ]
    if not filtered:
        return []

    # Index by (buy, sell) pair
    pair_idx: Dict[Tuple[str, str], List[Route]] = defaultdict(list)
    # Index by buy terminal
    buy_idx: Dict[str, List[Route]] = defaultdict(list)

    for r in filtered:
        key = (r.buy_terminal, r.sell_terminal)
        pair_idx[key].append(r)
        buy_idx[r.buy_terminal].append(r)

    # ── 2. Identify anchors ───────────────────────────────────────────────
    anchors: List[Route] = []
    for terminal, terminal_routes in buy_idx.items():
        if not terminal_routes:
            continue
        margins = sorted([r.margin for r in terminal_routes], reverse=True)
        threshold_idx = max(0, len(margins) // 4 - 1)
        margin_threshold = margins[threshold_idx] if margins else 0.0

        for r in terminal_routes:
            if r.margin >= margin_threshold and _available_scu(r) < ship_scu:
                anchors.append(r)

    # Fallback A: if no anchors found (all commodities fill the bay),
    # use the top route at each terminal as anchor anyway — mixed freight
    # can still combine commodities heading to the same destination.
    if not anchors:
        for terminal_routes in buy_idx.values():
            if terminal_routes:
                anchors.append(terminal_routes[0])

    # Fallback B: still nothing usable
    if not anchors:
        return []

    # ── 3. Build single-leg mixed loads from each anchor ──────────────────
    seen_legs: Dict[Tuple[str, str], MixedLeg] = {}

    for anchor in anchors:
        key = (anchor.buy_terminal, anchor.sell_terminal)
        if key in seen_legs:
            continue
        leg = build_single_mixed_leg(
            filtered, anchor.buy_terminal, anchor.sell_terminal,
            ship_scu, allow_illegal,
        )
        if leg is not None:
            seen_legs[key] = leg

    # Fallback C: if no mixed legs could be built (e.g. every terminal pair
    # has only one commodity), build legs from the top routes at each buy
    # terminal across ALL sell terminals.
    if not seen_legs:
        for bt, bt_routes in buy_idx.items():
            # Group by sell terminal and build a leg for each
            sell_groups: Dict[str, bool] = {}
            for r in bt_routes:
                st = r.sell_terminal
                if st not in sell_groups:
                    sell_groups[st] = True
                    leg = build_single_mixed_leg(
                        filtered, bt, st, ship_scu, allow_illegal,
                    )
                    if leg is not None:
                        seen_legs[(bt, st)] = leg

    if not seen_legs:
        return []

    # ── 4. Chain legs into multi-leg routes ───────────────────────────────
    seen_sigs: set = set()
    candidates: List[MixedRoute] = []

    # Collect all unique sell terminals reachable from each buy terminal
    sell_by_buy: Dict[str, List[str]] = defaultdict(list)
    for (bt, st) in seen_legs:
        sell_by_buy[bt].append(st)

    for (start_buy, start_sell), start_leg in seen_legs.items():
        # Single-leg route
        _add_candidate(
            [start_leg], ship_scu, seen_sigs, candidates,
        )

        # Extend greedily (point-to-point)
        path_terminals = {start_buy, start_sell}
        chain: List[MixedLeg] = [start_leg]
        current = start_sell

        for _ in range(max_stops_route - 1):
            best_leg: Optional[MixedLeg] = None
            best_profit = 0.0
            for next_sell in sell_by_buy.get(current, []):
                if next_sell in path_terminals:
                    continue
                leg = seen_legs.get((current, next_sell))
                if leg is not None and leg.total_profit() > best_profit:
                    best_profit = leg.total_profit()
                    best_leg = leg
            if best_leg is None:
                break
            chain.append(best_leg)
            path_terminals.add(best_leg.sell_terminal)
            current = best_leg.sell_terminal
            _add_candidate(list(chain), ship_scu, seen_sigs, candidates)

        # Try loop extension (return to origin)
        if len(chain) >= 1:
            loop_chain = list(chain)
            loop_terminals = set(path_terminals)
            loop_current = current
            for _ in range(max_stops_loop - len(loop_chain)):
                # Allow returning to start_buy only
                best_leg = None
                best_profit = 0.0
                for next_sell in sell_by_buy.get(loop_current, []):
                    if next_sell in loop_terminals and next_sell != start_buy:
                        continue
                    leg = seen_legs.get((loop_current, next_sell))
                    if leg is not None and leg.total_profit() > best_profit:
                        best_profit = leg.total_profit()
                        best_leg = leg
                if best_leg is None:
                    break
                loop_chain.append(best_leg)
                loop_terminals.add(best_leg.sell_terminal)
                loop_current = best_leg.sell_terminal
                _add_candidate(list(loop_chain), ship_scu, seen_sigs, candidates)
                if loop_current == start_buy:
                    break  # loop complete

    # ── 5. Score, filter, rank ────────────────────────────────────────────
    # Use calc-mode-aware profit (Monte Carlo, Risk Adjusted, etc.)
    result: List[Tuple[float, MixedRoute]] = []
    for mr in candidates:
        if mr.fill_efficiency() < min_fill_pct:
            continue
        n_stops = mr.num_legs()
        penalty = 1.0 - (stop_penalty_pct / 100.0) * max(0, n_stops - 1)
        penalty = max(penalty, 0.1)
        adjusted = calc_mixed_route_profit(mr) * penalty
        result.append((adjusted, mr))

    # Fallback D: if min_fill_pct filtered everything out (common with
    # very large ships), retry with no fill threshold so the user still
    # sees the best available options.
    if not result and candidates:
        log.info("No routes met %.0f%% fill threshold — showing all %d candidates",
                 min_fill_pct, len(candidates))
        for mr in candidates:
            n_stops = mr.num_legs()
            penalty = 1.0 - (stop_penalty_pct / 100.0) * max(0, n_stops - 1)
            penalty = max(penalty, 0.1)
            adjusted = calc_mixed_route_profit(mr) * penalty
            result.append((adjusted, mr))

    result.sort(key=lambda pair: pair[0], reverse=True)
    return [mr for _, mr in result[:top_k]]


def _add_candidate(
    legs: List[MixedLeg],
    ship_scu: int,
    seen_sigs: set,
    candidates: List[MixedRoute],
) -> None:
    """De-duplicate and append a MixedRoute candidate."""
    sig = "->".join(
        f"{leg.buy_terminal}:{leg.primary_commodity()}" for leg in legs
    )
    if sig in seen_sigs:
        return
    seen_sigs.add(sig)
    candidates.append(MixedRoute(legs=list(legs), ship_scu=ship_scu))


# ── Sort helper ───────────────────────────────────────────────────────────────

def sort_mixed_routes(
    routes: List[MixedRoute],
    col: str,
    reverse: bool,
    ship_scu: int = 0,
) -> List[MixedRoute]:
    """Sort a list of MixedRoutes by the given column key."""
    km: Dict[str, Any] = {
        "origin":       lambda m: m.start_terminal.lower(),
        "origin_sys":   lambda m: m.start_system.lower(),
        "legs":         lambda m: m.num_legs(),
        "commodities":  lambda m: m.commodity_summary().lower(),
        "fill_pct":     lambda m: m.fill_efficiency(),
        "avail":        lambda m: m.min_primary_avail(),
        "total_profit": lambda m: calc_mixed_route_profit(m),
    }
    key_fn = km.get(col, lambda m: calc_mixed_route_profit(m))
    return sorted(routes, key=key_fn, reverse=reverse)
