"""Neon holographic penguin listening indicator for the Assistant panel.

A 12-frame dance cycle that fades IN when the user starts speaking and fades OUT
when they stop. Purely decorative: the Assistant treats voice as optional, so
every failure path here ends in "stay hidden and say nothing", never in an
exception reaching the host panel.


WHAT IT PLAYS, in strict preference order
=========================================
1. A directory of individual PNG/WebP frames, natural-sorted. The best case:
   real alpha, authored boundaries, nothing to detect. Passed through as-is.
2. A sprite sheet, sliced by DETECTING the frames in the alpha channel. Falls
   back to an even cols x rows split if detection does not find the expected
   count. Frames are then normalised onto a uniform canvas (see below).
3. `assets/listener_penguin.png` - a single still with its own alpha channel,
   faded and gently bobbed. The fallback that keeps the widget useful when no
   sheet is present.
4. Nothing loadable: a permanently hidden no-op.


WHY THE GRID IS DETECTED AND NOT DIVIDED
========================================
Measured on the first sheet: 1774x887,
ARGB32, real alpha, 36.6% of pixels fully transparent. There is NO integer 6x2
grid: 1774/6 = 295.67, 887/2 = 443.5. Nor is there a usable non-integer one -
an even 6-way split of the content span (pitch 292.2) CLIPS frame 1, whose
content runs to x=615 against a cell boundary at x=599. Observed column pitch
wanders 285-314 px. This art was laid out by eye, so there is no authored grid
to recover and dividing by six is wrong in a way that shows.

What IS reliable is the alpha channel:

  ROW bands, stable across alpha thresholds 8..64:   y 13..439  and  y 458..865
  COLUMN bands: five per row, not six, because in each row one adjacent PAIR of
  penguins has overlapping glow and no fully transparent column between them:
      row 1:  289 | 303 | 286 | 294 | 533   <- the 533 is two frames
      row 2:  283 | 291 | 577 | 273 | 279   <- the 577 is two frames

So: detect the bands, then split any band that is ~k x the median single width
at the DEEPEST ALPHA VALLEY inside it, not at its midpoint. Measured, the valley
is decisive rather than a guess - alpha mass drops to 31 against a peak of 391
(8%) in row 1 and to 25 against 363 (7%) in row 2. It happens to land within
3 px and 8 px of the midpoint, which is the confirmation, not the method.

NOTHING ABOUT THIS FILE'S DIMENSIONS IS HARDCODED. 1774, 887, 295.67 and the
band numbers above appear only in this docstring. cols and rows are parameters,
the thresholds are parameters, and a third revision of the asset needs no code
change.


WHY FRAMES ARE NORMALISED, AND ON WHAT EVIDENCE
===============================================
A misaligned crop shows up as the penguin lurching sideways mid-dance, which is
horrible to debug later, so the alignment was chosen by measurement:

  - Consecutive-frame alpha IoU could NOT tell the strategies apart: 0.773 for
    bbox-centred, 0.773 for a uniform grid, 0.778 for feet-centred. It is
    dominated by the pose change and is too coarse an instrument for a 10 px
    registration question. Recorded because it looked like the obvious metric.
  - A "head centroid" metric screamed: 44 px of wobble on a 300 px frame. It was
    WRONG, and checking it is what saved this. Measured per band as a fraction
    of frame width, the TORSO (40-60% of height) sits at 49-58% of width in all
    twelve frames. The head metric was tracking the ANTENNA, which leans right in
    ten frames and left in two. That is the dance, not a defect.
  - Feet-centring looked perfect at 0 px wobble, which was circular - it was the
    criterion. Probed properly, moving its own sample band from the bottom 20%
    to the bottom 30% shifts the anchor by up to 10 px, the same order as the
    wobble it claimed to remove. Rejected.
  - The FLOORS are already planted: bbox bottoms are y 433-439 across row 1 and
    859-864 across row 2, a 6 px spread, while the TOPS range over 28 px. The
    dance bobs the head, not the feet.

Crop each frame to its own alpha bounding box, then compose onto one uniform
canvas: centred horizontally, BOTTOM-aligned vertically. That plants the feet on
a common floor, keeps the head bob that is really there, and gives every frame
an identical size so one cache scale factor produces no size jitter.

`frame_rects()` returns the detected source rectangles and `--dump-grid`
writes two PNGs to look at: the sheet with every rect outlined, and the
normalised frames in a strip. A crop this widget cannot justify is a crop you
can see.


HOW THE MOTION IS DRIVEN
========================
ONE QTimer, and every value comes from ELAPSED TIME off an injectable clock -
never from a frame counter:

  - Elapsed time means a dropped frame shortens no fade and skips no beat. A
    200 ms hitch advances everything 200 ms worth and lands where it should.
  - The fade is one float with one target, and its direction is the sign of
    (target - opacity). Starting speech during a fade-out flips the target and
    the value keeps travelling from wherever it is. There is no animation object
    to stop mid-flight, so being parked at a partial opacity is unrepresentable -
    that is the bug this design rules out rather than handles.
  - The frame index and the fade are DECOUPLED: frames advance at `fps`, the
    fade interpolates every tick. A 12 fps dance therefore does not make the fade
    chunky, and a smooth fade does not cost 30 repaints a second of dance.
  - The timer is STOPPED whenever the widget is hidden, and drops to the frame
    rate once a fade settles. Idle cost is a stopped QTimer: no paints, no
    wakeups. This runs next to Star Citizen.

Detection and scaling happen ONCE - per load and per widget size respectively.
No profiling, no rescale and no gradient construction occurs in paintEvent.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, QTimer, QRect, QRectF, QPointF, QSize
from PySide6.QtGui import QPainter, QPixmap, QImage, QColor, QRadialGradient
from PySide6.QtWidgets import QWidget

try:
    from shared.qt.theme import P as _P
    _GLOW_DEFAULT = _P.energy_cyan
except Exception:  # pragma: no cover - an ornament must not need the theme
    _GLOW_DEFAULT = "#44ccff"

_log = logging.getLogger(__name__)

_ASSETS = Path(__file__).resolve().parent.parent / "assets"

#: Single still WITH its own alpha channel - the always-available fallback.
DEFAULT_STILL = str(_ASSETS / "listener_penguin.png")

#: Looked for, in order, when no frame source is named.
DEFAULT_FRAME_DIR = str(_ASSETS / "listener_penguin_frames")
DEFAULT_SHEET = str(_ASSETS / "listener_penguin_sheet.png")

#: The sheet the owner sent is 6 columns x 2 rows. EXPECTED SHAPE, not a law:
#: detection runs first and these only say how many frames to expect.
DEFAULT_COLS = 6
DEFAULT_ROWS = 2

#: 12 frames at 9 fps is a 1.333 s dance cycle.
#:
#: The rate is about how the dance LOOKS, not about legibility. At 12 fps every pose can be told
#: apart and he still looks frantic; at 6 he drags; 9 was chosen by watching it. Do not re-derive it
#: from cycle length, repaint cost or animation convention and put it back to 12: none of those says
#: how it looks.
#: Cheap either way: 9 repaints a second once the fade has settled.
DEFAULT_FPS = 9.0

#: Mean alpha (0-255) above which a row/column counts as holding ink. Low,
#: because soft glow tails off gradually; the detected bands move <= 3 px
#: between 1 and 5, so this is not a knife edge.
BAND_THRESHOLD = 1

#: A band at least this many times the median single-frame width holds more
#: than one frame and gets split.
MERGE_RATIO = 1.5

#: How far either side of an even sub-boundary to hunt for the alpha valley.
SPLIT_SEARCH = 0.18

_FRAME_EXTS = (".png", ".webp")


# ───────────────────────────────────────────────────────────── detection helpers

def _natural_key(name: str) -> Tuple:
    """Sort frame_2.png before frame_10.png, which a plain sort does not."""
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", name)
    )


def _alpha_profile(img: QImage, per_column: bool) -> List[int]:
    """Mean alpha per column (or per row), 0-255.

    Squashes the image to a 1 px strip with Qt's smooth transform: the box
    filter averages each column in C++, so a 1.5 Mpixel sheet is profiled with
    no numpy and no Python loop over pixels. VALIDATED against a numpy column
    mean on the real asset - max absolute difference 6 of 255, and the row bands
    it finds agree with numpy's to within 3 px.
    """
    w, h = max(1, img.width()), max(1, img.height())
    if per_column:
        strip = img.scaled(w, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    else:
        strip = img.scaled(1, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    strip = strip.convertToFormat(QImage.Format_RGBA8888)
    if per_column:
        return [strip.pixelColor(x, 0).alpha() for x in range(strip.width())]
    return [strip.pixelColor(0, y).alpha() for y in range(strip.height())]


def _bands(profile: Sequence[int], threshold: int, min_len: int) -> List[Tuple[int, int]]:
    """Inclusive runs where the profile exceeds `threshold`, long enough to matter."""
    out: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, v in enumerate(profile):
        if v > threshold:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_len:
                out.append((start, i - 1))
            start = None
    if start is not None and len(profile) - start >= min_len:
        out.append((start, len(profile) - 1))
    return out


def _split_merged(
    profile: Sequence[int], x0: int, x1: int, k: int
) -> List[Tuple[int, int]]:
    """Cut a band holding `k` frames at the k-1 deepest alpha valleys.

    Each cut is the minimum of the profile within +/-SPLIT_SEARCH of the even
    sub-boundary - evidence of where two glows meet, rather than a midpoint
    guess. On the real asset the valleys sit at 7-8% of the band's peak.
    """
    width = x1 - x0 + 1
    cuts = [x0 - 1]
    for j in range(1, k):
        lo = max(x0, int(x0 + width * (j / k - SPLIT_SEARCH)))
        hi = min(x1, int(x0 + width * (j / k + SPLIT_SEARCH)))
        if hi <= lo:
            cuts.append(int(x0 + width * j / k))
            continue
        seg = profile[lo:hi + 1]
        cuts.append(lo + min(range(len(seg)), key=seg.__getitem__))
    cuts.append(x1)
    return [(cuts[j] + 1, cuts[j + 1]) for j in range(k)]


def _content_rect(img: QImage, cell: QRect) -> QRect:
    """Tight alpha bounding box of `cell` within `img`, in `img` coordinates."""
    sub = img.copy(cell)
    cols = _alpha_profile(sub, True)
    rows = _alpha_profile(sub, False)
    xs = [i for i, v in enumerate(cols) if v > BAND_THRESHOLD]
    ys = [i for i, v in enumerate(rows) if v > BAND_THRESHOLD]
    if not xs or not ys:
        return QRect(cell)
    return QRect(cell.x() + xs[0], cell.y() + ys[0],
                 xs[-1] - xs[0] + 1, ys[-1] - ys[0] + 1)


def detect_frame_rects(
    img: QImage, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS
) -> Tuple[List[QRect], str]:
    """Find the frame rectangles in a sprite sheet's alpha channel.

    Returns (rects, note). `note` is "" when detection succeeded outright and
    otherwise names exactly what it fell back to, so a caller never has to guess
    whether the grid was measured or assumed.
    """
    cols, rows = max(1, int(cols)), max(1, int(rows))
    W, H = img.width(), img.height()
    if not img.hasAlphaChannel():
        return _even_grid(W, H, cols, rows), "no alpha channel: even grid split"

    row_prof = _alpha_profile(img, False)
    row_bands = _bands(row_prof, BAND_THRESHOLD, max(4, H // (rows * 8)))
    # Notes ACCUMULATE. An earlier "or" here kept only the first reason, so a
    # row fallback masked a column fallback and each hid the other from any
    # test that only checked the note was non-empty.
    notes: List[str] = []
    if len(row_bands) != rows:
        notes.append(f"found {len(row_bands)} row band(s), expected {rows}: "
                     f"even row split")
        row_bands = [(round(r * H / rows), round((r + 1) * H / rows) - 1)
                     for r in range(rows)]

    rects: List[QRect] = []
    for (y0, y1) in row_bands:
        band = img.copy(QRect(0, y0, W, y1 - y0 + 1))
        col_prof = _alpha_profile(band, True)
        col_bands = _bands(col_prof, BAND_THRESHOLD, max(4, W // (cols * 8)))
        cells = _cells_in_row(col_prof, col_bands, W, cols)
        if len(cells) != cols:
            notes.append(f"row at y={y0}: resolved {len(cells)} of {cols} "
                         f"column(s): even column split")
            cells = [(round(c * W / cols), round((c + 1) * W / cols) - 1)
                     for c in range(cols)]
        for (x0, x1) in cells:
            cell = QRect(x0, y0, max(1, x1 - x0 + 1), max(1, y1 - y0 + 1))
            rects.append(_content_rect(img, cell))
    return rects, "; ".join(notes)


def _cells_in_row(
    profile: Sequence[int], col_bands: Sequence[Tuple[int, int]], width: int, cols: int
) -> List[Tuple[int, int]]:
    """Turn a row's column bands into exactly one cell per frame, if it can."""
    if not col_bands:
        return []
    widths = sorted(x1 - x0 + 1 for x0, x1 in col_bands)
    median = float(widths[len(widths) // 2])
    if median <= 0:
        return []
    cells: List[Tuple[int, int]] = []
    for (x0, x1) in col_bands:
        k = max(1, int(round((x1 - x0 + 1) / median)))
        if k == 1 or (x1 - x0 + 1) < median * MERGE_RATIO:
            cells.append((x0, x1))
        else:
            cells.extend(_split_merged(profile, x0, x1, k))
    return cells


def _even_grid(W: int, H: int, cols: int, rows: int) -> List[QRect]:
    """A plain cols x rows division, rounded per boundary so nothing is lost."""
    out = []
    for r in range(rows):
        y0, y1 = round(r * H / rows), round((r + 1) * H / rows)
        for c in range(cols):
            x0, x1 = round(c * W / cols), round((c + 1) * W / cols)
            out.append(QRect(x0, y0, max(1, x1 - x0), max(1, y1 - y0)))
    return out


def normalise_frames(
    img: QImage, rects: Sequence[QRect], pad: int = 6
) -> List[QPixmap]:
    """Compose detected rects onto ONE uniform canvas: centred, bottom-aligned.

    Identical frame sizes mean one cache scale factor and therefore no size
    jitter; bottom alignment plants the feet on a common floor while keeping the
    head bob, which is where the real motion measurably is.
    """
    if not rects:
        return []
    cw = max(r.width() for r in rects) + 2 * pad
    ch = max(r.height() for r in rects) + pad
    out: List[QPixmap] = []
    for r in rects:
        canvas = QPixmap(cw, ch)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        p.drawImage(QRect((cw - r.width()) // 2, ch - r.height(),
                          r.width(), r.height()), img, r)
        p.end()
        out.append(canvas)
    return out


# ───────────────────────────────────────────────────────────────── the widget

class ListenerPenguin(QWidget):
    """A dancing penguin that is visible exactly while the user is speaking.

    Public API. Every call is safe at any time, in any order, including before
    the widget has ever been shown and when no image could be loaded:

        on_speech_started()          slot  - fade in, start the dance
        on_speech_stopped()          slot  - fade out
        set_speaking(bool)           slot  - for a Signal(bool), e.g.
                                     VoiceController.listeningChanged
        set_source(path, cols=, rows=) -> bool
                                     dispatcher: a directory loads as PNG
                                     frames, a file with cols/rows as a sheet,
                                     otherwise as a still
        set_frame_dir(path)          -> bool
        set_frame_sheet(path, cols=6, rows=2, normalise=True) -> bool
        set_still(path)              -> bool
        set_fade_duration(ms) / fade_duration() -> int
        set_fps(float) / fps() -> float
        opacity() -> float           current 0.0..1.0 fade value
        mode() -> str                "frames" | "still" | "none"
        frame_count() -> int
        current_frame() -> int
        frame_rects() -> list[QRect] the DETECTED source rects, for inspection
        is_animating() -> bool       is the timer running
        asset_ok() -> bool           is anything loaded
        degrade_reason() -> str      WHY we are on a lesser path ("" if not)

    `advance_frame()` runs one animation step. The timer calls it; tests call it
    directly with a fake clock, so a 2-second fade costs no wall time.
    """

    # Idle motion for the STILL only. When real frames play, the art already
    # moves and a second bob on top of it reads as a wobble.
    BOB_FRACTION = 0.030
    SWAY_FRACTION = 0.014
    SCALE_PULSE = 0.035
    BOB_PERIOD_S = 2.6
    SWAY_PERIOD_S = 3.9

    # Both modes.
    GLOW_PERIOD_S = 1.7
    FLICKER_PERIOD_S = 0.21
    FLICKER_DEPTH = 0.045
    HALO_STRENGTH = 0.50

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        frames: Optional[str] = None,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        source: Optional[str] = None,
        fps: float = DEFAULT_FPS,
        fade_ms: int = 320,
        frame_ms: int = 33,
        glow_color: str = "",
        clock: Optional[Callable[[], float]] = None,
        autoload: bool = True,
    ) -> None:
        super().__init__(parent)

        self._clock: Callable[[], float] = clock or time.monotonic
        self._fade_ms = max(1, int(fade_ms))
        self._frame_ms = max(8, int(frame_ms))
        self._fps = max(0.1, float(fps))
        self._glow = QColor(glow_color or _GLOW_DEFAULT)

        self._opacity = 0.0
        self._target = 0.0
        self._t0 = self._clock()
        self._dance_t0 = self._t0
        self._last_t = self._t0

        self._src: List[QPixmap] = []       # source-resolution frames
        self._pix: List[QPixmap] = []       # scaled cache for this widget size
        self._rects: List[QRect] = []       # where each frame came from
        self._halo = QPixmap()
        self._cache_key: Tuple[int, int] = (-1, -1)
        self._mode = "none"
        self._reason = ""

        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)

        self._timer = QTimer(self)
        self._timer.setInterval(self._frame_ms)
        self._timer.setTimerType(Qt.CoarseTimer)    # 33 ms needs no precision
        self._timer.timeout.connect(self.advance_frame)

        # Hidden at construction, EXPLICITLY: isHidden() is then True whether or
        # not this widget ever gets a parent or a shown window.
        self.setVisible(False)

        if autoload:
            self._autoload(frames, cols, rows, source)

    # ───────────────────────────────────────────────────────────────── loading

    def _autoload(
        self, frames: Optional[str], cols: int, rows: int, source: Optional[str]
    ) -> None:
        """Best available asset wins; each failure records WHY and steps down."""
        tried: List[str] = []

        if frames:
            if self.set_source(frames, cols=cols, rows=rows):
                return
            tried.append(f"{frames}: {self._reason or 'did not load'}")
        else:
            for cand, kind in ((DEFAULT_FRAME_DIR, "dir"), (DEFAULT_SHEET, "sheet")):
                if not os.path.exists(cand):
                    continue
                ok = (self.set_frame_dir(cand) if kind == "dir"
                      else self.set_frame_sheet(cand, cols, rows))
                if ok:
                    return
                tried.append(f"{cand}: {self._reason or 'did not load'}")

        still = DEFAULT_STILL if source is None else source
        if self.set_still(still):
            if tried:
                self._reason = ("no usable frame set (" + "; ".join(tried)
                                + ") - playing the single still instead")
                _log.info("listener penguin: %s", self._reason)
            return

        self._reason = "; ".join(tried + [f"{still}: did not load"]) or "nothing to load"
        _log.info("listener penguin: nothing loadable (%s); staying hidden", self._reason)

    def set_source(
        self,
        path: Optional[str],
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> bool:
        """Load whatever `path` is: a frame directory, a sheet, or a still."""
        if not path:
            return self._adopt([], [], "none", "no path given")
        if os.path.isdir(path):
            return self.set_frame_dir(path)
        if cols or rows:
            return self.set_frame_sheet(path, cols or DEFAULT_COLS,
                                        rows or DEFAULT_ROWS)
        return self.set_still(path)

    def set_frame_dir(self, path: str) -> bool:
        """Load every PNG/WebP in a directory as one frame, natural-sorted.

        Individually authored frames are taken AS-IS: they already carry their
        own registration, and re-cropping them to their alpha boxes would throw
        that away and invent jitter.
        """
        try:
            names = sorted(
                (n for n in os.listdir(path) if n.lower().endswith(_FRAME_EXTS)),
                key=_natural_key,
            )
        except Exception as exc:
            return self._adopt([], [], "none", f"cannot list {path!r}: {exc}")
        if not names:
            return self._adopt([], [], "none", f"{path!r} holds no PNG frames")

        out: List[QPixmap] = []
        for n in names:
            pm = self._load_pixmap(os.path.join(path, n))
            if pm is None:
                return self._adopt([], [], "none",
                                   f"{n} in {path!r} is not a readable image")
            out.append(pm)
        if not out[0].hasAlphaChannel():
            return self._adopt([], [], "none", self._no_alpha_reason("frames"))
        return self._adopt(out, [], "frames", "")

    def set_frame_sheet(
        self,
        path: str,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        *,
        normalise: bool = True,
    ) -> bool:
        """Detect the frames in a sheet's alpha channel and load them."""
        pm = self._load_pixmap(path)
        if pm is None:
            return self._adopt([], [], "none", f"{path!r} is not a readable image")
        if not pm.hasAlphaChannel():
            # Refused DELIBERATELY. Keying a background colour out of soft neon
            # glow leaves halos exactly where the art is best, and a still with
            # real alpha beats a badly keyed dance.
            return self._adopt([], [], "none", self._no_alpha_reason("sheet"))

        img = pm.toImage()
        if img.width() < cols or img.height() < rows:
            return self._adopt([], [], "none",
                               f"{img.width()}x{img.height()} sheet cannot hold "
                               f"a {cols}x{rows} grid")

        rects, note = detect_frame_rects(img, cols, rows)
        if not rects:
            return self._adopt([], [], "none", "no frames detected in the sheet")
        out = (normalise_frames(img, rects) if normalise
               else [pm.copy(r) for r in rects])
        _log.info("listener penguin: %d frames from %s%s",
                  len(out), os.path.basename(path), f" ({note})" if note else "")
        return self._adopt(out, list(rects), "frames", note)

    def set_still(self, path: Optional[str]) -> bool:
        """Load one image and fade/bob it. The graceful-degradation target."""
        if not path:
            return self._adopt([], [], "none", "no still given")
        pm = self._load_pixmap(path)
        if pm is None:
            return self._adopt([], [], "none", f"{path!r} is not a readable image")
        if not pm.hasAlphaChannel():
            _log.debug("listener penguin: still %r has no alpha; using it anyway", path)
        return self._adopt([pm], [], "still", "")

    @staticmethod
    def _no_alpha_reason(what: str) -> str:
        return (f"{what} have no alpha channel; refusing to key a background "
                f"colour out of soft glow art - supply PNGs with real alpha")

    def _load_pixmap(self, path: str) -> Optional[QPixmap]:
        """Read an image. Every failure is None, never an exception."""
        try:
            if not os.path.isfile(path):
                _log.debug("listener penguin: no such file %r", path)
                return None
            pm = QPixmap(str(path))
            if pm.isNull() or pm.width() < 1 or pm.height() < 1:
                _log.debug("listener penguin: %r did not decode", path)
                return None
            return pm
        except Exception as exc:                        # pragma: no cover
            _log.debug("listener penguin: could not load %r: %s", path, exc)
            return None

    def _adopt(
        self, frames: List[QPixmap], rects: List[QRect], mode: str, reason: str
    ) -> bool:
        self._src = frames
        self._rects = rects
        self._pix = []
        self._cache_key = (-1, -1)
        self._mode = mode if frames else "none"
        self._reason = reason
        if not frames:
            self._timer.stop()
            self._opacity = 0.0
            self._target = 0.0
            self.setVisible(False)
            if reason:
                _log.debug("listener penguin: %s", reason)
            return False
        self._rebuild_cache()
        self.update()
        return True

    # ────────────────────────────────────────────────────────────── properties

    def asset_ok(self) -> bool:
        return bool(self._src)

    def mode(self) -> str:
        return self._mode

    def frame_count(self) -> int:
        return len(self._src)

    def frame_rects(self) -> List[QRect]:
        """The detected source rectangles. Empty for a still or a frame dir."""
        return list(self._rects)

    def degrade_reason(self) -> str:
        return self._reason

    def set_fade_duration(self, ms: int) -> None:
        self._fade_ms = max(1, int(ms))

    def fade_duration(self) -> int:
        return self._fade_ms

    def set_fps(self, fps: float) -> None:
        self._fps = max(0.1, float(fps))
        self._retune_timer()

    def fps(self) -> float:
        return self._fps

    def opacity(self) -> float:
        return self._opacity

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def current_frame(self) -> int:
        """Which frame the dance is on right now, from elapsed time."""
        n = len(self._src)
        if n <= 1:
            return 0
        return int((self._clock() - self._dance_t0) * self._fps) % n

    # ─────────────────────────────────────────────────────────────────── slots

    def on_speech_started(self) -> None:
        """The user started speaking: fade in from wherever we are."""
        if not self._src:
            return
        if self._opacity <= 0.0:
            # Begin the dance on frame 0, so it reads as "starts dancing". A
            # fade-out interrupted before it finished keeps its phase, because
            # the dance never actually stopped.
            self._dance_t0 = self._clock()
        self._target = 1.0
        if not self.isVisible():
            self.setVisible(True)
        self._start_timer()
        self.update()

    def on_speech_stopped(self) -> None:
        """The user stopped speaking: fade out from wherever we are."""
        if not self._src:
            return
        self._target = 0.0
        if self._opacity <= 0.0 or self.isHidden():
            # Already gone, or the HOST hid us mid-fade: nothing to animate
            # toward, and no reason to wake a timer to discover that.
            self._settle()
            return
        self._start_timer()

    def set_speaking(self, speaking: bool) -> None:
        """For connecting straight to a Signal(bool)."""
        if speaking:
            self.on_speech_started()
        else:
            self.on_speech_stopped()

    # ─────────────────────────────────────────────────────────────────── frame

    def advance_frame(self) -> None:
        """One animation step: move opacity toward the target by elapsed time."""
        now = self._clock()
        dt = now - self._last_t
        self._last_t = now
        if dt < 0.0:
            dt = 0.0        # a clock that went backwards must not rewind the fade

        step = dt * 1000.0 / self._fade_ms
        if self._target > self._opacity:
            self._opacity = min(1.0, self._opacity + step)
        elif self._target < self._opacity:
            self._opacity = max(0.0, self._opacity - step)

        if self._opacity <= 0.0 and self._target <= 0.0:
            self._settle()
            return

        self._retune_timer()
        self.update()

    def _start_timer(self) -> None:
        if self.isHidden():
            # A timer behind an explicitly hidden widget is pure waste; showEvent
            # restarts it if there is still a fade to run.
            return
        if not self._timer.isActive():
            self._last_t = self._clock()
            self._retune_timer()
            self._timer.start()

    def _retune_timer(self) -> None:
        """Tick fast during a fade, then drop to the rate the art needs.

        A settled 12 fps dance does not need 30 repaints a second; a fade does.
        The still keeps the fast rate because its bob is continuous motion.
        """
        fading = abs(self._target - self._opacity) > 1e-6
        if fading or self._mode != "frames":
            want = self._frame_ms
        else:
            want = max(self._frame_ms, int(1000.0 / self._fps))
        if self._timer.interval() != want:
            self._timer.setInterval(want)

    def _settle(self) -> None:
        """Fully faded out: stop the clock and get out of the way."""
        self._timer.stop()
        self._opacity = 0.0
        self.setVisible(False)

    def hideEvent(self, event) -> None:
        # Never leave an animation running behind a hidden widget - including
        # when the HOST hides us rather than the fade finishing.
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._src and (self._target > 0.0 or self._opacity > 0.0):
            self._start_timer()

    # ─────────────────────────────────────────────────────────────────── cache

    def sizeHint(self) -> QSize:
        return QSize(132, 129)

    def minimumSizeHint(self) -> QSize:
        return QSize(40, 40)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rebuild_cache()

    def _rebuild_cache(self) -> None:
        """Scale every source frame down ONCE for this widget size.

        Leaves headroom for the still's scale pulse so the breathe never clips,
        and bakes the halo into its own pixmap so no gradient is built per paint.
        A same-size call returns immediately, so resize storms cost nothing.
        """
        if not self._src:
            return
        w, h = max(1, self.width()), max(1, self.height())
        if (w, h) == self._cache_key and self._pix:
            return

        if self._mode == "still":
            headroom = 1.0 + self.SCALE_PULSE
            avail_w = w / (headroom + 2 * self.SWAY_FRACTION)
            avail_h = h / (headroom + 2 * self.BOB_FRACTION)
        else:
            avail_w, avail_h = w * 0.98, h * 0.98

        # ONE scale factor for ALL frames, taken from the largest frame, so a
        # frame that happens to be a pixel narrower is not drawn bigger.
        sw = max(pm.width() for pm in self._src)
        sh = max(pm.height() for pm in self._src)
        scale = min(avail_w / sw, avail_h / sh)

        self._pix = [
            pm.scaled(max(1, int(pm.width() * scale)),
                      max(1, int(pm.height() * scale)),
                      Qt.KeepAspectRatio, Qt.SmoothTransformation)
            for pm in self._src
        ]
        self._halo = self._build_halo(int(sw * scale * 1.5), int(sh * scale * 1.5))
        self._cache_key = (w, h)

    def _build_halo(self, w: int, h: int) -> QPixmap:
        w, h = max(1, w), max(1, h)
        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)
        g = QRadialGradient(QPointF(w / 2.0, h / 2.0), min(w, h) / 2.0)
        c = QColor(self._glow)
        for stop, alpha in ((0.0, 150), (0.35, 90), (0.62, 34), (1.0, 0)):
            c.setAlpha(alpha)
            g.setColorAt(stop, QColor(c))
        p = QPainter(pm)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QRectF(0, 0, w, h))
        p.end()
        return pm

    # ─────────────────────────────────────────────────────────────────── paint

    def paintEvent(self, event) -> None:
        if self._opacity <= 0.001 or not self._pix:
            return

        t = self._clock() - self._t0
        glow = 0.5 + 0.5 * math.sin(2 * math.pi * t / self.GLOW_PERIOD_S)
        flick = 1.0 - self.FLICKER_DEPTH * (
            0.5 + 0.5 * math.sin(2 * math.pi * t / self.FLICKER_PERIOD_S)
        )
        eff = max(0.0, min(1.0, self._opacity * flick))

        if self._mode == "still":
            bob = math.sin(2 * math.pi * t / self.BOB_PERIOD_S)
            sway = math.sin(2 * math.pi * t / self.SWAY_PERIOD_S)
            s = 1.0 + self.SCALE_PULSE * bob
            dx = sway * self.SWAY_FRACTION * self.width()
            dy = -bob * self.BOB_FRACTION * self.height()
        else:
            # The frames ARE the motion. A bob on top of a dance is a wobble.
            s, dx, dy = 1.0, 0.0, 0.0

        pix = self._pix[self.current_frame() % len(self._pix)]
        pw, ph = pix.width(), pix.height()

        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.translate(self.width() / 2.0 + dx, self.height() / 2.0 + dy)
        if s != 1.0:
            p.scale(s, s)

        if not self._halo.isNull():
            # The halo brightens with the pulse AND with the fade, never alone.
            p.setOpacity(eff * self.HALO_STRENGTH * (0.45 + 0.55 * glow))
            hw, hh = self._halo.width(), self._halo.height()
            p.drawPixmap(QRectF(-hw / 2.0, -hh / 2.0, hw, hh), self._halo,
                         QRectF(0, 0, hw, hh))

        p.setOpacity(eff)
        p.drawPixmap(QRectF(-pw / 2.0, -ph / 2.0, pw, ph), pix,
                     QRectF(0, 0, pw, ph))
        p.end()


