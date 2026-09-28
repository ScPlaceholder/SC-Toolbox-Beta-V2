"""rig_layout.py — ONE source of truth for where every part sits and where every bone goes.

⛔ WHY THIS FILE EXISTS, AND IT IS NOT TIDINESS. snap_rig.py derives the layout from the ART. pose.py
   read the BONE TABLE — the pre-art sketch whose own coordinate_note says to snap pivots to the
   final PNGs. So the two files placed the same penguin two different ways and the second one flew
   apart: flipper bones sat 374 px from centre while the body edge reached 268, i.e. 106 px of empty
   air on each side BEFORE anything rotated. J: "Why did it fly apart?" It never held together.

★ THE CLASS OF BUG, WHICH IS THE PART WORTH KEEPING: I had already fixed this. snap_rig exists
  BECAUSE of it and even writes a corrected skeleton to a file. Then I built the next tool against
  the original and never opened the file I had produced. A fix parked off the path the next tool
  walks is not a fix. [[a-fix-parked-off-the-hot-path-is-not-a-fix]]
  ⇒ So the remedy is not "remember to read the snapped file". It is that PIVOT, SCALE and the layout
    live in exactly one module, and a bone position is COMPUTED from the art rather than typed.

★★ THE FALSIFIABLE TEST THIS MAKES POSSIBLE, which matters more than the layout itself:
   a bone is DEFINED as the point where its anchor part's pivot lands in the snapped layout.
   Therefore posing everything at zero degrees MUST reproduce the layout exactly. `check_rest()`
   asserts that per part, in pixels. If bones and art agree at rest they agree under rotation; if
   they do not, the number names the part and the offset instead of me squinting at a penguin.
   ⚠ It is a consistency test, NOT a beauty test. It can pass on a layout that looks wrong — it
     only proves the two halves of the pipeline share one opinion. J's eye is still the judge of
     whether that opinion is any good.

⚠ WHAT IS MEASURED AND WHAT IS NOT, kept apart on purpose. Every bone carries a `provenance`:
    MEASURED  — from the designer's own composite (the body relationships only)
    DERIVED   — computed from part geometry that is itself measured (most bones)
    ASSUMED   — mine, unverified, first suspect when something looks off (head internals, eyes)
  Nothing is left at REFERENCE. If a future part arrives with no basis, say ASSUMED and say why;
  do not let it inherit the sketch silently, which is how this whole failure started.

    python rig_layout.py --parts out/rig/BASE --write /tmp/sk.json
    python rig_layout.py --selftest
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REFERENCE_SKELETON = os.path.join(HERE, "pico", "rigdata", "pico_skeleton.json")

#: Pivots INSIDE each part's own opaque bbox, as (x_fraction, y_fraction), y downward.
#: From Astra (gpt-6-astra, 2026-09-27), which labelled EVERY one "inferred" and said it would
#: verify the two flipper pivots by hand. So a pose that looks subtly wrong here is as likely a bad
#: pivot as a bad angle, and the pivots are the cheaper thing to doubt first.
PIVOT = {
    "body_back": (0.50, 0.78), "belly": (0.50, 0.05), "head_base": (0.50, 0.90),
    "visor": (0.50, 0.50), "beak": (0.50, 0.45),
    "flipper_L": (0.82, 0.10), "flipper_R": (0.18, 0.10),
    "foot_L": (0.52, 0.18), "foot_R": (0.48, 0.18),
    "tail": (0.50, 0.82),
}

#: Per-part scale relative to belly = 1.0. Astra's, and the set that made J say "Yup that's a Pico".
SCALE = {
    "belly": 1.0, "body_back": 1.0, "head_base": 1.0, "visor": 0.78, "beak": 0.5,
    "flipper_L": 0.6, "flipper_R": 0.6, "foot_L": 0.5, "foot_R": 0.5, "tail": 0.45,
}

#: Which bone drives each part, and which part DEFINES that bone's position.
#: ⚠ body_back and belly share the bone `body`. They cannot both define it — they have different
#:   pivots — so `belly` is the anchor and body_back gets an OFFSET instead. Getting this wrong is
#:   invisible: the rig still renders, one layer just sits slightly off forever.
DRIVER = {
    "body_back": "body", "belly": "body", "tail": "body",
    "head_base": "head", "visor": "visor", "beak": "beak",
    "flipper_L": "flipper_L", "flipper_R": "flipper_R",
    "foot_L": "foot_L", "foot_R": "foot_R",
}
ANCHOR = {"body": "belly", "head": "head_base", "visor": "visor", "beak": "beak",
          "flipper_L": "flipper_L", "flipper_R": "flipper_R",
          "foot_L": "foot_L", "foot_R": "foot_R"}

#: MEASURED from the designer's body_front_composite.png by silhouette correlation.
BELLY_IN_BODY = {"cx": 0.515, "neck_y": 0.041, "bottom_y": 0.802, "flipper_reach": 46 / 391}

#: ASSUMED — mine, from no measurement. Fractions of the HEAD's own height.
HEAD_ASSUMED = {"visor_cy": 0.47, "beak_cy": 0.70, "head_overlap_into_body": 0.12}

#: Pico's total height as a fraction of the canvas. The one free parameter, chosen once.
FILL = 0.68

#: Parts that must exist for a layout. `tail` is optional — Astra called its size a pure guess.
REQUIRED = ("body_back", "belly", "head_base", "visor", "beak",
            "flipper_L", "flipper_R", "foot_L", "foot_R")


def bbox_of(im):
    b = im.getchannel("A").point(lambda v: 255 if v > 96 else 0).getbbox()
    return b or (0, 0, im.width, im.height)


def load_parts(parts_dir, names=None):
    """Open, alpha-crop and return each part. Cropping here means every later fraction is a
    fraction of the ARTWORK, not of whatever transparent margin the export happened to leave."""
    from PIL import Image
    out = {}
    for n in (names or list(SCALE)):
        p = os.path.join(parts_dir, n + ".png")
        if not os.path.exists(p):
            continue
        im = Image.open(p).convert("RGBA")
        out[n] = im.crop(bbox_of(im))
    return out


def layout(parts_dir, canvas_w=2048, canvas_h=2048, fill=FILL):
    """Place the parts by their own geometry. Returns (geom, scale, parts).

    geom: name -> (x, y, w, h) of the placed, scaled part, top-left origin.
    ⚠ This is snap_rig.py's arrangement, kept deliberately rather than improved, because it is the
      one J looked at and accepted. Changing the look and the bone derivation in one step would
      leave me unable to say which change caused a difference.
    """
    parts = load_parts(parts_dir)
    missing = [n for n in REQUIRED if n not in parts]
    if missing:
        raise SystemExit("REFUSING: missing part(s): %s" % ", ".join(missing))

    def wh(n, s):
        im = parts[n]
        return max(1, int(im.width * s)), max(1, int(im.height * s))

    belly_h = parts["belly"].height
    head_h = parts["head_base"].height
    foot_h = parts["foot_L"].height
    raw_total = (head_h * (1 - HEAD_ASSUMED["head_overlap_into_body"])
                 + belly_h + foot_h * 0.45)
    scale = (canvas_h * fill) / raw_total

    total_h = raw_total * scale
    top = (canvas_h - total_h) / 2
    cx = canvas_w / 2
    head_cy = top + head_h * scale / 2
    body_cy = (top + head_h * scale * (1 - HEAD_ASSUMED["head_overlap_into_body"])
               + belly_h * scale / 2)

    geom = {}

    def place(n, ccx, ccy):
        w, h = wh(n, scale * SCALE.get(n, 1.0))
        geom[n] = (int(ccx - w / 2), int(ccy - h / 2), w, h)
        return geom[n]

    place("body_back", cx, body_cy)
    place("belly", cx, body_cy)
    body_w = geom["belly"][2]

    reach = body_w * (BELLY_IN_BODY["flipper_reach"] / (1 - 2 * BELLY_IN_BODY["flipper_reach"]))
    fl_cy = body_cy - geom["belly"][3] * 0.05
    place("flipper_L", cx - (body_w / 2 + reach * 0.35), fl_cy)
    place("flipper_R", cx + (body_w / 2 + reach * 0.35), fl_cy)

    foot_cy = geom["belly"][1] + geom["belly"][3] * 0.97
    place("foot_L", cx - body_w * 0.22, foot_cy)
    place("foot_R", cx + body_w * 0.22, foot_cy)

    place("head_base", cx, head_cy)
    hy, hh = geom["head_base"][1], geom["head_base"][3]
    place("visor", cx, hy + hh * HEAD_ASSUMED["visor_cy"])
    place("beak", cx, hy + hh * HEAD_ASSUMED["beak_cy"])
    if "tail" in parts:
        place("tail", cx, body_cy + geom["belly"][3] * 0.30)
    return geom, scale, parts


def pivot_point(geom, name):
    """Where `name`'s pivot lands on the canvas. This is the definition a bone is built from."""
    x, y, w, h = geom[name]
    fx, fy = PIVOT.get(name, (0.5, 0.5))
    return (x + w * fx, y + h * fy)


