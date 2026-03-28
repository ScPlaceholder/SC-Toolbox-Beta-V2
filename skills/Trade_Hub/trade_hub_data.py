"""
Trade Hub data layer — extracted from the monolithic trade_hub_app.py.

Contains: Route, MultiRoute dataclasses, FilterState, DataFetcher,
apply_filters, sort_routes, find_multi_routes, and all pure-data helpers.

DO NOT import any UI (tkinter, PySide6) code here.
"""
from __future__ import annotations
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from shared.ships import SHIP_PRESETS, scu_for_ship, QUICK_SHIPS  # noqa: E402
from shared.data_utils import retry_request  # noqa: E402
from shared.i18n import s_ as _  # noqa: E402
from shared.api_config import (  # noqa: E402
    UEX_BASE_URL as UEX_BASE, UEX_USER_AGENT, UEX_TIMEOUT,
)

log = logging.getLogger("TradeHub.data")

# ── Persistent config ─────────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_hub_config.json")


# ── Column definitions ────────────────────────────────────────────────────────
COLUMNS: List[Tuple[str, str, int, str]] = [
    ("commodity",       _("Item"),          120, "w"),
    ("buy_terminal",    _("Buy At"),        148, "w"),
    ("cs_origin",       _("CS"),             46, "center"),
    ("investment",      _("Invest..."),      90, "e"),
    ("available_scu",   _("SCU"),            58, "e"),
    ("scu_user_origin", "SCU-U",             58, "e"),
    ("sell_terminal",   _("Sell At"),       148, "w"),
    ("cs_dest",         _("CS"),             46, "center"),
    ("invest_dest",     _("Sell"),           90, "e"),
    ("scu_demand",      "SCU-C",             58, "e"),
    ("distance",        _("Distance"),       72, "e"),
    ("eta",             _("ETA"),            60, "e"),
    ("roi",             _("ROI"),            58, "e"),
    ("est_profit",      _("Income"),        100, "e"),
]

COLUMN_KEYS = tuple(c[0] for c in COLUMNS)

LOOP_COLUMNS: List[Tuple[str, str, int, str]] = [
    ("origin",       _("Origin Terminal"),  175, "w"),
    ("origin_sys",   _("Sys"),               65, "w"),
    ("legs",         _("Legs"),              42, "e"),
    ("commodities",  _("Commodity Chain"),  265, "w"),
    ("avail",        _("Min Avail SCU"),     95, "e"),
    ("total_profit", _("Est. Total Profit"),145, "e"),
]

LOOP_COLUMN_KEYS = tuple(c[0] for c in LOOP_COLUMNS)

MIXED_COLUMNS: List[Tuple[str, str, int, str]] = [
    ("origin",       _("Origin Terminal"),  160, "w"),
    ("origin_sys",   _("Sys"),               65, "w"),
    ("legs",         _("Legs"),              42, "e"),
    ("commodities",  _("Commodity Mix"),    280, "w"),
    ("fill_pct",     _("Fill %"),            65, "e"),
    ("avail",        _("Min Avail SCU"),     80, "e"),
    ("total_profit", _("Est. Total Profit"),145, "e"),
]
MIXED_COLUMN_KEYS = tuple(c[0] for c in MIXED_COLUMNS)


# ── Route data ────────────────────────────────────────────────────────────────

@dataclass
class Route:
    commodity:     str   = ""
    buy_terminal:  str   = ""
    buy_location:  str   = ""
    buy_system:    str   = ""
    sell_terminal: str   = ""
    sell_location: str   = ""
    sell_system:   str   = ""
    scu_available: int   = 0
    scu_demand:    int   = 0
    price_buy:     float = 0.0
    price_sell:    float = 0.0
    margin:        float = 0.0
    score:         float = 0.0
    investment:    float = 0.0
    profit:        float = 0.0
    price_roi:     float = 0.0
    distance:      float = 0.0
    container_sizes_origin: str = ""
    container_sizes_destination: str = ""
    scu_user_origin: int = 0
    scu_user_destination: int = 0
    id_terminal_buy:  int = 0
    id_terminal_sell: int = 0
    is_illegal:    bool  = False

    def effective_scu(self, ship_scu: int) -> int:
        if ship_scu <= 0:
            cap = 0
        else:
            cap = ship_scu
        if _max_profit_mode:
            # Max Profit: only cap by ship cargo and available stock
            stock = self.scu_available if self.scu_available > 0 else self.scu_demand
            return min(cap, stock) if cap > 0 else stock
        if self.scu_available > 0 and self.scu_demand > 0:
            effective = min(cap, self.scu_available, self.scu_demand) if cap > 0 else min(self.scu_available, self.scu_demand)
        else:
            stock = max(self.scu_available, self.scu_demand)
            effective = min(cap, stock) if cap > 0 else stock
        return effective

    def estimated_profit(self, ship_scu: int) -> float:
        return self.effective_scu(ship_scu) * self.margin

    def roi(self) -> float:
        if self.price_buy <= 0:
            return 0.0
        return (self.margin / self.price_buy) * 100.0


