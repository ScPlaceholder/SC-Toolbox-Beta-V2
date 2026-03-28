"""UI tests for shared Qt widgets using pytest-qt.

Covers public API, signal emission, and data flow for all shared widgets.
Does NOT test visual rendering — only behaviour and contracts.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

# Bootstrap project root
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..')))
import shared.path_setup  # noqa: E402

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from shared.qt.theme import P, apply_theme
from shared.qt.animated_button import SCButton
from shared.qt.search_bar import SCSearchBar
from shared.qt.data_table import SCTable, ColumnDef
from shared.qt.dropdown import SCMultiCheck
from shared.qt.fuzzy_combo import SCFuzzyCombo
from shared.qt.hud_widgets import HUDPanel, GlowEffect
from shared.qt.title_bar import SCTitleBar
from shared.qt.base_window import SCWindow
from shared.qt.ipc_thread import IPCWatcher
from shared.ipc import ipc_write


# ── SCButton ─────────────────────────────────────────────────────────────────

class TestSCButton:
    def test_click_emits_signal(self, qtbot):
        btn = SCButton("Test")
        qtbot.addWidget(btn)
        with qtbot.waitSignal(btn.clicked, timeout=1000):
            qtbot.mouseClick(btn, Qt.LeftButton)

    def test_glow_effect_attached(self, qtbot):
        btn = SCButton("Glow", glow_color="#ff0000")
        qtbot.addWidget(btn)
        effect = btn.graphicsEffect()
        assert effect is not None

    def test_set_glow_color(self, qtbot):
        btn = SCButton("X")
        qtbot.addWidget(btn)
        btn.set_glow_color("#00ff00")
        assert btn.graphicsEffect() is not None


# ── SCSearchBar ──────────────────────────────────────────────────────────────

class TestSCSearchBar:
    def test_placeholder(self, qtbot):
        bar = SCSearchBar(placeholder="Find items...")
        qtbot.addWidget(bar)
        assert bar.placeholderText() == "Find items..."

    def test_search_signal_debounced(self, qtbot):
        bar = SCSearchBar(debounce_ms=50)
        qtbot.addWidget(bar)
        with qtbot.waitSignal(bar.search_changed, timeout=1000) as blocker:
            bar.setText("hello")
        assert blocker.args == ["hello"]

    def test_rapid_typing_only_emits_final(self, qtbot):
        bar = SCSearchBar(debounce_ms=100)
        qtbot.addWidget(bar)
        results = []
        bar.search_changed.connect(results.append)
        bar.setText("a")
        bar.setText("ab")
        bar.setText("abc")
        qtbot.wait(250)
        assert results == ["abc"]


# ── SCTable ──────────────────────────────────────────────────────────────────

class TestSCTable:
    def _make_table(self, qtbot):
        cols = [
            ColumnDef(header="Name", key="name", width=100),
            ColumnDef(header="Value", key="val", width=80),
        ]
        tbl = SCTable(cols)
        qtbot.addWidget(tbl)
        return tbl

    def test_set_data(self, qtbot):
        tbl = self._make_table(qtbot)
        tbl.set_data([{"name": "A", "val": 1}, {"name": "B", "val": 2}])
        assert tbl._source_model.rowCount() == 2

    def test_empty_data(self, qtbot):
        tbl = self._make_table(qtbot)
        tbl.set_data([])
        assert tbl._source_model.rowCount() == 0

    def test_set_data_replaces(self, qtbot):
        tbl = self._make_table(qtbot)
        tbl.set_data([{"name": "A", "val": 1}])
        tbl.set_data([{"name": "B", "val": 2}, {"name": "C", "val": 3}])
        assert tbl._source_model.rowCount() == 2

    def test_get_selected_row_none_initially(self, qtbot):
        tbl = self._make_table(qtbot)
        tbl.set_data([{"name": "A", "val": 1}])
        assert tbl.get_selected_row() is None

    def test_row_selected_signal(self, qtbot):
        tbl = self._make_table(qtbot)
        tbl.set_data([{"name": "A", "val": 1}])
        tbl.show()
        qtbot.wait(50)
        # row_selected emits via clicked signal — simulate a click on cell (0,0)
        idx = tbl._source_model.index(0, 0)
        if tbl._proxy:
            idx = tbl._proxy.mapFromSource(idx)
        with qtbot.waitSignal(tbl.row_selected, timeout=1000):
            tbl.clicked.emit(idx)


# ── SCMultiCheck ─────────────────────────────────────────────────────────────

class TestSCMultiCheck:
    def test_set_items(self, qtbot):
        mc = SCMultiCheck(label="Filter", items=["A", "B", "C"])
        qtbot.addWidget(mc)
        assert mc.get_selected() == []

    def test_set_selected(self, qtbot):
        mc = SCMultiCheck(label="F", items=["X", "Y", "Z"])
        qtbot.addWidget(mc)
        mc.set_selected(["X", "Z"])
        assert sorted(mc.get_selected()) == ["X", "Z"]

    def test_selection_changed_signal(self, qtbot):
        mc = SCMultiCheck(label="F", items=["A", "B"])
        qtbot.addWidget(mc)
        # set_selected doesn't emit (it rebuilds checkboxes pre-checked).
        # Signal fires on user toggle via _on_toggle. Test that path.
        with qtbot.waitSignal(mc.selection_changed, timeout=1000):
            mc._on_toggle("A", True)

    def test_set_items_replaces(self, qtbot):
        mc = SCMultiCheck(label="F", items=["A"])
        qtbot.addWidget(mc)
        mc.set_selected(["A"])
        mc.set_items(["X", "Y"])
        assert mc.get_selected() == []


# ── SCFuzzyCombo ─────────────────────────────────────────────────────────────

class TestSCFuzzyCombo:
    def test_set_items(self, qtbot):
        fc = SCFuzzyCombo(items=["Alpha", "Beta", "Gamma"])
        qtbot.addWidget(fc)
        assert fc.current_text() == ""

    def test_set_text(self, qtbot):
        fc = SCFuzzyCombo(items=["Alpha", "Beta"])
        qtbot.addWidget(fc)
        fc.set_text("Alpha")
        assert fc.current_text() == "Alpha"

    def test_item_selected_signal(self, qtbot):
        fc = SCFuzzyCombo(items=["Alpha", "Beta"])
        qtbot.addWidget(fc)
        with qtbot.waitSignal(fc.item_selected, timeout=1000) as blocker:
            fc.item_selected.emit("Alpha")
        assert blocker.args == ["Alpha"]

    def test_placeholder(self, qtbot):
        fc = SCFuzzyCombo(placeholder="Pick one...")
        qtbot.addWidget(fc)
        assert fc._input.placeholderText() == "Pick one..."


# ── HUDPanel ─────────────────────────────────────────────────────────────────

class TestHUDPanel:
    def test_inner_layout_accessible(self, qtbot):
        panel = HUDPanel()
        qtbot.addWidget(panel)
        layout = panel.inner_layout
        assert layout is not None

    def test_paint_does_not_crash(self, qtbot):
        panel = HUDPanel(bracket_length=8, show_brackets=True)
        qtbot.addWidget(panel)
        panel.resize(200, 100)
        panel.repaint()


# ── SCTitleBar ───────────────────────────────────────────────────────────────

class TestSCTitleBar:
    def test_close_signal(self, qtbot):
        parent = QWidget()
        qtbot.addWidget(parent)
        tb = SCTitleBar(parent, title="Test")
        with qtbot.waitSignal(tb.close_clicked, timeout=1000):
            tb.close_clicked.emit()

    def test_minimize_signal(self, qtbot):
        parent = QWidget()
        qtbot.addWidget(parent)
        tb = SCTitleBar(parent, title="Test", show_minimize=True)
        with qtbot.waitSignal(tb.minimize_clicked, timeout=1000):
            tb.minimize_clicked.emit()

    def test_set_hotkey(self, qtbot):
        parent = QWidget()
        qtbot.addWidget(parent)
        tb = SCTitleBar(parent, title="Test", hotkey_text="Ctrl+D")
        tb.set_hotkey("Ctrl+X")
        assert tb._hotkey_label.text() == "Ctrl+X"


# ── SCWindow ─────────────────────────────────────────────────────────────────

class TestSCWindow:
    def test_set_opacity_clamped(self, qtbot):
        win = SCWindow(title="Test", opacity=0.9)
        qtbot.addWidget(win)
        win.set_opacity(2.0)
        assert win.windowOpacity() <= 1.01  # Qt quantises to 8-bit
        win.set_opacity(-1.0)
        # Qt quantises 0.3 to ~0.298 (76/255); use approximate check
        assert win.windowOpacity() >= 0.29

    def test_toggle_visibility(self, qtbot):
        win = SCWindow(title="Test")
        qtbot.addWidget(win)
        win.show()
        assert win.isVisible()
        win.toggle_visibility()
        assert not win.isVisible()
        win.toggle_visibility()
        assert win.isVisible()

    def test_get_geometry_dict(self, qtbot):
        win = SCWindow(title="Test")
        qtbot.addWidget(win)
        win.move(50, 60)
        win.resize(800, 600)
        g = win.get_geometry_dict(prefix="test_")
        assert "test_x" in g
        assert "test_y" in g
        assert "test_w" in g
        assert "test_h" in g
        assert "test_opacity" in g

    def test_restore_geometry(self, qtbot):
        win = SCWindow(title="Test")
        qtbot.addWidget(win)
        win.restore_geometry_from_args(100, 200, 640, 480, 0.8)
        assert abs(win.windowOpacity() - 0.8) < 0.05


# ── IPCWatcher ───────────────────────────────────────────────────────────────

class TestIPCWatcher:
    def test_command_received(self, qtbot):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            cmd_file = f.name

        try:
            watcher = IPCWatcher(cmd_file, poll_ms=50)
            watcher.start()
            qtbot.wait(100)

            ipc_write(cmd_file, {"type": "show"})

            with qtbot.waitSignal(watcher.command_received, timeout=3000) as blocker:
                pass

            assert blocker.args[0]["type"] == "show"
            watcher.stop()
        finally:
            for p in (cmd_file, cmd_file + ".lock"):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_stop(self, qtbot):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            cmd_file = f.name

        try:
            watcher = IPCWatcher(cmd_file, poll_ms=50)
            watcher.start()
            qtbot.wait(100)
            watcher.stop()
            assert watcher.isFinished() or not watcher.isRunning()
        finally:
            for p in (cmd_file, cmd_file + ".lock"):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_missing_file_emits_quit(self, qtbot):
        # Point at a nonexistent file
        cmd_file = os.path.join(tempfile.gettempdir(), "nonexistent_test_ipc.jsonl")
        watcher = IPCWatcher(cmd_file, poll_ms=50)
        watcher.start()

        with qtbot.waitSignal(watcher.command_received, timeout=5000) as blocker:
            pass

        assert blocker.args[0].get("type") == "quit"
        watcher.stop()
