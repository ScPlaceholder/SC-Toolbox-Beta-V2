"""Tests for core.process_manager — ManagedProcess and ProcessManager."""

import os, sys
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
import shared.path_setup
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

import subprocess
from unittest.mock import MagicMock, patch, mock_open

import pytest

from core.process_manager import ManagedProcess, ProcessManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mp(**overrides) -> ManagedProcess:
    """Create a ManagedProcess with sensible defaults for testing."""
    defaults = dict(
        skill_id="test_skill",
        python_exe="python",
        script="test_script.py",
        cwd="/tmp",
        args=[],
        base_dir="/tmp/base",
    )
    defaults.update(overrides)
    return ManagedProcess(**defaults)


def _make_running_mp(mock_popen_cls=None):
    """Return a ManagedProcess whose internal state looks like a running proc.

    Optionally accepts an already-configured mock Popen *class* so the caller
    can inspect it.
    """
    mp = _make_mp()
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None  # still running
    mock_proc.pid = 12345
    mp._proc = mock_proc
    mp._cmd_file = "/tmp/fake_cmd.jsonl"
    mp._visible = True
    return mp, mock_proc


# ===========================================================================
# ManagedProcess tests
# ===========================================================================


class TestManagedProcessInitialState:
    """1. test_initial_state"""

    def test_initial_state(self):
        mp = _make_mp()
        assert mp.running is False
        assert mp.visible is False
        assert mp.pid is None
        assert mp._consecutive_crashes == 0
        assert mp._current_cooldown == 5.0  # PROCESS_START_COOLDOWN default


class TestRecordCrash:
    """2–3. Crash tracking and exponential backoff."""

    def test_record_crash_increments(self):
        mp = _make_mp()
        # crash 1 → cooldown = 5 * 2^1 = 10
        mp._record_crash()
        assert mp._consecutive_crashes == 1
        assert mp._current_cooldown == 10.0

        # crash 2 → cooldown = 5 * 2^2 = 20
        mp._record_crash()
        assert mp._consecutive_crashes == 2
        assert mp._current_cooldown == 20.0

        # crash 3 → 40
        mp._record_crash()
        assert mp._consecutive_crashes == 3
        assert mp._current_cooldown == 40.0

        # crash 4 → 5*2^4 = 80, capped at 60
        mp._record_crash()
        assert mp._consecutive_crashes == 4
        assert mp._current_cooldown == 60.0

    def test_record_crash_caps_at_five(self):
        mp = _make_mp()
        for _ in range(10):
            mp._record_crash()
        assert mp._consecutive_crashes == 5
        assert mp._current_cooldown == 60.0


class TestMarkReady:
    """4. mark_ready resets crash state."""

    def test_mark_ready_resets_state(self):
        mp = _make_mp()
        mp._consecutive_crashes = 3
        mp._visible = False
        mp.mark_ready()
        assert mp._consecutive_crashes == 0
        assert mp._visible is True
        # cooldown back to base
        assert mp._current_cooldown == 5.0


class TestStartCooldown:
    """5. Cooldown prevents rapid relaunch."""

    @patch("core.process_manager.time")
    @patch("core.process_manager.get_subprocess_log_path", return_value="/tmp/test.log")
    def test_start_cooldown_blocks(self, _mock_log_path, mock_time):
        mp = _make_mp()
        # Simulate a recent start 2 seconds ago (cooldown is 5s)
        mock_time.monotonic.return_value = 100.0
        mp._last_start = 98.0
        result = mp.start()
        assert result is False


class TestStartSuccess:
    """6. Successful process launch."""

    @patch("core.process_manager.time")
    @patch("core.process_manager.get_subprocess_log_path", return_value="/tmp/test.log")
    @patch("core.process_manager.subprocess.Popen")
    def test_start_success(self, mock_popen_cls, _mock_log_path, mock_time):
        mock_time.monotonic.return_value = 200.0

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 9999
        mock_popen_cls.return_value = mock_proc

        mp = _make_mp()
        mp._last_start = 0.0  # long ago

        with patch("builtins.open", mock_open()):
            result = mp.start()

        assert result is True
        assert mp.running is True
        assert mp.pid == 9999
        assert mp._visible is True
        mock_popen_cls.assert_called_once()


class TestStop:
    """7. Graceful stop sequence."""

    @patch("core.process_manager.ipc_write", return_value=True)
    def test_stop_graceful(self, mock_ipc):
        mp, mock_proc = _make_running_mp()
        cmd_file = mp._cmd_file  # capture before stop clears it
        # Simulate the process exiting after wait()
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        mp.stop()

        # IPC quit was sent
        mock_ipc.assert_called_once_with(cmd_file, {"type": "quit"})
        # Process was waited on
        mock_proc.wait.assert_called_once()
        # After stop, state is cleaned up
        assert mp._proc is None
        assert mp._visible is False
        assert mp._stopping is False


