"""Signature-reader benchmark: run a CRNN model through the FULL signature pipeline on a pinned set
of real live captures, one fresh reader per crop, and compare against a saved baseline.

    python scripts/signature_ab_eval.py                          # current ocr/models/model_crnn.onnx vs baseline
    python scripts/signature_ab_eval.py --model path/to/x.onnx   # any candidate model
    python scripts/signature_ab_eval.py --write-baseline         # (re)record the baseline for the current model

WHY THE RESET MATTERS (2026-09-22): `_signal_recognize_pil` has deliberate hysteresis. `_STABLE_SIGNAL`
holds its value until 3 consecutive reads agree, which is right for the live app (one rock, many
frames) and WRONG for a benchmark of unrelated crops. Run without a reset, one crop's answer leaks
into the next: agreement with the capture-time reads read 123/220; with a reset it's 181/220. The
no-reset run also "found" a 21350 attractor bug that does not exist. So every crop gets
`_reset_consensus_buffers()`, the same reset the app does when a panel disappears.

WHY THE CROP LIST IS PINNED: live_samples/ keeps growing whenever the app runs with capture enabled
(.enabled sentinel), so a seeded random.sample over it silently picks different crops over time.
The baseline stores the exact 400 filenames; a missing file is reported, never skipped quietly.

WHAT THE NUMBERS MEAN, and don't:
  * `tag` = the value the LIVE reader wrote into the filename at capture time ("none" = it gave up).
    Agreement with it is agreement with the old live pipeline, NOT accuracy.
  * `verified` = values checked by eye against the image. The only ground truth here, and small.
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

SAMPLES = os.path.join(REPO, "live_samples")
BASELINE = os.path.join(HERE, "signature_ab_baseline_20260922.json")


def _session(model_path, meta_path):
    import onnxruntime as ort
    o = ort.SessionOptions()
    o.intra_op_num_threads = 1
    o.inter_op_num_threads = 1
    sess = ort.InferenceSession(model_path, sess_options=o, providers=["CPUExecutionProvider"])
    meta = json.load(open(meta_path)) if meta_path and os.path.isfile(meta_path) else {}
    return sess, meta


def run(model_path, meta_path, files):
    import logging
    logging.disable(logging.CRITICAL)
    from PIL import Image
    from ocr.sc_ocr import api, fallback
    if not fallback._ensure_crnn_model():
        raise SystemExit("the tool's own CRNN loader failed; nothing to benchmark")
    sess, meta = _session(model_path, meta_path)
    fallback._crnn_session = sess
    if meta:
        fallback._crnn_classes = meta.get("charClasses", fallback._crnn_classes)
        fallback._crnn_blank_idx = int(meta.get("blankIdx", len(fallback._crnn_classes)))
    reads, missing = {}, []
    for name in files:
        p = os.path.join(SAMPLES, name)
        if not os.path.isfile(p):
            missing.append(name)
            continue
        api._reset_consensus_buffers()
        api._LAST_SIGNAL_UNITS_HINT.clear()
        try:
            v = api._signal_recognize_pil(Image.open(p).convert("RGB"))
        except Exception as e:  # a crash is a result, not a skip
            v = "ERR:" + type(e).__name__
        reads[name] = "none" if v is None else str(v)
    return reads, missing


def tag_of(name):
    return name.rsplit("_", 1)[1][:-4]


def score(reads, verified):
    lab = [n for n in reads if tag_of(n) != "none"]
    unl = [n for n in reads if tag_of(n) == "none"]
    ver = [n for n in verified if n in reads]
    return {
        "n": len(reads),
        "agree_with_capture_read": [sum(reads[n] == tag_of(n) for n in lab), len(lab)],
        "recovered_on_none": [sum(reads[n] != "none" for n in unl), len(unl)],
        "gave_up_on_read_crops": [sum(reads[n] == "none" for n in lab), len(lab)],
        "verified_correct": [sum(reads[n] == verified[n] for n in ver), len(ver)],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.path.join(REPO, "ocr", "models", "model_crnn.onnx"))
    ap.add_argument("--meta", default=None, help="defaults to the .json beside --model")
    ap.add_argument("--baseline", default=BASELINE)
    ap.add_argument("--write-baseline", action="store_true")
    a = ap.parse_args()
    meta = a.meta or os.path.splitext(a.model)[0] + ".json"
    base = json.load(open(a.baseline, encoding="utf-8"))
    files, verified = base["files"], base.get("verified", {})

    t0 = time.time()
    reads, missing = run(a.model, meta, files)
    s = score(reads, verified)
    print("model   : %s" % os.path.relpath(a.model, REPO))
    print("crops   : %d of %d pinned (%.0fs)%s" % (s["n"], len(files), time.time() - t0,
          ("  ⚠ MISSING %d: %s" % (len(missing), missing[:5])) if missing else ""))
    for k in ("agree_with_capture_read", "recovered_on_none", "gave_up_on_read_crops", "verified_correct"):
        print("  %-24s %d/%d" % (k, *s[k]))

    if a.write_baseline:
        base["reads"] = reads
        base["score"] = s
        base["model"] = os.path.relpath(a.model, REPO)
        json.dump(base, open(a.baseline, "w", encoding="utf-8"), indent=1)
        print("baseline written -> %s" % os.path.relpath(a.baseline, REPO))
        return 0

    prev = base.get("reads", {})
    changed = [n for n in reads if n in prev and reads[n] != prev[n]]
    print("\nvs baseline (%s): %d crop(s) changed" % (base.get("model", "?"), len(changed)))
    for n in changed:
        mark = ""
        if n in verified:
            mark = "  ✓ now right" if reads[n] == verified[n] else ("  ✗ now WRONG" if prev[n] == verified[n] else "  (both wrong)")
        print("  %-32s tag=%-6s base=%-6s now=%s%s" % (n, tag_of(n), prev[n], reads[n], mark))
    print("\n== signature_ab_eval COMPLETE (rc=%d) ==" % (1 if missing else 0))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
