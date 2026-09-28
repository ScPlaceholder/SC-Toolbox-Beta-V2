"""snap_rig.py — lay the art out by its OWN geometry, then move the bones to match it.

⛔ THIS IS THE CORRECTION TO assemble.py, AND THE INSTRUCTION WAS IN THE DATA THE WHOLE TIME.
   pico_skeleton.json's own `coordinate_note` reads:

       "Reference coordinates for initial rig construction. Snap pivots to final PNG geometry
        without renaming bones."

   I printed that line to my screen, then spent a tick scaling the ART to fit the BONES — the exact
   inverse. J's verdict: "Thats definitely not scaled correctly and bones aren't snapped where they
   should go." The bone coordinates are a sketch made before the art existed. The art is the truth.

★ SO THE LAYOUT COMES FROM MEASUREMENT, NOT FROM THE BONE TABLE. And the measurement source is the
  designer's own assembly: `body_front_composite.png` is Pico's torso WITH flippers and feet
  attached, which records exactly where those parts sit relative to the belly. Located the belly
  inside it by silhouette correlation rather than colour — a colour mask bled, catching highlights
  on the dark body as "white" — and got:

      belly sits at offset (52, 18) in a 391x434 composite
      belly centre   x 0.515  y 0.422      neck line   y 0.041
      belly bottom   y 0.802               flippers reach 46 px past the belly each side

⚠ WHAT IS MEASURED AND WHAT IS ASSUMED, kept separate on purpose. The BODY relationships above are
  measured from the designer's composite. The HEAD relationships — where the visor crosses the
  skull, where the beak sits under it — are NOT yet measured and are marked ASSUMED below. The
  sheet's small assembled Pico thumbnails could settle them; until they do, those numbers are mine
  and are the first suspect when the face looks wrong.

    python snap_rig.py --parts out/rig/BASE --out /tmp/snapped.png --write-skeleton /tmp/sk.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKELETON = os.path.join(HERE, "pico", "rigdata", "pico_skeleton.json")

#: MEASURED from body_front_composite.png by silhouette correlation. See the docstring.
BELLY_IN_BODY = {"cx": 0.515, "neck_y": 0.041, "bottom_y": 0.802, "flipper_reach": 46 / 391}

#: ASSUMED — not yet measured from the designer's assembled thumbnails. First suspect if the face
#: looks wrong. Fractions of the HEAD's own height.
HEAD_ASSUMED = {"visor_cy": 0.47, "beak_cy": 0.70, "head_overlap_into_body": 0.12}

#: ★ PER-PART SCALE FROM ASTRA (gpt-6-astra, 2026-09-27, 96s, read-only sandbox). J's call to put a
#:   model that READS IMAGES on the proportions, because that is the axis I kept failing on — I can
#:   measure a silhouette to the pixel and cannot see that a visor is too wide for a skull.
#: ⚠ ASTRA LABELLED EVERY VALUE "inferred" AND THE TAIL "guess" — none "measured" — and said why:
#:   "No combination is labeled measured because its pivot is inferred." Treat these as a starting
#:   point that came from looking, not as fitted numbers.
#: ★★ AND IT FOUND WHAT I HAD MISSED ENTIRELY: "Both body_back and body_front_composite visibly
#:   contain two flippers and two orange feet. body_back is not a clean torso plate." I had assumed
#:   the BACK plate was clean and layered separate flippers over a body that already had them.
PER_PART = {
    "belly": 1.0, "body_back": 1.0, "head_base": 1.0,
    "visor": 0.78, "beak": 0.5,
    "flipper_L": 0.6, "flipper_R": 0.6,
    "foot_L": 0.5, "foot_R": 0.5,
}

#: The one scale decision, made once. Pico's total height as a fraction of the canvas.
FILL = 0.68


def bbox_of(im):
    b = im.getchannel("A").point(lambda v: 255 if v > 96 else 0).getbbox()
    return b or (0, 0, im.width, im.height)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--write-skeleton", help="write the SNAPPED skeleton here (never in place)")
    ap.add_argument("--guides", action="store_true")
    a = ap.parse_args(argv)

    from PIL import Image, ImageDraw
    sk = json.load(io.open(SKELETON, encoding="utf-8"))
    W, H = sk["canvas"]["width"], sk["canvas"]["height"]

    def load(name):
        p = os.path.join(a.parts, name + ".png")
        return Image.open(p).convert("RGBA") if os.path.exists(p) else None

    parts = {n: load(n) for n in ("body_back", "belly", "head_base", "visor", "beak",
                                 "flipper_L", "flipper_R", "foot_L", "foot_R")}
    missing = [k for k, v in parts.items() if v is None]
    if missing:
        print("REFUSING: missing part(s): %s" % ", ".join(missing))
        return 2

    # ── one scale, chosen from the assembled height rather than per part ──────────────────
    belly, head = parts["belly"], parts["head_base"]
    bb, hb = bbox_of(belly), bbox_of(head)
    belly_h, head_h = bb[3] - bb[1], hb[3] - hb[1]
    foot_h = bbox_of(parts["foot_L"])[3] - bbox_of(parts["foot_L"])[1]
    raw_total = head_h * (1 - HEAD_ASSUMED["head_overlap_into_body"]) + belly_h + foot_h * 0.45
    scale = (H * FILL) / raw_total
    print("one scale for everything: %.3f   (raw stacked height %d px -> %d px, %.0f%% of canvas)"
          % (scale, raw_total, int(raw_total * scale), 100 * FILL))

    def put(canvas, im, cx, cy, placed, part=None):
        b = bbox_of(im)
        im = im.crop(b)
        s_ = scale * PER_PART.get(part, 1.0)
        im = im.resize((max(1, int(im.width * s_)), max(1, int(im.height * s_))),
                       Image.LANCZOS)
        x, y = int(cx - im.width / 2), int(cy - im.height / 2)
        canvas.alpha_composite(im, (x, y))
        placed[-1] = (x, y, im.width, im.height)
        return x, y, im.width, im.height

    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    geom, slot = {}, []

    # vertical stack: head on top, body under it, feet on the ground
    total_h = raw_total * scale
    top = (H - total_h) / 2
    head_cy = top + head_h * scale / 2
    body_cy = top + head_h * scale * (1 - HEAD_ASSUMED["head_overlap_into_body"]) + belly_h * scale / 2
    cx = W / 2

    for name, cy in (("body_back", body_cy), ("belly", body_cy)):
        slot.append(None); geom[name] = put(out, parts[name], cx, cy, slot, name)

    body_w = geom["belly"][2]
    reach = body_w * (BELLY_IN_BODY["flipper_reach"] / (1 - 2 * BELLY_IN_BODY["flipper_reach"]))
    fl_cy = body_cy - geom["belly"][3] * 0.05
    for name, sgn in (("flipper_L", -1), ("flipper_R", 1)):
        slot.append(None)
        geom[name] = put(out, parts[name], cx + sgn * (body_w / 2 + reach * 0.35), fl_cy, slot, name)

    foot_cy = geom["belly"][1] + geom["belly"][3] * 0.97
    for name, sgn in (("foot_L", -1), ("foot_R", 1)):
        slot.append(None)
        geom[name] = put(out, parts[name], cx + sgn * body_w * 0.22, foot_cy, slot, name)

    slot.append(None); geom["head_base"] = put(out, parts["head_base"], cx, head_cy, slot, "head_base")
    hh = geom["head_base"][3]
    slot.append(None)
    geom["visor"] = put(out, parts["visor"], cx,
                        geom["head_base"][1] + hh * HEAD_ASSUMED["visor_cy"], slot, "visor")
    slot.append(None)
    geom["beak"] = put(out, parts["beak"], cx,
                       geom["head_base"][1] + hh * HEAD_ASSUMED["beak_cy"], slot, "beak")

    # ── SNAP: move every bone onto the art it drives. Names never change. ────────────────
    snapped = json.loads(json.dumps(sk))
    def centre(n):
        x, y, w, h = geom[n]; return x + w / 2, y + h / 2
    targets = {
        "body": centre("belly"), "head": centre("head_base"),
        "visor": centre("visor"), "beak": centre("beak"),
        "flipper_L": (geom["flipper_L"][0] + geom["flipper_L"][2] / 2, geom["flipper_L"][1]),
        "flipper_R": (geom["flipper_R"][0] + geom["flipper_R"][2] / 2, geom["flipper_R"][1]),
        "foot_L": (geom["foot_L"][0] + geom["foot_L"][2] / 2, geom["foot_L"][1] + geom["foot_L"][3]),
        "foot_R": (geom["foot_R"][0] + geom["foot_R"][2] / 2, geom["foot_R"][1] + geom["foot_R"][3]),
    }
    moved = 0
    for b in snapped["bones"]:
        t = targets.get(b["name"])
        if t:
            b["x"], b["y"], moved = int(t[0]), int(t[1]), moved + 1
    print("snapped %d bone(s) onto the art; %d bone(s) left at their reference position"
          % (moved, len(snapped["bones"]) - moved))
    print("  ⚠ The unsnapped ones drive parts that do not exist yet (legs, props, anchors).")
    print("    Leaving them is honest; moving them would be inventing a position for absent art.")

    if a.guides:
        dr = ImageDraw.Draw(out)
        for b in snapped["bones"]:
            x, y = int(b["x"]), int(b["y"])
            col = (0, 255, 90, 230) if b["name"] in targets else (255, 0, 255, 160)
            dr.line([(x - 16, y), (x + 16, y)], fill=col, width=4)
            dr.line([(x, y - 16), (x, y + 16)], fill=col, width=4)

    out.save(a.out)
    print("wrote %s" % a.out)
    for n in ("body_back", "belly", "head_base", "visor", "beak", "flipper_L", "foot_L"):
        print("   %-12s %4dx%-4d at %s" % (n, geom[n][2], geom[n][3], (geom[n][0], geom[n][1])))
    if a.write_skeleton:
        snapped["coordinate_note"] = (
            "SNAPPED to art by snap_rig.py. Body relationships MEASURED from "
            "body_front_composite.png; head relationships (visor_cy, beak_cy) ASSUMED and unverified.")
        io.open(a.write_skeleton, "w", encoding="utf-8").write(json.dumps(snapped, indent=2))
        print("   snapped skeleton -> %s  (NEVER written in place)" % a.write_skeleton)
    return 0


if __name__ == "__main__":
    sys.exit(main())
