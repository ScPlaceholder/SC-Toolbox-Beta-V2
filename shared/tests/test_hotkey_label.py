"""Title-bar hotkey labels follow the launcher's real binding."""
import json

from shared import hotkey_label as hl


def test_formats_launcher_bindings():
    assert hl.format_hotkey("<ctrl>+1") == "Ctrl+1"
    assert hl.format_hotkey("<shift>+7") == "Shift+7"
    assert hl.format_hotkey("<ctrl>+<shift>+f5") == "Ctrl+Shift+F5"
    assert hl.format_hotkey("<alt>+q") == "Alt+Q"
    assert hl.format_hotkey("<shift>+`") == "Shift+`"


def test_a_rebound_key_shows_the_new_binding(tmp_path, monkeypatch):
    f = tmp_path / "skill_launcher_settings.json"
    f.write_text(json.dumps({"hotkey_mining_signals": "<shift>+9"}))
    monkeypatch.setattr(hl, "SETTINGS_FILE", str(f))
    assert hl.hotkey_label("hotkey_mining_signals", "<ctrl>+1") == "Shift+9"


def test_missing_or_broken_settings_fall_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(hl, "SETTINGS_FILE", str(tmp_path / "nope.json"))
    assert hl.hotkey_label("hotkey_mining_signals", "<ctrl>+1") == "Ctrl+1"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(hl, "SETTINGS_FILE", str(bad))
    assert hl.hotkey_label("hotkey_playtime", "<ctrl>+4") == "Ctrl+4"
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"hotkey_playtime": ""}))
    monkeypatch.setattr(hl, "SETTINGS_FILE", str(empty))
    assert hl.hotkey_label("hotkey_playtime", "<ctrl>+4") == "Ctrl+4"
