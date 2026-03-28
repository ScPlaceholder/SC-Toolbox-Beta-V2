"""Tests for shared.config_models -- WindowGeometry, SkillConfig, and helpers."""

import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

from shared.config_models import (  # noqa: E402
    _safe_int,
    _safe_float,
    _clamp,
    WindowGeometry,
    SkillConfig,
)


# ---------------------------------------------------------------------------
# _safe_int
# ---------------------------------------------------------------------------

class TestSafeInt:
    def test_valid_int(self):
        assert _safe_int(42, 0) == 42

    def test_valid_string(self):
        assert _safe_int("42", 0) == 42

    def test_none_returns_default(self):
        assert _safe_int(None, 7) == 7

    def test_garbage_returns_default(self):
        assert _safe_int("not_a_number", 99) == 99


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

class TestSafeFloat:
    def test_valid_float(self):
        assert _safe_float(3.14, 0.0) == 3.14

    def test_valid_string(self):
        assert _safe_float("3.14", 0.0) == 3.14

    def test_none_returns_default(self):
        assert _safe_float(None, 1.5) == 1.5

    def test_garbage_returns_default(self):
        assert _safe_float("xyz", 2.0) == 2.0


# ---------------------------------------------------------------------------
# _clamp
# ---------------------------------------------------------------------------

class TestClamp:
    def test_value_in_range(self):
        assert _clamp(5, 0, 10) == 5

    def test_below_min(self):
        assert _clamp(-3, 0, 10) == 0

    def test_above_max(self):
        assert _clamp(15, 0, 10) == 10


# ---------------------------------------------------------------------------
# WindowGeometry.from_dict
# ---------------------------------------------------------------------------

class TestWindowGeometryFromDict:
    def test_complete_dict(self):
        data = {"x": 50, "y": 60, "w": 900, "h": 700, "opacity": 0.8}
        geom = WindowGeometry.from_dict(data)
        assert geom.x == 50
        assert geom.y == 60
        assert geom.w == 900
        assert geom.h == 700
        assert geom.opacity == 0.8

    def test_missing_fields_use_class_defaults(self):
        geom = WindowGeometry.from_dict({})
        assert geom.x == 100
        assert geom.y == 100
        assert geom.w == 1300
        assert geom.h == 800
        assert geom.opacity == 0.95

    def test_prefixed_keys(self):
        data = {"dps_x": 10, "dps_y": 20, "dps_w": 640, "dps_h": 480, "dps_opacity": 0.5}
        geom = WindowGeometry.from_dict(data, prefix="dps_")
        assert geom.x == 10
        assert geom.y == 20
        assert geom.w == 640
        assert geom.h == 480
        assert geom.opacity == 0.5

    def test_custom_defaults(self):
        custom = WindowGeometry(x=1, y=2, w=300, h=200, opacity=0.5)
        geom = WindowGeometry.from_dict({}, defaults=custom)
        assert geom.x == 1
        assert geom.y == 2
        assert geom.w == 300
        assert geom.h == 200
        assert geom.opacity == 0.5


# ---------------------------------------------------------------------------
# WindowGeometry.clamp_to_screen
# ---------------------------------------------------------------------------

class TestWindowGeometryClampToScreen:
    def test_normal_case_within_bounds(self):
        geom = WindowGeometry(x=50, y=50, w=400, h=300)
        clamped = geom.clamp_to_screen(1920, 1080)
        assert clamped.x == 50
        assert clamped.y == 50
        assert clamped.w == 400
        assert clamped.h == 300

    def test_window_too_big_clamps_position(self):
        """When the window is larger than the screen, x and y should be 0."""
        geom = WindowGeometry(x=500, y=500, w=2000, h=1200)
        clamped = geom.clamp_to_screen(1920, 1080)
        assert clamped.x == 0
        assert clamped.y == 0
        assert clamped.w == 2000
        assert clamped.h == 1200

    def test_negative_coords_clamped_to_zero(self):
        geom = WindowGeometry(x=-100, y=-200, w=400, h=300)
        clamped = geom.clamp_to_screen(1920, 1080)
        assert clamped.x == 0
        assert clamped.y == 0


