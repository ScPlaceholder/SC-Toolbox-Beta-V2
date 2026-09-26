"""Render glyph tiles from the real game font ("can't find a 9? render 100").

Some characters are rare on the panels, so collecting enough real glyphs of
them takes hours of play. This renders ``count`` tiles of one character
through the maintainers' font-variant synthesiser and queues them in the
kind's glyph pool as PENDING, source ``"font"``, for the same human review
as real glyphs.

Reused, not rewritten:
  ocr/font_variants.py      render_variant(): supersample, tracking, weight
                            (embolden/erode), shear, random downsample kernel,
                            anisotropic scale, blur/sharpen, JPEG, noise
  scripts/extract_labeled_glyphs._glyph_to_28x28 / _otsu
  scripts/extract_rgb_signal_glyphs._glyph_to_28x28_rgb
                            the exact cut real glyphs get (via devmode.segment)

Fonts are a per-FAMILY setting (region_font / set_region_font), because the
scanner (signal) and the HUD are NOT drawn in the same font. Measured
2026-09-25 against the maintainers' real, reviewed tiles (digits 0-9; NCC of
each real tile with the same digit rendered in the font, and nearest-template
accuracy over the ten digits; "ceiling" = a template made from the other half
of the real tiles):

    region  real set                         ceiling      furore      orbitron    quantico    jura
    hud     training_data_user_panel_rgb     .82 / 89%    .49 / 78%   .27 / 45%   .33 / 56%   .31 / 56%
    hud     training_data_hud_glyphs_extr.   .90 / 99%    .57 / 82%   .28 / 55%   .38 / 61%   .33 / 58%
    signal  training_data_user_sig_rgb       .80 / 95%    .31 / 46%   .31 / 18%   .28 / 31%   .30 / 19%

Furore clearly wins the HUD. For the scanner no bundled font matches: the
best NCC is a four-way tie at ~.30 and the best accuracy (Furore, 46%) is
half the ceiling. So DEFAULT_REGION_FONT sets hud -> furore and signal ->
None: rendering for a signal kind raises NoMatchingFontError ("no matching
font bundled for this region") until someone bundles the scanner font and
sets it. A wrong font would teach the model the wrong shapes.

Within a family that has a font, the other bundled faces are a small
minority (``lookalike_share``, default 15%, split evenly): a class learnt
mostly from one font's rasteriser memorises its edges, and a sprinkle of
other angular faces buys shape diversity while the family font stays the
prototype. The split is allocated exactly (100 renders = 85 of the family
font); set lookalike_share to 0 for the family font alone. A missing
look-alike file hands its share to the family font; a missing family font
is an error.

Tile geometry — identical to a real tile of the same kind: the render is
laid on a small row, the ink columns are found with the extractor's own
Otsu polarity rule, then the extractor's cutter crops the ink rows, pads 2 px
white and resizes to 28x28 (28x28x3 for rgb kinds). Every kind's CNN eats
28x28; rgb kinds keep colour, grey kinds get the luma after
segment._canon_bright_text, exactly as extract_glyphs does.

Colour — rgb kinds (signal_rgb, signal_rgb_inv, hud_rgb) copy the LOOK of
one approved real glyph of the pool per render (train split only, lightly
jittered): ink core colour, panel colour and the red/blue channel offset
(the HUD's chromatic aberration: on real tiles red sits ~2-4 tile px one
way and blue ~1-2 px the other). No approved real glyph ->
NeedRealGlyphsError; no colour is ever guessed. Grey kinds use the measured
grey levels of their real glyphs when there are any, else bright-on-dark
levels (both panels draw light text on a dark box; ocr/training_registry).

Inversion — ``_inv`` kinds share their pool with the upright twin, and
real tiles are stored UPRIGHT there and inverted at load (segment.to_input,
train_worker). Font tiles go in the same pool the same way, so they are
inverted by the same code, never here.

TRAINING ONLY: font glyphs have no capture (capture_id = ''). The benchmark
scores held-out CAPTURES and never reads the glyph table.
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Callable, Optional

import numpy as np
from PIL import Image

from . import db, glyphs, kinds, paths, segment

log = logging.getLogger(__name__)

Progress = Optional[Callable[[float, str], None]]

# Family -> bundled font name (see the measurement in the module docstring).
DEFAULT_REGION_FONT = {"hud": "furore", "signal": None}
DEFAULT_LOOKALIKE_SHARE = 0.15
MAX_COUNT = 2000

# On-screen DIGIT HEIGHT (ink, px) to render at, per family. Real glyphs are
# 12-24 px on screen (MINING_SIGNALS_WRITEUP.md); a 1440p signal capture in
# live_samples measures 17-20 px of digit ink, the HUD rows run smaller.
# Converted to a font size per font from its measured digit height, so the
# look-alikes (whose digits are taller or shorter per em) land at the same
# pixel height and blur the same way.
INK_PX = {"signal": (15, 16, 17, 18, 19, 20, 22), "hud": (11, 12, 13, 14, 15, 16, 18)}
_DIGIT_RATIO: dict[str, float] = {}

# Grey kinds with no approved real glyph yet: light text on a dark box.
DEFAULT_GREY_FG = (200, 250)
DEFAULT_GREY_BG = (18, 70)

_ATTEMPTS = 12


class FontRenderError(RuntimeError):
    pass


class NeedRealGlyphsError(FontRenderError):
    pass


class NoMatchingFontError(FontRenderError):
    pass


_FAMILY_NAMES = {"signal": "the scanner (signal) panel", "hud": "the mining HUD"}


def _fv():
    paths.ensure_import_paths()
    try:
        from ocr import font_variants  # noqa: WPS433 — lazy: pulls in ocr.synth_data
    except ImportError as exc:
        raise FontRenderError(f"the font renderer (ocr/font_variants.py) is not installed: {exc}") from exc
    return font_variants


def available_fonts() -> dict:
    """{name: path} of the bundled fonts on disk; {} (logged) when the
    renderer itself is missing, so callers can still show a reason."""
    try:
        return _fv().available_fonts(paths.TOOL_DIR)
    except FontRenderError as exc:
        log.warning("devmode: %s", exc)
        return {}


def _family(kind_or_family: str) -> str:
    return kind_or_family if kind_or_family in DEFAULT_REGION_FONT else kinds.family(kind_or_family)


def region_font(kind_or_family: str) -> dict:
    """The font setting of a region family: {"family", "font" (bundled name
    or None), "lookalike_share", "available" (bundled fonts on disk), "reason"}."""
    fam = _family(kind_or_family)
    st = db.read_state().get("region_fonts") or {}
    cur = st.get(fam) or {}
    font = cur["font"] if "font" in cur else DEFAULT_REGION_FONT[fam]
    share = float(cur.get("lookalike_share", DEFAULT_LOOKALIKE_SHARE))
    avail = sorted(available_fonts())
    if font is not None and font not in avail:
        return {"family": fam, "font": None, "lookalike_share": share, "available": avail,
                "reason": f"The {font} font file (or the font renderer) is missing from this "
                          f"install ({paths.TOOL_DIR}); nothing can be rendered."}
    if font is None:
        reason = (f"No matching font bundled for {_FAMILY_NAMES[fam]}: none of the bundled fonts "
                  f"({', '.join(avail)}) matches its real glyphs, and a wrong font would teach "
                  "the wrong shapes.")
    else:
        reason = f"{_FAMILY_NAMES[fam]} renders in {font}"
    return {"family": fam, "font": font, "lookalike_share": share, "available": avail,
            "reason": reason}


def set_region_font(family: str, font: Optional[str], lookalike_share: Optional[float] = None) -> dict:
    """Choose the bundled font for a region family (None = no font; rendering
    refuses). Stored in state.json; returns the new region_font()."""
    if family not in DEFAULT_REGION_FONT:
        raise ValueError(f"family must be one of {tuple(DEFAULT_REGION_FONT)}")
    if font is not None and font not in available_fonts():
        raise ValueError(f"font must be one of {sorted(available_fonts())} or None")
    entry = {"font": font}
    if lookalike_share is not None:
        if not 0.0 <= float(lookalike_share) <= 0.5:
            raise ValueError("lookalike_share must be between 0 and 0.5")
        entry["lookalike_share"] = float(lookalike_share)
    else:
        entry["lookalike_share"] = region_font(family)["lookalike_share"]
    with db.LOCK:
        fonts = dict(db.read_state().get("region_fonts") or {})
        fonts[family] = entry
        db.write_state(region_fonts=fonts)
    return region_font(family)


def font_plan(count: int, primary: str, fonts: Optional[dict] = None,
              lookalike_share: float = DEFAULT_LOOKALIKE_SHARE) -> list[str]:
    """Exact per-render font allocation: ``lookalike_share`` split evenly
    over the other bundled fonts, the rest ``primary``."""
    fonts = available_fonts() if fonts is None else fonts
    if primary not in fonts:
        raise FontRenderError(f"the {primary} font file is missing from {paths.TOOL_DIR}")
    others = sorted(n for n in fonts if n != primary)
    plan: list[str] = []
    if others and lookalike_share > 0:
        each = int(count * lookalike_share / len(others))
        for name in others:
            plan += [name] * each
    return [primary] * (count - len(plan)) + plan


# ── colour statistics from approved REAL glyphs ─────────────────────────

def _content_box(tile: np.ndarray) -> Optional[np.ndarray]:
    """Drop the white padding (all channels >= 250 across a whole row or
    column) plus one blended pixel, leaving the glyph's own pixels."""
    t = tile if tile.ndim == 3 else np.stack([tile] * 3, axis=-1)
    not_pad = (t < 250).any(axis=2)
    rows = np.where(not_pad.any(axis=1))[0]
    cols = np.where(not_pad.any(axis=0))[0]
    if len(rows) < 6 or len(cols) < 4:
        return None
    y1, y2, x1, x2 = rows[0] + 1, rows[-1], cols[0] + 1, cols[-1]
    return t[y1:y2, x1:x2]


