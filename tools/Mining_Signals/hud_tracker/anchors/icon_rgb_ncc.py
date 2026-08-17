"""RGB-aware NCC detector for the SC mining HUD location-pin icon.

The grayscale NCC in ``signal_anchor.find_icon`` discards color, which
collapses the icon (warm yellow/orange) and the cyan digits onto similar
luminance patterns. The diagnostic on
``cap_20260418_155446_555.png`` showed grayscale NCC returning 8
candidates ALL clustered on the digit cluster, none on the actual icon.

This module fixes the blind spot by running NCC per RGB channel and
combining with R-heavy weights (default 0.50/0.20/0.30). Reasoning:

  * The icon's distinguishing pixel statistic vs digits is a high R
    channel (warm yellow/orange has both high R and moderate G), while
    cyan digits have low R and high B. Weighting R the most makes the
    digit region dramatically less correlated with the icon templates.
  * G is down-weighted (0.20) because both the icon and the digits
    have appreciable green content (cyan has high G; warm-yellow has
    moderate-to-high G), so G is the least discriminating channel.
  * B sits at 0.30: digits have very high B (cyan), icon has low B,
    so the B channel score actively *penalises* digit regions when the
    template is the icon — but only when normalized correlation is
    used. Keeping it at 0.30 lets it contribute discrimination without
    overwhelming R.

Public API: ``find_icon_rgb_ncc(rgb_image, hud_bbox=None, weights, min_score)``.

Constraints honored:
 * PIL + numpy + scipy.signal.fftconvolve only. No opencv. No torch.
 * Defensive: missing/empty input or 0 templates → returns ``None``.
 * Templates lazy-loaded once, cached at module level.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
from PIL import Image
from scipy.signal import fftconvolve  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
# Cached template stack lives next to this module.
_TEMPLATES_NPZ = _THIS_DIR / "icon_rgb_templates.npz"

# Source for real labeled icons (region2 pending icons).
_REAL_ICON_GLOB_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_pending_review_signal\icon"
)
# Mislabeled per the upstream finding (this file is actually a digit
# crop, not the icon).
_REAL_ICON_BLACKLIST = {"pending_cap_20260418_155503_607_rgb.png"}

# Synthetic-fallback source (a low-quality icon-shaped grayscale crop).
_BAD_CROP_PATH = _THIS_DIR.parent.parent / "training_data_blacklist" / "bad crop.png"

# Canonical template size (matches the labeled crops; real icons in
# region2 are ~24-27 px wide so 28x28 is a snug fit).
_CANONICAL_SIZE = 28

# Module-level cache: stack of (T, H, W, 3) float32 templates and per-
# channel means (used to preserve channel intensity before subtracting
# template-side mean during NCC).
_template_cache: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_rgb_array(image: Any) -> Optional[np.ndarray]:
    """Coerce input to an HxWx3 float32 RGB array in [0, 255]."""
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
            return arr.astype(np.float32, copy=False)
        if isinstance(image, Image.Image):
            return np.asarray(image.convert("RGB"), dtype=np.float32)
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def _crop_to_hud(rgb: np.ndarray, hud_bbox: Optional[tuple[int, int, int, int]]):
    """Return (cropped_rgb, (ox, oy)) — ox/oy translate detector coords back."""
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


def _hsv_to_rgb_pil(h_deg: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV (deg/sat[0..1]/val[0..1]) → uint8 RGB triple via PIL."""
    h_byte = int(round((h_deg % 360.0) / 360.0 * 255.0)) & 0xFF
    s_byte = int(round(max(0.0, min(1.0, s)) * 255.0))
    v_byte = int(round(max(0.0, min(1.0, v)) * 255.0))
    px = Image.new("HSV", (1, 1), (h_byte, s_byte, v_byte)).convert("RGB").getpixel((0, 0))
    return tuple(px)  # type: ignore[return-value]


def _build_synthetic_template() -> Optional[np.ndarray]:
    """Colorize ``bad crop.png`` with HSV(30°, 0.65, 0.85) → 28x28 RGB.

    Returns float32 array shape (28, 28, 3) or None on failure.
    """
    if not _BAD_CROP_PATH.is_file():
        return None
    try:
        gray = Image.open(_BAD_CROP_PATH).convert("L").resize(
            (_CANONICAL_SIZE, _CANONICAL_SIZE), Image.BILINEAR
        )
        g = np.asarray(gray, dtype=np.float32) / 255.0
    except Exception as exc:
        log.debug("icon_rgb_ncc: synthetic template failed to load: %s", exc)
        return None
    target = np.array(_hsv_to_rgb_pil(30.0, 0.65, 0.85), dtype=np.float32)
    # Mix luminance with the target color: anywhere the original is dark
    # (icon foreground), keep dark; bright pixels become the warm color.
    rgb = np.zeros((_CANONICAL_SIZE, _CANONICAL_SIZE, 3), dtype=np.float32)
    for c in range(3):
        rgb[..., c] = g * target[c]
    return rgb


