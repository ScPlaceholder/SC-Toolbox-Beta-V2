"""World-model proportions extractor for the REGION 2 signature pill.

Reads every *.boxes.json under SOURCE_DIR, treats each capture's ``pill``
box as the panel reference frame, and computes fractional coordinates
(x_frac, y_frac, w_frac, h_frac) for every other labeled feature
(``icon``, ``value``).

Aggregates mean + std across all captures for each (feature, coordinate)
pair and writes the result to OUT_PATH so the value-bbox detector in
``auto_template_annotator.py`` (and the production runtime in
``ocr/sc_ocr/api.py``) can derive the digit area directly from
pill + icon proportions instead of glyph NCC matching.

Re-runnable: re-reads SOURCE_DIR and overwrites OUT_PATH on each
invocation.

Usage:
    python proportions_region2.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

SOURCE_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals\training_data_panels"
    r"\user_20260418_154408\region2"
)
OUT_PATH = Path(
    r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals"
    r"\hud_tracker\world_model_region2.json"
)
COORDS = ("x_frac", "y_frac", "w_frac", "h_frac")
REFERENCE = "pill"


def load_captures(source_dir: Path) -> List[Dict[str, Any]]:
    """Read every *.boxes.json file as {'name': stem, 'boxes': {...}}."""
    captures: List[Dict[str, Any]] = []
    for path in sorted(source_dir.glob("*.boxes.json")):
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        captures.append({
            "name": path.name,
            "image": data.get("image"),
            "boxes": data.get("boxes") or {},
        })
    return captures


def fractionalize(boxes: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """Express every non-reference box as a fraction of the reference (pill).

    Returns {} if the reference is missing or has zero width/height.
    """
    ref = boxes.get(REFERENCE)
    if not ref:
        return {}
    rw, rh = ref.get("w") or 0, ref.get("h") or 0
    if rw <= 0 or rh <= 0:
        return {}
    rx, ry = ref["x"], ref["y"]

    out: Dict[str, Dict[str, float]] = {}
    for name, box in boxes.items():
        if name == REFERENCE:
            out[name] = {"x_frac": 0.0, "y_frac": 0.0, "w_frac": 1.0, "h_frac": 1.0}
            continue
        try:
            out[name] = {
                "x_frac": (box["x"] - rx) / rw,
                "y_frac": (box["y"] - ry) / rh,
                "w_frac": box["w"] / rw,
                "h_frac": box["h"] / rh,
            }
        except (KeyError, TypeError, ZeroDivisionError):
            continue
    return out


def is_pill_sane(boxes: Dict[str, Dict[str, float]]) -> bool:
    """Return True if the pill bbox itself looks like a real pill.

    The signature pill is consistently a ~3:1 wide rectangle. When
    ``find_hud_panel`` mis-detects (e.g. it locks onto a fragment of
    the cyan stroke or grabs a different UI element), the resulting
    bbox has a wildly off aspect ratio. Use this as a first filter
    before computing fractions — a bad pill makes every fraction
    derived from it useless for the world model.

    Aspect range is taken from PILL_CALIBRATION (1.5 < aspect < 5.5,
    peak ~3.5). We tighten slightly to 2.2-3.8 here because the
    calibration is for raw detection and we want only the cleanly
    detected pills.
    """
    pill = boxes.get("pill")
    if not pill:
        return False
    pw, ph = pill.get("w") or 0, pill.get("h") or 0
    if pw < 60 or ph < 18:
        return False
    aspect = pw / max(1, ph)
    if aspect < 2.2 or aspect > 3.8:
        return False
    return True


def is_capture_sane(fracs: Dict[str, Dict[str, float]]) -> bool:
    """Return True if the capture's fractions look like a clean
    pill/icon/value layout.

    A sane region2 capture has the icon AND value fully inside the pill
    rectangle. When pill detection fails (returns a too-small bbox) or
    when a label is on the wrong feature, the fractions go out of range
    and we exclude the capture from the calibration.

    Tolerances are deliberately loose (-0.05 / 1.10) so we keep captures
    where the labeller drew slightly outside the pill stroke — only the
    truly broken cases are dropped.
    """
    icon = fracs.get("icon")
    value = fracs.get("value")
    if not value:
        return False  # value is required for the calibration we care about
    for box in (icon, value):
        if box is None:
            continue
        x = box.get("x_frac", 0.0)
        y = box.get("y_frac", 0.0)
        w = box.get("w_frac", 0.0)
        h = box.get("h_frac", 0.0)
        if x < -0.05 or y < -0.10:
            return False
        if x + w > 1.10 or y + h > 1.20:
            return False
        if w <= 0.01 or h <= 0.01 or w > 1.0 or h > 1.5:
            return False
    return True


def aggregate(
    per_capture: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Stack samples per feature and compute mean/std/min/max per coord."""
    feature_samples: Dict[str, Dict[str, List[float]]] = {}
    for entry in per_capture:
        for feat, fracs in entry["fracs"].items():
            slot = feature_samples.setdefault(feat, {c: [] for c in COORDS})
            for c in COORDS:
                slot[c].append(float(fracs[c]))

    summary: Dict[str, Dict[str, Dict[str, float]]] = {}
    for feat, coord_lists in feature_samples.items():
        per_coord: Dict[str, Dict[str, float]] = {}
        n = len(coord_lists[COORDS[0]])
        for c in COORDS:
            arr = np.asarray(coord_lists[c], dtype=np.float64)
            per_coord[c] = {
                "mean": float(arr.mean()),
                "std": float(arr.std(ddof=0)),
                "min": float(arr.min()),
                "max": float(arr.max()),
            }
        per_coord["count"] = n  # type: ignore[assignment]
        summary[feat] = per_coord
    return summary