@dataclass
class MultiRoute:
    legs: List[Route] = field(default_factory=list)

    def total_profit(self, ship_scu: int) -> float:
        return sum(calc_profit(r, ship_scu) for r in self.legs)

    def total_investment(self, ship_scu: int) -> float:
        return sum(r.effective_scu(ship_scu) * r.price_buy for r in self.legs)

    def roi_pct(self, ship_scu: int) -> float:
        inv = self.total_investment(ship_scu)
        return (self.total_profit(ship_scu) / inv * 100.0) if inv > 0 else 0.0

    def avg_margin(self) -> float:
        return sum(r.margin for r in self.legs) / len(self.legs) if self.legs else 0.0

    def min_avail(self) -> int:
        return min(r.scu_available for r in self.legs) if self.legs else 0

    def commodity_chain(self) -> str:
        return " \u203a ".join(r.commodity for r in self.legs)

    @property
    def start_terminal(self) -> str:
        return self.legs[0].buy_terminal if self.legs else ""

    @property
    def start_system(self) -> str:
        return self.legs[0].buy_system if self.legs else ""

    @property
    def end_terminal(self) -> str:
        return self.legs[-1].sell_terminal if self.legs else ""

    @property
    def num_legs(self) -> int:
        return len(self.legs)

    def total_distance(self) -> float:
        return sum(r.distance for r in self.legs)

    def profit_per_distance(self, ship_scu: int) -> float:
        td = self.total_distance()
        if td <= 0:
            return self.total_profit(ship_scu)
        return self.total_profit(ship_scu) / td


# ── Filter & sort ─────────────────────────────────────────────────────────────

@dataclass
class FilterState:
    system:         str   = ""
    location:       str   = ""
    commodity:      str   = ""
    search:         str   = ""
    min_margin_scu: float = 0.0
    buy_system:     str   = ""
    sell_system:    str   = ""
    buy_location:   str   = ""
    sell_location:  str   = ""
    buy_terminal:   str   = ""
    sell_terminal:  str   = ""
    min_scu:        int   = 0
    only_selected_systems: bool = False
    allow_illegal: bool = True  # When False, filter out illegal routes


def apply_filters(routes: List[Route], f: FilterState) -> List[Route]:
    result = routes
    if not f.allow_illegal:
        result = [r for r in result if not r.is_illegal]
    if f.system:
        s = f.system.lower()
        result = [r for r in result if s in r.buy_system.lower() or s in r.sell_system.lower()]
    if f.only_selected_systems and (f.buy_system or f.sell_system):
        allowed = set()
        if f.buy_system:
            allowed.add(f.buy_system.lower())
        if f.sell_system:
            allowed.add(f.sell_system.lower())
        result = [r for r in result
                  if r.buy_system.lower() in allowed
                  and r.sell_system.lower() in allowed]
    else:
        if f.buy_system:
            s = f.buy_system.lower()
            result = [r for r in result if s in r.buy_system.lower()]
        if f.sell_system:
            s = f.sell_system.lower()
            result = [r for r in result if s in r.sell_system.lower()]
    if f.location:
        loc = f.location.lower()
        result = [r for r in result if any(loc in x.lower() for x in [
            r.buy_location, r.buy_terminal, r.buy_system,
            r.sell_location, r.sell_terminal, r.sell_system])]
    if f.buy_location:
        bl = f.buy_location.lower()
        result = [r for r in result if bl in r.buy_location.lower() or bl in r.buy_terminal.lower()]
    if f.sell_location:
        sl = f.sell_location.lower()
        result = [r for r in result if sl in r.sell_location.lower() or sl in r.sell_terminal.lower()]
    if f.buy_terminal:
        bt = f.buy_terminal.lower()
        result = [r for r in result if bt in r.buy_terminal.lower()]
    if f.sell_terminal:
        st = f.sell_terminal.lower()
        result = [r for r in result if st in r.sell_terminal.lower()]
    if f.commodity:
        c = f.commodity.lower()
        result = [r for r in result if c in r.commodity.lower()]
    if f.search:
        q = f.search.lower()
        result = [r for r in result if any(q in x.lower() for x in [
            r.commodity, r.buy_location, r.buy_terminal, r.buy_system,
            r.sell_location, r.sell_terminal, r.sell_system])]
    if f.min_margin_scu > 0:
        result = [r for r in result if r.margin >= f.min_margin_scu]
    if f.min_scu > 0:
        result = [r for r in result if r.scu_available >= f.min_scu]
    return result


