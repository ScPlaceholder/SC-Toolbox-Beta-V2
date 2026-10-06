"""Files that were not empty in the last release and are empty now stop the delta from being built.

    python fix_emptied_files.py <staging folder> <Releases folder> <version being packed>
    python fix_emptied_files.py --selftest

vpk makes the small update by diffing every changed file against the copy in the previous full
package. Its zstd refuses to make a patch whose result is an empty file ("Assertion failed: v != 0,
file fileio.c"), the bsdiff it falls back to divides by zero, and the whole pack fails. That happened
on the first 3.0.0 build: a library in the Mining Signals OCR sidecar shipped a 550-byte __init__.py
in 2.4.0 and an empty one in the version pip fetched this time.

A file that was empty before and is empty now is not diffed at all, and one that goes from empty to
something diffs fine. So only "had bytes, now has none" needs help. For a Python source file a single
comment line changes nothing about what it does. Any other kind of file is reported and the build
stops: this script does not guess what an empty data file should contain.

Exit 0: nothing to do, or every such file was given its comment line. Exit 1: a file it will not touch,
or the folders could not be read.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import zipfile

FILLER = b"# (empty in this version)\n"
SAFE_SUFFIXES = (".py", ".pyi")
PREFIX = "lib/app/"                      # where vpk puts the staged tree inside a full package
_FULL = re.compile(r"^SC_Toolbox-(\d+(?:\.\d+)*)-full\.nupkg$")


def _key(version: str) -> tuple:
    return tuple(int(p) for p in version.split("."))


def previous_full(releases: str, version: str):
    """The newest full package older than `version`, or None when there is none (a first release)."""
    best = None
    try:
        names = os.listdir(releases)
    except OSError:
        return None
    for name in names:
        m = _FULL.match(name)
        if m and _key(m.group(1)) < _key(version):
            if best is None or _key(m.group(1)) > _key(best[0]):
                best = (m.group(1), os.path.join(releases, name))
    return best


def emptied(stage: str, package: str) -> list:
    """Paths relative to the stage (forward slashes) that are empty now and held bytes in `package`."""
    with zipfile.ZipFile(package) as z:
        old = {i.filename: i.file_size for i in z.infolist()}
    out = []
    for folder, _dirs, files in os.walk(stage):
        for name in files:
            path = os.path.join(folder, name)
            if os.path.getsize(path) == 0:
                rel = os.path.relpath(path, stage).replace(os.sep, "/")
                if old.get(PREFIX + rel, 0) > 0:
                    out.append(rel)
    return sorted(out)


def run(stage: str, releases: str, version: str) -> int:
    prev = previous_full(releases, version)
    if prev is None:
        print("  [OK] no earlier full package in Releases: no delta will be built, nothing to check")
        return 0
    try:
        found = emptied(stage, prev[1])
    except (OSError, zipfile.BadZipFile) as ex:
        print("  [FAIL] could not compare the stage with %s: %s" % (os.path.basename(prev[1]), ex))
        return 1
    refused = [r for r in found if not r.lower().endswith(SAFE_SUFFIXES)]
    if refused:
        print("  [FAIL] these files held data in %s and are empty now; the delta cannot be built and they are "
              "not Python source, so they are left alone:" % prev[0])
        for r in refused:
            print("         " + r)
        return 1
    for r in found:
        with open(os.path.join(stage, r.replace("/", os.sep)), "wb") as fh:
            fh.write(FILLER)
        print("  [OK] was not empty in %s, empty now, given one comment line: %s" % (prev[0], r))
    if not found:
        print("  [OK] no file went from having bytes in %s to being empty" % prev[0])
    return 0


def selftest() -> int:
    checks = []

    def ck(name, ok):
        checks.append(ok)
        print("  [%s] %s" % ("OK" if ok else "FAIL", name))

    with tempfile.TemporaryDirectory() as tmp:
        stage, rel = os.path.join(tmp, "stage"), os.path.join(tmp, "Releases")
        os.makedirs(os.path.join(stage, "pkg"))
        os.makedirs(rel)

        def put(path, data):
            with open(os.path.join(stage, path), "wb") as fh:
                fh.write(data)

        def package(version, files):
            with zipfile.ZipFile(os.path.join(rel, "SC_Toolbox-%s-full.nupkg" % version), "w") as z:
                for name, data in files.items():
                    z.writestr(PREFIX + name, data)

        package("2.3.1", {"pkg/a.py": b"", "pkg/b.py": b"x = 1\n"})
        package("2.4.0", {"pkg/a.py": b"x = 1\n", "pkg/b.py": b"", "pkg/c.py": b"", "pkg/d.json": b"{}", "pkg/e.py": b"1\n"})
        put("pkg/a.py", b"")              # had bytes in 2.4.0, empty now: the case
        put("pkg/b.py", b"")              # empty before, empty now: untouched
        put("pkg/c.py", b"y = 2\n")       # empty before, bytes now: untouched
        put("pkg/d.json", b"{}")
        put("pkg/e.py", b"1\n")
        put("pkg/new.py", b"")            # not in the old package at all: untouched

        ck("the newest older full package is the one compared against",
           previous_full(rel, "3.0.0")[0] == "2.4.0" and previous_full(rel, "2.4.0")[0] == "2.3.1")
        ck("a first release has nothing to compare with", previous_full(rel, "2.3.1") is None
           and run(stage, rel, "2.3.1") == 0)
        ck("only the file that lost its bytes is found", emptied(stage, previous_full(rel, "3.0.0")[1]) == ["pkg/a.py"])
        ck("it is given the comment line and the run passes", run(stage, rel, "3.0.0") == 0
           and open(os.path.join(stage, "pkg/a.py"), "rb").read() == FILLER)
        ck("the files that were empty before, or are new, are still empty",
           os.path.getsize(os.path.join(stage, "pkg/b.py")) == 0 and os.path.getsize(os.path.join(stage, "pkg/new.py")) == 0)
        ck("a second run finds nothing to do", emptied(stage, previous_full(rel, "3.0.0")[1]) == [])
        put("pkg/d.json", b"")            # a data file that lost its bytes: refused
        ck("an emptied file that is not Python source stops the build and is not written to",
           run(stage, rel, "3.0.0") == 1 and os.path.getsize(os.path.join(stage, "pkg/d.json")) == 0)
        ck("a missing Releases folder is a first release, not a crash", run(stage, os.path.join(tmp, "nope"), "3.0.0") == 0)
        open(os.path.join(rel, "SC_Toolbox-2.9.0-full.nupkg"), "wb").write(b"not a zip")
        ck("a damaged previous package stops the build", run(stage, rel, "3.0.0") == 1)
    print("  %d of %d checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    sys.exit(run(sys.argv[1], sys.argv[2], sys.argv[3]))
