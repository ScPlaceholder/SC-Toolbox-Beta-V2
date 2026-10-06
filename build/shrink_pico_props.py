"""Stage Pico's held props (weapons, food, signs, toys, gags) no larger than they can ever be drawn.

    staging\\python\\python.exe build\\shrink_pico_props.py <tools\\Pico> <staging\\tools\\Pico\\out\\snap_props>
    python build\\shrink_pico_props.py --check-packs <folder of outfit packs>

The prop PNGs in tools/Pico/out/snap_props are the authored art: up to 1600 px on the long side, about
306 MB for 592 files. Pico draws a prop `size` belly-widths long (snap_props.json), his belly is at most
BELLY_FRAC of his height, and the Customise slider stops at MAX_HEIGHT, so the biggest a prop is ever
drawn is  size * BELLY_FRAC * MAX_HEIGHT  pixels. This writes each prop at DRAW_SCALE times that, never
larger than its source, into the STAGING folder. The source folder is only read.

  * Only props the loop chooser can name are staged (it is asked, with every loop present, the way the
    window asks it). Props in the manifest that no loop uses, and PNGs the manifest does not name, stay
    behind; the staged snap_props.json lists exactly the props that are staged.
  * A resized PNG's aspect ratio is off by a rounding step, and pico/snap.py places a prop by its aspect
    ratio. So each resized prop's record gets "src_size": the authored (w, h). snap.authored_size() reads
    it, and the prop sits where it sat with the full-size art, to the last digit.

BELLY_FRAC was measured over all 19 outfit packs on 2026-10-06: belly_w / loop height is 0.390 (Drake,
the smallest) to 0.410 (Gatac). The default pack alone understates it, so it is a constant here with
headroom, and --check-packs re-measures it: run that when an outfit is added or re-rendered.

Exit 0 = staged, 1 = something is wrong (printed), 2 = bad invocation.
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import tarfile
from pathlib import Path

MAX_HEIGHT = 560        # the top of the Customise size slider (sprite_pal.py: self.size.setRange(140, 560))
BELLY_FRAC = 0.42       # belly_w / loop height; measured maximum 0.4097, see above
DRAW_SCALE = 2          # staged at twice the largest size drawn


def needed_px(size: float) -> int:
    """The long side, in pixels, a prop of this `size` is staged at (unless its source is smaller)."""
    return int(math.ceil(size * BELLY_FRAC * MAX_HEIGHT * DRAW_SCALE))


def reachable_props(tool: Path) -> set[str]:
    """Every prop id the loop chooser can ever name, for any outfit: a catalog that has every loop."""
    sys.path.insert(0, str(tool))
    sys.dont_write_bytecode = True               # no __pycache__ left in the source tree by a build
    try:
        from pico import sprites
    finally:
        sys.path.pop(0)

    class Every(dict):
        def __contains__(self, key):
            return True

        def __missing__(self, key):
            return Path("x")

    catalog = sprites.Catalog(Path(sprites.DEFAULT_DIR.name), Every())
    sprites.LoopChooser(catalog)
    return {sprites.split_snap(name)[1] for name in catalog.loops if sprites.split_snap(name)[1]}


def measure_packs(folder: Path) -> tuple[float, list[str]]:
    """(largest belly_w / loop height, one line per pack) over every *.tar.xz in `folder`."""
    worst, lines = 0.0, []
    for pack in sorted(folder.glob("*.tar.xz")):
        top, loops = 0.0, 0
        with tarfile.open(pack) as tar:
            for member in tar.getmembers():
                if member.name.endswith(".anchors.json"):
                    doc = json.load(tar.extractfile(member))
                    top = max(top, float(doc["belly_w"]) / float(doc["size"][1]))
                    loops += 1
        lines.append("%s: %d anchor files, belly_w / height up to %.4f" % (pack.name, loops, top))
        worst = max(worst, top)
    return worst, lines


def check_packs(folder: Path) -> int:
    worst, lines = measure_packs(folder)
    for line in lines:
        print("  " + line)
    if not lines:
        print("  [FAIL] no *.tar.xz outfit packs in %s" % folder)
        return 1
    if worst > BELLY_FRAC:
        print("  [FAIL] an outfit's belly is %.4f of its height, over BELLY_FRAC %.2f: props would be staged "
              "too small for it. Raise BELLY_FRAC." % (worst, BELLY_FRAC))
        return 1
    print("  [OK] %d packs, largest belly_w / height %.4f, BELLY_FRAC %.2f" % (len(lines), worst, BELLY_FRAC))
    return 0


def stage(tool: Path, dst: Path) -> int:
    from PIL import Image

    src = tool / "out" / "snap_props"
    manifest_path = src / "snap_props.json"
    if not manifest_path.is_file():
        print("  [FAIL] Pico: %s is missing. The props are not in git (tools/Pico/out is ignored): copy "
              "out\\snap_props from the art machine before building. Refusing to stage a Pico that can "
              "hold nothing." % manifest_path)
        return 1
    if dst.resolve() == src.resolve() or src.resolve() in dst.resolve().parents:
        print("  [FAIL] Pico: the staging folder %s is the source folder; the authored art is never "
              "written to." % dst)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reach = reachable_props(tool)
    unknown = sorted(reach - set(manifest))
    if not reach:
        print("  [FAIL] Pico: the loop chooser names no props at all; refusing to stage an empty set.")
        return 1
    dst.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}
    before = after = shrunk = 0
    problems: list[str] = []
    for pid, rec in manifest.items():
        if pid not in reach:
            continue
        png = src / rec["png"]
        if not png.is_file():
            problems.append("%s: %s is missing" % (pid, rec["png"]))
            continue
        data = png.read_bytes()
        before += len(data)
        new = dict(rec)
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                width, height = image.size
                need = needed_px(float(rec["size"]))
                if max(width, height) > need:
                    k = need / float(max(width, height))
                    small = image.convert("RGBA").resize(
                        (max(1, round(width * k)), max(1, round(height * k))), Image.LANCZOS)
                    buf = io.BytesIO()
                    small.save(buf, "PNG", optimize=True)
                    data = buf.getvalue()
                    new["src_size"] = [width, height]
                    shrunk += 1
        except Exception as exc:                                  # noqa: BLE001
            problems.append("%s: %s cannot be read as an image: %s" % (pid, rec["png"], exc))
            continue
        (dst / rec["png"]).write_bytes(data)
        after += len(data)
        out[pid] = new
    if problems:
        for line in problems[:20]:
            print("  [FAIL] Pico prop " + line)
        return 1
    (dst / "snap_props.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    left = sorted(set(manifest) - set(out))
    print("  [OK] Pico props staged: %d props (%d resized, %d already small enough), %.1f MB -> %.1f MB"
          % (len(out), shrunk, len(out) - shrunk, before / 1e6, after / 1e6))
    if left:
        print("       not staged, no loop uses them: %s" % ", ".join(left))
    if unknown:
        print("       named by a loop but not in snap_props.json (he plays those loops with empty flippers): %s"
              % ", ".join(unknown[:12]))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--check-packs":
        return check_packs(Path(argv[1]))
    if len(argv) != 2:
        print("Usage: shrink_pico_props.py <tools\\Pico> <staging props folder>\n"
              "       shrink_pico_props.py --check-packs <folder of outfit packs>", file=sys.stderr)
        return 2
    os.environ.pop("PICO_LOOPS_DIR", None)       # a developer's loop folders play no part in a build
    return stage(Path(argv[0]).resolve(), Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
