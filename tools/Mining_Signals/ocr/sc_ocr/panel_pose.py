"""Fingerprint-held panel pose state (verify, don't re-search).

The label-row geometry used to be re-derived from scratch on every
scan — every frame was a fresh chance to mis-detect, and the steady
state paid full template sweeps for information that had not changed.
This module gives the ANCHOR layer what the value-lock layer already
proved out: remember where the rows were, fingerprint the pixels under
each anchor region, and on the next frame VERIFY the fingerprints
(microseconds) instead of re-searching (~100 ms and a false-positive
opportunity). Detection runs only when the pixels under an anchor
actually changed, or on a periodic forced re-verify that bounds
staleness.

Held state is per-image-size and verified against the CURRENT frame's
pixels on every call — there is no TTL-based trust. A panel that
moves, rescales, or disappears changes the pixels under at least one
anchor band, breaks that fingerprint, and detection re-runs.

Kill switch: set environment variable ``SC_POSE_HOLD=0`` to disable
holding entirely (every call falls through to full detection).

Calibration note (2026-06-12): fingerprint tiles are 24x10 grayscale
of each row band's label region, compared by zero-mean NCC. On the
consecutive-capture recordings (panel_finder_recording 20260516_153757)
same-pose frame pairs score >= 0.97 while the 15 px bob between poses
drops band similarity below 0.80 — the 0.90 threshold sits between
with margin on both sides.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# Fingerprint tile geometry: small enough to compare in microseconds,
# large enough that a row of HUD text cannot slide unnoticed.
_TILE_W = 24
_TILE_H = 10

# Zero-mean NCC threshold for "this anchor region is unchanged".
_MATCH_THR = 0.90

# Bounded staleness: force one full re-detection after this many
# consecutive holds even if every fingerprint still matches. Mirrors
# the value-lock layer's periodic reverify cadence.
_FORCE_REDETECT_EVERY = 20

_lock = threading.Lock()
_state: dict = {
    "rows": None,        # last accepted label-rows dict
    "tiles": None,       # {key: (box, tile_float32)} fingerprints
    "img_size": None,    # (w, h) the state was built against
    "holds": 0,          # consecutive holds since last detection
    "ts": 0.0,           # monotonic time of last store()
}


def _band_box(img_w: int, img_h: int, entry) -> Optional[tuple]:
    """Fingerprint region for one row entry: the LABEL side of the
    band (x 0..label_right), where the static text lives. The value
    side is excluded on purpose — values change while geometry holds
    (that is the value-lock layer's job, not ours)."""
    try:
        y1, y2 = int(entry[0]), int(entry[1])
        lr = int(entry[2]) if len(entry) > 2 else img_w
        x2 = max(24, min(img_w, lr))
        if y2 - y1 < 4 or y2 > img_h or y1 < 0:
            return None
        return (0, y1, x2, y2)
    except Exception:
        return None


def _tile(img: Image.Image, box: tuple) -> Optional[np.ndarray]:
    try:
        t = img.crop(box).convert("L").resize(
            (_TILE_W, _TILE_H), Image.BILINEAR,
        )
        return np.asarray(t, dtype=np.float32)
    except Exception:
        return None


def _sim(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-mean NCC of two equal-shape tiles."""
    fa = a - a.mean()
    fb = b - b.mean()
    den = float(np.sqrt((fa * fa).sum() * (fb * fb).sum())) + 1e-6
    return float((fa * fb).sum() / den)


def observe(img: Image.Image) -> Optional[dict]:
    """Return the held label-rows dict when EVERY anchor fingerprint
    matches the current frame, else None (caller runs detection)."""
    with _lock:
        rows = _state["rows"]
        tiles = _state["tiles"]
        size = _state["img_size"]
        holds = _state["holds"]
    if not rows or not tiles:
        return None
    if size != img.size:
        return None
    if holds >= _FORCE_REDETECT_EVERY:
        log.debug(
            "panel_pose: forced re-detection after %d holds", holds,
        )
        return None
    for key, (box, ref) in tiles.items():
        cur = _tile(img, box)
        if cur is None:
            return None
        s = _sim(ref, cur)
        if s < _MATCH_THR:
            log.debug(
                "panel_pose: fingerprint break key=%s sim=%.3f "
                "(thr=%.2f) — trying envelope snap", key, s, _MATCH_THR,
            )
            return _snap(img)
    with _lock:
        # Re-check the state was not replaced while we verified.
        if _state["rows"] is not rows:
            return None
        _state["holds"] += 1
        held_n = _state["holds"]
    if held_n in (1, _FORCE_REDETECT_EVERY - 1):
        log.debug("panel_pose: pose HELD (%d consecutive)", held_n)
    return dict(rows)


_SNAP_RANGE = 20   # px, p99 of measured live motion (max seen: 15)
_SNAP_AGREE = 2    # px, max disagreement between anchors' deltas


def _snap(img: Image.Image) -> Optional[dict]:
    """Bounded pose re-acquisition: when a fingerprint breaks, search
    ONLY the measured jitter envelope (±_SNAP_RANGE px, VERTICAL — every
    recorded live reposition is pure dy; horizontal motion escalates to
    full detection) for a delta at which the WHOLE rigid pose re-locks.
    All anchors must agree on one shared delta within _SNAP_AGREE px and
    the mean similarity at that delta must clear _MATCH_THR — a
    lookalike elsewhere cannot drag one anchor without the others
    vetoing. On success the rows are shifted, re-fingerprinted via
    store(), and returned; any failure returns None (full detection +
    row-consensus acquisition handle it)."""
    # OPT-IN (SC_POSE_SNAP=1). Measured 2026-06-12: on the warm 117-
    # frame live recording the snap cut full detections 40 -> 7, BUT
    # the value harness regressed (instability 28->26, mineral 84->81)
    # because back-to-back DIFFERENT panels alias: the fingerprints
    # cover the LABEL side of each band and labels are the SAME TEXT
    # on every rock — position is the only discriminator, and the
    # snap searches positions by design. Safe snapping needs
    # rock-SPECIFIC tile content (name/value bands at higher
    # resolution) before this can default on.
    import os as _os
    if _os.environ.get("SC_POSE_SNAP") != "1":
        return None
    with _lock:
        rows = _state["rows"]
        tiles = _state["tiles"]
        size = _state["img_size"]
    if not rows or not tiles or size != img.size:
        return None
    deltas: list = []
    sims: list = []
    for _key, (box, ref) in tiles.items():
        bx1, by1, bx2, by2 = box
        bs, bd = -1.0, None
        for dy in range(-_SNAP_RANGE, _SNAP_RANGE + 1):
            if by1 + dy < 0 or by2 + dy > img.height:
                continue
            cur = _tile(img, (bx1, by1 + dy, bx2, by2 + dy))
            if cur is None:
                continue
            s = _sim(ref, cur)
            if s > bs:
                bs, bd = s, dy
        if bd is None:
            return None
        deltas.append(bd)
        sims.append(bs)
    if len(deltas) < 2:
        return None
    if max(deltas) - min(deltas) > _SNAP_AGREE:
        log.debug(
            "panel_pose: snap rejected — anchors disagree on delta %s",
            deltas,
        )
        return None
    # EVERY anchor must individually clear the hold threshold at the
    # shared delta — a snap re-establishes a FULL hold, not a partial
    # one. A mean-based bar let one weak anchor ride along, and on
    # back-to-back DIFFERENT panels whose row layouts alias within the
    # envelope that locked the previous panel's geometry onto the new
    # one (value-harness instability/mineral regression, 2026-06-12).
    if min(sims) < _MATCH_THR:
        log.debug(
            "panel_pose: snap rejected — weakest anchor %.3f < %.2f",
            min(sims), _MATCH_THR,
        )
        return None
    dy = int(round(sum(deltas) / len(deltas)))
    if dy == 0:
        # Fingerprint broke at the SAME pose (appearance change, not
        # motion) — that needs real re-detection, not a snap.
        return None
    shifted = {
        k: (int(v[0]) + dy, int(v[1]) + dy) + tuple(v[2:])
        for k, v in rows.items()
    }
    store(img, shifted)
    log.info(
        "panel_pose: pose SNAPPED dy=%+d (anchors=%d mean_sim=%.3f)",
        dy, len(deltas), sum(sims) / len(sims),
    )
    return dict(shifted)


def store(img: Image.Image, rows: dict) -> None:
    """Fingerprint the anchor regions of a fresh detection result."""
    if not rows:
        return
    needed = [k for k in ("mass", "resistance", "instability") if k in rows]
    if len(needed) < 2:
        # Too little geometry to verify against — do not hold partial
        # results; detection should keep running until rows are solid.
        return
    tiles: dict = {}
    w, h = img.size
    for key in needed + (["_mineral_row"] if "_mineral_row" in rows else []):
        box = _band_box(w, h, rows[key])
        if box is None:
            continue
        t = _tile(img, box)
        if t is None:
            continue
        tiles[key] = (box, t)
    if len(tiles) < 2:
        return
    with _lock:
        _state["rows"] = dict(rows)
        _state["tiles"] = tiles
        _state["img_size"] = img.size
        _state["holds"] = 0
        _state["ts"] = time.monotonic()


def reset() -> None:
    """Drop all held state (panel gone / region changed / tests)."""
    with _lock:
        _state["rows"] = None
        _state["tiles"] = None
        _state["img_size"] = None
        _state["holds"] = 0
        _state["ts"] = 0.0
