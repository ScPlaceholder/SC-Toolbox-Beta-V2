"""Extract per-digit 28×28 training glyphs from a labeled SCAN RESULTS panel.

Given a panel image and a label dict (from the labeler), this walks
every numeric field the user typed and pairs each character with the
corresponding segmented glyph in the image. Output: per-class PNGs
under ``training_data_user_panel/<char>/`` ready for
``ocr/train_model.py``.

Called by the labeler after each Save+Next.

Can also be run standalone to back-fill from all already-labeled
panels:
  python scripts/extract_labeled_glyphs.py --all
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
sys.path.insert(0, str(TOOL))

# NOTE: we deliberately don't import ocr.onnx_hud_reader because its
# _find_label_rows uses pytesseract which hangs indefinitely on some
# Python 3.14 installs. We re-implement label-row detection using
# direct tesseract.exe subprocess calls (same approach as the labeler).
import re as _re
import subprocess as _subprocess
from typing import Optional as _Optional

_TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _otsu(gray: np.ndarray) -> int:
    """Otsu's threshold — chooses threshold that maximizes between-class variance."""
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    sum_total = float(np.sum(np.arange(256) * hist))
    sum_back, w_back, max_var, threshold = 0.0, 0, 0.0, 128
    for t in range(256):
        w_back += int(hist[t])
        if w_back == 0:
            continue
        w_fore = total - w_back
        if w_fore == 0:
            break
        sum_back += t * int(hist[t])
        m_back = sum_back / w_back
        m_fore = (sum_total - sum_back) / w_fore
        var = w_back * w_fore * (m_back - m_fore) ** 2
        if var > max_var:
            max_var = var
            threshold = t
    return threshold


