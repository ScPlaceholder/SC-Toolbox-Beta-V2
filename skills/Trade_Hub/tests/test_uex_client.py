"""Tests for uex_client — RouteData logic, normalization, and HTTP mocking."""

import os
import sys
from unittest.mock import patch, MagicMock

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uex_client import RouteData, ShipData, UEXClient


# ── RouteData tests ──────────────────────────────────────────────────────────

class TestRouteDataEffectiveScu:
    def test_both_positive_takes_min(self):
        rd = RouteData(scu_available=100, scu_demand=50)
        assert rd.effective_scu() == 50

    def test_both_positive_with_ship_cap(self):
        rd = RouteData(scu_available=100, scu_demand=200)
        assert rd.effective_scu(ship_scu=80) == 80

    def test_ship_cap_smaller_than_both(self):
        rd = RouteData(scu_available=100, scu_demand=200)
        assert rd.effective_scu(ship_scu=50) == 50

    def test_zero_available_returns_zero(self):
        rd = RouteData(scu_available=0, scu_demand=100)
        assert rd.effective_scu() == 0

    def test_only_available_no_demand(self):
        rd = RouteData(scu_available=100, scu_demand=0)
        assert rd.effective_scu() == 100

    def test_ship_scu_zero_ignored(self):
        rd = RouteData(scu_available=50, scu_demand=80)
        assert rd.effective_scu(ship_scu=0) == 50


class TestRouteDataProfit:
    def test_estimated_profit(self):
        rd = RouteData(scu_available=100, scu_demand=200, margin=5.0)
        assert rd.estimated_profit(50) == 250.0

    def test_investment(self):
        rd = RouteData(scu_available=100, scu_demand=200, price_buy=10.0)
        assert rd.investment(50) == 500.0


# ── _normalize_route tests ───────────────────────────────────────────────────

class TestNormalizeRoute:
    def setup_method(self):
        self.client = UEXClient(cache_ttl=0)

    def test_basic_fields(self):
        raw = {
            "commodity_name": "Laranite",
            "star_system_origin": "Stanton",
            "terminal_origin": "Admin Office",
            "star_system_destination": "Pyro",
            "terminal_destination": "Ruin Station",
            "price_origin": 27.0,
            "price_destination": 35.0,
            "scu_origin": 500,
            "scu_destination": 300,
            "profit_margin": 8.0,
            "score": 42.0,
        }
        rd = self.client._normalize_route(raw)
        assert rd.commodity == "Laranite"
        assert rd.buy_system == "Stanton"
        assert rd.sell_system == "Pyro"
        assert rd.price_buy == 27.0
        assert rd.price_sell == 35.0
        assert rd.scu_available == 500
        assert rd.scu_demand == 300
        assert rd.margin == 8.0
        assert rd.score == 42.0

    def test_margin_computed_when_zero(self):
        raw = {
            "commodity_name": "Scrap",
            "price_origin": 10.0,
            "price_destination": 18.0,
            "profit_margin": 0,
        }
        rd = self.client._normalize_route(raw)
        assert rd.margin == 8.0

    def test_margin_pct_computed(self):
        raw = {
            "commodity_name": "Scrap",
            "price_origin": 100.0,
            "price_destination": 125.0,
            "profit_margin": 25.0,
            "profit_margin_percentage": 0,
        }
        rd = self.client._normalize_route(raw)
        assert abs(rd.margin_pct - 25.0) < 0.01

    def test_fallback_field_names(self):
        """When primary field names are absent, fallback names are used."""
        raw = {
            "commodity_name": "Titanium",
            "terminal_name_origin": "TDD Orison",
            "terminal_name_destination": "TDD Lorville",
            "price_buy": 15.0,
            "price_sell": 20.0,
            "scu_buy": 100,
            "scu_sell": 200,
            "margin": 5.0,
        }
        rd = self.client._normalize_route(raw)
        assert rd.buy_terminal == "TDD Orison"
        assert rd.sell_terminal == "TDD Lorville"
        assert rd.price_buy == 15.0
        assert rd.price_sell == 20.0
        assert rd.scu_available == 100
        assert rd.scu_demand == 200

    def test_none_values_default_to_empty(self):
        raw = {
            "commodity_name": None,
            "terminal_origin": None,
            "price_origin": None,
            "scu_origin": None,
        }
        rd = self.client._normalize_route(raw)
        assert rd.commodity == ""
        assert rd.buy_terminal == ""
        assert rd.price_buy == 0.0
        assert rd.scu_available == 0

    def test_empty_dict(self):
        rd = self.client._normalize_route({})
        assert rd.commodity == ""
        assert rd.margin == 0.0


