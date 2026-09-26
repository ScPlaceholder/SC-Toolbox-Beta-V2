"""Generate font-variant synthetic crops into training_data_crnn/.

Companion to ``ocr/font_variants.py`` (which does the rendering).
This script turns the variant samples into ON-DISK labeled training
data that ``ocr.train_crnn`` picks up automatically through
``training_data_crnn/manifest.json`` -- the same manifest the
label_pending flow appends to. No changes to train_crnn needed.

Why on disk and not a new streamer in pretrain_crnn: the fine-tune
path (train_crnn) is what ships in the ONNX the runtime loads, and
its real-crop augmenter already multiplies every manifest entry
(~200 variations each by default). Materializing variants as files
reuses ALL of that machinery for free.

Usage (from the Mining_Signals tool root, the Wingman env python):

    python _gen_font_variants.py --n 6000              # generate + append
    python _gen_font_variants.py --clear               # undo (removes
                                                       # font_variant entries)
    python _gen_font_variants.py --n 6000 --clear      # regenerate fresh

Then fine-tune as usual (lower --real-aug than the default: the
variant set is already large; 20-40x is plenty):

    python -m ocr.train_crnn --epochs 8 --n 20000 --lr 1e-4 \
        --batch-size 128 --real-aug 25 \
        --init-from ocr/models/model_crnn_pretrained.pt

Idempotent: re-running with --clear first leaves the manifest
containing exactly one fresh batch of font_variant_synth entries.
Real labeled crops (label_pending etc.) are never touched.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ocr.font_variants import available_fonts, iter_variant_samples

CRNN_DIR = ROOT / "training_data_crnn"
MANIFEST_PATH = CRNN_DIR / "manifest.json"
SOURCE_TAG = "font_variant_synth"

# Same safe-encoding as scripts/label_pending.py so filenames stay
# filesystem- and manifest-friendly.
def _safe_label(label: str) -> str:
    return (label.replace(".", "dot").replace("%", "pct")
                 .replace(" ", "_").replace("(", "-").replace(")", "-"))


def _load_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        return {"files": []}
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"files": []}


def _save_manifest(m: dict) -> None:
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)


def clear_variant_entries() -> int:
    """Remove all manifest entries tagged SOURCE_TAG and their files.

    Returns the number of entries removed. Manifest entries from
    other sources (real labeled crops) are left untouched.
    """
    m = _load_manifest()
    keep = []
    removed = 0
    for entry in m.get("files", []):
        if entry.get("source") == SOURCE_TAG:
            removed += 1
            try:
                (CRNN_DIR / entry["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        else:
            keep.append(entry)
    m["files"] = keep
    _save_manifest(m)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=6000,
                        help="Number of variant samples to generate (default 6000)")
    parser.add_argument("--seed", type=int, default=101)
    parser.add_argument("--clear", action="store_true",
                        help="Remove prior font_variant_synth entries (and their "
                             "PNGs) before generating. Safe: never touches "
                             "real labeled crops.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate and count samples without writing files.")
    args = parser.parse_args()

    fonts = available_fonts(ROOT)
    if not fonts:
        print("ERROR: no bundled fonts found at tool root "
              "(furore.otf / orbitron.ttf / quantico.ttf / jura.ttf)",
              file=sys.stderr)
        sys.exit(1)
    print(f"fonts: {sorted(fonts)}")

    if args.clear:
        n_removed = clear_variant_entries()
        print(f"cleared {n_removed} prior {SOURCE_TAG} entries")

    CRNN_DIR.mkdir(parents=True, exist_ok=True)
    m = _load_manifest()

    stamp = int(time.time()) % 1_000_000_000
    written = 0
    for i, (arr, label) in enumerate(iter_variant_samples(args.n, seed=args.seed)):
        if args.dry_run:
            written += 1
            continue
        fname = f"fontvar_{stamp}_{i:05d}_{_safe_label(label)}.png"
        from PIL import Image
        Image.fromarray(arr).save(CRNN_DIR / fname)
        m.setdefault("files", []).append({
            "path": fname,
            "label": label,
            "source": SOURCE_TAG,
        })
        written += 1
        if written % 1000 == 0:
            print(f"  {written}/{args.n} ...")

    if not args.dry_run:
        _save_manifest(m)
    if args.dry_run:
        print(f"done: {written} samples (dry-run, nothing written)")
    else:
        total = len(m['files'])
        print(f"done: {written} samples; manifest now has {total} entries total")
    print()
    print("Next step (from tool root):")
    print("  python -m ocr.train_crnn --epochs 8 --n 20000 --lr 1e-4 \\")
    print("      --batch-size 128 --real-aug 25 \\")
    print("      --init-from ocr/models/model_crnn_pretrained.pt")


if __name__ == "__main__":
    main()
