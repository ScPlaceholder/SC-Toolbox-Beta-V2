"""Trainer for the 28x20 (height x width) aspect-matched RGB digit CNN.

Experimental sibling to ``train_signal_rgb_v3.py``. Identical pipeline
and corpus — only difference is the image dimensions:
  v3:  28 tall x 28 wide (square; stretches narrow SC HUD digits)
  v3-aspect-28x20:  28 tall x 20 wide (matches the natural ~0.6
    aspect of SC's narrow HUD digit font)

Inputs (mirroring v3 so accuracy comparison is apples-to-apples):
  * Digits 0-9: union of
      - training_data_user_sig_rgb/<digit>/*.png  (signature font, RGB 28x28)
      - training_data_user_panel/<digit>/*.png    (panel font, L 28x28)
      - training_data_panels/_aspect_28x20/<digit>/*.png  (NEW
        live-extracted glyphs at native 28x20)
    All sources are resized to 28x20 RGB at load time.
  * @ class: SAME 5 real reviewed icons + 300 colorized synthetic
    icons as v3 (resized 28x28 -> 28x20).

Output (PRODUCTION TREE):
  ocr/models/model_signal_rgb_aspect_28x20.onnx
  ocr/models/model_signal_rgb_aspect_28x20.json

Architecture: 3 conv blocks + FC, takes (N, 3, 28, 20), outputs (N, 11).
Same hyperparameters as v3. Flatten dim adjusted to 64 * 7 * 5 (after
two max-pools the 28x20 input becomes 7x5).

Run:
    python ocr/train_signal_rgb_aspect_28x20.py
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

WINGMAN_TOOL_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)

# Digit data sources
SIG_RGB_DIGITS_DIR = WINGMAN_TOOL_DIR / "training_data_user_sig_rgb"
PANEL_DIGITS_DIR   = WINGMAN_TOOL_DIR / "training_data_user_panel"
LIVE_28x20_DIR     = PROD_TOOL_DIR / "training_data_panels" / "_aspect_28x20"

# @ class sources (identical to v3)
REAL_ICON_DIR = WINGMAN_TOOL_DIR / "training_data_pending_review_signal" / "icon"
EXCLUDED_REAL_ICONS = {"pending_cap_20260418_155503_607_rgb.png"}
GRAY_SYNTH_ICON_DIR = WINGMAN_TOOL_DIR / "training_data_user_sig" / "icon"
GRAY_SYNTH_PREFIX = "aug_bad_crop_"

# Staging dirs (sibling to v3's, namespaced for the experiment)
DIGIT_STAGING_DIR = PROD_TOOL_DIR / "_aspect_28x20_digit_staging_rgb"
ICON_STAGING_DIR  = PROD_TOOL_DIR / "_aspect_28x20_icon_staging_rgb"

# Output paths in the PRODUCTION tree
PROD_MODELS_DIR = PROD_TOOL_DIR / "ocr" / "models"
OUT_ONNX = PROD_MODELS_DIR / "model_signal_rgb_aspect_28x20.onnx"
OUT_META = PROD_MODELS_DIR / "model_signal_rgb_aspect_28x20.json"
OUT_LOG  = PROD_MODELS_DIR / "model_signal_rgb_aspect_28x20_train.log"


# --- Config ----------------------------------------------------------

CHAR_CLASSES = "0123456789@"  # 11 classes (same as v3)

N_REAL_AUGS_PER_REAL = 30
N_COLORIZED_SYNTH    = 300

DIGIT_TARGET_MIN = 250
DIGIT_TARGET_MAX = 400

EPOCHS = 60
LR = 1e-3
VAL_SPLIT = 0.15
SEED = 1337

IMG_H = 28  # rows
IMG_W = 20  # cols
# PIL takes (width, height); express as a tuple for resize calls.
IMG_SIZE_WH = (IMG_W, IMG_H)


# --- Logging --------------------------------------------------------

PROD_MODELS_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("train_signal_rgb_aspect_28x20")
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


def augment_real_icon(
    src_img: Image.Image, *, n: int, rng: random.Random,
) -> List[Image.Image]:
    src = src_img.convert("RGB").resize(IMG_SIZE_WH, Image.BILINEAR)
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
    g = gray_img.convert("L").resize(IMG_SIZE_WH, Image.BILINEAR)
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


# --- Stage 1: build the @ class -------------------------------------

def stage_icon_samples(rng: random.Random) -> Tuple[int, int]:
    if ICON_STAGING_DIR.exists():
        log.info("[stage] wiping %s", ICON_STAGING_DIR)
        shutil.rmtree(ICON_STAGING_DIR)
    ICON_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    real_files = sorted(REAL_ICON_DIR.glob("pending_*_rgb.png"))
    real_files = [f for f in real_files if f.name not in EXCLUDED_REAL_ICONS]
    log.info(
        "[stage] real RGB icons: %d", len(real_files),
    )
    n_real_derived = 0
    for src in real_files:
        try:
            base = Image.open(src).convert("RGB")
        except Exception:
            continue
        augs = augment_real_icon(base, n=N_REAL_AUGS_PER_REAL, rng=rng)
        for i, im in enumerate(augs):
            out_name = f"real_{src.stem}_aug{i:03d}.png"
            im.save(ICON_STAGING_DIR / out_name, format="PNG")
            n_real_derived += 1

    gray_files = sorted(GRAY_SYNTH_ICON_DIR.glob(f"{GRAY_SYNTH_PREFIX}*.png"))
    log.info("[stage] grayscale synth icons: %d", len(gray_files))
    if len(gray_files) > N_COLORIZED_SYNTH:
        gray_files = rng.sample(gray_files, N_COLORIZED_SYNTH)
    n_colorized = 0
    for src in gray_files:
        try:
            gray = Image.open(src).convert("L")
        except Exception:
            continue
        rgb = colorize_warm(gray, rng=rng)
        out_name = f"colorized_{src.stem}.png"
        rgb.save(ICON_STAGING_DIR / out_name, format="PNG")
        n_colorized += 1

    log.info(
        "[stage] icon staging: %d real + %d colorized = %d total",
        n_real_derived, n_colorized, n_real_derived + n_colorized,
    )
    return n_real_derived, n_colorized


# --- Stage 1b: stage combined digit samples --------------------------

def _load_rgb_28x20(path: Path) -> Image.Image:
    """Load any image as 28-tall x 20-wide RGB."""
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")  # L->RGB replicates the single channel
    if im.size != IMG_SIZE_WH:
        im = im.resize(IMG_SIZE_WH, Image.BILINEAR)
    return im


def stage_digit_samples(rng: random.Random) -> dict:
    """Materialize per-digit RGB samples at 28x20 into staging.

    Sources merged: sig_rgb (RGB), panel (L), live_28x20 (RGB native).
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
        live_dir = LIVE_28x20_DIR / ch

        sig_files = _quarantine_filter(sorted(sig_dir.glob("*.png"))) if sig_dir.is_dir() else []
        pan_files = _quarantine_filter(sorted(pan_dir.glob("*.png"))) if pan_dir.is_dir() else []
        live_files = _quarantine_filter(sorted(live_dir.glob("*.png"))) if live_dir.is_dir() else []
        n_sig_src = len(sig_files)
        n_pan_src = len(pan_files)
        n_live_src = len(live_files)

        sig_imgs: List[Image.Image] = []
        pan_imgs: List[Image.Image] = []
        live_imgs: List[Image.Image] = []
        for f in sig_files:
            try:
                sig_imgs.append(_load_rgb_28x20(f))
            except Exception:
                pass
        for f in pan_files:
            try:
                pan_imgs.append(_load_rgb_28x20(f))
            except Exception:
                pass
        for f in live_files:
            try:
                live_imgs.append(_load_rgb_28x20(f))
            except Exception:
                pass

        # Treat live samples as panel-class augmentation (same SC HUD font).
        # They count toward the panel pool when balancing.
        pan_imgs.extend(live_imgs)

        # Balance panel:sig within class. Aim panel >= sig.
        if pan_imgs and len(pan_imgs) < len(sig_imgs):
            need = len(sig_imgs) - len(pan_imgs)
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

        if len(combined) < DIGIT_TARGET_MIN and pan_imgs:
            need = DIGIT_TARGET_MIN - len(combined)
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
            "live_28x20_src": n_live_src,
            "total_after_balance": len(combined),
        }
        log.info(
            "[stage-digits] class %s: sig=%d pan=%d live28x20=%d -> total=%d",
            ch, n_sig_src, n_pan_src, n_live_src, len(combined),
        )
    return counts