def sort_routes(routes: List[Route], col: str, reverse: bool, ship_scu: int = 0) -> List[Route]:
    km = {
        "commodity":       lambda r: r.commodity.lower(),
        "buy_terminal":    lambda r: r.buy_terminal.lower(),
        "sell_terminal":   lambda r: r.sell_terminal.lower(),
        "cs_origin":       lambda r: r.container_sizes_origin,
        "cs_dest":         lambda r: r.container_sizes_destination,
        "investment":      lambda r: r.price_buy * r.effective_scu(ship_scu),
        "invest_dest":     lambda r: r.price_sell * r.effective_scu(ship_scu),
        "available_scu":   lambda r: r.effective_scu(ship_scu),
        "scu_user_origin": lambda r: r.scu_user_origin,
        "scu_demand":      lambda r: r.scu_demand,
        "scu_user_dest":   lambda r: r.scu_user_destination,
        "distance":        lambda r: r.distance,
        "eta":             lambda r: r.distance,
        "roi":             lambda r: r.roi(),
        "est_profit":      lambda r: calc_profit(r, ship_scu),
    }
    return sorted(routes, key=km.get(col, lambda r: r.score), reverse=reverse)


def find_multi_routes(routes: List[Route], ship_scu: int = 0,
                      max_steps: int = 3, top_k: int = 300) -> List[MultiRoute]:
    if not routes:
        return []
    adj: Dict[str, List[Route]] = {}
    for r in routes:
        if r.buy_terminal and r.sell_terminal and r.margin > 0:
            adj.setdefault(r.buy_terminal, []).append(r)
    for t in adj:
        if ship_scu > 0:
            adj[t].sort(key=lambda r: calc_profit(r, ship_scu), reverse=True)
        else:
            adj[t].sort(key=lambda r: r.margin * min(r.scu_available, 5000), reverse=True)

    seen_sigs: set = set()
    candidates: List[MultiRoute] = []
    for start_terminal, outgoing in adj.items():
        for start_route in outgoing[:5]:
            path = [start_route.buy_terminal, start_route.sell_terminal]
            legs: List[Route] = [start_route]
            current = start_route.sell_terminal
            for _ in range(max_steps - 1):
                options = adj.get(current, [])
                intermediates = set(path[1:])
                options = [r for r in options
                           if r.sell_terminal not in intermediates
                           and r.sell_terminal != current]
                if not options:
                    break
                best = options[0]
                legs.append(best)
                path.append(best.sell_terminal)
                current = best.sell_terminal
            sig = "->".join(f"{r.buy_terminal}:{r.commodity}" for r in legs)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            candidates.append(MultiRoute(legs=list(legs)))

    candidates.sort(key=lambda m: m.total_profit(ship_scu), reverse=True)
    return candidates[:top_k]


