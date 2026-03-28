"""Unit tests for CraftApiClient (mocked HTTP)."""

import sys
import os

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from unittest.mock import MagicMock, patch
import pytest

from shared.errors import Result
from data.api_client import CraftApiClient, _q


# ── URL encoding helper ─────────────────────────────────────────────────


class TestUrlEncoding:
    def test_plain_string(self):
        assert _q("Tungsten") == "Tungsten"

    def test_spaces(self):
        assert _q("Hand Mining") == "Hand%20Mining"

    def test_special_chars(self):
        assert _q("A&B=C") == "A%26B%3DC"

    def test_slash(self):
        assert _q("Armour / Combat") == "Armour%20%2F%20Combat"


# ── CraftApiClient ───────────────────────────────────────────────────────


class TestCraftApiClient:
    @pytest.fixture
    def client(self):
        c = CraftApiClient(version="TEST-1.0")
        c._http = MagicMock()
        return c

    def test_fetch_stats_success(self, client):
        client._http.get_json.return_value = Result.success(
            {"totalBlueprints": 100, "uniqueIngredients": 20, "version": "TEST-1.0"}
        )
        r = client.fetch_stats()
        assert r.ok
        assert r.data["totalBlueprints"] == 100
        client._http.get_json.assert_called_once()
        call_url = client._http.get_json.call_args[0][0]
        assert "/stats?" in call_url
        assert "version=TEST-1.0" in call_url

    def test_fetch_stats_failure(self, client):
        client._http.get_json.return_value = Result.failure("timeout", "network")
        r = client.fetch_stats()
        assert not r.ok
        assert r.error == "timeout"

    def test_fetch_filter_hints(self, client):
        client._http.get_json.return_value = Result.success(
            {"location": ["Pyro"], "resource": ["Tungsten"]}
        )
        r = client.fetch_filter_hints()
        assert r.ok
        assert r.data["location"] == ["Pyro"]
        call_url = client._http.get_json.call_args[0][0]
        assert "/filter-hints?" in call_url

    def test_fetch_blueprints_default_params(self, client):
        client._http.get_json.return_value = Result.success(
            {"items": [], "pagination": {"page": 1, "total": 0, "pages": 1, "limit": 50}}
        )
        r = client.fetch_blueprints()
        assert r.ok
        call_url = client._http.get_json.call_args[0][0]
        assert "page=1" in call_url
        assert "limit=50" in call_url
        assert "search=" in call_url

    def test_fetch_blueprints_with_filters(self, client):
        client._http.get_json.return_value = Result.success(
            {"items": [{"id": 1, "name": "T"}], "pagination": {"page": 1, "total": 1, "pages": 1, "limit": 50}}
        )
        r = client.fetch_blueprints(
            page=2, search="test", ownable=True,
            resource="Tungsten", mission_type="Hand Mining",
            location="Pyro / Bloom", contractor="BHG",
            category="Weapons / Sniper",
        )
        assert r.ok
        call_url = client._http.get_json.call_args[0][0]
        assert "page=2" in call_url
        assert "search=test" in call_url
        assert "ownable=1" in call_url
        assert "resource=Tungsten" in call_url
        assert "mission_type=Hand%20Mining" in call_url
        assert "location=Pyro%20%2F%20Bloom" in call_url
        assert "contractor=BHG" in call_url
        assert "category=Weapons%20%2F%20Sniper" in call_url

    def test_fetch_blueprints_ownable_false(self, client):
        client._http.get_json.return_value = Result.success(
            {"items": [], "pagination": {}}
        )
        client.fetch_blueprints(ownable=False)
        call_url = client._http.get_json.call_args[0][0]
        assert "ownable=0" in call_url

    def test_fetch_blueprints_ownable_none(self, client):
        client._http.get_json.return_value = Result.success(
            {"items": [], "pagination": {}}
        )
        client.fetch_blueprints(ownable=None)
        call_url = client._http.get_json.call_args[0][0]
        assert "ownable" not in call_url

    def test_fetch_blueprint_detail(self, client):
        client._http.get_json.return_value = Result.success(
            {"id": 1017, "name": "A03 Sniper Rifle"}
        )
        r = client.fetch_blueprint_detail(1017)
        assert r.ok
        assert r.data["name"] == "A03 Sniper Rifle"
        call_url = client._http.get_json.call_args[0][0]
        assert "/blueprints/1017?" in call_url
