"""Synthesize sharp RGB templates by colorizing labels.npz grayscale.

The averaged-from-annotations templates are too blurry because each
panel's text starts at a slightly different sub-row position. The
existing labels.npz already has SHARP grayscale templates (built
offline from clean reference panels). Colorizing them with the
measured mint-cyan foreground color and a reasonable HUD-interior
background gives us sharp RGB templates with the right color signature.

Outputs: labels_rgb_sharp.npz
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent

# Measured from _build_rgb_label_templates.py output (top-30% brightest
# of the averaged real templates):
#   mass         R=143 G=157 B=153  -> moderate mint
#   resistance   R=193 G=220 B=206  -> brighter mint
#   instability  R=179 G=204 B=198  -> bright mint
# The averaging across many panels likely under-represents the
# foreground brightness on sharp captures (since dim/skewed crops pull
# the mean down). Use the brighter resistance/instability values as
# representative since those crops were fewer + cleaner.
FG_COLORS = {
    "mass":        np.array([165, 200, 180], dtype=np.float32),
    "resistance":  np.array([165, 200, 180], dtype=np.float32),
    "instability": np.array([165, 200, 180], dtype=np.float32),
}
# Background: HUD interior is translucent dark cyan; absolute brightness
# varies across panels but the relative R<G≈B pattern is consistent.
BG_COLOR = np.array([15, 30, 35], dtype=np.float32)


def main() -> int:
    gray_npz = np.load(ROOT / "ocr" / "sc_templates" / "labels.npz")
    out = {}
    for key in ("mass", "resistance", "instability"):
        if key not in gray_npz:
            continue
        gray = gray_npz[key].astype(np.float32) / 255.0  # 0..1
        fg = FG_COLORS[key]
        rgb = gray[..., None] * fg + (1.0 - gray[..., None]) * BG_COLOR
        out[key] = rgb
        # Preview.
        preview = ROOT / "_template_dumps"
        preview.mkdir(exist_ok=True)
        Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(
            preview / f"{key}_rgb_synth.png"
        )
        print(f"{key}: shape={rgb.shape}")
    out["height"] = np.int32(gray_npz["height"]) if "height" in gray_npz else np.int32(28)
    np.savez_compressed(
        ROOT / "ocr" / "sc_templates" / "labels_rgb_sharp.npz",
        **out,
    )
    print(f"\nSaved labels_rgb_sharp.npz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
