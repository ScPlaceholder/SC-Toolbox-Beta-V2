"""Check the staged Pico Pals (staging/tools/Pico) before it is packaged.

    staging\\python\\python.exe build\\check_pico_stage.py <staging_root> [<source tools\\Pico>]

The dev folder tools/Pico is about 675 MB, most of it working material (renders, reference sheets,
generators, tests). build_installer.bat stages Pico file by file; this fails the build when that
went wrong in either direction:

  1. something that is not a run-time file was staged (a folder outside the five expected ones,
     tests, backups, notes, or more than MAX_MB in total);
  2. the default outfit pack is missing or is not the file packs.json describes (sha256 and size);
  3. the props: snap_props.json and the PNGs beside it do not list the same files, a PNG was staged at
     the wrong size (build/shrink_pico_props.py: twice the largest size it is drawn, never above its
     source), or the numbers that size is worked out from no longer hold (the top of the Customise
     size slider, the default outfit's belly width);
  4. a staged .py or .json holds a home-folder path or a developer's working note;
  5. Pico cannot start from the staged copy ALONE: a fresh interpreter with an empty APPDATA and an
     empty home folder, no network, must unpack the default pack, find every loop packs.json promises,
     build the loop chooser with props, load the Game.log bridge from tools/SuitMk2/core, and find
     the sheet each outfit aura draws from.

Exit 0 = good, 1 = at least one failure (each printed), 2 = bad invocation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shrink_pico_props as shrink  # noqa: E402

MAX_MB = 80             # measured 2026-10-06: about 50 MB (props 36 MB, the default outfit pack 13 MB)
ALLOWED_DIRS = ("", "pico", "packs", "out/snap_props", "assets/reference/sheets")
ALLOWED_SUFFIXES = {".py", ".json", ".png", ".xz"}
HOME_PATH = re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]")
# A developer's working folders and note links. The shipped files were cleaned of them; this keeps them out.
DEV_NOTE = re.compile(rb"elah-audio|BrAi|_forJ|\[\[[a-z0-9]+(?:-[a-z0-9]+)+\]\]")
SLIDER = re.compile(r"self\.size\.setRange\(\s*\d+\s*,\s*(\d+)\s*\)")

PROBE = r'''
import json, os, sys
tool = sys.argv[1]
sys.path.insert(0, tool)
os.chdir(tool)
import sprite_pal
from pico import aura, events, packs, sprites
out = {}
store = sprite_pal.open_store({"packs_url": ""})          # offline by construction
loops, worn = sprite_pal.start_outfit(sprites.DEFAULT_DIR, {}, store)
out["default_dir_exists"] = sprites.DEFAULT_DIR.is_dir()
out["worn"] = worn
out["default"] = store.default_code()
catalog = sprites.Catalog.scan(loops)
out["loop_files"] = len({str(p) for p in catalog.loops.values()})
chooser = sprites.LoopChooser(catalog)
out["props"] = len(chooser.snap_props)
class Every(dict):
    def __contains__(self, key):
        return True
    def __missing__(self, key):
        return sprites.Path("x")
anywhere = sprites.Catalog(sprites.Path(sprites.DEFAULT_DIR.name), Every())
sprites.LoopChooser(anywhere)
out["reachable"] = sorted({sprites.split_snap(n)[1] for n in anywhere.loops if sprites.split_snap(n)[1]})
# the props the code names by itself, whatever the manifest holds
from pico import signs
named = set()
def take(n):
    if isinstance(n, str) and sprites.SNAP_SEP in n:
        named.add(sprites.split_snap(n)[1])
for pool in sprites.MOOD_LOOPS.values():
    for n in pool:
        take(n)
for n in list(sprites.RARE_LOOPS) + list(sprites.EVENT_LOOPS.values()) + list(sprites.HAND_LOOPS.values()):
    take(n)
for seq in sprites.GAG_SEQS.values():
    for n, _secs in seq:
        take(n)
for variants in sprites.HAND_VARIANTS.values():
    for n in variants:
        take(n)
for pids in sprites.PROP_IDLES.values():
    named.update(pids)
named.update(sprites.FOOD_BY_ENTITY.values())
named.update("sign_" + row[0] for row in signs.SIGNS)
out["named"] = sorted(named)
out["snapped_names"] = sum(1 for n in catalog.loops if sprites.SNAP_SEP in n)
out["hand_keys"] = sorted(chooser.hand)
out["sign_names"] = sum(1 for n in catalog.loops if sprites.SNAP_SEP + "sign_" in n)
out["moods"] = {m: len(p) for m, p in chooser.pools.items()}
suit = events.load_suit()
out["suit_core"] = str(suit.core)
sheets = {}
for name, a in aura.AURAS.items():
    clip, rrec, srec, sample = aura.load_clip(a.clip)
    path = os.path.join(tool, "assets", "reference", "sheets", srec["file"])
    sheets[name] = [srec["file"], os.path.isfile(path)]
out["aura_sheets"] = sheets
print("PROBE " + json.dumps(out))
'''


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: check_pico_stage.py <staging_root> [<source tools\\Pico>]", file=sys.stderr)
        return 2
    stage = Path(sys.argv[1]).resolve()
    source = Path(sys.argv[2]).resolve() if len(sys.argv) == 3 else None
    tool = stage / "tools" / "Pico"
    fails: list[str] = []

    def bad(msg: str) -> None:
        fails.append(msg)
        print("  [FAIL] Pico: " + msg)

    if not (tool / "skill.json").is_file():
        print("  [FAIL] Pico: tools/Pico/skill.json is not staged (no tile, nothing to launch)")
        return 1

    # 1. nothing but run-time files
    total = 0
    count = 0
    for root, dirs, files in os.walk(tool):
        dirs[:] = [d for d in dirs if d != "__pycache__"]         # removed later by the build's own cleanup
        rel_dir = Path(root).relative_to(tool).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        for f in files:
            p = Path(root) / f
            total += p.stat().st_size
            count += 1
            rel = (rel_dir + "/" + f) if rel_dir else f
            if rel_dir not in ALLOWED_DIRS:
                bad("unexpected file %s (only %s are run-time folders)" % (rel, ", ".join(d or "." for d in ALLOWED_DIRS)))
            elif p.suffix.lower() not in ALLOWED_SUFFIXES or ".bak" in f.lower() or f.startswith("."):
                bad("unexpected file %s (not a run-time file type)" % rel)
            elif p.suffix.lower() in (".py", ".json"):
                data = p.read_bytes()
                if HOME_PATH.search(data):
                    bad("%s holds a home-folder path" % rel)
                note = DEV_NOTE.search(data)
                if note:
                    bad("%s holds a developer's working note (%s)" % (rel, note.group(0).decode("ascii", "replace")))
    for d in ("tests", "pico/tests", "out_placed", "pico/clips", "pico/rigdata"):
        if (tool / d).exists():
            bad("%s/ is staged; it is not used at run time" % d)
    if total > MAX_MB * 1e6:
        bad("staged Pico is %.0f MB, over the %d MB limit: working files were probably copied" % (total / 1e6, MAX_MB))

    # 2. the default outfit pack is the one packs.json describes
    try:
        manifest = json.loads((tool / "packs" / "packs.json").read_text(encoding="utf-8"))
        default = manifest["default"]
        entry = next(o for o in manifest["outfits"] if o["code"] == default)
    except Exception as exc:                                    # noqa: BLE001
        bad("packs/packs.json unreadable or has no default outfit: %s" % exc)
        entry = None
    if entry is not None:
        pack = tool / "packs" / entry["pack"]
        if not pack.is_file():
            bad("default outfit pack packs/%s is missing: Pico cannot start without it" % entry["pack"])
        else:
            sha = hashlib.sha256(pack.read_bytes()).hexdigest()
            if pack.stat().st_size != entry["bytes"] or sha != entry["sha256"]:
                bad("packs/%s is not the file packs.json describes (size %d vs %d, sha256 %s...)"
                    % (entry["pack"], pack.stat().st_size, entry["bytes"], sha[:12]))

    # 3. the props: the manifest and the PNGs agree, and each PNG is the size the build stages it at
    props_dir = tool / "out" / "snap_props"
    try:
        props = json.loads((props_dir / "snap_props.json").read_text(encoding="utf-8"))
    except Exception as exc:                                    # noqa: BLE001
        bad("out/snap_props/snap_props.json unreadable: %s (no held weapons, food, signs or toys)" % exc)
        props = {}
    missing = sorted(r["png"] for r in props.values() if not (props_dir / r["png"]).is_file())
    if missing:
        bad("%d prop PNG(s) named in snap_props.json are missing, first: %s" % (len(missing), ", ".join(missing[:5])))
    named = {r["png"] for r in props.values()}
    extra = sorted(p.name for p in props_dir.glob("*.png") if p.name not in named) if props_dir.is_dir() else []
    if extra:
        bad("%d PNG(s) in out/snap_props are not in snap_props.json (nothing draws them), first: %s"
            % (len(extra), ", ".join(extra[:5])))
    if props and not missing:
        from PIL import Image
        wrong = []
        for pid, rec in props.items():
            with Image.open(props_dir / rec["png"]) as image:
                long_side = max(image.size)
            need = shrink.needed_px(float(rec["size"]))
            src = rec.get("src_size")
            if src:                                             # resized: exactly the staged size
                if abs(long_side - need) > 1 or long_side > max(src):
                    wrong.append("%s is %d px, staged size is %d" % (rec["png"], long_side, need))
            elif long_side > need:                              # not resized: it had to be small enough already
                wrong.append("%s is %d px with no src_size, over the staged size %d" % (rec["png"], long_side, need))
        if wrong:
            bad("%d prop PNG(s) are not the size build/shrink_pico_props.py stages (was the props folder "
                "copied instead of staged?), first: %s" % (len(wrong), "; ".join(wrong[:3])))
    # with the source folder at hand: exactly the props its loops use were staged, none lost on the way
    if source is not None and props:
        try:
            want = shrink.reachable_props(source)
        except Exception as exc:                                # noqa: BLE001
            bad("the props the source's loops use cannot be worked out: %s" % exc)
            want = None
        if want is not None and want != set(props):
            lost = sorted(want - set(props))
            more = sorted(set(props) - want)
            bad("staged props differ from the props the source's loops use: %d not staged (%s), %d staged "
                "that are not used (%s)" % (len(lost), ", ".join(lost[:5]), len(more), ", ".join(more[:5])))
    # the two numbers the staged size is worked out from
    try:
        top = SLIDER.search((tool / "sprite_pal.py").read_text(encoding="utf-8"))
    except OSError:
        top = None
    if top is None or int(top.group(1)) != shrink.MAX_HEIGHT:
        bad("the top of the Customise size slider is %s, shrink_pico_props.MAX_HEIGHT is %d: the props are "
            "staged for that height, change both together" % (top.group(1) if top else "not found", shrink.MAX_HEIGHT))
    if entry is not None and (tool / "packs" / entry["pack"]).is_file():
        try:
            belly = 0.0
            with tarfile.open(tool / "packs" / entry["pack"]) as tar:
                for member in tar.getmembers():
                    if member.name.endswith(".anchors.json"):
                        doc = json.load(tar.extractfile(member))
                        belly = max(belly, float(doc["belly_w"]) / float(doc["size"][1]))
            if belly > shrink.BELLY_FRAC:
                bad("the default outfit's belly is %.4f of its height, over shrink_pico_props.BELLY_FRAC %.2f: "
                    "props are staged too small" % (belly, shrink.BELLY_FRAC))
        except Exception as exc:                                # noqa: BLE001
            bad("the default outfit pack's anchors cannot be read: %s" % exc)

    # 5. it starts from the staged copy alone
    with tempfile.TemporaryDirectory(prefix="pico_stage_check_") as tmp:
        home = os.path.join(tmp, "home")
        appdata = os.path.join(tmp, "appdata")
        os.makedirs(home)
        os.makedirs(appdata)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PICO_", "QT_SCALE"))}
        env.update(APPDATA=appdata, USERPROFILE=home, HOME=home, HOMEDRIVE=home[:2], HOMEPATH=home[2:],
                   QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        try:
            run = subprocess.run([sys.executable, "-c", PROBE, str(tool)], env=env, capture_output=True,
                                 timeout=180, cwd=str(tool))
            text = (run.stdout + run.stderr).decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            run, text = None, "timed out after 180 s"
    line = next((ln for ln in text.splitlines() if ln.startswith("PROBE ")), None)
    if run is None or run.returncode != 0 or line is None:
        bad("Pico does not start from the staged copy alone:\n" + text.strip()[-1500:])
    else:
        got = json.loads(line[6:])
        if got["default_dir_exists"]:
            bad("the probe saw a developer loop folder; its home folder was not empty, so this proves nothing")
        if entry is not None and (got["worn"] != entry["code"] or got["loop_files"] != entry["loops"]):
            bad("started with outfit %r and %d loop files; packs.json promises %r with %d"
                % (got["worn"], got["loop_files"], entry["code"], entry["loops"]))
        if not got["props"] or not got["snapped_names"] or not got["hand_keys"]:
            bad("no props reached the loop chooser (props=%d, held items=%s)" % (got["props"], got["hand_keys"]))
        if props and set(got["reachable"]) != set(props):
            lost = sorted(set(got["reachable"]) - set(props))
            idle = sorted(set(props) - set(got["reachable"]))
            bad("the staged props are not the props the loops use: %d a loop uses are not staged (%s), "
                "%d staged that no loop uses (%s)" % (len(lost), ", ".join(lost[:5]), len(idle), ", ".join(idle[:5])))
        unnamed = sorted(set(got["named"]) - set(props))
        if props and unnamed:
            bad("%d prop(s) the code names are not staged, so he would hold nothing there: %s"
                % (len(unnamed), ", ".join(unnamed[:8])))
        if not got["sign_names"]:
            bad("no signs reached the loop chooser (pico/signs.py or the sign props are missing)")
        if any(n == 0 for n in got["moods"].values()):
            bad("a mood has no loop: %s" % got["moods"])
        for outfit, (sheet, there) in got["aura_sheets"].items():
            if not there:
                bad("%s's aura draws from assets/reference/sheets/%s, which is not staged" % (outfit, sheet))
        used = {sheet for sheet, _there in got["aura_sheets"].values()}
        sheets_dir = tool / "assets" / "reference" / "sheets"
        spare = sorted(p.name for p in sheets_dir.iterdir() if p.name not in used) if sheets_dir.is_dir() else []
        if spare:
            bad("assets/reference/sheets holds %s, which no aura draws from (the full reference sheets are "
                "working material and do not ship)" % ", ".join(spare[:5]))
        if not fails:
            print("  [OK] Pico starts from staging alone: outfit %s, %d loops, %d props, moods %s"
                  % (got["worn"], got["loop_files"], got["props"], got["moods"]))

    print("  [%s] Pico Pals staged: %d files, %.1f MB" % ("OK" if not fails else "!!", count, total / 1e6))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
