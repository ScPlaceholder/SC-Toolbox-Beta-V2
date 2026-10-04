"""The window honours a rest: it shows frame 0 of the loop and does not run it.

J 2026-10-04: "He also doesn't need to do an animation every second. He can be idle at times", and "He
can [stand] still at times". pico/sprites.py decides WHEN he rests (its own selftest covers that, on a
fake clock); this file checks the one thing sprite_pal.py has to do about it.

The window is built in a child process, offscreen, with APPDATA pointed at a temp folder: a test must
never show a second Pico on a desktop that already has one, nor touch %APPDATA%\\PicoPal\\settings.json.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pico import sprites  # noqa: E402

CHILD = r'''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import QApplication
import sprite_pal
from pico import sprites

out = {"settings": str(sprite_pal.SETTINGS)}
app = QApplication([])
clock = [1000.0]
chooser = sprites.LoopChooser(sprites.Catalog.scan(sprites.DEFAULT_DIR), clock=lambda: clock[0])
pal = sprite_pal.Pal(chooser, pinned="calm")          # never shown


def spin(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def opaque(pal):
    img = pal.grab().toImage()
    return sum(1 for y in range(0, img.height(), 4) for x in range(0, img.width(), 4)
               if img.pixelColor(x, y).alpha() > 200)


def same_as_frame_0(pal):
    """What the label shows is frame 0 of the resting loop, at his on-screen size."""
    m = QMovie(str(chooser.catalog.loops[chooser.current]))
    m.jumpToFrame(0)
    want = m.currentImage().scaled(pal.movie.scaledSize()).convertToFormat(m.currentImage().format())
    got = pal.pic.pixmap().toImage().convertToFormat(want.format())
    if got.size() != want.size():
        return False
    pts = [(x, y) for y in range(0, want.height(), 7) for x in range(0, want.width(), 7)]
    close = sum(1 for x, y in pts if abs(got.pixelColor(x, y).alpha() - want.pixelColor(x, y).alpha()) < 40)
    return close > 0.97 * len(pts)


out["first_is_animation"] = (not chooser.resting) and pal.movie.state() == QMovie.Running
for _ in range(10):                                   # use up the passes, as the frames would
    if chooser.resting:
        break
    pal.play(chooser.on_loop_end(at=clock[0]))
out["resting"] = chooser.resting
out["rest_loop"] = chooser.current
out["paused"] = pal.movie.state() == QMovie.NotRunning
spin(900)                                             # a running loop would be several frames on by now
out["frame_after_wait"] = pal.movie.currentFrameNumber()
out["still_resting_after_wait"] = chooser.resting     # the 1 s tick ran: fake clock, so no time has passed
out["opaque_px_at_rest"] = opaque(pal)
out["rest_is_frame_0"] = same_as_frame_0(pal)
clock[0] = chooser.rest_until + 1.0                   # the rest is over: the window's own tick must notice
out["resumed"] = False
for _ in range(40):                                   # polled: a one-pass animation is over again in 1.4 s
    spin(50)
    if (not chooser.resting) and pal.movie.state() == QMovie.Running:
        out["resumed"] = True
        break
# a weapon out, held still: the prop must sit where a running loop would put it on frame 0
pal.play(chooser.on_hand(("draw", "slot1"), at=clock[0]))
for _ in range(10):
    if chooser.resting:
        break
    pal.play(chooser.on_loop_end(at=clock[0]))
out["held_still"] = chooser.resting and chooser.held == sprites.HAND_LOOPS["slot1"]
out["held_prop_shown"] = pal.prop_lbl.isVisibleTo(pal) and chooser.prop_for() == "pistol_white"
at_rest = (pal.prop_lbl.x(), pal.prop_lbl.y())
spin(200)
pal.place_prop(0)
out["held_prop_in_place"] = at_rest == (pal.prop_lbl.x(), pal.prop_lbl.y())
out["settings_written"] = sprite_pal.SETTINGS.exists()
dlg = sprite_pal.Customise(pal, chooser.catalog.root, pal.height_px)
out["lively_choices"] = [dlg.lively.itemData(i) for i in range(dlg.lively.count())]
out["lively_default"] = dlg.lively.currentData()
print("RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def child(tmp_path_factory):
    if not sprites.DEFAULT_DIR.is_dir():
        pytest.skip("no loop folder at %s" % sprites.DEFAULT_DIR)
    tmp = tmp_path_factory.mktemp("pico_rests")
    script = tmp / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", APPDATA=str(tmp / "appdata"), PYTHONIOENCODING="utf-8")
    env.pop("QT_SCALE_FACTOR", None)
    r = subprocess.run([sys.executable, str(script), str(HERE)], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, "child printed no result:\n%s\n%s" % (r.stdout[-2000:], r.stderr[-2000:])
    out = json.loads(line[len("RESULT "):])
    out["tmp"] = str(tmp)
    return out


def test_the_test_window_cannot_touch_the_real_settings(child):
    assert child["settings"].startswith(child["tmp"])
    assert not child["settings_written"]


def test_he_starts_with_an_animation(child):
    assert child["first_is_animation"]


def test_a_rest_is_a_paused_loop_on_frame_0(child):
    assert child["resting"]
    assert child["rest_loop"] == sprites.REST_LOOPS["calm"]
    assert child["paused"]
    assert child["frame_after_wait"] == 0
    assert child["still_resting_after_wait"]


def test_he_is_still_drawn_while_resting(child):
    assert child["rest_is_frame_0"]               # the standing pose, not the frame after it
    assert child["opaque_px_at_rest"] > 200       # a paused movie that showed nothing would be an empty window


def test_the_windows_tick_ends_the_rest(child):
    assert child["resumed"]


def test_a_weapon_held_still_keeps_its_prop_in_place(child):
    assert child["held_still"]
    assert child["held_prop_shown"]
    assert child["held_prop_in_place"]


def test_customise_offers_how_lively(child):
    assert child["lively_choices"] == list(sprites.LIVELINESS)
    assert child["lively_default"] == "normal"
