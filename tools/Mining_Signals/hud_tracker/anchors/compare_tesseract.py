"""Tesseract A/B against the production pipeline on region2 captures.

Hypothesis under test: our per-glyph segmenter is throwing away ink the
classifier would otherwise read correctly. Counter-test: feed the SAME
``work_canon`` strip our segmenter sees directly to Tesseract (no per-
glyph segmentation) and compare its read to ours.

Bucketing on the 106 labeled region2 captures::

    Both correct          : we agree, baseline noise floor
    Ours correct, T wrong : RGB CNN beats Tesseract on a clean crop
    Ours wrong, T correct : Tesseract found a digit our segmenter dropped
                            ⟶ segmenter / per-glyph filtering is the
                              bottleneck; the crop itself is fine
    Both wrong            : work_canon itself is bad; problem is upstream
                            of segmentation (crop_box / polarity / row-
                            isolate / occlusion / capture quality)

The size of "Ours wrong, T correct" tells us the upper bound on the
recovery available from segmenter relaxation. The size of "Both wrong"
tells us how much of our failure rate isn't reachable from any
classifier swap — those captures need upstream fixes.

Run::

    python hud_tracker/anchors/compare_tesseract.py

Outputs ``hud_tracker/anchors/tesseract_compare.csv`` + a console
histogram. No production code is modified.
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
# Mirror that — the user already has it installed.
_TESS_BIN = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if Path(_TESS_BIN).is_file():
    pytesseract.pytesseract.tesseract_cmd = _TESS_BIN

from ocr.sc_ocr import api as _api  # noqa: E402
from hud_tracker.anchors.icon_voter import localize_icon  # noqa: E402

# Suppress production logger chatter; we want the CSV + histogram
# clean. ERROR/CRITICAL still surfaces.
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

OUTPUT_CSV = _THIS_DIR / "tesseract_compare.csv"

# Tesseract config: single text line, digit + comma whitelist. PSM 7 is
# "treat the image as a single text line" — ideal for our work_canon
# which is a one-row signature value crop.
_TESS_CONFIG = (
    "--psm 7 "
    "-c tessedit_char_whitelist=0123456789,"
)


def _normalize_to_work_canon(png_path: Path) -> Optional[np.ndarray]:
    """Replicate the production crop_box → row-isolate → polarity-
    canonicalize path so Tesseract sees the EXACT same pixels our
    segmenter does. Returns the canonical work crop (uint8 grayscale,
    bright glyphs on dark background) or ``None`` on any pipeline
    failure.

    Mirrors the path in determinism_check.py + calibrate_kerning.py
    so all three diagnostic harnesses operate in the same coordinate
    space the production segmenter does.
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

    try:
        import extract_labeled_glyphs as xlg  # type: ignore
        band = xlg._find_main_row_bounds(work) if hasattr(
            xlg, "_find_main_row_bounds"
        ) else None
    except Exception:
        band = None
    if band is not None:
        by1, by2 = band
        work = work[by1:by2, :]

    w_arr = work.astype(np.float32)
    mn, mx = float(w_arr.min()), float(w_arr.max())
    if mx - mn > 8:
        w_arr = (w_arr - mn) * (255.0 / (mx - mn))
        work = np.clip(w_arr, 0, 255).astype(np.uint8)

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

    return _api._canonicalize_polarity(work)


def _tesseract_read_both_polarities(
    work_canon: np.ndarray,
) -> tuple[str, str]:
    """Run Tesseract on the bright-on-dark canonical AND its inverse.

    Tesseract is trained predominantly on dark-on-light text; our
    canonical is bright-on-dark. Feeding both polarities and taking
    the longer / cleaner read avoids polarity penalizing the OCR.

    Returns ``(raw_dark_on_light, raw_bright_on_dark)`` after digit-
    only filtering. Caller picks the more useful one.
    """
    # Tesseract's preference: dark text on light bg. Our work_canon is
    # bright-on-dark (white digits on dark pill bg). Invert for one
    # of the two reads.
    inverse = 255 - work_canon

    # Pad with a few pixels of bg so the digits aren't flush against
    # the image edge — Tesseract's adaptive thresholding likes a
    # margin. Pad with 255 (light) for the inverse and 0 (dark) for
    # the original, matching the dominant background colour each.
    pad = 6
    inv_padded = np.full(
        (inverse.shape[0] + 2 * pad, inverse.shape[1] + 2 * pad),
        255, dtype=np.uint8,
    )
    inv_padded[pad:pad + inverse.shape[0], pad:pad + inverse.shape[1]] = inverse

    bod_padded = np.full(
        (work_canon.shape[0] + 2 * pad, work_canon.shape[1] + 2 * pad),
        0, dtype=np.uint8,
    )
    bod_padded[pad:pad + work_canon.shape[0], pad:pad + work_canon.shape[1]] = work_canon

    def _run(arr: np.ndarray) -> str:
        try:
            raw = pytesseract.image_to_string(
                Image.fromarray(arr, mode="L"),
                config=_TESS_CONFIG,
            )
        except Exception:
            return ""
        # Strip whitespace + commas to digits-only; matches what we'd
        # do at integration time anyway.
        digits = "".join(c for c in raw if c.isdigit())
        return digits

    return _run(inv_padded), _run(bod_padded)


