# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
"""Lazy tabs: a tool is built the first time its tab is selected, never before.

J's requirement: opening the Everything Finder must cost about what opening ONE
tool costs, "each tool streamed in as normal". These tests pin that at three
levels: the stack, the window (with counting fake tools), and the real modules
(in a clean subprocess, so sys.modules tells the truth).
"""
import os
import subprocess
import sys
import textwrap

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
EF_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(EF_DIR)

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication  # noqa: E402


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(n=5):
    for _ in range(n):
        QCoreApplication.processEvents()


class _Counter:
    def __init__(self):
        self.calls = {}

    def factory(self, key):
        def make():
            self.calls[key] = self.calls.get(key, 0) + 1
            return QtWidgets.QLabel(f"tool {key}")
        return make


# ── the stack ────────────────────────────────────────────────────────────────

def test_registering_tabs_builds_nothing():
    _app()
    from everything_finder.lazy_tabs import LazyTabStack
    c = _Counter()
    st = LazyTabStack()
    for k in ("a", "b", "c"):
        st.add_tab(k, k.upper(), c.factory(k))
    assert c.calls == {}
    assert st.built_keys() == []


def test_select_builds_once_and_reselect_only_switches():
    _app()
    from everything_finder.lazy_tabs import LazyTabStack
    c = _Counter()
    st = LazyTabStack()
    for k in ("a", "b", "c"):
        st.add_tab(k, k.upper(), c.factory(k))
    wa = st.select("a")
    assert c.calls == {"a": 1}
    assert st.currentWidget() is wa
    st.select("b")
    st.select("a")
    st.select("a")
    assert c.calls == {"a": 1, "b": 1}, "a built tab must never be rebuilt"
    assert st.currentWidget() is wa
    assert st.built_keys() == ["a", "b"]


def test_cycle_wraps_through_every_tab():
    _app()
    from everything_finder.lazy_tabs import LazyTabStack
    c = _Counter()
    st = LazyTabStack()
    for k in ("a", "b", "c"):
        st.add_tab(k, k.upper(), c.factory(k))
    st.select("a")
    assert [st.cycle(), st.cycle(), st.cycle()] == ["b", "c", "a"]
    assert st.cycle(-1) == "c"


def test_a_failing_tool_shows_an_error_and_retries_on_next_select():
    _app()
    from everything_finder.lazy_tabs import LazyTabStack
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("UEX down")
        return QtWidgets.QLabel("ok now")
    st = LazyTabStack()
    st.add_tab("x", "X", flaky)
    assert st.select("x") is None
    assert "UEX down" in st.currentWidget().text()
    assert not st.is_built("x")
    w = st.select("x")
    assert w is not None and w.text() == "ok now"
    assert len(attempts) == 2


# ── the window, with counting fake tools ─────────────────────────────────────

@pytest.fixture
def window(monkeypatch, tmp_path):
    _app()
    from everything_finder import window as wmod
    monkeypatch.setattr(wmod.EverythingFinderWindow, "_state_path",
                        staticmethod(lambda: str(tmp_path / "window.json")))
    c = _Counter()
    facs = {k: c.factory(k) for k in (wmod.TAB_ITEM, wmod.TAB_TRADE, wmod.TAB_MAP)}
    w = wmod.EverythingFinderWindow(x=10, y=10, w=1000, h=700, initial_tab=wmod.TAB_TRADE,
                                    factories=facs)
    yield w, c, wmod
    w.hide()
    w.deleteLater()
    _pump()


def test_constructing_the_window_builds_no_tool(window):
    w, c, _wmod = window
    assert c.calls == {}
    assert w.tabs.built_keys() == []


def test_opening_the_window_builds_only_the_tab_it_opens_on(window):
    w, c, wmod = window
    w.show()
    _pump(10)
    assert c.calls == {wmod.TAB_TRADE: 1}, c.calls
    assert w.tabs.current_key() == wmod.TAB_TRADE
    assert w._tab_buttons[wmod.TAB_TRADE].isChecked()


def test_clicking_tabs_builds_each_tool_once(window):
    w, c, wmod = window
    w.show()
    _pump(10)
    for key in (wmod.TAB_ITEM, wmod.TAB_MAP, wmod.TAB_TRADE, wmod.TAB_ITEM):
        w._tab_buttons[key].click()
        _pump()
        assert w.tabs.current_key() == key
    assert c.calls == {wmod.TAB_ITEM: 1, wmod.TAB_TRADE: 1, wmod.TAB_MAP: 1}, c.calls


def test_last_tab_is_remembered_for_next_open(window, tmp_path):
    w, _c, wmod = window
    w.show()
    _pump(10)
    w.select_tab(wmod.TAB_MAP)
    assert w._saved_tab() == wmod.TAB_MAP


# ── the real tools, in a clean interpreter ───────────────────────────────────

_PROBE = textwrap.dedent(r'''
    import os, sys, json
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, ROOT)
    from shared.app_bootstrap import bootstrap_skill
    bootstrap_skill(os.path.join(EF_DIR, "everything_finder_app.py"))
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from everything_finder import window as wmod
    wmod.EverythingFinderWindow._state_path = staticmethod(lambda: os.path.join(TMP, "w.json"))
    TOOLS = {"item": "market_finder.ui.app", "trade": "trade_hub_app", "map": "ef_starmap.panel"}
    out = {}
    w = wmod.EverythingFinderWindow(initial_tab=wmod.TAB_ITEM)
    out["after_construct"] = sorted(k for k, m in TOOLS.items() if m in sys.modules)
    w.open_initial_tab()
    out["after_open"] = sorted(k for k, m in TOOLS.items() if m in sys.modules)
    w.select_tab(wmod.TAB_MAP)
    out["after_map"] = sorted(k for k, m in TOOLS.items() if m in sys.modules)
    print("PROBE " + json.dumps(out), flush=True)
    os._exit(0)
''')


def test_real_tools_are_imported_only_when_their_tab_opens(tmp_path):
    code = f"ROOT = {ROOT!r}\nEF_DIR = {EF_DIR!r}\nTMP = {str(tmp_path)!r}\n" + _PROBE
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180, env=env, encoding="utf-8", errors="replace")
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line is not None, f"probe printed nothing (rc={proc.returncode}):\n{proc.stderr[-2000:]}"
    import json
    got = json.loads(line[6:])
    assert got["after_construct"] == [], got
    assert got["after_open"] == ["item"], got
    assert got["after_map"] == ["item", "map"], got
