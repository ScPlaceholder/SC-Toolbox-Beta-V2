"""Re-calibrate kerning_model.json using approved Glyph Forge sidecars.

Previous version (``calibrate_kerning.py``) measured per-slot offsets
from the legacy segmenter's bbox output -- noisy because the
segmenter's bboxes can be off by several pixels per tile. With only
n=14 width-uniform captures contributing, sigma per slot was ~0.16
row-heights.

This version reads per-tile positions directly from
``<capture>.glyphs.json`` sidecars produced by the new Glyph Forge --
positions verified by the user during the Row Reviewer pass. We
filter to ``review_status == "approved"`` so only user-confirmed
data feeds the calibration. With n=161 approved sidecars, sigma
should drop to ~0.04 row-heights -- tight enough to re-attempt the
kerning-locked layout in production.

Output: ``kerning_model.json`` (overwrites the old one, after backing
up to ``kerning_model_v1_backup.json``).

Slot indexing (relative to the comma):
    slot[0]  = leftmost digit, only present when n_digits == 5
    slot[1]  = digit immediately left of the comma (always present)
    slot[2]  = digit immediately right of the comma (always present)
    slot[3]  = next-right digit (always present)
    slot[4]  = rightmost digit (always present)
"""
from __future__ import annotations

import json
import logging
import shutil
import statistics
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

_THIS_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _THIS_DIR.parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))
if str(_TOOL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR / "scripts"))

from ocr.sc_ocr import api as _api  # noqa: E402, F401
import extract_labeled_glyphs as _xlg  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
for name in (
    "ocr.sc_ocr.api",
):
    logging.getLogger(name).setLevel(logging.ERROR)

# Sidecar sources -- both panel-root candidates so this works
# whether sidecars live in the WingmanAI tree or the production tree.
PANEL_ROOT_CANDIDATES = [
    Path(
        r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
        r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
        r"\training_data_panels"
    ),
    _TOOL_DIR / "training_data_panels",
]

OUTPUT_PATH = _THIS_DIR / "kerning_model.json"
BACKUP_PATH = _THIS_DIR / "kerning_model_v1_backup.json"


