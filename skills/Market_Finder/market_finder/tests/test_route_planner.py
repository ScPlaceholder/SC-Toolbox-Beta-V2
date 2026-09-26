"""Tests for market_finder.route_planner -- the grocery list's route calculator.

Pure (no Qt, no network).  Optimality is checked against brute force: every
choice of terminal per item x every visit order, on instances small enough to
enumerate.
"""

import itertools
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))

import pytest  # noqa: E402

from market_finder import route_planner as rp  # noqa: E402
from market_finder.grocery import buy_locations  # noqa: E402


# -- helpers --------------------------------------------------------------------

def _row(tid, place, price, system="Stanton", x=0.0, y=0.0):
    return {"terminal": f"T{tid}", "terminal_id": tid, "system": system,
            "location": f"{system} > {place}", "places": [place], "price": price,
            "_xy": (x, y)}


class Plane:
    """Sites on a plane; distance = Euclidean between the sites' points."""

    def __init__(self, coords):
        self.coords = coords            # site name -> (x, y)

    def __call__(self, a, b):
        pa = self.coords[(a.get("places") or ["?"])[0]]
        pb = self.coords[(b.get("places") or ["?"])[0]]
        return math.dist(pa, pb)


def _brute_force(wants, dist):
    """Optimal (cost) over every eligible (cheapest-price) site per want x every order."""
    options = []
    for w in wants:
        cheapest = min(r["price"] for r in w["rows"])
        options.append({r["places"][0] for r in w["rows"] if r["price"] <= cheapest})
    best = math.inf
    for combo in itertools.product(*options):
        for perm in itertools.permutations(set(combo)):
            c = sum(dist({"places": [a]}, {"places": [b]}) for a, b in zip(perm, perm[1:]))
            best = min(best, c)
    return best


def _random_instance(rng, n_items, n_sites):
    coords = {f"S{i}": (rng.uniform(0, 100), rng.uniform(0, 100)) for i in range(n_sites)}
    wants = []
    tid = 1
    for i in range(n_items):
        sellers = rng.sample(sorted(coords), rng.randint(1, min(4, n_sites)))
        rows = []
        for s in sellers:
            rows.append(_row(tid, s, rng.choice([100, 100, 100, 120])))
            tid += 1
        wants.append({"item_id": i, "name": f"item{i}", "rows": rows})
    return wants, Plane(coords)


# -- ordering -------------------------------------------------------------------

class TestOrderStops:
    def test_order_is_optimal_not_nearest_neighbour_from_the_first_card(self):
        # Sites on a line at 0, 10, -1, 11.  Nearest-neighbour from the first
        # card (0) goes 0 -> -1 -> 10 -> 11 = 13; the best open path is
        # -1 -> 0 -> 10 -> 11 = 12.
        coords = {"A": (0, 0), "B": (10, 0), "C": (-1, 0), "D": (11, 0)}
        dist = Plane(coords)
        stops = [{"item_id": i, "name": n, "terminal_id": i + 1, "system": "Stanton",
                  "location": f"Stanton > {n}", "places": [n]}
                 for i, n in enumerate("ABCD")]
        ordered = rp.order_stops(stops, dist)
        cost = sum(dist(a, b) for a, b in zip(ordered, ordered[1:]))
        best = min(sum(dist(a, b) for a, b in zip(p, p[1:])) for p in itertools.permutations(stops))
        assert sorted(s["item_id"] for s in ordered) == [0, 1, 2, 3]
        assert cost == pytest.approx(best)
        assert cost == pytest.approx(12.0)


# -- joint terminal choice + order ---------------------------------------------

