"""Tests for DPS_Calculator.data.cache — DiskCache and FleetyardsCache."""

import json
import os
import sys
import time

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.cache import DiskCache, FleetyardsCache


class TestDiskCache:
    def test_save_and_load(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        cache = DiskCache(path, ttl=3600, version=1)
        cache.save({"ships": ["aurora"]}, game_version="4.0")
        result = cache.load()
        assert result == {"ships": ["aurora"]}

    def test_load_nonexistent_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "nope.json")
        cache = DiskCache(path, ttl=3600, version=1)
        assert cache.load() is None

    def test_load_expired_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        cache = DiskCache(path, ttl=3600, version=1)
        # Write cache with old timestamp
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time() - 7200, "version": 1, "data": {"x": 1}}, f)
        assert cache.load() is None

    def test_load_wrong_version_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        cache = DiskCache(path, ttl=3600, version=2)
        # Write with version 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "version": 1, "data": {"x": 1}}, f)
        assert cache.load() is None

    def test_load_corrupt_json_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        cache = DiskCache(path, ttl=3600, version=1)
        with open(path, "w") as f:
            f.write("not json at all {{{")
        assert cache.load() is None

    def test_load_game_version(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        cache = DiskCache(path, ttl=3600, version=1)
        cache.save({"ships": []}, game_version="4.0.2")
        assert cache.load_game_version() == "4.0.2"

    def test_load_game_version_nonexistent(self, tmp_path):
        path = os.path.join(tmp_path, "nope.json")
        cache = DiskCache(path, ttl=3600, version=1)
        assert cache.load_game_version() == ""

    def test_invalidate_removes_file(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        cache = DiskCache(path, ttl=3600, version=1)
        cache.save({"data": True})
        assert os.path.isfile(path)
        cache.invalidate()
        assert not os.path.isfile(path)

    def test_invalidate_nonexistent_no_error(self, tmp_path):
        path = os.path.join(tmp_path, "nope.json")
        cache = DiskCache(path, ttl=3600, version=1)
        cache.invalidate()  # Should not raise

    def test_save_overwrites_previous(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        cache = DiskCache(path, ttl=3600, version=1)
        cache.save({"first": True})
        cache.save({"second": True})
        result = cache.load()
        assert result == {"second": True}


class TestFleetyardsCache:
    def test_put_and_get(self, tmp_path):
        path = os.path.join(tmp_path, "fy.json")
        cache = FleetyardsCache(path, ttl=3600)
        cache.put("aurora-mr", [{"name": "S1 gun"}])
        result = cache.get("aurora-mr")
        assert result == [{"name": "S1 gun"}]

    def test_get_nonexistent_slug(self, tmp_path):
        path = os.path.join(tmp_path, "fy.json")
        cache = FleetyardsCache(path, ttl=3600)
        assert cache.get("not-a-ship") is None

    def test_expired_entry_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "fy.json")
        cache = FleetyardsCache(path, ttl=1)
        # Write with old timestamp directly
        cache._mem["test-ship"] = {"ts": time.time() - 10, "hardpoints": []}
        cache._disk_loaded = True
        assert cache.get("test-ship") is None

    def test_disk_persistence(self, tmp_path):
        path = os.path.join(tmp_path, "fy.json")
        cache1 = FleetyardsCache(path, ttl=3600)
        cache1.put("gladius", [{"name": "S3 laser"}])

        # New cache instance reads from disk
        cache2 = FleetyardsCache(path, ttl=3600)
        result = cache2.get("gladius")
        assert result == [{"name": "S3 laser"}]

    def test_corrupt_disk_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "fy.json")
        with open(path, "w") as f:
            f.write("broken json {{{")
        cache = FleetyardsCache(path, ttl=3600)
        assert cache.get("anything") is None

    def test_mem_property(self, tmp_path):
        path = os.path.join(tmp_path, "fy.json")
        cache = FleetyardsCache(path, ttl=3600)
        cache.put("avenger", [{"name": "S2"}])
        assert "avenger" in cache.mem

    def test_multiple_slugs(self, tmp_path):
        path = os.path.join(tmp_path, "fy.json")
        cache = FleetyardsCache(path, ttl=3600)
        cache.put("aurora", [{"s": 1}])
        cache.put("gladius", [{"s": 3}])
        cache.put("hammerhead", [{"s": 5}])
        assert cache.get("aurora") == [{"s": 1}]
        assert cache.get("gladius") == [{"s": 3}]
        assert cache.get("hammerhead") == [{"s": 5}]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
