"""Each tab button of the Assistant window says what its tool does, and its hotkey.

J, 2026-10-05: "Can we also have a tool tip pop up when users hover over each tool that summarizes
its functionality?" The launcher has one tile for this window (SuitMk2 is hidden, a tab of it), so
the two tools are told apart here, on the tab buttons. The text is the "summary" in each tool's own
skill.json, the same sentence its launcher tile shows (shared/tool_tips.py).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

A_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REPO = os.path.normpath(os.path.join(A_ROOT, "..", ".."))

from shared import hotkey_label as hl  # noqa: E402
from shared.tool_tips import WRAP  # noqa: E402

DIRS = {"assistant": A_ROOT, "suitmk2": os.path.join(REPO, "tools", "SuitMk2")}


def _entry():
    """toolbox_assistant_app.py as a module, leaving this process's sys.path and ``ui`` as they were."""
    name = "toolbox_assistant_app_tab_tooltips"
    saved_path, saved_ui = list(sys.path), sys.modules.get("ui")
    spec = importlib.util.spec_from_file_location(name, os.path.join(A_ROOT, "toolbox_assistant_app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
        if saved_ui is not None:
            sys.modules["ui"] = saved_ui
    return mod


def _summary(key: str) -> str:
    with open(os.path.join(DIRS[key], "skill.json"), encoding="utf-8") as f:
        return json.load(f)["summary"]


def _settings(monkeypatch, tmp_path, data: dict) -> None:
    f = tmp_path / "skill_launcher_settings.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(hl, "SETTINGS_FILE", str(f))
    monkeypatch.setattr(hl, "_LEGACY_SETTINGS_FILE", str(tmp_path / "no_legacy.json"))


def test_each_tab_says_what_its_tool_does_and_its_hotkey(monkeypatch, tmp_path):
    monkeypatch.delenv("SC_TOOLBOX_TABS_OFF", raising=False)
    _settings(monkeypatch, tmp_path, {})
    tabs = {t.key: t.tooltip for t in _entry().build_tabs(None)}
    assert set(tabs) == {"assistant", "suitmk2"}
    for key, default in (("assistant", "Hotkey: Ctrl+3"), ("suitmk2", "Hotkey: Ctrl+2")):
        lines = tabs[key].split("\n")
        assert " ".join(lines[:-1]) == _summary(key), f"{key}: the tab does not say what the tool does"
        assert lines[-1] == default
        assert max(map(len, lines)) <= WRAP
    assert _summary("assistant") != _summary("suitmk2")


def test_the_tab_hotkey_is_the_one_saved_in_the_launchers_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("SC_TOOLBOX_TABS_OFF", raising=False)
    _settings(monkeypatch, tmp_path, {"hotkey_suitmk2": "<alt>+9"})
    tabs = {t.key: t.tooltip for t in _entry().build_tabs(None)}
    assert tabs["suitmk2"].split("\n")[-1] == "Hotkey: Alt+9"
    assert tabs["assistant"].split("\n")[-1] == "Hotkey: Ctrl+3"


def test_a_tab_whose_tool_has_no_summary_still_shows_its_hotkey(monkeypatch, tmp_path):
    monkeypatch.delenv("SC_TOOLBOX_TABS_OFF", raising=False)
    _settings(monkeypatch, tmp_path, {})
    e = _entry()
    monkeypatch.setattr(e, "SUIT_DIR", str(tmp_path / "no_such_tool"))
    tabs = {t.key: t.tooltip for t in e.build_tabs(None)}
    assert tabs["suitmk2"] == "Hotkey: Ctrl+2"
