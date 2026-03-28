"""Tests for shared.http_client — HttpClient with retry, backoff, and structured errors."""

import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

import io
import json
import urllib.error
from unittest.mock import MagicMock, call, patch

import pytest

from shared.http_client import HttpClient


def _mock_response(data: dict) -> MagicMock:
    """Create a mock that behaves like the context-manager returned by urlopen."""
    body = json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int, reason: str = "Error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://example.com",
        code=code,
        msg=reason,
        hdrs={},
        fp=io.BytesIO(b""),
    )


# ---- Tests ------------------------------------------------------------------


class TestGetJsonSuccess:
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_success(self, mock_urlopen):
        """Valid JSON response yields Result.ok with parsed data."""
        payload = {"ships": [{"name": "Aurora"}]}
        mock_urlopen.return_value = _mock_response(payload)

        client = HttpClient("https://api.example.com")
        result = client.get_json("/ships")

        assert result.ok is True
        assert result.data == payload
        assert result.error is None


class TestUrlConstruction:
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_url_construction(self, mock_urlopen):
        """URL is base + '/' + endpoint when endpoint lacks leading slash."""
        mock_urlopen.return_value = _mock_response({})

        client = HttpClient("https://api.example.com/")
        client.get_json("v2/ships")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.example.com/v2/ships"

    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_url_endpoint_with_slash(self, mock_urlopen):
        """URL is base + endpoint when endpoint starts with '/'."""
        mock_urlopen.return_value = _mock_response({})

        client = HttpClient("https://api.example.com")
        client.get_json("/v2/ships")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.example.com/v2/ships"

    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_url_no_endpoint(self, mock_urlopen):
        """URL is just the base when endpoint is empty string."""
        mock_urlopen.return_value = _mock_response({})

        client = HttpClient("https://api.example.com")
        client.get_json("")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.example.com/"


class TestHeaders:
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_includes_headers(self, mock_urlopen):
        """Custom headers are forwarded to the Request object."""
        mock_urlopen.return_value = _mock_response({})
        headers = {"Authorization": "Bearer tok123", "Accept": "application/json"}

        client = HttpClient("https://api.example.com", headers=headers)
        client.get_json("/data")

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tok123"
        assert req.get_header("Accept") == "application/json"


class TestRetryBehavior:
    @patch("shared.http_client.time.sleep", return_value=None)
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_network_error_retries(self, mock_urlopen, _mock_sleep):
        """URLError triggers retries up to max_retries."""
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        client = HttpClient("https://api.example.com", max_retries=3)
        result = client.get_json("/data")

        assert result.ok is False
        assert mock_urlopen.call_count == 3

    @patch("shared.http_client.time.sleep", return_value=None)
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_429_retries(self, mock_urlopen, _mock_sleep):
        """HTTP 429 is retried, and success on later attempt is returned."""
        mock_urlopen.side_effect = [
            _http_error(429, "Too Many Requests"),
            _mock_response({"ok": True}),
        ]

        client = HttpClient("https://api.example.com", max_retries=3)
        result = client.get_json("/data")

        assert result.ok is True
        assert result.data == {"ok": True}
        assert mock_urlopen.call_count == 2

    @patch("shared.http_client.time.sleep", return_value=None)
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_4xx_no_retry(self, mock_urlopen, _mock_sleep):
        """Non-429 4xx errors are NOT retried (single attempt only)."""
        mock_urlopen.side_effect = _http_error(404, "Not Found")

        client = HttpClient("https://api.example.com", max_retries=3)
        result = client.get_json("/missing")

        assert result.ok is False
        assert mock_urlopen.call_count == 1

    @patch("shared.http_client.time.sleep", return_value=None)
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_max_retries_exhausted(self, mock_urlopen, _mock_sleep):
        """After max_retries attempts, failure Result is returned."""
        mock_urlopen.side_effect = urllib.error.URLError("timeout")

        client = HttpClient("https://api.example.com", max_retries=5)
        result = client.get_json("/slow")

        assert result.ok is False
        assert mock_urlopen.call_count == 5
        assert "Network error" in result.error


class TestFailureResult:
    @patch("shared.http_client.time.sleep", return_value=None)
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_failure_result(self, mock_urlopen, _mock_sleep):
        """Failure Result carries error message and error_type."""
        mock_urlopen.side_effect = _http_error(500, "Internal Server Error")

        client = HttpClient("https://api.example.com", max_retries=3)
        result = client.get_json("/boom")

        assert result.ok is False
        assert result.error is not None
        assert result.error_type == "api"
        assert "500" in result.error

    @patch("shared.http_client.time.sleep", return_value=None)
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_network_failure_type(self, mock_urlopen, _mock_sleep):
        """Network errors set error_type to 'network'."""
        mock_urlopen.side_effect = urllib.error.URLError("DNS failure")

        client = HttpClient("https://api.example.com", max_retries=1)
        result = client.get_json("/data")

        assert result.error_type == "network"


class TestJsonParseError:
    @patch("shared.http_client.urllib.request.urlopen")
    def test_get_json_json_parse_error(self, mock_urlopen):
        """Invalid JSON body returns a failure Result."""
        bad_resp = MagicMock()
        bad_resp.read.return_value = b"not-json{{"
        bad_resp.__enter__ = lambda s: s
        bad_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = bad_resp

        client = HttpClient("https://api.example.com", max_retries=1)
        result = client.get_json("/bad")

        assert result.ok is False
        assert "Parse error" in result.error
        assert result.error_type == "unknown"
