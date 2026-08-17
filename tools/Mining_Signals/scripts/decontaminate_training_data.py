"""Find icon-mislabeled-as-digit samples in the existing training corpus.

Walks every saved per-glyph PNG in ``training_data_user_sig_rgb/{0..9}/``
(and the WingmanAI-tree equivalent) and runs the production icon-blacklist
NCC + 4-CNN @ vote on each. Samples that trip the icon detection are
flagged as suspects — likely pre-pipeline-wired Glyph Forge sessions
where the user accidentally hit Save & Next on an icon tile that the
old UI didn't auto-skip.

Output: a CSV listing every flagged sample. The script only REPORTS;
nothing is moved or deleted. The user can review the CSV and either
delete the suspects manually OR run the script with ``--quarantine``
to move them to ``training_data_user_sig_rgb/_quarantine_decontam/``.

Run from the production tree::

    python scripts/decontaminate_training_data.py

Or with quarantine action::

    python scripts/decontaminate_training_data.py --quarantine

Output CSV: ``scripts/_decontam_suspects.csv``.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

THIS_DIR = Path(__file__).resolve().parent
TOOL_DIR = THIS_DIR.parent
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(THIS_DIR))

# Search these training-data roots in order. If a path doesn't exist
# it's skipped silently.
TRAINING_ROOTS = [
    Path(
        r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
        r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
        r"\training_data_user_sig_rgb"
    ),
    TOOL_DIR / "training_data_user_sig_rgb",
]

# Icon-detection thresholds. Match the Glyph Forge convention so the
# decontam pass agrees with the per-tile classifier the user sees in
# the labelling UI.
_BLACKLIST_NCC_THR = 0.55     # mirrors api._SIG_BLACKLIST_NCC_THR
_ICON_CNN_CONF = 0.80         # CNN must be ≥ this conf to count as @-vote
_ICON_CNN_VOTES = 2           # ≥ this many CNNs voting @ at high conf

OUTPUT_CSV = THIS_DIR / "_decontam_suspects.csv"
QUARANTINE_SUBDIR = "_quarantine_decontam"


def _ensure_blacklist_templates(api_module) -> list:
    try:
        return list(api_module._ensure_signature_blacklist_templates() or [])
    except Exception:
        return []


def _classify_one(
    png_path: Path,
    api_module,
    templates: list,
) -> dict:
    """Run icon detection on a single saved PNG. Returns a dict
    with the NCC score, per-CNN @ vote count, and the final
    ``is_icon`` verdict.
    """
    try:
        pil_rgb = Image.open(png_path).convert("RGB").resize((28, 28), Image.BILINEAR)
        pil_l = pil_rgb.convert("L")
    except Exception as exc:
        return {"err": f"open: {exc}", "is_icon": False, "ncc": 0.0, "votes": 0}

    rgb_arr = np.asarray(pil_rgb, dtype=np.uint8)
    gray_arr = np.asarray(pil_l, dtype=np.uint8)

    # Blacklist NCC (replicates api logic — keeps us out of api's
    # session caches).
    best_ncc = 0.0
    if templates:
        try:
            cand = gray_arr.astype(np.float32)
            if cand.max() > 1.5:
                cand = cand / 255.0
            cm = float(cand.mean())
            cs = float(cand.std())
            if cs >= 1e-6:
                cand_norm = (cand - cm) / cs
                for tmpl in templates:
                    val = float(np.mean(cand_norm * tmpl))
                    if val > best_ncc:
                        best_ncc = val
        except Exception:
            pass

    # 4-CNN classification.
    g_float = gray_arr.astype(np.float32) / 255.0
    g_inv = np.clip(1.0 - g_float, 0.0, 1.0).astype(np.float32)
    preds: dict[str, Optional[tuple]] = {
        "gray": None, "gray_inv": None, "rgb": None, "rgb_inv": None,
    }
    for key, fn_name, crops in (
        ("gray",     "_classify_crops_signal",     [g_float]),
        ("gray_inv", "_classify_crops_signal_inv", [g_inv]),
        ("rgb",      "_classify_crops_signal_rgb", [rgb_arr]),
        ("rgb_inv",  "_classify_crops_signal_rgb_inv", [rgb_arr]),
    ):
        try:
            fn = getattr(api_module, fn_name)
            out = fn(crops)
            if out:
                ch, cf = out[0]
                preds[key] = (str(ch), float(cf))
        except Exception:
            pass

    icon_votes_high = sum(
        1 for p in preds.values()
        if p and not p[0].isdigit() and p[1] >= _ICON_CNN_CONF
    )

    # Match Glyph Forge's leftmost-only-per-capture rule isn't
    # applicable here (we're scanning saved per-glyph crops, not
    # tile rows), so fall back to the simple 2-vote rule.
    is_icon = icon_votes_high >= _ICON_CNN_VOTES

    return {
        "ncc": best_ncc,
        "votes": icon_votes_high,
        "is_icon": is_icon,
        "preds": preds,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--quarantine",
        action="store_true",
        help="Move flagged suspects to <root>/_quarantine_decontam/<class>/",
    )
    args = ap.parse_args()

    # Lazy-load production helpers.
    print("loading production helpers...", flush=True)
    try:
        from ocr.sc_ocr import api as _api  # type: ignore
    except Exception as exc:
        print(f"FATAL: production helpers unavailable: {exc}")
        return 1
    templates = _ensure_blacklist_templates(_api)
    print(f"  {len(templates)} blacklist templates loaded")

    rows: list[dict] = []
    per_class_total: dict[str, int] = {}
    per_class_suspect: dict[str, int] = {}

    for root in TRAINING_ROOTS:
        if not root.is_dir():
            continue
        print(f"\nscanning {root}")
        for cls_dir in sorted(root.iterdir()):
            if not cls_dir.is_dir():
                continue
            if cls_dir.name.startswith("_"):
                continue
            # Skip the icon class itself — that one's SUPPOSED to
            # contain icon-shaped samples.
            if cls_dir.name in ("icon", "@"):
                continue
            cls = cls_dir.name
            for png in sorted(cls_dir.glob("*.png")):
                per_class_total[cls] = per_class_total.get(cls, 0) + 1
                result = _classify_one(png, _api, templates)
                if not result.get("is_icon"):
                    continue
                per_class_suspect[cls] = per_class_suspect.get(cls, 0) + 1
                preds = result.get("preds") or {}
                row = {
                    "path": str(png),
                    "class": cls,
                    "ncc": f"{result.get('ncc', 0.0):.3f}",
                    "icon_votes": result.get("votes", 0),
                    "gray": _fmt_pred(preds.get("gray")),
                    "gray_inv": _fmt_pred(preds.get("gray_inv")),
                    "rgb": _fmt_pred(preds.get("rgb")),
                    "rgb_inv": _fmt_pred(preds.get("rgb_inv")),
                }
                rows.append(row)
                if args.quarantine:
                    qdir = root / QUARANTINE_SUBDIR / cls
                    qdir.mkdir(parents=True, exist_ok=True)
                    dst = qdir / png.name
                    try:
                        shutil.move(str(png), str(dst))
                    except Exception as exc:
                        print(f"  quarantine failed for {png}: {exc}")

    # Write CSV.
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "path", "class", "ncc", "icon_votes",
                "gray", "gray_inv", "rgb", "rgb_inv",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    # Console summary.
    print("\n" + "-" * 60)
    print(f"Total suspects: {len(rows)} / "
          f"{sum(per_class_total.values())} samples scanned")
    print()
    print(f"{'class':<8} {'total':>8} {'suspect':>8} {'%':>6}")
    for cls in sorted(per_class_total.keys()):
        total = per_class_total[cls]
        susp = per_class_suspect.get(cls, 0)
        pct = 100.0 * susp / total if total else 0.0
        print(f"{cls:<8} {total:>8} {susp:>8} {pct:>5.1f}%")

    if args.quarantine:
        print("\nFlagged samples moved to <root>/_quarantine_decontam/")
    else:
        print("\nDry run (no files moved). Re-run with --quarantine to move.")
    print(f"\nCSV: {OUTPUT_CSV}")
    return 0


def _fmt_pred(p) -> str:
    if p is None:
        return ""
    return f"{p[0]}@{p[1]:.2f}"


if __name__ == "__main__":
    sys.exit(main())
