"""fx_play.py — play the FX clips from vfx_clips.json: standalone previews, or over rig renders.

★ WHY THIS FILE EXISTS. The contract (PICO_CONTRACT.md) puts FX in the fx_front slot at z 200
  and is explicit that the .anim format keyframes BONES ONLY — a skin swaps what is in a slot,
  and a swap is a discrete attachment, not an animated one. But an explosion that flashes,
  blooms to a fireball, and fades to smoke is inherently CONTINUOUS: region, position, scale,
  rotation, and alpha all move between keys. animate.py deliberately does not interpolate
  slot attachments, so the FX timelines live in vfx_clips.json (keyed {region,x,y,scale,rot,
  alpha} states, the slot-attachment counterpart to a bone keyframe) and THIS module is their
  player. Same philosophy as animate.py, one level up the stack: whole attachments, not bones.

★ BLENDING IS PART OF THE MAP, NOT A GUESS. vfx_map.json carries a per-sheet blend verdict
  from the alpha scans (chrono/_pico_vfx/README.md): sheets 58/59/60 are glow-on-black fields
  that never went transparent, so they SCREEN; sheet 61 is genuinely cuttable and goes
  SOURCE-OVER. Compositing everything source-over would draw faint black rectangles around
  every glow region.

⚠ WHAT IT CANNOT DO, and these are properties of the ART, not of this code:
    * Sheets 58/59/60 are INPUT-ONLY — the region boxes are hand-read to ~10px
      and the haze stays in the bbox. This player is for preview/grounding; production FX
      come from the 15 vfx/* re-render prompts in asset_manifest_reference.json.
    * Rotation sign: PIL rotates counter-clockwise, the browser preview rotates clockwise.
      fx_sprite() negates the angle to match preview.html. If a rendered GIF ever disagrees
      with the browser, the sign flip lives in exactly one place.

    python fx_play.py --list
    python fx_play.py --clip explosion --out out/fx
    python fx_play.py --all --out out/fx --fps 24
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_SHEETS_DIR = os.path.join(HERE, "assets", "reference", "sheets")

# ---------------------------------------------------------------------------
# Loading — with the same refusal discipline as animate.py's resolve_swap: a
# clip that references a region the map doesn't define is a spec bug, and the
# failure mode of guessing is an explosion that plays perfectly from the wrong
# pixels and nothing about the output would say so.
# ---------------------------------------------------------------------------

_SPECS = None


def load_specs():
    global _SPECS
    if _SPECS is not None:
        return _SPECS
    clips_path = os.path.join(HERE, "vfx_clips.json")
    map_path = os.path.join(HERE, "vfx_map.json")
    with open(clips_path, encoding="utf-8") as fh:
        clips_doc = json.load(fh)
    with open(map_path, encoding="utf-8") as fh:
        map_doc = json.load(fh)
    regions = map_doc.get("regions", {})
    clips = {c["name"]: c for c in clips_doc.get("clips", [])}
    bad = sorted({k["region"] for c in clips.values() for k in c["keys"]
                  if "region" in k and k["region"] not in regions})
    if bad:
        raise SystemExit("REFUSING: vfx_clips.json references regions missing from "
                         "vfx_map.json: %s. The map is the measured ground truth; a clip "
                         "that names a region that was never measured is not playable."
                         % ", ".join(bad))
    for c in clips.values():
        c["_filled"] = _filled_keys(c)
    _SPECS = {"clips": clips, "map": map_doc}
    return _SPECS


def _filled_keys(clip):
    """Expand keys to complete states: every field absent from a key CARRIES FORWARD.

    ⛔ ABSENT IS NOT ZERO. vfx_clips.json's spec says missing fields hold the previous
      key's value — a key that omits `alpha` means "keep the previous alpha". Reading
      absence as a default would snap scale to 1.0 and alpha to 0 mid-explosion.
    """
    full, cur = [], {"region": None, "x": 0.0, "y": 0.0,
                     "scale": 1.0, "rot": 0.0, "alpha": 1.0}
    for k in clip["keys"]:
        cur = dict(cur)
        for f in ("region", "x", "y", "scale", "rot", "alpha"):
            if f in k:
                cur[f] = k[f]
        cur["_ease"] = k.get("ease", "linear")
        cur["_t"] = k["t"]
        full.append(cur)
    return full


# ---------------------------------------------------------------------------
# Sampling — numeric fields interpolate between keys (eased per the LEFT key,
# the segment the ease describes); `region` changes discretely at its key.
# ---------------------------------------------------------------------------

def _ease_out(t):
    return 1 - (1 - t) ** 3


def _ease_in(t):
    return t ** 3


def _ease_in_out(t):
    return t * t * (3 - 2 * t)


_EASES = {"linear": lambda t: t, "out": _ease_out, "in": _ease_in, "inout": _ease_in_out}


def sample(clip, t):
    """Clip state at time t (seconds). Loops wrap; non-loops clamp at both ends."""
    keys = clip["_filled"]
    dur = clip.get("duration", 0) or 0
    if clip.get("loop") and dur > 0:
        t %= dur
    if t <= keys[0]["_t"]:
        return dict(keys[0])
    if t >= keys[-1]["_t"]:
        return dict(keys[-1])
    i = 1
    while i < len(keys) and keys[i]["_t"] < t:
        i += 1
    a, b = keys[i - 1], keys[i]
    span = b["_t"] - a["_t"]
    f = 0.0 if span <= 0 else (t - a["_t"]) / span
    f = _EASES.get(a["_ease"], _EASES["linear"])(min(1.0, max(0.0, f)))
    out = dict(a)
    out["_t"] = t
    for fld in ("x", "y", "scale", "alpha"):
        out[fld] = a[fld] + (b[fld] - a[fld]) * f
    # Shortest arc, so a loop seam of 350°->10° is a 20° sweep, not 340°.
    out["rot"] = a["rot"] + ((b["rot"] - a["rot"] + 180) % 360 - 180) * f
    out["region"] = a["region"]
    return out


# ---------------------------------------------------------------------------
# Sprites — region crops from the sheets, cached. The sheets are the same
# INPUT-ONLY reference art the map was measured from.
# ---------------------------------------------------------------------------

_REGION_CACHE = {}


def _region_image(specs, region):
    if region in _REGION_CACHE:
        return _REGION_CACHE[region]
    from PIL import Image
    r = specs["map"]["regions"][region]
    sheet = specs["map"]["sheets"][r["sheet"]]
    path = os.path.join(_SHEETS_DIR, sheet["file"])
    if not os.path.exists(path):
        raise SystemExit("REFUSING: region %r lives on %s, which is not on disk at %s."
                         % (region, sheet["file"], path))
    im = Image.open(path).convert("RGBA").crop((r["x"], r["y"],
                                                r["x"] + r["w"], r["y"] + r["h"]))
    _REGION_CACHE[region] = im
    return im


def fx_sprite(specs, clip, t):
    """(PIL RGBA image, state) for `clip` at `t`, or (None, state) when invisible."""
    # Every PIL import in this file is function-local, so `Image` is never a module global.
    #   This function uses `Image.LANCZOS` and `Image.BICUBIC` below and therefore needs its own
    #   import: without it the first scaled sprite raises NameError (seen on clip 'boost').
    from PIL import Image
    st = sample(clip, t)
    if not st["region"] or st["alpha"] <= 0:
        return None, st
    im = _region_image(specs, st["region"])
    s = st["scale"]
    if s != 1.0:
        im = im.resize((max(1, int(round(im.width * s))),
                        max(1, int(round(im.height * s)))), Image.LANCZOS)
    if st["rot"]:
        # PIL is counter-clockwise; the angle is authored against the canvas-clockwise
        # preview, so negate here and only here.
        im = im.rotate(-st["rot"], expand=True, resample=Image.BICUBIC)
    if st["alpha"] < 1.0:
        a = im.getchannel("A").point(lambda v: int(round(v * st["alpha"])))
        im.putalpha(a)
    return im, st


def _blend_mode(specs, region):
    sheet = specs["map"]["sheets"][specs["map"]["regions"][region]["sheet"]]
    return sheet.get("blend", "source-over")


def composite(base, specs, clip, t, anchor=None, blend=None):
    """Draw `clip` at `t` onto `base` (PIL RGBA), centred on `anchor` + state x/y."""
    # Both names are imported HERE, at the top, not inside the `if mode == "screen":` branch below:
    #   `Image.new` is called BEFORE that branch, and on a source-over sheet (61) the branch never
    #   runs at all. A lazy import must sit above EVERY use in its function.
    from PIL import Image, ImageChops
    im, st = fx_sprite(specs, clip, t)
    if im is None:
        return base
    if anchor is None:
        anchor = (base.width // 2, base.height // 2)
    px = int(round(anchor[0] + st["x"] - im.width / 2.0))
    py = int(round(anchor[1] + st["y"] - im.height / 2.0))
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(im, (px, py), im)
    mode = blend or _blend_mode(specs, st["region"])
    if mode == "screen":
        # Glow sheets never went transparent, so their black background must ADD
        # light, not overwrite. Scale the glow's RGB toward black by its own alpha
        # first — screen is unbounded, and an unpremultiplied haze edge would
        # otherwise lift the whole bbox at half strength.
        a = layer.getchannel("A")
        lum = Image.merge("RGB", (a, a, a))
        glow = ImageChops.multiply(layer.convert("RGB"), lum)
        merged = ImageChops.screen(base.convert("RGB"), glow)
        base.paste(merged, (0, 0), a)
    else:
        base.alpha_composite(layer)
    return base


# ---------------------------------------------------------------------------
# Rendering. Standalone previews go through animate.save_gif so the written
# file is read back and its timing verified there, not assumed here.
# ---------------------------------------------------------------------------

def _times(clip, fps):
    dur = clip.get("duration", 0) or 0
    n = max(1, int(round(dur * fps)))
    if clip.get("loop"):
        return [dur * i / n for i in range(n)]          # seam frame appears once
    return [dur * i / n for i in range(n + 1)]          # t=0 and t=dur both included


def render_gif(specs, name, out_dir, fps=24, size=360):
    """Standalone preview of one FX clip on a transparent field (save_gif flattens)."""
    from PIL import Image
    import animate as A
    clip = specs["clips"][name]
    frames = []
    for t in _times(clip, fps):
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        composite(canvas, specs, clip, t)
        frames.append(canvas)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "fx_" + name + ".gif")
    size_b, n_frames, secs = A.save_gif(frames, path, fps)
    print("   %s  %5.1f KB  %d frames  %.2fs" % (path, size_b / 1024.0, n_frames, secs))
    return path


def overlay(frames, box, scale, specs, name, fps, padded_size=(4096, 4096), anchor=None):
    """FX over frames produced by animate.render_clip, inside ITS crop box.

    `box`/`scale` are render_clip's second and third returns; the map's x/y offsets are
    master-canvas px, so they map through (anchor_padded - box origin) * scale.
    `padded_size` is master + rig_layout's pad (canvas 2048, pad W//2 -> 4096).
    """
    if anchor is None:
        anchor = (padded_size[0] // 2, padded_size[1] // 2)
    ax = (anchor[0] - box[0]) * scale
    ay = (anchor[1] - box[1]) * scale
    clip = specs["clips"][name]
    ts = _times(clip, fps)
    m = len(frames)
    n = len(ts)
    out = []
    for i, fr in enumerate(frames):
        # Stretch or squeeze the clip timeline across the host frames; a one-shot
        # FX on a longer host clip simply finishes and holds its last (faded) key.
        j = min((i * n) // m, n - 1)
        out.append(composite(fr.copy(), specs, clip, ts[j], anchor=(ax, ay)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="list clip names and exit")
    ap.add_argument("--clip", action="append", help="clip to render (repeatable)")
    ap.add_argument("--all", action="store_true", help="render every clip")
    ap.add_argument("--out", default="out/fx")
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--size", type=int, default=360)
    args = ap.parse_args(argv)

    specs = load_specs()
    if args.list:
        for name in sorted(specs["clips"]):
            c = specs["clips"][name]
            print("   %-18s %-20s %.2fs %s" % (name, c.get("manifest_id", ""),
                                               c.get("duration", 0),
                                               "loop" if c.get("loop") else "once"))
        return 0
    names = sorted(specs["clips"]) if args.all else (args.clip or [])
    if not names:
        ap.error("give --clip NAME, --all, or --list")
    unknown = [n for n in names if n not in specs["clips"]]
    if unknown:
        ap.error("unknown clips: %s (see --list)" % ", ".join(unknown))
    import animate as A
    A._claim_output(args.out)
    for name in names:
        print("fx %-18s" % name)
        render_gif(specs, name, args.out, fps=args.fps, size=args.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
