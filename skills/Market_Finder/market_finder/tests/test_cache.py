"""Tests for market_finder.cache — CacheManager with schema validation."""

import json
import os
import sys
import time

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))

from market_finder.cache import CacheManager, _validate


# ── Combined validation tests ─────────────────────────────────────────────────

class TestValidate:
    def test_valid_data(self):
        data = {
            "version": 3,
            "timestamp": time.time(),
            "items": [{"id": 1, "name": "Laser"}],
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        assert _validate(data) == []

    def test_missing_key(self):
        data = {"version": 3, "timestamp": time.time()}
        errors = _validate(data)
        assert len(errors) > 0
        assert any("Missing key" in e for e in errors)

    def test_wrong_type(self):
        data = {
            "version": "3",  # should be int
            "timestamp": time.time(),
            "items": [],
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        errors = _validate(data)
        assert len(errors) > 0
        assert any("version" in e for e in errors)

    def test_missing_item_field(self):
        data = {
            "version": 3,
            "timestamp": time.time(),
            "items": [{"id": 1}],  # missing "name"
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        errors = _validate(data)
        assert any("missing" in e.lower() for e in errors)

    def test_non_dict_item(self):
        data = {
            "version": 3,
            "timestamp": time.time(),
            "items": ["not a dict"],
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        errors = _validate(data)
        assert any("not a dict" in e for e in errors)

    def test_empty_items_valid(self):
        data = {
            "version": 3,
            "timestamp": time.time(),
            "items": [],
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        assert _validate(data) == []


# ── CacheManager tests ───────────────────────────────────────────────────────

class TestCacheManager:
    def _make_cache(self, tmp_path, version=3):
        path = os.path.join(tmp_path, "cache.json")
        return CacheManager(cache_file=path, cache_version=version)

    def _valid_data(self):
        return {
            "items": [{"id": 1, "name": "Test"}],
            "vehicles": [{"id": 1, "name": "Aurora"}],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {1: {"id": 1, "name": "TDD"}},
        }

    def test_save_and_load(self, tmp_path):
        cm = self._make_cache(tmp_path)
        save_result = cm.save(self._valid_data())
        assert save_result.ok or save_result.error is None

        load_result = cm.load(ttl=3600)
        assert load_result.ok
        assert load_result.data["items"] == [{"id": 1, "name": "Test"}]

    def test_load_nonexistent_returns_miss(self, tmp_path):
        cm = self._make_cache(tmp_path)
        result = cm.load()
        assert not result.ok
        assert result.error_type == "cache_miss"

    def test_load_expired_returns_expired(self, tmp_path):
        cm = self._make_cache(tmp_path)
        path = os.path.join(tmp_path, "cache.json")
        payload = {
            "version": 3,
            "timestamp": time.time() - 7200,
            "items": [{"id": 1, "name": "Old"}],
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        result = cm.load(ttl=3600)
        assert not result.ok
        assert result.error_type == "cache_expired"

    def test_load_wrong_version_no_migration(self, tmp_path):
        cm = self._make_cache(tmp_path, version=5)
        path = os.path.join(tmp_path, "cache.json")
        payload = {
            "version": 1,
            "timestamp": time.time(),
            "items": [],
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        result = cm.load(ttl=3600)
        assert not result.ok
        assert result.error_type == "cache_version"

    def test_load_v2_to_v3_migration(self, tmp_path):
        cm = self._make_cache(tmp_path, version=3)
        path = os.path.join(tmp_path, "cache.json")
        payload = {
            "version": 2,
            "timestamp": time.time(),
            "items": [{"id": 1, "name": "Migrated"}],
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        result = cm.load(ttl=3600)
        assert result.ok
        assert result.data["items"] == [{"id": 1, "name": "Migrated"}]

    def test_load_corrupt_json(self, tmp_path):
        cm = self._make_cache(tmp_path)
        path = os.path.join(tmp_path, "cache.json")
        with open(path, "w") as f:
            f.write("not json {{{{")

        result = cm.load()
        assert not result.ok
        assert result.error_type == "cache_corrupt"

    def test_load_bad_schema(self, tmp_path):
        cm = self._make_cache(tmp_path)
        path = os.path.join(tmp_path, "cache.json")
        payload = {
            "version": 3,
            "timestamp": time.time(),
            "items": "not a list",  # wrong type
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        result = cm.load(ttl=3600)
        assert not result.ok
        assert result.error_type == "cache_schema"

    def test_delete_removes_file(self, tmp_path):
        cm = self._make_cache(tmp_path)
        cm.save(self._valid_data())
        path = os.path.join(tmp_path, "cache.json")
        assert os.path.exists(path)
        cm.delete()
        assert not os.path.exists(path)

    def test_delete_nonexistent_no_error(self, tmp_path):
        cm = self._make_cache(tmp_path)
        cm.delete()  # should not raise

    def test_terminals_normalised_to_int_keys(self, tmp_path):
        cm = self._make_cache(tmp_path)
        data = self._valid_data()
        data["terminals"] = {1: {"id": 1, "name": "TDD"}}
        cm.save(data)

        result = cm.load(ttl=3600)
        assert result.ok
        # After save, keys are stringified; after load, they should be int again
        assert all(isinstance(k, int) for k in result.data["terminals"])

    def test_save_overwrites(self, tmp_path):
        cm = self._make_cache(tmp_path)
        cm.save({"items": [{"id": 1, "name": "First"}], "vehicles": [],
                 "rentals": [], "vehicle_purchases": [], "terminals": {}})
        cm.save({"items": [{"id": 2, "name": "Second"}], "vehicles": [],
                 "rentals": [], "vehicle_purchases": [], "terminals": {}})
        result = cm.load(ttl=3600)
        assert result.ok
        assert result.data["items"][0]["name"] == "Second"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
