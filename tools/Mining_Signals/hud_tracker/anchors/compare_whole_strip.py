"""Whole-strip CRNN A/B against the production pipeline on region2 captures.

Hypothesis under test: our per-glyph segmenter drops ~83% of captures.
Tesseract's whole-strip recovers 41.3% of those failures. Does our OWN
whole-strip CRNN -- already in production but rarely consulted -- recover
the same captures?

We feed the SAME ``work_canon`` strip the segmenter sees directly to
``_api._crnn_recognize`` (the multi-scale ensemble production wrapper)
with ``digit_only=True`` to mask non-digit classes from the CTC decoder.
No per-glyph segmentation.

Bucketing on the labeled region2 captures::

    both_correct      : production AND CRNN both match GT (digits-only)
    crnn_only         : only CRNN correct -- the wins this experiment
                        is looking for
    production_only   : only production correct
    both_wrong        : neither correct -- work_canon is too degraded
                        for any classifier to read

The size of "crnn_only" tells us the upper bound on the recovery
available from routing our own whole-strip CRNN past the segmenter.

Run::

    python hud_tracker/anchors/compare_whole_strip.py

Outputs ``hud_tracker/anchors/whole_strip_compare.csv`` + a console
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
from PIL import Image

# Path discovery + import gymnastics so this works from any cwd.
_THIS_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _THIS_DIR.parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))
if str(_TOOL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR / "scripts"))

from ocr.sc_ocr import api as _api  # noqa: E402
from hud_tracker.anchors.icon_voter import localize_icon  # noqa: E402

# Suppress production logger chatter; we want the CSV + histogram
# clean. ERROR/CRITICAL still surfaces.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
for _name in (
    "ocr.sc_ocr.api",
    "ocr.sc_ocr.signal_anchor",
    "ocr.sc_ocr.fallback",
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

OUTPUT_CSV = _THIS_DIR / "whole_strip_compare.csv"


def _normalize_to_work_canon(png_path: Path) -> Optional[np.ndarray]:
    """Replicate the production crop_box -> row-isolate -> polarity-
    canonicalize path so the CRNN sees the EXACT same pixels our
    segmenter does. Returns the canonical work crop (uint8 grayscale,
    bright glyphs on dark background) or ``None`` on any pipeline
    failure.

    Mirrors ``compare_tesseract.py`` so the two whole-strip A/B
    harnesses operate in the same coordinate space.
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


def _crnn_whole_strip_read(
    work_canon: np.ndarray,
) -> tuple[str, str, float]:
    """Feed work_canon to the production CRNN ensemble wrapper.

    ``_api._crnn_recognize`` is the function the production signal-value
    pipeline actually calls (api.py line 7963), so we mirror that exact
    call signature: PIL Image + ``digit_only=True``. The wrapper pulls
    its model input height (h_target=32) from the ONNX metadata
    (``fallback._crnn_input_height``) -- callers do not pass h_target.

    The CRNN's ``_crnn_decode`` body auto-inverts polarity if the
    input median > 140, so our bright-on-dark canonical is fine.

    Returns ``(raw_text, digits_only, mean_confidence)``. Empty strings
    + 0.0 mean either the model returned nothing or the digit filter
    consumed every char.
    """
    pil = Image.fromarray(work_canon, mode="L")
    try:
        out = _api._crnn_recognize(pil, digit_only=True)
    except Exception:
        return "", "", 0.0
    if out is None:
        return "", "", 0.0
    text, confs = out
    raw = text or ""
    digits = "".join(c for c in raw if c.isdigit())
    mean_conf = (sum(confs) / len(confs)) if confs else 0.0
    return raw, digits, mean_conf


