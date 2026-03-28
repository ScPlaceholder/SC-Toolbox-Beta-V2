"""Tests for market_finder.service — _TTLCache and DataService logic."""

import os
import sys
import time
from unittest.mock import MagicMock, patch

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))

from market_finder.errors import Result
from market_finder.service import _TTLCache, DataService


# ── _TTLCache tests ──────────────────────────────────────────────────────────

class TestTTLCache:
    def test_put_and_get(self):
        cache = _TTLCache(max_size=10, ttl=3600)
        cache.put(1, [{"price": 100}])
        assert cache.get(1) == [{"price": 100}]

    def test_get_missing_returns_none(self):
        cache = _TTLCache(max_size=10, ttl=3600)
        assert cache.get(999) is None

    def test_expired_entry_returns_none(self):
        cache = _TTLCache(max_size=10, ttl=1)
        cache.put(1, [{"price": 100}])
        # Manually backdate the entry
        with cache._lock:
            ts, val = cache._data[1]
            cache._data[1] = (ts - 10, val)
        assert cache.get(1) is None

    def test_lru_eviction(self):
        cache = _TTLCache(max_size=2, ttl=3600)
        cache.put(1, [{"a": 1}])
        cache.put(2, [{"a": 2}])
        cache.put(3, [{"a": 3}])  # should evict key 1
        assert cache.get(1) is None
        assert cache.get(2) is not None
        assert cache.get(3) is not None

    def test_lru_access_refreshes_order(self):
        cache = _TTLCache(max_size=2, ttl=3600)
        cache.put(1, [{"a": 1}])
        cache.put(2, [{"a": 2}])
        cache.get(1)  # touch key 1, making key 2 the LRU
        cache.put(3, [{"a": 3}])  # should evict key 2
        assert cache.get(1) is not None
        assert cache.get(2) is None
        assert cache.get(3) is not None

    def test_clear(self):
        cache = _TTLCache(max_size=10, ttl=3600)
        cache.put(1, [{"a": 1}])
        cache.put(2, [{"a": 2}])
        cache.clear()
        assert cache.get(1) is None
        assert cache.get(2) is None

    def test_put_overwrites_existing(self):
        cache = _TTLCache(max_size=10, ttl=3600)
        cache.put(1, [{"old": True}])
        cache.put(1, [{"new": True}])
        assert cache.get(1) == [{"new": True}]


# ── DataService tests ────────────────────────────────────────────────────────

class TestDataService:
    def _make_service(self):
        api = MagicMock()
        cache_mgr = MagicMock()
        return DataService(api=api, cache_mgr=cache_mgr)

    def test_initial_state(self):
        svc = self._make_service()
        assert not svc.is_loaded()
        assert svc.get_status() == ""
        assert svc.get_error() == ""

    def test_fetch_all_from_cache(self):
        svc = self._make_service()
        svc._cache.load.return_value = Result.success({
            "items": [{"id": 1, "name": "Test", "category": "Armor", "section": "Armor",
                        "company_name": "TestCo"}],
            "vehicles": [],
            "rentals": [],
            "vehicle_purchases": [],
            "terminals": {},
        })
        svc.fetch_all(force=False)
        assert svc.is_loaded()
        assert len(svc.items) == 1
        assert svc.items[0]["name"] == "Test"

    def test_fetch_all_cache_miss_falls_through(self):
        svc = self._make_service()
        svc._cache.load.return_value = Result.failure("miss", "cache_miss")
        # Mock API calls to return minimal data
        svc._api.get.return_value = Result.success([])
        svc._cache.save.return_value = Result.success(None)

        svc.fetch_all(force=False)
        assert svc.is_loaded()

    def test_cancel_sets_event(self):
        svc = self._make_service()
        svc.cancel()
        assert svc._check_cancelled()

    def test_index_rentals(self):
        svc = self._make_service()
        svc.rentals = [
            {"id_vehicle": 1, "price": 100},
            {"id_vehicle": 1, "price": 200},
            {"id_vehicle": 2, "price": 300},
        ]
        svc._index_rentals()
        assert len(svc.rental_by_vehicle[1]) == 2
        assert len(svc.rental_by_vehicle[2]) == 1

    def test_index_purchases(self):
        svc = self._make_service()
        svc.vehicle_purchases = [
            {"id_vehicle": 10, "price": 50000},
        ]
        svc._index_purchases()
        assert 10 in svc.purchase_by_vehicle

    def test_index_items_by_tab(self):
        svc = self._make_service()
        svc.items = [
            {"id": 1, "name": "Helmet", "category": "Armor", "section": "Armor",
             "company_name": "TestCo"},
            {"id": 2, "name": "Laser", "category": "Ship Weapons", "section": "Vehicle Weapons",
             "company_name": "KLWE"},
        ]
        svc._index_items_by_tab()
        assert "All" in svc.items_by_tab
        assert len(svc.items_by_tab["All"]) == 2
        assert "Armor" in svc.items_by_tab
        assert "Ship Weapons" in svc.items_by_tab

    def test_search_index_built(self):
        svc = self._make_service()
        svc.items = [
            {"id": 42, "name": "Arclight Pistol", "category": "Weapons",
             "section": "Personal Weapons", "company_name": "Gemini"},
        ]
        svc._index_items_by_tab()
        assert 42 in svc.search_index
        assert "arclight" in svc.search_index[42]
        assert "gemini" in svc.search_index[42]

    def test_fetch_item_prices_cached(self):
        svc = self._make_service()
        svc._price_cache.put(42, [{"price": 100}])
        result = svc.fetch_item_prices(42)
        assert result.ok
        assert result.data == [{"price": 100}]

    def test_fetch_item_prices_from_api(self):
        svc = self._make_service()
        svc._api.get.return_value = Result.success([{"price": 200}])
        result = svc.fetch_item_prices(99)
        assert result.ok
        assert result.data == [{"price": 200}]
        # Should be cached now
        assert svc._price_cache.get(99) == [{"price": 200}]

    def test_clear_cache(self):
        svc = self._make_service()
        svc._price_cache.put(1, [{"x": 1}])
        svc.clear_cache()
        assert svc._price_cache.get(1) is None
        svc._cache.delete.assert_called_once()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
