"""The launcher's title bar shows the whole version, at the size the launcher is started at.

The launcher is started 500 wide (device-independent pixels) and its title bar holds the title, the
hotkey badge, the opacity slider and six buttons, which together want more than 500.  A layout that
is short of room takes it from the widest widget, and the widest is the title, so the end of
"SC TOOLBOX  V3.0.0" was not drawn: the bar read "V3.0" (and "V2.4" before that).

The launcher's title now steps its font down until the whole text fits (SCTitleBar(fit_title=True),
shared/qt/title_bar.py).  What is pinned here, on the real LauncherWindow in a child process
(_launcher_title_probe.py):

    the title     is at least as wide as its text needs, at every UI scale, and ends in the version
    the font      never below 8pt
    the rest      the hotkey badge, the slider and the buttons are all there, at their own widths,
                  in order, none over another and none past the end of the bar
    a longer key  rebinding the launcher to a wider hotkey still leaves the whole title

UI scale is QT_SCALE_FACTOR, read once when the QApplication is built, so each case is its own
process with it set, as skill_launcher.py sets it.  The offscreen platform's default screen is
800x800, which at 2.0x is 400 wide and would clamp the launcher below its real width, so the probe
is given a 1920x1080 screen.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PROBE = os.path.join(REPO, "core", "tests", "_launcher_title_probe.py")

LAUNCHER_W = 500            # skill_launcher.py's default window: 100 100 500 550
SCALES = [1.0, 1.25, 1.5, 2.0]
MIN_PT = 8.0


def _probe(tmp_path, ui_scale: float, *args: str) -> dict:
    cfg = {"screens": [{"name": "1080p", "x": 0, "y": 0, "width": 1920, "height": 1080,
                        "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}
    (tmp_path / "screen.json").write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # A relative configfile with cwd=tmp_path: Qt splits the platform argument on ':', so "C:/..."
    # would be read as a platform option named "C".
    env["QT_QPA_PLATFORM"] = "offscreen:configfile=screen.json"
    if ui_scale == 1.0:
        env.pop("QT_SCALE_FACTOR", None)
    else:
        env["QT_SCALE_FACTOR"] = str(ui_scale)
    proc = subprocess.run([sys.executable, PROBE, "--w", str(LAUNCHER_W), *args], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert proc.returncode == 0 and lines, (
        f"probe failed (rc={proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-3000:]}")
    out = json.loads(lines[-1])
    # The case is only the one it claims to be if Qt really applied the scale and kept the width.
    assert out["dpr"] == pytest.approx(ui_scale), f"asked for {ui_scale}x, the window got {out['dpr']}x"
    assert out["window"][0] == LAUNCHER_W, f"the launcher is {out['window'][0]} wide, not {LAUNCHER_W}"
    return out


def _title(out: dict) -> dict:
    return out["items"][0]


def _check_whole_title(out: dict, where: str) -> None:
    title = _title(out)
    assert title["text"].endswith("V" + out["version"]), f"{where}: the title is {title['text']!r}"
    assert title["visible"]
    assert title["w"] >= title["need"], (
        f"{where}: the title {title['text']!r} needs {title['need']}px and was given {title['w']}px - "
        f"its last {title['need'] - title['w']}px are not drawn")
    assert title["pt"] >= MIN_PT, f"{where}: the title is drawn at {title['pt']}pt"


def _check_the_rest(out: dict, where: str) -> None:
    items, bar_w = out["items"], out["bar"][0]
    assert [i["cls"] for i in items] == ["QLabel", "QLabel", "QLabel", "QSlider"] + ["_TitleButton"] * 6, where
    assert all(i["visible"] for i in items), where
    badge, slider, buttons = items[1], items[3], items[4:]
    assert badge["text"] and badge["w"] >= badge["need"], f"{where}: hotkey badge {badge}"
    assert slider["w"] == 55, f"{where}: slider {slider}"
    assert [b["w"] for b in buttons] == [26] * 6, f"{where}: buttons {[b['w'] for b in buttons]}"
    for left, right in zip(items, items[1:]):
        assert left["x"] + left["w"] <= right["x"], (
            f"{where}: {left['cls']} {left['text']!r} ends at {left['x'] + left['w']} and "
            f"{right['cls']} {right['text']!r} starts at {right['x']}")
    assert items[-1]["x"] + items[-1]["w"] <= bar_w, f"{where}: the close button runs past the bar ({bar_w})"


@pytest.mark.parametrize("ui_scale", SCALES)
def test_the_whole_version_is_shown(tmp_path, ui_scale):
    out = _probe(tmp_path, ui_scale)
    _check_whole_title(out, f"{ui_scale}x")


@pytest.mark.parametrize("ui_scale", SCALES)
def test_nothing_else_gave_way(tmp_path, ui_scale):
    out = _probe(tmp_path, ui_scale)
    _check_the_rest(out, f"{ui_scale}x")


@pytest.mark.parametrize("ui_scale", [1.0, 1.5])
def test_a_longer_hotkey_still_leaves_the_whole_title(tmp_path, ui_scale):
    """The badge is set again when a key is rebound while the launcher is up; the title has to refit."""
    out = _probe(tmp_path, ui_scale, "--rebind", "<ctrl>+<shift>+f12")
    where = f"{ui_scale}x after a rebind"
    assert len(out["items"][1]["text"]) > 2, out["items"][1]
    _check_whole_title(out, where)
    _check_the_rest(out, where)
