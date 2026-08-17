"""End-to-end test for the signal CNN polarity routing + N-way digit
position consensus voter.

Runs the production signature recognition path on a curated set of real
region2 captures with known ground-truth values. Reports per-capture
read accuracy, per-position 4-CNN vote breakdown, and aggregate string-
match accuracy.

Run as a script:
    python hud_tracker/anchors/test_signal_cnn_polarity_voter.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

# Path bootstrap.
_THIS = Path(__file__).resolve()
_TOOL = _THIS.parent.parent.parent
if str(_TOOL) not in sys.path:
    sys.path.insert(0, str(_TOOL))


# Region2 capture fixture directory.
_REGION2_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels\user_20260418_154408\region2"
)


def _build_lexicon(region2_dir: Path) -> set[int]:
    lex: set[int] = set()
    for j in region2_dir.glob("*.json"):
        if j.name.endswith(".boxes.json"):
            continue
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
            v = d.get("value", "")
            if v:
                lex.add(int(v.replace(",", "")))
        except Exception:
            continue
    return lex


def _find_capture_for_value(
    region2_dir: Path, target: str,
) -> Optional[str]:
    """Return the stem (without extension) of the first region2 capture
    whose ``value`` JSON field matches ``target``."""
    for j in sorted(region2_dir.glob("*.json")):
        if j.name.endswith(".boxes.json"):
            continue
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(d.get("value", "")) == target:
            return j.stem
    return None


def _run_voter_e2e(
    pil_rgb: Image.Image,
    bbox_xyxy: tuple[int, int, int, int],
    lexicon: set[int] | None,
):
    """Run the polarity-fixed segmenter + 4-CNN voter on a single
    capture and return per-position votes + composed string."""
    from ocr.sc_ocr.api import (  # noqa: E402
        _classify_crops_signal,
        _classify_crops_signal_inv,
        _classify_crops_signal_rgb,
        _classify_crops_signal_rgb_inv,
        _tight_repad_glyph,
        _tight_repad_glyph_rgb,
        _vote_on_digit_string,
    )
    from hud_tracker.anchors.signal_proportional_segmenter import (  # noqa: E402
        segment_signal_proportional,
    )

    rgb_crop = pil_rgb.crop(bbox_xyxy)
    seg = segment_signal_proportional(
        rgb_crop, classifier=_classify_crops_signal, lexicon=lexicon,
    )
    if seg is None:
        return {
            "string": "",
            "n_voters": 0,
            "per_position": [],
            "voter_strings": {},
        }

    # Get the per-glyph 28x28 grayscale crops the segmenter built —
    # use the segmenter's own preprocessing so the per-position bboxes
    # already align with what the proportional segmenter produced.
    digit_bboxes = [
        d["bbox"] for d in seg.get("digits") or []
        if not d.get("is_comma")
    ]

    # Replicate segmenter's preprocessing to get the gray canon used
    # for slot crops. (The segmenter doesn't expose its gray_canon,
    # so we redo it here.)
    rgb_array = np.asarray(rgb_crop.convert("RGB"), dtype=np.uint8)
    gray = rgb_array.max(axis=2).astype(np.uint8)
    h0 = gray.shape[0]
    if h0 < 28:
        scale = max(2, 32 // max(1, h0))
        gray = np.asarray(
            Image.fromarray(gray, mode="L").resize(
                (gray.shape[1] * scale, h0 * scale),
                Image.LANCZOS,
            ),
            dtype=np.uint8,
        )
    g32 = gray.astype(np.float32)
    mn, mx = float(g32.min()), float(g32.max())
    if mx - mn > 8:
        g32 = (g32 - mn) * (255.0 / (mx - mn))
        gray = np.clip(g32, 0, 255).astype(np.uint8)

    from hud_tracker.anchors.signal_proportional_segmenter import (
        _canonicalize_polarity_local,
    )
    canon = _canonicalize_polarity_local(gray)

    # Build gray + RGB crops 28x28 via the tight-repad helper. The
    # legacy code did simple pad-with-255 + bilinear-resize on the
    # LOOSE proportional bboxes (full row height, proportional width)
    # which stretched narrow ``1`` glyphs to fill ~28×26 of the canvas.
    # Training samples have ink at ~22/28 with bg padding — so the
    # legacy flow's CNN inputs were wildly out of distribution.
    # ``_tight_repad_glyph`` finds the actual ink bbox in the loose
    # canonical sub, scales preserving aspect to fit ``target_inner``,
    # and re-pads with bg color → matches training distribution.
    gray_crops = []
    rgb_crops_for_cnn = []
    # RGB equivalent of canon: resize once to align bbox coords.
    if h0 < 28:
        scale_rgb = max(2, 32 // max(1, h0))
        rgb_resized = np.asarray(
            rgb_crop.convert("RGB").resize(
                (rgb_array.shape[1] * scale_rgb,
                 rgb_array.shape[0] * scale_rgb),
                Image.LANCZOS,
            ),
            dtype=np.uint8,
        )
    else:
        rgb_resized = rgb_array.copy()
    for bx, by, bw, bh in digit_bboxes:
        sub = canon[by:by + bh, bx:bx + bw].astype(np.float32) / 255.0
        gray_crops.append(
            _tight_repad_glyph(sub, skip_padding_ring=False)
        )

        sub_rgb = rgb_resized[by:by + bh, bx:bx + bw]
        rgb_crops_for_cnn.append(
            _tight_repad_glyph_rgb(sub_rgb, skip_padding_ring=False)
        )

    if not gray_crops:
        return {
            "string": "",
            "n_voters": 0,
            "per_position": [],
            "voter_strings": {},
        }

    pri = _classify_crops_signal(gray_crops)
    sec = _classify_crops_signal_inv(gray_crops)
    rgb = _classify_crops_signal_rgb(rgb_crops_for_cnn)
    rgb_inv = _classify_crops_signal_rgb_inv(rgb_crops_for_cnn)

    consensus = _vote_on_digit_string(
        primary_results=pri,
        secondary_results=sec,
        rgb_results=rgb,
        rgb_inv_results=rgb_inv,
        lexicon=lexicon,
    )

    voter_strings = {
        "primary": "".join(
            c if c.isdigit() else "?" for c, _ in (pri or [])
        ),
        "secondary": "".join(
            c if c.isdigit() else "?" for c, _ in (sec or [])
        ),
        "rgb": "".join(
            c if c.isdigit() else "?" for c, _ in (rgb or [])
        ),
        "rgb_inv": "".join(
            c if c.isdigit() else "?" for c, _ in (rgb_inv or [])
        ),
    }

    return {
        "string": consensus["string"],
        "n_voters": consensus["available_voters"],
        "per_position": consensus["per_position"],
        "voter_strings": voter_strings,
        "consensus_path": consensus.get("consensus_path", "?"),
        "mean_conf": consensus.get("mean_confidence", 0.0),
    }


def main() -> int:
    region2_dir = _REGION2_DIR
    if not region2_dir.is_dir():
        print(f"region2 dir not found: {region2_dir}")
        return 1

    lexicon = _build_lexicon(region2_dir)
    print(f"Lexicon: {len(lexicon)} known signature values")

    # Curated fixture set — explicitly named captures + auto-fill from
    # JSON sidecars to reach 12 unique GT values.
    fixtures: list[tuple[str, str]] = []

    # Specifically-named ones from the spec.
    for stem, gt in (
        ("cap_20260418_155500_306", "11,520"),
    ):
        if (region2_dir / (stem + ".png")).exists():
            fixtures.append((stem, gt))
    # Find the 3,400 capture by scanning JSON sidecars.
    found = _find_capture_for_value(region2_dir, "3,400")
    if found:
        fixtures.append((found, "3,400"))

    # Auto-fill the rest with one capture per unique GT until we have
    # 10 captures total.
    have_values = {gt for _, gt in fixtures}
    for j in sorted(region2_dir.glob("*.json")):
        if len(fixtures) >= 10:
            break
        if j.name.endswith(".boxes.json"):
            continue
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            continue
        v = d.get("value", "")
        if not v or v in have_values:
            continue
        if not (j.with_name(j.stem + ".boxes.json")).exists():
            continue
        fixtures.append((j.stem, v))
        have_values.add(v)

    print(f"Fixtures: {len(fixtures)}")
    print()

    print(
        f"{'GT':>10}  {'PROD':>8}  {'OK':>3}  {'CONSENSUS_PATH':<60}  "
        f"{'CAPTURE':<35}"
    )
    print("-" * 130)

    n_correct = 0
    n_total = 0
    rows: list[dict] = []

    for stem, gt_value in fixtures:
        png_path = region2_dir / (stem + ".png")
        boxes_path = region2_dir / (stem + ".boxes.json")
        if not (png_path.exists() and boxes_path.exists()):
            continue
        try:
            img = Image.open(png_path).convert("RGB")
            box = json.loads(
                boxes_path.read_text(encoding="utf-8"),
            )["boxes"]["value"]
        except Exception as exc:
            print(f"  skip {stem}: {exc}")
            continue

        bbox = (
            int(box["x"]),
            int(box["y"]),
            int(box["x"]) + int(box["w"]),
            int(box["y"]) + int(box["h"]),
        )
        gt_digits = "".join(c for c in gt_value if c.isdigit())

        try:
            res = _run_voter_e2e(img, bbox, lexicon)
        except Exception as exc:
            print(f"  ERROR {stem}: {exc}")
            continue

        prod_str = res["string"]
        ok = (prod_str == gt_digits)
        if ok:
            n_correct += 1
        n_total += 1

        # Truncate consensus_path for display
        cp = res.get("consensus_path", "")
        if len(cp) > 58:
            cp = cp[:55] + "..."

        print(
            f"{gt_value:>10}  {prod_str:>8}  "
            f"{'YES' if ok else 'no':>3}  {cp:<60}  {stem:<35}"
        )

        rows.append({
            "stem": stem,
            "gt": gt_value,
            "gt_digits": gt_digits,
            "prod": prod_str,
            "ok": ok,
            "n_voters": res["n_voters"],
            "voter_strings": res["voter_strings"],
            "consensus_path": res.get("consensus_path", ""),
            "mean_conf": res.get("mean_conf", 0.0),
        })

    print()
    print("--- Aggregate ---")
    print(
        f"  exact match: {n_correct}/{n_total} = "
        f"{100 * n_correct / max(1, n_total):.0f}%"
    )

    print()
    print("--- Per-capture voter breakdown ---")
    for r in rows:
        v = r["voter_strings"]
        print(
            f"  {r['gt']:>8}  prod={r['prod']:>6}  "
            f"pri={v['primary']:<8}  sec={v['secondary']:<8}  "
            f"rgb={v['rgb']:<8}  rgb_inv={v['rgb_inv']:<8}  "
            f"({r['stem']})"
        )

    print()
    print("--- Per-position consensus paths (first 3 captures) ---")
    for r in rows[:3]:
        print(f"\n  capture={r['stem']} GT={r['gt']}")
        # Re-run to grab the per_position dicts (they got summarized
        # in consensus_path). For brevity only the consensus_path is
        # carried in the row.
        print(f"    consensus_path={r['consensus_path']}")
        v = r["voter_strings"]
        print(f"    voter strings: pri={v['primary']!r} sec={v['secondary']!r} "
              f"rgb={v['rgb']!r} rgb_inv={v['rgb_inv']!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
