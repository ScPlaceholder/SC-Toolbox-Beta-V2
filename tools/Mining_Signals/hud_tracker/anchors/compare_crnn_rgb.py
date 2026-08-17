"""End-to-end test: feed work_canon (or its RGB sibling) to the new
RGB CRNN, decode via greedy CTC, compare to GT and to production read.

Mirrors the bucket structure of ``compare_tesseract.py`` /
``compare_hybrid.py`` so we can directly compare CRNN vs Tesseract
vs production on the same 232-capture set.

Outputs ``hud_tracker/anchors/crnn_compare.csv`` + a console
histogram.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import onnxruntime as ort
from PIL import Image

_THIS_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _THIS_DIR.parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))
if str(_TOOL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR / "scripts"))

from ocr.sc_ocr import api as _api  # noqa: E402
from hud_tracker.anchors.icon_voter import localize_icon  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
for name in (
    "ocr.sc_ocr.api",
    "hud_tracker.anchors.icon_voter",
    "hud_tracker.anchors.icon_geometry",
    "hud_tracker.anchors.icon_contour",
    "hud_tracker.anchors.icon_rgb_ncc",
    "hud_tracker.anchors.signal_proportional_segmenter",
):
    logging.getLogger(name).setLevel(logging.ERROR)

PANEL_ROOT = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels"
)
CRNN_ONNX = _TOOL_DIR / "ocr" / "models" / "model_signal_crnn_rgb.onnx"
CRNN_META = _TOOL_DIR / "ocr" / "models" / "model_signal_crnn_rgb.json"
OUTPUT_CSV = _THIS_DIR / "crnn_compare.csv"

# Match training preprocessing exactly.
H_TARGET = 48


def _normalize_to_work_rgb(png_path: Path) -> Optional[np.ndarray]:
    """Replicate production crop_box + row-isolate path, then resize
    to H_TARGET via Lanczos preserving aspect. Returns RGB uint8
    array of shape (H_TARGET, W, 3) or ``None`` on pipeline failure.

    Polarity canonicalization is applied per-channel via the
    production ``_canonicalize_polarity`` so the model sees inputs
    in the same distribution it was trained on.
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

    work_rgb = rgb[digits_y1:digits_y2, digits_x1:digits_x2].copy()
    work_gray = gray[digits_y1:digits_y2, digits_x1:digits_x2].copy()

    # Row-isolate using the same band detector the training pipeline
    # used (xlg._find_main_row_bounds operates on gray).
    try:
        import extract_labeled_glyphs as xlg  # type: ignore
        band = xlg._find_main_row_bounds(work_gray) if hasattr(
            xlg, "_find_main_row_bounds"
        ) else None
    except Exception:
        band = None
    if band is not None:
        by1, by2 = band
        work_rgb = work_rgb[by1:by2, :, :]

    # Lanczos resize to H_TARGET preserving aspect.
    if work_rgb.shape[0] <= 0 or work_rgb.shape[1] <= 0:
        return None
    h0 = work_rgb.shape[0]
    if h0 != H_TARGET:
        scale = H_TARGET / h0
        new_w = max(8, int(round(work_rgb.shape[1] * scale)))
        pil = Image.fromarray(work_rgb, mode="RGB").resize(
            (new_w, H_TARGET), Image.LANCZOS,
        )
        work_rgb = np.asarray(pil, dtype=np.uint8)

    # Per-channel polarity-canonicalize (matches training).
    out = np.empty_like(work_rgb)
    for c in range(3):
        ch = work_rgb[..., c]
        out[..., c] = _api._canonicalize_polarity(ch)
    return out


def _decode_greedy_ctc(logits: np.ndarray, alphabet: str, blank: int) -> str:
    """Greedy CTC decode. ``logits`` shape (T, B, C). Returns the
    decoded string for batch index 0.
    """
    # log_softmax not strictly needed for argmax; just argmax over C.
    preds = logits[:, 0, :].argmax(axis=-1)  # (T,)
    out: list[str] = []
    prev = -1
    for p in preds.tolist():
        p = int(p)
        if p == prev:
            prev = p
            continue
        prev = p
        if p == blank:
            continue
        if 0 <= p < len(alphabet):
            out.append(alphabet[p])
    return "".join(out)


def _production_read(png_path: Path) -> Optional[str]:
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
    try:
        cache = json.loads(
            (_TOOL_DIR / ".mining_chart_cache.json").read_text(
                encoding="utf-8",
            )
        )
    except Exception:
        return 0
    md = cache.get("mining_data", {})
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
    return 0