def _tesseract_char_boxes(
    img: Image.Image, whitelist: str = "0123456789", psm: str = "7",
) -> list[tuple[str, int, int, int, int]]:
    """Run Tesseract in makebox mode and return per-character boxes
    as ``[(char, x1, y1, x2, y2), …]`` in top-left-origin pixel
    coords, sorted left-to-right.

    Tesseract's character-level segmentation uses its trained model,
    which handles tightly-kerned digits (where column projection
    sees no gap), multi-line crops (it picks the dominant baseline),
    and icon/text noise (whitelist filtering eliminates non-digit
    pixels). Result is far more accurate per-glyph than pure
    column projection on SC's sci-fi font.

    Returns [] on any failure — caller should fall back to
    column-projection segmentation.
    """
    import tempfile
    import os as _os
    img_h = img.height
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp_in = tf.name
    base_out = tmp_in[:-4] + "_box"
    box_path = base_out + ".box"
    try:
        img.save(tmp_in)
        _subprocess.check_output(
            [
                _TESSERACT_EXE, tmp_in, base_out,
                "-c", f"tessedit_char_whitelist={whitelist}",
                "--psm", psm,
                "batch.nochop", "makebox",
            ],
            timeout=10,
            stderr=_subprocess.DEVNULL,
            creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        try: _os.unlink(tmp_in)
        except Exception: pass
        return []
    finally:
        try: _os.unlink(tmp_in)
        except Exception: pass

    if not _os.path.isfile(box_path):
        return []

    boxes: list[tuple[str, int, int, int, int]] = []
    try:
        with open(box_path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 5:
                    continue
                ch = parts[0]
                # Skip multi-character "words" Tesseract sometimes
                # emits when its character segmenter gives up.
                if len(ch) != 1:
                    continue
                try:
                    x1 = int(parts[1])
                    y1_from_bot = int(parts[2])
                    x2 = int(parts[3])
                    y2_from_bot = int(parts[4])
                except ValueError:
                    continue
                # Tesseract uses bottom-origin y; convert to top-origin
                # so it lines up with PIL/numpy [row, col] indexing.
                y_top = img_h - y2_from_bot
                y_bot = img_h - y1_from_bot
                boxes.append((ch, x1, y_top, x2, y_bot))
    finally:
        try: _os.unlink(box_path)
        except Exception: pass

    boxes.sort(key=lambda b: b[1])  # left-to-right
    return boxes


def _tesseract_tsv(img: Image.Image) -> list[dict]:
    """Run tesseract and return per-word TSV rows (dict with text/left/top/width/height)."""
    import tempfile
    import os as _os
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp = tf.name
    try:
        img.save(tmp)
        out = _subprocess.check_output(
            [_TESSERACT_EXE, tmp, "-", "tsv"],
            timeout=10, text=True,
            stderr=_subprocess.DEVNULL,
            creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    finally:
        try: _os.unlink(tmp)
        except Exception: pass
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            rows.append({
                "text": text,
                "left": int(parts[6]),
                "top": int(parts[7]),
                "width": int(parts[8]),
                "height": int(parts[9]),
            })
        except Exception:
            continue
    return rows


def _preprocess_img_for_tess(img: Image.Image) -> Image.Image:
    """Warm-channel + auto-polarity preprocessing for Tesseract.
    Works on both dark-space and light-nebula backgrounds.
    """
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    R = arr[..., 0]; G = arr[..., 1]; B = arr[..., 2]
    # Orange signature strength
    warm = np.clip((R - B).clip(0, None).astype(np.int32) +
                   (R - G).clip(0, None).astype(np.int32) * 2,
                   0, 255).astype(np.uint8)
    # Stretch contrast
    lo, hi = int(np.percentile(warm, 50)), int(np.percentile(warm, 99))
    if hi > lo:
        warm = np.clip((warm.astype(np.int32) - lo) * 255 // max(1, hi - lo),
                       0, 255).astype(np.uint8)
    # Binary: bright warm = text; invert so text is BLACK for Tesseract
    binary = np.where(warm > 60, 0, 255).astype(np.uint8)
    return Image.fromarray(binary, mode="L")


def _find_label_rows(img: Image.Image) -> dict:
    """Find y-bands + right-edge of MASS/RESISTANCE/INSTABILITY labels.
    Uses direct tesseract.exe call (no pytesseract).

    Returns {field: (y1, y2, label_right_x)} for fields that were found.
    """
    # Try preprocessed first (robust across backgrounds), fall back to
    # raw grayscale if that yields nothing.
    proc = _preprocess_img_for_tess(img)
    rows = _tesseract_tsv(proc)
    if not rows:
        rows = _tesseract_tsv(img.convert("L"))
    out: dict = {}
    for r in rows:
        t = r["text"].upper().rstrip(":")
        field = None
        if "MASS" in t: field = "mass"
        elif "RESIS" in t or "RESIS1" in t: field = "resistance"
        elif "INSTAB" in t: field = "instability"
        if field and field not in out:
            y1 = r["top"]
            y2 = r["top"] + r["height"]
            label_right = r["left"] + r["width"]
            # Extend band ±10 px vertically to catch the full row
            out[field] = (max(0, y1 - 10), y2 + 10, label_right)
    return out


def _find_value_crop(img: Image.Image, gray: np.ndarray,
                     y1: int, y2: int, x_min: int = 0) -> _Optional[Image.Image]:
    """Crop the value portion to the right of the label. Pure-numpy.
    Polarity-aware: text may be brighter OR darker than the background
    depending on whether the panel is on dark-space or light-nebula."""
    if y1 < 0: y1 = 0
    if y2 > gray.shape[0]: y2 = gray.shape[0]
    band = gray[y1:y2, x_min:]
    if band.size == 0:
        return None
    # Auto-detect polarity via median
    median = int(np.median(band))
    light_bg = median > 140
    if light_bg:
        # Text is darker than bg — count DARK pixels per column
        col_mask = (band < median - 20).sum(axis=0)
    else:
        col_mask = (band > median + 20).sum(axis=0)
    thr = max(1, int(band.shape[0] * 0.10))
    active = np.where(col_mask >= thr)[0]
    if len(active) < 3:
        return None
    vx1 = int(active[0])
    vx2 = int(active[-1]) + 1
    vx1 = max(0, vx1 - 4)
    vx2 = min(band.shape[1], vx2 + 4)
    crop = img.crop((x_min + vx1, y1, x_min + vx2, y2))
    return crop

PANELS_ROOT = TOOL / "training_data_panels"
PANEL_GLYPH_ROOT = TOOL / "training_data_user_panel"
SIG_GLYPH_ROOT   = TOOL / "training_data_user_sig"
BLACKLIST_DIR    = TOOL / "training_data_blacklist"

# Similarity threshold for blacklist match (0..1, lower = stricter)
_BLACKLIST_THR = 0.88


def _phash(img_arr: np.ndarray) -> np.ndarray:
    """Simple 8x8 perceptual hash — average-then-threshold.
    Input: any HxW grayscale array. Output: 64-bit bit array (uint8[64]).
    """
    from PIL import Image as _PILImg
    pil = _PILImg.fromarray(img_arr.astype(np.uint8)).convert("L").resize((8, 8), _PILImg.BILINEAR)
    small = np.asarray(pil, dtype=np.float32)
    mean = small.mean()
    return (small > mean).astype(np.uint8).flatten()


def _hash_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Hamming similarity in [0, 1]. 1.0 = identical."""
    if a.shape != b.shape:
        return 0.0
    return 1.0 - float(np.sum(a != b)) / len(a)


_blacklist_hashes: Optional[list[np.ndarray]] = None


def _load_blacklist() -> list[np.ndarray]:
    """Compute perceptual hashes for every PNG in training_data_blacklist/."""
    global _blacklist_hashes
    if _blacklist_hashes is not None:
        return _blacklist_hashes
    hashes: list[np.ndarray] = []
    if BLACKLIST_DIR.is_dir():
        for f in BLACKLIST_DIR.rglob("*.png"):
            try:
                im = Image.open(f).convert("L")
                arr = np.asarray(im, dtype=np.uint8)
                hashes.append(_phash(arr))
            except Exception:
                continue
    _blacklist_hashes = hashes
    if hashes:
        _debug(f"loaded {len(hashes)} blacklist hashes")
    return hashes


def _locate_icon_via_blacklist_match(gray: np.ndarray) -> int:
    """Slide each blacklist entry across the leftmost half of the
    image, return the rightmost x of the best-matching window.

    Returns -1 if no blacklist entry / no match ≥ 0.80 similarity /
    the input is too small to scan.

    Works regardless of whether the icon is touching adjacent digits
    (the failure mode of column-projection-based detection): pHash
    operates on the visual content of a window, not on whether
    columns are zero between regions.
    """
    bl = _load_blacklist()
    if not bl or gray.shape[0] < 8 or gray.shape[1] < 12:
        return -1
    H, W = gray.shape
    # Try a handful of candidate window widths bracketing observed
    # icon sizes (~10-30 px tall, similar wide). The pHash downsamples
    # everything to 8x8 anyway so the absolute window size matters
    # less than that it ROUGHLY matches the icon's aspect ratio.
    best_sim = 0.0
    best_x = -1
    search_end = int(W * 0.55)
    # Window dimensions to try: square-ish, sized for the row band
    win_h = min(H, max(12, int(H * 0.9)))
    y_off = (H - win_h) // 2
    win_widths = [10, 14, 20, 28, 36, 44]
    step = 2
    for wW in win_widths:
        if wW > search_end:
            continue
        for x in range(0, search_end - wW, step):
            window = gray[y_off:y_off + win_h, x:x + wW]
            if window.shape[0] < 4 or window.shape[1] < 4:
                continue
            try:
                h = _phash(window.astype(np.uint8))
            except Exception:
                continue
            for ref in bl:
                sim = _hash_similarity(h, ref)
                if sim > best_sim:
                    best_sim = sim
                    best_x = x + wW
    if best_sim >= 0.80:
        return best_x
    return -1


def _is_blacklisted(glyph_28x28: np.ndarray) -> bool:
    """Check glyph against blacklist. Returns True if it looks like a
    known-bad UI icon or artifact."""
    bl = _load_blacklist()
    if not bl:
        return False
    h = _phash(glyph_28x28)
    for ref in bl:
        if _hash_similarity(h, ref) >= _BLACKLIST_THR:
            return True
    return False

# Debug log — each extraction run appends one line per field/row
_DEBUG_LOG = TOOL / "extract_debug.log"
_DEBUG = True  # Set False to silence


def _debug(msg: str) -> None:
    if not _DEBUG:
        return
    try:
        from datetime import datetime as _dt
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{_dt.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass

# Reference height the label detector is tuned for
REF_H = 541

# Numeric fields in region1 and how they map to the label dict + which
# panel row to look in.
#   label_field: the key in the user's JSON label
#   row_key:     where in `_find_label_rows(panel)` to look for the y-band
#                (or None if we locate it differently)
PANEL_FIELDS = [
    # simple values keyed off label_rows
    ("mass",        "mass",         None),
    ("resistance",  "resistance",   None),
    ("instability", "instability",  None),
]


def _isolate_main_row(gray: np.ndarray) -> np.ndarray:
    """Crop a multi-line capture to just the row containing the
    largest text band.

    Bug this fixes: dual_capture overlays are sometimes dragged
    taller than the signature number (capturing the icon caption
    'UNK', secondary line '13', etc. above/below). Column-projection
    segmentation collapses ink from EVERY row into one long ink-run,
    producing 'glyphs' that are vertical stacks of multiple lines.
    Doing row segmentation first guarantees we hand the column
    segmenter only one line of digits.

    Strategy: polarity-canonicalize → Otsu → row projection → find
    contiguous bright bands → return the band with the most TOTAL
    ink (= largest font in that crop). Falls back to the original
    image if row segmentation finds nothing useful (pure-digit
    crop with no surrounding text)."""
    if gray.size == 0 or gray.shape[0] < 6:
        return gray
    # Polarity canonicalize: make digits the BRIGHT class.
    if np.median(gray) > 140:
        work = 255 - gray
    else:
        work = gray
    thr = _otsu(work)
    binary = (work > thr).astype(np.uint8)
    row_proj = binary.sum(axis=1)  # ink per row
    if row_proj.max() == 0:
        return gray
    # Find row bands: contiguous runs where ink ≥ 10% of the row's
    # max projection. 10% is generous enough to include the thin
    # parts of digits (the gap inside an '8', the tail of a '7')
    # but cuts the empty rows between text lines.
    threshold = max(1, int(row_proj.max() * 0.10))
    in_band = False
    band_start = 0
    bands: list[tuple[int, int, int]] = []  # (y_start, y_end, total_ink)
    for y in range(len(row_proj) + 1):
        v = int(row_proj[y]) if y < len(row_proj) else 0
        if v >= threshold and not in_band:
            in_band = True
            band_start = y
        elif v < threshold and in_band:
            in_band = False
            if y - band_start >= 4:  # ignore < 4 px tall bands (noise)
                ink = int(row_proj[band_start:y].sum())
                bands.append((band_start, y, ink))
    if not bands:
        return gray
    # Pick the band with MOST ink (= biggest text). Pad ±2 px so we
    # don't clip ascenders/descenders.
    bands.sort(key=lambda b: b[2], reverse=True)
    y1, y2, _ = bands[0]
    y1 = max(0, y1 - 2)
    y2 = min(gray.shape[0], y2 + 2)
    return gray[y1:y2, :]


def _find_main_row_bounds(gray: np.ndarray) -> Optional[tuple[int, int]]:
    """Same band-finding logic as :func:`_isolate_main_row` but returns
    the (y_start, y_end) slice bounds instead of the trimmed array.

    Lets a caller apply the SAME y-band trim to multiple parallel
    arrays (e.g. luma + max-of-channels grayscales of the same panel)
    so they share a coordinate system. Returns None when no band is
    detected (caller should treat this the same way as
    ``_isolate_main_row`` no-op'ing — keep the original full array).
    """
    if gray.size == 0 or gray.shape[0] < 6:
        return None
    if np.median(gray) > 140:
        work = 255 - gray
    else:
        work = gray
    thr = _otsu(work)
    binary = (work > thr).astype(np.uint8)
    row_proj = binary.sum(axis=1)
    if row_proj.max() == 0:
        return None
    threshold = max(1, int(row_proj.max() * 0.10))
    in_band = False
    band_start = 0
    bands: list[tuple[int, int, int]] = []
    for y in range(len(row_proj) + 1):
        v = int(row_proj[y]) if y < len(row_proj) else 0
        if v >= threshold and not in_band:
            in_band = True
            band_start = y
        elif v < threshold and in_band:
            in_band = False
            if y - band_start >= 4:
                ink = int(row_proj[band_start:y].sum())
                bands.append((band_start, y, ink))
    if not bands:
        return None
    bands.sort(key=lambda b: b[2], reverse=True)
    y1, y2, _ = bands[0]
    y1 = max(0, y1 - 2)
    y2 = min(gray.shape[0], y2 + 2)
    return (y1, y2)


def _segment_digits(gray_crop: np.ndarray,
                    expected_count: Optional[int] = None) -> list[tuple[int, int]]:
    """Return list of (x_start, x_end) spans for each digit-like run.

    Two improvements vs the original strict ``v==0`` segmenter:

    1. **Threshold-based gap detection.** SC's sci-fi font has near-
       zero column gaps between digits (antialiasing leaves 1-2 px of
       ink in the boundary column). Treating gaps as "ink ≤ 5% of
       per-column max" instead of strict zero correctly separates
       tightly-kerned digits like "274" or "54" that would otherwise
       merge into one ink-blob.

    2. **Width-equalising splitter.** When ``expected_count`` is
       given and the threshold pass still finds fewer spans, the
       fallback used to bail at the first "ambiguous" minimum (the
       0.7 ratio test). The new splitter ALWAYS splits the widest
       span at its lowest-ink interior column until count matches OR
       no remaining span is splittable. Even an imperfect split is
       better than glomming N digits into one glyph that gets saved
       under the label of the leading character.
    """
    # Polarity: if background is light, invert so glyphs are the bright set
    if np.median(gray_crop) > 140:
        work = 255 - gray_crop
    else:
        work = gray_crop
    thr = _otsu(work)
    binary = (work > thr).astype(np.uint8)
    proj = binary.sum(axis=0)
    w = binary.shape[1]
    if w == 0:
        return []

    # Strict v==0 for the first pass — separates digits that have
    # ANY background gap, keeps clean captures from being over-
    # segmented by antialiasing speckle. Tightly-kerned digits with
    # NO zero-ink gap are recovered later by the always-split
    # fallback.
    spans_list: list[list[int]] = []
    in_c = False
    start = 0
    for x in range(w + 1):
        v = int(proj[x]) if x < w else 0
        if v > 0 and not in_c:
            in_c = True
            start = x
        elif v == 0 and in_c:
            in_c = False
            if x - start >= 2:
                spans_list.append([start, x])

    if expected_count is None or len(spans_list) >= expected_count:
        return [tuple(s) for s in spans_list]
    if not spans_list:
        return []  # nothing to split

    # Need to split wide spans. Iteratively split the widest one at
    # its minimum-ink interior column until count matches or no span
    # is wide enough to safely split.
    MIN_SPLIT_WIDTH = 6
    safety = 32
    while len(spans_list) < expected_count and safety > 0 and spans_list:
        safety -= 1
        widest_idx = max(range(len(spans_list)),
                         key=lambda i: spans_list[i][1] - spans_list[i][0])
        s, e = spans_list[widest_idx]
        if e - s < MIN_SPLIT_WIDTH * 2:
            # Nothing wider than 12 px → no safe split anywhere.
            break
        # Look for the minimum projection in the central 80% of the
        # span (margin 10% each side) so we don't slice off a thin
        # ascender/descender at the edges.
        margin = max(1, (e - s) // 10)
        mid_a = s + margin
        mid_b = e - margin
        if mid_b - mid_a < 2:
            break
        sub = proj[mid_a:mid_b]
        split_x = mid_a + int(np.argmin(sub))
        # Always split — an imperfect split is preferable to leaving
        # multiple digits glued together. The 0.7-ratio bailout from
        # the previous version produced glyphs like "274" labeled as
        # one digit class.
        spans_list = (
            spans_list[:widest_idx]
            + [[s, split_x], [split_x, e]]
            + spans_list[widest_idx + 1:]
        )

    return [tuple(s) for s in spans_list]


def _glyph_to_28x28(gray_crop: np.ndarray, x1: int, x2: int) -> Optional[np.ndarray]:
    """Crop a glyph and normalize to a 28×28 uint8 array.

    Matches the training-data format produced by the legacy harvester:
    - tight crop around the ink
    - 2-px white padding
    - resized to 28×28 with BILINEAR
    """
    if np.median(gray_crop) > 140:
        work = 255 - gray_crop
    else:
        work = gray_crop
    thr = _otsu(work)
    binary = (work > thr).astype(np.uint8)
    glyph_col = binary[:, x1:x2]
    ys = np.where(np.any(glyph_col > 0, axis=1))[0]
    if len(ys) < 2:
        return None
    ya, yb = int(ys[0]), int(ys[-1]) + 1
    glyph = gray_crop[ya:yb, x1:x2].astype(np.float32)
    pad = 2
    padded = np.full(
        (glyph.shape[0] + pad * 2, glyph.shape[1] + pad * 2),
        255.0, dtype=np.float32,
    )
    padded[pad:pad + glyph.shape[0], pad:pad + glyph.shape[1]] = glyph
    pil = Image.fromarray(padded.astype(np.uint8)).resize(
        (28, 28), Image.BILINEAR,
    )
    return np.asarray(pil, dtype=np.uint8)


def _save_glyph(glyph: np.ndarray, char: str, src_name: str, out_root: Path) -> bool:
    """Save glyph under out_root/<class>/user_<src>_<i>.png.
    Class mapping: digits as-is, '.'→dot, '%'→pct.
    Dash and comma intentionally not trained — never appear on the
    panel except as OCR noise. Skips blacklisted glyphs."""
    class_map = {".": "dot", "%": "pct"}
    cls = class_map.get(char, char)
    if not (cls.isdigit() or cls in ("dot", "pct")):
        return False
    # Blacklist filter
    if _is_blacklisted(glyph):
        _debug(f"    BLACKLISTED glyph for {char!r} from {src_name}")
        return False
    d = out_root / cls
    d.mkdir(parents=True, exist_ok=True)
    # Find next free index so we never overwrite
    i = 0
    while True:
        out = d / f"user_{src_name}_{i}.png"
        if not out.exists():
            break
        i += 1
    try:
        Image.fromarray(glyph, mode="L").save(out)
        return True
    except Exception:
        return False


def _upscale_to_ref(img: Image.Image) -> tuple[Image.Image, float]:
    H = img.height
    if H < REF_H:
        s = REF_H / H
        return img.resize(
            (int(img.width * s), int(img.height * s)), Image.LANCZOS,
        ), s
    return img, 1.0


def extract_region1_glyphs(
    panel_png: Path, label: dict, out_root: Path,
) -> dict[str, int]:
    """Return {char: n_saved} for one labeled region-1 panel."""
    counts: dict[str, int] = {}
    _debug(f"=== {panel_png.name} ===")
    try:
        img = Image.open(panel_png).convert("RGB")
    except Exception as exc:
        _debug(f"  open failed: {exc}")
        return counts

    img, _scale = _upscale_to_ref(img)
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    _debug(f"  upscaled size={img.size} scale={_scale:.2f}")

    # Use the label-row detector to anchor fields
    try:
        rows = _find_label_rows(img)
    except Exception as exc:
        _debug(f"  label rows failed: {exc}")
        return counts
    if not rows:
        _debug("  no label rows found (Tesseract couldn't find MASS/etc)")
        return counts
    _debug(f"  label rows: {list(rows.keys())}")

    src_name = panel_png.stem

    for field_key, row_key, _unused in PANEL_FIELDS:
        raw = str(label.get(field_key, "")).strip()
        if not raw:
            continue
        # Chars we know how to train on
        chars = [c for c in raw if c.isdigit() or c in ".%"]
        if not chars:
            continue

        entry = rows.get(row_key)
        if entry is None:
            continue
        y1, y2, lbl_right = entry
        x_min = max(0, lbl_right + 6)
        value_crop = _find_value_crop(img, gray, y1, y2, x_min=x_min)
        if value_crop is None:
            continue

        gray_crop = np.asarray(value_crop.convert("L"), dtype=np.uint8)
        spans = _segment_digits(gray_crop, expected_count=len(chars))
        _debug(f"  field={field_key} raw={raw!r} chars={chars} spans={len(spans)}")
        if len(spans) < len(chars):
            _debug(f"    SKIP: not enough spans ({len(spans)} < {len(chars)})")
            continue  # not enough glyphs segmented
        if len(spans) > len(chars):
            # Value crop may include label text to the left (e.g.
            # "RESISTANCE: 0%"). Take the RIGHTMOST len(chars) spans
            # that are close together horizontally — that's the value.
            # Keep the last N spans as-is (they're right-aligned).
            spans = spans[-len(chars):]

        for (x1, x2), ch in zip(spans, chars):
            glyph = _glyph_to_28x28(gray_crop, x1, x2)
            if glyph is None:
                continue
            if _save_glyph(glyph, ch, src_name, out_root):
                counts[ch] = counts.get(ch, 0) + 1

    # SCU value — extracted from label text using a heuristic.
    # We'll search for a numeric cluster above the first mineral row.
    scu = str(label.get("scu", "")).strip()
    scu_chars = [c for c in scu if c.isdigit() or c == "."]
    if scu_chars:
        # SCU sits above mass row, right-aligned. Search top third of panel.
        H = gray.shape[0]
        band_top = H // 5
        band_bot = H // 2
        band = gray[band_top:band_bot]
        # Find horizontal rows with text
        row_dens = (band > 140).sum(axis=1)
        if row_dens.max() > 5:
            # Pick the row around the peak
            peak_y = int(np.argmax(row_dens))
            y_lo = max(0, peak_y - 15)
            y_hi = min(band.shape[0], peak_y + 15)
            scu_band = gray[band_top + y_lo:band_top + y_hi]
            # Use right half only (SCU is right-aligned)
            W = scu_band.shape[1]
            scu_crop = scu_band[:, W // 2:]
            spans = _segment_digits(scu_crop, expected_count=len(scu_chars))
            # Filter spans to numeric chars only (len should match)
            if spans and len(spans) >= len(scu_chars):
                for (x1, x2), ch in zip(spans[-len(scu_chars):], scu_chars):
                    glyph = _glyph_to_28x28(scu_crop, x1, x2)
                    if glyph is None:
                        continue
                    if _save_glyph(glyph, ch, src_name + "_scu", out_root):
                        counts[ch] = counts.get(ch, 0) + 1

    # Composition rows — find them below the COMPOSITION bar and
    # extract digits from pct and count columns.
    comp = label.get("composition", [])
    if isinstance(comp, list) and comp:
        _extract_composition_glyphs(img, gray, comp, src_name, out_root, counts)

    return counts


def _extract_composition_glyphs(
    img: Image.Image, gray: np.ndarray,
    comp_rows: list[dict], src_name: str, out_root: Path,
    counts: dict[str, int],
) -> None:
    """Extract per-digit glyphs from the composition section of a panel.

    Composition rows sit below a dashed bar. Each row has 3 segments:
    [pct%]  [material name]  [count]
    We only mine digits from the pct and count columns (names aren't
    labeled per-char).
    """
    H, W = gray.shape
    # Heuristic: composition section starts in the bottom half of the
    # panel. Scan from y=H//2 downward for rows of numeric text.
    start_y = H // 2
    # Row heights are roughly H/15 — rows detected by horizontal density
    row_mask = (gray > 140).sum(axis=1)
    # Find row bands with text in bottom half
    thr = max(3, int(np.percentile(row_mask[start_y:], 60)))
    in_band = False
    bands: list[tuple[int, int]] = []
    b_start = 0
    for y in range(start_y, H):
        if row_mask[y] >= thr:
            if not in_band:
                in_band = True
                b_start = y
        else:
            if in_band:
                in_band = False
                if y - b_start >= 8:
                    bands.append((b_start, y))
    if in_band:
        bands.append((b_start, H))

    # Match rows: skip the composition bar (very dense row) and the
    # first text row after it is the composition header. Data rows
    # follow, matching len(comp_rows).
    # Take the last N bands (where N = len(comp_rows)) as the data rows
    # since they sit at the bottom of the panel.
    _debug(f"  comp: saved_rows={len(comp_rows)} detected_bands={len(bands)}")
    if len(bands) < len(comp_rows):
        _debug(f"    SKIP: not enough bands for {len(comp_rows)} rows")
        return
    data_bands = bands[-len(comp_rows):]

    for (y1, y2), row in zip(data_bands, comp_rows):
        pct = str(row.get("pct", "")).strip()
        count = str(row.get("count", "")).strip()
        pct_chars = [c for c in pct if c.isdigit() or c in ".%"]
        count_chars = [c for c in count if c.isdigit()]
        row_band = gray[y1:y2]

        # Column structure: pct on left, name in middle, count on right.
        # Use column density to find 3 clusters, pick leftmost (pct)
        # and rightmost (count).
        col_mask = (row_band > 140).sum(axis=0)
        col_thr = max(1, int(row_band.shape[0] * 0.10))
        active = col_mask >= col_thr
        clusters = []
        cs = ce = -1
        for x in range(len(active)):
            if active[x]:
                if cs < 0:
                    cs = x
                ce = x
            elif cs >= 0 and x - ce > 12:
                clusters.append((cs, ce + 1))
                cs = ce = -1
        if cs >= 0:
            clusters.append((cs, ce + 1))
        _debug(f"    row pct={pct!r} count={count!r} clusters={len(clusters)}")
        if len(clusters) < 2:
            _debug("      SKIP: < 2 clusters (needs pct + count columns)")
            continue

        # pct = leftmost cluster
        if pct_chars:
            x1, x2 = clusters[0]
            crop = row_band[:, x1:x2]
            spans = _segment_digits(crop, expected_count=len(pct_chars))
            _debug(f"      pct spans={len(spans)} need={len(pct_chars)}")
            if len(spans) > len(pct_chars):
                spans = spans[-len(pct_chars):]
            if len(spans) == len(pct_chars):
                for (sx1, sx2), ch in zip(spans, pct_chars):
                    glyph = _glyph_to_28x28(crop, sx1, sx2)
                    if glyph is None:
                        continue
                    if _save_glyph(glyph, ch, src_name + "_pct", out_root):
                        counts[ch] = counts.get(ch, 0) + 1

        # count = rightmost cluster
        if count_chars:
            x1, x2 = clusters[-1]
            crop = row_band[:, x1:x2]
            spans = _segment_digits(crop, expected_count=len(count_chars))
            _debug(f"      count spans={len(spans)} need={len(count_chars)}")
            if len(spans) > len(count_chars):
                spans = spans[-len(count_chars):]
            if len(spans) == len(count_chars):
                for (sx1, sx2), ch in zip(spans, count_chars):
                    glyph = _glyph_to_28x28(crop, sx1, sx2)
                    if glyph is None:
                        continue
                    if _save_glyph(glyph, ch, src_name + "_cnt", out_root):
                        counts[ch] = counts.get(ch, 0) + 1


def extract_region2_glyphs(
    img_png: Path, label: dict, out_root: Path,
    left_mask_pct: float = 0.30,
) -> dict[str, int]:
    """Extract digits from a small signature-scanner crop.

    ``left_mask_pct`` (0.0 = no mask, 0.5 = ignore left half) blanks
    out the leftmost fraction of every crop BEFORE segmentation. The
    SC signature panel always renders a location-pin icon to the
    left of the digits; that icon (a) creates spurious spans, (b)
    sometimes merges with the leading digit, and (c) varies in
    pixel position across captures (the proportional position is
    stable, the absolute pixel position isn't because users capture
    at different crop sizes — observed ~204×140 and ~358×192). A
    proportional mask at ~30% reliably clears the icon column in
    both formats while leaving the entire digit string intact (in
    every observed sample the leading digit starts past the 30%
    mark).

    Robustness vs garbage glyphs:
      1. Apply ``left_mask_pct`` to the source image before
         segmentation so the icon never produces a span at all.
      2. Drop residual spans matching the blacklist (catches
         secondary artifacts on the right edge etc.).
      3. Drop spans wider than 45% of the image (whole-signature
         blob fallback) or narrower than 3 px (single-stroke noise).
      4. Only after these filters does the count have to match
         len(chars). If filtered count != chars, skip the capture
         rather than save aligned-wrong garbage.
    """
    counts: dict[str, int] = {}
    value = str(label.get("value", "")).strip().replace(",", "")
    if not value:
        return counts
    chars = [c for c in value if c.isdigit() or c == "."]
    if not chars:
        return counts
    try:
        # Luma-only extraction — historically-validated path. The
        # earlier dual-grayscale attempt (luma for Tesseract verify,
        # max-of-channels for saved 28×28 crops) produced visually
        # garbage crops for classes 0 / 6 / 8: the ``_glyph_to_28x28``
        # helper re-runs polarity-canonicalize + Otsu on the input
        # gray to find the y-extent of the glyph within the column
        # range, and max-of-channels' brighter histogram shifted
        # Otsu's threshold so the y-extent locked onto BACKGROUND
        # pixels — saved crops became multi-region collages with
        # ink fragments at top, middle, and bottom rather than a
        # single digit. Reverted to luma-only (one ``gray`` array
        # threaded through icon-mask, row-isolate, Tesseract verify,
        # span detection, AND final crop save) — same as the original
        # historical extractor that produced the 6,305-sample pool.
        img = Image.open(img_png).convert("L")
    except Exception:
        return counts
    gray = np.asarray(img, dtype=np.uint8)
    img_w = gray.shape[1]

    # ── Icon mask via blacklist pHash sliding match ──
    # The signature panel always renders a location-pin icon to the
    # left of the digits. Position varies per capture (x=35 to x=140)
    # and the icon often touches the leading digit, so column-
    # projection-based detection can't tell icon from digits.
    #
    # Sliding pHash match against the blacklist works regardless of
    # adjacency: scan candidate windows in the leftmost 50% of the
    # image, compare each window's pHash to every blacklist entry,
    # take the position with highest similarity. If similarity ≥
    # 0.80, treat it as the icon and mask through its right edge.
    bg = int(np.median(gray))
    gray = gray.copy()  # never mutate the on-disk image
    icon_right = _locate_icon_via_blacklist_match(gray)
    floor_mask = int(img_w * left_mask_pct) if left_mask_pct > 0 else 0
    mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
    if 0 < mask_w < img_w:
        gray[:, :mask_w] = bg
    if icon_right > 0:
        _debug(
            f"  region2: {img_png.name} icon-pHash-match "
            f"right={icon_right} mask_w={mask_w}/{img_w}"
        )

    # ── Row-band isolation ──
    # Capture-overlay rectangles are sometimes drawn taller than the
    # signature line, picking up captions like 'UNK' above or
    # secondary numbers like '13' below. Without this step the
    # column-projection segmenter collapses ink from EVERY visible
    # row into one ink-run per column, producing glyphs that are
    # vertical stacks of multiple lines. Crop to the row band with
    # the most total ink (= biggest text = the actual signature).
    pre_h = gray.shape[0]
    gray = _isolate_main_row(gray)
    if gray.shape[0] != pre_h:
        _debug(
            f"  region2: {img_png.name} row-isolated "
            f"{pre_h}px -> {gray.shape[0]}px"
        )

    # ── Tesseract char-box pass (ONLY segmenter for region2) ──
    # Tesseract's trained character recognizer cleanly separates
    # tightly-kerned digits and skips icons/captions via the digit
    # whitelist. We try multiple PSM modes + upscale variants to
    # maximize the agreement rate with the user's typed label.
    #
    # CRITICAL DESIGN CHOICE: there is NO column-projection fallback.
    # That fallback was the source of every garbage glyph the user
    # saw in the reviewer (multi-digit blobs, icons, wrong-character
    # crops aligned by accident). Better to lose a capture than save
    # contaminated training data — quality > quantity for OCR
    # training.
    label_clean = "".join(c for c in chars if c.isdigit() or c == ".")
    spans: list[tuple[int, int]] = []
    used_tesseract = False
    tess_attempts: list[str] = []
    try:
        # Build a list of (image, tag) variants. Cheap to enumerate
        # — we stop at the first one whose Tesseract read matches
        # the label.
        variants: list[tuple["Image.Image", str]] = []
        try:
            base = Image.fromarray(gray, mode="L")
            variants.append((base, "1x"))
            # Upscaled 2x — tiny digits read more reliably
            variants.append((
                base.resize(
                    (base.width * 2, base.height * 2), Image.LANCZOS,
                ),
                "2x",
            ))
            # Upscaled 3x — last-ditch for very small captures
            variants.append((
                base.resize(
                    (base.width * 3, base.height * 3), Image.LANCZOS,
                ),
                "3x",
            ))
            # Inverted polarity (Tesseract expects black-on-white)
            inv = Image.fromarray(255 - gray, mode="L")
            variants.append((inv, "1x_inv"))
            variants.append((
                inv.resize(
                    (inv.width * 2, inv.height * 2), Image.LANCZOS,
                ),
                "2x_inv",
            ))
        except Exception:
            variants = []

        for psm in ("7", "13", "8", "6"):
            for img_v, tag in variants:
                if used_tesseract:
                    break
                try:
                    tess_boxes = _tesseract_char_boxes(
                        img_v, whitelist="0123456789.", psm=psm,
                    )
                except Exception:
                    continue
                if not tess_boxes:
                    tess_attempts.append(f"{tag}/psm{psm}=empty")
                    continue
                tess_clean = "".join(
                    b[0] for b in tess_boxes
                    if b[0].isdigit() or b[0] == "."
                )
                tess_attempts.append(f"{tag}/psm{psm}={tess_clean!r}")
                # Normalize: strip dots from BOTH sides for the
                # comparison. Tesseract often emits a phantom '.'
                # where the SC font puts a thousands-comma (the user
                # typed 21,350; Tesseract reads 21.350; the digit
                # sequence underneath is identical and that's all we
                # need for per-digit segmentation). The dot also gets
                # filtered out of the spans list before saving since
                # signal training is digits-only.
                tess_digits_only = tess_clean.replace(".", "")
                label_digits_only = label_clean.replace(".", "")
                if tess_digits_only == label_digits_only and len(tess_boxes) >= len(chars):
                    # Tesseract VERIFIES that the masked image has
                    # exactly len(chars) digits in the typed sequence
                    # — that's the only thing it's reliable for on
                    # SC's tightly-kerned font.
                    #
                    # Tesseract's box COORDINATES are not trustworthy:
                    # observed bbox for the leading "2" of "21,350"
                    # spanned x=163..220, with the "1"'s bbox
                    # x=211..224 — saving each digit by its own bbox
                    # captures pixels from the neighbouring digit
                    # (the "1" tile ends up containing "21"). Even
                    # midpoint-of-centers fails because the centers
                    # themselves are biased toward whichever side of
                    # the kerning pair has more ink.
                    #
                    # Use _segment_digits on the MASKED image (which
                    # now has the icon erased) to find actual digit
                    # boundaries via column projection. Pair those
                    # boundaries with the user's typed digits.
                    spans = _segment_digits(gray, expected_count=len(chars))
                    if len(spans) != len(chars):
                        # Column projection couldn't find the right
                        # number of distinct digits even after the
                        # icon mask. Skip this capture rather than
                        # save aligned-wrong garbage.
                        _debug(
                            f"  region2: {img_png.name} tesseract OK "
                            f"but column-proj found {len(spans)} spans "
                            f"vs {len(chars)} chars — skipping"
                        )
                        used_tesseract = False
                        continue
                    used_tesseract = True
                    _debug(
                        f"  region2: {img_png.name} tesseract OK via "
                        f"{tag}/psm{psm} -> {label_clean!r}"
                    )
            if used_tesseract:
                break
    except Exception as exc:
        _debug(f"  region2: {img_png.name} tesseract pass failed: {exc}")

    if not used_tesseract:
        _debug(
            f"  region2: {img_png.name} value={value!r} "
            f"tesseract DISAGREEMENT — skipping (no column-proj fallback). "
            f"Attempts: {tess_attempts[:6]}"
        )
        return counts

    # Filter: drop spans that are clearly not a digit. Two cheap
    # checks per span — no model required, runs in microseconds.
    #
    # NOTE on the blacklist: we deliberately do NOT run
    # ``_is_blacklisted`` here for region2. The location-pin icon is
    # already eliminated by the ``left_mask_pct`` pre-pass above, and
    # the SC font's digit '9' (circle on a stick) hashes within the
    # 0.88 pHash similarity threshold of the pin icon — running the
    # blacklist in the unmasked region drops every '9' too. Blacklist
    # support is preserved for the HUD region (which doesn't have
    # this conflict) and for future use cases.
    MAX_SPAN_FRACTION = 0.45  # a single digit is < ~45% of the crop
    MIN_SPAN_WIDTH = 3
    filtered: list[tuple[int, int]] = []
    for (x1, x2) in spans:
        w = x2 - x1
        if w < MIN_SPAN_WIDTH:
            _debug(f"    drop span (too narrow w={w}) from {img_png.name}")
            continue
        if w > img_w * MAX_SPAN_FRACTION:
            _debug(
                f"    drop span (too wide w={w}/{img_w}) from {img_png.name}"
                f" — likely whole-signature blob"
            )
            continue
        filtered.append((x1, x2))

    if len(filtered) != len(chars):
        _debug(
            f"  region2: {img_png.name} value={value!r} "
            f"spans_raw={len(spans)} spans_filtered={len(filtered)} "
            f"chars={len(chars)} — count mismatch, skipping"
        )
        return counts

    # ── Median-width consistency check ──
    # When two adjacent digits merge into a single ink-blob (no
    # zero-ink column between them), the merged span is roughly 2×
    # the width of a clean digit. The OTHER spans in the capture
    # are individual digits at normal width, so the median is a
    # good baseline. Anything > 1.7× median is almost certainly a
    # 2-or-more-digit blob and the position-pair-up will assign it
    # to the wrong character class.
    #
    # Reject the whole capture in that case — saving a "19" tile
    # under the label "1" silently corrupts the "1" class, AND the
    # capture's other spans get shifted and corrupt their classes
    # too. Better to lose one capture than poison multiple classes.
    if len(filtered) >= 3:
        widths = sorted(x2 - x1 for x1, x2 in filtered)
        median = widths[len(widths) // 2]
        if median > 0:
            # 1.7× median = caught by experiment as the cleanest
            # threshold. 2.0× misses real merges (a "19" was 1.92×).
            # Tightly-kerned but still-distinct digits stay below
            # this. Skipping the whole capture is correct because
            # any merged-digit span corrupts a class label.
            outliers = [
                (x1, x2) for (x1, x2) in filtered
                if (x2 - x1) >= int(median * 1.7)
            ]
            if outliers:
                _debug(
                    f"  region2: {img_png.name} value={value!r} "
                    f"width-outlier spans {outliers} (median={median}) — "
                    f"likely merged digits, skipping whole capture"
                )
                return counts

    src_name = img_png.stem

    # PRE-CHECK: render the FIRST glyph and see if it matches the
    # blacklist. If it does, the icon survived our left-mask attempt
    # AND took the slot of the leading character — meaning every
    # subsequent (span, char) pair is shifted by one and would save
    # the wrong digit content under the wrong label. Reject the
    # whole capture rather than poison N classes simultaneously.
    if filtered:
        x1_first, x2_first = filtered[0]
        first_glyph = _glyph_to_28x28(gray, x1_first, x2_first)
        if first_glyph is not None and _is_blacklisted(first_glyph):
            _debug(
                f"  region2: {img_png.name} leading glyph matched "
                f"blacklist (icon survived mask) — rejecting whole "
                f"capture to prevent N-class poisoning"
            )
            return counts

    for (x1, x2), ch in zip(filtered, chars):
        glyph = _glyph_to_28x28(gray, x1, x2)
        if glyph is None:
            continue
        if _save_glyph(glyph, ch, src_name, out_root):
            counts[ch] = counts.get(ch, 0) + 1
    return counts


def extract_one(img_path: Path, label: dict) -> dict[str, int]:
    """Dispatch on region."""
    region = img_path.parent.name
    if region == "region1":
        return extract_region1_glyphs(img_path, label, PANEL_GLYPH_ROOT)
    elif region == "region2":
        return extract_region2_glyphs(img_path, label, SIG_GLYPH_ROOT)
    return {}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true",
                   help="Process every labeled user_* capture under training_data_panels")
    p.add_argument("--path", help="Process just this single image (needs sidecar JSON)")
    args = p.parse_args()

    total_saved = 0
    total_panels = 0

    targets: list[Path] = []
    if args.path:
        targets = [Path(args.path)]
    elif args.all:
        for root in PANELS_ROOT.iterdir():
            if not root.is_dir() or not root.name.startswith("user_"):
                continue
            for region in ("region1", "region2"):
                d = root / region
                if d.is_dir():
                    targets.extend(sorted(d.glob("cap_*.png")))

    for img_path in targets:
        json_path = img_path.with_suffix(".json")
        if not json_path.is_file():
            continue
        try:
            label = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        counts = extract_one(img_path, label)
        if counts:
            total_panels += 1
            n = sum(counts.values())
            total_saved += n
            print(f"  {img_path.parent.name}/{img_path.name}: +{n} glyphs {counts}")

    print(f"\nTotal: {total_saved} glyphs from {total_panels} labeled panels")


if __name__ == "__main__":
    main()
