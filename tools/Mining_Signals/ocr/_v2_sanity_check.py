"""Sanity check the v2 RGB signal CNN: load the new ONNX model and run
inference on (a) one sample from each digit class, (b) the real icon
samples, (c) a digit (should NOT classify as @).

Also compares per-class accuracy to v1 on the digit-only data."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image


PROD_MODELS_DIR = Path(
    r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals\ocr\models"
)
SOURCE_TOOL_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)

V1_PATH = PROD_MODELS_DIR / "model_signal_rgb_cnn.onnx"
V2_PATH = PROD_MODELS_DIR / "model_signal_rgb_cnn_v2.onnx"
V2_META = PROD_MODELS_DIR / "model_signal_rgb_cnn_v2.json"

DIGITS_DIR = SOURCE_TOOL_DIR / "training_data_user_sig_rgb"
REAL_ICON_DIR = SOURCE_TOOL_DIR / "training_data_pending_review_signal" / "icon"
EXCLUDED_REAL = {"pending_cap_20260418_155503_607_rgb.png"}

CHAR_CLASSES_V2 = "0123456789@"
CHAR_CLASSES_V1 = "0123456789"


def load_rgb_28(p: Path) -> np.ndarray:
    arr = np.asarray(
        Image.open(p).convert("RGB").resize((28, 28), Image.BILINEAR),
        dtype=np.float32,
    ) / 255.0
    return arr.transpose(2, 0, 1)


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def main() -> int:
    if not V2_PATH.is_file():
        print(f"ERR: missing {V2_PATH}")
        return 1
    print(f"v2 ONNX: {V2_PATH}  ({V2_PATH.stat().st_size:,} bytes)")
    print(f"v2 META: {V2_META}")

    sess_v2 = ort.InferenceSession(
        str(V2_PATH), providers=["CPUExecutionProvider"]
    )
    in_v2 = sess_v2.get_inputs()[0].name
    print(f"v2 input: {in_v2} shape={sess_v2.get_inputs()[0].shape}")
    print(f"v2 output: shape={sess_v2.get_outputs()[0].shape}")

    sess_v1 = ort.InferenceSession(
        str(V1_PATH), providers=["CPUExecutionProvider"]
    )
    in_v1 = sess_v1.get_inputs()[0].name

    # ── (1) One sample from each digit class ──
    print("\n=== Per-digit sanity (v2) ===")
    for d in CHAR_CLASSES_V1:
        cls_dir = DIGITS_DIR / d
        files = sorted(cls_dir.glob("*.png"))
        if not files:
            continue
        x = load_rgb_28(files[0])[None, ...]
        logits_v2 = sess_v2.run(None, {in_v2: x})[0][0]
        probs_v2 = softmax(logits_v2)
        pred_idx = int(np.argmax(probs_v2))
        pred_ch = CHAR_CLASSES_V2[pred_idx]
        p_at = float(probs_v2[CHAR_CLASSES_V2.index("@")])
        ok = "OK" if pred_ch == d else "WRONG"
        print(
            f"  digit {d!r}  pred={pred_ch!r} conf={float(probs_v2[pred_idx]):.4f}  "
            f"p(@)={p_at:.4f}  [{ok}]"
        )

    # ── (2) Real icon samples ──
    print("\n=== Real icon sanity (v2) ===")
    real_files = sorted(REAL_ICON_DIR.glob("pending_*_rgb.png"))
    real_files = [f for f in real_files if f.name not in EXCLUDED_REAL]
    for src in real_files:
        x = load_rgb_28(src)[None, ...]
        logits = sess_v2.run(None, {in_v2: x})[0][0]
        probs = softmax(logits)
        pred_idx = int(np.argmax(probs))
        pred_ch = CHAR_CLASSES_V2[pred_idx]
        p_at = float(probs[CHAR_CLASSES_V2.index("@")])
        ok = "OK" if pred_ch == "@" else "WRONG"
        print(
            f"  {src.name}  pred={pred_ch!r} conf={float(probs[pred_idx]):.4f}  "
            f"p(@)={p_at:.4f}  [{ok}]"
        )

    # ── (3) The mislabeled real (should NOT be @) ──
    print("\n=== Mislabeled real (excluded from training; should be a digit) ===")
    excl_path = REAL_ICON_DIR / "pending_cap_20260418_155503_607_rgb.png"
    if excl_path.is_file():
        x = load_rgb_28(excl_path)[None, ...]
        logits = sess_v2.run(None, {in_v2: x})[0][0]
        probs = softmax(logits)
        pred_idx = int(np.argmax(probs))
        p_at = float(probs[CHAR_CLASSES_V2.index("@")])
        print(
            f"  {excl_path.name}  pred={CHAR_CLASSES_V2[pred_idx]!r} "
            f"conf={float(probs[pred_idx]):.4f}  p(@)={p_at:.4f}"
        )

    # ── (4) Per-class digit accuracy comparison v1 vs v2 ──
    print("\n=== Per-class digit accuracy v1 vs v2 (full digit folder) ===")
    print("  digit |  v1 acc  |  v2 acc  |  v2 mean p(@)")
    print("  ------+----------+----------+--------------")
    for d in CHAR_CLASSES_V1:
        cls_dir = DIGITS_DIR / d
        files = sorted(cls_dir.glob("*.png"))
        if not files:
            continue
        v1_correct = 0
        v2_correct = 0
        v2_p_at = 0.0
        for f in files:
            x = load_rgb_28(f)[None, ...]
            l1 = sess_v1.run(None, {in_v1: x})[0][0]
            p1 = softmax(l1)
            v1_pred = CHAR_CLASSES_V1[int(np.argmax(p1))]
            if v1_pred == d:
                v1_correct += 1
            l2 = sess_v2.run(None, {in_v2: x})[0][0]
            p2 = softmax(l2)
            v2_pred = CHAR_CLASSES_V2[int(np.argmax(p2))]
            if v2_pred == d:
                v2_correct += 1
            v2_p_at += float(p2[CHAR_CLASSES_V2.index("@")])
        n = len(files)
        print(
            f"  {d!r}     |  {v1_correct/n*100:5.1f}%  |  {v2_correct/n*100:5.1f}%  |  "
            f"{v2_p_at/n:.4f}"
        )

    # ── (5) Print the saved metadata for context ──
    print("\n=== v2 metadata ===")
    if V2_META.is_file():
        meta = json.loads(V2_META.read_text(encoding="utf-8"))
        for k, v in meta.items():
            if k == "notes":
                continue  # too long
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