def main() -> int:
    if not CRNN_ONNX.is_file():
        print(f"FATAL: CRNN ONNX not found at {CRNN_ONNX}")
        return 1
    meta = json.loads(CRNN_META.read_text(encoding="utf-8"))
    alphabet = meta["alphabet"]
    blank = int(meta["blank_idx"])
    print(f"CRNN model: {CRNN_ONNX.name}")
    print(f"  alphabet={alphabet!r} blank={blank}")
    print(f"  checkpoint val_acc={meta.get('checkpoint_val_acc'):.3f} "
          f"(epoch {meta.get('checkpoint_epoch')})")

    sess = ort.InferenceSession(
        str(CRNN_ONNX), providers=["CPUExecutionProvider"],
    )
    in_name = sess.get_inputs()[0].name

    n_lex = _maybe_load_lexicon()
    print(f"lexicon: {n_lex} known signature values")

    captures = sorted(PANEL_ROOT.glob("user_*/region2/*.png"))
    print(f"walking {len(captures)} captures...")

    rows: list[dict] = []
    bucket_counts = {
        "both_correct": 0, "ours_only": 0, "crnn_only": 0,
        "both_wrong": 0, "no_pipeline": 0,
    }

    for i, png in enumerate(captures):
        if png.with_suffix(".skip").exists():
            continue
        json_path = png.with_suffix(".json")
        if not json_path.exists():
            continue
        try:
            meta_j = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        gt_raw = str(meta_j.get("value", "")).strip()
        gt_digits = "".join(c for c in gt_raw if c.isdigit())
        if not gt_digits:
            continue

        try:
            _api._reset_consensus_buffers()
        except Exception:
            pass

        work_rgb = _normalize_to_work_rgb(png)
        if work_rgb is None:
            rows.append({
                "capture": png.name, "gt": gt_digits,
                "ours": "", "crnn": "", "bucket": "no_pipeline",
            })
            bucket_counts["no_pipeline"] += 1
            continue

        # CRNN forward.
        x = work_rgb.astype(np.float32).transpose(2, 0, 1)[None, ...] / 255.0
        try:
            logits = sess.run(None, {in_name: x})[0]
            crnn_raw = _decode_greedy_ctc(logits, alphabet, blank)
        except Exception as exc:
            crnn_raw = f"(err: {exc})"
        crnn_digits = "".join(c for c in crnn_raw if c.isdigit())

        ours = _production_read(png)
        o_ok = (ours is not None) and (ours == gt_digits)
        c_ok = (crnn_digits == gt_digits)
        if o_ok and c_ok:
            bucket = "both_correct"
        elif o_ok and not c_ok:
            bucket = "ours_only"
        elif c_ok and not o_ok:
            bucket = "crnn_only"
        else:
            bucket = "both_wrong"
        bucket_counts[bucket] += 1
        rows.append({
            "capture": png.name, "gt": gt_digits,
            "ours": ours or "", "crnn": crnn_digits,
            "crnn_raw": crnn_raw, "bucket": bucket,
        })

        if (i + 1) % 25 == 0:
            print(f"  [{i + 1}/{len(captures)}]")

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["capture", "gt", "ours", "crnn", "crnn_raw", "bucket"],
        )
        w.writeheader()
        w.writerows(rows)

    print()
    print("-" * 60)
    total = sum(bucket_counts.values())
    comparable = total - bucket_counts["no_pipeline"]
    print(f"  Total captures:           {total}")
    print(f"  No-pipeline (skipped):    {bucket_counts['no_pipeline']}")
    print(f"  Both correct:             {bucket_counts['both_correct']}")
    print(f"  Production correct only:  {bucket_counts['ours_only']}")
    print(f"  CRNN correct only:        {bucket_counts['crnn_only']}")
    print(f"  Both wrong:               {bucket_counts['both_wrong']}")
    print("-" * 60)
    if comparable:
        prod_ok = bucket_counts['both_correct'] + bucket_counts['ours_only']
        crnn_ok = bucket_counts['both_correct'] + bucket_counts['crnn_only']
        print(f"  Production accuracy:      {prod_ok}/{comparable} ({100*prod_ok/comparable:.1f}%)")
        print(f"  CRNN accuracy:            {crnn_ok}/{comparable} ({100*crnn_ok/comparable:.1f}%)")
    print(f"\nCSV: {OUTPUT_CSV}")

    print("\n  Sample of CRNN wins (production wrong, CRNN correct):")
    for r in [r for r in rows if r["bucket"] == "crnn_only"][:8]:
        print(f"    {r['capture']:<35} gt={r['gt']:<6} "
              f"ours={r['ours']!r:<10} crnn={r['crnn']!r}")
    print("\n  Sample of production wins (CRNN wrong, production correct):")
    for r in [r for r in rows if r["bucket"] == "ours_only"][:8]:
        print(f"    {r['capture']:<35} gt={r['gt']:<6} "
              f"ours={r['ours']!r:<10} crnn={r['crnn']!r}")
    print("\n  Sample of both-wrong:")
    for r in [r for r in rows if r["bucket"] == "both_wrong"][:8]:
        print(f"    {r['capture']:<35} gt={r['gt']:<6} "
              f"ours={r['ours']!r:<10} crnn={r['crnn']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