def find_max_profit_routes(routes: List[Route], ship_scu: int = 0,
                           max_steps: int = 5, top_k: int = 300) -> List[MultiRoute]:
    """Find trade chains that maximise absolute profit via exhaustive search.

    Unlike the greedy ``find_multi_routes`` this function explores ALL viable
    chains up to *max_steps* legs using a bounded DFS with aggressive pruning.

    Key optimisations
    -----------------
    * **Best-first per terminal** — outgoing routes are pre-sorted by
      ``calc_profit`` so the first option explored is the highest-value one.
    * **Upper-bound pruning** — if the best possible remaining profit
      (assuming every future leg earns the global-max single-leg profit)
      cannot beat an already-found solution, the branch is abandoned.
    * **Top-N per terminal cap** — only the top 10 outgoing routes per
      terminal are explored, keeping branching factor manageable.
    * **Profit-per-distance scoring** — final ranking divides total profit
      by total distance so short, high-profit chains rank above long
      mediocre ones.  Zero-distance legs are treated as near-instant.
    """
    if not routes:
        return []

    # ── Build adjacency ──────────────────────────────────────────────
    adj: Dict[str, List[Route]] = {}
    for r in routes:
        if r.buy_terminal and r.sell_terminal and r.margin > 0:
            adj.setdefault(r.buy_terminal, []).append(r)

    # Pre-sort by ship-aware profit; cap branching factor per terminal
    BRANCH_CAP = 10
    for t in adj:
        if ship_scu > 0:
            adj[t].sort(key=lambda r: calc_profit(r, ship_scu), reverse=True)
        else:
            adj[t].sort(key=lambda r: r.margin * min(r.scu_available, 5000), reverse=True)
        adj[t] = adj[t][:BRANCH_CAP]

    # Global upper bound: the best single-leg profit achievable anywhere
    max_single = 0.0
    for bucket in adj.values():
        if bucket:
            p = calc_profit(bucket[0], ship_scu) if ship_scu > 0 else bucket[0].margin * min(bucket[0].scu_available, 5000)
            if p > max_single:
                max_single = p

    # ── DFS with pruning ─────────────────────────────────────────────
    seen_sigs: set = set()
    # (total_profit, distance, MultiRoute)
    best_routes: List[Tuple[float, float, MultiRoute]] = []
    # Track the profit threshold to beat (Kth best so far)
    profit_floor = 0.0

    def _dfs(legs: List[Route], visited: set, current: str, cumulative_profit: float,
             cumulative_distance: float) -> None:
        nonlocal profit_floor

        # Record every valid chain (≥ 1 leg)
        sig = "->".join(f"{r.buy_terminal}:{r.commodity}" for r in legs)
        if sig not in seen_sigs:
            seen_sigs.add(sig)
            mr = MultiRoute(legs=list(legs))
            dist = cumulative_distance if cumulative_distance > 0 else 1.0
            best_routes.append((cumulative_profit, dist, mr))

            # Update floor if we have enough candidates
            if len(best_routes) > top_k * 2:
                best_routes.sort(key=lambda t: t[0], reverse=True)
                del best_routes[top_k:]
                profit_floor = best_routes[-1][0] if best_routes else 0.0

        # Pruning: can remaining legs possibly beat what we have?
        remaining_steps = max_steps - len(legs)
        if remaining_steps <= 0:
            return
        upper_bound = cumulative_profit + remaining_steps * max_single
        if upper_bound <= profit_floor:
            return

        # Explore next hops
        for r in adj.get(current, []):
            dest = r.sell_terminal
            if dest in visited or dest == current:
                continue
            leg_profit = calc_profit(r, ship_scu) if ship_scu > 0 else r.margin * min(r.scu_available, 5000)
            new_profit = cumulative_profit + leg_profit
            new_dist = cumulative_distance + max(r.distance, 0.0)

            visited.add(dest)
            legs.append(r)
            _dfs(legs, visited, dest, new_profit, new_dist)
            legs.pop()
            visited.discard(dest)

    # Launch DFS from every terminal
    for start_terminal, outgoing in adj.items():
        for start_route in outgoing[:BRANCH_CAP]:
            dest = start_route.sell_terminal
            if dest == start_terminal:
                continue
            leg_profit = calc_profit(start_route, ship_scu) if ship_scu > 0 else start_route.margin * min(start_route.scu_available, 5000)
            visited = {start_terminal, dest}
            _dfs(
                [start_route], visited, dest,
                leg_profit, max(start_route.distance, 0.0),
            )

    # ── Rank by profit (primary), profit-per-distance (secondary) ─────
    # Primary sort: absolute profit descending
    # Secondary: profit-per-distance for tiebreaking
    best_routes.sort(key=lambda t: (t[0], t[0] / max(t[1], 1.0)), reverse=True)
    return [mr for _, _, mr in best_routes[:top_k]]


