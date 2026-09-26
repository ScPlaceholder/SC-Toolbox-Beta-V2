"""The HUD must never open off-screen (J, 2026-09-25: it got lost on monitors
with a different resolution than the one its defaults assumed)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _bar(x, y):
    w = QWidget()
    w.resize(300, 40)
    w.move(x, y)
    return w


def _centre_of_primary(w):
    from PySide6.QtGui import QGuiApplication
    g = QGuiApplication.primaryScreen().availableGeometry()
    return (g.center().x() - w.width() // 2, g.center().y() - w.height() // 2)


def test_first_launch_centres_on_the_primary_screen(app):
    from hud_app import _place_on_screen
    w = _bar(60, 880)                       # the old WingmanAI default
    _place_on_screen(w, has_saved_position=False)
    assert (w.x(), w.y()) == _centre_of_primary(w)


def test_a_saved_position_off_every_screen_is_rescued(app):
    from hud_app import _place_on_screen
    w = _bar(5000, 5000)                    # e.g. saved on an unplugged second monitor
    _place_on_screen(w, has_saved_position=True)
    assert (w.x(), w.y()) == _centre_of_primary(w)


def test_a_saved_position_on_screen_is_kept(app):
    from hud_app import _place_on_screen
    w = _bar(20, 30)
    _place_on_screen(w, has_saved_position=True)
    assert (w.x(), w.y()) == (20, 30)


def test_mostly_off_screen_counts_as_lost(app):
    from hud_app import _place_on_screen
    from PySide6.QtGui import QGuiApplication
    g = QGuiApplication.primaryScreen().availableGeometry()
    w = _bar(g.right() - 50, 30)            # only 50 of 300 px visible
    _place_on_screen(w, has_saved_position=True)
    assert (w.x(), w.y()) == _centre_of_primary(w)


def test_settings_live_outside_the_install_folder():
    from ui import options_popup
    assert ".sctoolbox" in options_popup._SETTINGS_FILE
    here = os.path.dirname(os.path.dirname(os.path.abspath(options_popup.__file__)))
    assert not os.path.abspath(options_popup._SETTINGS_FILE).startswith(here)


def test_legacy_settings_file_is_migrated(tmp_path, monkeypatch):
    import json
    from ui import options_popup
    new, old = tmp_path / "new" / "settings.json", tmp_path / "battle_buddy_settings.json"
    old.write_text(json.dumps({"window_x": 11, "window_y": 22, "log_path": str(old)}))
    monkeypatch.setattr(options_popup, "_SETTINGS_FILE", str(new))
    monkeypatch.setattr(options_popup, "_LEGACY_SETTINGS_FILE", str(old))
    s = options_popup.load_settings()
    assert (s["window_x"], s["window_y"]) == (11, 22)
    assert json.loads(new.read_text())["window_x"] == 11
