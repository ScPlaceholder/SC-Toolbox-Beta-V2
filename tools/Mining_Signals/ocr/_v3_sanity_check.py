"""Sanity-check v3 RGB CNN against panel-font + signature-font samples.

For each digit class 0-9 (focusing on 0,1,5,7,8 per task), pick 5
PANEL samples and 5 SIGNATURE samples, run them through v3, and
report classification + confidence.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

PROD = Path(r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals")
WINGMAN = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)

V3_PATH = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v3.onnx"
V2_PATH = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v2.onnx"

SIG_DIR = WINGMAN / "training_data_user_sig_rgb"
PAN_DIR = WINGMAN / "training_data_user_panel"

CHAR_CLASSES = "0123456789@"


def load_session(path: Path) -> ort.InferenceSession:
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])


def to_input(im: Image.Image) -> np.ndarray:
    if im.mode != "RGB":
        im = im.convert("RGB")
    if im.size != (28, 28):
        im = im.resize((28, 28), Image.BILINEAR)
    arr = np.asarray(im, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)[None, ...]
    return arr.astype(np.float32)


def classify(sess: ort.InferenceSession, im: Image.Image) -> tuple[str, float]:
    x = to_input(im)
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    logits = out[0]
    e = np.exp(logits - logits.max())
    p = e / e.sum()
    idx = int(np.argmax(p))
    return CHAR_CLASSES[idx], float(p[idx])


def sample_class(class_dir: Path, n: int = 5) -> list[Path]:
    files = sorted(class_dir.glob("*.png"))
    if len(files) <= n:
        return files
    step = max(1, len(files) // n)
    return [files[i * step] for i in range(n)]


def main() -> int:
    if not V3_PATH.exists():
        print(f"v3 not found: {V3_PATH}")
        return 1

    print(f"Loading v3 from {V3_PATH}")
    v3 = load_session(V3_PATH)

    classes_to_check = ["0", "1", "5", "7", "8"]

    summary = {"v3": {}}
    for ch in classes_to_check:
        sig_files = sample_class(SIG_DIR / ch, n=5)
        pan_files = sample_class(PAN_DIR / ch, n=5)
        sig_correct = 0
        pan_correct = 0
        sig_conf = []
        pan_conf = []

        print(f"\n=== class '{ch}' ===")
        print("  signature samples:")
        for f in sig_files:
            im = Image.open(f)
            cls, conf = classify(v3, im)
            ok = (cls == ch)
            sig_correct += int(ok)
            sig_conf.append(conf if ok else 0.0)
            mark = "OK " if ok else "WRONG"
            print(f"    [{mark}] {f.name} -> '{cls}' p={conf:.3f}")
        print("  panel samples:")
        for f in pan_files:
            im = Image.open(f)
            cls, conf = classify(v3, im)
            ok = (cls == ch)
            pan_correct += int(ok)
            pan_conf.append(conf if ok else 0.0)
            mark = "OK " if ok else "WRONG"
            print(f"    [{mark}] {f.name} -> '{cls}' p={conf:.3f}")

        avg_sig = float(np.mean(sig_conf)) if sig_conf else 0.0
        avg_pan = float(np.mean(pan_conf)) if pan_conf else 0.0
        summary["v3"][ch] = {
            "sig_correct": f"{sig_correct}/{len(sig_files)}",
            "pan_correct": f"{pan_correct}/{len(pan_files)}",
            "sig_conf_avg": round(avg_sig, 3),
            "pan_conf_avg": round(avg_pan, 3),
        }

    print("\n=== summary ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
