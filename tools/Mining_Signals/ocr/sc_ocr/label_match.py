"""NCC-based label finder for the SC mining HUD.

Replaces Tesseract for label-row detection. The SC HUD always renders
the labels "MASS:", "RESISTANCE:", "INSTABILITY:" in the same fixed
font. Once we have a canonicalized template of each, we can find their
exact pixel positions in any captured panel via normalized
cross-correlation (NCC) — fast (~5 ms total per scan), polarity-
independent (works on dark / light / colored backgrounds uniformly),
and gives us CONCRETE GROUND TRUTH per row instead of geometric
inference that compounds errors.

Templates live at ``ocr/sc_templates/labels.npz`` and are generated
offline by ``scripts/build_label_templates.py`` from a small handful
of clean panel captures.

Public API:

    from ocr.sc_ocr.label_match import find_label_positions

    matches = find_label_positions(img)
    # matches = {
    #     "mass":        {"x": 480, "y": 348, "w": 120, "h": 28, "score": 0.83},
    #     "resistance":  {"x": 480, "y": 392, "w": 230, "h": 28, "score": 0.81},
    #     "instability": {"x": 480, "y": 436, "w": 240, "h": 28, "score": 0.79},
    # }
    # Missing labels are absent from the dict (not None entries).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# Templates path (next to the digit templates).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOL_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))
_TEMPLATES_PATH = os.path.join(
    _TOOL_DIR, "ocr", "sc_templates", "labels.npz",
)

# Lazy-loaded template cache: {label_name: float32 array (H, W), zero-mean unit-variance}
_templates_cache: Optional[dict] = None
_templates_height: int = 28  # canonical height the templates are rendered at

# Acceptable NCC score for a confident match. Below this, the label
# is considered "not found" and the caller falls back.
_MIN_MATCH_SCORE = 0.45

# Multi-scale search: panels can be captured at different resolutions,
# so the rendered label height varies. Search at multiple scales and
# pick the best match. Scales relative to the template's native height.
_SCALE_FACTORS = (0.6, 0.75, 0.9, 1.0, 1.15, 1.35, 1.6, 2.0)


def _load_templates() -> Optional[dict]:
    """Load the canonicalized label templates from disk, cached.

    Returns dict {"mass": np.ndarray, "resistance": np.ndarray,
    "instability": np.ndarray} or None if the file is missing.
    """
    global _templates_cache, _templates_height
    if _templates_cache is not None:
        return _templates_cache
    if not os.path.isfile(_TEMPLATES_PATH):
        log.warning(
            "label_match: templates not found at %s — run "
            "scripts/build_label_templates.py to bootstrap them",
            _TEMPLATES_PATH,
        )
        return None
    try:
        data = np.load(_TEMPLATES_PATH)
        templates = {}
        for key in ("mass", "resistance", "instability"):
            if key not in data:
                log.warning("label_match: template missing for %r", key)
                continue
            arr = data[key].astype(np.float32)
            # Pre-normalize to zero-mean unit-variance (NCC-ready).
            mean = float(arr.mean())
            std = float(arr.std())
            if std < 1e-3:
                log.warning(
                    "label_match: template for %r is degenerate", key,
                )
                continue
            templates[key] = (arr - mean) / std
        if "height" in data:
            _templates_height = int(data["height"])
        _templates_cache = templates
        log.info(
            "label_match: loaded %d templates from %s (canonical h=%d)",
            len(templates), _TEMPLATES_PATH, _templates_height,
        )
        return _templates_cache
    except Exception as exc:
        log.warning("label_match: template load failed: %s", exc)
        return None


def _canonicalize(gray: np.ndarray) -> np.ndarray:
    """Polarity-canonicalize a grayscale image so dark-text-on-light
    and bright-text-on-dark both end up looking the same to NCC.

    Returns a float32 array, zero-mean unit-variance, where text
    pixels are HIGH and background is LOW (independent of source
    polarity).
    """
    if gray.size == 0:
        return gray.astype(np.float32)
    # Decide polarity: in a small window of pixels, "text" is usually
    # the minority class. Run a quick Otsu-equivalent split.
    # Histogram-based 2-class threshold.
    hist, _ = np.histogram(gray.flatten(), bins=256, range=(0, 256))
    total = gray.size
    sum_total = np.sum(np.arange(256) * hist)
    sum_bg, w_bg = 0.0, 0
    max_var, threshold = 0.0, 127
    for t in range(256):
        w_bg += int(hist[t])
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += t * int(hist[t])
        m_bg = sum_bg / w_bg
        m_fg = (sum_total - sum_bg) / w_fg
        var = w_bg * w_fg * (m_bg - m_fg) ** 2
        if var > max_var:
            max_var = var
            threshold = t

    bright = int((gray > threshold).sum())
    dark = total - bright
    # Want text to be BRIGHT (the minority class). If dark is minority,
    # invert.
    if dark < bright:
        out = (255 - gray).astype(np.float32)
    else:
        out = gray.astype(np.float32)

    mean = float(out.mean())
    std = float(out.std())
    if std < 1e-3:
        return out - mean
    return (out - mean) / std


def _ncc_search(
    target: np.ndarray, template: np.ndarray,
) -> tuple[float, int, int]:
    """Sliding-window NCC. Returns (best_score, best_x, best_y).

    Both ``target`` and ``template`` must be float32, zero-mean,
    unit-variance. Returns (-1.0, 0, 0) if template doesn't fit.

    Implementation note: uses ``scipy.signal.fftconvolve`` rather than
    ``correlate2d`` because for our typical sizes (target ~280×578,
    template ~28×75 across 8 scales) FFT-based correlation is ~70×
    faster — direct correlation is O(H·W·h·w) while FFT is
    O(H·W·log(H·W)). For 8-scale sweeps this is the difference
    between 3 seconds per scan and 50 ms per scan.
    """
    H, W = target.shape
    h, w = template.shape
    if h > H or w > W:
        return -1.0, 0, 0
    n = float(h * w)
    # FFT correlation = FFT convolve with the template flipped in both
    # axes. Both arrays are already zero-mean unit-variance so we just
    # divide by n to normalise the inner product.
    try:
        from scipy.signal import fftconvolve  # type: ignore
        c = fftconvolve(target, template[::-1, ::-1], mode="valid")
        c /= n
        idx = int(np.argmax(c))
        by, bx = divmod(idx, c.shape[1])
        return float(c[by, bx]), int(bx), int(by)
    except Exception:
        pass
    # Pure NumPy stride-trick fallback (only used if scipy unavailable).
    best_score = -2.0
    best_xy = (0, 0)
    for y in range(0, H - h + 1):
        row = target[y:y + h, :]
        for x in range(0, W - w + 1):
            window = row[:, x:x + w]
            score = float(np.sum(window * template) / n)
            if score > best_score:
                best_score = score
                best_xy = (x, y)
    return best_score, best_xy[0], best_xy[1]


def _resize_template(template: np.ndarray, scale: float) -> np.ndarray:
    """Resize a (H, W) float32 template to (round(H*scale), round(W*scale))."""
    h, w = template.shape
    new_h = max(8, int(round(h * scale)))
    new_w = max(8, int(round(w * scale)))
    # Use PIL for resize (handles float32 via 'F' mode).
    pil = Image.fromarray(template, mode="F").resize(
        (new_w, new_h), Image.BILINEAR,
    )
    arr = np.asarray(pil, dtype=np.float32)
    # Re-normalize after resize (resampling can shift the mean/std slightly).
    mean = float(arr.mean())
    std = float(arr.std())
    if std < 1e-3:
        return arr - mean
    return (arr - mean) / std


_LAST_CALL_CACHE: Optional[tuple[int, tuple[int, int], str, dict]] = None


def find_label_positions(
    img: Image.Image,
) -> dict[str, dict]:
    """NCC-match the three label templates against the panel image.

    Returns a dict mapping label name → match info::

        {
            "mass":        {"x": 480, "y": 348, "w": 120, "h": 28, "score": 0.83},
            "resistance":  {"x": 480, "y": 392, "w": 230, "h": 28, "score": 0.81},
            "instability": {"x": 480, "y": 436, "w": 230, "h": 28, "score": 0.79},
        }

    Missing or low-confidence matches are simply absent from the dict.
    Caller should treat an empty dict as "no labels found, fall back".

    Caching: a single-entry "last image" cache keyed on
    (id(img), img.size, img.mode) returns the cached result when the
    same image is passed multiple times. The HUD pipeline calls this
    via several entry points per scan (the drift-correction block,
    ``_find_label_rows_by_ncc``, the lock-validation fast path…) —
    without this cache each call re-runs the 8-scale NCC sweep
    (~100-150 ms each), tripling the per-scan latency for no benefit.
    """
    global _LAST_CALL_CACHE
    cache_key = (id(img), img.size, img.mode)
    if _LAST_CALL_CACHE is not None and _LAST_CALL_CACHE[:3] == cache_key:
        return _LAST_CALL_CACHE[3]

    templates = _load_templates()
    if not templates:
        _LAST_CALL_CACHE = (cache_key[0], cache_key[1], cache_key[2], {})
        return {}

    # Restrict search to the LEFT 65% of the image (labels never sit
    # in the right half — that's the value column).
    W, H = img.size
    if W < 40 or H < 40:
        return {}
    search_w = int(W * 0.65)
    crop = img.crop((0, 0, search_w, H))
    target_gray = np.asarray(crop.convert("L"), dtype=np.uint8)
    target = _canonicalize(target_gray)

    # ── MASS-FIRST search ──
    # MASS is the anchor. Find the best MASS match across all scales.
    # That scale + position defines the panel's geometry. Then search
    # for RESISTANCE and INSTABILITY at THE SAME SCALE within a
    # narrow Y window around MASS_y + pitch and MASS_y + 2×pitch.
    #
    # This prevents the previous failure mode: RESISTANCE or INSTAB
    # matching a false positive far below the panel (e.g. asteroid
    # noise or COMPOSITION-section text) at an unrelated scale, which
    # would then drag MASS to be flagged as the inconsistent outlier.
    if "mass" not in templates:
        log.warning("label_match: no MASS template — can't anchor")
        _LAST_CALL_CACHE = (cache_key[0], cache_key[1], cache_key[2], {})
        return {}

    # Step 1: Find the best MASS match across all scales.
    best_mass: Optional[dict] = None
    for scale in _SCALE_FACTORS:
        scaled = _resize_template(templates["mass"], scale)
        score, x, y = _ncc_search(target, scaled)
        if score < _MIN_MATCH_SCORE:
            continue
        if best_mass is None or score > best_mass["score"]:
            best_mass = {
                "x": x, "y": y,
                "w": scaled.shape[1], "h": scaled.shape[0],
                "score": float(score), "scale": float(scale),
            }
    if best_mass is None:
        log.debug("label_match: MASS not found at any scale")
        _LAST_CALL_CACHE = (cache_key[0], cache_key[1], cache_key[2], {})
        return {}

    # Step 2: Pitch (vertical row spacing) ≈ 1.4× MASS-template height
    # at the matched scale. Used to predict where RESISTANCE and
    # INSTABILITY should appear.
    mass_h = best_mass["h"]
    pitch = int(round(mass_h * 1.4))
    mass_cy = best_mass["y"] + mass_h // 2

    results: dict[str, dict] = {"mass": best_mass}

    # Step 3: Search for RESISTANCE / INSTABILITY at the MASS scale,
    # WITHIN A NARROW Y WINDOW around their expected position.
    expected = {
        "resistance":  mass_cy + pitch,
        "instability": mass_cy + 2 * pitch,
    }
    # Allow ±35% of pitch slack — covers font-rendering variations
    # without admitting matches from far-off areas like the
    # COMPOSITION list.
    y_tolerance = int(pitch * 0.35)

    for name, expected_cy in expected.items():
        if name not in templates:
            continue
        scaled = _resize_template(templates[name], best_mass["scale"])
        h_scaled = scaled.shape[0]
        # Crop the search target to the Y window for this label.
        win_top = max(0, expected_cy - h_scaled // 2 - y_tolerance)
        win_bot = min(target.shape[0], expected_cy + h_scaled // 2 + y_tolerance)
        if win_bot - win_top < h_scaled:
            continue
        windowed = target[win_top:win_bot, :]
        score, x, y = _ncc_search(windowed, scaled)
        if score < _MIN_MATCH_SCORE:
            log.debug(
                "label_match: %s rejected score=%.2f (expected_cy=%d, "
                "window=[%d,%d])",
                name, score, expected_cy, win_top, win_bot,
            )
            continue
        results[name] = {
            "x": x,
            "y": y + win_top,  # convert window-local back to image coord
            "w": scaled.shape[1],
            "h": scaled.shape[0],
            "score": float(score),
            "scale": float(best_mass["scale"]),
        }

    for name, m in results.items():
        log.debug(
            "label_match: %s matched at (x=%d, y=%d, w=%d, h=%d) "
            "score=%.2f scale=%.2f",
            name, m["x"], m["y"], m["w"], m["h"], m["score"], m["scale"],
        )

    _LAST_CALL_CACHE = (cache_key[0], cache_key[1], cache_key[2], results)
    return results


def reset_template_cache() -> None:
    """Clear the in-memory template cache. Call after rebuilding the
    templates file so the new templates are picked up without a
    process restart."""
    global _templates_cache, _LAST_CALL_CACHE
    _templates_cache = None
    _LAST_CALL_CACHE = None
