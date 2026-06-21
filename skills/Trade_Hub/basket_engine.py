"""Multi-commodity basket route planner.

Given a set of commodities the user wants to acquire and a starting
terminal, this picks the minimum-churn sequence of pickup stops by
clustering commodities that share a seller terminal and ordering
remaining stops by shortest real distance (UEX Gm) from the current
route head.

Algorithm: iterative greedy set-cover with a distance tiebreak.

  1. Index all routes → ``{terminal → {commodity → (scu, price)}}``,
     restricted to terminals with at least one selected commodity in
     stock.
  2. While the selected-commodity set is not fully covered:
     a. For every candidate terminal, compute its "coverage" =
        |offerings ∩ still-uncovered|.
     b. Rank candidates by (−coverage, distance_from_head_Gm).  The
        winner is the terminal that grabs the most missing commodities,
        breaking ties by proximity to the previous stop.
     c. Append a stop, mark its commodities covered, move head to it.
  3. Stop early if any commodity cannot be sourced — the remaining
     items show up in ``unresolved``.

Pure data layer. No PySide6 imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from trade_hub_data import Route, DistanceCache


@dataclass(frozen=True)
class TerminalKey:
    """Uniquely identifies a pickup terminal."""
    terminal_id: int
    terminal_name: str
    location: str
    system: str


@dataclass
class Offer:
    """What one terminal offers for one commodity."""
    commodity: str
    scu_available: int
    price_buy: float


@dataclass
class BasketStop:
    """One stop in a planned multi-commodity pickup route."""
    terminal: TerminalKey
    picks: List[Offer]
    distance_from_prev_gm: Optional[float]  # None if unresolved


@dataclass
class BasketPlan:
    start_terminal_id: Optional[int]
    stops: List[BasketStop] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)
    total_distance_gm: float = 0.0
    label: str = ""
    # "buy"  → stops are pickup locations (where player acquires commodities)
    # "sell" → stops are dropoff locations (where player sells commodities)
    # Consumers (detail dialog, cards) key their labelling off this.
    mode: str = "buy"


SellersIndex = Dict[TerminalKey, Dict[str, Offer]]


def build_sellers_index(
    routes: Iterable[Route],
    selected: Set[str],
) -> SellersIndex:
    """Aggregate routes into ``{terminal → {commodity → best offer}}``.

    Only includes terminals that have stock (``scu_available > 0``) and
    a non-zero buy price for at least one selected commodity. When the
    same (terminal, commodity) appears in multiple routes (once per
    sell pairing), we keep the entry with the highest listed stock and
    lowest buy price — same terminal inventory either way.
    """
    idx: SellersIndex = {}
    for r in routes:
        if r.commodity not in selected:
            continue
        if r.scu_available <= 0 or r.price_buy <= 0 or not r.id_terminal_buy:
            continue
        key = TerminalKey(
            terminal_id=r.id_terminal_buy,
            terminal_name=r.buy_terminal or "",
            location=r.buy_location or "",
            system=r.buy_system or "",
        )
        offers = idx.setdefault(key, {})
        existing = offers.get(r.commodity)
        if existing is None:
            offers[r.commodity] = Offer(r.commodity, r.scu_available, r.price_buy)
        else:
            offers[r.commodity] = Offer(
                r.commodity,
                max(existing.scu_available, r.scu_available),
                min(existing.price_buy, r.price_buy),
            )
    return idx


def _distance(
    cache: DistanceCache,
    origin_id: Optional[int],
    dest_id: int,
) -> Optional[float]:
    """Lookup-only — never hits network. Caller should prefetch."""
    if origin_id is None or origin_id == dest_id:
        return 0.0
    return cache.get(origin_id, dest_id)


def distance_pairs_needed(
    sellers: SellersIndex,
    start_terminal_id: Optional[int],
) -> Set[Tuple[int, int]]:
    """Every terminal pair the greedy planner might consult.

    Used to warm :class:`DistanceCache` before calling :func:`plan_route`
    so the plan step itself is synchronous and fast.
    """
    ids = [k.terminal_id for k in sellers.keys()]
    pairs: Set[Tuple[int, int]] = set()
    # Start → every candidate
    if start_terminal_id is not None:
        for tid in ids:
            if tid != start_terminal_id:
                pairs.add((start_terminal_id, tid))
    # Every candidate → every other candidate (for tiebreaks mid-route)
    for a in ids:
        for b in ids:
            if a != b:
                pairs.add((a, b))
    return pairs


_RankFn = "callable"


def _rank_min_stops(coverage: int, dist: float, avg_price: float) -> Tuple:
    return (-coverage, dist, avg_price)


def _rank_min_distance(coverage: int, dist: float, avg_price: float) -> Tuple:
    return (dist, -coverage, avg_price)


def _rank_best_price(coverage: int, dist: float, avg_price: float) -> Tuple:
    return (avg_price, -coverage, dist)


def _rank_best_sell_price(coverage: int, dist: float, avg_price: float) -> Tuple:
    # Mirror of _rank_best_price for the sell side: higher price is better,
    # so sort by -avg_price (still ascending tuple compare → larger price
    # ends up first).
    return (-avg_price, -coverage, dist)


def _plan_with_strategy(
    sellers: SellersIndex,
    selected: Set[str],
    start_terminal_id: Optional[int],
    dist_cache: DistanceCache,
    rank_fn,
    force_first: Optional[TerminalKey] = None,
) -> BasketPlan:
    plan = BasketPlan(start_terminal_id=start_terminal_id)
    uncovered: Set[str] = set(selected)
    head: Optional[int] = start_terminal_id
    remaining: SellersIndex = dict(sellers)
    first_iter = True

    while uncovered and remaining:
        best: Optional[TerminalKey] = None
        best_rank = None

        if first_iter and force_first is not None and force_first in remaining:
            best = force_first
        else:
            for term, offers in remaining.items():
                covered_here = uncovered & set(offers.keys())
                if not covered_here:
                    continue
                coverage = len(covered_here)
                dist = _distance(dist_cache, head, term.terminal_id)
                dist_val = dist if dist is not None else float("inf")
                prices = [offers[c].price_buy for c in covered_here]
                avg_price = sum(prices) / len(prices) if prices else float("inf")
                rank = rank_fn(coverage, dist_val, avg_price)
                if best_rank is None or rank < best_rank:
                    best = term
                    best_rank = rank

        if best is None:
            break

        offers = remaining.pop(best)
        picks = sorted(
            (offers[c] for c in offers if c in uncovered),
            key=lambda o: o.commodity.casefold(),
        )
        if not picks:
            first_iter = False
            continue
        step_dist = _distance(dist_cache, head, best.terminal_id)
        plan.stops.append(BasketStop(
            terminal=best,
            picks=picks,
            distance_from_prev_gm=step_dist,
        ))
        if step_dist is not None:
            plan.total_distance_gm += step_dist
        for p in picks:
            uncovered.discard(p.commodity)
        head = best.terminal_id
        first_iter = False

    plan.unresolved = sorted(uncovered, key=str.casefold)
    return plan


def plan_route(
    sellers: SellersIndex,
    selected: Set[str],
    start_terminal_id: Optional[int],
    dist_cache: DistanceCache,
) -> BasketPlan:
    """Greedy set-cover route. Requires distances already cached."""
    return _plan_with_strategy(
        sellers, selected, start_terminal_id, dist_cache, _rank_min_stops
    )


# ── Sell-side ────────────────────────────────────────────────────

def build_buyers_index(
    routes: Iterable[Route],
    selected: Set[str],
) -> SellersIndex:
    """Aggregate routes into ``{terminal → {commodity → best offer}}`` for
    the *sell* side — terminals that accept the selected commodities and
    pay the player for them.

    Only includes terminals with active demand (``scu_demand > 0``) and a
    non-zero sell price. When the same (terminal, commodity) appears on
    multiple routes we keep the highest demand + highest sell price seen
    (since both are terminal inventory facts, not route-specific).

    Returns the same structure as :func:`build_sellers_index` so the
    planner can be reused verbatim. The :class:`Offer` fields are read as
    "quantity the terminal will accept" and "price per unit paid to the
    player" in this direction.
    """
    idx: SellersIndex = {}
    for r in routes:
        if r.commodity not in selected:
            continue
        if r.scu_demand <= 0 or r.price_sell <= 0 or not r.id_terminal_sell:
            continue
        key = TerminalKey(
            terminal_id=r.id_terminal_sell,
            terminal_name=r.sell_terminal or "",
            location=r.sell_location or "",
            system=r.sell_system or "",
        )
        offers = idx.setdefault(key, {})
        existing = offers.get(r.commodity)
        if existing is None:
            offers[r.commodity] = Offer(r.commodity, r.scu_demand, r.price_sell)
        else:
            offers[r.commodity] = Offer(
                r.commodity,
                max(existing.scu_available, r.scu_demand),
                # Sell side: keep the HIGHEST price we've seen for this
                # commodity at this terminal (opposite of buy side).
                max(existing.price_buy, r.price_sell),
            )
    return idx


def plan_sell_variants(
    buyers: SellersIndex,
    selected: Set[str],
    start_terminal_id: Optional[int],
    dist_cache: DistanceCache,
    max_variants: int = 5,
) -> List[BasketPlan]:
    """Mirror of :func:`plan_variants` for the sell side.

    Same three-strategy exploration (min stops / shortest trip / best
    price) plus forced-first variants, but "best price" flips to prefer
    the HIGHEST per-unit sell price.

    Every returned plan is tagged ``mode="sell"`` so downstream UI can
    label stops as dropoffs and show revenue instead of cost.
    """
    strategies = [
        ("MIN STOPS",     _rank_min_stops),
        ("SHORTEST TRIP", _rank_min_distance),
        ("BEST PRICE",    _rank_best_sell_price),
    ]
    plans: List[BasketPlan] = []
    seen: Set[Tuple[int, ...]] = set()

    def _tag(p: BasketPlan, label: str) -> None:
        p.label = label
        p.mode = "sell"

    for name, rank_fn in strategies:
        p = _plan_with_strategy(buyers, selected, start_terminal_id, dist_cache, rank_fn)
        sig = tuple(s.terminal.terminal_id for s in p.stops)
        if p.stops and sig not in seen:
            _tag(p, name)
            plans.append(p)
            seen.add(sig)
        if len(plans) >= max_variants:
            return plans

    # Forced-first variants ordered by (coverage desc, distance asc) —
    # same idea as the buy path, gives the user more real alternatives.
    candidates = sorted(
        buyers.items(),
        key=lambda it: (
            -len(set(selected) & set(it[1].keys())),
            _distance(dist_cache, start_terminal_id, it[0].terminal_id) or float("inf"),
        ),
    )
    for term, offers in candidates:
        if len(plans) >= max_variants:
            break
        if not (set(selected) & set(offers.keys())):
            continue
        p = _plan_with_strategy(
            buyers, selected, start_terminal_id, dist_cache,
            _rank_min_stops, force_first=term,
        )
        sig = tuple(s.terminal.terminal_id for s in p.stops)
        if p.stops and sig not in seen:
            _tag(p, f"VIA {term.terminal_name}".upper())
            plans.append(p)
            seen.add(sig)

    return plans


def plan_variants(
    sellers: SellersIndex,
    selected: Set[str],
    start_terminal_id: Optional[int],
    dist_cache: DistanceCache,
    max_variants: int = 5,
) -> List[BasketPlan]:
    """Produce up to ``max_variants`` distinct candidate plans.

    Combines three scoring strategies (minimum stops, shortest travel,
    best average buy price) plus forced-first-stop variants so the user
    can compare real options rather than just the greedy winner.
    """
    strategies = [
        ("MIN STOPS",     _rank_min_stops),
        ("SHORTEST TRIP", _rank_min_distance),
        ("BEST PRICE",    _rank_best_price),
    ]
    plans: List[BasketPlan] = []
    seen: Set[Tuple[int, ...]] = set()

    for name, rank_fn in strategies:
        p = _plan_with_strategy(sellers, selected, start_terminal_id, dist_cache, rank_fn)
        sig = tuple(s.terminal.terminal_id for s in p.stops)
        if p.stops and sig not in seen:
            p.label = name
            plans.append(p)
            seen.add(sig)
        if len(plans) >= max_variants:
            return plans

    # Forced-first variants: try alternative starting stops, ranked by
    # how much they cover, to give additional distinct options.
    candidates = sorted(
        sellers.items(),
        key=lambda it: (
            -len(set(selected) & set(it[1].keys())),
            _distance(dist_cache, start_terminal_id, it[0].terminal_id) or float("inf"),
        ),
    )
    for term, offers in candidates:
        if len(plans) >= max_variants:
            break
        if not (set(selected) & set(offers.keys())):
            continue
        p = _plan_with_strategy(
            sellers, selected, start_terminal_id, dist_cache,
            _rank_min_stops, force_first=term,
        )
        sig = tuple(s.terminal.terminal_id for s in p.stops)
        if p.stops and sig not in seen:
            p.label = f"VIA {term.terminal_name}".upper()
            plans.append(p)
            seen.add(sig)

    return plans
