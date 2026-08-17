"""End-to-end HUD-OCR benchmark on labeled panel captures.

Mirror of ``compare_crnn_rgb.py`` but for the mining HUD's value rows
(mass / resistance / instability). Walks every labeled panel capture
under ``training_data_panels/user_*/region*/`` (any sidecar JSON with
``schema in {region1, region2}`` and at least one labeled value
field), monkey-patches ``capture.grab`` so the production
``scan_hud_onnx`` API consumes our pre-captured PNG instead of doing
a screen grab, and tabulates per-field exact-match accuracy.

This benchmark establishes the baseline for any HUD-CRNN work — it's
the ``compare_crnn_rgb.py`` counterpart that lets us measure whether
a new gate-0 promotion (or, later, a trained HUD-RGB-CRNN) actually
moves the needle.

Run:
    python hud_tracker/anchors/compare_crnn_hud.py

Outputs:
    hud_tracker/anchors/crnn_hud_compare.csv
    plus per-field summary to stdout.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from PIL import Image

_THIS_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _THIS_DIR.parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
for n in (
    "ocr.sc_ocr.api",
    "ocr.onnx_hud_reader",
    "ocr.sc_ocr.label_match",
    "ocr.sc_ocr.fallback",
    "ocr.sc_ocr.preprocess",
):
    logging.getLogger(n).setLevel(logging.ERROR)


# ── Where the labeled captures live ────────────────────────────────
PANEL_ROOT = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels"
)
OUTPUT_CSV = _THIS_DIR / "crnn_hud_compare.csv"


# ── Normalization for label vs OCR comparison ──────────────────────
# Labels in sidecar JSONs are stored as user-typed strings (e.g.
# ``"7260"`` or ``"0%"`` or ``"8.01"``). Production returns floats.
# Normalize both sides into a canonical comparable form so e.g.
# ``"0%"`` matches ``0.0`` and ``"8.01"`` matches ``8.01``.
def _norm_label_str(s: str) -> Optional[str]:
    """Return digit-only canonical form, or None on parse failure."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    # Strip trailing '%'
    if s.endswith("%"):
        s = s[:-1].strip()
    # Drop commas (HUD renders "10,486" but labels store "10486")
    s = s.replace(",", "")
    # Sometimes the label is literally "0" for "0.00" — make a numeric
    # comparison robust by trying float.
    try:
        v = float(s)
    except ValueError:
        return None
    # Canonicalize: integer values lose trailing ".0"; floats keep 2
    # decimal places. Resistance is always integer-percent. Mass is
    # always integer. Instability has decimals.
    if v == int(v):
        return str(int(v))
    # Floats — fix to 2-place form (matches label storage convention)
    return f"{v:.2f}"


