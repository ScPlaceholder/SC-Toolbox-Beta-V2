"""Aspect-matched 28x20 (height x width) glyph extractor.

Sibling experiment to ``extract_labeled_glyphs.py``. Uses the same
icon-mask + row-isolate + Tesseract-verify pipeline but emits per-digit
glyphs at 28-tall x 20-wide (matching the natural ~0.6 aspect of SC
HUD digits) instead of 28x28 (which forces a horizontal stretch on
narrow digits).

Source: ``training_data_panels/user_*/region2/*.png`` from the
WingmanAI custom-skills dir (the production tree mirror is empty
in this environment; the v3 trainer also reads from Wingman).

Output: ``PROD_TOOL_DIR/training_data_panels/_aspect_28x20/<class>/``
(28-tall x 20-wide RGB PNGs, replicated from grayscale).

Run:
    python scripts/extract_labeled_glyphs_aspect_28x20.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

# Import the production extractor's helpers verbatim. We don't modify
# the production module — we just reuse its battle-tested pipeline
# functions for icon detection, row isolation, and Tesseract char-box
# verification.
THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent  # PROD Mining_Signals dir
sys.path.insert(0, str(TOOL))

from scripts.extract_labeled_glyphs import (  # noqa: E402
    _otsu,
    _save_glyph,
    _locate_icon_via_blacklist_match,
    _isolate_main_row,
    _tesseract_char_boxes,
    _find_main_row_bounds,
    _segment_digits,
    _is_blacklisted,
    _debug,
    BLACKLIST_DIR,
)


# --- Paths ----------------------------------------------------------

PROD_TOOL_DIR = TOOL
WINGMAN_TOOL_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)

# Source captures (Wingman has the data; the production mirror is empty).
SRC_PANELS_ROOT = WINGMAN_TOOL_DIR / "training_data_panels"

# Output: per-digit 28x20 RGB PNGs in the production tree under a
# distinct subdir so we don't clobber any existing dataset.
OUT_ROOT = PROD_TOOL_DIR / "training_data_panels" / "_aspect_28x20"


# --- 28x20 glyph normalization --------------------------------------

OUT_H = 28
OUT_W = 20


def _glyph_to_28x20(gray_crop: np.ndarray, x1: int, x2: int) -> Optional[np.ndarray]:
    """Crop a glyph and normalize to a 28x20 (H x W) uint8 array.

    Mirrors the production ``_glyph_to_28x28`` helper exactly except for
    the final resize target: PIL's ``resize`` takes ``(width, height)``,
    so ``(20, 28)`` produces a 28-row x 20-col output. Aspect-matched
    to the natural ~0.6 wide-to-tall SC HUD digit aspect ratio so we
    don't horizontally distort narrow digits when feeding the CNN.
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
        (OUT_W, OUT_H), Image.BILINEAR,  # PIL: (width, height) -> 28-tall x 20-wide
    )
    return np.asarray(pil, dtype=np.uint8)


def _save_glyph_rgb(glyph_gray: np.ndarray, char: str, src_name: str,
                    out_root: Path) -> bool:
    """Save a 28x20 glyph as RGB (replicate single channel into 3).

    Class mapping: digits as-is, dot/pct skipped (digits-only experiment).
    Skips blacklisted glyphs (uses pHash check on the gray glyph).
    """
    if not (len(char) == 1 and char.isdigit()):
        return False
    # Blacklist filter (matches production behaviour)
    if _is_blacklisted(glyph_gray):
        _debug(f"    BLACKLISTED 28x20 glyph for {char!r} from {src_name}")
        return False
    cls = char
    d = out_root / cls
    d.mkdir(parents=True, exist_ok=True)
    i = 0
    while True:
        out = d / f"user_{src_name}_{i}.png"
        if not out.exists():
            break
        i += 1
    try:
        # Replicate L -> RGB: 3 identical channels.
        rgb = np.stack([glyph_gray, glyph_gray, glyph_gray], axis=-1)
        Image.fromarray(rgb.astype(np.uint8), mode="RGB").save(out)
        return True
    except Exception:
        return False


# --- Region 2 extraction (28x20 variant) ----------------------------

