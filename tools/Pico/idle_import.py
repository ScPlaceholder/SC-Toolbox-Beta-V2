"""idle_import.py — turn Astra's authored idle JSON into clips, and REFUSE the ones that fail.

★ J, 2026-09-27: "You can have Astra work through the animation list ... and just help keep him on
  task." This is the "keep him on task" half. Astra authors; this checks and converts.

★★ WHY A GATE AND NOT A LOADER. Astra wrote these without touching the disk — it says so itself in
  `what_you_could_not_determine`: "Exact beak indices and visor filenames; no disk assets were
  inspected." So every asset name in its output is a SEMANTIC TARGET, not a verified file, and the
  honest thing is to bind them here and fail loudly on anything that does not bind. A converter that
  silently substitutes a default face would produce clips that run perfectly and mean the wrong
  thing. [[a-silent-decision-passes-a-correctness-review]]

WHAT IT CHECKS, each because it has already bitten:
  1. LOOP CLOSURE — first and last keyframe must match in angles AND grow. My own breathing clip
     inhaled to 1.030 and never exhaled; every frame was a valid picture of a penguin holding its
     breath, and no test of the maths could see it.
  2. ASSET BINDING — every visor/beak name must exist in the tables, which point at real files.
  3. BONE NAMES — a pose driving a bone that does not exist is an angle silently ignored, i.e. a
     clip that looks wrong for no visible reason.
  4. SLOT NAMES for grow — same argument, and a typo'd slot name grows nothing at all.
  5. FRAME BUDGET — a hold under 2 frames is invisible at 20 fps and the GIF encoder may drop it.

⚠ WHAT IT CANNOT CHECK is whether the result looks good. It verifies that a clip is well-formed and
  buildable. Whether `content_pendulum` reads as contentment is an eye's call, and Astra flagged
  its own uncertainty on four of the six.

    python idle_import.py --json ../../../../../Projects/elah-audio/_astra_idles.txt --check
    python idle_import.py --json <path> --write idles_generated.py
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

EASES = ("ease_in_out", "ease_out", "linear", "overshoot")


def load(path):
    t = io.open(path, encoding="utf-8").read()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        raise SystemExit("REFUSING: no JSON object in %s" % path)
    return json.loads(t[i:j + 1])


def convert(idle, bones, slots, visor_tbl, beak_tbl):
    """Return (clip, problems). A clip is the list of tuples animate.CLIPS holds."""
    probs, clip = [], []
    kfs = idle.get("keyframes") or []
    if len(kfs) < 2:
        probs.append("only %d keyframe(s) — nothing to interpolate" % len(kfs))
    for n, kf in enumerate(kfs):
        ang = dict(kf.get("angles") or {})
        grow = dict(kf.get("grow") or {})
        swaps = dict(kf.get("swaps") or {})
        hold = int(kf.get("hold") or 0)
        trans = int(kf.get("transition") or 0)
        ease = kf.get("ease") or "ease_in_out"
        for b in ang:
            if b not in bones:
                probs.append("kf%d drives bone %r, which does not exist" % (n, b))
        for sl in grow:
            if sl not in slots:
                probs.append("kf%d grows slot %r, which does not exist" % (n, sl))
        for sl, name in list(swaps.items()):
            tbl = {"visor": visor_tbl, "beak": beak_tbl}.get(sl)
            if tbl is None:
                probs.append("kf%d swaps slot %r, which has no art source" % (n, sl))
            elif name not in tbl:
                probs.append("kf%d wants %s=%r, not in the table (have: %s)"
                             % (n, sl, name, ", ".join(sorted(tbl))))
        if ease not in EASES:
            probs.append("kf%d ease %r unknown" % (n, ease))
            ease = "ease_in_out"
        if hold and hold < 2:
            probs.append("kf%d hold=%d — under 2 frames is invisible at 20fps and the GIF "
                         "encoder may drop it" % (n, hold))
        clip.append((ang, hold, trans, ease, swaps or None, grow))
    # loop closure, on the AUTHORED keyframes rather than the expanded frames, so the message
    # names a keyframe the author can actually edit
    if len(kfs) >= 2 and idle.get("loops", True):
        a0, a1 = dict(kfs[0].get("angles") or {}), dict(kfs[-1].get("angles") or {})
        g0, g1 = dict(kfs[0].get("grow") or {}), dict(kfs[-1].get("grow") or {})
        s0, s1 = dict(kfs[0].get("swaps") or {}), dict(kfs[-1].get("swaps") or {})
        for k in set(a0) | set(a1):
            if abs(float(a0.get(k, 0)) - float(a1.get(k, 0))) > 0.05:
                probs.append("LOOP: angle %s starts %s ends %s — it will SNAP every repeat"
                             % (k, a0.get(k, 0), a1.get(k, 0)))
        for k in set(g0) | set(g1):
            if abs(float(g0.get(k, 1)) - float(g1.get(k, 1))) > 0.001:
                probs.append("LOOP: grow %s starts %s ends %s — it will SNAP every repeat"
                             % (k, g0.get(k, 1), g1.get(k, 1)))
        for k in set(s0) | set(s1):
            if s0.get(k) != s1.get(k):
                probs.append("LOOP: %s starts %r ends %r — the face changes at the loop point"
                             % (k, s0.get(k), s1.get(k)))
    return clip, probs


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--parts", default="out/rig/BASE")
    ap.add_argument("--write", help="write the accepted clips to this python file")
    a = ap.parse_args(argv)

    sys.path.insert(0, HERE)
    import rig_layout as RL
    import animate as A

    sk, _g, _s = RL.build(a.parts)
    bones = {b["name"] for b in sk["bones"]}
    slots = {s.get("name") for s in sk["slots"]}

    d = load(a.json)
    idles = d.get("idles") or []
    print("Astra authored %d idle(s). Binding them to real bones, slots and art.\n" % len(idles))

    ok, rejected, out = 0, 0, {}
    for idle in idles:
        name = idle.get("name") or "?"
        clip, probs = convert(idle, bones, slots, A.VISOR, A.BEAK)
        shape = (idle.get("shape") or "")[:38]
        if probs:
            rejected += 1
            print("  ⛔ %-22s %-38s REJECTED" % (name, shape))
            for p in probs[:6]:
                print("        %s" % p)
            if len(probs) > 6:
                print("        ... and %d more" % (len(probs) - 6))
        else:
            ok += 1
            out[name] = clip
            print("  ✓  %-22s %-38s %d keyframes, %.1fs claimed"
                  % (name, shape, len(clip), idle.get("seconds") or 0))
    print("\n%d accepted, %d rejected." % (ok, rejected))
    if rejected:
        print("⚠ A rejection is NOT a judgement on the animation — it means the spec names something")
        print("  that does not exist here. Rebinding is usually a one-word fix, and doing it")
        print("  silently is what would be dangerous.")

    if a.write and out:
        lines = ['"""idles_generated.py — AUTHORED BY ASTRA, bound and gated by idle_import.py.',
                 "",
                 "⚠ DO NOT HAND-EDIT. Regenerate from the JSON so the loop and binding checks run.",
                 "  Every asset name here was a SEMANTIC TARGET in Astra's output — it never saw the",
                 "  disk — and was bound to a real file by idle_import, which refuses anything that",
                 "  does not bind rather than substituting a default face.",
                 '"""', "", "IDLES = {"]
        for n, clip in out.items():
            lines.append("    %r: [" % n)
            for (ang, hold, trans, ease, swaps, grow) in clip:
                lines.append("        (%r, %d, %d, %r, %r, %r)," % (ang, hold, trans, ease, swaps, grow))
            lines.append("    ],")
        lines.append("}")
        io.open(os.path.join(HERE, a.write), "w", encoding="utf-8").write("\n".join(lines) + "\n")
        print("\nwrote %d clip(s) -> %s" % (len(out), a.write))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
