"""Extract per-digit 40x28 (height x width) training glyphs from labeled
SC mining-signature captures (region2).

Sibling experiment to ``extract_labeled_glyphs.py``. The production
extractor resizes each tight-cropped glyph to 28x28 (square), which
distorts SC HUD digits whose natural aspect is ~0.6 wide-to-tall. This
script preserves the natural aspect by resizing into a 40-tall x
28-wide canvas instead.

Output layout::

  training_data_panels/_aspect_40x28/<digit>/user_<src>_<i>.png  (grayscale L)

Same icon-mask + row-isolate + Tesseract-verify pipeline as the
production extractor's ``extract_region2_glyphs`` — only the final
resize target changes (PIL resize((28, 40)) instead of ((28, 28))).

Run::

    python scripts/extract_labeled_glyphs_aspect.py --all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
sys.path.insert(0, str(TOOL))

# Reuse helpers from the production extractor without modifying it.
from scripts.extract_labeled_glyphs import (  # type: ignore
    _otsu,
    _is_blacklisted,
    _locate_icon_via_blacklist_match,
    _isolate_main_row,
    _tesseract_char_boxes,
    _segment_digits,
    _debug,
)

# The brief points at training_data_panels/user_*/region2 in the prod
# tree. Empirically those captures live in the WingmanAI tree because
# that's where the labeler writes them. We honor both, but the WingmanAI
# tree is the canonical source.
WINGMAN_TOOL = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)
PANELS_ROOT = WINGMAN_TOOL / "training_data_panels"
if not PANELS_ROOT.is_dir():
    PANELS_ROOT = TOOL / "training_data_panels"

OUT_RGB_ROOT = TOOL / "training_data_panels" / "_aspect_40x28"

OUT_HEIGHT = 40
OUT_WIDTH = 28


def _glyph_to_40x28(
    gray_crop: np.ndarray, x1: int, x2: int,
) -> Optional[np.ndarray]:
    """Crop a glyph and normalize to a 40x28 (HxW) uint8 array.

    Same logic as the production ``_glyph_to_28x28`` (tight-y-crop +
    2px pad + BILINEAR resize) but with the destination canvas at
    40 tall x 28 wide instead of square. PIL resize takes ``(width,
    height)`` so this passes ``(28, 40)``.
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
        (OUT_WIDTH, OUT_HEIGHT), Image.BILINEAR,
    )
    return np.asarray(pil, dtype=np.uint8)


def _save_glyph(
    glyph: np.ndarray, char: str, src_name: str, out_root: Path,
) -> bool:
    """Save glyph under out_root/<class>/user_<src>_<i>.png.

    Skips dot/pct/comma classes (digits-only experiment) and skips
    glyphs that match the production blacklist.
    """
    if not (len(char) == 1 and char.isdigit()):
        return False
    # Blacklist runs on a rough 28x28 view of the glyph (pHash
    # downsamples to 8x8 anyway so any near-square stand-in works).
    pil_for_hash = Image.fromarray(glyph, mode="L").resize(
        (28, 28), Image.BILINEAR,
    )
    if _is_blacklisted(np.asarray(pil_for_hash, dtype=np.uint8)):
        return False
    d = out_root / char
    d.mkdir(parents=True, exist_ok=True)
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