def _shift_of(ref: np.ndarray, ch: np.ndarray) -> tuple[int, int]:
    """(dx, dy) in tile pixels that best aligns channel ``ch`` onto ``ref``
    (normalised cross-correlation over a small window)."""
    best, arg = -2.0, (0, 0)
    r = ref - ref.mean()
    rn = float(np.sqrt((r * r).sum())) or 1.0
    for dy in range(-2, 3):
        for dx in range(-5, 6):
            c = np.roll(np.roll(ch, dy, axis=0), dx, axis=1)
            c = c - c.mean()
            score = float((r * c).sum()) / (rn * (float(np.sqrt((c * c).sum())) or 1.0))
            if score > best:
                best, arg = score, (dx, dy)
    return arg


def _look(tile: np.ndarray) -> Optional[dict]:
    """What a real glyph looks like, measured from its tile:
    fg  the ink core (pixels where ALL channels are high: the part every
        colour channel overlaps, i.e. the text colour itself),
    bg  the darkest 12% of the box (the panel behind the text),
    r_shift / b_shift  the red / blue channel offset against green, in tile
        pixels: the HUD's chromatic aberration (0 for grey tiles)."""
    box = _content_box(tile)
    if box is None:
        return None
    px = box.reshape(-1, 3).astype(np.float32)
    luma = (0.299 * px[:, 0] + 0.587 * px[:, 1] + 0.114 * px[:, 2]).astype(np.uint8)
    thr = segment._helpers()._otsu(luma)
    hi, lo = luma > thr, luma <= thr
    if hi.sum() < 4 or lo.sum() < 4:
        return None
    if float(luma[hi].mean()) - float(luma[lo].mean()) < 12:
        return None                                   # no usable contrast
    # Both panels draw LIGHT text on a darker box and extract_glyphs keeps
    # the capture's own colours, so the brightest overlap is the ink.
    core = px[:, :].min(axis=1)
    top = core >= np.percentile(core, 85)
    fg = tuple(int(v) for v in px[top].mean(axis=0))
    # The panel is the darkest part of the box. The Otsu dark class is
    # mostly aberration fringe on a tight tile, so take its dark tail.
    dark = luma <= np.percentile(luma, 12)
    bg = tuple(int(v) for v in px[dark].mean(axis=0))
    f = box.astype(np.float32)
    return {"fg": fg, "bg": bg,
            "r_shift": _shift_of(f[:, :, 1], f[:, :, 0]),
            "b_shift": _shift_of(f[:, :, 1], f[:, :, 2])}


