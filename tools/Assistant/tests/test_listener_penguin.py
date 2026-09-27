"""The listening penguin must fade, dance, and above all never hurt the panel.

Every test names the fault it stands for. Runs offscreen; nothing reaches a
screen and no window is shown.

⛔ NOTHING HERE WRITES SHARED STATE. Every path a test hands the widget is under
pytest's `tmp_path`. The widget keeps no on-disk cache of its own - the frame
cache is in-memory and dies with the instance - so there is no production cache
to poison. The one shared file any test touches is the shipped asset, and it is
only ever READ.

⚠ PySide6 is imported directly, NOT through `pytest.importorskip`. A skip would
turn "the UI toolkit is missing" into a green suite, and this whole file is
about a widget. If PySide6 is absent this must be a loud error. Run under
Python313, which has PySide6 6.11; plain `python` is 3.10 and does not.

The clock is hand-cranked, so a 2-second fade costs no wall time and no test
depends on a real timer firing.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")))
sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

import pytest                                                     # noqa: E402
from PySide6.QtCore import Qt, QRectF                             # noqa: E402
from PySide6.QtGui import QImage, QPainter, QColor                # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget               # noqa: E402

from assistant.listener_penguin import (                          # noqa: E402
    ListenerPenguin, DEFAULT_SHEET, DEFAULT_STILL,
    detect_frame_rects, normalise_frames,
)


# ───────────────────────────────────────────────────────────────── fixtures

@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


class Clock:
    """A hand-cranked monotonic clock: a 2 s fade costs no wall time."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_sheet(path, w=1774, h=887, cols=6, rows=2, alpha=True, touch_pair=None):
    """A synthetic sheet shaped like the real one.

    1774x887 divides into NO integer 6x2 grid (295.67 x 443.5), which is the
    point: detection must not be a division. `touch_pair` bulges one cell so its
    blob overlaps its neighbour with no transparent column between them, which
    is what the real art does once per row.
    """
    img = QImage(int(w), int(h),
                 QImage.Format_ARGB32 if alpha else QImage.Format_RGB32)
    img.fill(Qt.transparent if alpha else QColor("white"))
    p = QPainter(img)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#44ccff"))
    for r in range(rows):
        for c in range(cols):
            cw, ch = w / cols, h / rows
            bulge = 0.46 * cw if (touch_pair is not None and c == touch_pair) else 0.0
            p.drawEllipse(QRectF(c * cw + cw * 0.12 + bulge, r * ch + ch * 0.10,
                                 cw * 0.76, ch * 0.80))
    p.end()
    assert img.save(str(path)), f"could not write {path}"
    return str(path)


def make_still(path, alpha=True):
    img = QImage(200, 200, QImage.Format_ARGB32 if alpha else QImage.Format_RGB32)
    img.fill(Qt.transparent if alpha else QColor("white"))
    p = QPainter(img)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#44ccff"))
    p.drawEllipse(QRectF(20, 20, 160, 160))
    p.end()
    assert img.save(str(path))
    return str(path)


def penguin(tmp_path, **kw):
    """A widget whose every path is under tmp_path unless a test says otherwise."""
    kw.setdefault("clock", Clock())
    kw.setdefault("fade_ms", 400)
    kw.setdefault("frames", str(tmp_path / "no_such_frames"))
    kw.setdefault("source", make_still(tmp_path / "still.png"))
    w = ListenerPenguin(**kw)
    w.resize(160, 160)
    return w


# ─────────────────────────────────────────────────────── the fade, and its edges

def test_hidden_and_transparent_at_construction(app, tmp_path):
    """An ornament that appears before anyone speaks is a bug, not a greeting."""
    w = penguin(tmp_path)
    assert w.isHidden()
    assert w.opacity() == 0.0
    assert not w.is_animating()


