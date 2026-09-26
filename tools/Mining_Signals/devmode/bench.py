"""Benchmark a model on the HELD-OUT confirmed captures.

Score = whole-value exact match per capture: segment the capture into
len(label) tiles (devmode.segment — the same cut used for training), run
the model, drop icon ('@') predictions exactly as the runtime does, and
compare with the human label. A capture that cannot be cut counts as a
miss for every model, so it never favours one side.

per_class is per-character accuracy over captures that segmented cleanly.

Only captures with status 'confirmed' are ever scored; proposals are not
ground truth. Any capture id listed in a model's sidecar ``trainCaptureIds``
is excluded as well (belt and braces for labels edited after training).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
from PIL import Image

from . import kinds, labels, segment

log = logging.getLogger(__name__)

_sessions: dict = {}
_tiles: dict = {}
_TILE_CACHE_MAX = 20000


class BenchmarkError(RuntimeError):
    pass


def _session(path: Path):
    import onnxruntime as ort  # noqa: WPS433
    key = (str(path), path.stat().st_mtime_ns)
    s = _sessions.get(key)
    if s is None:
        try:
            s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        except Exception as exc:  # onnxruntime's pybind errors do not derive from RuntimeError
            raise BenchmarkError(f"cannot load {path.name}: {exc}") from exc
        _sessions.clear()  # keep only a handful alive
        _sessions[key] = s
    return s


def sidecar(model_path: Path) -> dict:
    side = Path(model_path).with_suffix(".json")
    if side.is_file():
        try:
            return json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("devmode: unreadable sidecar %s: %s", side, exc)
    return {}


def model_classes(kind: str, model_path: Path, n_out: int) -> str:
    cc = sidecar(model_path).get("charClasses") or kinds.classes_for(kind)
    if len(cc) == n_out:
        return cc
    if len(cc) + 1 == n_out and "@" not in cc:
        return cc + "@"
    raise BenchmarkError(f"{model_path.name}: {n_out} outputs but classes {cc!r}")


def default_model(kind: str) -> Path:
    from .activate import active_model_path  # noqa: WPS433 — avoid import cycle
    ap = active_model_path(kind)
    return Path(ap) if ap else kinds.stock_model_path(kind)


def heldout_captures(kind: str, exclude_ids: Iterable[str] = ()) -> list[dict]:
    ex = set(exclude_ids)
    return [c for c in labels.confirmed_rows(kinds.family(kind), "heldout") if c["id"] not in ex]


def _tiles_for(cap: dict, kind: str):
    key = (cap["id"], cap["label"], kinds.is_rgb(kind))
    if key in _tiles:
        return _tiles[key]
    try:
        with Image.open(cap["image_path"]) as im:
            im.load()
            img = im.copy()
    except (OSError, ValueError) as exc:
        log.warning("devmode: held-out capture %s unreadable: %s", cap["id"], exc)
        return None
    t = segment.glyphs_for(img, kind, cap["label"])
    if len(_tiles) > _TILE_CACHE_MAX:
        _tiles.clear()
    _tiles[key] = t
    return t


def benchmark(kind: str, model_path: Optional[str] = None, *,
              captures: Optional[list[dict]] = None) -> dict:
    kinds.check_kind(kind)
    mp = Path(model_path) if model_path else default_model(kind)
    if not mp.is_file():
        raise BenchmarkError(f"model not found: {mp}")
    if captures is None:
        captures = heldout_captures(kind, sidecar(mp).get("trainCaptureIds") or ())
    sess = _session(mp)
    inp = sess.get_inputs()[0].name
    n_out = int(sess.get_outputs()[0].shape[-1])
    classes = model_classes(kind, mp, n_out)

    correct = seg_fail = 0
    pc_ok: dict[str, int] = {}
    pc_n: dict[str, int] = {}
    for cap in captures:
        label = cap["label"]
        tiles = _tiles_for(cap, kind)
        if not tiles:
            seg_fail += 1
            continue
        try:
            logits = sess.run(None, {inp: segment.to_input(tiles, kind)})[0]
        except Exception as exc:  # onnxruntime raises pybind-level errors (shape/type mismatch)
            raise BenchmarkError(f"{mp.name} failed on capture {cap['id']}: {exc}") from exc
        pred_chars = [classes[int(i)] for i in np.argmax(logits, axis=1)]
        for truth, p in zip(label, pred_chars):
            pc_n[truth] = pc_n.get(truth, 0) + 1
            pc_ok[truth] = pc_ok.get(truth, 0) + (1 if p == truth else 0)
        if "".join(ch for ch in pred_chars if ch != "@") == label:
            correct += 1
    n = len(captures)
    return {
        "accuracy": (correct / n) if n else 0.0,
        "n": n,
        "correct": correct,
        "segment_failures": seg_fail,
        "per_class": {ch: pc_ok[ch] / pc_n[ch] for ch in sorted(pc_n)},
        "per_class_n": dict(sorted(pc_n.items())),
        "model": str(mp),
        "kind": kind,
    }
