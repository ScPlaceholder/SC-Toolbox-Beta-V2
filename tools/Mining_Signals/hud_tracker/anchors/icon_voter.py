"""Hierarchical multi-voter for the SC mining HUD location-pin icon.

Replaces the strict AND-gate that previously sat inside
``_cnn_filter_icon_candidates``. The strict gate let the grayscale CNN
veto every candidate (including the real icon), causing the pipeline
to fall back to its temporal cache and stale anchors.

The voter splits the decision into TIERS:

  * **Primary** (decorrelated structural detectors):
      1. ``find_icon_by_geometry``  -- color + structure (HSV warm
         mask + teardrop / oval / notch checks).
      2. ``find_icon_by_contour``   -- luma + edge contour matched
         against the canonical icon silhouette. Built by a parallel
         agent; imported defensively.

  * **Secondary** (RGB CNN with ``@`` class) -- consulted only when
    primaries disagree. ``model_signal_rgb_cnn_v2.onnx``. Trained by
    a parallel agent; loaded defensively.

  * **Tertiary** (existing grayscale CNN with ``@`` class) -- the
    classifier that used to be the single gate. Now it gets ONE vote
    of four; no longer a precondition.

Decision tree (``vote_on_icon_candidate``):

    geom_says   = primary 1
    contour_says = primary 2

    if both agree YES:           accept
    if both agree NO:            reject

    # primaries disagreed -- consult RGB CNN
    rgb_at_prob = rgb_cnn(crop)['@']
    if available:
        if rgb_at_prob >= 0.7:    accept
        if rgb_at_prob <= 0.3:    reject
        # else fall through

    # fall through -- consult gray CNN
    gray_at_prob = gray_cnn(crop)['@']
    return gray_at_prob >= 0.5

Degraded paths:

  * No contour module: primary tier becomes "geometry alone". A
    geometry YES still has to be confirmed by a CNN voter (RGB or
    gray); a geometry NO falls through to CNN voters too. We never
    let a single primary vote make the call.
  * No RGB v2: secondary tier abstains; we drop straight to the
    grayscale CNN.
  * No grayscale CNN session passed in: we attempt to lazy-import the
    production helper from ``ocr.sc_ocr.api`` (if available). If even
    that fails, gray CNN abstains and the result is whatever the
    primaries said (rejecting on full abstention).

Public API: ``vote_on_icon_candidate``.

Constraints honored:
 * PIL + numpy + (optional) onnxruntime + (optional) scipy.
 * Defensive: bad input returns a sensible "all unavailable, reject".
 * Graceful degradation when the parallel-agent outputs are missing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defensive imports for parallel-agent outputs
# ---------------------------------------------------------------------------

# 1. Contour detector (parallel agent — may not exist yet).
try:
    from hud_tracker.anchors.icon_contour import (  # type: ignore[import-not-found]
        find_icon_by_contour,
    )
    _HAS_CONTOUR = True
except Exception:  # ImportError, ModuleNotFoundError, anything else
    find_icon_by_contour = None  # type: ignore[assignment]
    _HAS_CONTOUR = False

# 2. Geometry detector (already exists and required).
from hud_tracker.anchors.icon_geometry import find_icon_by_geometry

# 2b. RGB NCC structural localizer (color-aware peer to geometry).
try:
    from hud_tracker.anchors.icon_rgb_ncc import (  # type: ignore[import-not-found]
        find_icon_rgb_ncc,
    )
    _HAS_RGB_NCC = True
except Exception:
    find_icon_rgb_ncc = None  # type: ignore[assignment]
    _HAS_RGB_NCC = False

# 3. RGB CNN v2 model (parallel agent — may not exist yet).
_TOOL_DIR = Path(__file__).resolve().parent.parent.parent
_RGB_CNN_V2_PATH = _TOOL_DIR / "ocr" / "models" / "model_signal_rgb_cnn_v2.onnx"
_RGB_CNN_V2_JSON = _TOOL_DIR / "ocr" / "models" / "model_signal_rgb_cnn_v2.json"
try:
    _HAS_RGB_V2 = bool(_RGB_CNN_V2_PATH.is_file())
except Exception:
    _HAS_RGB_V2 = False

# 3b. RGB INVERSE CNN — decorrelated polarity peer of the v2 RGB CNN.
# Trained on (255 - sample) per channel of the same corpus (including the
# ``@`` icon class), so it fires on colour/polarity-INVERTED icons exactly
# where v2 goes quiet — and vice-versa. Running BOTH on the raw crop and
# taking ``max(@-prob)`` makes the RGB voter polarity-robust without
# having to re-derive the crop's polarity: each model natively handles
# its own polarity, and non-icons stay quiet on BOTH (so false positives
# don't rise). Mirrors the digit ensemble's rgb / rgb_inv symmetry.
# Prefer v3 (panel + signature fonts, 99.25% val acc); fall back to v1.
_RGB_CNN_INV_PATH = _TOOL_DIR / "ocr" / "models" / "model_signal_rgb_inv_cnn_v3.onnx"
try:
    if not _RGB_CNN_INV_PATH.is_file():
        _RGB_CNN_INV_PATH = (
            _TOOL_DIR / "ocr" / "models" / "model_signal_rgb_inv_cnn.onnx"
        )
    _HAS_RGB_INV = bool(_RGB_CNN_INV_PATH.is_file())
except Exception:
    _HAS_RGB_INV = False
_RGB_CNN_INV_JSON = _RGB_CNN_INV_PATH.with_suffix(".json")


# ---------------------------------------------------------------------------
# Module-level config
# ---------------------------------------------------------------------------

# Threshold for the secondary tier (RGB CNN with @ class).
# >= ACCEPT ⇒ accept, <= REJECT ⇒ reject, else fall through.
_RGB_AT_ACCEPT_THR = 0.7
_RGB_AT_REJECT_THR = 0.3

# Threshold for the tertiary tier (gray CNN with @ class).
_GRAY_AT_ACCEPT_THR = 0.5

# Higher threshold when we only have geometry as the lone primary — we
# need stronger CNN agreement to commit.
_GRAY_AT_ACCEPT_THR_LONE_PRIMARY = 0.5

# Sessions cached lazily so each crop in a per-frame batch doesn't
# pay the ONNX session-load cost.
_rgb_cnn_v2_session = None
_rgb_cnn_v2_classes = "0123456789@"

# Inverse RGB CNN session (polarity peer) — lazily cached, same as v2.
_rgb_cnn_inv_session = None
_rgb_cnn_inv_classes = "0123456789@"

# Optional gray CNN fallback (production helper) — used when the
# caller doesn't pass one in.
_gray_cnn_helper = None
_gray_cnn_helper_attempted = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_pil_rgb(image: Any) -> Optional[Image.Image]:
    """Coerce input to a PIL RGB image; return None on bad input."""
    if image is None:
        return None
    try:
        if isinstance(image, Image.Image):
            return image.convert("RGB")
        if isinstance(image, np.ndarray):
            arr = image
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=-1)
            elif arr.ndim == 3 and arr.shape[2] == 4:
                arr = arr[..., :3]
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype(np.uint8)
            return Image.fromarray(arr).convert("RGB")
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def _bbox_clip(bbox: tuple[int, int, int, int], W: int, H: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(int(x1), W))
    y1 = max(0, min(int(y1), H))
    x2 = max(0, min(int(x2), W))
    y2 = max(0, min(int(y2), H))
    if x2 <= x1 or y2 <= y1:
        return (0, 0, 0, 0)
    return (x1, y1, x2, y2)


def _crop_rgb(image: Image.Image, bbox: tuple[int, int, int, int]) -> Optional[Image.Image]:
    W, H = image.size
    cb = _bbox_clip(bbox, W, H)
    if cb == (0, 0, 0, 0):
        return None
    return image.crop(cb)


# ---------------------------------------------------------------------------
# Voter helpers (each returns "yes" / "no" / "abstain" / "unavailable")
# ---------------------------------------------------------------------------


def _vote_geometry(crop_rgb: Image.Image) -> str:
    try:
        res = find_icon_by_geometry(crop_rgb)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("icon_voter: geometry raised: %s", exc)
        return "unavailable"
    return "yes" if res is not None else "no"


def _vote_contour(crop_rgb: Image.Image) -> str:
    if not _HAS_CONTOUR or find_icon_by_contour is None:
        return "unavailable"
    try:
        res = find_icon_by_contour(crop_rgb)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("icon_voter: contour raised: %s", exc)
        return "unavailable"
    return "yes" if res is not None else "no"


def _ensure_rgb_cnn_v2_session():
    """Return (session, input_name, classes) or (None, None, None)."""
    global _rgb_cnn_v2_session, _rgb_cnn_v2_classes
    if not _HAS_RGB_V2:
        return None, None, None
    if _rgb_cnn_v2_session is not None:
        try:
            return (
                _rgb_cnn_v2_session,
                _rgb_cnn_v2_session.get_inputs()[0].name,
                _rgb_cnn_v2_classes,
            )
        except Exception:  # pragma: no cover - defensive
            return None, None, None
    try:
        import onnxruntime as ort  # type: ignore
    except Exception as exc:
        # WARNING (not DEBUG): onnxruntime failing to import means
        # EVERY CNN voter in the scanner is permanently unavailable
        # for this process — a user-facing total-scanner failure.
        # Pre-v2.2.10-audit this was swallowed at DEBUG, so user
        # crash logs only showed the SYMPTOM (`rgb_cnn=unavailable`)
        # with no hint of the CAUSE.  Common causes worth checking
        # when this fires: missing Visual C++ Redistributable, CPU
        # without AVX2 (some onnxruntime wheels require it), an AV
        # blocking the onnxruntime DLL.
        log.warning(
            "icon_voter: onnxruntime import failed (%s: %s) — "
            "RGB CNN voter permanently unavailable",
            type(exc).__name__, exc,
        )
        return None, None, None
    try:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        sess = ort.InferenceSession(
            str(_RGB_CNN_V2_PATH),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        # WARNING (not DEBUG): the model file is on disk
        # (_HAS_RGB_V2 was True above) but onnxruntime can't open
        # it.  Could be a corrupted file (AV quarantined a few
        # bytes, partial Velopack apply), an incompatible onnxruntime
        # version, or session-creation failure.  Surfacing the
        # exception type + message in user logs so we can diagnose.
        log.warning(
            "icon_voter: failed to load RGB CNN v2 from %s "
            "(%s: %s) — RGB CNN voter unavailable",
            _RGB_CNN_V2_PATH, type(exc).__name__, exc,
        )
        return None, None, None
    classes = "0123456789@"
    try:
        if _RGB_CNN_V2_JSON.is_file():
            import json
            meta = json.loads(_RGB_CNN_V2_JSON.read_text(encoding="utf-8"))
            classes = meta.get("charClasses", classes)
    except Exception:
        pass
    _rgb_cnn_v2_session = sess
    _rgb_cnn_v2_classes = classes
    return sess, sess.get_inputs()[0].name, classes


def _ensure_rgb_cnn_inv_session():
    """Return (session, input_name, classes) for the inverse RGB CNN, or
    (None, None, None) when it's missing / can't load.

    Decorrelated polarity peer of v2 (see ``_RGB_CNN_INV_PATH``). Loaded
    and cached the same way as v2; absence degrades gracefully (the RGB
    voter just falls back to v2-only, i.e. pre-change behavior).
    """
    global _rgb_cnn_inv_session, _rgb_cnn_inv_classes
    if not _HAS_RGB_INV:
        return None, None, None
    if _rgb_cnn_inv_session is not None:
        try:
            return (
                _rgb_cnn_inv_session,
                _rgb_cnn_inv_session.get_inputs()[0].name,
                _rgb_cnn_inv_classes,
            )
        except Exception:  # pragma: no cover - defensive
            return None, None, None
    try:
        import onnxruntime as ort  # type: ignore
    except Exception as exc:
        log.debug(
            "icon_voter: onnxruntime import failed for inverse RGB CNN "
            "(%s) — polarity-robust RGB voting unavailable", exc,
        )
        return None, None, None
    try:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        sess = ort.InferenceSession(
            str(_RGB_CNN_INV_PATH),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        log.warning(
            "icon_voter: failed to load inverse RGB CNN from %s (%s: %s) "
            "— polarity-robust RGB voting unavailable; falling back to "
            "normal-polarity RGB CNN only",
            _RGB_CNN_INV_PATH, type(exc).__name__, exc,
        )
        return None, None, None
    classes = "0123456789@"
    try:
        if _RGB_CNN_INV_JSON.is_file():
            import json
            meta = json.loads(_RGB_CNN_INV_JSON.read_text(encoding="utf-8"))
            classes = meta.get("charClasses", classes)
    except Exception:
        pass
    _rgb_cnn_inv_session = sess
    _rgb_cnn_inv_classes = classes
    log.info(
        "icon_voter: loaded inverse RGB CNN (%s) — RGB @ voting is now "
        "polarity-robust", _RGB_CNN_INV_PATH.name,
    )
    return sess, sess.get_inputs()[0].name, classes


def _at_prob_for_session(sess, inp_name, classes, batch) -> Optional[float]:
    """Run a prepared NCHW batch through an RGB CNN session and return the
    ``@``-class probability, or ``None`` if the session/class is
    unusable or inference fails."""
    if sess is None or inp_name is None:
        return None
    at_idx = _at_index(classes)
    if at_idx < 0:
        return None
    try:
        logits = sess.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug("icon_voter: RGB CNN inference failed: %s", exc)
        return None
    if logits is None or logits.size == 0:
        return None
    probs = _softmax_row(logits[0])
    if at_idx >= probs.shape[0]:
        return None
    return float(probs[at_idx])


def _at_index(classes: str) -> int:
    """Index of the ``@`` class in the model's char vocabulary, or -1."""
    return classes.find("@")


