"""check_leftovers_stage.py - no leftover dev file is in the stage.

    staging\\python\\python.exe build\\check_leftovers_stage.py <staging_root>

The 3.0.0 package was built with 16 checkpoint copies of source files in tools\\Mining_Signals (names with ".bak"
in them) and, in skills\\DPS_Calculator, prompts and findings notes, generated reports, a backup of a data file and
the dev tools that write them. None of it was private; none of it belongs in an installer.

build_installer.bat now leaves them out: its two staging loops hand robocopy the patterns below (/XF), and delete
the named files. This script is the other half. It walks the WHOLE stage, not only those two folders, and fails
the build if a file matching a pattern, or one of the named files, is there, so a new staging step that copies a
folder some other way cannot bring them back unseen.

The patterns here and the LEFTOVERS line in build_installer.bat are the same list; this script reads that line
and fails if the two have drifted apart.

Exit code 0 when clean, 1 when a leftover is staged, 2 on bad arguments.
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
from pathlib import Path

# Anywhere in the stage, by name (matched without regard to case, as Windows and robocopy do).
PATTERNS = ("*.bak*", "*.orig", "*.elah_*", "*_PROMPT.md", "*_findings.md", "*_report.txt", "*_backup.json")
# By name, where no pattern is safe: dev tools beside the DPS Calculator and the files they write. Nothing the
# calculator runs imports or reads them (checked 2026-10-06 by searching every shipped .py and .json for each name).
NAMED = {
    "skills/DPS_Calculator": ("audit_slot_extractor.py", "erkul_config_diff.py", "erkul_full_slots.py",
                              "erkul_truth_parity.py", "refresh_erkul_cache.py", "dps_blast_radius.py",
                              "blast_baseline.json"),
}
BAT_LINE = re.compile(r'^set "LEFTOVERS=([^"]*)"\s*$', re.M)


def is_leftover(name: str) -> str:
    """The pattern a file name matches, or ""."""
    low = name.lower()
    for pat in PATTERNS:
        if fnmatch.fnmatchcase(low, pat.lower()):
            return pat
    return ""


def find(stage: Path) -> list:
    """[(path relative to the stage, why)] for every leftover staged, sorted."""
    out = []
    for root, _dirs, files in os.walk(stage):
        for f in files:
            pat = is_leftover(f)
            if pat:
                out.append(((Path(root) / f).relative_to(stage).as_posix(), "matches " + pat))
    for folder, names in NAMED.items():
        for n in names:
            if (stage / folder / n).is_file():
                out.append((folder + "/" + n, "a dev tool or its working file, named in this script"))
    return sorted(out)


def bat_patterns(bat: Path):
    """The patterns build_installer.bat hands robocopy, or None when the line is not there."""
    try:
        m = BAT_LINE.search(bat.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n"))
    except OSError:
        return None
    return tuple(m.group(1).split()) if m else None


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: check_leftovers_stage.py <staging_root>", file=sys.stderr)
        return 2
    stage = Path(sys.argv[1]).resolve()
    if not stage.is_dir():
        print("  [FAIL] leftovers: %s is not a folder" % stage)
        return 1
    rc = 0
    theirs = bat_patterns(Path(__file__).resolve().parent / "build_installer.bat")
    if theirs is None or sorted(p.lower() for p in theirs) != sorted(p.lower() for p in PATTERNS):
        print("  [FAIL] leftovers: build_installer.bat's LEFTOVERS line (%s) is not this script's list (%s)"
              % (" ".join(theirs) if theirs else "not found", " ".join(PATTERNS)))
        rc = 1
    found = find(stage)
    for rel, why in found:
        print("  [FAIL] leftovers: %s is staged (%s)" % (rel, why))
    if found:
        print("  [!!] %d leftover dev file(s) in the stage" % len(found))
        return 1
    if rc == 0:
        print("  [OK] no leftover dev files in the stage")
    return rc


if __name__ == "__main__":
    sys.exit(main())