def extract_region2_glyphs_40x28(
    img_png: Path, label: dict, out_root: Path,
    left_mask_pct: float = 0.30,
) -> dict:
    """Mirror of production ``extract_region2_glyphs`` but emits 40x28
    glyphs instead of 28x28. Identical icon-mask + row-isolate +
    Tesseract-verify + width-outlier-reject path.
    """
    counts: dict = {}
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

    bg = int(np.median(gray))
    gray = gray.copy()
    icon_right = _locate_icon_via_blacklist_match(gray)
    floor_mask = int(img_w * left_mask_pct) if left_mask_pct > 0 else 0
    mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
    if 0 < mask_w < img_w:
        gray[:, :mask_w] = bg

    gray = _isolate_main_row(gray)

    label_clean = "".join(c for c in chars if c.isdigit() or c == ".")
    spans = []
    used_tesseract = False
    try:
        variants = []
        try:
            base = Image.fromarray(gray, mode="L")
            variants.append((base, "1x"))
            variants.append((base.resize(
                (base.width * 2, base.height * 2), Image.LANCZOS,
            ), "2x"))
            variants.append((base.resize(
                (base.width * 3, base.height * 3), Image.LANCZOS,
            ), "3x"))
            inv = Image.fromarray(255 - gray, mode="L")
            variants.append((inv, "1x_inv"))
            variants.append((inv.resize(
                (inv.width * 2, inv.height * 2), Image.LANCZOS,
            ), "2x_inv"))
        except Exception:
            variants = []

        for psm in ("7", "13", "8", "6"):
            for img_v, _tag in variants:
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
                if (tess_digits_only == label_digits_only
                        and len(tess_boxes) >= len(chars)):
                    spans = _segment_digits(gray, expected_count=len(chars))
                    if len(spans) != len(chars):
                        used_tesseract = False
                        continue
                    used_tesseract = True
            if used_tesseract:
                break
    except Exception:
        return counts

    if not used_tesseract:
        # Fallback: trust the user's typed label and use column-projection
        # alone. The production extractor doesn't do this (quality > quantity
        # for OCR training), but for this experiment we need enough samples
        # of every class. Column-projection on the masked + row-isolated
        # gray is reliable when (a) icon mask cleared the leading icon and
        # (b) the segmenter finds at least len(chars) spans (we keep the
        # rightmost N — the value is right-justified within the pill).
        spans = _segment_digits(gray, expected_count=len(chars))
        if len(spans) > len(chars):
            spans = spans[-len(chars):]
        if len(spans) != len(chars):
            return counts

    MAX_SPAN_FRACTION = 0.45
    MIN_SPAN_WIDTH = 3
    filtered = []
    for (x1, x2) in spans:
        w = x2 - x1
        if w < MIN_SPAN_WIDTH:
            continue
        if w > img_w * MAX_SPAN_FRACTION:
            continue
        filtered.append((x1, x2))

    if len(filtered) != len(chars):
        return counts

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

    if filtered:
        x1_first, x2_first = filtered[0]
        first_glyph = _glyph_to_40x28(gray, x1_first, x2_first)
        if first_glyph is not None:
            # Reuse existing blacklist via a 28x28 hash.
            pil_h = Image.fromarray(first_glyph, mode="L").resize(
                (28, 28), Image.BILINEAR,
            )
            if _is_blacklisted(np.asarray(pil_h, dtype=np.uint8)):
                return counts

    for (x1, x2), ch in zip(filtered, chars):
        glyph = _glyph_to_40x28(gray, x1, x2)
        if glyph is None:
            continue
        if not (ch.isdigit()):
            continue  # skip dot/comma
        if _save_glyph(glyph, ch, src_name, out_root):
            counts[ch] = counts.get(ch, 0) + 1
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true",
                   help="Process every region2 capture under "
                        "training_data_panels/user_*")
    p.add_argument("--path", help="Process just this single image")
    args = p.parse_args()

    targets = []
    if args.path:
        targets = [Path(args.path)]
    elif args.all:
        for root in PANELS_ROOT.iterdir():
            if not root.is_dir() or not root.name.startswith("user_"):
                continue
            d = root / "region2"
            if d.is_dir():
                targets.extend(sorted(d.glob("cap_*.png")))

    print(f"[aspect-extract] panels root: {PANELS_ROOT}")
    print(f"[aspect-extract] out root:    {OUT_RGB_ROOT}")
    print(f"[aspect-extract] targets:     {len(targets)} captures")

    OUT_RGB_ROOT.mkdir(parents=True, exist_ok=True)

    total_saved = 0
    captures_used = 0
    per_class = {str(d): 0 for d in range(10)}

    for img_path in targets:
        json_path = img_path.with_suffix(".json")
        if not json_path.is_file():
            continue
        try:
            label = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        counts = extract_region2_glyphs_40x28(img_path, label, OUT_RGB_ROOT)
        if counts:
            captures_used += 1
            for ch, n in counts.items():
                if ch in per_class:
                    per_class[ch] += n
                total_saved += n

    print()
    print(f"[aspect-extract] captures_used: {captures_used}/{len(targets)}")
    print(f"[aspect-extract] total saved:   {total_saved}")
    print("[aspect-extract] per-class breakdown:")
    for ch in "0123456789":
        print(f"  {ch}: {per_class[ch]}")


if __name__ == "__main__":
    main()
