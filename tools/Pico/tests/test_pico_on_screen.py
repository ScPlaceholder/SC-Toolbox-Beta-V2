"""Pico himself must be on a screen, not just his window's corner, and the launcher's UI scale must not
reach him. J 2026-10-04, first launch from the tile: "why is pico pals have a giant square" and "also the
pico is off screen". The launcher starts tools with QT_SCALE_FACTOR (his is 1.5); it multiplied Pico's saved
position past the right edge, and the old check only asked whether the window's top-left corner was on a
screen, which it was."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PySide6.QtCore import QPoint, QRect, QSize  # noqa: E402

import pico_pals_app  # noqa: E402
import sprite_pal  # noqa: E402


class _Screen:
    def __init__(self, x, y, w, h):
        self._r = QRect(x, y, w, h)

    def availableGeometry(self):
        return self._r


MAIN = [_Screen(0, 0, 3840, 2160)]
WINDOW = QSize(1140, 483)            # the size J's off-screen window really had


def test_corner_on_screen_but_pico_off_is_refused():
    # exactly J's case: window at 3359,1601 -> corner on the main screen, Pico's centre at x=3929, past 3840
    assert MAIN[0].availableGeometry().contains(QPoint(3359, 1601))
    assert not sprite_pal.pico_on_a_screen(MAIN, QPoint(3359, 1601), WINDOW)


def test_pico_on_screen_is_accepted_even_if_the_window_overhangs():
    # the transparent margin may hang over the edge; what matters is where he is
    assert sprite_pal.pico_on_a_screen(MAIN, QPoint(3000, 1601), WINDOW)


def test_a_second_screen_counts():
    two = MAIN + [_Screen(3840, 0, 1280, 1024)]
    assert sprite_pal.pico_on_a_screen(two, QPoint(3359, 300), WINDOW)


def test_the_adapter_drops_the_launchers_ui_scale(monkeypatch):
    seen = {}

    def fake_main(args, on_ready=None):
        seen["scale"] = os.environ.get("QT_SCALE_FACTOR")
        return 0

    monkeypatch.setenv("QT_SCALE_FACTOR", "1.5")
    monkeypatch.setattr(sprite_pal, "main", fake_main)
    monkeypatch.setattr(pico_pals_app, "toolbox_game_log", lambda: None)
    assert pico_pals_app.main(["--demo"]) == 0
    assert seen["scale"] is None


AREA = QRect(0, 0, 3840, 2112)       # the main screen minus a taskbar


def test_no_saved_spot_starts_him_in_the_middle_of_the_main_screen():
    pos = sprite_pal.start_position(MAIN, AREA, {}, WINDOW)
    centre = QPoint(pos.x() + WINDOW.width() // 2, pos.y() + WINDOW.height() // 2)
    assert abs(centre.x() - AREA.center().x()) <= 1 and abs(centre.y() - AREA.center().y()) <= 1


def test_a_saved_spot_that_keeps_him_on_screen_is_kept():
    assert sprite_pal.start_position(MAIN, AREA, {"x": 500, "y": 700}, WINDOW) == QPoint(500, 700)


def test_a_saved_spot_that_puts_him_off_screen_goes_back_to_the_middle():
    pos = sprite_pal.start_position(MAIN, AREA, {"x": 3359, "y": 1601}, WINDOW)
    assert sprite_pal.pico_on_a_screen(MAIN, pos, WINDOW)
    assert abs(pos.x() + WINDOW.width() // 2 - AREA.center().x()) <= 1


def test_a_corrupt_saved_spot_does_not_stop_him_starting():
    pos = sprite_pal.start_position(MAIN, AREA, {"x": "left", "y": None}, WINDOW)
    assert sprite_pal.pico_on_a_screen(MAIN, pos, WINDOW)
