"""Trainer for the v2 RGB signal CNN — adds the @ icon class as the
11th output so the icon_voter can use it as the secondary tier between
geometry+contour and the grayscale CNN.

Inputs:
  * Digits 0-9: training_data_user_sig_rgb/<digit>/*.png  (200 each)
  * @ class: combination of
      - 5 real reviewed icons from training_data_pending_review_signal/
        icon/pending_*_rgb.png (excluding 155503_607 which is a
        mislabeled digit crop) — augmented ~30x each => ~150 samples.
      - ~300 colorized warm-tinted samples derived from the 600
        grayscale aug_bad_crop_*.png in training_data_user_sig/icon/
        (HSV hue ~30° + jitter, saturation/value mid-high).

Output: PRODUCTION TREE
  C:\\Users\\_user\\AppData\\Local\\SC_Toolbox\\current\\tools\\
    Mining_Signals\\ocr\\models\\model_signal_rgb_cnn_v2.onnx

Architecture: matches v1 — 3 conv blocks + FC, takes (N, 3, 28, 28),
outputs (N, 11).

Run:
    python ocr/train_signal_rgb_v2.py
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



# ─── Paths ──────────────────────────────────────────────────────────────

THIS_FILE = Path(__file__).resolve()
SOURCE_TOOL_DIR = THIS_FILE.parent.parent   # ...\Mining_Signals\
PROD_TOOL_DIR = Path(
    r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals"
)

# Digit data (real RGB digit captures, 200 each, 0-9).
DIGITS_RGB_DIR = SOURCE_TOOL_DIR / "training_data_user_sig_rgb"

# Real labeled RGB icons for review (5 usable + 1 excluded).
REAL_ICON_DIR = SOURCE_TOOL_DIR / "training_data_pending_review_signal" / "icon"
EXCLUDED_REAL_ICONS = {"pending_cap_20260418_155503_607_rgb.png"}  # mislabeled

# Grayscale synthetic icons (600 of them) — colorize to RGB at training.
GRAY_SYNTH_ICON_DIR = SOURCE_TOOL_DIR / "training_data_user_sig" / "icon"
GRAY_SYNTH_PREFIX = "aug_bad_crop_"

# Staging for combined real-aug + colorized-synthetic icon RGB samples.
ICON_STAGING_DIR = SOURCE_TOOL_DIR / "_v2_icon_staging_rgb"

# Output paths in the PRODUCTION tree (where the voter looks).
PROD_MODELS_DIR = PROD_TOOL_DIR / "ocr" / "models"
OUT_ONNX = PROD_MODELS_DIR / "model_signal_rgb_cnn_v2.onnx"
OUT_META = PROD_MODELS_DIR / "model_signal_rgb_cnn_v2.json"
OUT_LOG  = PROD_MODELS_DIR / "model_signal_rgb_cnn_v2_train.log"


# ─── Config ─────────────────────────────────────────────────────────────

CHAR_CLASSES = "0123456789@"  # 11 classes

# Targets
N_REAL_AUGS_PER_REAL = 30         # 5 reals * 30 = 150 real-derived samples
N_COLORIZED_SYNTH    = 300        # subsample from 600 grayscale

EPOCHS = 60
LR = 1e-3
VAL_SPLIT = 0.15
SEED = 1337

# Per-channel input size
IMG_SIZE = 28


# ─── Logging setup ──────────────────────────────────────────────────────

PROD_MODELS_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("train_signal_rgb_v2")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(OUT_LOG, mode="w", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(message)s"))
log.handlers.clear()
log.addHandler(_fh)
log.addHandler(_sh)


# ─── Augmentation utilities ─────────────────────────────────────────────

def _affine_jitter(
    img: Image.Image,
    *,
    max_rot_deg: float,
    max_trans_frac: float,
    min_scale: float,
    max_scale: float,
    rng: random.Random,
) -> Image.Image:
    """Rotate + translate + scale jitter (CPU, PIL only)."""
    angle = rng.uniform(-max_rot_deg, max_rot_deg)
    sx = rng.uniform(min_scale, max_scale)
    sy = rng.uniform(min_scale, max_scale)
    tx = rng.uniform(-max_trans_frac, max_trans_frac) * img.size[0]
    ty = rng.uniform(-max_trans_frac, max_trans_frac) * img.size[1]
    # Rotation first (around center), then resize, then paste with offset.
    out = img.rotate(angle, resample=Image.BILINEAR, fillcolor=(0, 0, 0))
    nw = max(1, int(round(out.size[0] * sx)))
    nh = max(1, int(round(out.size[1] * sy)))
    out = out.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", img.size, (0, 0, 0))
    cx = (img.size[0] - nw) // 2 + int(round(tx))
    cy = (img.size[1] - nh) // 2 + int(round(ty))
    canvas.paste(out, (cx, cy))
    return canvas


def _photo_jitter(
    img: Image.Image, *, rng: random.Random,
) -> Image.Image:
    """Brightness + contrast + slight saturation jitter."""
    b = rng.uniform(0.75, 1.25)
    c = rng.uniform(0.75, 1.25)
    s = rng.uniform(0.85, 1.15)
    img = ImageEnhance.Brightness(img).enhance(b)
    img = ImageEnhance.Contrast(img).enhance(c)
    img = ImageEnhance.Color(img).enhance(s)
    return img


def augment_real_icon(
    src_img: Image.Image, *, n: int, rng: random.Random,
) -> List[Image.Image]:
    """Aggressively jitter a real RGB icon ~n times. The icon's
    in-game rendering is fairly stable in pose, so jitter is moderate
    rotation + small translation + scale + photometric."""
    src = src_img.convert("RGB").resize((IMG_SIZE, IMG_SIZE), Image.BILINEAR)
    out: List[Image.Image] = [src]  # always include unmodified
    for _ in range(n - 1):
        a = _affine_jitter(
            src,
            max_rot_deg=12.0,
            max_trans_frac=0.10,
            min_scale=0.85,
            max_scale=1.10,
            rng=rng,
        )
        a = _photo_jitter(a, rng=rng)
        out.append(a)
    return out


def colorize_warm(
    gray_img: Image.Image, *, rng: random.Random,
) -> Image.Image:
    """Convert a single-channel grayscale icon to a warm-tinted RGB
    sample. Hue ~30° (warm yellow/orange — the in-game pin color),
    with hue jitter ±10° and saturation jitter ±20%.

    Strategy: grayscale value → V channel, fixed warm H, jittered S,
    then HSV→RGB. This gives us a plausible warm pin on a near-black
    bg (preserving the dark→bright luma of the original gray sample)."""
    g = gray_img.convert("L").resize((IMG_SIZE, IMG_SIZE), Image.BILINEAR)
    v_arr = np.asarray(g, dtype=np.float32) / 255.0

    base_hue_deg = 30.0 + rng.uniform(-10.0, 10.0)   # 20–40°
    base_sat = 0.65 + rng.uniform(-0.20, 0.20)       # 0.45–0.85
    base_sat = float(np.clip(base_sat, 0.0, 1.0))

    h = (base_hue_deg / 360.0) * np.ones_like(v_arr, dtype=np.float32)
    # Saturation should fade out where the gray value is near 0 (so dark
    # background pixels stay near-black, not heavily-tinted dark color).
    s = base_sat * v_arr  # darker pixels => less saturated
    v = v_arr

    # Manual HSV->RGB (vectorized)
    hi = np.floor(h * 6.0).astype(np.int32) % 6
    f = h * 6.0 - np.floor(h * 6.0)
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)

    r = np.where(hi == 0, v, np.where(hi == 1, q, np.where(hi == 2, p,
        np.where(hi == 3, p, np.where(hi == 4, t, v)))))
    gch = np.where(hi == 0, t, np.where(hi == 1, v, np.where(hi == 2, v,
        np.where(hi == 3, q, np.where(hi == 4, p, p)))))
    bch = np.where(hi == 0, p, np.where(hi == 1, p, np.where(hi == 2, t,
        np.where(hi == 3, v, np.where(hi == 4, v, q)))))

    rgb = np.stack([r, gch, bch], axis=-1)
    rgb = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(rgb, mode="RGB")


# ─── Stage 1: build the @ class ─────────────────────────────────────────

def stage_icon_samples(rng: random.Random) -> Tuple[int, int]:
    """Materialize the @ class into ICON_STAGING_DIR.

    Returns (n_real_derived, n_colorized_synth)."""
    if ICON_STAGING_DIR.exists():
        log.info("[stage] wiping %s", ICON_STAGING_DIR)
        shutil.rmtree(ICON_STAGING_DIR)
    ICON_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    # ── Real icons (augmented) ──
    real_files = sorted(REAL_ICON_DIR.glob("pending_*_rgb.png"))
    real_files = [f for f in real_files if f.name not in EXCLUDED_REAL_ICONS]
    log.info(
        "[stage] real RGB icons found: %d (after excluding %s)",
        len(real_files), sorted(EXCLUDED_REAL_ICONS),
    )

    n_real_derived = 0
    for src in real_files:
        try:
            base = Image.open(src).convert("RGB")
        except Exception as exc:
            log.warning("[stage] skip real %s: %s", src.name, exc)
            continue
        augs = augment_real_icon(base, n=N_REAL_AUGS_PER_REAL, rng=rng)
        for i, im in enumerate(augs):
            out_name = f"real_{src.stem}_aug{i:03d}.png"
            im.save(ICON_STAGING_DIR / out_name, format="PNG")
            n_real_derived += 1

    # ── Colorized synthetic icons ──
    gray_files = sorted(GRAY_SYNTH_ICON_DIR.glob(f"{GRAY_SYNTH_PREFIX}*.png"))
    log.info(
        "[stage] grayscale synthetic icons available: %d", len(gray_files)
    )
    if len(gray_files) > N_COLORIZED_SYNTH:
        gray_files = rng.sample(gray_files, N_COLORIZED_SYNTH)

    n_colorized = 0
    for src in gray_files:
        try:
            gray = Image.open(src).convert("L")
        except Exception as exc:
            log.warning("[stage] skip gray %s: %s", src.name, exc)
            continue
        rgb = colorize_warm(gray, rng=rng)
        out_name = f"colorized_{src.stem}.png"
        rgb.save(ICON_STAGING_DIR / out_name, format="PNG")
        n_colorized += 1

    log.info(
        "[stage] icon staging: %d real-derived + %d colorized = %d total",
        n_real_derived, n_colorized, n_real_derived + n_colorized,
    )
    return n_real_derived, n_colorized


# ─── Stage 2: load full dataset (digits + staged icons) ─────────────────

def load_dataset() -> Tuple[np.ndarray, np.ndarray, dict]:
    """Load all class data as (N, 3, 28, 28) float32 in [0, 1].

    Returns (X, y, per_class_counts)."""
    images: List[np.ndarray] = []
    labels: List[int] = []
    counts: dict = {}

    for cls_idx, ch in enumerate(CHAR_CLASSES):
        if ch == "@":
            cls_dir = ICON_STAGING_DIR
        else:
            cls_dir = DIGITS_RGB_DIR / ch
        if not cls_dir.is_dir():
            counts[ch] = 0
            log.warning("[load] missing dir for class %r: %s", ch, cls_dir)
            continue
        n = 0
        for png in _quarantine_filter(cls_dir.glob("*.png")):
            try:
                arr = np.asarray(
                    Image.open(png).convert("RGB").resize(
                        (IMG_SIZE, IMG_SIZE), Image.BILINEAR
                    ),
                    dtype=np.float32,
                ) / 255.0
            except Exception as exc:
                log.warning("[load] skip %s: %s", png.name, exc)
                continue
            # HWC -> CHW
            images.append(arr.transpose(2, 0, 1))
            labels.append(cls_idx)
            n += 1
        counts[ch] = n
        log.info("[load] class %r: %d samples from %s", ch, n, cls_dir.name)

    X = np.stack(images, axis=0).astype(np.float32)
    y = np.asarray(labels, dtype=np.int64)
    return X, y, counts


# ─── Stage 3: model + training (matches v1 architecture) ────────────────

def build_model(num_classes: int):
    """Architecture matching v1 model_signal_rgb_cnn.onnx — 3 conv
    blocks (conv-relu-pool, conv-relu-pool, conv-relu) + FC head."""
    import torch.nn as nn

    class SignalRGBCNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(3, 32, 3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),                  # 14
                nn.Conv2d(32, 64, 3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),                  # 7
                nn.Conv2d(64, 64, 3, padding=1),
                nn.ReLU(),
            )
            self.classifier = nn.Sequential(
                nn.Flatten(),
                nn.Linear(64 * 7 * 7, 128),
                nn.ReLU(),
                nn.Dropout(0.3),
                nn.Linear(128, num_classes),
            )

        def forward(self, x):
            return self.classifier(self.features(x))

    return SignalRGBCNN()


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

    # Inverse-frequency class weighting (capped at 5x median)
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
    scheduler = optim.lr_scheduler.StepLR(
        optimizer, step_size=15, gamma=0.5,
    )

    train_loader = DataLoader(
        TensorDataset(X_tr, y_tr), batch_size=64, shuffle=True,
    )
    val_loader = DataLoader(TensorDataset(X_va, y_va), batch_size=128)

    log.info(
        "[train] epochs=%d train=%d val=%d", EPOCHS, len(tr_idx), len(va_idx),
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
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    if best_state is None:
        raise RuntimeError("No epoch improved val accuracy — refusing to save.")
    model.load_state_dict(best_state)
    model.eval().to(device)

    # ── Per-class val accuracy + confusion matrix on the val set ──
    K = len(CHAR_CLASSES)
    confusion = np.zeros((K, K), dtype=np.int64)
    per_class_correct = np.zeros(K, dtype=np.int64)
    per_class_total   = np.zeros(K, dtype=np.int64)
    val_preds: List[int] = []
    val_truth: List[int] = []
    with torch.no_grad():
        logits = model(X_va)
        pred = logits.argmax(1).cpu().numpy()
        truth = y_va.cpu().numpy()
    for p, t in zip(pred, truth):
        confusion[int(t), int(p)] += 1
        per_class_total[int(t)] += 1
        if p == t:
            per_class_correct[int(t)] += 1
        val_preds.append(int(p))
        val_truth.append(int(t))

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

    # ── Export to ONNX ──
    PROD_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dummy = torch.randn(1, 3, IMG_SIZE, IMG_SIZE, device=device)
    torch.onnx.export(
        model, dummy, str(OUT_ONNX),
        input_names=["input"], output_names=["logits"],
        dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=13,
    )
    log.info("[train] wrote ONNX: %s", OUT_ONNX)

    return float(best_val), per_class_acc, confusion, np.asarray(val_truth)


# ─── Driver ─────────────────────────────────────────────────────────────

def main() -> int:
    rng = random.Random(SEED)

    log.info("=== train_signal_rgb_v2 ===")
    log.info("source tool dir: %s", SOURCE_TOOL_DIR)
    log.info("prod tool dir:   %s", PROD_TOOL_DIR)
    log.info("digits source:   %s", DIGITS_RGB_DIR)
    log.info("real icon src:   %s", REAL_ICON_DIR)
    log.info("synth icon src:  %s", GRAY_SYNTH_ICON_DIR)
    log.info("icon staging:    %s", ICON_STAGING_DIR)
    log.info("output ONNX:     %s", OUT_ONNX)
    log.info("output META:     %s", OUT_META)
    log.info("output LOG:      %s", OUT_LOG)

    n_real_derived, n_colorized = stage_icon_samples(rng)

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
        "kind": "signal_rgb",
        "version": "v2",
        "charClasses": CHAR_CLASSES,
        "numClasses": len(CHAR_CLASSES),
        "inputShape": [1, 3, IMG_SIZE, IMG_SIZE],
        "valAccuracy": best_val,
        "trainSamples": int(len(y) - sum(val_samples_per_class.values())),
        "valSamples": int(sum(val_samples_per_class.values())),
        "perClassCounts": {ch: int(counts.get(ch, 0)) for ch in CHAR_CLASSES},
        "perClassValAccuracy": per_class_acc,
        "perClassValSamples": val_samples_per_class,
        "trainedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "trainingSeconds": dt,
        "modelPath": str(OUT_ONNX),
        "stagingDir": str(DIGITS_RGB_DIR),
        "iconStagingDir": str(ICON_STAGING_DIR),
        "iconSampleSources": {
            "realDerivedAugs": int(n_real_derived),
            "colorizedSynthetic": int(n_colorized),
            "excludedReals": sorted(EXCLUDED_REAL_ICONS),
        },
        "notes": (
            "v2 adds the @ icon class as the 11th output to give the "
            "icon_voter a color-aware secondary tier between geometry+"
            "contour and the grayscale CNN. The @ class is built from "
            "(a) ~5 real labeled RGB icons aggressively augmented "
            "(rotation, scale, translation, brightness/contrast/sat "
            "jitter) ~30x each into ~150 samples, plus (b) ~300 "
            "warm-tinted (HSV hue ~30° ±10°, saturation ~0.65 ±0.20) "
            "colorized samples derived from the existing 600 grayscale "
            "aug_bad_crop_*.png synthetic icons in training_data_user_sig"
            "/icon/. The colorized-synthetic majority is a BRIDGE "
            "MEASURE; the real fix is to collect more labeled region2 "
            "icon captures (training_data_pending_review_signal/icon/) "
            "and re-train. With only 5 real source icons, the model's "
            "@ class is largely a colorized-grayscale-shape detector; "
            "if the colorization distribution drifts from real captures "
            "the model is likely brittle. Use confusion matrix + live-"
            "capture validation to monitor."
        ),
    }
    OUT_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("[meta] wrote %s", OUT_META)

    log.info(
        "[done] best_val=%.2f%%  total_seconds=%.1f",
        best_val * 100, dt,
    )
    return 0 if best_val >= 0.85 else 2


if __name__ == "__main__":
    sys.exit(main())
