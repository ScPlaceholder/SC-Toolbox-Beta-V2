"""Tests for config validation and persistence."""
import json
import os
import sys
import tempfile
import pytest

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

from models.items import NONE_GADGET, NONE_LASER, NONE_MODULE
from services.config_service import (
    CONFIG_VERSION,
    _validate_config,
    load_config,
    save_config,
)


class TestValidateConfig:
    def test_defaults_for_empty(self):
        cfg = _validate_config({})
        assert cfg["version"] == CONFIG_VERSION
        assert cfg["ship"] == "MOLE"
        assert cfg["hotkey"] == "ctrl+shift+m"
        assert cfg["gadget"] == NONE_GADGET

    def test_preserves_valid_ship(self):
        cfg = _validate_config({"ship": "Golem"})
        assert cfg["ship"] == "Golem"

    def test_rejects_unknown_ship(self):
        cfg = _validate_config({"ship": "Carrack"})
        assert cfg["ship"] == "MOLE"

    def test_preserves_hotkey(self):
        cfg = _validate_config({"hotkey": "ctrl+alt+k"})
        assert cfg["hotkey"] == "ctrl+alt+k"

    def test_loadout_validation(self):
        raw = {
            "ship": "Prospector",
            "loadout": {
                "turret_0": {
                    "laser": "Test Laser",
                    "modules": ["Mod1"],
                },
            },
        }
        cfg = _validate_config(raw)
        t0 = cfg["loadout"]["turret_0"]
        assert t0["laser"] == "Test Laser"
        assert len(t0["modules"]) == 2
        assert t0["modules"][0] == "Mod1"
        assert t0["modules"][1] == NONE_MODULE

    def test_v1_migration(self):
        """Config without version key should be treated as v1 and migrated."""
        cfg = _validate_config({"ship": "Golem"})
        assert cfg["version"] == CONFIG_VERSION


class TestLoadSaveConfig:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "test_config.json")
        save_config(
            ship="Golem",
            hotkey="ctrl+shift+g",
            turret_lasers=["Pitman Mining Laser"],
            turret_modules=[["Focus II Module", "Focus III Module"]],
            gadget=NONE_GADGET,
            path=path,
        )
        cfg = load_config(path)
        assert cfg["ship"] == "Golem"
        assert cfg["hotkey"] == "ctrl+shift+g"
        assert cfg["loadout"]["turret_0"]["laser"] == "Pitman Mining Laser"
        assert cfg["loadout"]["turret_0"]["modules"] == ["Focus II Module", "Focus III Module"]

    def test_load_missing_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        cfg = load_config(path)
        assert cfg["ship"] == "MOLE"
        assert cfg["version"] == CONFIG_VERSION

    def test_load_corrupt_file(self, tmp_path):
        path = str(tmp_path / "corrupt.json")
        with open(path, "w") as f:
            f.write("not json{{{")
        cfg = load_config(path)
        assert cfg["ship"] == "MOLE"

    def test_saved_version(self, tmp_path):
        path = str(tmp_path / "test_config.json")
        save_config("MOLE", "ctrl+shift+m", [], [], NONE_GADGET, path)
        with open(path) as f:
            raw = json.load(f)
        assert raw["version"] == CONFIG_VERSION