def sort_multi_routes(multi: List[MultiRoute], col: str, reverse: bool,
                      ship_scu: int = 0) -> List[MultiRoute]:
    km: Dict[str, Any] = {
        "origin":       lambda m: m.start_terminal.lower(),
        "origin_sys":   lambda m: m.start_system.lower(),
        "legs":         lambda m: m.num_legs,
        "commodities":  lambda m: m.commodity_chain().lower(),
        "avail":        lambda m: m.min_avail(),
        "profit_per_distance": lambda m: m.profit_per_distance(ship_scu),
        "total_profit": lambda m: m.total_profit(ship_scu),
    }
    return sorted(multi, key=km.get(col, lambda m: m.total_profit(ship_scu)), reverse=reverse)


def profit_tier(margin: float) -> str:
    return "high" if margin >= 1000 else ("med" if margin >= 300 else "low")


# ── Market mode (max profit vs reported demand) ──────────────────────────────
_max_profit_mode: bool = False


def set_market_mode(use_max: bool) -> None:
    global _max_profit_mode
    _max_profit_mode = use_max


def get_market_mode() -> bool:
    return _max_profit_mode


# ── Calculation mode (switchable at runtime) ──────────────────────────────────
_calc_mode: Dict = {"id": "standard", "params": {}}


def set_calc_mode(mode: Dict) -> None:
    global _calc_mode
    _calc_mode = mode


def get_calc_mode() -> Dict:
    return _calc_mode


