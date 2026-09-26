"""Font-variant synthesis for CRNN training.

Why this exists
---------------
The CRNN is trained mostly on Furore as rasterized on ONE machine.
Other users' machines render the same font differently:

* Windows ClearType on/off, or grayscale antialiasing
* Display scaling (100/125/150 %) -> different effective stroke weights
* GPU/driver texture filtering -> sharper or softer glyph edges
* Different render resolutions before the HUD downscales to screen

The logical glyph is identical; the raster is not. Stroke weight,
edge phase, and tracking all shift slightly -- enough to move the
CRNN's confidences on machines that are not Elah's.

This module turns each bundled font into a FAMILY of raster
variants along four axes:

  weight      -- embolden (``stroke_width``) / thin (erosion)
  geometry    -- anisotropic scale, shear, per-char tracking jitter
  raster      -- supersample + fractional phase, random downsample
                 kernel (BILINEAR/BICUBIC/LANCZOS simulate different
                 antialiasing/hinting decisions)
  photometric -- brightness/contrast, blur/sharpen, JPEG round-trip,
                 noise, background level

The bundled fonts (Furore, Orbitron, Quantico, Jura) all share the
angular sci-fi aesthetic of the SC HUD but rasterize differently,
so cross-font samples teach shape, not memorized pixels.

Only the label sampler is shared with ``ocr.synth_data`` so the
training distribution stays anchored to real HUD vocabulary.

Usage (library)::

    from ocr.font_variants import iter_variant_samples
    for img, label in iter_variant_samples(n=1000, seed=0):
        ...  # img: uint8 ndarray, bright text on dark bg

Dependencies: numpy, PIL only -- no torch.
"""
from __future__ import annotations

import io
import random
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ocr.synth_data import _sample_label

_MODULE_DIR = Path(__file__).resolve().parent
_TOOL_ROOT = _MODULE_DIR.parent

# All HUD-ish display fonts shipped with the toolbox. Furore is the
# authoritative SC mining font; the others are stylistic stand-ins
# that widen the raster distribution so the model learns shapes.
BUNDLED_FONTS: dict[str, str] = {
    "furore": "furore.otf",
    "orbitron": "orbitron.ttf",
    "quantico": "quantico.ttf",
    "jura": "jura.ttf",
}

# Size tiers. The small tier is the critical one: extraction-mode
# SCAN RESULTS renders digits at ~10-15 px native, and small-size
# rasterization is where hinting/ClearType differences are largest.
SIZE_TIERS: tuple[tuple[int, float], ...] = (
    (0, 0.45),   # 10-17 px -- critical small tier
    (1, 0.35),   # 18-30 px -- typical HUD values
    (2, 0.20),   # 34-48 px -- large / zoomed captures
)
_SIZES = {
    0: (10, 12, 14, 16),
    1: (18, 22, 26, 30),
    2: (34, 40, 48),
}


def available_fonts(root: Optional[Path] = None) -> dict[str, Path]:
    """Return {font_name: path} for bundled fonts that exist on disk."""
    root = root or _TOOL_ROOT
    out: dict[str, Path] = {}
    for name, fname in BUNDLED_FONTS.items():
        p = root / fname
        if p.is_file():
            out[name] = p
    return out


def _pick_size(rng: random.Random) -> int:
    r = rng.random()
    acc = 0.0
    for tier, w in SIZE_TIERS:
        acc += w
        if r < acc:
            return rng.choice(_SIZES[tier])
    return rng.choice(_SIZES[1])


