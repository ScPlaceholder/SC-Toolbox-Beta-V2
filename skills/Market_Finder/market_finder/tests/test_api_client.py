"""Tests for market_finder.api_client — UexApiClient retry and error handling."""

import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))

from market_finder.api_client import UexApiClient


class TestUexApiClient:
    def _make_client(self, **kwargs):
        defaults = {
            "base_url": "https://test.example.com",
            "timeout": 5,
            "max_retries": 1,
            "backoff_base": 0.0,  # no sleep in tests
        }
        defaults.update(kwargs)
        return UexApiClient(**defaults)

    def test_successful_response(self):
        client = self._make_client()
        body = json.dumps({"status": "ok", "data": [{"id": 1}]}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client.get("items")

        assert result.ok
        assert result.data == [{"id": 1}]

    def test_null_data_returns_empty_list(self):
        client = self._make_client()
        body = json.dumps({"status": "ok", "data": None}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client.get("empty_category")

        assert result.ok
        assert result.data == []

    def test_network_error(self):
        client = self._make_client()
        with patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError("Connection refused")):
            result = client.get("items")

        assert not result.ok
        assert result.error_type == "network"

    def test_http_404_not_retried(self):
        client = self._make_client(max_retries=3)
        call_count = 0

        def _raise_404(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(
                "https://test.example.com/items", 404, "Not Found", {}, None
            )

        with patch("urllib.request.urlopen", side_effect=_raise_404):
            result = client.get("items")

        assert not result.ok
        assert call_count == 1  # 4xx (non-429) should not retry

    def test_http_429_retried(self):
        client = self._make_client(max_retries=2, backoff_base=0.0)
        call_count = 0

        def _raise_429(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(
                "https://test.example.com/items", 429, "Rate Limited", {}, None
            )

        with patch("urllib.request.urlopen", side_effect=_raise_429):
            result = client.get("items")

        assert not result.ok
        assert call_count == 2  # should retry on 429

    def test_json_decode_error(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client.get("items")

        assert not result.ok

    def test_unexpected_status(self):
        client = self._make_client()
        body = json.dumps({"status": "error", "message": "bad request"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client.get("items")

        assert not result.ok
        assert result.error_type == "api"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
