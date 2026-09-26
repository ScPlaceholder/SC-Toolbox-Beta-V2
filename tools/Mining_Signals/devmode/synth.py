"""Synthetic glyphs to balance weak classes.

Each class of the kind's model is topped up to ``per_class`` samples
(approved + synthetic). Augmentation reuses the maintainers' code:

  grey pools: scripts/augment_signal_glyphs._make_variants
              (shift, icon-edge artefact, neighbour ghosting, contrast, noise)
  RGB pools:  scripts/augment_rgb_signal_glyphs._augment_one

Seeds, in priority order:
  1. the class's APPROVED REAL glyphs (cut from train-split captures);
  2. its APPROVED FONT glyphs (devmode.fontglyphs), after the real ones:
     when a class has both, at least REAL_SEED_MIN_SHARE of the synthetic
     images (more if real seeds are the majority) grow from real seeds;
  3. only when a class has neither, stock seeds so training is not blocked
     by one missing class:
  digits (grey)  -> ocr/synth_data._load_sc_templates (hand-curated SC digits)
  '.' '%' (grey) -> ocr/synth_data._render_char
  '@' icon       -> tools/Mining_Signals/training_data_blacklist/*.png (read-only)
RGB pools get no template fallback (the templates carry no colour); such a
class stays at 0 and train() will name it.

Approved real + font glyphs both count toward ``per_class``: they are
training samples themselves (train.build_dataset). ``synth_seeds(kind)``
reports, per class, how many seeds are real / font / stock.

Synthetic glyphs are regenerated from scratch on every call and never
enter the benchmark.
"""
from __future__ import annotations

import logging
import random
import shutil
from typing import Callable, Optional

import numpy as np
from PIL import Image

from . import glyphs, kinds, paths

log = logging.getLogger(__name__)

Progress = Optional[Callable[[float, str], None]]


REAL_SEED_MIN_SHARE = 0.5


def synth_dir(kind: str):
    return paths.sub("synth", kinds.pool(kind))


def _tile_from_tight(arr: np.ndarray, rgb: bool) -> np.ndarray:
    """Tight bright-on-dark glyph -> the 2-px-white-pad 28x28 tile format
    produced by extract_labeled_glyphs._glyph_to_28x28."""
    arr = np.asarray(arr, dtype=np.uint8)
    pad = 2
    h, w = arr.shape[:2]
    if rgb:
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        canvas = np.full((h + 2 * pad, w + 2 * pad, 3), 255, dtype=np.uint8)
        canvas[pad:pad + h, pad:pad + w] = arr
        return np.asarray(Image.fromarray(canvas, "RGB").resize((28, 28), Image.BILINEAR))
    if arr.ndim == 3:
        arr = np.asarray(Image.fromarray(arr).convert("L"))
    canvas = np.full((h + 2 * pad, w + 2 * pad), 255, dtype=np.uint8)
    canvas[pad:pad + h, pad:pad + w] = arr
    return np.asarray(Image.fromarray(canvas, "L").resize((28, 28), Image.BILINEAR))


def _fallback_seeds(kind: str, ch: str) -> list[np.ndarray]:
    rgb = kinds.is_rgb(kind)
    paths.ensure_import_paths()
    if ch == "@":
        out = []
        bl = paths.TOOL_DIR / "training_data_blacklist"
        for f in sorted(bl.glob("*.png")) if bl.is_dir() else []:
            try:
                with Image.open(f) as im:
                    arr = np.asarray(im.convert("RGB" if rgb else "L"))
            except (OSError, ValueError) as exc:
                log.warning("devmode: icon seed %s unreadable: %s", f.name, exc)
                continue
            out.append(_tile_from_tight(arr, rgb))
        return out
    if rgb:
        return []
    from ocr import synth_data  # noqa: WPS433
    if ch.isdigit():
        tpl = synth_data._load_sc_templates().get(ch)
        return [_tile_from_tight(tpl, False)] if tpl is not None else []
    return [_tile_from_tight(synth_data._render_char(ch, 20), False)]


def _load_tile(path: str, rgb: bool) -> Optional[np.ndarray]:
    try:
        with Image.open(path) as im:
            return np.asarray(im.convert("RGB" if rgb else "L").resize((28, 28), Image.BILINEAR))
    except (OSError, ValueError) as exc:
        log.warning("devmode: glyph %s unreadable: %s", path, exc)
        return None


