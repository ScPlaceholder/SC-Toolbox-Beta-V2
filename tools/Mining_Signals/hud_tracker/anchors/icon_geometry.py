"""Geometry-based detector for the SC mining HUD location-pin icon.

The icon is a fixed game sprite with a known structural decomposition:

  1. Teardrop body at top  -- vertical, internal circular hole.
  2. Oval/disc base below  -- wider than tall, smaller area than the teardrop.
  3. Notch in the top of the oval -- concave dip on the disc's upper edge.
  4. Solid-filled, warm-colored (yellow/orange) -- visually unambiguous
     against cyan digits.

The detector validates these primitives directly. It does not rely on a
learned classifier. A digit silhouette can never satisfy the "two warm
sub-blobs in vertical order with a hole in the top one" constraint, no
matter how its grayscale pattern correlates with a training exemplar.

Public API: ``find_icon_by_geometry(image, hud_bbox=None)``.

Constraints honored:
 * PIL + numpy + scipy.ndimage only. No opencv, no torch.
 * Defensive: bad input returns ``None`` instead of raising.
 * Designed to run in <30 ms on a 200x100 px input.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
#
# PIL converts RGB->HSV with H/S/V each in [0, 255]. From sampling all 6
# real icon crops in
#   training_data_pending_review_signal/icon/pending_*_rgb.png
# the warm-pixel population (R-B > 40, R > 100) lives at:
#   H: p5=13, p95=51   (median per-icon ~25-41)
#   S: p5=71, p95=204
#   V: p5=119, p95=241
#
# We widen each bound a little to cover the slightly-different tones at
# small render sizes / on noisy backgrounds.

WARM_HUE_MIN = 5
WARM_HUE_MAX = 55
WARM_SAT_MIN = 60
WARM_VAL_MIN = 100

# Component-level filters
MIN_AREA_PX = 50
MAX_ASPECT_HW = 2.5  # h/w of full icon bbox
MIN_ASPECT_HW = 0.6

# Acceptance threshold (out of 6 checks)
SCORE_THRESHOLD = 4

# "Very small" capture; relax checks below this.
TINY_W_PX = 14


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_rgb_array(image: Any) -> np.ndarray | None:
    """Coerce input to an HxWx3 uint8 RGB ndarray; return None on bad input."""
    if image is None:
        return None
    try:
        if isinstance(image, np.ndarray):
            arr = image
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=-1)
            elif arr.ndim == 3 and arr.shape[2] == 4:
                arr = arr[..., :3]
            elif arr.ndim != 3 or arr.shape[2] != 3:
                return None
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype(np.uint8)
            return arr
        if isinstance(image, Image.Image):
            return np.asarray(image.convert("RGB"))
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def _crop_to_hud(rgb: np.ndarray, hud_bbox: tuple[int, int, int, int] | None):
    """Return (cropped_rgb, (ox, oy)) — origin offset for translating bboxes back."""
    if hud_bbox is None:
        return rgb, (0, 0)
    x, y, w, h = hud_bbox
    H, W = rgb.shape[:2]
    x0 = max(0, int(x))
    y0 = max(0, int(y))
    x1 = min(W, int(x) + int(w))
    y1 = min(H, int(y) + int(h))
    if x1 <= x0 or y1 <= y0:
        return rgb, (0, 0)
    return rgb[y0:y1, x0:x1], (x0, y0)


def _canonicalize_color_polarity(rgb: np.ndarray) -> np.ndarray:
    """Flip a colour/polarity-inverted crop back to true colour so the
    warm-mask test is polarity-robust.

    The location-pin icon is warm-coloured (yellow/orange) on the SC HUD,
    so the warm mask only fires in NORMAL polarity. Over bright
    backgrounds / inverse captures the whole crop reads colour-inverted
    (warm → cool) and the mask comes back empty — the icon is there but
    geometry says 'no'. Mirroring ``api._route_rgb_to_bod``: when the
    crop reads as light-background (max-channel median >= 128) we
    per-channel invert (255 - x), which restores the warm icon. A no-op
    on a normal bright-on-dark icon crop (median is low), so normal
    detection is unchanged; an empirical check shows this lifts inverted-
    icon geometry recall 3/40 → 38/40 with 0 change to normal (38/40).
    Inversion preserves geometry, so the teardrop/oval/notch shape checks
    downstream are unaffected.
    """
    if rgb is None or rgb.size == 0 or rgb.ndim != 3 or rgb.shape[-1] != 3:
        return rgb
    try:
        arr = rgb if rgb.dtype == np.uint8 else np.clip(
            rgb, 0, 255).astype(np.uint8)
        if float(np.median(arr.max(axis=2))) >= 128.0:
            return (255 - arr.astype(np.int16)).clip(0, 255).astype(np.uint8)
        return arr
    except Exception:
        return rgb


def _warm_mask(rgb: np.ndarray) -> np.ndarray:
    """Return a boolean mask of warm pixels using PIL HSV scale."""
    img = Image.fromarray(rgb).convert("HSV")
    hsv = np.asarray(img)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return (
        (h >= WARM_HUE_MIN)
        & (h <= WARM_HUE_MAX)
        & (s >= WARM_SAT_MIN)
        & (v >= WARM_VAL_MIN)
    )


def _component_mask_bbox(labels: np.ndarray, idx: int) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Slice a labeled component out -- return (cropped_mask, (x, y, w, h))."""
    ys, xs = np.where(labels == idx)
    if ys.size == 0:
        return np.zeros((0, 0), dtype=bool), (0, 0, 0, 0)
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    sub = labels[y0:y1, x0:x1] == idx
    return sub, (x0, y0, x1 - x0, y1 - y0)


