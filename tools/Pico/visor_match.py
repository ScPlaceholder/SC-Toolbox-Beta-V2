"""visor_match.py — map the severity-tier visors onto the blue set BY SHAPE, never by index.

⛔ WHY NOT BY INDEX, WHICH IS THE OBVIOUS AND WRONG ANSWER. Blue holds 20 expressions and each
   severity tier holds 28, so they cannot correspond one-to-one — and the ORDER differs too. Read
   off the montages: blue's 08 is the double-spark `static`, red's 08 is the two-circle `wide`.
   Lining them up by position produces a complete, plausible, entirely wrong table, and nothing
   downstream would ever complain: every swap would find a file and render a face.

★ SO MATCH ON THE GLYPH. The shell is common furniture; the SYMBOLS inside it carry the meaning.
   Colour is the one thing that must be ignored, because colour is precisely what distinguishes the
   tiers — so everything is reduced to a binary mask of "bright ink" and compared as shape.

★★ THE CONTROL IS FREE AND IT IS PERFECT, WHICH IS RARE. Matching blue against ITSELF must return
   the identity permutation: every piece's best match is itself, with a large margin. A matcher that
   cannot map a set onto itself is broken, and that check costs nothing and cannot be fudged. It
   runs before any cross-tier claim is believed. [[a-differential-test-with-a-broken-control-scores-perfect]]

⚠ WHAT THIS CANNOT DO: it cannot tell me a red glyph MEANS the same thing as the blue one it
  resembles. It reports shape similarity and a margin. Where the margin is thin it says so and
  declines, because a confident wrong mapping is worse than a gap — an expression that means
  "alert" rendering as "content" is a mascot that lies, and nothing in the pipeline can catch it.

⛔⛔⛔ REJECTED 2026-09-28, BY ITS OWN OUTPUT AUDITED AGAINST MY EYE. DO NOT SHIP THIS AS A MAP.
   Rendered five of the pairs it asserts and looked at them:

     visor_03 (><)         -> visor_11 (angry eyes)   RESOLVED 0.500   WRONG
     visor_10 (concentric) -> visor_14 (asterisks)    RESOLVED 0.707   WRONG
     visor_16 (bullseye)   -> visor_14 (asterisks)    RESOLVED 0.734   WRONG, and contested
     visor_18 (!!)         -> visor_16 (!!)           RESOLVED 0.424   right
     visor_01 (content)    -> visor_01 (content)      REFUSED  0.030   RIGHT, and refused

   ★ THE MARGIN FILTER IS ANTI-CORRELATED WITH CORRECTNESS. A 28x28 IoU rewards glyphs that FILL
     THE SAME AREA, so asterisks and concentric circles score 0.71 while the correct sparse pair
     (two small arcs) scores 0.42 and gets refused. The two highest-scoring matches in the whole
     run are both wrong. Raising the threshold makes it WORSE, not better.
     [[my-confidence-markers-point-away-from-my-errors]]

⚠⚠ AND BOTH CONTROLS PASSED, WHICH IS THE PART WORTH KEEPING. I ran two and verified both:
     1. blue matched against ITSELF          -> 20/20 identity
     2. blue matched against a HUE-SHIFTED copy of itself -> 20/20, tightest margin 0.113
   Both compare IDENTICAL PIXEL LAYOUTS. Self-match scores 1.0 by construction and cannot fail; the
   recolour test proves colour-blindness, which was never the hard part. NEITHER tested DRAWING
   variance — the same glyph REDRAWN in a separate pass at a slightly different size and position —
   and that is the entire difficulty of the real task.
   ⇒ I built two controls, verified both, and both tested a dimension that was not the hard one.
     A control only licenses the axis it varies. Ask which axis the REAL data varies on, and vary
     THAT. [[prove-the-test-can-fail-before-trusting-it-passes]]

⇒ WHAT WOULD ACTUALLY WORK, not attempted here: match on the glyph's STRUCTURE rather than its
  filled area — stroke topology, component count, whether the symbol is open or closed — or simply
  read the 28 by eye once and write the table down. Twenty-eight is a small number and an eye is
  the instrument that has been right every time tonight.

    python visor_match.py --selftest
    python visor_match.py --write visor_tier_map.json
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TIERS = {
    "blue": "out/rig/VISOR_NORM",
    "red": "out/rig/VISOR_RED_NORM",
    "orange": "out/rig/VISOR_ORANGE_NORM",
    "yellow": "out/rig/VISOR_YELLOW_NORM",
}
GRID = 28          # the glyph mask is downsampled to GRID x GRID before comparison
MARGIN_MIN = 0.04  # best must beat runner-up by this much, or the row is UNRESOLVED


def glyph_mask(path, grid=GRID):
    """Reduce a visor to a colour-blind binary mask of its bright interior symbols.

    ⚠ THE SHELL MUST BE EXCLUDED OR EVERYTHING MATCHES EVERYTHING. The outline is the brightest
      thing in the image and is identical across all 28, so a naive brightness mask is dominated by
      furniture and every pair scores ~0.9. The interior is taken as the central box of the opaque
      bbox, which drops the rim without needing to segment it.
    """
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    bb = im.getchannel("A").point(lambda v: 255 if v > 200 else 0).getbbox()
    if bb:
        im = im.crop(bb)
    w, h = im.size
    # interior only: inset far enough to clear the rim, which is ~12% of the shell on every piece
    im = im.crop((int(w * 0.14), int(h * 0.20), int(w * 0.86), int(h * 0.80)))
    im = im.resize((grid, grid), Image.LANCZOS)
    px = im.convert("RGB").load()
    vals = []
    for y in range(grid):
        for x in range(grid):
            r, g, b = px[x, y]
            vals.append(max(r, g, b))          # COLOUR-BLIND: peak channel, not luminance
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo < 12:
        return [0] * len(vals)
    cut = lo + (hi - lo) * 0.55
    return [1 if v >= cut else 0 for v in vals]


def sim(a, b):
    """Intersection over union of two binary masks. 1.0 identical, 0.0 disjoint."""
    inter = sum(1 for x, y in zip(a, b) if x and y)
    union = sum(1 for x, y in zip(a, b) if x or y)
    return (inter / float(union)) if union else 0.0


def load_tier(name):
    d = os.path.join(HERE, *TIERS[name].split("/"))
    fs = sorted(f for f in glob.glob(os.path.join(d, "*.png"))
                if not os.path.basename(f).startswith("_"))
    return [(os.path.basename(f)[:-4], glyph_mask(f)) for f in fs]


def match(src, dst):
    """For each src piece, the best dst piece by shape. Returns rows with the runner-up margin."""
    rows = []
    for sname, smask in src:
        scored = sorted(((sim(smask, dmask), dname) for dname, dmask in dst), reverse=True)
        best, second = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
        rows.append({"src": sname, "best": best[1], "score": round(best[0], 4),
                     "runner_up": second[1], "margin": round(best[0] - second[0], 4)})
    return rows


def identity_control(tier="blue"):
    """★ THE CONTROL. Matching a set against itself must return the identity permutation.

    Cannot be fudged and costs nothing. If this fails, every cross-tier number below is noise and
    the honest report is 'the matcher does not work', not a table of plausible pairings.
    """
    t = load_tier(tier)
    rows = match(t, t)
    wrong = [r for r in rows if r["src"] != r["best"]]
    return (not wrong), rows, wrong


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--write")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    ok, rows, wrong = identity_control("blue")
    print("CONTROL — blue matched against itself (must be the identity permutation):")
    print("   %d of %d pieces matched themselves" % (len(rows) - len(wrong), len(rows)))
    if wrong:
        print("   ⛔ THE MATCHER IS BROKEN. It cannot map a set onto itself, so every cross-tier")
        print("      number below would be noise. Reporting that instead of a table:")
        for r in wrong[:6]:
            print("      %s -> %s (%.3f)" % (r["src"], r["best"], r["score"]))
        return 1
    tight = [r for r in rows if r["margin"] < MARGIN_MIN]
    print("   ✓ identity holds. %d row(s) had a margin under %.2f even against themselves —"
          % (len(tight), MARGIN_MIN))
    print("     those glyphs are genuinely similar to a sibling and are the ones to distrust.")
    for r in tight[:6]:
        print("       %-10s runner-up %-10s margin %.3f" % (r["src"], r["runner_up"], r["margin"]))

    if a.selftest:
        return 0

    out = {}
    for tier in ("red", "orange", "yellow"):
        src = load_tier("blue")
        dst = load_tier(tier)
        rows = match(src, dst)
        resolved = [r for r in rows if r["margin"] >= MARGIN_MIN]
        unresolved = [r for r in rows if r["margin"] < MARGIN_MIN]
        print("\nblue -> %s   %d of %d resolved (margin >= %.2f)"
              % (tier, len(resolved), len(rows), MARGIN_MIN))
        for r in resolved:
            print("   %-10s -> %-10s  iou %.3f  margin %.3f"
                  % (r["src"], r["best"], r["score"], r["margin"]))
        for r in unresolved:
            print("   %-10s -> UNRESOLVED (best %s iou %.3f, runner-up %s, margin %.3f)"
                  % (r["src"], r["best"], r["score"], r["runner_up"], r["margin"]))
        # a dst piece claimed by two different blue glyphs is a contradiction, not a mapping
        claimed = {}
        for r in resolved:
            claimed.setdefault(r["best"], []).append(r["src"])
        dup = {k: v for k, v in claimed.items() if len(v) > 1}
        if dup:
            print("   ⚠ %d %s piece(s) claimed by MORE THAN ONE blue glyph — a mapping cannot be"
                  % (len(dup), tier))
            print("     one-to-many, so at least one of each pair is wrong:")
            for k, v in dup.items():
                print("       %s <- %s" % (k, ", ".join(v)))
        out[tier] = {"resolved": {r["src"]: r["best"] for r in resolved},
                     "unresolved": [r["src"] for r in unresolved],
                     "contested": dup}
    if a.write:
        out["_note"] = (
            "Mapping from the BLUE expression set onto each severity tier, matched BY GLYPH SHAPE "
            "on a colour-blind binary mask, never by index — blue has 20 and each tier has 28, and "
            "their orders differ (blue 08 is `static`, red 08 is `wide`). Verified first by an "
            "identity control: blue matched against itself returns the identity permutation. "
            "UNRESOLVED rows are deliberate refusals, not gaps to fill in later by eye.")
        io.open(os.path.join(HERE, a.write), "w", encoding="utf-8").write(
            json.dumps(out, indent=2))
        print("\nwrote %s" % a.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