# ---------------------------------------------------------------------------
# WindowGeometry.as_args
# ---------------------------------------------------------------------------

class TestWindowGeometryAsArgs:
    def test_returns_correct_string_list(self):
        geom = WindowGeometry(x=10, y=20, w=800, h=600, opacity=0.9)
        args = geom.as_args()
        assert args == ["10", "20", "800", "600", "0.9"]

    def test_default_geometry_as_args(self):
        geom = WindowGeometry()
        args = geom.as_args()
        assert args == ["100", "100", "1300", "800", "0.95"]


# ---------------------------------------------------------------------------
# SkillConfig.from_dict
# ---------------------------------------------------------------------------

class TestSkillConfigFromDict:
    def test_all_fields_present(self):
        data = {
            "id": "trade",
            "name": "Trade Hub",
            "icon": "cart.png",
            "color": "#00ff00",
            "folder": "Trade_Hub",
            "script": "main.py",
            "hotkey": "<ctrl>+t",
            "settings_key": "hotkey_trade",
            "custom_args": ["--verbose", "--port=8080"],
        }
        cfg = SkillConfig.from_dict(data)
        assert cfg.id == "trade"
        assert cfg.name == "Trade Hub"
        assert cfg.icon == "cart.png"
        assert cfg.color == "#00ff00"
        assert cfg.folder == "Trade_Hub"
        assert cfg.script == "main.py"
        assert cfg.hotkey == "<ctrl>+t"
        assert cfg.settings_key == "hotkey_trade"
        assert cfg.custom_args == ["--verbose", "--port=8080"]

    def test_missing_optional_fields_get_defaults(self):
        data = {
            "id": "test",
            "name": "Test Skill",
            "icon": "test.png",
            "color": "#fff",
            "folder": "test_folder",
            "script": "run.py",
        }
        cfg = SkillConfig.from_dict(data)
        assert cfg.hotkey == ""
        assert cfg.settings_key == ""
        assert cfg.custom_args == []

    def test_coerces_non_string_values(self):
        data = {
            "id": 123,
            "name": 456,
            "icon": True,
            "color": None,
            "folder": 0,
            "script": 3.14,
        }
        cfg = SkillConfig.from_dict(data)
        assert cfg.id == "123"
        assert cfg.name == "456"
        assert cfg.icon == "True"
        assert cfg.color == "None"
        assert cfg.folder == "0"
        assert cfg.script == "3.14"


# ---------------------------------------------------------------------------
# SkillConfig.to_dict
# ---------------------------------------------------------------------------

class TestSkillConfigToDict:
    def test_round_trip(self):
        data = {
            "id": "mining",
            "name": "Mining Tool",
            "icon": "pick.png",
            "color": "#aabbcc",
            "folder": "Mining",
            "script": "mine.py",
            "hotkey": "<alt>+m",
            "settings_key": "hotkey_mining",
            "custom_args": ["--depth=5"],
        }
        cfg = SkillConfig.from_dict(data)
        result = cfg.to_dict()
        assert result["id"] == "mining"
        assert result["name"] == "Mining Tool"
        assert result["custom_args"] == ["--depth=5"]

    def test_omits_empty_custom_args(self):
        cfg = SkillConfig(
            id="a", name="b", icon="c", color="d", folder="e", script="f"
        )
        result = cfg.to_dict()
        assert "custom_args" not in result

    def test_includes_custom_args_when_non_empty(self):
        cfg = SkillConfig(
            id="a", name="b", icon="c", color="d", folder="e", script="f",
            custom_args=["--flag"],
        )
        result = cfg.to_dict()
        assert result["custom_args"] == ["--flag"]
