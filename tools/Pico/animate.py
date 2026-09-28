"""animate.py — turn the static poses into clips. J, 2026-09-27: "Do they actually animate or are
they stills?" They were stills. This is the answer.

★ WHAT IT ADDS AND WHAT IT DELIBERATELY DOES NOT. It interpolates BONE ANGLES over time and renders
  a frame per step. That is all animation is for a cut-out rig: the hard parts — a pivot per part, a
  child orbiting its parent, a placement that agrees with the artwork — were built and debugged in
  rig_layout.py and pose.py, and this file is a loop over them. Saying so keeps the credit in the
  right place: if a clip looks wrong, the bug is almost certainly upstream of here.

★ EASING MATTERS MORE THAN FRAME COUNT. Linear interpolation reads as mechanical no matter how many
  frames you give it, because real motion accelerates and settles. ease_in_out is the default;
  `overshoot` exists for a wave or a nod, where a limb passing its target and coming back is the
  difference between a puppet and a character.

⚠ WHAT IT CANNOT DO, and these are properties of the ART, not of this code:
    * no translation. A bone has an angle and no position channel, so a walk cycle, a hop, or
      death_flop staying in frame are all out of reach. Rotation alone cannot move a character.
    * no deformation. Every part is a rigid PNG. A flipper swings, it never bends.
    * no turning. look_left is a TILT. A flat image cannot rotate in depth; a real turn needs the
      side-view art from the manufacturer sheets.
  ⇒ Each of those is a decision for J, not a thing to quietly approximate.

    python animate.py --parts out/rig/BASE --clip wave --out out/clips
    python animate.py --parts out/rig/BASE --all --out out/clips --fps 24
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys

import rig_layout as RL
import pose as P


def ease_in_out(t):
    """Smoothstep. Zero velocity at both ends, which is what makes a move look intentional."""
    return t * t * (3 - 2 * t)


def ease_out(t):
    return 1 - (1 - t) ** 3


def linear(t):
    return t


def overshoot(t, k=1.7):
    """Passes the target and settles back. The difference between a puppet and a character."""
    t -= 1.0
    return t * t * ((k + 1) * t + k) + 1.0


EASES = {"ease_in_out": ease_in_out, "ease_out": ease_out, "linear": linear,
         "overshoot": overshoot}


def lerp_pose(a, b, t):
    """Blend two bone->degrees dicts. A bone absent from either side is 0 there, not missing —
    otherwise a limb named in only one keyframe would pop instead of moving."""
    out = {}
    for k in set(a) | set(b):
        out[k] = a.get(k, 0.0) * (1 - t) + b.get(k, 0.0) * t
    return out


#: ★ THE EXPRESSION SET, READ BY EYE off the designer's visor sheet (20 pieces, out/sliced/VISOR).
#: J, 2026-09-27: "You can achieve cheap expression animation by flipping the visor to another
#: visor and holding it for a few seconds before flipping back." He is right and it is the only
#: route available — the rig has no facial deformation and a rigid PNG cannot smile.
#: ⚠ THESE NAMES ARE MINE, ASSIGNED BY LOOKING, and nobody has checked them. `annoyed` for the >‹
#:   pair is a reading, not a label the artist wrote. If a clip reads emotionally wrong, suspect
#:   this table before suspecting the timing.
VISOR = {
    "content": "visor_01", "content2": "visor_02", "squint": "visor_03",
    "flat": "visor_04", "flat2": "visor_05",
    "glitch_L": "visor_06", "glitch_L2": "visor_07", "static": "visor_08",
    "wide": "visor_09", "focus": "visor_10", "woozy": "visor_11", "angry": "visor_12",
    "downcast": "visor_13", "love": "visor_14", "starstruck": "visor_15",
    "locked_on": "visor_16", "ko": "visor_17", "alert": "visor_18",
    "sparks": "visor_19", "crackle": "visor_20",
}

#: A clip is a list of (angles, hold_frames, transition_frames, ease) with an optional 5th element:
#: a dict of slot -> expression name, applied AT that keyframe and held until changed.
#: ⚠ SWAPS DO NOT INTERPOLATE. An image either is or is not on screen; there is no half-heart. So a
#:   swap lands on the frame the keyframe starts, and the transition frames before it still carry
#:   the PREVIOUS art. That is deliberate — blending two expressions would look like a ghost.
#: ⚠ HAND-AUTHORED TIMINGS. They are starting values that make the machinery visible, not J's
#:   direction. Every number here is a guess with an opinion, and the opinions are cheap to change.
CLIPS = {
    # ⛔ EXPRESSION BUDGET, J 2026-09-27: "besides the death one it shouldn't alternate to so many
    #    different emotions." He is right and the reason is legible once said: a face that changes
    #    every half-second reads as a slideshow, not a mood. An expression needs to be HELD long
    #    enough to be a state. So: ONE expression per clip, with a second only where the clip is
    #    explicitly about a change of state — and `ko` keeps its cycle, because a malfunction
    #    flickering IS the point.
    "idle_breathe": [
        ({}, 2, 0, "ease_in_out", {"visor": "content"}),
        ({"body": 1.1, "head": -1.4, "flipper_L": 2.0, "flipper_R": -2.0}, 2, 14, "ease_in_out"),
        ({}, 2, 14, "ease_in_out"),
    ],
    "wave": [
        ({}, 3, 0, "ease_in_out", {"visor": "content"}),
        ({"shoulder_R": -25, "flipper_R": -55, "head": -4}, 1, 9, "overshoot"),
        ({"shoulder_R": -25, "flipper_R": -22, "head": -4}, 1, 7, "ease_in_out"),
        ({"shoulder_R": -25, "flipper_R": -60, "head": -4}, 1, 7, "ease_in_out"),
        ({"shoulder_R": -25, "flipper_R": -25, "head": -4}, 1, 7, "ease_in_out"),
        ({}, 4, 11, "ease_in_out"),
    ],
    # ★ RED'S deadpan_wave, built. Wave normally, then flip to flat at the APEX and hold, so the
    #   face contradicts the body. Red's rule: mismatch against the MOTION is comedy, mismatch
    #   against the SITUATION is a bug — and the hold MUST end in a visible snap-out, or it is
    #   indistinguishable from a stuck frame.
    "deadpan_wave": [
        ({}, 3, 0, "ease_in_out", {"visor": "content"}),
        ({"shoulder_R": -25, "flipper_R": -58, "head": -4}, 10, 9, "overshoot", {"visor": "flat"}),
        ({"shoulder_R": -25, "flipper_R": -30, "head": -4}, 6, 8, "ease_in_out"),
        ({}, 5, 10, "ease_in_out", {"visor": "content"}),
    ],
    "look_around": [
        ({}, 4, 0, "ease_in_out", {"visor": "focus"}),
        ({"head": -12}, 6, 11, "ease_in_out"),
        ({"head": 12}, 6, 16, "ease_in_out"),
        ({}, 4, 11, "ease_in_out"),
    ],
    "nod": [
        ({}, 2, 0, "ease_in_out", {"visor": "content"}),
        ({"neck": 7, "head": 3}, 1, 5, "ease_out"),
        ({"neck": -3, "head": -2}, 1, 5, "ease_in_out"),
        ({"neck": 7, "head": 3}, 1, 5, "ease_in_out"),
        ({}, 3, 6, "ease_in_out"),
    ],
    # ★★ THE DANCE, REBUILT TWICE OVER. J: "it still looks like it's being hung by his head ... his
    #    feet need to be more dancy."
    #    (1) The hanging was the RIG, not the timing: `body` sat 5% down the belly, level with the
    #        neck, so swaying it swung the hips and feet from under the chin. rig_layout now puts
    #        `body` on the hip line. Same art, same rest pose, different pivot.
    #    (2) The feet now carry the beat rather than trailing it — bigger ankle angles, opposed,
    #        and they lead the torso sway by landing on the keyframe rather than easing into it.
    #    ⚠ Still a waddle, not a step. One rigid foot image means no knee and no lift; the feet
    #      pivot at the ankle and that is the whole vocabulary available.
    "dance": [
        ({}, 1, 0, "ease_in_out", {"visor": "starstruck"}),
        ({"body": 8, "head": -7, "flipper_L": -26, "flipper_R": 16,
          "foot_L": -19, "foot_R": 9}, 2, 7, "ease_out"),
        ({"body": -8, "head": 7, "flipper_L": -16, "flipper_R": 26,
          "foot_L": -9, "foot_R": 19}, 2, 9, "ease_out"),
        ({"body": 8, "head": -7, "flipper_L": -26, "flipper_R": 16,
          "foot_L": -19, "foot_R": 9}, 2, 9, "ease_out"),
        ({"body": -8, "head": 7, "flipper_L": -16, "flipper_R": 26,
          "foot_L": -9, "foot_R": 19}, 2, 9, "ease_out"),
        ({}, 3, 8, "ease_in_out"),
    ],
    # ONE expression, held. The body does the work; the face is the state it is in.
    "love": [
        ({}, 3, 0, "ease_in_out", {"visor": "content"}),
        ({"head": 4, "body": -2, "flipper_L": -8, "flipper_R": 8}, 30, 9, "overshoot",
         {"visor": "love"}),
        ({}, 5, 9, "ease_in_out", {"visor": "content"}),
    ],
    "startled": [
        ({}, 4, 0, "ease_in_out", {"visor": "content"}),
        ({"body": -5, "head": -7, "flipper_L": -30, "flipper_R": 30}, 22, 3, "overshoot",
         {"visor": "alert"}),
        ({}, 4, 10, "ease_in_out", {"visor": "content"}),
    ],
    # ★ THE EXEMPTION, AND IT IS NAMED RATHER THAN ASSUMED. This is the one clip whose SUBJECT is
    #   instability, so a flickering face is the content, not churn. Everything else holds one.
    "ko": [
        ({}, 3, 0, "ease_in_out", {"visor": "content"}),
        ({"head": -6, "flipper_L": -14, "flipper_R": 14}, 2, 4, "overshoot", {"visor": "alert"}),
        ({"root": 4, "head": 8}, 2, 6, "ease_in_out", {"visor": "woozy"}),
        ({"root": -4, "head": -8}, 2, 8, "ease_in_out", {"visor": "static"}),
        ({"root": 3, "head": 6}, 2, 8, "ease_in_out", {"visor": "ko"}),
        ({"root": 72, "head": 14, "flipper_L": -34, "flipper_R": 26}, 18, 12, "ease_in_out"),
    ],
}


_ART_CACHE = {}


def _art_for(parts_dir, swaps):
    """Load the alternate art named by a swap dict, alpha-cropped like every other part.

    ⛔ REFUSES on an unknown expression rather than falling back to the default face. A silently
      ignored swap is a clip that plays perfectly and conveys the wrong emotion, and nothing about
      the output would tell me — the failure mode is a penguin that looks fine and means something
      else. [[a-silent-decision-passes-a-correctness-review]]
    """
    if not swaps:
        return None
    from PIL import Image
    out = {}
    for slot, name in swaps.items():
        stem = VISOR.get(name, name)
        key = (parts_dir, slot, stem)
        if key not in _ART_CACHE:
            here = os.path.dirname(os.path.abspath(__file__))
            # ★ NORMALISED ART ONLY. out/sliced/VISOR holds the raw cut pieces, whose shells vary
            #   15% across two drawn size groups; out/rig/VISOR_NORM holds the same twenty with
            #   every shell scaled to one size. J caught the raw version by eye: "the visors need
            #   to stay the same size."
            path = os.path.join(here, "out", "rig", "VISOR_NORM", stem + ".png")
            if not os.path.exists(path):
                raise SystemExit("REFUSING: expression %r for slot %r is not on disk (%s). Run "
                                 "`python visor_norm.py --write` first. Known: %s"
                                 % (name, slot, path, ", ".join(sorted(VISOR))))
            im = Image.open(path).convert("RGBA")
            # ⛔ MATCH THE SHELL TO THE SLOT'S DEFAULT ART, NOT THE BOUNDING BOX. The default
            #    visor.png is a different drawing at a different resolution — 425x238 against the
            #    sheet's ~272x134 — so dropping a normalised piece straight in rendered every
            #    expression about a third smaller than the resting face. Two separate size bugs
            #    were stacked here and only one of them was visible.
            #    Measuring SHELLS (alpha>240) rather than bboxes means a crackling face, whose glow
            #    legitimately spills further, is not shrunk to compensate for its own glow.
            base_p = os.path.join(parts_dir, slot + ".png")
            if os.path.exists(base_p):
                bimg = Image.open(base_p).convert("RGBA")
                bs = bimg.getchannel("A").point(lambda v: 255 if v > 240 else 0).getbbox()
                ns = im.getchannel("A").point(lambda v: 255 if v > 240 else 0).getbbox()
                if bs and ns and (ns[2] - ns[0]) > 0 and (ns[3] - ns[1]) > 0:
                    fx = (bs[2] - bs[0]) / float(ns[2] - ns[0])
                    fy = (bs[3] - bs[1]) / float(ns[3] - ns[1])
                    im = im.resize((max(1, int(round(im.width * fx))),
                                    max(1, int(round(im.height * fy)))), Image.LANCZOS)
            bb = im.getchannel("A").point(lambda v: 255 if v > 96 else 0).getbbox()
            _ART_CACHE[key] = im.crop(bb) if bb else im
        out[slot] = _ART_CACHE[key]
    return out


def build_frames(clip):
    """Expand a clip into [(angles, swaps)] — one entry per frame.

    `swaps` is the CURRENT art selection, carried forward until a keyframe changes it, so a clip
    never has a frame with no expression on it.
    """
    frames, prev, cur = [], None, {}
    for step in clip:
        target, hold, trans, ease = step[0], step[1], step[2], step[3]
        swaps = step[4] if len(step) > 4 else None
        f = EASES[ease]
        if prev is not None and trans:
            for i in range(1, trans + 1):
                frames.append((lerp_pose(prev, target, f(i / float(trans))), dict(cur)))
        elif prev is None:
            if swaps:
                cur.update(swaps)
            frames.append((dict(target), dict(cur)))
        if swaps:
            cur.update(swaps)
        for _ in range(hold):
            frames.append((dict(target), dict(cur)))
        prev = target
    return frames


def render_clip(parts_dir, sk, frames, box=None, size=360):
    """Render every frame into the SAME crop box, so the character does not jitter between frames.

    ⛔ THE SHARED BOX IS THE WHOLE POINT AND IT IS EASY TO GET WRONG. Cropping each frame to its own
      content makes every frame a different size and re-centres the character on its own bounding
      box — the result is a penguin that vibrates while its limbs move, and the vibration reads as a
      rig fault. The box is computed ONCE across all frames, then applied to all of them.
      Same defect as the contact sheet where I scaled each panel to its own height and J asked why
      one was giant. A per-item normalisation inside a comparison is a lie about the comparison.
    """
    from PIL import Image
    W, H = sk["canvas"]["width"], sk["canvas"]["height"]
    pad = W // 2
    raw, coh = [], []
    for ang, swaps in frames:
        big, _rows = P.render(parts_dir, sk, ang, pad=pad, art=_art_for(parts_dir, swaps))
        raw.append(big)
        # ⛔ COHESION IS MEASURED HERE, ON THE FRAME WE ALREADY HAVE. It used to run in a second
        #    loop in main() that re-rendered every frame from scratch — an exact 2x waste of the
        #    most expensive operation in the file, on ~40 frames per clip at 3072x3072. Nothing
        #    was wrong with the numbers; I was just paying for them twice because the check was
        #    written after the renderer and never looked to see what was already in hand.
        coh.append(P.cohesion(big)[0])
    if box is None:
        x0 = y0 = 10 ** 9
        x1 = y1 = -1
        for im in raw:
            bb = im.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
            if not bb:
                continue
            x0, y0 = min(x0, bb[0]), min(y0, bb[1])
            x1, y1 = max(x1, bb[2]), max(y1, bb[3])
        m = 24
        box = (x0 - m, y0 - m, x1 + m, y1 + m)
    bw, bh = box[2] - box[0], box[3] - box[1]
    s = size / float(bh)
    out = []
    for im in raw:
        c = im.crop(box).resize((max(1, int(bw * s)), size), Image.LANCZOS)
        out.append(c)
    return out, box, coh


def save_gif(frames, path, fps, bg=(24, 22, 26)):
    """Flatten onto a solid background — GIF has 1-bit alpha and a soft edge turns into fringe.

    Returns (bytes, frames_in_file, seconds_in_file) — read back FROM THE FILE, never assumed.

    ⚠ THE WRITTEN FILE HAS FEWER FRAMES THAN I HAND IT, AND THAT IS CORRECT. PIL merges identical
      consecutive frames and extends the previous frame's duration to compensate, so a 5-frame hold
      becomes one frame at 250 ms. I fed 53 and the file holds 42, both lasting 2.65 s.
    ★ I nearly reported "53 frames" for a file containing 42 — describing my INPUT as though it were
      the artefact. It happened to be harmless here only because I checked the durations and found
      the timing preserved; had PIL dropped the frames without extending them, the same sentence
      would have described a clip a fifth shorter than the one J actually received. Read the
      artefact back and quote that. [[verify-through-the-consumer-not-your-own-writer]]
    """
    from PIL import Image, ImageSequence
    flat = []
    for f in frames:
        b = Image.new("RGB", f.size, bg)
        b.paste(f, (0, 0), f)
        flat.append(b.convert("P", palette=Image.ADAPTIVE, colors=255))
    flat[0].save(path, save_all=True, append_images=flat[1:],
                 duration=int(1000.0 / fps), loop=0, optimize=True)
    got = Image.open(path)
    durs = [fr.info.get("duration", 0) for fr in ImageSequence.Iterator(got)]
    want = len(frames) * (1000.0 / fps)
    if abs(sum(durs) - want) > max(60, 0.02 * want):
        print("   ⛔ TIMING LOST IN THE WRITE: asked for %.2fs, the file plays %.2fs. Duplicate "
              "frames were dropped WITHOUT their duration being carried over."
              % (want / 1000.0, sum(durs) / 1000.0))
    return os.path.getsize(path), len(durs), sum(durs) / 1000.0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", required=True)
    ap.add_argument("--clip", default="wave")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--size", type=int, default=360)
    ap.add_argument("--skeleton")
    a = ap.parse_args(argv)

    if a.skeleton:
        sk = json.load(io.open(a.skeleton, encoding="utf-8"))
    else:
        sk, _g, _s = RL.build(a.parts)

    names = sorted(CLIPS) if a.all else [a.clip]
    bad = [n for n in names if n not in CLIPS]
    if bad:
        print("REFUSING: unknown clip(s) %s. Known: %s" % (", ".join(bad), ", ".join(sorted(CLIPS))))
        return 2
    os.makedirs(a.out, exist_ok=True)

    for name in names:
        frames = build_frames(CLIPS[name])
        imgs, box, coh = render_clip(a.parts, sk, frames, size=a.size)
        dest = os.path.join(a.out, "%s.gif" % name)
        nbytes, nfile, secs = save_gif(imgs, dest, a.fps)
        # cohesion on the whole clip, not just one frame: a limb that detaches for three frames
        # mid-swing is exactly the failure a single still cannot show.
        worst = max(coh) if coh else 1
        worst_i = coh.index(worst) if coh else -1
        print("%-13s composed %d frames -> %d in the file, plays %.2fs   %6.0f KB   %s"
              % (name, len(frames), nfile, secs, nbytes / 1024.0, dest))
        if worst > 1:
            print("   ⛔ frame %d breaks into %d pieces — a still would never have shown this"
                  % (worst_i, worst))
        else:
            print("   cohesion: every frame is one connected penguin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
