"""place_assets.py — put a generated cutout onto the 2048x2048 master canvas at its bone's pivot.

⛔ THIS STEP EXISTS BECAUSE THE GENERATOR AND THE CONTRACT DISAGREE ON PURPOSE.
   PICO_CONTRACT.md, J's words: "Every raster attachment is exported on a 2048x2048 transparent
   master canvas. Do not trim individual PNGs during authoring." The reason is the good part -- it
   makes every manufacturer attachment land on the same pivot with no per-skin offsets.
   But asking an image model for a mostly-empty 2048 square wastes almost all of the pixels it will
   ever draw: a cap filling its own 1024 frame carries roughly four times the real art of a cap
   adrift in the middle of a 2048 one. So generation happens at native size and PLACEMENT happens
   here. The contract's invariant is preserved; only the authoring shortcut is not.

★ THE ANCHOR RULES ARE WRITTEN DOWN, NOT INFERRED. A hat does not sit centred on the point it hangs
  from -- its BASE meets the anchor and the crown rises above it. A belt is centred on the body
  pivot. A held prop is centred in the hand. Inferring this from the art would mean guessing, and a
  guess here puts a helmet through a penguin's skull. Every slot names its rule; an unlisted slot
  gets CENTER and says so out loud rather than silently.

⚠ WHAT THIS CANNOT DO: it cannot tell whether the art is the right SIZE relative to the penguin.
  A correctly anchored hat that is twice too wide is still wrong, and no pivot arithmetic sees that.
  Scale is J's eye, and the manifest carries no scale yet.

    python place_assets.py                 # place everything accepted in out/
    python place_assets.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKELETON = os.path.join(HERE, "pico", "rigdata", "pico_skeleton.json")
MANIFEST = os.path.join(HERE, "assets", "asset_manifest.json")
SRC = os.path.join(HERE, "out")
DST = os.path.join(HERE, "out_placed")

# How a slot's art meets its bone pivot.
#   CENTER        — the art's centre sits on the pivot (belts, jackets, props in a hand)
#   BOTTOM_CENTER — the art's bottom edge sits on the pivot (things that REST on something)
#   TOP_CENTER    — the art's top edge sits on the pivot (things that HANG from something)
ANCHOR = {
    "headwear": "BOTTOM_CENTER",   # a cap's brim meets the head; the crown goes up
    "visor":    "CENTER",
    "backpack": "CENTER",
    "jacket_front": "CENTER",
    "jacket_back":  "CENTER",
    "shirt_or_hoodie": "CENTER",
    "belt_gear": "CENTER",
    "sleeve_L": "TOP_CENTER",      # a sleeve hangs from the shoulder end of the flipper
    "sleeve_R": "TOP_CENTER",
    "prop_L": "CENTER",
    "prop_R": "CENTER",
    "prop_world": "CENTER",
}
DEFAULT_ANCHOR = "CENTER"


def load_pivots(path=SKELETON):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    canvas = d["canvas"]
    bones = {b["name"]: b for b in d.get("bones", [])}
    slots = {s["name"]: s for s in d.get("slots", [])}
    return canvas, bones, slots


def alpha_bbox(img):
    """-> (x0, y0, x1, y1) inclusive of opaque-ish pixels, or None if the image is empty."""
    w, h = img.width(), img.height()
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > 8:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 < 0:
        return None
    return x0, y0, x1, y1


def anchor_offset(anchor, bw, bh):
    """Where the art's top-left goes, relative to the pivot, for a bw x bh trimmed piece."""
    if anchor == "BOTTOM_CENTER":
        return -bw // 2, -bh
    if anchor == "TOP_CENTER":
        return -bw // 2, 0
    return -bw // 2, -bh // 2          # CENTER


