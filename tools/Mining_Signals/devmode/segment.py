"""Cut a labelled capture into per-character 28x28 glyphs.

Wraps the maintainers' extractor helpers rather than re-implementing them:

  scripts/extract_labeled_glyphs.py
      _locate_icon_via_blacklist_match  (signal location-pin icon mask)
      _find_main_row_bounds             (row-band isolation)
      _segment_digits                   (column projection + width splitter)
      _glyph_to_28x28                   (grey tile format the CNNs train on)
  scripts/extract_rgb_signal_glyphs.py
      _glyph_to_28x28_rgb               (RGB tile format)

What is NOT reused: ``extract_region2_glyphs`` / ``extract_region1_glyphs``
themselves. They verify segmentation with a Tesseract executable at a fixed
path, write straight into the install tree, and log into
``tools/Mining_Signals/extract_debug.log``. Here the human-confirmed label
plus the glyph-approval step replace the Tesseract verifier, and the helper
module's debug log is redirected into dev_root()/logs.

The SAME function segments captures for training (glyph extraction) and for
benchmarking, so the stock and candidate models are scored on identical
tiles.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from PIL import Image

from . import kinds, paths

log = logging.getLogger(__name__)

SIGNAL_LEFT_MASK_PCT = 0.30   # extract_region2_glyphs' default


def _helpers():
    paths.ensure_import_paths()
    import extract_labeled_glyphs as xlg          # noqa: WPS433
    # Never append to the install tree's extract_debug.log.
    xlg._DEBUG_LOG = paths.sub("logs") / "extract_debug.log"
    return xlg


def _rgb_helper():
    paths.ensure_import_paths()
    import extract_rgb_signal_glyphs as xrgb      # noqa: WPS433
    return xrgb


def _drop_separators(row: np.ndarray, spans: list) -> list:
    """Drop thousands-separator commas from a signal row. The typed label
    has no ',', so a comma span would shift every glyph after it. A comma is
    a span whose ink is under 55% of the tallest span's height."""
    if len(spans) < 2:
        return list(spans)
    heights = _ink_heights(row, spans)
    tallest = max(heights)
    return [s for s, h in zip(spans, heights) if h >= 0.55 * tallest]


def _canon_bright_text(gray: np.ndarray) -> np.ndarray:
    """The helpers decide polarity by ``median > 140 -> dark text``. A HUD
    row under the selection highlight is WHITE text on a LIGHT-blue
    background (median ~170), which that rule inverts the wrong way. When
    the bright Otsu class is the minority (it is the text), shift the
    levels down so the helpers keep the text bright. A pure level shift:
    stroke contrast is unchanged."""
    med = float(np.median(gray))
    if med <= 140:
        return gray
    xlg = _helpers()
    bright_frac = float((gray > xlg._otsu(gray)).mean())
    if bright_frac >= 0.45:
        return gray                      # genuinely dark text on light bg
    return np.clip(gray.astype(np.int16) - int(med - 100), 0, 255).astype(np.uint8)


def _ink_heights(row: np.ndarray, spans: list) -> list[int]:
    xlg = _helpers()
    work = 255 - row if np.median(row) > 140 else row
    binary = work > xlg._otsu(work)
    out = []
    for x1, x2 in spans:
        ys = np.where(binary[:, int(x1):int(x2)].any(axis=1))[0]
        out.append(int(ys[-1] - ys[0] + 1) if len(ys) else 0)
    return out


def _signal_layout(gray: np.ndarray, n: int):
    """Signal panel: [pin icon] gap [d,ddd]. Measured on live captures the
    pin's ink is ~1.8x the digit height and the comma under half of it, so
    both are removed by HEIGHT (colour does not separate them: the pin and
    the digits have the same saturation). The remaining digit band is then
    handed to _segment_digits with the expected count, so its width
    splitter can undo kerning merges like "10" -> "1","0"."""
    xlg = _helpers()
    bounds = xlg._find_main_row_bounds(gray)
    y1, y2 = bounds if bounds else (0, gray.shape[0])
    row = gray[y1:y2]
    spans = list(xlg._segment_digits(row))
    if not spans:
        return [], bounds, gray
    hs = _ink_heights(row, spans)
    med = float(np.median(hs))
    # Leading icon(s): taller than the digit run.
    while len(spans) > 1 and hs[0] > 1.35 * med:
        spans, hs = spans[1:], hs[1:]
    digit_h = max(hs)
    keep = [s for s, h in zip(spans, hs) if h >= 0.55 * digit_h]
    if not keep:
        return [], bounds, gray
    x_lo, x_hi = int(keep[0][0]), int(keep[-1][1])
    g = gray.copy()
    bg = int(np.median(row))
    g[:, :x_lo] = bg
    for s, h in zip(spans, hs):
        if h < 0.55 * digit_h:
            g[y1:y2, int(s[0]):int(s[1])] = bg          # erase commas
    band = g[y1:y2, x_lo:x_hi]
    sub: list = []
    # A comma kerned into the next digit (",1") survives the height test;
    # the width splitter cuts it off and the height test then drops it.
    for extra in (0, 1, 2):
        sub = _drop_separators(band, xlg._segment_digits(band, expected_count=n + extra))
        if len(sub) >= n:
            break
    return [(int(a) + x_lo, int(b) + x_lo) for a, b in sub[-n:]], bounds, g


