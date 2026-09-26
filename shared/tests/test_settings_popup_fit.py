"""The Settings popup must stay on the screen at every UI scale — issue #6.

UI scale is applied through QT_SCALE_FACTOR, which Qt reads once when QApplication is built, so
the launcher relaunches itself to change it.  These tests do the same: each case is a subprocess
(shared/tests/_settings_popup_probe.py) with QT_SCALE_FACTOR set and the offscreen platform told
to pretend it is a 1920x1080 monitor, which makes the bug measurable instead of argued about.

Measured on the unfixed code, simulated 1920x1080 with a taskbar (so 1920x1040 available):

    scale   screen (device-independent)   popup opens at    Apply button   verdict
    1.0     1920x1040                      560x560 @ y=239   y=766..787     on screen
    2.0      960x520                       560x560 @ y=-21   y=506..527     7px past the bottom
    2.5      768x416                       560x560 @ y=-73   y=454..475     59px past, title bar gone
    3.0      640x347                       560x560 @ y=-107  y=420..441     94px past, title bar gone

And on a 2560x1440 panel, where the popup is supposedly safe, 3.0x still puts Apply 34px past the
bottom edge.  The audit that found this called 2.0x on 1080p arithmetic-certain.  The WINDOW
overflowing is certain; the BUTTON going with it is not — the popup is centred, so it loses only
half the overflow at the bottom, and Apply sits ~23px above the window's own edge.  On a 1080p
with no taskbar at all, 2.0x leaves Apply 3px inside the screen and still clickable (that case
passes here, before and after).  The total lock-out — no Apply, no title bar to drag, no [x] —
starts at 2.5x.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Imported hard, like shared/tests/test_qt_widgets.py: run under an interpreter without PySide6
# and this file must fail loudly, not skip.  A silently skipped Qt suite is how a broken change
# looks fine.
from PySide6.QtCore import QRect  # noqa: E402

from shared.qt import screen_fit  # noqa: E402

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROBE = os.path.join(REPO, "shared", "tests", "_settings_popup_probe.py")

# 1920x1080 as the panel reports it, and the same panel with a ~40px Windows taskbar, which is
# what a user actually has.  The offscreen platform sets availableGeometry == geometry, so the
# taskbar is simulated by shrinking the screen itself.
SCREENS = {
    "1080p": (1920, 1080),
    "1080p_taskbar": (1920, 1040),
    "1440p": (2560, 1400),
}


def _run_probe(tmp_path, screen: str, ui_scale: float, *extra: str) -> dict:
    w, h = SCREENS[screen]
    cfg = {"screens": [{"name": screen, "x": 0, "y": 0, "width": w, "height": h,
                        "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}
    (tmp_path / "screen.json").write_text(json.dumps(cfg), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = REPO
    env["PYTHONIOENCODING"] = "utf-8"
    # The configfile path is deliberately relative, with cwd=tmp_path: Qt splits the platform
    # argument on ':', so an absolute Windows path ("C:/...") is parsed as the name "C" and the
    # process dies with no output at all.
    env["QT_QPA_PLATFORM"] = "offscreen:configfile=screen.json"
    if ui_scale == 1.0:
        env.pop("QT_SCALE_FACTOR", None)
    else:
        env["QT_SCALE_FACTOR"] = str(ui_scale)

    proc = subprocess.run(
        [sys.executable, PROBE, str(ui_scale), *extra],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180,
    )
    # The probe prints one JSON line last; imports above it may log to stderr (no sounddevice on
    # a test box, for one), which is not a failure.
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert proc.returncode == 0 and lines, (
        f"probe failed rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return json.loads(lines[-1])


def _rect(seq) -> QRect:
    return QRect(seq[0], seq[1], seq[2], seq[3])


# ── The trap itself ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("screen", ["1080p", "1080p_taskbar", "1440p"])
@pytest.mark.parametrize("ui_scale", [1.0, 1.5, 2.0, 2.5, 3.0])
def test_apply_button_stays_on_screen(tmp_path, screen, ui_scale):
    """Apply is the only control that can undo a UI scale.  If it leaves the screen the setting
    has locked the user out of the settings, so this is the assertion the fix exists for."""
    out = _run_probe(tmp_path, screen, ui_scale)
    avail, apply_r = _rect(out["screen_avail"]), _rect(out["apply"])
    assert avail.contains(apply_r), (
        f"Apply {apply_r} is outside the {screen} screen {avail} at {ui_scale}x "
        f"(popup {out['popup']}) — the user cannot get back to 1x")


@pytest.mark.parametrize("screen", ["1080p", "1080p_taskbar", "1440p"])
@pytest.mark.parametrize("ui_scale", [1.0, 2.0, 2.5, 3.0])
def test_title_bar_stays_on_screen(tmp_path, screen, ui_scale):
    """The documented escape from the trap is to drag the title bar up and close with its [x].
    A popup centred while too tall loses the TOP as well, which takes that escape away too."""
    out = _run_probe(tmp_path, screen, ui_scale)
    avail, popup = _rect(out["screen_avail"]), _rect(out["popup"])
    assert avail.top() <= popup.top(), (
        f"popup top {popup.top()} is above the {screen} screen at {ui_scale}x — the title bar, "
        f"the drag handle and the [x] are all unreachable")
    assert avail.contains(popup), f"popup {popup} does not fit the screen {avail} at {ui_scale}x"


@pytest.mark.parametrize("screen", ["1080p_taskbar", "1440p"])
@pytest.mark.parametrize("ui_scale", [1.0, 2.0, 3.0])
def test_bottom_bar_survives_the_squeeze(tmp_path, screen, ui_scale):
    """Clamping the window is only half of it — the bottom bar must still be inside the window
    after the layout is squeezed, or Apply is merely off the widget instead of off the screen."""
    out = _run_probe(tmp_path, screen, ui_scale)
    popup, apply_r = _rect(out["popup"]), _rect(out["apply"])
    assert popup.contains(apply_r), f"Apply {apply_r} squeezed out of the popup {popup}"


# ── The setting that cannot make itself unreachable ───────────────────────────

def test_scale_choices_bounded_by_the_screen(tmp_path):
    out = _run_probe(tmp_path, "1080p_taskbar", 1.0)
    offered = out["scales_offered"]
    assert 1.0 in offered and 0.75 in offered, "there must always be a way down"
    assert 2.0 in offered, "2x is usable on 1080p once the popup is clamped"
    assert 2.5 not in offered and 3.0 not in offered, (
        f"1920x1040 cannot show Settings above ~2.4x, yet it offers {offered}")


def test_big_screen_keeps_every_choice(tmp_path):
    out = _run_probe(tmp_path, "1440p", 1.0)
    assert out["scales_offered"] == [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]


def test_stuck_user_sees_their_own_scale_and_a_way_down(tmp_path):
    """Someone already at 3x — hand-edited config, or a monitor swap — must not be shown a combo
    that hides the value in force, and must be able to pick 1x from it."""
    out = _run_probe(tmp_path, "1080p_taskbar", 3.0)
    assert out["scale_current"] == 3.0, f"combo lost the scale in force: {out}"
    assert 3.0 in out["scales_offered"]
    assert 1.0 in out["scales_offered"]


def test_close_button_still_commits_the_scale(tmp_path):
    """The recovery path a stuck user is told about: move the popup clear, pick 1x, then close it
    with its own [x] rather than Apply, which commits exactly like Apply (closeEvent saves; only
    Cancel discards).  It is the one escape that works without the Apply button, so it must keep
    working whatever the clamp does."""
    out = _run_probe(tmp_path, "1080p_taskbar", 2.5, "--escape")
    esc = out["escape"]
    assert esc["found_1x"], "1x must be in the list even from a scale the screen cannot show"
    assert esc["found_close_btn"], "the title bar [x] is gone"
    assert esc["title_bar_on_screen"], "the [x] is off-screen, so this escape is not clickable"
    assert esc["saves"] == 1, f"closing should save exactly once, got {esc}"
    assert esc["saved_scale"] == 1.0, f"[x] did not commit 1x: {esc}"


# ── The geometry, without Qt in the way ──────────────────────────────────────

class TestScreenFit:
    def test_clamp_size_shrinks_only(self):
        avail = QRect(0, 0, 960, 520)
        assert screen_fit.clamp_size(560, 560, avail) == (560, 520)
        assert screen_fit.clamp_size(560, 400, avail) == (560, 400)

    def test_clamp_size_without_a_screen_changes_nothing(self):
        assert screen_fit.clamp_size(560, 560, None) == (560, 560)
        assert screen_fit.clamp_size(560, 560, QRect()) == (560, 560)

    def test_clamp_pos_pulls_a_window_back_inside(self):
        avail = QRect(0, 0, 1920, 1040)
        # right()/bottom() are inclusive, so the last legal x is 1919 - 560 + 1 = 1360: the window
        # then spans 1360..1919 and ends exactly on the screen edge.
        assert screen_fit.clamp_pos(1800, 900, 560, 520, avail) == (1360, 520)
        assert screen_fit.clamp_pos(-40, -20, 560, 520, avail) == (0, 0)
        assert screen_fit.clamp_pos(100, 100, 560, 520, avail) == (100, 100)

    def test_clamp_pos_respects_a_screen_that_is_not_at_the_origin(self):
        avail = QRect(1920, 0, 1920, 1040)
        assert screen_fit.clamp_pos(0, 0, 560, 520, avail) == (1920, 0)

    def test_max_scale_is_the_same_whatever_scale_we_are_running_at(self):
        """The ceiling is a fact about the monitor.  Computed from availableGeometry, which is
        already divided by the current scale, it has to be multiplied back by it — otherwise a
        user stuck at 3x is told 3x is the only thing their screen can do."""
        at_1x = screen_fit.max_ui_scale(QRect(0, 0, 1920, 1040), 1.0)
        at_3x = screen_fit.max_ui_scale(QRect(0, 0, 640, 347), 3.0)
        assert at_1x == pytest.approx(2.476, abs=0.01)
        assert at_3x == pytest.approx(at_1x, abs=0.02)

    def test_max_scale_cannot_tell_without_a_screen(self):
        assert screen_fit.max_ui_scale(None, 1.0) is None
        assert screen_fit.max_ui_scale(QRect(0, 0, 1920, 1040), 0) is None

    def test_usable_scales_filters_and_keeps(self):
        choices = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
        avail = QRect(0, 0, 1920, 1040)
        assert screen_fit.usable_ui_scales(choices, avail, 1.0) == [0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
        # Already stuck at 3x on that same monitor: availableGeometry is the rect ALREADY divided
        # by 3, which is the whole reason max_ui_scale multiplies the current scale back in.
        kept = screen_fit.usable_ui_scales(choices, QRect(0, 0, 640, 347), 3.0, keep=(3.0, 0.75))
        assert 3.0 in kept and 2.5 not in kept, kept
        assert 1.0 in kept, "a stuck user must still be offered the way down"

    def test_usable_scales_offers_everything_when_it_cannot_tell(self):
        choices = [0.75, 1.0, 3.0]
        assert screen_fit.usable_ui_scales(choices, None, 1.0) == choices