def _gather_approved_sidecars() -> list[tuple[Path, Path, dict]]:
    """Returns list of (sidecar_path, source_png, sidecar_doc)."""
    out: list[tuple[Path, Path, dict]] = []
    seen: set[Path] = set()
    for root in PANEL_ROOT_CANDIDATES:
        if not root.is_dir():
            continue
        for sc in sorted(root.rglob("*.glyphs.json")):
            real = sc.resolve()
            if real in seen:
                continue
            seen.add(real)
            try:
                doc = json.loads(sc.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(doc.get("review_status", "")).lower() != "approved":
                continue
            stem = sc.stem.replace(".glyphs", "")
            png = sc.parent / f"{stem}.png"
            if not png.is_file():
                continue
            out.append((sc, png, doc))
    return out


def _row_isolate_for_kerning(png: Path) -> Optional[dict]:
    """Replicate Glyph Forge's row-isolate so we measure in the SAME
    coordinate system the sidecar positions live in.

    Returns dict with ``gray_iso``, ``rgb_iso`` (the row-isolated
    arrays) plus ``row_height`` (gray_iso.shape[0]). Returns None on
    pipeline failure.
    """
    try:
        img_l = Image.open(png).convert("L")
        img_rgb = Image.open(png).convert("RGB")
    except Exception:
        return None
    gray_full = np.asarray(img_l, dtype=np.uint8)
    rgb_full = np.asarray(img_rgb, dtype=np.uint8)

    # Match Glyph Forge's preprocessing: icon-mask + row-isolate.
    bg = int(np.median(gray_full))
    gray = gray_full.copy()
    icon_right = _xlg._locate_icon_via_blacklist_match(gray)
    img_w = gray.shape[1]
    floor_mask = int(img_w * 0.30)
    mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
    if 0 < mask_w < img_w:
        gray[:, :mask_w] = bg

    band = _xlg._find_main_row_bounds(gray)
    if band is None:
        # No row band -- fall back to full image.
        return {
            "gray_iso": gray, "rgb_iso": rgb_full,
            "row_height": float(gray.shape[0]),
        }
    y1, y2 = band
    y1 = max(0, y1 - 2)
    y2 = min(gray.shape[0], y2 + 2)
    return {
        "gray_iso": gray[y1:y2, :],
        "rgb_iso": rgb_full[y1:y2, :, :],
        "row_height": float(y2 - y1),
    }


def _measure_one(
    sidecar_doc: dict,
    png: Path,
) -> Optional[dict]:
    """Extract per-slot offsets from one approved sidecar.

    Returns dict with ``slot_offsets`` (str_slot_idx → float offset
    in row-height units) and ``digit_widths`` (same keys → digit
    width in row-height units), OR None if the capture can't be
    processed (missing comma, bad row-isolate, etc.).
    """
    digits_only = "".join(c for c in str(sidecar_doc.get("label", "")) if c.isdigit())
    n_digits = len(digits_only)
    if n_digits not in (4, 5):
        return None

    norm = _row_isolate_for_kerning(png)
    if norm is None:
        return None
    row_h = float(norm["row_height"])
    if row_h <= 0:
        return None

    # Extract per-tile verified positions from the sidecar. We
    # only care about NON-SKIPPED tiles whose saved_class is a
    # digit -- those are the user-verified-as-real digit tiles.
    tiles = sidecar_doc.get("tiles") or []
    real_tiles: list[tuple[int, int]] = []
    for t in tiles:
        if t.get("skipped"):
            continue
        saved = t.get("saved_class")
        if saved is None or not str(saved).isdigit():
            continue
        try:
            x1 = int(t["x1"])
            x2 = int(t["x2"])
        except Exception:
            continue
        if x2 <= x1:
            continue
        real_tiles.append((x1, x2))
    if len(real_tiles) != n_digits:
        # Sidecar's saved digit count doesn't match the typed
        # label -- skip (Glyph Forge's structural-warn flagged
        # this kind of capture at save time, but the user could
        # still approve; we filter here for kerning purity).
        return None

    real_tiles.sort(key=lambda kv: kv[0])

    # Derive comma center DIRECTLY from the sidecar tiles instead of
    # running find_comma_voted on the row-isolated PNG. The original
    # implementation called find_comma_voted but it was designed for
    # tight crop_boxes (digit-value area only) and misfired on the
    # full PNG row -- returning fake comma positions in the icon
    # tail on ~15% of captures. The sidecar's tile positions are
    # user-verified, so the inter-tile gap between the pre-comma
    # and post-comma tiles is a more reliable comma anchor.
    #
    #   5-digit "DD,DDD": tiles = [d0, d1, d2, d3, d4]
    #                     comma sits between tile[1] (slot 1) and
    #                     tile[2] (slot 2)
    #   4-digit "D,DDD":  tiles = [d1, d2, d3, d4]
    #                     comma sits between tile[0] (slot 1) and
    #                     tile[1] (slot 2)
    comma_gap_left_idx = 1 if n_digits == 5 else 0
    comma_gap_right_idx = comma_gap_left_idx + 1
    if comma_gap_right_idx >= len(real_tiles):
        return None
    left_x2 = real_tiles[comma_gap_left_idx][1]
    right_x1 = real_tiles[comma_gap_right_idx][0]
    if right_x1 <= left_x2:
        # No real gap -- pre-comma and post-comma tiles overlap.
        # Probably an indication the tiles weren't properly split
        # around the comma; skip to keep calibration clean.
        return None
    comma_center = (float(left_x2) + float(right_x1)) * 0.5

    slot_offsets: dict[str, float] = {}
    digit_widths: dict[str, float] = {}
    base_slot = 0 if n_digits == 5 else 1
    for k, (bx1, bx2) in enumerate(real_tiles):
        slot_idx = base_slot + k
        center = (bx1 + bx2) * 0.5
        offset = (center - comma_center) / row_h
        slot_offsets[str(slot_idx)] = float(offset)
        digit_widths[str(slot_idx)] = float(bx2 - bx1) / row_h

    return {
        "stem": png.stem,
        "n_digits": n_digits,
        "row_height": row_h,
        "comma_center": comma_center,
        "slot_offsets": slot_offsets,
        "digit_widths": digit_widths,
    }


def main() -> int:
    items = _gather_approved_sidecars()
    print(f"Found {len(items)} approved sidecars")
    if not items:
        print("No approved sidecars to calibrate from. Aborting.")
        return 1

    samples: list[dict] = []
    n_skipped_pipeline = 0
    n_skipped_count_mismatch = 0
    for sc_path, png, doc in items:
        m = _measure_one(doc, png)
        if m is None:
            n_skipped_pipeline += 1
            continue
        samples.append(m)

    print(f"Used {len(samples)} of {len(items)} approved sidecars")
    print(f"  skipped (pipeline / count): {len(items) - len(samples)}")

    if not samples:
        print("No usable samples produced. Aborting.")
        return 1

    # Aggregate per slot.
    per_slot_offsets: dict[str, list[float]] = {str(i): [] for i in range(5)}
    per_slot_widths: dict[str, list[float]] = {str(i): [] for i in range(5)}
    n_digits_hist: dict[int, int] = {4: 0, 5: 0}
    for s in samples:
        n_digits_hist[s["n_digits"]] = n_digits_hist.get(s["n_digits"], 0) + 1
        for k, v in s["slot_offsets"].items():
            per_slot_offsets[k].append(v)
        for k, v in s["digit_widths"].items():
            per_slot_widths[k].append(v)

    print("\nDigit-count histogram:")
    for k, v in sorted(n_digits_hist.items()):
        print(f"  n_digits={k}: {v}")
    print("\nPer-slot sample counts:")
    for k in sorted(per_slot_offsets.keys()):
        print(f"  slot[{k}]: n={len(per_slot_offsets[k])}")

    slots_out: dict[str, dict[str, float]] = {}
    for k in sorted(per_slot_offsets.keys()):
        offs = per_slot_offsets[k]
        widths = per_slot_widths[k]
        if not offs:
            continue
        slots_out[k] = {
            "center_offset_mean": float(statistics.fmean(offs)),
            "center_offset_median": float(statistics.median(offs)),
            "center_offset_sigma": (
                float(statistics.stdev(offs)) if len(offs) >= 2 else 0.0
            ),
            "digit_w_mean": float(statistics.fmean(widths)) if widths else 0.0,
            "digit_w_median": (
                float(statistics.median(widths)) if widths else 0.0
            ),
            "n": len(offs),
        }

    # Back up old model before overwriting.
    if OUTPUT_PATH.is_file() and not BACKUP_PATH.is_file():
        shutil.copy2(OUTPUT_PATH, BACKUP_PATH)
        print(f"\nBacked up old kerning_model.json -> {BACKUP_PATH.name}")

    out_doc = {
        "schema": "kerning_v1",
        "units": "fraction-of-row-height",
        "calibration_source": "approved_sidecars",
        "n_sidecars_used": len(samples),
        "n_digits_histogram": n_digits_hist,
        "slots": slots_out,
        "comment": (
            "Re-calibrated from user-approved Glyph Forge sidecar JSONs. "
            "Positions are user-verified during the Row Reviewer pass. "
            "Sigma per slot should be substantially tighter than the "
            "previous version which measured from segmenter output."
        ),
    }

    OUTPUT_PATH.write_text(
        json.dumps(out_doc, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {OUTPUT_PATH}")
    print("\nPer-slot summary (offsets in row-height units):")
    for k in sorted(slots_out.keys()):
        info = slots_out[k]
        print(
            f"  slot[{k}]: mean={info['center_offset_mean']:+.3f} "
            f"median={info['center_offset_median']:+.3f} "
            f"sigma={info['center_offset_sigma']:.3f} "
            f"digit_w_median={info['digit_w_median']:.3f} "
            f"n={info['n']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