def _spans_for(gray: np.ndarray, n: int, fam: str) -> tuple[list, Optional[tuple[int, int]], np.ndarray]:
    """Return (spans, (y1, y2) row bounds or None, the grey array used)."""
    xlg = _helpers()
    attempts: list[np.ndarray] = []
    if fam == "signal":
        spans, bounds, g = _signal_layout(gray, n)
        if len(spans) == n:
            return spans, bounds, g
        # Fall back to the maintainers' mask: 30% left floor + blacklist icon match.
        w = gray.shape[1]
        icon_right = xlg._locate_icon_via_blacklist_match(gray)
        icon_mask = icon_right + 4 if icon_right > 0 else 0
        for floor in (int(w * SIGNAL_LEFT_MASK_PCT), 0):
            mask_w = max(floor, icon_mask)
            g = gray.copy()
            if 0 < mask_w < w:
                g[:, :mask_w] = int(np.median(gray))
            attempts.append(g)
    else:
        attempts.append(gray)
    best: tuple[list, Optional[tuple[int, int]], np.ndarray] = ([], None, gray)
    for g in attempts:
        bounds = xlg._find_main_row_bounds(g)
        row = g[bounds[0]:bounds[1]] if bounds else g
        spans = xlg._segment_digits(row, expected_count=n)
        if fam == "signal":
            spans = _drop_separators(row, spans)
            if len(spans) < n:
                # A dropped comma leaves one span short: ask the splitter again.
                spans = _drop_separators(row, xlg._segment_digits(row, expected_count=n + 1))
        if len(spans) >= n:
            # Extra spans are leading chrome (icon residue, a field label);
            # values are right-aligned, so keep the rightmost n.
            return list(spans[-n:]), bounds, g
        if len(spans) > len(best[0]):
            best = (list(spans), bounds, g)
    return best


def glyphs_for(img: Image.Image, kind: str, label: str) -> Optional[list[np.ndarray]]:
    """Tiles for each character of ``label`` (28x28 L, or 28x28x3 RGB for
    rgb kinds), or None when the capture cannot be cut into exactly
    len(label) glyphs."""
    fam = kinds.family(kind)
    n = len(label)
    if n == 0:
        return None
    rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    if gray.shape[0] < 6 or gray.shape[1] < 6:
        return None
    gray = _canon_bright_text(gray)
    spans, bounds, gray_used = _spans_for(gray, n, fam)
    if len(spans) != n:
        return None
    y1, y2 = bounds if bounds else (0, gray.shape[0])
    g_row = gray_used[y1:y2]
    out: list[np.ndarray] = []
    if kinds.is_rgb(kind):
        xrgb = _rgb_helper()
        rgb_row = rgb[y1:y2]
        for x1, x2 in spans:
            t = xrgb._glyph_to_28x28_rgb(rgb_row, g_row, int(x1), int(x2))
            if t is None:
                return None
            out.append(t)
    else:
        xlg = _helpers()
        for x1, x2 in spans:
            t = xlg._glyph_to_28x28(g_row, int(x1), int(x2))
            if t is None:
                return None
            out.append(t)
    return out


def is_blacklisted(tile: np.ndarray) -> bool:
    xlg = _helpers()
    t = tile if tile.ndim == 2 else np.asarray(Image.fromarray(tile).convert("L"))
    return bool(xlg._is_blacklisted(t))


def to_input(tiles: list[np.ndarray], kind: str) -> np.ndarray:
    """Tiles -> model batch, same convention as the runtime
    (sc_ocr/api.py) and scripts/train_for_region.py: float32 /255, CHW,
    ``1 - x`` for ``_inv`` kinds."""
    if kinds.is_rgb(kind):
        arr = np.stack([np.asarray(t, dtype=np.float32) for t in tiles]).transpose(0, 3, 1, 2)
    else:
        arr = np.stack([np.asarray(t, dtype=np.float32) for t in tiles])[:, None, :, :]
    arr = arr / 255.0
    if kinds.is_inv(kind):
        arr = 1.0 - arr
    return arr.astype(np.float32)
