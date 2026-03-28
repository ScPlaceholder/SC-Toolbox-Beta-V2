"""Versioned TTL-based caching for erkul and fleetyards data.

Uses orjson for fast JSON parsing (5-10x faster than stdlib json on large
files like the 19.8 MB erkul cache).  Falls back to stdlib json if orjson
is not installed.
"""
import logging
import os
import time

_log = logging.getLogger(__name__)

try:
    import orjson as _json_fast

    def _loads(data: bytes):
        return _json_fast.loads(data)

    def _dumps(obj) -> bytes:
        return _json_fast.dumps(obj)

    _JSON_LIB = "orjson"
except ImportError:
    import json as _json_stdlib
    _log.info("orjson not available, falling back to stdlib json (slower cache loads)")

    def _loads(data: bytes):
        return _json_stdlib.loads(data)

    def _dumps(obj) -> bytes:
        return _json_stdlib.dumps(obj).encode("utf-8")

    _JSON_LIB = "json"


class DiskCache:
    def __init__(self, path: str, ttl: int, version: int):
        self.path = path
        self.ttl = ttl
        self.version = version
        # Metadata cached from the last successful load() — avoids
        # re-reading/re-parsing the 18 MB file for game_version or freshness.
        self._last_ts: float = 0.0
        self._last_game_version: str = ""
        self._last_version_ok: bool = False

    def load(self, stale_ok: bool = False):
        """Load cached data.

        *stale_ok* — if True, return data even when the TTL has expired
        (version must still match).  Useful for pre-loading at startup so
        the UI can display immediately while a background refresh runs.
        """
        try:
            if not os.path.isfile(self.path):
                _log.debug("Cache file not found: %s", self.path)
                return None
            _log.debug("Cache: opening %s (parser=%s) ...", self.path, _JSON_LIB)
            t0 = time.time()
            with open(self.path, "rb") as f:
                obj = _loads(f.read())
            elapsed = time.time() - t0
            _log.debug("Cache: parse took %.3fs", elapsed)
            if obj.get("version") != self.version:
                _log.debug("Cache: version mismatch (got %s, want %s)",
                           obj.get("version"), self.version)
                return None
            # Cache envelope metadata so load_game_version / is_fresh
            # never need to re-read the file.
            self._last_ts = obj.get("ts", 0)
            self._last_game_version = obj.get("game_version", "")
            self._last_version_ok = True
            age = time.time() - self._last_ts
            if age >= self.ttl:
                if stale_ok:
                    _log.debug("Cache: stale but usable (age=%.0fs, ttl=%ds)", age, self.ttl)
                    return obj.get("data", {})
                _log.debug("Cache: expired (age=%.0fs, ttl=%ds)", age, self.ttl)
                return None
            _log.debug("Cache: valid (age=%.0fs, ttl=%ds)", age, self.ttl)
            return obj.get("data", {})
        except (OSError, ValueError, KeyError, TypeError) as e:
            _log.warning("Cache load failed (%s): %s", self.path, e)
            return None

    def is_fresh(self) -> bool:
        """Return True if the last load() found valid, non-expired data."""
        if not self._last_version_ok:
            return False
        age = time.time() - self._last_ts
        return age < self.ttl

    def save(self, data: dict, game_version: str = ""):
        try:
            payload = _dumps({"ts": time.time(), "version": self.version,
                              "game_version": game_version, "data": data})
            with open(self.path, "wb") as f:
                f.write(payload)
        except OSError as e:
            _log.warning("Cache save failed (%s): %s", self.path, e)

    def load_game_version(self) -> str:
        """Return game version from the last successful load() (no file I/O)."""
        return self._last_game_version

    def invalidate(self):
        try:
            if os.path.isfile(self.path):
                os.remove(self.path)
        except OSError as e:
            _log.warning("Cache invalidate failed (%s): %s", self.path, e)


class FleetyardsCache:
    def __init__(self, path: str, ttl: int):
        self.path = path
        self.ttl = ttl
        self._mem = {}
        self._disk_loaded = False

    def _load_disk(self) -> dict:
        try:
            if os.path.isfile(self.path):
                with open(self.path, "rb") as f:
                    return _loads(f.read())
        except (OSError, ValueError) as e:
            _log.warning("FY disk cache load failed: %s", e)
        return {}

    def _save_disk(self):
        try:
            with open(self.path, "wb") as f:
                f.write(_dumps(self._mem))
        except OSError as e:
            _log.warning("FY disk cache save failed: %s", e)

    def get(self, slug: str):
        mem = self._mem.get(slug)
        if mem and time.time() - mem.get("ts", 0) < self.ttl:
            return mem["hardpoints"]
        if not self._disk_loaded:
            self._mem = self._load_disk()
            self._disk_loaded = True
            mem = self._mem.get(slug)
            if mem and time.time() - mem.get("ts", 0) < self.ttl:
                return mem["hardpoints"]
        return None

    def put(self, slug: str, hardpoints: list):
        self._mem[slug] = {"ts": time.time(), "hardpoints": hardpoints}
        self._save_disk()

    @property
    def mem(self) -> dict:
        return self._mem
