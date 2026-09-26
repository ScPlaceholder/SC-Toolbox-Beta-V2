"""ONNX CNN fallback for low-confidence template matches.

Keeps the shipped ``ocr/models/model_cnn.onnx`` model around as a
safety net. Only invoked when a glyph's NCC confidence is below
``FALLBACK_CONF_THRESHOLD`` or the top-two template scores are
within a small ambiguity gap.

Model-path priority order at load time (``_ensure_model``):

  1. ``%LOCALAPPDATA%/SC_Toolbox/model_cnn_online.onnx`` —
     online-learner-trained model, if present.
  2. ``ocr/models/model_hud_cnn.onnx`` — HUD-specific 12-class CNN
     trained by ``ocr/train_hud_cnn.py``. PREFERRED when present:
     this loader feeds ``_classify_crops`` in ``api.py`` which is
     the HUD primary per-glyph path. The HUD font visually differs
     from the signature font, so the HUD CNN must NEVER be
     substituted for the signature CNN — that model has its own
     session (``_signal_session`` in ``ocr/sc_ocr/api.py``) and
     never touches this loader.
  3. ``ocr/models/model_cnn.onnx`` — the shipped legacy model
     (no provenance info; trained against a similar 12-class
     alphabet but with no documented HUD/Signature isolation).
     Used only if neither (1) nor (2) is on disk.

Metadata sidecar (``model_*.json``) — read from beside the model
file, so the alphabet matches whichever model was loaded.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

from .config import (
    ONNX_MODEL_PATH, ONNX_MODEL_PATH_INV,
    CRNN_MODEL_PATH, CRNN_META_PATH,
    CRNN2_MODEL_PATH, CRNN2_META_PATH,
)

log = logging.getLogger(__name__)

# Lazy-loaded 28×28 single-glyph classifier (safety-net fallback).
_session = None
_char_classes: str = "0123456789.-%"

# Optional polarity-INVERTED 28×28 classifier (secondary voter).
# Trained on the same crops as ``_session`` but with each pixel
# inverted (255 - p), so it expects DARK text on LIGHT background.
# When present, ``api._ocr_value_crop`` feeds it the inverted version
# of the primary path's crops — different polarity AND different
# weights → maximally decorrelated peer voter.  Gracefully absent
# until trained; see scripts/make_inverted_dataset.py + train_model.py
# --inverted.
_session_inv = None
_char_classes_inv: str = "0123456789.-%"
_session_inv_tried: bool = False

# Lazy-loaded end-to-end CRNN (primary value-crop recognizer).
# Default alphabet matches the expanded set used by synth_data /
# train_crnn / pretrain_crnn. Overwritten from model_crnn.json at
# load time if present — this default only matters when no manifest
# is present on disk.
_crnn_session = None
_crnn_classes: str = "0123456789.-% ()ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_crnn_blank_idx: int = len(_crnn_classes)
_crnn_input_height: int = 32

# Optional secondary CRNN (ensemble partner). Same schema as the
# primary. Present when model_crnn_v2.onnx exists on disk; absent
# otherwise (gracefully degrades to single-CRNN behaviour).
_crnn2_session = None
_crnn2_classes: str = ""
_crnn2_blank_idx: int = -1
_crnn2_input_height: int = 32
_crnn2_tried: bool = False  # set True after first load attempt

_ONLINE_MODEL_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "SC_Toolbox", "model_cnn_online.onnx",
)


def _resolve_hud_cnn_path() -> str:
    """Pick the HUD-primary CNN file with the priority documented in
    this module's docstring.

    Importing the training registry is best-effort; if it isn't on
    sys.path for some reason (e.g. minimal runtime ship), the helper
    falls back to ``ONNX_MODEL_PATH`` so the production cascade keeps
    working with the shipped legacy ``model_cnn.onnx``.
    """
    if os.path.isfile(_ONLINE_MODEL_PATH):
        return _ONLINE_MODEL_PATH
    # HUD-specific CNN (preferred over the legacy generic model_cnn.onnx).
    try:
        from ..training_registry import get_runtime_model_path
        hud_path = str(get_runtime_model_path("hud"))
        if os.path.isfile(hud_path):
            return hud_path
    except Exception as exc:  # pragma: no cover — defensive
        log.debug(
            "sc_ocr.fallback: registry lookup for HUD CNN failed: %s", exc,
        )
    # Legacy generic shipped model.
    return str(ONNX_MODEL_PATH)


def _ensure_model() -> bool:
    global _session, _char_classes
    if _session is not None:
        return True

    path = _resolve_hud_cnn_path()
    if not os.path.isfile(path):
        log.debug("sc_ocr.fallback: ONNX model not found at %s", path)
        return False

    try:
        import onnxruntime as ort
    except ImportError:
        log.debug("sc_ocr.fallback: onnxruntime not installed")
        return False

    try:
        import json
        # Sidecar JSON lives next to whichever model file we loaded.
        # ``model_hud_cnn.onnx`` → ``model_hud_cnn.json``, etc.
        base = os.path.splitext(os.path.basename(path))[0]
        meta_path = os.path.join(os.path.dirname(path), f"{base}.json")
        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
                _char_classes = meta.get("charClasses", _char_classes)
        else:
            # Older shipped model_cnn.onnx ships with model_cnn.json — same
            # filename stem, just keep the original lookup as the fallback.
            legacy = os.path.join(os.path.dirname(path), "model_cnn.json")
            if os.path.isfile(legacy):
                with open(legacy) as f:
                    meta = json.load(f)
                    _char_classes = meta.get("charClasses", _char_classes)

        # Single-threaded to respect the 7% CPU budget
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        _session = ort.InferenceSession(
            path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        log.info(
            "sc_ocr.fallback: ONNX loaded (%s, classes=%r)",
            os.path.basename(path), _char_classes,
        )
        return True
    except Exception as exc:
        log.warning("sc_ocr.fallback: ONNX load failed: %s", exc)
        return False


def _ensure_model_inv() -> bool:
    """Lazy-load the optional polarity-inverted 28×28 classifier.

    Mirrors ``_ensure_model`` against ``ONNX_MODEL_PATH_INV``.  If the
    inverted model isn't on disk, returns False once and short-circuits
    on every subsequent call (``_session_inv_tried`` stays True) so we
    don't repeatedly stat() a missing file in the hot inference path.
    """
    global _session_inv, _char_classes_inv, _session_inv_tried
    if _session_inv is not None:
        return True
    if _session_inv_tried:
        return False
    _session_inv_tried = True

    path = str(ONNX_MODEL_PATH_INV)
    if not os.path.isfile(path):
        log.debug("sc_ocr.fallback: inverted ONNX not found at %s", path)
        return False

    try:
        import onnxruntime as ort
    except ImportError:
        log.debug("sc_ocr.fallback: onnxruntime not installed (inv)")
        return False

    try:
        import json
        meta_path = os.path.join(os.path.dirname(path), "model_cnn_inv.json")
        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
                _char_classes_inv = meta.get("charClasses", _char_classes_inv)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        _session_inv = ort.InferenceSession(
            path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        log.info("sc_ocr.fallback: inverted ONNX loaded (%s)",
                 os.path.basename(path))
        return True
    except Exception as exc:
        log.warning("sc_ocr.fallback: inverted ONNX load failed: %s", exc)
        return False


def _ensure_crnn_model() -> bool:
    """Lazy-load the value-crop CRNN.

    Follows the same pattern as ``_ensure_model``: returns False
    gracefully if the model file is missing or onnxruntime is not
    installed, so the Tesseract + 28×28 classifier fallback path in
    ``api._ocr_value_crop`` can still serve reads.
    """
    global _crnn_session, _crnn_classes, _crnn_blank_idx, _crnn_input_height
    if _crnn_session is not None:
        return True

    path = str(CRNN_MODEL_PATH)
    if not os.path.isfile(path):
        log.debug("sc_ocr.fallback: CRNN model not found at %s", path)
        return False

    try:
        import onnxruntime as ort
    except ImportError:
        log.debug("sc_ocr.fallback: onnxruntime not installed (CRNN)")
        return False

    try:
        import json
        if os.path.isfile(CRNN_META_PATH):
            with open(CRNN_META_PATH) as f:
                meta = json.load(f)
            _crnn_classes = meta.get("charClasses", _crnn_classes)
            _crnn_blank_idx = int(meta.get("blankIdx", len(_crnn_classes)))
            _crnn_input_height = int(meta.get("inputHeight", 32))
        else:
            _crnn_blank_idx = len(_crnn_classes)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        _crnn_session = ort.InferenceSession(
            path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        log.info(
            "sc_ocr.fallback: CRNN loaded (%s, classes=%r, H=%d)",
            os.path.basename(path), _crnn_classes, _crnn_input_height,
        )
        return True
    except Exception as exc:
        log.warning("sc_ocr.fallback: CRNN load failed: %s", exc)
        return False


def _ensure_crnn2_model() -> bool:
    """Lazy-load an optional secondary CRNN for ensembling.

    Mirrors ``_ensure_crnn_model`` exactly, but against the
    ``model_crnn_v2.*`` files. If the v2 model isn't present on
    disk, returns False and sets ``_crnn2_tried`` so subsequent
    calls short-circuit without repeated stat() churn.
    """
    global _crnn2_session, _crnn2_classes, _crnn2_blank_idx
    global _crnn2_input_height, _crnn2_tried
    if _crnn2_session is not None:
        return True
    if _crnn2_tried:
        return False
    _crnn2_tried = True

    path = str(CRNN2_MODEL_PATH)
    if not os.path.isfile(path):
        log.debug("sc_ocr.fallback: CRNN v2 not found at %s", path)
        return False

    try:
        import onnxruntime as ort
    except ImportError:
        return False

    try:
        import json
        if os.path.isfile(CRNN2_META_PATH):
            with open(CRNN2_META_PATH) as f:
                meta = json.load(f)
            _crnn2_classes = meta.get("charClasses", _crnn_classes)
            _crnn2_blank_idx = int(meta.get("blankIdx", len(_crnn2_classes)))
            _crnn2_input_height = int(meta.get("inputHeight", 32))
        else:
            _crnn2_classes = _crnn_classes
            _crnn2_blank_idx = len(_crnn2_classes)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        _crnn2_session = ort.InferenceSession(
            path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        log.info(
            "sc_ocr.fallback: CRNN v2 loaded (%s, classes=%r, H=%d)",
            os.path.basename(path), _crnn2_classes, _crnn2_input_height,
        )
        return True
    except Exception as exc:
        log.warning("sc_ocr.fallback: CRNN v2 load failed: %s", exc)
        return False


def classify_glyph(crop_28: np.ndarray) -> Optional[tuple[str, float]]:
    """Run a single 28x28 glyph through the ONNX CNN.

    Expects a uint8 or float32 grayscale image, shape (28, 28),
    with text BRIGHT on a dark background (post-preprocess).
    Returns (char, confidence) or None if the model isn't loaded.
    """
    if not _ensure_model():
        return None

    if crop_28.shape != (28, 28):
        return None

    # Normalize to [0, 1] float32 (ONNX model's input convention)
    arr = crop_28.astype(np.float32)
    if arr.max() > 1.0:
        arr = arr / 255.0
    arr = arr.reshape(1, 1, 28, 28)

    try:
        inp_name = _session.get_inputs()[0].name
        logits = _session.run(None, {inp_name: arr})[0][0]  # (13,)
        # Softmax
        logits = logits - logits.max()
        exp = np.exp(logits)
        probs = exp / exp.sum()
        idx = int(np.argmax(probs))
        return _char_classes[idx], float(probs[idx])
    except Exception as exc:
        log.debug("sc_ocr.fallback: inference failed: %s", exc)
        return None
