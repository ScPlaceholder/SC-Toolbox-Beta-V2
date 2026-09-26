"""hud_config_sim.py — simulate different HUD configurations and test read robustness.

Different user setups (resolution, HUD scale, brightness/gamma, display sharpness) change
what the scanner sees. This applies a MATRIX of those transforms to the labeled glyph set
and runs the glyph classifier on each, so we can see where the reader holds up and where it
starts to break — WITHOUT needing to physically reconfigure a game client at each resolution.

Scope (honest): this exercises the CLASSIFIER (the part that reads a segmented glyph), which
is what runs headlessly. The SEGMENTER (finding + cutting glyphs out of the live HUD at a given
resolution) is Tesseract-gated in the full pipeline and needs the runtime / live captures to
test end-to-end. So a clean pass here means "the model is robust to these configs"; the
segmenter's robustness is the remaining live-test piece.

    python hud_config_sim.py
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import onnxruntime as ort
from PIL import Image, ImageEnhance, ImageFilter

HERE = Path(__file__).resolve().parent
MODEL = HERE / "ocr" / "models" / "model_cnn.onnx"
GLYPHS = HERE / "training_data_hud_glyphs_extracted"
CHARS = "0123456789.%"
DIRMAP = {**{str(i): i for i in range(10)}, "dot": 10, "pct": 11}


def load_glyphs():
    X, y = [], []
    for dn, ci in DIRMAP.items():
        d = GLYPHS / dn
        if not d.is_dir():
            continue
        for f in d.glob("*.png"):
            X.append(Image.open(f).convert("L"))
            y.append(ci)
    return X, np.array(y)


# --- HUD configuration transforms (each simulates a real user-setup axis) ---
def cfg_scale(im, s):
    """Resolution / HUD-scale: round-trip through a different pixel size."""
    w = max(4, int(28 * s))
    return im.resize((w, w), Image.BILINEAR)


def cfg_brightness(im, b):
    return ImageEnhance.Brightness(im).enhance(b)


def cfg_gamma(im, g):
    a = np.asarray(im, dtype=np.float32) / 255.0
    return Image.fromarray((np.power(a, g) * 255).astype(np.uint8))


def cfg_sharpness(im, k):
    if k < 1.0:
        return im.filter(ImageFilter.GaussianBlur(max(0.1, (1 - k) * 2)))
    return ImageEnhance.Sharpness(im).enhance(k)


def cfg_contrast(im, c):
    return ImageEnhance.Contrast(im).enhance(c)


CONFIGS = [
    ("baseline", lambda im: im),
    ("res 0.4x (small HUD / low res)", lambda im: cfg_scale(im, 0.4)),
    ("res 0.6x", lambda im: cfg_scale(im, 0.6)),
    ("res 1.5x (large HUD / 4K)", lambda im: cfg_scale(im, 1.5)),
    ("res 2.0x", lambda im: cfg_scale(im, 2.0)),
    ("bright 0.6x (dim display)", lambda im: cfg_brightness(im, 0.6)),
    ("bright 1.5x (bright display)", lambda im: cfg_brightness(im, 1.5)),
    ("gamma 0.6 (raised)", lambda im: cfg_gamma(im, 0.6)),
    ("gamma 1.8 (crushed)", lambda im: cfg_gamma(im, 1.8)),
    ("soft (blur / low sharpness)", lambda im: cfg_sharpness(im, 0.4)),
    ("low contrast 0.6x", lambda im: cfg_contrast(im, 0.6)),
    ("stress: 0.5x + dim + blur", lambda im: cfg_sharpness(cfg_brightness(cfg_scale(im, 0.5), 0.7), 0.5)),
]


def main():
    if not MODEL.is_file():
        print("model_cnn.onnx not found"); return 1
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    nm = sess.get_inputs()[0].name
    X, y = load_glyphs()
    print(f"HUD config simulation — {len(X)} labeled glyphs x {len(CONFIGS)} configs\n")
    print(f"  {'config':34s} {'overall':>8s} {'digits':>8s} {'decimal':>8s}")
    for name, fn in CONFIGS:
        tot = hit = dtot = dhit = digt = digh = 0
        for im, ci in zip(X, y):
            t = fn(im).resize((28, 28), Image.BILINEAR)
            a = (np.asarray(t, dtype=np.float32) / 255.0)[None, None, :, :]
            p = int(np.argmax(sess.run(None, {nm: a})[0][0]))
            tot += 1; hit += (p == ci)
            if ci == 10:
                dtot += 1; dhit += (p == 10)
            elif ci < 10:
                digt += 1; digh += (p == ci)
        ov = 100 * hit / tot
        dg = 100 * digh / digt if digt else 0
        dc = 100 * dhit / dtot if dtot else 0
        flag = "" if ov >= 95 else ("  <- degraded" if ov >= 85 else "  <- BREAKS")
        print(f"  {name:34s} {ov:7.1f}% {dg:7.1f}% {dc:7.0f}%{flag}")
    print("\nNote: this tests the CLASSIFIER's config-robustness. The SEGMENTER (panel-find +"
          "\nglyph-cut at a live resolution) is the remaining piece and needs runtime/live captures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