def derive_bones(geom):
    """Compute every bone position from the placed art. Returns name -> (x, y, provenance, why).

    ★ THE FIVE CO-LOCATED PAIRS ARE DELIBERATE AND ARE A FINDING, NOT A SHORTCUT.
      The reference sketch gives each limb TWO joints — shoulder_L 170 px above flipper_L, hip above
      foot, neck above head. Two joints describe a limb that BENDS. Our flippers, feet and head are
      single rigid PNGs; nothing in them can bend. So the second joint has no art to justify a
      position, and inventing an offset would make `flipper_L` an elbow — rotating it would spin the
      flipper about its middle like a propeller, which is precisely the failure pose.py's own
      docstring warns about.
      ⇒ Collapse them onto the one real joint and SAY SO, so a future bendy version knows the elbow
        was given up on purpose and not lost by accident.
    """
    B = {}

    def put(name, xy, prov, why):
        B[name] = (float(xy[0]), float(xy[1]), prov, why)

    for bone, part in ANCHOR.items():
        put(bone, pivot_point(geom, part), "DERIVED",
            "pivot of %s in the snapped layout — this is what DEFINES the bone" % part)

    bx = pivot_point(geom, "belly")[0]
    feet_bottom = max(geom["foot_L"][1] + geom["foot_L"][3], geom["foot_R"][1] + geom["foot_R"][3])
    put("root", (bx, feet_bottom), "DERIVED", "ground line = lowest pixel of the feet")

    sh_y = (B["flipper_L"][1] + B["flipper_R"][1]) / 2
    hip_y = (B["foot_L"][1] + B["foot_R"][1]) / 2
    put("spine_upper", (bx, sh_y), "DERIVED", "belly centre-line at the shoulder height the flippers set")
    put("spine_lower", (bx, hip_y), "DERIVED", "belly centre-line at the hip height the feet set")

    # ⛔⛔ `body` IS MOVED TO THE HIPS, AND THIS IS THE ONE PLACE A BONE IS NOT ITS PART'S PIVOT.
    #    J, 2026-09-27, on the dance clip: "it still looks like it's being hung by his head". He was
    #    describing the rig exactly. Astra's belly pivot is (0.50, 0.05) — top-centre — which lands
    #    the body bone 5% down the belly, level with the neck. Everything hangs off `body`, hips and
    #    feet included, so a dance rotating `body` swung the entire lower half about a point under
    #    the chin. Not a timing problem and not a bad angle: a correct rotation about a point no
    #    torso ever hinges at.
    # ★ THE DISTINCTION THE RIG WAS MISSING: where a part ATTACHES and where it ROTATES are two
    #   different questions, and most rigs get away with one bone because the answers coincide. For
    #   a belly they do not — it attaches at the neck and it pivots at the hips.
    # ⇒ The offset machinery already handles this and was built for exactly this shape: a part's
    #   pivot lands at bone + offset, so moving `body` to the hip line leaves the belly rendering in
    #   precisely the same place (verify_rest still passes to the pixel) while changing what it
    #   turns around. No art moved. Nothing was re-tuned. One coordinate.
    # ⚠ hip_L/hip_R/foot_* are children of `body` and sit AT the hip line, so they now barely move
    #   when the torso sways — which is what "feet planted" means and why the dance reads as weight
    #   shifting rather than swinging.
    put("body", (bx, hip_y), "DERIVED",
        "hip line — the belly ATTACHES at its top but ROTATES at the hips; belly's offset carries "
        "the art back up, so placement is unchanged and only the pivot moves")

    put("neck", B["head"][:2], "DERIVED",
        "COLLAPSED onto head: a rigid head PNG has one joint, and that joint is where it meets the body")
    for s in ("L", "R"):
        put("shoulder_%s" % s, B["flipper_%s" % s][:2], "DERIVED",
            "COLLAPSED onto flipper_%s: one rigid flipper image, one joint" % s)
        put("hip_%s" % s, B["foot_%s" % s][:2], "DERIVED",
            "COLLAPSED onto foot_%s: one rigid foot image, one joint" % s)
        # the distal tip is the corner opposite the pivot, in the part's own frame
        fx, fy = PIVOT["flipper_%s" % s]
        x, y, w, h = geom["flipper_%s" % s]
        put("hand_%s" % s, (x + w * (1 - fx), y + h * (1 - fy)), "DERIVED",
            "far corner of the flipper art, opposite its pivot")

    hx, hy, hw, hh = geom["head_base"]
    put("hat_anchor", (hx + hw * 0.5, hy), "DERIVED", "top-centre of the skull art")
    vx, vy, vw, vh = geom["visor"]
    put("eye_L", (vx + vw * 0.30, vy + vh * 0.5), "ASSUMED",
        "inset 30%/70% across the visor band; nothing measures where eyes sit inside a visor")
    put("eye_R", (vx + vw * 0.70, vy + vh * 0.5), "ASSUMED", "mirror of eye_L, same caveat")
    bbx, bby, bbw, bbh = geom["body_back"]
    put("back_anchor", (bbx + bbw * 0.5, bby + bbh * 0.5), "DERIVED", "centre of the rear layer")
    put("prop_anchor", B["hand_R"][:2], "ASSUMED",
        "a held prop belongs at the hand; that it is the RIGHT hand is my choice, not a measurement")
    return B


