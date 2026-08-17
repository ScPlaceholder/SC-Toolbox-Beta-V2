"""Tests for the proportional signature segmenter.

Runs the new ``segment_signal_proportional`` against a curated set of
real region2 captures with known ground-truth values, and compares
its read accuracy + per-digit confidences against the legacy
``_segment_glyphs`` (column-projection) segmenter on the same
captures. Outputs a comparison table + summary the user can verify.

Run as a script:
    python -m hud_tracker.anchors.test_signal_proportional_segmenter

This is a script-style test (not a pytest module) because it depends
on real image fixtures and an ONNX CNN session that pytest's collect
phase shouldn't pull in. Each test case prints its result line and
the summary block at the end aggregates accuracy.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image


# Path bootstrap so we can import the segmenter when launched via
# ``python -m`` from the Mining_Signals tool root.
_THIS = Path(__file__).resolve()
_TOOL = _THIS.parent.parent.parent
if str(_TOOL) not in sys.path:
    sys.path.insert(0, str(_TOOL))

from hud_tracker.anchors.signal_proportional_segmenter import (  # noqa: E402
    segment_signal_proportional,
)

# Default region2 capture folder.
_REGION2_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels\user_20260418_154408\region2"
)


def _build_lexicon(region2_dir: Path) -> set[int]:
    """Build the lexicon of known signature values from sidecar JSONs."""
    lex: set[int] = set()
    for j in region2_dir.glob("*.json"):
        if j.name.endswith(".boxes.json"):
            continue
        try:
            d = json.loads(j.read_text())
            v = d.get("value", "")
            if v:
                lex.add(int(v.replace(",", "")))
        except Exception:
            continue
    return lex


def _load_runtime_classifier():
    """Load ``_classify_crops_signal`` from the api module.

    Returns ``None`` when the api can't import (e.g. running on a
    fresh checkout without the trained ONNX model). Tests still
    execute, but only the segmenter's geometric output is exercised.
    """
    try:
        from ocr.sc_ocr.api import _classify_crops_signal  # type: ignore
        return _classify_crops_signal
    except Exception:
        return None


def _load_legacy_segmenter():
    """Load runtime ``_segment_glyphs`` + helpers for comparison."""
    try:
        from ocr.sc_ocr.api import (  # type: ignore
            _segment_glyphs,
            _canonicalize_polarity,
            _adaptive_binarize_multi,
            _strip_pill_outline_bridges,
            _mask_commas_in_signature_band,
        )
        return {
            "segment_glyphs": _segment_glyphs,
            "canonicalize_polarity": _canonicalize_polarity,
            "adaptive_binarize_multi": _adaptive_binarize_multi,
            "strip_pill_outline_bridges": _strip_pill_outline_bridges,
            "mask_commas_in_signature_band": _mask_commas_in_signature_band,
        }
    except Exception:
        return None


def _run_legacy_segmenter(
    legacy_helpers: dict,
    pil_rgb: Image.Image,
    bbox_xyxy: tuple[int, int, int, int],
    classifier,
) -> tuple[str, list[float]]:
    """Run the legacy column-projection segmenter on the same value
    bbox the proportional segmenter sees and return ``(composed,
    per_digit_confs)``.
    """
    rgb_arr = np.asarray(pil_rgb.convert("RGB"), dtype=np.uint8)
    gray = rgb_arr.max(axis=2).astype(np.uint8)
    x1, y1, x2, y2 = bbox_xyxy
    crop = gray[y1:y2, x1:x2]
    canon = legacy_helpers["canonicalize_polarity"](crop)
    binary = legacy_helpers["adaptive_binarize_multi"](
        canon, expected_count=5,
    )
    binary = legacy_helpers["strip_pill_outline_bridges"](binary)
    binary = legacy_helpers["mask_commas_in_signature_band"](binary)
    crops, _boxes = legacy_helpers["segment_glyphs"](
        canon, binary, disable_gap_cut=True,
    )
    if classifier is None or not crops:
        return "", []
    results = classifier(crops)
    composed = "".join(
        cls if cls.isdigit() else "?" for cls, _ in results
    )
    confs = [float(c) for _, c in results]
    return composed, confs


# Test fixtures: (capture stem, ground-truth value). Curated to cover
# both 4-digit and 5-digit forms, multiple panel colors, and the two
# captures the user explicitly mentioned in the spec.
_FIXTURES: list[tuple[str, str]] = [
    ("cap_20260418_155500_306", "11,520"),  # user-cited 5-digit
    ("cap_20260425_094818_085", "3,400"),   # user-cited 4-digit
    # Diverse coverage from the unique-value list. We pick one capture
    # per ground-truth value to keep the suite small but broad.
]


def _augment_fixtures_from_directory(
    region2_dir: Path,
    out: list[tuple[str, str]],
    target_count: int = 12,
) -> None:
    """Fill ``out`` with one capture per unique GT value until
    ``target_count`` is reached. Skips values already in the list.
    """
    have_values = {gt for _, gt in out}
    for j in sorted(region2_dir.glob("*.json")):
        if len(out) >= target_count:
            return
        if j.name.endswith(".boxes.json"):
            continue
        try:
            d = json.loads(j.read_text())
        except Exception:
            continue
        v = d.get("value", "")
        if not v or v in have_values:
            continue
        boxes_path = j.with_name(j.stem + ".boxes.json")
        if not boxes_path.exists():
            continue
        out.append((j.stem, v))
        have_values.add(v)


def main() -> int:
    region2_dir = _REGION2_DIR
    if not region2_dir.is_dir():
        print(f"region2 dir not found: {region2_dir}")
        return 1

    fixtures = list(_FIXTURES)
    _augment_fixtures_from_directory(region2_dir, fixtures, target_count=12)

    lexicon = _build_lexicon(region2_dir)
    classifier = _load_runtime_classifier()
    legacy = _load_legacy_segmenter()

    print(f"Test set: {len(fixtures)} captures (region2_dir={region2_dir})")
    print(f"Lexicon: {len(lexicon)} known values")
    print(f"Classifier loaded: {classifier is not None}")
    print(f"Legacy segmenter loaded: {legacy is not None}")
    print()

    print(
        f"{'GT':>10}  {'PROP':>8}  {'PROP_OK':>7}  "
        f"{'PROP_CONF':>9}  {'LEGACY':>8}  {'LEGACY_OK':>9}  "
        f"{'CAPTURE':<35}"
    )
    print("-" * 100)

    n_prop_ok = 0
    n_legacy_ok = 0
    n_total = 0
    rows: list[tuple] = []

    for stem, gt_value in fixtures:
        png_path = region2_dir / (stem + ".png")
        boxes_path = region2_dir / (stem + ".boxes.json")
        if not (png_path.exists() and boxes_path.exists()):
            continue
        try:
            img = Image.open(png_path).convert("RGB")
            box = json.loads(boxes_path.read_text())["boxes"]["value"]
        except Exception as exc:
            print(f"  skip {stem}: {exc}")
            continue
        bbox = (
            int(box["x"]),
            int(box["y"]),
            int(box["x"]) + int(box["w"]),
            int(box["y"]) + int(box["h"]),
        )
        rgb_crop = img.crop(bbox)

        gt_digits = "".join(ch for ch in gt_value if ch.isdigit())

        # Proportional segmenter run.
        prop_result = segment_signal_proportional(
            rgb_crop,
            classifier=classifier,
            lexicon=lexicon if lexicon else None,
        )
        if prop_result is None:
            prop_composed = ""
            prop_score = 0.0
            prop_confs: list[float] = []
        else:
            prop_composed = prop_result["details"]["string_composed"]
            prop_score = float(prop_result["confidence"])
            prop_confs = [
                float(d.get("confidence", 0.0))
                for d in prop_result["digits"]
                if not d.get("is_comma")
            ]

        prop_ok = (prop_composed == gt_digits)
        if prop_ok:
            n_prop_ok += 1

        # Legacy segmenter run on the same bbox.
        if legacy is not None:
            legacy_composed, legacy_confs = _run_legacy_segmenter(
                legacy, img, bbox, classifier,
            )
        else:
            legacy_composed = ""
            legacy_confs = []
        legacy_ok = (legacy_composed == gt_digits)
        if legacy_ok:
            n_legacy_ok += 1

        n_total += 1
        print(
            f"{gt_value:>10}  {prop_composed:>8}  "
            f"{('YES' if prop_ok else 'no'):>7}  "
            f"{prop_score:>9.3f}  "
            f"{legacy_composed:>8}  "
            f"{('YES' if legacy_ok else 'no'):>9}  "
            f"{stem:<35}"
        )

        rows.append({
            "stem": stem,
            "gt_value": gt_value,
            "gt_digits": gt_digits,
            "prop_composed": prop_composed,
            "prop_ok": prop_ok,
            "prop_confs": prop_confs,
            "prop_score": prop_score,
            "legacy_composed": legacy_composed,
            "legacy_ok": legacy_ok,
            "legacy_confs": legacy_confs,
        })

    print()
    print("--- Summary ---------------------------------------------")
    print(
        f"Proportional:     {n_prop_ok}/{n_total} correct "
        f"({100 * n_prop_ok / max(1, n_total):.0f}%)"
    )
    print(
        f"Column-projection:{n_legacy_ok}/{n_total} correct "
        f"({100 * n_legacy_ok / max(1, n_total):.0f}%)"
    )

    # Per-position mean confidence for proportional reads.
    if rows:
        max_pos = max(len(r["prop_confs"]) for r in rows)
        print()
        print("Mean proportional CNN confidence per digit position:")
        for pos in range(max_pos):
            vals = [r["prop_confs"][pos] for r in rows
                    if pos < len(r["prop_confs"])]
            if vals:
                print(
                    f"  position {pos}: "
                    f"mean={sum(vals) / len(vals):.3f}  "
                    f"(n={len(vals)})"
                )

    # Specific-capture spotlight on the user-cited cases.
    print()
    print("User-cited captures:")
    for r in rows:
        if r["gt_value"] in ("11,520", "3,400"):
            print(
                f"  {r['gt_value']}: prop={r['prop_composed']!r} "
                f"(ok={r['prop_ok']})  "
                f"legacy={r['legacy_composed']!r} (ok={r['legacy_ok']})"
            )

    return 0 if n_prop_ok >= n_legacy_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
