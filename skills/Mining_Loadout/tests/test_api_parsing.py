"""Tests for API response parsing helpers."""
import os
import sys
import pytest

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

from services.api_client import (
    _build_attr_map,
    _build_price_map,
    _float_attr,
    _parse_power,
    _str_attr,
    _validate_item_record,
)


class TestParsepower:
    def test_range(self):
        assert _parse_power("480-2400") == (480.0, 2400.0)

    def test_range_with_spaces(self):
        assert _parse_power("480 - 2400") == (480.0, 2400.0)

    def test_single_value(self):
        assert _parse_power("1200") == (1200.0, 1200.0)

    def test_decimal(self):
        assert _parse_power("1.5-3.5") == (1.5, 3.5)

    def test_empty(self):
        assert _parse_power("") == (0.0, 0.0)

    def test_none(self):
        assert _parse_power(None) == (0.0, 0.0)

    def test_garbage(self):
        assert _parse_power("abc") == (0.0, 0.0)


class TestFloatAttr:
    def test_basic(self):
        attrs = {1: {"Power": "25.5"}}
        assert _float_attr(attrs, 1, "Power") == 25.5

    def test_with_percent(self):
        attrs = {1: {"Resistance": "+25%"}}
        assert _float_attr(attrs, 1, "Resistance") == 25.0

    def test_with_comma(self):
        attrs = {1: {"Range": "1,500"}}
        assert _float_attr(attrs, 1, "Range") == 1500.0

    def test_empty(self):
        attrs = {1: {"Power": ""}}
        assert _float_attr(attrs, 1, "Power") is None

    def test_missing(self):
        assert _float_attr({}, 1, "Power") is None

    def test_garbage(self):
        attrs = {1: {"Power": "abc"}}
        assert _float_attr(attrs, 1, "Power") is None


class TestStrAttr:
    def test_basic(self):
        attrs = {1: {"Name": "  Test  "}}
        assert _str_attr(attrs, 1, "Name") == "Test"

    def test_missing(self):
        assert _str_attr({}, 1, "Name") == ""


class TestBuildAttrMap:
    def test_groups_by_item(self):
        raw = [
            {"id_item": 1, "attribute_name": "Power", "value": "100"},
            {"id_item": 1, "attribute_name": "Range", "value": "200"},
            {"id_item": 2, "attribute_name": "Power", "value": "300"},
        ]
        result = _build_attr_map(raw)
        assert result[1]["Power"] == "100"
        assert result[1]["Range"] == "200"
        assert result[2]["Power"] == "300"

    def test_empty(self):
        assert _build_attr_map([]) == {}


class TestBuildPriceMap:
    def test_min_price(self):
        raw = [
            {"id_item": 1, "price_buy": 100},
            {"id_item": 1, "price_buy": 80},
            {"id_item": 1, "price_buy": 120},
        ]
        result = _build_price_map(raw)
        assert result[1] == 80.0

    def test_zero_price_excluded(self):
        raw = [{"id_item": 1, "price_buy": 0}]
        result = _build_price_map(raw)
        assert 1 not in result

    def test_empty(self):
        assert _build_price_map([]) == {}


class TestValidateItemRecord:
    def test_valid(self):
        assert _validate_item_record({"id": 1, "name": "Test"}, "laser") is True

    def test_no_id(self):
        assert _validate_item_record({"name": "Test"}, "laser") is False

    def test_no_name(self):
        assert _validate_item_record({"id": 1}, "laser") is False

    def test_not_dict(self):
        assert _validate_item_record("bad", "laser") is False
