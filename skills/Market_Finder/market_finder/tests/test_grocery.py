"""Tests for market_finder.grocery — pure buy-location normalization."""

import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))

from market_finder.grocery import buy_locations, format_location, format_price


# ── format_location ──────────────────────────────────────────────────────────

class TestFormatLocation:
    def test_joins_populated_fields_in_order(self):
        price = {
            "star_system_name": "Stanton",
            "planet_name": "Hurston",
            "city_name": "Lorville",
        }
        assert format_location(price) == "Stanton > Hurston > Lorville"

    def test_skips_empty_fields(self):
        price = {"star_system_name": "Stanton", "planet_name": "", "city_name": None}
        assert format_location(price) == "Stanton"

    def test_no_location_returns_empty(self):
        assert format_location({}) == ""


# ── format_price ─────────────────────────────────────────────────────────────

class TestFormatPrice:
    def test_thousands_separator_and_suffix(self):
        assert format_price(1234) == "1,234 aUEC"

    def test_zero_and_negative_dashed(self):
        assert format_price(0) == "—"
        assert format_price(-5) == "—"

    def test_non_numeric_dashed(self):
        assert format_price(None) == "—"
        assert format_price("n/a") == "—"


# ── buy_locations ────────────────────────────────────────────────────────────

class TestBuyLocations:
    def test_sorted_cheapest_first(self):
        prices = [
            {"terminal_name": "B", "price_buy": 300, "city_name": "Area18"},
            {"terminal_name": "A", "price_buy": 100, "city_name": "Lorville"},
            {"terminal_name": "C", "price_buy": 200, "city_name": "NB"},
        ]
        rows = buy_locations(prices)
        assert [r["price"] for r in rows] == [100, 200, 300]
        assert rows[0]["terminal"] == "A"
        assert rows[0]["location"] == "Lorville"

    def test_drops_rows_without_buy_price(self):
        prices = [
            {"terminal_name": "Sell Only", "price_sell": 500, "price_buy": 0},
            {"terminal_name": "Buyable", "price_buy": 50},
            {"terminal_name": "Missing", "price_sell": 9},
        ]
        rows = buy_locations(prices)
        assert len(rows) == 1
        assert rows[0]["terminal"] == "Buyable"

    def test_missing_terminal_name_falls_back(self):
        rows = buy_locations([{"price_buy": 10}])
        assert rows[0]["terminal"] == "Unknown"
        assert rows[0]["location"] == ""

    def test_empty_input(self):
        assert buy_locations([]) == []

    def test_non_numeric_price_skipped(self):
        rows = buy_locations([{"terminal_name": "Bad", "price_buy": "n/a"}])
        assert rows == []