def worst_std(summary: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """For each feature, surface the worst-coord std for quick scanning."""
    worst: Dict[str, Dict[str, float]] = {}
    for feat, info in summary.items():
        if feat == REFERENCE:
            continue
        std_by_coord = {c: info[c]["std"] for c in COORDS}
        c_worst = max(std_by_coord, key=std_by_coord.get)
        worst[feat] = {"coord": c_worst, "std": std_by_coord[c_worst]}  # type: ignore[dict-item]
    return worst


def variance_verdict(worst_per_feature: Dict[str, Dict[str, float]]) -> Dict[str, str]:
    """Classify each feature's worst std into sound/workable/problem.

    Thresholds chosen for region2 — pill is ~110×36 px so 1% std = ~1 px
    on width and ~0.4 px on height. Slack is appropriate because the
    pill bbox itself has stroke-width ambiguity (~3 px) baked into the
    label noise.

    Thresholds:
      < 2% std  -> "sound"     (proportional derivation is reliable;
                                expected error ~1-2 px)
      2%-7% std -> "workable"  (usable, expect ~3-5 px wobble — still
                                much better than NCC misses entirely)
      > 7% std  -> "problem"   (too much variance — re-review labels)
    """
    out: Dict[str, str] = {}
    for feat, info in worst_per_feature.items():
        std = info["std"]
        if std < 0.02:
            out[feat] = "sound"
        elif std < 0.07:
            out[feat] = "workable"
        else:
            out[feat] = "problem"
    return out


def main() -> Dict[str, Any]:
    captures = load_captures(SOURCE_DIR)

    per_capture: List[Dict[str, Any]] = []
    skipped_no_pill: List[str] = []
    skipped_bad_pill: List[Dict[str, Any]] = []
    skipped_outliers: List[Dict[str, Any]] = []
    for cap in captures:
        if not is_pill_sane(cap["boxes"]):
            pill = cap["boxes"].get("pill")
            if not pill:
                skipped_no_pill.append(cap["name"])
            else:
                skipped_bad_pill.append({
                    "name": cap["name"],
                    "pill": pill,
                })
            continue
        fracs = fractionalize(cap["boxes"])
        if not fracs:
            skipped_no_pill.append(cap["name"])
            continue
        if not is_capture_sane(fracs):
            # Outlier — pill bbox almost certainly misdetected.
            # Most common cause: tiny pill (w<60) where value/icon
            # extend outside the pill, giving fractions > 1 or < 0.
            skipped_outliers.append({
                "name": cap["name"],
                "fracs": fracs,
            })
            continue
        per_capture.append({
            "name": cap["name"],
            "image": cap["image"],
            "feature_count": len(fracs),
            "fracs": fracs,
        })

    summary = aggregate(per_capture) if per_capture else {}

    feature_counts: Dict[str, int] = {}
    for entry in per_capture:
        for feat in entry["fracs"]:
            feature_counts[feat] = feature_counts.get(feat, 0) + 1

    worst = worst_std(summary)
    verdict = variance_verdict(worst)

    out = {
        "source_dir": str(SOURCE_DIR),
        "reference": REFERENCE,
        "captures_total": len(captures),
        "captures_used": len(per_capture),
        "captures_skipped_no_pill": skipped_no_pill,
        "captures_skipped_bad_pill": skipped_bad_pill,
        "captures_skipped_outliers": skipped_outliers,
        "feature_capture_counts": feature_counts,
        "features": summary,
        "worst_std_per_feature": worst,
        "variance_verdict": verdict,
        # Per-capture fractions retained so outliers can be inspected later.
        "per_capture": per_capture,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    return out


if __name__ == "__main__":
    result = main()
    n_used = result["captures_used"]
    n_total = result["captures_total"]
    print(f"Captures used: {n_used}/{n_total}")
    if result["captures_skipped_no_pill"]:
        print(
            f"Skipped (no {REFERENCE} bbox at all): "
            f"{len(result['captures_skipped_no_pill'])} captures"
        )
        for s in result["captures_skipped_no_pill"][:5]:
            print(f"  - {s}")
    if result["captures_skipped_bad_pill"]:
        print(
            f"Skipped (pill bbox aspect/size off — likely misdetect): "
            f"{len(result['captures_skipped_bad_pill'])} captures"
        )
        for s in result["captures_skipped_bad_pill"][:5]:
            p = s["pill"]
            print(f"  - {s['name']}  pill={p['w']}x{p['h']}")
    if result["captures_skipped_outliers"]:
        print(
            f"Skipped (icon/value outside pill — likely label noise): "
            f"{len(result['captures_skipped_outliers'])} captures"
        )
        for s in result["captures_skipped_outliers"][:5]:
            print(f"  - {s['name']}")
    print(f"Feature counts: {result['feature_capture_counts']}")
    print()
    print("Per-feature mean fractional coords (x, y, w, h):")
    for feat, info in sorted(result["features"].items()):
        if feat == REFERENCE:
            continue
        x = info["x_frac"]["mean"]
        y = info["y_frac"]["mean"]
        w = info["w_frac"]["mean"]
        h = info["h_frac"]["mean"]
        print(f"  {feat:8s}  x={x:+.4f}  y={y:+.4f}  w={w:.4f}  h={h:.4f}")
    print()
    print("Worst std per feature (max across x/y/w/h fractions):")
    for feat, info in sorted(
        result["worst_std_per_feature"].items(),
        key=lambda kv: kv[1]["std"],
        reverse=True,
    ):
        v = result["variance_verdict"][feat]
        print(f"  {feat:8s}  {info['coord']:7s}  std={info['std']:.6f}  -> {v}")
    print()
    print("Variance verdict summary:")
    counts: Dict[str, int] = {"sound": 0, "workable": 0, "problem": 0}
    for v in result["variance_verdict"].values():
        counts[v] = counts.get(v, 0) + 1
    print(
        f"  sound={counts['sound']}  "
        f"workable={counts['workable']}  "
        f"problem={counts['problem']}"
    )
    print()
    print(f"Wrote: {OUT_PATH}")