def colour_looks(kind: str) -> list[dict]:
    """The look of every approved REAL glyph of the kind's pool (train split
    only). Font glyphs never feed their own colours back in."""
    out = []
    for g in glyphs.approved_real_glyphs(kind):
        try:
            with Image.open(g["image_path"]) as im:
                tile = np.asarray(im.convert("RGB"))
        except (OSError, ValueError) as exc:
            log.warning("devmode: glyph %s unreadable: %s", g["image_path"], exc)
            continue
        lk = _look(tile)
        if lk is not None:
            out.append(lk)
    return out


def _default_grey_look(rng: random.Random) -> dict:
    f, b = rng.randint(*DEFAULT_GREY_FG), rng.randint(*DEFAULT_GREY_BG)
    return {"fg": (f, f, f), "bg": (b, b, b), "r_shift": (0, 0), "b_shift": (0, 0)}


# ── rendering ──────────────────────────────────────────────────────────

def _mask(arr: np.ndarray) -> np.ndarray:
    """font_variants output (light text, dark ground, its own photometric
    variation) -> ink coverage 0..1, keeping blur / JPEG / noise shape."""
    a = arr.astype(np.float32)
    lo, hi = np.percentile(a, 5), np.percentile(a, 99.5)
    if hi - lo < 1:
        raise ValueError("flat render")
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def _jitter(c, rng: random.Random, lo=0.92, hi=1.06) -> np.ndarray:
    return np.clip(np.asarray(c, np.float32) * rng.uniform(lo, hi), 0, 255)


