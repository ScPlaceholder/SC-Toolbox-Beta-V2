"""Train v2 RGB CNN for the signal_rgb region with the @ icon class.

This is a one-shot v2 driver. It:
  * Reuses ``train_for_region._load_dataset('signal_rgb')`` to load the
    11-class RGB dataset from training_data_user_sig_rgb/ (which now
    includes the icon/ folder thanks to the updated label_set).
  * Trains the same DigitCNN architecture as the v1 but with
    on-the-fly augmentation (rotation ±10°, scale ±15%, brightness/
    contrast jitter, slight gaussian noise).
  * Writes ONNX + JSON to a v2-specific path so v1 stays untouched.

Usage:
    python _train_v2_rgb_cnn.py [--epochs 30] [--lr 1e-3]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import TensorDataset, DataLoader

# Make the staging dir's `ocr` importable. We use the production
# Mining_Signals install (where the registry update lives).
ROAMING_TOOL = Path(
    r"C:\Users\prjgn\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)
sys.path.insert(0, str(ROAMING_TOOL))
sys.path.insert(0, str(ROAMING_TOOL / "scripts"))

from ocr import training_registry  # noqa: E402
from train_for_region import _load_dataset, _build_model  # noqa: E402

KIND = "signal_rgb"
SPEC = training_registry.get(KIND)
MODELS_DIR = ROAMING_TOOL / "ocr" / "models"
V2_ONNX = MODELS_DIR / "model_signal_rgb_cnn_v2.onnx"
V2_META = MODELS_DIR / "model_signal_rgb_cnn_v2.json"


def augment_batch(xb: torch.Tensor, training: bool) -> torch.Tensor:
    """Affine + photometric augmentation for RGB tensors (B, 3, H, W).

    Affine: translate ±0.1, rotate ±10°, scale 0.85-1.15.
    Photometric: brightness ±15%, contrast 0.85-1.15, gaussian noise σ=0.02.
    """
    if not training:
        return xb
    B = xb.shape[0]
    device = xb.device
    angles = (torch.rand(B, device=device) - 0.5) * 0.35  # ±~10°
    tx = (torch.rand(B, device=device) - 0.5) * 0.2
    ty = (torch.rand(B, device=device) - 0.5) * 0.2
    scales = 0.85 + torch.rand(B, device=device) * 0.30  # 0.85..1.15
    cos = torch.cos(angles) / scales
    sin = torch.sin(angles) / scales
    theta = torch.zeros(B, 2, 3, device=device)
    theta[:, 0, 0] = cos
    theta[:, 0, 1] = -sin
    theta[:, 0, 2] = tx
    theta[:, 1, 0] = sin
    theta[:, 1, 1] = cos
    theta[:, 1, 2] = ty
    grid = F.affine_grid(theta, xb.size(), align_corners=False)
    out = F.grid_sample(
        xb, grid, mode="bilinear", padding_mode="zeros", align_corners=False
    )
    # Photometric jitter — per-sample scalar brightness + contrast
    brightness = (torch.rand(B, 1, 1, 1, device=device) - 0.5) * 0.30  # ±0.15
    contrast = 0.85 + torch.rand(B, 1, 1, 1, device=device) * 0.30  # 0.85..1.15
    out = (out - 0.5) * contrast + 0.5 + brightness
    # Gaussian noise
    out = out + torch.randn_like(out) * 0.02
    return out.clamp(0.0, 1.0)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--val-split", type=float, default=0.10)
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--batch", type=int, default=64)
    args = p.parse_args()

    print(f"=== v2 RGB CNN trainer ({KIND}) ===")
    print(f"  staging:    {SPEC.glyph_staging_dir}")
    print(f"  label_set:  {SPEC.label_set!r}")
    print(f"  v2 onnx:    {V2_ONNX}")
    print(f"  v2 meta:    {V2_META}")
    print()

    # Load dataset (registry-driven; uses updated label_set incl. @)
    print("[load] reading dataset…")
    X, y, char_classes = _load_dataset(KIND)
    if len(X) == 0:
        print("[!] empty dataset — abort.")
        return 1
    n_classes = len(char_classes)
    bincounts = np.bincount(y, minlength=n_classes)
    print(f"[load] N={len(X)}  classes={n_classes}  shape={X.shape}")
    for i, ch in enumerate(char_classes):
        print(f"        {ch!r}: {int(bincounts[i])}")

    # Train/val split (random, but stratification not strictly needed
    # since we have 200/class for digits and 606 for icon — large enough
    # that random split lands roughly proportional).
    rng = np.random.default_rng(args.seed)
    perm = rng.permutation(len(X))
    n_val = max(int(round(len(X) * args.val_split)), n_classes * 5)
    val_idx = perm[:n_val]
    tr_idx = perm[n_val:]
    print(f"[split] train={len(tr_idx)}  val={len(val_idx)}")

    # Build model
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[device] {device}")
    if device == "cuda":
        print(f"  GPU: {torch.cuda.get_device_name(0)}")

    in_channels = X.shape[1]
    model = _build_model(num_classes=n_classes, in_channels=in_channels).to(device)

    # Inverse-frequency class weights (capped 5x median)
    safe_counts = np.maximum(bincounts.astype(np.float32), 1.0)
    inv_freq = float(safe_counts.sum()) / (n_classes * safe_counts)
    median_w = float(np.median(inv_freq))
    weights = np.minimum(inv_freq, median_w * 5.0).astype(np.float32)
    print("[weights]")
    for i, ch in enumerate(char_classes):
        print(
            f"  {ch!r}: count={int(bincounts[i]):4d}  weight={weights[i]:.3f}"
        )
    weights_t = torch.from_numpy(weights).to(device)
    criterion = nn.CrossEntropyLoss(weight=weights_t)
    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    # Move tensors to device
    X_tr = torch.from_numpy(X[tr_idx]).to(device)
    y_tr = torch.from_numpy(y[tr_idx]).to(device)
    X_val = torch.from_numpy(X[val_idx]).to(device)
    y_val = torch.from_numpy(y[val_idx]).to(device)

    print(f"[train] epochs={args.epochs}  lr={args.lr}  batch={args.batch}  augment=ON")
    best_val_acc = 0.0
    best_state = None
    t0 = time.time()
    for epoch in range(args.epochs):
        model.train()
        perm_tr = torch.randperm(len(X_tr), device=device)
        loss_sum = 0.0
        train_correct = 0
        train_total = 0
        for i in range(0, len(X_tr), args.batch):
            bi = perm_tr[i:i + args.batch]
            xb = augment_batch(X_tr[bi], training=True)
            yb = y_tr[bi]
            optimizer.zero_grad()
            out = model(xb)
            loss = criterion(out, yb)
            loss.backward()
            optimizer.step()
            loss_sum += float(loss) * len(bi)
            train_correct += int((out.argmax(1) == yb).sum())
            train_total += len(bi)
        scheduler.step()

        model.eval()
        with torch.no_grad():
            out = model(X_val)
            pred = out.argmax(1)
            val_correct = int((pred == y_val).sum())
            val_total = len(y_val)
        train_acc = train_correct / max(train_total, 1)
        val_acc = val_correct / max(val_total, 1)
        avg_loss = loss_sum / max(train_total, 1)
        cur_lr = scheduler.get_last_lr()[0]
        print(
            f"  epoch {epoch+1:3d}/{args.epochs}: "
            f"loss={avg_loss:.4f}  train={train_acc*100:5.1f}%  "
            f"val={val_acc*100:5.1f}%  lr={cur_lr:.5f}"
        )
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    train_dt = time.time() - t0
    print(f"[train] best val_acc={best_val_acc*100:.2f}%  time={train_dt:.1f}s")

    if best_state is None:
        print("[!] no improvement — abort.")
        return 1
    model.load_state_dict(best_state)
    model.eval()
    model.to(device)

    # Confusion matrix on val split (using best weights)
    with torch.no_grad():
        out = model(X_val)
        pred = out.argmax(1).cpu().numpy()
    truth = y_val.cpu().numpy()
    cm = np.zeros((n_classes, n_classes), dtype=np.int64)
    for t, p in zip(truth, pred):
        cm[t, p] += 1

    print("\n[confusion matrix] rows=truth cols=pred")
    header = "     " + "  ".join(f"{c:>4}" for c in char_classes)
    print(header)
    for i, ch in enumerate(char_classes):
        row = "  ".join(f"{cm[i, j]:>4d}" for j in range(n_classes))
        n_class = int(cm[i].sum())
        n_corr = int(cm[i, i])
        acc = (n_corr / n_class * 100.0) if n_class > 0 else 0.0
        print(f"  {ch!r}: {row}   [{n_corr}/{n_class} = {acc:.1f}%]")

    # @-class confusion summary
    if "@" in char_classes:
        at_idx = char_classes.index("@")
        at_total = int(cm[at_idx].sum())
        at_correct = int(cm[at_idx, at_idx])
        at_wrong = at_total - at_correct
        wrong_breakdown = []
        for j in range(n_classes):
            if j != at_idx and cm[at_idx, j] > 0:
                wrong_breakdown.append(f"{char_classes[j]}:{int(cm[at_idx, j])}")
        print(
            f"\n[@-class] correct={at_correct}/{at_total}  "
            f"wrong={at_wrong} (-> {', '.join(wrong_breakdown) or 'none'})"
        )
        # Also report digits classified AS @
        digit_to_at = []
        for i, ch in enumerate(char_classes):
            if ch == "@":
                continue
            if cm[i, at_idx] > 0:
                digit_to_at.append(f"{ch}:{int(cm[i, at_idx])}")
        print(
            f"[@-class] digits misread as @: {', '.join(digit_to_at) or 'none'}"
        )

    # Export ONNX (CPU, batch=dynamic)
    model_cpu = model.to("cpu")
    dummy = torch.randn(1, in_channels, 28, 28)
    print(f"\n[export] writing {V2_ONNX.name}")
    torch.onnx.export(
        model_cpu, dummy, str(V2_ONNX),
        input_names=["input"], output_names=["logits"],
        dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=13,
    )

    meta = {
        "kind": KIND,
        "version": "v2",
        "charClasses": char_classes,
        "numClasses": n_classes,
        "inputShape": [1, in_channels, 28, 28],
        "valAccuracy": best_val_acc,
        "trainSamples": int(len(tr_idx)),
        "valSamples": int(len(val_idx)),
        "perClassCounts": {
            ch: int(bincounts[i]) for i, ch in enumerate(char_classes)
        },
        "confusionMatrix": cm.tolist(),
        "trainedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "trainingTimeSeconds": train_dt,
        "epochs": args.epochs,
        "lr": args.lr,
        "augmentations": [
            "affine: translate ±0.1, rotate ±10°, scale 0.85-1.15",
            "photometric: brightness ±15%, contrast 0.85-1.15",
            "gaussian noise sigma=0.02",
        ],
        "modelPath": str(V2_ONNX),
        "stagingDir": str(SPEC.glyph_staging_dir),
        "supersedes": "model_signal_rgb_cnn.onnx",
    }
    V2_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[export] wrote {V2_META.name}")
    print(f"\nDONE.  val_acc={best_val_acc*100:.2f}%  time={train_dt:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