def _production_read(png_path: Path) -> Optional[str]:
    """Run the full production OCR path and return the digits-only
    composed value, or ``None`` if the pipeline returns no read.

    IMPORTANT: caller must invoke ``_reset_consensus_buffers`` BEFORE
    each call. The api maintains per-capture temporal-vote state
    that leaks between adjacent captures otherwise, producing the
    "everyone reads as 7780" pattern observed without the reset.
    Mirrors the profiler's per-capture state hygiene.
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
    """Feed the mining chart's known signature values into the api's
    lexicon BEFORE the loop runs. Without the lexicon, the production
    gate threshold is 0.85 (vs 0.65 with a lexicon hit), and many
    runtime-correct reads get rejected at the gate and never reach
    the caller — depressing measured "ours" accuracy on this harness.

    Two paths tried in order:
      1. ``services.sheet_fetcher.SheetFetcher`` (the path the profiler
         uses) — fails outside the production process because it pulls
         in ``shared`` modules that aren't on this script's import
         path.
      2. Read ``.mining_chart_cache.json`` directly and enumerate
         per-element ``scanSignature`` × {1..20} multipliers as the
         lexicon. Equivalent semantics; doesn't need ``shared``.

    Returns the number of values loaded.
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

    # Fallback: parse the chart cache directly.
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
            for k in ("scanSignature", "groundScanSignature", "fpsScanSignature"):
                v = elem.get(k)
                if v:
                    try:
                        bases.add(int(v))
                    except (TypeError, ValueError):
                        pass
        # Each base × N nodes (1..20) is a valid signal value.
        # Production typically caps at ~20 nodes per cluster; 25 is
        # generous insurance against off-by-one.
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
    gt_digits: str, ours: Optional[str], tess: Optional[str],
) -> str:
    o_ok = (ours is not None) and (ours == gt_digits)
    t_ok = (tess is not None) and (tess == gt_digits)
    if o_ok and t_ok:
        return "both_correct"
    if o_ok and not t_ok:
        return "ours_only"
    if t_ok and not o_ok:
        return "tess_only"
    return "both_wrong"


def main() -> int:
    rows: list[dict[str, str]] = []
    bucket_counts: dict[str, int] = {
        "both_correct": 0, "ours_only": 0,
        "tess_only": 0, "both_wrong": 0,
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
        # temporal vote buffers don't leak. Profiler does this too
        # (``_reset_state``); without it, every capture adopts the
        # first capture's STABLE_SIGNAL and you get the misleading
        # "everyone reads as 7780" output we saw on the first run.
        try:
            _api._reset_consensus_buffers()
        except Exception:
            pass

        work_canon = _normalize_to_work_canon(png)
        if work_canon is None:
            n_no_pipeline += 1
            rows.append({
                "capture": png.name,
                "gt": gt_digits,
                "ours": "",
                "tess_dol": "",
                "tess_bod": "",
                "tess_best": "",
                "bucket": "no_pipeline",
            })
            continue

        ours = _production_read(png)
        tess_dol, tess_bod = _tesseract_read_both_polarities(work_canon)

        # Best Tesseract read = whichever matches the GT length, else
        # whichever is longer, else either. This prevents truncated
        # reads from one polarity from disqualifying the candidate.
        candidates = [c for c in (tess_dol, tess_bod) if c]

        def _score(s: str) -> tuple[int, int]:
            # Preferred: matching GT length AND being a known signal
            # value or matching exactly. Fallback: matching length.
            len_match = 1 if len(s) == len(gt_digits) else 0
            return (len_match, len(s))

        if candidates:
            tess_best = max(candidates, key=_score)
        else:
            tess_best = ""

        bucket = _bucket(gt_digits, ours, tess_best or None)
        bucket_counts[bucket] += 1

        rows.append({
            "capture": png.name,
            "gt": gt_digits,
            "ours": ours or "",
            "tess_dol": tess_dol,
            "tess_bod": tess_bod,
            "tess_best": tess_best,
            "bucket": bucket,
        })

        if (i + 1) % 25 == 0:
            print(f"  [{i + 1}/{len(captures)}]")

    # Write CSV.
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "capture", "gt", "ours", "tess_dol", "tess_bod",
                "tess_best", "bucket",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    # Console summary.
    total_buckets = sum(bucket_counts.values())
    print("\n------------------------------------------------")
    print(f"  Total captures walked:   {n_seen}")
    print(f"  No-pipeline (skipped):   {n_no_pipeline}")
    print(f"  Both correct:            {bucket_counts['both_correct']}")
    print(f"  Ours correct only:       {bucket_counts['ours_only']}")
    print(f"  Tesseract correct only:  {bucket_counts['tess_only']}")
    print(f"  Both wrong:              {bucket_counts['both_wrong']}")
    print("------------------------------------------------")
    if total_buckets:
        print(
            f"  Production accuracy:     "
            f"{(bucket_counts['both_correct'] + bucket_counts['ours_only']) / total_buckets:.1%}"
        )
        print(
            f"  Tesseract accuracy:      "
            f"{(bucket_counts['both_correct'] + bucket_counts['tess_only']) / total_buckets:.1%}"
        )
    print(f"\nCSV: {OUTPUT_CSV}")

    # First 12 examples per "interesting" bucket.
    for bucket_name in ("tess_only", "both_wrong", "ours_only"):
        examples = [r for r in rows if r["bucket"] == bucket_name][:12]
        if examples:
            print(f"\n  Examples — bucket={bucket_name!r}:")
            for ex in examples:
                print(
                    f"    {ex['capture']:<35} gt={ex['gt']:<6} "
                    f"ours={ex['ours']!r:<10} "
                    f"tess_dol={ex['tess_dol']!r:<10} "
                    f"tess_bod={ex['tess_bod']!r:<10}"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
