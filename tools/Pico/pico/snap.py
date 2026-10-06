"""pico/snap.py — SNAP STATES: draw a prop onto a loop at runtime instead of baking it into a copy.

Snap states exist to cut down the number of saved animations. One
loop + its <loop>.anchors.json (grip points per desktop frame) + a small prop PNG replaces a whole
baked animation per prop. Bomb gags stay their own baked animations because the POSE changes.

Pure logic, no Qt: given a frame's anchors and a prop's record from out/snap_props/snap_props.json,
return the rectangle to draw the prop in, in the loop's own pixel space. The window scales it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

PROPS_DIR = Path(__file__).resolve().parent.parent / "out" / "snap_props"


class SnapError(Exception):
    pass


def load_props(folder: Path = PROPS_DIR) -> dict:
    path = Path(folder) / "snap_props.json"
    if not path.exists():
        raise SnapError("no snap props at %s" % path)
    return json.loads(path.read_text(encoding="utf-8"))


def load_anchors(loop_path: Path) -> Optional[dict]:
    """The anchors next to a loop (<loop>.anchors.json), or None if it has none (an old build)."""
    p = Path(str(loop_path).rsplit(".", 1)[0] + ".anchors.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def authored_size(rec: dict, png_size: tuple[int, int]) -> tuple[int, int]:
    """The (w, h) a prop is placed by: the size its art was authored at.

    That is the PNG's own size, unless the record carries "src_size". The installer build ships each prop
    PNG no larger than it can ever be drawn (build/shrink_pico_props.py) and writes the authored size into
    the record, because a resized PNG's aspect ratio is off by a rounding step, and place() turns that
    into a prop that sits a pixel off the flipper. With "src_size" the rectangle is the same to the last
    digit whichever PNG is on disk."""
    src = rec.get("src_size")
    if src and len(src) == 2 and src[0] > 0 and src[1] > 0:
        return (int(src[0]), int(src[1]))
    return (int(png_size[0]), int(png_size[1]))


def place(rec: dict, frame: dict, belly_w: float, prop_size: tuple[int, int]) -> Optional[tuple]:
    """Rectangle (x, y, w, h) for the prop on this frame, in loop pixels, or None if the frame has no
    usable anchor. prop_size is the prop's authored (w, h), from authored_size(); only its aspect ratio
    matters."""
    pt = frame.get(rec["anchor"])
    if not pt:
        return None
    pw, ph = prop_size
    long_side = rec["size"] * belly_w
    k = long_side / float(max(pw, ph))
    w, h = pw * k, ph * k
    gx, gy = rec["grip"][0] * w, rec["grip"][1] * h
    dx, dy = rec.get("offset", [0.0, 0.0])
    x = pt[0] + dx * belly_w - gx
    y = pt[1] + dy * belly_w - gy
    return (x, y, w, h)


def selftest() -> int:
    fails = 0

    def ck(name, ok):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name)
        fails += 0 if ok else 1

    rec = {"anchor": "tip", "size": 0.5, "grip": [0.25, 0.5], "offset": [0.0, 0.0]}
    r = place(rec, {"tip": [100.0, 200.0]}, 200.0, (200, 100))
    ck("the grip point lands on the anchor", r is not None and abs(r[0] + 0.25 * r[2] - 100) < 1e-6
       and abs(r[1] + 0.5 * r[3] - 200) < 1e-6)
    ck("size is the longer side as a multiple of belly_w", r is not None and abs(r[2] - 100) < 1e-6)
    ck("a frame without the anchor draws nothing", place(rec, {"tip": None}, 200.0, (200, 100)) is None)
    rec2 = dict(rec, offset=[0.1, -0.1])
    r2 = place(rec2, {"tip": [100.0, 200.0]}, 200.0, (200, 100))
    ck("offset moves it in belly units", abs(r2[0] - r[0] - 20) < 1e-6 and abs(r2[1] - r[1] + 20) < 1e-6)
    ck("a record with no src_size is placed by its PNG", authored_size(rec, (200, 100)) == (200, 100))
    small = dict(rec, src_size=[1024, 333])
    ck("a smaller PNG is placed by the size it was authored at", authored_size(small, (400, 130)) == (1024, 333)
       and place(small, {"tip": [100.0, 200.0]}, 200.0, authored_size(small, (400, 130)))
       == place(rec, {"tip": [100.0, 200.0]}, 200.0, (1024, 333)))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest())
