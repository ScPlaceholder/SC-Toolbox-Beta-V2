"""Detect + quarantine glyph training samples that visually contain
MORE than one digit (e.g. an "8" crop that shows "08").

Background: ``augment_from_source.py`` was generating training crops
by jittering Tesseract bbox positions ±4 px without checking that
the shifts didn't cross into adjacent digits. With SC HUD's tight
4-6 px inter-digit kerning, those shifts pulled neighbor digit
pixels into the crop. Result: thousands of training samples labeled
e.g. ``8`` whose actual visual content is ``08``, ``13`` for ``3``,
etc. The classifier learned the wrong concept.

This script scans the per-class folders under
``training_data_user_sig/`` (and optionally
``training_data_user_panel/``), detects multi-digit content via
column-cluster counting, and MOVES contaminated samples to a
``_quarantine/`` subfolder so they're preserved for inspection
but excluded from future training.

Detection heuristic:
  1. Resize each crop to a canonical 28×28 (already the standard
     trainer input size).
  2. Adaptive-binarize via the same routine the OCR pipeline uses
     so we see what the classifier sees.
  3. Project bright pixels onto the x-axis.
  4. Count distinct vertical column clusters separated by ≥ 2 px
     of zero-projection gap.
  5. If ≥ 2 distinct column clusters of width ≥ 3 px → multi-digit
     → quarantine.

SAFETY:
  * NEVER touches ``user_*.png`` (your hand-curated samples).
  * NEVER touches ``aug_*.png`` (the synthetic-augment outputs).
  * Only ``src_*.png`` (the buggy bbox-jitter augmentation output)
    is examined / quarantined.
  * Files are MOVED, never deleted. To restore, drag back from
    ``_quarantine/``.

Run:
    python scripts/quarantine_contaminated_glyphs.py
    python scripts/quarantine_contaminated_glyphs.py --dry-run
    python scripts/quarantine_contaminated_glyphs.py --kind both
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent

KIND_DIRS = {
    "signal": TOOL / "training_data_user_sig",
    "hud":    TOOL / "training_data_user_panel",
}

# Only touch files that match this prefix. ``user_*.png`` are the
# user's hand-labels — never touched. ``aug_*.png`` are synthetic
# variants from a different augmenter — also untouched (different
# script, different bug surface).
TARGET_PREFIX = "src_"


def _binarize_for_detection(gray: np.ndarray) -> np.ndarray:
    """Polarity-aware binarization tuned for 28×28 training glyphs.

    The src_*.png files are stored white-BG / dark-text (the
    ``_glyph_to_28x28`` helper pads crops with white). For 28×28
    crops, adaptive thresholding degenerates because the Gaussian
    window is the size of the whole image — so we just canonicalize
    polarity (text→BRIGHT) and use a fixed midpoint threshold.
    """
    if gray.size == 0:
        return np.zeros_like(gray, dtype=np.uint8)
    g = gray.copy()
    # Canonicalize: if median is bright, BG is bright → invert to make
    # text BRIGHT (matches the rest of the OCR conventions).
    if float(np.median(g)) > 128:
        g = 255 - g
    # Otsu-style: find the threshold via the histogram's between-class
    # variance peak (handles all polarities cleanly).
    hist, _ = np.histogram(g.flatten(), bins=256, range=(0, 256))
    total = g.size
    sum_total = float(np.sum(np.arange(256) * hist))
    sum_bg, w_bg = 0.0, 0
    max_var, thr = 0.0, 127
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
            thr = t
    return (g > thr).astype(np.uint8)


def _count_x_separated_blobs(
    binary: np.ndarray,
    min_comp_size: int = 12,
    min_x_gap: int = 1,
) -> int:
    """Count distinct HORIZONTALLY-SEPARATED blob groups in the binary
    mask. Robust to font-quirks where one digit has multiple strokes.

    Algorithm:
      1. Connected-components label the binary mask.
      2. Drop tiny components (< min_comp_size px) — anti-aliasing
         halos and salt-pepper noise.
      3. Compute each surviving component's (x_min, x_max).
      4. Sort components by x_min, then merge any pair whose x-ranges
         overlap (or are within min_x_gap px of each other) into a
         single horizontal blob group.
      5. Return the number of resulting groups.

    Single digit → 1 group regardless of stroke count:
      * ``0``: 1 closed-loop component → 1 group
      * ``8``: 1 figure-8 component → 1 group
      * ``9``: top-loop + tail at SAME x-range (stacked) → 1 group
      * ``4``: vertical stem + horizontal bar at SAME x-range → 1 group
      * ``i``: stem + dot at same x → 1 group

    Multi-digit contamination → 2+ groups:
      * ``08``: ``0`` blob at x≈4-12, ``8`` blob at x≈14-22 → 2 groups
      * ``20``: same pattern → 2 groups
    """
    from scipy.ndimage import label
    labels, n = label(binary)
    if n == 0:
        return 0
    # Per-component bounding box.
    blobs: list[tuple[int, int]] = []  # (x_min, x_max) for surviving comps
    for i in range(1, n + 1):
        ys, xs = np.where(labels == i)
        if xs.size < min_comp_size:
            continue
        blobs.append((int(xs.min()), int(xs.max())))
    if not blobs:
        return 0
    # Merge x-overlapping (or near-touching) components into groups.
    blobs.sort(key=lambda b: b[0])
    groups = 1
    cur_x_max = blobs[0][1]
    for x_min, x_max in blobs[1:]:
        if x_min - cur_x_max <= min_x_gap:
            # Same horizontal blob group — extend the right edge.
            cur_x_max = max(cur_x_max, x_max)
        else:
            # Gap large enough → new group.
            groups += 1
            cur_x_max = x_max
    return groups


def _is_contaminated(path: Path) -> bool:
    """True if the crop shows ≥ 2 distinct digit-shaped column
    clusters (multi-digit contamination)."""
    try:
        img = Image.open(path).convert("L")
    except Exception:
        return False
    arr = np.asarray(img, dtype=np.uint8)
    if arr.shape != (28, 28):
        # Resize to canonical size for consistent detection.
        img = img.resize((28, 28), Image.LANCZOS)
        arr = np.asarray(img, dtype=np.uint8)
    binary = _binarize_for_detection(arr)
    if _count_x_separated_blobs(binary) >= 2:
        return True
    # Backstop: even one connected blob is suspicious if its bounding
    # box spans most of the crop width — the SC HUD font's widest
    # single digit is ~13-15 px in a 28×28 crop (≤55% width). Anything
    # spanning >70% of width is almost certainly two digits whose
    # outlines fused together at the binarization step (e.g. "20"
    # where the "2"'s right stem touches the "0"'s left edge).
    if binary.any():
        cols = binary.sum(axis=0) > 0
        xs = np.where(cols)[0]
        if xs.size > 0:
            span = int(xs[-1] - xs[0] + 1)
            if span > int(binary.shape[1] * 0.70):
                return True
    return False


def quarantine_kind(kind: str, dry_run: bool) -> dict[str, int]:
    """Scan one training folder, quarantine contaminated src_*.png.
    Returns per-class quarantine counts."""
    root = KIND_DIRS[kind]
    if not root.is_dir():
        print(f"[skip] {kind}: {root} doesn't exist")
        return {}
    print(f"\n=== {kind}: scanning {root} ===")
    counts: dict[str, int] = {}
    for cls_dir in sorted(root.iterdir()):
        if not cls_dir.is_dir() or cls_dir.name.startswith("_"):
            continue
        cls = cls_dir.name
        candidates = sorted(cls_dir.glob(f"{TARGET_PREFIX}*.png"))
        if not candidates:
            continue
        contaminated: list[Path] = []
        for p in candidates:
            if _is_contaminated(p):
                contaminated.append(p)
        counts[cls] = len(contaminated)
        if contaminated:
            print(
                f"  class {cls!r:>5}: {len(contaminated):5d} of "
                f"{len(candidates):5d} src_*.png files contaminated "
                f"({100.0 * len(contaminated) / len(candidates):.1f}%)"
            )
            if not dry_run:
                quarantine_dir = cls_dir / "_quarantine"
                quarantine_dir.mkdir(exist_ok=True)
                for p in contaminated:
                    target = quarantine_dir / p.name
                    # If a file with the same name already exists in
                    # quarantine (re-running), append a counter.
                    counter = 1
                    while target.exists():
                        target = quarantine_dir / (
                            f"{p.stem}__dup{counter}{p.suffix}"
                        )
                        counter += 1
                    shutil.move(str(p), str(target))
        else:
            print(
                f"  class {cls!r:>5}: 0 of {len(candidates)} clean (good)"
            )
    return counts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--kind", choices=["signal", "hud", "both"], default="signal",
        help="Which training folder to scan. Default: signal "
             "(only place augment_from_source has written so far).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report contamination counts without moving any files.",
    )
    args = p.parse_args()

    print(f"=== Contaminated-glyph quarantine ===")
    print(f"    kind:    {args.kind}")
    print(f"    mode:    {'DRY-RUN (no files moved)' if args.dry_run else 'live (files will be moved to _quarantine/)'}")
    print(f"    target:  {TARGET_PREFIX}*.png  (user_*.png and aug_*.png are NOT touched)")

    kinds = ["signal", "hud"] if args.kind == "both" else [args.kind]
    grand_total = 0
    for kind in kinds:
        counts = quarantine_kind(kind, args.dry_run)
        kind_total = sum(counts.values())
        print(f"  -> {kind} total: {kind_total} contaminated")
        grand_total += kind_total

    print()
    print(f"=== Grand total: {grand_total} files {'identified' if args.dry_run else 'moved to _quarantine/'} ===")
    if not args.dry_run and grand_total > 0:
        print()
        print("Next: re-augment with the FIXED script "
              "(neighbor-aware shifts, no more cross-boundary contamination):")
        print("  python scripts/augment_from_source.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
