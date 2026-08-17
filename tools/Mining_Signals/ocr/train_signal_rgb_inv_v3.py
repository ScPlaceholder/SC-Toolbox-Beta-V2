"""Trainer for the v3 INVERTED-polarity RGB signal CNN — mirror of
``train_signal_rgb_v3.py`` but with each loaded sample's pixel values
flipped per-channel (``inverted = 255 - sample``) before training.

Why: the 4-way voter consensus for signature digits combines four
classifiers (gray + gray_inv + rgb + rgb_inv). The v3 retrain extended
the RGB CNN's training corpus to include panel-font digits in addition
to the original signature-font digits, fixing catastrophic
misclassification of panel "0" as "7". Its decorrelated peer
``model_signal_rgb_inv_cnn.onnx`` was NOT retrained — it still only
knew the signature font, so it now votes WRONG on panel-font crops
while the v3 RGB votes correctly. This script produces the matching
v3 inverse model so coverage is symmetric across the polarity pair.

Inputs (identical staging to ``train_signal_rgb_v3.py``):
  * Digits 0-9: union of
      - training_data_user_sig_rgb/<digit>/*.png  (signature font, RGB)
      - training_data_user_panel/<digit>/*.png    (panel font, grayscale -> RGB)
  * @ class: 5 real reviewed icons * 30 augmentations + 300 colorized
    synthetic icons.

Polarity inversion:
  Each loaded uint8 sample is converted to ``255 - sample`` per
  channel BEFORE the standard ``/255`` normalization. This matches
  the original ``model_signal_rgb_inv_cnn.onnx`` training convention
  (the trainer flipped the source distribution so the resulting
  model expects bright-on-dark inputs at inference time).

Output: PRODUCTION TREE
  C:\\Users\\_user\\AppData\\Local\\SC_Toolbox\\current\\tools\\
    Mining_Signals\\ocr\\models\\model_signal_rgb_inv_cnn_v3.onnx
  C:\\Users\\_user\\AppData\\Local\\SC_Toolbox\\current\\tools\\
    Mining_Signals\\ocr\\models\\model_signal_rgb_inv_cnn_v3.json

Architecture: SAME as v3 RGB — 3 conv blocks + FC, takes (N, 3, 28, 28),
outputs (N, 11). Only the per-sample polarity inversion differs.

Augmentations: same as v3 RGB — moderate rotation, scale, translation,
photometric jitter applied during staging (BEFORE inversion). Class-
weighted cross-entropy for any imbalance.

Run:
    python ocr/train_signal_rgb_inv_v3.py
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

# Training data lives in the WingmanAI tree (where the auto-annotator
# writes user-labeled samples). The v3 RGB trainer used the same source.
WINGMAN_TOOL_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)

# Digit data sources
SIG_RGB_DIGITS_DIR = WINGMAN_TOOL_DIR / "training_data_user_sig_rgb"  # signature font, RGB
PANEL_DIGITS_DIR   = WINGMAN_TOOL_DIR / "training_data_user_panel"   # panel font, grayscale L

# @ class sources (identical to v3 RGB)
REAL_ICON_DIR = WINGMAN_TOOL_DIR / "training_data_pending_review_signal" / "icon"
EXCLUDED_REAL_ICONS = {"pending_cap_20260418_155503_607_rgb.png"}  # mislabeled
GRAY_SYNTH_ICON_DIR = WINGMAN_TOOL_DIR / "training_data_user_sig" / "icon"
GRAY_SYNTH_PREFIX = "aug_bad_crop_"

# Staging dirs — REUSE the same dirs the v3 RGB trainer wrote so we
# don't re-stage. The inversion happens at load time, NOT in the
# staged PNGs (which remain dark-on-light). If the staging dirs
# don't exist or are empty, we re-run staging.
DIGIT_STAGING_DIR = PROD_TOOL_DIR / "_v3_digit_staging_rgb"
ICON_STAGING_DIR  = PROD_TOOL_DIR / "_v3_icon_staging_rgb"

# Output paths in the PRODUCTION tree
PROD_MODELS_DIR = PROD_TOOL_DIR / "ocr" / "models"
OUT_ONNX = PROD_MODELS_DIR / "model_signal_rgb_inv_cnn_v3.onnx"
OUT_META = PROD_MODELS_DIR / "model_signal_rgb_inv_cnn_v3.json"
OUT_LOG  = PROD_MODELS_DIR / "model_signal_rgb_inv_cnn_v3_train.log"


# --- Config ----------------------------------------------------------

CHAR_CLASSES = "0123456789@"  # 11 classes (same as v3 RGB)

# @ class targets (same as v3 RGB)
N_REAL_AUGS_PER_REAL = 30      # 5 reals * 30 = 150 real-derived samples
N_COLORIZED_SYNTH    = 300     # subsample from 600 grayscale

DIGIT_TARGET_MIN = 250
DIGIT_TARGET_MAX = 400

EPOCHS = 60
LR = 1e-3
VAL_SPLIT = 0.15
SEED = 1337

IMG_SIZE = 28


# --- Logging --------------------------------------------------------

PROD_MODELS_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("train_signal_rgb_inv_v3")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(OUT_LOG, mode="w", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(message)s"))
log.handlers.clear()
log.addHandler(_fh)
log.addHandler(_sh)


# --- Augmentation utilities (identical to v3 RGB) -------------------

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


def augment_real_icon(
    src_img: Image.Image, *, n: int, rng: random.Random,
) -> List[Image.Image]:
    src = src_img.convert("RGB").resize((IMG_SIZE, IMG_SIZE), Image.BILINEAR)
    out: List[Image.Image] = [src]
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


def colorize_warm(gray_img: Image.Image, *, rng: random.Random) -> Image.Image:
    g = gray_img.convert("L").resize((IMG_SIZE, IMG_SIZE), Image.BILINEAR)
    v_arr = np.asarray(g, dtype=np.float32) / 255.0
    base_hue_deg = 30.0 + rng.uniform(-10.0, 10.0)
    base_sat = 0.65 + rng.uniform(-0.20, 0.20)
    base_sat = float(np.clip(base_sat, 0.0, 1.0))
    h = (base_hue_deg / 360.0) * np.ones_like(v_arr, dtype=np.float32)
    s = base_sat * v_arr
    v = v_arr
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


# --- Stage 1: build the @ class (identical to v3 RGB) ---------------

def stage_icon_samples(rng: random.Random) -> Tuple[int, int]:
    if ICON_STAGING_DIR.exists():
        log.info("[stage] wiping %s", ICON_STAGING_DIR)
        shutil.rmtree(ICON_STAGING_DIR)
    ICON_STAGING_DIR.mkdir(parents=True, exist_ok=True)

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

    gray_files = sorted(GRAY_SYNTH_ICON_DIR.glob(f"{GRAY_SYNTH_PREFIX}*.png"))
    log.info("[stage] grayscale synthetic icons available: %d", len(gray_files))
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


# --- Stage 1b: stage combined digit samples (identical to v3 RGB) ---

def _load_rgb_28(path: Path) -> Image.Image:
    """Load any image as 28x28 RGB (panel L gets replicated to 3 channels)."""
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    if im.size != (IMG_SIZE, IMG_SIZE):
        im = im.resize((IMG_SIZE, IMG_SIZE), Image.BILINEAR)
    return im


def stage_digit_samples(rng: random.Random) -> dict:
    """Materialize per-digit RGB samples into DIGIT_STAGING_DIR/<digit>/.

    Identical staging strategy to ``train_signal_rgb_v3.py``. The staged
    PNGs remain in dark-on-light polarity; inversion to bright-on-dark
    happens at LOAD time in :func:`load_dataset`, not here.
    """
    if DIGIT_STAGING_DIR.exists():
        log.info("[stage-digits] wiping %s", DIGIT_STAGING_DIR)
        shutil.rmtree(DIGIT_STAGING_DIR)
    DIGIT_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    counts = {}
    for ch in "0123456789":
        out_dir = DIGIT_STAGING_DIR / ch
        out_dir.mkdir(parents=True, exist_ok=True)
        sig_dir = SIG_RGB_DIGITS_DIR / ch
        pan_dir = PANEL_DIGITS_DIR / ch

        sig_files = _quarantine_filter(sorted(sig_dir.glob("*.png"))) if sig_dir.is_dir() else []
        pan_files = _quarantine_filter(sorted(pan_dir.glob("*.png"))) if pan_dir.is_dir() else []
        n_sig_src = len(sig_files)
        n_pan_src = len(pan_files)

        sig_imgs: List[Image.Image] = []
        pan_imgs: List[Image.Image] = []
        for f in sig_files:
            try:
                im = _load_rgb_28(f)
            except Exception as exc:
                log.warning("[stage-digits] skip %s: %s", f.name, exc)
                continue
            sig_imgs.append(im)
        for f in pan_files:
            try:
                im = _load_rgb_28(f)
            except Exception as exc:
                log.warning("[stage-digits] skip %s: %s", f.name, exc)
                continue
            pan_imgs.append(im)

        if pan_imgs and len(pan_imgs) < len(sig_imgs):
            need = len(sig_imgs) - len(pan_imgs)
            log.info(
                "[stage-digits] class %s: upsampling panel %d -> %d via aug",
                ch, len(pan_imgs), len(pan_imgs) + need,
            )
            base = list(pan_imgs)
            i = 0
            while i < need:
                src = base[i % len(base)]
                aug = _digit_aug(src, rng=rng)
                pan_imgs.append(aug)
                i += 1

        combined = list(sig_imgs) + list(pan_imgs)
        if len(combined) > DIGIT_TARGET_MAX:
            keep_pan = min(len(pan_imgs), DIGIT_TARGET_MAX // 2)
            keep_sig = DIGIT_TARGET_MAX - keep_pan
            sig_keep = rng.sample(sig_imgs, min(keep_sig, len(sig_imgs)))
            pan_keep = rng.sample(pan_imgs, min(keep_pan, len(pan_imgs)))
            combined = sig_keep + pan_keep
            log.info(
                "[stage-digits] class %s: capped to %d (sig=%d pan=%d)",
                ch, len(combined), len(sig_keep), len(pan_keep),
            )

        if len(combined) < DIGIT_TARGET_MIN and pan_imgs:
            need = DIGIT_TARGET_MIN - len(combined)
            log.info(
                "[stage-digits] class %s: bottom-up to %d (adding %d panel augs)",
                ch, DIGIT_TARGET_MIN, need,
            )
            base = list(pan_imgs)
            i = 0
            while i < need:
                src = base[i % len(base)]
                aug = _digit_aug(src, rng=rng)
                combined.append(aug)
                i += 1

        rng.shuffle(combined)
        for i, im in enumerate(combined):
            im.save(DIGIT_STAGING_DIR / ch / f"{ch}_{i:04d}.png", format="PNG")
        counts[ch] = {
            "sig_src": n_sig_src,
            "pan_src": n_pan_src,
            "total_after_balance": len(combined),
        }
        log.info(
            "[stage-digits] class %s: sig_src=%d pan_src=%d total=%d",
            ch, n_sig_src, n_pan_src, len(combined),
        )
    return counts


def _staging_is_complete() -> bool:
    """True iff both staging dirs are populated for the v3 schema.

    The v3 RGB trainer left these dirs intact after its run. If a
    fresh run already produced them, we can skip staging here and
    just re-load + invert. Otherwise we re-stage from scratch.
    """
    if not DIGIT_STAGING_DIR.is_dir():
        return False
    if not ICON_STAGING_DIR.is_dir():
        return False
    if not any(ICON_STAGING_DIR.glob("*.png")):
        return False
    for ch in "0123456789":
        d = DIGIT_STAGING_DIR / ch
        if not d.is_dir() or not any(d.glob("*.png")):
            return False
    return True


# --- Stage 2: load full dataset WITH per-channel inversion -----------

def load_dataset() -> Tuple[np.ndarray, np.ndarray, dict]:
    """Same as v3 RGB's ``load_dataset`` but applies ``inverted = 255 - sample``
    per channel before normalizing to [0, 1]. Equivalent to ``1.0 - x``
    after normalize."""
    images: List[np.ndarray] = []
    labels: List[int] = []
    counts: dict = {}

    for cls_idx, ch in enumerate(CHAR_CLASSES):
        if ch == "@":
            cls_dir = ICON_STAGING_DIR
        else:
            cls_dir = DIGIT_STAGING_DIR / ch
        if not cls_dir.is_dir():
            counts[ch] = 0
            log.warning("[load] missing dir for class %r: %s", ch, cls_dir)
            continue
        n = 0
        for png in _quarantine_filter(cls_dir.glob("*.png")):
            try:
                # Load uint8, invert per channel, then normalize.
                u8 = np.asarray(
                    Image.open(png).convert("RGB").resize(
                        (IMG_SIZE, IMG_SIZE), Image.BILINEAR
                    ),
                    dtype=np.uint8,
                )
                # POLARITY INVERSION: 255 - sample, per channel.
                inv = (255 - u8).astype(np.float32) / 255.0
            except Exception as exc:
                log.warning("[load] skip %s: %s", png.name, exc)
                continue
            images.append(inv.transpose(2, 0, 1))
            labels.append(cls_idx)
            n += 1
        counts[ch] = n
        log.info("[load] class %r: %d samples (inverted) from %s",
                 ch, n, cls_dir.name)

    X = np.stack(images, axis=0).astype(np.float32)
    y = np.asarray(labels, dtype=np.int64)
    return X, y, counts


# --- Stage 3: model + training (identical architecture) -------------

def build_model(num_classes: int):
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
    dummy = torch.randn(1, 3, IMG_SIZE, IMG_SIZE, device=device)
    torch.onnx.export(
        model, dummy, str(OUT_ONNX),
        input_names=["input"], output_names=["logits"],
        dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=13,
    )
    log.info("[train] wrote ONNX: %s", OUT_ONNX)

    return float(best_val), per_class_acc, confusion, np.asarray(val_truth_list)


# --- Driver --------------------------------------------------------

def main() -> int:
    rng = random.Random(SEED)

    log.info("=== train_signal_rgb_inv_v3 ===")
    log.info("prod tool dir:   %s", PROD_TOOL_DIR)
    log.info("wingman tool dir:%s", WINGMAN_TOOL_DIR)
    log.info("sig digits src:  %s", SIG_RGB_DIGITS_DIR)
    log.info("panel digits src:%s", PANEL_DIGITS_DIR)
    log.info("real icon src:   %s", REAL_ICON_DIR)
    log.info("synth icon src:  %s", GRAY_SYNTH_ICON_DIR)
    log.info("digit staging:   %s", DIGIT_STAGING_DIR)
    log.info("icon  staging:   %s", ICON_STAGING_DIR)
    log.info("output ONNX:     %s", OUT_ONNX)
    log.info("output META:     %s", OUT_META)
    log.info("output LOG:      %s", OUT_LOG)
    log.info("polarity:        per-channel inversion (255 - sample) at load")

    # If the v3 RGB trainer's staging dirs are intact, reuse them
    # verbatim — staging is polarity-agnostic, the inversion happens
    # at load time. Otherwise re-stage.
    if _staging_is_complete():
        log.info(
            "[stage] reusing existing v3 staging dirs (digit + icon "
            "PNGs already on disk from train_signal_rgb_v3.py)"
        )
        n_real_derived = 0
        n_colorized = 0
        # Recover counts by scanning. Same shape as the v3 RGB script's
        # ``stage_icon_samples`` return for metadata fidelity.
        for p in ICON_STAGING_DIR.glob("real_*.png"):
            n_real_derived += 1
        for p in ICON_STAGING_DIR.glob("colorized_*.png"):
            n_colorized += 1
        log.info(
            "[stage] icon dir summary: %d real-derived + %d colorized",
            n_real_derived, n_colorized,
        )
        digit_counts: dict = {}
        for ch in "0123456789":
            sig_dir = SIG_RGB_DIGITS_DIR / ch
            pan_dir = PANEL_DIGITS_DIR / ch
            n_sig_src = len(list(sig_dir.glob("*.png"))) if sig_dir.is_dir() else 0
            n_pan_src = len(list(pan_dir.glob("*.png"))) if pan_dir.is_dir() else 0
            total = len(list((DIGIT_STAGING_DIR / ch).glob("*.png")))
            digit_counts[ch] = {
                "sig_src": n_sig_src,
                "pan_src": n_pan_src,
                "total_after_balance": total,
            }
            log.info(
                "[stage-digits] class %s reused: sig_src=%d pan_src=%d total=%d",
                ch, n_sig_src, n_pan_src, total,
            )
    else:
        log.info("[stage] staging dirs missing/empty — re-staging")
        n_real_derived, n_colorized = stage_icon_samples(rng)
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
        "kind": "signal_rgb_inv",
        "version": "v3",
        "charClasses": CHAR_CLASSES,
        "numClasses": len(CHAR_CLASSES),
        "inputShape": [1, 3, IMG_SIZE, IMG_SIZE],
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
        "iconStagingDir":  str(ICON_STAGING_DIR),
        "iconSampleSources": {
            "realDerivedAugs": int(n_real_derived),
            "colorizedSynthetic": int(n_colorized),
            "excludedReals": sorted(EXCLUDED_REAL_ICONS),
        },
        "polarityConvention": (
            "Trainer applies (255 - sample) per channel before normalize, "
            "matching the original model_signal_rgb_inv_cnn.onnx training "
            "convention. Resulting model expects bright-on-dark inputs at "
            "inference (caller routes polarity via _route_rgb_to_bod / "
            "_feed_signal_cnn)."
        ),
        "notes": (
            "v3_inv mirrors train_signal_rgb_v3.py's training corpus "
            "(signature + panel digit samples + @ icons) but inverts "
            "each loaded sample's pixels per channel (255 - sample). "
            "Architecture identical to v3 RGB (3 conv blocks + FC, "
            "3x28x28 input, 11 outputs). Produced because the RGB v3 "
            "retrain extended the primary RGB CNN to handle BOTH "
            "signature-font AND panel-font digits, but its decorrelated "
            "inverse-polarity peer was still v1 (signature font only) "
            "and was voting WRONG on panel-font crops. This restores "
            "symmetry across the rgb / rgb_inv pair so the 4-way voter "
            "consensus (gray + gray_inv + rgb + rgb_inv) covers both "
            "fonts uniformly."
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
