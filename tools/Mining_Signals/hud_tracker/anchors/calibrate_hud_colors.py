"""Calibrate HSV color bands for ``hud_color_finder.find_hud_panel``.

Reads ``.boxes.json`` ground-truth files alongside their PNG, samples
*bright* pixels (V high, S high) inside the chrome-bearing feature
boxes (top_line, bot_line for the green chrome; mass_row,
resistance_row, instability_row, scan_results for the cyan text),
clusters the sampled hues into "cyan" and "green" bands, computes
robust percentile bounds, and writes
``hud_tracker/hud_color_calibration.json``.

Usage
-----
Run with no args to default to the user_20260418_154408 set::

    python calibrate_hud_colors.py

Or point at any folder of labeled captures::

    python calibrate_hud_colors.py --source <folder> --output <json>

Notes
-----
With only a handful of labeled captures the calibration is
approximate; the saved JSON is documented as such (n_captures field).
The fallback in ``hud_color_finder.DEFAULT_CALIBRATION`` exists for
the same reason.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("calibrate_hud_colors")


# Feature boxes that we sample for calibration. Each has a "primary"
# expected hue band — we use this only for clustering, not for
# rejection — so it's a hint, not a filter. The actual percentile
# bounds come out of the full sampled distribution.
CHROME_FEATURES = ["top_line", "bot_line"]
TEXT_FEATURES = ["scan_results", "mass_row", "resistance_row", "instability_row"]
ALL_FEATURES = CHROME_FEATURES + TEXT_FEATURES


def _iter_labeled_captures(source: Path) -> Iterable[tuple[Path, dict]]:
    """Yield (png_path, boxes_json_dict) for every capture in ``source``
    that has both a PNG and a ``.boxes.json`` sibling."""
    for boxes_path in sorted(source.glob("*.boxes.json")):
        png_path = boxes_path.with_suffix("").with_suffix(".png")
        if not png_path.is_file():
            log.warning("orphan boxes file with no PNG: %s", boxes_path)
            continue
        try:
            data = json.loads(boxes_path.read_text())
        except Exception as exc:
            log.warning("failed to read %s: %s", boxes_path, exc)
            continue
        if "boxes" not in data:
            continue
        yield png_path, data


def _sample_bright_pixels(
    img: Image.Image,
    boxes: dict,
    feature_names: Iterable[str],
    *,
    val_floor: int = 130,
    sat_floor: int = 100,
) -> np.ndarray:
    """Return the (N, 3) HSV pixels (uint8) that are bright + saturated
    inside the union of the requested feature boxes.

    Skips features missing from ``boxes``.
    """
    arr = np.asarray(img.convert("RGB"))
    hsv = np.asarray(img.convert("HSV"))
    out_pixels: list[np.ndarray] = []
    H_full, W_full = arr.shape[:2]
    for fname in feature_names:
        b = boxes.get(fname)
        if not b:
            continue
        try:
            x = int(b["x"]); y = int(b["y"])
            w = int(b["w"]); h = int(b["h"])
        except (KeyError, TypeError):
            continue
        x0 = max(0, x); y0 = max(0, y)
        x1 = min(W_full, x + w); y1 = min(H_full, y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        crop = hsv[y0:y1, x0:x1]
        v = crop[:, :, 2]
        s = crop[:, :, 1]
        mask = (v >= val_floor) & (s >= sat_floor)
        if not mask.any():
            continue
        out_pixels.append(crop[mask])
    if not out_pixels:
        return np.empty((0, 3), dtype=np.uint8)
    return np.concatenate(out_pixels, axis=0)


def _hue_cluster_bounds(
    pixels: np.ndarray,
    *,
    cluster_low_hint: tuple[int, int] = (15, 80),
    cluster_high_hint: tuple[int, int] = (90, 200),
    pct_low: float = 5.0,
    pct_high: float = 95.0,
    min_pad: int = 5,
) -> dict:
    """Split sampled pixels into a "low-H green/yellow" cluster and a
    "high-H cyan" cluster using the hint windows, then compute
    percentile bounds for each cluster.

    The hint windows are coarse: we look at hue distribution within
    each window and trim to a tighter ``[pct_low, pct_high]`` range,
    pad by ``min_pad`` so we don't clip too aggressively, and clamp
    to [0, 255]. Returns::

        {
          "green_band": {"h_min": int, "h_max": int, "n_samples": int},
          "cyan_band":  {"h_min": int, "h_max": int, "n_samples": int},
        }

    If a cluster is empty we fall back to the hint range (still
    workable, just untuned).
    """
    H = pixels[:, 0]

    def cluster(window: tuple[int, int]) -> tuple[int, int, int]:
        lo, hi = window
        sub = H[(H >= lo) & (H <= hi)]
        n = int(sub.size)
        if n == 0:
            return lo, hi, 0
        p_lo = float(np.percentile(sub, pct_low))
        p_hi = float(np.percentile(sub, pct_high))
        return (
            int(max(0, round(p_lo) - min_pad)),
            int(min(255, round(p_hi) + min_pad)),
            n,
        )

    g_lo, g_hi, g_n = cluster(cluster_low_hint)
    c_lo, c_hi, c_n = cluster(cluster_high_hint)

    return {
        "green_band": {"h_min": g_lo, "h_max": g_hi, "n_samples": g_n},
        "cyan_band":  {"h_min": c_lo, "h_max": c_hi, "n_samples": c_n},
    }


def _percentile_floor(values: np.ndarray, pct: float) -> int:
    """Return ``int(percentile)`` clipped to [0, 255], or 0 on empty."""
    if values.size == 0:
        return 0
    return int(max(0, min(255, round(float(np.percentile(values, pct))))))


def calibrate(source: Path, output: Path) -> dict:
    """Run the calibration pass; write JSON to ``output`` and return
    the calibration dict that was written."""
    captures = list(_iter_labeled_captures(source))
    if not captures:
        raise SystemExit(f"no labeled captures found in {source}")

    log.info("calibrating from %d labeled captures in %s",
             len(captures), source)

    # Aggregate all bright pixels across captures and features.
    all_pixels_chunks: list[np.ndarray] = []
    per_capture_summary: list[dict] = []

    for png_path, data in captures:
        try:
            img = Image.open(png_path).convert("RGB")
        except Exception as exc:
            log.warning("failed to open %s: %s", png_path, exc)
            continue
        boxes = data.get("boxes", {})
        chunk = _sample_bright_pixels(
            img, boxes, ALL_FEATURES,
            val_floor=130, sat_floor=100,
        )
        per_capture_summary.append({
            "image": png_path.name,
            "n_samples": int(chunk.shape[0]),
            "features_used": [f for f in ALL_FEATURES if f in boxes],
        })
        if chunk.size:
            all_pixels_chunks.append(chunk)

    if not all_pixels_chunks:
        raise SystemExit("no bright pixels collected — empty calibration")

    pixels = np.concatenate(all_pixels_chunks, axis=0)
    log.info("collected %d bright HSV samples across captures",
             pixels.shape[0])

    # Cluster the hue distribution.
    bounds = _hue_cluster_bounds(pixels)

    # Saturation/value floors: take the 5th percentile so dim panels
    # still pass. Don't go below conservative minimums (sat 80, val
    # 80) — anything dimmer is probably background, not chrome.
    sat_floor = max(80, _percentile_floor(pixels[:, 1], 5.0))
    val_floor = max(80, _percentile_floor(pixels[:, 2], 5.0))

    calibration = {
        "version": 1,
        "source": str(source),
        "n_captures": len(per_capture_summary),
        "n_total_samples": int(pixels.shape[0]),
        "cyan_band": {
            "h_min": int(bounds["cyan_band"]["h_min"]),
            "h_max": int(bounds["cyan_band"]["h_max"]),
            "_n_samples": int(bounds["cyan_band"]["n_samples"]),
        },
        "green_band": {
            "h_min": int(bounds["green_band"]["h_min"]),
            "h_max": int(bounds["green_band"]["h_max"]),
            "_n_samples": int(bounds["green_band"]["n_samples"]),
        },
        "sat_min": int(sat_floor),
        "val_min": int(val_floor),
        # Geometry — kept matching the defaults in hud_color_finder.
        "min_area_px": 1500,
        "min_bbox_aspect": 0.4,
        "max_bbox_aspect": 1.5,
        "bbox_aspect_peak": 1.0,
        "min_extent": 0.05,
        "morph_seed_iterations": 2,
        "morph_vert_close_px": 30,
        "morph_horiz_close_px": 8,
        "_per_capture_summary": per_capture_summary,
        "_doc": (
            "Calibration with a small labeled set. Numbers are "
            "approximate and should be re-derived as more captures "
            "are labeled. PIL HSV scale: H, S, V each in 0-255."
        ),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(calibration, indent=2))
    log.info("wrote calibration to %s", output)
    return calibration


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    default_source = Path(
        r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
        r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
        r"\training_data_panels\user_20260418_154408\region1"
    )
    default_output = (
        Path(__file__).resolve().parent.parent / "hud_color_calibration.json"
    )
    ap.add_argument("--source", type=Path, default=default_source,
                    help=f"folder of labeled captures (default: {default_source})")
    ap.add_argument("--output", type=Path, default=default_output,
                    help=f"output JSON (default: {default_output})")
    ap.add_argument("--only", nargs="*",
                    help="if given, restrict to PNG basenames containing any of these substrings")
    args = ap.parse_args()

    src = args.source
    if not src.is_dir():
        raise SystemExit(f"source not found: {src}")

    if args.only:
        # Optional filter so the caller can run with just the 2
        # requested captures. We do this by temporarily writing a
        # filter into the loop here.
        original = _iter_labeled_captures.__globals__["_iter_labeled_captures"]

        def filtered(_src):
            for p, d in original(_src):
                if any(k in p.name for k in args.only):
                    yield p, d

        # Monkey-patch within this run.
        _iter_labeled_captures.__globals__["_iter_labeled_captures"] = filtered
        try:
            calibrate(src, args.output)
        finally:
            _iter_labeled_captures.__globals__["_iter_labeled_captures"] = original
    else:
        calibrate(src, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
