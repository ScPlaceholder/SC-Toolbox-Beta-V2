"""Settings live outside the install folder, and an update's shipped copy is never migrated as a user's choice.

Releases 2.2.17 to 2.4.0 put the developer's own settings files inside the package. Velopack replaces the
install folder on update, so the in-folder file an installed user has is very often that shipped copy.
"""
import json
import os

import pytest

from shared import user_settings as US


def _shipped_bytes(name):
    """Bytes whose sha256 we can register as 'shipped' for this test."""
    return json.dumps({"shipped": name}).encode()


@pytest.fixture
def shipped(monkeypatch):
    import hashlib
    data = _shipped_bytes("skill_launcher_settings.json")
    monkeypatch.setitem(US.SHIPPED, "skill_launcher_settings.json", {hashlib.sha256(data).hexdigest()})
    return data


def _installed(tmp_path):
    d = tmp_path / "AppData" / "Local" / "SC_Toolbox" / "current"
    d.mkdir(parents=True)
    return d


def test_the_new_file_wins_when_it_exists(tmp_path):
    new = tmp_path / "new.json"
    new.write_text(json.dumps({"ui_scale": 1.0}))
    legacy = tmp_path / "skill_launcher_settings.json"
    legacy.write_text(json.dumps({"ui_scale": 2.0}))
    assert US.load_json(str(new), legacy=str(legacy)) == {"ui_scale": 1.0}


def test_a_customised_legacy_file_is_migrated(tmp_path):
    legacy = _installed(tmp_path) / "skill_launcher_settings.json"
    legacy.write_text(json.dumps({"ui_scale": 1.25}))
    assert US.load_json(str(tmp_path / "new.json"), legacy=str(legacy)) == {"ui_scale": 1.25}


def test_the_shipped_copy_in_an_install_is_not_migrated(tmp_path, shipped):
    legacy = _installed(tmp_path) / "skill_launcher_settings.json"
    legacy.write_bytes(shipped)
    assert US.load_json(str(tmp_path / "new.json"), legacy=str(legacy)) == {}
    assert US.load_json(str(tmp_path / "new.json"), legacy=str(legacy), default={"d": 1}) == {"d": 1}


def test_the_same_bytes_in_a_developer_checkout_ARE_kept(tmp_path, shipped):
    """The package is built from the developer's file, so there the 'shipped copy' is the owner's settings."""
    legacy = tmp_path / "checkout" / "skill_launcher_settings.json"
    legacy.parent.mkdir()
    legacy.write_bytes(shipped)
    assert US.load_json(str(tmp_path / "new.json"), legacy=str(legacy)) == {"shipped": "skill_launcher_settings.json"}


def test_save_writes_only_the_new_path_and_creates_its_folder(tmp_path):
    new = tmp_path / "home" / ".sctoolbox" / "launcher" / "settings.json"
    legacy = _installed(tmp_path) / "skill_launcher_settings.json"
    legacy.write_text(json.dumps({"ui_scale": 1.25}))
    US.save_json(str(new), {"ui_scale": 1.5})
    assert json.loads(new.read_text()) == {"ui_scale": 1.5}
    assert json.loads(legacy.read_text()) == {"ui_scale": 1.25}
    assert US.load_json(str(new), legacy=str(legacy)) == {"ui_scale": 1.5}


def test_a_garbled_file_falls_back_to_defaults(tmp_path):
    new = tmp_path / "new.json"
    new.write_text("{not json")
    assert US.load_json(str(new), default={"d": 1}) == {"d": 1}


def test_installed_copy_detection():
    assert US.in_installed_copy(os.path.join("C:" + os.sep, "Users", "u", "AppData", "Local", "SC_Toolbox",
                                             "current", "skills", "x.json"))
    assert not US.in_installed_copy(os.path.join("C:" + os.sep, "Users", "u", "SC_Toolbox_Beta_V1.2", "x.json"))


def test_every_real_shipped_hash_table_entry_is_a_sha256():
    for name, digests in US.SHIPPED.items():
        assert digests, name
        for d in digests:
            assert len(d) == 64 and int(d, 16) >= 0, (name, d)
