"""Stage RGB icon training data for the v2 RGB-CNN.

Reads:
  - training_data_user_sig/icon/aug_bad_crop_*.png        (600 grayscale)
  - training_data_pending_review_signal/icon/pending_*_rgb.png (6 real RGB)

Writes:
  - training_data_user_sig_rgb/icon/aug_bad_crop_NNNN_rgb.png   (600 colorized)
  - training_data_user_sig_rgb/icon/real_<source_id>_rgb.png    (6 real)

Colorization: each grayscale glyph gets a warm hue (HSV 15-45),
saturation [120-220], value [180-255], applied as a per-pixel
multiply against the grayscale luminance so the icon's stroke
inherits the in-game yellow/orange tint while bg stays dark.
"""
from __future__ import annotations

import random
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(
    r"C:\Users\prjgn\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)
GRAY_DIR = ROOT / "training_data_user_sig" / "icon"
REAL_DIR = ROOT / "training_data_pending_review_signal" / "icon"
OUT_DIR = ROOT / "training_data_user_sig_rgb" / "icon"

random.seed(20260508)


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[float, float, float]:
    """h in [0, 360), s/v in [0, 255]. Returns (r, g, b) in [0, 255]."""
    h = h % 360.0
    s = max(0.0, min(255.0, s)) / 255.0
    v = max(0.0, min(255.0, v)) / 255.0
    c = v * s
    h_ = h / 60.0
    x = c * (1 - abs((h_ % 2) - 1))
    if 0 <= h_ < 1:
        r1, g1, b1 = c, x, 0.0
    elif 1 <= h_ < 2:
        r1, g1, b1 = x, c, 0.0
    elif 2 <= h_ < 3:
        r1, g1, b1 = 0.0, c, x
    elif 3 <= h_ < 4:
        r1, g1, b1 = 0.0, x, c
    elif 4 <= h_ < 5:
        r1, g1, b1 = x, 0.0, c
    else:
        r1, g1, b1 = c, 0.0, x
    m = v - c
    return ((r1 + m) * 255.0, (g1 + m) * 255.0, (b1 + m) * 255.0)


def colorize(gray_arr: np.ndarray, hue: float, sat: float, val: float) -> np.ndarray:
    """Multiply grayscale luma against an HSV-derived warm color.

    Result: dark pixels stay dark, bright pixels take on the chosen
    hue. This mimics how a colored glyph renders on a dark bg in-game.

    gray_arr: (H, W) uint8.
    Returns: (H, W, 3) uint8.
    """
    r, g, b = hsv_to_rgb(hue, sat, val)
    luma = gray_arr.astype(np.float32) / 255.0  # [0, 1]
    out = np.empty(gray_arr.shape + (3,), dtype=np.float32)
    out[..., 0] = luma * r
    out[..., 1] = luma * g
    out[..., 2] = luma * b
    return np.clip(out, 0.0, 255.0).astype(np.uint8)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Colorize 600 grayscale augmentations
    gray_files = sorted(GRAY_DIR.glob("aug_bad_crop_*.png"))
    print(f"colorizing {len(gray_files)} grayscale icons -> {OUT_DIR}")
    n_color = 0
    for src in gray_files:
        try:
            gray = np.asarray(Image.open(src).convert("L"), dtype=np.uint8)
            if gray.shape != (28, 28):
                # Resize to 28x28 if not already
                pil = Image.open(src).convert("L").resize(
                    (28, 28), Image.LANCZOS
                )
                gray = np.asarray(pil, dtype=np.uint8)
            hue = random.uniform(15.0, 45.0)
            sat = random.uniform(120.0, 220.0)
            val = random.uniform(180.0, 255.0)
            rgb = colorize(gray, hue, sat, val)
            # Filename pattern mirrors gray: aug_bad_crop_NNNN_rgb.png
            stem = src.stem  # aug_bad_crop_0000
            out_name = f"{stem}_rgb.png"
            Image.fromarray(rgb, mode="RGB").save(OUT_DIR / out_name)
            n_color += 1
        except Exception as e:
            print(f"  skip {src.name}: {e}")

    # 2) Copy 6 real RGB icons (only _rgb variants)
    real_files = sorted(REAL_DIR.glob("pending_*_rgb.png"))
    print(f"copying {len(real_files)} real RGB icons -> {OUT_DIR}")
    n_real = 0
    for src in real_files:
        try:
            # Make sure they are 28x28 RGB
            pil = Image.open(src).convert("RGB")
            if pil.size != (28, 28):
                pil = pil.resize((28, 28), Image.LANCZOS)
            # Source id: pending_cap_20260418_155446_555 -> real_<id>_rgb.png
            # Strip "pending_" prefix and "_rgb" suffix from stem.
            stem = src.stem  # pending_cap_20260418_155446_555_rgb
            assert stem.endswith("_rgb")
            inner = stem[:-len("_rgb")]  # pending_cap_..._555
            assert inner.startswith("pending_")
            source_id = inner[len("pending_"):]  # cap_..._555
            out_name = f"real_{source_id}_rgb.png"
            pil.save(OUT_DIR / out_name)
            n_real += 1
        except Exception as e:
            print(f"  skip {src.name}: {e}")

    total = sum(1 for _ in OUT_DIR.glob("*.png"))
    print(f"done: colorized={n_color}  real={n_real}  total_in_icon_dir={total}")


if __name__ == "__main__":
    main()
