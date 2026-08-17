"""Profile the signature scanner's failure modes across labeled
region2 captures.

Runs the production pipeline ``ocr.sc_ocr.api._signal_recognize_pil``
on every labeled capture in ``training_data_panels/user_*/region2/``,
captures intermediate state via monkey-patches on the modules the
pipeline imports, and classifies each failing capture into ONE
root-cause stage:

    Stage  1 — Pill detection wrong
    Stage  2 — Icon localization wrong
    Stage  3 — Crop_box wrong (clips significant digit ink)
    Stage  4 — Comma detection failed
    Stage  5 — Comma at wrong column
    Stage  6 — Wrong digit count hypothesis
    Stage  7 — Per-digit segmentation wrong
    Stage  8 — CNN per-digit classification wrong
    Stage  9 — Lexicon backtracking failed
    Stage 10 — Consensus / lock-cache rejected

This is a measurement-only task: nothing in production code is
modified. Monkey-patching is local to this profiler process.

Outputs
-------
``hud_tracker/anchors/failure_profile.csv``
    one row per capture: capture, gt, read, match, failure_stage, notes.

Stdout
    summary report — total captures, accuracy %, per-stage histogram,
    top failure stage.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Force UTF-8 stdout so any unicode chars in notes don't crash on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(  # type: ignore[assignment]
        sys.stdout.buffer, encoding="utf-8", errors="replace",
    )

import numpy as np
from PIL import Image


# ── Path setup so production modules import correctly ─────────────
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent  # tools/Mining_Signals
_TOOLBOX_ROOT = _REPO_ROOT.parent.parent  # tools/Mining_Signals/.. = current
for _p in (_TOOLBOX_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Quiet the logger noise from production modules. We only want our
# own per-capture lines.
logging.basicConfig(level=logging.WARNING, format="%(message)s")

# ── Production imports (after sys.path is rigged) ─────────────────
from ocr.sc_ocr import api as _api  # type: ignore
from hud_tracker.anchors import (  # type: ignore
    hud_color_finder as _hcf,
    icon_voter as _iv,
    comma_finder as _cf,
    signal_proportional_segmenter as _sps,
)


CAPTURES_ROOT = Path(
    r"C:\Users\_user\Documents\sc_ocr_engine\training_data_panels"
)
CSV_OUT = _THIS_DIR / "failure_profile.csv"


# ── Per-capture instrumentation state ────────────────────────────
class _CaptureState:
    """Holds intermediate state captured during a single pipeline run."""

    def __init__(self) -> None:
        # Stage 1
        self.pill_calls: list[Optional[dict]] = []
        # Stage 2
        self.icon_calls: list[Optional[dict]] = []
        # Stage 4-5: comma_voted called multiple times (api-side and segmenter-side)
        self.comma_calls: list[Optional[dict]] = []
        # Stage 6-9: proportional segmenter outcome
        self.seg_calls: list[Optional[dict]] = []
        # Stage 3: crop_box from log line ``crop_box=(x1, y1, x2, y2)``
        self.crop_box_log: Optional[tuple[int, int, int, int]] = None
        # Logs intercepted from production
        self.log_lines: list[str] = []
        # Final return value of _signal_recognize_pil (already STABLE_SIGNAL).
        self.final_read: Optional[int] = None
        # Pre-stable raw read inferred from log lines (RGB-primary,
        # n-way-consensus, CRNN, Tesseract, etc.).
        self.raw_read: Optional[int] = None


_CAPTURE: _CaptureState = _CaptureState()


# ── Monkey-patch wrappers ─────────────────────────────────────────
_orig_find_hud_panel = _hcf.find_hud_panel
_orig_localize_icon = _iv.localize_icon
_orig_find_comma_voted = _cf.find_comma_voted
_orig_segment_signal_proportional = _sps.segment_signal_proportional


def _patched_find_hud_panel(*args, **kwargs):
    res = _orig_find_hud_panel(*args, **kwargs)
    try:
        _CAPTURE.pill_calls.append(res if res is None else dict(res))
    except Exception:
        _CAPTURE.pill_calls.append(None)
    return res


def _patched_localize_icon(*args, **kwargs):
    res = _orig_localize_icon(*args, **kwargs)
    try:
        _CAPTURE.icon_calls.append(res if res is None else dict(res))
    except Exception:
        _CAPTURE.icon_calls.append(None)
    return res


def _patched_find_comma_voted(*args, **kwargs):
    res = _orig_find_comma_voted(*args, **kwargs)
    try:
        if res is None:
            _CAPTURE.comma_calls.append(None)
        else:
            # res may contain primary/inverted nested dicts; keep a flat
            # view that's safe to JSON-summarize.
            _CAPTURE.comma_calls.append({
                "bbox": tuple(int(v) for v in res.get("bbox") or ()),
                "x_center": int(res.get("x_center", -1)),
                "confidence": float(res.get("confidence", 0.0)),
                "voted": bool(res.get("voted", False)),
                "agreed": bool(res.get("agreed", False)),
            })
    except Exception:
        _CAPTURE.comma_calls.append(None)
    return res


def _patched_segment_signal_proportional(*args, **kwargs):
    res = _orig_segment_signal_proportional(*args, **kwargs)
    try:
        if res is None:
            _CAPTURE.seg_calls.append(None)
        else:
            # Keep only JSON-safe fields. Strip ``gray_canon_used`` ndarray.
            digits = []
            for d in res.get("digits") or []:
                d2 = {
                    "bbox": tuple(int(v) for v in d.get("bbox") or ()),
                    "is_comma": bool(d.get("is_comma", False)),
                }
                if "classification" in d:
                    d2["classification"] = str(d["classification"])
                if "confidence" in d:
                    d2["confidence"] = float(d["confidence"])
                digits.append(d2)
            details = res.get("details") or {}
            # Strip nested array out of comma_anchor.
            comma_anchor = details.get("comma_anchor")
            if isinstance(comma_anchor, dict):
                ca_safe = {}
                for k, v in comma_anchor.items():
                    if k in ("primary", "inverted"):
                        continue
                    if hasattr(v, "tolist"):
                        continue
                    ca_safe[k] = v
                comma_anchor = ca_safe
            slim_details = {
                "crop_w": int(details.get("crop_w", 0)),
                "crop_h": int(details.get("crop_h", 0)),
                "ink_extent": details.get("ink_extent"),
                "comma_extent": details.get("comma_extent"),
                "winner_n_digits": details.get("winner_n_digits"),
                "winner_used_blob_centers": details.get("winner_used_blob_centers"),
                "string_composed": details.get("string_composed"),
                "comma_anchor_used": details.get("comma_anchor_used"),
                "comma_anchor": comma_anchor,
                "upscale_used": details.get("upscale_used", 1),
                "hypotheses": details.get("hypotheses"),
            }
            _CAPTURE.seg_calls.append({
                "n_digits": int(res.get("n_digits", 0)),
                "comma_position": int(res.get("comma_position", -1)),
                "confidence": float(res.get("confidence", 0.0)),
                "digits": digits,
                "details": slim_details,
            })
    except Exception:
        _CAPTURE.seg_calls.append(None)
    return res


# ── Logging interceptor ───────────────────────────────────────────
class _CaptureHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = record.getMessage()
        except Exception:
            return
        _CAPTURE.log_lines.append(line)


def _install_patches() -> None:
    _hcf.find_hud_panel = _patched_find_hud_panel
    _iv.localize_icon = _patched_localize_icon
    _cf.find_comma_voted = _patched_find_comma_voted
    _sps.segment_signal_proportional = _patched_segment_signal_proportional
    # The api module imports the names locally inside the function
    # body, so the patching above takes effect on the next import.
    # Add the log handler last.
    h = _CaptureHandler(level=logging.DEBUG)
    logging.getLogger("ocr.sc_ocr.api").addHandler(h)
    logging.getLogger("ocr.sc_ocr.api").setLevel(logging.INFO)
    logging.getLogger("hud_tracker").addHandler(h)
    logging.getLogger("hud_tracker").setLevel(logging.INFO)


# ── Stage classification ─────────────────────────────────────────
def _gt_to_digits(gt_str: str) -> str:
    """Strip comma; return ``DDDD`` or ``DDDDD``."""
    return "".join(c for c in (gt_str or "") if c.isdigit())


def _parse_crop_box_from_logs(lines: list[str]) -> Optional[tuple[int, int, int, int]]:
    """Extract crop_box=(x1, y1, x2, y2) from the crop_box log line."""
    import re
    pat = re.compile(r"crop_box=\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)")
    last = None
    for L in lines:
        m = pat.search(L)
        if m:
            last = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return last


def _find_pill_bbox(state: _CaptureState) -> Optional[tuple[int, int, int, int]]:
    for c in state.pill_calls:
        if c is None:
            continue
        bb = c.get("bbox")
        if bb and len(bb) == 4:
            return tuple(int(v) for v in bb)
    return None


def _find_icon_bbox(state: _CaptureState) -> Optional[tuple[int, int, int, int]]:
    for c in state.icon_calls:
        if c is None:
            continue
        bb = c.get("bbox")
        if bb and len(bb) == 4:
            return tuple(int(v) for v in bb)
    return None


def _crop_includes_digit_ink(
    rgb: np.ndarray,
    crop_box: tuple[int, int, int, int],
    gt_digits: str,
) -> bool:
    """Heuristic: does the crop's WIDTH support ``len(gt_digits)`` digits?

    SC HUD's signal font glyphs are ~10-14 px wide in the original
    panel resolution at 1080p, with ~2-3 px kerning between them. A
    5-digit value needs at least 5*10 + 4*1 = ~54 px (minimum). We
    use a generous lower bound (~7 px per digit) to stay forgiving.

    Returns True if the crop is wide enough to plausibly hold all
    GT digits; False if it's clearly too narrow.
    """
    x1, y1, x2, y2 = crop_box
    crop_w = x2 - x1
    if crop_w <= 0 or y2 <= y1:
        return False
    n = len(gt_digits)
    # 7 px per digit minimum, plus 5 px slack for icon-side gutter.
    min_w = 7 * n + 5
    return crop_w >= min_w


def _expected_comma_x_in_crop_blob(
    seg: dict,
    gt_n_digits: int,
) -> Optional[float]:
    """Comma center as the midpoint of the segmenter's GT-aligned
    digit blob centers. Returns None when seg's chosen n_digits does
    not match GT (in that case we don't have GT-aligned blob centers
    to use)."""
    seg_n = int(seg.get("n_digits", 0))
    if seg_n != gt_n_digits:
        return None
    digit_cxs: list[float] = []
    for d in seg.get("digits") or []:
        if d.get("is_comma"):
            continue
        bb = d.get("bbox") or ()
        if len(bb) != 4:
            continue
        digit_cxs.append(float(bb[0]) + float(bb[2]) / 2.0)
    if len(digit_cxs) != gt_n_digits:
        return None
    n_left = 1 if gt_n_digits == 4 else 2
    if 0 < n_left < len(digit_cxs):
        return (digit_cxs[n_left - 1] + digit_cxs[n_left]) / 2.0
    return None


def _expected_comma_x_in_crop_uniform(
    seg: dict,
    gt_n_digits: int,
) -> Optional[float]:
    """Comma center via uniform proportional layout — mirrors the
    segmenter's ``_fallback_proportional_bboxes``: digit_pitch =
    ink_w / (n + 0.4), comma center at ix1 + (n_left * pitch +
    0.2*pitch) where comma_pitch = 0.4*pitch and we offset by
    half its width."""
    details = seg.get("details") or {}
    ink = details.get("ink_extent")
    if not ink or len(ink) != 2:
        return None
    ix1, ix2 = int(ink[0]), int(ink[1])
    if ix2 <= ix1:
        return None
    pitch = (ix2 - ix1) / (gt_n_digits + 0.4)
    if gt_n_digits == 4:
        return float(ix1 + 1.2 * pitch)
    if gt_n_digits == 5:
        return float(ix1 + 2.2 * pitch)
    return None


def _classify_all_failure_stages(
    state: _CaptureState,
    gt_str: str,
    gt_int: int,
    rgb: np.ndarray,
) -> list[int]:
    """Run all stage checks (without short-circuiting) and return every
    stage whose check fires. Used for compound-failure diagnostics."""
    fired: list[int] = []
    gt_digits = _gt_to_digits(gt_str)
    gt_n = len(gt_digits)

    pill = _find_pill_bbox(state)
    if pill is None:
        fired.append(1)
    icon = _find_icon_bbox(state)
    if icon is None:
        fired.append(2)
    crop_box = _parse_crop_box_from_logs(state.log_lines)
    seg = state.seg_calls[-1] if state.seg_calls else None
    seg_details = seg.get("details") if seg else {}
    if crop_box is None or seg is None:
        fired.append(3)
    else:
        seg_n = int(seg.get("n_digits", 0))
        if seg_n < gt_n and not _crop_includes_digit_ink(
            rgb, crop_box, gt_digits,
        ):
            fired.append(3)

    if not any(c is not None for c in state.comma_calls):
        fired.append(4)

    # Stages 5+ require seg.
    if seg is not None:
        upscale = int((seg_details or {}).get("upscale_used", 1) or 1)
        comma_tol_px = 6 * max(1, upscale)
        expected_blob = _expected_comma_x_in_crop_blob(seg, gt_n)
        expected_uniform = _expected_comma_x_in_crop_uniform(seg, gt_n)
        seg_side_comma = None
        for c in reversed(state.comma_calls):
            if c is not None:
                seg_side_comma = c
                break
        if seg_side_comma is not None and (
            expected_blob is not None or expected_uniform is not None
        ):
            comma_det_cx = float(seg_side_comma.get("x_center", -1))
            if comma_det_cx >= 0:
                errs = []
                if expected_blob is not None:
                    errs.append(abs(comma_det_cx - expected_blob))
                if expected_uniform is not None:
                    errs.append(abs(comma_det_cx - expected_uniform))
                if min(errs) > comma_tol_px:
                    fired.append(5)

        seg_n = int(seg.get("n_digits", 0))
        if seg_n != gt_n:
            fired.append(6)

        used_blob_centers = bool(
            (seg_details or {}).get("winner_used_blob_centers", True)
        )
        if not used_blob_centers:
            fired.append(7)

        composed = str((seg_details or {}).get("string_composed") or "")
        if composed != gt_digits:
            fired.append(8)
            backtracked = False
            if (seg_details or {}).get("hypotheses"):
                for h in seg_details["hypotheses"]:
                    if h.get("n_digits") == seg_n:
                        backtracked = bool(h.get("backtracked"))
                        break
            if backtracked:
                fired.append(9)

    if (
        seg is not None
        and gt_int is not None
        and state.final_read != gt_int
    ):
        composed_seg = str((seg_details or {}).get("string_composed") or "")
        if composed_seg == gt_digits:
            fired.append(10)
    return fired


def _classify_failure_stage(
    state: _CaptureState,
    gt_str: str,
    gt_int: int,
    rgb: np.ndarray,
) -> tuple[int, str, Optional[int]]:
    """Determine the EARLIEST pipeline stage that diverged from GT.

    Returns ``(stage, notes, raw_read)``.
    """
    gt_digits = _gt_to_digits(gt_str)
    gt_n = len(gt_digits)

    # Stage 1: pill bbox missing.
    pill = _find_pill_bbox(state)
    if pill is None:
        return 1, "no pill bbox returned", None

    # Stage 2: icon bbox missing.
    icon = _find_icon_bbox(state)
    if icon is None:
        return 2, "no icon bbox returned", None

    # Stage 3: crop_box clips significant digit ink.
    crop_box = _parse_crop_box_from_logs(state.log_lines)
    if crop_box is None:
        return 3, "no crop_box logged", None

    # Stage 4-9: examine the proportional segmenter's outcome.
    # The api calls the segmenter exactly once on the upscaled work crop.
    seg = state.seg_calls[-1] if state.seg_calls else None
    if seg is None:
        # No segmenter result at all — likely the segmenter declined
        # (crop too small / failed). Treat as Stage 3 (crop too off).
        return 3, "proportional segmenter returned None", None
    seg_details = seg.get("details") or {}

    # Stage 3 (refined): the segmenter chose ``n_digits`` < GT digit
    # count AND the input crop's column-projection confirms there
    # aren't enough ink blobs to support GT digits — i.e. the crop
    # is clipping the leading digit. The first condition alone is
    # ambiguous (could be Stage 6 hypothesis-pick); the conjunction
    # is what makes it Stage 3.
    seg_n = int(seg.get("n_digits", 0))
    if seg_n < gt_n and not _crop_includes_digit_ink(
        rgb, crop_box, gt_digits,
    ):
        return 3, (
            f"crop_box={crop_box} segmenter chose n={seg_n} < GT n={gt_n} "
            f"and crop ink-blob count is insufficient — leading digit "
            f"likely clipped"
        ), None

    # Stage 4: comma not found by find_comma_voted.
    # find_comma_voted is called twice: once at api-side (for crop
    # extension) and once inside segment_signal_proportional. We treat
    # Stage 4 as "neither call found a comma".
    any_comma_hit = any(c is not None for c in state.comma_calls)
    if not any_comma_hit:
        return 4, "find_comma_voted returned None on all calls", None

    # Stage 5: comma at wrong column. We compare the comma DETECTOR's
    # x_center against TWO independent estimates of where the comma
    # should be:
    #
    #   * blob-midpoint: midpoint of the segmenter's digit-blob
    #     centers around the comma slot. Robust when blob detection
    #     succeeded.
    #   * uniform-proportional: ink_extent + n_digits + 0.4 layout.
    #     Robust to proportional-font bias when blob detection
    #     produced wrong centers (e.g. fused leading digit + icon).
    #
    # Stage 5 fires only when BOTH estimates disagree with the
    # detector by > tolerance. This avoids false positives when
    # one estimate is unreliable for the specific failure mode
    # (e.g. fused-digit cases where blob midpoint is wrong but
    # uniform layout is approximately correct).
    upscale = int(seg_details.get("upscale_used", 1) or 1)
    # Spec says 3 px tolerance in original-space pixels. Upscaled
    # crops use 3*upscale px = 6 px at 2x upscale. We add slop for
    # proportional-font width variance — `1` vs `0` shifts the
    # comma column by 3-4 px in the original frame, multiplied by
    # upscale this adds 6-8 px of legitimate variance.
    comma_tol_px = 6 * max(1, upscale)
    expected_blob = _expected_comma_x_in_crop_blob(seg, gt_n)
    expected_uniform = _expected_comma_x_in_crop_uniform(seg, gt_n)
    seg_side_comma = None
    for c in reversed(state.comma_calls):
        if c is None:
            continue
        seg_side_comma = c
        break
    if seg_side_comma is not None and (
        expected_blob is not None or expected_uniform is not None
    ):
        comma_det_cx = float(seg_side_comma.get("x_center", -1))
        if comma_det_cx >= 0:
            errs = []
            if expected_blob is not None:
                errs.append(("blob", abs(comma_det_cx - expected_blob)))
            if expected_uniform is not None:
                errs.append(("uniform", abs(comma_det_cx - expected_uniform)))
            min_label, min_err = min(errs, key=lambda kv: kv[1])
            if min_err > comma_tol_px:
                return 5, (
                    f"detector comma_cx={comma_det_cx:.1f} disagrees with "
                    f"both blob-midpt({expected_blob}) and uniform"
                    f"({expected_uniform}); min_err={min_err:.1f} via "
                    f"{min_label} > tol {comma_tol_px}"
                ), None

    # Stage 6: wrong digit count hypothesis.
    if seg_n != gt_n:
        return 6, f"segmenter n_digits={seg_n} != GT {gt_n}", None

    # Stage 7: per-digit segmentation off. Fires only when the
    # segmenter FELL BACK to the proportional layout (i.e. blob
    # detection couldn't find ``n_digits + 1`` centers, so it spread
    # uniform-pitch slots over the ink_extent). When the segmenter
    # successfully used blob detection (``used_blob_centers=True``),
    # its digit bboxes ARE at the actual ink centers — Stage 7 is by
    # construction unreachable.
    used_blob_centers = bool(seg_details.get("winner_used_blob_centers", True))
    if not used_blob_centers:
        # Compare the segmenter's chosen bboxes to a proportional
        # GT-aligned model. If they're > 4 px off, the slots
        # mis-aligned with actual ink.
        ink = seg_details.get("ink_extent")
        if ink and len(ink) == 2:
            ix1, ix2 = int(ink[0]), int(ink[1])
            pitch = (ix2 - ix1) / (gt_n + 0.4)  # mirror seg fallback
            n_left = 1 if gt_n == 4 else 2
            expected_centers = []
            for i in range(gt_n):
                if i < n_left:
                    expected_centers.append(ix1 + (i + 0.5) * pitch)
                else:
                    # +0.4 slot for the comma between left and right groups
                    expected_centers.append(ix1 + (i + 0.9) * pitch)
            actual_centers: list[float] = []
            for d in seg.get("digits") or []:
                if d.get("is_comma"):
                    continue
                bb = d.get("bbox") or ()
                if len(bb) != 4:
                    continue
                actual_centers.append(float(bb[0]) + float(bb[2]) / 2.0)
            if len(actual_centers) == gt_n:
                tol_px = 4 * max(1, upscale)
                errs = [abs(a - e) for a, e in zip(actual_centers, expected_centers)]
                max_err = max(errs)
                if max_err > tol_px:
                    return 7, (
                        f"used_blob_centers=False; digit center errs="
                        f"{[round(e, 1) for e in errs]} max={max_err:.1f} "
                        f"> tol {tol_px}"
                    ), None

    # Stage 8/9: classification or lexicon failure.
    #
    # Stage 9 (lexicon backtracking failed) is the narrow case where
    # the segmenter's lexicon backtracker successfully ran AND chose
    # a swap, but the chosen alternative still doesn't match GT. That's
    # a true lexicon-coverage problem (no swap exists that lands on the
    # actual GT) or a top-2 failure where the wrong alternative was
    # picked.
    #
    # Stage 8 covers everything else where composed != GT:
    #  - CNN top-1 wrong AND top-2 also wrong (no usable backtrack)
    #  - CNN confidently wrong on a value that IS in the lexicon (so
    #    backtracking didn't even try).
    composed = str(seg_details.get("string_composed") or "")
    backtracked = False
    in_lexicon = False
    if seg_details.get("hypotheses"):
        for h in seg_details["hypotheses"]:
            if h.get("n_digits") == seg_n:
                backtracked = bool(h.get("backtracked"))
                in_lexicon = bool(h.get("in_lexicon"))
                break

    if composed != gt_digits:
        raw = state.raw_read
        try:
            composed_int = int(composed) if composed.isdigit() else None
        except ValueError:
            composed_int = None
        composed_in_lex = (
            composed_int is not None
            and composed_int in _api._KNOWN_SIGNAL_VALUES
        ) if _api._KNOWN_SIGNAL_VALUES else False

        if backtracked:
            return 9, (
                f"backtracked to {composed!r} but still != GT {gt_digits!r}"
            ), raw
        return 8, (
            f"composed={composed!r} != GT {gt_digits!r}"
            + (" (in lexicon)" if composed_in_lex else " (not in lexicon)")
        ), raw

    # composed == gt_digits but the production pipeline returned None
    # or wrong. That's Stage 10 (consensus / gate rejected the correct
    # intermediate read).
    return 10, (
        f"intermediate composed={composed!r} matches GT but final="
        f"{state.final_read}"
    ), state.raw_read


# ── Final-read inference from logs ────────────────────────────────
def _parse_raw_read_from_logs(lines: list[str]) -> Optional[int]:
    """Sniff the last value the production pipeline DERIVED, regardless
    of whether the final return is the same.

    Looks at ``RGB-PRIMARY``, ``N-WAY CONSENSUS``, ``stable swap``,
    ``Tesseract``, and ``CRNN`` log lines for the most recent integer
    that the pipeline accepted.
    """
    import re
    out: Optional[int] = None
    # Order: prefer the last accepted-value log line.
    accept_pat = re.compile(
        r"(?:N-WAY CONSENSUS gate accepted|RGB PRIMARY .*?→|"
        r"CRNN-SECONDARY gate accepted|Tesseract gate accepted|"
        r"stable swap \d+ →) (\d{4,5})\b"
    )
    for L in lines:
        m = accept_pat.search(L)
        if m:
            out = int(m.group(1))
    if out is None:
        # Fallback: pick any "composed" or "consensus str=" hit.
        comp_pat = re.compile(
            r"(?:string_composed=|str=|composed=)['\"]?(\d{4,5})['\"]?"
        )
        for L in lines:
            m = comp_pat.search(L)
            if m:
                out = int(m.group(1))
    return out


# ── Main loop ────────────────────────────────────────────────────
def _list_labeled_captures() -> list[tuple[Path, Path, str]]:
    """Walk training_data_panels/user_*/region2/ and yield
    ``(png_path, json_path, gt_value_str)`` triples.
    """
    out: list[tuple[Path, Path, str]] = []
    if not CAPTURES_ROOT.exists():
        print(f"WARNING: captures root missing: {CAPTURES_ROOT}", file=sys.stderr)
        return out
    for user_dir in sorted(CAPTURES_ROOT.glob("user_*")):
        r2 = user_dir / "region2"
        if not r2.is_dir():
            continue
        for jsonp in sorted(r2.glob("*.json")):
            png = jsonp.with_suffix(".png")
            if not png.exists():
                continue
            try:
                with jsonp.open() as f:
                    meta = json.load(f)
            except Exception:
                continue
            v = meta.get("value")
            if not v:
                continue
            out.append((png, jsonp, str(v)))
    return out


def _reset_state() -> None:
    """Clear per-capture instrumentation and the api's stable buffers
    so a previous capture's value can't leak into the next one's read.
    """
    global _CAPTURE
    _CAPTURE = _CaptureState()
    # Reset api consensus buffers so each capture is evaluated fresh.
    try:
        _api._reset_consensus_buffers()
    except Exception:
        pass


def _maybe_load_lexicon() -> int:
    """Best-effort: load the mining chart and feed its known signature
    values into the api's lexicon. Without a lexicon the gate
    confidence threshold is 0.85 (vs 0.65 with a hit) — production at
    runtime has the lexicon loaded, so we mirror that.

    Returns the number of values loaded.
    """
    try:
        from services.sheet_fetcher import SheetFetcher  # type: ignore
    except Exception as exc:
        print(f"lexicon load skipped (SheetFetcher import failed: {exc})",
              file=sys.stderr)
        return 0
    try:
        sf = SheetFetcher()
        res = sf.load(force_refresh=False)
        if not getattr(res, "ok", False):
            return 0
        known: set[int] = set()
        for r in res.data:
            for n in range(1, 21):
                v = r.get(str(n), 0)
                if v:
                    try:
                        known.add(int(v))
                    except (TypeError, ValueError):
                        pass
        _api.set_known_signal_values(known)
        return len(known)
    except Exception as exc:
        print(f"lexicon load failed: {exc}", file=sys.stderr)
        return 0


def main() -> int:
    _install_patches()
    n_loaded = _maybe_load_lexicon()
    print(f"lexicon loaded: {n_loaded} known signature values")

    captures = _list_labeled_captures()
    print(f"found {len(captures)} labeled region2 captures")

    rows: list[dict[str, Any]] = []
    n_correct = 0
    stage_hist: dict[int, int] = {i: 0 for i in range(1, 11)}

    for idx, (png, jsonp, gt_str) in enumerate(captures, 1):
        _reset_state()
        try:
            img = Image.open(png).convert("RGB")
        except Exception as exc:
            print(f"  [{idx}/{len(captures)}] {png.name}: open failed ({exc})")
            continue
        rgb = np.asarray(img, dtype=np.uint8)
        try:
            final = _api._signal_recognize_pil(img, region=None)
        except Exception as exc:
            final = None
            _CAPTURE.log_lines.append(f"_signal_recognize_pil raised: {exc}")
        _CAPTURE.final_read = final if isinstance(final, int) else None
        _CAPTURE.raw_read = _parse_raw_read_from_logs(_CAPTURE.log_lines)

        try:
            gt_int = int("".join(c for c in gt_str if c.isdigit()))
        except ValueError:
            continue

        match = (final == gt_int)
        if match:
            n_correct += 1
            stage = 0
            notes = "ok"
            raw_read = final
            all_fired: list[int] = []
        else:
            stage, notes, raw_read = _classify_failure_stage(
                _CAPTURE, gt_str, gt_int, rgb,
            )
            stage_hist[stage] = stage_hist.get(stage, 0) + 1
            all_fired = _classify_all_failure_stages(
                _CAPTURE, gt_str, gt_int, rgb,
            )

        rows.append({
            "capture": str(png),
            "gt": gt_str,
            "read": "" if final is None else str(final),
            "raw_read": "" if raw_read is None else str(raw_read),
            "match": "1" if match else "0",
            "failure_stage": "0" if match else str(stage),
            "all_failed_stages": ",".join(str(s) for s in all_fired),
            "notes": notes,
        })

        if idx % 25 == 0 or idx == len(captures):
            print(
                f"  [{idx}/{len(captures)}] {png.name} gt={gt_str} "
                f"read={final} stage={'0' if match else stage}"
            )

    # Write CSV.
    with CSV_OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "capture", "gt", "read", "raw_read", "match",
                "failure_stage", "all_failed_stages", "notes",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Summary report.
    n_total = len(rows)
    n_failures = n_total - n_correct
    print()
    print(f"Total labeled captures:     {n_total}")
    if n_total:
        print(
            f"Correct reads:              {n_correct} "
            f"({100.0 * n_correct / n_total:.1f}%)"
        )
    print(f"Failures:                   {n_failures}")
    print()
    print("Failure breakdown by stage:")
    stage_names = {
        1: "pill",
        2: "icon",
        3: "crop_box",
        4: "comma missing",
        5: "comma off",
        6: "wrong n_digits",
        7: "digit position",
        8: "CNN class",
        9: "lexicon",
        10: "consensus",
    }
    for s in range(1, 11):
        print(f"  Stage {s} ({stage_names[s]:>14}):  {stage_hist[s]}")

    if n_failures > 0:
        top_stage = max(stage_hist.items(), key=lambda kv: kv[1])
        pct = 100.0 * top_stage[1] / max(1, n_failures)
        print()
        print(
            f"TOP FAILURE STAGE: Stage {top_stage[0]} "
            f"({stage_names[top_stage[0]]}) — {top_stage[1]} captures "
            f"({pct:.1f}% of failures)"
        )
        # Compound-failure analysis: how many failing captures have
        # MULTIPLE stage checks fire? (Even though we attribute the
        # earliest as root cause, this tells us how interconnected the
        # failures are.)
        n_compound = 0
        for r in rows:
            if r["match"] == "1":
                continue
            stages = r.get("all_failed_stages", "") or ""
            if "," in stages:
                n_compound += 1
        print()
        print(
            f"Compound failures: {n_compound} of {n_failures} failing "
            f"captures had >1 stage check fire ({100.0*n_compound/max(1, n_failures):.1f}%)"
        )

    print()
    print(f"CSV: {CSV_OUT}")

    # Per-stage example captures (1-2 per stage).
    print()
    print("Example captures per stage:")
    by_stage: dict[int, list[dict]] = {i: [] for i in range(1, 11)}
    for r in rows:
        if r["match"] == "1":
            continue
        try:
            s = int(r["failure_stage"])
        except ValueError:
            continue
        if s in by_stage and len(by_stage[s]) < 2:
            by_stage[s].append(r)
    for s in range(1, 11):
        if not by_stage[s]:
            continue
        print(f"  Stage {s} ({stage_names[s]}):")
        for r in by_stage[s]:
            print(
                f"    {Path(r['capture']).name} gt={r['gt']} "
                f"read={r['read'] or 'None'} :: {r['notes']}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