# ──────────────────────────────────────────────────────────── inspection tools

def dump_grid(sheet: str, out_dir: str, cols: int = DEFAULT_COLS,
              rows: int = DEFAULT_ROWS) -> int:
    """Write two PNGs to LOOK at, because a bad crop is horrible to debug later.

      <out>/penguin_grid.png   the sheet on a dark card, every detected rect
                               outlined in alternating colours
      <out>/penguin_strip.png  the normalised frames in playback order, on a
                               common floor - sideways lurch is visible here
    """
    img = QImage(sheet)
    if img.isNull():
        print(f"cannot read {sheet}")
        return 2
    rects, note = detect_frame_rects(img, cols, rows)
    print(f"{sheet}\n  {img.width()}x{img.height()} alpha={img.hasAlphaChannel()} "
          f"-> {len(rects)} frames" + (f"  NOTE: {note}" if note else "  (detected)"))
    for i, r in enumerate(rects):
        print(f"  frame {i:2d}  x={r.x():5d} y={r.y():5d} w={r.width():4d} h={r.height():4d}")

    os.makedirs(out_dir, exist_ok=True)
    card = QPixmap(img.width(), img.height())
    card.fill(QColor("#0b0e14"))
    p = QPainter(card)
    p.drawImage(0, 0, img)
    for i, r in enumerate(rects):
        p.setPen(QColor("#ff7733") if i % 2 else QColor("#33dd88"))
        p.drawRect(r.adjusted(0, 0, -1, -1))
    p.end()
    g = os.path.join(out_dir, "penguin_grid.png")
    card.save(g)

    frames = normalise_frames(img, rects)
    if frames:
        fw, fh = frames[0].width(), frames[0].height()
        strip = QPixmap(fw * len(frames), fh)
        strip.fill(QColor("#0b0e14"))
        p = QPainter(strip)
        for i, f in enumerate(frames):
            p.drawPixmap(i * fw, 0, f)
            p.setPen(QColor("#1e2738"))
            p.drawLine(i * fw, 0, i * fw, fh)
        p.setPen(QColor("#44ccff"))
        p.drawLine(0, fh - 1, fw * len(frames), fh - 1)   # the common floor
        p.end()
        s = os.path.join(out_dir, "penguin_strip.png")
        strip.save(s)
        print(f"\n  grid:  {g}\n  strip: {s}  ({len(frames)} x {fw}x{fh}, "
              f"cyan line = the common floor)")
    return 0


