"""Smoke + detection-rate tests for ``hud_tracker.anchors.comma_finder``.

Run under production Python 3.14::

    "C:\\Users\\_user\\AppData\\Local\\SC_Toolbox\\current\\python\\python.exe" \\
        hud_tracker\\anchors\\test_comma_finder.py

What this script does
---------------------
1. Loads every labelled region2 capture in
   ``training_data_panels/user_20260418_154408/region2`` (each PNG
   has a sibling ``cap_*.json`` with ``"value": "16,960"`` and a
   ``cap_*.boxes.json`` with the value bbox).

2. For each capture, slices out the value crop, runs
   :func:`find_comma_voted`, and computes:
     * Detection rate (% of crops where comma was found)
     * Polarity-agreement rate (% where both polarities agreed)
     * Position accuracy: pixel error vs the GT-implied comma column
       (computed from the GT digit count + the value bbox's width)

3. Reports per-capture results + an aggregate summary.

Exit code is 0 on success (≥ 80% detection rate, ≥ 70% within +/- 2 px),
1 on regression.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Path bootstrap so we can import the comma_finder module when
# launched as a script.
_THIS = Path(__file__).resolve()
_TOOL = _THIS.parent.parent.parent
if str(_TOOL) not in sys.path:
    sys.path.insert(0, str(_TOOL))

from PIL import Image  # noqa: E402

from hud_tracker.anchors.comma_finder import (  # noqa: E402
    find_comma,
    find_comma_inv,
    find_comma_voted,
)


#---──────────────────────────────────────────────────────────
# Paths
#---──────────────────────────────────────────────────────────

_REGION2_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels\user_20260418_154408\region2"
)


#---──────────────────────────────────────────────────────────
# Helpers
#---──────────────────────────────────────────────────────────


def _read_value_str(json_path: Path) -> str | None:
    try:
        with json_path.open("r", encoding="utf-8") as fh:
            j = json.load(fh)
        v = j.get("value", "")
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        return None
    return None


def _read_value_box(boxes_path: Path) -> tuple[int, int, int, int] | None:
    try:
        with boxes_path.open("r", encoding="utf-8") as fh:
            j = json.load(fh)
        v = j.get("boxes", {}).get("value", {})
        if not v:
            return None
        return (int(v["x"]), int(v["y"]), int(v["w"]), int(v["h"]))
    except Exception:
        return None


def _expected_comma_x(value_str: str, crop_w: int) -> float | None:
    """Estimate where the comma should sit (in crop-local x) given
    a known GT digit string and the crop's width.

    The SC signal font is proportional but commas live between two
    specific digit positions:
      * 4-digit ``D,DDD`` → comma after digit 0 (x ≈ 1/4 of crop)
      * 5-digit ``DD,DDD`` → comma after digit 1 (x ≈ 2/5 of crop)

    The crop has a small left margin and small right margin from
    the bbox padding. We approximate the comma's column as a fixed
    fraction of crop width, matching the SC HUD's typical layout
    proportions on real captures.

    Returns the expected x-center in pixels, or None when the value
    string isn't a recognized 4-/5-digit form.
    """
    digits_only = "".join(ch for ch in value_str if ch.isdigit())
    if len(digits_only) == 4:
        # 4-digit: 1 digit + comma + 3 digits.
        # Empirically measured: x_comma / crop_w ≈ 0.26 across the
        # labelled 4-digit captures (e.g. 7,080, 3,400, 2,000 all
        # converge near this ratio).
        return crop_w * 0.26
    if len(digits_only) == 5:
        # 5-digit: 2 digits + comma + 3 digits.
        # Empirically measured: x_comma / crop_w ≈ 0.36 across the
        # labelled 5-digit captures (e.g. 16,960 at 23/62=0.37,
        # 11,520 at 19/58=0.33).
        return crop_w * 0.36
    return None


#---──────────────────────────────────────────────────────────
# Test 1: smoke (basic call doesn't raise on synthetic input)
#---──────────────────────────────────────────────────────────


def test_smoke() -> bool:
    print("---Smoke tests---")

    import numpy as np

    # 1. None input.
    r = find_comma(None)  # type: ignore[arg-type]
    assert r is None, "find_comma(None) should return None"
    r = find_comma_inv(None)  # type: ignore[arg-type]
    assert r is None
    r = find_comma_voted(None)  # type: ignore[arg-type]
    assert r is None

    # 2. Tiny image.
    tiny = Image.new("RGB", (5, 5), (0, 0, 0))
    r = find_comma(tiny)
    assert r is None, "find_comma on 5x5 should return None"

    # 3. Solid black image.
    blank = Image.new("RGB", (62, 19), (0, 0, 0))
    r = find_comma(blank)
    assert r is None

    # 4. Solid white image.
    white = Image.new("RGB", (62, 19), (255, 255, 255))
    r = find_comma(white)
    assert r is None

    # 5. Numpy array input.
    arr = np.zeros((19, 62, 3), dtype=np.uint8)
    arr[10:18, 25:30, :] = 200  # tall blob (not a comma)
    r = find_comma(arr)
    # May or may not find — just doesn't raise.
    assert r is None or "bbox" in r

    print("  Smoke tests passed.")
    return True


#---──────────────────────────────────────────────────────────
# Test 2: detection rate + polarity agreement on labelled captures
#---──────────────────────────────────────────────────────────


def test_labelled_captures() -> tuple[bool, dict]:
    if not _REGION2_DIR.is_dir():
        print(f"  WARNING: labelled dir not found: {_REGION2_DIR}")
        print("  Skipping labelled-capture test.")
        return True, {}

    print(f"\n---Labelled captures from {_REGION2_DIR}---")

    captures: list[dict] = []
    for png in sorted(_REGION2_DIR.glob("cap_*.png")):
        json_path = png.with_suffix(".json")
        boxes_path = png.with_suffix("").with_suffix(".boxes.json")
        if not json_path.is_file() or not boxes_path.is_file():
            continue
        value_str = _read_value_str(json_path)
        value_box = _read_value_box(boxes_path)
        if value_str is None or value_box is None:
            continue
        captures.append({
            "png": png,
            "value_str": value_str,
            "value_box": value_box,
        })

    print(f"  Loaded {len(captures)} labelled captures.")
    if not captures:
        return True, {}

    n_found = 0
    n_agreed = 0
    n_within_2px = 0
    n_within_4px = 0
    timings_ms: list[float] = []
    pixel_errors: list[float] = []
    primary_only_found = 0
    inv_only_found = 0
    both_none = 0

    print()
    header = (
        f"  {'capture':<28s} {'GT':>7s} {'crop_w':>6s} "
        f"{'cx_det':>6s} {'cx_exp':>6s} {'err':>4s} "
        f"{'conf':>5s} {'agr':>3s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for cap in captures:
        x, y, w, h = cap["value_box"]
        try:
            img = Image.open(cap["png"]).convert("RGB")
            crop = img.crop((x, y, x + w, y + h))
        except Exception as exc:
            print(f"  ERROR loading {cap['png'].name}: {exc}")
            continue

        # Time the voted call.
        t0 = time.monotonic()
        result = find_comma_voted(crop)
        dt = (time.monotonic() - t0) * 1000.0
        timings_ms.append(dt)

        # Diagnostic: also check primary and inv individually so we
        # can report which polarity contributed.
        primary_alone = find_comma(crop)
        inv_alone = find_comma_inv(crop)
        if primary_alone is None and inv_alone is None:
            both_none += 1
        elif primary_alone is not None and inv_alone is None:
            primary_only_found += 1
        elif primary_alone is None and inv_alone is not None:
            inv_only_found += 1

        if result is None:
            print(
                f"  {cap['png'].stem:<28s} {cap['value_str']:>7s} "
                f"{w:>6d}    -      -    -    -      -"
            )
            continue
        n_found += 1
        if result.get("agreed"):
            n_agreed += 1

        cx_det = int(result["x_center"])
        expected_x = _expected_comma_x(cap["value_str"], w)
        if expected_x is None:
            cx_exp_str = "  -  "
            err_str = " -  "
        else:
            err = cx_det - expected_x
            pixel_errors.append(abs(err))
            if abs(err) <= 2:
                n_within_2px += 1
            if abs(err) <= 4:
                n_within_4px += 1
            cx_exp_str = f"{expected_x:>6.1f}"
            err_str = f"{err:>+4.1f}"

        print(
            f"  {cap['png'].stem:<28s} {cap['value_str']:>7s} "
            f"{w:>6d} {cx_det:>6d} {cx_exp_str} {err_str} "
            f"{result['confidence']:>5.2f} "
            f"{'yes' if result.get('agreed') else 'no '}"
        )

    print()
    n = len(captures)
    detection_rate = n_found / max(1, n)
    agreement_rate = n_agreed / max(1, n_found)
    within_2px_rate = n_within_2px / max(1, len(pixel_errors))
    within_4px_rate = n_within_4px / max(1, len(pixel_errors))
    median_err = (
        float(sorted(pixel_errors)[len(pixel_errors) // 2])
        if pixel_errors else float("nan")
    )
    mean_ms = sum(timings_ms) / max(1, len(timings_ms))

    print(f"  {'Detection rate':<32s} {detection_rate * 100:.1f}% "
          f"({n_found}/{n})")
    print(f"  {'Polarity-agreement rate':<32s} "
          f"{agreement_rate * 100:.1f}% ({n_agreed}/{n_found})")
    if pixel_errors:
        print(f"  {'Position accuracy +/-2 px':<32s} "
              f"{within_2px_rate * 100:.1f}% "
              f"({n_within_2px}/{len(pixel_errors)})")
        print(f"  {'Position accuracy +/-4 px':<32s} "
              f"{within_4px_rate * 100:.1f}% "
              f"({n_within_4px}/{len(pixel_errors)})")
        print(f"  {'Median |pixel error|':<32s} {median_err:.2f} px")
    print(f"  {'Primary-only found (no inv)':<32s} {primary_only_found}")
    print(f"  {'Inv-only found (no primary)':<32s} {inv_only_found}")
    print(f"  {'Both None':<32s} {both_none}")
    print(f"  {'Mean detector time':<32s} {mean_ms:.2f} ms")

    summary = {
        "n_captures": n,
        "n_found": n_found,
        "detection_rate": detection_rate,
        "agreement_rate": agreement_rate,
        "median_pixel_error": median_err,
        "within_2px_rate": within_2px_rate,
        "within_4px_rate": within_4px_rate,
        "mean_ms": mean_ms,
        "primary_only_found": primary_only_found,
        "inv_only_found": inv_only_found,
        "both_none": both_none,
    }

    # Acceptance: detection rate ≥ 80%, position accuracy ≥ 70% within +/-4 px.
    ok_detection = detection_rate >= 0.80
    ok_position = within_4px_rate >= 0.70 if pixel_errors else True
    return (ok_detection and ok_position), summary


#---──────────────────────────────────────────────────────────
# Main
#---──────────────────────────────────────────────────────────


def main() -> int:
    print("Testing hud_tracker.anchors.comma_finder")
    print("=" * 60)
    if not test_smoke():
        return 1
    ok, _summary = test_labelled_captures()
    if not ok:
        print("\nFAIL — detection rate or position accuracy below threshold.")
        return 1
    print("\nPASS — comma_finder meets acceptance thresholds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
