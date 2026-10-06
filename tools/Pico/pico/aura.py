"""pico/aura.py — OUTFIT AURAS: an effect that rides on Pico on every frame of every loop, per outfit.

The special rule: Origin has the sparkle animation on the character on every
frame. Origin is the luxury house (white top hat, gold trim, cream tailcoat), so Pico in Origin
twinkles constantly; no other outfit does.

The rule is DATA: AURAS maps an outfit name to an Aura. Origin is the only entry today; giving another
outfit an aura is one more line here, not a new branch in paint code.

Pure logic, no Qt. The window (sprite_pal.py) owns drawing; this module decides WHERE each sparkle is,
HOW BIG and HOW BRIGHT, at a given time. Positions are NORMALISED to the loop frame (0..1 in x and y),
so a loop change, or a resize from the Customise dialog, never moves a sparkle that is mid-twinkle:
the window just maps the same numbers onto the new frame rectangle.

The twinkle itself is the existing "star" FX clip from vfx_clips.json (region s60.star_d: a white
four-point star, scale 0.4 -> 0.7 -> 0.4, alpha 0.3 -> 1 -> 0.3, a 20 degree turn), sampled with
fx_play.sample() so the clip stays the single source of truth for how a star moves. Each sparkle plays
it on its OWN phase, multiplied by a sin envelope so it reaches zero at the end of each cycle; that is
the moment it hops to a new spot, so a hop is never visible as a pop.
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

TOOL_DIR = Path(__file__).resolve().parent.parent      # tools/Pico, where fx_play.py lives


class AuraError(Exception):
    pass


@dataclass(frozen=True)
class Aura:
    clip: str            # FX clip name in vfx_clips.json
    count: int           # sparkles alive at once
    size: float          # sparkle long side at the clip's biggest key, as a fraction of Pico's height
    opacity: float       # peak opacity; subtle, so a prop held in front of him stays readable
    period: float        # seconds per twinkle cycle (the clip is stretched to fit)
    margin: float        # how far past his silhouette's bounding box a sparkle may sit, frame fraction
    tint: Optional[tuple] = None   # (r, g, b) the sprite is recoloured to, keeping its brightness; None = as drawn


# The rule. Keys are outfit names as the Customise dialog shows them (case-insensitive lookup).
AURAS: dict[str, Aura] = {
    # gold, not the clip's own lavender: Origin is white and gold (the reference star read pink on the coat)
    "Origin": Aura(clip="star", count=7, size=0.11, opacity=0.85, period=1.7, margin=0.04, tint=(255, 200, 80)),
}


def aura_for(outfit: Optional[str]) -> Optional[Aura]:
    """The aura for this outfit, or None. Unknown and missing outfits get nothing."""
    if not outfit:
        return None
    key = outfit.strip().lower()
    for name, aura in AURAS.items():
        if name.lower() == key:
            return aura
    return None


def load_clip(name: str):
    """(clip, region_record, sheet_record, sample_fn) for an FX clip, via fx_play.

    fx_play.load_specs() raises SystemExit on a bad spec (it is a CLI first). A desktop pet must not
    exit because a sparkle is misconfigured, so that becomes an AuraError the window can swallow."""
    if str(TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(TOOL_DIR))
    try:
        import fx_play
        specs = fx_play.load_specs()
    except (SystemExit, OSError, ValueError, ImportError) as ex:
        raise AuraError("FX specs unreadable: %s" % ex)
    clip = specs["clips"].get(name)
    if clip is None:
        raise AuraError("no FX clip %r in vfx_clips.json" % name)
    region = next((k["region"] for k in clip["keys"] if k.get("region")), None)
    if region is None:
        raise AuraError("FX clip %r names no region" % name)
    rrec = specs["map"]["regions"][region]
    srec = specs["map"]["sheets"][rrec["sheet"]]
    return clip, rrec, srec, fx_play.sample


def box_of(cells: Sequence[tuple[float, float]], cell: float, margin: float) -> tuple:
    """Bounding box (x0, y0, x1, y1) of the silhouette cells, grown by half a cell plus margin,
    clamped to the frame. No cells means the whole frame (an empty mask must not hide the aura)."""
    if not cells:
        return (0.0, 0.0, 1.0, 1.0)
    pad = cell / 2.0 + margin
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    return (max(0.0, min(xs) - pad), max(0.0, min(ys) - pad),
            min(1.0, max(xs) + pad), min(1.0, max(ys) + pad))


class SparkleField:
    """`count` sparkles, each twinkling on its own phase and hopping to a fresh spot on Pico's body
    each time its cycle ends. Persistent across loop changes: set_area() changes where FUTURE hops
    land; it never restarts a twinkle."""

    def __init__(self, aura: Aura, clip: dict, sample: Callable, seed: Optional[int] = None):
        self.aura, self.clip, self.sample = aura, clip, sample
        self.rng = random.Random(seed)
        self.dur = float(clip.get("duration") or 1.0)
        self.max_scale = max(float(k.get("scale", 1.0)) for k in clip["_filled"]) or 1.0
        n = max(1, aura.count)
        # Even spread plus a little jitter: distinct phases, so they never blink in unison.
        self.phase = [(i + self.rng.uniform(0.0, 0.6)) / n for i in range(n)]
        self.cycle = [None] * n
        self.pos = [(0.5, 0.5)] * n
        self.cells: list[tuple[float, float]] = []
        self.cell = 0.0
        self.box = (0.0, 0.0, 1.0, 1.0)

    def set_area(self, cells: Sequence[tuple[float, float]], cell: float) -> None:
        """Where Pico's body is in the frame: centres of opaque grid cells (normalised), cell size."""
        self.cells, self.cell = list(cells), float(cell)
        self.box = box_of(self.cells, self.cell, self.aura.margin)
        # A sparkle that was placed for the previous loop and now sits outside his body box is
        # clamped in, so the rule "inside the box" holds on every frame, not just after a hop.
        self.pos = [self._clamp(p) for p in self.pos]

    def _clamp(self, p):
        x0, y0, x1, y1 = self.box
        return (min(x1, max(x0, p[0])), min(y1, max(y0, p[1])))

    def _spot(self):
        if self.cells:
            cx, cy = self.rng.choice(self.cells)
            h = self.cell / 2.0
            return self._clamp((cx + self.rng.uniform(-h, h), cy + self.rng.uniform(-h, h)))
        x0, y0, x1, y1 = self.box
        return (self.rng.uniform(x0, x1), self.rng.uniform(y0, y1))

    def states(self, t: float) -> list[tuple]:
        """Every visible sparkle at time t: (x, y, size, opacity, rot_deg). x, y normalised to the
        frame; size as a fraction of Pico's height."""
        a = self.aura
        out = []
        for i, ph in enumerate(self.phase):
            v = t / a.period + ph
            k = math.floor(v)
            u = v - k
            if self.cycle[i] != k:            # a new cycle: hop (the envelope is at zero here)
                self.cycle[i] = k
                self.pos[i] = self._spot()
            st = self.sample(self.clip, u * self.dur)
            env = math.sin(math.pi * u)
            alpha = a.opacity * float(st["alpha"]) * env
            if alpha <= 0.01:
                continue
            size = a.size * float(st["scale"]) / self.max_scale
            out.append((self.pos[i][0], self.pos[i][1], size, alpha, float(st["rot"])))
        return out