def test_hidden_at_construction_even_when_the_ASSETS_LOAD(app, tmp_path):
    """The load path that SUCCEEDS must also leave the widget hidden.

    ⛔ Written after a mutation survived. `test_hidden_and_transparent_at_
    construction` uses a fixture whose frame path is deliberately bogus, and a
    failed load calls `_adopt`, which hides the widget on its way out. So the
    constructor's own `setVisible(False)` was masked: flipping it to True still
    passed, because the failure path hid the widget again a moment later. The
    real widget loads its frames successfully and takes NEITHER of those
    branches. A guard is only watched on the path the product actually walks.
    """
    sheet = make_sheet(tmp_path / "sheet.png")
    w = ListenerPenguin(frames=sheet, cols=6, rows=2, clock=Clock())
    assert w.mode() == "frames", "precondition: the load must have SUCCEEDED"
    assert w.isHidden(), "a successful load must still leave it hidden"
    assert w.opacity() == 0.0
    assert not w.is_animating()

    d = ListenerPenguin(clock=Clock())           # the shipped assets, read-only
    assert d.mode() == "frames", "precondition: the real assets loaded"
    assert d.isHidden(), "the SHIPPED widget must be hidden at construction"
    assert not d.is_animating()


def test_no_timer_runs_before_anyone_speaks(app, tmp_path):
    """Idle cost. A timer behind a hidden widget is CPU spent next to the game."""
    w = penguin(tmp_path)
    assert not w.is_animating()
    for _ in range(5):
        w.advance_frame()          # even poked directly, it must not wake up
    assert w.isHidden()


def test_opacity_rises_after_speech_starts(app, tmp_path):
    c = Clock()
    w = penguin(tmp_path, clock=c, fade_ms=400)
    w.on_speech_started()
    assert not w.isHidden(), "must be shown as soon as speech starts"
    assert w.is_animating()

    c.advance(0.1)
    w.advance_frame()
    assert w.opacity() == pytest.approx(0.25, abs=1e-6), "100 ms of a 400 ms fade"
    c.advance(0.1)
    w.advance_frame()
    assert w.opacity() == pytest.approx(0.50, abs=1e-6)
    c.advance(1.0)
    w.advance_frame()
    assert w.opacity() == 1.0, "a long tick must clamp, never overshoot"


def test_opacity_falls_after_speech_stops_and_then_hides(app, tmp_path):
    c = Clock()
    w = penguin(tmp_path, clock=c, fade_ms=400)
    w.on_speech_started()
    c.advance(1.0)
    w.advance_frame()
    assert w.opacity() == 1.0

    w.on_speech_stopped()
    c.advance(0.1)
    w.advance_frame()
    assert w.opacity() == pytest.approx(0.75, abs=1e-6)
    assert not w.isHidden(), "still fading, so still on screen"

    c.advance(1.0)
    w.advance_frame()
    assert w.opacity() == 0.0
    assert w.isHidden(), "a finished fade-out must hide the widget"
    assert not w.is_animating(), "and must stop the timer"


def test_start_during_fade_out_ends_fully_visible(app, tmp_path):
    """THE interruption case: it must not park at a partial opacity."""
    c = Clock()
    w = penguin(tmp_path, clock=c, fade_ms=400)
    w.on_speech_started()
    c.advance(1.0)
    w.advance_frame()

    w.on_speech_stopped()
    c.advance(0.16)
    w.advance_frame()
    midway = w.opacity()
    assert 0.0 < midway < 1.0, "precondition: caught mid fade-out"

    w.on_speech_started()
    c.advance(0.04)
    w.advance_frame()
    assert w.opacity() > midway, "the fade must reverse from where it was"
    c.advance(1.0)
    w.advance_frame()
    assert w.opacity() == 1.0
    assert not w.isHidden()


def test_rapid_chatter_never_leaves_a_partial_opacity(app, tmp_path):
    """Speech that stutters on and off must settle at exactly 0.0 or 1.0."""
    c = Clock()
    w = penguin(tmp_path, clock=c, fade_ms=400)
    for i in range(24):
        w.set_speaking(i % 2 == 0)
        c.advance(0.037)           # deliberately not a multiple of the frame
        if w.is_animating():
            w.advance_frame()
        assert 0.0 <= w.opacity() <= 1.0

    w.on_speech_started()
    c.advance(2.0)
    w.advance_frame()
    assert w.opacity() == 1.0
    w.on_speech_stopped()
    c.advance(2.0)
    w.advance_frame()
    assert w.opacity() == 0.0 and w.isHidden()


def test_fade_is_measured_in_time_not_in_ticks(app, tmp_path):
    """A dropped frame must not lengthen the fade. Irregular dt, same landing."""
    c = Clock()
    w = penguin(tmp_path, clock=c, fade_ms=500)
    w.on_speech_started()
    for dt in (0.01, 0.2, 0.005, 0.15, 0.03):   # 0.395 s of a 500 ms fade
        c.advance(dt)
        w.advance_frame()
    assert w.opacity() == pytest.approx(0.395 / 0.5, abs=1e-6)


