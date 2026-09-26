"""Anchor-geometry evaluation harness.

Scores the pipeline's ANCHOR GEOMETRY — where the boxes actually land —
against the hand-corrected ``cap_*.boxes.json`` annotations in
``training_data_panels``. This is the complement of the value-accuracy
harness: values can read correctly while boxes drift onto the wrong
rows (downstream fallbacks hide the damage), and the value harness
stays byte-identical through geometry bugs. Every overlay/box defect
found on 2026-06-11 (resource band spanning the whole panel, a local
title search accepting lookalike text, dual-coordinate-space row
draws) was invisible to the value harness and would have been caught
here.

What is scored, per annotated panel:

  region1 (HUD scan panel):
    * ``scan_results``  — title pose from the full-frame template
      sweep (``find_scan_results_anchor``), 2-D IoU + center error.
    * ``mass_row`` / ``resistance_row`` / ``instability_row`` — row
      bands from ``_find_label_rows``, 1-D vertical IoU + y-center
      error (the rows are full-width bands; vertical placement is
      what matters downstream).
    * ``resource`` — the mineral-name band from
      ``_refine_mineral_band_above_mass`` (structural anchor with the
      parenthesized-suffix pass), 1-D vertical IoU + y-center error.

  region2 (signature pill) — the PRODUCTION detector stack, the same
  functions ``_signal_recognize_pil`` calls with the same inputs:
    * ``pill``  — ``api._find_pill_for_signal``.
    * ``icon``  — ``hud_tracker.anchors.icon_voter.localize_icon``
      (the primary consensus detector; ``signal_anchor.find_icon`` is
      only a legacy fallback and is NOT what production geometry
      comes from).
    * ``value`` — the world-model fractional derivation from the pill
      box (mirrors the arithmetic at the ``_signal_recognize_pil``
      call site, WITHOUT the icon-LHS refinement — so a small
      systematic left-edge difference vs the annotations is expected;
      placement is what is being scored).
    * ``value_legacy`` — ``signal_anchor.find_digit_crop_box``, the
      fallback used when the primaries disagree. Scored so fallback
      rot is visible — production reads can stay correct via the
      primary path while the fallback silently dies.

Detector/tracker caches are reset between panels, so this measures
COLD ACQUISITION on every panel — no cross-frame smoothing credit.

Pass criteria (summary verdict only — the numbers are the output):
    2-D anchors (title/pill/icon/value): hit when IoU >= 0.50.
    Row/band anchors (rows/resource): hit when the vertical center
    error is <= 25% of the annotated row height — the pipeline's
    bands are deliberately tighter than the annotated rows (h~24 vs
    h~58), so raw IoU under-reports correct placement; what matters
    downstream is that the band is centered on the right row.
    weak = not hit but IoU >= 0.20; miss = detector returned None.

Usage:
    python scripts/anchor_geometry_eval.py            # both regions
    python scripts/anchor_geometry_eval.py --region 1
    python scripts/anchor_geometry_eval.py --region 2
    set LIMIT=10 for a quick pass.

Writes ``%TEMP%\\_anchor_geometry_report.txt`` (summary) and
``%TEMP%\\_anchor_geometry_detail.csv`` (per-panel rows), and prints
the summary. Intended to be run alongside the value harness before
shipping any change that touches anchors, rows, bands, or templates.
"""

import platform
platform._wmi = None  # noqa: E402  (Py3.14 WMI import hang guard)

import argparse
import contextlib
import glob
import io
import json
import os
import sys

import numpy as np
from PIL import Image

_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_TOOL_ROOT)
sys.path.insert(0, _TOOL_ROOT)
sys.path.append(os.path.join(_TOOL_ROOT, "..", ".."))

import logging
logging.basicConfig(level=logging.CRITICAL)

