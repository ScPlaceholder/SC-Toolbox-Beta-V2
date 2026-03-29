"""
Configuration data models with validation.

Uses pure dataclasses (no external dependencies) to validate settings,
skill configs, and window geometry.  Each model provides a ``from_dict``
class method that applies defaults and type coercion.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(val, hi))


# ── Window geometry ──────────────────────────────────────────────────────────

@dataclass
class WindowGeometry:
    x: int = 100
    y: int = 100
    w: int = 1300
    h: int = 800
    opacity: float = 0.95

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        prefix: str = "",
        defaults: WindowGeometry | None = None,
    ) -> WindowGeometry:
        """Build from a flat dict, optionally prefixed (e.g. ``dps_x``)."""
        d = defaults or cls()
        return cls(
            x=_safe_int(data.get(f"{prefix}x", d.x), d.x),
            y=_safe_int(data.get(f"{prefix}y", d.y), d.y),
            w=_safe_int(data.get(f"{prefix}w", d.w), d.w),
            h=_safe_int(data.get(f"{prefix}h", d.h), d.h),
            opacity=_safe_float(data.get(f"{prefix}opacity", d.opacity), d.opacity),
        )

    def clamp_to_screen(self, screen_w: int, screen_h: int) -> WindowGeometry:
        return WindowGeometry(
            x=_clamp(self.x, 0, max(0, screen_w - self.w)),
            y=_clamp(self.y, 0, max(0, screen_h - self.h)),
            w=self.w,
            h=self.h,
            opacity=self.opacity,
        )

    def as_args(self) -> list[str]:
        return [str(self.x), str(self.y), str(self.w), str(self.h), str(self.opacity)]


# ── Skill definition ─────────────────────────────────────────────────────────

@dataclass
class SkillConfig:
    id: str
    name: str
    icon: str
    color: str
    folder: str
    script: str
    hotkey: str = ""
    settings_key: str = ""
    custom_args: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillConfig:
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            icon=str(data.get("icon", "")),
            color=str(data.get("color", "#ffffff")),
            folder=str(data.get("folder", "")),
            script=str(data.get("script", "")),
            hotkey=str(data.get("hotkey", "")),
            settings_key=str(data.get("settings_key", "")),
            custom_args=list(data.get("custom_args", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "color": self.color,
            "folder": self.folder,
            "script": self.script,
            "hotkey": self.hotkey,
            "settings_key": self.settings_key,
        }
        if self.custom_args:
            d["custom_args"] = self.custom_args
        return d


# ── Launcher settings ────────────────────────────────────────────────────────

@dataclass
class LauncherSettings:
    hotkey_launcher: str = "<shift>+`"
    language: str = "en"
    scroll_on_hover: bool = False  # scroll wheel adjusts spinboxes/sliders on hover
    grid_rows: int = 3
    grid_cols: int = 2
    launcher_opacity: float = 0.95  # persisted across sessions
    ui_scale: float = 1.0  # QT_SCALE_FACTOR for high-DPI monitors (0.75 – 3.0)
    disabled_skills: list[str] = field(default_factory=list)
    grid_layout: dict[str, str] = field(default_factory=dict)  # "row,col" -> skill_id
    skill_hotkeys: dict[str, str] = field(default_factory=dict)
    skill_windows: dict[str, WindowGeometry] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any], skills: list[SkillConfig]) -> LauncherSettings:
        hotkey_launcher = str(data.get("hotkey_launcher", "<shift>+`"))
        language = str(data.get("language", "en"))
        scroll_on_hover = bool(data.get("scroll_on_hover", False))
        grid_rows = _clamp(_safe_int(data.get("grid_rows", 3), 3), 1, 10)
        grid_cols = _clamp(_safe_int(data.get("grid_cols", 2), 2), 1, 10)
        launcher_opacity = max(0.3, min(1.0, _safe_float(data.get("launcher_opacity", 0.95), 0.95)))
        ui_scale = max(0.75, min(3.0, _safe_float(data.get("ui_scale", 1.0), 1.0)))
        disabled_skills = list(data.get("disabled_skills", []))
        grid_layout = dict(data.get("grid_layout", {}))

        skill_hotkeys: dict[str, str] = {}
        skill_windows: dict[str, WindowGeometry] = {}
        for skill in skills:
            if skill.settings_key:
                saved_hk = data.get(skill.settings_key, "")
                if saved_hk:
                    skill_hotkeys[skill.id] = str(saved_hk)
            skill_windows[skill.id] = WindowGeometry.from_dict(data, prefix=f"{skill.id}_")

        return cls(
            hotkey_launcher=hotkey_launcher,
            language=language,
            scroll_on_hover=scroll_on_hover,
            grid_rows=grid_rows,
            grid_cols=grid_cols,
            launcher_opacity=launcher_opacity,
            ui_scale=ui_scale,
            disabled_skills=disabled_skills,
            grid_layout=grid_layout,
            skill_hotkeys=skill_hotkeys,
            skill_windows=skill_windows,
            raw=dict(data),
        )

    def to_dict(self) -> dict[str, Any]:
        """Flatten back to the JSON format used on disk."""
        out = dict(self.raw)
        out["hotkey_launcher"] = self.hotkey_launcher
        out["language"] = self.language
        out["scroll_on_hover"] = self.scroll_on_hover
        out["grid_rows"] = self.grid_rows
        out["grid_cols"] = self.grid_cols
        out["launcher_opacity"] = self.launcher_opacity
        out["ui_scale"] = self.ui_scale
        out["disabled_skills"] = self.disabled_skills
        out["grid_layout"] = self.grid_layout
        for sid, hk in self.skill_hotkeys.items():
            # Reconstruct the settings_key — convention is ``hotkey_{id}``
            out[f"hotkey_{sid}"] = hk
        for sid, geom in self.skill_windows.items():
            out[f"{sid}_x"] = geom.x
            out[f"{sid}_y"] = geom.y
            out[f"{sid}_w"] = geom.w
            out[f"{sid}_h"] = geom.h
            out[f"{sid}_opacity"] = geom.opacity
        return out
