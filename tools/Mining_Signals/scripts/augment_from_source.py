"""Generate training crops from REAL source captures at varying
positions, instead of synthesizing fake noise.

Insight: we have ~161 labeled signal-panel captures. Each capture
shows known digits at known positions in real pixel data. The
inference-time failure mode is "segmenter cropped 1-3 px off". By
extracting each digit at MANY slightly-different crop positions
around its true bbox, we teach the model to tolerate exactly the
kind of position noise it sees at inference — using REAL pixels,
not synthesized transforms.

For each labeled capture:

  1. Apply the inference-time icon mask + row isolate.
  2. Use Tesseract to find per-character bounding boxes (same
     multi-PSM/scale routine as the offline extractor).
  3. Verify Tesseract's read matches the user's typed label
     (gives us ground-truth bbox positions).
  4. For each digit position, generate N variants by shifting the
     bbox by ±dx in x and ±dx_w in width (+small y shifts).
  5. Render each variant to 28×28 and save under the matching
     class folder with prefix ``src_<src>_<chari>_<vi>.png`` so
     they're identifiable separately from human-curated and
     synthetic-augmented files.

The result: every clean digit position in every successfully-read
capture becomes ~30 training examples, all from REAL inference-
shaped crops, all sharing the same ground-truth label.

Run:
    python scripts/augment_from_source.py
    python scripts/augment_from_source.py --variants 30 --clear
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
sys.path.insert(0, str(TOOL))
sys.path.insert(0, str(TOOL / "scripts"))

from ocr import training_registry  # noqa: E402
import extract_labeled_glyphs as _xlg  # noqa: E402

KIND = "signal"
SRC_PREFIX = "src_"  # variants generated from source captures


def _wipe_source_variants(staging_dir: Path) -> int:
    n = 0
    for cls in "0123456789":
        d = staging_dir / cls
        if not d.is_dir():
            continue
        for f in d.glob(f"{SRC_PREFIX}*.png"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def _tesseract_anchor_boxes(
    gray: np.ndarray, expected_label: str,
) -> tuple[list[tuple[int, int]], int] | tuple[None, int]:
    """Return ([(x1, x2), …], scale) where the spans are in
    ORIGINAL gray coords and Tesseract's read of the cleaned image
    matches the typed label. (None, 1) on no agreement."""
    base = Image.fromarray(gray, mode="L")
    variants = [
        (base, "1x", 1),
        (
            base.resize((base.width * 2, base.height * 2), Image.LANCZOS),
            "2x", 2,
        ),
        (
            base.resize((base.width * 3, base.height * 3), Image.LANCZOS),
            "3x", 3,
        ),
    ]
    for psm in ("7", "13", "8"):
        for img_v, _tag, scale in variants:
            try:
                boxes = _xlg._tesseract_char_boxes(
                    img_v, whitelist="0123456789.", psm=psm,
                )
            except Exception:
                continue
            if not boxes:
                continue
            digits = "".join(b[0] for b in boxes if b[0].isdigit())
            if digits != expected_label:
                continue
            digit_boxes = [b for b in boxes if b[0].isdigit()]
            spans = [(b[1] // scale, b[3] // scale) for b in digit_boxes]
            # Resolve overlapping boxes via midpoint (same as
            # extractor + scan_region paths).
            for i in range(len(spans)):
                if i + 1 < len(spans):
                    cx1, cx2 = spans[i]
                    nx1, nx2 = spans[i + 1]
                    if nx1 < cx2:
                        cur_c = (cx1 + cx2) / 2.0
                        nxt_c = (nx1 + nx2) / 2.0
                        if nxt_c > cur_c:
                            boundary = int((cur_c + nxt_c) / 2.0)
                            spans[i] = (cx1, boundary)
                            spans[i + 1] = (boundary, nx2)
            return spans, scale
    return None, 1


def _generate_variants(
    gray: np.ndarray,
    x1: int,
    x2: int,
    n_variants: int,
    prev_x2: int | None = None,
    next_x1: int | None = None,
    safety_px: int = 2,
) -> list[np.ndarray]:
    """For one anchor bbox (x1, x2), produce ~n_variants 28×28 crops
    by shifting and resizing the bbox in small steps. Each variant
    crops REAL pixels from `gray` — no synthetic transforms.

    The shift amounts are drawn from a distribution centered at the
    Tesseract anchor and biased toward small offsets (±1-2 px most
    common, ±3-4 px rarer). This matches the actual segmenter
    error distribution observed at inference.

    NEIGHBOR-AWARE CLAMPING (critical):
      ``prev_x2`` / ``next_x1`` are the right edge of the previous
      digit's bbox and the left edge of the next digit's bbox in the
      same row, or ``None`` at the row's outer ends. Without this
      clamp, the original ±4 px shift would happily push the new
      ``nx1`` *into* the previous digit's pixels (and similarly on
      the right) — producing training samples labeled "8" that
      visually show "08", "32" labeled as "3", etc. The SC HUD font
      has tight inter-digit kerning (~4-6 px), so even the clamped
      ±4 px is enough to cross the boundary.

      With this clamp the shift is hard-stopped at the neighbor's
      far edge minus a small safety margin (default 2 px), so
      every variant stays cleanly within the target digit's pixel
      territory.
    """
    out: list[np.ndarray] = []
    rng = np.random.default_rng()
    H, W = gray.shape

    # Hard limits on the new bbox edges. The shift cannot push the
    # left edge before (prev digit right + safety) and cannot push
    # the right edge past (next digit left - safety).
    left_limit = (prev_x2 + safety_px) if prev_x2 is not None else 0
    right_limit = (next_x1 - safety_px) if next_x1 is not None else W
    # Defensive: if the anchor itself sits past the limit (data
    # weirdness, overlapping bboxes), fall back to no clamp on that
    # side rather than producing zero variants.
    if left_limit > x1:
        left_limit = 0
    if right_limit < x2:
        right_limit = W

    seen_keys: set[tuple[int, int]] = set()
    # Always include the anchor itself
    base = _xlg._glyph_to_28x28(gray, x1, x2)
    if base is not None:
        out.append(base)
        seen_keys.add((x1, x2))
    # Now generate N variants. Use a Gaussian distribution for offsets
    # so most variants are very close to the anchor (matches what the
    # segmenter actually does — usually within 1-2 px).
    attempts = 0
    while len(out) < n_variants + 1 and attempts < n_variants * 4:
        attempts += 1
        dx_left = int(round(rng.normal(0, 1.5)))
        dx_right = int(round(rng.normal(0, 1.5)))
        # Clamp to sane shifts (prevents wild Gaussian tails)
        dx_left = max(-4, min(4, dx_left))
        dx_right = max(-4, min(4, dx_right))
        # Apply shift, then clamp to neighbor-aware hard limits.
        nx1 = max(left_limit, x1 + dx_left)
        nx2 = min(right_limit, x2 + dx_right)
        if nx2 - nx1 < 6:
            continue
        key = (nx1, nx2)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        g = _xlg._glyph_to_28x28(gray, nx1, nx2)
        if g is not None:
            out.append(g)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--variants", type=int, default=20,
        help="How many positional variants per anchored digit.",
    )
    p.add_argument(
        "--clear", action="store_true",
        help="Wipe existing src_*.png files before generating new ones.",
    )
    args = p.parse_args()

    spec = training_registry.get(KIND)
    staging = spec.glyph_staging_dir
    staging.mkdir(parents=True, exist_ok=True)

    if args.clear:
        n = _wipe_source_variants(staging)
        print(f"[clear] removed {n} previous src_*.png files")

    sources = training_registry.get_training_sources(KIND)
    if not sources:
        print(f"[!] no registered sources for {KIND!r}")
        return 2

    captures: list[tuple[Path, str]] = []
    for src_dir in sources:
        for j in src_dir.glob("cap_*.json"):
            if j.name.endswith(".boxes.json"):
                continue
            try:
                d = json.loads(j.read_text(encoding="utf-8"))
            except Exception:
                continue
            v = (d.get(spec.label_field) or "").strip().replace(",", "")
            if not v.isdigit():
                continue
            png = j.with_suffix(".png")
            if not png.is_file():
                continue
            captures.append((png, v))

    print(f"=== Source-pixel augmentation ===")
    print(f"    captures eligible: {len(captures)}")
    print(f"    variants per anchor: {args.variants}")
    print()

    per_class = {ch: 0 for ch in "0123456789"}
    n_anchored = n_skipped = 0
    for png, label in captures:
        try:
            img = Image.open(png).convert("L")
            gray = np.asarray(img, dtype=np.uint8)
        except Exception:
            n_skipped += 1
            continue
        # Same pre-processing as inference: icon mask + row isolate.
        bg = int(np.median(gray))
        gray = gray.copy()
        icon_right = _xlg._locate_icon_via_blacklist_match(gray)
        floor_mask = int(gray.shape[1] * 0.30)
        mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
        if 0 < mask_w < gray.shape[1]:
            gray[:, :mask_w] = bg
        gray = _xlg._isolate_main_row(gray)
        if gray.shape[0] < 6 or gray.shape[1] < 12:
            n_skipped += 1
            continue

        spans, _scale = _tesseract_anchor_boxes(gray, label)
        if spans is None or len(spans) != len(label):
            n_skipped += 1
            continue
        n_anchored += 1
        src_name = png.stem
        for char_i, ((x1, x2), ch) in enumerate(zip(spans, label)):
            # Neighbor-aware shift clamp: pass the right edge of the
            # previous span and the left edge of the next span so
            # _generate_variants won't shift the bbox into adjacent
            # digits' territory (the bug that caused "8" crops to
            # show "08", etc.).
            prev_x2 = spans[char_i - 1][1] if char_i > 0 else None
            next_x1 = (
                spans[char_i + 1][0] if char_i + 1 < len(spans) else None
            )
            variants = _generate_variants(
                gray, x1, x2, args.variants,
                prev_x2=prev_x2, next_x1=next_x1,
            )
            cls_dir = staging / ch
            cls_dir.mkdir(parents=True, exist_ok=True)
            for vi, g in enumerate(variants):
                out = cls_dir / f"{SRC_PREFIX}{src_name}_c{char_i}_v{vi}.png"
                try:
                    Image.fromarray(g, mode="L").save(out)
                    per_class[ch] += 1
                except Exception:
                    pass

    print(f"[anchored] {n_anchored} captures (skipped {n_skipped})")
    print()
    print("Per-class new files:")
    total = 0
    for ch in "0123456789":
        print(f"  {ch!r}: +{per_class[ch]}")
        total += per_class[ch]
    print()
    print(f"[done] {total} new src_*.png files")
    print()
    print("Next: re-train on the combined corpus:")
    print("  python scripts/train_for_region.py signal --no-extract --force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
