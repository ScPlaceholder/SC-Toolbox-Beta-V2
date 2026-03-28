"""Tests for data.cache — atomic cache with version and TTL validation."""

import json
import os
import sys
import time

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub config before importing cache
import types
config_mod = types.ModuleType("config")
config_mod.CACHE_TTL = 3600
config_mod.CACHE_VERSION = 1
config_mod.HIDDEN_LOCATIONS = frozenset()
config_mod.MINING_GROUP_TYPES = {}
sys.modules["config"] = config_mod

from data.cache import load_cache, save_cache, version_cache_path


# ── version_cache_path tests ─────────────────────────────────────────────────

class TestVersionCachePath:
    def test_dots_and_dashes_replaced(self):
        path = version_cache_path("4.0.2-live.merged")
        assert "4_0_2_live_merged" in path
        assert path.endswith(".json")


# ── save_cache + load_cache round-trip ───────────────────────────────────────

class TestCacheRoundTrip:
    def test_save_and_load(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        data = {"contracts": [{"title": "Test"}]}
        save_cache(data, path)

        result = load_cache(path)
        assert result is not None
        assert result["contracts"] == [{"title": "Test"}]
        assert result["_cache_version"] == 1

    def test_load_nonexistent_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "nope.json")
        assert load_cache(path) is None

    def test_load_expired_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        data = {
            "_cache_version": 1,
            "_ts": time.time() - 7200,
            "contracts": [],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        assert load_cache(path) is None

    def test_load_wrong_version_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        data = {
            "_cache_version": 999,
            "_ts": time.time(),
            "contracts": [],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        assert load_cache(path) is None

    def test_load_corrupt_json_returns_none(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        with open(path, "w") as f:
            f.write("not json {{{")

        assert load_cache(path) is None

    def test_save_overwrites(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        save_cache({"v": 1}, path)
        save_cache({"v": 2}, path)
        result = load_cache(path)
        assert result is not None
        assert result["v"] == 2

    def test_save_atomic_creates_file(self, tmp_path):
        path = os.path.join(tmp_path, "cache.json")
        save_cache({"data": True}, path)
        assert os.path.exists(path)
        # No leftover .tmp files
        tmp_files = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
        assert len(tmp_files) == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
