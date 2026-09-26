"""Diagnose: why does the short RESI/INSTA template fail when the
underlying glyphs are identical to the leading edge of the full word?

Saves visualizations + runs head-to-head NCC at known-good positions.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 1. Dump raw templates as PNGs so we can SEE them.
data = np.load(ROOT / "ocr" / "sc_templates" / "labels.npz")
out = ROOT / "_template_dumps"
out.mkdir(exist_ok=True)
for key in ("mass", "resistance", "instability"):
    arr = data[key].astype(np.uint8)
    Image.fromarray(arr).save(out / f"{key}_full.png")
    print(f"{key} full: shape={arr.shape}")
    if key == "resistance":
        slice_w = int(arr.shape[1] * 0.42)
        sliced = arr[:, :slice_w]
        Image.fromarray(sliced).save(out / f"{key}_short.png")
        print(f"  -> short (RESI): shape={sliced.shape}")
    elif key == "instability":
        slice_w = int(arr.shape[1] * 0.46)
        sliced = arr[:, :slice_w]
        Image.fromarray(sliced).save(out / f"{key}_short.png")
        print(f"  -> short (INSTA): shape={sliced.shape}")

# 2. On a known-LOCKING ANNOTATED panel, get the FULL template's match
#    position, then check what the SHORT template scores AT THAT POSITION
#    and what its peak position+score are.
import glob
img_paths = sorted(glob.glob(str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")))
# Pick a few panels that LOCKED (so we have ground truth from the full template).
sample_paths = img_paths[:5] + img_paths[100:105]

from ocr.sc_ocr.label_match import (
    _load_templates,
    _canonicalize,
    _resize_template,
    _ncc_search,
    _SCALE_FACTORS,
)

templates = _load_templates()
print(f"\nLoaded templates: {sorted(templates.keys())}\n")

# Get sliced (un-normalized) versions directly for visualization.
res_full = data['resistance'].astype(np.float32)
res_short_raw = res_full[:, :int(res_full.shape[1] * 0.42)]
inst_full = data['instability'].astype(np.float32)
inst_short_raw = inst_full[:, :int(inst_full.shape[1] * 0.46)]

# Quick test: load a panel and see what RESISTANCE-full vs RESI-short find.
for p in sample_paths:
    name = os.path.basename(p)
    img = Image.open(p).convert("RGB")
    W, H = img.size
    search_w = int(W * 0.65)
    crop = img.crop((0, 0, search_w, H))
    target_gray = np.asarray(crop.convert("L"), dtype=np.uint8)
    target = _canonicalize(target_gray)

    # Find MASS first to determine scale (just like production does).
    best_mass = None
    for scale in _SCALE_FACTORS:
        sc = _resize_template(templates["mass"], scale)
        score, x, y = _ncc_search(target, sc)
        if best_mass is None or score > best_mass[0]:
            best_mass = (score, x, y, scale)
    if best_mass is None or best_mass[0] < 0.45:
        continue
    mass_score, mass_x, mass_y, mass_scale = best_mass
    print(f"\n--- {name}  ({W}x{H}) ---")
    print(f"  MASS scored {mass_score:.3f} at ({mass_x}, {mass_y}) scale={mass_scale:.2f}")

    # Test resistance full vs short at MASS scale, full-frame.
    for lab in ("resistance", "instability"):
        full = _resize_template(templates[lab], mass_scale)
        short = _resize_template(templates[f"{lab}_short"], mass_scale)
        fsc, fx, fy = _ncc_search(target, full)
        ssc, sx, sy = _ncc_search(target, short)
        # Also: what does short score AT THE FULL'S best position?
        if 0 <= fx <= target.shape[1] - short.shape[1] and 0 <= fy <= target.shape[0] - short.shape[0]:
            window = target[fy:fy + short.shape[0], fx:fx + short.shape[1]]
            n = float(short.shape[0] * short.shape[1])
            short_at_full = float(np.sum(window * short) / n)
        else:
            short_at_full = float('nan')
        print(f"  {lab:12s}: full={fsc:.3f}@({fx},{fy})  "
              f"short_peak={ssc:.3f}@({sx},{sy})  "
              f"short@full_pos={short_at_full:.3f}")

print(f"\nTemplate dumps saved to {out}")