def selftest() -> int:
    fails = 0

    def ck(name, ok):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name)
        fails += 0 if ok else 1

    # -- the rule: Origin, and only Origin ------------------------------------------------------
    ck("Origin has an aura", aura_for("Origin") is not None)
    ck("lookup ignores case and padding", aura_for(" origin ") is aura_for("Origin"))
    ck("Drake has no aura", aura_for("Drake") is None)
    ck("an unknown outfit has no aura", aura_for("Anvil") is None and aura_for("") is None
       and aura_for(None) is None)
    ck("Origin is the ONLY entry today", [k for k in AURAS if aura_for(k)] == ["Origin"])

    # -- the field, against a fake clip shaped like the real "star" one ------------------------
    fake = {"duration": 0.9, "_filled": [{"scale": 0.4}, {"scale": 0.7}, {"scale": 0.4}]}

    def fake_sample(clip, t):
        f = t / clip["duration"]
        return {"alpha": 0.3 + 0.7 * math.sin(math.pi * f), "scale": 0.4 + 0.3 * math.sin(math.pi * f),
                "rot": 20.0 * f}

    aura = AURAS["Origin"]
    cells = [(0.40 + 0.05 * i, 0.30 + 0.05 * j) for i in range(5) for j in range(9)]   # a body blob
    fld = SparkleField(aura, fake, fake_sample, seed=7)
    fld.set_area(cells, 0.05)
    x0, y0, x1, y1 = fld.box
    ck("box hugs the silhouette plus margin", abs(x0 - (0.40 - 0.025 - aura.margin)) < 1e-9
       and abs(y1 - (0.70 + 0.025 + aura.margin)) < 1e-9)
    inside, seen, n_vis = True, set(), 0
    for step in range(600):                       # 600 x 40 ms = 24 s, many hops per sparkle
        for (x, y, size, alpha, rot) in fld.states(step * 0.04):
            n_vis += 1
            inside &= (x0 - 1e-9 <= x <= x1 + 1e-9) and (y0 - 1e-9 <= y <= y1 + 1e-9)
            inside &= 0 < alpha <= aura.opacity + 1e-9 and 0 < size <= aura.size + 1e-9
            seen.add((round(x, 4), round(y, 4)))
    ck("every sparkle, every frame, stays inside the box", inside and n_vis > 1000)
    ck("sparkles hop to new spots (not frozen in place)", len(seen) > 3 * aura.count)
    ph = sorted(fld.phase)
    ck("phases all differ", len(set(round(p, 6) for p in ph)) == len(ph)
       and min(b - a for a, b in zip(ph, ph[1:])) > 0.02)
    lit = [len(fld.states(t * 0.05)) for t in range(200)]
    ck("never all dark, never all in unison", min(lit) >= 1 and max(lit) <= aura.count)

    # Loop change: the field is not rebuilt, so a twinkle in flight keeps its phase and cycle.
    before = list(fld.cycle)
    fld.set_area([(0.2, 0.2), (0.25, 0.25)], 0.05)
    x0, y0, x1, y1 = fld.box
    ck("a new area does not restart any twinkle", fld.cycle == before)
    ck("sparkles from the old loop are pulled inside the new box",
       all(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in fld.pos))
    fld.set_area([], 0.05)
    ck("an empty mask falls back to the whole frame", fld.box == (0.0, 0.0, 1.0, 1.0))

    # -- the real clip, if the FX specs are on disk --------------------------------------------
    try:
        clip, rrec, srec, sample = load_clip(aura.clip)
        real = SparkleField(aura, clip, sample, seed=1)
        real.set_area(cells, 0.05)
        ok = all(0 < s[3] <= aura.opacity for t in range(50) for s in real.states(t * 0.1))
        ck("the real %r clip loads and samples (region on %s)" % (aura.clip, srec["file"]), ok)
    except AuraError as ex:
        ck("the real %r clip loads (%s)" % (aura.clip, ex), False)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