# ─────────────────────────────────────────────────────────────────── selftest

def _selftest() -> int:
    """Assert the behaviour, headless, on a fake clock. Returns an exit code."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import tempfile
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    failures: List[str] = []

    def check(cond: bool, what: str) -> None:
        print(("  PASS  " if cond else "  FAIL  ") + what)
        if not cond:
            failures.append(what)

    class Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

        def advance(self, dt: float) -> None:
            self.t += dt

    tmp = tempfile.mkdtemp(prefix="penguin_selftest_")

    def make_sheet(path, w, h, alpha, touch_pair=False):
        """A synthetic 6x2 sheet; `touch_pair` overlaps one pair like the real art."""
        img = QImage(w, h, QImage.Format_ARGB32 if alpha else QImage.Format_RGB32)
        img.fill(Qt.transparent if alpha else QColor("white"))
        p = QPainter(img)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#44ccff"))
        for r in range(2):
            for c in range(6):
                cw, chh = w / 6.0, h / 2.0
                bulge = 0.46 if (touch_pair and c == 4) else 0.0
                p.drawEllipse(QRectF(c * cw + cw * 0.12 + bulge * cw,
                                     r * chh + chh * 0.10,
                                     cw * 0.76, chh * 0.80))
        p.end()
        img.save(path)
        return path

    c = Clock()
    w = ListenerPenguin(fade_ms=400, clock=c)
    w.resize(160, 160)
    check(w.asset_ok(), f"an asset loaded: mode={w.mode()} frames={w.frame_count()}")
    check(w.isHidden() and w.opacity() == 0.0, "hidden and transparent at construction")
    check(not w.is_animating(), "no timer running at construction")

    w.on_speech_started()
    check(not w.isHidden(), "shown by on_speech_started()")
    check(w.is_animating(), "timer running while fading")
    c.advance(0.1)
    w.advance_frame()
    o1 = w.opacity()
    check(0.2 < o1 < 0.3, f"opacity rose to ~0.25 after 100 ms of a 400 ms fade ({o1:.3f})")
    c.advance(0.4)
    w.advance_frame()
    check(w.opacity() == 1.0, "fade-in clamps at 1.0")

    w.on_speech_stopped()
    c.advance(0.1)
    w.advance_frame()
    check(0.7 < w.opacity() < 0.8, f"opacity falls after stop ({w.opacity():.3f})")

    w.on_speech_started()
    c.advance(1.0)
    w.advance_frame()
    check(w.opacity() == 1.0 and not w.isHidden(),
          "start during fade-out ends FULLY visible, not parked part-way")

    w.on_speech_stopped()
    c.advance(1.0)
    w.advance_frame()
    check(w.isHidden() and w.opacity() == 0.0, "full fade-out hides the widget")
    check(not w.is_animating(), "timer stopped once hidden")

    gone = ListenerPenguin(source=os.path.join(tmp, "nope.png"),
                           frames=os.path.join(tmp, "nodir"), clock=c)
    check(not gone.asset_ok() and gone.mode() == "none",
          "missing image: asset_ok() False, mode 'none'")
    gone.on_speech_started()
    gone.advance_frame()
    check(gone.isHidden() and not gone.is_animating(),
          "missing image stays hidden and starts no timer")

    sh = ListenerPenguin(frames=make_sheet(os.path.join(tmp, "a.png"), 1774, 887, True),
                         clock=c)
    check(sh.mode() == "frames" and sh.frame_count() == 12,
          f"detected 12 frames in a non-integer 1774x887 sheet ({sh.frame_count()})")

    tp = ListenerPenguin(frames=make_sheet(os.path.join(tmp, "t.png"), 1774, 887,
                                           True, touch_pair=True), clock=c)
    check(tp.frame_count() == 12,
          f"still 12 frames when one PAIR touches ({tp.frame_count()})")

    bad = ListenerPenguin(frames=make_sheet(os.path.join(tmp, "w.jpg"), 1280, 640, False),
                          clock=c)
    check(bad.mode() == "still" and "alpha" in bad.degrade_reason(),
          f"no-alpha sheet REFUSED, fell back to the still ({bad.mode()})")

    sh.resize(160, 160)
    sh.on_speech_started()
    seen = set()
    for _ in range(12):
        seen.add(sh.current_frame())
        c.advance(1.0 / sh.fps())
    check(len(seen) == 12, f"the dance visits all 12 frames at {sh.fps()} fps ({len(seen)})")

    sizes = {(p.width(), p.height()) for p in sh._src}
    check(len(sizes) == 1, f"all frames are ONE size, so no size jitter ({sizes})")

    print(f"\n{'SELFTEST PASS' if not failures else 'SELFTEST FAIL: ' + str(len(failures))}")
    return 0 if not failures else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if "--dump-grid" in sys.argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        a = sys.argv[sys.argv.index("--dump-grid") + 1:]
        sheet = a[0] if a else DEFAULT_SHEET
        out = a[1] if len(a) > 1 else "."
        raise SystemExit(dump_grid(sheet, out))
    print(__doc__)
    raise SystemExit(0)
