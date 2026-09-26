"""The shared Star Citizen install setting and its first-launch popup."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from shared import sc_install


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the shared settings file at a temp dir (never the user's real one)."""
    monkeypatch.setattr(sc_install, "SHARED_DIR", tmp_path / "store")
    monkeypatch.setattr(sc_install, "SHARED_FILE", tmp_path / "store" / "shared_settings.json")
    return tmp_path


@pytest.fixture
def install(tmp_path):
    """A fake install: StarCitizen/{LIVE,PTU}/Game.log, PTU written more recently."""
    root = tmp_path / "Games" / "StarCitizen"
    for ch, mtime in (("LIVE", 1_000_000), ("PTU", 2_000_000)):
        (root / ch).mkdir(parents=True)
        log = root / ch / "Game.log"
        log.write_text("x")
        os.utime(log, (mtime, mtime))
    return root


def test_every_shape_a_user_picks_normalises_to_the_install_root(install):
    want = str(install).replace("\\", "/")
    assert sc_install.normalise_root(install) == want
    assert sc_install.normalise_root(install / "LIVE") == want
    assert sc_install.normalise_root(install / "LIVE" / "Game.log") == want
    (install / "LIVE" / "logbackups").mkdir()
    assert sc_install.normalise_root(install / "LIVE" / "logbackups") == want


def test_a_folder_without_channels_is_refused(tmp_path, store):
    (tmp_path / "Downloads").mkdir()
    assert sc_install.normalise_root(tmp_path / "Downloads") is None
    with pytest.raises(ValueError):
        sc_install.set_sc_root(tmp_path / "Downloads")


def test_newest_game_log_picks_the_most_recently_played_channel(install):
    assert sc_install.newest_game_log(str(install)).endswith("PTU/Game.log")
    assert sc_install.newest_game_log(None) is None


def test_prompt_shows_once_then_never_after_confirm(store, install):
    assert sc_install.first_launch_prompt_needed()
    sc_install.set_sc_root(install / "LIVE")
    assert not sc_install.first_launch_prompt_needed()
    assert sc_install.get_sc_root() == str(install).replace("\\", "/")


def test_skip_stops_the_prompt_without_saving_a_root(store):
    sc_install.mark_prompt_done()
    assert not sc_install.first_launch_prompt_needed()
    assert sc_install.get_sc_root() is None


def test_a_saved_root_that_was_uninstalled_reads_as_none(store, install):
    sc_install.set_sc_root(install)
    for ch in ("LIVE", "PTU"):
        (install / ch / "Game.log").unlink()
        (install / ch).rmdir()
    assert sc_install.get_sc_root() is None


def test_corrupt_store_is_treated_as_empty(store):
    sc_install.SHARED_DIR.mkdir(parents=True)
    sc_install.SHARED_FILE.write_text("{not json")
    assert sc_install.first_launch_prompt_needed()


# ── the popup ──

@pytest.fixture(scope="module")
def app():
    PySide6 = pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_popup_confirm_saves_the_detected_root(app, store, install):
    from shared.qt.sc_path_dialog import ScPathDialog
    dlg = ScPathDialog(detected=str(install))
    assert dlg._confirm.isEnabled()
    dlg._on_confirm()
    assert dlg.root == str(install).replace("\\", "/")
    assert sc_install.get_sc_root() == dlg.root


def test_popup_with_nothing_detected_cannot_confirm(app, store):
    from shared.qt.sc_path_dialog import ScPathDialog
    dlg = ScPathDialog(detected=None)
    assert not dlg._confirm.isEnabled()
    dlg._on_skip()
    assert not sc_install.first_launch_prompt_needed()
    assert sc_install.get_sc_root() is None


def test_popup_is_not_shown_again_once_answered(app, store, install, monkeypatch):
    from shared.qt import sc_path_dialog
    sc_install.set_sc_root(install)
    monkeypatch.setattr(sc_path_dialog, "ScPathDialog",
                        lambda *a, **k: pytest.fail("popup shown after it was answered"))
    assert sc_path_dialog.maybe_prompt_for_sc_path() == str(install).replace("\\", "/")
