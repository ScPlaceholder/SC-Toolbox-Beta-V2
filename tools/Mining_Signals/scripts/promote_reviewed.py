"""Promote reviewed (non-quarantined) staging samples into the
active HUD training pool.

Workflow:
  1. ``stage_for_review.py`` copies auto-labeled samples from legacy
     pools into ``training_data_pending_review/<digit>/``.
  2. User runs ``review_glyphs.py pending_hud`` and clicks trash
     glyphs. Marked glyphs get moved to ``<digit>/_quarantine/``.
  3. THIS script moves the SURVIVING (non-quarantined) staged files
     into ``training_data_user_panel/<digit>/`` so the trainer
     picks them up on the next run. Files are renamed with a
     ``user_promoted_*`` prefix so their provenance is visible.

SAFETY:
  * Only MOVES files matching ``pending_*.png`` (the stager's prefix).
    Anything else in the staging dir is left alone.
  * Skips files in ``<digit>/_quarantine/`` — those are user-rejected.
  * Dedupes by content hash against the active pool — won't promote
    a file that's already there byte-for-byte.
  * Pass ``--dry-run`` to preview the move without touching anything.

After this script runs, retrain the HUD model:
    python ocr/train_torch.py
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent

PENDING_DIR = TOOL / "training_data_pending_review"
ACTIVE_DIR = TOOL / "training_data_user_panel"
PENDING_PREFIX = "pending_"

DIGIT_CLASSES = list("0123456789")


def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(8192)
                if not buf:
                    break
                h.update(buf)
    except Exception:
        return ""
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be promoted without moving any files.",
    )
    args = p.parse_args()

    if not PENDING_DIR.is_dir():
        print(f"No staging dir at {PENDING_DIR}; nothing to promote.")
        return 0

    print("=== Promote reviewed staging samples → active pool ===")
    print(f"  staging dir: {PENDING_DIR}")
    print(f"  active dir:  {ACTIVE_DIR}")
    print(f"  mode:        {'DRY-RUN' if args.dry_run else 'live (files will be moved)'}")
    print()

    # Hash everything currently in the active pool to skip duplicates.
    active_hashes: set[str] = set()
    if ACTIVE_DIR.is_dir():
        for cls in ACTIVE_DIR.iterdir():
            if not cls.is_dir() or cls.name.startswith("_"):
                continue
            for f in cls.glob("*.png"):
                h = _file_hash(f)
                if h:
                    active_hashes.add(h)

    per_class = {d: 0 for d in DIGIT_CLASSES}
    skipped_dup = 0
    skipped_quar = 0
    for digit in DIGIT_CLASSES:
        src_cls = PENDING_DIR / digit
        if not src_cls.is_dir():
            continue
        # Skip _quarantine/ entries — user already rejected those.
        for f in sorted(src_cls.glob(f"{PENDING_PREFIX}*.png")):
            # Glob doesn't recurse, but defensive check anyway.
            if "_quarantine" in f.parts:
                skipped_quar += 1
                continue
            h = _file_hash(f)
            if h and h in active_hashes:
                skipped_dup += 1
                continue
            active_hashes.add(h)
            # Promote: pending_<src>_<orig> → user_promoted_<src>_<orig>
            new_name = "user_promoted_" + f.name[len(PENDING_PREFIX):]
            dst_cls = ACTIVE_DIR / digit
            dst_cls.mkdir(parents=True, exist_ok=True)
            target = dst_cls / new_name
            counter = 1
            while target.exists():
                target = dst_cls / (
                    new_name[:-len(".png")] + f"__d{counter}.png"
                )
                counter += 1
            if not args.dry_run:
                try:
                    shutil.move(str(f), str(target))
                except Exception as exc:
                    print(f"  [warn] move failed {f.name}: {exc}")
                    continue
            per_class[digit] += 1

    print("=== Per-class promoted counts ===")
    total = 0
    for d in DIGIT_CLASSES:
        n = per_class[d]
        bar = "#" * min(n // 5, 50)
        print(f"  '{d}': {n:5d} {bar}")
        total += n
    print(f"  total: {total}")
    print()
    print(f"  skipped (duplicate of active pool): {skipped_dup}")
    print(f"  skipped (in _quarantine):           {skipped_quar}")
    print()
    if args.dry_run:
        print("(dry-run — re-run without --dry-run to actually move the files)")
    else:
        print(f"Done. Active pool now has {len(active_hashes)} unique HUD glyphs.")
        print()
        print("Next: retrain the HUD model with the larger corpus:")
        print("  python ocr/train_torch.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