class TestSend:
    """8–9. send() behaviour when stopped vs running."""

    def test_send_when_stopped(self):
        mp = _make_mp()
        assert mp.send({"type": "test"}) is False

    @patch("core.process_manager.ipc_write", return_value=True)
    def test_send_when_running(self, mock_ipc):
        mp, _ = _make_running_mp()
        result = mp.send({"type": "show"})
        assert result is True
        mock_ipc.assert_called_once_with("/tmp/fake_cmd.jsonl", {"type": "show"})


class TestToggle:
    """10–12. Toggle behaviour depending on process state."""

    @patch("core.process_manager.time")
    @patch("core.process_manager.get_subprocess_log_path", return_value="/tmp/test.log")
    @patch("core.process_manager.subprocess.Popen")
    def test_toggle_starts_when_stopped(self, mock_popen_cls, _mock_log_path, mock_time):
        mock_time.monotonic.return_value = 500.0

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 7777
        mock_popen_cls.return_value = mock_proc

        mp = _make_mp()

        with patch("builtins.open", mock_open()):
            mp.toggle()

        # Should have started the process
        mock_popen_cls.assert_called_once()
        assert mp.running is True

    @patch("core.process_manager.ipc_write", return_value=True)
    def test_toggle_hides_when_visible(self, mock_ipc):
        mp, _ = _make_running_mp()
        mp._visible = True
        mp.toggle()
        mock_ipc.assert_called_once_with("/tmp/fake_cmd.jsonl", {"type": "hide"})
        assert mp._visible is False

    @patch("core.process_manager.ipc_write", return_value=True)
    def test_toggle_shows_when_hidden(self, mock_ipc):
        mp, _ = _make_running_mp()
        mp._visible = False
        mp.toggle()
        mock_ipc.assert_called_once_with("/tmp/fake_cmd.jsonl", {"type": "show"})
        assert mp._visible is True


class TestHealthCheck:
    """13. health_check returns expected structure."""

    def test_health_check_structure(self):
        mp = _make_mp(skill_id="cargo")
        result = mp.health_check()
        assert result == {
            "skill_id": "cargo",
            "running": False,
            "visible": False,
            "pid": None,
        }

    def test_health_check_running(self):
        mp, mock_proc = _make_running_mp()
        mp.skill_id = "dps"
        result = mp.health_check()
        assert result["skill_id"] == "dps"
        assert result["running"] is True
        assert result["visible"] is True
        assert result["pid"] == 12345


class TestRestart:
    """14. restart calls stop then start."""

    @patch.object(ManagedProcess, "_start_unlocked", return_value=True)
    @patch.object(ManagedProcess, "_stop_unlocked")
    def test_restart(self, mock_stop, mock_start):
        mp = _make_mp()
        result = mp.restart()
        mock_stop.assert_called_once()
        mock_start.assert_called_once()
        assert result is True


# ===========================================================================
# ProcessManager tests
# ===========================================================================


class TestProcessManagerRegister:
    """15–16. Registration and lookup."""

    def test_register_and_get(self):
        pm = ProcessManager()
        mp = pm.register("cargo", "python", "cargo_app.py", "/tmp", [], "/tmp/base")
        assert isinstance(mp, ManagedProcess)
        assert pm.get("cargo") is mp

    def test_get_unknown_returns_none(self):
        pm = ProcessManager()
        assert pm.get("nonexistent") is None


class TestProcessManagerStopAll:
    """17. stop_all stops every registered process."""

    def test_stop_all(self):
        pm = ProcessManager()
        mp1 = pm.register("a", "python", "a.py", "/tmp", [], "/tmp/base")
        mp2 = pm.register("b", "python", "b.py", "/tmp", [], "/tmp/base")

        with patch.object(mp1, "stop") as s1, patch.object(mp2, "stop") as s2:
            pm.stop_all()
            s1.assert_called_once()
            s2.assert_called_once()


class TestProcessManagerHealth:
    """18. health returns dict keyed by skill_id."""

    def test_health_returns_all(self):
        pm = ProcessManager()
        pm.register("x", "python", "x.py", "/tmp", [], "/tmp/base")
        pm.register("y", "python", "y.py", "/tmp", [], "/tmp/base")

        result = pm.health()
        assert set(result.keys()) == {"x", "y"}
        for key in ("x", "y"):
            assert "skill_id" in result[key]
            assert "running" in result[key]
            assert "visible" in result[key]
            assert "pid" in result[key]