def extract_region2_glyphs_28x20(
    img_png: Path, label: dict, out_root: Path,
    left_mask_pct: float = 0.30,
) -> dict[str, int]:
    """Same pipeline as production ``extract_region2_glyphs`` but emits
    28x20 RGB instead of 28x28 grayscale.

    The pipeline body is copied verbatim except where it calls
    ``_glyph_to_28x28`` / ``_save_glyph`` — those become the 28x20 RGB
    variants. We don't modify the production module; this is a sibling
    extractor for an experiment.
    """
    counts: dict[str, int] = {}
    value = str(label.get("value", "")).strip().replace(",", "")
    if not value:
        return counts
    chars = [c for c in value if c.isdigit() or c == "."]
    if not chars:
        return counts
    try:
        img = Image.open(img_png).convert("L")
    except Exception:
        return counts
    gray = np.asarray(img, dtype=np.uint8)
    img_w = gray.shape[1]

    # Icon mask via blacklist pHash sliding match (verbatim from prod)
    bg = int(np.median(gray))
    gray = gray.copy()
    icon_right = _locate_icon_via_blacklist_match(gray)
    floor_mask = int(img_w * left_mask_pct) if left_mask_pct > 0 else 0
    mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
    if 0 < mask_w < img_w:
        gray[:, :mask_w] = bg

    # Row-band isolation
    gray = _isolate_main_row(gray)

    # Tesseract char-box pass — verify the masked image's digit
    # sequence matches the user's typed label, and use the masked
    # image for column-projection segmentation.
    label_clean = "".join(c for c in chars if c.isdigit() or c == ".")
    spans: list[tuple[int, int]] = []
    used_tesseract = False
    try:
        variants: list[tuple["Image.Image", str]] = []
        try:
            base = Image.fromarray(gray, mode="L")
            variants.append((base, "1x"))
            variants.append((
                base.resize(
                    (base.width * 2, base.height * 2), Image.LANCZOS,
                ),
                "2x",
            ))
            variants.append((
                base.resize(
                    (base.width * 3, base.height * 3), Image.LANCZOS,
                ),
                "3x",
            ))
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
                    continue
                tess_clean = "".join(
                    b[0] for b in tess_boxes
                    if b[0].isdigit() or b[0] == "."
                )
                tess_digits_only = tess_clean.replace(".", "")
                label_digits_only = label_clean.replace(".", "")
                if tess_digits_only == label_digits_only and len(tess_boxes) >= len(chars):
                    spans = _segment_digits(gray, expected_count=len(chars))
                    if len(spans) != len(chars):
                        used_tesseract = False
                        continue
                    used_tesseract = True
            if used_tesseract:
                break
    except Exception:
        pass

    if not used_tesseract:
        return counts

    # Span filter: too narrow / too wide -> drop
    MAX_SPAN_FRACTION = 0.45
    MIN_SPAN_WIDTH = 3
    filtered: list[tuple[int, int]] = []
    for (x1, x2) in spans:
        w = x2 - x1
        if w < MIN_SPAN_WIDTH:
            continue
        if w > img_w * MAX_SPAN_FRACTION:
            continue
        filtered.append((x1, x2))

    if len(filtered) != len(chars):
        return counts

    # Median-width consistency check (reject merged-digit captures)
    if len(filtered) >= 3:
        widths = sorted(x2 - x1 for x1, x2 in filtered)
        median = widths[len(widths) // 2]
        if median > 0:
            outliers = [
                (x1, x2) for (x1, x2) in filtered
                if (x2 - x1) >= int(median * 1.7)
            ]
            if outliers:
                return counts

    src_name = img_png.stem

    # Pre-check: leading glyph blacklist match -> reject whole capture
    if filtered:
        x1_first, x2_first = filtered[0]
        first_glyph_28x28 = _glyph_to_28x20(gray, x1_first, x2_first)
        if first_glyph_28x28 is not None and _is_blacklisted(first_glyph_28x28):
            return counts

    for (x1, x2), ch in zip(filtered, chars):
        glyph = _glyph_to_28x20(gray, x1, x2)
        if glyph is None:
            continue
        if _save_glyph_rgb(glyph, ch, src_name, out_root):
            counts[ch] = counts.get(ch, 0) + 1
    return counts


# --- Driver ---------------------------------------------------------

def main() -> int:
    if not SRC_PANELS_ROOT.is_dir():
        print(f"[!] source panels dir not found: {SRC_PANELS_ROOT}")
        return 1

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    targets: list[Path] = []
    for root in sorted(SRC_PANELS_ROOT.iterdir()):
        if not root.is_dir() or not root.name.startswith("user_"):
            continue
        d = root / "region2"
        if d.is_dir():
            targets.extend(sorted(d.glob("cap_*.png")))

    print(f"[scan] found {len(targets)} region2 captures")
    total_saved = 0
    total_panels = 0
    per_class: dict[str, int] = {str(d): 0 for d in range(10)}
    for img_path in targets:
        json_path = img_path.with_suffix(".json")
        if not json_path.is_file():
            continue
        try:
            label = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if label.get("schema") != "region2":
            continue
        counts = extract_region2_glyphs_28x20(img_path, label, OUT_ROOT)
        if counts:
            total_panels += 1
            n = sum(counts.values())
            total_saved += n
            for k, v in counts.items():
                per_class[k] = per_class.get(k, 0) + v

    print(f"[done] {total_saved} glyphs saved from {total_panels} panels")
    print("[per-class] (28x20 RGB)")
    for d in sorted(per_class.keys()):
        print(f"  {d}: {per_class[d]}")
    print(f"[output] {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
