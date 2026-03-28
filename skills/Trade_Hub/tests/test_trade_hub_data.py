"""Tests for trade_hub_data — Route, MultiRoute, filtering, sorting, and helpers."""

import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
# Add Trade_Hub/ for local imports
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_hub_data import (
    Route, MultiRoute, FilterState,
    apply_filters, sort_routes, find_multi_routes,
    profit_tier, get_unique_commodities, fmt_distance, fmt_eta,
    route_from_api,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _route(**overrides) -> Route:
    defaults = dict(
        commodity="Laranite", buy_terminal="Admin Office", buy_location="Lorville",
        buy_system="Stanton", sell_terminal="TDD Orison", sell_location="Orison",
        sell_system="Stanton", scu_available=500, scu_demand=300,
        price_buy=27.0, price_sell=35.0, margin=8.0, score=42.0,
    )
    defaults.update(overrides)
    return Route(**defaults)


# ── Route tests ──────────────────────────────────────────────────────────────

class TestRoute:
    def test_effective_scu_both_positive(self):
        r = _route(scu_available=100, scu_demand=200)
        assert r.effective_scu(80) == 80

    def test_effective_scu_no_cap(self):
        r = _route(scu_available=100, scu_demand=200)
        assert r.effective_scu(0) == 100

    def test_estimated_profit(self):
        r = _route(scu_available=100, scu_demand=200, margin=5.0)
        assert r.estimated_profit(50) == 250.0

    def test_roi(self):
        r = _route(price_buy=100.0, margin=25.0)
        assert abs(r.roi() - 25.0) < 0.01

    def test_roi_zero_price(self):
        r = _route(price_buy=0.0)
        assert r.roi() == 0.0


# ── MultiRoute tests ────────────────────────────────────────────────────────

class TestMultiRoute:
    def test_total_profit(self):
        legs = [_route(margin=10.0, scu_available=100, scu_demand=200),
                _route(margin=5.0, scu_available=50, scu_demand=100)]
        mr = MultiRoute(legs=legs)
        # effective_scu(80) = min(80, 100, 200)=80 and min(80, 50, 100)=50
        assert mr.total_profit(80) == (80 * 10.0 + 50 * 5.0)

    def test_commodity_chain(self):
        legs = [_route(commodity="Laranite"), _route(commodity="Titanium")]
        mr = MultiRoute(legs=legs)
        assert "Laranite" in mr.commodity_chain()
        assert "Titanium" in mr.commodity_chain()

    def test_min_avail(self):
        legs = [_route(scu_available=100), _route(scu_available=50)]
        mr = MultiRoute(legs=legs)
        assert mr.min_avail() == 50

    def test_start_and_end(self):
        legs = [_route(buy_terminal="A", sell_terminal="B"),
                _route(buy_terminal="B", sell_terminal="C")]
        mr = MultiRoute(legs=legs)
        assert mr.start_terminal == "A"
        assert mr.end_terminal == "C"

    def test_empty_legs(self):
        mr = MultiRoute(legs=[])
        assert mr.total_profit(100) == 0
        assert mr.min_avail() == 0
        assert mr.start_terminal == ""
        assert mr.commodity_chain() == ""


# ── apply_filters tests ─────────────────────────────────────────────────────

class TestApplyFilters:
    def test_no_filters(self):
        routes = [_route(), _route(commodity="Titanium")]
        result = apply_filters(routes, FilterState())
        assert len(result) == 2

    def test_filter_by_system(self):
        routes = [
            _route(buy_system="Stanton", sell_system="Stanton"),
            _route(buy_system="Pyro", sell_system="Pyro"),
        ]
        result = apply_filters(routes, FilterState(system="Stanton"))
        assert len(result) == 1

    def test_filter_by_commodity(self):
        routes = [_route(commodity="Laranite"), _route(commodity="Titanium")]
        result = apply_filters(routes, FilterState(commodity="laran"))
        assert len(result) == 1
        assert result[0].commodity == "Laranite"

    def test_filter_by_min_margin(self):
        routes = [_route(margin=5.0), _route(margin=15.0)]
        result = apply_filters(routes, FilterState(min_margin_scu=10.0))
        assert len(result) == 1
        assert result[0].margin == 15.0

    def test_filter_by_search(self):
        routes = [_route(commodity="Laranite"), _route(commodity="Titanium")]
        result = apply_filters(routes, FilterState(search="titan"))
        assert len(result) == 1

    def test_filter_by_buy_system(self):
        routes = [
            _route(buy_system="Stanton"),
            _route(buy_system="Pyro"),
        ]
        result = apply_filters(routes, FilterState(buy_system="pyro"))
        assert len(result) == 1
        assert result[0].buy_system == "Pyro"

    def test_filter_by_min_scu(self):
        routes = [_route(scu_available=100), _route(scu_available=10)]
        result = apply_filters(routes, FilterState(min_scu=50))
        assert len(result) == 1


# ── sort_routes tests ────────────────────────────────────────────────────────

class TestSortRoutes:
    def test_sort_by_commodity(self):
        routes = [_route(commodity="Titanium"), _route(commodity="Astatine")]
        result = sort_routes(routes, "commodity", reverse=False)
        assert result[0].commodity == "Astatine"

    def test_sort_by_profit_descending(self):
        routes = [
            _route(margin=5.0, scu_available=100, scu_demand=100),
            _route(margin=20.0, scu_available=100, scu_demand=100),
        ]
        result = sort_routes(routes, "est_profit", reverse=True, ship_scu=50)
        assert result[0].margin == 20.0

    def test_sort_unknown_column_uses_score(self):
        routes = [_route(score=10), _route(score=50)]
        result = sort_routes(routes, "nonexistent", reverse=True)
        assert result[0].score == 50


# ── find_multi_routes tests ──────────────────────────────────────────────────

class TestFindMultiRoutes:
    def test_empty_input(self):
        assert find_multi_routes([]) == []

    def test_simple_chain(self):
        routes = [
            _route(buy_terminal="A", sell_terminal="B", margin=10.0, scu_available=100, scu_demand=100),
            _route(buy_terminal="B", sell_terminal="C", margin=5.0, scu_available=50, scu_demand=50),
        ]
        multi = find_multi_routes(routes, ship_scu=100)
        assert len(multi) > 0
        # At least one multi-route should have 2 legs
        has_multi = any(m.num_legs >= 2 for m in multi)
        assert has_multi


# ── Helper function tests ────────────────────────────────────────────────────

class TestHelpers:
    def test_profit_tier_high(self):
        assert profit_tier(1000) == "high"
        assert profit_tier(5000) == "high"

    def test_profit_tier_med(self):
        assert profit_tier(300) == "med"
        assert profit_tier(999) == "med"

    def test_profit_tier_low(self):
        assert profit_tier(100) == "low"
        assert profit_tier(0) == "low"

    def test_get_unique_commodities(self):
        routes = [_route(commodity="B"), _route(commodity="A"), _route(commodity="B")]
        result = get_unique_commodities(routes)
        assert result == ["A", "B"]

    def test_fmt_distance_zero(self):
        assert fmt_distance(0) == "\u2014"

    def test_fmt_distance_gm(self):
        assert "Gm" in fmt_distance(500.0)

    def test_fmt_distance_tm(self):
        assert "Tm" in fmt_distance(1500.0)

    def test_fmt_eta_zero(self):
        assert fmt_eta(0) == "\u2014"

    def test_fmt_eta_short_distance(self):
        # Short distance still returns a valid time string
        result = fmt_eta(0.001)
        assert result != "\u2014"
        assert any(c in result for c in ("s", "m", "h"))

    def test_fmt_eta_minutes(self):
        result = fmt_eta(5.0)
        assert "m" in result or "h" in result


# ── route_from_api tests ────────────────────────────────────────────────────

class TestRouteFromApi:
    def test_valid_route(self):
        raw = {
            "commodity_name": "Laranite",
            "terminal_origin": "Admin",
            "star_system_origin": "Stanton",
            "terminal_destination": "TDD",
            "star_system_destination": "Stanton",
            "price_origin": 27.0,
            "price_destination": 35.0,
            "scu_origin": 500,
            "scu_destination": 300,
            "profit_margin": 8.0,
            "score": 42.0,
        }
        r = route_from_api(raw)
        assert r is not None
        assert r.commodity == "Laranite"
        assert r.margin == 8.0

    def test_zero_margin_no_profit_returns_none(self):
        raw = {
            "commodity_name": "Bad",
            "price_origin": 10.0,
            "price_destination": 5.0,
            "profit_margin": 0,
        }
        r = route_from_api(raw)
        assert r is None

    def test_empty_commodity_returns_none(self):
        raw = {"commodity_name": "", "profit_margin": 10}
        r = route_from_api(raw)
        assert r is None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
