"""Keep a window inside the screen it is on, and work out which UI scales a screen can show.

Pure geometry.  Every function takes the available rect as an argument instead of asking the
machine, so a test can hand it a simulated 1920x1080 and get a decision without a real monitor.

Why this exists — UI scale is applied through QT_SCALE_FACTOR (skill_launcher.py), which Qt reads
once, when QApplication is built.  Widget sizes and QScreen.availableGeometry() are BOTH in
device-independent pixels, so raising the scale does not grow the widget's number: it shrinks the
screen's.  At 2x a 1920x1080 monitor reports 960x540, and a window that asks for 560x560 no longer
fits.  Nothing clips it — the parts that fall outside are simply not on the screen, including,
for the Settings popup, the Apply button that is the only way back to 1x.

Written against a measured run, not from the arithmetic, because the arithmetic overstates it:
the window overflowing the screen does NOT by itself put the bottom bar off-screen.  A centred
window splits its overflow between top and bottom, and the Apply button sits ~12px above the
window's own bottom edge, so on a taskbar-free 1920x1080 at 2x the button lands 3px inside the
screen and stays clickable; the lock-out becomes total at 2.5x, and a taskbar is enough to make
2x fail by 7px.  Every number is in shared/tests/test_settings_popup_fit.py.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication

# The smallest Settings popup that is still worth showing, in device-independent pixels.
# Height: 36 title bar + 1 accent + 34 tab bar + 1 separator + 44 bottom bar = 116 of chrome,
# leaving ~300 for the tab body, which scrolls.  Used as the yardstick for which UI scales a
# screen can display — NOT as a size anyone sets.
MIN_POPUP_W = 380
MIN_POPUP_H = 420


def available_rect(near: Optional[QPoint] = None) -> Optional[QRect]:
    """availableGeometry() of the screen under `near`, else of the primary screen.

    Returns None when Qt reports no screen at all (headless CI), which every caller here treats
    as "do not clamp" rather than as a zero-sized screen.
    """
    screen = QGuiApplication.screenAt(near) if near is not None else None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return None
    return screen.availableGeometry()


def clamp_size(w: int, h: int, avail: Optional[QRect]) -> tuple[int, int]:
    """Shrink a requested size to what the screen can show.  Never grows it."""
    if avail is None or avail.isEmpty():
        return w, h
    return min(w, avail.width()), min(h, avail.height())


def clamp_pos(x: int, y: int, w: int, h: int, avail: Optional[QRect]) -> tuple[int, int]:
    """Move a w x h window so it lies inside `avail`, if it can.

    Call clamp_size() first: for a window wider or taller than the screen both bounds collapse
    and the corner is pinned to the top-left, which is what the SCWindow rescue in c9046c3 does
    and is exactly the case that leaves a bottom bar off-screen.
    """
    if avail is None or avail.isEmpty():
        return x, y
    x = max(avail.left(), min(x, avail.right() - w + 1))
    y = max(avail.top(), min(y, avail.bottom() - h + 1))
    return x, y


def max_ui_scale(
    avail: Optional[QRect],
    current_scale: float,
    base_w: int = MIN_POPUP_W,
    base_h: int = MIN_POPUP_H,
) -> Optional[float]:
    """The largest UI scale at which a base_w x base_h window still fits on this screen.

    availableGeometry() is in device-independent pixels, so it already shrinks as the scale
    grows: at scale c a 1920x1080 screen reports 1920/c x 1080/c.  A candidate scale S would
    therefore leave avail * c / S of room, and the window fits while base <= avail * c / S, i.e.

        S <= c * min(avail_w / base_w, avail_h / base_h)

    Expressed against the CURRENT scale on purpose: the answer is then the same whichever scale
    we happen to be running at, so a user already stuck at 3x computes the same ceiling as one
    sitting at 1x and is never told that the scale they are on is the only one available.

    None means "cannot tell" (no screen, or a nonsensical current scale) — callers must not read
    that as a ceiling of zero and hide every choice.
    """
    if avail is None or avail.isEmpty() or current_scale <= 0:
        return None
    if base_w <= 0 or base_h <= 0:
        return None
    return current_scale * min(avail.width() / base_w, avail.height() / base_h)


def usable_ui_scales(
    choices: Sequence[float],
    avail: Optional[QRect],
    current_scale: float,
    base_w: int = MIN_POPUP_W,
    base_h: int = MIN_POPUP_H,
    keep: Iterable[float] = (),
) -> list[float]:
    """The subset of `choices` this screen can display, plus anything in `keep`.

    `keep` is for values that must stay offerable whatever the screen says — the scale already in
    force (hiding it would make the combo lie about the current state) and the smallest choice
    (there must always be a way down).  When the ceiling cannot be computed, every choice is
    returned: refusing them all would replace a trap with a dead end.
    """
    ceiling = max_ui_scale(avail, current_scale, base_w, base_h)
    kept = set(keep)
    if ceiling is None:
        return list(choices)
    return [c for c in choices if c <= ceiling or c in kept]