def test_a_backwards_clock_does_not_rewind_the_fade(app, tmp_path):
    c = Clock()
    w = penguin(tmp_path, clock=c, fade_ms=400)
    w.on_speech_started()
    c.advance(0.2)
    w.advance_frame()
    before = w.opacity()
    c.advance(-5.0)               # NTP step, suspend/resume, a bad monotonic
    w.advance_frame()
    assert w.opacity() == before, "must hold, not travel backwards"


def test_host_hiding_the_widget_stops_the_timer(app, tmp_path):
    """The panel may hide us mid-fade; nothing may keep animating behind it."""
    c = Clock()
    w = penguin(tmp_path, clock=c)
    w.on_speech_started()
    assert w.is_animating()
    w.hide()
    assert not w.is_animating(), "a hidden widget must not hold a running timer"
    w.on_speech_stopped()
    assert not w.is_animating(), "and must not start one to fade out unseen"


# ──────────────────────────────────────────────── the missing-asset degradation

def test_missing_image_stays_hidden_and_never_raises(app, tmp_path):
    """Voice is optional. An absent ornament must not break the host panel."""
    w = ListenerPenguin(
        frames=str(tmp_path / "nope_dir"),
        source=str(tmp_path / "nope.png"),
        clock=Clock(),
    )
    assert not w.asset_ok()
    assert w.mode() == "none"
    assert w.degrade_reason(), "it must say WHY, not fail silently"

    w.on_speech_started()          # every public call, on a dead widget
    w.on_speech_stopped()
    w.set_speaking(True)
    w.set_speaking(False)
    w.advance_frame()
    w.resize(80, 80)
    assert w.isHidden()
    assert not w.is_animating()
    assert w.current_frame() == 0


def test_missing_image_still_paints_without_raising(app, tmp_path):
    """paintEvent on an empty widget must be a no-op, not an exception."""
    w = ListenerPenguin(frames=str(tmp_path / "nope_dir"),
                        source=str(tmp_path / "nope.png"), clock=Clock())
    w.resize(64, 64)
    img = QImage(64, 64, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    w.render(img)                  # must not raise
    assert img.size().width() == 64


def test_a_broken_image_file_degrades_like_a_missing_one(app, tmp_path):
    """A truncated or non-image file must be refused, not half-loaded."""
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"this is definitely not a PNG")
    w = ListenerPenguin(frames=str(junk), cols=6, rows=2,
                        source=str(tmp_path / "also_missing.png"), clock=Clock())
    assert w.mode() == "none" and not w.asset_ok()


def test_a_bad_sheet_falls_back_to_a_good_still(app, tmp_path):
    """The whole point of degrading: a usable ornament beats none."""
    still = make_still(tmp_path / "still.png")
    w = ListenerPenguin(frames=str(tmp_path / "nothing_here"), source=still,
                        clock=Clock())
    assert w.mode() == "still"
    assert w.frame_count() == 1
    assert w.asset_ok()


def test_a_sheet_without_alpha_is_refused_rather_than_keyed(app, tmp_path):
    """Keying white out of soft glow art leaves halos where the art is best."""
    sheet = make_sheet(tmp_path / "flat.png", alpha=False)
    still = make_still(tmp_path / "still.png")
    w = ListenerPenguin(frames=sheet, cols=6, rows=2, source=still, clock=Clock())
    assert w.mode() == "still", "must NOT accept the no-alpha sheet"
    assert "alpha" in w.degrade_reason().lower()


# ─────────────────────────────────────────────────────────── frame detection

def test_a_non_integer_grid_is_detected_not_divided(app, tmp_path):
    """1774x887 divides into no integer 6x2 grid. Detection must not care."""
    sheet = make_sheet(tmp_path / "sheet.png", 1774, 887)
    w = ListenerPenguin(frames=sheet, cols=6, rows=2,
                        source=make_still(tmp_path / "s.png"), clock=Clock())
    assert w.mode() == "frames"
    assert w.frame_count() == 12
    assert w.degrade_reason() == "", "detection succeeded, so it must claim nothing"