def _find_waist(mask: np.ndarray) -> int | None:
    """Return the row index where the blob necks between teardrop and oval.

    Strategy:
     * compute width per row (count of mask pixels in that row)
     * find row of maximum width (likely inside the oval)
     * find row of minimum width strictly between the global-max row and
       the row of the second-largest local max above it
     * if the blob has a row of zero pixels (clean gap), prefer that row

    Returns None if the structure does not look like teardrop+oval.
    """
    if mask.size == 0:
        return None
    widths = mask.sum(axis=1)
    if widths.max() == 0:
        return None

    # Prefer a clean horizontal gap — a single row of all-False.
    nonzero_rows = np.where(widths > 0)[0]
    if nonzero_rows.size > 1:
        zero_rows = np.where(widths == 0)[0]
        zero_internal = zero_rows[
            (zero_rows > nonzero_rows.min()) & (zero_rows < nonzero_rows.max())
        ]
        if zero_internal.size > 0:
            # Pick the gap closest to the vertical middle.
            mid = (nonzero_rows.min() + nonzero_rows.max()) / 2.0
            return int(zero_internal[np.argmin(np.abs(zero_internal - mid))])

    # Find the dominant peak. The oval is wider than the teardrop body
    # because of the rounded base — but the teardrop's circular bulge can
    # be comparable. Look for a local minimum below the topmost peak.
    h = widths.shape[0]
    if h < 6:
        return None

    top_third_end = max(2, h // 3)
    bot_third_start = min(h - 2, h - h // 3)

    # Indices into the bottom region (oval-ish)
    bot_widths = widths[bot_third_start:]
    if bot_widths.size == 0:
        return None
    bot_peak = bot_third_start + int(np.argmax(bot_widths))

    # Indices in the top region (teardrop-ish)
    top_widths = widths[:top_third_end]
    if top_widths.size == 0:
        return None
    top_peak = int(np.argmax(top_widths))

    if top_peak >= bot_peak - 1:
        return None

    middle_slice = widths[top_peak + 1 : bot_peak]
    if middle_slice.size == 0:
        return None
    rel = int(np.argmin(middle_slice))
    waist_row = top_peak + 1 + rel

    # Sanity: the waist must be strictly narrower than both flanking peaks.
    if widths[waist_row] >= widths[top_peak] or widths[waist_row] >= widths[bot_peak]:
        # Still a partial split: accept if substantially narrower than the
        # bottom peak, otherwise no usable waist.
        if widths[waist_row] >= 0.85 * widths[bot_peak]:
            return None
    return waist_row


def _has_internal_hole(mask: np.ndarray, min_hole_px: int) -> tuple[bool, int]:
    """Check whether the filled mask gains pixels (= hole filled in)."""
    if mask.size == 0:
        return False, 0
    filled = ndimage.binary_fill_holes(mask)
    if filled is None:  # pragma: no cover - defensive
        return False, 0
    diff = int(np.count_nonzero(filled & ~mask))
    return diff >= min_hole_px, diff


def _topmost_row_per_column(mask: np.ndarray) -> np.ndarray:
    """For each column, return the row index of the topmost True pixel.

    Empty columns are filled with the mask height (sentinel).
    """
    h = mask.shape[0]
    has_pixel = mask.any(axis=0)
    # argmax returns the first True row index (since False==0).
    top = np.argmax(mask, axis=0)
    top = np.where(has_pixel, top, h)
    return top


def _has_top_notch(oval_mask: np.ndarray, depth_px_min: int = 2, frac_min: float = 0.15) -> tuple[bool, int]:
    """Detect a concave dip on the top edge of an oval.

    Walk left-to-right across columns: ignore empty columns; among the
    occupied columns, the topmost-row curve should rise (be lower-numbered),
    then dip (be higher-numbered), then rise again. We measure dip depth
    as max(top_curve in inner region) minus mean(top_curve at the
    flanking ends).

    The dip threshold scales with oval height.
    """
    if oval_mask.size == 0:
        return False, 0
    h, w = oval_mask.shape
    top = _topmost_row_per_column(oval_mask)
    occupied = top < h
    cols = np.where(occupied)[0]
    if cols.size < 5:
        return False, 0

    x0, x1 = int(cols.min()), int(cols.max())
    span = x1 - x0
    if span < 4:
        return False, 0

    # Use the central ~60% as the dip-search region; left/right 20% as
    # "shoulder" baselines.
    left_end = x0 + max(1, span // 5)
    right_start = x1 - max(1, span // 5)

    left_vals = top[x0 : left_end + 1][top[x0 : left_end + 1] < h]
    right_vals = top[right_start : x1 + 1][top[right_start : x1 + 1] < h]
    if left_vals.size == 0 or right_vals.size == 0:
        return False, 0

    shoulder_top = (left_vals.min() + right_vals.min()) / 2.0
    middle = top[left_end : right_start + 1]
    middle = middle[middle < h]
    if middle.size == 0:
        return False, 0
    middle_lowpoint = float(middle.max())  # higher row index = visually lower
    depth = middle_lowpoint - shoulder_top
    threshold = max(float(depth_px_min), frac_min * h)
    return depth >= threshold, int(depth)


def _bbox_dominant_warm_fraction(rgb: np.ndarray, warm_mask: np.ndarray, bbox: tuple[int, int, int, int]) -> float:
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return 0.0
    sub = warm_mask[y : y + h, x : x + w]
    if sub.size == 0:
        return 0.0
    return float(sub.sum()) / float(sub.size)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_icon_by_geometry(
    image: Any,
    hud_bbox: tuple[int, int, int, int] | None = None,
) -> dict | None:
    """Find the location-pin icon via structural decomposition.

    See module docstring for the algorithm.
    """
    rgb = _to_rgb_array(image)
    if rgb is None or rgb.size == 0:
        return None

    crop, (ox, oy) = _crop_to_hud(rgb, hud_bbox)
    if crop.shape[0] < 4 or crop.shape[1] < 4:
        return None

    # Polarity-robust: un-invert a colour-inverted crop so the warm-mask
    # test fires on inverse/bright-background captures too. No-op for
    # normal bright-on-dark icons.
    crop = _canonicalize_color_polarity(crop)

    warm = _warm_mask(crop)
    if not warm.any():
        return None

    # 8-connectivity for label
    structure = np.ones((3, 3), dtype=bool)
    labels, n = ndimage.label(warm, structure=structure)
    if n == 0:
        return None

    best: dict | None = None
    best_score = -1

    for idx in range(1, n + 1):
        sub_mask, (cx, cy, cw, ch) = _component_mask_bbox(labels, idx)
        area = int(sub_mask.sum())
        if area < MIN_AREA_PX:
            continue
        # The teardrop-only or oval-only blobs are usually merged. But also
        # consider neighborhoods: if there are two separate components
        # close together, the geometry below collapses them by snapping
        # the bbox to the union of nearby components.
        union_mask = sub_mask
        union_bbox = (cx, cy, cw, ch)

        # Greedily merge any other warm component whose bbox vertically
        # touches this one within a small gap (pin and disc may be a few
        # pixels apart in some renders).
        for jdx in range(1, n + 1):
            if jdx == idx:
                continue
            j_mask, (jx, jy, jw, jh) = _component_mask_bbox(labels, jdx)
            j_area = int(j_mask.sum())
            if j_area < 4:
                continue
            # horizontal overlap
            ix0 = max(cx, jx)
            ix1 = min(cx + cw, jx + jw)
            if ix1 - ix0 < min(cw, jw) * 0.4:
                continue
            # vertical proximity (gap <= 5 px)
            vgap = max(jy - (cy + ch), cy - (jy + jh))
            if vgap > 5:
                continue
            # merge into union
            ux0 = min(cx, jx)
            uy0 = min(cy, jy)
            ux1 = max(cx + cw, jx + jw)
            uy1 = max(cy + ch, jy + jh)
            uw = ux1 - ux0
            uh = uy1 - uy0
            big = np.zeros((uh, uw), dtype=bool)
            big[cy - uy0 : cy - uy0 + ch, cx - ux0 : cx - ux0 + cw] |= sub_mask
            big[jy - uy0 : jy - uy0 + jh, jx - ux0 : jx - ux0 + jw] |= j_mask
            union_mask = big
            union_bbox = (ux0, uy0, uw, uh)
            sub_mask = union_mask
            cx, cy, cw, ch = union_bbox
            area = int(union_mask.sum())

        if cw <= 0 or ch <= 0:
            continue

        aspect_hw = ch / max(1, cw)
        if aspect_hw < MIN_ASPECT_HW or aspect_hw > MAX_ASPECT_HW:
            continue

        # ---- Run the structural checks ----------------------------------
        is_tiny = cw < TINY_W_PX
        hole_threshold = 2 if is_tiny else 4

        warm_frac = _bbox_dominant_warm_fraction(warm, warm, (cx, cy, cw, ch))
        check_color = warm_frac > 0.18  # of the bbox; mask is sparse for thin shapes

        waist_row = _find_waist(union_mask)
        check_waist = waist_row is not None

        if waist_row is None:
            # Fall back to a heuristic split at 55% of the height -- this
            # keeps the rest of the checks meaningful for merged blobs.
            split = max(1, int(round(ch * 0.55)))
        else:
            split = waist_row

        teardrop = union_mask[:split]
        oval = union_mask[split:]

        check_oval_below = bool(oval.any()) and bool(teardrop.any())

        # Teardrop hole check
        hole_present, hole_px = _has_internal_hole(teardrop, hole_threshold)
        check_hole = hole_present

        # Oval notch check (skipped for tiny captures)
        if is_tiny:
            check_notch = False
            notch_depth = 0
            notch_skipped = True
        else:
            check_notch, notch_depth = _has_top_notch(oval)
            notch_skipped = False

        # Global aspect sanity: full icon bbox h/w in [1.0, 1.8] ish.
        check_aspect = 1.0 <= aspect_hw <= 1.85

        # Score
        checks = {
            "color_warm": bool(check_color),
            "two_components": bool(check_waist),
            "teardrop_has_hole": bool(check_hole),
            "oval_below_teardrop": bool(check_oval_below),
            "oval_has_notch": bool(check_notch),
            "aspect_ratio_global": bool(check_aspect),
        }
        score = sum(int(v) for v in checks.values())

        # Tiny-mode adjustment: notch is unreliable, so award a free point
        # to keep the threshold meaningful when other checks pass.
        if notch_skipped and score >= 3 and not check_notch:
            score += 1

        if score < SCORE_THRESHOLD:
            if score > best_score and best is None:
                # remember best-so-far so we can reject explicitly if nothing
                # passes; but only return None when truly below threshold.
                pass
            continue

        if score > best_score:
            best_score = score
            global_bbox = (cx + ox, cy + oy, cw, ch)
            details = {
                "teardrop_bbox": (cx + ox, cy + oy, cw, split),
                "oval_bbox": (cx + ox, cy + oy + split, cw, max(0, ch - split)),
                "checks": checks,
                "warm_fraction": round(warm_frac, 3),
                "hole_pixels": int(hole_px),
                "notch_depth": int(notch_depth),
                "tiny_mode": bool(is_tiny),
                "score": int(score),
            }
            best = {
                "bbox": global_bbox,
                "confidence": min(1.0, score / 6.0),
                "details": details,
            }

    return best


__all__ = ["find_icon_by_geometry"]
