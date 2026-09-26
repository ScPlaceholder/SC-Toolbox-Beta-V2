"""Build RGB label templates from annotated panel captures.

Reads .boxes.json annotations to find ground-truth row positions, then
crops the leftmost portion of each row (the label text only, not the
value column) in RGB, normalizes each crop to the canonical template
height (28 px), and averages across panels.

Output: ocr/sc_templates/labels_rgb.npz with keys:
  mass (28, W_mass, 3)        — RGB float32, 0-255
  resistance (28, W_res, 3)
  instability (28, W_inst, 3)
  height (int) = 28
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent

# Use the EXISTING grayscale templates' widths as canonical so the RGB
# templates have the same dimensions and pivot. labels.npz already
# encodes "this is how wide the label text actually is at canonical
# height 28 px".
existing = np.load(ROOT / "ocr" / "sc_templates" / "labels.npz")
CANONICAL_H = 28
LABEL_WIDTHS = {
    "mass": int(existing["mass"].shape[1]),         # 65
    "resistance": int(existing["resistance"].shape[1]),  # 171
    "instability": int(existing["instability"].shape[1]),  # 159
}
print(f"Canonical widths: {LABEL_WIDTHS}")


def _build_one(label: str) -> Optional[np.ndarray]:
    """Average RGB crops of `label` from annotated panels."""
    row_key = f"{label}_row"
    target_w = LABEL_WIDTHS[label]
    accumulator: list[np.ndarray] = []

    paths = sorted(glob.glob(
        str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.boxes.json")
    ))
    print(f"\n=== {label} (target {CANONICAL_H}x{target_w}x3) ===")
    print(f"  scanning {len(paths)} annotation files...")
    for ann_path in paths:
        with open(ann_path) as f:
            ann = json.load(f)
        boxes = ann.get("boxes", {})
        if row_key not in boxes:
            continue
        b = boxes[row_key]
        img_path = ann_path.replace(".boxes.json", ".png")
        if not os.path.exists(img_path):
            continue
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            continue

        # Crop the row. The annotation box spans the whole row including
        # value column; we just want the LABEL text on the left.
        row_x = int(b["x"])
        row_y = int(b["y"])
        row_w = int(b["w"])
        row_h = int(b["h"])
        # Tight-crop to row height.
        row_crop = img.crop((row_x, row_y, row_x + row_w, row_y + row_h))

        # Resize so the row height matches our canonical (28 px). After
        # resize the label text occupies the leftmost portion of the
        # row's width, scaled by the same factor.
        scale = CANONICAL_H / max(1, row_h)
        new_w = max(1, int(round(row_w * scale)))
        if new_w < target_w:
            continue  # row too narrow at canonical — skip
        scaled = row_crop.resize((new_w, CANONICAL_H), Image.BILINEAR)

        # Crop the LEFTMOST `target_w` pixels — that's where the label
        # word lives (anything to the right of that is the value column).
        label_crop = scaled.crop((0, 0, target_w, CANONICAL_H))
        accumulator.append(np.asarray(label_crop, dtype=np.float32))

    if not accumulator:
        print(f"  -> no usable crops for {label}")
        return None
    print(f"  -> averaged {len(accumulator)} crops")
    stacked = np.stack(accumulator, axis=0)  # (N, H, W, 3)
    avg = stacked.mean(axis=0)
    # Save preview for sanity check.
    preview = Path(__file__).parent / "_template_dumps"
    preview.mkdir(exist_ok=True)
    Image.fromarray(np.clip(avg, 0, 255).astype(np.uint8)).save(
        preview / f"{label}_rgb_avg.png"
    )
    # Report per-channel stats of foreground pixels (top 30% brightest).
    gray_for_thr = avg.mean(axis=2)
    fg_thr = np.percentile(gray_for_thr, 70)
    fg_mask = gray_for_thr >= fg_thr
    if fg_mask.any():
        fg_pixels = avg[fg_mask]
        print(f"  fg color (avg of top-30% brightest): "
              f"R={fg_pixels[:, 0].mean():.0f} "
              f"G={fg_pixels[:, 1].mean():.0f} "
              f"B={fg_pixels[:, 2].mean():.0f}")
    return avg


def main() -> int:
    out_path = ROOT / "ocr" / "sc_templates" / "labels_rgb.npz"
    templates: dict[str, np.ndarray] = {}
    for label in ("mass", "resistance", "instability"):
        t = _build_one(label)
        if t is not None:
            templates[label] = t
    if not templates:
        print("FAILED: no templates built")
        return 1
    np.savez_compressed(
        out_path,
        height=np.int32(CANONICAL_H),
        **templates,
    )
    print(f"\nSAVED {len(templates)} templates to {out_path}")
    for k, v in templates.items():
        print(f"  {k}: shape={v.shape} dtype={v.dtype}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