# --- Stage 2: load full dataset -------------------------------------

def load_dataset() -> Tuple[np.ndarray, np.ndarray, dict]:
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
            continue
        n = 0
        for png in _quarantine_filter(cls_dir.glob("*.png")):
            try:
                arr = np.asarray(
                    Image.open(png).convert("RGB").resize(
                        IMG_SIZE_WH, Image.BILINEAR
                    ),
                    dtype=np.float32,
                ) / 255.0
            except Exception:
                continue
            # arr shape (H, W, 3) -> CNN wants (3, H, W)
            images.append(arr.transpose(2, 0, 1))
            labels.append(cls_idx)
            n += 1
        counts[ch] = n
        log.info("[load] class %r: %d samples", ch, n)

    X = np.stack(images, axis=0).astype(np.float32)
    y = np.asarray(labels, dtype=np.int64)
    return X, y, counts


# --- Stage 3: model + training --------------------------------------

def build_model(num_classes: int):
    """Same architecture as v3, but Flatten dim shrinks from 64*7*7
    to 64*7*5 because 28x20 -> two MaxPool2d -> 7x5 spatial."""
    import torch.nn as nn

    class SignalRGBCNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(3, 32, 3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),                  # 28x20 -> 14x10
                nn.Conv2d(32, 64, 3, padding=1),
                nn.ReLU(),
                nn.MaxPool2d(2),                  # 14x10 -> 7x5
                nn.Conv2d(64, 64, 3, padding=1),
                nn.ReLU(),
            )
            self.classifier = nn.Sequential(
                nn.Flatten(),
                nn.Linear(64 * 7 * 5, 128),
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
    # Dummy input shape: (N, 3, 28, 20)
    dummy = torch.randn(1, 3, IMG_H, IMG_W, device=device)
    # The torch 2.11 ONNX exporter (dynamo path) prints status emoji
    # to stdout. Windows cp1252 console can't encode them and crashes
    # the process mid-export. Reconfigure stdout/stderr to utf-8 just
    # before the export call so the print statements succeed.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    torch.onnx.export(
        model, dummy, str(OUT_ONNX),
        input_names=["input"], output_names=["logits"],
        dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=13,
    )
    log.info("[train] wrote ONNX: %s", OUT_ONNX)

    return float(best_val), per_class_acc, confusion, np.asarray(val_truth_list)


