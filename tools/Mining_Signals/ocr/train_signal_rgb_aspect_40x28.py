"""Trainer for the aspect-matched 40x28 (HxW) RGB signal CNN.

Sibling experiment to ``train_signal_rgb_v3.py``. Same architecture
shape (3 conv blocks + FC, RGB input, ReLU, Dropout 0.3) and same
hyperparameters; the only differences are:

  * Input shape ``(3, 40, 28)`` instead of ``(3, 28, 28)``.
  * Training corpus is the 40x28 extraction at
    ``training_data_panels/_aspect_40x28/`` (signature region2 only,
    digits 0-9 — no @ class).

Output (production tree)::

  ocr/models/model_signal_rgb_aspect_40x28.onnx
  ocr/models/model_signal_rgb_aspect_40x28.json
  ocr/models/model_signal_rgb_aspect_40x28_train.log

Run::

    python ocr/train_signal_rgb_aspect_40x28.py
"""
from __future__ import annotations

import json
import logging
import random
import shutil
import sys
import time
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageEnhance

# Quarantine gate — Glyph Review decisions filter every training input.
# Disable with SC_TRAIN_NO_GATE=1. See ocr/glyph_gate.py.
try:
    from ocr.glyph_gate import filter_clean as _quarantine_filter
except Exception:  # gate unavailable -> train unfiltered (legacy behaviour)
    def _quarantine_filter(paths, **_kw):
        return list(paths)



# --- Paths ----------------------------------------------------------

THIS_FILE = Path(__file__).resolve()
PROD_TOOL_DIR = Path(
    r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals"
)

# 40x28 extraction lives in the production tree (the new extractor
# writes here regardless of where the source captures came from).
ASPECT_DIGITS_DIR = PROD_TOOL_DIR / "training_data_panels" / "_aspect_40x28"

# Staging dir for balanced/augmented samples
DIGIT_STAGING_DIR = PROD_TOOL_DIR / "_aspect_40x28_digit_staging"

# Output paths
PROD_MODELS_DIR = PROD_TOOL_DIR / "ocr" / "models"
OUT_ONNX = PROD_MODELS_DIR / "model_signal_rgb_aspect_40x28.onnx"
OUT_META = PROD_MODELS_DIR / "model_signal_rgb_aspect_40x28.json"
OUT_LOG  = PROD_MODELS_DIR / "model_signal_rgb_aspect_40x28_train.log"


# --- Config ----------------------------------------------------------

# 10 digit classes — no @ class in this experiment (per brief).
CHAR_CLASSES = "0123456789"

# Same target band as v3 — 250-400 per class via balancing.
DIGIT_TARGET_MIN = 250
DIGIT_TARGET_MAX = 400

# Training hyperparameters — match v3.
EPOCHS = 60
LR = 1e-3
VAL_SPLIT = 0.15
SEED = 1337

OUT_HEIGHT = 40
OUT_WIDTH = 28


# --- Logging --------------------------------------------------------

