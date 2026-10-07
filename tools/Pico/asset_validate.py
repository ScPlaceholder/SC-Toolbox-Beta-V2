"""asset_validate.py — the gate between a generated image and the production skin set.

J's pipeline (relayed from GPT, 2026-09-27): a local script calls the image API one asset at a
time from a manifest, and I act as art director / validator / file organiser. This is the
validator half, and it is the half that decides whether the expensive half was worth paying for.

⛔ BUILT BEFORE ANY PAID GENERATION, DELIBERATELY. The generation is separately billed —
   ~150 assets minimum, more with rejects. A validator written afterwards gets tuned until the
   images it already has pass, which is not a test, it is a rationalisation with a threshold.
   Written first, against the rules, it can still say no to everything.

WHAT IT CHECKS, and every one is mechanical — no model, no taste:

    ALPHA        the file has a real alpha channel, not RGB pretending
    CUTOUT       the background is actually transparent, not a colour baked in
    COVERAGE     the subject occupies a plausible share of the frame for what it claims to be
    EDGES        the art does not run off the frame (a clipped hat has no pivot)
    DIMENSIONS   at least the minimum useful source resolution
    UNIFORM      the image is not a flat fill or near-empty

★ THE COVERAGE RULE IS THE ONE WITH TEETH, and it is why the manifest carries a `kind`.
  A cap filling 80% of its frame is wrong — it is a penguin wearing a cap, or a mannequin, which
  is exactly the failure J's prompt negatives are fighting ("No penguin. No head. No face.").
  A cap at 3% is a speck. Neither is detectable without knowing the thing is a cap, so the
  expected band lives in the manifest per kind, not as one global number.

⚠ WHAT IT CANNOT CHECK, stated so nobody mistakes a pass for approval: whether the art is any
  good, whether it matches the established style, whether the Drake cap looks like Drake. Those
  are human judgements. A PASS here means "structurally usable as a game attachment", never
  "correct". The reject bin is for machine-detectable failure; the accept pile still needs eyes.

    python asset_validate.py <image.png> --kind headwear
    python asset_validate.py --manifest assets/asset_manifest.json --dir out/
    python asset_validate.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

# ── expected coverage bands, as fractions of the frame's own bounding box ────────────
#
# ⚠ THESE ARE SEED VALUES FROM ONE MEASURED EXAMPLE, NOT A CALIBRATION. Measured on the
#   listener penguin sheet 2026-09-27: an isolated full-body penguin frame occupies ~36% of
#   its own cell. Everything else below is reasoned from that single anchor, which is exactly
#   the kind of extrapolation I have been wrong about before, so the bands are deliberately
#   WIDE and the failure message always prints the measured value so a human can retune from
#   real data rather than from my guess.
COVERAGE: Dict[str, Tuple[float, float]] = {
    "body":       (0.20, 0.75),
    "head":       (0.15, 0.70),
    "headwear":   (0.05, 0.55),
    "jacket":     (0.10, 0.70),
    "sleeve":     (0.03, 0.45),
    "belt":       (0.03, 0.45),
    "prop":       (0.03, 0.60),
    "patch":      (0.02, 0.60),
    "expression": (0.02, 0.70),
    "fx":         (0.02, 0.95),          # effects legitimately fill the frame
    "unknown":    (0.01, 0.95),          # no claim -> no useful band; says so in the reason
}

MIN_EDGE = 256          # below this there is no point placing it on a 2048 canvas
EDGE_BLEED_FRAC = 0.002  # >0.2% of a border row/col lit counts as running off the frame
FLAT_ALPHA_SPREAD = 4    # an alpha channel this uniform is a fill, not a cutout

PASS, REJECT, CANNOT_TELL = "PASS", "REJECT", "CANNOT-TELL"


class Result:
    def __init__(self, path: str, kind: str):
        self.path, self.kind = path, kind
        self.checks: List[Tuple[str, bool, str]] = []
        self.measured: Dict[str, float] = {}

    def add(self, name: str, ok: bool, why: str) -> None:
        self.checks.append((name, bool(ok), why))

    @property
    def verdict(self) -> str:
        if any(not ok for _n, ok, _w in self.checks):
            return REJECT
        return PASS

    def failures(self) -> List[str]:
        return ["%s: %s" % (n, w) for n, ok, w in self.checks if not ok]

    def __str__(self) -> str:
        head = "%-9s %s  [kind=%s]" % (self.verdict, os.path.basename(self.path), self.kind)
        if self.verdict == PASS:
            return head
        return head + "\n" + "\n".join("    - " + f for f in self.failures())


def _load(path: str):
    """Returns (QImage, None) or (None, reason). Qt is used because the toolbox already ships it
    and because it reads exactly what the renderer will read — a second decoder could disagree
    with the thing that actually draws the asset."""
    try:
        from PySide6.QtGui import QImage
    except Exception as exc:                      # noqa: BLE001
        return None, "PySide6 unavailable (%s)" % type(exc).__name__
    img = QImage(path)
    if img.isNull():
        return None, "unreadable or not an image"
    return img.convertToFormat(QImage.Format_ARGB32), None


def validate(path: str, kind: str = "unknown") -> Result:
    r = Result(path, kind)
    img, why = _load(path)
    if img is None:
        r.add("readable", False, why)
        return r

    w, h = img.width(), img.height()
    r.measured["w"], r.measured["h"] = w, h
    r.add("dimensions", min(w, h) >= MIN_EDGE,
          "%dx%d — shortest edge under %d px, too little source to place on a 2048 canvas"
          % (w, h, MIN_EDGE))

    if not img.hasAlphaChannel():
        r.add("alpha", False, "no alpha channel at all")
        return r
    r.add("alpha", True, "")

    # sample the alpha channel
    step = max(1, min(w, h) // 256)
    alphas: List[int] = []
    lit = 0
    total = 0
    min_a, max_a = 255, 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            a = img.pixelColor(x, y).alpha()
            total += 1
            min_a = min(min_a, a)
            max_a = max(max_a, a)
            if a > 8:
                lit += 1
            alphas.append(a)

    spread = max_a - min_a
    r.measured["alpha_spread"] = spread
    r.add("cutout", spread > FLAT_ALPHA_SPREAD,
          "alpha is nearly uniform (spread %d) — this is a flat fill or an opaque image with an "
          "alpha channel bolted on, not a cutout" % spread)

    coverage = lit / float(total or 1)
    r.measured["coverage"] = coverage
    lo, hi = COVERAGE.get(kind, COVERAGE["unknown"])
    ok = lo <= coverage <= hi
    extra = ""
    if kind == "unknown":
        extra = " (no kind given, so this band is almost meaningless — pass a --kind)"
    r.add("coverage", ok,
          "%.1f%% of the frame is opaque; expected %.0f-%.0f%% for kind '%s'%s. Too high usually "
          "means the generator drew the whole penguin instead of just the part."
          % (coverage * 100, lo * 100, hi * 100, kind, extra))

    # edges: art running off the frame has no usable pivot
    def _border_lit() -> float:
        n = c = 0
        for x in range(0, w, step):
            for y in (0, h - 1):
                n += 1
                if img.pixelColor(x, y).alpha() > 8:
                    c += 1
        for y in range(0, h, step):
            for x in (0, w - 1):
                n += 1
                if img.pixelColor(x, y).alpha() > 8:
                    c += 1
        return c / float(n or 1)

    bleed = _border_lit()
    r.measured["edge"] = bleed
    r.add("edges", bleed <= EDGE_BLEED_FRAC or kind == "fx",
          "%.2f%% of the border is opaque — the art runs off the frame and has no clean pivot "
          "(fx are exempt)" % (bleed * 100))
    return r


def run_manifest(manifest_path: str, out_dir: str) -> int:
    with open(manifest_path, encoding="utf-8") as fh:
        man = json.load(fh)
    assets = man.get("assets") if isinstance(man, dict) else man
    rejected = missing = 0
    for a in assets or []:
        rel, kind = a.get("path"), a.get("kind", "unknown")
        p = os.path.join(out_dir, rel) if rel else None
        if not p or not os.path.exists(p):
            missing += 1
            print("%-9s %s  [kind=%s]" % ("MISSING", rel, kind))
            continue
        res = validate(p, kind)
        print(res)
        if res.verdict == REJECT:
            rejected += 1
    print()
    print("%d rejected, %d not generated yet" % (rejected, missing))
    print("⚠ A PASS is 'structurally usable', never 'correct'. Style, likeness and whether the "
          "Drake cap looks like Drake are human judgements this file cannot make.")
    return 1 if rejected else 0


def _selftest() -> int:
    """Fixtures built in memory, so nothing is written and no generated asset is needed.

    ★ THE CONTROL PAIR: `good` and `whole_penguin` differ ONLY in how much of the frame is
      opaque. An implementation that ignored coverage would pass both; one that rejected
      everything would fail `good`. Neither can satisfy both assertions.
    """
    fails: List[str] = []
    try:
        from PySide6.QtGui import QImage, QColor, QPainter
    except Exception as exc:                      # noqa: BLE001
        print("asset_validate selftest: CANNOT TELL — PySide6 unavailable (%s)" % type(exc).__name__)
        return 2

    import tempfile

    def make(name, size=512, fill_frac=0.3, opaque_bg=False, to_edge=False):
        img = QImage(size, size, QImage.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 255) if opaque_bg else QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.setBrush(QColor(60, 160, 255, 255))
        p.setPen(QColor(60, 160, 255, 255))
        if to_edge:
            p.drawRect(0, 0, size - 1, size - 1)
        else:
            side = int(size * (fill_frac ** 0.5))
            off = (size - side) // 2
            p.drawRect(off, off, side, side)
        p.end()
        d = tempfile.mkdtemp()
        path = os.path.join(d, name + ".png")
        img.save(path)
        return path

    good = validate(make("good", fill_frac=0.30), "headwear")
    if good.verdict != PASS:
        fails.append("a clean 30%% cutout was rejected: %s" % good.failures())

    whole = validate(make("whole_penguin", fill_frac=0.92), "headwear")
    if whole.verdict != REJECT:
        fails.append("★ CONTROL: an 92%-coverage image passed as 'headwear' — the coverage rule "
                     "is not firing, so 'generator drew the whole penguin' cannot be caught")

    opaque = validate(make("opaque", opaque_bg=True), "headwear")
    if opaque.verdict != REJECT:
        fails.append("an opaque background passed — background contamination is undetected")

    tiny = validate(make("tiny", size=64), "headwear")
    if tiny.verdict != REJECT:
        fails.append("a 64px image passed the dimensions check")

    edge = validate(make("edge", to_edge=True), "headwear")
    if edge.verdict != REJECT:
        fails.append("art running to the border passed the edge check")

    missing = validate(os.path.join(tempfile.mkdtemp(), "nope.png"), "headwear")
    if missing.verdict != REJECT:
        fails.append("a nonexistent file did not reject")

    print("asset_validate selftest:", "PASS" if not fails else "FAIL")
    for f in fails:
        print("   -", f)
    return 1 if fails else 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                              # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?")
    ap.add_argument("--kind", default="unknown")
    ap.add_argument("--manifest")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.manifest:
        return run_manifest(a.manifest, a.dir)
    if not a.image:
        ap.print_help()
        return 2
    res = validate(a.image, a.kind)
    print(res)
    for k, v in sorted(res.measured.items()):
        print("    %-14s %s" % (k, round(v, 4) if isinstance(v, float) else v))
    return 1 if res.verdict == REJECT else 0


if __name__ == "__main__":
    sys.exit(main())