# --- Stage 4: spot-check vs v3 on known-confused captures ----------

V3_ONNX_PATH = PROD_MODELS_DIR / "model_signal_rgb_cnn_v3.onnx"

# Region 2 captures from the Wingman tree where v3 (28x28) is known
# to misread a specific digit. Each entry: (capture path, label, the
# bbox of the misread digit in the SOURCE image, what 28x28 says,
# what the human label says).
SPOT_CHECK_CAPS = [
    {
        "path": WINGMAN_TOOL_DIR / "training_data_panels"
                / "user_20260418_081525" / "region2"
                / "cap_20260418_085431_378.png",
        "label": "11,520",
        "target_digit": "5",
        "x1": 121,
        "x2": 138,
        "v28x28_misread": "3",
    },
    {
        "path": WINGMAN_TOOL_DIR / "training_data_panels"
                / "user_20260418_081525" / "region2"
                / "cap_20260418_085436_978.png",
        "label": "21,200",
        "target_digit": "0",
        # No glyphs.json sidecar exists for this capture; we'll
        # derive the trailing-0 bbox from the rightmost span found
        # by the production segmenter at run time.
        "x1": None,
        "x2": None,
        "trailing": True,
        "v28x28_misread": "7",
    },
    {
        "path": WINGMAN_TOOL_DIR / "training_data_panels"
                / "user_20260418_081525" / "region2"
                / "cap_20260418_085400_739.png",
        "label": "17,020",
        "target_digit": "0",
        # Per the glyphs.json sidecar this capture has tiles for
        # 1, 7, 0, 2, 0. The "second 0" means index 4 (the trailing
        # 0). x1=157, x2=169 from the sidecar.
        "x1": 157,
        "x2": 169,
        "v28x28_misread": "8",
    },
]


