"""
World-model proportions extractor for the SCAN RESULTS HUD panel.

Reads every *.boxes.json under SOURCE_DIR, treats each capture's `scan_results`
box as the panel reference frame, and computes fractional coordinates
(x_frac, y_frac, w_frac, h_frac) for every other labeled feature.

Aggregates mean + std across all captures for each (feature, coordinate) pair
and writes the result to OUT_PATH so a single anchor can later be inverted to
a full panel pose.

Re-runnable: re-reads SOURCE_DIR and overwrites OUT_PATH on each invocation.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List

import numpy as np

SOURCE_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals\training_data_panels"
    r"\user_20260418_154408\region1"
)
OUT_PATH = Path(
    r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals"
    r"\hud_tracker\world_model.json"
)
COORDS = ("x_frac", "y_frac", "w_frac", "h_frac")
REFERENCE = "scan_results"


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
    """Express every non-reference box as a fraction of the reference box.

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
            # Reference frame is identity by definition; record it for completeness.
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


def aggregate(
    per_capture: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Stack samples per feature and compute mean/std/count per coord."""
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


def main() -> Dict[str, Any]:
    captures = load_captures(SOURCE_DIR)

    per_capture: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for cap in captures:
        fracs = fractionalize(cap["boxes"])
        if not fracs:
            skipped.append(cap["name"])
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

    out = {
        "source_dir": str(SOURCE_DIR),
        "reference": REFERENCE,
        "captures_total": len(captures),
        "captures_used": len(per_capture),
        "captures_skipped": skipped,
        "feature_capture_counts": feature_counts,
        "features": summary,
        "worst_std_per_feature": worst_std(summary),
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
    if result["captures_skipped"]:
        print(f"Skipped (no usable {REFERENCE}): {result['captures_skipped']}")
    print(f"Feature counts: {result['feature_capture_counts']}")
    print()
    print("Worst std per feature (max across x/y/w/h fractions):")
    for feat, info in sorted(
        result["worst_std_per_feature"].items(),
        key=lambda kv: kv[1]["std"],
        reverse=True,
    ):
        print(f"  {feat:20s}  {info['coord']:7s}  std={info['std']:.6f}")
    print()
    print(f"Wrote: {OUT_PATH}")
