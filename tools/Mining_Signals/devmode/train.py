"""Train a candidate model for one region kind (in a subprocess).

Why a thin trainer here instead of driving the existing ``ocr/train_*.py``
scripts: every one of them hardcodes its output to the SHIPPED model path
(``spec.model_path``) and reads the maintainers' staging dirs; two
(``train_hud_cnn.py``, ``train_hud_rgb_cnn.py``) even truncate a log file in
``ocr/models`` at import time. None takes an output/data argument. So the
architecture is reused VERBATIM — the builder function is lifted out of the
original file with ``ast`` and executed alone (no module-level side
effects) — and the loop/data plumbing lives in ``train_worker.py``:

  signal, signal_inv        scripts/train_for_region.py::_build_model(in_channels=1)
  signal_rgb                ocr/train_signal_rgb_v3.py::build_model
  signal_rgb_inv            ocr/train_signal_rgb_inv_v3.py::build_model
  hud                       ocr/train_hud_cnn.py::build_model
  hud_rgb                   ocr/train_hud_rgb_cnn.py::build_model

Data = APPROVED glyphs from TRAIN-split confirmed captures + this kind's
synthetic glyphs. Nothing proposed, pending, rejected or held-out.
The candidate is written under dev_root()/models/<kind>/ — never the
install tree — and only becomes live through activate().
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from . import engine, glyphs, kinds, labels, paths, synth

log = logging.getLogger(__name__)

Progress = Optional[Callable[[float, str], None]]

BUILDERS = {
    "signal": ("scripts/train_for_region.py", "_build_model", {"in_channels": 1}),
    "signal_inv": ("scripts/train_for_region.py", "_build_model", {"in_channels": 1}),
    "signal_rgb": ("ocr/train_signal_rgb_v3.py", "build_model", {}),
    "signal_rgb_inv": ("ocr/train_signal_rgb_inv_v3.py", "build_model", {}),
    "hud": ("ocr/train_hud_cnn.py", "build_model", {}),
    "hud_rgb": ("ocr/train_hud_rgb_cnn.py", "build_model", {}),
}

DEFAULT_EPOCHS = 30

# Run the worker with sys.path set from argv: the embedded interpreter's
# ._pth file makes it ignore PYTHONPATH.
_BOOT = (
    "import sys, runpy; a = sys.argv[1:]; sys.argv = ['train_worker'] + a[2:]; "
    "sys.path[:0] = [a[1]]; sys.path.append(a[0]); "
    "runpy.run_module('devmode.train_worker', run_name='__main__')"
)


class TrainError(RuntimeError):
    pass


class TorchMissingError(TrainError):
    pass


class NotEnoughDataError(TrainError):
    pass


def model_dir(kind: str) -> Path:
    d = paths.sub("models", kinds.check_kind(kind))
    if paths.is_inside_install(d):
        raise TrainError(f"refusing to write models inside the install tree: {d}")
    return d


def candidate_path(kind: str) -> Path:
    return model_dir(kind) / "candidate.onnx"


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    data = Path(str(p) + ".data")
    if data.is_file():
        with open(data, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def stock_output_name(kind: str) -> str:
    p = kinds.stock_model_path(kind)
    if not p.is_file():
        return "logits"
    import onnxruntime as ort  # noqa: WPS433
    s = ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])
    return s.get_outputs()[0].name


def build_dataset(kind: str) -> tuple[list[tuple[str, int]], dict[str, int], list[str]]:
    """(samples, per-class counts, source capture ids). Enforces the
    label discipline with explicit tripwires."""
    fam = kinds.family(kind)
    classes = kinds.classes_for(kind)
    heldout = {c["id"] for c in labels.confirmed_rows(fam, "heldout")}
    samples: list[tuple[str, int]] = []
    counts = {ch: 0 for ch in classes}
    cap_ids: set[str] = set()
    for g in glyphs.approved_training_glyphs(kind):
        if g["capture_id"] in heldout:
            raise TrainError(f"tripwire: glyph {g['id']} comes from held-out capture {g['capture_id']}")
        if g["status"] != "approved" or g["char"] not in counts:
            continue
        samples.append((g["image_path"], classes.index(g["char"])))
        counts[g["char"]] += 1
        cap_ids.add(g["capture_id"])
    synth_root = (paths.dev_root() / "synth").resolve()
    for ch, files in synth.synth_files(kind).items():
        for f in files:
            Path(f).resolve().relative_to(synth_root)   # tripwire: raises ValueError if outside
            samples.append((f, classes.index(ch)))
            counts[ch] += 1
    return samples, counts, sorted(cap_ids)


def train(kind: str, progress: Progress = None) -> str:
    def say(f: float, m: str) -> None:
        if progress:
            progress(f, m)

    kinds.check_kind(kind)
    if not engine.torch_status()["installed"]:
        raise TorchMissingError("the training engine (PyTorch) is not installed; install it first")
    say(0.0, "collecting training data")
    samples, counts, cap_ids = build_dataset(kind)
    floor = int(kinds.spec(kind).floor_per_class)
    weak = {ch: n for ch, n in counts.items() if n < floor}
    if weak:
        raise NotEnoughDataError(
            f"need at least {floor} samples per class for {kind}; short: "
            + ", ".join(f"{ch!r}={n}" for ch, n in weak.items())
            + " (confirm more captures, approve glyphs, or run Synth)"
        )
    src, fn, kw = BUILDERS[kind]
    mdir = model_dir(kind)
    tmp_out = mdir / "candidate.tmp.onnx"
    manifest = {
        "kind": kind,
        "classes": kinds.classes_for(kind),
        "rgb": kinds.is_rgb(kind),
        "invert": kinds.is_inv(kind),
        "builder": {"file": str(paths.TOOL_DIR / src), "function": fn, "kwargs": kw},
        "samples": samples,
        "out": str(tmp_out),
        "output_name": stock_output_name(kind),
        "epochs": int(os.environ.get("SC_DEVMODE_EPOCHS", DEFAULT_EPOCHS)),
        "seed": 1337,
    }
    man_path = mdir / "train_manifest.json"
    man_path.write_text(json.dumps(manifest), encoding="utf-8")
    log_path = paths.sub("logs") / f"train_{kind}.log"
    cmd = [engine.python_exe(), "-c", _BOOT, str(paths.pyenv_dir()), str(paths.TOOL_DIR),
           "--manifest", str(man_path)]
    say(0.02, f"training {kind} on {len(samples)} glyphs")
    tail: list[str] = []
    val_acc = None
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", cwd=str(paths.TOOL_DIR),
                                creationflags=engine._popen_flags())
        assert proc.stdout is not None
        for line in proc.stdout:
            lf.write(line)
            s = line.strip()
            tail = (tail + [s])[-20:]
            if s.startswith("PROGRESS "):
                parts = s.split(" ", 2)
                try:
                    say(0.02 + 0.95 * float(parts[1]), parts[2] if len(parts) > 2 else "")
                except ValueError:
                    log.debug("devmode: bad progress line %r", s)
            elif s.startswith("RESULT "):
                val_acc = json.loads(s[len("RESULT "):]).get("val_acc")
        rc = proc.wait()
    if rc != 0 or not tmp_out.is_file():
        raise TrainError(f"trainer exited {rc}; see {log_path}\n" + "\n".join(tail))
    cand = candidate_path(kind)
    tmp_out.replace(cand)
    tmp_data = Path(str(tmp_out) + ".data")
    if tmp_data.is_file():
        shutil.move(str(tmp_data), str(cand) + ".data")
    meta = {
        "kind": kind,
        "charClasses": manifest["classes"],
        "trainedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "trainingSeconds": round(time.time() - t0, 1),
        "perClassCounts": counts,
        "trainSamples": len(samples),
        "trainCaptureIds": cap_ids,
        "internalValAccuracy": val_acc,
        "sha256": file_sha256(cand),
        "source": "devmode",
    }
    cand.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    say(1.0, "candidate ready")
    return str(cand)