class TestPlanRoute:
    def test_matches_brute_force_optimum_on_random_instances(self):
        rng = random.Random(1234)
        for _ in range(120):
            wants, dist = _random_instance(rng, rng.randint(2, 5), rng.randint(2, 6))
            stops = rp.plan_route(wants, dist)
            got = rp.route_cost(stops, dist)
            assert got == pytest.approx(_brute_force(wants, dist), abs=1e-6)

    def test_every_item_covered_once_at_a_terminal_that_sells_it_at_the_cheapest_price(self):
        rng = random.Random(99)
        for _ in range(60):
            wants, dist = _random_instance(rng, rng.randint(1, 6), rng.randint(1, 7))
            stops = rp.plan_route(wants, dist)
            assert sorted(s["item_id"] for s in stops) == sorted(w["item_id"] for w in wants)
            for s in stops:
                w = next(w for w in wants if w["item_id"] == s["item_id"])
                sold_at = {r["terminal_id"]: r["price"] for r in w["rows"]}
                assert s["terminal_id"] in sold_at, "impossible location"
                assert s["price"] == min(sold_at.values())

    def test_no_needless_stop(self):
        rng = random.Random(7)
        for _ in range(60):
            wants, dist = _random_instance(rng, rng.randint(2, 6), rng.randint(2, 7))
            stops = rp.plan_route(wants, dist)
            vs = rp.visits(stops)
            for v in vs:
                assert v["items"], "a stop that buys nothing"
            # every visit is a different site (no back-and-forth to one site)
            assert len({v["key"] for v in vs}) == len(vs)

    def test_consolidates_equally_cheap_terminals_real_uex_rows(self):
        # Real UEX items_prices rows (2026-09-26): the Omnisky III Cannon costs
        # 14,845 aUEC at MIC-L2, CRU-L5, HUR-L3 AND Orison; the other item is
        # only sold at Orison.  Cheapest-row-per-item picked MIC-L2 (first tie)
        # and flew across the system for nothing -- one stop is enough.
        omnisky = buy_locations([
            {"id_terminal": 107, "terminal_name": "CenterMass - IO North Tower - Area 18", "price_buy": 15461,
             "star_system_name": "Stanton", "planet_name": "ArcCorp", "city_name": "Area 18"},
            {"id_terminal": 108, "terminal_name": "CenterMass - New Babbage", "price_buy": 15461,
             "star_system_name": "Stanton", "planet_name": "MicroTech", "city_name": "New Babbage"},
            {"id_terminal": 194, "terminal_name": "Ship Weapons - MIC-L2", "price_buy": 14845,
             "star_system_name": "Stanton", "planet_name": "MicroTech",
             "space_station_name": "MIC-L2 Long Forest Station"},
            {"id_terminal": 189, "terminal_name": "Ship Weapons - CRU-L5", "price_buy": 14845,
             "star_system_name": "Stanton", "planet_name": "Crusader",
             "space_station_name": "CRU-L5 Beautiful Glen Station"},
            {"id_terminal": 191, "terminal_name": "Ship Weapons - HUR-L3", "price_buy": 14845,
             "star_system_name": "Stanton", "planet_name": "Hurston",
             "space_station_name": "HUR-L3 Thundering Express Station"},
            {"id_terminal": 111, "terminal_name": "Cousin Crow's - Providence Platform - Orison",
             "price_buy": 14845, "star_system_name": "Stanton", "planet_name": "Crusader",
             "city_name": "Orison"},
        ])
        other = buy_locations([
            {"id_terminal": 205, "terminal_name": "Makau - Clothing Shop - Cloudview Center - Orison",
             "price_buy": 900, "star_system_name": "Stanton", "planet_name": "Crusader",
             "city_name": "Orison"},
        ])
        wants = [{"item_id": 1, "name": "Omnisky III Cannon", "rows": omnisky},
                 {"item_id": 2, "name": "Jacket", "rows": other}]
        from market_finder.starmap.distances import body_distance
        stops = rp.plan_route(wants, body_distance)
        assert {s["terminal_id"] for s in stops} == {111, 205}
        assert len(rp.visits(stops)) == 1
        assert rp.route_cost(stops, body_distance) == 0.0
        assert [s["price"] for s in stops if s["item_id"] == 1] == [14845]

    def test_price_premium_constant_trades_price_for_travel(self):
        coords = {"Near": (0, 0), "Far": (100, 0)}
        dist = Plane(coords)
        wants = [{"item_id": 1, "name": "a", "rows": [_row(1, "Near", 50)]},
                 {"item_id": 2, "name": "b", "rows": [_row(2, "Far", 100), _row(3, "Near", 105)]}]
        strict = rp.plan_route(wants, dist, max_price_premium=0.0)
        loose = rp.plan_route(wants, dist, max_price_premium=0.10)
        assert rp.route_cost(strict, dist) == pytest.approx(100.0)
        assert rp.route_cost(loose, dist) == 0.0
        assert rp.MAX_PRICE_PREMIUM == 0.0      # default: never pay more than the cheapest price

    def test_unknown_distances_never_drop_an_item(self):
        wants = [{"item_id": i, "name": str(i), "rows": [_row(i, f"P{i}", 10)]} for i in range(4)]
        stops = rp.plan_route(wants, lambda a, b: None)
        assert sorted(s["item_id"] for s in stops) == [0, 1, 2, 3]

    def test_heuristic_fallback_covers_everything_with_no_needless_stop(self, monkeypatch):
        monkeypatch.setattr(rp, "EXACT_MAX_EXPANSIONS", 0)       # force the fallback
        rng = random.Random(5)
        worse = 0
        for _ in range(40):
            wants, dist = _random_instance(rng, rng.randint(3, 6), rng.randint(3, 7))
            stops = rp.plan_route(wants, dist)
            assert sorted(s["item_id"] for s in stops) == sorted(w["item_id"] for w in wants)
            if rp.route_cost(stops, dist) > _brute_force(wants, dist) + 1e-6:
                worse += 1
        assert worse <= 4            # heuristic, not exact: allowed to miss occasionally

    def test_long_list_stays_fast_enough_for_the_ui(self):
        rng = random.Random(3)
        wants, dist = _random_instance(rng, 30, 60)
        t = time.perf_counter()
        stops = rp.plan_route(wants, dist)
        assert time.perf_counter() - t < 5.0
        assert len(stops) == 30


