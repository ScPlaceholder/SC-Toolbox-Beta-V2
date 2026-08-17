"""Hybrid OCR experiment: Tesseract POSITIONS + our trained CNNs CLASSIFICATION.

Hypothesis under test: Tesseract's whole-strip column-projection finds
digit POSITIONS that our per-glyph segmenter misses, but our trained-on-
SC-font RGB / gray CNNs classify INDIVIDUAL digits more accurately than
Tesseract does (Tesseract's known SC-font failure: reads ``0`` as ``6``
or ``8`` in a substantial fraction of captures). So combining the two
should outperform either alone on the 232-capture region2 pool.

Pipeline per capture:

1. ``_normalize_to_work_canon_pair`` reproduces the production
   ``crop_box → row-isolate → polarity-canonicalize`` path AND keeps a
   parallel ``work_rgb`` aligned 1-to-1 with ``work_canon`` so per-
   bbox crops feed both CNN heads cleanly.

   DESIGN CHOICE: implemented as a SIBLING helper rather than modifying
   the existing ``_normalize_to_work_canon`` in ``compare_tesseract.py``.
   Rationale: keep the original A/B harness untouched so its prior
   results remain reproducible byte-for-byte, and so no upstream import
   is depending on a changed return type.

2. Run ``pytesseract.image_to_boxes`` on both polarities (light-padded
   inverse + dark-padded original) and pick whichever yields more
   digit-character boxes. Convert from Tesseract's bottom-left origin
   to top-left image coordinates. Filter to ``0123456789`` only.

3. For each Tesseract bbox: crop ``work_canon`` (gray) and
   ``work_rgb`` at the same coords; normalize per the production
   segmenter's ``_crop_to_28x28`` convention (gray: pad 2 with 255,
   bilinear to 28×28, /255 → float32 [0,1]; RGB: pad 2 with white,
   bilinear to 28×28, return uint8).

4. Classify each crop set with ``_classify_crops_signal`` (gray CNN)
   and ``_classify_crops_signal_rgb`` (RGB CNN v2). Compose left-to-
   right reads.

5. Bucket each capture::

       all_correct        : production AND tess_raw AND hybrid_gray
                            AND hybrid_rgb all match GT
       hybrid_recovers    : production wrong but hybrid_gray OR
                            hybrid_rgb correct (the win this experiment
                            is designed to surface)
       tess_only          : only tess_raw correct (Tesseract found the
                            digits AND classified them right; hybrid
                            and production both miss)
       production_only    : only production correct (regression watch)
       all_wrong          : none correct

Output: ``hud_tracker/anchors/hybrid_compare.csv`` + console summary.
No production code modified.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pytesseract
from PIL import Image

# Path discovery + import gymnastics so this works from any cwd.
_THIS_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _THIS_DIR.parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))
if str(_TOOL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR / "scripts"))

# Tesseract path: SCSI hardcoded "C:\Program Files\Tesseract-OCR".
# Mirror what compare_tesseract.py does.
_TESS_BIN = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if Path(_TESS_BIN).is_file():
    pytesseract.pytesseract.tesseract_cmd = _TESS_BIN

from ocr.sc_ocr import api as _api  # noqa: E402
from hud_tracker.anchors.icon_voter import localize_icon  # noqa: E402

# Suppress production logger chatter; we want the CSV + histogram clean.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
for _name in (
    "ocr.sc_ocr.api",
    "ocr.sc_ocr.signal_anchor",
    "hud_tracker.anchors.icon_geometry",
    "hud_tracker.anchors.icon_contour",
    "hud_tracker.anchors.icon_rgb_ncc",
    "hud_tracker.anchors.icon_voter",
    "hud_tracker.anchors.comma_finder",
    "hud_tracker.signal_proportional_segmenter",
):
    logging.getLogger(_name).setLevel(logging.ERROR)

PANEL_ROOT = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels"
)

OUTPUT_CSV = _THIS_DIR / "hybrid_compare.csv"

# Same digits-only PSM-7 line config as compare_tesseract.py — keeps
# results comparable across the two harnesses.
_TESS_CONFIG = (
    "--psm 7 "
    "-c tessedit_char_whitelist=0123456789,"
)


def _normalize_to_work_canon_pair(
    png_path: Path,
) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """Reproduce the production ``crop_box → row-isolate → upscale →
    polarity-canonicalize`` path and return BOTH the canonical gray
    work crop AND a spatially-aligned RGB work crop. Returns ``None``
    on any pipeline failure (anchor miss, world-model load, etc.).

    Mirrors the helper ``_normalize_to_work_canon`` in
    ``compare_tesseract.py`` line-for-line for the gray output, then
    threads the SAME slices + Lanczos upscale through a parallel RGB
    array so per-bbox crops on ``work_canon`` map 1-to-1 onto the RGB
    output. Matches the production ``_work_rgb`` derivation pattern in
    ``api._signal_recognize_pil``.
    """
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

    work = gray[digits_y1:digits_y2, digits_x1:digits_x2].copy()
    work_rgb = rgb[digits_y1:digits_y2, digits_x1:digits_x2].copy()

    # Row-isolate (bounds variant, so we can mirror trim onto RGB).
    try:
        import extract_labeled_glyphs as xlg  # type: ignore
        band = (
            xlg._find_main_row_bounds(work)
            if hasattr(xlg, "_find_main_row_bounds") else None
        )
    except Exception:
        band = None
    if band is not None:
        by1, by2 = band
        work = work[by1:by2, :]
        work_rgb = work_rgb[by1:by2, :]

    # Min-max contrast stretch on gray (matches production).
    w_arr = work.astype(np.float32)
    mn, mx = float(w_arr.min()), float(w_arr.max())
    if mx - mn > 8:
        w_arr = (w_arr - mn) * (255.0 / (mx - mn))
        work = np.clip(w_arr, 0, 255).astype(np.uint8)

    # Lanczos upscale to ~32 px tall on BOTH gray and RGB so coords
    # stay aligned. Same convention production uses.
    h_pre = work.shape[0]
    if h_pre < 28:
        scale_up = max(2, 32 // max(1, h_pre))
        work = np.asarray(
            Image.fromarray(work, mode="L").resize(
                (work.shape[1] * scale_up, h_pre * scale_up),
                Image.LANCZOS,
            ),
            dtype=np.uint8,
        )
        try:
            work_rgb = np.asarray(
                Image.fromarray(work_rgb, mode="RGB").resize(
                    (
                        work_rgb.shape[1] * scale_up,
                        work_rgb.shape[0] * scale_up,
                    ),
                    Image.LANCZOS,
                ),
                dtype=np.uint8,
            )
        except Exception:
            return None

    work_canon = _api._canonicalize_polarity(work)
    return work_canon, work_rgb


def _tess_boxes_pick_polarity(
    work_canon: np.ndarray,
) -> tuple[list[tuple[str, int, int, int, int]], str]:
    """Run ``image_to_boxes`` on both light-padded inverse and dark-
    padded original; return the (boxes, label) pair with more digit
    characters. Boxes are returned in IMAGE coordinates (top-left
    origin) AFTER stripping the pad we added before feeding Tesseract.

    Each box: ``(char, x_left, y_top, x_right, y_bottom)`` in pixels.
    """
    pad = 6

    def _prep_inverse() -> np.ndarray:
        inv = 255 - work_canon
        out = np.full(
            (inv.shape[0] + 2 * pad, inv.shape[1] + 2 * pad),
            255, dtype=np.uint8,
        )
        out[pad:pad + inv.shape[0], pad:pad + inv.shape[1]] = inv
        return out

    def _prep_original() -> np.ndarray:
        out = np.full(
            (work_canon.shape[0] + 2 * pad, work_canon.shape[1] + 2 * pad),
            0, dtype=np.uint8,
        )
        out[pad:pad + work_canon.shape[0],
            pad:pad + work_canon.shape[1]] = work_canon
        return out

    h_canon = work_canon.shape[0]

    def _run(arr: np.ndarray) -> list[tuple[str, int, int, int, int]]:
        try:
            raw = pytesseract.image_to_boxes(
                Image.fromarray(arr, mode="L"),
                config=_TESS_CONFIG,
            )
        except Exception:
            return []
        out: list[tuple[str, int, int, int, int]] = []
        # Each line: ``char left bottom right top page`` with bottom-
        # left origin. ``arr.shape[0]`` is the FULL padded image
        # height; flip Y, then subtract pad to get coords inside the
        # original work_canon.
        h_arr = arr.shape[0]
        for line in raw.splitlines():
            parts = line.strip().split(" ")
            if len(parts) < 5:
                continue
            ch = parts[0]
            if ch not in "0123456789":
                continue
            try:
                left = int(parts[1])
                bottom = int(parts[2])
                right = int(parts[3])
                top = int(parts[4])
            except ValueError:
                continue
            # Convert to top-left origin, then strip pad. Clamp into
            # work_canon.
            y_top_img = h_arr - top
            y_bot_img = h_arr - bottom
            x_left_img = left
            x_right_img = right
            x1 = max(0, x_left_img - pad)
            x2 = min(work_canon.shape[1], x_right_img - pad)
            y1 = max(0, y_top_img - pad)
            y2 = min(h_canon, y_bot_img - pad)
            if x2 <= x1 or y2 <= y1:
                continue
            out.append((ch, x1, y1, x2, y2))
        # Sort left-to-right.
        out.sort(key=lambda r: r[1])
        return out

    boxes_inv = _run(_prep_inverse())
    boxes_orig = _run(_prep_original())
    if len(boxes_inv) >= len(boxes_orig):
        return boxes_inv, "inv"
    return boxes_orig, "orig"


def _crop_gray_28(
    gray: np.ndarray, x1: int, y1: int, x2: int, y2: int,
) -> np.ndarray:
    """Mirror ``_crop_to_28x28`` from the production segmenter: pad
    with 255 (pad=2), bilinear-resize to 28×28, divide by 255 → float32
    [0,1]. Convention required by ``_classify_crops_signal``.
    """
    h_full, w_full = gray.shape[:2]
    x1 = max(0, min(x1, w_full - 1))
    y1 = max(0, min(y1, h_full - 1))
    x2 = max(x1 + 1, min(x2, w_full))
    y2 = max(y1 + 1, min(y2, h_full))
    crop = gray[y1:y2, x1:x2].astype(np.float32)
    pad = 2
    padded = np.full(
        (crop.shape[0] + pad * 2, crop.shape[1] + pad * 2),
        255.0, dtype=np.float32,
    )
    padded[pad:pad + crop.shape[0], pad:pad + crop.shape[1]] = crop
    pil = Image.fromarray(padded.astype(np.uint8)).resize(
        (28, 28), Image.BILINEAR,
    )
    return np.array(pil, dtype=np.float32) / 255.0


def _crop_rgb_28(
    rgb: np.ndarray, x1: int, y1: int, x2: int, y2: int,
) -> np.ndarray:
    """Mirror the production ``_hud_rgb_crops`` block: pad with 255
    (white) on all three channels (pad=2), bilinear-resize to 28×28,
    return uint8 (28, 28, 3). Convention required by
    ``_classify_crops_signal_rgb``.
    """
    h_full, w_full = rgb.shape[:2]
    x1 = max(0, min(x1, w_full - 1))
    y1 = max(0, min(y1, h_full - 1))
    x2 = max(x1 + 1, min(x2, w_full))
    y2 = max(y1 + 1, min(y2, h_full))
    crop = rgb[y1:y2, x1:x2].astype(np.float32)
    bh = crop.shape[0]
    bw = crop.shape[1]
    pad = 2
    padded = np.full(
        (bh + pad * 2, bw + pad * 2, 3),
        255.0, dtype=np.float32,
    )
    padded[pad:pad + bh, pad:pad + bw] = crop
    pil = Image.fromarray(padded.astype(np.uint8), mode="RGB").resize(
        (28, 28), Image.BILINEAR,
    )
    return np.asarray(pil, dtype=np.uint8)


def _production_read(png_path: Path) -> Optional[str]:
    """Run the full production OCR path. Caller MUST call
    ``_reset_consensus_buffers`` before this — same hygiene as
    ``compare_tesseract.py``. Returns digits-only string or ``None``.
    """
    img = Image.open(str(png_path)).convert("RGB")
    try:
        text = _api._signal_recognize_pil(img)
    except Exception:
        return None
    if text is None:
        return None
    digits = "".join(c for c in str(text) if c.isdigit())
    return digits or None


def _maybe_load_lexicon() -> int:
    """Load known signature values into the api lexicon BEFORE the
    walk, so production gate threshold falls from 0.85 → 0.65 on
    lexicon hits. Verbatim from ``compare_tesseract.py``: tries
    ``services.sheet_fetcher`` first, falls back to direct read of
    ``.mining_chart_cache.json``. Returns count loaded.
    """
    try:
        from services.sheet_fetcher import SheetFetcher  # type: ignore
        sf = SheetFetcher()
        res = sf.load(force_refresh=False)
        if getattr(res, "ok", False):
            known: set[int] = set()
            for r in res.data:
                for n in range(1, 21):
                    v = r.get(str(n), 0)
                    if v:
                        try:
                            known.add(int(v))
                        except (TypeError, ValueError):
                            pass
            if known:
                _api.set_known_signal_values(known)
                return len(known)
    except Exception:
        pass

    try:
        import json as _j
        cache_path = _TOOL_DIR / ".mining_chart_cache.json"
        if not cache_path.is_file():
            return 0
        data = _j.loads(cache_path.read_text(encoding="utf-8"))
        md = data.get("mining_data", {})
        elements = md.get("mineableElements", {}) or {}
        bases: set[int] = set()
        for elem in elements.values():
            for k in (
                "scanSignature", "groundScanSignature", "fpsScanSignature",
            ):
                v = elem.get(k)
                if v:
                    try:
                        bases.add(int(v))
                    except (TypeError, ValueError):
                        pass
        known: set[int] = set()
        for b in bases:
            for n in range(1, 26):
                known.add(b * n)
        if known:
            _api.set_known_signal_values(known)
            return len(known)
    except Exception:
        pass
    return 0


def _bucket(
    gt: str,
    production: Optional[str],
    tess_raw: Optional[str],
    hybrid_gray: Optional[str],
    hybrid_rgb: Optional[str],
) -> str:
    p_ok = (production is not None) and (production == gt)
    t_ok = (tess_raw is not None) and (tess_raw == gt)
    g_ok = (hybrid_gray is not None) and (hybrid_gray == gt)
    r_ok = (hybrid_rgb is not None) and (hybrid_rgb == gt)
    if p_ok and t_ok and g_ok and r_ok:
        return "all_correct"
    # The headline win: production missed, hybrid recovers.
    if (not p_ok) and (g_ok or r_ok):
        return "hybrid_recovers"
    if t_ok and not (p_ok or g_ok or r_ok):
        return "tess_only"
    if p_ok and not (t_ok or g_ok or r_ok):
        return "production_only"
    if not (p_ok or t_ok or g_ok or r_ok):
        return "all_wrong"
    # Mixed cases that don't fall into any bucket above (e.g.
    # production+hybrid both correct AND tess correct, or
    # production+tess correct but both hybrids wrong). Treat as
    # "all_correct"-class for any all-match, else "production_only"-
    # class for any production-correct mix, else "hybrid_recovers" for
    # any hybrid-correct mix. Falling through here is rare; we leave
    # an explicit catch-all.
    if p_ok and (g_ok or r_ok):
        return "all_correct"
    if g_ok or r_ok:
        return "hybrid_recovers"
    if p_ok:
        return "production_only"
    if t_ok:
        return "tess_only"
    return "all_wrong"


def main() -> int:
    rows: list[dict[str, str]] = []
    bucket_counts: dict[str, int] = {
        "all_correct": 0,
        "hybrid_recovers": 0,
        "tess_only": 0,
        "production_only": 0,
        "all_wrong": 0,
    }
    n_seen = 0
    n_no_pipeline = 0

    n_lex = _maybe_load_lexicon()
    print(f"lexicon loaded: {n_lex} known signature values")

    captures = sorted(PANEL_ROOT.glob("user_*/region2/*.png"))
    print(f"walking {len(captures)} captures...")

    for i, png in enumerate(captures):
        if png.with_suffix(".skip").exists():
            continue
        json_path = png.with_suffix(".json")
        if not json_path.exists():
            continue
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        gt_raw = str(meta.get("value", "")).strip()
        gt_digits = "".join(c for c in gt_raw if c.isdigit())
        if not gt_digits:
            continue

        n_seen += 1

        # Reset api state BEFORE the production read so per-capture
        # temporal vote buffers don't leak. Same hygiene as
        # compare_tesseract.py — without it every capture inherits
        # the first capture's STABLE_SIGNAL.
        try:
            _api._reset_consensus_buffers()
        except Exception:
            pass

        pair = _normalize_to_work_canon_pair(png)
        if pair is None:
            n_no_pipeline += 1
            rows.append({
                "capture": png.name,
                "gt": gt_digits,
                "production": "",
                "tess_raw": "",
                "hybrid_gray": "",
                "hybrid_rgb": "",
                "n_tess_chars": "0",
                "bucket": "no_pipeline",
            })
            continue
        work_canon, work_rgb = pair
        # Sanity: heights should match after the parallel Lanczos.
        if work_canon.shape[0] != work_rgb.shape[0] or \
           work_canon.shape[1] != work_rgb.shape[1]:
            # Treat shape mismatch as no-pipeline so we don't feed
            # mis-aligned crops to the RGB CNN.
            n_no_pipeline += 1
            rows.append({
                "capture": png.name,
                "gt": gt_digits,
                "production": "",
                "tess_raw": "",
                "hybrid_gray": "",
                "hybrid_rgb": "",
                "n_tess_chars": "0",
                "bucket": "no_pipeline",
            })
            continue

        production = _production_read(png)

        boxes, polarity = _tess_boxes_pick_polarity(work_canon)
        tess_raw = "".join(b[0] for b in boxes)

        if boxes:
            gray_crops = [
                _crop_gray_28(work_canon, x1, y1, x2, y2)
                for (_ch, x1, y1, x2, y2) in boxes
            ]
            rgb_crops = [
                _crop_rgb_28(work_rgb, x1, y1, x2, y2)
                for (_ch, x1, y1, x2, y2) in boxes
            ]
            gray_results = _api._classify_crops_signal(gray_crops)
            rgb_results = _api._classify_crops_signal_rgb(rgb_crops)

            def _compose(
                results: list[tuple[str, float]],
            ) -> str:
                if not results or len(results) != len(boxes):
                    return ""
                # Filter out non-digit predictions (model's @ class
                # would otherwise leak into the read).
                return "".join(
                    c for c, _ in results if c in "0123456789"
                )

            hybrid_gray = _compose(gray_results)
            hybrid_rgb = _compose(rgb_results)
        else:
            hybrid_gray = ""
            hybrid_rgb = ""

        bucket = _bucket(
            gt_digits,
            production,
            tess_raw or None,
            hybrid_gray or None,
            hybrid_rgb or None,
        )
        bucket_counts[bucket] += 1

        rows.append({
            "capture": png.name,
            "gt": gt_digits,
            "production": production or "",
            "tess_raw": tess_raw,
            "hybrid_gray": hybrid_gray,
            "hybrid_rgb": hybrid_rgb,
            "n_tess_chars": str(len(boxes)),
            "bucket": bucket,
        })

        if (i + 1) % 25 == 0:
            print(f"  [{i + 1}/{len(captures)}]")

    # Write CSV.
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "capture", "gt", "production", "tess_raw",
                "hybrid_gray", "hybrid_rgb", "n_tess_chars", "bucket",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    # Console summary.
    total = sum(bucket_counts.values())
    print("\n------------------------------------------------")
    print(f"  Total captures walked:    {n_seen}")
    print(f"  No-pipeline (skipped):    {n_no_pipeline}")
    print(f"  Comparable (bucketed):    {total}")
    print("------------------------------------------------")
    for k in (
        "all_correct", "hybrid_recovers",
        "tess_only", "production_only", "all_wrong",
    ):
        n = bucket_counts[k]
        pct = (n / total) if total else 0.0
        print(f"  {k:<20s}      {n:>4d}   {pct:>6.1%}")
    print("------------------------------------------------")
    if total:
        # Per-method overall accuracy (denominator = comparable rows).
        n_prod_correct = sum(
            1 for r in rows
            if r["bucket"] != "no_pipeline" and r["production"] == r["gt"]
        )
        n_tess_correct = sum(
            1 for r in rows
            if r["bucket"] != "no_pipeline" and r["tess_raw"] == r["gt"]
        )
        n_hg_correct = sum(
            1 for r in rows
            if r["bucket"] != "no_pipeline" and r["hybrid_gray"] == r["gt"]
        )
        n_hr_correct = sum(
            1 for r in rows
            if r["bucket"] != "no_pipeline" and r["hybrid_rgb"] == r["gt"]
        )
        print(f"  production accuracy:      "
              f"{n_prod_correct}/{total} = {n_prod_correct / total:.1%}")
        print(f"  hybrid_gray accuracy:     "
              f"{n_hg_correct}/{total} = {n_hg_correct / total:.1%}")
        print(f"  hybrid_rgb accuracy:      "
              f"{n_hr_correct}/{total} = {n_hr_correct / total:.1%}")
        print(f"  tess_raw accuracy:        "
              f"{n_tess_correct}/{total} = {n_tess_correct / total:.1%}")
    print(f"\nCSV: {OUTPUT_CSV}")

    # Up to 8 examples per "interesting" bucket.
    for bucket_name in (
        "hybrid_recovers", "production_only", "tess_only", "all_wrong",
    ):
        examples = [r for r in rows if r["bucket"] == bucket_name][:8]
        if examples:
            print(f"\n  Examples -- bucket={bucket_name!r}:")
            for ex in examples:
                print(
                    f"    {ex['capture']:<35} "
                    f"gt={ex['gt']:<6} "
                    f"prod={ex['production']!r:<8} "
                    f"tess={ex['tess_raw']!r:<8} "
                    f"hg={ex['hybrid_gray']!r:<8} "
                    f"hr={ex['hybrid_rgb']!r:<8}"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