def _otsu_inline(gray: np.ndarray) -> int:
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    sum_total = float(np.sum(np.arange(256) * hist))
    sum_back, w_back, max_var, threshold = 0.0, 0, 0.0, 128
    for t in range(256):
        w_back += int(hist[t])
        if w_back == 0:
            continue
        w_fore = total - w_back
        if w_fore == 0:
            break
        sum_back += t * int(hist[t])
        m_back = sum_back / w_back
        m_fore = (sum_total - sum_back) / w_fore
        var = w_back * w_fore * (m_back - m_fore) ** 2
        if var > max_var:
            max_var = var
            threshold = t
    return threshold


def _resolve_bbox(cap: dict, gray_masked: np.ndarray, value_str: str) -> tuple[int, int] | None:
    """For caps where x1/x2 are pre-known, return as-is.

    For the trailing-digit case (no sidecar), run the production
    segmenter on the masked gray and use the rightmost span."""
    if cap["x1"] is not None and cap["x2"] is not None:
        return int(cap["x1"]), int(cap["x2"])
    sys.path.insert(0, str(PROD_TOOL_DIR))
    from scripts.extract_labeled_glyphs import _segment_digits  # noqa: WPS433
    chars = [c for c in value_str if c.isdigit() or c == "."]
    spans = _segment_digits(gray_masked, expected_count=len(chars))
    if len(spans) != len(chars):
        return None
    return tuple(spans[-1])  # rightmost span = trailing digit


def _crop_glyph_rgb(rgb_iso: np.ndarray, gray_iso: np.ndarray,
                    x1: int, x2: int, out_w: int, out_h: int) -> np.ndarray | None:
    """RGB glyph cropper that mirrors the production gray extractor's
    polarity-canonicalised pipeline.

    Critical detail vs the naive RGB-passthrough cropper: the v3 and
    28x20 training corpora were both extracted with
    ``_glyph_to_28x28``, which pads with WHITE (255) and produces
    bright-bg glyphs regardless of source background. To match that
    distribution at inference time we polarity-canonicalise the
    cropped RGB tile too — if the source crop has a dark background
    (real region2 captures: bg ~30 RGB), invert it so the digit ends
    up dark on bright. This makes the inference-time glyph
    statistically consistent with the training distribution.
    """
    if np.median(gray_iso) > 140:
        work = 255 - gray_iso
    else:
        work = gray_iso
    thr = _otsu_inline(work)
    binary = (work > thr).astype(np.uint8)
    glyph_col = binary[:, x1:x2]
    ys = np.where(np.any(glyph_col > 0, axis=1))[0]
    if len(ys) < 2:
        return None
    ya, yb = int(ys[0]), int(ys[-1]) + 1
    crop_rgb = rgb_iso[ya:yb, x1:x2, :].copy()
    crop_gray = gray_iso[ya:yb, x1:x2]
    # Polarity match: training data had bright background. If source
    # crop has dark median (typical for region2 captures), invert.
    if np.median(crop_gray) < 120:
        crop_rgb = 255 - crop_rgb
    pad = 2
    H, W = crop_rgb.shape[:2]
    padded = np.full((H + 2 * pad, W + 2 * pad, 3), 255, dtype=np.uint8)
    padded[pad:pad + H, pad:pad + W, :] = crop_rgb
    pil = Image.fromarray(padded).resize((out_w, out_h), Image.BILINEAR)
    return np.asarray(pil, dtype=np.uint8)