def _norm_ocr_value(v) -> Optional[str]:
    """Normalize production's float (or None) result."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}"


def _eq(label: Optional[str], got: Optional[str]) -> bool:
    """Exact-match compare with numeric tolerance for floats.

    Treats "8.01" == "8.01", "0" == "0.00" == "0". Avoid floating
    drift biting us on near-identical reads.
    """
    if label is None and got is None:
        return True  # both unread is "agreement" but counts as miss
    if label is None or got is None:
        return False
    if label == got:
        return True
    # Allow off-by-tolerance for float values (single hundredth)
    try:
        lf = float(label)
        gf = float(got)
        return abs(lf - gf) < 0.01
    except (TypeError, ValueError):
        return False


# ── Stub for capture.grab ──────────────────────────────────────────
# scan_hud_onnx calls ``capture.grab(region)`` to get the panel image
# off-screen. We intercept that call so it returns our pre-captured
# PNG instead. The region dict's shape is preserved so any downstream
# code reading region.{x,y,w,h} still works (we pass the full image
# size as the region).
_CURRENT_IMAGE: Optional[Image.Image] = None


def _stub_grab(region: dict) -> Optional[Image.Image]:
    return _CURRENT_IMAGE


def _walk_captures(root: Path) -> list[tuple[Path, dict]]:
    items: list[tuple[Path, dict]] = []
    for sc in root.rglob("*.json"):
        if "glyphs" in sc.name:
            continue
        try:
            d = json.loads(sc.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("schema") not in ("region1", "region2"):
            continue
        png = sc.with_suffix(".png")
        if not png.is_file():
            continue
        # At least one numeric label
        if not any(d.get(k) for k in ("mass", "resistance", "instability")):
            continue
        items.append((png, d))
    return items


def main() -> int:
    from ocr.sc_ocr import api as _api
    from ocr.sc_ocr import capture as _capture

    # Monkey-patch capture.grab.
    _capture.grab = _stub_grab  # type: ignore[assignment]

    items = _walk_captures(PANEL_ROOT)
    print(f"Walking {len(items)} labeled captures from {PANEL_ROOT}")
    if not items:
        print("FATAL: no labeled captures found")
        return 1

    rows: list[dict] = []
    n_mass = n_res = n_inst = 0
    h_mass = h_res = h_inst = 0
    pv_total = 0
    t_total = 0.0
    fields = ("mass", "resistance", "instability")

    global _CURRENT_IMAGE
    for i, (png, label) in enumerate(items, start=1):
        if i % 50 == 0:
            print(f"  [{i}/{len(items)}]")
        try:
            _CURRENT_IMAGE = Image.open(png).convert("RGB")
        except Exception as exc:
            rows.append({
                "capture": png.name,
                "load_error": str(exc),
                "panel_visible": False,
            })
            continue

        # Region dict shape — full image, since the stub ignores it.
        region = {
            "x": 0,
            "y": 0,
            "w": _CURRENT_IMAGE.width,
            "h": _CURRENT_IMAGE.height,
        }

        # ── Clear ALL module-level cache state between captures ──
        # The field-lock cache (_field_lock_cache), difficulty cache
        # (_difficulty_cache), consensus buffers, etc. all key off
        # the region tuple. Multiple consecutive captures using the
        # same region key with different content will get stuck on
        # the FIRST capture's locked values — production behavior
        # we WANT for stable display, benchmark behavior that masks
        # actual per-capture accuracy. Clear before each scan so
        # the benchmark measures pure single-capture performance.
        try:
            _api.reset_all_consensus()
        except Exception:
            pass

        t0 = time.time()
        try:
            result = _api.scan_hud_onnx(region)
        except Exception as exc:
            rows.append({
                "capture": png.name,
                "scan_error": str(exc),
                "panel_visible": False,
            })
            continue
        elapsed_ms = (time.time() - t0) * 1000.0
        t_total += elapsed_ms
        if result.get("panel_visible"):
            pv_total += 1

        row = {
            "capture": png.name,
            "panel_visible": bool(result.get("panel_visible")),
            "elapsed_ms": f"{elapsed_ms:.0f}",
        }
        for f in fields:
            lbl = _norm_label_str(label.get(f))
            got = _norm_ocr_value(result.get(f))
            ok = _eq(lbl, got)
            row[f + "_label"] = lbl or ""
            row[f + "_got"] = got or ""
            row[f + "_ok"] = "1" if ok else "0"
            if lbl is not None:
                if f == "mass":
                    n_mass += 1
                    if ok: h_mass += 1
                elif f == "resistance":
                    n_res += 1
                    if ok: h_res += 1
                elif f == "instability":
                    n_inst += 1
                    if ok: h_inst += 1
        rows.append(row)

    _CURRENT_IMAGE = None

    # ── Summary ──
    n_caps = len(items)
    print()
    print("=" * 60)
    print(f"  Total captures:           {n_caps}")
    print(f"  Panel visible:            {pv_total} ({pv_total*100/max(1,n_caps):.0f}%)")
    print(f"  Mean elapsed:             {t_total/max(1,n_caps):.0f} ms")
    print("-" * 60)
    print(f"  Mass:        {h_mass}/{n_mass} ({h_mass*100/max(1,n_mass):.1f}%)")
    print(f"  Resistance:  {h_res}/{n_res} ({h_res*100/max(1,n_res):.1f}%)")
    print(f"  Instability: {h_inst}/{n_inst} ({h_inst*100/max(1,n_inst):.1f}%)")
    print("-" * 60)
    all_hits = h_mass + h_res + h_inst
    all_lbls = n_mass + n_res + n_inst
    print(f"  Overall:     {all_hits}/{all_lbls} ({all_hits*100/max(1,all_lbls):.1f}%)")
    print("=" * 60)

    # ── CSV ──
    if rows:
        cols = sorted({k for r in rows for k in r.keys()})
        OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"\nCSV: {OUTPUT_CSV}")

    # ── Per-field failure samples ──
    def _samples(field: str) -> list[dict]:
        return [r for r in rows
                if r.get(field + "_label") and r.get(field + "_ok") == "0"][:8]
    for f in fields:
        misses = _samples(f)
        if not misses:
            continue
        print(f"\nSample {f} misses:")
        for r in misses:
            print(f"  {r['capture']:<40s} label={r[f+'_label']!r:>10s} got={r[f+'_got']!r:>10s}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
