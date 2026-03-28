import os, sys
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
import shared.path_setup
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

import json
import pytest

from core.skill_registry import (
    _try_load_skill_json,
    discover_skills,
    resolve_skill_path,
    resolve_script_path,
)
from shared.config_models import SkillConfig


def _make_skill_json(directory, data):
    """Write a skill.json into *directory* and return the path."""
    path = os.path.join(directory, "skill.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _valid_skill_data(**overrides):
    base = {
        "id": "test_skill",
        "name": "Test Skill",
        "icon": "T",
        "color": "#123456",
        "folder": "Test_Skill",
        "script": "test_app.py",
        "hotkey": "<shift>+9",
        "settings_key": "hotkey_test",
    }
    base.update(overrides)
    return base


# ── _try_load_skill_json ────────────────────────────────────────────────────


def test_try_load_skill_json_valid(tmp_path):
    data = _valid_skill_data()
    _make_skill_json(tmp_path, data)
    cfg = _try_load_skill_json(str(tmp_path))
    assert cfg is not None
    assert isinstance(cfg, SkillConfig)
    assert cfg.id == "test_skill"
    assert cfg.script == "test_app.py"
    assert cfg.name == "Test Skill"


def test_try_load_skill_json_missing_file(tmp_path):
    assert _try_load_skill_json(str(tmp_path)) is None


def test_try_load_skill_json_invalid_json(tmp_path):
    bad = tmp_path / "skill.json"
    bad.write_text("{not valid json!!!", encoding="utf-8")
    assert _try_load_skill_json(str(tmp_path)) is None


def test_try_load_skill_json_missing_id(tmp_path):
    data = _valid_skill_data()
    del data["id"]  # from_dict defaults missing id to "", which is falsy
    _make_skill_json(tmp_path, data)
    assert _try_load_skill_json(str(tmp_path)) is None


# ── discover_skills ─────────────────────────────────────────────────────────


def test_discover_skills_with_json(tmp_path):
    base = tmp_path / "project"
    skills_dir = base / "skills"

    # Create two skill folders with valid skill.json
    for name, sid in [("Alpha_Skill", "alpha"), ("Beta_Skill", "beta")]:
        d = skills_dir / name
        d.mkdir(parents=True)
        _make_skill_json(str(d), _valid_skill_data(id=sid, name=name, folder=name, script="app.py"))

    result = discover_skills(str(base))
    ids = [s.id for s in result]
    assert "alpha" in ids
    assert "beta" in ids
    assert len(result) == 2


def test_discover_skills_fallback_builtin(tmp_path):
    base = tmp_path / "project"
    skills_dir = base / "skills"

    # Create a folder matching a built-in skill but without skill.json
    (skills_dir / "Trade_Hub").mkdir(parents=True)

    result = discover_skills(str(base))
    ids = [s.id for s in result]
    assert "trade" in ids
    # Should use the built-in metadata
    trade = [s for s in result if s.id == "trade"][0]
    assert trade.script == "trade_hub_app.py"


def test_discover_skills_empty(tmp_path):
    base = tmp_path / "project"
    (base / "skills").mkdir(parents=True)
    result = discover_skills(str(base))
    assert result == []


# ── resolve_skill_path ──────────────────────────────────────────────────────


def test_resolve_skill_path_found(tmp_path):
    base = tmp_path / "project"
    skill_dir = base / "skills" / "My_Skill"
    skill_dir.mkdir(parents=True)

    skill = SkillConfig(
        id="my", name="My Skill", icon="M", color="#000",
        folder="My_Skill", script="app.py",
    )
    result = resolve_skill_path(skill, str(base))
    assert result is not None
    assert os.path.basename(result) == "My_Skill"


def test_resolve_skill_path_not_found(tmp_path):
    base = tmp_path / "project"
    (base / "skills").mkdir(parents=True)

    skill = SkillConfig(
        id="nope", name="Nope", icon="N", color="#000",
        folder="Nonexistent", script="app.py",
    )
    assert resolve_skill_path(skill, str(base)) is None


# ── resolve_script_path ─────────────────────────────────────────────────────


def test_resolve_script_path_found(tmp_path):
    base = tmp_path / "project"
    skill_dir = base / "skills" / "My_Skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "app.py").write_text("# entry", encoding="utf-8")

    skill = SkillConfig(
        id="my", name="My Skill", icon="M", color="#000",
        folder="My_Skill", script="app.py",
    )
    result = resolve_script_path(skill, str(base))
    assert result is not None
    assert result.endswith("app.py")
    assert os.path.isfile(result)


def test_resolve_script_path_not_found(tmp_path):
    base = tmp_path / "project"
    skill_dir = base / "skills" / "My_Skill"
    skill_dir.mkdir(parents=True)
    # Folder exists but script file does not

    skill = SkillConfig(
        id="my", name="My Skill", icon="M", color="#000",
        folder="My_Skill", script="missing.py",
    )
    assert resolve_script_path(skill, str(base)) is None