def _draw_tracked(
    label: str,
    font: ImageFont.FreeTypeFont,
    ss: int,
    rng: random.Random,
    stroke: int,
    phase: tuple[float, float],
) -> Image.Image:
    """Render ``label`` on a supersampled canvas with per-char tracking.

    Draws character-by-character so tracking (inter-glyph gap) can
    vary per sample -- real HUD text spacing shifts subtly with
    resolution and font-substitution. Returns an "L" image at
    ``size * ss`` scale with bright text on dark background.
    """
    probe = ImageDraw.Draw(Image.new("L", (1, 1), 0))
    widths: list[int] = []
    bboxes: list[tuple[int, int, int, int]] = []
    for ch in label:
        try:
            bb = probe.textbbox((0, 0), ch, font=font, stroke_width=stroke)
        except Exception:
            bb = (0, 0, font.size // 2, font.size)
        bboxes.append(bb)
        widths.append(max(1, bb[2] - bb[0]))

    # Tracking jitter in supersampled px. Mean slightly negative
    # (tight, HUD-like) with occasional wide samples.
    gap = int(rng.uniform(-0.04, 0.18) * font.size)
    gap = max(-font.size // 8, gap)

    total_w = sum(widths) + gap * (len(label) - 1) + 4 * ss
    line_h = int(font.size * 1.5) + 4 * ss
    img = Image.new("L", (total_w, line_h), color=rng.randint(6, 28))
    draw = ImageDraw.Draw(img)
    fill = rng.randint(205, 255)

    x = 2 * ss + int(phase[0] * ss)
    base_y = (line_h - font.size) // 2 + int(phase[1] * ss)
    for i, ch in enumerate(label):
        bb = bboxes[i]
        # Per-char vertical jitter -- baseline wobble.
        y_j = rng.randint(-ss, ss) if rng.random() < 0.3 else 0
        try:
            draw.text(
                (x - bb[0], base_y - bb[1] + y_j),
                ch, fill=fill, font=font, stroke_width=stroke,
                stroke_fill=fill,
            )
        except Exception:
            draw.text((x, base_y), ch, fill=fill, font=font)
        x += widths[i] + gap
    return img


def render_variant(
    label: str,
    font_path: Path,
    size: int,
    rng: random.Random,
) -> Image.Image:
    """Render one label in one variant of ``font_path``.

    Pipeline: supersample -> tracked draw with weight/phase variants ->
    shear -> downsample with a random kernel -> anisotropic scale ->
    photometric augment. Returns a tight-cropped "L" image, bright
    text on dark background.
    """
    # Higher supersampling for small sizes: sub-pixel hinting
    # differences live exactly there.
    ss = 4 if size < 20 else 3
    try:
        font = ImageFont.truetype(str(font_path), size=size * ss)
    except Exception:
        font = ImageFont.load_default()

    # Weight variant: embolden ~1 supersampled px with p=0.35.
    stroke = ss // 3 if rng.random() < 0.35 else 0
    # Fractional phase: which sub-pixel grid the glyph lands on.
    phase = (rng.choice((0.0, 0.25, 0.5, 0.75)), rng.choice((0.0, 0.25, 0.5)))

    img = _draw_tracked(label, font, ss, rng, stroke, phase)

    # Shear (italic-like distortion from perspective/kerning diffs).
    shx = rng.uniform(-0.05, 0.05)
    if abs(shx) > 0.01:
        img = img.transform(
            img.size, Image.AFFINE, (1, shx, 0, 0, 1, 0),
            resample=Image.BILINEAR, fillcolor=0,
        )

    # Downsample with a random kernel -- kernel choice IS the
    # antialiasing/hinting decision we are simulating.
    kernel = rng.choice((Image.BILINEAR, Image.BICUBIC, Image.LANCZOS))
    w, h = img.size
    img = img.resize((max(8, w // ss), max(8, h // ss)), kernel)

    # Anisotropic scale: non-uniform DPI across axes happens with
    # non-square pixels / odd capture scaling.
    if rng.random() < 0.30:
        w, h = img.size
        img = img.resize(
            (max(6, int(w * rng.uniform(0.92, 1.08))),
             max(6, int(h * rng.uniform(0.92, 1.08)))),
            Image.BILINEAR,
        )

    # Thin variant: erode with p=0.20 (counterpart to embolden).
    if stroke == 0 and rng.random() < 0.20:
        img = img.filter(ImageFilter.MinFilter(3))

    # -- Photometric -------------------------------------------------
    arr = np.asarray(img, dtype=np.float32)
    arr *= rng.uniform(0.75, 1.10)
    arr += rng.uniform(-12, 12)
    if rng.random() < 0.35:
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.15, 0.9)))
        arr = np.asarray(img, dtype=np.float32)
    elif rng.random() < 0.15:
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=140))
        arr = np.asarray(img, dtype=np.float32)
    if rng.random() < 0.30:
        buf = io.BytesIO()
        try:
            Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(
                buf, format="JPEG", quality=rng.randint(40, 85))
            buf.seek(0)
            arr = np.asarray(Image.open(buf).convert("L"), dtype=np.float32)
        except Exception:
            pass
    if rng.random() < 0.30:
        arr += rng.uniform(2, 8) * np.random.randn(*arr.shape)
    arr = np.clip(arr, 0, 255).astype(np.uint8)

    # Tight crop to content + small pad; reject degenerate renders.
    mask = arr > 80
    if not mask.any():
        raise ValueError("empty render")
    ys = np.where(mask.any(axis=1))[0]
    xs = np.where(mask.any(axis=0))[0]
    p = 2
    y1, y2 = max(0, ys[0] - p), min(arr.shape[0], ys[-1] + p + 1)
    x1, x2 = max(0, xs[0] - p), min(arr.shape[1], xs[-1] + p + 1)
    if (y2 - y1) < 5 or (x2 - x1) < 3:
        raise ValueError("degenerate render")
    return Image.fromarray(arr[y1:y2, x1:x2])