def calc_profit(route: Route, ship_scu: int) -> float:
    """Calculate profit using the active calculation mode."""
    import random as _rnd
    mode_id = _calc_mode.get("id", "standard")
    params = _calc_mode.get("params", {})

    if mode_id == "standard" or not params:
        return route.estimated_profit(ship_scu)

    elif mode_id == "monte_carlo":
        # Scenario simulation: price drift (primary), inventory fluctuation, rare cargo loss
        iters = params.get("iterations", 500)
        noise_sell = params.get("price_noise_sell", 0.12)
        noise_buy = params.get("price_noise_buy", 0.03)
        inv_drop_rate = params.get("inventory_drop_rate", 0.15)
        inv_drop_min = params.get("inventory_drop_min", 0.2)
        inv_drop_max = params.get("inventory_drop_max", 0.8)
        cargo_loss_rate = params.get("cargo_loss_rate", 0.005)
        eff = route.effective_scu(ship_scu)
        if eff <= 0:
            return 0.0
        results = []
        pb = route.price_buy
        ps = route.price_sell
        for _ in range(iters):
            # Price drift (gaussian, clipped)
            buy_adj = pb * max(0.75, min(1.25, 1 + _rnd.gauss(0, noise_buy)))
            sell_adj = ps * max(0.75, min(1.25, 1 + _rnd.gauss(0, noise_sell)))
            # Inventory fluctuation — another player bought stock
            iter_eff = eff
            if _rnd.random() < inv_drop_rate:
                iter_eff = max(1, int(eff * (1 - _rnd.uniform(inv_drop_min, inv_drop_max))))
            # Rare cargo loss — total investment loss
            if _rnd.random() < cargo_loss_rate:
                results.append(-(buy_adj * iter_eff))
            else:
                results.append(iter_eff * (sell_adj - buy_adj))
        results.sort()
        return results[len(results) // 2]

    elif mode_id == "risk_adjusted":
        # 1-in-50 disaster amortization
        freq = params.get("disaster_frequency", 50)
        mult = params.get("loss_multiplier", 1.0)
        eff = route.effective_scu(ship_scu)
        normal_profit = route.estimated_profit(ship_scu)
        total_loss = route.price_buy * eff * mult
        return ((freq - 1) * normal_profit - total_loss) / freq

    elif mode_id == "multi_hop":
        # Multi-hop mode: single routes fall back to standard
        return route.estimated_profit(ship_scu)

    return route.estimated_profit(ship_scu)


def get_unique_commodities(routes: List[Route]) -> List[str]:
    return sorted({r.commodity for r in routes if r.commodity})


def fmt_distance(d: float) -> str:
    if d <= 0:
        return "\u2014"
    if d >= 1000:
        return f"{d / 1000:.1f}Tm"
    return f"{d:.1f}Gm"


def fmt_eta(distance_gm: float, speed_gms: float = 0.283) -> str:
    """Format ETA from distance in Gm and quantum speed in Gm/s."""
    if distance_gm <= 0:
        return "\u2014"
    secs = distance_gm / speed_gms if speed_gms > 0 else 0
    if secs < 60:
        return f"{secs:.0f}s"
    mins = secs / 60
    if mins < 60:
        return f"{mins:.0f}m"
    return f"{mins / 60:.1f}h"


# ── Route parsing ─────────────────────────────────────────────────────────────

def _safe(d: dict, key: str, default=""):
    v = d.get(key)
    return v if v is not None else default


def _best_loc_api(r: dict, suffix: str) -> str:
    for key in (f"outpost_{suffix}", f"city_{suffix}", f"space_station_{suffix}",
                f"moon_{suffix}", f"planet_{suffix}", f"star_system_{suffix}"):
        v = (r.get(key) or "").strip()
        if v:
            return v
    return ""


def route_from_api(r: dict, is_illegal: bool = False) -> Optional[Route]:
    margin = float(r.get("profit_margin", 0) or r.get("margin", 0) or 0)
    buy    = float(r.get("price_origin",  0) or r.get("price_buy",  0) or 0)
    sell   = float(r.get("price_destination", 0) or r.get("price_sell", 0) or 0)
    if margin == 0 and sell > buy:
        margin = sell - buy
    commodity = _safe(r, "commodity_name")
    if not commodity or margin <= 0:
        return None
    cs_origin = _safe(r, "container_sizes_origin")
    cs_dest   = _safe(r, "container_sizes_destination")
    return Route(
        commodity    = commodity,
        buy_terminal = (_safe(r, "terminal_origin") or _safe(r, "terminal_name_origin")),
        buy_location = _best_loc_api(r, "origin"),
        buy_system   = _safe(r, "star_system_origin"),
        sell_terminal= (_safe(r, "terminal_destination") or _safe(r, "terminal_name_destination")),
        sell_location= _best_loc_api(r, "destination"),
        sell_system  = _safe(r, "star_system_destination"),
        scu_available= int(_safe(r, "scu_origin",      0) or _safe(r, "scu_buy",  0) or 0),
        scu_demand   = int(_safe(r, "scu_destination", 0) or _safe(r, "scu_sell", 0) or 0),
        price_buy    = buy,
        price_sell   = sell,
        margin       = margin,
        score        = float(r.get("score", 0) or 0),
        investment   = float(r.get("investment", 0) or 0),
        profit       = float(r.get("profit", 0) or 0),
        price_roi    = float(r.get("price_roi", 0) or 0),
        distance     = float(r.get("distance", 0) or 0),
        container_sizes_origin      = str(cs_origin) if cs_origin else "",
        container_sizes_destination = str(cs_dest) if cs_dest else "",
        scu_user_origin      = int(r.get("scu_origin_users", 0) or 0),
        scu_user_destination = int(r.get("scu_destination_users", 0) or 0),
        is_illegal   = is_illegal,
    )


# ── Distance cache ────────────────────────────────────────────────────────────

_DISTANCE_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox")
_DISTANCE_CACHE_PATH = os.path.join(_DISTANCE_CACHE_DIR, "distance_cache.json")


class DistanceCache:
    """Persistent cache of terminal-to-terminal distances from the UEX API."""

    def __init__(self) -> None:
        self._cache: Dict[str, float] = {}
        self._load()

    @staticmethod
    def _key(origin_id: int, dest_id: int) -> str:
        return f"{origin_id}-{dest_id}"

    def _load(self) -> None:
        try:
            with open(_DISTANCE_CACHE_PATH, "r", encoding="utf-8") as fh:
                self._cache = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._cache = {}

    def _save(self) -> None:
        try:
            os.makedirs(_DISTANCE_CACHE_DIR, exist_ok=True)
            with open(_DISTANCE_CACHE_PATH, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh)
        except OSError:
            log.warning("Could not save distance cache")

    def get(self, origin_id: int, dest_id: int) -> Optional[float]:
        return self._cache.get(self._key(origin_id, dest_id))

    def _fetch_one(self, origin_id: int, dest_id: int) -> Optional[float]:
        """Fetch a single terminal-pair distance from the API."""
        try:
            headers = {"User-Agent": UEX_USER_AGENT, "Accept": "application/json"}
            url = (f"{UEX_BASE}/terminals_distances"
                   f"?id_terminal_origin={origin_id}"
                   f"&id_terminal_destination={dest_id}")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=UEX_TIMEOUT) as resp:
                body = json.loads(resp.read())
            data = body.get("data")
            if isinstance(data, list) and data:
                return float(data[0].get("distance", 0) or 0)
            elif isinstance(data, dict):
                return float(data.get("distance", 0) or 0)
            return 0.0
        except Exception:
            log.debug("Could not fetch distance %d→%d", origin_id, dest_id)
            return None

    def fetch_missing(self, pairs: set, on_progress=None) -> None:
        """Fetch distances for (origin_id, dest_id) pairs not in cache.

        Uses parallel requests for speed.  *on_progress(done, total)* is
        called periodically so the UI can show a status update.
        """
        missing = [(o, d) for o, d in pairs if self._key(o, d) not in self._cache]
        if not missing:
            return
        total = len(missing)
        log.info("Fetching %d missing terminal distances (parallel)...", total)
        fetched = 0
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(self._fetch_one, o, d): (o, d)
                for o, d in missing
            }
            for future in as_completed(futures):
                o, d = futures[future]
                dist = future.result()
                if dist is not None:
                    self._cache[self._key(o, d)] = dist
                    fetched += 1
                if on_progress and fetched % 25 == 0:
                    on_progress(fetched, total)
        if fetched:
            log.info("Fetched %d distances, saving cache", fetched)
            self._save()

    def populate_routes(self, routes: List[Route]) -> None:
        """Fill in distance on routes from cache."""
        for r in routes:
            if r.id_terminal_buy and r.id_terminal_sell:
                d = self.get(r.id_terminal_buy, r.id_terminal_sell)
                if d is not None:
                    r.distance = d