def _softmax_row(row: np.ndarray) -> np.ndarray:
    e = np.exp(row - np.max(row))
    return e / e.sum()


def _vote_rgb_cnn(
    crop_rgb: Image.Image,
    rgb_cnn: Optional[Any] = None,
) -> tuple[str, Optional[float]]:
    """Return (verdict, prob_at) where verdict is yes/no/abstain/unavailable.

    Polarity-robust ensemble: the crop is classified by BOTH the normal-
    polarity RGB CNN (v2) and its inverse-polarity peer, and we take the
    MAX of their ``@``-class probabilities. The two models are
    complements — each fires on the polarity it was trained for and stays
    quiet on the other — so:
      * a real icon fires whichever model matches its polarity (normal
        OR colour-inverted) → ``max`` is high → accept;
      * a non-icon stays quiet on both → ``max`` is low → reject.
    This (together with the polarity-robust geometry detector) is what
    lets the icon anchor work over inverted / bright-bg captures, where
    v2 alone collapses to ~0. If the inverse model is absent it degrades
    to v2-only (the pre-change behavior).

    ``rgb_cnn`` may be a pre-loaded onnxruntime InferenceSession to use
    as the normal-polarity model; when None we lazy-load v2 from disk.
    """
    # Prepare the NCHW batch once (shared by both models).
    try:
        crop28 = crop_rgb.convert("RGB").resize((28, 28), Image.BILINEAR)
        arr = np.asarray(crop28, dtype=np.float32) / 255.0
        # NCHW float32; HWC -> CHW
        batch = arr.transpose(2, 0, 1)[None, ...]
    except Exception as exc:
        log.debug("icon_voter: RGB CNN crop prep failed: %s", exc)
        return "abstain", None

    # ── Normal-polarity model: caller-supplied session, else lazy v2 ──
    n_sess = rgb_cnn
    n_inp = None
    n_classes = "0123456789@"
    if n_sess is not None:
        try:
            n_inp = n_sess.get_inputs()[0].name
        except Exception:
            n_sess = None
    if n_sess is None:
        n_sess, n_inp, n_classes = _ensure_rgb_cnn_v2_session()
    p_norm = _at_prob_for_session(n_sess, n_inp, n_classes, batch)

    # ── Inverse-polarity peer (decorrelated) ──
    i_sess, i_inp, i_classes = _ensure_rgb_cnn_inv_session()
    p_inv = _at_prob_for_session(i_sess, i_inp, i_classes, batch)

    cands = [p for p in (p_norm, p_inv) if p is not None]
    if not cands:
        # Neither model usable (no @ class / no session / inference
        # failed). Mirror the legacy "can't vote" outcomes: unavailable
        # if nothing loaded, abstain otherwise — both fall through to
        # the gray CNN in the caller.
        return ("unavailable" if (n_sess is None and i_sess is None)
                else "abstain"), None

    p_at = max(cands)
    if p_at >= _RGB_AT_ACCEPT_THR:
        return "yes", p_at
    if p_at <= _RGB_AT_REJECT_THR:
        return "no", p_at
    return "abstain", p_at