def build(parts_dir, canvas=None):
    """Return (skeleton_dict, geom, scale) with bones derived and per-slot offsets computed."""
    ref = json.load(io.open(REFERENCE_SKELETON, encoding="utf-8"))
    cw = (canvas or ref["canvas"])["width"]
    ch = (canvas or ref["canvas"])["height"]
    geom, scale, _parts = layout(parts_dir, cw, ch)
    B = derive_bones(geom)

    sk = json.loads(json.dumps(ref))
    unknown = [b["name"] for b in sk["bones"] if b["name"] not in B]
    if unknown:
        raise SystemExit("REFUSING: no derivation for bone(s) %s. Every bone must be DERIVED or "
                         "explicitly ASSUMED; silently leaving one at its sketch position is the "
                         "bug this module exists to make impossible." % ", ".join(unknown))
    #: ⛔ ZERO THE HIDDEN REST ROTATIONS, AND THE REASON IS THE BEST THING THE REST CHECK HAS DONE.
    #    The reference sketch gives flipper_L rotation 12 and flipper_R -12. layout() applies no
    #    rest rotation at all, so at "rest" the two halves of the pipeline meant different things:
    #    the pose path rotated the flippers 12° and grew them 76 px via expand=True, while the layout
    #    had them square. verify_rest caught it as dx -69 dy -42 dw +76 — a defect I would otherwise
    #    have seen only as a penguin whose arms sit oddly, and probably blamed on a pivot.
    #    ★ WHICH SIDE IS RIGHT: the no-rotation arrangement is the one J looked at and accepted
    #      ("Yup that's a Pico"), and Astra's read is that the flipper ART ALREADY slopes outward and
    #      downward from its narrow dark shoulder end. So a further 12° is a second helping of a
    #      slope the drawing has already got.
    #    ⚠ The value is PRESERVED as `reference_rotation`, not deleted. If J wants more slope it
    #      should be a pose value he can see, not a constant hiding in a bone that makes "rest" mean
    #      two different things in two files.
    zeroed = []
    for b in sk["bones"]:
        x, y, prov, why = B[b["name"]]
        b["x"], b["y"] = int(round(x)), int(round(y))
        b["provenance"], b["derivation"] = prov, why
        if b.get("rotation"):
            b["reference_rotation"] = b["rotation"]
            zeroed.append("%s %+d°" % (b["name"], b["rotation"]))
            b["rotation"] = 0
    if zeroed:
        sk["rest_rotation_note"] = (
            "Zeroed by rig_layout: %s. The sketch carried these as rest angles; layout() applies "
            "none, so they made 'rest' ambiguous between the placement and the pose path. Kept as "
            "reference_rotation. Reinstate as an explicit pose value if wanted, not as a hidden "
            "constant." % ", ".join(zeroed))

    # per-part offsets, in the driving bone's local frame, for parts that are not its anchor
    for s in sk["slots"]:
        n = s.get("name")
        if n not in DRIVER or n not in geom:
            continue
        bone = DRIVER[n]
        px, py = pivot_point(geom, n)
        s["bone"] = bone
        s["scale"] = SCALE.get(n, 1.0)
        s["pivot"] = list(PIVOT.get(n, (0.5, 0.5)))
        s["offset"] = [round(px - B[bone][0], 2), round(py - B[bone][1], 2)]
    sk["scale"] = round(scale, 6)
    sk["coordinate_note"] = (
        "DERIVED by rig_layout.py from the art in the parts directory. Every bone position is "
        "computed, none typed. Body relationships MEASURED from body_front_composite.png; head "
        "internals and eye placement ASSUMED (see each bone's `provenance`). neck/shoulder_*/hip_* "
        "are COLLAPSED onto their single real joint because the art is rigid — see derive_bones().")
    return sk, geom, scale


