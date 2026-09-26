"""HUD value benchmark: J's hand-labelled HUD crops through the PRODUCTION reader, `_ocr_value_crop`,
with the generic CRNN (model_crnn) swapped between candidates. One fresh reader per crop.

    python scripts/hud_value_eval.py                                  # current model_crnn.onnx
    python scripts/hud_value_eval.py --model a.onnx --model b.onnx    # compare several

WHY END-TO-END (2026-09-22): the CRNN is one voter behind gates (a HUD-RGB CRNN runs first, then this
model behind a 0.95-confidence gate, then Tesseract). A model's own accuracy on crops says nothing
about whether it ever reaches the user. Measured the same night: the old model scored 0/377 on these
crops through the raw decode; the reader's answer is what matters.

⚠ CONTAMINATION: every model trained on training_data_crnn/manifest.json has SEEN these crops. For
such a model, only the held-out session (see train_crnn._plan_real_split) is an honest number, and
only if the model was trained with that session held out. The script prints both, labelled.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _num(s):
    """The value a reading means: '00', '0.00' and '0' are all 0; '1%' is 1. None if unparseable."""
    s = (s or "").strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def value_ok(text, label):
    """⚠ Score by VALUE, not string (2026-09-22). The HUD shows '0.00' where J labelled '0', and the
    reader returns '00': the same number. Exact-string scoring called the reader 56% right when it
    was 73% right by value, and that wrong number went to J before I looked at a crop."""
    a, b = _num(text), _num(label)
    return a is not None and b is not None and abs(a - b) < 1e-9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", default=None,
                    help="onnx path (repeatable); meta = the .json beside it")
    a = ap.parse_args()
    models = a.model or [os.path.join(REPO, "ocr", "models", "model_crnn.onnx")]

    import logging
    logging.disable(logging.CRITICAL)
    import onnxruntime as ort
    from PIL import Image
    from ocr import train_crnn as T
    from ocr.sc_ocr import api, fallback

    rows = json.load(open(T._CRNN_MANIFEST_PATH))["files"]
    _, holdout, test_groups, _ = T._plan_real_split(rows, "auto", 42)
    real = [e for e in rows if e.get("label") and T._real_group(e).startswith("user_")]
    if not fallback._ensure_crnn_model():
        raise SystemExit("the tool's own CRNN loader failed; nothing to benchmark")

    for mp in models:
        meta = json.load(open(os.path.splitext(mp)[0] + ".json"))
        o = ort.SessionOptions()
        o.intra_op_num_threads = 1
        fallback._crnn_session = ort.InferenceSession(mp, sess_options=o, providers=["CPUExecutionProvider"])
        fallback._crnn_classes = meta.get("charClasses", fallback._crnn_classes)
        fallback._crnn_blank_idx = int(meta.get("blankIdx", len(fallback._crnn_classes)))
        t0, res, exact = time.time(), {"held-out session": [0, 0], "all real": [0, 0]}, 0
        by_field = {}
        for e in real:
            api._reset_consensus_buffers()
            img = Image.open(str(T._CRNN_TRAINING_DIR / e["path"])).convert("RGB")
            try:
                text, _ = api._ocr_value_crop(img, field=e.get("field", ""))
            except Exception as ex:  # a crash is a result
                text = "ERR:" + type(ex).__name__
            ok = value_ok(text, e["label"])
            exact += (text or "").strip() == e["label"]
            res["all real"][0] += ok
            res["all real"][1] += 1
            f = by_field.setdefault(e.get("field", "?"), [0, 0])
            f[0] += ok
            f[1] += 1
            if T._real_group(e) in test_groups:
                res["held-out session"][0] += ok
                res["held-out session"][1] += 1
        print("model: %s  (%s, %.0fs)" % (os.path.relpath(mp, REPO), meta.get("valStringAcc", "?"), time.time() - t0))
        for k, (ok, n) in res.items():
            print("  %-18s %d/%d = %.1f%%" % (k, ok, n, 100.0 * ok / max(1, n)))
        print("  (scored by VALUE; exact-string matches: %d/%d)" % (exact, res["all real"][1]))
        print("  by field: " + ", ".join("%s %d/%d" % (k, v[0], v[1]) for k, v in sorted(by_field.items())))
        print("  held-out session = %s. Honest ONLY for a model trained with it held out." % holdout)
    print("== hud_value_eval COMPLETE (rc=0) ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
