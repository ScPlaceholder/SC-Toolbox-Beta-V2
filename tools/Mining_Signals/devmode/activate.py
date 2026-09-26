"""Compare / activate / revert per region kind.

"current" = the user's activated model if there is one, else the shipped
model. A candidate is activated ONLY when, on the same held-out set, its
exact-match accuracy is STRICTLY higher than current's and the set holds at
least MIN_HELDOUT captures. Equal is not better.

Activation copies the candidate to dev_root()/models/<kind>/active.onnx;
shipped models are never touched. revert() deletes the active copy, which
always succeeds (missing files are fine) and returns the kind to stock.
active_model_path() is the single switch the runtime reads (see the
integration notes in the report).
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

from . import bench, kinds, train

log = logging.getLogger(__name__)

MIN_HELDOUT = 5
_ERRORS = (bench.BenchmarkError, RuntimeError, OSError, ValueError)


def _active(kind: str) -> Path:
    return train.model_dir(kind) / "active.onnx"


def active_model_path(kind: str) -> Optional[str]:
    p = _active(kind)
    if p.is_file() and p.with_suffix(".json").is_file():
        return str(p)
    return None


def _train_ids(model: Optional[Path]) -> list[str]:
    if model is None or not Path(model).is_file():
        return []
    return list(bench.sidecar(Path(model)).get("trainCaptureIds") or [])


def compare(kind: str) -> dict:
    kinds.check_kind(kind)
    cand = train.candidate_path(kind)
    has_cand = cand.is_file()
    act = active_model_path(kind)
    exclude = set(_train_ids(Path(act)) if act else []) | set(_train_ids(cand) if has_cand else [])
    caps = bench.heldout_captures(kind, exclude)
    out: dict = {"current": None, "candidate": None, "better": False, "reason": ""}
    try:
        out["current"] = bench.benchmark(kind, None, captures=caps)
    except _ERRORS as exc:
        log.warning("devmode: benchmarking current %s failed: %s", kind, exc)
        out["current"] = {"error": str(exc), "accuracy": 0.0, "n": len(caps)}
    if not has_cand:
        out["reason"] = "no candidate trained yet"
        return out
    try:
        out["candidate"] = bench.benchmark(kind, str(cand), captures=caps)
    except _ERRORS as exc:
        log.warning("devmode: benchmarking candidate %s failed: %s", kind, exc)
        out["candidate"] = {"error": str(exc), "accuracy": 0.0, "n": len(caps)}
        out["reason"] = f"candidate could not be scored: {exc}"
        return out
    cur, can = out["current"], out["candidate"]
    if "error" in cur:
        out["reason"] = f"current model could not be scored: {cur['error']}"
    elif can["n"] != cur["n"]:
        out["reason"] = "scored on different held-out sets"
    elif can["n"] < MIN_HELDOUT:
        out["reason"] = f"need at least {MIN_HELDOUT} held-out confirmed captures (have {can['n']})"
    elif can["accuracy"] > cur["accuracy"]:
        out["better"] = True
        out["reason"] = f"{can['accuracy']:.1%} vs {cur['accuracy']:.1%} on {can['n']} held-out captures"
    else:
        out["reason"] = (f"candidate {can['accuracy']:.1%} does not beat current "
                         f"{cur['accuracy']:.1%} on {can['n']} held-out captures")
    return out


def activate(kind: str) -> bool:
    res = compare(kind)
    if not res["better"]:
        log.info("devmode: activate(%s) refused: %s", kind, res["reason"])
        return False
    cand = train.candidate_path(kind)
    dst = _active(kind)
    tmp = dst.with_name("active.tmp.onnx")
    shutil.copyfile(cand, tmp)
    data = Path(str(cand) + ".data")
    if data.is_file():
        shutil.copyfile(data, str(dst) + ".data")
    meta = bench.sidecar(cand)
    meta.update({
        "activatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "benchmarkAtActivation": {"current": res["current"], "candidate": res["candidate"]},
        "sha256": train.file_sha256(tmp),
    })
    tmp.replace(dst)
    dst.with_suffix(".json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return True


def revert(kind: str) -> None:
    """Back to stock. The sidecar goes first: without it active_model_path()
    is None, so the revert holds even if Windows keeps the .onnx locked."""
    bench._sessions.clear()
    p = _active(kind)
    for f in (p.with_suffix(".json"), p, Path(str(p) + ".data")):
        try:
            f.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if f.suffix == ".json":
                raise
            log.warning("devmode: revert(%s) left %s behind (%s); it is inactive", kind, f.name, exc)