def place(src_path, dst_path, pivot, anchor, canvas):
    from PySide6.QtGui import QImage, QColor, QPainter

    img = QImage(src_path)
    if img.isNull():
        raise RuntimeError("unreadable: %s" % src_path)
    img = img.convertToFormat(QImage.Format_ARGB32)
    box = alpha_bbox(img)
    if box is None:
        raise RuntimeError("no opaque pixels at all in %s — nothing to place" % os.path.basename(src_path))
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    trimmed = img.copy(x0, y0, bw, bh)

    out = QImage(canvas["width"], canvas["height"], QImage.Format_ARGB32)
    out.fill(QColor(0, 0, 0, 0))
    dx, dy = anchor_offset(anchor, bw, bh)
    px, py = int(pivot[0] + dx), int(pivot[1] + dy)
    # ⛔⛔ OFF-CANVAS PLACEMENT USED TO CLIP IN SILENCE, AND THE FIRST REAL ASSET DID IT.
    #   DRAKE/headwear trimmed to 637x445. BOTTOM_CENTER at the hat_anchor (1024, 330) puts its top
    #   at y = 330 - 445 = -115, so 115 px of the helmet was cut off by the canvas edge and the
    #   written file looked fine: bottom on the pivot, centred on x, bbox starting at y=0.
    # ★ THE SELFTEST PASSED THROUGHOUT, because its fixture is a 40x20 block near the middle. I
    #   tested the arithmetic and never the BOUNDARY — a branch no fixture ever took.
    #   [[a-correct-rule-can-guard-a-branch-nothing-takes]]
    # ⇒ Reported, not silently fixed. Clipping usually means the art is the wrong SIZE for the rig,
    #   and shrinking it here would hide a scale problem behind a placement that "worked".
    overflow = {
        "left": max(0, -px),
        "top": max(0, -py),
        "right": max(0, (px + bw) - canvas["width"]),
        "bottom": max(0, (py + bh) - canvas["height"]),
    }
    clipped = {k: v for k, v in overflow.items() if v}

    p = QPainter(out)
    p.drawImage(px, py, trimmed)
    p.end()
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    out.save(dst_path)
    return {"trimmed": (bw, bh), "placed_at": (px, py), "anchor": anchor, "clipped": clipped}


def run(src=SRC, dst=DST, verbose=True):
    canvas, bones, slots = load_pivots()
    with open(MANIFEST, encoding="utf-8") as fh:
        man = json.load(fh)
    done, skipped, errors = [], [], []
    for a in man["assets"]:
        sp = os.path.join(src, a["path"].replace("/", os.sep))
        if not os.path.exists(sp):
            skipped.append(a["path"])
            continue
        bone = bones.get(a["bone"])
        if not bone:
            errors.append("%s: bone %r is not in the skeleton" % (a["path"], a["bone"]))
            continue
        anchor = ANCHOR.get(a["slot"], DEFAULT_ANCHOR)
        try:
            info = place(sp, os.path.join(dst, a["path"].replace("/", os.sep)),
                         (bone["x"], bone["y"]), anchor, canvas)
        except Exception as exc:                     # noqa: BLE001
            errors.append("%s: %s: %s" % (a["path"], type(exc).__name__, exc))
            continue
        done.append((a["slot"], anchor, info))
    if verbose:
        print("placed %d, skipped %d (not generated), errors %d" % (len(done), len(skipped), len(errors)))
        unlisted = sorted({s for s, anc, _ in done if s not in ANCHOR})
        if unlisted:
            print("  ⚠ %d slot(s) used the DEFAULT %s anchor because no rule names them: %s"
                  % (len(unlisted), DEFAULT_ANCHOR, ", ".join(unlisted)))
        bad = [(s, i) for s, _a, i in done if i.get("clipped")]
        if bad:
            print("  ⛔ %d asset(s) RUN OFF THE CANVAS and were clipped:" % len(bad))
            for slot, info in bad:
                print("     %-14s %s px lost: %s" % (slot, sum(info["clipped"].values()),
                                                     ", ".join("%s %d" % kv for kv in sorted(info["clipped"].items()))))
            print("     This usually means the art is the wrong SIZE for the rig, not that the")
            print("     anchor is wrong. Not auto-shrunk: that would hide a scale problem.")
        for slot, anchor, info in done[:8]:
            print("  %-14s %-14s trimmed %-11s -> top-left %s%s"
                  % (slot, anchor, "%dx%d" % info["trimmed"], info["placed_at"],
                     "  ⛔CLIPPED" if info.get("clipped") else ""))
        for e in errors:
            print("  ERROR %s" % e)
        print()
        print("⚠ Anchoring is not scale. A correctly pivoted hat that is twice too wide is still")
        print("  wrong, and no arithmetic here can see that — it needs J's eye.")
    return done, skipped, errors


