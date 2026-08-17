"""Save side-by-side visualizations of the production-vs-compare-script
CRNN input bytes for each of the 9 originally-failing captures.

Layout of each saved PNG (top to bottom):
  1. PRE-comma-extension crop (compare-script's _work_rgb base)
  2. PRE-comma-extension crop after row-isolate
  3. POST-comma-extension crop (production's _work_rgb base)
  4. POST-comma-extension crop after row-isolate
  5. POST + 2x Lanczos upscale (what production fed CRNN before fix)
"""
from __future__ import annotations

import sys
import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_THIS_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _THIS_DIR.parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))
if str(_TOOL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR / "scripts"))

logging.basicConfig(level=logging.WARNING)
for name in (
    "ocr.sc_ocr.api",
    "hud_tracker.anchors.icon_voter",
    "hud_tracker.anchors.icon_geometry",
    "hud_tracker.anchors.icon_contour",
    "hud_tracker.anchors.icon_rgb_ncc",
    "hud_tracker.anchors.signal_proportional_segmenter",
):
    logging.getLogger(name).setLevel(logging.ERROR)

from ocr.sc_ocr import api as _api  # noqa: E402
from hud_tracker.anchors.icon_voter import localize_icon  # noqa: E402

VIZ_DIR = _THIS_DIR / "preprocessing_divergence_viz"
VIZ_DIR.mkdir(exist_ok=True)


def _label_strip(text, width, height=16):
    img = Image.new("RGB", (width, height), color=(20, 20, 20))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 12)
    except Exception:
        font = ImageFont.load_default()
    draw.text((4, 1), text, fill=(220, 220, 80), font=font)
    return np.asarray(img, dtype=np.uint8)


def _pad_to_width(arr, target_w):
    if arr.shape[1] >= target_w:
        return arr
    pad = target_w - arr.shape[1]
    return np.pad(arr, ((0, 0), (0, pad), (0, 0)),
                  mode="constant", constant_values=0)


def _stack_vertical(rows, label_height=16, gap=2):
    max_w = max(r.shape[1] for r in rows if r is not None)
    out = []
    for r in rows:
        if r is None:
            continue
        out.append(_pad_to_width(r, max_w))
        out.append(np.full((gap, max_w, 3), 64, dtype=np.uint8))
    return np.concatenate(out[:-1], axis=0)