ROAM = (
    r"C:\Users\prjgn\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)
SESSIONS = ("user_20260418_081525", "user_20260418_154408")
REPORT = os.path.join(os.environ.get("TEMP", "."), "_anchor_geometry_report.txt")
CSV = os.path.join(os.environ.get("TEMP", "."), "_anchor_geometry_detail.csv")


# ── Geometry metrics ─────────────────────────────────────────────────────

def iou_2d(a, b):
    """IoU of two (x, y, w, h) boxes."""
    ax1, ay1, ax2, ay2 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx1, by1, bx2, by2 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def iou_1d(a1, a2, b1, b2):
    """IoU of two vertical intervals [a1, a2) and [b1, b2)."""
    inter = max(0, min(a2, b2) - max(a1, b1))
    union = (a2 - a1) + (b2 - b1) - inter
    return inter / union if union > 0 else 0.0


def center_err_y(a1, a2, b1, b2):
    return abs((a1 + a2) / 2.0 - (b1 + b2) / 2.0)


# ── Panel loading ────────────────────────────────────────────────────────

def load_annotated(region_name):
    """Yield (png_path, boxes_dict) for every annotated panel."""
    out = []
    for sess in SESSIONS:
        d = os.path.join(ROAM, "training_data_panels", sess, region_name)
        for bj in sorted(glob.glob(os.path.join(d, "cap_*.boxes.json"))):
            try:
                meta = json.load(open(bj, encoding="utf-8"))
            except Exception:
                continue
            boxes = meta.get("boxes") or {}
            png = os.path.join(d, meta.get("image") or "")
            if boxes and os.path.isfile(png):
                out.append((png, boxes))
    return out


def _box_tuple(b):
    return (int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"]))


# ── Region1 scoring ──────────────────────────────────────────────────────

def eval_region1(limit=0):
    from ocr.sc_ocr import scan_results_match as srm
    from ocr.sc_ocr import api
    from ocr import onnx_hud_reader as ohr

    panels = load_annotated("region1")
    if limit:
        panels = panels[:limit]
    rows = []
    for png, gt in panels:
        # Cold acquisition per panel: clear cross-frame state so one
        # panel's pose can't subsidise (or sabotage) the next.
        try:
            srm.reset_anchor_tracker()
            srm._LAST_CALL_CACHE = None
        except Exception:
            pass
        try:
            ohr._set_current_region(None)
        except Exception:
            pass
        img = Image.open(png).convert("RGB")
        name = os.path.basename(png)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            # Title pose.
            try:
                anc = srm.find_scan_results_anchor(img)
            except Exception:
                anc = None
            # Row bands.
            try:
                lr = ohr._find_label_rows(img)
            except Exception:
                lr = {}
            # Mineral-name band (needs the mass row top as its cap).
            band = None
            try:
                me = lr.get("mass")
                band = api._refine_mineral_band_above_mass(
                    img, int(me[0]) if me else None,
                )
            except Exception:
                band = None

        # Score: title (2-D, hit = IoU >= 0.5).
        if "scan_results" in gt:
            g = _box_tuple(gt["scan_results"])
            if anc is not None:
                p = (anc["title_x"], anc["title_y"],
                     anc["title_w"], anc["title_h"])
                _iou = iou_2d(p, g)
                rows.append((name, "title", _iou,
                             center_err_y(p[1], p[1] + p[3],
                                          g[1], g[1] + g[3]),
                             _iou >= 0.50, str(p)))
            else:
                rows.append((name, "title", None, None, False, "MISS"))

        # Score: the three row bands (1-D vertical, hit = centered).
        for field, key in (("mass", "mass_row"),
                           ("resistance", "resistance_row"),
                           ("instability", "instability_row")):
            if key not in gt:
                continue
            g = _box_tuple(gt[key])
            e = lr.get(field)
            if e is not None:
                y1, y2 = int(e[0]), int(e[1])
                yerr = center_err_y(y1, y2, g[1], g[1] + g[3])
                rows.append((name, field,
                             iou_1d(y1, y2, g[1], g[1] + g[3]),
                             yerr, yerr <= 0.25 * g[3],
                             "y=%d-%d" % (y1, y2)))
            else:
                rows.append((name, field, None, None, False, "MISS"))

        # Score: resource band (1-D vertical, hit = centered).
        if "resource" in gt:
            g = _box_tuple(gt["resource"])
            if band is not None:
                y1, y2 = int(band[0]), int(band[1])
                yerr = center_err_y(y1, y2, g[1], g[1] + g[3])
                rows.append((name, "resource",
                             iou_1d(y1, y2, g[1], g[1] + g[3]),
                             yerr, yerr <= 0.25 * g[3],
                             "y=%d-%d" % (y1, y2)))
            else:
                rows.append((name, "resource", None, None, False, "MISS"))
    return rows


# ── Region2 scoring ──────────────────────────────────────────────────────

def eval_region2(limit=0):
    from ocr.sc_ocr import signal_anchor
    from ocr.sc_ocr import api
    try:
        from hud_tracker.anchors.icon_voter import localize_icon
    except Exception:
        localize_icon = None

    wm = None
    try:
        wm = api._load_region2_world_model_for_api()
    except Exception:
        wm = None
    vfrac = (wm.get("features") or {}).get("value") if wm else None

    panels = load_annotated("region2")
    if limit:
        panels = panels[:limit]
    rows = []

    def _score2d(name, anchor, pred, g):
        if pred is not None:
            p = tuple(int(v) for v in pred[:4])
            _iou = iou_2d(p, g)
            rows.append((name, anchor, _iou,
                         center_err_y(p[1], p[1] + p[3],
                                      g[1], g[1] + g[3]),
                         _iou >= 0.50, str(p)))
        else:
            rows.append((name, anchor, None, None, False, "MISS"))

    for png, gt in panels:
        try:
            signal_anchor.reset_anchor_cache()
        except Exception:
            pass
        img = Image.open(png).convert("RGB")
        rgb = np.asarray(img, dtype=np.uint8)
        gray = rgb.max(axis=2).astype(np.uint8)
        name = os.path.basename(png)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                pill = api._find_pill_for_signal(rgb)
            except Exception:
                pill = None
            icon = None
            if localize_icon is not None:
                try:
                    _loc = localize_icon(rgb)
                    if _loc is not None:
                        icon = _loc.get("bbox")
                except Exception:
                    icon = None
            # World-model fractional value box (mirrors the
            # _signal_recognize_pil derivation, minus the icon-LHS
            # refinement — see module docstring).
            val = None
            if pill is not None and vfrac:
                try:
                    _px, _py, _pw, _ph = pill
                    val = (
                        int(round(_px + float(vfrac["x_frac"]["mean"]) * _pw)),
                        int(round(_py + float(vfrac["y_frac"]["mean"]) * _ph)),
                        int(round(float(vfrac["w_frac"]["mean"]) * _pw)),
                        int(round(float(vfrac["h_frac"]["mean"]) * _ph)),
                    )
                except Exception:
                    val = None
            try:
                val_legacy = signal_anchor.find_digit_crop_box(
                    gray, rgb_image=rgb,
                )
            except Exception:
                val_legacy = None

        if "pill" in gt:
            _score2d(name, "pill", pill, _box_tuple(gt["pill"]))
        if "icon" in gt:
            _score2d(name, "icon", icon, _box_tuple(gt["icon"]))
        if "value" in gt:
            g = _box_tuple(gt["value"])
            _score2d(name, "value", val, g)
            _score2d(name, "value_legacy", val_legacy, g)
    return rows


# ── Reporting ────────────────────────────────────────────────────────────

def summarize(tag, rows, out_lines):
    by_anchor = {}
    for _name, anchor, iou, yerr, ok, _detail in rows:
        by_anchor.setdefault(anchor, []).append((iou, yerr, ok))
    out_lines.append("== %s ==" % tag)
    for anchor in sorted(by_anchor):
        vals = by_anchor[anchor]
        n = len(vals)
        misses = sum(1 for i, _, _o in vals if i is None)
        ious = sorted(i for i, _, _o in vals if i is not None)
        yerrs = sorted(y for _, y, _o in vals if y is not None)
        hit = sum(1 for _, _, o in vals if o)
        weak = sum(
            1 for i, _, o in vals if not o and i is not None and i >= 0.20
        )
        bad = sum(
            1 for i, _, o in vals if not o and i is not None and i < 0.20
        )

        def _pct(k):
            return 100.0 * k / n if n else 0.0

        med_iou = ious[len(ious) // 2] if ious else 0.0
        med_y = yerrs[len(yerrs) // 2] if yerrs else float("nan")
        p90_y = yerrs[int(0.9 * (len(yerrs) - 1))] if yerrs else float("nan")
        out_lines.append(
            "  %-12s n=%-3d hit=%3d (%5.1f%%)  weak=%-2d bad=%-2d "
            "miss=%-2d  medIoU=%.2f  y_err med/p90=%.1f/%.1fpx"
            % (anchor, n, hit, _pct(hit), weak, bad, misses,
               med_iou, med_y, p90_y)
        )
    # Worst offenders (failing rows, lowest IoU incl. misses).
    worst = sorted(
        (r for r in rows if not r[4]),
        key=lambda r: (-1.0 if r[2] is None else r[2]),
    )[:8]
    for name, anchor, iou, _y, _ok, detail in worst:
        out_lines.append(
            "    worst: %-38s %-12s IoU=%s  %s"
            % (name, anchor,
               "MISS" if iou is None else "%.2f" % iou, detail)
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", choices=("1", "2", "both"), default="both")
    args = ap.parse_args()
    limit = int(os.environ.get("LIMIT", "0") or 0)

    all_rows = []
    out_lines = ["anchor_geometry_eval"]
    if args.region in ("1", "both"):
        r1 = eval_region1(limit)
        all_rows += [("region1",) + r for r in r1]
        summarize("region1 (HUD)", r1, out_lines)
    if args.region in ("2", "both"):
        r2 = eval_region2(limit)
        all_rows += [("region2",) + r for r in r2]
        summarize("region2 (signature)", r2, out_lines)

    with open(CSV, "w", encoding="utf-8") as f:
        f.write("region,panel,anchor,iou,y_center_err,hit,detail\n")
        for reg, name, anchor, iou, yerr, ok, detail in all_rows:
            f.write("%s,%s,%s,%s,%s,%s,%s\n" % (
                reg, name, anchor,
                "" if iou is None else "%.3f" % iou,
                "" if yerr is None else "%.1f" % yerr,
                int(bool(ok)),
                detail.replace(",", ";"),
            ))
    out_lines.append("detail -> %s" % CSV)
    text = "\n".join(out_lines)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