def _classify(sess, glyph_rgb_uint8: np.ndarray) -> tuple[str, float]:
    rgb = glyph_rgb_uint8.astype(np.float32) / 255.0
    rgb = rgb.transpose(2, 0, 1)[None, ...]
    out = sess.run(None, {sess.get_inputs()[0].name: rgb})[0][0]
    e = np.exp(out - out.max())
    p = e / e.sum()
    idx = int(np.argmax(p))
    return (CHAR_CLASSES[idx], float(p[idx]))


def spot_check() -> dict:
    """Compare 28x28 (v3) vs 28x20 readings on known-confused captures.

    For each capture: load RGB+gray, run icon-mask + row-isolate on
    BOTH (using the same y-bounds derived from gray), resolve the
    target-digit bbox (x1, x2), crop a glyph at 28x28 and at 28x20
    sharing the same color path, classify with each model.
    """
    if not V3_ONNX_PATH.exists():
        log.warning("[spot-check] v3 ONNX not found at %s", V3_ONNX_PATH)
        return {}
    if not OUT_ONNX.exists():
        log.warning("[spot-check] new ONNX not found at %s", OUT_ONNX)
        return {}

    import onnxruntime as ort
    sys.path.insert(0, str(PROD_TOOL_DIR))
    from scripts.extract_labeled_glyphs import (  # noqa: WPS433
        _locate_icon_via_blacklist_match,
        _find_main_row_bounds,
    )

    log.info("[spot-check] === comparing 28x28 (v3) vs 28x20 ===")
    sess_v3 = ort.InferenceSession(
        str(V3_ONNX_PATH), providers=["CPUExecutionProvider"],
    )
    sess_new = ort.InferenceSession(
        str(OUT_ONNX), providers=["CPUExecutionProvider"],
    )

    results: dict = {}
    for cap in SPOT_CHECK_CAPS:
        path = cap["path"]
        if not path.is_file():
            log.warning("[spot-check] capture missing: %s", path)
            continue
        img = Image.open(path)
        rgb_full = np.asarray(img.convert("RGB"), dtype=np.uint8).copy()
        gray_full = np.asarray(img.convert("L"), dtype=np.uint8).copy()
        img_w = gray_full.shape[1]
        bg = int(np.median(gray_full))

        # Icon mask (same logic as production)
        icon_right = _locate_icon_via_blacklist_match(gray_full)
        floor_mask = int(img_w * 0.30)
        mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
        if 0 < mask_w < img_w:
            gray_full[:, :mask_w] = bg
            rgb_full[:, :mask_w, :] = bg

        # Row-band y-bounds
        bounds = _find_main_row_bounds(gray_full)
        if bounds:
            y1b, y2b = bounds
        else:
            y1b, y2b = 0, gray_full.shape[0]
        gray_iso = gray_full[y1b:y2b, :]
        rgb_iso = rgb_full[y1b:y2b, :, :]

        bbox = _resolve_bbox(cap, gray_iso, cap["label"].replace(",", ""))
        if bbox is None:
            log.warning(
                "[spot-check] could not resolve bbox for %s", path.name,
            )
            continue
        x1, x2 = bbox

        # 28x28 RGB crop + classify (v3)
        glyph_28x28 = _crop_glyph_rgb(rgb_iso, gray_iso, x1, x2, 28, 28)
        if glyph_28x28 is None:
            v3_ch, v3_p = ("?", 0.0)
        else:
            v3_ch, v3_p = _classify(sess_v3, glyph_28x28)

        # 28x20 RGB crop + classify (new)
        glyph_28x20 = _crop_glyph_rgb(rgb_iso, gray_iso, x1, x2, IMG_W, IMG_H)
        if glyph_28x20 is None:
            new_ch, new_p = ("?", 0.0)
        else:
            new_ch, new_p = _classify(sess_new, glyph_28x20)

        expected = cap["target_digit"]
        results[path.stem] = {
            "expected": expected,
            "label": cap["label"],
            "v3_28x28_pred": v3_ch,
            "v3_28x28_conf": v3_p,
            "new_28x20_pred": new_ch,
            "new_28x20_conf": new_p,
            "claimed_28x28_misread_as": cap.get("v28x28_misread"),
            "x1": int(x1),
            "x2": int(x2),
        }
        v3_verdict = "right" if v3_ch == expected else "wrong"
        new_verdict = "right" if new_ch == expected else "wrong"
        log.info(
            "  cap=%s label=%s digit=%s  bbox=(%d,%d) "
            "v3(28x28)=%s@%.2f (%s)  new(28x20)=%s@%.2f (%s)",
            path.name, cap["label"], expected, x1, x2,
            v3_ch, v3_p, v3_verdict,
            new_ch, new_p, new_verdict,
        )

    correct_v3 = sum(1 for r in results.values() if r["v3_28x28_pred"] == r["expected"])
    correct_new = sum(1 for r in results.values() if r["new_28x20_pred"] == r["expected"])
    log.info(
        "[spot-check] summary: 28x28 correct %d/%d, 28x20 correct %d/%d",
        correct_v3, len(results), correct_new, len(results),
    )
    return results


