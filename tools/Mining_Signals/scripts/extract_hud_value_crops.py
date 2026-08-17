"""Extract labeled HUD value crops for CRNN training.

Walks every labeled panel capture under ``training_data_panels/user_*/
region*/`` and, for each capture's mass/resistance/instability row,
extracts the value sub-region using the production crop pipeline
(``find_label_positions`` + ``_find_value_crop``), then writes the
crop to disk paired with its label.

Output structure::

    training_data_hud_crops/
        mass/
            <user>__<capture>__<value>.png
            <user>__<capture>__<value>.png
            ...
        resistance/
            <user>__<capture>__<value>.png
            ...
        instability/
            <user>__<capture>__<value>.png
            ...
        index.csv             (path, label, field, source_capture)

These crops are the training data for a HUD-specific RGB CRNN.
Each per-field crop's label is the user-confirmed value from the
sidecar JSON (e.g. ``"3384"`` for mass, ``"1"`` for resistance,
``"1.43"`` for instability — comma-stripped, %-stripped).

Run::

    python scripts/extract_hud_value_crops.py
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
)
log = logging.getLogger("extract_hud_value_crops")

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
OUT_ROOT = TOOL / "training_data_hud_crops"
INDEX_CSV = OUT_ROOT / "index.csv"

PANEL_ROOTS = [
    Path(r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
         r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
         r"\training_data_panels"),
    TOOL / "training_data_panels",
]

# Make the tool tree importable for the production crop pipeline.
if str(TOOL) not in sys.path:
    sys.path.insert(0, str(TOOL))

# Pull production functions for label-row finding + value-crop
# derivation. Both use the (now-tight) labels.npz templates.
from ocr.sc_ocr import label_match as _lm  # noqa: E402
from ocr import onnx_hud_reader as _hud  # noqa: E402


def _norm_label(field: str, raw: Optional[str]) -> Optional[str]:
    """Return the canonical label string for training (digit-only)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
    s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}"


def _label_safe_filename(label: str) -> str:
    """Turn a label like '1.43' into a filename-safe stub like '1p43'."""
    return label.replace(".", "p").replace("-", "n").replace("/", "_")


def _extract_per_row_crops(
    img: Image.Image,
) -> dict[str, Optional[Image.Image]]:
    """Return {field: PIL crop or None} for mass / resistance / instability.

    Uses the PRODUCTION crop pipeline (``_find_label_rows`` + the
    asymmetric-padded value crop) so the training data the CRNN
    consumes is identical to what production scans extract at
    inference time. Includes:
      * Tier A NCC label matching → Tier B measured bands → Tier C
        title-anchored proportional fallback (whichever fires first)
      * Asymmetric Y-padding (PAD_Y_TOP=8, PAD_Y_BOT=4) so digit
        tops aren't clipped
      * Per-row shared ``label_right`` column with column_x_offset
        applied (same path as ``_label_rows_for_region``)
    """
    gray = np.array(img.convert("L"), dtype=np.uint8)
    try:
        label_rows = _hud._find_label_rows(img)
    except Exception as exc:
        log.debug("_find_label_rows failed: %s", exc)
        return {"mass": None, "resistance": None, "instability": None}
    out: dict[str, Optional[Image.Image]] = {}
    for field in ("mass", "resistance", "instability"):
        entry = label_rows.get(field)
        if entry is None:
            out[field] = None
            continue
        try:
            y1, y2, label_right = entry
        except (TypeError, ValueError):
            out[field] = None
            continue
        if y2 <= y1 or (y2 - y1) < 6:
            out[field] = None
            continue
        x_min = min(img.width - 8, int(label_right) + 6)
        try:
            crop = _hud._find_value_crop(
                img, gray, int(y1), int(y2), x_min=x_min,
            )
        except Exception as exc:
            log.debug("  %s _find_value_crop failed: %s", field, exc)
            crop = None
        out[field] = crop
    return out


def _walk_captures() -> list[tuple[Path, dict]]:
    items: list[tuple[Path, dict]] = []
    seen: set[Path] = set()
    for root in PANEL_ROOTS:
        if not root.exists():
            continue
        for sc in root.rglob("*.json"):
            if "glyphs" in sc.name or "boxes" in sc.name:
                continue
            try:
                d = json.loads(sc.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("schema") not in ("region1", "region2"):
                continue
            if not any(d.get(k) for k in ("mass", "resistance", "instability")):
                continue
            png = sc.with_suffix(".png")
            if not png.is_file():
                continue
            if png in seen:
                continue
            seen.add(png)
            items.append((png, d))
    return items


def main() -> int:
    OUT_ROOT.mkdir(exist_ok=True)
    for field in ("mass", "resistance", "instability"):
        (OUT_ROOT / field).mkdir(exist_ok=True)

    items = _walk_captures()
    log.info("found %d labeled captures", len(items))
    if not items:
        log.error("no labeled captures found")
        return 1

    rows_out: list[dict] = []
    per_field_count = {"mass": 0, "resistance": 0, "instability": 0}
    per_field_skipped_reviewed = {"mass": 0, "resistance": 0, "instability": 0}
    n_capture_failed = 0

    for i, (png, label) in enumerate(items, start=1):
        if i % 25 == 0 or i == len(items):
            log.info("[%d/%d] %s", i, len(items), png.name)
        try:
            img = Image.open(png).convert("RGB")
        except Exception as exc:
            log.warning("  open failed: %s", exc)
            n_capture_failed += 1
            continue
        crops = _extract_per_row_crops(img)
        user_dir = png.parent.parent.name        # user_<timestamp>
        capture_stem = png.stem                  # cap_<timestamp>
        for field in ("mass", "resistance", "instability"):
            raw_label = label.get(field)
            norm = _norm_label(field, raw_label)
            crop = crops.get(field)
            if norm is None or crop is None:
                continue
            # ── Per-field review filter ──
            # The HUD Row Reviewer (scripts/hud_row_reviewer.py) writes
            # ``review_status_<field>`` per sidecar. When that key is
            # present and equals "rejected", the user has marked the
            # extracted crop as not matching the label (e.g. wrong
            # row content) and we skip it so it doesn't poison
            # training. Missing key or "approved"/"pending" = include.
            review_status = label.get(f"review_status_{field}", "pending")
            if review_status == "rejected":
                per_field_skipped_reviewed[field] += 1
                continue
            label_stub = _label_safe_filename(norm)
            out_name = f"{user_dir}__{capture_stem}__{label_stub}.png"
            out_path = OUT_ROOT / field / out_name
            try:
                crop.save(out_path)
            except Exception as exc:
                log.debug("  %s save failed: %s", field, exc)
                continue
            rows_out.append({
                "path": str(out_path.relative_to(TOOL)).replace("\\", "/"),
                "label": norm,
                "field": field,
                "source_capture": str(png).replace("\\", "/"),
                "review_status": review_status,
            })
            per_field_count[field] += 1

    # Write index CSV.
    if rows_out:
        with INDEX_CSV.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(
                fh, fieldnames=["path", "label", "field", "source_capture", "review_status"],
            )
            w.writeheader()
            for r in rows_out:
                w.writerow(r)

    log.info("=" * 50)
    log.info("Extraction complete:")
    for field in ("mass", "resistance", "instability"):
        log.info(
            "  %s: %d crops kept  (%d skipped as rejected)",
            field, per_field_count[field], per_field_skipped_reviewed[field],
        )
    log.info("  capture-open failures: %d", n_capture_failed)
    log.info("  index: %s (%d rows)", INDEX_CSV, len(rows_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