# Singleton distance cache
_dist_cache = DistanceCache()


# ── Data fetcher ──────────────────────────────────────────────────────────────

class DataFetcher:
    def __init__(self, refresh_interval: float = 300.0) -> None:
        self.refresh_interval = refresh_interval

    def fetch_async(self, callback, on_error=None,
                    on_distances_done=None, on_distance_progress=None) -> None:
        threading.Thread(
            target=self._worker,
            args=(callback, on_error, on_distances_done, on_distance_progress),
            daemon=True, name="TradeHubFetch",
        ).start()

    def _worker(self, callback, on_error=None,
                on_distances_done=None, on_distance_progress=None) -> None:
        routes, source = self._fetch(
            on_distances_done=on_distances_done,
            on_distance_progress=on_distance_progress,
        )
        try:
            callback(routes, source)
        except Exception:  # broad catch intentional: top-level UI handler
            log.warning("Fetch callback failed: %s", traceback.format_exc())
            if on_error:
                try:
                    on_error()
                except Exception:  # broad catch intentional: top-level UI handler
                    pass

    def _fetch(self, on_distances_done=None, on_distance_progress=None):
        try:
            routes = self._fetch_api(
                on_distances_done=on_distances_done,
                on_distance_progress=on_distance_progress,
            )
            return routes, "UEX API"
        except (urllib.error.URLError, json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
            return [], f"Error: {exc}"

    @staticmethod
    def _fetch_api(on_distances_done=None, on_distance_progress=None) -> List[Route]:
        headers = {"User-Agent": UEX_USER_AGENT, "Accept": "application/json"}

        def _get(path):
            url = f"{UEX_BASE}/{path}"
            req = urllib.request.Request(url, headers=headers)
            def _do_request():
                with urllib.request.urlopen(req, timeout=UEX_TIMEOUT) as resp:
                    return json.loads(resp.read()).get("data", [])
            return retry_request(_do_request, retries=1, backoff=1.0)

        prices = _get("commodities_prices_all")
        terminals = {t["id"]: t for t in _get("terminals")}
        commodities = {c["id"]: c for c in _get("commodities")}

        by_commodity: Dict[int, Dict[str, list]] = {}
        for p in prices:
            cid = p.get("id_commodity", 0)
            if not cid:
                continue
            entry = by_commodity.setdefault(cid, {"buys": [], "sells": []})
            pb = p.get("price_buy", 0) or 0
            ps = p.get("price_sell", 0) or 0
            if pb > 0:
                entry["buys"].append(p)
            if ps > 0:
                entry["sells"].append(p)

        def _loc(t):
            for k in ("city_name", "space_station_name", "outpost_name",
                      "moon_name", "planet_name"):
                v = (t.get(k) or "").strip()
                if v:
                    return v
            return ""

        routes: List[Route] = []
        for cid, data in by_commodity.items():
            comm = commodities.get(cid, {})
            comm_name = comm.get("name", f"Commodity {cid}")
            for buy in data["buys"]:
                for sell in data["sells"]:
                    bt_id = buy.get("id_terminal", 0)
                    st_id = sell.get("id_terminal", 0)
                    if bt_id == st_id:
                        continue
                    pb = float(buy.get("price_buy", 0) or 0)
                    ps = float(sell.get("price_sell", 0) or 0)
                    if ps <= pb or pb <= 0:
                        continue
                    margin = ps - pb
                    scu_avail = int(buy.get("scu_buy", buy.get("scu_buy_avg", 0)) or 0)
                    scu_demand = int(sell.get("scu_sell", sell.get("scu_sell_avg", 0)) or 0)
                    if scu_avail <= 0 and scu_demand <= 0:
                        continue
                    scu = min(scu_avail, scu_demand) if scu_avail > 0 and scu_demand > 0 else max(scu_avail, scu_demand)
                    if scu <= 0:
                        continue
                    inv = pb * scu
                    profit = margin * scu
                    roi = (margin / pb * 100) if pb > 0 else 0
                    bt = terminals.get(bt_id, {})
                    st = terminals.get(st_id, {})
                    # Use price record's own terminal_name as fallback when
                    # the terminal is missing from the terminals endpoint.
                    buy_tname = (bt.get("name") or bt.get("displayname")
                                 or buy.get("terminal_name") or f"T{bt_id}")
                    sell_tname = (st.get("name") or st.get("displayname")
                                  or sell.get("terminal_name") or f"T{st_id}")
                    r = Route(
                        commodity=comm_name,
                        buy_terminal=buy_tname,
                        buy_location=_loc(bt) or buy.get("location_name", ""),
                        buy_system=bt.get("star_system_name", "") or buy.get("star_system_name", ""),
                        sell_terminal=sell_tname,
                        sell_location=_loc(st) or sell.get("location_name", ""),
                        sell_system=st.get("star_system_name", "") or sell.get("star_system_name", ""),
                        scu_available=scu_avail,
                        scu_demand=scu_demand,
                        price_buy=pb,
                        price_sell=ps,
                        margin=margin,
                        score=profit,
                        investment=inv,
                        profit=profit,
                        price_roi=roi,
                        distance=0,
                        container_sizes_origin=buy.get("container_sizes", ""),
                        container_sizes_destination=sell.get("container_sizes", ""),
                        scu_user_origin=int(buy.get("scu_buy_users", 0) or 0),
                        scu_user_destination=int(sell.get("scu_sell_users", 0) or 0),
                        id_terminal_buy=bt_id,
                        id_terminal_sell=st_id,
                        is_illegal=bool(int(comm.get("is_illegal", 0) or 0)),
                    )
                    routes.append(r)

        routes.sort(key=lambda r: r.profit, reverse=True)
        routes = routes[:5000]

        # Apply any already-cached distances immediately
        pairs = {(r.id_terminal_buy, r.id_terminal_sell)
                 for r in routes if r.id_terminal_buy and r.id_terminal_sell}
        _dist_cache.populate_routes(routes)

        # Fetch missing distances in background — caller gets routes now,
        # distances arrive later via on_distances_done callback.
        missing = [(o, d) for o, d in pairs
                   if _dist_cache.get(o, d) is None]
        if missing and on_distances_done:
            def _bg_fetch():
                try:
                    _dist_cache.fetch_missing(pairs, on_progress=on_distance_progress)
                    _dist_cache.populate_routes(routes)
                    on_distances_done(routes)
                except Exception:
                    log.warning("Background distance fetch failed: %s",
                                traceback.format_exc())
            threading.Thread(target=_bg_fetch, daemon=True,
                             name="DistanceFetch").start()
        elif missing:
            # No callback — fetch synchronously (fallback)
            try:
                _dist_cache.fetch_missing(pairs)
                _dist_cache.populate_routes(routes)
            except Exception:
                log.warning("Distance enrichment failed: %s", traceback.format_exc())

        return routes


# ── Config persistence ────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
    except OSError:
        pass