def test_a_touching_pair_is_split_at_the_alpha_valley(app, tmp_path):
    """The real art has one overlapping pair per row: 5 bands, 6 frames."""
    sheet = make_sheet(tmp_path / "touch.png", 1774, 887, touch_pair=4)
    img = QImage(sheet)
    rects, note = detect_frame_rects(img, 6, 2)
    assert len(rects) == 12, f"expected 12 frames, got {len(rects)} ({note})"
    # The split must fall BETWEEN the two blobs, not inside either one.
    xs = sorted(r.x() for r in rects[:6])
    assert len(set(xs)) == 6, "six distinct frame origins in the first row"


def test_detection_reports_a_fallback_instead_of_pretending(app, tmp_path):
    """A sheet it cannot resolve must SAY so, not silently hand back a guess.

    ⚠ Asserts the ROW reason and the COLUMN reason SEPARATELY, each on a phrase
    the OTHER message cannot contain. Two mutations got through here before
    this wording:
      1. "the note is non-empty" passed while the row message was blanked,
         because the column fallback had fired too and filled it.
      2. `"row" in note` passed for the same mutant, because the column
         message reads "row at y=0: resolved 1 of 6 column(s)" and carries the
         word "row" itself.
    A substring assertion is only as strong as its ability to EXCLUDE the
    neighbouring message.
    """
    img = QImage(600, 400, QImage.Format_ARGB32)
    img.fill(QColor("#44ccff"))                  # one solid block, no gutters
    p = str(tmp_path / "solid.png")
    assert img.save(p)
    rects, note = detect_frame_rects(QImage(p), 6, 2)
    assert len(rects) == 12
    assert "row band(s), expected" in note, f"row fallback unnamed: {note!r}"
    assert "even column split" in note, f"column fallback unnamed: {note!r}"


def test_every_frame_is_one_size_so_nothing_jitters(app, tmp_path):
    """Differently sized frames scale differently and the penguin pulses."""
    sheet = make_sheet(tmp_path / "sheet.png")
    w = ListenerPenguin(frames=sheet, cols=6, rows=2,
                        source=make_still(tmp_path / "s.png"), clock=Clock())
    sizes = {(p.width(), p.height()) for p in w._src}
    assert len(sizes) == 1, f"frames must share one canvas, got {sizes}"


def test_frames_are_bottom_aligned_on_a_common_floor(app, tmp_path):
    """Feet planted: the measured dance bobs the head, not the ground."""
    sheet = make_sheet(tmp_path / "sheet.png")
    img = QImage(sheet)
    rects, _ = detect_frame_rects(img, 6, 2)
    frames = normalise_frames(img, rects)
    floors = []
    for f in frames:
        a = f.toImage().convertToFormat(QImage.Format_ARGB32)
        rows = [y for y in range(a.height())
                if any(a.pixelColor(x, y).alpha() > 8
                       for x in range(0, a.width(), 4))]
        floors.append(rows[-1] if rows else -1)
    assert max(floors) - min(floors) <= 2, f"floors must agree, got {floors}"


def test_frame_rects_are_inspectable(app, tmp_path):
    """A crop you cannot look at is a crop you cannot debug."""
    sheet = make_sheet(tmp_path / "sheet.png")
    w = ListenerPenguin(frames=sheet, cols=6, rows=2,
                        source=make_still(tmp_path / "s.png"), clock=Clock())
    rects = w.frame_rects()
    assert len(rects) == 12
    assert all(r.width() > 0 and r.height() > 0 for r in rects)


# ──────────────────────────────────────────────────────────────── the dance

def test_the_dance_visits_every_frame_at_the_stated_rate(app, tmp_path):
    c = Clock()
    sheet = make_sheet(tmp_path / "sheet.png")
    w = ListenerPenguin(frames=sheet, cols=6, rows=2, fps=12.0,
                        source=make_still(tmp_path / "s.png"), clock=c)
    w.resize(160, 160)
    w.on_speech_started()
    seen = []
    for _ in range(12):
        seen.append(w.current_frame())
        c.advance(1.0 / 12.0)
    assert sorted(seen) == list(range(12)), f"12 fps must walk 12 frames: {seen}"
    # The loop sampled at t = 0 .. 11/12 and left the clock at exactly 1.0 s,
    # which is one whole cycle, so the wrap has ALREADY happened here.
    assert w.current_frame() == 0, "one second at 12 fps must wrap to frame 0"
    c.advance(1.0 / 12.0)
    assert w.current_frame() == 1, "and carry on into the second cycle"


