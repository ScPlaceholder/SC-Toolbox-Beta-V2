"""Train the value-crop CRNN and export to ONNX.

Usage:
    python -m ocr.train_crnn [--epochs 40] [--n 20000] [--lr 1e-3]

Every run now writes TWO checkpoint files alongside the ONNX export:

    ocr/models/model_crnn.pt                       # canonical (overwritten)
    ocr/models/model_crnn_<stamp>_val<XX>.pt       # versioned snapshot
    ocr/models/model_crnn_<stamp>_val<XX>.json     # CLI args + metrics

The versioned files are never overwritten, so a regressed run can
always be rolled back by copying a prior snapshot over ``model_crnn.pt``
and re-exporting.

Reproducing the b925lgy0r 78.85% val run (for reference):
    python -m ocr.train_crnn \
        --epochs 8 --n 20000 --lr 1e-4 \
        --batch-size 128 --real-aug 50 \
        --init-from ocr/models/model_crnn_pretrained.pt

Reads single-glyph labeled crops from ``training_data/{0-9}/`` via
``ocr.synth_data.generate_dataset`` (which synthesizes sequence
crops by concatenating single glyphs with augmentation), trains a
small conv + BiLSTM + CTC recognizer, and exports the trained model
to ``ocr/models/model_crnn.onnx`` plus a ``model_crnn.json`` manifest.

Runtime (``ocr/sc_ocr/api.py::_crnn_recognize``) prefers this model
over the existing 28×28 classifier + Tesseract voter. If the ONNX
file is missing at runtime, the existing voter still serves reads
unchanged.

Dev dependencies (NOT in runtime ``requirements.txt``):
    pip install torch onnx

Training takes ~5-10 minutes on CPU with the default ``--n 20000``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ocr.synth_data import (
    CHAR_CLASSES,
    BLANK_IDX,
    CANVAS_H,
    generate_dataset,
    label_to_indices,
)

_CRNN_TRAINING_DIR = Path(__file__).resolve().parent.parent / "training_data_crnn"
_CRNN_MANIFEST_PATH = _CRNN_TRAINING_DIR / "manifest.json"

log = logging.getLogger(__name__)

_MODULE_DIR = Path(__file__).resolve().parent
_MODEL_DIR = _MODULE_DIR / "models"
_OUT_ONNX = _MODEL_DIR / "model_crnn.onnx"
_OUT_META = _MODEL_DIR / "model_crnn.json"

# Num classes for the softmax = alphabet + CTC blank.
# BLANK_IDX is positioned AFTER the alphabet (last index).
NUM_TOKENS = len(CHAR_CLASSES) + 1


# ── Model ──────────────────────────────────────────────────────────


def build_model(size: str = "small"):
    """Conv + BiLSTM + linear recognizer.

    Input: (batch, 1, 32, W) float32 in [0, 1].
    Output: (T, batch, NUM_TOKENS) log-softmax-ready logits.

    With H=32 the conv stack collapses height to 1 after:
      H: 32 → 16 → 8 → 4 → 2 → 1
      (pool,2 ×3) then (pool,(2,1) ×1) then (conv kernel (2,1)).
    Width path: W → W/2 → W/4 → W/4 → W/4 → W/4 − 1 ≈ T.

    ``size`` selects capacity:
      * ``"small"`` — 1.3M params, 40 ms/call CPU (legacy)
      * ``"large"`` — ~5M params, ~150 ms/call CPU (fp32) /
        ~80 ms/call (int8 quant). Widens conv channels 1.5× and
        RNN hidden 2×. Trained on the same synth+real pipeline;
        drop-in upgrade once the 5M weights exist on disk.
    """
    import torch
    import torch.nn as nn

    if size == "small":
        c = (32, 64, 128, 256, 256)
        rnn_hidden = 128
    elif size == "large":
        c = (48, 96, 192, 384, 384)
        rnn_hidden = 256
    else:
        raise ValueError(f"unknown size {size!r}, expected 'small' or 'large'")

    class CRNN(nn.Module):
        def __init__(self, n_tokens: int = NUM_TOKENS):
            super().__init__()
            self.cnn = nn.Sequential(
                nn.Conv2d(1, c[0], 3, padding=1), nn.BatchNorm2d(c[0]), nn.ReLU(),
                nn.MaxPool2d(2),                                        # 32 → 16
                nn.Conv2d(c[0], c[1], 3, padding=1), nn.BatchNorm2d(c[1]), nn.ReLU(),
                nn.MaxPool2d(2),                                        # 16 → 8
                nn.Conv2d(c[1], c[2], 3, padding=1), nn.BatchNorm2d(c[2]), nn.ReLU(),
                nn.MaxPool2d((2, 1)),                                   # 8 → 4
                nn.Conv2d(c[2], c[3], 3, padding=1), nn.BatchNorm2d(c[3]), nn.ReLU(),
                nn.MaxPool2d((2, 1)),                                   # 4 → 2
                nn.Conv2d(c[3], c[4], (2, 1)), nn.BatchNorm2d(c[4]), nn.ReLU(),  # 2 → 1
            )
            self.rnn = nn.LSTM(
                input_size=c[4],
                hidden_size=rnn_hidden,
                num_layers=2,
                bidirectional=True,
                dropout=0.1,
            )
            self.fc = nn.Linear(rnn_hidden * 2, n_tokens)

        def forward(self, x):  # x: (B, 1, 32, W)
            f = self.cnn(x)  # (B, 256, 1, T)
            B, C, H, T = f.shape
            assert H == 1, f"expected height-collapsed features, got H={H}"
            f = f.squeeze(2).permute(2, 0, 1)  # (T, B, C)
            r, _ = self.rnn(f)                 # (T, B, 2H)
            return self.fc(r)                  # (T, B, NUM_TOKENS)

    return CRNN()


# ── Real labeled crops ─────────────────────────────────────────────


def _real_group(entry: dict) -> str:
    """The unit a real crop must NOT be split across: its capture SESSION.

    Added 2026-09-22 after finding the old split leaked. Augmentation ran FIRST and the 90/10 split
    SECOND, so each crop's 1 + real_aug copies landed on both sides and val graded the model on
    jittered copies of its own training images. Adjacent captures in one session are near-identical
    frames of the same rock, so even a per-capture split would leak; the session is the honest unit.
    HUD crops are named ``user_<session>__cap_<capture>__<value>.png``. Anything else (e.g. the
    synthetic font variants) is its own group, since each row is an independent render.
    """
    import re
    base = str(entry.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1]
    m = re.match(r"(user_\d+_\d+)__cap_", base)
    return m.group(1) if m else "row:" + base


def _real_capture(entry: dict) -> str:
    """Finer unit than the session: one capture (its mass/resistance/instability crops share it)."""
    import re
    base = str(entry.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1]
    m = re.match(r"(user_\d+_\d+__cap_\d+_\d+_\d+)__", base)
    return m.group(1) if m else "row:" + base


def _prep_real_crop(img) -> np.ndarray:
    """Grayscale PIL crop -> the canonical training/eval array: dark-on-light inverted, height CANVAS_H.

    Factored out so evaluation preprocesses EXACTLY as training does; two copies drift.
    """
    from PIL import Image
    base = np.array(img, dtype=np.uint8)
    if float(np.median(base)) > 140:
        base = 255 - base
    H, W = base.shape
    w_new = max(16, int(round(W * CANVAS_H / max(1, H))))
    return np.array(
        Image.fromarray(base).resize((w_new, CANVAS_H), Image.BILINEAR),
        dtype=np.uint8,
    )


def _load_real_samples(
    augment_per_sample: int = 200,
    include_groups: Optional[set] = None,
    exclude_groups: Optional[set] = None,
    group_key=None,
) -> list[tuple[np.ndarray, str]]:
    """Load (image, label) pairs from training_data_crnn/ and augment.

    Reads ``manifest.json`` for labels. Each real crop gets
    ``augment_per_sample`` random variations with AGGRESSIVE
    multi-resolution + quality augmentation so the CRNN is robust
    across monitor sizes, render resolutions, and capture qualities:

      * **Random downscale → upscale** (simulates low-res captures)
      * **JPEG compression artifacts** (simulates video-compressed
        captures and screenshots saved as JPG)
      * **Variable final height** (20-44 px range, the canvas still
        pads to 32 for training but content is rendered at varied
        sizes before centering)
      * Brightness/contrast jitter
      * Gaussian blur (0.1-1.0 σ)
      * Sharpening (negative blur)
      * Small horizontal shift
      * Gaussian noise injection (simulates compression noise)

    The combination teaches the model the same label looks many
    different ways in the wild.
    """
    import io
    import json
    import random
    from PIL import Image, ImageFilter

    if not _CRNN_MANIFEST_PATH.is_file():
        return []
    try:
        with open(_CRNN_MANIFEST_PATH) as f:
            manifest = json.load(f)
    except Exception:
        return []

    rng = random.Random(13)
    out: list[tuple[np.ndarray, str]] = []
    for entry in manifest.get("files", []):
        path = _CRNN_TRAINING_DIR / entry["path"]
        label = entry.get("label", "")
        if not path.is_file() or not label:
            continue
        g = (group_key or _real_group)(entry)
        if include_groups is not None and g not in include_groups:
            continue
        if exclude_groups is not None and g in exclude_groups:
            continue
        try:
            img = Image.open(path).convert("L")
        except Exception:
            continue

        base = _prep_real_crop(img)

        # Emit the unaugmented version once so the model sees the
        # "canonical" real crop.
        out.append((base.copy(), label))

        for _ in range(augment_per_sample):
            pil = Image.fromarray(base)
            pw, ph = pil.size

            # (1) Random down/up scale (tempered). Earlier aggressive
            # 0.35-1.2x range taught the model to recognize severely
            # degraded crops at the cost of fidelity on clean ones.
            # Narrower range (0.65-1.15) still covers realistic capture
            # variations without pushing the model into unrecognizable
            # territory.
            scale = rng.uniform(0.65, 1.15)
            small_h = max(8, int(CANVAS_H * scale))
            small_w = max(8, int(pw * scale))
            if scale < 0.95:
                pil = pil.resize((small_w, small_h), Image.BILINEAR)
                pil = pil.resize((pw, ph), Image.BILINEAR)
            elif scale > 1.05:
                pil = pil.resize((small_w, small_h), Image.LANCZOS)
                pil = pil.resize((pw, ph), Image.LANCZOS)

            # (2) JPEG compression (lower frequency, higher quality)
            if rng.random() < 0.25:
                buf = io.BytesIO()
                try:
                    pil.save(buf, format="JPEG", quality=rng.randint(65, 92))
                    buf.seek(0)
                    pil = Image.open(buf).convert("L")
                except Exception:
                    pass

            # (3) Gaussian blur or unsharp-masking (sharpening)
            r = rng.random()
            if r < 0.25:
                pil = pil.filter(ImageFilter.GaussianBlur(rng.uniform(0.1, 0.6)))
            elif r < 0.35:
                pil = pil.filter(ImageFilter.UnsharpMask(radius=1, percent=120))

            aug = np.asarray(pil, dtype=np.float32)

            # (4) Brightness + contrast (tempered)
            aug *= rng.uniform(0.85, 1.10)
            aug += rng.uniform(-8, 8)

            # (5) Gaussian noise (light touch)
            if rng.random() < 0.20:
                aug += rng.uniform(1, 4) * np.random.randn(*aug.shape)

            aug = np.clip(aug, 0, 255).astype(np.uint8)

            # (6) Horizontal shift ±3 px (wider than before)
            shift = rng.randint(-3, 3)
            if shift != 0:
                W_a = aug.shape[1]
                if shift > 0:
                    aug = np.concatenate(
                        [np.full((CANVAS_H, shift), rng.randint(10, 30), dtype=np.uint8),
                         aug[:, :W_a - shift]],
                        axis=1,
                    )
                else:
                    aug = np.concatenate(
                        [aug[:, -shift:],
                         np.full((CANVAS_H, -shift), rng.randint(10, 30), dtype=np.uint8)],
                        axis=1,
                    )

            out.append((aug, label))
    return out


# ── Data pipeline ──────────────────────────────────────────────────


class _SynthDataset:
    """Minimal torch Dataset-compatible wrapper over the synth samples."""
    def __init__(self, samples: Sequence[tuple[np.ndarray, str]]):
        self.samples = samples

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        import torch
        img, label = self.samples[idx]
        # Normalize to [0, 1] float32, shape (1, H, W)
        arr = img.astype(np.float32) / 255.0
        tensor = torch.from_numpy(arr).unsqueeze(0)
        target = torch.tensor(label_to_indices(label), dtype=torch.long)
        return tensor, target, label


def _collate(batch):
    """Pad variable-width images to the batch max, concat targets."""
    import torch
    imgs, targets, labels = zip(*batch)
    max_w = max(t.shape[-1] for t in imgs)
    B = len(imgs)
    padded = torch.zeros((B, 1, CANVAS_H, max_w), dtype=torch.float32)
    for i, t in enumerate(imgs):
        padded[i, :, :, :t.shape[-1]] = t
    # Flat target tensor + per-sample lengths (for CTCLoss)
    target_lens = torch.tensor([len(t) for t in targets], dtype=torch.long)
    targets_flat = torch.cat(targets) if targets else torch.empty(0, dtype=torch.long)
    # Input length per-sample == T dimension after CNN. For width W
    # the conv stack produces T = W/4 - 1. We compute it here and
    # clamp to 1 so CTC never divides by zero on tiny crops.
    input_lens = torch.tensor(
        [max(1, img.shape[-1] // 4 - 1) for img in imgs],
        dtype=torch.long,
    )
    return padded, targets_flat, input_lens, target_lens, list(labels)


# ── Decoding / metrics ─────────────────────────────────────────────


def greedy_decode(logits: np.ndarray) -> str:
    """Greedy CTC decode for a single sample.

    logits: (T, NUM_TOKENS)
    Returns the decoded string.
    """
    preds = logits.argmax(axis=-1)
    out: list[str] = []
    prev = -1
    for p in preds:
        p = int(p)
        if p != prev and p != BLANK_IDX:
            out.append(CHAR_CLASSES[p])
        prev = p
    return "".join(out)


def _cer(pred: str, truth: str) -> float:
    """Character-error-rate (Levenshtein / max(len(truth), 1))."""
    # Tiny DP — both strings are short (≤ 8 chars), so this is cheap.
    m, n = len(pred), len(truth)
    if n == 0:
        return 0.0 if m == 0 else 1.0
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if pred[i - 1] == truth[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
    return dp[m][n] / n


# ── Training loop ──────────────────────────────────────────────────


def _plan_real_split(manifest_rows, holdout_session="auto", seed=42):
    """-> (sessions, holdout_session, test_groups, val_caps). Pure, so it can be tested without training.

    TEST = one whole session ('auto' = the smallest; ties by name). VAL = ~10% of the CAPTURES in the
    remaining sessions. Everything else trains. No capture, and no session, is ever on two sides.
    """
    real_rows = [e for e in manifest_rows if e.get("label") and _real_group(e).startswith("user_")]
    sessions = sorted({_real_group(e) for e in real_rows})
    if holdout_session == "auto":
        sizes = {g: sum(_real_group(e) == g for e in real_rows) for g in sessions}
        holdout_session = min(sizes, key=lambda g: (sizes[g], g)) if len(sessions) > 1 else None
    test_groups = {holdout_session} if holdout_session else set()
    train_rows = [e for e in real_rows if _real_group(e) not in test_groups]
    caps = sorted({_real_capture(e) for e in train_rows})
    random.Random(seed).shuffle(caps)
    val_caps = set(caps[: max(1, len(caps) // 10)]) if caps else set()
    return sessions, holdout_session, test_groups, val_caps


def _evaluate(model, samples, device, batch_size=64):
    """(string accuracy, CER, n) over a list of (img, label). Empty list -> (None, None, 0)."""
    if not samples:
        return None, None, 0
    import torch
    from torch.utils.data import DataLoader
    loader = DataLoader(_SynthDataset(samples), batch_size=batch_size, shuffle=False,
                        collate_fn=_collate, num_workers=0)
    correct, total, cer_sum = 0, 0, 0.0
    model.eval()
    with torch.no_grad():
        for padded, _tf, _il, _tl, labels in loader:
            log_probs = model(padded.to(device)).log_softmax(-1).cpu().numpy()
            for b, truth in enumerate(labels):
                pred = greedy_decode(log_probs[:, b, :])
                total += 1
                correct += pred == truth
                cer_sum += _cer(pred, truth)
    return correct / max(1, total), cer_sum / max(1, total), total


def train(
    epochs: int,
    n_samples: int,
    lr: float,
    batch_size: int,
    seed: int,
    real_aug_multiplier: int = 200,
    init_from: Optional[str] = None,
    size: str = "small",
    holdout_session: Optional[str] = "auto",
) -> None:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import DataLoader

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    # ── THE SPLIT, AND WHY IT CHANGED (2026-09-22) ───────────────────────────────────
    # It used to augment every real crop FIRST and split 90/10 SECOND, so each crop's copies landed
    # on BOTH sides and val scored the model on jittered versions of its own training images. And
    # ~84% of val was synthetic, so "val 93%" was mostly a font score. Now:
    #   TEST  = one whole held-out capture SESSION, un-augmented. Measured every epoch, NEVER used
    #           to pick the model. This is the honest "new evening of play" number.
    #   VAL   = held-out CAPTURES from the training sessions (un-augmented) + 10% of synthetic.
    #           Selection happens here; real and synthetic accuracy are printed separately.
    # --no-holdout restores training on every real crop (for a final production run, after the
    # honest number has been read).
    import json as _json
    manifest_rows = []
    if _CRNN_MANIFEST_PATH.is_file():
        manifest_rows = _json.load(open(_CRNN_MANIFEST_PATH)).get("files", [])
    sessions, holdout_session, test_groups, val_caps = _plan_real_split(manifest_rows, holdout_session, seed)

    print(f"Loading real labeled crops from {_CRNN_TRAINING_DIR}/...")
    print(f"  sessions: {sessions}  held-out TEST session: {holdout_session or 'NONE (--no-holdout)'}")
    real_train = _load_real_samples(real_aug_multiplier, exclude_groups=test_groups | val_caps,
                                    group_key=lambda e: _real_capture(e) if _real_capture(e) in val_caps else _real_group(e))
    real_val = _load_real_samples(0, include_groups=val_caps, group_key=_real_capture)
    real_test = _load_real_samples(0, include_groups=test_groups) if test_groups else []
    print(f"  real: train {len(real_train)} (augmented), val {len(real_val)} ({len(val_caps)} captures), "
          f"test {len(real_test)} (whole session, un-augmented)")

    print(f"Generating {n_samples} synthetic samples (seed={seed})...")
    synth_samples = generate_dataset(n=n_samples, seed=seed)
    random.Random(seed).shuffle(synth_samples)
    s_split = int(len(synth_samples) * 0.9)
    synth_train, synth_val = synth_samples[:s_split], synth_samples[s_split:]

    train_samples = real_train + synth_train
    random.Random(seed).shuffle(train_samples)
    val_samples = real_val + synth_val
    print(f"  total: train {len(train_samples)}, val {len(val_samples)} "
          f"({len(real_val)} real + {len(synth_val)} synth), test {len(real_test)} real")

    train_ds = _SynthDataset(train_samples)
    val_ds = _SynthDataset(val_samples)
    train_loader = DataLoader(
        train_ds, batch_size=batch_size, shuffle=True, collate_fn=_collate, num_workers=0,
    )
    val_loader = DataLoader(
        val_ds, batch_size=batch_size, shuffle=False, collate_fn=_collate, num_workers=0,
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = build_model(size=size)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"Model: {n_params:,} parameters ({size}) on {device}")

    if init_from:
        if not os.path.isfile(init_from):
            print(f"WARNING: --init-from {init_from} not found; starting from scratch")
        else:
            try:
                state = torch.load(init_from, map_location="cpu", weights_only=True)
                # Strict=False so mismatched heads (different vocab) are skipped
                missing, unexpected = model.load_state_dict(state, strict=False)
                print(f"Loaded pretrained weights from {init_from}")
                if missing:
                    print(f"  missing keys: {len(missing)}")
                if unexpected:
                    print(f"  unexpected keys: {len(unexpected)}")
            except Exception as exc:
                print(f"WARNING: failed to load {init_from}: {exc}; starting from scratch")

    model = model.to(device)
    criterion = nn.CTCLoss(blank=BLANK_IDX, zero_infinity=True)
    optimizer = optim.Adam(model.parameters(), lr=lr)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    print(f"Training for {epochs} epochs (lr={lr}, train={len(train_ds)}, val={len(val_ds)})")
    print("-" * 68)

    best_val_acc = -1.0
    best_real_val = best_test = best_test_cer = None
    best_epoch = 0
    best_state = None

    for epoch in range(epochs):
        model.train()
        train_loss_sum = 0.0
        n_batches = 0
        for padded, targets_flat, input_lens, target_lens, _labels in train_loader:
            padded = padded.to(device)
            optimizer.zero_grad()
            logits = model(padded)  # (T, B, C)
            log_probs = logits.log_softmax(-1).cpu()
            T = log_probs.shape[0]
            input_lens_c = input_lens.clamp(max=T)
            loss = criterion(log_probs, targets_flat, input_lens_c, target_lens)
            if not torch.isfinite(loss):
                continue
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()
            train_loss_sum += loss.item()
            n_batches += 1
        scheduler.step()

        # Validate
        model.eval()
        correct = 0
        total = 0
        cer_sum = 0.0
        with torch.no_grad():
            for padded, _tf, _il, _tl, labels in val_loader:
                padded = padded.to(device)
                logits = model(padded)  # (T, B, C)
                log_probs = logits.log_softmax(-1).cpu().numpy()
                for b, truth in enumerate(labels):
                    pred = greedy_decode(log_probs[:, b, :])
                    total += 1
                    if pred == truth:
                        correct += 1
                    cer_sum += _cer(pred, truth)

        val_acc = correct / max(1, total)
        val_cer = cer_sum / max(1, total)
        avg_loss = train_loss_sum / max(1, n_batches)
        rv_acc, _, rv_n = _evaluate(model, real_val, device)
        te_acc, te_cer, te_n = _evaluate(model, real_test, device)
        _pct = lambda a: "n/a" if a is None else f"{a*100:.1f}%"
        print(
            f"  Epoch {epoch+1:3d}/{epochs}: "
            f"loss={avg_loss:.3f}  val_str_acc={val_acc*100:.1f}%  val_cer={val_cer*100:.2f}%"
            f"  | real_val={_pct(rv_acc)} (n={rv_n})  TEST_real={_pct(te_acc)} (n={te_n})"
        )

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            best_real_val, best_test, best_test_cer, best_epoch = rv_acc, te_acc, te_cer, epoch + 1

    print("-" * 68)
    if best_state is None:
        print("No improvement — nothing to export.")
        return
    print(f"Best val string-accuracy: {best_val_acc*100:.2f}%  (epoch {best_epoch}; mixes real + synthetic)")
    if best_test is not None:
        print(f"HELD-OUT SESSION ({holdout_session}) at that epoch: {best_test*100:.2f}% string acc, "
              f"{best_test_cer*100:.2f}% CER over {len(real_test)} crops  <- the honest number")

    model.load_state_dict(best_state)
    model.eval()

    # Checkpoint the best weights BEFORE attempting ONNX export. The
    # torch ONNX exporter can fail on version/env mismatches (needs
    # onnxscript on torch>=2.5); if that happens, we want to keep the
    # trained weights so a user can retry export without a 5-minute
    # retrain.
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    import torch as _torch
    from datetime import datetime
    # Versioned checkpoint — timestamp + val acc baked into the filename.
    # The canonical model_crnn.pt gets overwritten every run (so the rest
    # of the pipeline picks up the latest), but every training run also
    # leaves a dated snapshot behind so we can always roll back to a
    # prior good run. Pair the .pt with a .json sidecar recording the
    # exact CLI args so the run can be reproduced later.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    val_tag = int(round(best_val_acc * 100))
    versioned_pt = _MODEL_DIR / f"model_crnn_{stamp}_val{val_tag:02d}.pt"
    versioned_json = _MODEL_DIR / f"model_crnn_{stamp}_val{val_tag:02d}.json"
    _torch.save(best_state, str(versioned_pt))
    try:
        import json as _json
        run_meta = {
            "stamp": stamp,
            "val_string_acc": float(best_val_acc),
            "val_real_string_acc": None if best_real_val is None else float(best_real_val),
            "test_holdout_session": holdout_session,
            "test_real_string_acc": None if best_test is None else float(best_test),
            "test_real_n": len(real_test),
            "split": "session-holdout + capture-level val, un-augmented eval (2026-09-22)",
            "epochs": epochs,
            "n_samples": n_samples,
            "lr": lr,
            "batch_size": batch_size,
            "seed": seed,
            "real_aug_multiplier": real_aug_multiplier,
            "init_from": init_from,
            "charClasses": CHAR_CLASSES,
            "numTokens": NUM_TOKENS,
            "inputHeight": CANVAS_H,
        }
        with open(versioned_json, "w") as f:
            _json.dump(run_meta, f, indent=2)
    except Exception:
        pass
    ckpt_path = _MODEL_DIR / "model_crnn.pt"
    _torch.save(best_state, str(ckpt_path))
    print(f"Versioned checkpoint: {versioned_pt}")
    print(f"Canonical checkpoint: {ckpt_path}")

    # Export to ONNX with dynamic width. Use the legacy TorchScript-
    # based exporter (dynamo=False) which doesn't require onnxscript
    # and is more stable for LSTM ops on opset 17.
    # Move model to CPU for export so the exported graph doesn't bake
    # in CUDA ops (which onnxruntime CPUExecutionProvider can't run)
    # and so dummy input doesn't need to be on GPU.
    model = model.cpu()
    dummy = _torch.randn(1, 1, CANVAS_H, 64)
    _torch.onnx.export(
        model, dummy,
        str(_OUT_ONNX),
        input_names=["input"],
        output_names=["logits"],
        dynamic_axes={
            "input": {0: "batch", 3: "width"},
            "logits": {0: "time", 1: "batch"},
        },
        opset_version=17,
        dynamo=False,
    )

    meta = {
        "charClasses": CHAR_CLASSES,
        "blankIdx": BLANK_IDX,
        "numTokens": NUM_TOKENS,
        "inputHeight": CANVAS_H,
        "valStringAcc": float(best_val_acc),
        "testRealStringAcc": None if best_test is None else float(best_test),
        "testHoldoutSession": holdout_session,
        "trainSamples": len(train_ds),
        "valSamples": len(val_ds),
        "modelKind": "crnn",
        "modelSize": size,
        "numParams": int(n_params),
    }
    with open(_OUT_META, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\nWrote {_OUT_ONNX}")
    print(f"Wrote {_OUT_META}")
    print("\nRestart the tool; sc_ocr will pick up the model automatically.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the value-crop CRNN")
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--n", type=int, default=8000, help="Synthetic samples to generate")
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--real-aug", type=int, default=200,
        help="Augmented copies per real crop (default 200)",
    )
    parser.add_argument(
        "--init-from", type=str, default=None,
        help="Pretrained state_dict to load before training (from ocr.pretrain_crnn)",
    )
    parser.add_argument(
        "--size", type=str, default="small", choices=("small", "large"),
        help="Model capacity: 'small' (1.3M, legacy) or 'large' (~5M)",
    )
    parser.add_argument(
        "--holdout-session", type=str, default="auto",
        help="Real capture SESSION held out as a pure test set ('auto' = the smallest). See train().",
    )
    parser.add_argument(
        "--out-dir", type=str, default=None,
        help="Write checkpoints/ONNX/meta HERE instead of ocr/models. Use for experiments and smoke "
             "tests: without it EVERY run, even a 1-epoch test, overwrites the production model.",
    )
    parser.add_argument(
        "--no-holdout", action="store_true",
        help="Train on every real crop (production run). The honest TEST number is then unavailable.",
    )
    args = parser.parse_args()

    if args.out_dir:
        # ⚠ Added 2026-09-22 after a 1-epoch smoke test replaced the production model_crnn.onnx/.pt/
        #   .json with a 19%-accuracy model (restored from a hashed backup). The export always wrote
        #   to ocr/models, so there was no way to train WITHOUT shipping the result.
        global _MODEL_DIR, _OUT_ONNX, _OUT_META
        _MODEL_DIR = Path(args.out_dir).resolve()
        _OUT_ONNX = _MODEL_DIR / "model_crnn.onnx"
        _OUT_META = _MODEL_DIR / "model_crnn.json"
        print(f"--out-dir: exporting to {_MODEL_DIR} (production ocr/models untouched)")

    # Force line-buffered stdout so per-epoch progress is visible when
    # the output is piped to a log file (default Python buffering only
    # flushes on process exit, which hides epoch-by-epoch progress).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        import torch  # noqa: F401
    except ImportError:
        print("ERROR: torch not installed. Run: pip install torch onnx", file=sys.stderr)
        sys.exit(1)

    train(
        epochs=args.epochs,
        n_samples=args.n,
        lr=args.lr,
        batch_size=args.batch_size,
        seed=args.seed,
        real_aug_multiplier=args.real_aug,
        init_from=args.init_from,
        size=args.size,
        holdout_session=None if args.no_holdout else args.holdout_session,
    )


if __name__ == "__main__":
    # Allow running as both `python -m ocr.train_crnn` and direct script
    if __package__ is None:
        sys.path.insert(0, str(_MODULE_DIR.parent))
    main()