def _production_read(png_path: Path) -> Optional[str]:
    """Run the full production OCR path and return the digits-only
    composed value, or ``None`` if the pipeline returns no read.

    IMPORTANT: caller must invoke ``_reset_consensus_buffers`` BEFORE
    each call. The api maintains per-capture temporal-vote state
    that leaks between adjacent captures otherwise.
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
    the caller -- depressing measured production accuracy.

    Verbatim from ``compare_tesseract.py``.
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
    gt_digits: str, prod: Optional[str], crnn: Optional[str],
) -> str:
    p_ok = (prod is not None) and (prod == gt_digits)
    c_ok = (crnn is not None) and (crnn == gt_digits)
    if p_ok and c_ok:
        return "both_correct"
    if p_ok and not c_ok:
        return "production_only"
    if c_ok and not p_ok:
        return "crnn_only"
    return "both_wrong"


def main() -> int:
    rows: list[dict[str, str]] = []
    bucket_counts: dict[str, int] = {
        "both_correct": 0, "crnn_only": 0,
        "production_only": 0, "both_wrong": 0,
    }
    n_seen = 0
    n_no_pipeline = 0

    n_lex = _maybe_load_lexicon()
    print(f"lexicon loaded: {n_lex} known signature values")

    # Touch the CRNN once to surface the h_target it picked up from
    # ONNX metadata, for log clarity. Production callers never pass
    # h_target to ``_crnn_recognize`` -- the wrapper reads it from
    # ``fallback._crnn_input_height``. We just print it.
    try:
        from ocr.sc_ocr import fallback as _fb
        if _fb._ensure_crnn_model():
            print(
                f"CRNN model ready: h_target="
                f"{int(_fb._crnn_input_height)} "
                f"(pulled from ONNX metadata; production wrapper "
                f"_crnn_recognize uses this value)"
            )
        else:
            print("CRNN model FAILED to load -- aborting.")
            return 2
    except Exception as exc:
        print(f"CRNN model load error: {exc}")
        return 2

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
        # temporal vote buffers don't leak. Mirrors the tesseract A/B.
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
                "production": "",
                "crnn_raw": "",
                "crnn_digits": "",
                "crnn_mean_conf": "",
                "bucket": "no_pipeline",
            })
            continue

        production = _production_read(png)
        crnn_raw, crnn_digits, crnn_mean = _crnn_whole_strip_read(work_canon)

        bucket = _bucket(gt_digits, production, crnn_digits or None)
        bucket_counts[bucket] += 1

        rows.append({
            "capture": png.name,
            "gt": gt_digits,
            "production": production or "",
            "crnn_raw": crnn_raw,
            "crnn_digits": crnn_digits,
            "crnn_mean_conf": f"{crnn_mean:.3f}",
            "bucket": bucket,
        })

        if (i + 1) % 25 == 0:
            print(f"  [{i + 1}/{len(captures)}]")

    # Write CSV.
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "capture", "gt", "production", "crnn_raw",
                "crnn_digits", "crnn_mean_conf", "bucket",
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
    print(f"  Production correct only: {bucket_counts['production_only']}")
    print(f"  CRNN correct only:       {bucket_counts['crnn_only']}")
    print(f"  Both wrong:              {bucket_counts['both_wrong']}")
    print("------------------------------------------------")
    if total_buckets:
        print(
            f"  Production accuracy:     "
            f"{(bucket_counts['both_correct'] + bucket_counts['production_only']) / total_buckets:.1%}"
        )
        print(
            f"  CRNN accuracy:           "
            f"{(bucket_counts['both_correct'] + bucket_counts['crnn_only']) / total_buckets:.1%}"
        )
    print(f"\nCSV: {OUTPUT_CSV}")

    # Up to 8 examples per "interesting" bucket.
    for bucket_name in ("crnn_only", "both_wrong", "production_only"):
        examples = [r for r in rows if r["bucket"] == bucket_name][:8]
        if examples:
            print(f"\n  Examples -- bucket={bucket_name!r}:")
            for ex in examples:
                print(
                    f"    {ex['capture']:<35} gt={ex['gt']:<6} "
                    f"prod={ex['production']!r:<10} "
                    f"crnn_digits={ex['crnn_digits']!r:<10} "
                    f"raw={ex['crnn_raw']!r:<14} "
                    f"conf={ex['crnn_mean_conf']}"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