PROD_MODELS_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("train_signal_rgb_aspect_40x28")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(OUT_LOG, mode="w", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(message)s"))
log.handlers.clear()
log.addHandler(_fh)
log.addHandler(_sh)


# --- Augmentation utilities (same as v3) ----------------------------

def _affine_jitter(
    img: Image.Image,
    *,
    max_rot_deg: float,
    max_trans_frac: float,
    min_scale: float,
    max_scale: float,
    rng: random.Random,
) -> Image.Image:
    angle = rng.uniform(-max_rot_deg, max_rot_deg)
    sx = rng.uniform(min_scale, max_scale)
    sy = rng.uniform(min_scale, max_scale)
    tx = rng.uniform(-max_trans_frac, max_trans_frac) * img.size[0]
    ty = rng.uniform(-max_trans_frac, max_trans_frac) * img.size[1]
    out = img.rotate(angle, resample=Image.BILINEAR, fillcolor=(0, 0, 0))
    nw = max(1, int(round(out.size[0] * sx)))
    nh = max(1, int(round(out.size[1] * sy)))
    out = out.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", img.size, (0, 0, 0))
    cx = (img.size[0] - nw) // 2 + int(round(tx))
    cy = (img.size[1] - nh) // 2 + int(round(ty))
    canvas.paste(out, (cx, cy))
    return canvas


def _photo_jitter(img: Image.Image, *, rng: random.Random) -> Image.Image:
    b = rng.uniform(0.75, 1.25)
    c = rng.uniform(0.75, 1.25)
    s = rng.uniform(0.85, 1.15)
    img = ImageEnhance.Brightness(img).enhance(b)
    img = ImageEnhance.Contrast(img).enhance(c)
    img = ImageEnhance.Color(img).enhance(s)
    return img


def _digit_aug(src: Image.Image, *, rng: random.Random) -> Image.Image:
    a = _affine_jitter(
        src,
        max_rot_deg=10.0,
        max_trans_frac=0.06,
        min_scale=0.85,
        max_scale=1.15,
        rng=rng,
    )
    return _photo_jitter(a, rng=rng)


def _load_rgb_aspect(path: Path) -> Image.Image:
    """Load any image as 40x28 (HxW) RGB. Source extractor saves
    grayscale L; ``convert('RGB')`` replicates the channel."""
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    if im.size != (OUT_WIDTH, OUT_HEIGHT):
        im = im.resize((OUT_WIDTH, OUT_HEIGHT), Image.BILINEAR)
    return im


# --- Stage: balance per-class samples -------------------------------

def stage_digit_samples(rng: random.Random) -> dict:
    if DIGIT_STAGING_DIR.exists():
        log.info("[stage] wiping %s", DIGIT_STAGING_DIR)
        shutil.rmtree(DIGIT_STAGING_DIR)
    DIGIT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    counts: dict = {}
    for ch in CHAR_CLASSES:
        out_dir = DIGIT_STAGING_DIR / ch
        out_dir.mkdir(parents=True, exist_ok=True)
        src_dir = ASPECT_DIGITS_DIR / ch
        if not src_dir.is_dir():
            log.warning("[stage] missing class dir %s", src_dir)
            counts[ch] = {"src": 0, "total_after_balance": 0}
            continue
        files = _quarantine_filter(sorted(src_dir.glob("*.png")))
        n_src = len(files)
        imgs: List[Image.Image] = []
        for f in files:
            try:
                imgs.append(_load_rgb_aspect(f))
            except Exception as exc:
                log.warning("[stage] skip %s: %s", f.name, exc)

        # If the class is well-stocked beyond the cap, downsample.
        if len(imgs) > DIGIT_TARGET_MAX:
            imgs = rng.sample(imgs, DIGIT_TARGET_MAX)

        # Augment up to DIGIT_TARGET_MIN if undersized.
        if len(imgs) < DIGIT_TARGET_MIN and imgs:
            need = DIGIT_TARGET_MIN - len(imgs)
            base = list(imgs)
            i = 0
            while i < need:
                src = base[i % len(base)]
                imgs.append(_digit_aug(src, rng=rng))
                i += 1

        rng.shuffle(imgs)
        for i, im in enumerate(imgs):
            im.save(out_dir / f"{ch}_{i:04d}.png", format="PNG")
        counts[ch] = {
            "src": n_src,
            "total_after_balance": len(imgs),
        }
        log.info("[stage] class %s: src=%d total=%d", ch, n_src, len(imgs))
    return counts


# --- Load dataset ---------------------------------------------------

def load_dataset() -> Tuple[np.ndarray, np.ndarray, dict]:
    images: List[np.ndarray] = []
    labels: List[int] = []
    counts: dict = {}

    for cls_idx, ch in enumerate(CHAR_CLASSES):
        cls_dir = DIGIT_STAGING_DIR / ch
        if not cls_dir.is_dir():
            counts[ch] = 0
            continue
        n = 0
        for png in _quarantine_filter(cls_dir.glob("*.png")):
            try:
                arr = np.asarray(
                    Image.open(png).convert("RGB").resize(
                        (OUT_WIDTH, OUT_HEIGHT), Image.BILINEAR,
                    ),
                    dtype=np.float32,
                ) / 255.0
            except Exception as exc:
                log.warning("[load] skip %s: %s", png.name, exc)
                continue
            # arr shape: (H=40, W=28, 3) -> (3, 40, 28) for PyTorch
            images.append(arr.transpose(2, 0, 1))
            labels.append(cls_idx)
            n += 1
        counts[ch] = n
        log.info("[load] class %r: %d samples", ch, n)

    X = np.stack(images, axis=0).astype(np.float32)
    y = np.asarray(labels, dtype=np.int64)
    return X, y, counts


# --- Model: same shape as v3, adjusted for 40x28 input --------------

def build_model(num_classes: int):
    import torch.nn as nn

    class SignalRGBAspectCNN(nn.Module):
        def __init__(self):
            super().__init__()
            # Input: (3, 40, 28)
            # After conv + pool x2: (64, 10, 7)
            # After conv: (64, 10, 7)
            self.features = nn.Sequential(
                nn.Conv2d(3, 32, 3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),                   # (32, 20, 14)
                nn.Conv2d(32, 64, 3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),                   # (64, 10, 7)
                nn.Conv2d(64, 64, 3, padding=1),
                nn.ReLU(),
            )
            self.classifier = nn.Sequential(
                nn.Flatten(),
                nn.Linear(64 * 10 * 7, 128),
                nn.ReLU(),
                nn.Dropout(0.3),
                nn.Linear(128, num_classes),
            )

        def forward(self, x):
            return self.classifier(self.features(x))

    return SignalRGBAspectCNN()


# --- Train + export -------------------------------------------------

def train_and_export(
    X: np.ndarray, y: np.ndarray, counts: dict,
) -> Tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import TensorDataset, DataLoader

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("[train] device=%s torch=%s", device, torch.__version__)

    rng = np.random.default_rng(SEED)
    perm = rng.permutation(len(X))
    split = int(len(perm) * (1 - VAL_SPLIT))
    tr_idx, va_idx = perm[:split], perm[split:]

    X_tr = torch.from_numpy(X[tr_idx]).to(device)
    y_tr = torch.from_numpy(y[tr_idx]).to(device)
    X_va = torch.from_numpy(X[va_idx]).to(device)
    y_va = torch.from_numpy(y[va_idx]).to(device)

    bincnt = np.bincount(y, minlength=len(CHAR_CLASSES)).astype(np.float32)
    safe = np.maximum(bincnt, 1.0)
    inv = float(safe.sum()) / (float(len(CHAR_CLASSES)) * safe)
    median_w = float(np.median(inv))
    weights = np.minimum(inv, median_w * 5.0).astype(np.float32)
    log.info("[train] class weights:")
    for ch, w, c in zip(CHAR_CLASSES, weights, bincnt):
        log.info("  %r: count=%d weight=%.3f", ch, int(c), float(w))
    weights_t = torch.from_numpy(weights).to(device)

    model = build_model(len(CHAR_CLASSES)).to(device)
    criterion = nn.CrossEntropyLoss(weight=weights_t)
    optimizer = optim.Adam(model.parameters(), lr=LR)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=15, gamma=0.5)

    train_loader = DataLoader(
        TensorDataset(X_tr, y_tr), batch_size=64, shuffle=True,
    )
    val_loader = DataLoader(TensorDataset(X_va, y_va), batch_size=128)

    log.info(
        "[train] epochs=%d train=%d val=%d",
        EPOCHS, len(tr_idx), len(va_idx),
    )

    best_val = 0.0
    best_state = None
    for epoch in range(EPOCHS):
        model.train()
        loss_sum, n_seen, n_correct = 0.0, 0, 0
        for xb, yb in train_loader:
            optimizer.zero_grad()
            logits = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            optimizer.step()
            loss_sum += float(loss.item()) * len(xb)
            n_correct += int((logits.argmax(1) == yb).sum().item())
            n_seen += len(xb)
        scheduler.step()
        train_loss = loss_sum / max(n_seen, 1)
        train_acc = n_correct / max(n_seen, 1)

        model.eval()
        v_correct, v_seen = 0, 0
        with torch.no_grad():
            for xb, yb in val_loader:
                logits = model(xb)
                v_correct += int((logits.argmax(1) == yb).sum().item())
                v_seen += len(xb)
        val_acc = v_correct / max(v_seen, 1)

        log.info(
            "  epoch %3d/%d  loss=%.4f  train=%.1f%%  val=%.1f%%",
            epoch + 1, EPOCHS, train_loss, train_acc * 100, val_acc * 100,
        )

        if val_acc > best_val:
            best_val = val_acc
            best_state = {
                k: v.detach().cpu().clone()
                for k, v in model.state_dict().items()
            }

    if best_state is None:
        raise RuntimeError("No epoch improved val accuracy.")
    model.load_state_dict(best_state)
    model.eval().to(device)

    K = len(CHAR_CLASSES)
    confusion = np.zeros((K, K), dtype=np.int64)
    per_class_correct = np.zeros(K, dtype=np.int64)
    per_class_total   = np.zeros(K, dtype=np.int64)
    val_truth_list: List[int] = []
    with torch.no_grad():
        logits = model(X_va)
        pred = logits.argmax(1).cpu().numpy()
        truth = y_va.cpu().numpy()
    for p, t in zip(pred, truth):
        confusion[int(t), int(p)] += 1
        per_class_total[int(t)] += 1
        if p == t:
            per_class_correct[int(t)] += 1
        val_truth_list.append(int(t))

    log.info("[train] per-class val accuracy:")
    per_class_acc: dict = {}
    for i, ch in enumerate(CHAR_CLASSES):
        if per_class_total[i] == 0:
            log.info("  %r: (no val samples)", ch)
            per_class_acc[ch] = None
        else:
            acc = per_class_correct[i] / per_class_total[i]
            per_class_acc[ch] = float(acc)
            log.info(
                "  %r: %d/%d = %.1f%%",
                ch, int(per_class_correct[i]), int(per_class_total[i]),
                acc * 100,
            )

    log.info("[train] confusion matrix (rows=truth, cols=pred):")
    header = "      " + "  ".join(f"{c:>4}" for c in CHAR_CLASSES)
    log.info(header)
    for i, ch in enumerate(CHAR_CLASSES):
        row = "  ".join(f"{int(confusion[i, j]):4d}" for j in range(K))
        log.info("  %s  %s", ch, row)

    PROD_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dummy = torch.randn(1, 3, OUT_HEIGHT, OUT_WIDTH, device=device)
    # Force the legacy TorchScript-based exporter — the new dynamo
    # path prints UTF-8 status icons that crash on Windows cp1252
    # consoles. Equivalent ONNX output for a static MLP.
    torch.onnx.export(
        model, dummy, str(OUT_ONNX),
        input_names=["input"], output_names=["logits"],
        dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=13,
        dynamo=False,
    )
    log.info("[train] wrote ONNX: %s", OUT_ONNX)

    return float(best_val), per_class_acc, confusion, np.asarray(val_truth_list)


