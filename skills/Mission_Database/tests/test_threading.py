"""Tests for utils.threading — background worker utility."""

import os
import sys
import threading
import time

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.threading import run_in_background


class TestRunInBackground:
    def test_calls_fn_and_on_done(self):
        results = []

        def task():
            results.append("task_ran")

        def done():
            results.append("done_called")

        t = run_in_background(task, on_done=done)
        t.join(timeout=5)
        assert "task_ran" in results
        assert "done_called" in results

    def test_calls_on_error_on_exception(self):
        errors = []

        def failing_task():
            raise ValueError("boom")

        def on_error(exc):
            errors.append(str(exc))

        t = run_in_background(failing_task, on_error=on_error)
        t.join(timeout=5)
        assert len(errors) == 1
        assert "boom" in errors[0]

    def test_on_done_not_called_on_error(self):
        results = []

        def failing_task():
            raise RuntimeError("fail")

        def done():
            results.append("done")

        def on_error(exc):
            results.append("error")

        t = run_in_background(failing_task, on_done=done, on_error=on_error)
        t.join(timeout=5)
        assert "error" in results
        assert "done" not in results

    def test_returns_thread(self):
        t = run_in_background(lambda: None)
        assert isinstance(t, threading.Thread)
        t.join(timeout=5)

    def test_daemon_thread(self):
        t = run_in_background(lambda: None)
        assert t.daemon is True
        t.join(timeout=5)

    def test_no_callbacks(self):
        """fn runs even without on_done or on_error."""
        results = []

        def task():
            results.append("ran")

        t = run_in_background(task)
        t.join(timeout=5)
        assert "ran" in results

    def test_exception_without_on_error(self):
        """Exception in fn does not crash when on_error is None."""
        def failing_task():
            raise RuntimeError("unhandled")

        t = run_in_background(failing_task)
        t.join(timeout=5)
        # Should not raise — exception is logged, not propagated


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
