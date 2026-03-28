"""Tests for shared.data_utils — safe conversions, CLI parsing, retry logic."""

import os
import sys
import time
from unittest.mock import patch

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config

from shared.data_utils import safe_float, pct_diff, parse_cli_args, retry_request


class TestSafeFloat:
    def test_int(self):
        assert safe_float(42) == 42.0

    def test_float(self):
        assert safe_float(3.14) == 3.14

    def test_string_number(self):
        assert safe_float("2.5") == 2.5

    def test_none_returns_default(self):
        assert safe_float(None) == 0.0
        assert safe_float(None, -1.0) == -1.0

    def test_garbage_string_returns_default(self):
        assert safe_float("abc") == 0.0

    def test_empty_string_returns_default(self):
        assert safe_float("") == 0.0

    def test_custom_default(self):
        assert safe_float("bad", 99.9) == 99.9

    def test_bool_coerces(self):
        assert safe_float(True) == 1.0
        assert safe_float(False) == 0.0

    def test_negative_string(self):
        assert safe_float("-3.5") == -3.5


class TestPctDiff:
    def test_both_zero(self):
        assert pct_diff(0, 0) == 0.0

    def test_identical(self):
        assert pct_diff(100, 100) == 0.0

    def test_basic_diff(self):
        result = pct_diff(100, 80)
        assert abs(result - 20.0) < 0.01

    def test_symmetric(self):
        assert pct_diff(80, 100) == pct_diff(100, 80)

    def test_one_zero(self):
        assert pct_diff(100, 0) == 100.0

    def test_negative_values(self):
        result = pct_diff(-10, -20)
        assert result > 0


class TestParseCliArgs:
    def test_full_args(self):
        argv = ["200", "300", "800", "600", "0.9", "cmd.jsonl"]
        d = parse_cli_args(argv)
        assert d["x"] == 200
        assert d["y"] == 300
        assert d["w"] == 800
        assert d["h"] == 600
        assert d["opacity"] == 0.9
        assert d["cmd_file"] == "cmd.jsonl"
        assert d["extras"] == []

    def test_with_extras(self):
        argv = ["100", "100", "500", "400", "extra1", "extra2", "0.95", "cmd.jsonl"]
        d = parse_cli_args(argv)
        assert d["extras"] == ["extra1", "extra2"]
        assert d["opacity"] == 0.95

    def test_empty_args_uses_defaults(self):
        d = parse_cli_args([])
        assert d["x"] == 100
        assert d["y"] == 100
        assert d["cmd_file"] is None

    def test_custom_defaults(self):
        d = parse_cli_args([], defaults={"x": 50, "w": 1000})
        assert d["x"] == 50
        assert d["w"] == 1000

    def test_invalid_numbers_fall_back(self):
        d = parse_cli_args(["abc", "def", "ghi", "jkl"])
        # Should keep defaults for x, y, w, h
        assert d["x"] == 100

    def test_five_args_opacity_only(self):
        argv = ["100", "100", "500", "400", "0.8"]
        d = parse_cli_args(argv)
        assert d["opacity"] == 0.8
        assert d["cmd_file"] is None


class TestRetryRequest:
    def test_success_first_try(self):
        result = retry_request(lambda: "ok")
        assert result == "ok"

    def test_retries_on_failure(self):
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return "recovered"

        result = retry_request(flaky, retries=2, backoff=0.01)
        assert result == "recovered"
        assert call_count == 3

    def test_raises_after_exhausting_retries(self):
        def always_fail():
            raise ValueError("permanent")

        try:
            retry_request(always_fail, retries=2, backoff=0.01)
            assert False, "Should have raised"
        except ValueError as e:
            assert "permanent" in str(e)

    def test_no_retries(self):
        call_count = 0

        def fail_once():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        try:
            retry_request(fail_once, retries=0, backoff=0.01)
        except RuntimeError:
            pass
        assert call_count == 1

    def test_backoff_increases(self):
        timestamps = []

        def fail():
            timestamps.append(time.monotonic())
            raise RuntimeError("fail")

        try:
            retry_request(fail, retries=2, backoff=0.05)
        except RuntimeError:
            pass

        assert len(timestamps) == 3
        gap1 = timestamps[1] - timestamps[0]
        gap2 = timestamps[2] - timestamps[1]
        # Second gap should be roughly 2x the first (exponential backoff)
        assert gap2 > gap1 * 1.5


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
