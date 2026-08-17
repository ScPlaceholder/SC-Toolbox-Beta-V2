"""Train an RGB CRNN for SC mining-signature digit reading.

Replaces the per-glyph segmenter with a whole-strip CRNN that reads
the entire signature value end-to-end. RGB input gives background-
variability robustness (gray thresholding fails when the scene behind
the pill changes); width-preserving Lanczos scaling preserves enough
time-axis resolution that wide-strip inputs don't collapse below
greedy CTC's ability to decode.

Run::

    %LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe \\
        ocr/train_signal_crnn_rgb.py

Outputs (under ocr/models/):
  * model_signal_crnn_rgb.onnx      - exported ONNX (dynamic batch + width)
  * model_signal_crnn_rgb.json      - alphabet / blank index / metadata
  * model_signal_crnn_rgb_train.log - training log
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image, ImageFilter
from torch.utils.data import Dataset, DataLoader

# --------------------------------------------------------------------
# Paths and constants
# --------------------------------------------------------------------
THIS_DIR = Path(__file__).resolve().parent          # .../tools/Mining_Signals/ocr
TOOL_DIR = THIS_DIR.parent                          # .../tools/Mining_Signals
MODELS_DIR = THIS_DIR / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

OUT_ONNX = MODELS_DIR / "model_signal_crnn_rgb.onnx"
OUT_META = MODELS_DIR / "model_signal_crnn_rgb.json"
OUT_LOG = MODELS_DIR / "model_signal_crnn_rgb_train.log"

PANEL_ROOT = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels"
)

# Production tree imports - need TOOL_DIR on sys.path before importing the
# pipeline helpers we mirror.
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TOOL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(TOOL_DIR / "scripts"))

# Hyperparameters
ALPHABET = "0123456789,"        # 11 classes
NUM_CLASSES = len(ALPHABET) + 1  # +1 blank
BLANK_IDX = NUM_CLASSES - 1     # last index is blank
H_TARGET = 48
EPOCHS = int(os.environ.get("SC_CRNN_EPOCHS", "100"))
BATCH_SIZE = 16
LR = 1e-3
WEIGHT_DECAY = 1e-4
EARLY_STOP_PATIENCE = 15
AUG_PER_CAPTURE = 5
VAL_FRAC = 0.15
SEED = 1234

# --------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------
log = logging.getLogger("train_signal_crnn_rgb")
log.setLevel(logging.INFO)
# clear any old handlers if reloaded
for _h in list(log.handlers):
    log.removeHandler(_h)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
# Console handler
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
log.addHandler(_ch)
# File handler (truncate at start of run)
_fh = logging.FileHandler(str(OUT_LOG), mode="w", encoding="utf-8")
_fh.setFormatter(_fmt)
log.addHandler(_fh)

# Silence noisy production loggers when imported.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
for name in (
    "ocr.sc_ocr.api",
    "ocr.sc_ocr.signal_anchor",
    "hud_tracker.anchors.icon_geometry",
    "hud_tracker.anchors.icon_contour",
    "hud_tracker.anchors.icon_rgb_ncc",
    "hud_tracker.anchors.icon_voter",
    "hud_tracker.anchors.comma_finder",
):
    logging.getLogger(name).setLevel(logging.ERROR)


# --------------------------------------------------------------------
# Production-pipeline preprocessing
# --------------------------------------------------------------------
# Lazy imports - only loaded once per process.
_API = None
_LOCALIZE_ICON = None
_FIND_MAIN_ROW_BOUNDS = None


def _import_pipeline():
    """Import production helpers on first use (kept lazy so the script
    can be imported by tools that only want utility functions without
    pulling in onnx / opencv).
    """
    global _API, _LOCALIZE_ICON, _FIND_MAIN_ROW_BOUNDS
    if _API is None:
        from ocr.sc_ocr import api as _api  # type: ignore
        _API = _api
    if _LOCALIZE_ICON is None:
        from hud_tracker.anchors.icon_voter import (  # type: ignore
            localize_icon as _li,
        )
        _LOCALIZE_ICON = _li
    if _FIND_MAIN_ROW_BOUNDS is None:
        try:
            import extract_labeled_glyphs as xlg  # type: ignore
            _FIND_MAIN_ROW_BOUNDS = getattr(xlg, "_find_main_row_bounds", None)
        except Exception as exc:
            log.warning("extract_labeled_glyphs unavailable: %s", exc)
            _FIND_MAIN_ROW_BOUNDS = None
    return _API, _LOCALIZE_ICON, _FIND_MAIN_ROW_BOUNDS


def _canonicalize_polarity_rgb(rgb: np.ndarray) -> np.ndarray:
    """Apply production polarity canonicalization independently to each
    RGB channel.

    The production helper :func:`api._canonicalize_polarity` takes a
    grayscale and returns a uint8 with text bright on dark. We apply
    the same minority-class rule per channel so the network sees a
    consistent text-bright orientation regardless of HUD color.
    """
    api, _, _ = _import_pipeline()
    out = np.empty_like(rgb)
    for c in range(3):
        out[:, :, c] = api._canonicalize_polarity(rgb[:, :, c])
    return out


def preprocess_capture(png_path: Path) -> Optional[np.ndarray]:
    """Run the production preprocessing path on one PNG and return
    the polarity-canonicalized RGB strip at H_TARGET (variable W).

    Returns None when any pipeline step fails.
    """
    api, localize_icon, find_main_row_bounds = _import_pipeline()
    try:
        img = Image.open(str(png_path)).convert("RGB")
    except Exception:
        return None
    rgb = np.asarray(img, dtype=np.uint8)
    gray = rgb.max(axis=2).astype(np.uint8)

    wmr = api._load_region2_world_model_for_api()
    if wmr is None:
        return None
    vfrac = (wmr.get("features") or {}).get("value")
    if vfrac is None:
        return None

    pill = api._find_pill_for_signal(rgb)
    if pill is None:
        return None
    px, py, pw, ph = pill

    vx = int(round(px + float(vfrac["x_frac"]["mean"]) * pw))
    vy = int(round(py + float(vfrac["y_frac"]["mean"]) * ph))
    vw = int(round(float(vfrac["w_frac"]["mean"]) * pw))
    vh = int(round(float(vfrac["h_frac"]["mean"]) * ph))

    icon_loc = localize_icon(rgb)
    if icon_loc is None:
        return None
    ix, iy, iw, ih = icon_loc["bbox"]
    icon_anchor = ix + iw + max(2, int(pw * 0.03))
    delta = vx - icon_anchor
    vx = icon_anchor
    vw = vw + delta

    rhs_ceiling = px + pw - max(2, int(pw * 0.05))
    digits_x2 = min(vx + vw, rhs_ceiling, gray.shape[1])
    digits_x1 = max(0, vx)
    digits_y1 = max(0, vy)
    digits_y2 = min(vy + vh, gray.shape[0])
    if digits_x2 <= digits_x1 or digits_y2 <= digits_y1:
        return None

    work_gray = gray[digits_y1:digits_y2, digits_x1:digits_x2].copy()
    work_rgb = rgb[digits_y1:digits_y2, digits_x1:digits_x2].copy()

    # Row-isolate via the same helper calibrate_kerning uses.
    if find_main_row_bounds is not None:
        try:
            band = find_main_row_bounds(work_gray)
        except Exception:
            band = None
        if band is not None:
            by1, by2 = band
            work_rgb = work_rgb[by1:by2, :]
            work_gray = work_gray[by1:by2, :]

    if work_rgb.size == 0 or work_rgb.shape[0] < 4 or work_rgb.shape[1] < 8:
        return None

    # Polarity-canonicalize per channel.
    canon = _canonicalize_polarity_rgb(work_rgb)

    # Lanczos resize to H_TARGET, preserving aspect ratio.
    H, W = canon.shape[:2]
    new_w = max(16, int(round(W * H_TARGET / max(1, H))))
    pil = Image.fromarray(canon, mode="RGB")
    pil = pil.resize((new_w, H_TARGET), Image.LANCZOS)
    return np.asarray(pil, dtype=np.uint8)


# --------------------------------------------------------------------
# Augmentation
# --------------------------------------------------------------------
def augment(img: np.ndarray, rng: random.Random) -> np.ndarray:
    """Apply a random subset of augmentations to a uint8 RGB strip.

    Returns a new uint8 array. Width may change (horizontal stretch
    + edge crop). Height stays at H_TARGET because batching pads
    only along width.
    """
    out = img

    # Horizontal stretch 0.95 - 1.05.
    if rng.random() < 0.7:
        scale = rng.uniform(0.95, 1.05)
        new_w = max(16, int(round(out.shape[1] * scale)))
        pil = Image.fromarray(out, mode="RGB").resize(
            (new_w, H_TARGET), Image.LANCZOS,
        )
        out = np.asarray(pil, dtype=np.uint8)

    # Brightness +-15%.
    if rng.random() < 0.7:
        factor = 1.0 + rng.uniform(-0.15, 0.15)
        out = np.clip(out.astype(np.float32) * factor, 0, 255).astype(np.uint8)

    # Gaussian blur sigma=0.3 with 30% probability.
    if rng.random() < 0.3:
        pil = Image.fromarray(out, mode="RGB").filter(
            ImageFilter.GaussianBlur(radius=0.3),
        )
        out = np.asarray(pil, dtype=np.uint8)

    # ── DEGRADATION AUGMENTATION (2026-05, low-res + chromatic aberration) ──
    # The production failures are LOW-RESOLUTION captures: the signature
    # panel renders ~16-24px tall on screen and gets Lanczos-upscaled to
    # H_TARGET, so the digits arrive blurry, fringed (SC's chromatic-
    # aberration HUD effect), and contrast-crushed. The mild sigma=0.3
    # blur above never exposed the CRNN to that regime, so it underfits
    # exactly those captures. This block reproduces it. Validated on a
    # per-glyph POC: hard-degraded digit accuracy 86% -> 99.5% with no
    # loss on clean. Each transform is independently gated so a mix of
    # clean and degraded variants reaches the model.
    #
    # 1) Low-res: downscale height to ~14-28px then back up to H_TARGET.
    if rng.random() < 0.55:
        small_h = rng.randint(14, 28)
        small_w = max(16, int(round(out.shape[1] * small_h / max(1, out.shape[0]))))
        pil = Image.fromarray(out, mode="RGB").resize(
            (small_w, small_h), Image.BILINEAR,
        ).resize((out.shape[1], H_TARGET), Image.LANCZOS)
        out = np.asarray(pil, dtype=np.uint8)
    # 2) Stronger Gaussian blur (defocus / upscale softness).
    if rng.random() < 0.45:
        pil = Image.fromarray(out, mode="RGB").filter(
            ImageFilter.GaussianBlur(radius=rng.uniform(0.6, 1.8)),
        )
        out = np.asarray(pil, dtype=np.uint8)
    # 3) Chromatic aberration: shift R left, B right (lateral CA).
    if rng.random() < 0.5:
        a = out.astype(np.float32)
        sr = rng.choice([-2, -1, 1]); sb = rng.choice([-1, 1, 2])
        a[..., 0] = np.roll(a[..., 0], sr, axis=1)
        a[..., 2] = np.roll(a[..., 2], sb, axis=1)
        out = np.clip(a, 0, 255).astype(np.uint8)
    # 4) Contrast crush + offset (dim HUD / variable exposure).
    if rng.random() < 0.4:
        a = out.astype(np.float32)
        lo, hi = float(a.min()), float(a.max())
        if hi - lo > 1:
            a = (a - lo) / (hi - lo) * 255.0 * rng.uniform(0.6, 1.0) + rng.uniform(0, 45)
        out = np.clip(a, 0, 255).astype(np.uint8)

    # Background noise overlay at low alpha (simulate variable bg).
    if rng.random() < 0.5:
        noise = rng.randint(2, 8)
        n = np.random.randint(0, noise + 1, size=out.shape, dtype=np.int16)
        out = np.clip(out.astype(np.int16) + n - noise // 2, 0, 255).astype(
            np.uint8,
        )

    # Small width-edge crop +-2 px.
    if rng.random() < 0.5 and out.shape[1] > 24:
        left = rng.randint(0, 2)
        right = rng.randint(0, 2)
        if left + right < out.shape[1]:
            out = out[:, left:out.shape[1] - right]

    return out


# --------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------
def encode_label(s: str) -> list[int]:
    """Map characters in ``s`` to alphabet indices. Drop anything not
    in ALPHABET (and warn).
    """
    out: list[int] = []
    for c in s:
        idx = ALPHABET.find(c)
        if idx >= 0:
            out.append(idx)
    return out


def decode_greedy(logits_tcv: np.ndarray) -> str:
    """CTC greedy decode over a (T, num_classes) array."""
    preds = logits_tcv.argmax(axis=-1)
    chars: list[str] = []
    prev = -1
    for p in preds:
        p = int(p)
        if p != prev and p != BLANK_IDX and 0 <= p < len(ALPHABET):
            chars.append(ALPHABET[p])
        prev = p
    return "".join(chars)


class StripDataset(Dataset):
    """In-memory dataset of (preprocessed_strip, label_string).

    ``train=True`` re-augments on every __getitem__. ``train=False``
    returns the deterministic preprocessed strip.
    """

    def __init__(
        self,
        items: list[tuple[np.ndarray, str]],
        train: bool,
        seed: int = 0,
    ):
        self.items = items
        self.train = train
        # one rng per worker isn't necessary since DataLoader defaults
        # to no workers on Windows-CPU; we re-seed on each call instead.
        self._rng = random.Random(seed)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor, int]:
        img, label = self.items[idx]
        if self.train:
            img = augment(img, self._rng)
        # to (C, H, W) float32 in [0, 1].
        arr = img.astype(np.float32) / 255.0
        arr = np.transpose(arr, (2, 0, 1))  # (3, H, W)
        x = torch.from_numpy(arr.copy())
        y = torch.tensor(encode_label(label), dtype=torch.long)
        return x, y, x.shape[-1]


def collate_pad(batch):
    """Pad each strip in the batch to max width (right-pad with 0).

    Returns:
        images:   (B, 3, H, W_max) float32
        targets:  (sum(target_lengths),) int64
        input_widths: (B,) int64 - per-sample width before padding
        target_lengths: (B,) int64
    """
    images, labels, widths = zip(*batch)
    H = images[0].shape[1]
    max_w = max(int(w) for w in widths)
    B = len(images)
    out = torch.zeros((B, 3, H, max_w), dtype=torch.float32)
    for i, img in enumerate(images):
        out[i, :, :, : img.shape[-1]] = img
    targets = torch.cat(labels) if any(len(l) for l in labels) else torch.zeros(
        (0,), dtype=torch.long,
    )
    target_lengths = torch.tensor(
        [len(l) for l in labels], dtype=torch.long,
    )
    input_widths = torch.tensor(widths, dtype=torch.long)
    return out, targets, input_widths, target_lengths


# --------------------------------------------------------------------
# Model: RGB CRNN (height-aggressive pool, time-axis preserving)
# --------------------------------------------------------------------
class RGBCRNN(nn.Module):
    """RGB CRNN per the spec.

    Backbone downsamples height 12x and width 4x to produce a
    (T = W/4 - 1, B, 512) sequence. Two BiLSTM layers then a linear
    head over NUM_CLASSES (digits + comma + blank).
    """

    def __init__(self, num_classes: int = NUM_CLASSES, hidden: int = 256):
        super().__init__()
        # Conv backbone.
        self.conv1 = nn.Conv2d(3, 64, 3, padding=1)
        self.conv2 = nn.Conv2d(64, 128, 3, padding=1)
        self.conv3 = nn.Conv2d(128, 256, 3, padding=1)
        self.conv4 = nn.Conv2d(256, 256, 3, padding=1)
        self.conv5 = nn.Conv2d(256, 512, 3, padding=1)
        self.bn5 = nn.BatchNorm2d(512)
        self.conv6 = nn.Conv2d(512, 512, 3, padding=1)
        self.bn6 = nn.BatchNorm2d(512)
        # Conv 2x2 no padding (collapses height by 1, width by 1).
        self.conv7 = nn.Conv2d(512, 512, 2, padding=0)

        # BiLSTM stack.
        self.rnn = nn.LSTM(
            input_size=512,
            hidden_size=hidden,
            num_layers=2,
            bidirectional=True,
            batch_first=False,
        )
        self.fc = nn.Linear(2 * hidden, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, 3, H=48, W)
        x = F.relu(self.conv1(x))
        x = F.max_pool2d(x, 2)              # H=24, W=W/2
        x = F.relu(self.conv2(x))
        x = F.max_pool2d(x, 2)              # H=12, W=W/4
        x = F.relu(self.conv3(x))
        x = F.relu(self.conv4(x))
        x = F.max_pool2d(x, kernel_size=(2, 1))  # H=6, W unchanged
        x = F.relu(self.bn5(self.conv5(x)))
        x = F.relu(self.bn6(self.conv6(x)))
        x = F.max_pool2d(x, kernel_size=(2, 1))  # H=3, W unchanged
        x = F.relu(self.conv7(x))           # H=2, W' = W/4 - 1
        # Collapse remaining height dim 2 -> 1. Using a fixed-kernel
        # max_pool2d here instead of adaptive_max_pool2d because the
        # ONNX exporter (both legacy TorchScript and new dynamo path)
        # can't handle adaptive pooling with a dynamic output width
        # — and our width is dynamic by design. Max-pool with
        # kernel_size=(2, 1) over a 2-tall input is mathematically
        # equivalent to adaptive_max_pool2d to height 1 (both take
        # max over the same 2-pixel height window per column), so
        # the existing checkpoint weights remain valid.
        x = F.max_pool2d(x, kernel_size=(2, 1))
        # (B, 512, 1, T) -> (T, B, 512)
        x = x.squeeze(2)                    # (B, 512, T)
        x = x.permute(2, 0, 1).contiguous()  # (T, B, 512)
        x, _ = self.rnn(x)
        x = self.fc(x)                      # (T, B, num_classes)
        return x


def time_dim_for_width(w: int) -> int:
    """Return the temporal length T produced by the conv backbone for an
    input width ``w``. Backbone has total width-stride 4 then a -1 from
    the final 2x2 no-padding conv, i.e. T = w//4 - 1.
    """
    return max(0, (w // 4) - 1)


# --------------------------------------------------------------------
# Build dataset from PANEL_ROOT
# --------------------------------------------------------------------
def build_source_items() -> list[tuple[np.ndarray, str]]:
    items: list[tuple[np.ndarray, str]] = []
    skipped_no_gt = 0
    skipped_pipeline = 0
    skipped_empty_label = 0
    walked = 0

    for png_path in sorted(PANEL_ROOT.glob("user_*/region2/*.png")):
        if png_path.with_suffix(".skip").exists():
            continue
        json_path = png_path.with_suffix(".json")
        if not json_path.exists():
            continue
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        gt_raw = str(meta.get("value", "")).strip()
        if not gt_raw:
            skipped_no_gt += 1
            continue
        # Keep digits + comma; drop anything else.
        gt = "".join(c for c in gt_raw if c in ALPHABET)
        if not gt:
            skipped_empty_label += 1
            continue

        walked += 1
        try:
            arr = preprocess_capture(png_path)
        except Exception as exc:
            log.debug("preprocess failed %s: %s", png_path.name, exc)
            skipped_pipeline += 1
            continue
        if arr is None:
            skipped_pipeline += 1
            continue
        items.append((arr, gt))

    log.info(
        "Source dataset: walked=%d kept=%d "
        "skipped(pipeline)=%d skipped(no_gt)=%d skipped(empty_label)=%d",
        walked, len(items), skipped_pipeline, skipped_no_gt, skipped_empty_label,
    )
    return items


# --------------------------------------------------------------------
# Train / val
# --------------------------------------------------------------------
def split_train_val(
    items: list[tuple[np.ndarray, str]], val_frac: float, seed: int,
):
    rng = random.Random(seed)
    indices = list(range(len(items)))
    rng.shuffle(indices)
    n_val = max(1, int(round(len(indices) * val_frac)))
    val_idx = set(indices[:n_val])
    train_items = [it for i, it in enumerate(items) if i not in val_idx]
    val_items = [it for i, it in enumerate(items) if i in val_idx]
    return train_items, val_items


def expand_train(
    items: list[tuple[np.ndarray, str]], k: int,
) -> list[tuple[np.ndarray, str]]:
    """Expand training items by repeating; the dataset class re-augments
    on each __getitem__.
    """
    out: list[tuple[np.ndarray, str]] = []
    for _ in range(k):
        out.extend(items)
    return out


def evaluate_string_acc(
    model: nn.Module,
    items: list[tuple[np.ndarray, str]],
    device: torch.device,
) -> tuple[float, list[tuple[str, str, bool]]]:
    """Run greedy CTC over each item; return (acc, per_item_records)."""
    model.eval()
    n_match = 0
    records: list[tuple[str, str, bool]] = []
    with torch.no_grad():
        for img, label in items:
            arr = img.astype(np.float32) / 255.0
            arr = np.transpose(arr, (2, 0, 1))[None, ...]
            x = torch.from_numpy(arr).to(device)
            logits = model(x)  # (T, 1, C)
            logits_np = logits.detach().cpu().numpy()[:, 0, :]
            pred = decode_greedy(logits_np)
            ok = pred == label
            n_match += int(ok)
            records.append((label, pred, ok))
    acc = n_match / max(1, len(items))
    return acc, records


def main() -> int:
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)

    device = torch.device(
        "cuda" if (os.environ.get("SC_FORCE_CPU") != "1"
                   and torch.cuda.is_available()) else "cpu"
    )
    log.info("Device: %s", device)
    log.info("PyTorch: %s", torch.__version__)
    log.info("Alphabet: %r blank=%d num_classes=%d", ALPHABET, BLANK_IDX, NUM_CLASSES)

    # 1. Build source items.
    log.info("Walking %s ...", PANEL_ROOT)
    t0 = time.time()
    items = build_source_items()
    log.info("Built source items in %.1fs (n=%d)", time.time() - t0, len(items))
    if not items:
        log.error("No usable training items - aborting.")
        return 1

    train_items, val_items = split_train_val(items, VAL_FRAC, SEED)
    log.info("Split: train=%d val=%d", len(train_items), len(val_items))

    # Fixed DEGRADED copy of the held-out val set. Model selection scores
    # clean + degraded together, so the chosen checkpoint is the one that
    # reads the low-res / chromatic-aberration regime (where production
    # fails) — not just clean crops (where every checkpoint already aces
    # it, hiding the improvement). Deterministic per index → stable across
    # epochs so early-stopping behaves.
    val_items_deg = [
        (augment(_img, random.Random(9000 + _i)), _lab)
        for _i, (_img, _lab) in enumerate(val_items)
    ]
    val_eval_items = val_items + val_items_deg
    log.info(
        "val selection set: %d clean + %d degraded = %d",
        len(val_items), len(val_items_deg), len(val_eval_items),
    )

    # 2. Expand train set with per-source repetitions; augmentation runs
    # at __getitem__ time so each pass is a fresh sample.
    train_expanded = expand_train(train_items, AUG_PER_CAPTURE)
    log.info("Expanded train set: %d items (%dx)", len(train_expanded), AUG_PER_CAPTURE)

    train_ds = StripDataset(train_expanded, train=True, seed=SEED)
    val_ds = StripDataset(val_items, train=False, seed=SEED + 1)
    train_loader = DataLoader(
        train_ds, batch_size=BATCH_SIZE, shuffle=True,
        collate_fn=collate_pad, num_workers=0,
    )
    val_loader = DataLoader(
        val_ds, batch_size=BATCH_SIZE, shuffle=False,
        collate_fn=collate_pad, num_workers=0,
    )

    # 3. Model + optimizer + scheduler.
    model = RGBCRNN().to(device)
    total_params = sum(p.numel() for p in model.parameters())
    log.info("Model params: %d", total_params)

    # Checkpoint path -- written to disk on every val improvement so
    # a kill mid-training doesn't lose all progress. Sibling
    # ``export_crnn_to_onnx.py`` can load this and produce the ONNX
    # if the main training loop never reaches its own export step.
    ckpt_path = OUT_ONNX.parent / "model_signal_crnn_rgb_best.pth"

    optim = torch.optim.AdamW(
        model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY,
    )
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(optim, T_max=EPOCHS)
    ctc = nn.CTCLoss(blank=BLANK_IDX, zero_infinity=True)

    best_val_loss = math.inf
    best_val_acc = -1.0
    best_state: Optional[dict] = None
    bad_epochs = 0
    train_start = time.time()

    # Warm-restart support: if a saved best checkpoint already exists
    # on disk (e.g. from a prior training run that got killed before
    # ONNX export), load it as starting weights. The val-acc/loss
    # bookkeeping starts fresh -- they'll be re-established on the
    # next val pass after epoch 1.
    if ckpt_path.is_file():
        try:
            saved = torch.load(str(ckpt_path), map_location=device)
            if isinstance(saved, dict) and "state_dict" in saved:
                model.load_state_dict(saved["state_dict"])
                # Warm-start the WEIGHTS but DELIBERATELY do NOT restore
                # the best-metric bar: the selection metric changed to
                # clean+degraded val, so the old clean-only val_acc=0.833
                # isn't comparable and would block every (degraded-better)
                # checkpoint. Leave best_val_acc=-1 so epoch 1 re-baselines
                # on the new metric.
                best_state = {
                    k: v.detach().cpu().clone()
                    for k, v in saved["state_dict"].items()
                }
                log.info(
                    "Loaded existing checkpoint: val_loss=%.4f val_acc=%.3f",
                    best_val_loss, best_val_acc,
                )
            else:
                model.load_state_dict(saved)
                log.info("Loaded existing checkpoint (raw state_dict)")
        except Exception as exc:
            log.warning("Failed to load checkpoint %s: %s", ckpt_path, exc)

    for epoch in range(1, EPOCHS + 1):
        model.train()
        epoch_loss = 0.0
        n_batches = 0
        for images, targets, input_widths, target_lengths in train_loader:
            images = images.to(device)
            targets = targets.to(device)
            target_lengths = target_lengths.to(device)
            optim.zero_grad()
            logits = model(images)  # (T, B, C)
            log_probs = F.log_softmax(logits, dim=2)
            T = log_probs.shape[0]
            # Per-sample input length: T_for(w) clamped to <= T.
            input_lengths = torch.tensor(
                [min(T, time_dim_for_width(int(w))) for w in input_widths],
                dtype=torch.long, device=device,
            )
            # Skip batches where any input_length < target_length (CTC
            # cannot produce a label longer than the sequence). Doesn't
            # happen often but we guard anyway.
            if (input_lengths < target_lengths).any():
                continue
            loss = ctc(log_probs, targets, input_lengths, target_lengths)
            if not torch.isfinite(loss):
                continue
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optim.step()
            epoch_loss += float(loss.item())
            n_batches += 1
        sched.step()
        train_loss = epoch_loss / max(1, n_batches)

        # Validation: CTC loss + greedy string match.
        model.eval()
        val_loss = 0.0
        n_val_batches = 0
        with torch.no_grad():
            for images, targets, input_widths, target_lengths in val_loader:
                images = images.to(device)
                targets = targets.to(device)
                target_lengths = target_lengths.to(device)
                logits = model(images)
                log_probs = F.log_softmax(logits, dim=2)
                T = log_probs.shape[0]
                input_lengths = torch.tensor(
                    [min(T, time_dim_for_width(int(w))) for w in input_widths],
                    dtype=torch.long, device=device,
                )
                if (input_lengths < target_lengths).any():
                    continue
                vloss = ctc(log_probs, targets, input_lengths, target_lengths)
                if torch.isfinite(vloss):
                    val_loss += float(vloss.item())
                    n_val_batches += 1
        val_loss = val_loss / max(1, n_val_batches)

        val_acc, _ = evaluate_string_acc(model, val_eval_items, device)
        val_acc_clean, _ = evaluate_string_acc(model, val_items, device)

        log.info(
            "epoch %3d/%d  train_loss=%.4f  val_loss=%.4f  "
            "val_acc(clean+deg)=%.3f  val_acc_clean=%.3f  lr=%.5f",
            epoch, EPOCHS, train_loss, val_loss, val_acc, val_acc_clean,
            optim.param_groups[0]["lr"],
        )

        # Select the checkpoint by the clean+degraded val_acc (primary):
        # that's the model that handles the failing regime. val_loss is
        # logged only.
        if val_loss < best_val_loss - 1e-4:
            best_val_loss = val_loss
        improved = val_acc > best_val_acc
        if improved:
            best_val_acc = val_acc
        if improved:
            best_state = {
                k: v.detach().cpu().clone() for k, v in model.state_dict().items()
            }
            bad_epochs = 0
            # Persist to disk on every improvement so a SIGKILL/
            # harness-reap doesn't lose the best weights. We can
            # then either resume training via warm-restart OR run
            # ``export_crnn_to_onnx.py`` to write ONNX from this
            # checkpoint without retraining.
            try:
                torch.save(
                    {
                        "state_dict": best_state,
                        "val_loss": best_val_loss,
                        "val_acc": best_val_acc,
                        "epoch": epoch,
                    },
                    str(ckpt_path),
                )
            except Exception as exc:
                log.warning("Checkpoint save failed: %s", exc)
        else:
            bad_epochs += 1
            if bad_epochs >= EARLY_STOP_PATIENCE:
                log.info(
                    "Early stop at epoch %d (val plateaued for %d epochs)",
                    epoch, EARLY_STOP_PATIENCE,
                )
                break

    train_dur = time.time() - train_start
    log.info(
        "Training done in %.1fs. best_val_loss=%.4f best_val_acc=%.3f",
        train_dur, best_val_loss, best_val_acc,
    )

    # Restore best weights.
    if best_state is not None:
        model.load_state_dict(best_state)

    # Final val accuracy (greedy CTC, exact match).
    final_val_acc, val_records = evaluate_string_acc(model, val_items, device)
    log.info("Final held-out val accuracy: %.3f (%d/%d)",
             final_val_acc, sum(1 for r in val_records if r[2]), len(val_records))

    # 4. ONNX export.
    log.info("Exporting ONNX to %s", OUT_ONNX)
    model.eval()
    model = model.to("cpu")   # export on CPU (avoid cuda/cpu dummy mismatch)
    dummy_w = 200  # arbitrary; dynamic axis below
    dummy = torch.randn(1, 3, H_TARGET, dummy_w, dtype=torch.float32)
    try:
        torch.onnx.export(
            model, dummy, str(OUT_ONNX),
            opset_version=14,
            input_names=["image"],
            output_names=["logits"],
            dynamic_axes={
                "image": {0: "batch", 3: "width"},
                "logits": {1: "batch", 0: "time"},
            },
            dynamo=False,   # force legacy TorchScript exporter: torch 2.11's
                            # default dynamo path fails on the BiLSTM
                            # ("always_classified is unsupported")
        )
        export_ok = True
    except Exception as exc:
        log.error("ONNX export failed: %s", exc)
        export_ok = False

    onnx_size = OUT_ONNX.stat().st_size if OUT_ONNX.exists() else 0
    log.info("ONNX size: %d bytes (%.2f MB)", onnx_size, onnx_size / 1e6)

    # Verify ONNX with onnxruntime.
    ort_ok = False
    if export_ok:
        try:
            import onnxruntime as ort  # type: ignore
            sess = ort.InferenceSession(str(OUT_ONNX), providers=["CPUExecutionProvider"])
            test_in = np.random.rand(1, 3, H_TARGET, 220).astype(np.float32)
            out = sess.run(None, {"image": test_in})[0]
            log.info("ONNX runtime smoke test: input=%s output=%s",
                     test_in.shape, out.shape)
            ort_ok = True
        except Exception as exc:
            log.error("ONNX runtime smoke test failed: %s", exc)

    # 5. Spot check on the three named captures + held-out val.
    spot_paths = [
        PANEL_ROOT / "user_20260418_081525" / "region2" / "cap_20260418_085400_739.png",
        PANEL_ROOT / "user_20260418_081525" / "region2" / "cap_20260418_085431_378.png",
        PANEL_ROOT / "user_20260418_081525" / "region2" / "cap_20260418_085436_978.png",
    ]
    spot_gt = ["17,020", "11,520", "21,200"]
    spot_results: list[dict] = []
    if ort_ok:
        try:
            import onnxruntime as ort  # type: ignore
            sess = ort.InferenceSession(
                str(OUT_ONNX), providers=["CPUExecutionProvider"],
            )
            log.info("Spot-check (ONNX greedy CTC):")
            for p, gt in zip(spot_paths, spot_gt):
                if not p.exists():
                    log.info("  %s -> MISSING capture", p.name)
                    spot_results.append({"file": p.name, "gt": gt,
                                         "pred": None, "match": False,
                                         "reason": "missing"})
                    continue
                arr = preprocess_capture(p)
                if arr is None:
                    log.info("  %s -> preprocess returned None", p.name)
                    spot_results.append({"file": p.name, "gt": gt,
                                         "pred": None, "match": False,
                                         "reason": "preprocess_fail"})
                    continue
                inp = (arr.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
                logits = sess.run(None, {"image": inp})[0]  # (T, 1, C)
                pred = decode_greedy(logits[:, 0, :])
                ok = pred == gt
                log.info("  %s  GT=%s  pred=%s  match=%s",
                         p.name, gt, pred, "Y" if ok else "N")
                spot_results.append({"file": p.name, "gt": gt,
                                     "pred": pred, "match": ok})
        except Exception as exc:
            log.error("Spot-check failed: %s", exc)

    # 6. Metadata.
    meta = {
        "schema": "signal_crnn_rgb_v1",
        "alphabet": ALPHABET,
        "num_classes": NUM_CLASSES,
        "blank_idx": BLANK_IDX,
        "input_height": H_TARGET,
        "input_channels": 3,
        "input_dtype": "float32 in [0, 1]",
        "input_layout": "NCHW",
        "output_layout": "TBC",
        "polarity_canon": "per-channel _canonicalize_polarity (text-bright on dark)",
        "scaling": "Lanczos resize to height=H_TARGET, width preserved proportionally",
        "training": {
            "source_captures": len(items),
            "train_n": len(train_items),
            "val_n": len(val_items),
            "augmentation_factor": AUG_PER_CAPTURE,
            "expanded_train_n": len(train_expanded),
            "epochs_target": EPOCHS,
            "early_stop_patience": EARLY_STOP_PATIENCE,
            "batch_size": BATCH_SIZE,
            "lr": LR,
            "weight_decay": WEIGHT_DECAY,
            "best_val_loss": best_val_loss,
            "best_val_acc": best_val_acc,
            "final_val_acc": final_val_acc,
            "duration_sec": train_dur,
        },
        "spot_check": spot_results,
        "onnx": {
            "size_bytes": onnx_size,
            "ort_smoke_test": ort_ok,
            "exported": export_ok,
            "opset": 14,
        },
    }
    OUT_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("Wrote metadata: %s", OUT_META)

    log.info("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