def rot(px, py, ox, oy, deg):
    """Rotate (px,py) about (ox,oy) by deg clockwise. Screen coords: y grows downward."""
    import math
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    dx, dy = px - ox, py - oy
    return ox + dx * c - dy * s, oy + dx * s + dy * c


def render_part(part_im, scale, pivot, bone_xy, offset, angle):
    """★ THE ONE PLACEMENT FUNCTION. Everything that draws a part calls THIS.

    Returns (image, (left, top)) ready for alpha_composite. The part is scaled, rotated about its
    own pivot by `angle`, and positioned so that pivot lands at the bone plus its offset — where
    the offset is in the BONE'S frame and therefore turns with it, which is what makes a secondary
    layer like body_back travel with the torso instead of sliding off it.

    ⚠ IT EXISTS BECAUSE HAVING TWO OF IT IS THE BUG. snap_rig placed parts by their CENTRE, pose.py
      by their PIVOT, and neither knew the other disagreed. Two placement implementations cannot be
      kept in step by discipline; there has to be one.
    """
    from PIL import Image
    w = max(1, int(part_im.width * scale))
    h = max(1, int(part_im.height * scale))
    im = part_im.resize((w, h), Image.LANCZOS)
    pvx, pvy = w * pivot[0], h * pivot[1]
    if abs(angle) < 1e-9:
        rotated, rpx, rpy = im, pvx, pvy
    else:
        rotated = im.rotate(-angle, resample=Image.BICUBIC, expand=True)
        rpx, rpy = rot(pvx, pvy, w / 2, h / 2, angle)
        rpx += (rotated.width - w) / 2
        rpy += (rotated.height - h) / 2
    ox, oy = rot(offset[0], offset[1], 0, 0, angle)
    tx, ty = bone_xy[0] + ox, bone_xy[1] + oy
    return rotated, (int(round(tx - rpx)), int(round(ty - rpy)))


