"""Tests for shared.cache_manager — DiskCache with TTL, versioning, and validation."""

import os, sys
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
import shared.path_setup
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

import json
import time

import pytest

from shared.cache_manager import DiskCache


# ── Helpers ───────────────────────────────────────────────────────────


def _write_json(path, obj):
    """Write a Python object as JSON to *path*."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ── Tests ─────────────────────────────────────────────────────────────


class TestLoadMissing:
    def test_missing_file_returns_cache_miss(self, tmp_path):
        cache = DiskCache(str(tmp_path / "nope.json"))
        r = cache.load(ttl=3600)
        assert r.ok is False
        assert r.error_type == "cache_miss"


class TestLoadCorrupt:
    def test_corrupt_json_returns_cache_corrupt(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as fh:
            fh.write("{not valid json!!")
        cache = DiskCache(path)
        r = cache.load(ttl=3600)
        assert r.ok is False
        assert r.error_type == "cache_corrupt"


class TestLoadVersionMismatch:
    def test_wrong_version_returns_cache_version(self, tmp_path):
        path = str(tmp_path / "v.json")
        _write_json(path, {"version": 99, "timestamp": time.time()})
        cache = DiskCache(path, cache_version=1)
        r = cache.load(ttl=3600)
        assert r.ok is False
        assert r.error_type == "cache_version"


class TestLoadExpired:
    def test_expired_ttl_returns_cache_expired(self, tmp_path):
        path = str(tmp_path / "old.json")
        _write_json(path, {"version": 1, "timestamp": time.time() - 7200})
        cache = DiskCache(path, cache_version=1)
        r = cache.load(ttl=3600)
        assert r.ok is False
        assert r.error_type == "cache_expired"


class TestLoadValid:
    def test_valid_cache_returns_success(self, tmp_path):
        path = str(tmp_path / "ok.json")
        payload = {"version": 1, "timestamp": time.time(), "items": [1, 2]}
        _write_json(path, payload)
        cache = DiskCache(path, cache_version=1)
        r = cache.load(ttl=3600)
        assert r.ok is True
        assert r.data["items"] == [1, 2]


class TestSave:
    def test_creates_directory_if_missing(self, tmp_path):
        nested = tmp_path / "a" / "b"
        path = str(nested / "cache.json")
        cache = DiskCache(path, cache_version=1)
        r = cache.save({"foo": "bar"})
        # save returns Result(data=None) which has ok=False by design
        assert r.error is None
        assert os.path.isfile(path)

    def test_stamps_version_and_timestamp(self, tmp_path):
        path = str(tmp_path / "stamp.json")
        cache = DiskCache(path, cache_version=5)
        before = time.time()
        cache.save({"x": 1})
        after = time.time()
        stored = _read_json(path)
        assert stored["version"] == 5
        assert stored["_cache_version"] == 5
        assert before <= stored["timestamp"] <= after + 1

    def test_overwrites_existing_cache(self, tmp_path):
        path = str(tmp_path / "overwrite.json")
        cache = DiskCache(path, cache_version=1)
        cache.save({"round": 1})
        cache.save({"round": 2})
        stored = _read_json(path)
        assert stored["round"] == 2


class TestSaveLoadRoundTrip:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "rt.json")
        cache = DiskCache(path, cache_version=3)
        cache.save({"stuff": [10, 20]})
        r = cache.load(ttl=3600)
        assert r.ok is True
        assert r.data["stuff"] == [10, 20]
        assert r.data["version"] == 3


class TestDelete:
    def test_removes_file(self, tmp_path):
        path = str(tmp_path / "del.json")
        cache = DiskCache(path, cache_version=1)
        cache.save({"a": 1})
        assert os.path.isfile(path)
        cache.delete()
        assert not os.path.exists(path)

    def test_missing_file_no_error(self, tmp_path):
        path = str(tmp_path / "ghost.json")
        cache = DiskCache(path)
        cache.delete()  # should not raise


class TestPathProperty:
    def test_returns_cache_file(self, tmp_path):
        path = str(tmp_path / "p.json")
        cache = DiskCache(path)
        assert cache.path == path


class TestValidation:
    def test_validator_passes(self, tmp_path):
        path = str(tmp_path / "vpass.json")
        cache = DiskCache(path, cache_version=1, validate=lambda d: [])
        cache.save({"key": "val"})
        r = cache.load(ttl=3600)
        assert r.ok is True

    def test_validator_fails_returns_cache_schema(self, tmp_path):
        path = str(tmp_path / "vfail.json")

        def bad_validator(data):
            return ["missing field X"]

        cache = DiskCache(path, cache_version=1, validate=bad_validator)
        cache.save({"key": "val"})
        r = cache.load(ttl=3600)
        assert r.ok is False
        assert r.error_type == "cache_schema"
        assert "missing field X" in r.error

    def test_validator_multiple_errors(self, tmp_path):
        path = str(tmp_path / "vmulti.json")

        def multi_validator(data):
            return ["err1", "err2"]

        cache = DiskCache(path, cache_version=1, validate=multi_validator)
        cache.save({"key": "val"})
        r = cache.load(ttl=3600)
        assert r.ok is False
        assert "err1" in r.error
        assert "err2" in r.error