# -- cost model -----------------------------------------------------------------

class TestSiteDistance:
    @pytest.fixture(autouse=True)
    def _no_disk_cache(self, monkeypatch, tmp_path):
        from market_finder.starmap import distances
        monkeypatch.setattr(distances, "_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(distances, "_CACHE_PATH", str(tmp_path / "distance_cache.json"))
        monkeypatch.setattr(distances, "_cache", {})
        return distances

    A18 = {"key": ("stanton", "area 18"), "system": "Stanton",
           "places": ["Area 18", "ArcCorp"], "terminal_ids": [94]}
    NB = {"key": ("stanton", "new babbage"), "system": "Stanton",
          "places": ["New Babbage", "MicroTech"], "terminal_ids": [108]}
    RUIN = {"key": ("pyro", "ruin station"), "system": "Pyro",
            "places": ["Ruin Station", "Terminus"], "terminal_ids": [457]}

    def test_body_model_is_in_gigametres_and_matches_uex(self, _no_disk_cache):
        d = _no_disk_cache
        # UEX terminals_distances (2026-09-26): 94<->108 = 59, 94 -> 457 = 99.
        assert d.site_distance(self.A18, self.NB) == pytest.approx(59, abs=1.0)
        assert d.site_distance(self.A18, self.RUIN) == pytest.approx(99 + d.JUMP_PENALTY_GM, abs=1.0)

    def test_telemetry_is_used_in_either_direction_plus_the_jump_penalty(self, _no_disk_cache):
        d = _no_disk_cache
        d._cache = {"108-94": 61.0, "94-457": 99.0}
        assert d.site_distance(self.A18, self.NB) == 61.0          # stored only as 108 -> 94
        assert d.site_distance(self.RUIN, self.A18) == 99.0 + d.JUMP_PENALTY_GM

    def test_same_site_is_free_and_telemetry_pairs_skip_it(self, _no_disk_cache):
        d = _no_disk_cache
        other_a18 = dict(self.A18, terminal_ids=[107])
        assert d.site_distance(self.A18, other_a18) == 0.0
        # Two Area 18 shops + New Babbage: the shops are one site, so ONE
        # representative pair is fetched (UEX answers same-orbit pairs with no
        # data at all), not every terminal permutation.
        wants = [{"item_id": 1, "name": "a", "rows": [
                     {"terminal_id": 94, "system": "Stanton", "places": ["Area 18", "ArcCorp"], "price": 5},
                     {"terminal_id": 108, "system": "Stanton", "places": ["New Babbage", "MicroTech"], "price": 5}]},
                 {"item_id": 2, "name": "b", "rows": [
                     {"terminal_id": 107, "system": "Stanton", "places": ["Area 18", "ArcCorp"], "price": 5}]}]
        sites, _eligible = rp.candidate_sites(wants)
        assert len(sites) == 2
        assert d.telemetry_pairs(sites) == {(94, 108)}