# --- Driver ---------------------------------------------------------

def main() -> int:
    rng = random.Random(SEED)

    log.info("=== train_signal_rgb_aspect_40x28 ===")
    log.info("aspect digits src: %s", ASPECT_DIGITS_DIR)
    log.info("digit staging:     %s", DIGIT_STAGING_DIR)
    log.info("output ONNX:       %s", OUT_ONNX)

    if not ASPECT_DIGITS_DIR.is_dir():
        log.error("[!] aspect digits dir missing: %s", ASPECT_DIGITS_DIR)
        log.error("    Run scripts/extract_labeled_glyphs_aspect.py --all first.")
        return 1

    digit_counts = stage_digit_samples(rng)

    t0 = time.time()
    X, y, counts = load_dataset()
    log.info("[load] X shape=%s y shape=%s", X.shape, y.shape)

    if len(X) == 0:
        log.error("[!] No data loaded.")
        return 1

    best_val, per_class_acc, confusion, val_truth = train_and_export(
        X, y, counts,
    )
    dt = time.time() - t0

    val_samples_per_class = {
        ch: int(np.sum(val_truth == i))
        for i, ch in enumerate(CHAR_CLASSES)
    }

    meta = {
        "kind": "signal_rgb_aspect",
        "version": "aspect_40x28",
        "charClasses": CHAR_CLASSES,
        "numClasses": len(CHAR_CLASSES),
        "inputShape": [1, 3, OUT_HEIGHT, OUT_WIDTH],
        "valAccuracy": best_val,
        "trainSamples": int(len(y) - sum(val_samples_per_class.values())),
        "valSamples": int(sum(val_samples_per_class.values())),
        "perClassCounts": {ch: int(counts.get(ch, 0)) for ch in CHAR_CLASSES},
        "perClassValAccuracy": per_class_acc,
        "perClassValSamples": val_samples_per_class,
        "perClassDigitSources": digit_counts,
        "trainedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "trainingSeconds": dt,
        "modelPath": str(OUT_ONNX),
        "digitStagingDir": str(DIGIT_STAGING_DIR),
        "notes": (
            "Aspect-matched 40x28 (HxW) experiment. SC HUD digits have "
            "a natural ~0.6 wide-to-tall aspect; v3's 28x28 square canvas "
            "distorts the glyph during the resize step. This model uses "
            "a 40-tall x 28-wide canvas so glyphs need less shape "
            "distortion. Architecture is a 3-conv + FC RGB CNN matching "
            "v3 in spirit (only the FC bottleneck dimension changes from "
            "64*7*7 to 64*10*7 to accommodate the taller feature map). "
            "Training corpus is signature-region2 only (no @ class, no "
            "panel-font samples) — apples-to-apples with the digits-only "
            "subset of v3."
        ),
    }
    OUT_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("[meta] wrote %s", OUT_META)

    log.info(
        "[done] best_val=%.2f%%  total_seconds=%.1f",
        best_val * 100, dt,
    )
    return 0 if best_val >= 0.92 else 2


if __name__ == "__main__":
    sys.exit(main())
