"""pose.py — rotate bones and carry their children. The first thing here that actually moves.

★ WHAT WAS MISSING. assemble.py and snap_rig.py PLACE parts and ignore rotation entirely. Every one
  of J's freeze-gate clips — idle_breathe, blink, look_left, look_right, wave, dance, death_flop —
  needs bones that turn and children that follow. Until now nothing did.

★ TWO THINGS MAKE A ROTATION CORRECT AND BOTH ARE EASY TO GET WRONG:
  1. A part rotates about ITS OWN PIVOT, not its centre. A flipper swung from its middle looks like
     a propeller; swung from the shoulder it looks like an arm.
  2. A child inherits its parent's rotation ABOUT THE PARENT'S PIVOT. Rotating shoulder_L must carry
     flipper_L with it, ORBITING, not just spinning in place. That is the whole reason a hierarchy
     exists and it is the part a naive implementation silently skips.
  Both live in rig_layout.render_part() now, not here, because having two copies is how this file
  broke in the first place.

⛔ THE BUG THIS VERSION FIXES, AND IT WAS MINE TWICE. The first pose.py read
  `pico/rigdata/pico_skeleton.json` — the pre-art reference sketch — while snap_rig.py derived
  placement from the ART. J: "Why did it fly apart?" It never held together: flipper bones sat 374 px
  from centre against a body edge reaching 268, so 106 px of air each side BEFORE any rotation, and
  the head overlapped the body by twelve pixels, thinner than the outline, which is the only reason
  it ever looked joined.
  ★ THE PART WORTH KEEPING: I had ALREADY fixed this. snap_rig exists because of it and writes a
    corrected skeleton to a file. Then I built this tool against the original and never opened the
    file I had produced. A fix parked off the path the next tool walks is not a fix — so the remedy
    is not "remember", it is that geometry now has exactly one home.
    [[a-fix-parked-off-the-hot-path-is-not-a-fix]]

⚠ WHAT THIS IS NOT: a renderer, an animation system, or a rig. It computes one static pose. No
  interpolation, no timeline, no deformation — a flipper is a rigid image that turns. For a chibi
  penguin that may be enough; for anything needing a bending arm it is not, and saying so here is
  cheaper than discovering it at clip four.

    python pose.py --parts out/rig/BASE --pose wave --out /tmp/wave.png
    python pose.py --parts out/rig/BASE --verify-rest
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import rig_layout as RL

#: Poses as bone -> degrees. Positive is clockwise on screen (y grows downward).
#: ⚠ HAND-WRITTEN STARTING VALUES, not derived from anything. They exist to prove the machinery
#:   turns and carries children; whether they look good is an eye's call and probably J's.
#: ⚠⚠ neck/shoulder_*/hip_* are COLLAPSED onto their child by rig_layout (a rigid PNG has one joint),
#:   so driving `shoulder_R` and `flipper_R` both swing from the same point and simply ADD. That is
#:   deliberate; see rig_layout.derive_bones(). It means a pose cannot bend a limb, only swing it.
POSES = {
    "rest": {},
    "wave": {"flipper_R": -55, "shoulder_R": -25, "head": -4},
    "look_left": {"head": -12},
    "look_right": {"head": 12},
    "lean": {"body": 8, "head": -5},
    # ⚠ death_flop drives `root`, NOT `body`. A whole-body topple pivots on the GROUND; `body` sits
    #   at the belly's top, and rotating there flung the feet into two opposite canvas corners and
    #   scored 6 separate pieces on the cohesion check. The bone choice is the pose, not a detail.
    "death_flop": {"root": 78, "head": 12, "flipper_L": -40, "flipper_R": 30},
}


#: ★ ONE HIERARCHY COMPOSER, and it lives in rig_layout beside the one placement function.
#: A reviewer showed the cost of having two: check_rest validated at a flat 0.0 while this file
#: composed the bones, so a skeleton carrying a 30 deg REST rotation passed validation and then
#: rendered 86 px away. The validator and the renderer must not hold separate opinions about what
#: "rest" means — that is the whole defect this rig was rebuilt to remove.
#: ⚠ Kept as a module-level name rather than called through RL so pose.selftest can substitute the
#:   known-bad version and prove the rigid invariant still catches it.
compose = RL.compose


def render(parts_dir, skeleton, angles, guides=False, pad=0, art=None):
    """Draw one pose. Returns (image, placed_rows).

    ⚠ `pad` widens the working canvas by that many pixels on every side and shifts everything into
      it. It exists because CLIPPING AND DETACHMENT ARE INDISTINGUISHABLE to the cohesion check: a
      character rotated partly off-canvas is chopped into several pieces by the border, and reads as
      "flew apart". death_flop at 78° did exactly that and I nearly diagnosed a rig fault from it.
      Rendering with pad measures cohesion on the whole character; the overflow is then reported as
      what it is — a framing problem, not a rig problem.
    """
    from PIL import Image, ImageDraw
    sk = skeleton
    W, H = sk["canvas"]["width"], sk["canvas"]["height"]
    bones = {b["name"]: dict(b) for b in sk["bones"]}
    unknown = [k for k in angles if k not in bones]
    if unknown:
        raise SystemExit("REFUSING: pose drives bone(s) that do not exist: %s. A silently ignored "
                         "angle is a pose that looks wrong for no visible reason."
                         % ", ".join(sorted(unknown)))
    posed = compose(bones, angles)
    parts = RL.load_parts(parts_dir)
    if art:
        # ★ ART SWAP, J's idea 2026-09-27: "cheap expression animation by flipping the visor to
        #   another visor and holding it for a few seconds before flipping back." Swapping the IMAGE
        #   in a slot costs nothing and buys a whole emotional range a rotation cannot reach — this
        #   rig has no facial deformation and never will, so expression has to come from the art.
        # ⚠ The replacement is placed by the SAME pivot fraction and the SAME scale as the original.
        #   That is correct only while the alternates are drawn on a consistent footprint; if one
        #   visor is cropped differently it will sit wrong, and the giveaway is a face that shifts
        #   when the expression changes rather than when the head moves.
        for slot_name, im in art.items():
            if im is not None:
                parts[slot_name] = im
    scale = sk.get("scale")
    if not scale:
        raise SystemExit("REFUSING: skeleton carries no `scale`. It was not written by rig_layout, "
                         "so its coordinates are the pre-art sketch — the exact source of the "
                         "fly-apart. Regenerate with rig_layout.py --parts ... --write ...")

    out = Image.new("RGBA", (W + 2 * pad, H + 2 * pad), (0, 0, 0, 0))
    rows = []
    for s in sorted(sk["slots"], key=lambda s: s.get("z", 0)):
        n = s.get("name")
        if n not in parts or "offset" not in s:
            continue
        bone = s["bone"]
        if bone not in posed:
            print("   UNBOUND: %s -> bone %r not in skeleton" % (n, bone))
            continue
        p = posed[bone]
        im, at = RL.render_part(parts[n], scale * s["scale"], s["pivot"],
                               (p["x"], p["y"]), s["offset"], p["acc"])
        out.alpha_composite(im, (at[0] + pad, at[1] + pad))
        rows.append((s.get("z"), n, bone, round(p["acc"], 1), at, im.size))

    if guides:
        dr = ImageDraw.Draw(out)
        for n, pz in posed.items():
            x, y = int(pz["x"]) + pad, int(pz["y"]) + pad
            col = (0, 255, 90, 230) if n in angles else (255, 0, 255, 140)
            dr.line([(x - 12, y), (x + 12, y)], fill=col, width=3)
            dr.line([(x, y - 12), (x, y + 12)], fill=col, width=3)
    return out, rows


def verify_rest(parts_dir, skeleton):
    """★ THE GUARD THAT WOULD HAVE CAUGHT THE FLY-APART BEFORE I SENT A PICTURE.

    Render the rest pose through the full pose path and compare each part's landing to what
    rig_layout.layout() says. Zero angles everywhere means the pose path reduces to placement, so
    any difference is the two halves of the pipeline disagreeing — which is exactly what the old
    pose.py was doing, and what I could only see as "it flew apart".

    ⚠ It proves AGREEMENT, not beauty. A rig can agree with itself perfectly and still look wrong;
      pivots in particular are invisible here because rest rotation is zero. rig_layout.check_pivots
      covers what it can of that, and the rest is J's eye.
    """
    geom, _scale, _p = RL.layout(parts_dir, skeleton["canvas"]["width"],
                                 skeleton["canvas"]["height"])
    _img, rows = render(parts_dir, skeleton, {})
    bad, out = 0, []
    for z, n, bone, acc, at, size in rows:
        gx, gy, gw, gh = geom[n]
        dx, dy = at[0] - gx, at[1] - gy
        dw, dh = size[0] - gw, size[1] - gh
        good = max(abs(dx), abs(dy), abs(dw), abs(dh)) <= 1
        bad += 0 if good else 1
        out.append((n, bone, dx, dy, dw, dh, good))
    return bad == 0, out


def cohesion(img, thresh=96, min_rel=0.004):
    """★ "DID IT FLY APART?" AS A NUMBER INSTEAD OF A GLANCE.

    Flood-fill the rendered alpha and count components bigger than min_rel of the total opaque area.
    A penguin whose limbs are attached is ONE blob. The old pose.py produced several with 106 px of
    air between them, and the only instrument I had was J asking why it flew apart.

    Returns (n_components, [(area, bbox), ...] largest first).
    ⚠ ONE COMPONENT IS NECESSARY, NOT SUFFICIENT. Parts can overlap into one blob and still be
      arranged wrongly — a beak on a foot is perfectly connected. It rules out the specific,
      embarrassing failure; it does not rule in a good pose.
    ⚠ And it is nearly useless in a pose that SHOULD separate — a wave at full extension, or
      death_flop. Read it as "expected 1, got 3" only when the pose has no reason to detach.
    """
    a = img.getchannel("A").point(lambda v: 255 if v > thresh else 0)
    w, h = a.size
    px = a.load()
    seen = bytearray(w * h)
    comps = []
    total = 0
    for y in range(h):
        base = y * w
        for x in range(w):
            if px[x, y] and not seen[base + x]:
                stack = [(x, y)]
                seen[base + x] = 1
                area = 0
                x0 = x1 = x
                y0 = y1 = y
                while stack:
                    cx, cy = stack.pop()
                    area += 1
                    if cx < x0:
                        x0 = cx
                    if cx > x1:
                        x1 = cx
                    if cy < y0:
                        y0 = cy
                    if cy > y1:
                        y1 = cy
                    for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                        if 0 <= nx < w and 0 <= ny < h and px[nx, ny] and not seen[ny * w + nx]:
                            seen[ny * w + nx] = 1
                            stack.append((nx, ny))
                comps.append((area, (x0, y0, x1, y1)))
                total += area
    comps.sort(reverse=True)
    keep = [c for c in comps if total and c[0] >= total * min_rel]
    return len(keep), keep


def selftest(parts_dir):
    """★ THE RIGID-ROTATION INVARIANT — the test that found the frame-mixing bug in compose().

    Rotating `root` rotates the entire character as one solid object. Therefore, at ANY angle:
      * the piece count must stay 1 (nothing can detach from a rigid body), and
      * the opaque area must stay the same to within resampling noise.
    This is an invariant, not a golden image: it needs no reference render, no eyeballing, and it
    cannot drift. The broken version scored 7 pieces and +32% area at 78°.

    ⚠ WHY IT IS WORTH MORE THAN THE POSES IT CHECKS: every other test I had drove a LEAF bone, whose
      children carry no artwork, so posed == rest for everything visible and a frame-mixing bug is
      invisible. The bug needed a rotation with DESCENDANTS to show itself, and `root` is the one
      bone guaranteed to have them. Pick the case whose failure is arithmetically impossible, then
      any deviation is a defect rather than a judgement call.
    """
    import rig_layout as _RL
    sk, _g, _s = _RL.build(parts_dir)
    W = sk["canvas"]["width"]
    pad = W // 2
    base, _ = render(parts_dir, sk, {}, pad=pad)
    n0, c0 = cohesion(base)
    a0 = sum(c[0] for c in c0)
    fails = []
    if n0 != 1:
        fails.append("rest itself is %d pieces, so the invariant has no valid baseline" % n0)
    for ang in (15, 45, 78, -60, 180):
        big, _ = render(parts_dir, sk, {"root": ang}, pad=pad)
        n, c = cohesion(big)
        a = sum(x[0] for x in c)
        drift = abs(a - a0) / max(1, a0)
        if n != 1:
            fails.append("root %+d° split a rigid body into %d pieces" % (ang, n))
        if drift > 0.01:
            fails.append("root %+d° changed opaque area by %.2f%% — parts stopped overlapping"
                         % (ang, 100 * drift))
    # NEGATIVE CONTROL: reintroduce the old frame-mixing and demand the invariant catch it.
    orig = compose

    def broken(bones, angles):
        posed, seen = {}, set()

        def res(name):
            if name in seen:
                return posed[name]
            seen.add(name)
            b = bones[name]
            px, py, own = float(b["x"]), float(b["y"]), float(b.get("rotation", 0))
            p = b.get("parent")
            if p and p in bones:
                pp = res(p)
                px, py = _RL.rot(px, py, pp["x"], pp["y"], pp["acc"])
                acc = pp["acc"] + own + angles.get(name, 0)
            else:
                acc = own + angles.get(name, 0)
            posed[name] = {"x": px, "y": py, "acc": acc}
            return posed[name]
        for n_ in bones:
            res(n_)
        return posed

    globals()["compose"] = broken
    try:
        big, _ = render(parts_dir, sk, {"root": 78}, pad=pad)
        n, c = cohesion(big)
        caught = (n != 1) or abs(sum(x[0] for x in c) - a0) / max(1, a0) > 0.01
    finally:
        globals()["compose"] = orig
    if not caught:
        fails.append("control failed: the known-bad frame-mixing compose() passed the invariant, so "
                     "this selftest cannot detect the bug it was written for")
    print("pose selftest: rigid invariant at 5 angles + 1 control, %d failure(s)" % len(fails))
    for f in fails:
        print("   FAIL %s" % f)
    return 0 if not fails else 1


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", required=True)
    ap.add_argument("--pose", default="rest")
    ap.add_argument("--out")
    ap.add_argument("--skeleton", help="a rig_layout-derived skeleton; default is to derive one now")
    ap.add_argument("--guides", action="store_true")
    ap.add_argument("--verify-rest", action="store_true")
    ap.add_argument("--all", action="store_true", help="render every pose, --out is a directory")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(a.parts)

    if a.skeleton:
        sk = json.load(io.open(a.skeleton, encoding="utf-8"))
        src = a.skeleton
    else:
        sk, _geom, _scale = RL.build(a.parts)
        src = "derived on the spot by rig_layout.build()"
    print("skeleton: %s" % src)
    provs = {}
    for b in sk["bones"]:
        provs[b.get("provenance", "REFERENCE")] = provs.get(b.get("provenance", "REFERENCE"), 0) + 1
    print("   bones %d — %s" % (len(sk["bones"]),
                                ", ".join("%s %d" % kv for kv in sorted(provs.items()))))
    if provs.get("REFERENCE"):
        print("   ⛔ %d bone(s) still at REFERENCE — those are pre-art sketch coordinates and are "
              "what made the penguin fly apart." % provs["REFERENCE"])

    if a.verify_rest:
        ok, rows = verify_rest(a.parts, sk)
        print("\nREST VERIFY — pose path vs rig_layout.layout(), %d part(s):" % len(rows))
        for n, bone, dx, dy, dw, dh, good in rows:
            print("   %-12s on %-11s dx %+4d dy %+4d  dw %+4d dh %+4d  %s"
                  % (n, bone, dx, dy, dw, dh, "ok" if good else "<-- DISAGREES"))
        print("   %s" % ("agreed — the pose path places exactly where the layout says"
                         if ok else "DISAGREEMENT: the pose path and the layout are not the same rig"))
        if not a.out:
            return 0 if ok else 1

    names = sorted(POSES) if a.all else [a.pose]
    if not a.all and a.pose not in POSES:
        print("REFUSING: unknown pose %r. Known: %s" % (a.pose, ", ".join(sorted(POSES))))
        return 2
    if not a.out:
        print("nothing written (no --out)")
        return 0

    W, H = sk["canvas"]["width"], sk["canvas"]["height"]
    PAD = W // 2
    for name in names:
        angles = POSES[name]
        # measure on a padded canvas so the border cannot manufacture a fake break-up, then crop
        big, rows = render(a.parts, sk, angles, guides=a.guides, pad=PAD)
        img = big.crop((PAD, PAD, PAD + W, PAD + H))
        dest = os.path.join(a.out, "pose_%s.png" % name) if a.all else a.out
        if a.all:
            os.makedirs(a.out, exist_ok=True)
        img.save(dest)
        moved = [r for r in rows if abs(r[3]) > 0.05]
        print("\npose %-11s -> %s" % (name, dest))
        if angles:
            print("   driven: %s" % ", ".join("%s %+d°" % kv for kv in angles.items()))
        for z, n, bone, acc, at, size in rows:
            print("   z=%-4s %-11s on %-11s total %+7.1f°  at %s" % (z, n, bone, acc, at))
        print("   %d of %d parts carry a non-zero rotation" % (len(moved), len(rows)))
        if angles and not moved:
            print("   ⚠ A POSE WAS REQUESTED AND NOTHING TURNED. Wiring fault, not a still pose.")
        n, comps = cohesion(big)
        note = "one blob — nothing detached" if n == 1 else "%d SEPARATE PIECES" % n
        print("   cohesion: %s   %s"
              % (note, ", ".join("%d px %s" % (c[0], c[1]) for c in comps[:4])))
        bb = big.getchannel("A").point(lambda v: 255 if v > 96 else 0).getbbox()
        if bb:
            over = (max(0, PAD - bb[0]), max(0, PAD - bb[1]),
                    max(0, bb[2] - (PAD + W)), max(0, bb[3] - (PAD + H)))
            if any(over):
                print("   ⚠ OVERFLOWS THE FRAME by L%d T%d R%d B%d px — a FRAMING problem, not a rig"
                      % over)
                print("      one. The rig has rotation and no translation, so a pose that leaves the")
                print("      canvas cannot be brought back by any angle. That is J's call: pan the")
                print("      camera, or give the root a position channel.")
        # ⛔ THIS CHECK HAD AN EXEMPTION LIST FOR ABOUT FOUR MINUTES AND death_flop WAS IN IT.
        #    I wrote the exemption in the same edit as the check, before running either — i.e. I
        #    pre-authorised the only pose that would fail, on the assumption that a violent pose is
        #    allowed to come apart. Then it failed with SIX pieces and the diagnosis took one look:
        #    death_flop drove `body`, whose pivot sits at the belly's TOP, so 78° swung the feet
        #    through an enormous arc into the canvas corners. The right bone for a whole-body flop is
        #    `root` — the ground line — and driving that keeps the character rigid.
        #    ★ A check that ships with an excuse for its own failures is not a check. The exemption
        #      would have hidden a real error in my pose values behind a plausible-sounding rule
        #      ("violent poses separate"). No exemptions: if a pose legitimately separates, say so at
        #      the call site with the reason, not in a tuple here. [[the-branch-that-agreed-with-me-got-no-armour]]
        if n > 1:
            print("   ⛔ IT CAME APART. Before blaming the angle, check WHICH BONE the pose drives:")
            print("      a whole-body rotation belongs on `root` (ground), not `body` (belly top) —")
            print("      that exact mistake put the feet in the corners at 6 pieces.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