# --- Driver ---------------------------------------------------------

def main() -> int:
    rng = random.Random(SEED)

    log.info("=== train_signal_rgb_aspect_28x20 ===")
    log.info("input H x W: %d x %d", IMG_H, IMG_W)
    log.info("sig src:      %s", SIG_RGB_DIGITS_DIR)
    log.info("panel src:    %s", PANEL_DIGITS_DIR)
    log.info("live 28x20:   %s", LIVE_28x20_DIR)
    log.info("output ONNX:  %s", OUT_ONNX)
    log.info("output META:  %s", OUT_META)

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
        "kind": "signal_rgb",
        "version": "aspect_28x20",
        "charClasses": CHAR_CLASSES,
        "numClasses": len(CHAR_CLASSES),
        "inputShape": [1, 3, IMG_H, IMG_W],
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
        "notes": (
            "Aspect-matched experiment: same training corpus and "
            "architecture as v3, but input is 28-tall x 20-wide "
            "(matching SC HUD digits' natural ~0.6 wide:tall aspect) "
            "instead of 28x28. Goal: see if not horizontally stretching "
            "narrow digits during the resize improves classification "
            "of the digits the 28x28 model misreads ('5' as '3', "
            "'0' as '7' / '8'). Flatten dim adjusts from 64*7*7 to "
            "64*7*5; everything else identical to v3."
        ),
    }
    OUT_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("[meta] wrote %s", OUT_META)

    log.info(
        "[done] best_val=%.2f%%  total_seconds=%.1f",
        best_val * 100, dt,
    )

    # Spot-check vs v3 on the three known-confused captures.
    spot = spot_check()
    if spot:
        # Append the spot-check results to the metadata for later
        # inspection.
        meta["spotCheck"] = spot
        OUT_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return 0 if best_val >= 0.92 else 2


if __name__ == "__main__":
    sys.exit(main())