def _do_capture(png_path: Path, gt: str):
    img = Image.open(str(png_path)).convert("RGB")
    rgb = np.asarray(img, dtype=np.uint8)
    gray = rgb.max(axis=2).astype(np.uint8)

    wmr = _api._load_region2_world_model_for_api()
    if wmr is None:
        return None
    vfrac = (wmr.get("features") or {}).get("value")
    if vfrac is None:
        return None
    pill = _api._find_pill_for_signal(rgb)
    if pill is None:
        return None
    px, py, pw, ph = pill
    vx = int(round(px + float(vfrac["x_frac"]["mean"]) * pw))
    vy = int(round(py + float(vfrac["y_frac"]["mean"]) * ph))
    vw = int(round(float(vfrac["w_frac"]["mean"]) * pw))
    vh = int(round(float(vfrac["h_frac"]["mean"]) * ph))

    icon_loc = localize_icon(rgb)
    if icon_loc is None:
        return None
    ix, iy, iw, ih = icon_loc["bbox"]
    icon_anchor = ix + iw + max(2, int(pw * 0.03))
    delta = vx - icon_anchor
    vx = icon_anchor
    vw = vw + delta
    rhs_ceiling = px + pw - max(2, int(pw * 0.05))
    digits_x2 = min(vx + vw, rhs_ceiling, gray.shape[1])
    digits_x1 = max(0, vx)
    digits_y1 = max(0, vy)
    digits_y2 = min(vy + vh, gray.shape[0])
    if digits_x2 <= digits_x1 or digits_y2 <= digits_y1:
        return None
    pre_box = (digits_x1, digits_y1, digits_x2, digits_y2)
    pre_rgb = rgb[digits_y1:digits_y2, digits_x1:digits_x2].copy()
    pre_gray = gray[digits_y1:digits_y2, digits_x1:digits_x2].copy()

    # Comma-anchored extension as production runs it.
    post_box = pre_box
    try:
        from hud_tracker.anchors.comma_finder import find_comma_voted
        comma = find_comma_voted(pre_rgb)
        if comma is not None:
            cxc = int(comma["x_center"])
            tw = int(pre_rgb.shape[1])
            rw = tw - cxc
            if rw >= 12:
                pitch = rw / 3.5
                lx5 = cxc - 2 * pitch
                lx4 = cxc - pitch
                min_lx = min(lx4, lx5)
                if min_lx < 4:
                    ext = max(0, int(round(4 - min_lx)))
                    new_x1 = max(0, digits_x1 - ext)
                    if new_x1 < digits_x1:
                        post_box = (new_x1, digits_y1, digits_x2, digits_y2)
    except Exception:
        pass
    pe_x1, pe_y1, pe_x2, pe_y2 = post_box
    post_rgb = rgb[pe_y1:pe_y2, pe_x1:pe_x2].copy()
    post_gray = gray[pe_y1:pe_y2, pe_x1:pe_x2].copy()

    try:
        import extract_labeled_glyphs as xlg  # type: ignore
        pre_band = (
            xlg._find_main_row_bounds(pre_gray)
            if hasattr(xlg, "_find_main_row_bounds") else None
        )
        post_band = (
            xlg._find_main_row_bounds(post_gray)
            if hasattr(xlg, "_find_main_row_bounds") else None
        )
    except Exception:
        pre_band = None
        post_band = None
    if pre_band is not None:
        a, b = pre_band
        pre_iso = pre_rgb[a:b, :, :]
    else:
        pre_iso = pre_rgb
    if post_band is not None:
        a, b = post_band
        post_iso = post_rgb[a:b, :, :]
    else:
        post_iso = post_rgb

    # The 2x Lanczos upscale production used to do.
    h_pre_iso = post_iso.shape[0]
    if h_pre_iso < 28:
        scale = max(2, 32 // max(1, h_pre_iso))
        pil = Image.fromarray(post_iso, mode="RGB").resize(
            (post_iso.shape[1] * scale, post_iso.shape[0] * scale),
            Image.LANCZOS,
        )
        upscaled = np.asarray(pil, dtype=np.uint8)
    else:
        upscaled = post_iso

    extended_px = (pe_x2 - pe_x1) - (digits_x2 - digits_x1)

    rows = [
        _label_strip(
            f"{png_path.name} gt={gt}", 800, height=18,
        ),
        _label_strip(
            f"[A] PRE-extension crop ({pre_box}) — fed to CRNN AFTER fix",
            800,
        ),
        pre_rgb,
        _label_strip("[B] PRE-extension + row-isolate", 800),
        pre_iso,
        _label_strip(
            f"[C] POST-extension crop ({post_box}, +{extended_px}px) — "
            f"fed to CRNN BEFORE fix",
            800,
        ),
        post_rgb,
        _label_strip("[D] POST-extension + row-isolate", 800),
        post_iso,
        _label_strip(
            f"[E] POST + 2x Lanczos upscale (production _work_rgb) — "
            f"what CRNN saw BEFORE fix",
            800,
        ),
        upscaled,
    ]
    viz = _stack_vertical(rows)
    out_path = VIZ_DIR / f"{png_path.stem}_compare.png"
    Image.fromarray(viz, mode="RGB").save(str(out_path))
    print(f"saved {out_path.name}  pre_box={pre_box} post_box={post_box} ext={extended_px}px")


def main(captures):
    for png_path in captures:
        png_path = Path(png_path)
        if not png_path.exists():
            print(f"NOT FOUND: {png_path.name}")
            continue
        gt_path = png_path.with_suffix(".json")
        gt = "?"
        if gt_path.exists():
            try:
                gt = json.loads(gt_path.read_text(encoding="utf-8")).get(
                    "value", "?"
                )
            except Exception:
                pass
        _do_capture(png_path, gt)


if __name__ == "__main__":
    PANEL_ROOT = Path(
        r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
        r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
        r"\training_data_panels"
    )
    captures = [
        PANEL_ROOT / "user_20260418_081525" / "region2" / "cap_20260418_085619_138.png",
        PANEL_ROOT / "user_20260418_081525" / "region2" / "cap_20260418_085649_075.png",
        PANEL_ROOT / "user_20260418_081525" / "region2" / "cap_20260418_085716_872.png",
        PANEL_ROOT / "user_20260418_081525" / "region2" / "cap_20260418_085745_646.png",
        PANEL_ROOT / "user_20260418_154408" / "region2" / "cap_20260425_094842_773.png",
        PANEL_ROOT / "user_20260418_154408" / "region2" / "cap_20260425_095113_320.png",
        PANEL_ROOT / "user_20260418_154408" / "region2" / "cap_20260425_095139_966.png",
        PANEL_ROOT / "user_20260418_154408" / "region2" / "cap_20260425_134947_211.png",
        PANEL_ROOT / "user_20260418_154408" / "region2" / "cap_20260425_135120_544.png",
    ]
    main(captures)
