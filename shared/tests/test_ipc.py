"""Tests for shared.ipc — JSONL file-based IPC with locking."""

import json
import os
import sys
import tempfile
import threading

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config

from shared.ipc import ipc_write, ipc_read_and_clear, ipc_read_incremental, MAX_FILE_BYTES


class TestIpcWrite:
    def _make_cmd_file(self, tmp_path):
        path = os.path.join(tmp_path, "test_cmds.jsonl")
        with open(path, "w"):
            pass
        return path

    def test_write_returns_true(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        assert ipc_write(cmd_file, {"type": "show"}) is True

    def test_write_appends_json_line(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"type": "show"})
        with open(cmd_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["type"] == "show"

    def test_multiple_writes_append(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"type": "show"})
        ipc_write(cmd_file, {"type": "hide"})
        ipc_write(cmd_file, {"type": "quit"})
        with open(cmd_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 3
        types = [json.loads(l)["type"] for l in lines]
        assert types == ["show", "hide", "quit"]

    def test_write_to_nonexistent_dir_returns_false(self, tmp_path):
        bad_path = os.path.join(tmp_path, "nonexistent", "cmds.jsonl")
        assert ipc_write(bad_path, {"type": "show"}) is False

    def test_write_preserves_unicode(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"name": "\u2694 DPS"})
        with open(cmd_file, "r", encoding="utf-8") as f:
            data = json.loads(f.readline())
        assert data["name"] == "\u2694 DPS"


class TestIpcReadAndClear:
    def _make_cmd_file(self, tmp_path):
        path = os.path.join(tmp_path, "test_cmds.jsonl")
        with open(path, "w"):
            pass
        return path

    def test_read_empty_file(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        assert ipc_read_and_clear(cmd_file) == []

    def test_read_returns_written_commands(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"type": "show"})
        ipc_write(cmd_file, {"type": "quit"})
        cmds = ipc_read_and_clear(cmd_file)
        assert len(cmds) == 2
        assert cmds[0]["type"] == "show"
        assert cmds[1]["type"] == "quit"

    def test_read_clears_file(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"type": "show"})
        ipc_read_and_clear(cmd_file)
        # Second read should be empty
        assert ipc_read_and_clear(cmd_file) == []

    def test_skips_malformed_lines(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        with open(cmd_file, "w", encoding="utf-8") as f:
            f.write('{"type": "show"}\n')
            f.write('this is not json\n')
            f.write('{"type": "quit"}\n')
        cmds = ipc_read_and_clear(cmd_file)
        assert len(cmds) == 2
        assert cmds[0]["type"] == "show"
        assert cmds[1]["type"] == "quit"

    def test_skips_blank_lines(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        with open(cmd_file, "w", encoding="utf-8") as f:
            f.write('{"type": "show"}\n')
            f.write('\n')
            f.write('   \n')
            f.write('{"type": "quit"}\n')
        cmds = ipc_read_and_clear(cmd_file)
        assert len(cmds) == 2

    def test_nonexistent_file_returns_empty(self, tmp_path):
        bad_path = os.path.join(tmp_path, "does_not_exist.jsonl")
        assert ipc_read_and_clear(bad_path) == []


class TestIpcReadIncremental:
    def _make_cmd_file(self, tmp_path):
        path = os.path.join(tmp_path, "test_cmds.jsonl")
        with open(path, "w"):
            pass
        return path

    def test_read_from_zero(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"type": "show"})
        cmds, offset = ipc_read_incremental(cmd_file, 0)
        assert len(cmds) == 1
        assert cmds[0]["type"] == "show"
        assert offset > 0

    def test_incremental_reads_only_new_data(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"type": "show"})
        _, offset = ipc_read_incremental(cmd_file, 0)

        ipc_write(cmd_file, {"type": "quit"})
        cmds, offset2 = ipc_read_incremental(cmd_file, offset)
        assert len(cmds) == 1
        assert cmds[0]["type"] == "quit"
        assert offset2 > offset

    def test_no_new_data_returns_empty(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"type": "show"})
        _, offset = ipc_read_incremental(cmd_file, 0)
        cmds, offset2 = ipc_read_incremental(cmd_file, offset)
        assert cmds == []
        assert offset2 == offset

    def test_does_not_truncate_file(self, tmp_path):
        cmd_file = self._make_cmd_file(tmp_path)
        ipc_write(cmd_file, {"type": "show"})
        ipc_read_incremental(cmd_file, 0)
        # File should still contain data
        with open(cmd_file, "r", encoding="utf-8") as f:
            assert len(f.readlines()) == 1


class TestIpcConcurrency:
    """Verify locking prevents data corruption under concurrent access."""

    def test_concurrent_writes_no_data_loss(self, tmp_path):
        cmd_file = os.path.join(tmp_path, "concurrent.jsonl")
        with open(cmd_file, "w"):
            pass

        n_threads = 4
        writes_per_thread = 25
        errors = []

        def writer(thread_id):
            for i in range(writes_per_thread):
                ok = ipc_write(cmd_file, {"tid": thread_id, "seq": i})
                if not ok:
                    errors.append((thread_id, i))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        cmds = ipc_read_and_clear(cmd_file)
        expected = n_threads * writes_per_thread
        # Allow for some lock timeouts under heavy contention
        assert len(cmds) + len(errors) == expected
        # Verify each command is valid JSON with expected fields
        for cmd in cmds:
            assert "tid" in cmd
            assert "seq" in cmd

    def test_concurrent_write_and_read(self, tmp_path):
        """Writer and reader running simultaneously shouldn't crash."""
        cmd_file = os.path.join(tmp_path, "rw.jsonl")
        with open(cmd_file, "w"):
            pass

        stop = threading.Event()
        all_read = []

        def writer():
            for i in range(50):
                ipc_write(cmd_file, {"seq": i})

        def reader():
            while not stop.is_set():
                cmds = ipc_read_and_clear(cmd_file)
                all_read.extend(cmds)
                if not cmds:
                    stop.wait(0.05)

        rt = threading.Thread(target=reader)
        wt = threading.Thread(target=writer)
        rt.start()
        wt.start()
        wt.join()
        stop.wait(0.5)
        stop.set()
        rt.join()

        # Drain any remaining
        all_read.extend(ipc_read_and_clear(cmd_file))

        # Every command should be valid
        for cmd in all_read:
            assert "seq" in cmd


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