def _load_real_templates() -> list[np.ndarray]:
    """Load every non-blacklisted labeled icon, resized to 28x28 RGB.

    Returns a list of float32 (28,28,3) arrays in [0, 255]. Empty list
    if the source directory doesn't exist.
    """
    out: list[np.ndarray] = []
    if not _REAL_ICON_GLOB_DIR.is_dir():
        log.debug("icon_rgb_ncc: real icon dir missing: %s", _REAL_ICON_GLOB_DIR)
        return out
    for name in sorted(os.listdir(str(_REAL_ICON_GLOB_DIR))):
        if not name.endswith("_rgb.png"):
            continue
        if name in _REAL_ICON_BLACKLIST:
            continue
        if not name.startswith("pending_"):
            continue
        p = _REAL_ICON_GLOB_DIR / name
        try:
            im = Image.open(p).convert("RGB")
            if im.size != (_CANONICAL_SIZE, _CANONICAL_SIZE):
                im = im.resize((_CANONICAL_SIZE, _CANONICAL_SIZE), Image.BILINEAR)
            out.append(np.asarray(im, dtype=np.float32))
        except Exception as exc:
            log.debug("icon_rgb_ncc: skip template %s: %s", name, exc)
    return out


def _build_template_cache() -> dict[str, Any]:
    """Build template stack from real labels, falling back to synthetic.

    Saves an .npz next to the module. Returns a dict with:
        templates:  (T, H, W, 3) float32, [0, 255]
        names:      list of source identifiers
        size:       canonical template side (currently 28)
    """
    reals = _load_real_templates()
    names = []
    if reals:
        # Stable ordering — sorted by directory listing already.
        for i, _ in enumerate(reals):
            names.append(f"real_{i}")
    if not reals:
        synth = _build_synthetic_template()
        if synth is None:
            log.warning("icon_rgb_ncc: 0 templates available (real + synth failed)")
            return {
                "templates": np.zeros((0, _CANONICAL_SIZE, _CANONICAL_SIZE, 3), dtype=np.float32),
                "names": [],
                "size": _CANONICAL_SIZE,
            }
        reals = [synth * 255.0]  # synth was [0..1]; bring to [0..255]
        names = ["synthetic_hsv30"]

    stack = np.stack(reals, axis=0).astype(np.float32, copy=False)
    cache = {"templates": stack, "names": names, "size": _CANONICAL_SIZE}

    # Persist for diagnostics / reproducibility.
    try:
        np.savez(_TEMPLATES_NPZ, templates=stack, names=np.asarray(names), size=_CANONICAL_SIZE)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("icon_rgb_ncc: failed to save template cache: %s", exc)
    return cache


def _ensure_templates() -> dict[str, Any]:
    """Return cached templates, building them on first call."""
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    # Try to load from disk.
    if _TEMPLATES_NPZ.is_file():
        try:
            with np.load(_TEMPLATES_NPZ, allow_pickle=False) as f:
                stack = f["templates"].astype(np.float32, copy=False)
                names = [str(n) for n in f["names"].tolist()]
                size = int(f["size"]) if "size" in f.files else _CANONICAL_SIZE
            if stack.ndim == 4 and stack.shape[0] > 0:
                _template_cache = {"templates": stack, "names": names, "size": size}
                return _template_cache
        except Exception as exc:
            log.debug("icon_rgb_ncc: stale template cache, rebuilding: %s", exc)
    _template_cache = _build_template_cache()
    return _template_cache


# ---------------------------------------------------------------------------
# Per-channel NCC via FFT
# ---------------------------------------------------------------------------