def generate_synth(kind: str, per_class: int, progress: Progress = None) -> dict:
    if per_class < 0:
        raise ValueError("per_class must be >= 0")
    paths.ensure_import_paths()
    import augment_signal_glyphs as aug_gray       # noqa: WPS433
    import augment_rgb_signal_glyphs as aug_rgb    # noqa: WPS433

    rgb = kinds.is_rgb(kind)
    classes = kinds.classes_for(kind)
    root = synth_dir(kind)
    shutil.rmtree(root, ignore_errors=False)
    root.mkdir(parents=True, exist_ok=True)

    real, font = _approved_tiles(kind)

    result: dict[str, int] = {}
    for ci, ch in enumerate(classes):
        if progress:
            progress(ci / max(1, len(classes)), f"class {ch!r}")
        need = per_class - len(real[ch]) - len(font[ch])
        if need <= 0:
            result[ch] = 0
            continue
        r_seeds, f_seeds = real[ch], font[ch]
        if not r_seeds and not f_seeds:
            r_seeds = _fallback_seeds(kind, ch)       # stock, last resort
        if not r_seeds and not f_seeds:
            result[ch] = 0
            continue
        rng = random.Random(1337 + ci)
        nrng = np.random.default_rng(1337 + ci)
        np.random.seed(1337 + ci)          # _augment_one draws from the global RNG
        others = ([t for c in classes if c != ch for t in real[c] + font[c]]
                  if not rgb else [])
        out_dir = root / kinds.class_dirname(ch)
        out_dir.mkdir(parents=True, exist_ok=True)
        pick = _seed_picker(r_seeds, f_seeds, random.Random(7331 + ci))
        for i in range(need):
            seed = pick()
            if rgb:
                v = aug_rgb._augment_one(seed, rng)
                Image.fromarray(v, "RGB").save(out_dir / f"syn_{i:05d}.png")
            else:
                v = aug_gray._make_variants(seed, others, 1, nrng)[0]
                Image.fromarray(v, "L").save(out_dir / f"syn_{i:05d}.png")
        result[ch] = need
    if progress:
        progress(1.0, f"{sum(result.values())} synthetic glyphs")
    return result


def _approved_tiles(kind: str) -> tuple[dict, dict]:
    rgb = kinds.is_rgb(kind)
    classes = kinds.classes_for(kind)
    real: dict[str, list[np.ndarray]] = {ch: [] for ch in classes}
    font: dict[str, list[np.ndarray]] = {ch: [] for ch in classes}
    for dest, rows in ((real, glyphs.approved_real_glyphs(kind)),
                       (font, glyphs.approved_font_glyphs(kind))):
        for g in rows:
            if g["char"] not in dest:
                continue
            t = _load_tile(g["image_path"], rgb)
            if t is not None:
                dest[g["char"]].append(t)
    return real, font


def _seed_picker(real: list, font: list, rng: random.Random):
    """Real seeds first: each list is cycled in order (every seed used
    before any repeats); when both exist, a draw is real with probability
    max(REAL_SEED_MIN_SHARE, real share of the seeds)."""
    share = 1.0 if not font else (0.0 if not real else
                                  max(REAL_SEED_MIN_SHARE, len(real) / (len(real) + len(font))))
    idx = {"r": 0, "f": 0}

    def pick():
        key, pool = ("r", real) if rng.random() < share else ("f", font)
        seed = pool[idx[key] % len(pool)]
        idx[key] += 1
        return seed
    return pick


def synth_seeds(kind: str) -> dict[str, dict[str, int]]:
    """Per class: how many synth seeds are real / font / stock right now.
    Stock seeds are only used (and only counted) when a class has neither."""
    classes = kinds.classes_for(kind)
    out = {ch: {"real": 0, "font": 0, "stock": 0} for ch in classes}
    for key, rows in (("real", glyphs.approved_real_glyphs(kind)),
                      ("font", glyphs.approved_font_glyphs(kind))):
        for g in rows:
            if g["char"] in out:
                out[g["char"]][key] += 1
    for ch, d in out.items():
        if not d["real"] and not d["font"]:
            d["stock"] = len(_fallback_seeds(kind, ch))
    return out


def synth_files(kind: str) -> dict[str, list[str]]:
    root = paths.dev_root() / "synth" / kinds.pool(kind)
    out: dict[str, list[str]] = {}
    for ch in kinds.classes_for(kind):
        d = root / kinds.class_dirname(ch)
        out[ch] = sorted(str(p) for p in d.glob("*.png")) if d.is_dir() else []
    return out
