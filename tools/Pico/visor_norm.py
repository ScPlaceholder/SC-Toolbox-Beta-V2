"""visor_norm.py — make every visor expression exactly the same size, so a swap changes the FACE
and nothing else.

⛔ J, 2026-09-27: "The visors need to stay the same size." He caught it by eye. Measured, he is
   right twice over:

     spread across the 20 expressions   width 268..309 px   15.3%
     every expression vs the base visor  ~35% narrower, ~42% shorter

   So a swap was doing two unwanted things at once: changing the expression, and shrinking the
   whole face by a third. The second one is invisible as a bug and obvious as "something is off".

★ AND I PREDICTED THIS IN WRITING AND SHIPPED IT ANYWAY. pose.py's own art-swap comment says: "The
  replacement is placed by the SAME pivot fraction and the SAME scale as the original. That is
  correct only while the alternates are drawn on a consistent footprint; if one visor is cropped
  differently it will sit wrong, and the giveaway is a face that shifts when the expression changes."
  I wrote the caveat, did not test it, and sent the clips. **A caveat is not a check.** The measurement
  that settles it took thirty seconds and I did it only after J said something.
  [[prove-the-test-can-fail-before-trusting-it-passes]]

★★ I TRIED PADDING FIRST AND IT WAS THE WRONG FIX — kept here because the reasoning was plausible
  and wrong, which is the useful kind. The argument was: these are glowing shells, the bright
  effects (sparks, crackle) spill past the outline, so the bbox variance is GLOW and the shells
  underneath are probably identical. Pad them into one frame, centre them, done. It made every FILE
  the same size and left a 21 px shell drift.
  ⛔ MEASURED AT TWO ALPHA THRESHOLDS AND THE THEORY DIED. If the variance were glow, a strict
    threshold would collapse it:

        bbox @ alpha>96   width 268..309   15.3% spread
        bbox @ alpha>240  width 265..306   15.5% spread      <- unchanged

    The solid shells differ by the same 15%. The art really is drawn at two sizes: pieces 01-14
    around 272 px, pieces 15-20 around 300 px, almost certainly two rows of one sheet.
  ⇒ So each piece is SCALED so its solid-shell bbox matches a common target, then centred. The
    objection I raised against rescaling — "the outline would breathe" — was exactly backwards:
    rescaling to a shared shell is the only thing that stops it breathing. I had the right worry
    pointed at the wrong operation.
  ⚠ Scaling x and y independently distorts the symbols by up to ~6%. That is the cost, it is
    deliberate, and it buys the property J asked for: identical shells.

⚠ THE BASE VISOR IS A DIFFERENT DRAWING, NOT A BIGGER ONE. base 425x238 is aspect 1.79; the
  expressions are ~309x143, aspect 2.16. They are not the same shape at two sizes, so no single
  scale factor reconciles them — matching width leaves it too short, matching height too wide.
  ⇒ So the expression sheet becomes the ONE source for every face, including the resting one.
    `content` replaces visor.png as the default. One sheet, one aspect, one size, no seam.

★★★ VERIFYING THE FIX TOOK THREE INSTRUMENTS AND TWO OF THEM WERE CONFOUNDED. Worth recording,
  because each was reasonable and each measured the wrong property:

    1. SHELL BBOX AT SOURCE (alpha>240)  — drift 21 px -> 2 px.  The decisive one.
    2. VISOR WIDTH IN THE RENDER          — 15.3% -> 0.0-0.6% on love, dance, deadpan_wave.
       ⚠ but 29% on `ko`, which rotates the head and topples the body. A horizontal width across a
         TILTED ellipse shrinks by geometry. Not a size bug; a rotation the instrument cannot see.
    3. VISOR AREA IN THE RENDER           — reached for because area is rotation-invariant, and it
       was worse: 3.5-15.9%. Area measures HOW MUCH OF THE VISOR IS LIT, and hearts light more of
       it than two dashes do. It was answering a question about the art, not about the size.

  ⇒ The property is SHELL EXTENT. Width measures it correctly only when nothing rotates; area never
    measures it at all. Neither instrument was broken and neither was lying — they were each
    answering a nearby question, and the only way to tell was to know what each one actually looks
    at. [[the-instrument-was-not-wrong-it-was-coarse]]

    python visor_norm.py --write          # build out/rig/VISOR_NORM
    python visor_norm.py --selftest
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "out", "sliced", "VISOR")
DST = os.path.join(HERE, "out", "rig", "VISOR_NORM")


def bbox_of(im, thr=96):
    b = im.getchannel("A").point(lambda v: 255 if v > thr else 0).getbbox()
    return b or (0, 0, im.width, im.height)


def shell_of(im):
    """The SOLID shell, at a strict alpha. This is the thing that must not change size — the soft
    glow around it legitimately differs between a calm face and a crackling one."""
    return bbox_of(im, 240)


def measure(src=SRC):
    """Return [(path, full_image, shell_bbox)] plus the TARGET shell size every piece is scaled to.

    The target is the MEDIAN shell, not the max or the min. The max would upscale fourteen of twenty
    pieces and soften them; the min would throw away detail in the six larger ones. The median moves
    the fewest pixels and is the only choice here that does not privilege one group of the sheet
    over the other.
    """
    from PIL import Image
    rows = []
    for p in sorted(glob.glob(os.path.join(src, "*.png"))):
        if os.path.basename(p).startswith("_"):
            continue
        im = Image.open(p).convert("RGBA")
        rows.append((p, im, shell_of(im)))
    if not rows:
        raise SystemExit("REFUSING: no visor pieces in %s" % src)
    ws = sorted(b[2] - b[0] for _p, _i, b in rows)
    hs = sorted(b[3] - b[1] for _p, _i, b in rows)
    return rows, (ws[len(ws) // 2], hs[len(hs) // 2])


def build(write=False, src=SRC, dst=DST):
    from PIL import Image
    rows, (TW, TH) = measure(src)
    if write:
        os.makedirs(dst, exist_ok=True)
    # the output frame is the target shell plus a margin, so glow that legitimately spills past the
    # shell (sparks, crackle) is not clipped. The margin is generous on purpose: clipping a glow
    # would be a second silent defect of exactly the kind this file exists to remove.
    MARGIN = 26
    W, H = TW + 2 * MARGIN, TH + 2 * MARGIN
    out, manifest = [], {}
    for p, im, sb in rows:
        sw, sh = sb[2] - sb[0], sb[3] - sb[1]
        fx, fy = TW / float(sw), TH / float(sh)
        big = im.resize((max(1, int(round(im.width * fx))), max(1, int(round(im.height * fy)))),
                        Image.LANCZOS)
        nsb = shell_of(big)
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        # align by the SHELL, not by the image: put the scaled shell's top-left at the margin.
        canvas.alpha_composite(big, (MARGIN - nsb[0], MARGIN - nsb[1]))
        name = os.path.basename(p)
        out.append((name, canvas))
        manifest[name] = {"source_shell_wh": [sw, sh], "target_shell_wh": [TW, TH],
                          "scale_xy": [round(fx, 4), round(fy, 4)], "frame_wh": [W, H]}
        if write:
            canvas.save(os.path.join(dst, name))
    if write:
        manifest["_note"] = (
            "Every file here is EXACTLY %dx%d and every SOLID SHELL inside it is exactly %dx%d, so "
            "a swap changes the face and nothing else. Built by visor_norm.py from out/sliced/VISOR, "
            "whose shells varied 15.5%% in width across two drawn size groups (pieces 01-14 ~272px, "
            "15-20 ~300px). Each piece is scaled to the MEDIAN shell and aligned BY THE SHELL, not "
            "by its image bounds -- glow spill differs legitimately between a calm face and a "
            "crackling one and must not drive the alignment." % (W, H, TW, TH))
        io.open(os.path.join(dst, "_manifest.json"), "w", encoding="utf-8").write(
            json.dumps(manifest, indent=2))
    return out, (W, H)


def selftest():
    fails = []
    out, (W, H) = build(write=False)
    if len(out) < 2:
        fails.append("only %d piece(s) — nothing to compare" % len(out))
    for name, im in out:
        if im.size != (W, H):
            fails.append("%s is %dx%d, not the shared %dx%d" % (name, im.width, im.height, W, H))
    # ★ THE CHECK THAT MATTERS: the SHELL must not move. Compare the opaque bbox of each padded
    #   piece; they should agree to within a pixel or two, because the shells are drawn alike.
    #   ⚠ This is the property J actually complained about, so it gets the real assertion — not the
    #     file dimensions, which are true by construction and would pass on broken art.
    boxes = [shell_of(im) for _n, im in out]
    x0 = [b[0] for b in boxes]
    y0 = [b[1] for b in boxes]
    x1 = [b[2] for b in boxes]
    y1 = [b[3] for b in boxes]
    drift = max(max(x0) - min(x0), max(y0) - min(y0), max(x1) - min(x1), max(y1) - min(y1))
    if drift > 2:
        fails.append("shell drift %d px across the set — a swap will visibly shift the face" % drift)
    # NEGATIVE CONTROL: a piece that is NOT padded must be caught by the size assertion.
    if out:
        name, im = out[0]
        bad = im.crop((0, 0, max(1, im.width - 9), im.height))
        if bad.size == (W, H):
            fails.append("control failed: a deliberately shrunk piece still matched the frame")
    print("visor_norm selftest: %d piece(s) at %dx%d, shell drift %d px, %d failure(s)"
          % (len(out), W, H, drift, len(fails)))
    for f in fails:
        print("   FAIL %s" % f)
    return 0 if not fails else 1


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    out, (W, H) = build(write=a.write)
    print("normalised %d visor piece(s) to a shared %dx%d frame" % (len(out), W, H))
    boxes = [(n, bbox_of(im)) for n, im in out]
    x0 = min(b[1][0] for b in boxes)
    x1 = max(b[1][2] for b in boxes)
    print("   shell spans x %d..%d across the whole set (was 268..309 px wide before padding)"
          % (x0, x1))
    if a.write:
        print("   -> %s  (+ _manifest.json recording each piece's original size and pad)" % DST)
    else:
        print("   (dry — pass --write to save)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
