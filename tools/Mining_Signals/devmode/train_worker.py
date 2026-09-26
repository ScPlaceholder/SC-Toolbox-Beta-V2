"""Subprocess trainer. Launched by train.py; prints ``PROGRESS <frac> <msg>``
and a final ``RESULT {json}`` line. Needs torch; nothing else in Dev Mode
does.

The model architecture is the maintainers' own builder function, lifted out
of its source file with ``ast`` so that importing the trainer module (and its
side effects on the shipped model directory) never happens.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def load_builder(file: str, function: str):
    src = Path(file).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=file)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function), None)
    if fn is None:
        raise SystemExit(f"builder {function} not found in {file}")
    mod = ast.Module(body=[fn], type_ignores=[])
    ns: dict = {"__name__": "devmode_builder"}
    exec(compile(mod, file, "exec"), ns)  # noqa: S102 — the toolbox's own source file
    return ns[function]


def progress(frac: float, msg: str) -> None:
    print(f"PROGRESS {frac:.4f} {msg}", flush=True)


def load_samples(man: dict) -> tuple[np.ndarray, np.ndarray]:
    mode = "RGB" if man["rgb"] else "L"
    xs, ys = [], []
    for path, idx in man["samples"]:
        with Image.open(path) as im:
            arr = np.asarray(im.convert(mode).resize((28, 28), Image.BILINEAR), dtype=np.float32) / 255.0
        xs.append(arr)
        ys.append(int(idx))
    X = np.stack(xs)
    X = X.transpose(0, 3, 1, 2) if man["rgb"] else X[:, None, :, :]
    if man["invert"]:
        X = 1.0 - X
    return X.astype(np.float32), np.asarray(ys, dtype=np.int64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))

    import torch
    import torch.nn as nn

    seed = int(man.get("seed", 1337))
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    progress(0.0, "loading glyphs")
    X, y = load_samples(man)
    classes = man["classes"]
    k = len(classes)
    b = man["builder"]
    model = load_builder(b["file"], b["function"])(k, **b.get("kwargs", {}))

    rng = np.random.default_rng(seed)
    perm = rng.permutation(len(X))
    n_val = max(k, len(X) // 7)
    va, tr = perm[:n_val], perm[n_val:]
    Xtr, ytr = torch.from_numpy(X[tr]), torch.from_numpy(y[tr])
    Xva, yva = torch.from_numpy(X[va]), torch.from_numpy(y[va])

    cnt = np.maximum(np.bincount(y[tr], minlength=k).astype(np.float32), 1.0)
    w = cnt.sum() / (k * cnt)
    w = np.minimum(w, float(np.median(w)) * 5.0).astype(np.float32)
    crit = nn.CrossEntropyLoss(weight=torch.from_numpy(w))
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    epochs = max(1, int(man.get("epochs", 30)))
    sched = torch.optim.lr_scheduler.StepLR(opt, step_size=max(1, epochs // 3), gamma=0.5)
    bs = 64
    best, best_state = -1.0, None
    for ep in range(epochs):
        model.train()
        order = torch.randperm(len(Xtr))
        for i in range(0, len(Xtr), bs):
            idx = order[i:i + bs]
            opt.zero_grad()
            loss = crit(model(Xtr[idx]), ytr[idx])
            loss.backward()
            opt.step()
        sched.step()
        model.eval()
        with torch.no_grad():
            acc = float((model(Xva).argmax(1) == yva).float().mean()) if len(Xva) else 0.0
        if acc > best:
            best = acc
            best_state = {kk: v.detach().clone() for kk, v in model.state_dict().items()}
        progress((ep + 1) / (epochs + 1), f"epoch {ep + 1}/{epochs} val={acc * 100:.1f}%")
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()

    progress(epochs / (epochs + 1), "exporting ONNX")
    c = 3 if man["rgb"] else 1
    dummy = torch.randn(1, c, 28, 28)
    buf = io.BytesIO()
    out_name = man.get("output_name") or "logits"
    kw = dict(input_names=["input"], output_names=[out_name],
              dynamic_axes={"input": {0: "batch"}, out_name: {0: "batch"}}, opset_version=13)
    try:
        torch.onnx.export(model, dummy, buf, dynamo=False, **kw)
    except TypeError:
        torch.onnx.export(model, dummy, buf, **kw)
    Path(man["out"]).write_bytes(buf.getvalue())
    print("RESULT " + json.dumps({"val_acc": best, "n": int(len(X))}), flush=True)
    progress(1.0, "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