def _selftest():
    import tempfile
    fails = []
    try:
        from PySide6.QtGui import QImage, QColor, QPainter
    except Exception as exc:                          # noqa: BLE001
        print("place_assets selftest: CANNOT TELL — PySide6 unavailable (%s)" % type(exc).__name__)
        return 2
    canvas, bones, _slots = load_pivots()
    tmp = tempfile.mkdtemp(prefix="picoplace_")
    try:
        # a 40x20 opaque block adrift in a 512 frame — trimming must find it wherever it sits
        src = os.path.join(tmp, "src.png")
        img = QImage(512, 512, QImage.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.fillRect(100, 60, 40, 20, QColor(255, 0, 0, 255))
        p.end()
        img.save(src)

        pivot = (1024, 330)
        for anchor, want in (("CENTER", (1024 - 20, 330 - 10)),
                             ("BOTTOM_CENTER", (1024 - 20, 330 - 20)),
                             ("TOP_CENTER", (1024 - 20, 330))):
            dst = os.path.join(tmp, anchor + ".png")
            info = place(src, dst, pivot, anchor, canvas)
            if info["trimmed"] != (40, 20):
                fails.append("%s: trimmed to %s, expected (40, 20) — the bbox is wrong"
                             % (anchor, info["trimmed"]))
            if info["placed_at"] != want:
                fails.append("%s: placed at %s, expected %s" % (anchor, info["placed_at"], want))

            # ★ THE CHECK WITH REAL TEETH: read the RESULT back and confirm the art's anchor point
            #   actually lands on the pivot. The arithmetic above could be self-consistently wrong;
            #   this measures the written file, which is what the rig will load.
            out = QImage(dst).convertToFormat(QImage.Format_ARGB32)
            box = alpha_bbox(out)
            if box is None:
                fails.append("%s: the placed canvas is EMPTY" % anchor)
                continue
            bx0, by0, bx1, by1 = box
            cx = (bx0 + bx1 + 1) // 2
            got_y = {"CENTER": (by0 + by1 + 1) // 2, "BOTTOM_CENTER": by1 + 1, "TOP_CENTER": by0}[anchor]
            if abs(cx - pivot[0]) > 1:
                fails.append("%s: art centres on x=%d but the pivot is x=%d" % (anchor, cx, pivot[0]))
            if abs(got_y - pivot[1]) > 1:
                fails.append("%s: art anchor lands at y=%d but the pivot is y=%d" % (anchor, got_y, pivot[1]))

        # ★ THE BOUNDARY, which no fixture reached until a real asset hit it. A piece taller than
        #   its own anchor height MUST be reported as clipped rather than quietly truncated.
        tall = os.path.join(tmp, "tall.png")
        t = QImage(512, 512, QImage.Format_ARGB32)
        t.fill(QColor(0, 0, 0, 0))
        tp = QPainter(t)
        tp.fillRect(20, 20, 60, 460, QColor(0, 255, 0, 255))     # 60x460, taller than pivot y=330
        tp.end()
        t.save(tall)
        info = place(tall, os.path.join(tmp, "tall_out.png"), (1024, 330), "BOTTOM_CENTER", canvas)
        if not info.get("clipped"):
            fails.append("a 460px piece anchored at y=330 must report clipping (top would be -130) "
                         "— it was truncated in silence")
        elif info["clipped"].get("top") != 130:
            fails.append("clipped top reported as %r, expected 130" % info["clipped"].get("top"))
        # and a piece that fits must NOT be flagged, or the warning is wallpaper
        ok = place(src, os.path.join(tmp, "fits.png"), (1024, 330), "BOTTOM_CENTER", canvas)
        if ok.get("clipped"):
            fails.append("a piece that fits was flagged as clipped: %r" % ok["clipped"])

        # an entirely transparent source must REFUSE, not emit a blank 2048 canvas the rig would load
        blank = os.path.join(tmp, "blank.png")
        b = QImage(64, 64, QImage.Format_ARGB32)
        b.fill(QColor(0, 0, 0, 0))
        b.save(blank)
        try:
            place(blank, os.path.join(tmp, "blank_out.png"), pivot, "CENTER", canvas)
            fails.append("an all-transparent source was placed instead of refused")
        except RuntimeError:
            pass

        print("place_assets selftest:", "PASS" if not fails else "FAIL",
              "(3 anchors verified against the written file, plus the empty-source refusal)")
        for f in fails:
            print("   -", f)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if fails else 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--dst", default=DST)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    run(src=a.src, dst=a.dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