def check_rest(parts_dir, canvas=None, tol=1.0, skeleton=None):
    """★ THE TEST: drive the REAL placement path at zero degrees and compare to the layout.

    Returns (ok, rows). A bone is defined as its anchor part's pivot, so at rest render_part() must
    put each part back exactly where layout() put it. Disagreement means the two halves of the
    pipeline have drifted — the condition that made the penguin fly apart, previously visible only
    as a bad-looking picture.

    ⚠ AN EARLIER VERSION OF THIS FUNCTION WAS VERY NEARLY A TAUTOLOGY and I nearly kept it: it
      compared build()'s offsets against the pivot points that DEFINED those offsets, so it could
      only fail on rounding. It would have printed a confident row of `ok` for a rig whose renderer
      was broken in any way at all. It now goes through render_part(), the same function pose.py
      calls, so a defect in the placement maths shows up here instead of in J's eye.

    ⛔⛔ AND IT IS STRUCTURALLY BLIND TO A WRONG PIVOT. NOT a gap to fix — a property to know.
      Its own negative control proved it: moving beak's pivot 25% of the part's width changed
      NOTHING. At rest the pivot is used twice and cancels — once to DEFINE the bone
      (bone = pivot_point(geom, part)) and once to PLACE the part (pivot goes to the bone). Shift it
      and both move together. A pivot error is therefore invisible at zero degrees and only appears
      under ROTATION, where it becomes an arc about the wrong centre.
      ★ Which is exactly the axis Astra labelled "inferred" for every single part and said it would
        verify the two flipper pivots by hand. So the one uncertain input is the one this test cannot
        see, and had the control not fired I would have read nine `ok` rows as covering it.
        [[a-correct-rule-can-guard-a-branch-nothing-takes]]
    ⛔⛔⛔ AND THE SAME ARGUMENT KILLED THE SCALE CONTROL TOO, WHICH IS THE REAL CHARACTERISATION.
      Corrupting SCALE["beak"] by 40% also passed, because layout() and render_part() BOTH read the
      live module in the same run, so every parameter cancels exactly as the pivot did. There is no
      number in this file that a fresh-build check_rest can detect being wrong.
      ⇒ So say what it is instead of what I wish it were: **check_rest with no `skeleton` is a
        CROSS-IMPLEMENTATION test.** It catches layout() and render_part() disagreeing about HOW to
        place a part — which is precisely the historical bug (snap_rig placed by centre, pose.py by
        pivot) and is worth having. It catches no data error at all.
      ★ To test DATA, pass `skeleton`: a path or dict written earlier. Then the two sides are
        genuinely independent — the file is fixed, the art is re-measured — and the check answers the
        question that actually bites in practice: *is the skeleton on disk still in step with the
        art?* That is the staleness pose.py will consume, since pose.py reads a file, not this
        module's globals.
      What covers pivots: check_pivots() below — does the pivot sit on artwork, and does rotation
        hold it still. Neither test can tell a defensible pivot from a better one; that is an eye's
        job and Astra said so.
    """
    if skeleton is not None:
        if isinstance(skeleton, str):
            skeleton = json.load(io.open(skeleton, encoding="utf-8"))
        sk = skeleton
        geom, _scale, _p = layout(parts_dir, sk["canvas"]["width"], sk["canvas"]["height"])
        _scale = sk.get("scale", _scale)
    else:
        sk, geom, _scale = build(parts_dir, canvas)
    bones = {b["name"]: b for b in sk["bones"]}
    parts = load_parts(parts_dir)
    rows, ok = [], True
    for s in sk["slots"]:
        n = s.get("name")
        if n not in geom or "offset" not in s or n not in parts:
            continue
        b = bones[s["bone"]]
        im, (left, top) = render_part(parts[n], _scale * s["scale"], s["pivot"],
                                      (b["x"], b["y"]), s["offset"], 0.0)
        gx, gy, gw, gh = geom[n]
        dx, dy = left - gx, top - gy
        dw, dh = im.width - gw, im.height - gh
        good = max(abs(dx), abs(dy), abs(dw), abs(dh)) <= tol
        ok = ok and good
        rows.append((n, s["bone"], dx, dy, dw, dh, good))
    return ok, rows