def iter_variant_samples(
    n: int,
    seed: int = 101,
    fonts: Optional[dict[str, Path]] = None,
    font_weights: Optional[dict[str, float]] = None,
) -> Iterator[tuple[np.ndarray, str]]:
    """Yield ``n`` (image, label) variant samples.

    ``font_weights`` biases how often each font is picked -- Furore
    should dominate (it IS the HUD font) but not monopolize:

        {"furore": 0.55, "orbitron": 0.20, "quantico": 0.15, "jura": 0.10}

    Skips degenerate renders instead of yielding blanks.
    """
    rng = random.Random(seed)
    np.random.seed(seed)
    fonts = fonts or available_fonts()
    if not fonts:
        return
    if font_weights is None:
        # Furore dominant -- it is the exact HUD font.
        font_weights = {name: (0.55 if name == "furore" else 0.15)
                        for name in fonts}
    names = list(fonts.keys())
    weights = [font_weights.get(nm, 0.1) for nm in names]

    yielded = 0
    attempts = 0
    while yielded < n and attempts < n * 20:
        attempts += 1
        label = _sample_label(rng)
        font_name = rng.choices(names, weights=weights, k=1)[0]
        size = _pick_size(rng)
        try:
            img = render_variant(label, fonts[font_name], size, rng)
        except Exception:
            continue
        yield np.asarray(img, dtype=np.uint8), label
        yielded += 1


if __name__ == "__main__":
    # Eyeball a handful of variants: writes a montage grid PNG next
    # to this file. Not part of any training flow.
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--out", default=str(_MODULE_DIR / "_font_variant_montage.png"))
    args = parser.parse_args()

    samples = list(iter_variant_samples(args.n))
    cols = 5
    rows = (len(samples) + cols - 1) // cols
    cell_w = max(s[0].shape[1] for s in samples) + 8
    cell_h = max(s[0].shape[0] for s in samples) + 8
    grid = Image.new("L", (cols * cell_w, rows * cell_h), 60)
    for i, (arr, _lbl) in enumerate(samples):
        im = Image.fromarray(arr)
        x = (i % cols) * cell_w + 4
        y = (i // cols) * cell_h + 4
        grid.paste(im, (x, y))
    grid.save(args.out)
    print(f"wrote {args.out} ({len(samples)} samples)")