def _shifted(canvas: np.ndarray, roll_dx: float, roll_dy: float) -> np.ndarray:
    """Sub-pixel np.roll equivalent (content moves by +roll_dx, +roll_dy)."""
    if not roll_dx and not roll_dy:
        return canvas
    im = Image.fromarray(canvas.astype(np.float32), "F")
    im = im.transform(im.size, Image.AFFINE, (1, 0, -roll_dx, 0, 1, -roll_dy),
                      resample=Image.BILINEAR, fillcolor=0.0)
    return np.asarray(im, dtype=np.float32)


def _compose(mask: np.ndarray, look: dict, rng: random.Random) -> np.ndarray:
    """Tint the mask onto a small row with a margin of background. The red
    and blue planes are offset from green by the look's measured aberration,
    converted from tile pixels to this render's native pixels (a tile maps
    ink+4 px of native width/height onto 28). Returns HxWx3 uint8."""
    m = rng.randint(4, 7)
    h, w = mask.shape
    canvas = np.zeros((h + 2 * m, w + 2 * m), np.float32)
    canvas[m:m + h, m:m + w] = mask
    sx, sy = (w + 4) / 28.0, (h + 4) / 28.0
    j = rng.uniform(0.8, 1.2)
    # _shift_of returns the roll that maps the channel ONTO green, so the
    # channel itself sits at minus that roll.
    rdx, rdy = look["r_shift"]
    bdx, bdy = look["b_shift"]
    chans = [_shifted(canvas, -rdx * sx * j, -rdy * sy * j), canvas,
             _shifted(canvas, -bdx * sx * j, -bdy * sy * j)]
    fg, bg = _jitter(look["fg"], rng), _jitter(look["bg"], rng, 0.85, 1.15)
    out = np.stack([bg[i] + (fg[i] - bg[i]) * chans[i] for i in range(3)], axis=-1)
    out += np.random.default_rng(rng.randrange(1 << 30)).normal(0, rng.uniform(0.5, 3.0), out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def _cut(row_rgb: np.ndarray, kind: str, rng: random.Random) -> Optional[np.ndarray]:
    """The real cut: grey = L + _canon_bright_text (as segment.glyphs_for),
    ink columns by the extractor's polarity + Otsu rule, then the extractor's
    own 28x28 cutter (which finds the ink rows itself)."""
    xlg = segment._helpers()
    gray = np.asarray(Image.fromarray(row_rgb, "RGB").convert("L"), dtype=np.uint8)
    gray = segment._canon_bright_text(gray)
    work = 255 - gray if np.median(gray) > 140 else gray
    cols = np.where((work > xlg._otsu(work)).any(axis=0))[0]
    if len(cols) == 0:
        return None
    # Real spans come from a column projection and sometimes keep one
    # column of air on either side.
    x1 = max(0, int(cols[0]) - rng.choice((0, 0, 1)))
    x2 = min(gray.shape[1], int(cols[-1]) + 1 + rng.choice((0, 0, 1)))
    if kinds.is_rgb(kind):
        return segment._rgb_helper()._glyph_to_28x28_rgb(row_rgb, gray, x1, x2)
    return xlg._glyph_to_28x28(gray, x1, x2)


def _digit_ratio(font_path) -> float:
    """Digit ink height per px of font size ('8' at 100 px)."""
    key = str(font_path)
    if key not in _DIGIT_RATIO:
        from PIL import ImageDraw, ImageFont  # noqa: WPS433
        try:
            font = ImageFont.truetype(key, 100)
        except OSError as exc:
            raise FontRenderError(f"cannot load font {font_path}: {exc}") from exc
        im = Image.new("L", (300, 300), 0)
        ImageDraw.Draw(im).text((50, 50), "8", font=font, fill=255)
        ys = np.where((np.asarray(im) > 128).any(axis=1))[0]
        _DIGIT_RATIO[key] = (int(ys[-1] - ys[0] + 1) / 100.0) if len(ys) else 0.7
    return _DIGIT_RATIO[key]


def render_tile(kind: str, char: str, font_path, rng: random.Random,
                looks: list[dict]) -> Optional[np.ndarray]:
    """One tile, or None if every attempt degenerated. ``looks`` empty is
    only legal for grey kinds (default bright-on-dark levels)."""
    fv = _fv()
    ratio = _digit_ratio(font_path)
    sizes = tuple(max(8, round(h / ratio)) for h in INK_PX[kinds.family(kind)])
    for attempt in range(_ATTEMPTS):
        # '.' is a few pixels of ink; lean to the large sizes after a miss.
        size = rng.choice(sizes[len(sizes) // 2:] if attempt and char == "." else sizes)
        try:
            arr = np.asarray(fv.render_variant(char, font_path, size, rng), dtype=np.uint8)
            mask = _mask(arr)
        except ValueError as exc:                    # font_variants: empty / degenerate render
            log.debug("devmode: font render %r at %dpx rejected: %s", char, size, exc)
            continue
        look = rng.choice(looks) if looks else _default_grey_look(rng)
        tile = _cut(_compose(mask, look, rng), kind, rng)
        if tile is None or segment.is_blacklisted(tile):
            continue
        return tile
    return None


def render_font_glyphs(kind: str, char: str, count: int, progress: Progress = None,
                       *, seed: Optional[int] = None) -> int:
    """Render ``count`` tiles of ``char`` for ``kind``'s pool from the game
    font; queue them PENDING with source "font". Returns how many were added."""
    kinds.check_kind(kind)
    classes = kinds.classes_for(kind)
    if not isinstance(char, str) or len(char) != 1 or char not in classes:
        raise ValueError(f"{char!r} is not a character class of {kind} ({classes!r})")
    if char == "@":
        raise ValueError("the location-pin icon is not a font character; collect it from captures")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count must be between 1 and {MAX_COUNT}")

    setting = region_font(kind)
    if setting["font"] is None:
        raise NoMatchingFontError(setting["reason"])
    fonts = available_fonts()
    plan = font_plan(count, setting["font"], fonts, setting["lookalike_share"])
    looks = colour_looks(kind)
    if kinds.is_rgb(kind):
        if not looks:
            raise NeedRealGlyphsError(
                f"{kind} is a colour kind and has no approved REAL glyphs yet, so the game's "
                "colours are unknown. Confirm a few captures, cut and approve a few real glyphs "
                "of any character in step 4, then render from the font.")

    rng = random.Random(seed if seed is not None else uuid.uuid4().int)
    rng.shuffle(plan)
    pl = kinds.pool(kind)
    out_dir = paths.sub("glyphs", pl, "font", kinds.class_dirname(char))
    added = failed = 0
    rows: list[tuple] = []

    def flush() -> None:
        if not rows:
            return
        with db.LOCK, db.connect() as con:
            con.executemany(
                "INSERT INTO glyphs (id, pool, capture_id, char, pos, file, status, created, source, font)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        rows.clear()

    for i, font_name in enumerate(plan):
        if progress and i % 10 == 0:
            progress(i / count, f"rendering {char!r} {i}/{count} ({font_name})")
        tile = render_tile(kind, char, fonts[font_name], rng, looks)
        if tile is None:
            failed += 1
            continue
        gid = uuid.uuid4().hex[:16]
        dest = out_dir / f"{gid}.png"
        Image.fromarray(tile).save(dest)
        rel = dest.relative_to(paths.dev_root()).as_posix()
        rows.append((gid, pl, "", char, 0, rel, "pending", time.time(), "font", fonts[font_name].name))
        added += 1
        if len(rows) >= 50:
            flush()
    flush()
    if failed:
        log.warning("devmode: %d of %d font renders of %r degenerated and were skipped", failed, count, char)
    if added == 0:
        raise FontRenderError(f"every render of {char!r} degenerated; nothing was added")
    if progress:
        progress(1.0, f"{added} font glyphs of {char!r} waiting for review")
    return added