def check_pivots(parts_dir, tol=1.5):
    """What check_rest cannot see. Two questions a pivot can be wrong about on its own:

    1. IS IT ON ARTWORK? A shoulder pivot sitting in transparent space means the flipper swings from
       thin air — the arc is centred outside the limb and the part visibly slides. This is a real
       check with a real failure mode, not a formality.
       ⚠ It is NECESSARY, NOT SUFFICIENT. A pivot can sit squarely on opaque pixels and still be the
         wrong point (a flipper swung from its tip is on artwork and looks like a propeller). This
         catches the absurd, not the merely wrong.
    2. DOES ROTATION HOLD IT STILL? render_part must keep the pivot pixel fixed as the angle
       changes. If it drifts, every limb wanders as it swings and no amount of angle-tuning fixes it.

    Returns (ok, rows) with rows = (part, on_art, alpha, max_drift_px, good).
    """
    geom, scale, parts = layout(parts_dir)
    rows, ok = [], True
    for n in sorted(parts):
        if n not in PIVOT:
            continue
        im = parts[n]
        fx, fy = PIVOT[n]
        sx = min(im.width - 1, max(0, int(im.width * fx)))
        sy = min(im.height - 1, max(0, int(im.height * fy)))
        alpha = im.getchannel("A").getpixel((sx, sy))
        on_art = alpha > 96
        # rotation must not move the pivot: place at a known bone and watch where the pivot lands
        bone = (1000.0, 1000.0)
        drift = 0.0
        for ang in (-90, -45, -7, 7, 45, 90, 180):
            r, (left, top) = render_part(im, scale * SCALE.get(n, 1.0), (fx, fy), bone, (0, 0), ang)
            # recover where the pivot actually landed, independently of render_part's own maths
            w = max(1, int(im.width * scale * SCALE.get(n, 1.0)))
            h = max(1, int(im.height * scale * SCALE.get(n, 1.0)))
            pvx, pvy = w * fx, h * fy
            if ang:
                px, py = rot(pvx, pvy, w / 2, h / 2, ang)
                px += (r.width - w) / 2
                py += (r.height - h) / 2
            else:
                px, py = pvx, pvy
            drift = max(drift, abs((left + px) - bone[0]), abs((top + py) - bone[1]))
        good = on_art and drift <= tol
        ok = ok and good
        rows.append((n, on_art, alpha, round(drift, 3), good))
    return ok, rows


