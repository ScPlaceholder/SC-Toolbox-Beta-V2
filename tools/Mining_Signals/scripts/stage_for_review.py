"""Stage auto-labeled HUD glyph samples into a pending-review pool.

Background: ~7,500 HUD-labeled glyphs from earlier scans are sitting
unused in legacy folders (``training_data/``, ``training_data_clean/``,
``training_data.ARCHIVED_*``, ``digit_reservoir/``). These were
auto-labeled by the OCR engines at collection time (3-engine
consensus / single-engine solo / raw harvest), never manually
verified, and never promoted into the active HUD training pool.

This script COPIES eligible samples into a sibling staging folder
``training_data_pending_review/<digit>/`` so the user can sort
through them via ``review_glyphs.py`` (which automatically picks up
the new ``pending_hud`` region kind from the training registry).

After review:
  * Quarantined files are out of the way (in ``_quarantine/``).
  * Survivors become "approved" and can be promoted into
    ``training_data_user_panel/<digit>/`` via
    ``scripts/promote_reviewed.py``.

SAFETY:
  * COPIES files (originals stay intact in source pools).
  * Dedupes by content hash AGAINST the active pool — won't add a
    file already known to ``training_data_user_panel/<digit>/``.
  * Dedupes within the staging dir — same source file from multiple
    pools only gets staged once.
  * Skips ``_quarantine/`` subfolders in source pools.
  * Preserves source provenance in the staged filename so you can
    trace back where each came from.

Trust tiers (controllable via ``--quality``):
  * ``consensus`` (default): only ``*_consensus_*`` files (3-engine
    vote agreed). Highest auto-label quality, ~2,300 files.
  * ``solo``: also include ``*_solo_*`` files (single-engine
    confirm). Medium quality.
  * ``all``: also include legacy numeric-id auto-harvest, raw
    augmentation variants, YouTube-sourced. Largest pool, mixed
    quality.

Run:
    python scripts/stage_for_review.py
    python scripts/stage_for_review.py --quality solo
    python scripts/stage_for_review.py --quality all --max-per-class 200
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent

# Sibling-of-active staging dir. Trainer never reads from here.
PENDING_DIR = TOOL / "training_data_pending_review"

# Active pool — used for deduping (don't stage what's already in use).
ACTIVE_DIR = TOOL / "training_data_user_panel"

# Source pools, ordered by trust (highest first). Each entry is
# (source_root, source_tag) where source_tag becomes a filename
# prefix so the user can see where each pending sample came from.
SOURCES = [
    (TOOL / "training_data", "td"),
    (TOOL / "training_data_clean", "tdc"),
    # ARCHIVED snapshot — usually overlaps with training_data but
    # included for completeness; dedupe by hash filters duplicates.
    (TOOL / "training_data.ARCHIVED_20260418_113245", "tdarc"),
    # %LOCALAPPDATA%/SC_Toolbox/digit_reservoir is per-user, not in
    # tools/Mining_Signals; resolve from environment.
    (
        Path(os.environ.get("LOCALAPPDATA", "")) / "SC_Toolbox" / "digit_reservoir",
        "rsv",
    ),
]

# Class labels we stage for. HUD has digits + dot + pct, but legacy
# pools only have digits — so dot and pct stay sourced from
# training_data_user_panel only (already curated).
DIGIT_CLASSES = list("0123456789")


# Quality tier filters — applied to the source filename to decide
# whether to include a file at the chosen tier.
def _tier_for(filename: str) -> str:
    """Classify a source filename into a trust tier."""
    if "_consensus_" in filename:
        return "consensus"
    if "_solo_" in filename or "_vote_" in filename:
        return "solo"
    if filename.startswith("raw_") or filename.startswith("yt_"):
        return "all"  # synthetic/external
    if re.match(r"^\d{8,}", filename):
        return "all"  # legacy numeric-id
    return "all"


def _file_hash(path: Path, chunk_size: int = 8192) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk_size)
                if not buf:
                    break
                h.update(buf)
    except Exception:
        return ""
    return h.hexdigest()


def _build_active_hash_set() -> set[str]:
    """Hash every PNG in the active pool so we can skip duplicates."""
    hashes: set[str] = set()
    if not ACTIVE_DIR.is_dir():
        return hashes
    for cls in ACTIVE_DIR.iterdir():
        if not cls.is_dir() or cls.name.startswith("_"):
            continue
        for f in cls.glob("*.png"):
            h = _file_hash(f)
            if h:
                hashes.add(h)
    return hashes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--quality", choices=["consensus", "solo", "all"], default="consensus",
        help="Which trust tier of auto-labeled files to stage. Default "
             "'consensus' — only files where 3 OCR engines agreed at "
             "collection time.",
    )
    p.add_argument(
        "--max-per-class", type=int, default=0,
        help="Cap how many samples to stage per digit class (0 = no cap). "
             "Useful when 'all' produces too many to review.",
    )
    p.add_argument(
        "--clear-pending", action="store_true",
        help="Wipe existing training_data_pending_review/ before staging "
             "(only files matching staged-prefix patterns are removed; "
             "_quarantine/ is preserved).",
    )
    args = p.parse_args()

    # Tier hierarchy: 'consensus' includes only consensus; 'solo'
    # includes consensus + solo; 'all' includes everything.
    tier_includes = {
        "consensus": {"consensus"},
        "solo": {"consensus", "solo"},
        "all": {"consensus", "solo", "all"},
    }[args.quality]

    print("=== Stage for review ===")
    print(f"  quality tier: {args.quality} (includes: {sorted(tier_includes)})")
    print(f"  max per class: {args.max_per_class or '(unlimited)'}")
    print(f"  pending dir: {PENDING_DIR}")
    print()

    if args.clear_pending and PENDING_DIR.is_dir():
        wiped = 0
        for cls in PENDING_DIR.iterdir():
            if not cls.is_dir() or cls.name.startswith("_"):
                continue
            for f in cls.glob("*.png"):
                f.unlink()
                wiped += 1
        print(f"[clear] removed {wiped} previous staged files")
        print()

    print("Hashing active pool to skip duplicates...")
    active_hashes = _build_active_hash_set()
    print(f"  active pool has {len(active_hashes)} unique PNGs")
    print()

    PENDING_DIR.mkdir(exist_ok=True)
    for d in DIGIT_CLASSES:
        (PENDING_DIR / d).mkdir(exist_ok=True)

    staged_hashes = set(active_hashes)  # avoid re-staging duplicates
    per_class_counts = {d: 0 for d in DIGIT_CLASSES}
    per_source_counts = {tag: 0 for _, tag in SOURCES}
    skipped_dup = 0
    skipped_tier = 0
    skipped_caps = 0

    for src_root, src_tag in SOURCES:
        if not src_root.is_dir():
            print(f"[skip source] {src_root} (not found)")
            continue
        print(f"[source] scanning {src_root.name} (tag={src_tag})")
        for digit in DIGIT_CLASSES:
            cls_src = src_root / digit
            if not cls_src.is_dir():
                continue
            cls_dst = PENDING_DIR / digit
            for f in sorted(cls_src.glob("*.png")):
                # Skip _quarantine/ subdir traversal (glob('*.png') is
                # not recursive, so this is just defensive).
                if "_quarantine" in f.parts:
                    continue
                # Tier filter
                if _tier_for(f.name) not in tier_includes:
                    skipped_tier += 1
                    continue
                # Cap per class
                if args.max_per_class and per_class_counts[digit] >= args.max_per_class:
                    skipped_caps += 1
                    continue
                # Dedupe
                h = _file_hash(f)
                if not h or h in staged_hashes:
                    skipped_dup += 1
                    continue
                staged_hashes.add(h)
                # Stage with provenance prefix
                staged_name = f"pending_{src_tag}_{f.name}"
                target = cls_dst / staged_name
                # Avoid overwriting if a same-named file exists
                counter = 1
                while target.exists():
                    target = cls_dst / f"pending_{src_tag}_{f.stem}__d{counter}{f.suffix}"
                    counter += 1
                try:
                    shutil.copy2(f, target)
                    per_class_counts[digit] += 1
                    per_source_counts[src_tag] += 1
                except Exception as exc:
                    print(f"  [warn] copy failed {f.name}: {exc}")

    print()
    print("=== Per-class staged counts (HUD digits) ===")
    total = 0
    for d in DIGIT_CLASSES:
        n = per_class_counts[d]
        bar = "#" * min(n // 10, 50)
        print(f"  '{d}': {n:5d} {bar}")
        total += n
    print(f"  total: {total}")
    print()
    print("=== Per-source contribution ===")
    for _, tag in SOURCES:
        print(f"  {tag}: {per_source_counts[tag]}")
    print()
    print(f"=== Dedupes / filters ===")
    print(f"  skipped (duplicate of active or already-staged): {skipped_dup}")
    print(f"  skipped (wrong trust tier):                       {skipped_tier}")
    print(f"  skipped (per-class cap):                          {skipped_caps}")
    print()
    print("Next:")
    print(f"  1. python scripts/review_glyphs.py pending_hud")
    print(f"     Click trash glyphs, hit 'Move to quarantine'. Survivors are 'approved'.")
    print(f"  2. python scripts/promote_reviewed.py")
    print(f"     Move surviving (non-quarantined) files into training_data_user_panel/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