def _ensure_gray_cnn_helper():
    """Lazy-import the production gray-CNN classifier helper.

    Returns a callable ``(crops_28x28_float32) -> [(label, conf), ...]``
    or None when unavailable.
    """
    global _gray_cnn_helper, _gray_cnn_helper_attempted
    if _gray_cnn_helper_attempted:
        return _gray_cnn_helper
    _gray_cnn_helper_attempted = True
    try:
        from ocr.sc_ocr.api import _classify_crops_signal  # type: ignore
        _gray_cnn_helper = _classify_crops_signal
    except Exception as exc:
        # WARNING (not DEBUG): if this import fails the gray voter
        # is permanently unavailable for the process.  Often fires
        # TOGETHER with the RGB voter's onnxruntime failure above
        # (they share onnxruntime), producing the user-facing
        # "votes={'rgb_cnn': 'unavailable', 'gray_cnn':
        # 'unavailable'}" cascade where the scanner gives up
        # before even looking at the panel.  Promoting to WARNING
        # so the actual exception text reaches user crash logs.
        log.warning(
            "icon_voter: gray CNN helper import failed "
            "(%s: %s) — gray CNN voter permanently unavailable",
            type(exc).__name__, exc,
        )
        _gray_cnn_helper = None
    return _gray_cnn_helper


