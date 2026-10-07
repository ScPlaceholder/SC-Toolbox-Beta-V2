"""Take the files the build needs and git does not hold out of a published release package.

    python build/assets_from_release.py <SC_Toolbox-x.y.z-full.nupkg>

Five sets of files are too large for this repository, or are art, and are kept out of git:

  the props Pico holds            ->  tools/Pico/out/snap_props/
  Pico's default outfit pack      ->  tools/Pico/packs/
  the sound classifier (YAMNet)   ->  tools/SuitMk2/models/yamnet/yamnet.onnx and yamnet.data
  the two SuitMk2 characters      ->  build/release_assets/suitmk2_models/
  the two SuitMk2 voices          ->  build/release_assets/suitmk2_voices/

Every release package carries them, at the size they ship. This script copies them out of one into
the places build_installer.bat reads, and prints the two settings the build needs to find the
characters and the voices. A file that already exists is left alone and reported, so running this on
a machine that has the original art changes nothing there.

The package is a zip file. Nothing is downloaded: fetch the package from the project's releases page
first. Only the Python standard library is used.

Exit 0 = every file is in place, 1 = something is missing or damaged (printed), 2 = bad invocation.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile

APP = "lib/app/"
BUILD = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BUILD)
MODELS_DIR = os.path.join(BUILD, "release_assets", "suitmk2_models")
VOICES_DIR = os.path.join(BUILD, "release_assets", "suitmk2_voices")


def wanted(name: str):
    """Where a package member goes on disk, or None when it is not one of the five sets."""
    if not name.startswith(APP) or name.endswith("/"):
        return None
    rel = name[len(APP):]
    base = rel.rsplit("/", 1)[-1]
    if rel.startswith("tools/Pico/out/snap_props/"):
        return os.path.join(ROOT, *rel.split("/"))
    if rel.startswith("tools/Pico/packs/") and rel.count("/") == 3 and base.endswith(".tar.xz"):
        return os.path.join(ROOT, *rel.split("/"))
    if rel in ("tools/SuitMk2/models/yamnet/yamnet.onnx", "tools/SuitMk2/models/yamnet/yamnet.data"):
        return os.path.join(ROOT, *rel.split("/"))
    if rel.startswith("tools/SuitMk2/models/") and rel.count("/") == 3:
        if base.endswith(".delta.gguf") or base == "manifest.json":
            return os.path.join(MODELS_DIR, base)
    if rel.startswith("tools/SuitMk2/voices/") and rel.count("/") == 3:
        if base.endswith(".onnx") or base.endswith(".onnx.json"):
            return os.path.join(VOICES_DIR, base)
    return None


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list) -> int:
    if len(argv) != 1:
        print(__doc__.strip().split("\n\n")[0], file=sys.stderr)
        print("\nUsage: assets_from_release.py <SC_Toolbox-x.y.z-full.nupkg>", file=sys.stderr)
        return 2
    package = argv[0]
    if not os.path.isfile(package):
        print("[FAIL] no such file: %s" % package)
        return 1
    written = kept = 0
    seen = {"props": 0, "packs": 0, "yamnet": 0, "models": 0, "voices": 0}
    with zipfile.ZipFile(package) as archive:
        for info in archive.infolist():
            target = wanted(info.filename)
            if target is None:
                continue
            if "snap_props" in info.filename:
                seen["props"] += 1
            elif "/Pico/packs/" in info.filename:
                seen["packs"] += 1
            elif "yamnet" in info.filename:
                seen["yamnet"] += 1
            elif target.startswith(MODELS_DIR):
                seen["models"] += 1
            else:
                seen["voices"] += 1
            if os.path.exists(target):
                kept += 1
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            partial = target + ".part"
            with archive.open(info) as source, open(partial, "wb") as out:
                for block in iter(lambda: source.read(1 << 20), b""):
                    out.write(block)
            os.replace(partial, target)
            written += 1
    print("Pico props: %d files, outfit packs: %d, sound classifier: %d, character files: %d, voice files: %d"
          % (seen["props"], seen["packs"], seen["yamnet"], seen["models"], seen["voices"]))
    print("%d file(s) written, %d already there and left alone" % (written, kept))

    problems = []
    if not os.path.isfile(os.path.join(ROOT, "tools", "Pico", "out", "snap_props", "snap_props.json")):
        problems.append("the package holds no tools/Pico/out/snap_props/snap_props.json")
    if seen["packs"] < 1:
        problems.append("the package holds no Pico outfit pack")
    if seen["yamnet"] != 2:
        problems.append("expected 2 sound classifier files in the package, found %d" % seen["yamnet"])
    if seen["voices"] != 4:
        problems.append("expected 4 voice files in the package, found %d" % seen["voices"])
    manifest_path = os.path.join(MODELS_DIR, "manifest.json")
    if not os.path.isfile(manifest_path):
        problems.append("the package holds no tools/SuitMk2/models/manifest.json")
    else:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        for name, record in sorted(manifest.items()):
            path = os.path.join(MODELS_DIR, name)
            if not os.path.isfile(path):
                problems.append("%s is named in manifest.json and is not in the package" % name)
                continue
            expected = str(record.get("sha256", "")).split(":")[-1]
            actual = sha256_of(path)
            if expected and actual != expected:
                problems.append("%s does not match the sha256 in manifest.json" % name)
            else:
                print("[OK] %s matches manifest.json" % name)
    if problems:
        for line in problems:
            print("[FAIL] " + line)
        return 1
    print("")
    print("Before running the build in this window, set:")
    print('  set "SUITMK2_MODELS_SRC=%s"' % MODELS_DIR)
    print('  set "SUITMK2_VOICES_SRC=%s"' % VOICES_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