# ── _best_location tests ────────────────────────────────────────────────────

class TestBestLocation:
    def test_outpost_preferred(self):
        r = {"outpost_origin": "Benson Mining", "city_origin": "Lorville"}
        assert UEXClient._best_location(r, "origin") == "Benson Mining"

    def test_city_when_no_outpost(self):
        r = {"city_origin": "Area 18", "planet_origin": "ArcCorp"}
        assert UEXClient._best_location(r, "origin") == "Area 18"

    def test_empty_when_all_blank(self):
        assert UEXClient._best_location({}, "destination") == ""

    def test_strips_whitespace(self):
        r = {"outpost_origin": "  Benson Mining  "}
        assert UEXClient._best_location(r, "origin") == "Benson Mining"


# ── HTTP mocking tests ──────────────────────────────────────────────────────

class TestFetchRoutes:
    def test_fetch_routes_parses_response(self):
        from shared.errors import Result
        mock_body = {
            "data": [
                {
                    "commodity_name": "Laranite",
                    "star_system_origin": "Stanton",
                    "terminal_origin": "Admin",
                    "star_system_destination": "Stanton",
                    "terminal_destination": "TDD",
                    "price_origin": 27.0,
                    "price_destination": 35.0,
                    "scu_origin": 500,
                    "scu_destination": 300,
                    "profit_margin": 8.0,
                    "score": 50.0,
                },
            ],
        }

        client = UEXClient()
        with patch.object(client._client, "get_json", return_value=Result.success(mock_body)):
            routes = client._fetch_routes()

        assert len(routes) == 1
        assert routes[0].commodity == "Laranite"
        assert routes[0].margin == 8.0

    def test_fetch_routes_filters_zero_margin(self):
        from shared.errors import Result
        mock_body = {
            "data": [
                {"commodity_name": "Bad", "profit_margin": 0, "price_origin": 10, "price_destination": 5},
            ],
        }

        client = UEXClient()
        with patch.object(client._client, "get_json", return_value=Result.success(mock_body)):
            routes = client._fetch_routes()

        assert len(routes) == 0

    def test_fetch_routes_network_error_returns_stale(self):
        from shared.errors import Result
        client = UEXClient()
        client._cache_routes = [RouteData(commodity="Stale")]
        with patch.object(client._client, "get_json", return_value=Result.failure("down", "network")):
            routes = client._fetch_routes()

        assert len(routes) == 1
        assert routes[0].commodity == "Stale"


class TestFetchShips:
    def test_parses_ships(self):
        from shared.errors import Result
        mock_body = {
            "data": [
                {"name": "Caterpillar", "scu": 576, "manufacturer_name": "Drake"},
                {"name": "Aurora", "scu": 0},  # should be excluded
            ],
        }

        client = UEXClient()
        with patch.object(client._client, "get_json", return_value=Result.success(mock_body)):
            ships = client._fetch_ships()

        assert len(ships) == 1
        assert ships[0].name == "Caterpillar"
        assert ships[0].scu == 576


# ── Cache behaviour ──────────────────────────────────────────────────────────

class TestCacheTTL:
    def test_returns_cached_within_ttl(self):
        client = UEXClient(cache_ttl=300)
        client._cache_routes = [RouteData(commodity="Cached")]
        import time
        client._cache_routes_time = time.time()
        routes = client.get_routes()
        assert routes[0].commodity == "Cached"

    def test_context_manager(self):
        with UEXClient() as client:
            assert client is not None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