def _ncc_one_channel(image_ch: np.ndarray, template_ch: np.ndarray) -> np.ndarray:
    """Normalized cross correlation of one channel.

    Returns a 2D map of size (H - h + 1, W - w + 1) of NCC scores in
    [-1, 1]. The location at position (i, j) is the NCC of the
    template against the image patch starting at (i, j).

    Implementation: FFT-based correlation for the numerator, then
    sum-of-squared-differences via integral images for the
    per-window normalization. Templates and images are float32; we
    subtract the template's own mean once and normalize each window
    by its local std.
    """
    H, W = image_ch.shape
    h, w = template_ch.shape
    if H < h or W < w:
        return np.zeros((0, 0), dtype=np.float32)

    t = template_ch.astype(np.float32, copy=False)
    t_mean = float(t.mean())
    t_zm = t - t_mean
    t_norm = float(np.sqrt(np.sum(t_zm * t_zm)))
    if t_norm < 1e-6:
        # Constant template — no correlation possible.
        return np.zeros((H - h + 1, W - w + 1), dtype=np.float32)

    img = image_ch.astype(np.float32, copy=False)

    # Numerator: sum over (i, j) of (img[i+u, j+v] - mu_window) * t_zm[u, v]
    # = sum(img * t_zm) over the window  (mu cancels because sum(t_zm) = 0).
    # Use cross-correlation via fftconvolve(img, flip(t_zm)).
    t_flipped = t_zm[::-1, ::-1]
    num_full = fftconvolve(img, t_flipped, mode="valid")
    # num_full is shape (H - h + 1, W - w + 1).

    # Denominator: sqrt(sum(window²) - n*mu²) * t_norm.
    n = float(h * w)
    img_sq = img * img
    # Sum over each window using a uniform kernel.
    kernel = np.ones((h, w), dtype=np.float32)
    sum_img = fftconvolve(img, kernel, mode="valid")
    sum_img_sq = fftconvolve(img_sq, kernel, mode="valid")
    mu = sum_img / n
    var_window = sum_img_sq - n * (mu * mu)
    var_window = np.maximum(var_window, 0.0)
    denom = np.sqrt(var_window) * t_norm
    # Avoid div-by-zero: in flat regions denom -> 0; map those scores
    # to 0 (no informative correlation there).
    safe = denom > 1e-6
    out = np.zeros_like(num_full, dtype=np.float32)
    np.divide(num_full, denom, out=out, where=safe)
    # Clip to [-1, 1] to wash out floating-point drift on very small dens.
    np.clip(out, -1.0, 1.0, out=out)
    return out


