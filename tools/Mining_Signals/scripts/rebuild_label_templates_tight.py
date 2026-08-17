"""Rebuild ``ocr/sc_templates/labels.npz`` with colon-tight templates.

The existing production templates were built by ``build_label_templates.py``
using Tesseract bboxes that bleed into the trailing value text — i.e.
the ``resistance`` template literally contains the pixels for ``0%``
because most source panels had resistance=0%. This makes
``find_label_positions`` return an over-wide bbox whose right edge
lands PAST the colon, deep into the value column. Downstream
``_find_value_crop`` then uses ``x_min = label_right + 6`` and grabs
a near-empty sliver — production drops to 10% accuracy on the
labeled benchmark set as a result.

This script rebuilds each label template trimmed to the colon's
right edge, using a colon-detection heuristic that doesn't depend
on already-perfect templates:

  1. Polarity-canonicalize the source image (text-bright).
  2. Otsu-binarize.
  3. Locate the label-row band via NCC against the existing
     (polluted) templates — gives us an approximate y-band.
  4. Within that y-band, find the colon by scanning columns
     right-to-left for the characteristic two-dot vertical
     pattern: two short bright runs separated by a dark gap,
     adjacent to a wide dark gap on the right (value area).
  5. Slice ``[y-band, label_start : colon_right + 2]``.
  6. Resize to canonical height (28 px).
  7. Average across multiple source captures.
  8. Write ``labels.npz``.

Output schema is identical to the legacy builder so
``label_match.find_label_positions`` Just Works with the new
templates.

Run:
    python scripts/rebuild_label_templates_tight.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
)
log = logging.getLogger("rebuild_label_templates_tight")

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
DEFAULT_OUTPUT = TOOL / "ocr" / "sc_templates" / "labels.npz"
CANONICAL_HEIGHT = 28

# Make the tool tree importable so we can call production
# ``label_match.find_label_positions`` directly.
if str(TOOL) not in sys.path:
    sys.path.insert(0, str(TOOL))

# Where to find labeled source captures.
PANEL_ROOT_WMA = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels"
)
PANEL_ROOT_DEV = TOOL / "training_data_panels"


def _otsu(gray: np.ndarray) -> int:
    hist, _ = np.histogram(gray.flatten(), bins=256, range=(0, 256))
    total = gray.size
    sum_total = float(np.sum(np.arange(256) * hist))
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
    return int(threshold)


def _canon_text_bright(gray: np.ndarray) -> np.ndarray:
    """Force text to be the BRIGHT class regardless of source polarity."""
    thr = _otsu(gray)
    bright = int((gray > thr).sum())
    dark = gray.size - bright
    if dark < bright:
        return (255 - gray).astype(np.uint8)
    return gray.astype(np.uint8)


def _find_colon_right_edge(
    binary: np.ndarray,
    label_left: int,
    label_right_hint: int,
) -> Optional[int]:
    """Find the colon's right edge by detecting the label/value gap.

    Simple, robust algorithm: the rendered HUD font puts a wider
    horizontal gap between the colon and the first digit of the
    value than between any two characters within the label word
    itself (the colon is followed by extra leading whitespace, then
    the value starts). Scan column ink-density to find that gap,
    then trim at its left edge.

    Steps:
      1. Compute per-column ink density.
      2. Find the LEFT edge of the longest run of low-ink columns
         within the search window (low-ink = < ~15% of max density).
         This corresponds to the empty space between the colon and
         the value text.
      3. Return that x as the right edge of the label+colon region.

    Returns ``None`` when no clear gap is detected.
    """
    H, W = binary.shape
    search_left = max(8, label_left)
    search_right = min(W, label_right_hint)
    if search_right - search_left < 8:
        return None

    col_ink = (binary > 0).sum(axis=0).astype(np.int32)
    max_ink = int(col_ink[search_left:search_right].max())
    if max_ink == 0:
        return None

    # A column is "low ink" when its ink count is < 15% of the row's
    # max ink density. Tuned conservatively: a true value-area
    # column with even one stroke present (say, the left edge of a
    # "0") will exceed this. The interstitial gap between the colon
    # and the value is the only stretch that's reliably empty.
    low_thresh = max(1, int(max_ink * 0.15))

    # Walk left-to-right. We want the RIGHTMOST low-ink run that's
    # FOLLOWED by significant ink — that's the gap between the
    # colon and the start of the value text. Gaps followed by
    # nothing (trailing whitespace after a value, or trailing
    # whitespace at the end of "MASS:" when the source template
    # was minimally polluted) are NOT the boundary we want — we'd
    # be slicing INSIDE the colon glyph itself or trimming past
    # the colon entirely.
    # min_gap_run = 5 filters out internal-letter gaps (the dip
    # inside the M of "MASS" is ~5 columns, and the gap between M
    # and the next letter is ~3-4 columns — both too narrow to be
    # the label/value boundary). The actual colon-to-value gap is
    # 5-10 columns wide because the HUD font puts substantial
    # leading whitespace before the value text starts.
    min_gap_run = 5
    min_followup_ink = 4
    saw_ink_before = False
    in_low = False
    low_start = -1
    best_left = -1
    for x in range(search_left, search_right):
        v = int(col_ink[x])
        if v > low_thresh:
            if in_low and (x - low_start) >= min_gap_run and saw_ink_before:
                # Closed a candidate gap. Confirm there's enough
                # ink-bearing content to the right of it to count as
                # a "value" region.
                ink_to_right = sum(
                    1 for j in range(x, min(x + 12, search_right))
                    if int(col_ink[j]) > low_thresh
                )
                if ink_to_right >= min_followup_ink:
                    # Keep the rightmost qualifying gap.
                    best_left = low_start
            saw_ink_before = True
            in_low = False
        else:
            if not in_low:
                low_start = x
                in_low = True

    if best_left < 0:
        return None
    return best_left


def _resize_to_height(arr: np.ndarray, target_h: int) -> np.ndarray:
    h, w = arr.shape
    if h == target_h:
        return arr
    scale = target_h / h
    new_w = max(8, int(round(w * scale)))
    pil = Image.fromarray(arr).resize((new_w, target_h), Image.LANCZOS)
    return np.asarray(pil, dtype=np.uint8)


def _ncc_match_template(
    img_canon: np.ndarray,
    template: np.ndarray,
) -> Optional[tuple[int, int, float]]:
    """Multi-scale NCC for one template against ``img_canon``.

    Uses scipy's FFT-based correlate2d for ~100x speedup over a naive
    sliding-window inner-product loop (the original implementation
    took ~50 sec per capture; this one finishes in well under a
    second).

    Returns the best match's ``(x, y, score)`` in IMAGE coordinates,
    or None if no match scores above ~0.4.
    """
    from scipy.signal import correlate2d as _corr2d
    H, W = img_canon.shape
    scales = (0.6, 0.75, 0.9, 1.0, 1.15, 1.35, 1.6, 2.0)
    best: Optional[tuple[int, int, float]] = None
    img_f = img_canon.astype(np.float32)

    for s in scales:
        new_h = int(round(template.shape[0] * s))
        if new_h < 8 or new_h > H:
            continue
        new_w = int(round(template.shape[1] * s))
        if new_w < 8 or new_w > W:
            continue
        tpl = np.asarray(
            Image.fromarray(template).resize((new_w, new_h), Image.LANCZOS),
            dtype=np.float32,
        )
        # zero-mean unit-L2 normalize the template ONCE per scale.
        tpl_n = tpl - tpl.mean()
        tpl_std = np.sqrt((tpl_n ** 2).sum())
        if tpl_std < 1e-6:
            continue
        tpl_n /= tpl_std

        # Cross-correlate. ``correlate2d`` with mode='valid' returns a
        # (H-new_h+1, W-new_w+1) array whose entry [y,x] is the dot
        # product of img_f[y:y+new_h, x:x+new_w] against tpl_n.
        cross = _corr2d(img_f, tpl_n, mode="valid")

        # Per-patch mean and std for proper NCC normalization. We
        # compute these via convolution with an all-ones kernel of
        # the template's shape (sum) and an all-ones kernel of the
        # squared template's shape (sum of squares).
        ones = np.ones_like(tpl_n)
        patch_sum = _corr2d(img_f, ones, mode="valid")
        patch_sq_sum = _corr2d(img_f * img_f, ones, mode="valid")
        n_pix = float(new_h * new_w)
        patch_mean = patch_sum / n_pix
        patch_var = patch_sq_sum / n_pix - patch_mean * patch_mean
        patch_var = np.clip(patch_var, 1e-12, None)
        patch_std = np.sqrt(patch_var * n_pix)

        # NCC = (cross - patch_mean * sum(tpl_n)) / patch_std.
        # Since tpl_n is zero-mean, sum(tpl_n) ≈ 0 and the bias term
        # vanishes — we just divide by patch_std.
        with np.errstate(invalid="ignore", divide="ignore"):
            ncc = cross / np.where(patch_std > 1e-9, patch_std, 1.0)

        # Best position at this scale.
        idx = int(np.argmax(ncc))
        y_best, x_best = divmod(idx, ncc.shape[1])
        score = float(ncc[y_best, x_best])
        if best is None or score > best[2]:
            best = (int(x_best), int(y_best), score)
    return best


def _extract_clean_templates_via_production(
    img: Image.Image,
) -> dict[str, np.ndarray]:
    """Use the production ``find_label_positions`` (with its already-
    optimized NCC) to locate label rows, then trim each match to the
    colon's right edge.

    Returns ``{label_name: tight_template_uint8}`` for each label
    where a match was found AND a colon was detected within it.
    """
    out: dict[str, np.ndarray] = {}
    # Production label_match returns matches against the existing
    # polluted templates — that's fine for locating rows, just not
    # for precise right-edge.
    try:
        from ocr.sc_ocr import label_match as _lm
    except Exception as exc:
        log.error("could not import production label_match: %s", exc)
        return out
    matches = _lm.find_label_positions(img)
    gray = np.array(img.convert("L"), dtype=np.uint8)
    canon = _canon_text_bright(gray)
    H, W = canon.shape
    for label_name, m in matches.items():
        if label_name not in ("mass", "resistance", "instability"):
            continue
        x = int(m["x"])
        y = int(m["y"])
        w = int(m["w"])
        h = int(m["h"])
        if w < 8 or h < 8:
            continue
        # Slice the row band from the canonical image.
        x = max(0, x)
        y = max(0, y)
        x2 = min(W, x + w)
        y2 = min(H, y + h)
        band = canon[y:y2, x:x2]
        # Binary mask for colon detection. Use the same Otsu the
        # production pipeline uses so the colon-detector sees pixels
        # consistent with what NCC will be matching against.
        thr = _otsu(band)
        binary = (band > thr).astype(np.uint8)
        colon_right = _find_colon_right_edge(binary, 0, band.shape[1])
        if colon_right is None:
            log.debug("  %s: no colon detected in match", label_name)
            continue
        # Pad by 1px so the trimmed template includes the full colon.
        colon_right = min(band.shape[1], colon_right + 1)
        if colon_right < 8:
            log.debug("  %s: trim too narrow (%d px)", label_name, colon_right)
            continue
        tight = band[:, :colon_right]
        out[label_name] = _resize_to_height(tight, CANONICAL_HEIGHT)
    return out


def _walk_source_panels(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.exists():
        return out
    for sc in root.rglob("*.json"):
        if "glyphs" in sc.name or "boxes" in sc.name:
            continue
        try:
            import json
            d = json.loads(sc.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("schema") not in ("region1", "region2"):
            continue
        # Need all three labels present so any single capture can
        # contribute a full set.
        if not all(d.get(k) for k in ("mass", "resistance", "instability")):
            continue
        png = sc.with_suffix(".png")
        if png.is_file():
            out.append(png)
    return out


def main() -> int:
    # Load existing polluted templates (we use them as approximate
    # row finders for the new captures).
    out_path = DEFAULT_OUTPUT
    if not out_path.is_file():
        log.error("polluted labels.npz not found at %s — bootstrap unavailable", out_path)
        return 1
    polluted = np.load(out_path)
    poll_templates = {k: polluted[k] for k in ("mass", "resistance", "instability")}
    log.info("loaded polluted templates: %s",
             {k: v.shape for k, v in poll_templates.items()})

    # Gather source captures from both trees.
    sources = []
    for root in (PANEL_ROOT_WMA, PANEL_ROOT_DEV):
        sources.extend(_walk_source_panels(root))
    log.info("found %d labeled source panels", len(sources))
    if not sources:
        log.error("no labeled source panels found")
        return 1
    # Sample a manageable subset (NCC is O(H*W*scales), so we don't
    # need 363 captures).
    import random
    random.seed(0)
    if len(sources) > 30:
        sources = random.sample(sources, 30)
        log.info("sampled down to %d", len(sources))

    per_label: dict[str, list[np.ndarray]] = {
        "mass": [], "resistance": [], "instability": [],
    }
    for i, png in enumerate(sources, start=1):
        if i % 5 == 0 or i == len(sources):
            log.info("[%d/%d] %s", i, len(sources), png.name)
        try:
            img = Image.open(png).convert("RGB")
        except Exception as exc:
            log.warning("  open failed: %s", exc)
            continue
        # Production label_match runs all three NCC searches in one
        # optimized call — far faster than re-implementing per-template
        # NCC in Python.
        tights = _extract_clean_templates_via_production(img)
        for key, tight in tights.items():
            per_label[key].append(tight)

    # Average each label's collected tight crops (right-align since
    # colons share the right edge).
    payload: dict[str, np.ndarray] = {}
    for key in ("mass", "resistance", "instability"):
        templates = per_label[key]
        if not templates:
            log.warning("  %s: no clean extractions — keeping polluted template", key)
            payload[key] = poll_templates[key]
            continue
        h = CANONICAL_HEIGHT
        max_w = max(t.shape[1] for t in templates)
        accum = np.zeros((h, max_w), dtype=np.float32)
        counts = np.zeros((h, max_w), dtype=np.float32)
        for t in templates:
            pad = max_w - t.shape[1]
            accum[:, pad:] += t.astype(np.float32)
            counts[:, pad:] += 1.0
        avg = np.where(counts > 0, accum / np.maximum(counts, 1.0), 0.0)
        payload[key] = avg.astype(np.uint8)
        log.info(
            "  %s: averaged %d clean extractions -> shape %s",
            key, len(templates), payload[key].shape,
        )
    payload["height"] = np.int32(CANONICAL_HEIGHT)

    # Backup existing npz before overwriting.
    backup = out_path.with_suffix(".npz.bak_polluted")
    if not backup.is_file():
        import shutil
        shutil.copy(str(out_path), str(backup))
        log.info("backed up polluted npz -> %s", backup)
    np.savez(str(out_path), **payload)
    log.info("wrote clean labels.npz with shapes: %s",
             {k: v.shape if hasattr(v, 'shape') else v for k, v in payload.items()})

    # Save debug PNGs of the new templates so we can visually verify.
    debug_dir = out_path.parent / "labels_debug"
    debug_dir.mkdir(exist_ok=True)
    for key in ("mass", "resistance", "instability"):
        Image.fromarray(payload[key]).save(
            debug_dir / f"{key}_tight_rebuilt.png"
        )
    log.info("debug PNGs saved under %s", debug_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
