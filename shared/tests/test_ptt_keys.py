"""The two push-to-talk keys read the same way whichever tool's format they are saved in (shared/ptt_keys.py)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)

from shared import ptt_keys  # noqa: E402


def test_the_same_key_is_the_same_in_either_tools_format():
    assert ptt_keys.ident({"kind": "key", "code": "z"}) == ptt_keys.ident({"kind": "keyboard", "code": 90, "label": "z"})
    assert ptt_keys.ident({"kind": "key", "code": "scroll_lock"}) == ptt_keys.ident(ptt_keys.SUIT_DEFAULT)
    assert ptt_keys.ident({"kind": "key", "code": "Key.pause"}) == ptt_keys.ident(ptt_keys.ASSISTANT_DEFAULT)
    assert ptt_keys.ident({"kind": "mouse", "code": "Button.x1"}) == \
        ptt_keys.ident({"kind": "mouse", "code": "Button.x1", "label": "MOUSE X1"})
    assert ptt_keys.ident({"kind": "key", "code": "z"}) != ptt_keys.ident({"kind": "key", "code": "x"})
    assert ptt_keys.ident({"kind": "key", "code": "x1"}) != ptt_keys.ident({"kind": "mouse", "code": "Button.x1"})
    assert ptt_keys.ident(None) is None and ptt_keys.ident({}) is None and ptt_keys.ident({"kind": "key"}) is None


def test_keys_are_named_the_way_a_person_reads_them():
    assert ptt_keys.label(ptt_keys.ASSISTANT_DEFAULT) == "Pause"
    assert ptt_keys.label(ptt_keys.SUIT_DEFAULT) == "Scroll Lock"
    assert ptt_keys.label({"kind": "key", "code": "z"}) == "Z"
    assert ptt_keys.label({"kind": "keyboard", "code": 120, "label": "F9"}) == "F9"
    assert ptt_keys.label({"kind": "mouse", "code": "Button.x1"}) == "Mouse X1"
    assert ptt_keys.label({"kind": "joystick", "code": 3, "label": "Stick btn 3", "joy_index": 1}) == "Stick btn 3"
    assert ptt_keys.label(None) == ""


def test_a_tool_with_no_key_saved_gets_its_default_and_a_saved_key_is_kept():
    assert ptt_keys.assistant_binding({}) == ptt_keys.ASSISTANT_DEFAULT
    assert ptt_keys.assistant_binding(None) == ptt_keys.ASSISTANT_DEFAULT
    assert ptt_keys.assistant_binding({"binding": {"kind": "key", "code": "z"}}) == {"kind": "key", "code": "z"}
    assert ptt_keys.suit_binding({"talk_key": None}) == ptt_keys.SUIT_DEFAULT
    assert ptt_keys.suit_binding({}) == ptt_keys.SUIT_DEFAULT
    kept = {"kind": "keyboard", "code": 88, "label": "x", "joy_index": 0}
    assert ptt_keys.suit_binding({"talk_key": kept}) == kept


def test_both_saved_keys_are_read_from_the_two_tools_own_files(tmp_path, monkeypatch):
    a, s = tmp_path / "assistant_panel.json", tmp_path / "settings.json"
    monkeypatch.setattr(ptt_keys, "_ASSISTANT_STATE", str(a))
    monkeypatch.setattr(ptt_keys, "_SUIT_SETTINGS", str(s))
    assert ptt_keys.saved_labels() == {"assistant": "Pause", "suitmk2": "Scroll Lock"}      # neither file exists
    a.write_text(json.dumps({"binding": {"kind": "key", "code": "z"}}), encoding="utf-8")
    s.write_text("{ not json", encoding="utf-8")
    assert ptt_keys.saved_labels() == {"assistant": "Z", "suitmk2": "Scroll Lock"}
    s.write_text(json.dumps({"talk_key": {"kind": "keyboard", "code": 120, "label": "F9"}}), encoding="utf-8")
    assert ptt_keys.saved_labels() == {"assistant": "Z", "suitmk2": "F9"}


def test_suitmk2s_own_copy_of_its_default_is_the_same_dict():
    path = os.path.join(REPO, "tools", "SuitMk2", "core", "settings.py")
    spec = importlib.util.spec_from_file_location("_suitmk2_settings_for_ptt_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DEFAULT_TALK_KEY == ptt_keys.SUIT_DEFAULT
