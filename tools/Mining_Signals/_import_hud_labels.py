"""Import J's hand-labeled HUD crops into the manifest train_crnn actually reads.

THE PROBLEM THIS SOLVES
-----------------------
There are 377 approved hand-labeled crops in ``training_data_hud_crops/index.csv``
(dated 2026-06-17). ``ocr/train_crnn.py`` reads real crops from exactly one place --
``training_data_crnn/manifest.json`` (see ``_CRNN_MANIFEST_PATH``) -- and that file
does not exist. So the labeling has never entered training. Not once.

This writes the bridge. It does NOT move, copy, or modify a single image: manifest
entries point back at the existing PNGs via a relative path, verified to resolve.

THREE TRAPS, ALL MEASURED AGAINST THE REAL LOADER BEFORE WRITING THIS
---------------------------------------------------------------------
1. ⛔ LABELS MUST BE STRINGS. ``train_crnn._load_real_samples`` skips an entry when
   ``not label`` is true. **177 of the 377 rows carry the label 0** -- 47% of the
   corpus. Emitted as JSON integers, ``not 0`` is True and those rows are dropped
   SILENTLY, with no error and no count. Half the hand-labeling would vanish and
   the training run would look completely normal. Every label here is str().

2. ⛔ THE LOADER FAILS SILENTLY BY DESIGN. Line 185 is
   ``if not path.is_file() or not label: continue`` -- a wrong path is skipped with
   no warning, and a manifest full of wrong paths trains on nothing while printing
   success. So this script RESOLVES EVERY PATH ITSELF and refuses to write if any
   entry would be skipped. The loader cannot tell you; this can.

3. ⛔ NEVER CLOBBER THE MANIFEST. The companion ``_gen_font_variants.py`` reads the
   manifest through a bare ``except: return {"files": []}`` and then writes that
   back -- so one unreadable or briefly-locked manifest silently becomes an empty
   one, taking every hand-labeled entry with it. This script writes ATOMICALLY
   (temp file + os.replace) and refuses to overwrite an existing manifest unless
   --merge is given, in which case entries from other sources are preserved
   verbatim. The synthetic variants regenerate in minutes; these labels do not.

USAGE
-----
    python _import_hud_labels.py                # DRY RUN (default) -- writes nothing
    python _import_hud_labels.py --write        # create manifest.json
    python _import_hud_labels.py --write --merge  # keep existing non-HUD entries

Dry run is the default deliberately: this is someone else's irreplaceable data.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CRNN_DIR = ROOT / "training_data_crnn"
MANIFEST_PATH = CRNN_DIR / "manifest.json"
INDEX_CSV = ROOT / "training_data_hud_crops" / "index.csv"
SOURCE_TAG = "hud_crop_labeled"


def load_rows() -> list[dict]:
    """Approved rows from index.csv. Raises rather than returning [] on failure.

    ⚠ Deliberately NOT wrapped in a bare except. An unreadable index must stop
    this script, not quietly produce an empty import that reads as 'done'.
    """
    with open(INDEX_CSV, "r", encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("review_status") == "approved"]


def build_entries(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """-> (entries, problems). Every path is resolved HERE, because the loader won't."""
    entries, problems = [], []
    for r in rows:
        rel = "../" + r["path"].replace("\\", "/")
        label = str(r.get("label", "")).strip()      # ★ str(): see trap 1
        if not label:
            problems.append("EMPTY LABEL: %s" % r.get("path"))
            continue
        if not (CRNN_DIR / rel).is_file():
            problems.append("PATH DOES NOT RESOLVE: %s" % rel)
            continue
        entries.append({
            "path": rel,
            "label": label,
            "source": SOURCE_TAG,
            "field": r.get("field", ""),
        })
    return entries, problems


def write_atomic(manifest: dict) -> None:
    """temp file + os.replace, so an interrupted write cannot truncate the manifest."""
    CRNN_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CRNN_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        os.replace(tmp, MANIFEST_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--merge", action="store_true",
                    help="keep existing entries whose source is not %s" % SOURCE_TAG)
    a = ap.parse_args()

    if not INDEX_CSV.is_file():
        print("ERROR: no index.csv at %s" % INDEX_CSV, file=sys.stderr)
        return 1

    rows = load_rows()
    entries, problems = build_entries(rows)

    print("approved rows in index.csv : %d" % len(rows))
    print("entries that RESOLVE       : %d" % len(entries))
    print("problems                   : %d" % len(problems))
    for p in problems[:10]:
        print("   %s" % p)

    labels = Counter(e["label"] for e in entries)
    zero = labels.get("0", 0)
    print("distinct labels            : %d" % len(labels))
    print("label '0' rows             : %d (%.0f%% of corpus) -- these are the ones an"
          % (zero, 100.0 * zero / max(1, len(entries))))
    print("                             integer label would have silently dropped")
    print("by field                   : %s" % dict(Counter(e["field"] for e in entries)))

    if problems:
        print("\nREFUSING TO WRITE: %d entry(s) would be SILENTLY SKIPPED by the loader."
              % len(problems))
        print("A manifest the loader skips trains on nothing and reports success.")
        return 2

    keep: list[dict] = []
    if MANIFEST_PATH.is_file():
        if not a.merge:
            print("\nREFUSING TO WRITE: %s already exists. Re-run with --merge to keep its"
                  % MANIFEST_PATH.name)
            print("non-%s entries, and note that this script never deletes images." % SOURCE_TAG)
            return 2
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            keep = [e for e in json.load(f).get("files", []) if e.get("source") != SOURCE_TAG]
        print("merging: keeping %d existing entry(s) from other sources" % len(keep))

    if not a.write:
        print("\nDRY RUN -- nothing written. Re-run with --write to commit %d entries."
              % len(entries))
        return 0

    write_atomic({"files": keep + entries})
    print("\nwrote %s : %d entry(s) (%d imported + %d preserved)"
          % (MANIFEST_PATH.name, len(keep) + len(entries), len(entries), len(keep)))
    print("train_crnn will now see these as real crops and augment each one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
