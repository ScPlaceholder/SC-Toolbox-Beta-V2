"""Tests for shared.errors — exception hierarchy and Result wrapper."""

import os, sys
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
import shared.path_setup
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

import pytest

from shared.errors import (
    SCToolboxError,
    ApiError,
    NetworkError,
    CacheError,
    SchemaError,
    Result,
)


# ── Exception hierarchy ──────────────────────────────────────────────


class TestSCToolboxError:
    def test_is_exception(self):
        assert issubclass(SCToolboxError, Exception)

    def test_instantiation(self):
        err = SCToolboxError("boom")
        assert str(err) == "boom"


class TestApiError:
    def test_inherits_sctoolbox_error(self):
        assert issubclass(ApiError, SCToolboxError)

    def test_isinstance_check(self):
        err = ApiError("/ships", "not found", status_code=404)
        assert isinstance(err, SCToolboxError)
        assert isinstance(err, Exception)

    def test_message_format(self):
        err = ApiError("/ships", "not found", status_code=404)
        assert str(err) == "API error for /ships: not found"

    def test_attributes(self):
        err = ApiError("/prices", "timeout", status_code=504)
        assert err.endpoint == "/prices"
        assert err.status_code == 504

    def test_status_code_defaults_to_none(self):
        err = ApiError("/items", "server error")
        assert err.status_code is None


class TestNetworkError:
    def test_inherits_api_error(self):
        assert issubclass(NetworkError, ApiError)

    def test_isinstance_chain(self):
        err = NetworkError("/data", "DNS failure")
        assert isinstance(err, ApiError)
        assert isinstance(err, SCToolboxError)
        assert isinstance(err, Exception)

    def test_message_format(self):
        err = NetworkError("/data", "DNS failure", status_code=None)
        assert str(err) == "API error for /data: DNS failure"


class TestCacheError:
    def test_inherits_sctoolbox_error(self):
        assert issubclass(CacheError, SCToolboxError)

    def test_not_api_error(self):
        assert not issubclass(CacheError, ApiError)


class TestSchemaError:
    def test_inherits_cache_error(self):
        assert issubclass(SchemaError, CacheError)

    def test_isinstance_chain(self):
        err = SchemaError("bad schema")
        assert isinstance(err, CacheError)
        assert isinstance(err, SCToolboxError)
        assert isinstance(err, Exception)


# ── Result wrapper ────────────────────────────────────────────────────


class TestResultSuccess:
    def test_success_creates_ok_result(self):
        r = Result.success({"key": "val"})
        assert r.ok is True
        assert r.data == {"key": "val"}
        assert r.error is None
        assert r.error_type is None

    def test_success_with_list_data(self):
        r = Result.success([1, 2, 3])
        assert r.ok is True
        assert r.data == [1, 2, 3]

    def test_success_with_string_data(self):
        r = Result.success("hello")
        assert r.ok is True
        assert r.data == "hello"


class TestResultFailure:
    def test_failure_default_error_type(self):
        r = Result.failure("something broke")
        assert r.ok is False
        assert r.data is None
        assert r.error == "something broke"
        assert r.error_type == "unknown"

    def test_failure_custom_error_type(self):
        r = Result.failure("gone", error_type="cache_miss")
        assert r.ok is False
        assert r.error_type == "cache_miss"

    def test_failure_data_is_none(self):
        r = Result.failure("err")
        assert r.data is None


class TestResultOkProperty:
    def test_ok_true_on_success(self):
        assert Result.success({"x": 1}).ok is True

    def test_ok_false_on_failure(self):
        assert Result.failure("err").ok is False

    def test_ok_false_when_data_is_none(self):
        r = Result(data=None, error=None, error_type=None)
        assert r.ok is False


class TestResultFrozen:
    def test_cannot_mutate_data(self):
        r = Result.success(42)
        with pytest.raises(AttributeError):
            r.data = 99  # type: ignore[misc]

    def test_cannot_mutate_error(self):
        r = Result.failure("oops")
        with pytest.raises(AttributeError):
            r.error = "changed"  # type: ignore[misc]