def selftest():
    """Mechanical, no art required for the parts that need none, and it must FAIL on a broken rig."""
    fails = []
    # 1. every reference bone has a derivation branch
    ref = json.load(io.open(REFERENCE_SKELETON, encoding="utf-8"))
    names = {b["name"] for b in ref["bones"]}
    fake = {n: (0, 0, 100, 200) for n in list(SCALE)}
    try:
        B = derive_bones(fake)
    except Exception as e:  # noqa: BLE001
        fails.append("derive_bones raised on a synthetic layout: %r" % e)
        B = {}
    for n in sorted(names - set(B)):
        fails.append("bone %s has no derivation" % n)
    # 2. the collapsed pairs really are collapsed — if a future edit separates them by accident,
    #    the rig gains a phantom elbow and nothing else complains.
    for a, b in (("neck", "head"), ("shoulder_L", "flipper_L"), ("shoulder_R", "flipper_R"),
                 ("hip_L", "foot_L"), ("hip_R", "foot_R")):
        if B and B[a][:2] != B[b][:2]:
            fails.append("%s and %s are meant to be collapsed and are not" % (a, b))
    # 3. ANCHOR must not claim a part that is not in DRIVER, or offsets become meaningless
    for bone, part in ANCHOR.items():
        if DRIVER.get(part) != bone:
            fails.append("ANCHOR[%s]=%s contradicts DRIVER" % (bone, part))
    # 4. the shared-bone case: body_back and belly must both name `body`, or the offset logic is
    #    silently doing nothing and the bug returns in a new disguise
    if DRIVER.get("body_back") != "body" or DRIVER.get("belly") != "body":
        fails.append("body_back/belly no longer share the `body` bone")
    # 5. NEGATIVE CONTROL: a deliberately wrong pivot must move a bone. A test that cannot fail is
    #    the thing I keep building by accident.
    if B:
        saved = PIVOT["flipper_L"]
        PIVOT["flipper_L"] = (0.0, 0.0)
        moved = derive_bones(fake)["shoulder_L"][:2] != B["shoulder_L"][:2]
        PIVOT["flipper_L"] = saved
        if not moved:
            fails.append("control failed: corrupting a pivot did not move its bone, so this "
                         "selftest proves nothing")
    # 6. NEGATIVE CONTROL ON THE REST TEST ITSELF, which is the check I would otherwise trust
    #    blindest. A rest test that cannot fail is worse than none: it reports `ok` on a broken
    #    renderer and I stop looking. So nudge one pivot and demand check_rest NOTICE.
    groups = 5
    art = os.path.join(HERE, "out", "rig", "BASE")
    if os.path.isdir(art):
        groups = 6
        try:
            clean, _ = check_rest(art)
            if not clean:
                fails.append("check_rest fails on the real BASE art at rest — that is a genuine "
                             "finding, not a test bug; read its rows")
            # ⛔⛔ THE CONTROL CORRUPTS THE WRITTEN SKELETON, NOT A MODULE CONSTANT, AND IT TOOK TWO
            #    FAILED ATTEMPTS TO WORK OUT WHY THAT IS THE ONLY VERSION THAT CAN FIRE.
            #    Attempt 1 moved beak's PIVOT by 25% of its width: passed. Attempt 2 grew beak's
            #    SCALE by 40%: also passed. Both because layout() and render_part() read the same
            #    live globals in one run, so any parameter change moves both sides together and
            #    cancels. Corrupting a constant can never fail a self-consistency check.
            #    ⇒ Only a SEPARATE artefact makes the two sides independent. So write a skeleton,
            #      damage one offset in it, and demand check_rest catch it — which is also the real
            #      failure this guards: a skeleton file left behind when the art moved on.
            sk_good, _g, _s = build(art)
            sk_bad = json.loads(json.dumps(sk_good))
            hit = False
            for s in sk_bad["slots"]:
                if s.get("name") == "beak" and "offset" in s:
                    s["offset"][0] += 40
                    hit = True
            if not hit:
                fails.append("control could not be set up: no beak slot with an offset to damage")
            else:
                dirty, rows = check_rest(art, skeleton=sk_bad)
                if dirty:
                    fails.append("control failed: a 40px offset error in the written skeleton "
                                 "passed check_rest, so it cannot detect staleness and proves "
                                 "nothing")
                elif not any(r[0] == "beak" and not r[-1] for r in rows):
                    fails.append("control mislocated: damaging beak's offset flagged another part")
                clean2, _ = check_rest(art, skeleton=sk_good)
                if not clean2:
                    fails.append("the undamaged written skeleton FAILS its own art check — the "
                                 "control has no working baseline, so its pass means nothing")
            # and the pivots, which the above is blind to by construction
            pok, prows = check_pivots(art)
            groups = 7
            if not pok:
                for n, on_art, alpha, drift, good in prows:
                    if not good:
                        fails.append("pivot %s: on_art=%s alpha=%d rotation_drift=%.2fpx"
                                     % (n, on_art, alpha, drift))
        except Exception as e:  # noqa: BLE001
            fails.append("check_rest raised on real art: %r" % e)
    else:
        print("   NOTE: %s absent, so the rest-test control did NOT run. Five groups, not six —"
              " and an unrun control is not a passed one." % art)
    print("rig_layout selftest: %d check group(s), %d failure(s)" % (groups, len(fails)))
    for f in fails:
        print("   FAIL %s" % f)
    return 0 if not fails else 1


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts")
    ap.add_argument("--write", help="write the derived skeleton here (NEVER in place)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.parts:
        print("need --parts (or --selftest)")
        return 2

    sk, geom, scale = build(a.parts)
    print("one scale for everything: %.4f    canvas %dx%d"
          % (scale, sk["canvas"]["width"], sk["canvas"]["height"]))
    counts = {}
    for b in sk["bones"]:
        counts[b["provenance"]] = counts.get(b["provenance"], 0) + 1
    print("bones: %d total — %s" % (len(sk["bones"]),
          ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items()))))
    print("   none left at REFERENCE, which is the whole point of this module")
    for b in sk["bones"]:
        print("   %-12s %-9s (%4d,%4d)  %s" % (b["name"], b["provenance"], b["x"], b["y"],
                                               b["derivation"][:66]))
    print("\nper-part offsets from their driving bone (0,0 = this part IS the anchor):")
    for s in sk["slots"]:
        if "offset" in s:
            print("   %-12s on %-11s offset %8.2f,%8.2f  scale %.2f"
                  % (s["name"], s["bone"], s["offset"][0], s["offset"][1], s["scale"]))

    ok, rows = check_rest(a.parts)
    bad = [r for r in rows if not r[-1]]
    print("\nREST AGREEMENT (through render_part, the same path pose.py uses):"
          " %d part(s), %d disagree" % (len(rows), len(bad)))
    for n, bone, dx, dy, dw, dh, good in rows:
        print("   %-12s on %-11s  dx %+4d dy %+4d  dw %+4d dh %+4d  %s"
              % (n, bone, dx, dy, dw, dh, "ok" if good else "<-- DISAGREES"))
    if ok:
        print("   ⇒ layout() and render_part() hold ONE opinion about placement. That is all this")
        print("     row of zeros means: it is invariant to every constant in this file (proved by")
        print("     its own controls), so it is a cross-implementation check, not a data check.")

    pok, prows = check_pivots(a.parts)
    print("\nPIVOTS — the axis check_rest is blind to:")
    for n, on_art, alpha, drift, good in prows:
        print("   %-12s on_art=%-5s alpha=%3d  rotation_drift=%5.2f px  %s"
              % (n, on_art, alpha, drift, "ok" if good else "<-- SUSPECT"))
    print("   ⚠ on_art catches a pivot in empty space; it cannot tell a defensible pivot from a")
    print("     better one. Astra called every pivot 'inferred' and that remains the open question.")
    if a.write:
        io.open(a.write, "w", encoding="utf-8").write(json.dumps(sk, indent=2))
        print("\nderived skeleton -> %s   (NEVER written in place)" % a.write)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