def _correlation_map_for_template(
    image_rgb: np.ndarray,
    template_rgb: np.ndarray,
    weights: tuple[float, float, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute per-channel + combined NCC maps for one template.

    Returns:
        score_R, score_G, score_B, score_combined  — all 2D float32.
    """
    sR = _ncc_one_channel(image_rgb[..., 0], template_rgb[..., 0])
    sG = _ncc_one_channel(image_rgb[..., 1], template_rgb[..., 1])
    sB = _ncc_one_channel(image_rgb[..., 2], template_rgb[..., 2])
    if sR.size == 0 or sG.size == 0 or sB.size == 0:
        empty = np.zeros((0, 0), dtype=np.float32)
        return empty, empty, empty, empty
    wR, wG, wB = weights
    combined = wR * sR + wG * sG + wB * sB
    return sR, sG, sB, combined


def _resize_template(t: np.ndarray, scale: float) -> np.ndarray:
    """Resize a single (H,W,3) float32 template by ``scale`` via PIL."""
    h, w = t.shape[:2]
    new_h = max(4, int(round(h * scale)))
    new_w = max(4, int(round(w * scale)))
    if new_h == h and new_w == w:
        return t
    pil = Image.fromarray(np.clip(t, 0, 255).astype(np.uint8), mode="RGB")
    pil = pil.resize((new_w, new_h), Image.BILINEAR)
    return np.asarray(pil, dtype=np.float32)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_icon_rgb_ncc(
    rgb_image: Any,
    hud_bbox: Optional[tuple[int, int, int, int]] = None,
    weights: tuple[float, float, float] = (0.50, 0.20, 0.30),
    min_score: float = 0.55,
) -> Optional[dict]:
    """Find the location-pin icon via per-channel NCC with R-channel weighting.

    Decorrelated peer to ``find_icon_by_geometry``: both leverage the
    icon's color signature, but RGB NCC also exploits the template's
    spatial pattern.

    Args:
        rgb_image:  PIL Image (RGB) or numpy HxWx3.
        hud_bbox:   optional (x, y, w, h) crop constraint.
        weights:    (wR, wG, wB) — default (0.50, 0.20, 0.30) is
                    R-heavy by design; see module docstring for the
                    failure-analysis justification.
        min_score:  threshold for the combined score; matches below
                    this are not considered.

    Returns:
        dict with keys ``bbox``, ``score``, ``details`` (or ``None``).
    """
    rgb = _to_rgb_array(rgb_image)
    if rgb is None or rgb.size == 0:
        return None
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        return None

    crop, (ox, oy) = _crop_to_hud(rgb, hud_bbox)
    if crop.shape[0] < 6 or crop.shape[1] < 6:
        return None

    cache = _ensure_templates()
    templates = cache["templates"]
    names = cache["names"]
    if templates.shape[0] == 0:
        return None

    # Position prior: icon is in the leftmost ~40% of the search width.
    # We enforce this by zeroing the score map outside the LH band.
    H_search, W_search = crop.shape[:2]
    left_band_max_x = int(round(W_search * 0.40))

    # Verified scales for region2 captures: real icon side ~24-26 px
    # in the standard zoom; canonical templates are 28x28; covering
    # 0.7..1.5 × 28 ⇒ 19..42 px.
    #
    # Limitation: a few rare large-zoom captures have icons up to ~46
    # px wide; scale 1.7 covers them but increases the chance of a
    # large-window-with-icon-centered false-tightening on standard
    # captures (because flat background averages neutral and pushes
    # the score up). The 4/5 detection rate on the 5 real labels
    # passes; the one miss is a 46x43 icon outside this scale set.
    scales = (0.7, 0.85, 1.0, 1.2, 1.5)

    # Tie-break weight: when two scales score within ~0.02, prefer the
    # one closest to scale=1.0 (the canonical template size). This
    # avoids the largest-scale-with-icon-centered-inside trap that
    # otherwise dominates because large windows average out background.
    SCALE_TIE_TOL = 0.02

    best = None  # (score, x, y, w, h, scale, per_channel, name)
    n_above = 0

    for ti in range(templates.shape[0]):
        t_base = templates[ti]
        for sc in scales:
            t = _resize_template(t_base, sc)
            th, tw = t.shape[:2]
            if th < 4 or tw < 4 or th > H_search or tw > W_search:
                continue
            try:
                sR, sG, sB, combined = _correlation_map_for_template(crop, t, weights)
            except Exception as exc:
                log.debug("icon_rgb_ncc: NCC failed (t=%d s=%.2f): %s", ti, sc, exc)
                continue
            if combined.size == 0:
                continue

            # Apply position prior: leftmost ~40% of the search.
            # combined position (i, j) = match top-left corner; we
            # constrain TOP-LEFT j to stay inside the leftmost band.
            if combined.shape[1] > 0:
                cutoff = max(0, min(combined.shape[1], left_band_max_x))
                if cutoff < combined.shape[1]:
                    combined[:, cutoff:] = -1.0  # disqualify

            # Count candidates above threshold for diagnostics.
            n_above += int((combined >= min_score).sum())

            # Pick the single highest-scoring location for this template+scale.
            flat_idx = int(np.argmax(combined))
            if combined.size == 0:
                continue
            yi, xi = np.unravel_index(flat_idx, combined.shape)
            score = float(combined[yi, xi])
            if score < min_score:
                continue
            per_ch = (
                float(sR[yi, xi]) if 0 <= yi < sR.shape[0] and 0 <= xi < sR.shape[1] else 0.0,
                float(sG[yi, xi]) if 0 <= yi < sG.shape[0] and 0 <= xi < sG.shape[1] else 0.0,
                float(sB[yi, xi]) if 0 <= yi < sB.shape[0] and 0 <= xi < sB.shape[1] else 0.0,
            )
            cand = (
                score,
                int(xi),
                int(yi),
                int(tw),
                int(th),
                float(sc),
                per_ch,
                names[ti] if ti < len(names) else f"t{ti}",
            )
            if best is None:
                best = cand
            else:
                # Strictly higher score wins; near-ties prefer scale closer to 1.0.
                if score > best[0] + SCALE_TIE_TOL:
                    best = cand
                elif abs(score - best[0]) <= SCALE_TIE_TOL:
                    if abs(sc - 1.0) < abs(best[5] - 1.0):
                        best = cand

    if best is None:
        return None
    score, x_in_crop, y_in_crop, w_t, h_t, sc, per_ch, tname = best
    bbox_full = (x_in_crop + ox, y_in_crop + oy, w_t, h_t)
    return {
        "bbox": (int(bbox_full[0]), int(bbox_full[1]), int(bbox_full[2]), int(bbox_full[3])),
        "score": float(score),
        "details": {
            "scale": float(sc),
            "per_channel_scores": (float(per_ch[0]), float(per_ch[1]), float(per_ch[2])),
            "n_candidates_above_thresh": int(n_above),
            "template_used": str(tname),
            "weights": (float(weights[0]), float(weights[1]), float(weights[2])),
        },
    }


def availability() -> dict:
    """Diagnostic: how many templates loaded, where they came from."""
    cache = _ensure_templates()
    n = int(cache["templates"].shape[0])
    return {
        "n_templates": n,
        "names": list(cache["names"]),
        "size": int(cache["size"]),
        "cache_path": str(_TEMPLATES_NPZ),
        "real_dir": str(_REAL_ICON_GLOB_DIR),
    }


__all__ = ["find_icon_rgb_ncc", "availability"]