def _vote_gray_cnn(
    crop_gray: np.ndarray,
    gray_cnn: Optional[Any] = None,
    accept_thr: float = _GRAY_AT_ACCEPT_THR,
) -> tuple[str, Optional[float]]:
    """Return (verdict, prob_at).

    ``gray_cnn`` may be one of:
      * an onnxruntime InferenceSession,
      * a callable ``(crops) -> [(label, conf), ...]``,
      * ``None`` -> lazy-load the production helper.

    The crop must be a 28x28 float32 array in [0, 1].
    """
    if crop_gray is None:
        return "unavailable", None

    # Prepare batch the helper expects: list of (28,28) float32 in [0,1].
    if crop_gray.ndim != 2 or crop_gray.shape != (28, 28):
        try:
            pil = Image.fromarray(np.asarray(crop_gray).astype(np.uint8))
            pil = pil.convert("L").resize((28, 28), Image.BILINEAR)
            crop_gray = np.asarray(pil, dtype=np.float32) / 255.0
        except Exception:
            return "unavailable", None
    crop_gray = crop_gray.astype(np.float32)
    if float(crop_gray.max()) > 1.0001:
        crop_gray = crop_gray / 255.0

    # Path A: caller supplied a callable
    if callable(gray_cnn):
        try:
            results = gray_cnn([crop_gray])
        except Exception as exc:
            log.debug("icon_voter: gray CNN callable failed: %s", exc)
            return "unavailable", None
        if not results:
            return "unavailable", None
        label, conf = results[0]
        if label == "@":
            return ("yes" if conf >= accept_thr else "no", float(conf))
        return ("no", 0.0)

    # Path B: caller supplied an onnxruntime session
    if gray_cnn is not None:
        try:
            inp_name = gray_cnn.get_inputs()[0].name
            batch = crop_gray.reshape(1, 1, 28, 28).astype(np.float32)
            logits = gray_cnn.run(None, {inp_name: batch})[0]
            probs = _softmax_row(logits[0])
            # Assume the gray signal CNN classes follow the registry
            # ('0123456789@'). The session itself doesn't store that.
            classes = "0123456789@"
            at_idx = _at_index(classes)
            if at_idx < 0 or at_idx >= probs.shape[0]:
                return "abstain", None
            p_at = float(probs[at_idx])
            return ("yes" if p_at >= accept_thr else "no", p_at)
        except Exception as exc:
            log.debug("icon_voter: gray CNN session failed: %s", exc)
            return "unavailable", None

    # Path C: fall through to the production helper
    helper = _ensure_gray_cnn_helper()
    if helper is None:
        return "unavailable", None
    try:
        results = helper([crop_gray])
    except Exception as exc:
        log.debug("icon_voter: gray CNN helper invocation failed: %s", exc)
        return "unavailable", None
    if not results:
        return "unavailable", None
    label, conf = results[0]
    if label == "@":
        return ("yes" if conf >= accept_thr else "no", float(conf))
    return ("no", 0.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def vote_on_icon_candidate(
    rgb_image: Any,
    candidate_bbox: tuple[int, int, int, int],
    gray_cnn: Optional[Any] = None,
    rgb_cnn: Optional[Any] = None,
    *,
    gray_crop: Optional[np.ndarray] = None,
) -> dict:
    """Vote on whether a candidate region contains the location-pin icon.

    Args:
        rgb_image: PIL Image (RGB) or numpy HxWx3. Required for the
            primary tier (geometry / contour) and the secondary tier
            (RGB CNN). When ``None`` the voter operates in
            "gray-only" mode: only the tertiary tier is consulted.
        candidate_bbox: (x1, y1, x2, y2) in image coordinates. The
            voter pads slightly internally before running the
            primary detectors.
        gray_cnn: optional pre-loaded grayscale CNN — either an
            onnxruntime InferenceSession or a callable. When None,
            we lazy-import the production helper.
        rgb_cnn: optional pre-loaded RGB CNN session. When None and
            v2 is on disk, we lazy-load it. When v2 is missing the
            secondary tier abstains and we fall through to the gray
            CNN.
        gray_crop: optional pre-extracted 28x28 grayscale crop. When
            supplied we don't re-derive it from ``rgb_image``;
            useful when the caller already canonicalized the gray
            polarity for the existing CNN.

    Returns:
        ``{
            "accepted": bool,
            "confidence": float,
            "votes": {
                "geometry": "yes" | "no" | "unavailable",
                "contour":  "yes" | "no" | "unavailable",
                "rgb_cnn":  "yes" | "no" | "abstain" | "unavailable",
                "gray_cnn": "yes" | "no" | "abstain" | "unavailable",
            },
            "decision_path": str,
            "details": {
                "rgb_at_prob": float | None,
                "gray_at_prob": float | None,
            },
        }``
    """
    votes = {
        "geometry": "unavailable",
        "contour": "unavailable",
        "rgb_cnn": "unavailable",
        "gray_cnn": "unavailable",
    }
    rgb_at_prob: Optional[float] = None
    gray_at_prob: Optional[float] = None

    pil = _to_pil_rgb(rgb_image)

    # ── Primary tier (only when we have an RGB image) ──────────────
    if pil is not None:
        # Pad the crop slightly so the geometry detector sees the icon's
        # full ink extent (NCC can lock onto a sub-portion).
        x1, y1, x2, y2 = candidate_bbox
        pad_x = max(2, (int(x2) - int(x1)) // 4)
        pad_y = max(2, (int(y2) - int(y1)) // 4)
        bbox_padded = (
            int(x1) - pad_x, int(y1) - pad_y,
            int(x2) + pad_x, int(y2) + pad_y,
        )
        crop_rgb = _crop_rgb(pil, bbox_padded)
        if crop_rgb is None or crop_rgb.size[0] < 4 or crop_rgb.size[1] < 4:
            crop_rgb = None

        if crop_rgb is not None:
            votes["geometry"] = _vote_geometry(crop_rgb)
            votes["contour"] = _vote_contour(crop_rgb)

    # ── Tier-1 short-circuit: both primaries agree ─────────────────
    geom = votes["geometry"]
    cont = votes["contour"]

    # Both YES -> accept.
    if geom == "yes" and cont == "yes":
        return {
            "accepted": True,
            "confidence": 0.95,
            "votes": votes,
            "decision_path": "primaries_agree_yes",
            "details": {"rgb_at_prob": None, "gray_at_prob": None},
        }
    # Both NO -> reject.
    if geom == "no" and cont == "no":
        return {
            "accepted": False,
            "confidence": 0.05,
            "votes": votes,
            "decision_path": "primaries_agree_no",
            "details": {"rgb_at_prob": None, "gray_at_prob": None},
        }

    # ── Secondary tier (RGB CNN with @ class) ─────────────────────
    pil_for_cnn = pil
    if pil_for_cnn is not None:
        x1, y1, x2, y2 = candidate_bbox
        crop_rgb = _crop_rgb(pil_for_cnn, (int(x1), int(y1), int(x2), int(y2)))
        if crop_rgb is not None and crop_rgb.size[0] >= 2 and crop_rgb.size[1] >= 2:
            verdict, p = _vote_rgb_cnn(crop_rgb, rgb_cnn=rgb_cnn)
            votes["rgb_cnn"] = verdict
            rgb_at_prob = p
            if verdict == "yes":
                return {
                    "accepted": True,
                    "confidence": float(min(1.0, max(0.7, p or 0.7))),
                    "votes": votes,
                    "decision_path": "primaries_disagree, rgb_cnn=accept",
                    "details": {"rgb_at_prob": p, "gray_at_prob": None},
                }
            if verdict == "no":
                return {
                    "accepted": False,
                    "confidence": float(max(0.0, min(0.3, p or 0.3))),
                    "votes": votes,
                    "decision_path": "primaries_disagree, rgb_cnn=reject",
                    "details": {"rgb_at_prob": p, "gray_at_prob": None},
                }
            # abstain / unavailable -> fall through.

    # ── Tertiary tier (grayscale CNN) ─────────────────────────────
    # Prepare a 28x28 float32 crop for the gray CNN.
    crop28 = gray_crop
    if crop28 is None and pil is not None:
        try:
            x1, y1, x2, y2 = candidate_bbox
            sub_rgb = _crop_rgb(pil, (int(x1), int(y1), int(x2), int(y2)))
            if sub_rgb is not None:
                pil_l = sub_rgb.convert("L").resize((28, 28), Image.BILINEAR)
                crop28 = np.asarray(pil_l, dtype=np.float32) / 255.0
        except Exception:
            crop28 = None

    # Pick the gray-CNN threshold based on what evidence we have.
    if (geom == "unavailable" and cont == "unavailable" and votes["rgb_cnn"] in ("unavailable", "abstain")):
        # Gray-only mode (e.g. detect_icon adapter): keep legacy behavior.
        gray_thr = _GRAY_AT_ACCEPT_THR
    elif geom == "yes" and cont == "unavailable":
        # Lone-primary YES: still gate on CNN.
        gray_thr = _GRAY_AT_ACCEPT_THR_LONE_PRIMARY
    elif geom == "no" and cont == "unavailable":
        # Lone-primary NO + fall-through CNN: keep base threshold.
        gray_thr = _GRAY_AT_ACCEPT_THR
    else:
        gray_thr = _GRAY_AT_ACCEPT_THR

    g_verdict, g_prob = _vote_gray_cnn(
        crop28 if crop28 is not None else np.zeros((28, 28), dtype=np.float32),
        gray_cnn=gray_cnn,
        accept_thr=gray_thr,
    )
    votes["gray_cnn"] = g_verdict
    gray_at_prob = g_prob

    # Final decision:
    if g_verdict == "yes":
        decision_path = (
            "all_primaries_unavailable, gray_cnn=accept"
            if pil is None or (geom == "unavailable" and cont == "unavailable")
            else "primaries_disagree_or_partial, gray_cnn=accept"
        )
        return {
            "accepted": True,
            "confidence": float(min(1.0, max(0.5, g_prob or 0.5))),
            "votes": votes,
            "decision_path": decision_path,
            "details": {"rgb_at_prob": rgb_at_prob, "gray_at_prob": gray_at_prob},
        }

    # gray says no, abstain, or unavailable -> reject.
    decision_path_parts = []
    if geom == "unavailable" and cont == "unavailable":
        decision_path_parts.append("all_primaries_unavailable")
    elif geom == "yes" and cont == "unavailable":
        decision_path_parts.append("lone_primary_yes_unconfirmed")
    elif geom == "no" and cont == "unavailable":
        decision_path_parts.append("lone_primary_no")
    else:
        decision_path_parts.append("primaries_disagree")
    if votes["rgb_cnn"] in ("abstain", "unavailable"):
        decision_path_parts.append(f"rgb_cnn={votes['rgb_cnn']}")
    decision_path_parts.append(f"gray_cnn={g_verdict}")
    return {
        "accepted": False,
        "confidence": float(g_prob or 0.0),
        "votes": votes,
        "decision_path": ", ".join(decision_path_parts),
        "details": {"rgb_at_prob": rgb_at_prob, "gray_at_prob": gray_at_prob},
    }


def availability() -> dict:
    """Diagnostic: report which voters are available at runtime."""
    return {
        "geometry": True,
        "contour": _HAS_CONTOUR,
        "rgb_cnn_v2": _HAS_RGB_V2,
        "rgb_cnn_v2_path": str(_RGB_CNN_V2_PATH),
        "rgb_ncc": _HAS_RGB_NCC,
    }


# ---------------------------------------------------------------------------
# Primary localizer (proposes WHERE the icon is)
# ---------------------------------------------------------------------------


def _bbox_xywh_to_xyxy(bbox) -> tuple[int, int, int, int]:
    """Convert (x, y, w, h) → (x1, y1, x2, y2)."""
    x, y, w, h = bbox
    return int(x), int(y), int(x) + int(w), int(y) + int(h)


def _iou_xywh(a, b) -> float:
    ax1, ay1, ax2, ay2 = _bbox_xywh_to_xyxy(a)
    bx1, by1, bx2, by2 = _bbox_xywh_to_xyxy(b)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    aa = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    bb = max(0, bx2 - bx1) * max(0, by2 - by1)
    ua = aa + bb - inter
    return float(inter) / float(ua) if ua > 0 else 0.0


def localize_icon(
    rgb_image: Any,
    hud_bbox: Optional[tuple[int, int, int, int]] = None,
    *,
    iou_thr: float = 0.4,
) -> Optional[dict]:
    """Primary localization — propose where the icon is, then validate.

    Runs the structural primary detectors directly on the image:

      1. ``find_icon_by_geometry`` (warm color + structure)
      2. ``find_icon_rgb_ncc``     (color-aware NCC)

    Both detectors propose a bbox. If their bboxes overlap (IoU > iou_thr),
    we accept and return the consensus result. If only one detector
    reports a hit, we return None and let the caller fall back to the
    legacy NCC + voter pipeline. If both disagree, also return None.

    Returns:
        dict like::

            {
                "bbox": (x, y, w, h),
                "score": float,
                "detector": "consensus(geometry,rgb_ncc)",
                "details": {
                    "geometry": <res or None>,
                    "rgb_ncc":  <res or None>,
                    "iou":      float,
                },
            }

        or ``None`` when the primaries don't agree.
    """
    pil = _to_pil_rgb(rgb_image)
    if pil is None:
        return None

    rgb_np = np.asarray(pil, dtype=np.uint8)

    # ---- Run both primaries in parallel (sequentially in code, but
    # they're independent and fast). ----
    try:
        geom_res = find_icon_by_geometry(rgb_np, hud_bbox=hud_bbox)
    except Exception as exc:
        log.debug("localize_icon: geometry raised: %s", exc)
        geom_res = None

    rgb_ncc_res = None
    if _HAS_RGB_NCC and find_icon_rgb_ncc is not None:
        try:
            rgb_ncc_res = find_icon_rgb_ncc(rgb_np, hud_bbox=hud_bbox)
        except Exception as exc:
            log.debug("localize_icon: rgb_ncc raised: %s", exc)
            rgb_ncc_res = None

    # No-hit short-circuits.
    if geom_res is None and rgb_ncc_res is None:
        return None
    if geom_res is None or rgb_ncc_res is None:
        # Only one detector hit — not enough consensus.
        return None

    geom_bbox = geom_res.get("bbox")
    ncc_bbox = rgb_ncc_res.get("bbox")
    if geom_bbox is None or ncc_bbox is None:
        return None

    iou = _iou_xywh(geom_bbox, ncc_bbox)
    if iou < iou_thr:
        log.debug(
            "localize_icon: primaries disagree (IoU=%.2f, geom=%s, rgb_ncc=%s)",
            iou, geom_bbox, ncc_bbox,
        )
        return None

    # Consensus: prefer the rgb_ncc bbox directly. NCC's per-channel
    # template match pins both top-left corner and size very accurately
    # (verified on cap_20260418_155446_555.png: IoU 0.90 vs GT). The
    # geometry detector's bbox can drift because the warm-mask greedy
    # merge step extends the bbox to include any nearby warm halo
    # (e.g. anti-aliased pixels just above or below the icon body).
    # We use geometry as a structural sanity check on POSITION, not as
    # a corner contributor.
    nx, ny, nw, nh = (int(ncc_bbox[0]), int(ncc_bbox[1]),
                      int(ncc_bbox[2]), int(ncc_bbox[3]))
    cx, cy, cw, ch = nx, ny, nw, nh

    # Combined confidence: NCC's normalized score plus a geometry
    # boost (geometry doesn't return a normalized score, just a tier).
    ncc_score = float(rgb_ncc_res.get("score", 0.0))
    geom_conf = float(geom_res.get("confidence", 0.0))
    score = float(min(1.0, 0.7 * ncc_score + 0.3 * geom_conf + 0.05))

    return {
        "bbox": (cx, cy, cw, ch),
        "score": score,
        "detector": "consensus(geometry,rgb_ncc)",
        "details": {
            "geometry": geom_res,
            "rgb_ncc": rgb_ncc_res,
            "iou": float(iou),
        },
    }


__all__ = ["vote_on_icon_candidate", "availability", "localize_icon"]