def test_the_dance_starts_on_frame_zero(app, tmp_path):
    c = Clock()
    sheet = make_sheet(tmp_path / "sheet.png")
    w = ListenerPenguin(frames=sheet, cols=6, rows=2,
                        source=make_still(tmp_path / "s.png"), clock=c)
    w.resize(160, 160)
    c.advance(37.3)                 # time passes while nobody speaks
    w.on_speech_started()
    assert w.current_frame() == 0, "it should begin dancing, not resume mid-step"


def test_the_timer_slows_once_the_fade_has_settled(app, tmp_path):
    """A settled 12 fps dance does not need 30 repaints a second."""
    c = Clock()
    sheet = make_sheet(tmp_path / "sheet.png")
    w = ListenerPenguin(frames=sheet, cols=6, rows=2, fps=12.0, frame_ms=33,
                        source=make_still(tmp_path / "s.png"), clock=c)
    w.resize(160, 160)
    w.on_speech_started()
    assert w._timer.interval() == 33, "fast while fading"
    c.advance(2.0)
    w.advance_frame()
    assert w.opacity() == 1.0
    assert w._timer.interval() >= 83, "slow once settled"


# ─────────────────────────────────────────────────── it actually draws something

def test_it_paints_pixels_when_visible_and_nothing_when_not(app, tmp_path):
    """State moving is not the same as something appearing on screen."""
    c = Clock()
    sheet = make_sheet(tmp_path / "sheet.png")
    w = ListenerPenguin(frames=sheet, cols=6, rows=2,
                        source=make_still(tmp_path / "s.png"), clock=c)
    w.resize(120, 120)

    def painted():
        img = QImage(120, 120, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        w.render(img)
        return sum(1 for y in range(0, 120, 3) for x in range(0, 120, 3)
                   if img.pixelColor(x, y).alpha() > 8)

    assert painted() == 0, "nothing drawn before speech"
    w.on_speech_started()
    c.advance(2.0)
    w.advance_frame()
    assert painted() > 50, "the penguin must actually be on screen"
    w.on_speech_stopped()
    c.advance(2.0)
    w.advance_frame()
    assert painted() == 0, "and gone again afterwards"


def test_it_survives_being_parented_and_resized(app, tmp_path):
    """The host owns the layout; a resize storm must not cost or crash."""
    c = Clock()
    host = QWidget()
    w = ListenerPenguin(host, frames=make_sheet(tmp_path / "sheet.png"),
                        cols=6, rows=2, source=make_still(tmp_path / "s.png"),
                        clock=c)
    w.on_speech_started()
    for size in (40, 200, 41, 200, 40, 133):
        w.resize(size, size)
    c.advance(2.0)
    w.advance_frame()
    assert w.opacity() == 1.0
    assert w._pix and w._pix[0].width() > 0
    host.deleteLater()


# ──────────────────────────────────────────── the asset that actually ships

def test_the_shipped_sheet_loads_as_twelve_frames(app):
    """Read-only check of the real asset. NOT skipped if absent - that is news.

    A skip here would turn a deleted or corrupted asset into a green suite, and
    this asset is the entire feature.
    """
    assert os.path.isfile(DEFAULT_SHEET), f"missing shipped sheet {DEFAULT_SHEET}"
    rects, note = detect_frame_rects(QImage(DEFAULT_SHEET), 6, 2)
    assert len(rects) == 12, f"got {len(rects)} frames ({note})"
    assert note == "", f"the real sheet must DETECT, not fall back: {note}"
    # Bodies sit in their boxes at a consistent size: no crop is wildly off.
    ws = [r.width() for r in rects]
    hs = [r.height() for r in rects]
    assert max(ws) / min(ws) < 1.25, f"frame widths disagree too much: {ws}"
    assert max(hs) / min(hs) < 1.25, f"frame heights disagree too much: {hs}"


def test_the_shipped_still_exists_and_has_alpha(app):
    """The fallback is only a fallback if it is genuinely transparent."""
    assert os.path.isfile(DEFAULT_STILL), f"missing shipped still {DEFAULT_STILL}"
    img = QImage(DEFAULT_STILL)
    assert not img.isNull()
    assert img.hasAlphaChannel(), "the fallback must have real alpha"


def test_the_default_widget_prefers_frames_over_the_still(app):
    """With both assets shipped, the dance must win."""
    w = ListenerPenguin(clock=Clock())
    assert w.mode() == "frames", f"expected frames, got {w.mode()}: {w.degrade_reason()}"
    assert w.frame_count() == 12
