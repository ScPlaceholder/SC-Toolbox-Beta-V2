"""Smoke-test v2 RGB CNN: real icon, digits, ensure correct classification.

Loads the freshly-exported model_signal_rgb_cnn_v2.onnx via onnxruntime
and runs it against:
  1. A real RGB icon (training_data_pending_review_signal/icon/pending_*_rgb.png)
  2. A digit '5' from the training pool
  3. All 6 real RGB icons (since they're scarce, we want to know each)
  4. One sample from each digit class
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

ROOT = Path(
    r"C:\Users\prjgn\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)
MODEL_V2 = ROOT / "ocr" / "models" / "model_signal_rgb_cnn_v2.onnx"
MODEL_V1 = ROOT / "ocr" / "models" / "model_signal_rgb_cnn.onnx"
RGB_TRAIN = ROOT / "training_data_user_sig_rgb"
REAL_ICONS = ROOT / "training_data_pending_review_signal" / "icon"

CHAR_CLASSES = "0123456789@"


def load_rgb_28(path: Path) -> np.ndarray:
    pil = Image.open(path).convert("RGB")
    if pil.size != (28, 28):
        pil = pil.resize((28, 28), Image.LANCZOS)
    arr = np.asarray(pil, dtype=np.float32) / 255.0  # (H, W, 3)
    arr = arr.transpose(2, 0, 1)  # (3, H, W)
    return arr[None, :, :, :]  # (1, 3, H, W)


def softmax(logits: np.ndarray) -> np.ndarray:
    e = np.exp(logits - logits.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def classify(sess: ort.InferenceSession, x: np.ndarray) -> tuple[str, float, list[tuple[str, float]]]:
    out = sess.run(None, {"input": x})[0]  # (1, 11)
    probs = softmax(out)[0]
    top = int(np.argmax(probs))
    top_char = CHAR_CLASSES[top] if top < len(CHAR_CLASSES) else f"?{top}"
    # Top-3 for debugging
    top3 = np.argsort(probs)[-3:][::-1]
    top3_list = [(CHAR_CLASSES[i] if i < len(CHAR_CLASSES) else f"?{i}",
                  float(probs[i])) for i in top3]
    return top_char, float(probs[top]), top3_list


def main() -> int:
    if not MODEL_V2.exists():
        print(f"[!] missing {MODEL_V2}")
        return 1

    print(f"[load] {MODEL_V2.name}")
    sess = ort.InferenceSession(str(MODEL_V2), providers=["CPUExecutionProvider"])
    print(f"  inputs:  {[i.name + ' ' + str(i.shape) for i in sess.get_inputs()]}")
    print(f"  outputs: {[o.name + ' ' + str(o.shape) for o in sess.get_outputs()]}")
    print()

    # 1. All 6 real RGB icons
    print("=== 6 real RGB icons (from pending_review_signal/icon) ===")
    real_icons = sorted(REAL_ICONS.glob("pending_*_rgb.png"))
    n_correct_icon = 0
    for src in real_icons:
        x = load_rgb_28(src)
        ch, conf, top3 = classify(sess, x)
        ok = "OK" if ch == "@" else "FAIL"
        if ch == "@":
            n_correct_icon += 1
        print(f"  {src.name[:50]:50s}  -> {ch!r}  conf={conf:.3f}  [{ok}]  top3={top3}")
    print(f"  {n_correct_icon}/{len(real_icons)} real icons classified as @")
    print()

    # 2. One sample per digit class from training_data_user_sig_rgb
    print("=== One sample per digit class ===")
    n_correct_digit = 0
    n_total = 0
    for d in "0123456789":
        cls_dir = RGB_TRAIN / d
        samples = sorted(cls_dir.glob("*.png"))
        if not samples:
            continue
        # Pick first one
        src = samples[0]
        x = load_rgb_28(src)
        ch, conf, top3 = classify(sess, x)
        ok = "OK" if ch == d else "FAIL"
        if ch == d:
            n_correct_digit += 1
        n_total += 1
        print(f"  {d}/{src.name[:36]:36s} -> {ch!r}  conf={conf:.3f}  [{ok}]")
    print(f"  {n_correct_digit}/{n_total} digit samples classified correctly")
    print()

    # 3. Synthetic test: also pick one colorized augmentation, since
    #    that's the bulk of the @ training data (600/606), and confirm.
    print("=== Synthetic colorized icon (1 from training pool) ===")
    icon_dir = RGB_TRAIN / "icon"
    icon_samples = sorted(icon_dir.glob("aug_bad_crop_*_rgb.png"))[:3]
    for src in icon_samples:
        x = load_rgb_28(src)
        ch, conf, top3 = classify(sess, x)
        ok = "OK" if ch == "@" else "FAIL"
        print(f"  {src.name[:42]:42s} -> {ch!r}  conf={conf:.3f}  [{ok}]")
    print()

    # 4. Compare to v1 on the same real icons - what does v1 say?
    if MODEL_V1.exists():
        print("=== v1 (10-class digits-only) vs v2 (11-class) on real icons ===")
        sess_v1 = ort.InferenceSession(
            str(MODEL_V1), providers=["CPUExecutionProvider"]
        )
        for src in real_icons:
            x = load_rgb_28(src)
            # v1 has only 10 classes (digits), no @
            out1 = sess_v1.run(None, {"input": x})[0]
            probs1 = softmax(out1)[0]
            top1 = int(np.argmax(probs1))
            ch_v1 = "0123456789"[top1]
            conf_v1 = float(probs1[top1])
            ch_v2, conf_v2, _ = classify(sess, x)
            print(
                f"  {src.name[:36]:36s}  v1->{ch_v1!r} ({conf_v1:.3f})   "
                f"v2->{ch_v2!r} ({conf_v2:.3f})"
            )
    print()

    print("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
