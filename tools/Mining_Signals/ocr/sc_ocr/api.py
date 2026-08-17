"""Public API for SC-OCR.

Signature-compatible replacements for the legacy three-engine
call sites:

    scan_region(region)     →  Optional[int]
    scan_hud_onnx(region)   →  dict[str, Optional[float]]
    scan_refinery(region)   →  Optional[list[dict]]

Architecture:
  capture → polarity-correct → Otsu binarize → find mineral row
  (pure NumPy) → fixed offsets to value rows → _find_value_crop
  (NumPy column-density) → segment glyphs → ONNX batch classify
  → validate

23 ms per scan. No Tesseract. No PaddleOCR. No subprocesses.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# WORKAROUND (Python 3.14 + this host): importing ``scipy.ndimage`` (pulled in
# by ``hud_color_finder`` via ``_find_pill_for_signal`` and the region1 HUD
# path) imports ``numpy.testing``, which calls ``platform`` at import time.
# On 3.14 that routes ``platform.win32_ver`` -> ``_wmi_query`` -> a WMI COM
# query that HANGS for minutes on this machine. The first signature/HUD read
# then stalls 90s+, so the live pipeline looks "frozen / not sending results".
# ``platform._win32_ver`` already catches ``OSError`` from ``_wmi_query`` and
# falls back to a fast registry path, so we force that path by disabling the
# WMI accessor. Effect is cosmetic (Windows version string comes from the
# registry instead of WMI); ``platform.machine()`` is unaffected. Must run
# before any ``scipy``/``numpy.testing`` import, hence the top of this module.
try:  # pragma: no cover - defensive, never fatal
    import platform as _platform_nowmi
    _platform_nowmi._wmi = None
except Exception:
    pass

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

import numpy as np
from PIL import Image

from . import capture, fallback, preprocess, validate

log = logging.getLogger(__name__)

# Pre-compiled regex patterns (module scope)
_RE_NUMERIC_TOKEN = re.compile(r"\d[\d.,]*%?")
_RE_PARENS_GROUP = re.compile(r"\s*\(.*?\)\s*")

# One-shot debug save per field per process session — first successful
# value crop for each of mass/resistance/instability gets saved to
# debug_value_<field>_crop.png at the repo root so we can see what the
# OCR is actually receiving when reads go wrong.
_SAVED_DEBUG: dict[str, bool] = {}

# Cached region2 world model proportions. ``None`` until the first
# lookup; ``False`` if the file is missing so we don't keep retrying.
_WORLD_MODEL_REGION2: object = None


def _load_region2_world_model_for_api() -> Optional[dict]:
    """Load + memoise hud_tracker/world_model_region2.json.

    Lives in the production tree alongside this module
    (``Mining_Signals/hud_tracker/world_model_region2.json``).
    Returns the parsed dict on success, or None when calibration
    hasn't been run yet — caller falls back to its existing heuristic.
    """
    global _WORLD_MODEL_REGION2
    if _WORLD_MODEL_REGION2 is not None:
        return _WORLD_MODEL_REGION2 if _WORLD_MODEL_REGION2 is not False else None

    try:
        import json as _json
        from pathlib import Path as _Path
        # api.py lives at <tool>/ocr/sc_ocr/api.py; world model at
        # <tool>/hud_tracker/world_model_region2.json.
        _wmp = (
            _Path(__file__).resolve().parent.parent.parent
            / "hud_tracker" / "world_model_region2.json"
        )
        if _wmp.is_file():
            with _wmp.open("r", encoding="utf-8") as _fh:
                _WORLD_MODEL_REGION2 = _json.load(_fh)
            log.info(
                "sc_ocr.signal: loaded region2 world model from %s",
                _wmp,
            )
            return _WORLD_MODEL_REGION2  # type: ignore[return-value]
    except Exception as _exc:
        log.debug(
            "sc_ocr.signal: failed to load region2 world model: %s",
            _exc,
        )

    _WORLD_MODEL_REGION2 = False
    return None


def _find_pill_for_signal(rgb_arr) -> Optional[tuple[int, int, int, int]]:
    """Try to locate the signature pill via hud_color_finder.

    Returns ``(x, y, w, h)`` or None. Used by the world-model digit-crop
    derivation in ``_signal_recognize_pil``. Tolerates missing imports
    (the hud_tracker module might not be on sys.path in some deployment
    paths) by returning None and letting the caller fall back.

    Two-pass for background robustness: the pill is a semi-transparent
    badge whose contrast against the world varies with the environment.
    The base color pass occasionally (a) latches the WHOLE panel when a
    saturated teal background falls inside the cyan band — a "pill"
    covering ~90% of the panel that yields a garbage value crop — or
    (b) finds nothing on a desaturated badge. When the base pass
    degenerates or finds nothing we retry with a TIGHTENED pass (higher
    sat/val floors) that excludes washed-out background; if that still
    degenerates we return None rather than emit a panel-spanning box.
    Measured on the labeled pill/icon failures: +4 real pills recovered,
    5 panel-spanning "pills" suppressed, ZERO regression on the working
    set (base runs first and still wins there).
    """
    try:
        from hud_tracker.anchors.hud_color_finder import find_hud_panel
    except Exception as _exc:
        log.debug(
            "sc_ocr.signal: find_hud_panel unavailable for pill anchor: %s",
            _exc,
        )
        return None
    # Region2-tuned calibration mirrors the auto-annotator's
    # PILL_CALIBRATION constant. Pinned here so the runtime gets the
    # same pill geometry filters as the labeller.
    _calib = {
        "version": 1,
        "source": "region2-fallback-defaults",
        "n_captures": 0,
        "cyan_band": {"h_min": 100, "h_max": 180},
        "green_band": {"h_min": 15, "h_max": 60},
        "sat_min": 60,
        "val_min": 80,
        "min_area_px": 600,
        "min_bbox_aspect": 1.5,
        "max_bbox_aspect": 5.5,
        "min_extent": 0.4,
        "morph_seed_iterations": 2,
        "morph_vert_close_px": 3,
        "morph_horiz_close_px": 30,
        "bbox_aspect_peak": 3.5,
    }
    # Tightened fallback: raise the saturation/value floors so a
    # washed-out teal/blue BACKGROUND no longer passes the chrome mask.
    # Only used when the base pass degenerates or finds nothing.
    _calib_tight = dict(_calib)
    _calib_tight.update({
        "source": "region2-tight-fallback", "sat_min": 95, "val_min": 110,
    })
    try:
        _H, _W = int(rgb_arr.shape[0]), int(rgb_arr.shape[1])
    except Exception:
        _H = _W = 0
    _panel_area = float(_W * _H) if (_W and _H) else 0.0

    def _detect(_cal):
        try:
            res = find_hud_panel(rgb_arr, calibration=_cal)
        except Exception as _exc:
            log.debug("sc_ocr.signal: find_hud_panel raised: %s", _exc)
            return None
        if res is None:
            return None
        bbox = res.get("bbox")
        if not bbox or len(bbox) != 4:
            return None
        try:
            return (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
        except (TypeError, ValueError):
            return None

    def _degenerate(bb):
        # A real pill is a small fraction of the panel; a box covering
        # >55% of it is the background-latched failure, not a pill.
        return (bb is not None and _panel_area > 0
                and (bb[2] * bb[3]) > 0.55 * _panel_area)

    bbox = _detect(_calib)
    if bbox is None:
        # Under-detection ONLY (desaturated badge / off-background):
        # retry with a tightened pass that excludes washed-out
        # background. This direction is read-safe — the base found no
        # pill, so there was no read to lose. The OPPOSITE failure (a
        # panel-SPANNING degenerate box) is deliberately left as-is:
        # measured, suppressing or re-tightening a degenerate box
        # REGRESSED reads, because some degenerate boxes still yield the
        # correct world-model value crop by luck (e.g. one capture read
        # 21350 from a 90%-panel box), so that direction is left as-is.
        bbox_tight = _detect(_calib_tight)
        if bbox_tight is not None and not _degenerate(bbox_tight):
            log.debug("sc_ocr.signal: pill recovered by tightened pass")
            return bbox_tight
    return bbox

# Consensus buffer: last 5 raw reads per field. Scans are ~1 Hz so
# 5 entries cover ~5 seconds. The display layer uses 2-of-3 in the
# rolling window to suppress single-frame noise; the LOCK layer
# (further down) requires stricter agreement across the full
# window to avoid locking in garbage.
from collections import deque as _deque
# v2.2.7: reduced from 5 to 3 — at scan_interval_seconds=3 the old
# 5-frame window meant a minimum 15s to lock even with perfect reads.
# 3 frames cover ~9s and still defeat single-frame noise (the dominant
# failure mode the consensus is designed for). All-of-3 agreement
# (preserved via _LOCK_VALUE_AGREEMENT below) keeps the strictness on
# value identity; we just need fewer samples to confirm.
_LOCK_WINDOW = 3  # frames considered for lock decision
_RECENT_READS: dict[str, _deque] = {
    "mass": _deque(maxlen=_LOCK_WINDOW),
    "resistance": _deque(maxlen=_LOCK_WINDOW),
    "instability": _deque(maxlen=_LOCK_WINDOW),
}
# Parallel buffer of downsampled crop images per field. Used by the
# pre-lock verifier to confirm that the CROP itself is stable
# (i.e. the row isn't jumping between a digit row and an unrelated
# UI element like the difficulty progress bar). If the OCR text is
# consistent but the underlying pixels are wildly different, that's
# almost certainly a "trash crop" coincidence and we refuse to lock.
_RECENT_CROPS: dict[str, _deque] = {
    "mass": _deque(maxlen=_LOCK_WINDOW),
    "resistance": _deque(maxlen=_LOCK_WINDOW),
    "instability": _deque(maxlen=_LOCK_WINDOW),
}
# Crop fingerprint size — downsample each crop to this resolution
# before storing, so pairwise comparison is O(N²×64) per field
# instead of O(N²×W×H). 8×24 keeps enough horizontal detail to
# distinguish 4-digit vs 1-digit reads, color stripes, etc.
_CROP_FP_W = 24
_CROP_FP_H = 8
# Required agreement across the full window before locking:
#   * all _LOCK_WINDOW reads must produce the same value
#   * mean pairwise crop similarity (NCC) ≥ this threshold
_LOCK_VALUE_AGREEMENT = _LOCK_WINDOW   # all-of-N (strictest)
# v2.2.7: lowered from 0.85 to 0.70. The 0.85 threshold was rejecting
# many valid lock attempts (user logs showed "crop_sim=0.00 (need
# 0.85)" repeatedly even when OCR readings agreed) because the HUD's
# subpixel wiggle animation moves the value crop ±2px between frames,
# and that's enough to drag NCC below 0.85. 0.70 still rejects
# wildly-different crops (the failure mode this gate guards against)
# but tolerates the wiggle.
_LOCK_CROP_NCC_MIN = 0.70
# Last displayed (stabilized) value per field. When a scan produces
# a one-off outlier we stick with this until the buffer confirms a
# new value.
_STABLE_VALUE: dict[str, Optional[float]] = {
    "mass": None, "resistance": None, "instability": None,
}

# ── Persistence-bias state ───────────────────────────────────────
# When the displayed value has been the SAME for ≥_PERSIST_STREAK_MIN
# consecutive scans, it becomes "sticky" — a new disagreeing read needs
# either high confidence OR multi-source confirmation to overwrite it.
# This defends against the 2-of-3 consensus rule flipping on a single
# high-confidence bad read followed by one corroborating misread.
#
# State per field:
#   * ``_PERSIST_STREAK[field]`` — consecutive-equal-read counter. Bumps
#     when the current displayed value equals the previous displayed
#     value; resets to 1 when a different value sticks.
#   * ``_PERSIST_LAST[field]``  — the value that the streak is tracking.
#     Used to detect "same vs different" without depending on the
#     consensus buffer, which can return None on transient OCR misses.
#
# Lifecycle: reset by ``reset_all_consensus()`` / ``_reset_consensus_buffers``
# (same trigger as the lock cache), since stickiness is per-rock.
_PERSIST_STREAK_MIN = 4    # become sticky after this many equal reads
_PERSIST_CONF_HIGH = 0.92  # confidence bar to override a sticky value
_PERSIST_VOTER_MIN = 2     # min voters needed to override at low conf
_PERSIST_STREAK: dict[str, int] = {
    "mass": 0, "resistance": 0, "instability": 0,
}
_PERSIST_LAST: dict[str, Optional[float]] = {
    "mass": None, "resistance": None, "instability": None,
}


def _persistence_check(
    field: str,
    v_new: float,
    conf: float,
    voter_agreement_count: int,
) -> tuple[bool, str]:
    """Decide whether ``v_new`` may overwrite a sticky displayed value.

    Returns ``(display_new, reason)``:
      * ``display_new=True``  → caller should display ``v_new``.
      * ``display_new=False`` → caller should hold the sticky value;
        ``v_new`` is still buffered (so eventually a real change will
        accumulate enough evidence to flip the streak naturally).

    Stickiness rule:
      * If the streak counter for ``field`` is < ``_PERSIST_STREAK_MIN``
        the field isn't sticky yet — always allow the update.
      * If the streak is ≥ ``_PERSIST_STREAK_MIN`` AND ``v_new`` differs
        from the last displayed value (``_PERSIST_LAST[field]``),
        require either:
          (a) ``conf >= _PERSIST_CONF_HIGH`` (high confidence wins), OR
          (b) ``voter_agreement_count >= _PERSIST_VOTER_MIN``
              (multi-source agreement wins).
        Otherwise the sticky value is preserved.

    The streak counter / last-value bookkeeping is NOT touched here —
    the caller updates it AFTER deciding what to actually display
    (see :func:`_persistence_track_streak`). This separation keeps the
    helper a pure decision function for testability.
    """
    # Periodic-reverify bypass: if the lock-reverify path just
    # invalidated a stale lock with a high-confidence disagreement,
    # `_REVERIFY_BYPASS[field]` holds the fresh value. For ONE scan,
    # allow that value through unconditionally so the corrected read
    # doesn't have to fight any leftover sticky-streak inertia. The
    # entry is consumed (cleared to None) once it matches v_new.
    bypass_val = _REVERIFY_BYPASS.get(field)
    if bypass_val is not None:
        try:
            if float(bypass_val) == float(v_new):
                _REVERIFY_BYPASS[field] = None
                return True, ""
        except (TypeError, ValueError):
            pass

    streak = _PERSIST_STREAK.get(field, 0)
    last = _PERSIST_LAST.get(field)
    if last is None or streak < _PERSIST_STREAK_MIN:
        return True, ""
    try:
        if float(v_new) == float(last):
            return True, ""
    except (TypeError, ValueError):
        return True, ""
    # Disagreement against a sticky value — require strong evidence.
    try:
        conf_f = float(conf)
    except (TypeError, ValueError):
        conf_f = 0.0
    if conf_f >= _PERSIST_CONF_HIGH:
        return True, ""
    if voter_agreement_count >= _PERSIST_VOTER_MIN:
        return True, ""
    return (
        False,
        f"sticky streak={streak} requires conf>={_PERSIST_CONF_HIGH} "
        f"or voters>={_PERSIST_VOTER_MIN} (got conf={conf_f:.2f} "
        f"voters={voter_agreement_count})",
    )


def _persistence_track_streak(field: str, displayed: Optional[float]) -> None:
    """Update the streak counter after a display decision is made.

    Caller passes the value that's actually being SHOWN this scan
    (which may be either the new candidate or the sticky hold). The
    counter is incremented when it matches the previous tracked value,
    reset to 1 otherwise.
    """
    if displayed is None:
        return
    try:
        v = float(displayed)
    except (TypeError, ValueError):
        return
    last = _PERSIST_LAST.get(field)
    if last is not None and float(last) == v:
        _PERSIST_STREAK[field] = _PERSIST_STREAK.get(field, 0) + 1
    else:
        _PERSIST_STREAK[field] = 1
    _PERSIST_LAST[field] = v


def _persistence_smoke_test() -> None:
    """Inline assertions for the persistence-bias logic.

    Not invoked at import — call via ``python -c "from ocr.sc_ocr.api
    import _persistence_smoke_test; _persistence_smoke_test()"``.
    Mutates the module-level streak/last dicts, so callers should
    plan to reset them afterward if running inside a live process
    (the test resets to None / 0 on exit).
    """
    # Save & clear state.
    saved_streak = dict(_PERSIST_STREAK)
    saved_last = dict(_PERSIST_LAST)
    try:
        for k in _PERSIST_STREAK:
            _PERSIST_STREAK[k] = 0
        for k in _PERSIST_LAST:
            _PERSIST_LAST[k] = None

        # Cold start: no streak yet → always allow.
        ok, _ = _persistence_check("mass", 15683.0, 0.5, 1)
        assert ok, "cold start should pass"

        # Build up a 4-read streak on the same value.
        for _ in range(4):
            _persistence_track_streak("mass", 15683.0)
        assert _PERSIST_STREAK["mass"] == 4, "streak should reach 4"

        # Same value → always allow.
        ok, _ = _persistence_check("mass", 15683.0, 0.1, 0)
        assert ok, "same value passes regardless of conf/voters"

        # Disagreeing low-conf low-voter → REJECT.
        ok, why = _persistence_check("mass", 21449.0, 0.71, 1)
        assert not ok, "weak disagreeing read should be held off"
        assert "sticky" in why

        # Disagreeing high-conf → ALLOW.
        ok, _ = _persistence_check("mass", 21449.0, 0.95, 1)
        assert ok, "high-conf override should bypass stickiness"

        # Disagreeing low-conf BUT 2+ voters → ALLOW.
        ok, _ = _persistence_check("mass", 21449.0, 0.5, 2)
        assert ok, "voter-confirmed disagreement bypasses stickiness"

        # Streak resets after a flip is displayed.
        _persistence_track_streak("mass", 21449.0)
        assert _PERSIST_STREAK["mass"] == 1, "streak resets on flip"

        # Pre-sticky (streak < 4) always passes.
        _PERSIST_STREAK["resistance"] = 3
        _PERSIST_LAST["resistance"] = 46.0
        ok, _ = _persistence_check("resistance", 80.0, 0.1, 0)
        assert ok, "streak<4 means not-yet-sticky"

        print("api._persistence_smoke_test: all assertions passed")
    finally:
        _PERSIST_STREAK.update(saved_streak)
        _PERSIST_LAST.update(saved_last)


def _lock_reverify_smoke_test() -> None:
    """Inline assertions for the periodic lock re-verification logic.

    Validates the three terminal verdicts of ``_lock_reverify_field``:
      * INVALIDATED — fresh high-conf disagreement clears the lock and
        propagates the fresh value.
      * CONFIRMED  — fresh agreeing read resets the counter and keeps
        the lock.
      * NO_READ    — fresh empty / low-conf read keeps the lock and
        leaves the counter incremented.

    Also exercises:
      * Counter increment + threshold trip integrated against the
        `_field_lock_cache` shape used by the ALL LOCKED fast path.
      * `_REVERIFY_BYPASS` handshake with `_persistence_check`.

    Run via:
        python -c "from ocr.sc_ocr.api import _lock_reverify_smoke_test; \\
                   _lock_reverify_smoke_test()"
    """
    import ocr.sc_ocr.api as _self_mod  # patch _ocr_value_crop in-place

    # Save & clear all touched module state.
    saved_lock_cache = OrderedDict(_field_lock_cache)
    saved_scans = dict(_scans_since_lock_verify)
    saved_bypass = dict(_REVERIFY_BYPASS)
    saved_persist_streak = dict(_PERSIST_STREAK)
    saved_persist_last = dict(_PERSIST_LAST)
    saved_ocr_value_crop = _self_mod._ocr_value_crop

    try:
        # ── Fake locked state: a single region with all three fields
        # locked to the values from the original bug report.
        _field_lock_cache.clear()
        fake_region_key = (0, 0, 800, 600)
        _field_lock_cache[fake_region_key] = {
            "mass": (54663.0, None),
            "resistance": (82.0, None),
            "instability": (52.33, None),
        }
        for k in _scans_since_lock_verify:
            _scans_since_lock_verify[k] = 0
        for k in _REVERIFY_BYPASS:
            _REVERIFY_BYPASS[k] = None
        for k in _PERSIST_STREAK:
            _PERSIST_STREAK[k] = 0
        for k in _PERSIST_LAST:
            _PERSIST_LAST[k] = None

        # Place the instability counter over the threshold so the
        # reverify branch trips on next read.
        _scans_since_lock_verify["instability"] = 6
        assert _scans_since_lock_verify["instability"] >= _REVERIFY_THRESHOLD, (
            "test setup: counter must be over threshold"
        )

        # Dummy crop (1×1 RGB) — we mock _ocr_value_crop so the
        # actual pixels don't matter.
        dummy_crop = Image.new("RGB", (8, 16), (0, 0, 0))

        # ── Case 1: INVALIDATED ──
        # Fresh OCR returns "523.33" at mean_conf 0.92 — high-conf
        # disagreement with locked 52.33.
        def _mock_ocr_high_conf(value_crop, field=""):
            assert field == "instability"
            return ("523.33", [0.92, 0.92, 0.92, 0.92, 0.92, 0.92])

        _self_mod._ocr_value_crop = _mock_ocr_high_conf
        verdict, fresh, mean = _lock_reverify_field(
            dummy_crop, "instability", 52.33,
        )
        assert verdict == "INVALIDATED", f"expected INVALIDATED got {verdict}"
        assert fresh == 523.33, f"expected fresh=523.33 got {fresh}"
        assert mean >= _REVERIFY_CONF_MIN, f"mean conf below floor: {mean}"

        # ── Case 2: CONFIRMED ──
        # Fresh OCR returns the locked value — should reset counter.
        def _mock_ocr_agreeing(value_crop, field=""):
            return ("52.33", [0.95, 0.95, 0.95, 0.95, 0.95])

        _self_mod._ocr_value_crop = _mock_ocr_agreeing
        verdict, fresh, mean = _lock_reverify_field(
            dummy_crop, "instability", 52.33,
        )
        assert verdict == "CONFIRMED", f"expected CONFIRMED got {verdict}"
        assert fresh == 52.33

        # ── Case 3: NO_READ — empty OCR result ──
        def _mock_ocr_empty(value_crop, field=""):
            return ("", [])

        _self_mod._ocr_value_crop = _mock_ocr_empty
        verdict, fresh, mean = _lock_reverify_field(
            dummy_crop, "instability", 52.33,
        )
        assert verdict == "NO_READ", f"expected NO_READ got {verdict}"
        assert fresh is None
        assert mean == 0.0

        # ── Case 4: NO_READ — low-conf disagreement (should NOT
        # override the lock — we don't trust it enough).
        def _mock_ocr_low_conf(value_crop, field=""):
            return ("523.33", [0.50, 0.50, 0.50, 0.50, 0.50, 0.50])

        _self_mod._ocr_value_crop = _mock_ocr_low_conf
        verdict, fresh, mean = _lock_reverify_field(
            dummy_crop, "instability", 52.33,
        )
        assert verdict == "NO_READ", (
            f"low-conf disagreement should be NO_READ got {verdict}"
        )
        # fresh value is still surfaced for telemetry but lock holds.
        assert fresh == 523.33
        assert mean < _REVERIFY_CONF_MIN

        # ── Case 5: persistence-bias bypass handshake ──
        # Without bypass, a sticky streak on the wrong value would
        # reject a low-conf disagreement.
        _PERSIST_STREAK["instability"] = 4
        _PERSIST_LAST["instability"] = 52.33
        ok, why = _persistence_check("instability", 523.33, 0.50, 1)
        assert not ok, (
            "baseline: sticky streak should reject low-conf "
            "disagreement (got ok)"
        )

        # Set the bypass to the fresh value and re-check — now it
        # should pass through.
        _REVERIFY_BYPASS["instability"] = 523.33
        ok, why = _persistence_check("instability", 523.33, 0.50, 1)
        assert ok, (
            "with reverify bypass set, fresh value should pass "
            "even against sticky streak"
        )
        # The bypass should have been consumed.
        assert _REVERIFY_BYPASS["instability"] is None, (
            "bypass should clear after one successful read"
        )

        # ── Case 6: _reset_consensus_buffers clears the counter ──
        _scans_since_lock_verify["instability"] = 7
        _REVERIFY_BYPASS["mass"] = 99.0
        # _reset_consensus_buffers also clears other state — we just
        # care about the new dict here.
        _reset_consensus_buffers()
        assert _scans_since_lock_verify["instability"] == 0, (
            "reset should zero the counter"
        )
        assert _REVERIFY_BYPASS["mass"] is None, (
            "reset should clear the bypass"
        )

        print("api._lock_reverify_smoke_test: all assertions passed")
    finally:
        # Restore module state exactly as we found it.
        _field_lock_cache.clear()
        _field_lock_cache.update(saved_lock_cache)
        _scans_since_lock_verify.update(saved_scans)
        _REVERIFY_BYPASS.update(saved_bypass)
        _PERSIST_STREAK.update(saved_persist_streak)
        _PERSIST_LAST.update(saved_persist_last)
        _self_mod._ocr_value_crop = saved_ocr_value_crop


# ──────────────────────────────────────────
# Signal (signature scanner) consensus
# ──────────────────────────────────────────
# Mining HUD jitter (~1-3 px subpixel animation at ~3 Hz) makes per-frame
# Tesseract reads on the signal cluster swing between adjacent values,
# e.g. 17,020 ↔ 17,011 across consecutive frames even though the rock's
# true signature is constant. We dampen this with a small rolling buffer
# of the last N raw reads and require K-of-N agreement before swapping
# the displayed value.
# v2.2.7: tightened from 6/4 to 4/3 — same ratio, fewer total scans
# needed before a signal swap. At scan_interval_seconds=3 this drops
# minimum lock time from ~12s to ~9s. The dual-polarity CNN voter still
# protects against single-frame misreads independently of buffer size.
_SIGNAL_BUFFER_LEN = 8            # remember last 8 raw reads
_SIGNAL_AGREEMENT_REQ = 3         # require 3 agreeing reads before swap
# Sticky-table agreement: when the current stable value is in the chart
# AND the candidate new value is not, require this many consecutive
# reads of the new value before swapping. Defends against Tesseract's
# intermittent leading-digit drops (e.g. "11,700" → "1700") that would
# otherwise flip the display to a plausible-looking non-table number
# after just _SIGNAL_AGREEMENT_REQ frames.
_SIGNAL_STICKY_AGREEMENT_REQ = 6
# Stricter agreement (was 2) so single-frame OCR misreads — like
# 5-vs-6 ambiguity that flips 11,565 → 11,655 — can't swap the
# stable signal value. With ~1.5-3 s scan cadence, requiring 4
# consecutive identical reads imposes ~6-12 s of stability before
# the displayed value follows the OCR. Combined with the dual-
# polarity CNN voter below, real value changes still propagate
# within that window because both reads agree on the new value.
_RECENT_SIGNAL_READS: _deque = _deque(maxlen=_SIGNAL_BUFFER_LEN)
_STABLE_SIGNAL: Optional[int] = None

# ── Scanning timeout — revert to "scanning" state when the icon
# disappears for too long ──
#
# Even when the user has clearly looked away from the rock — the
# location-pin icon and the digit cluster have both vanished — the
# consensus / lock cache holds the last successfully-read value
# indefinitely. Without a freshness gate, the displayed number sits
# on stale OCR while the panel chrome (or even just an empty
# desktop) is on screen.
#
# We treat each scan tick as either "icon seen" or "no icon".
# A scan marks the icon as seen iff one of the structural anchor
# branches (world-model + pill with icon refinement, localize_icon
# consensus, find_digit_crop_box mode in {"combo", "digit_only"})
# located the icon for this frame. If no scan has marked the icon
# as seen within the last ``_SIGNAL_SCANNING_TIMEOUT_SEC`` seconds,
# ``_signal_recognize_pil`` resets the signature consensus and
# returns None so the upstream UI reverts to its "scanning"
# placeholder. The 3.0 s threshold matches the default scan cadence
# (1 missed scan tolerated, 2 missed scans triggers the reset),
# striking a balance between flicker on a single bad frame and a
# stale value on real user disengagement.
#
# Initial state: ``0.0`` means ``monotonic() - 0.0`` is huge, so the
# very first call fires the timeout unless the icon-detection step
# updates the timestamp first. That's the intended "cold-start
# starts in scanning state" behaviour.
_signal_last_icon_seen_ts: float = 0.0
_SIGNAL_SCANNING_TIMEOUT_SEC: float = 3.0


def _signal_mark_icon_seen() -> None:
    """Mark the current monotonic time as the most recent successful
    signature icon / pill anchor detection. Called from each
    structural anchor branch in ``_signal_recognize_pil`` that
    produced a valid crop with verifiable icon evidence.

    Cheap module-state write — caller already holds the per-tick
    scan serialization (one signature scan in flight at a time)."""
    global _signal_last_icon_seen_ts
    _signal_last_icon_seen_ts = time.monotonic()


def _signal_scanning_timeout_exceeded() -> bool:
    """Return True when the gap since the last successful icon
    detection exceeds ``_SIGNAL_SCANNING_TIMEOUT_SEC``. Caller is
    responsible for ``_reset_signal_consensus()`` and returning None
    to its caller — this is a pure predicate so it's also useful
    from the inline smoke test."""
    return (
        time.monotonic() - _signal_last_icon_seen_ts
        > _SIGNAL_SCANNING_TIMEOUT_SEC
    )


def _signal_scanning_timeout_smoke_test() -> None:
    """Inline assertions for the scanning-timeout behaviour.

    Not invoked at import — call via ``python -c "from ocr.sc_ocr.api
    import _signal_scanning_timeout_smoke_test as t; t()"``. Mutates
    the module-level timestamp + consensus state, then restores both
    on exit so a live process retains its pre-test state.

    Total runtime ~4 s due to the deliberate ``sleep(3+)`` step that
    proves the timeout fires after the configured window.
    """
    global _signal_last_icon_seen_ts
    saved_ts = _signal_last_icon_seen_ts
    saved_stable = _STABLE_SIGNAL
    saved_recent = list(_RECENT_SIGNAL_READS)
    try:
        # Step 1: fresh-state reset → timeout should fire immediately
        # (timestamp is 0.0, monotonic() is huge).
        _signal_last_icon_seen_ts = 0.0
        assert _signal_scanning_timeout_exceeded(), (
            "cold-start state should report timeout exceeded"
        )

        # Step 2: simulate a successful icon detection.
        before = time.monotonic()
        _signal_mark_icon_seen()
        after = time.monotonic()
        assert before <= _signal_last_icon_seen_ts <= after, (
            "_signal_mark_icon_seen should update the timestamp to "
            "the current monotonic time"
        )
        assert not _signal_scanning_timeout_exceeded(), (
            "immediately after icon-seen, timeout must NOT fire"
        )

        # Step 3: 1 s later — still within the 3 s window.
        time.sleep(1.0)
        assert not _signal_scanning_timeout_exceeded(), (
            "1 s after icon-seen, timeout must NOT fire (window=3s)"
        )

        # Step 4: 3 more seconds (total 4 s since icon-seen) → timeout
        # MUST fire. Verify the reset clears consensus too.
        _RECENT_SIGNAL_READS.clear()
        _RECENT_SIGNAL_READS.append(12345)
        _RECENT_SIGNAL_READS.append(12345)
        globals()["_STABLE_SIGNAL"] = 12345
        time.sleep(3.05)
        assert _signal_scanning_timeout_exceeded(), (
            "4 s after icon-seen, timeout MUST fire (window=3s)"
        )
        _reset_signal_consensus()
        assert _STABLE_SIGNAL is None, (
            "_reset_signal_consensus should clear _STABLE_SIGNAL"
        )
        assert len(_RECENT_SIGNAL_READS) == 0, (
            "_reset_signal_consensus should clear the recent reads "
            "deque"
        )
        assert _signal_last_icon_seen_ts == 0.0, (
            "_reset_signal_consensus should also zero "
            "_signal_last_icon_seen_ts"
        )

        print("api._signal_scanning_timeout_smoke_test: all assertions passed")
    finally:
        _signal_last_icon_seen_ts = saved_ts
        globals()["_STABLE_SIGNAL"] = saved_stable
        _RECENT_SIGNAL_READS.clear()
        for v in saved_recent:
            _RECENT_SIGNAL_READS.append(v)


# Telemetry: signal-region-relative crop box used by the most recent
# ``_signal_recognize_pil`` call. Populated as a 4-tuple ``[x, y, w, h]``
# whenever the icon anchor (or manual calibration) successfully
# located the digit cluster. Empty list when the heuristic-mask
# fallback path was used (no precise box available). Read by the
# Calibration Dialog so its "signature" row can show absolute coords
# in the live preview AND seed Lock with a real rectangle instead of
# the placeholder.
_LAST_SIGNAL_CROP_BOX: list[int] = []
# Digit-grid anchor (the voted comma's absolute x in signal-region px),
# stashed per scan for the region2 nervous system. Mutable holder so it
# can be set from inside _signal_recognize_pil without a global decl —
# same pattern as _LAST_SIGNAL_CROP_BOX. Empty = no comma this scan.
_LAST_SIGNAL_COMMA_X: list[float] = []
# Units-glyph terminal-digit hint (0 or 5) for the 0/5 structural snap,
# stashed per scan by the per-glyph classifier when it ran. Same
# mutable-holder pattern. Empty = no per-glyph hint this scan, so the
# snap falls back to the lexicon / nearest-by-value. See
# _snap_signal_units_05.
_LAST_SIGNAL_UNITS_HINT: list[int] = []


def get_last_signal_crop_box() -> Optional[dict]:
    """Public accessor for the signature row's last known crop box.

    Returns ``{"x": int, "y": int, "w": int, "h": int}`` in signal-
    region coordinates, or None if no scan has produced a precise box
    yet (cold start, heuristic-mask fallback, anchor miss).
    """
    if len(_LAST_SIGNAL_CROP_BOX) != 4:
        return None
    x, y, w, h = _LAST_SIGNAL_CROP_BOX
    return {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}

# Known-signature value set, populated from the mining chart data via
# ``set_known_signal_values()``. Used as a tie-breaker AND as a sanity
# floor in the variant voter: if ANY variant's read exact-matches a
# known signature value, we strongly prefer it over arbitrary in-range
# numbers. Empty set = no preference applied (fail-open behaviour).
_KNOWN_SIGNAL_VALUES: set[int] = set()


_VERIFIED_SIGNAL_VALUES_CACHE: Optional[set[int]] = None


def _load_verified_signal_values() -> set[int]:
    """Load user-verified signature values from the on-disk cache.

    Returns a set of integer signature values built by
    ``scripts/build_verified_signals_cache.py`` walking the approved
    Glyph Forge sidecars (``review_status == "approved"``). This is
    the "ground truth" set the user has manually confirmed via the
    Row Reviewer — values that DEFINITELY exist as real mining
    signatures even when the chart-export data hasn't been
    re-extracted to include them yet.

    The chart-derived lexicon is built from a multiplier expansion
    of the chart's ``scanSignature`` bases (``base * n`` for n in
    ``1..25``). That covers most signatures, but specific
    multiplier-base combinations are missing — empirically: 10000,
    26000, 14000, 21565, 8276, 2000. Unioning the verified cache
    closes the gap without requiring a new chart export.

    Cached in module state after first call so the disk read
    doesn't repeat per signal-recognize tick. Safe to call from
    any context — returns ``set()`` on any failure.
    """
    global _VERIFIED_SIGNAL_VALUES_CACHE
    if _VERIFIED_SIGNAL_VALUES_CACHE is not None:
        return _VERIFIED_SIGNAL_VALUES_CACHE
    from pathlib import Path as _Path
    # Search the tree root (per-user installs typically point at one
    # of the SC_Toolbox / WingmanAI roots). The api.py file lives at
    # ``<tree>/ocr/sc_ocr/api.py``, so .parent.parent.parent is the
    # tree root. Mirror of the world-model loader.
    candidates = [
        _Path.home() / ".mining_signals_verified.json",
        _Path(__file__).resolve().parent.parent.parent / ".mining_signals_verified.json",
    ]
    for p in candidates:
        try:
            if not p.is_file():
                continue
            import json as _json
            data = _json.loads(p.read_text(encoding="utf-8"))
            values = data.get("values") if isinstance(data, dict) else data
            if not isinstance(values, list):
                continue
            verified = {int(v) for v in values if isinstance(v, (int, float, str)) and str(v).strip()}
            log.info(
                "sc_ocr: loaded %d user-verified signal values from %s",
                len(verified), p,
            )
            _VERIFIED_SIGNAL_VALUES_CACHE = verified
            return verified
        except Exception as exc:
            log.debug("sc_ocr: verified-signals load failed at %s: %s", p, exc)
    _VERIFIED_SIGNAL_VALUES_CACHE = set()
    return _VERIFIED_SIGNAL_VALUES_CACHE


def set_known_signal_values(values) -> None:
    """Register the set of all valid signature values from the mining
    chart. Called from ``ui/app.py:_on_data_loaded`` after the
    chart rows are loaded.

    The voter uses this set as a tie-breaker: if among the 6 PSM ×
    scale Tesseract variants two produce ``17020`` (a known Silicon
    × 4-rocks value) and one produces ``17011`` (not in any known
    table), the voter returns ``17020`` even before majority is
    reached. This kills the dominant flicker pattern outright.

    The chart-derived set is UNIONED with the on-disk verified
    cache (``.mining_signals_verified.json``) so user-confirmed
    signatures that don't appear in the chart's multiplier expansion
    still pass the lexicon check. See ``_load_verified_signal_values``.
    """
    global _KNOWN_SIGNAL_VALUES
    base = {int(v) for v in values if v}
    verified = _load_verified_signal_values()
    new_set = base | verified
    # Canonical signatures end in 0/5 (the structural prior; verified
    # 184/184 across the chart + signal caches). Drop any non-0/5 entry:
    # those are transient mid-scan readouts that leaked into the verified
    # cache (e.g. 8276 — a fluctuating far-scan value the Row Reviewer
    # approved before we knew the live HUD fluctuates). Keeping them would
    # bias the variant voter — which "strongly prefers a known value" —
    # toward the very misreads _snap_signal_units_05 exists to correct.
    _non05 = {v for v in new_set if v % 10 not in (0, 5)}
    if _non05:
        log.info(
            "sc_ocr: lexicon dropped %d non-0/5 entr%s (transient "
            "pollution): %s",
            len(_non05), "y" if len(_non05) == 1 else "ies",
            sorted(_non05)[:10],
        )
        new_set -= _non05
    extra = len(new_set) - len(base)
    if extra > 0:
        log.info(
            "sc_ocr: lexicon expanded by %d user-verified values "
            "(chart=%d + verified=%d -> union=%d)",
            extra, len(base), len(verified), len(new_set),
        )
    _KNOWN_SIGNAL_VALUES = new_set


# ── Units-digit structural prior (region2 signatures) ────────────────
# Every SETTLED rock signature in the mining chart ends in 0 or 5
# (verified 184/184 across the chart + signal-value caches). A read
# whose units digit is not 0/5 is therefore a misread of the last glyph
# — or a transient mid-scan readout of a still-settling rock (the live
# HUD genuinely fluctuates through non-0/5 values while the rock is far
# / unsettled, e.g. 8276→8274→8271 at 12.9 km). Per the owner's call we
# always snap such reads to their canonical 0/5 sibling so chart-matching
# lands on the real value. We NEVER roll into the next ten — only the
# last digit was wrong, the tens-and-up are trusted. The 0-vs-5 choice
# is disambiguated strongest-evidence-first: (1) the known-value lexicon,
# (2) the units glyph's own CNN preference, (3) nearest by digit value.
def _snap_signal_units_05(value, units_hint=None):
    """Snap a signature read's units digit to the canonical 0 or 5.

    ``value``       the read integer (or None — returned unchanged)
    ``units_hint``  the per-glyph CNN's preferred terminal digit (0 or
                    5) for the last position, used only when the lexicon
                    can't disambiguate. None = no hint this scan.

    Returns ``value`` unchanged when it already ends in 0/5.
    """
    if value is None:
        return value
    u = value % 10
    if u == 0 or u == 5:
        return value
    base = value - u
    cand0, cand5 = base, base + 5
    # 1) Lexicon — promote a KNOWN signature, never invent one. Only
    #    decisive when exactly one sibling is a known value (the other
    #    isn't), so we can't be fooled by a polluted non-0/5 entry.
    known = _KNOWN_SIGNAL_VALUES
    if known:
        in0, in5 = cand0 in known, cand5 in known
        if in0 and not in5:
            return cand0
        if in5 and not in0:
            return cand5
    # 2) The units glyph's own CNN 0-vs-5 preference (the units anchor).
    if units_hint == 0:
        return cand0
    if units_hint == 5:
        return cand5
    # 3) Nearest 0/5 by digit value (1,2→0; 3,4,6,7,8,9→5). Never rolls
    #    into the next ten.
    return cand0 if u <= 2 else cand5


def _reset_signal_consensus() -> None:
    """Clear the signal consensus buffer. Call when the user changes
    rocks or the signature panel disappears.

    Also resets ``_signal_last_icon_seen_ts`` to 0.0 so a fresh scan
    cycle starts from the "no icon seen yet" cold-start state — the
    next icon detection will set the timestamp, and the scanning
    timeout gate behaves correctly without leaking the previous
    rock's icon-seen recency into a freshly-reset session."""
    global _STABLE_SIGNAL, _signal_last_icon_seen_ts
    _RECENT_SIGNAL_READS.clear()
    _STABLE_SIGNAL = None
    _signal_last_icon_seen_ts = 0.0


def _reset_consensus_buffers() -> None:
    """Clear all consensus buffers. Called when the panel disappears
    (user stopped looking at a scan result) so the next rock's reads
    aren't contaminated by the previous rock's values."""
    for b in _RECENT_READS.values():
        b.clear()
    for b in _RECENT_CROPS.values():
        b.clear()
    for k in _STABLE_VALUE:
        _STABLE_VALUE[k] = None
    # Persistence-bias is per-rock — clear it on rock change too.
    for k in _PERSIST_STREAK:
        _PERSIST_STREAK[k] = 0
    for k in _PERSIST_LAST:
        _PERSIST_LAST[k] = None
    # Periodic lock-reverify counter is per-rock — when the lock cache
    # is cleared, the counter has nothing left to count against.
    for k in _scans_since_lock_verify:
        _scans_since_lock_verify[k] = 0
    for k in _REVERIFY_BYPASS:
        _REVERIFY_BYPASS[k] = None
    _field_lock_cache.clear()
    _difficulty_cache.clear()
    # Also drop the signal consensus — same lifecycle. If the user
    # looked away from the rock, the next rock starts fresh.
    _reset_signal_consensus()
    # Anchor tracker too: a new rock can be at a different panel
    # position (player moved view between scans). Smoothing toward the
    # old rock's anchor would force the new rock's panel rows through
    # the slow outlier-hysteresis path instead of snapping immediately.
    try:
        from . import scan_results_match as _srm
        _srm.reset_anchor_tracker()
    except Exception as _srm_exc:
        log.debug("sc_ocr.api: anchor tracker reset swallowed: %s", _srm_exc)
    # And the (W, H)-keyed _scan_results_anchor_cache in
    # onnx_hud_reader: it's intentionally per-image-SIZE not per-image-
    # CONTENT, so two distinct rocks at the same capture resolution
    # share a cache slot. Without this clear, the first rock's cached
    # anchor leaks onto the second rock's first scan and the row
    # finder lays out cyan bands at the previous panel's coordinates
    # — visible in the benchmark as offset-by-N-rows mismatches.
    try:
        from .. import onnx_hud_reader as _ohr
        _ohr._scan_results_anchor_cache.clear()
    except Exception as _ohr_exc:
        log.debug(
            "sc_ocr.api: scan_results_anchor_cache clear swallowed: %s",
            _ohr_exc,
        )


# ── Cross-scan value-consistency reflex (field-generic) ──────────────
# Live 2026-06-12: resistance flapped 34 -> 100 -> 11 -> 54 across scans
# of an UNCHANGED panel because the HUD-RGB CRNN gate accepted weak
# reads (down to 0.49 "plausible") and bypassed the per-glyph vote.
# The flap signature is already encoded in the existing buffers:
# window reads DISAGREE (unanimous is None) while the crop pixels are
# STABLE (crop_ok). When that happens, the field loses its gate-bypass
# privileges for a few scans and every read goes through the full
# vote — one mechanism for ALL fields, replacing per-field patches.
_BYPASS_REVOKE_SCANS = 10
_bypass_revoked_until: dict = {}
_consistency_scan_n: dict = {}


def _consistency_reflex(field, unanimous, crop_ok, crop_sim) -> None:
    """Note one scan's (field, agreement, crop-stability) and revoke
    gate bypasses when reads flap on visually-stable pixels."""
    n = _consistency_scan_n.get(field, 0) + 1
    _consistency_scan_n[field] = n
    if crop_ok and unanimous is None:
        prev = _bypass_revoked_until.get(field, 0)
        _bypass_revoked_until[field] = n + _BYPASS_REVOKE_SCANS
        if prev <= n:
            log.info(
                "sc_ocr: CONSISTENCY REFLEX field=%s — reads disagree "
                "on a visually-stable crop (sim=%.2f): gate bypasses "
                "revoked for %d scans (full vote required)",
                field, crop_sim, _BYPASS_REVOKE_SCANS,
            )


def _bypass_revoked(field) -> bool:
    return (
        _consistency_scan_n.get(field, 0)
        < _bypass_revoked_until.get(field, 0)
    )


def reset_all_consensus() -> None:
    """Public escape hatch — clear ALL module-level consensus and lock
    state so the next scan starts completely fresh.

    Wired to the Calibrate dialog's "Reset consensus" button. Use when
    a stuck stable-swap lock (4-of-6 signal agreement, all-of-5 HUD
    field agreement, plus per-region field-lock and difficulty caches)
    is holding a wrong value and the user wants to break it without
    closing the panel / changing rocks.

    Safe to call concurrently with an in-flight scan: clearing the
    deques mid-scan just means the next-completing scan repopulates
    them from scratch. No locks held.
    """
    _reset_consensus_buffers()
    _bypass_revoked_until.clear()
    _consistency_scan_n.clear()
    log.info("sc_ocr.api: reset_all_consensus() — buffers/caches cleared")


def _on_anchor_baseline_reset() -> None:
    """Called by ``scan_results_match`` when the tracker accepts a
    sustained panel-position jump.

    The anchor's smoothed position just snapped to a new location.
    Every lock state populated from the OLD anchor's crop pixels is
    now suspect — the stored fingerprints reference a region that's
    no longer where the title (and therefore the value rows) live.
    Clearing the lock cache forces the next scan to read fresh value
    crops from the new position and re-acquire locks honestly.

    Note: ``_difficulty_cache`` is INTENTIONALLY not cleared. Difficulty
    is a property of the rock itself, not of the title-anchor position.
    A panel-position jump (e.g. player turned view) doesn't change
    which rock is being scanned, and the difficulty cache is per-region
    not per-anchor-pixel-region — so it remains valid.
    """
    log.info(
        "sc_ocr: anchor tracker re-baselined — invalidating field locks"
    )
    _field_lock_cache.clear()
    # Mirror the relevant parts of _reset_consensus_buffers() that are
    # tied to the title-anchor's crop pixels: stable consensus,
    # rolling read history, crop fingerprints, persistence streaks.
    for b in _RECENT_READS.values():
        b.clear()
    for b in _RECENT_CROPS.values():
        b.clear()
    for k in _STABLE_VALUE:
        _STABLE_VALUE[k] = None
    for k in _PERSIST_STREAK:
        _PERSIST_STREAK[k] = 0
    for k in _PERSIST_LAST:
        _PERSIST_LAST[k] = None


# Register the anchor-baseline-reset handler. The tracker fires
# this callback when its outlier-hysteresis branch confirms a
# sustained large jump. We register at module import time so the
# wiring is in place before the first scan; failures here are
# swallowed because the callback hook is optional infrastructure
# — the tracker still works without it.
try:
    from . import scan_results_match as _srm_for_cb
    _srm_for_cb.register_baseline_reset_callback(
        _on_anchor_baseline_reset
    )
except Exception:
    log.debug("sc_ocr.api: could not register anchor-baseline-reset callback")


def _annotate_frozen_snapshot(
    img: "Image.Image",
    label_rows: Optional[dict],
    raw_values: dict[str, Optional[float]],
) -> "Image.Image":
    """Draw OCR overlay on ``img`` for the frozen-panel UI.

    Renders the same kind of overlay the live debug-overlay produces
    (cyan row band rectangles + per-field value labels), plus a red
    "FROZEN" watermark in the top-right corner. The result is what
    the user sees in the left pane of the panel-finder popout: a
    visualization of the "second scanner" — the OCR pipeline's
    interpretation of the static frozen image.

    Best-effort: any draw failure returns the unmodified image rather
    than corrupting the snapshot. Caller should still feed valid
    inputs but won't crash on edge cases.
    """
    try:
        from PIL import ImageDraw
    except Exception:
        return img
    try:
        annotated = img.convert("RGB").copy()
    except Exception:
        return img
    try:
        draw = ImageDraw.Draw(annotated)
        W, H = annotated.size

        # FROZEN watermark — top-right, red, hard to miss.
        try:
            draw.text((max(0, W - 80), 4), "FROZEN", fill=(255, 80, 80))
        except Exception:
            pass

        # Per-field row band rectangles + OCR value labels.
        if label_rows:
            for _field in ("mass", "resistance", "instability"):
                _row = label_rows.get(_field)
                if not _row:
                    continue
                try:
                    _y1, _y2, _label_right = _row
                except (TypeError, ValueError):
                    continue
                _y1 = max(0, int(_y1))
                _y2 = min(H - 1, int(_y2))
                if _y2 <= _y1:
                    continue
                # Cyan rectangle around the row band.
                try:
                    draw.rectangle(
                        [(0, _y1), (W - 1, _y2)],
                        outline=(0, 200, 200),
                        width=2,
                    )
                except Exception:
                    pass
                # OCR'd value label right of the label column.
                _v = raw_values.get(_field)
                _text = (
                    f"{_field}={_v}" if _v is not None
                    else f"{_field}=?"
                )
                try:
                    draw.text(
                        (int(_label_right) + 4, _y1 + 2),
                        _text, fill=(0, 255, 255),
                    )
                except Exception:
                    pass
    except Exception as exc:
        log.debug("annotate_frozen_snapshot: draw failed: %s", exc)
        # Fall back to the unannotated image — better than nothing.
    return annotated


def _signal_otsu(gray: np.ndarray) -> int:
    """Return an Otsu threshold for a tight signal crop."""
    if gray.size == 0:
        return 0
    hist = np.bincount(gray.astype(np.uint8, copy=False).ravel(), minlength=256).astype(np.float64)
    total = float(gray.size)
    sum_total = float(np.dot(np.arange(256), hist))
    sum_b = 0.0
    w_b = 0.0
    best_var = -1.0
    best = int(np.median(gray))
    for i in range(256):
        w_b += float(hist[i])
        if w_b <= 0:
            continue
        w_f = total - w_b
        if w_f <= 0:
            break
        sum_b += float(i * hist[i])
        mean_b = sum_b / w_b
        mean_f = (sum_total - sum_b) / w_f
        between = w_b * w_f * (mean_b - mean_f) ** 2
        if between > best_var:
            best_var = between
            best = i
    return int(best)


def _signal_main_row_mask(gray: np.ndarray) -> Optional[np.ndarray]:
    """Isolate the dominant signal-number row as a boolean mask."""
    if gray.ndim != 2 or gray.size == 0:
        return None
    thr = _signal_otsu(gray)
    mask = gray > thr
    # If polarity is inverted, keep the sparser foreground.
    if int(mask.sum()) > (mask.size // 2):
        mask = ~mask
    row_counts = mask.sum(axis=1)
    min_count = max(2, int(mask.shape[1] * 0.05))
    rows: list[tuple[int, int, int]] = []
    in_row = False
    start = 0
    peak = 0
    for y in range(mask.shape[0] + 1):
        val = int(row_counts[y]) if y < mask.shape[0] else 0
        if val >= min_count and not in_row:
            in_row = True
            start = y
            peak = val
        elif val >= min_count and in_row:
            peak = max(peak, val)
        elif val < min_count and in_row:
            in_row = False
            if y - start >= 4:
                rows.append((start, y, peak))
    if not rows:
        return None
    y1, y2, _peak = max(rows, key=lambda r: (r[2], r[1] - r[0]))
    return mask[y1:y2, :]


def _signal_component_spans(row_mask: np.ndarray) -> list[tuple[int, int]]:
    """Segment foreground column runs in a normalized signal row."""
    if row_mask.ndim != 2 or row_mask.size == 0:
        return []
    col_counts = row_mask.sum(axis=0)
    spans: list[tuple[int, int]] = []
    in_span = False
    start = 0
    min_hot = 1
    for x in range(row_mask.shape[1] + 1):
        val = int(col_counts[x]) if x < row_mask.shape[1] else 0
        if val >= min_hot and not in_span:
            in_span = True
            start = x
        elif val < min_hot and in_span:
            in_span = False
            if x - start >= 1:
                spans.append((start, x))
    return spans


def _signal_trim_component(component: np.ndarray) -> Optional[np.ndarray]:
    ys, xs = np.where(component)
    if ys.size == 0 or xs.size == 0:
        return None
    return component[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _signal_component_is_comma(component: np.ndarray, row_h: int) -> bool:
    trimmed = _signal_trim_component(component)
    if trimmed is None:
        return True
    h, w = trimmed.shape
    ys, _xs = np.where(component)
    center_y = float(ys.mean()) if ys.size else 0.0
    return h < row_h * 0.45 and w <= max(3, int(row_h * 0.25)) and center_y > row_h * 0.45


def _signal_component_is_icon(component: np.ndarray, row_h: int, remaining: int) -> bool:
    trimmed = _signal_trim_component(component)
    if trimmed is None:
        return False
    h, w = trimmed.shape
    return remaining >= 4 and w >= row_h * 0.70 and h >= row_h * 0.70


def _signal_component_holes(component: np.ndarray) -> int:
    """Count enclosed background holes in a trimmed boolean glyph."""
    h, w = component.shape
    if h <= 2 or w <= 2:
        return 0
    seen = np.zeros(component.shape, dtype=bool)
    holes = 0
    for y in range(h):
        for x in range(w):
            if component[y, x] or seen[y, x]:
                continue
            stack = [(y, x)]
            seen[y, x] = True
            touches_border = False
            while stack:
                cy, cx = stack.pop()
                if cy == 0 or cx == 0 or cy == h - 1 or cx == w - 1:
                    touches_border = True
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if (
                        0 <= ny < h
                        and 0 <= nx < w
                        and not component[ny, nx]
                        and not seen[ny, nx]
                    ):
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if not touches_border:
                holes += 1
    return holes


def _signal_digit_from_geometry(component: np.ndarray) -> Optional[str]:
    """Classify clean SC signal glyphs when OCR engines are unavailable.

    This is intentionally conservative: it handles high-confidence
    morphology visible in tight signature crops and returns None for
    ambiguous glyphs so other readers can decide.
    """
    trimmed = _signal_trim_component(component)
    if trimmed is None:
        return None
    h, w = trimmed.shape
    if h <= 0 or w <= 0:
        return None
    density = float(trimmed.mean())
    aspect = w / float(h)
    if aspect <= 0.42 and density >= 0.45:
        return "1"

    thirds = max(1, h // 3)
    top = float(trimmed[:thirds, :].mean())
    mid = float(trimmed[thirds: max(2 * thirds, thirds + 1), :].mean())
    bottom = float(trimmed[-thirds:, :].mean())
    center_w = max(1, w // 3)
    cx1 = max(0, (w - center_w) // 2)
    cx2 = min(w, cx1 + center_w)
    center_density = float(trimmed[thirds: max(h - thirds, thirds + 1), cx1:cx2].mean())
    left_density = float(trimmed[:, :max(1, w // 3)].mean())
    right_density = float(trimmed[:, max(0, w - max(1, w // 3)):].mean())
    holes_hint = _signal_component_holes(trimmed)
    if holes_hint >= 2:
        return "8"

    # Zero: strong side walls and a dense lower/middle body.
    if holes_hint == 1 and 0.43 <= aspect <= 0.75 and left_density >= 0.75 and right_density >= 0.75 and mid >= 0.65:
        return "0"

    # Seven: heavy top bar, lighter lower body, and much less side-wall density.
    if 0.43 <= aspect <= 0.75 and top >= 0.85 and mid <= 0.60 and density <= 0.70:
        return "7"

    def _region(y1: int, y2: int, x1: int, x2: int) -> float:
        yy1 = max(0, min(h, y1))
        yy2 = max(0, min(h, y2))
        xx1 = max(0, min(w, x1))
        xx2 = max(0, min(w, x2))
        if yy2 <= yy1 or xx2 <= xx1:
            return 0.0
        return float(trimmed[yy1:yy2, xx1:xx2].mean())

    # Seven-segment-style occupancy, used as a broader fallback for
    # clean segmented glyphs. SC signal numerals are not literally
    # seven-segment, but these coarse regions distinguish the common
    # digit topology without needing Tesseract, CRNN, or onnxruntime.
    top_seg = _region(0, max(2, h // 5), w // 4, 3 * w // 4) > 0.50
    mid_seg = _region(2 * h // 5, 3 * h // 5, w // 4, 3 * w // 4) > 0.50
    bot_seg = _region(max(0, h - h // 5), h, w // 4, 3 * w // 4) > 0.50
    upper_left = _region(h // 6, h // 2, 0, max(1, w // 3)) > 0.35
    upper_right = _region(h // 6, h // 2, max(0, 2 * w // 3), w) > 0.35
    lower_left_score = _region(h // 2, 5 * h // 6, 0, max(1, w // 3))
    lower_right_score = _region(h // 2, 5 * h // 6, max(0, 2 * w // 3), w)
    upper_left_score = _region(h // 6, h // 2, 0, max(1, w // 3))
    upper_right_score = _region(h // 6, h // 2, max(0, 2 * w // 3), w)
    top_score = _region(0, max(2, h // 5), w // 4, 3 * w // 4)
    mid_score = _region(2 * h // 5, 3 * h // 5, w // 4, 3 * w // 4)
    bot_score = _region(max(0, h - h // 5), h, w // 4, 3 * w // 4)
    lower_left = lower_left_score > 0.35
    lower_right = lower_right_score > 0.35

    holes = _signal_component_holes(trimmed)
    if holes >= 2:
        return "8"
    if holes == 1:
        # Open-topped/left-light loop in SC's rendered 4.
        if upper_left_score < 0.30 and lower_right_score >= 0.60 and mid_score >= 0.75:
            return "4"
        if lower_left_score < 0.30 and upper_left_score >= 0.55 and bot_score >= 0.50:
            return "9"
        if lower_left_score >= 0.50 and upper_left_score >= 0.55 and upper_right_score < 0.35:
            return "6"
        return "0"

    # Common no-hole glyphs from real tight crops.
    if top_score >= 0.85 and mid_score >= 0.50 and bot_score >= 0.80:
        if upper_left_score >= 0.70 and upper_right_score < 0.55 and lower_right_score >= 0.55:
            return "5"
        if upper_right_score >= 0.55 and lower_left_score >= 0.50 and lower_right_score < 0.45:
            return "2"
    if top_score < 0.70 and mid_score >= 0.75 and lower_right_score >= 0.60:
        return "4"

    pattern = (
        top_seg,
        upper_left,
        upper_right,
        bot_seg,
        lower_left,
        lower_right,
        mid_seg,
    )
    segment_digits = {
        (True, True, True, True, True, True, False): "0",
        (False, False, True, False, False, True, False): "1",
        (True, False, True, True, True, False, True): "2",
        (True, False, True, True, False, True, True): "3",
        (False, True, True, False, False, True, True): "4",
        (True, True, False, True, False, True, True): "5",
        (True, True, False, True, True, True, True): "6",
        (True, False, True, False, False, True, False): "7",
        (True, True, True, True, True, True, True): "8",
        (True, True, True, True, False, True, True): "9",
    }
    return segment_digits.get(pattern)


def _signal_read_tight_local(gray: np.ndarray) -> Optional[int]:
    """Dependency-free signal OCR for tight icon+number crops.

    It implements the experimental no-training strategy: isolate the
    number row, segment relative to row height, ignore comma/noise, and
    classify obvious local glyph geometry before applying range checks.
    """
    row = _signal_main_row_mask(gray)
    if row is None:
        return None
    spans = _signal_component_spans(row)
    if not spans:
        return None

    row_h = row.shape[0]
    digits: list[str] = []
    total = len(spans)
    for idx, (x1, x2) in enumerate(spans):
        component = row[:, x1:x2]
        if _signal_component_is_icon(component, row_h, total - idx - 1):
            continue
        if _signal_component_is_comma(component, row_h):
            continue
        digit = _signal_digit_from_geometry(component)
        if digit is None:
            log.debug("sc_ocr.signal: local fallback ambiguous component span=%s", (x1, x2))
            return None
        digits.append(digit)

    if not (4 <= len(digits) <= 5):
        return None
    value = int("".join(digits))
    if 1000 <= value <= 35000:
        log.info("sc_ocr.signal: local tight-crop fallback read %d", value)
        return value
    return None


def _signal_accept_candidate(value: int, source: str) -> int:
    """Apply the existing signal consensus policy to a raw candidate."""
    global _STABLE_SIGNAL
    _RECENT_SIGNAL_READS.append(value)
    if _STABLE_SIGNAL is None:
        _STABLE_SIGNAL = value
    elif value != _STABLE_SIGNAL:
        recent = list(_RECENT_SIGNAL_READS)[-_SIGNAL_AGREEMENT_REQ:]
        if len(recent) >= _SIGNAL_AGREEMENT_REQ and all(r == value for r in recent):
            log.info("sc_ocr.signal: stable swap %d → %d (%s consensus)", _STABLE_SIGNAL, value, source)
            _STABLE_SIGNAL = value
    return int(_STABLE_SIGNAL)


def _crop_fingerprint(value_crop: "Image.Image") -> Optional[np.ndarray]:
    """Downsample a value crop to a fixed (_CROP_FP_H × _CROP_FP_W)
    grayscale fingerprint for pairwise similarity comparison.

    Returns a zero-mean unit-variance float32 array (NCC-ready), or
    None if the input is degenerate.
    """
    try:
        gray = value_crop.convert("L").resize(
            (_CROP_FP_W, _CROP_FP_H), Image.BILINEAR,
        )
        arr = np.asarray(gray, dtype=np.float32).ravel()
        std = float(arr.std())
        if std < 1e-3:
            return None
        return (arr - float(arr.mean())) / std
    except Exception as exc:
        log.debug("api: _crop_fingerprint swallowed: %s", exc)
        return None


def _crop_buffer_consistent(field: str) -> tuple[bool, float]:
    """Return (is_consistent, mean_pairwise_NCC) for the field's crop buffer.

    A buffer is consistent when its frames all look like the same
    underlying scene — i.e. the row crop has been STABLE across the
    window. If the row was jumping (digits one frame, progress bar
    the next), pairwise NCC will be low and we refuse to lock.
    """
    fps = [fp for fp in _RECENT_CROPS[field] if fp is not None]
    if len(fps) < _LOCK_WINDOW:
        return False, 0.0
    n = len(fps[0])
    sims = []
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            sims.append(float(np.dot(fps[i], fps[j]) / n))
    if not sims:
        return False, 0.0
    mean_sim = sum(sims) / len(sims)
    return mean_sim >= _LOCK_CROP_NCC_MIN, mean_sim


def _value_buffer_unanimous(field: str) -> Optional[float]:
    """Return the unanimous value if the buffer is full and all frames
    agree, else None. Stricter than _consensus_value's 2-of-3 rule —
    used as the lock gate."""
    buf = _RECENT_READS.get(field)
    if buf is None or len(buf) < _LOCK_WINDOW:
        return None
    vals = [round(float(v), 4) for v in buf if v is not None]
    if len(vals) < _LOCK_WINDOW:
        return None
    if len(set(vals)) == 1:
        return vals[0]
    return None


# ── Field-value lock cache ─────────────────────────────────────────
#
# Once a field reads a value that PASSES VALIDATION (and has high
# enough CNN confidence), we lock it for the duration the panel
# remains visible. Subsequent scans for the same panel skip the OCR
# work entirely for that field and return the locked value. The
# cache is cleared the moment the panel disappears (mineral row
# undetectable) — i.e. when the user looks away from the rock.
#
# Why locking is necessary even with consensus: the consensus buffer
# requires 2-of-3 agreement to display a value, which means a single
# good frame surrounded by misreads will still show garbage. Locking
# treats the FIRST validated read as truth, and only re-evaluates
# when the panel goes away (rock changed).
#
# Keyed by region (x, y, w, h) so multiple scan regions don't share
# cache state (e.g. two Mining Signals instances).
#
# Each entry stores BOTH the locked value AND the crop fingerprint
# that was in effect when the lock fired. On every subsequent scan
# we compare the current frame's crop fingerprint against the
# stored one; a significant divergence drops the lock and resumes
# OCR. This prevents a wrong locked value from persisting silently
# if the row geometry drifts after locking (e.g. ship moves and the
# panel re-anchors slightly differently).
# LRU bound: long sessions with calibration drift accumulate stale
# region keys forever. _CACHE_MAX caps both _field_lock_cache and
# _difficulty_cache; oldest-touched entries are evicted FIFO, and
# every read/write that hits an existing key bumps it to the end.
_CACHE_MAX = 16
_field_lock_cache: "OrderedDict[tuple[int, int, int, int], dict[str, tuple[float, np.ndarray]]]" = OrderedDict()
# Threshold: if current crop NCC vs stored fingerprint < this, the
# lock is invalidated. Lower than the lock-acquisition threshold
# (0.85) so transient noise doesn't immediately drop a good lock.
_LOCK_INVALIDATE_NCC = 0.65

# ── Periodic lock re-verification ───────────────────────────────────
# Background: locks acquired under degenerate anchor states can latch a
# wrong-by-one-digit value (e.g. real 523.33 read as 52.33 because a
# leading digit was clipped). NCC-drift invalidation only triggers when
# the crop GEOMETRY changes — if the row stays in roughly the same
# pixels but its CONTENT was misread, the lock holds indefinitely.
#
# We defend with a periodic forced re-OCR: every Nth pass through the
# "all locked" fast path we re-OCR ONE field (round-robin via the
# counter), and if the fresh read is high-confidence AND disagrees with
# the locked value, invalidate the lock and accept the fresh read.
#
# Per-field counter (keyed by field name, NOT region — the counter is
# only meaningful while the lock holds, and the lock cache is keyed by
# region; if a different region becomes active the old region's lock
# gets cleared by _reset_consensus_buffers anyway). Lifecycle:
#   * Initialized to 0 when a lock is set (lines below the lock-gate)
#   * Incremented every time ALL LOCKED shortcuts past a field
#   * Reset to 0 on successful CONFIRM (lock holds, fresh read agreed)
#   * Cleared by _reset_consensus_buffers() and on lock invalidation
_REVERIFY_THRESHOLD = 5         # force fresh OCR every Nth scan per field
_REVERIFY_CONF_MIN = 0.90       # min mean conf for fresh read to override lock
_scans_since_lock_verify: dict[str, int] = {
    "mass": 0, "resistance": 0, "instability": 0,
}
# Signals to _persistence_check that this field was just freshly
# re-verified with high-conf disagreement, so the sticky-streak bias
# should be bypassed for ONE scan. Caller sets it to the candidate
# value (which then propagates regardless of streak). Cleared after
# the persistence check consumes it.
_REVERIFY_BYPASS: dict[str, Optional[float]] = {
    "mass": None, "resistance": None, "instability": None,
}


def _lock_reverify_field(
    value_crop: "Image.Image",
    field: str,
    locked_val: float,
) -> tuple[str, Optional[float], float]:
    """Run a fresh OCR pass on ``value_crop`` for re-verification.

    Returns a tuple ``(verdict, fresh_val, mean_conf)``:
      * ``verdict="INVALIDATED"`` — fresh read at conf>=_REVERIFY_CONF_MIN
        disagrees with ``locked_val``. Caller MUST drop the lock and
        accept ``fresh_val`` as the new displayed value.
      * ``verdict="CONFIRMED"``  — fresh read agrees with ``locked_val``
        (any confidence). Caller resets the counter and keeps the lock.
      * ``verdict="NO_READ"``    — fresh OCR produced no validated value
        OR the value was below the conf threshold. Caller keeps the
        lock; counter handling per docstring of caller.

    This helper is intentionally NARROW — it runs only the
    ``_ocr_value_crop`` primary path (no full-row Tesseract fallback,
    no template voter, no priors). That keeps reverify latency to ~one
    CRNN inference (~150 ms) rather than the full ~1.5 s field budget,
    so the cumulative steady-state overhead stays small.
    """
    try:
        text, confs = _ocr_value_crop(value_crop, field=field)
    except Exception as _exc:
        log.debug("sc_ocr: reverify ocr failed for %s: %s", field, _exc)
        return ("NO_READ", None, 0.0)
    if not text:
        return ("NO_READ", None, 0.0)
    try:
        mean_conf = float(sum(confs) / len(confs)) if confs else 0.0
    except Exception:
        mean_conf = 0.0
    fresh_val: Optional[float] = None
    try:
        if field == "mass":
            fresh_val = validate.validate_mass(text)
        elif field == "resistance":
            fresh_val = validate.validate_pct(text)
        elif field == "instability":
            fresh_val = validate.validate_instability(text, confidences=confs)
    except Exception:
        fresh_val = None
    if fresh_val is None:
        return ("NO_READ", None, mean_conf)
    try:
        agrees = float(fresh_val) == float(locked_val)
    except (TypeError, ValueError):
        agrees = False
    if not agrees and field == "instability" and "." not in text:
        # DOT-DROPPED reads (live 2026-06-10): instability renders with
        # exactly two decimals, so a dotless OCR string whose value is
        # locked*100 is the known lost-dot signature, not disagreement.
        # The main path repairs this via proactive-decimal-recover
        # AFTER validation; this helper compared the RAW value and
        # false-invalidated 1.43 vs '143' every reverify cycle —
        # clearing the lock and emitting instability=143.0 for a frame.
        try:
            if abs(float(fresh_val) - float(locked_val) * 100.0) < 1e-6:
                return ("CONFIRMED", float(locked_val), mean_conf)
        except (TypeError, ValueError):
            pass
    if agrees:
        return ("CONFIRMED", float(fresh_val), mean_conf)
    if mean_conf >= _REVERIFY_CONF_MIN:
        return ("INVALIDATED", float(fresh_val), mean_conf)
    # Disagrees but low confidence — don't trust it enough to override.
    return ("NO_READ", float(fresh_val), mean_conf)

# ── Difficulty cache (per-rock) ────────────────────────────────────
# Difficulty (EASY/MEDIUM/HARD/EXTREME/IMPOSSIBLE) is a property of
# the current rock — it cannot change without the panel disappearing
# and a new rock being scanned. The detection routine runs 4
# Tesseract subprocess calls every scan tick, which is wasted work
# once we've already determined the difficulty for this rock.
#
# Lifecycle mirrors _field_lock_cache exactly:
#   * Keyed by _region_key(region) so multiple scan regions don't
#     share state.
#   * Cleared in _reset_consensus_buffers() (panel gone, user looked
#     away — next rock starts fresh).
#   * Dropped per-region when mineral_row is None (panel disappeared
#     mid-scan).
#   * Dropped per-region when ANY field lock invalidates due to NCC
#     drift (the rock just changed under us — re-detect difficulty).
#
# Stores None on a miss so we don't retry 4 Tesseract calls every
# scan when the difficulty bar is genuinely unreadable. Cleared by
# the same rock-change events.
_difficulty_cache: "OrderedDict[tuple[int, int, int, int], Optional[str]]" = OrderedDict()


def _region_key(region: dict) -> tuple[int, int, int, int]:
    return (
        int(region.get("x", 0)),
        int(region.get("y", 0)),
        int(region.get("w", 0)),
        int(region.get("h", 0)),
    )


def _consensus_value(field: str, new_value: Optional[float]) -> Optional[float]:
    """Sticky consensus: return last stable value unless a new value
    appears 2+ times in the rolling 3-read buffer.

    Behaviour:
      * None input → return last stable (don't corrupt buffer).
      * New value that matches an existing buffer entry → counts go up.
      * If most-frequent value has >= 2 occurrences → that becomes the
        new stable value and is returned.
      * Otherwise → return the previously-displayed stable value
        (outlier suppressed).

    First-ever non-None read: return it immediately (no history to
    stick to).
    """
    buf = _RECENT_READS.get(field)
    if buf is None:
        return new_value
    if new_value is None:
        return _STABLE_VALUE.get(field)

    buf.append(new_value)
    counts: dict[float, int] = {}
    for v in buf:
        if v is None:
            continue
        key = round(float(v), 4)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return new_value

    best_key, best_n = max(counts.items(), key=lambda kv: kv[1])
    if best_n >= 2:
        # Confirmed — update stable value and return it
        _STABLE_VALUE[field] = best_key
        return best_key

    # No 2-agreement yet — prefer the last stable value if we have one,
    # else accept the new read as provisional
    last = _STABLE_VALUE.get(field)
    if last is not None:
        return last
    _STABLE_VALUE[field] = new_value
    return new_value

# Reuse proven legacy helpers that are pure NumPy (no Tesseract dep)
# Now that _build_text_mask is polarity-aware, _find_mineral_row and
# _find_value_crop both work on light AND dark backgrounds.
from ..onnx_hud_reader import (  # noqa: E402
    _find_mineral_row,
    _find_value_crop,
    _otsu,
)


def _canonicalize_polarity(gray: np.ndarray) -> np.ndarray:
    """Force the image to bright-text-on-dark-background (model's training polarity).

    The HUD ships values in many colors (white, yellow, cyan, green,
    red) over many backgrounds (black space, bright sky, dim cloud
    gradient, snowy asteroid). Background-median heuristics like
    ``median > 140 → invert`` get fooled by:
      - Bright sky backgrounds where the sky pixels dominate the
        median even though the text is BRIGHTER than the sky.
      - Mixed crops where a UI element occupies more area than the
        text itself.

    Minority-class rule (background-agnostic):
      1. Run Otsu on the grayscale image — splits pixels into the
         two cleanest groups.
      2. Whichever group has FEWER pixels is the foreground (text).
         Text is always a small fraction of any reasonable crop;
         backgrounds always dominate by area.
      3. If the minority group is DARK, invert so the text ends up
         BRIGHT (on dark background). The model was trained on
         bright-text-on-dark-bg crops surrounded by white padding;
         we preserve that convention here so downstream segmentation
         and the CNN see what they expect.

    Returns the polarity-normalized grayscale (uint8).
    """
    if gray.size == 0:
        return gray
    thr = _otsu(gray)
    bright_count = int((gray > thr).sum())
    dark_count = int((gray <= thr).sum())
    # Minority class = text. We want text BRIGHT (matches training
    # data). If the minority class is already bright, leave alone.
    # If the minority class is dark, invert to bring it bright.
    if dark_count < bright_count:
        return (255 - gray).astype(np.uint8)
    return gray


def _adaptive_binarize(
    gray: np.ndarray,
    block_size: int = 31,
    C: float = 15.0,
) -> np.ndarray:
    """Locally-adaptive binarization. Returns a uint8 mask where text
    pixels are 255 and background is 0.

    Replaces global Otsu, which assumes the histogram has TWO
    well-separated clusters (text vs background). That assumption
    holds when the panel sits on uniform dark space, but breaks on
    bright sandy / asteroid backgrounds where the BG luminance
    overlaps the text luminance and Otsu picks a threshold that
    either eats the text or admits asteroid noise as text.

    Adaptive threshold sidesteps that entirely: instead of one global
    threshold, every pixel is compared against a Gaussian-weighted
    mean of its local neighbourhood, then marked text iff it's at
    least ``C`` luma units BRIGHTER than that local mean. Background
    luminance can vary across the image without breaking the
    decision rule — only LOCAL contrast matters.

    Note on sign convention: OpenCV's ``cv2.adaptiveThreshold`` with
    ``THRESH_BINARY`` uses ``T = mean - C`` because it targets the
    common case of DARK text on LIGHT background (document scanning).
    We're in the inverse case — BRIGHT text on DARKish background —
    so we test ``pixel > mean + C`` instead. Same idea, opposite
    polarity.

    Parameters chosen for SC HUD digit text (≈24-28 px tall at our
    REF_H scale):
      * ``block_size = 31`` — slightly larger than text height so the
        local window includes the text + a thin BG margin. Too small
        and the window fits inside a single stroke (stroke = its own
        background); too large and the result degrades back toward
        global Otsu.
      * ``C = 15`` — pixels must be ≥15 luma units above local mean
        to count as text. The SC HUD's typical text-vs-BG contrast
        on bright asteroid is ~30-50 units, so 15 leaves margin for
        noise and antialiasing while reliably separating text from
        BG. Tunable per real captures if needed.

    Input must already be in canonical polarity (text BRIGHT). Run
    :func:`_canonicalize_polarity` first.

    Cost: ~0.3 ms per crop via ``scipy.ndimage.gaussian_filter`` —
    cheaper than global Otsu's histogram pass, so a slight perf win.
    """
    if gray.size == 0:
        return np.zeros_like(gray, dtype=np.uint8)
    try:
        from scipy.ndimage import gaussian_filter  # type: ignore
    except ImportError:
        # No scipy → fall back to global Otsu so the pipeline still
        # works (just with the original bright-BG weakness).
        thr = _otsu(gray)
        return ((gray > thr).astype(np.uint8)) * 255

    # Sigma chosen so that ~99% of the Gaussian's mass falls within
    # ``block_size`` pixels (6σ rule).
    sigma = max(1.0, block_size / 6.0)
    g32 = gray.astype(np.float32)
    local_mean = gaussian_filter(g32, sigma=sigma, mode="reflect")
    raw = (g32 > (local_mean + C))
    # Morphological opening (3×3) removes single-pixel noise specks
    # without eroding real text strokes (which are ≥2 px wide in the
    # SC HUD font at our REF_H scale). Without this, anti-aliasing
    # halos and BG noise create dozens of 1-px "glyphs" downstream.
    try:
        from scipy.ndimage import binary_opening  # type: ignore
        cleaned = binary_opening(raw, structure=np.ones((2, 2), dtype=bool))
        return (cleaned.astype(np.uint8)) * 255
    except ImportError:
        return (raw.astype(np.uint8)) * 255


def _adaptive_binarize_multi(
    canon_gray: np.ndarray,
    expected_count: int = 5,
) -> np.ndarray:
    """Multi-recipe binarization for chromatically-aberrated SC HUD digits.

    The legacy :func:`_adaptive_binarize` uses a single fixed recipe
    (Gaussian local-mean threshold, ``C = 15``) which works on the
    common HUD case but breaks on bubble-glow signal panels where:

      * Heavy chromatic aberration compresses the dynamic range so
        the local-mean window has very low contrast — adaptive
        threshold then either floods the whole crop on (entire crop =
        one merged blob) or admits nothing (mask is mostly empty).
      * The sharp glow gradient around digits drags the local mean
        UP near each glyph, eating a fixed-``C`` margin so the digit
        edges flicker on/off.

    This function tries several recipes and picks whichever one
    produces a column-projection span count CLOSEST to
    ``expected_count`` (4 or 5 for signal values). Each recipe is a
    standard binarization variant, so the worst case is "same result
    as before" — never worse than the legacy single-recipe path.

    Recipes tried (in order, all on canonical-polarity input where
    text is BRIGHT on dark background):

      1. ``otsu_global``    — global Otsu threshold (cheap baseline).
      2. ``percentile_60``  — top-40% bright pixels become ink.
      3. ``percentile_70``  — top-30% bright pixels become ink.
      4. ``percentile_80``  — top-20% bright pixels become ink.
      5. ``adaptive_w11``   — local-mean threshold, finer 11-px window.
      6. ``adaptive_w21``   — local-mean threshold, coarser 21-px window.
      7. ``legacy``         — existing :func:`_adaptive_binarize`.

    Selection: for each recipe's binary mask we compute the column-ink
    projection and count contiguous non-zero column runs whose width
    is in ``[3, work.shape[1] / 2]`` ("good-width" spans). The score
    is ``-abs(good_count - expected_count)`` with extra penalties for
    counts of 1, 2, or >7 (clearly degenerate). Highest score wins;
    ties broken by recipe order (Otsu first, legacy last).

    Returns the chosen ``uint8`` mask (text=255, background=0). If
    ``canon_gray`` is empty, returns an all-zero mask of matching
    shape — same contract as :func:`_adaptive_binarize`.

    Cost: ~7 recipes × ~0.3 ms each = ~2 ms — negligible vs. the
    ~100-300 ms downstream Tesseract / CNN call.
    """
    if canon_gray.size == 0:
        return np.zeros_like(canon_gray, dtype=np.uint8)
    if canon_gray.ndim != 2:
        # Defensive: fall back to legacy on shape we can't handle.
        return _adaptive_binarize(canon_gray)

    H, W = canon_gray.shape
    g32 = canon_gray.astype(np.float32)

    # Width bounds for "digit-shaped" spans. A real digit is roughly
    # H/4 to H wide in this font (digit aspect ratio ~0.4-0.6 + AA),
    # so anything narrower than ~H/5 is noise speckle and anything
    # wider than W/2 is a merged-everything blob.
    min_span_w = max(3, H // 5)
    max_span_w = max(min_span_w + 1, W // 2)

    def _measure_mask(mask: np.ndarray) -> tuple[int, int, int]:
        """Return ``(good_runs, noise_runs, mega_runs)`` for ``mask``.

        Walks the column-projection of ``mask`` (any-ink column =
        active, mirrors :func:`_segment_glyphs`'s default segmenter).
        Each contiguous active-column run is bucketed by width:

          * ``good_runs``  — width in ``[min_span_w, max_span_w]``
            (looks like a digit).
          * ``noise_runs`` — width below ``min_span_w`` (1-2 px stray
            ink, noise speckle that downstream will treat as a span).
          * ``mega_runs``  — width above ``max_span_w`` (the merged-
            everything blob case).

        Recipes that produce many ``noise_runs`` along with a few
        ``good_runs`` are unstable: the actual segmenter sees the
        noise spans too and produces 15-20 outputs where downstream
        only wanted 5, so we want to PENALISE such recipes. Pure
        ``good_runs == expected_count`` with zero noise is the goal.
        """
        if mask.size == 0:
            return 0, 0, 0
        any_ink = (mask > 0).any(axis=0)
        good = 0
        noise = 0
        mega = 0
        in_run = False
        start = 0
        for x in range(W + 1):
            val = bool(any_ink[x]) if x < W else False
            if val and not in_run:
                in_run = True
                start = x
            elif not val and in_run:
                in_run = False
                w = x - start
                if w > max_span_w:
                    mega += 1
                elif w >= min_span_w:
                    good += 1
                else:
                    noise += 1
        return good, noise, mega

    def _score(good: int, noise: int, mega: int) -> float:
        """Score a recipe by closeness to expected_count + cleanliness.

        Three components, additive (all ≤ 0; higher = better):

          1. **Distance**: ``-abs(good - expected_count)`` if ``good``
             is in the splitter-recoverable range ``[expected_count-2,
             expected_count+2]`` (i.e. 3-7 for the typical 5-digit
             signature). The downstream wide-span splitter can split
             a single fused-digit pair to take 4→5, so 4 IS in the
             sweet spot. It can also split a fused TRIPLE (e.g. "234"
             into "2", "3", "4") to take 3→5, so 3 IS in too. But
             splitting two fused pairs at once on a noisy crop is
             unreliable — anything ≤2 we treat as under-segmented.

          2. **Noise penalty**: ``-15 × noise`` — every below-min-width
             run downstream is a phantom span the segmenter will emit
             then send to the CNN, where it'll be misclassified as a
             random digit. Penalty is heavy enough that "5 digits + 5
             noise spans" loses to "3 digits + 0 noise spans" — the
             latter is recoverable downstream via the wide-span
             splitter, the former isn't.

          3. **Mega penalty**: ``-50 × mega`` — a single >max_span_w
             run is the entire-crop-one-blob failure; nothing
             downstream can split it back into 5 digits.

        Degenerate cases (good == 0) get a hard floor so they only
        win when EVERY recipe degenerates.
        """
        if good <= 0:
            return -1000.0 - 15.0 * noise - 50.0 * mega
        # Distance term: 3-7 is splitter-recoverable, 1-2 = under,
        # 8-10 = mild over, >10 = noise flood.
        if expected_count - 2 <= good <= expected_count + 2:
            distance = -float(abs(good - expected_count))
        elif good <= 2:
            distance = -100.0 - abs(good - expected_count)
        elif good <= 10:
            distance = -10.0 - abs(good - expected_count)
        else:
            distance = -100.0 - abs(good - expected_count)
        return distance - 15.0 * noise - 50.0 * mega

    def _box_local_mean(window: int) -> Optional[np.ndarray]:
        """Sliding-window local mean via separable cumulative sums.

        Pure NumPy — no scipy required, so this works as a fallback
        when the legacy recipe's gaussian_filter import fails. Returns
        None on bad inputs.
        """
        if window < 3 or window % 2 == 0:
            return None
        r = window // 2
        # Pad with reflect to keep edges sane.
        try:
            padded = np.pad(g32, ((r, r), (r, r)), mode="reflect")
        except Exception:
            return None
        # Integral image so each window-mean is O(1) per pixel.
        integral = padded.cumsum(axis=0).cumsum(axis=1)
        # Windowed sum at every output pixel via 4-corner trick.
        # integral has shape (H+2r+1-1, W+2r+1-1) after cumsum on
        # padded. We need to reconstruct the (H, W) windowed sum.
        # Simplest path: use uniform_filter1d if available, else
        # fall back to the slower corner trick.
        try:
            from scipy.ndimage import uniform_filter  # type: ignore
            return uniform_filter(g32, size=window, mode="reflect")
        except ImportError:
            # Manual O(N) box filter via convolution along each axis.
            kernel = np.ones(window, dtype=np.float32) / float(window)
            tmp = np.apply_along_axis(
                lambda v: np.convolve(v, kernel, mode="same"),
                axis=0, arr=g32,
            )
            return np.apply_along_axis(
                lambda v: np.convolve(v, kernel, mode="same"),
                axis=1, arr=tmp,
            )

    recipes: list[tuple[str, np.ndarray]] = []

    # Recipe 1: global Otsu.
    try:
        thr = float(_otsu(canon_gray))
        recipes.append((
            "otsu_global",
            ((canon_gray > thr).astype(np.uint8)) * 255,
        ))
    except Exception:
        pass

    # Recipes 2-4: percentile thresholds. Bright text on dark BG
    # means the ink pixels live in the top end of the histogram.
    for pct in (60.0, 70.0, 80.0):
        try:
            thr = float(np.percentile(canon_gray, pct))
            recipes.append((
                f"percentile_{int(pct)}",
                ((canon_gray > thr).astype(np.uint8)) * 255,
            ))
        except Exception:
            continue

    # Recipes 5-6: sliding-window adaptive at two window sizes. C=10
    # is slightly looser than legacy (15) — looser margins help on
    # the low-contrast bubble-glow case where digits barely beat the
    # local mean.
    for win in (11, 21):
        try:
            mean = _box_local_mean(win)
            if mean is None:
                continue
            mask = (g32 > (mean + 10.0)).astype(np.uint8) * 255
            recipes.append((f"adaptive_w{win}", mask))
        except Exception:
            continue

    # Recipe 7: legacy adaptive (baseline). Always last so it loses
    # on exact ties to a simpler recipe.
    try:
        recipes.append(("legacy", _adaptive_binarize(canon_gray)))
    except Exception:
        pass

    if not recipes:
        # Every recipe failed — should never happen since at least
        # one of percentile / Otsu always succeeds. Defensive
        # fallback to a zero mask so downstream segmentation gets
        # the empty-result it already handles.
        log.warning(
            "_adaptive_binarize_multi: all %d recipes failed, returning zero mask",
            7,
        )
        return np.zeros_like(canon_gray, dtype=np.uint8)

    # Score every recipe and pick the highest. Ties broken by recipe
    # order via stable sort (earlier = wins on equal score).
    #
    # We score each recipe TWICE — once on the raw mask, once on the
    # strip-cleaned mask (pill-outline bridge rows / wall cols
    # zeroed). The cleaned score is what the actual segmenter sees,
    # so it's the right metric for selection. Without cleaning, a
    # bridge-prone capture forces every recipe into ``mega=1``
    # territory and the picker falls back to a degenerate percentile-
    # only recipe that fragments the digit interiors.
    scored: list[tuple[str, np.ndarray, int, int, int, float]] = []
    for name, mask in recipes:
        cleaned = _strip_pill_outline_bridges(mask)
        good, noise, mega = _measure_mask(cleaned)
        s = _score(good, noise, mega)
        scored.append((name, mask, good, noise, mega, s))

    # Pick best score; on ties prefer EARLIER recipes (insertion order).
    best_idx = 0
    for i in range(1, len(scored)):
        if scored[i][5] > scored[best_idx][5]:
            best_idx = i

    best_name, best_mask, best_good, best_noise, best_mega, best_score = (
        scored[best_idx]
    )
    log.info(
        "_adaptive_binarize_multi: chose recipe=%s good=%d noise=%d "
        "mega=%d (expected=%d, score=%.1f, scored on cleaned mask) — "
        "all=%s",
        best_name, best_good, best_noise, best_mega, expected_count,
        best_score,
        ", ".join(f"{n}:g{g}/n{n_}/m{m_}" for n, _, g, n_, m_, _ in scored),
    )
    return best_mask


def _find_mineral_row_universal(img: Image.Image) -> Optional[tuple[int, int]]:
    """Find the mineral-name row on ANY background via local contrast.

    Uses a high-pass filter (|pixel - gaussian_blur|) to detect text
    edges regardless of polarity. Text has sharp edges that differ
    from their local neighborhood; smooth backgrounds (bright sky,
    dark space, asteroid rock) have low local contrast. This works
    identically on dark-on-light AND light-on-dark text.

    Then runs the same header → mineral-row detection logic as the
    legacy pipeline.
    """
    from PIL import ImageFilter

    gray = np.array(img.convert("L"), dtype=np.float32)
    H, W = gray.shape
    median = float(np.median(gray))

    if median < 130:
        # Dark background: proven brightness-based approach
        # (matches legacy _find_mineral_row exactly)
        text_mask = gray > 150
    else:
        # Light background: local-contrast approach
        # Detects text edges regardless of polarity
        blurred = np.asarray(
            Image.fromarray(gray.astype(np.uint8)).filter(
                ImageFilter.GaussianBlur(radius=5)
            ),
            dtype=np.float32,
        )
        local_contrast = np.abs(gray - blurred)
        text_mask = local_contrast > 15
    row_counts = text_mask.sum(axis=1)

    # Build row spans
    MIN_ROW_HEIGHT = 12
    rows: list[tuple[int, int, int]] = []
    in_row = False
    start = 0
    peak = 0
    for y in range(H + 1):
        val = int(row_counts[y]) if y < H else 0
        if val >= 5 and not in_row:
            in_row = True
            start = y
            peak = val
        elif val >= 5 and in_row:
            peak = max(peak, val)
        elif val < 5 and in_row:
            in_row = False
            if y - start >= MIN_ROW_HEIGHT:
                rows.append((start, y, peak))

    if len(rows) < 2:
        return None

    # Find the header ("SCAN RESULTS"): first band with decent peak
    header_idx = None
    for i, (y1, y2, pk) in enumerate(rows):
        if pk >= 40 and (y2 - y1) <= 40:
            header_idx = i
            break
    if header_idx is None:
        for i, (y1, y2, pk) in enumerate(rows):
            if pk >= 20 and (y2 - y1) <= 40:
                header_idx = i
                break
    if header_idx is None:
        return None

    # Mineral name = next qualifying band after header
    for y1, y2, pk in rows[header_idx + 1:]:
        if pk >= 20 and (y2 - y1) <= 40:
            return (y1, y2)

    return None

# HUD geometry ratios (fraction of panel HEIGHT from mineral-row center
# to each value row). Measured from the 397x541 test fixture and
# verified to scale proportionally across panel sizes.
_ROW_HEIGHT_HALF_RATIO = 0.028  # ±15/541
_OFFSET_RATIOS = {"mass": 0.079, "resistance": 0.152, "instability": 0.222}
# Label right-edge ratios (fraction of panel WIDTH)
_LABEL_RIGHT_RATIOS = {"mass": 0.277, "resistance": 0.504, "instability": 0.516}


# ── Glyph extraction + ONNX classification ────────────────────────

def _segment_glyphs(
    gray: np.ndarray, binary: np.ndarray,
    *, disable_gap_cut: bool = False,
    field: str = "",
) -> tuple[list[np.ndarray], list[tuple[int, int, int, int]]]:
    """Segment individual glyphs → (28x28 float32 crops, source bboxes).

    Replicates the EXACT preprocessing the ONNX model was trained on:
      glyph from grayscale → pad with 255 → resize 28x28 → / 255.

    Right-anchored: drops spans before the largest gap between
    consecutive characters. The largest gap usually marks where label-
    text intrusion (e.g. the trailing colon of "RESISTANCE:") ends and
    the actual value begins.

    Set ``disable_gap_cut=True`` for callers whose crops have NO label
    intrusion — region2 (signature) is the canonical example, where
    the world-model crop_box already starts immediately after the
    icon. With gap-cut enabled there, a wide right-edge gap
    (between the rightmost digit and a pill end-cap artifact) gets
    misinterpreted as the label-to-value boundary, dropping every
    real digit on the left of it.

    ``field`` is an optional hint used to tune the leading-narrow-span
    pruning. The pass2/pass3 leading-span droppers were designed to
    strip label residue (colon ticks, AA dots, etc.) from in front of
    the value, but they have a structural blind spot for SHORT
    INSTABILITY values like ``"1.03"``: the raw segmenter correctly
    finds 4 spans (``1``, ``.``, ``0``, ``3``), but the kerning gap
    between ``"1"`` and ``"."`` matches the gap-cut threshold (8 px on
    typical captures) AND the ``.`` itself looks like a narrow leading
    speck to pass3 — both get pruned, leaving ``"03"`` which structurally
    rejects downstream. With ``field="instability"`` we scan the raw
    span list for a dot-shaped INTERIOR span before pass2 fires, and
    when one is present we skip pass2 + pass3 entirely (no leading
    pruning) so the ``1`` and the ``.`` both survive. The rest of the
    fields (mass, resistance, mineral row, signal) keep the original
    aggressive pruning behavior — they have no dot to protect and
    benefit from the label-residue stripping.

    The second tuple element is a parallel list of ``(x, y, w, h)``
    bounding boxes in *source* (pre-normalization) coordinates. Used by
    :func:`_dot_label_from_box` to detect tiny `.` glyphs whose 28×28
    normalization strips the size signal that distinguishes them from
    full-height digits — the CNN routinely misclassifies such crops
    as `0`/`4`/`7` because every glyph occupies the full normalized
    canvas regardless of original size.
    """
    h, w = gray.shape
    proj = np.sum(binary > 0, axis=0)
    log.debug(
        "_segment_glyphs: gray.shape=%s, %d initial-projection columns",
        gray.shape, int((proj > 0).sum()),
    )
    spans: list[tuple[int, int]] = []
    in_char = False
    start = 0
    for x in range(w + 1):
        val = proj[x] if x < w else 0
        if val > 0 and not in_char:
            in_char = True; start = x
        elif val == 0 and in_char:
            in_char = False
            # Min span width 1 (was 2) — handles edge case where
            # binarization renders `1` as a 1-px-wide stripe (chromatic
            # aberration / thin-stroke fonts). Prevents leading `1`
            # from being silently dropped before the rescue helpers
            # even get a chance to inspect it.
            if x - start >= 1:
                spans.append((start, x))

    log.warning(
        "[DIAG] _segment_glyphs: %d raw spans: %s (gray.shape=%s)",
        len(spans), spans, gray.shape,
    )

    # ── Explicit leading "1" detector (bypasses the heuristic filters) ──
    # Why this exists: the projection-based segmenter + 3-pass leading-
    # narrow filter has been observed to drop genuine leading "1"s from
    # SC HUD values (e.g. mass="18629" arriving at the CNN as "8629" —
    # 4 glyphs instead of 5). Multiple failure modes feed this:
    #   1. The "1" is rendered with chromatic aberration → top-flag and
    #      stem are vertically separated → projection creates 2 thin
    #      spans, neither of which individually passes `_looks_like_one`
    #      (which inspects ONE span at a time).
    #   2. The "1" is wider than expected (subpixel AA puffs it up) →
    #      `_looks_like_one`'s aspect-ratio threshold (1.5) rejects it.
    #   3. Pass 2's gap-cut has NO leading-1 guard — if the "1"-to-"8"
    #      kerning gap is wider than digit-to-digit elsewhere in the
    #      number, gap-cut treats it as label-to-value boundary and
    #      drops the "1".
    #
    # The fix: positive evidence detection BEFORE the heuristic passes.
    # We scan the leftmost portion of the row for the SC "1" shape
    # signature — a contiguous run of stroke-density columns with
    # pixels distributed top + middle + bottom of the row. If we find
    # one, we ENSURE a span covering those columns survives all
    # subsequent filtering.
    #
    # v2.2.7 first-cut had `run_w <= 10` which was way too tight — at
    # typical render scales the SC HUD "1" is 15-25 columns wide
    # (it's a stylized character with a flag, body, and serif, not a
    # thin vertical bar). The detector silently skipped every real
    # capture. Now: cap is a fraction of the row's expected digit
    # width (estimated from height, since digits are ~0.6× as wide as
    # tall in this font). Diagnostic logs every skip with the actual
    # measurements so we can re-tune from real data.
    leading_one_span: Optional[tuple[int, int]] = None
    if w >= 6:
        # Scan up to half the row width (anything beyond is past where
        # a leading digit could be — leading digits live near the left
        # by definition, even allowing for label intrusion).
        scan_cols = max(8, min(w // 2, 60))
        col_stroke_counts = (binary[:, :scan_cols] > 0).sum(axis=0)
        # Identify "stroke columns" — columns where >25% of rows have
        # a stroke pixel. A vertical "1" stem produces a tall cluster
        # of these.
        stroke_cols = col_stroke_counts > max(2, int(h * 0.25))
        # Find the LEFTMOST contiguous run of stroke columns.
        run_start = None
        run_end = None
        for x in range(scan_cols):
            if stroke_cols[x]:
                if run_start is None:
                    run_start = x
                run_end = x + 1
            elif run_start is not None:
                # Allow up to 4-column gaps within the run (chromatic
                # aberration / sub-pixel rendering can briefly drop a
                # column's stroke density below threshold even within
                # a single character). Breaking on the first gap
                # would split many real "1"s.
                if x - run_end >= 5:
                    break
        if run_start is not None and run_end is not None:
            run_w = run_end - run_start
            # SC HUD digits are roughly 0.55-0.75× as wide as tall.
            # An "1" is roughly 0.55-0.85× the width of an "8". So the
            # max plausible "1" width relative to row height is:
            #   0.85 × 0.75 × h ≈ 0.64 × h
            # Use a generous 0.85 × h cap to catch wide-rendered "1"s
            # while still rejecting full-shape digits like "0" / "8"
            # which tend to be 0.55-0.75× h (so they'd land below the
            # cap — wait, that's a false positive risk).
            #
            # Better discriminator: shape, not just width. A real
            # "0"/"8" has stroke pixels distributed across MORE than
            # one column-band horizontally (left edge, right edge,
            # middle gap). A "1" is concentrated mostly in one
            # vertical band even when wide (the body).
            #
            # We accept run_w up to 0.85×h but THEN require a "single
            # vertical band" check: the column with peak stroke
            # density must contain >70% of total stroke pixels in
            # the run. "0"/"8" fail this because their strokes spread
            # across the whole width.
            sub = binary[:, run_start:run_end]
            row_has_stroke = np.any(sub > 0, axis=1)
            vert_coverage = row_has_stroke.mean()
            top_third    = row_has_stroke[: h // 3].any()
            middle_third = row_has_stroke[h // 3 : 2 * h // 3].any()
            bottom_third = row_has_stroke[2 * h // 3 :].any()
            # Width cap proportional to row height (font-relative).
            width_cap = max(8, int(h * 0.85))
            # Single-vertical-band check: ratio of peak-column density
            # to total density. "1" → high (most ink in one band).
            # "0"/"8" → low (ink distributed left + right edges).
            col_densities = (sub > 0).sum(axis=0)
            total_ink = int(col_densities.sum())
            peak_col_ink = int(col_densities.max()) if col_densities.size else 0
            # Use the densest 30% of columns to be lenient — a slightly
            # tilted "1" smears its stroke across 2-3 adjacent columns.
            top_30pct_cols = max(1, int(round(run_w * 0.30)))
            top_density_sum = int(np.sort(col_densities)[-top_30pct_cols:].sum())
            band_concentration = (
                (top_density_sum / total_ink) if total_ink > 0 else 0.0
            )
            # Two regimes:
            #   NARROW (run_w < 0.4×h): a real "1" glyph in the SC HUD
            #     font is ~0.15–0.40× as wide as tall. A "0"/"8" needs
            #     ~0.55–0.75× the row height to render (closed loop).
            #     So a narrow run with full-height ink coverage is
            #     almost certainly a "1" — band-concentration becomes
            #     a weak secondary signal. Relax to ≥0.30. Pre-fix the
            #     unified 0.55 threshold rejected the user's leading
            #     "1"s at this render scale (band_conc 0.30–0.50)
            #     because the SC "1" has a flag + serif that spread
            #     ink across columns.
            #   WIDE (run_w ≥ 0.4×h): width alone can't distinguish "1"
            #     from "0"/"8". Keep the strict 0.55 band_concentration
            #     check as the discriminator.
            narrow_regime = run_w < int(h * 0.40)
            band_threshold = 0.30 if narrow_regime else 0.55
            looks_like_one_shape = (
                vert_coverage > 0.50
                and top_third
                and middle_third
                and bottom_third
                and run_w <= width_cap
                and band_concentration >= band_threshold
            )
            if looks_like_one_shape:
                leading_one_span = (int(run_start), int(run_end))
                log.warning(
                    "[DIAG] leading-1 detector LOCKED span "
                    "%s (run_w=%d cap=%d, vert_cov=%.2f, h=%d, "
                    "band_conc=%.2f, top/mid/bot=%s)",
                    leading_one_span, run_w, width_cap, vert_coverage,
                    h, band_concentration,
                    (top_third, middle_third, bottom_third),
                )
            else:
                # Diagnostic: log WHY we skipped so we can tune from
                # real-capture data without guessing. WARNING level so
                # it shows up regardless of the running app's log
                # config (some configs filter INFO and below).
                log.warning(
                    "[DIAG] leading-1 detector SKIPPED run "
                    "(start=%d end=%d run_w=%d cap=%d, vert_cov=%.2f, "
                    "h=%d, band_conc=%.2f, top/mid/bot=%s)",
                    run_start, run_end, run_w, width_cap, vert_coverage,
                    h, band_concentration,
                    (top_third, middle_third, bottom_third),
                )

    # Right-anchored span filter: SC HUD values are read right-to-left
    # and the LEFT edge is where label-text intrusion shows up
    # (e.g. trailing colon of "RESISTANCE:" leaking in front of "0%").

    # Helper: a leading "narrow" span looks like a real digit `1` if
    # it's TALL relative to its width. The SC font's `1` glyph has
    # aspect ratio (height/width) of roughly 2.5-3.5, while halo
    # dots / chromatic aberration / colon residue are roughly square
    # (ratio ~1.0-1.3) or wider. Without this guard, the width-based
    # "leading-narrow drop" eats real `1`s every time the value
    # starts with one — turning 14156 into 4156, 11565 into 1565,
    # etc.
    def _looks_like_one(span_idx: int) -> bool:
        if not (0 <= span_idx < len(spans)):
            return False
        s, e = spans[span_idx]
        w_px = max(1, e - s)
        ys = np.where(np.any(binary[:, s:e] > 0, axis=1))[0]
        if ys.size == 0:
            return False
        h_px = int(ys[-1] - ys[0] + 1)
        # Aspect threshold lowered 2.0->1.5 to catch real `1`s widened
        # by chromatic aberration / halo (e.g. 10px wide × 18px tall ≈
        # ratio 1.8). The previous 2.0 floor was rejecting these
        # genuine `1`s, sending them down the leading-narrow drop path.
        return (h_px / w_px) >= 1.5

    # Helper: a leading span is real-digit-shaped (NOT noise/colon
    # residue) if its content is full-height. Digits 0-9 all rise to
    # roughly the row's cap height; colons / halo dots / chromatic
    # aberration are SHORT (less than half the row). The width-only
    # narrowness check that lived here previously was dropping real
    # leading "0"s in values like "0%" because "0" is naturally
    # narrower than "%" — full-height-vs-short is the right signal.
    def _looks_full_height(span_idx: int) -> bool:
        if not (0 <= span_idx < len(spans)):
            return False
        s, e = spans[span_idx]
        ys = np.where(np.any(binary[:, s:e] > 0, axis=1))[0]
        if ys.size == 0:
            return False
        h_px = int(ys[-1] - ys[0] + 1)
        # Height threshold lowered 50%->40% so a `1` whose binarization
        # has small gaps (mid-stroke flicker) still passes the rescue.
        # Real digits 0-9 in SC HUD fonts NEVER render shorter than 40%
        # of row height; sub-40% leading spans are halo dots / colon
        # residue, which is still rejected.
        return h_px >= int(gray.shape[0] * 0.4)

    # ── Instability dot-protect pre-scan ──
    # When the caller declared ``field="instability"``, scan the raw
    # span list for a dot-shaped interior span before the pruning
    # passes run. A dot in this context is:
    #
    #   * narrow:        width <= max(6, 0.65 × median_w)
    #   * vertically concentrated: either short (height ≤ max(4,
    #     0.30 × row_h)) OR low vert-coverage (≤ 0.55 of rows
    #     contain ink)
    #   * INTERIOR:      not the leftmost or rightmost span (a
    #     leading or trailing tiny span is far more likely to be
    #     label residue or noise, not the decimal point)
    #
    # When such a span is detected, we skip pass2 (gap-cut) and pass3
    # (leading-narrow drop) entirely. This protects the structural
    # ``X.YZ`` / ``XX.YZ`` pattern that gets shredded by the default
    # pruning on short instability values: the kerning gap between
    # the leading digit and the dot is just barely above the gap-cut
    # threshold, and the dot's own narrow shape makes pass3 think it
    # was the next leading-narrow speck to drop.
    #
    # Bounds for "instability is the field":
    #   * Skipping pruning means leading label residue could survive
    #     into the cascade. But the instability value column is
    #     already isolated by ``_find_value_crop`` (label-row anchor
    #     finder strips ``INSTABILITY:`` from the left), so legitimate
    #     leading content past that crop boundary should ALL be real
    #     glyphs. Downstream structural validators (must contain ``.``,
    #     value ≤ 200) catch any genuine label-residue survivors.
    #   * No-op for the other fields — mass/resistance have no
    #     interior small-glyph signature to protect.
    _instab_dot_detected = False
    if field == "instability" and len(spans) >= 3:
        widths_now = sorted(e - s for s, e in spans)
        median_w_now = widths_now[len(widths_now) // 2]
        dot_w_cap = max(6, int(median_w_now * 0.65))
        dot_h_cap = max(4, int(h * 0.30))
        for i, (s, e) in enumerate(spans):
            # Only INTERIOR spans qualify — leading / trailing tiny
            # spans are more likely to be junk.
            if i == 0 or i == len(spans) - 1:
                continue
            w_px = e - s
            if w_px > dot_w_cap:
                continue
            # Per-row ink check restricted to this span's column slice.
            row_has_ink = np.any(binary[:, s:e] > 0, axis=1)
            ys_local = np.where(row_has_ink)[0]
            if ys_local.size == 0:
                continue
            span_h = int(ys_local[-1] - ys_local[0] + 1)
            vert_cov = float(row_has_ink.mean())
            if span_h <= dot_h_cap or vert_cov <= 0.55:
                _instab_dot_detected = True
                log.info(
                    "_segment_glyphs: instability-dot pre-scan detected "
                    "span=%s idx=%d (w=%d h=%d vert_cov=%.2f median_w=%d "
                    "row_h=%d) — skipping pass2 gap-cut and pass3 "
                    "leading-narrow drop to preserve leading digit + dot",
                    (s, e), i, w_px, span_h, vert_cov, median_w_now, h,
                )
                break

    if len(spans) >= 2:
        # (1) Drop ANY leading span whose width OR HEIGHT is small
        # relative to the median real digit. Catches colons, halo,
        # chromatic-aberration dots — but NOT a real leading `1` (aspect-
        # ratio guard) or full-height digit `0` (full-height guard).
        if len(spans) >= 3:
            widths = sorted(e - s for s, e in spans)
            median_w = widths[len(widths) // 2]
            # Width threshold lowered 0.80->0.40 of median: only fire
            # the leading-narrow filter if the leading span is REALLY
            # narrow (less than 40% of median, where halo dots and
            # colon-tails live). Real `1`s in SC HUD font are usually
            # 40-60% of median digit width, so they no longer trigger
            # this filter at all — sidestepping the brittle rescue
            # helpers entirely for the common case.
            min_real_width = max(4, int(median_w * 0.40))
            while spans and (spans[0][1] - spans[0][0]) < min_real_width:
                # Evaluate rescue helpers BEFORE popping (popping
                # changes the index, invalidating their span lookup).
                popped_span = spans[0]
                w_px = popped_span[1] - popped_span[0]
                looks_one = _looks_like_one(0)
                looks_full = _looks_full_height(0)
                if looks_one or looks_full:
                    break  # real digit, leave alone
                log.info(
                    "_segment_glyphs: pass1 dropped leading span %s "
                    "width=%d (median_w=%d, min_real_width=%d, "
                    "looks_like_one=%s, looks_full_height=%s)",
                    popped_span, w_px, median_w, min_real_width,
                    looks_one, looks_full,
                )
                spans.pop(0)
        elif len(spans) == 2:
            w1 = spans[0][1] - spans[0][0]
            w2 = spans[1][1] - spans[1][0]
            # Drop leading if it's noticeably narrower than the next
            # AND not a real digit. Real digits are either tall-narrow
            # like `1` OR full-height like `0`/`8`/etc.
            if (w1 < w2 * 0.6
                    and not _looks_like_one(0)
                    and not _looks_full_height(0)):
                log.info(
                    "_segment_glyphs: pass1 (2-span) dropped leading "
                    "span %s width=%d (next_width=%d, "
                    "looks_like_one=False, looks_full_height=False)",
                    spans[0], w1, w2,
                )
                spans = spans[1:]

        # (2) Find the largest gap between adjacent spans; discard
        # everything LEFT of it (label-to-value boundary).
        # Tightened so even modestly-larger gaps trigger the cut —
        # inter-digit gaps in SC HUD font are very uniform.
        #
        # Skipped entirely when ``disable_gap_cut`` is True (region2
        # signature path) — those crops have no label intrusion to
        # cut away, and a wide right-edge gap (between the last real
        # digit and a pill end-cap artifact) would otherwise drag
        # every real digit into the "left of gap" bucket and erase
        # them. Stages 1 and 3 still run, so spans of comma/colon
        # residue and pure-noise leading specks are still removed.
        if (
            not disable_gap_cut
            and not _instab_dot_detected
            and len(spans) >= 2
        ):
            gaps = [(spans[i + 1][0] - spans[i][1], i) for i in range(len(spans) - 1)]
            largest_gap, gap_idx = max(gaps, key=lambda g: g[0])
            sorted_gaps = sorted(g for g, _ in gaps)
            median_gap = sorted_gaps[len(sorted_gaps) // 2]
            # gap >1.4× median (was 1.6) OR >8px absolute (was 12)
            threshold = max(8, int(median_gap * 1.4 + 1))
            if largest_gap >= threshold:
                log.info(
                    "_segment_glyphs: pass2 gap-cut at idx=%d "
                    "(gap=%d, median_gap=%d, threshold=%d) — dropped "
                    "%d leading spans",
                    gap_idx, largest_gap, median_gap, threshold,
                    gap_idx + 1,
                )
                spans = spans[gap_idx + 1:]

        # (3) After the gap-cut, if the new leading span is STILL
        # disproportionately narrow vs the rest, drop it once more —
        # but again only if it doesn't look like a real digit.
        # Skipped when the instability dot-protect pre-scan latched:
        # the leading span on those crops is the value's leading
        # digit (often a narrow ``1``) and the dot itself, both of
        # which we want to keep.
        if not _instab_dot_detected and len(spans) >= 3:
            widths = [e - s for s, e in spans[1:]]
            avg_real = sum(widths) / len(widths)
            lead_w = spans[0][1] - spans[0][0]
            if (lead_w < avg_real * 0.65
                    and not _looks_like_one(0)
                    and not _looks_full_height(0)):
                log.info(
                    "_segment_glyphs: pass3 dropped leading span %s "
                    "width=%d (avg_real=%.1f, threshold=%.1f, "
                    "looks_like_one=False, looks_full_height=False)",
                    spans[0], lead_w, avg_real, avg_real * 0.65,
                )
                spans = spans[1:]

    # ── Reconcile with the leading-1 detector ──
    # If the leading-1 detector locked a span but the heuristic filters
    # have either dropped it OR replaced it with a sub-fragment of
    # itself, restore the locked span as the leftmost entry. This is
    # the actual rescue path that makes the detector useful — without
    # it, the detector's findings would be silently discarded by
    # whatever pass dropped the leading span.
    if leading_one_span is not None:
        lone_s, lone_e = leading_one_span
        # Has any surviving span overlap with the locked range?
        overlapping_idx: Optional[int] = None
        for i, (s, e) in enumerate(spans):
            # Treat any overlap as "this span IS the leading 1 (or part
            # of it)" — rather than re-prepending, snap the leftmost
            # overlap-with-detector span to the detector's full range.
            if not (e <= lone_s or s >= lone_e):
                overlapping_idx = i
                break
        if overlapping_idx is None:
            # Detector found a "1" but the heuristics dropped it
            # entirely. Prepend.
            log.info(
                "_segment_glyphs: leading-1 detector restoring dropped "
                "span %s (no overlap with surviving spans)",
                leading_one_span,
            )
            spans.insert(0, leading_one_span)
        else:
            # Surviving span overlaps the detector's range. Make sure
            # the leftmost surviving span covers the FULL detector range
            # (a chromatic-aberration-split "1" might survive as just
            # its stem; we want to extend back to include the flag).
            cur_s, cur_e = spans[overlapping_idx]
            new_s = min(cur_s, lone_s)
            new_e = max(cur_e, lone_e)
            if (new_s, new_e) != (cur_s, cur_e):
                log.info(
                    "_segment_glyphs: leading-1 detector extending span "
                    "%s -> %s",
                    (cur_s, cur_e), (new_s, new_e),
                )
                spans[overlapping_idx] = (new_s, new_e)
            # Drop any spans that the heuristics may have placed BEFORE
            # the detector's locked "1" — those would be label intrusion
            # (we've identified the real leading digit; nothing should
            # come before it in the final read).
            if overlapping_idx > 0:
                log.info(
                    "_segment_glyphs: leading-1 detector trimming %d "
                    "pre-1 spans: %s",
                    overlapping_idx, spans[:overlapping_idx],
                )
                spans = spans[overlapping_idx:]

    # Final defensive guard: log the surviving span count + widths +
    # heights so the caller can grep "returning %d spans" to check
    # whether segmentation matches the expected digit count for the
    # field. (We don't have a field-aware count hint yet — caller's
    # responsibility for now.)
    # ── Post-segmentation rescue: scan-for-missed-leading-1 ──
    # After all the heuristic passes, look at the columns LEFT of the
    # leftmost surviving span. If they contain a tall column of ink,
    # the projection-based segmenter (or one of the leading-narrow
    # filters) lost a real digit — almost always a "1", since it's
    # the thinnest character in SC's HUD font and thus the easiest
    # for the heuristics to mistake for noise / colon residue.
    #
    # This complements the pre-segmentation `leading_one_span` detector
    # — that one tries to predict the "1" up front; this one cleans up
    # whatever escaped it. Either layer can fix the bug; both layers
    # together cover more failure modes than one alone.
    #
    # Conservative: requires positive evidence (actual ink, with
    # ≥40% vertical coverage in the leftward region). Doesn't
    # fabricate spans where the leftward area is empty.
    if spans:
        leftmost_start = spans[0][0]
        # Need at least 4 cols of unaccounted space on the left to
        # plausibly contain a missed digit.
        if leftmost_start >= 4:
            pre = binary[:, :leftmost_start]
            # Per-column ink density. Even a sub-threshold stroke
            # registers — we want to catch faint AA'd "1"s here.
            col_inky = (pre > 0).sum(axis=0) > max(1, int(h * 0.08))
            if col_inky.any():
                # Find the rightmost contiguous run of inky columns —
                # that's the candidate digit, closest to the
                # detected glyphs.
                inky_idx = np.where(col_inky)[0]
                last_idx = int(inky_idx[-1])
                run_start = last_idx
                while run_start > 0 and col_inky[run_start - 1]:
                    run_start -= 1
                run_end = last_idx + 1
                # Verify vertical coverage in this run — digits are
                # tall, dust/halo/colon residue is short.
                sub_run = binary[:, run_start:run_end]
                row_has_stroke = np.any(sub_run > 0, axis=1)
                vert_coverage = float(row_has_stroke.mean())
                # Also need a real gap from the leftmost glyph (else
                # we're just extending an existing span).
                gap = leftmost_start - run_end
                # Cap on width so we don't accidentally catch a
                # full-width character that the segmenter for some
                # reason didn't link to the rest.
                run_w = run_end - run_start
                width_cap = max(8, int(h * 0.85))
                if (vert_coverage >= 0.40
                        and gap >= 1
                        and run_w <= width_cap):
                    log.warning(
                        "[DIAG] rescue-missed-1: prepending span "
                        "(%d, %d) before leftmost=%d "
                        "(gap=%d run_w=%d cap=%d vert_cov=%.2f)",
                        run_start, run_end, leftmost_start,
                        gap, run_w, width_cap, vert_coverage,
                    )
                    spans.insert(0, (int(run_start), int(run_end)))
                else:
                    log.warning(
                        "[DIAG] rescue-missed-1: candidate run "
                        "(%d, %d) rejected (gap=%d run_w=%d cap=%d "
                        "vert_cov=%.2f leftmost=%d)",
                        run_start, run_end, gap, run_w, width_cap,
                        vert_coverage, leftmost_start,
                    )

    final_widths = [e - s for s, e in spans]
    final_heights = []
    for s, e in spans:
        ys_f = np.where(np.any(binary[:, s:e] > 0, axis=1))[0]
        final_heights.append(int(ys_f[-1] - ys_f[0] + 1) if ys_f.size else 0)
    log.warning(
        "[DIAG] _segment_glyphs: returning %d spans (widths=%s, heights=%s)",
        len(spans), final_widths, final_heights,
    )

    # NOTE: previous version had a "split merged-digit spans" pass
    # here that fired when the widest span was ≥1.55× median. It
    # caused regressions on resistance/instability where '%' is
    # naturally ~1.6× a digit's width — the splitter sliced '%' in
    # half, breaking reads that were previously locked at 0.95+ conf.
    # Removed pending a more conservative re-implementation (likely
    # gated by an expected_count hint from a Tesseract pre-read).

    crops: list[np.ndarray] = []
    boxes: list[tuple[int, int, int, int]] = []
    for x1, x2 in spans:
        ys = np.where(np.any(binary[:, x1:x2] > 0, axis=1))[0]
        # Min 1 active row keeps narrow `.` glyphs (height 1-2 px after
        # binarization) from being silently dropped. Dropping the dot
        # turns "2.78" into "278" and downstream decimal-recovery can
        # then place the dot at the wrong position (e.g. "27.80").
        if len(ys) < 1:
            continue
        y1, y2 = ys[0], ys[-1] + 1
        crop = gray[y1:y2, x1:x2].astype(np.float32)
        pad = 2
        padded = np.full(
            (crop.shape[0] + pad * 2, crop.shape[1] + pad * 2),
            255.0, dtype=np.float32,
        )
        padded[pad:pad + crop.shape[0], pad:pad + crop.shape[1]] = crop
        pil = Image.fromarray(padded.astype(np.uint8)).resize(
            (28, 28), Image.BILINEAR,
        )
        crops.append(np.array(pil, dtype=np.float32) / 255.0)
        # Store source-coordinate bbox (pre-normalization) so the dot
        # detector downstream can compare relative widths/heights.
        boxes.append((int(x1), int(y1), int(x2 - x1), int(y2 - y1)))
    return crops, boxes


# ── Canonical blacklist templates for signature glyph filtering ──
# pHash on 8×8 average-thresholded fingerprints proved unreliable for
# distinguishing the location-pin icon from a 28×28 padded digit:
# both shapes hash to "blob in middle". NCC against full 28×28 icon
# templates (polarity-canonicalized + padded to match segment output)
# is dramatically more discriminating.
_SIG_BLACKLIST_TEMPLATES: Optional[list[np.ndarray]] = None
# NCC threshold: 0.55 catches the icon at every scale/polarity we've
# seen in user captures while leaving plenty of headroom over the
# best digit-vs-icon NCC (digits score 0.15–0.30 against the icon
# template).
_SIG_BLACKLIST_NCC_THR = 0.55


def _build_signature_blacklist_templates() -> list[np.ndarray]:
    """Render every blacklist PNG as a normalized 28×28 float32 [0,1]
    template, in the SAME representation segments come back as.

    For each blacklist source we generate BOTH polarities so the
    matcher catches the icon regardless of which polarity the work
    crop was canonicalized to.  Returns a list of zero-mean unit-
    variance 28×28 templates ready for direct NCC comparison.
    """
    from PIL import Image as _PILImg
    out: list[np.ndarray] = []
    try:
        import sys
        from pathlib import Path as _Path
        _scripts = _Path(__file__).resolve().parent.parent.parent / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))
        import extract_labeled_glyphs as _xlg  # type: ignore
    except Exception as exc:
        log.debug("api: blacklist template build swallowed: %s", exc)
        return out

    bl_dir = _xlg.BLACKLIST_DIR
    if not bl_dir.is_dir():
        return out

    for src_path in sorted(bl_dir.rglob("*.png")):
        try:
            src = np.asarray(
                _PILImg.open(src_path).convert("L"), dtype=np.uint8,
            )
        except Exception:
            continue
        if src.size == 0 or src.shape[0] < 4 or src.shape[1] < 4:
            continue
        # Canonical polarity (bright icon on dark) — same convention
        # _segment_glyphs feeds to _classify_crops.
        canon = _canonicalize_polarity(src)
        # Pad with 255 then resize to 28×28 — exact replay of the
        # _segment_glyphs tail.
        for variant_name, arr in (("canon", canon), ("inv", 255 - canon)):
            pad = 2
            crop = arr.astype(np.float32)
            padded = np.full(
                (crop.shape[0] + pad * 2, crop.shape[1] + pad * 2),
                255.0, dtype=np.float32,
            )
            padded[pad:pad + crop.shape[0], pad:pad + crop.shape[1]] = crop
            seg = _PILImg.fromarray(padded.astype(np.uint8)).resize(
                (28, 28), _PILImg.BILINEAR,
            )
            seg_f32 = np.asarray(seg, dtype=np.float32) / 255.0
            mean = float(seg_f32.mean())
            std = float(seg_f32.std())
            if std < 1e-6:
                continue
            normed = (seg_f32 - mean) / std
            out.append(normed)
            log.debug(
                "blacklist template loaded: %s [%s] shape=%s",
                src_path.name, variant_name, seg_f32.shape,
            )
    return out


def _ensure_signature_blacklist_templates() -> list[np.ndarray]:
    global _SIG_BLACKLIST_TEMPLATES
    if _SIG_BLACKLIST_TEMPLATES is None:
        _SIG_BLACKLIST_TEMPLATES = _build_signature_blacklist_templates()
        log.info(
            "_ensure_signature_blacklist_templates: built %d normalized "
            "28×28 NCC templates",
            len(_SIG_BLACKLIST_TEMPLATES),
        )
    return _SIG_BLACKLIST_TEMPLATES


def _drop_blacklisted_signature_glyphs(
    crops: list[np.ndarray],
    boxes: list[tuple[int, int, int, int]],
) -> tuple[list[np.ndarray], list[tuple[int, int, int, int]]]:
    """Drop signature crops that match a blacklist icon template.

    Switched from pHash similarity to NCC on full 28×28 representations
    after pHash kept silently missing the icon. The pHash fingerprint
    averages a 28×28 down to an 8×8 binary mask — at that resolution,
    the location-pin icon and a 0/8/round digit hash to nearly the same
    pattern, so even a 0.88 threshold doesn't separate them reliably.
    NCC compares full 28×28 normalized intensities pixel-by-pixel and
    cleanly distinguishes icons from digits (icons score 0.6–0.9
    against the icon template; digits score < 0.35 in stress tests).
    """
    if not crops or not boxes:
        return crops, boxes
    templates = _ensure_signature_blacklist_templates()
    if not templates:
        # No blacklist on this install — nothing to filter. Pipeline
        # behaves as if the filter is a no-op.
        return crops, boxes

    out_crops: list[np.ndarray] = []
    out_boxes: list[tuple[int, int, int, int]] = []
    for i, (crop, box) in enumerate(zip(crops, boxes)):
        try:
            # ── Aspect-ratio prefilter ──
            # Source-coordinate aspect: the location-pin icon is
            # approximately square (h/w ~0.9–1.1 across scales —
            # confirmed against multiple find_icon NCC picks in
            # production logs). Real digits and the comma-fused
            # multi-digit blobs that reach this filter are much
            # taller than wide (h/w ≥ 1.6 in every observed case).
            # At small render scales the 28×28 canonicalization
            # washes out enough icon-vs-digit shape difference that
            # NCC alone can't separate them — round digits like
            # 0/6/8 score 0.55–0.85 against the icon template, the
            # same band the icon itself lands in. Pre-filtering by
            # the source box's aspect prevents tall digit-shaped
            # boxes from ever entering the NCC race.
            bx, by, bw, bh = box
            if bw > 0 and (bh / float(bw)) >= 1.6:
                out_crops.append(crop)
                out_boxes.append(box)
                continue
            if crop.dtype != np.float32:
                cand = crop.astype(np.float32)
                if cand.max() > 1.5:
                    cand = cand / 255.0
            else:
                cand = crop
            if cand.shape != (28, 28):
                # Defensive — _segment_glyphs always produces 28×28 but
                # downstream callers may resize. Skip filter rather
                # than fail.
                out_crops.append(crop)
                out_boxes.append(box)
                continue
            cand_mean = float(cand.mean())
            cand_std = float(cand.std())
            if cand_std < 1e-6:
                out_crops.append(crop)
                out_boxes.append(box)
                continue
            cand_norm = (cand - cand_mean) / cand_std

            best_ncc = -2.0
            for tmpl in templates:
                ncc = float(np.mean(cand_norm * tmpl))
                if ncc > best_ncc:
                    best_ncc = ncc

            if best_ncc >= _SIG_BLACKLIST_NCC_THR:
                log.info(
                    "_drop_blacklisted_signature_glyphs: dropped span "
                    "idx=%d box=%s (NCC=%.2f vs blacklist template, "
                    "threshold=%.2f — likely the location-pin icon)",
                    i, box, best_ncc, _SIG_BLACKLIST_NCC_THR,
                )
                continue
        except Exception as exc:
            log.debug(
                "api: blacklist NCC check failed at idx=%d: %s", i, exc,
            )
        out_crops.append(crop)
        out_boxes.append(box)
    return out_crops, out_boxes


# ── Comma fraction constants ────────────────────────────────────────
# A SC HUD signature comma is roughly 3-5 px tall in a band that
# typically holds 22-25 px tall digits. So a comma's contribution to
# a fused digit+comma box is ~15-25% of the digit's height. We use
# 1.15× the median digit-only box height as the "this box has a
# comma fused below it" trigger — strong enough to ignore natural
# inter-digit height variance (1-2 px), tight enough to catch every
# 3-5 px comma protrusion. Mirror of how _DOT_W_FRAC / _DOT_H_FRAC
# work for the HUD's `.` detector.
# A SC HUD signature comma is roughly 3-5 px tall in a band that
# typically holds 22-25 px tall digits. That's ~12-22% of digit
# height. We trigger the trimmer at 1.10× median (10% over) AND
# at least 2 px excess — the previous 1.15×+3px threshold was too
# lenient and was missing real cases like a 22-px digit + 4-px
# comma fusion (h=26, threshold=28). At 1.10×+2px the same case
# crosses (h=26, threshold=24).
_COMMA_HEIGHT_RATIO = 1.10
_COMMA_HEIGHT_ABS_PX = 2  # also require at least N px excess


def _trim_comma_fused_into_signature_boxes(
    crops: list[np.ndarray],
    boxes: list[tuple[int, int, int, int]],
    work_canon: np.ndarray,
    binary: np.ndarray,
) -> tuple[list[np.ndarray], list[tuple[int, int, int, int]]]:
    """Trim comma protrusions fused into digit boxes.

    Mirrors the HUD's ``_dot_label_from_box`` size-based heuristic but
    inverted: the HUD uses box-width-and-height to *identify* a dot
    that already lives in its own box; here, the comma is fused into
    the preceding digit's box because column-projection segmentation
    saw no zero column between them. The fused box has full digit
    width but its **height** extends below the digit baseline to
    include the comma's pixels.

    Detection: any box whose height is more than
    ``_COMMA_HEIGHT_RATIO`` × median(box heights) — and at least
    ``_COMMA_HEIGHT_ABS_PX`` px taller than the median — has a comma
    fused below the digit. We trim the box back to the median height
    (chops off the comma) and re-extract the 28×28 crop from the
    canonical work image so the CNN sees a clean digit.

    Why height and not width: a comma sits in the same x-columns as
    the trailing edge of the preceding digit (that's why it fuses to
    begin with), so the box's WIDTH stays at digit-width. The
    distinguishing feature is the box's vertical extent reaching
    below the digit baseline.

    Why median rather than absolute: digit heights vary by font /
    capture resolution (~20 px at 1080p, ~28 px at 4K). Median across
    the row is the only stable reference.

    Skipped when the row has fewer than 3 boxes — too few samples for
    a reliable median, and a 1-2-box capture is almost certainly an
    anchor failure where structural recovery is hopeless anyway.
    """
    if not crops or not boxes:
        return crops, boxes
    if len(boxes) < 3:
        return crops, boxes

    heights = [int(b[3]) for b in boxes]
    median_h = float(np.median(heights))
    if median_h < 8:
        return crops, boxes
    cutoff_height = int(max(
        median_h * _COMMA_HEIGHT_RATIO,
        median_h + _COMMA_HEIGHT_ABS_PX,
    ))

    new_crops: list[np.ndarray] = []
    new_boxes: list[tuple[int, int, int, int]] = []
    n_trimmed = 0
    for crop, box in zip(crops, boxes):
        x, y, w, h = box
        if h <= cutoff_height:
            new_crops.append(crop)
            new_boxes.append(box)
            continue

        # Trim to the median height. The digit body is at the TOP of
        # the box (digits sit on the baseline), so we keep top rows
        # and drop the bottom rows where the comma lives.
        new_h = int(median_h)
        if new_h < 8:
            new_crops.append(crop)
            new_boxes.append(box)
            continue

        # Re-extract the crop using the same padding + 28×28 resize
        # convention as _segment_glyphs so downstream classifiers see
        # the exact same tensor shape.
        try:
            crop_gray = work_canon[y:y + new_h, x:x + w].astype(np.float32)
            pad = 2
            padded = np.full(
                (crop_gray.shape[0] + pad * 2, crop_gray.shape[1] + pad * 2),
                255.0, dtype=np.float32,
            )
            padded[pad:pad + new_h, pad:pad + w] = crop_gray
            new_pil = Image.fromarray(padded.astype(np.uint8)).resize(
                (28, 28), Image.BILINEAR,
            )
            trimmed = np.array(new_pil, dtype=np.float32) / 255.0
        except Exception as exc:
            log.debug(
                "_trim_comma_fused_into_signature_boxes: re-extract "
                "failed for box %s: %s — keeping original crop",
                box, exc,
            )
            new_crops.append(crop)
            new_boxes.append(box)
            continue

        new_crops.append(trimmed)
        new_boxes.append((x, y, w, new_h))
        n_trimmed += 1
        log.info(
            "_trim_comma_fused_into_signature_boxes: trimmed box "
            "(x=%d, y=%d, w=%d) from h=%d to h=%d (median=%.1f, "
            "cutoff=%d) — fused comma stripped",
            x, y, w, h, new_h, median_h, cutoff_height,
        )

    if n_trimmed > 0:
        log.info(
            "_trim_comma_fused_into_signature_boxes: trimmed %d/%d boxes",
            n_trimmed, len(boxes),
        )
    return new_crops, new_boxes


def _strip_non_digit_signature_secondary(
    sec_results: list[tuple[str, float]],
    pri_results: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Replace non-digit secondary classifications with the matching
    primary class so the live viewer doesn't surface spurious '%' / '.'
    on signature rows.

    Why: the secondary classifier on the signature path is the HUD-
    inverted CNN, whose alphabet is ``0123456789.%`` — it has no
    comma class, so when the segmenter produces a fused digit+comma
    crop, the secondary classifies the dangling-mark shape as ``%``.
    That ``%`` then shows up in the SIGNATURE (SECONDARY) tile row of
    the live viewer next to the otherwise-correct primary read,
    confuses the dual-agree gate, and looks broken to the user even
    though the final integer (taken from primary) is right.

    Replacement rather than dropping keeps the secondary list aligned
    with primary, so dual-agree comparisons still work. Confidence
    drops to 0 for any replaced position so the dual-agree mean check
    correctly recognizes the secondary didn't actually classify those
    crops as digits.
    """
    if not sec_results or len(sec_results) != len(pri_results):
        return sec_results
    out: list[tuple[str, float]] = []
    n_replaced = 0
    for (pri_ch, _pri_c), (sec_ch, sec_c) in zip(pri_results, sec_results):
        if not sec_ch.isdigit() and pri_ch.isdigit():
            out.append((pri_ch, 0.0))
            n_replaced += 1
        else:
            out.append((sec_ch, sec_c))
    if n_replaced > 0:
        log.info(
            "_strip_non_digit_signature_secondary: replaced %d "
            "non-digit secondary classifications (e.g. '%%' from "
            "fused digit+comma crops) with the matching primary "
            "char at confidence 0",
            n_replaced,
        )
    return out


def _strip_pill_outline_bridges(binary: np.ndarray) -> np.ndarray:
    """Trim pill-outline bridge rows from the binary mask.

    Region2 (signature) crops are derived proportionally from the pill
    bbox via ``world_model_region2``. The proportional offsets are an
    average over labelled captures, so on individual frames the crop
    extends a few rows past the pill body — into the dark space below
    the pill outline. After ``_canonicalize_polarity`` inverts to
    bright-text-on-dark convention, that originally-dark boundary
    becomes a BRIGHT stripe in the binary mask: a near-full-width
    horizontal bridge that connects all digit columns into one
    contiguous run, breaking the column-projection segmenter.

    A real digit row never spans more than ~50% of the crop's columns
    (digits are separated by background gaps); the bridge spans 80%+
    by definition. Trim any row within the first/last 25% of the band
    whose ink fraction exceeds 0.55, then peel additional edge rows
    until the column projection has at least one zero gap (so the
    segmenter can split the band into digit spans).

    Region2-specific (signature pipeline). Region1 / HUD callers do
    NOT route their masks through this — the HUD value crops use
    their own row-isolate that already trims past the panel bezel.
    """
    if binary is None or binary.size == 0:
        return binary
    H, W = binary.shape
    if H < 8 or W < 8:
        return binary

    out = binary.copy()

    # ── Stage 0: zero full-height vertical walls ──
    # When the world-model crop_box extends past the icon (left) or
    # past the pill end-cap (right), those originally-dark boundary
    # columns become BRIGHT after polarity-canonicalize and form a
    # near-solid vertical stripe in the binary mask. Walk inward from
    # each edge while the column ink fraction is ≥0.85 (a real digit
    # column never reaches that fraction even on closed-loop digits
    # like 0/8). Stop at the first sub-threshold column from each
    # side.
    wall_col_thr = 0.85
    col_frac = (out > 0).sum(axis=0) / max(1, H)
    # Edge tolerance: the pill's rounded cap can fade to a low-frac
    # column at the very edge before the straight wall picks up. Allow
    # up to 2 such "tail" columns at each edge — if a wall column sits
    # within 2 cols inward, treat the tail as part of the wall.
    edge_skip_k = 2
    left_wall_end = 0
    if W > edge_skip_k and col_frac[0] < wall_col_thr:
        for k in range(1, edge_skip_k + 1):
            if col_frac[k] >= wall_col_thr:
                left_wall_end = k
                break
    while left_wall_end < W and col_frac[left_wall_end] >= wall_col_thr:
        left_wall_end += 1

    right_wall_start = W
    if W > edge_skip_k and col_frac[W - 1] < wall_col_thr:
        for k in range(1, edge_skip_k + 1):
            if col_frac[W - 1 - k] >= wall_col_thr:
                right_wall_start = W - k
                break
    while (
        right_wall_start > left_wall_end
        and col_frac[right_wall_start - 1] >= wall_col_thr
    ):
        right_wall_start -= 1
    if left_wall_end > 0 or right_wall_start < W:
        if left_wall_end > 0:
            out[:, :left_wall_end] = 0
        if right_wall_start < W:
            out[:, right_wall_start:] = 0
        log.info(
            "_strip_pill_outline_bridges: stage-0 zeroed %d left + "
            "%d right wall cols (frac>=%.2f) — W=%d kept=[%d:%d]",
            left_wall_end, W - right_wall_start, wall_col_thr,
            W, left_wall_end, right_wall_start,
        )

    # ── Stage 1: zero bridge rows in the edge windows ──
    # The pill-outline bridge sits 1-3 rows inset from the very edge
    # of the crop band — e.g. bottom edge at row 41, bridge at rows
    # 38-40, row 41 is background falloff (lower frac than the
    # bridge). So we scan a window from each edge for ANY row whose
    # ink fraction exceeds the bridge threshold and zero those rows.
    #
    # Window size: 25% of the row band. Real digit rows can occupy
    # the entire band, so we wouldn't want to scan deeper than this
    # without risking zeroing real digit ink. The bottom edge of an
    # SC HUD signature pill clips the digit baseline at most ~3 rows
    # past the cap line — well inside 25%.
    bridge_row_thr = 0.55
    row_frac = (out > 0).sum(axis=1) / max(1, W)
    edge_window = max(2, int(H * 0.25))
    n_top_trimmed = 0
    n_bot_trimmed = 0
    for i in range(min(edge_window, H)):
        if row_frac[i] > bridge_row_thr:
            out[i] = 0
            n_top_trimmed += 1
    for i in range(max(H - edge_window, 0), H):
        if row_frac[i] > bridge_row_thr:
            out[i] = 0
            n_bot_trimmed += 1
    if n_top_trimmed > 0 or n_bot_trimmed > 0:
        log.info(
            "_strip_pill_outline_bridges: zeroed %d top-window + %d "
            "bot-window bridge rows (frac>%.2f, window=%d) — H=%d",
            n_top_trimmed, n_bot_trimmed, bridge_row_thr,
            edge_window, H,
        )

    # ── Stage 2: span-recovery peel ──
    # If the column projection still has NO zero column (every column
    # has at least one ink row), pick the highest-density edge-window
    # row and zero it. Repeat until the projection has at least one
    # zero gap (so the segmenter can split). Cap at edge_window so we
    # never zero more than 25% of the band height total — beyond that,
    # the mask is too corrupted to recover and we'd rather emit no
    # result than fabricate spans.
    edge_peeled = 0
    while edge_peeled < edge_window:
        col_proj = (out > 0).any(axis=0)
        if not col_proj.all():
            break
        candidates: list[tuple[float, int]] = []
        for i in range(min(edge_window, H)):
            f = float((out[i] > 0).sum()) / max(1, W)
            if f > 0:
                candidates.append((f, i))
        for i in range(max(H - edge_window, 0), H):
            f = float((out[i] > 0).sum()) / max(1, W)
            if f > 0:
                candidates.append((f, i))
        if not candidates:
            break
        candidates.sort(reverse=True)
        worst = candidates[0][1]
        out[worst] = 0
        edge_peeled += 1
    if edge_peeled > 0:
        log.info(
            "_strip_pill_outline_bridges: span-recovery peeled %d "
            "edge row(s) until projection had a gap",
            edge_peeled,
        )

    return out


def _mask_commas_in_signature_band(binary: np.ndarray) -> np.ndarray:
    """Mask out comma-shaped ink in the signature row band BEFORE
    column-projection segmentation runs.

    Two mechanisms run in series:

    1. **Below-baseline trim.** Signature digits ``0``-``9`` have
       no descenders — their ink sits entirely between the cap line
       and the baseline. So anything below the LAST row whose ink
       count crosses the digit-body threshold is, by elimination,
       comma / dust / AA halo bleeding under the baseline. We trim
       all those rows to zero. This catches the most common failure
       mode: a comma rendered in the same columns as the trailing
       edge of the preceding digit, where chromatic aberration
       bridges the digit baseline → comma so column-projection
       segmentation sees them as one contiguous run.

    2. **Per-column gap detection.** Catches commas in columns where
       the digit body is narrow enough that AA didn't bridge the
       baseline. We walk each column, find runs of consecutive ink,
       and mask the bottom run when there's a clean gap between it
       and a digit-tall top run (preserves hollow-digit interiors).

    Both run unconditionally — the row-band trim is fast (one
    np.sum + one slice assignment) and idempotent on bands that
    already lack below-baseline ink.
    """
    if binary is None or binary.size == 0:
        return binary
    H, W = binary.shape
    if H < 8 or W < 4:
        return binary

    out = binary.copy()

    # ── Stage 1: REMOVED ──
    # Earlier attempts at a band-wide "below-baseline trim" all over-
    # trimmed digits like ``7`` whose diagonal stroke has the same
    # per-row ink count (and same widest-run width) as a comma. There
    # is no reliable row-level signal that distinguishes the bottom
    # of a thin-stroke digit from a comma. We rely on stage 2 (per-
    # column gap detection) for clean-gap cases, and the post-seg
    # _enforce_comma_signature_structure filter as a last line of
    # defence. AA-bridged digit+comma cases reach the CNN as a fused
    # crop; the primary signal CNN classifies the merged shape as
    # the dominant digit (correct), and the secondary HUD-inverted
    # CNN's spurious '%' read on the same crop gets stripped post-
    # classification by ``_drop_non_digit_secondary_classifications``.

    # ── Stage 2: per-column gap detection ──
    # Thresholds are PROPORTIONAL to the input height H so the same
    # logic works at native panel scale (H≈14-18 px) AND after the
    # Lanczos upscale-to-~32px the runtime applies before
    # segmentation. The previous absolute thresholds (``bot_h <= 5``,
    # ``top_h >= 10``) were tuned for native scale and silently
    # no-op'd after the upscale, letting the comma slip through to
    # the per-glyph CNN as either its own tile or a fused chunk on a
    # neighbouring digit.
    #
    # Calibration (native H=18 baseline):
    #   bot_h <= 5  →  5/18 ≈ 0.28  →  ``H * 0.30`` with floor 3
    #   top_h >= 10 →  10/18 ≈ 0.56 →  ``H * 0.55`` with floor 8
    # At H=32 (post-Lanczos): bot ≤ 9, top ≥ 17 — catches a typical
    # comma's 6-9 px bottom-run while excluding thin-stroke digit
    # bodies whose runs span the full height.
    bottom_band_y = int(H * 0.6)
    bot_h_max = max(3, int(H * 0.30))
    top_h_min = max(8, int(H * 0.55))
    n_masked_columns = 0
    n_masked_pixels = 0

    for x in range(W):
        col = out[:, x] > 0
        if not col.any():
            continue
        runs: list[tuple[int, int]] = []
        in_run = False
        run_start = 0
        for y in range(H + 1):
            v = bool(col[y]) if y < H else False
            if v and not in_run:
                in_run = True
                run_start = y
            elif not v and in_run:
                in_run = False
                runs.append((run_start, y))
        if len(runs) < 2:
            continue
        top_run = runs[0]
        bot_run = runs[-1]
        top_h = top_run[1] - top_run[0]
        bot_h = bot_run[1] - bot_run[0]
        if (
            bot_h <= bot_h_max
            and top_h >= top_h_min
            and bot_run[0] >= bottom_band_y
        ):
            out[bot_run[0]:bot_run[1], x] = 0
            n_masked_columns += 1
            n_masked_pixels += bot_h

    if n_masked_columns > 0:
        log.info(
            "_mask_commas_in_signature_band: stage-2 column-gap "
            "masked %d pixels across %d columns",
            n_masked_pixels, n_masked_columns,
        )

    # ── Stage 3: orphan comma-tail columns (no top run) ──
    # Catches comma tails where the comma sits in the empty space
    # between two digits — no digit body in those columns to anchor
    # stage 2 against. Without this, the orphan tail bridges its
    # neighbouring digits into one merged span at the segmenter.
    #
    # Conditions for masking under stage 3:
    #   * Column ink is ONLY in the bottom band (≥ ``bottom_band_y``).
    #   * Column total ink ≤ ``bot_h_max`` (comma-height bound).
    #   * Column belongs to a SHORT contiguous run of similar bottom-
    #     only columns. A real comma's tail is at most ~5-6 columns
    #     wide at the rendered scale + AA. Wider runs are pill-outline
    #     bridges that ``_strip_pill_outline_bridges`` should have
    #     already handled — applying stage 3 to a leftover bridge
    #     would erase digit-row segmentation cues we still need.
    bottom_only_run_w_max = max(4, int(W * 0.06))
    bottom_only_cols = np.zeros(W, dtype=bool)
    for x in range(W):
        col_inds = np.where(out[:, x] > 0)[0]
        if col_inds.size == 0:
            continue
        if (
            int(col_inds[0]) >= bottom_band_y
            and col_inds.size <= bot_h_max
        ):
            bottom_only_cols[x] = True

    n_stage3_columns = 0
    n_stage3_pixels = 0
    x = 0
    while x < W:
        if not bottom_only_cols[x]:
            x += 1
            continue
        run_start = x
        while x < W and bottom_only_cols[x]:
            x += 1
        run_end = x
        run_w = run_end - run_start
        if run_w <= bottom_only_run_w_max:
            for cx in range(run_start, run_end):
                px_count = int((out[:, cx] > 0).sum())
                n_stage3_pixels += px_count
                out[:, cx] = 0
                n_stage3_columns += 1
    if n_stage3_columns > 0:
        log.info(
            "_mask_commas_in_signature_band: stage-3 orphan-comma "
            "masked %d pixels across %d columns (run_w_max=%d)",
            n_stage3_pixels, n_stage3_columns, bottom_only_run_w_max,
        )
    return out


def _enforce_comma_signature_structure(
    crops: list[np.ndarray],
    boxes: list[tuple[int, int, int, int]],
) -> tuple[list[np.ndarray], list[tuple[int, int, int, int]]]:
    """Drop comma-shaped spans + enforce signature value structure.

    Signature values are integers in [1000, 35000] formatted with a
    thousands separator: ``D,DDD`` (4 digits) or ``DD,DDD`` (5 digits).
    Two structural priors fall out of that:

    1. **Comma is narrow + short.** A comma-shaped span is < 50% the
       median digit width AND < 60% the median digit height. A digit
       ``1`` is narrow but full-height; a comma is narrow AND short.
       Drop any span matching this shape — the signal CNN's training
       set is digits-only (no comma class), so leaving it in would
       just have the CNN guess a random digit class for it,
       corrupting the read.

    2. **Post-comma count is ALWAYS 3.** When a comma sits at an
       interior index, the spans following it are exactly 3 digits
       (because signature values follow ``D,DDD`` or ``DD,DDD``).
       Pre-comma is 1-2 digits. Trimming to those bounds catches:

       - **Right-edge artifacts**: bubble-edge speckle that the
         segmenter picks up as a "digit" past the real last digit.
         Visible as 4+ post-comma spans where the trailing one is
         narrow/dim. Drop the rightmost extras.
       - **Left-edge residue**: icon body pieces that slipped past
         the blacklist filter, or chromatic-aberration ghost bits.
         Visible as 3+ pre-comma spans. Drop the leftmost extras.

    Returns the trimmed crops + boxes with the comma span dropped.
    Caller's splitter still runs after this so genuinely under-
    counted spans get split.
    """
    if not crops or not boxes:
        return crops, boxes

    widths = sorted([b[2] for b in boxes])
    heights = sorted([b[3] for b in boxes])
    if not widths or not heights:
        return crops, boxes
    median_w = widths[len(widths) // 2]
    median_h = heights[len(heights) // 2]
    if median_w < 4 or median_h < 6:
        # Too noisy to reason about — let later stages handle it.
        return crops, boxes

    # ── Pre-pass: drop noise specks AND wall-stripe artifacts ──
    # A speck is BOTH narrow AND short — far smaller than even a
    # comma's bbox. Real commas have h ≥ ~3 px (the comma tail) and
    # w ≥ ~2 px; specks are 1 px tall × 1-2 px wide.
    #
    # A wall stripe is full-height but extremely narrow (aspect ratio
    # h/w > 6). Real digits, including the thin "1", have aspect
    # ratio at most ~5. Anything thinner is pill-outline residue
    # that survived the upstream strip — typically 1-2 px wide × the
    # entire row height. Only drop these at the FAR LEFT or FAR
    # RIGHT of the span sequence; thin verticals inside the value
    # cluster could be a real "1" character.
    #
    # Keeping either of these would mis-anchor the comma detector
    # below: if a speck or stripe sits to the right of all real
    # digits, the structural trim treats THE LAST SPAN BEFORE THE
    # ARTIFACT as the comma and drops everything left of it —
    # destroying the leading digits. (Field-tested case: a "11,520"
    # capture where a 2×38 px right-edge wall stripe caused the
    # pre-comma trim to drop the leading "1" AND the "1+5" merged
    # span, leaving only "5+2", "0", and the artifact.)
    speck_h_max = max(2, int(median_h * 0.15))
    speck_w_max = max(2, int(median_w * 0.25))
    # Drop only the LAST (rightmost) span if it's an extreme aspect
    # ratio AND extremely narrow. Real values never end with a thin
    # vertical artifact — those are pill-end-cap residues. The
    # leading position is NOT subject to this filter (a real leading
    # "1" can be 3 px wide × 38 px tall, aspect 12).
    stripe_aspect_min = 10.0
    stripe_w_max = 3
    speck_indices = set()
    for i, (_x, _y, w, h) in enumerate(boxes):
        if h <= speck_h_max and w <= speck_w_max:
            speck_indices.add(i)
            continue
        # Right-edge wall stripe: only check the LAST span. Drop if
        # it's both very narrow AND aspect-extreme. Tight thresholds
        # so we never accidentally drop a real digit.
        if (
            i == len(boxes) - 1
            and len(boxes) >= 2
            and w <= stripe_w_max
            and h > 0
            and (h / max(1, w)) >= stripe_aspect_min
        ):
            speck_indices.add(i)
    if speck_indices:
        log.info(
            "_enforce_comma_signature_structure: dropping %d noise "
            "speck(s) at indices=%s before comma detection",
            len(speck_indices), sorted(speck_indices),
        )
        keep_idx = [i for i in range(len(boxes)) if i not in speck_indices]
        crops = [crops[i] for i in keep_idx]
        boxes = [boxes[i] for i in keep_idx]
        if not crops:
            return crops, boxes
        # Refresh medians after speck removal — they're used by the
        # comma detector below and we want them based on real-digit
        # geometry.
        widths = sorted([b[2] for b in boxes])
        heights = sorted([b[3] for b in boxes])
        if widths and heights:
            median_w = widths[len(widths) // 2]
            median_h = heights[len(heights) // 2]

    # Comma identification is OR-of-strong-conditions — a comma will
    # almost always trip the height check (commas sit below the digit
    # baseline so their bbox is FAR shorter than median digit height),
    # so we trust ``h < median_h * 0.6`` even when the comma's column
    # span happens to be wider than expected (e.g. when AA + chromatic
    # aberration smear the comma over more columns than its actual
    # ink extent). The width check is kept as a secondary trigger for
    # the rare case where vertical anti-aliasing inflates the comma's
    # bbox height — those still tend to be narrow.
    comma_indices = [
        i for i, (_x, _y, w, h) in enumerate(boxes)
        if (h < median_h * 0.6) or (w < median_w * 0.5 and h < median_h * 0.75)
    ]

    if not comma_indices:
        return crops, boxes

    # Edge-position commas are noise (a real comma sits BETWEEN
    # digits). Drop noisy edge commas but leave the structural trim
    # off when no interior comma exists.
    interior_commas = [
        i for i in comma_indices if 0 < i < len(boxes) - 1
    ]
    if not interior_commas:
        keep = [i for i in range(len(crops)) if i not in comma_indices]
        if len(keep) == len(crops):
            return crops, boxes
        log.info(
            "_enforce_comma_signature_structure: dropped %d edge "
            "comma-shaped spans (no interior comma found)",
            len(crops) - len(keep),
        )
        return [crops[i] for i in keep], [boxes[i] for i in keep]

    # The signature value has at most one comma. If somehow more than
    # one was detected (false positive), keep the leftmost interior
    # one as the canonical comma — it's the one most likely to be
    # between the leading 1-2 digits and the trailing 3.
    comma_idx = interior_commas[0]
    pre_idx = list(range(comma_idx))
    post_idx = list(range(comma_idx + 1, len(boxes)))

    drops_pre = 0
    drops_post = 0
    if len(pre_idx) > 2:
        drops_pre = len(pre_idx) - 2
        # Keep the 2 spans CLOSEST to the comma (rightmost of the
        # pre-comma group). Anything further left is pre-icon residue.
        pre_idx = pre_idx[-2:]
    if len(post_idx) > 3:
        drops_post = len(post_idx) - 3
        # Keep the 3 spans CLOSEST to the comma (leftmost of the
        # post-comma group). Anything further right is bubble-edge
        # speckle past the value's last digit.
        post_idx = post_idx[:3]

    keep = pre_idx + post_idx  # comma_idx itself is dropped
    log.info(
        "_enforce_comma_signature_structure: dropped comma at idx=%d, "
        "trimmed %d pre-comma extras + %d post-comma extras "
        "(in=%d -> out=%d)",
        comma_idx, drops_pre, drops_post, len(crops), len(keep),
    )
    return [crops[i] for i in keep], [boxes[i] for i in keep]


def _grow_signal_glyph_holes(
    crop: np.ndarray, size: int = 3,
) -> np.ndarray:
    """Grayscale-dilate the bright pixels of a signature glyph crop so
    its loop interiors and open corners read as decisive background to
    the per-glyph CNN.

    The signal CNN's hardest-to-disambiguate digit pairs all share
    rounded-rectangle silhouettes — `0` vs `6` vs `8` vs `9` differ
    only in which corners of the silhouette are CLOSED (digit ink
    fills the corner) vs OPEN (background pokes through). At the
    captured signal-panel resolution, those open corners can render
    as 1–3 pixels wide; on the per-glyph 28×28 crop after the
    bilinear resize, they shrink to sub-pixel features that bias the
    CNN's decision toward whichever closed-loop digit fits best.

    Grayscale dilation expands the bright (background) regions in
    each pixel's (size × size) neighborhood. Holes that were 1 px
    wide become 3 px wide; open corners that were ambiguous become
    decisive. The cost is symmetric: digit strokes erode by
    ``(size - 1) / 2`` pixels each side. At ``size = 3`` (1-pixel
    erosion) this is acceptable on SC's signal font where strokes
    are typically 3–5 px wide on the 28×28 crop — strokes thin to
    2–4 px, still classifier-readable, while holes go from 1–2 px to
    3–4 px wide and become unambiguously background.

    Trade-off versus training distribution: the historical training
    pool was extracted WITHOUT this dilation, so applying it at
    inference creates a small train/test mismatch. In the failure
    cases this targets — `6` misread as `8` because the 6's open
    top corner closed up at sub-pixel scale — the mismatch is
    acceptable because the alternative is a confidently-wrong read.

    Returns the dilated crop. Falls back to the input unchanged if
    scipy is unavailable.
    """
    try:
        from scipy.ndimage import grey_dilation as _grey_dilation
        return _grey_dilation(crop, size=size).astype(crop.dtype)
    except Exception as exc:
        log.debug("api: _grow_signal_glyph_holes swallowed: %s", exc)
        return crop


def _split_wide_signature_spans(
    work_canon: np.ndarray,
    binary: np.ndarray,
    crops: list[np.ndarray],
    boxes: list[tuple[int, int, int, int]],
    expected_count: int = 5,
) -> tuple[list[np.ndarray], list[tuple[int, int, int, int]]]:
    """Re-split merged-digit spans for the signature pipeline.

    The HUD's :func:`_segment_glyphs` deliberately removed its merged-
    digit splitter because ``%`` on resistance/instability is naturally
    ~1.6× a digit's width and the splitter sliced it. Signature values
    are pure 4-5 digit integers in [1000, 35000] with no ``%`` and no
    decimal — so we can safely split spans wider than 1.5× the median
    width.

    Two trigger conditions:
      A. ``len(spans) < expected_count`` AND any span is wider than
         1.5× the median (under-segmented; needs to grow).
      B. ANY span is wider than 1.7× the median, even when count
         already matches (someone got merged but the segmenter found
         the right number of total spans because something else split
         spuriously). Without this branch, the splitter never fires
         when the icon adds a fake span — count looks fine, but a real
         merged span sits in the result.

    Iteratively picks the widest span, splits at its lowest-ink
    interior column, repeats until count hits ``expected_count`` or no
    span exceeds the trigger threshold.
    """
    if not boxes:
        return crops, boxes
    widths = [b[2] for b in boxes]
    if not widths:
        return crops, boxes
    sorted_w = sorted(widths)
    median_w = sorted_w[len(sorted_w) // 2]
    if median_w < 4:
        # Too noisy — splitting in this regime fabricates fake digits
        # from anti-aliasing speckle.
        return crops, boxes

    # Convert boxes (x, y, w, h) → mutable column spans for splitting.
    spans_list: list[list[int]] = [[b[0], b[0] + b[2]] for b in boxes]
    proj = (binary > 0).astype(np.int32).sum(axis=0)

    # Floor for a "splittable" span: each half must be at least this
    # wide to be plausibly a single digit. Tied to the input image
    # height (digits in SC's HUD font are ~0.5–0.7× as wide as tall).
    digit_min_width = max(6, work_canon.shape[0] // 4)
    safety = 16
    grew = False
    while safety > 0:
        safety -= 1
        # Hard cap on total spans: don't over-segment past one extra.
        # Trigger B (count-matches outlier) can keep firing as the
        # median drops after each split, so without this cap the loop
        # cascades — observed in unit testing producing 7 spans for an
        # expected_count of 5.
        if len(spans_list) > expected_count:
            break
        widest_idx = max(
            range(len(spans_list)),
            key=lambda i: spans_list[i][1] - spans_list[i][0],
        )
        s, e = spans_list[widest_idx]
        widest_w = e - s
        under_count = len(spans_list) < expected_count
        # Two trigger regimes:
        #   under-count: ALWAYS split the widest until count matches,
        #     subject only to the digit_min_width floor. The user's
        #     "19,275" case had 3 spans for 5 digits with all spans
        #     similar width — no span was an outlier vs the median, so
        #     the previous wide-factor gate never fired and the
        #     merged-digit clusters stayed merged.
        #   count-matches: only split clear outliers (≥1.7× median) so
        #     a clean read with the right count doesn't get spuriously
        #     re-segmented into garbage.
        if under_count:
            # Floor: each half must be ≥ digit_min_width.
            if widest_w < digit_min_width * 2:
                break
            # Estimate how many digits this single merged span should
            # become from its width vs the median single-digit width.
            # Cap by how many pieces we can still produce without
            # exceeding expected_count: the widest span replaces one
            # entry in spans_list, so room = expected_count - len + 1.
            room = expected_count - len(spans_list) + 1
            est = int(round(widest_w / max(1, median_w)))
            expected_subcount = max(2, min(est, room))
            # Validate that each piece will be at least digit_min_width.
            # If not, fall back to a single 2-way lowest-ink split as
            # last resort (the legacy behaviour).
            if expected_subcount * digit_min_width <= widest_w:
                piece_w = widest_w // expected_subcount
                # Compute initial equal-width cut positions, then snap
                # each to the nearest local ink minimum within a window.
                # Pre-fix: equal-width splits cut "11" into [half-1,
                # half-1] when the actual gap between digits sits a few
                # pixels off-center. The classifier then sees half of
                # one digit + half of the other and confidently
                # misclassifies. Snapping each cut to the minimum-ink
                # column within ±(piece_w//4) lands cuts in the actual
                # valleys between glyphs.
                # Search half-window: enough to cover normal digit
                # spacing variance, but bounded so a proposed cut can't
                # cross into a neighbouring piece. piece_w//4 keeps
                # adjacent pieces from cannibalizing each other.
                snap_half = max(1, piece_w // 4)
                # Floor for accepting a snapped cut: the resulting
                # piece widths must each remain ≥ digit_min_width //2
                # (allow modest asymmetry — real glyph spacing isn't
                # uniform — but reject snaps that produce a sliver).
                min_piece_after_snap = max(2, digit_min_width // 2)
                cut_positions: list[int] = []
                for k in range(1, expected_subcount):
                    nominal = s + k * piece_w
                    lo = max(s + min_piece_after_snap, nominal - snap_half)
                    hi = min(e - min_piece_after_snap, nominal + snap_half)
                    if hi > lo:
                        # argmin over the projection in the snap window.
                        # proj is in original-image coords (x = column).
                        window_proj = proj[lo:hi]
                        snap = lo + int(np.argmin(window_proj))
                        # Guard against the snapped cut crossing the
                        # previous accepted cut (could happen with
                        # extreme asymmetric ink distribution).
                        prev = cut_positions[-1] if cut_positions else s
                        if snap - prev < min_piece_after_snap:
                            snap = prev + min_piece_after_snap
                        cut_positions.append(snap)
                    else:
                        cut_positions.append(nominal)
                pieces: list[list[int]] = []
                cur = s
                for cut in cut_positions:
                    pieces.append([cur, cut])
                    cur = cut
                pieces.append([cur, e])
                piece_widths = [p[1] - p[0] for p in pieces]
                spans_list = (
                    spans_list[:widest_idx]
                    + pieces
                    + spans_list[widest_idx + 1:]
                )
                grew = True
                log.info(
                    "_split_wide_signature_spans: equal-width split "
                    "(widest_w=%d, median_w=%d, expected_subcount=%d, "
                    "digit_min_width=%d, piece_widths=%s) — "
                    "spans now %d (was %d-%d)",
                    widest_w, median_w, expected_subcount,
                    digit_min_width, piece_widths,
                    len(spans_list), s, e,
                )
                # Refresh median to track the post-split distribution.
                new_widths = [se[1] - se[0] for se in spans_list]
                new_widths.sort()
                median_w = max(4, new_widths[len(new_widths) // 2])
                continue
            # Fallback: equal-width pieces would each be too narrow.
            # Do a single legacy 2-way lowest-ink split.
            margin = max(1, widest_w // 10)
            mid_a = s + margin
            mid_b = e - margin
            if mid_b - mid_a < 2:
                break
            sub = proj[mid_a:mid_b]
            split_x = mid_a + int(np.argmin(sub))
            spans_list = (
                spans_list[:widest_idx]
                + [[s, split_x], [split_x, e]]
                + spans_list[widest_idx + 1:]
            )
            grew = True
            log.info(
                "_split_wide_signature_spans: fallback 2-way lowest-ink "
                "split (widest_w=%d, median_w=%d, digit_min_width=%d, "
                "piece_widths=%s) at x=%d (was %d-%d), spans now %d",
                widest_w, median_w, digit_min_width,
                [split_x - s, e - split_x], split_x, s, e,
                len(spans_list),
            )
            new_widths = [se[1] - se[0] for se in spans_list]
            new_widths.sort()
            median_w = max(4, new_widths[len(new_widths) // 2])
            continue
        else:
            wide_factor = 1.7
            if widest_w < int(median_w * wide_factor):
                break
            if widest_w < digit_min_width * 2:
                break
        margin = max(1, widest_w // 10)
        mid_a = s + margin
        mid_b = e - margin
        if mid_b - mid_a < 2:
            break
        sub = proj[mid_a:mid_b]
        split_x = mid_a + int(np.argmin(sub))
        # Reject splits where either piece would be a sliver. The
        # lowest-ink-column snap can land near an edge of the merged
        # span if the digit's stroke distribution is asymmetric (e.g.
        # a "2" with a long bottom horizontal followed by AA dropoff
        # near the right edge of its bbox), producing a 3+24 split
        # where the 3-px sliver is meaningless. Require both halves
        # to be at least ~0.4× the median digit width.
        min_half = max(digit_min_width // 2, int(median_w * 0.4))
        if (split_x - s) < min_half or (e - split_x) < min_half:
            log.info(
                "_split_wide_signature_spans: count-matches outlier "
                "REJECTED split at x=%d (would produce piece widths "
                "%s, both must be ≥%d) — leaving span unsplit",
                split_x, [split_x - s, e - split_x], min_half,
            )
            break
        spans_list = (
            spans_list[:widest_idx]
            + [[s, split_x], [split_x, e]]
            + spans_list[widest_idx + 1:]
        )
        grew = True
        log.info(
            "_split_wide_signature_spans: count-matches outlier split "
            "(widest_w=%d, median_w=%d, digit_min_width=%d, "
            "piece_widths=%s) at x=%d (was %d-%d), spans now %d",
            widest_w, median_w, digit_min_width,
            [split_x - s, e - split_x], split_x, s, e,
            len(spans_list),
        )
        # Refresh median to track the post-split distribution. Without
        # this, repeated splits would keep using the original median
        # and over-split clean digits.
        new_widths = [se[1] - se[0] for se in spans_list]
        new_widths.sort()
        median_w = max(4, new_widths[len(new_widths) // 2])

    if not grew:
        return crops, boxes

    # Re-extract crops + boxes for the new (split) spans. Mirrors the
    # tail of _segment_glyphs so downstream classifiers see the exact
    # same tensor shape / normalisation they expect.
    new_crops: list[np.ndarray] = []
    new_boxes: list[tuple[int, int, int, int]] = []
    for x1, x2 in spans_list:
        x1, x2 = int(x1), int(x2)
        if x2 <= x1:
            continue
        ys = np.where(np.any(binary[:, x1:x2] > 0, axis=1))[0]
        if len(ys) < 1:
            continue
        y1, y2 = int(ys[0]), int(ys[-1] + 1)
        crop = work_canon[y1:y2, x1:x2].astype(np.float32)
        pad = 2
        padded = np.full(
            (crop.shape[0] + pad * 2, crop.shape[1] + pad * 2),
            255.0, dtype=np.float32,
        )
        padded[pad:pad + crop.shape[0], pad:pad + crop.shape[1]] = crop
        pil = Image.fromarray(padded.astype(np.uint8)).resize(
            (28, 28), Image.BILINEAR,
        )
        new_crops.append(np.array(pil, dtype=np.float32) / 255.0)
        new_boxes.append((x1, y1, x2 - x1, y2 - y1))
    if not new_crops:
        return crops, boxes
    return new_crops, new_boxes


def _merge_narrow_signature_spans(
    work_canon: np.ndarray,
    binary: np.ndarray,
    crops: list[np.ndarray],
    boxes: list[tuple[int, int, int, int]],
    expected_count: int,
) -> tuple[list[np.ndarray], list[tuple[int, int, int, int]]]:
    """Merge adjacent narrow spans until count drops to ``expected_count``.

    Mirror image of :func:`_split_wide_signature_spans`: when the
    segmenter produced MORE boxes than the CRNN says there should be,
    a digit was probably over-split (a "0" sliced through its hole, a
    "1" treated as two thin halves, a comma promoted to a span). This
    helper iteratively merges the narrowest adjacent pair until the
    count matches the expected digit count.

    Pairing strategy: walk the (x-sorted) box list, find the adjacent
    pair whose combined width is closest to the median digit width
    (i.e. the pair most consistent with being one digit that got split).
    Merge them into a single span covering ``[min(x), max(x+w))``.

    Aborts safely when:
      * no boxes left to merge,
      * the candidate merge would produce a span >1.8x the median
        width (which suggests we're merging two genuine adjacent digits
        rather than two halves of one).
    """
    if not boxes or expected_count <= 0:
        return crops, boxes
    if len(boxes) <= expected_count:
        return crops, boxes

    # Sort by x position so "adjacent" means visually adjacent.
    # Track original indices so we know which crops to drop.
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    cur_boxes = [boxes[i] for i in order]

    widths = [b[2] for b in cur_boxes]
    sorted_w = sorted(widths)
    median_w = max(4, sorted_w[len(sorted_w) // 2])

    safety = 16
    n_merges = 0
    while safety > 0 and len(cur_boxes) > expected_count:
        safety -= 1
        # Score each adjacent pair: prefer pairs whose combined width
        # is close to median_w (most likely an over-split digit).
        best_idx = -1
        best_score = float("inf")
        for i in range(len(cur_boxes) - 1):
            a = cur_boxes[i]
            b = cur_boxes[i + 1]
            combined_w = (b[0] + b[2]) - a[0]
            # Reject when combined width is way above median (probably
            # two adjacent genuine digits, not one over-split digit).
            if combined_w > median_w * 1.8:
                continue
            score = abs(combined_w - median_w)
            if score < best_score:
                best_score = score
                best_idx = i
        if best_idx < 0:
            log.info(
                "_merge_narrow_signature_spans: no safe merge candidate "
                "found (count=%d expected=%d median_w=%d) — leaving as is",
                len(cur_boxes), expected_count, median_w,
            )
            break
        a = cur_boxes[best_idx]
        b = cur_boxes[best_idx + 1]
        # Build the merged span. y/h come from the union of the two
        # boxes so the merged crop captures the full digit height.
        nx = a[0]
        ny = min(a[1], b[1])
        nx2 = b[0] + b[2]
        ny2 = max(a[1] + a[3], b[1] + b[3])
        cur_boxes = (
            cur_boxes[:best_idx]
            + [(nx, ny, nx2 - nx, ny2 - ny)]
            + cur_boxes[best_idx + 2:]
        )
        n_merges += 1
        log.info(
            "_merge_narrow_signature_spans: merged adjacent pair at "
            "x=[%d,%d] + [%d,%d] -> [%d,%d] (combined_w=%d, median_w=%d), "
            "count now %d / expected %d",
            a[0], a[0] + a[2], b[0], b[0] + b[2],
            nx, nx2, nx2 - nx, median_w, len(cur_boxes), expected_count,
        )

    if n_merges == 0:
        return crops, boxes

    # Rebuild 28x28 crops from the merged boxes against ``work_canon``.
    new_crops: list[np.ndarray] = []
    new_boxes: list[tuple[int, int, int, int]] = []
    for (nx, ny, nw, nh) in cur_boxes:
        nx = max(0, int(nx))
        ny = max(0, int(ny))
        nx2 = min(work_canon.shape[1], int(nx + nw))
        ny2 = min(work_canon.shape[0], int(ny + nh))
        if nx2 <= nx or ny2 <= ny:
            continue
        # Re-tighten y to the merged span's actual ink rows so the
        # crop's height reflects digit ink, not the union of two
        # possibly-misaligned boxes. Matches what
        # :func:`_split_wide_signature_spans` does after splits.
        col_strip = binary[ny:ny2, nx:nx2]
        ys = np.where(np.any(col_strip > 0, axis=1))[0]
        if len(ys) >= 1:
            ny_tight = ny + int(ys[0])
            ny2_tight = ny + int(ys[-1] + 1)
        else:
            ny_tight, ny2_tight = ny, ny2
        crop = work_canon[ny_tight:ny2_tight, nx:nx2].astype(np.float32)
        pad = 2
        padded = np.full(
            (crop.shape[0] + pad * 2, crop.shape[1] + pad * 2),
            255.0, dtype=np.float32,
        )
        padded[pad:pad + crop.shape[0], pad:pad + crop.shape[1]] = crop
        pil = Image.fromarray(padded.astype(np.uint8)).resize(
            (28, 28), Image.BILINEAR,
        )
        new_crops.append(np.array(pil, dtype=np.float32) / 255.0)
        new_boxes.append((nx, ny_tight, nx2 - nx, ny2_tight - ny_tight))
    if not new_crops:
        return crops, boxes
    return new_crops, new_boxes


# ── Box-size dot detector ─────────────────────────────────────────────
# Constants tuned to be conservative: better to miss a dot (CNN handles
# it) than to false-positive a `1` as a `.`. Both axes must be small.
_DOT_W_FRAC = 0.50
_DOT_H_FRAC = 0.70


def _dot_label_from_box(
    boxes: list[tuple[int, int, int, int]],
) -> list[Optional[str]]:
    """Mark glyph boxes that are clearly dots (`.`) by size.

    Returns a list parallel to ``boxes`` whose entries are ``"."`` for
    a confidently-detected dot and ``None`` otherwise (caller should
    use the CNN classification for those positions).

    Heuristic: a `.` source box is typically 3-6 px wide and short,
    while digits are 14-18 px wide and full-height. The 28×28
    normalization downstream of the segmenter strips this size signal
    — every glyph fills the canvas regardless of original size — so
    the CNN tends to misread tiny dots as `0`, `4`, or `7`.

    To be robust against inputs with several dots (e.g. ``"12.34.56"``)
    we estimate digit scale from the *upper half* of widths/heights;
    that median is unaffected by the dots themselves. A box must be
    much narrower AND much shorter than the digit median to count.
    """
    if len(boxes) < 2:
        # Need at least one digit-shaped peer to compare against.
        return [None] * len(boxes)
    widths = [w for (_x, _y, w, _h) in boxes]
    heights = [h for (_x, _y, _w, h) in boxes]
    # Sort descending and take the upper half — biased toward digit
    # scale even when many of the boxes are dots.
    big_widths = sorted(widths, reverse=True)[: max(1, len(widths) // 2)]
    big_heights = sorted(heights, reverse=True)[: max(1, len(heights) // 2)]
    median_w = float(np.median(big_widths))
    median_h = float(np.median(big_heights))
    out: list[Optional[str]] = []
    for (_x, _y, w, h) in boxes:
        if (
            median_w > 0 and median_h > 0
            and w < median_w * _DOT_W_FRAC
            and h < median_h * _DOT_H_FRAC
        ):
            out.append(".")
        else:
            out.append(None)
    return out


def _one_vs_seven_pixel_rule(crop: np.ndarray) -> Optional[str]:
    """Pixel-intensity disambiguator for 1 vs 7 in the SC HUD font.

    The CNN consistently misreads SC's "1" as "7" because both are
    dominantly vertical-stroke glyphs. SC's font has two distinguishing
    features the CNN under-weights:

      * "1" has a bottom horizontal serif (base stroke spanning the
        bottom row of the glyph)
      * "7" has a top horizontal stroke (spanning the top row) plus a
        diagonal to bottom-left, leaving the bottom row mostly empty

    Discriminator:
      * bottom row dense + top-right empty → "1"
      * top-right dense + bottom row empty → "7"
      * otherwise → None (let CNN's softmax stand)

    Polarity-agnostic: detects background by sampling the four corners
    (which are virtually always background regardless of polarity), so
    works on both ``_classify_crops`` (HUD-native polarity) and
    ``_classify_crops_inv`` (inverted-polarity) inputs.
    """
    if crop.ndim != 2 or crop.shape != (28, 28):
        return None
    arr = crop.astype(np.float32)

    # Background value = median of the 4 corner 3×3 patches.
    corners = np.concatenate([
        arr[0:3, 0:3].flatten(),
        arr[0:3, 25:28].flatten(),
        arr[25:28, 0:3].flatten(),
        arr[25:28, 25:28].flatten(),
    ])
    bg = float(np.median(corners))
    val_range = max(abs(bg), abs(255.0 - bg), 1.0)
    # Stroke = pixel deviates from background by > 30% of available range.
    stroke = np.abs(arr - bg) > (0.30 * val_range)

    # Bottom row 22-27: where "1"'s serif lives.
    # Top-right 0-5 × 14-27: where "7"'s horizontal stroke continues
    #   past the area "1"'s flag occupies. Picking the right half
    #   makes the test asymmetric — "1"'s top-LEFT flag stays out of
    #   this region, "7"'s top stroke fills it.
    bottom_density = float(stroke[22:28, :].mean())
    top_right_density = float(stroke[0:6, 14:28].mean())

    if bottom_density > 0.30 and top_right_density < 0.25:
        return "1"
    if top_right_density > 0.45 and bottom_density < 0.20:
        return "7"
    return None


def _apply_one_vs_seven_rule(
    crops: list[np.ndarray],
    results: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Post-process classifier results: override CNN's choice with the
    1-vs-7 pixel rule when the rule disagrees and CNN wasn't certain.

    Only fires when CNN labelled the glyph as "1" or "7" with
    confidence < 0.99. Preserves the CNN's verdict for any other digit
    and for very-confident reads.
    """
    out: list[tuple[str, float]] = []
    for crop, (label, conf) in zip(crops, results):
        if label in ("1", "7") and conf < 0.99:
            rule_label = _one_vs_seven_pixel_rule(crop)
            if rule_label is not None and rule_label != label:
                log.debug(
                    "sc_ocr: 1-vs-7 rule override %r -> %r (cnn_conf=%.2f)",
                    label, rule_label, conf,
                )
                label = rule_label
                # Floor confidence so downstream voting treats this as
                # high-confidence. The rule is rare-fire and conservative
                # — when it does fire, we trust it.
                conf = max(conf, 0.90)
        out.append((label, conf))
    return out


def _one_vs_seven_pixel_rule_signal(crop: np.ndarray) -> Optional[str]:
    """Range-aware 1-vs-7 disambiguator for the signature pipeline.

    The existing :func:`_one_vs_seven_pixel_rule` (used by the HUD
    classifier) has a long-standing bug: it computes
    ``val_range = max(abs(bg), abs(255.0 - bg), 1.0)`` which assumes
    crops are in ``[0, 255]``. The crops fed into ``_classify_crops``
    and ``_classify_crops_signal`` are normalized to ``[0, 1]``, so
    that ``val_range`` ends up at ~254 and the stroke threshold of
    ``0.30 * 254 = 76`` is unreachable for any pixel in [0, 1] —
    meaning the rule never fires on the signal pipeline at all.

    This version detects the input range and computes ``full_range``
    correctly. Same shape-discriminating logic otherwise:

      * "1" has a bottom horizontal serif (dense bottom row) but no
        top-right ink (since the flag is on the LEFT side of the
        glyph)
      * "7" has a top horizontal stroke (dense top-right) and a
        bottom that's mostly empty (the diagonal terminates at the
        bottom-LEFT, leaving the bottom-RIGHT empty)
    """
    if crop.ndim != 2 or crop.shape != (28, 28):
        return None
    arr = crop.astype(np.float32)
    corners = np.concatenate([
        arr[0:3, 0:3].flatten(),
        arr[0:3, 25:28].flatten(),
        arr[25:28, 0:3].flatten(),
        arr[25:28, 25:28].flatten(),
    ])
    bg = float(np.median(corners))
    full_range = 255.0 if arr.max() > 1.5 else 1.0
    val_range = max(abs(bg), abs(full_range - bg), full_range * 0.01)
    stroke = np.abs(arr - bg) > (0.30 * val_range)

    bottom_density = float(stroke[22:28, :].mean())
    top_right_density = float(stroke[0:6, 14:28].mean())

    if bottom_density > 0.30 and top_right_density < 0.25:
        return "1"
    if top_right_density > 0.45 and bottom_density < 0.20:
        return "7"
    return None


def _apply_one_vs_seven_rule_signal(
    crops: list[np.ndarray],
    results: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Apply the range-aware 1-vs-7 rule to signal-pipeline results.

    Same gate as the HUD's :func:`_apply_one_vs_seven_rule`: only
    overrides when CNN labelled the glyph as ``1`` or ``7`` with
    confidence < 0.99 AND the rule disagrees. Floor the confidence
    to 0.90 on override so downstream voting treats it as confident.

    Signature-only because the existing HUD pipeline already has its
    own (broken-but-shipped) 1-vs-7 application; we don't want to
    perturb the HUD by patching the shared helper, so we ship a
    parallel one here.
    """
    out: list[tuple[str, float]] = []
    for crop, (label, conf) in zip(crops, results):
        if label in ("1", "7") and conf < 0.99:
            rule_label = _one_vs_seven_pixel_rule_signal(crop)
            if rule_label is not None and rule_label != label:
                log.info(
                    "sc_ocr.signal: 1-vs-7 rule override %r -> %r "
                    "(cnn_conf=%.2f)", label, rule_label, conf,
                )
                label = rule_label
                conf = max(conf, 0.90)
        out.append((label, conf))
    return out


def _eight_vs_zero_pixel_rule(crop: np.ndarray) -> Optional[str]:
    """Pixel-pattern disambiguator for 8 vs 0/6 in the SC HUD signature font.

    SC's signature font renders these three digits with very similar
    elliptical outlines (oval bounding rim) — the CNN keeps confusing
    them on borderline crops. The interior pixel pattern distinguishes
    them cleanly:

      * ``8`` has a HORIZONTAL WAIST: ink dips to a thin connector
        between two stacked closed loops at the digit's vertical
        midline.
      * ``0`` has a VERTICAL STRUT: a thin column of ink runs the
        full height through the digit's horizontal midline (this is
        the font's "this is a zero, not the letter O" mark).
      * ``6`` has a closed bottom loop + a single tail curving up-
        right from the loop's top — its horizontal midline is
        relatively dense (the tail crosses there) but with no clear
        waist.

    Discriminator: compute the per-row ink count across the digit's
    vertical extent. Look at the middle band's ink vs the top and
    bottom bands' ink. If the middle band is significantly LESS
    dense than either neighbor (a clear waist), the glyph is an 8.
    Otherwise return None (let CNN's softmax stand).

    Polarity-agnostic via 4-corner background sampling, same
    convention as ``_one_vs_seven_pixel_rule``.

    Returns ``"8"`` when the waist is unambiguous; ``None`` otherwise.
    """
    if crop.ndim != 2 or crop.shape != (28, 28):
        return None
    arr = crop.astype(np.float32)

    # Background value from 4 corners. Crops fed to ``_classify_crops``
    # are normalized to [0, 1] (float32 / 255) so we compute the
    # threshold relative to the [0, 1] range — NOT [0, 255]. (The
    # existing _one_vs_seven_pixel_rule has a long-standing typo
    # using ``255.0 - bg`` here that effectively prevents it from
    # ever firing on normalized crops; this rule does it right.)
    corners = np.concatenate([
        arr[0:3, 0:3].flatten(),
        arr[0:3, 25:28].flatten(),
        arr[25:28, 0:3].flatten(),
        arr[25:28, 25:28].flatten(),
    ])
    bg = float(np.median(corners))
    if arr.max() > 1.5:
        # Caller passed [0, 255] — adapt.
        full_range = 255.0
    else:
        full_range = 1.0
    val_range = max(abs(bg), abs(full_range - bg), full_range * 0.01)
    # Lower threshold than the 1-vs-7 rule because the digit's INTERIOR
    # ink we're measuring is dimmer than its perimeter strokes.
    stroke = np.abs(arr - bg) > (0.20 * val_range)

    # Find the digit's vertical extent (rows where ink is present at
    # all). 28×28 includes white padding around the actual glyph; we
    # need to measure ink density relative to the GLYPH BAND, not the
    # full 28-row crop, otherwise padding rows dilute the middle-row
    # measurement.
    row_ink = stroke.sum(axis=1)
    inky_rows = np.where(row_ink >= 2)[0]
    if len(inky_rows) < 12:
        # Glyph too short to reliably detect a waist.
        return None
    glyph_top = int(inky_rows[0])
    glyph_bot = int(inky_rows[-1] + 1)
    glyph_h = glyph_bot - glyph_top
    if glyph_h < 12:
        return None

    # Sample density in three regions of the digit's vertical extent:
    #   * top band  — first 30% of the glyph height (top loop)
    #   * mid band  — central 30% (waist for 8, strut for 0, tail for 6)
    #   * bot band  — last 30% (bottom loop)
    # Note: NOT 5 equal bands. A 5-band split puts every band INSIDE
    # the waist for SC's font where the digit's actual loops occupy
    # the top ~30% and bottom ~30% of the bounding box. Using
    # 30/30/30 with 5% gaps centres each band on the structural
    # feature it's supposed to measure.
    top_band_y1 = glyph_top
    top_band_y2 = glyph_top + max(2, int(round(glyph_h * 0.30)))
    mid_band_y1 = glyph_top + max(2, int(round(glyph_h * 0.35)))
    mid_band_y2 = glyph_top + max(2, int(round(glyph_h * 0.65)))
    bot_band_y1 = glyph_top + max(2, int(round(glyph_h * 0.70)))
    bot_band_y2 = glyph_bot

    if (
        mid_band_y2 <= mid_band_y1
        or top_band_y2 <= top_band_y1
        or bot_band_y2 <= bot_band_y1
    ):
        return None

    top_density = float(stroke[top_band_y1:top_band_y2, :].mean())
    mid_density = float(stroke[mid_band_y1:mid_band_y2, :].mean())
    bot_density = float(stroke[bot_band_y1:bot_band_y2, :].mean())

    # The "waist" rule: middle band density less than 60% of BOTH
    # the top and bottom bands. Necessary BUT not sufficient — a
    # ``0`` with dim rim pixels (just below the binarization
    # threshold) also satisfies this because only the central
    # vertical strut clears the threshold.
    waist_ratio_top = mid_density / max(top_density, 1e-3)
    waist_ratio_bot = mid_density / max(bot_density, 1e-3)
    if not (waist_ratio_top < 0.60 and waist_ratio_bot < 0.60):
        return None
    if top_density < 0.04 or bot_density < 0.04:
        return None

    # ── Anti-rule: detect a "0"'s vertical strut by comparing
    # central-column darkening against side-column darkening in the
    # middle band.
    #
    # Both 8 and 0 have darkening in the mid band, but the SHAPE of
    # the darkening profile is qualitatively different:
    #
    #   * 0: ONE central peak (the vertical strut at the digit's
    #     horizontal centre) PLUS faint shoulders at the rims. The
    #     central peak is ~3-5× stronger than the shoulders.
    #
    #   * 8: TWO peaks at the LEFT and RIGHT (the loop pillars
    #     where the top and bottom loops are connected at the
    #     waist), with a valley in the middle. The side peaks are
    #     stronger than anything in the centre.
    #
    # If the central peak dominates (central_peak > 0.7 × side_peak),
    # it's a 0 with strut → block the 8 override. Otherwise it's an
    # 8 with side pillars → proceed with the override.
    mid_slice = arr[mid_band_y1:mid_band_y2, :]
    if mid_slice.shape[0] < 1:
        return None
    col_mean_intensity = mid_slice.mean(axis=0)
    if bg > 0.5 * full_range:
        col_darkening = bg - col_mean_intensity
    else:
        col_darkening = col_mean_intensity - bg

    top_slice = stroke[top_band_y1:top_band_y2, :]
    top_col_any = top_slice.any(axis=0)
    top_inky = np.where(top_col_any)[0]
    if len(top_inky) >= 4:
        digit_left = int(top_inky[0])
        digit_right = int(top_inky[-1] + 1)
        digit_width = max(1, digit_right - digit_left)
        # Central band: middle 30% of the digit's horizontal extent.
        center_x1 = digit_left + int(round(digit_width * 0.35))
        center_x2 = digit_left + int(round(digit_width * 0.65))
        # Side bands: leftmost 25% and rightmost 25%.
        left_x1 = digit_left
        left_x2 = digit_left + int(round(digit_width * 0.25))
        right_x1 = digit_left + int(round(digit_width * 0.75))
        right_x2 = digit_right
        if (
            center_x2 > center_x1
            and left_x2 > left_x1
            and right_x2 > right_x1
        ):
            central_peak = float(col_darkening[center_x1:center_x2].max())
            left_peak = float(col_darkening[left_x1:left_x2].max())
            right_peak = float(col_darkening[right_x1:right_x2].max())
            side_peak = max(left_peak, right_peak)
            # Central strut detection: central peak comparable to or
            # stronger than side peaks → it's a 0 (or anything with
            # a central vertical mark — but 0 is the only digit in
            # our 11-class set with that property in the SC HUD font).
            if central_peak > side_peak * 0.7:
                log.debug(
                    "_eight_vs_zero_pixel_rule: ANTI-RULE engaged "
                    "(central_peak=%.3f side_peak=%.3f, "
                    "ratio=%.2f) — looks like a 0 with vertical "
                    "strut, NOT an 8",
                    central_peak, side_peak,
                    central_peak / max(side_peak, 1e-3),
                )
                return None

    log.debug(
        "_eight_vs_zero_pixel_rule: WAIST detected "
        "(top=%.2f mid=%.2f bot=%.2f, ratios %.2f/%.2f) -> '8'",
        top_density, mid_density, bot_density,
        waist_ratio_top, waist_ratio_bot,
    )
    return "8"


def _apply_eight_vs_zero_rule(
    crops: list[np.ndarray],
    results: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Post-process classifier results: override CNN's choice with
    the 8-vs-0 pixel rule when the CNN said ``0``/``6`` at borderline
    confidence but the glyph clearly has an 8's waist.

    Fires only when:
      * CNN's predicted label is ``0`` or ``6``,
      * CNN's confidence is below 0.99,
      * the pixel rule positively identifies the glyph as ``8``.

    Conservative: never overrides ``8 → 0/6`` because the CNN's
    positive-8 predictions are already trusted; only flips the
    common false-negative direction.
    """
    out: list[tuple[str, float]] = []
    for crop, (label, conf) in zip(crops, results):
        if label in ("0", "6") and conf < 0.99:
            rule_label = _eight_vs_zero_pixel_rule(crop)
            if rule_label == "8":
                log.info(
                    "sc_ocr: 8-vs-0 rule override %r -> '8' "
                    "(cnn_conf=%.2f)", label, conf,
                )
                label = "8"
                conf = max(conf, 0.90)
        out.append((label, conf))
    return out


def _apply_zero_six_eight_secondary_tiebreak(
    primary_results: list[tuple[str, float]],
    secondary_results: list[tuple[str, float]],
    *,
    sec_conf_floor: float = 0.85,
) -> list[tuple[str, float]]:
    """Resolve primary/secondary disagreements within ``{0, 6, 8}`` by
    deferring to the secondary classifier.

    Scoped to the rounded-rectangle digit set ``{0, 6, 8}`` because
    that's where the chromatic-aberration / mid-band-shadow artifacts
    on SC's signal panel most reliably split the two classifiers'
    reads. Outside that set (e.g. primary ``1`` vs secondary ``7``),
    the rule does NOT fire — the two classifiers don't have a
    structural advantage on cleanly-distinct shapes, so a tiebreak
    there would just amplify whichever happens to be louder on a
    given frame.

    Override conditions (all must hold):
      * primary[i].label != secondary[i].label
      * primary[i].label ∈ {"0", "6", "8"}
      * secondary[i].label ∈ {"0", "6", "8"}
      * secondary[i].conf >= ``sec_conf_floor``

    When all conditions hold, the override replaces the i-th primary
    result with the i-th secondary result. The function returns a NEW
    list and does not mutate its inputs (so the secondary's separate
    list is unchanged for any downstream dual-vote / mean-confidence
    evaluations that need to inspect both opinions independently).
    """
    AMBIGUOUS = {"0", "6", "8"}
    if (
        not primary_results
        or not secondary_results
        or len(primary_results) != len(secondary_results)
    ):
        return primary_results
    out: list[tuple[str, float]] = []
    n_overridden = 0
    for i, ((p_label, p_conf), (s_label, s_conf)) in enumerate(
        zip(primary_results, secondary_results)
    ):
        if (
            p_label != s_label
            and p_label in AMBIGUOUS
            and s_label in AMBIGUOUS
            and s_conf >= sec_conf_floor
        ):
            log.info(
                "sc_ocr.signal: 0/6/8 secondary-tiebreak overriding "
                "primary glyph[%d] %r (conf=%.2f) → secondary %r "
                "(conf=%.2f)",
                i, p_label, p_conf, s_label, s_conf,
            )
            out.append((s_label, s_conf))
            n_overridden += 1
        else:
            out.append((p_label, p_conf))
    if n_overridden:
        log.info(
            "sc_ocr.signal: 0/6/8 secondary-tiebreak fired for %d/%d "
            "glyph(s)", n_overridden, len(primary_results),
        )
    return out


# ──────────────────────────────────────────────────────────────────────
# N-way digit-position consensus across the four signal CNNs
# ──────────────────────────────────────────────────────────────────────

def _vote_on_digit_position(
    voter_results: dict[str, tuple[str, float] | None],
) -> dict:
    """Cast an N-way vote on a SINGLE digit position.

    ``voter_results`` is a dict mapping voter name (``"primary"``,
    ``"secondary"``, ``"rgb"``, ``"rgb_inv"``) to the per-glyph
    classification at this position — either ``(char, conf)`` or
    ``None`` (voter not available / abstained).

    Consensus rules (in priority order):

      1. **All voters agree** on a single class → that class with
         confidence = mean of contributing votes.
      2. **3-of-N agree** (with at least 3 voters present) → that
         class with confidence = mean of the 3 agreeing votes.
      3. **2-of-N agree AND the agreeing pair includes one gray voter
         and one RGB voter (decorrelated)** → that class with
         confidence = mean of the agreeing pair.
      4. **2-of-N agree on the same polarity tier** (e.g. both gray)
         → flagged with ``needs_lexicon_disambiguation=True`` so the
         caller can decide string-level via the lexicon.
      5. **Otherwise** — sum confidence per candidate class across
         all voters, return the argmax.

    The returned dict carries:

      * ``char`` — consensus character (single digit, ``"@"`` for
        icon, or ``"?"`` if every voter abstained).
      * ``confidence`` — aggregated confidence in [0, 1].
      * ``votes`` — passthrough of input ``voter_results`` for
        downstream debug / lexicon reasoning.
      * ``consensus_path`` — short label describing which rule fired.
      * ``needs_lexicon_disambiguation`` — only ``True`` for path 4.
    """
    # Normalize: drop voters that abstained (``None``) and split into
    # per-voter (char, conf) tuples we can iterate.
    active: list[tuple[str, str, float]] = []  # (voter, char, conf)
    for vname in ("primary", "secondary", "rgb", "rgb_inv"):
        r = voter_results.get(vname)
        if r is None:
            continue
        ch, conf = r
        active.append((vname, ch, float(conf)))
    if not active:
        return {
            "char": "?",
            "confidence": 0.0,
            "votes": dict(voter_results),
            "consensus_path": "all_abstain",
            "needs_lexicon_disambiguation": False,
        }

    # Per-class vote counts and confidence sums.
    class_votes: dict[str, list[tuple[str, float]]] = {}
    for vname, ch, conf in active:
        class_votes.setdefault(ch, []).append((vname, conf))

    n_active = len(active)

    # Path 1: all active voters agree.
    if len(class_votes) == 1:
        ch = next(iter(class_votes))
        confs = [c for _, c in class_votes[ch]]
        return {
            "char": ch,
            "confidence": sum(confs) / len(confs),
            "votes": dict(voter_results),
            "consensus_path": f"all_{n_active}_agree",
            "needs_lexicon_disambiguation": False,
        }

    # Find the largest agreeing block.
    best_ch, best_voters = max(
        class_votes.items(), key=lambda kv: len(kv[1]),
    )
    best_size = len(best_voters)

    # Path 2: 3 (or more) voters agree.
    if best_size >= 3 and n_active >= 3:
        confs = [c for _, c in best_voters]
        return {
            "char": best_ch,
            "confidence": sum(confs) / len(confs),
            "votes": dict(voter_results),
            "consensus_path": f"{best_size}_of_{n_active}_agree",
            "needs_lexicon_disambiguation": False,
        }

    # Path 3 / 4: 2 voters agree on a class. Path-3 fires when the
    # agreeing pair spans the gray/RGB tier (decorrelated). Path-4
    # fires when both agreeing voters live in the same tier (only
    # gray↔gray or rgb↔rgb).
    GRAY_VOTERS = {"primary", "secondary"}
    RGB_VOTERS = {"rgb", "rgb_inv"}
    if best_size == 2:
        agreeing_voters = {v for v, _ in best_voters}
        is_gray = bool(agreeing_voters & GRAY_VOTERS)
        is_rgb = bool(agreeing_voters & RGB_VOTERS)
        confs = [c for _, c in best_voters]
        if is_gray and is_rgb:
            # Decorrelated pair — strong evidence.
            return {
                "char": best_ch,
                "confidence": sum(confs) / len(confs),
                "votes": dict(voter_results),
                "consensus_path": f"2_of_{n_active}_decorrelated",
                "needs_lexicon_disambiguation": False,
            }
        # Same-tier pair — weaker. Still pick this class but flag
        # so the string-level voter can fall back to the lexicon.
        return {
            "char": best_ch,
            "confidence": sum(confs) / len(confs),
            "votes": dict(voter_results),
            "consensus_path": f"2_of_{n_active}_same_tier",
            "needs_lexicon_disambiguation": True,
        }

    # Path 5: no class has more than one vote. Sum confidence per
    # class across all voters and pick argmax. The summed-confidence
    # rule resists single-voter overconfidence on a wrong class.
    class_score: dict[str, float] = {}
    for ch, votes in class_votes.items():
        class_score[ch] = sum(c for _, c in votes)
    pick_ch = max(class_score.items(), key=lambda kv: kv[1])[0]
    pick_conf = class_score[pick_ch]
    # Normalize to [0, 1]: the pick took at most n_active voters' worth
    # of conf mass, so divide by n_active to keep the output bounded.
    return {
        "char": pick_ch,
        "confidence": min(1.0, pick_conf / max(1, n_active)),
        "votes": dict(voter_results),
        "consensus_path": "argmax_summed_conf",
        "needs_lexicon_disambiguation": True,
    }


def _vote_on_digit_string(
    primary_results: list[tuple[str, float]] | None,
    secondary_results: list[tuple[str, float]] | None,
    rgb_results: list[tuple[str, float]] | None,
    rgb_inv_results: list[tuple[str, float]] | None,
    *,
    lexicon: set[int] | None = None,
) -> dict:
    """N-way consensus on a sequence of digit-position votes from
    1–4 signal CNNs.

    Every voter's results list is per-position aligned (indexed 0..N
    where N is the segmented-digit count). When a voter is missing
    (``None`` or empty), it abstains on every position.

    For each position, runs :func:`_vote_on_digit_position`. Then
    composes the per-position chars into a string. When at least one
    position flagged ``needs_lexicon_disambiguation=True`` AND the
    composed string isn't in the lexicon, this function tries each
    voter's full string and picks the one that IS in the lexicon —
    on the rationale that a 2-vs-2 same-tier split is less reliable
    than a full-string lexicon hit by another voter.

    Returns:
      dict with
        ``string`` — composed digit string (only ``isdigit()`` chars
                    — ``@`` is dropped).
        ``mean_confidence`` — mean of per-position confidences for
                              the digit-positions actually used.
        ``per_position`` — list of per-position dicts from
                          :func:`_vote_on_digit_position`.
        ``consensus_path`` — short summary, e.g. ``"all_4_agree"`` or
                            ``"lexicon_override:rgb"``.
        ``available_voters`` — count of non-abstaining voters present
                              for at least one position.
    """
    # Determine the position count. All present voters must agree on
    # length; if they don't, we trust the longest. The classifiers'
    # behaviour is to either return one entry per crop or [], so a
    # length disagreement only happens when the icon-drop pruned
    # different counts (which the segmenter callers prevent in the
    # current code). Defensive code path nonetheless.
    voters: dict[str, list[tuple[str, float]] | None] = {
        "primary": primary_results,
        "secondary": secondary_results,
        "rgb": rgb_results,
        "rgb_inv": rgb_inv_results,
    }
    available = [
        name for name, r in voters.items() if r and len(r) > 0
    ]
    if not available:
        return {
            "string": "",
            "mean_confidence": 0.0,
            "per_position": [],
            "consensus_path": "no_voters",
            "available_voters": 0,
        }

    # Use the most-voted-on length: it's the segmenter's decision the
    # gates already rely on, and any voter shorter than that abstains
    # on the missing tail.
    lengths = [len(voters[v]) for v in available]
    n_pos = max(lengths)

    per_position: list[dict] = []
    for i in range(n_pos):
        pos_votes: dict[str, tuple[str, float] | None] = {}
        for name in ("primary", "secondary", "rgb", "rgb_inv"):
            r = voters[name]
            if r is None or i >= len(r):
                pos_votes[name] = None
            else:
                pos_votes[name] = r[i]
        per_position.append(_vote_on_digit_position(pos_votes))

    # Compose the consensus string (digits only — drop @ and ?).
    composed_chars = []
    composed_confs = []
    needs_lex = False
    for p in per_position:
        ch = p["char"]
        if ch.isdigit():
            composed_chars.append(ch)
            composed_confs.append(float(p["confidence"]))
        # Non-digit characters (icon @ or ?) are dropped — that's the
        # same convention the existing gates use after the icon-class
        # drop pass.
        if p.get("needs_lexicon_disambiguation"):
            needs_lex = True

    composed = "".join(composed_chars)
    mean_conf = (
        sum(composed_confs) / len(composed_confs)
        if composed_confs else 0.0
    )

    # Roll up the per-position consensus paths for diag output.
    paths_summary = "+".join(
        p["consensus_path"] for p in per_position
    )

    out = {
        "string": composed,
        "mean_confidence": mean_conf,
        "per_position": per_position,
        "consensus_path": paths_summary,
        "available_voters": len(available),
    }

    # Lexicon tiebreak: when the composed string isn't in the lexicon
    # and at least one position needed disambiguation, try every
    # voter's full string and prefer the first one that IS in the
    # lexicon.
    if (
        lexicon
        and needs_lex
        and composed
        and len(composed) >= 4
    ):
        try:
            composed_int = int(composed)
        except ValueError:
            composed_int = None
        if composed_int is None or composed_int not in lexicon:
            for name in ("rgb", "rgb_inv", "primary", "secondary"):
                r = voters[name]
                if not r:
                    continue
                cand = "".join(c for c, _ in r if c.isdigit())
                if not cand:
                    continue
                try:
                    cand_int = int(cand)
                except ValueError:
                    continue
                if cand_int in lexicon:
                    cand_confs = [
                        float(c) for ch, c in r if ch.isdigit()
                    ]
                    out["string"] = cand
                    out["mean_confidence"] = (
                        sum(cand_confs) / len(cand_confs)
                        if cand_confs else 0.0
                    )
                    out["consensus_path"] = (
                        f"lexicon_override:{name}({paths_summary})"
                    )
                    log.info(
                        "sc_ocr.signal: voter consensus composed=%r "
                        "(not in lexicon) — overriding to %r from "
                        "%s voter via lexicon match",
                        composed, cand, name,
                    )
                    break

    return out


def _classify_crops(crops: list[np.ndarray]) -> list[tuple[str, float]]:
    """Batch-classify 28x28 crops via the ONNX CNN."""
    if not crops or not fallback._ensure_model():
        return []
    session = fallback._session
    char_classes = fallback._char_classes
    inp_name = session.get_inputs()[0].name
    batch = np.array(crops, dtype=np.float32).reshape(-1, 1, 28, 28)
    try:
        logits = session.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug("sc_ocr: ONNX inference failed: %s", exc)
        return []
    results = []
    for i in range(len(crops)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        idx = int(np.argmax(probs))
        results.append((char_classes[idx], float(probs[idx])))
    # Apply the 1-vs-7 pixel rule (SC HUD font specific) before
    # returning. No-op for any glyph the CNN didn't classify as "1"/"7".
    return _apply_one_vs_seven_rule(crops, results)


def _classify_crops_topk(
    crops: list[np.ndarray],
    k: int = 2,
) -> list[list[tuple[str, float]]]:
    """Top-K variant of :func:`_classify_crops`.

    Returns ``[[(top1_char, top1_conf), (top2_char, top2_conf), ...], ...]``
    — one inner list per input crop, each holding the top ``k`` softmax
    classes ordered by descending probability. Mirror of
    :func:`_classify_crops_signal_topk` but uses the HUD per-glyph CNN
    session (``fallback._session``) instead of the signal session.

    Used by the ``.`` ↔ digit backtrack logic in :func:`_ocr_value_crop`:
    when the CNN's top-1 says ``.`` at a position whose source bbox is
    too large to be a real dot AND the field grammar forbids ``.`` at
    that position, we swap to top-2 to recover the most-likely digit.

    The 1-vs-7 pixel rule is applied to top-1 only — top-2/3 are
    emitted raw from the softmax. Returns an empty list when the
    model isn't available; callers detect that and fall back to top-1.
    """
    if not crops or not fallback._ensure_model():
        return []
    session = fallback._session
    char_classes = fallback._char_classes
    inp_name = session.get_inputs()[0].name
    batch = np.array(crops, dtype=np.float32).reshape(-1, 1, 28, 28)
    try:
        logits = session.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug("sc_ocr: ONNX top-K inference failed: %s", exc)
        return []
    n_classes = len(char_classes)
    k_eff = max(1, min(k, n_classes))
    results: list[list[tuple[str, float]]] = []
    for i in range(len(crops)):
        row = logits[i]
        probs = np.exp(row - np.max(row))
        probs /= probs.sum()
        if k_eff < n_classes:
            top_idx = np.argpartition(probs, -k_eff)[-k_eff:]
        else:
            top_idx = np.arange(n_classes)
        top_idx = top_idx[np.argsort(-probs[top_idx])]
        per_crop: list[tuple[str, float]] = []
        for ci in top_idx:
            cidx = int(ci)
            if 0 <= cidx < n_classes:
                per_crop.append((char_classes[cidx], float(probs[cidx])))
        if not per_crop:
            per_crop = [("?", 0.0)]
        results.append(per_crop)
    # 1-vs-7 rule only on top-1 (the rule disambiguates a single
    # vertical-stroke pair; top-2/3 stay raw to give backtrackers
    # the full softmax view).
    top1_pairs: list[tuple[str, float]] = [
        r[0] if r else ("?", 0.0) for r in results
    ]
    top1_pairs = _apply_one_vs_seven_rule(crops, top1_pairs)
    for i, p in enumerate(top1_pairs):
        if results[i]:
            results[i][0] = p
    return results


# ──────────────────────────────────────────────────────────────────────
# Per-CNN polarity routing for the four signal classifiers
# ──────────────────────────────────────────────────────────────────────
# The runtime canonicalizes panel crops to BRIGHT-text-on-DARK-bg via
# ``_canonicalize_polarity`` (text pixels ≈ 1.0, bg pixels ≈ 0.0 in the
# float32 [0, 1] domain). But the four signal CNN models were trained
# on different polarities — verified by sampling their staging dirs:
#
#   training_data_user_sig/0..9 :   median ≈ 190 → DARK-on-LIGHT source
#   training_data_user_sig_rgb/0..9 : median ≈ 200 → DARK-on-LIGHT source
#
# And combining with the trainer behaviour
# (``scripts/train_for_region.py:_load_dataset`` flips ``1.0 - x`` for
# any kind whose name contains ``inv``):
#
#   PRIMARY  (signal)        : trainer NO-flip  → expects DARK-on-LIGHT
#   SECONDARY(signal_inv)    : trainer flips    → expects BRIGHT-on-DARK
#   SIGNAL_RGB(signal_rgb_v2): trainer NO-flip  → expects DARK-on-LIGHT
#   SIGNAL_RGB_INV(rgb_inv)  : trainer flips    → expects BRIGHT-on-DARK
#
# So the runtime (which produces canonical BRIGHT-on-DARK crops) must
# INVERT before feeding PRIMARY and SIGNAL_RGB, and feed AS-IS to
# SECONDARY and SIGNAL_RGB_INV.
#
# Before this routing was in place, PRIMARY (and SIGNAL_RGB) were being
# fed the polarity OPPOSITE to what they were trained on, which caused
# digit misreads on every signature capture in the test suite.
_CNN_POLARITY: dict[str, str] = {
    "primary":  "dark_on_light",   # model_signal_cnn.onnx — invert canonical
    "secondary": "bright_on_dark", # model_signal_inv_cnn.onnx — feed canonical
    "rgb":      "dark_on_light",   # model_signal_rgb_cnn_v2.onnx — invert canonical
    "rgb_inv":  "bright_on_dark",  # model_signal_rgb_inv_cnn.onnx — feed canonical
}


# ──────────────────────────────────────────────────────────────────────
# Tight crop + repad: distribution-match the CNN input to training data
# ──────────────────────────────────────────────────────────────────────
# The proportional segmenter produces per-digit bboxes that span the
# FULL row height of the value crop and have proportional widths. When
# resized to 28×28 by the simple pad+bilinear in the segmenter's
# ``_crop_to_28x28``, narrow digits (like ``1`` at ~5×24 ink in a
# 14×44 slot) get HORIZONTALLY STRETCHED 2× and VERTICALLY SHRUNK by
# 0.6×. The result is a 28×28 with the digit ink occupying roughly
# the FULL canvas — wildly out of distribution.
#
# Training samples were extracted from real captures with a frame of
# pure padding around the actual ink. Empirical calibration over 100
# samples (10 per class × 10 classes):
#
#   training_data_user_sig (gray)     : larger_dim_med = 0.786 → ~22/28
#   training_data_user_sig_rgb (RGB)  : larger_dim_med = 0.857 → ~24/28
#
# So every gray training sample has the ink's larger bbox dimension
# occupying ~22 of the 28 pixels, with ~3 px of bg padding each side.
# RGB training samples are slightly tighter at 24/28.
#
# The fix: BEFORE handing a crop to the CNN, find the actual ink bbox
# inside the 28×28 canvas, scale it so its larger dimension equals
# ``target_inner`` (22 for gray, 24 for RGB), and re-place it centered
# on a fresh 28×28 canvas with the bg color filling the rest. That
# matches training distribution → CNN classification works.
#
# The helpers below run BEFORE ``_feed_signal_cnn``; the polarity
# routing helper still inverts/passes-through afterwards. Ordering:
# canonical-loose → tight-repad → polarity-route → CNN.
_TIGHT_TARGET_INNER_GRAY = 22
_TIGHT_TARGET_INNER_RGB = 24


def _tight_repad_glyph(
    crop_canonical: np.ndarray,
    *,
    skip_padding_ring: bool = True,
) -> np.ndarray:
    """Tighten a loose canonical glyph crop and re-pad to 28×28.

    Args:
        crop_canonical: grayscale crop, ANY size or dtype, with
            CANONICAL bright-on-dark polarity (text bright, bg dark).
            Input may be float [0, 1] or uint8 [0, 255].
        skip_padding_ring: if True and input is 28×28, ignore the
            outer 2-px ring during ink detection (those pixels are
            from the segmenter's white-pad-then-resize tail and would
            be misclassified as ink). Default True. Pass False when
            the input is the loose canonical sub (no padding ring).

    Returns:
        28×28 array, same dtype/range as input. Output structure
        mimics training samples' three-tier composition:

          Outer 2-px ring  : padding bright extreme (255 for canon)
          Middle annulus   : pill-bg mid-tier (sampled from input's
                             non-ink, non-chrome pixels)
          Inner            : digit ink centered, larger dim = 22 px

        Caller hands the result to :func:`_feed_signal_cnn` for
        per-model polarity routing.

    Empirical calibration (n=100, 10 samples per digit class 0..9 from
    ``training_data_user_sig``): larger ink-bbox dim ratio = 0.786 →
    target_inner = 22 of 28.

    Why three-tier output: training samples were extracted from real
    captures with the panel-pill bg embedded in the 28×28 (median ~200
    for gray). A two-tier output (pure ink + pure padding) lacks the
    mid-tier the CNN was trained to recognize as "digit lives inside
    a frame." Empirically: synthetic two-tier inputs misclassify as
    different digits than three-tier inputs of the same shape.
    """
    if crop_canonical is None or crop_canonical.size == 0:
        return np.zeros((28, 28), dtype=np.float32)

    src = crop_canonical
    if src.dtype != np.uint8:
        src_u8 = np.clip(src.astype(np.float32) * 255.0, 0, 255).astype(np.uint8)
        out_dtype = src.dtype
        out_scale = 1.0 / 255.0
    else:
        src_u8 = src
        out_dtype = np.uint8
        out_scale = 1.0

    h, w = src_u8.shape

    interior_geom = np.ones_like(src_u8, dtype=bool)
    if skip_padding_ring and h == 28 and w == 28:
        interior_geom[:2, :] = False
        interior_geom[-2:, :] = False
        interior_geom[:, :2] = False
        interior_geom[:, -2:] = False

    interior = src_u8[interior_geom]
    if interior.size == 0:
        canvas = np.zeros((28, 28), dtype=np.uint8)
        if out_dtype != np.uint8:
            return canvas.astype(out_dtype) * out_scale
        return canvas

    # ── Detect digit ink (the bright digit cluster) ──
    # Canonical bright-on-dark structure: digit is BRIGHTEST.
    if skip_padding_ring and h == 28 and w == 28:
        ink_pct = 85.0
    else:
        ink_pct = 78.0
    thr = max(
        min(254, int(np.percentile(interior, ink_pct))),
        int(np.median(interior)) + 6,
    )
    ink_mask = (src_u8 >= thr) & interior_geom

    if int(ink_mask.sum()) < 5:
        # No detectable ink — return blank training-like canvas.
        canvas = np.full((28, 28), 255, dtype=np.uint8)
        if out_dtype != np.uint8:
            return canvas.astype(out_dtype) * out_scale
        return canvas

    # Tight bbox of the ink, with a small halo to capture the
    # gradient transition that training samples exhibit.
    rows = np.any(ink_mask, axis=1)
    cols = np.any(ink_mask, axis=0)
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]

    halo = 1  # 1-px halo so resize bilinear gets proper edge gradient
    rmin_h = max(0, rmin - halo)
    rmax_h = min(h - 1, rmax + halo)
    cmin_h = max(0, cmin - halo)
    cmax_h = min(w - 1, cmax + halo)

    tight = src_u8[rmin_h:rmax_h + 1, cmin_h:cmax_h + 1]
    th, tw = tight.shape

    target_inner = _TIGHT_TARGET_INNER_GRAY
    scale = target_inner / max(th, tw)
    new_h = max(1, min(target_inner, int(round(th * scale))))
    new_w = max(1, min(target_inner, int(round(tw * scale))))
    try:
        tight_resized = np.asarray(
            Image.fromarray(tight, mode="L").resize(
                (new_w, new_h), Image.BILINEAR,
            ),
            dtype=np.uint8,
        )
    except Exception:
        canvas = np.full((28, 28), 255, dtype=np.uint8)
        if out_dtype != np.uint8:
            return canvas.astype(out_dtype) * out_scale
        return canvas

    # Build TRAINING-DISTRIBUTION OUTPUT in dark-on-light:
    #   Outer area : 255 (pure white padding)
    #   Inner      : INVERTED tight-resized pixels (training has
    #                dark digit on bright bg; canonical input has
    #                bright digit on dark bg → invert).
    canvas = np.full((28, 28), 255, dtype=np.uint8)

    y0 = (28 - new_h) // 2
    x0 = (28 - new_w) // 2
    # Invert: canonical bright (~250) ink → dark (~5) digit; canonical
    # dark (~0) bg → bright (~255). This preserves the gradient
    # structure from the source.
    inverted = (255 - tight_resized.astype(np.int16)).clip(0, 255).astype(np.uint8)
    canvas[y0:y0 + new_h, x0:x0 + new_w] = inverted

    if out_dtype != np.uint8:
        return canvas.astype(out_dtype) * out_scale
    return canvas


def _aspect_pad_resize_28(padded: np.ndarray, bg: int = 255) -> np.ndarray:
    """Aspect-preserving alternative to ``resize((28, 28))`` for glyphs.

    The OLD-STYLE crop tail (pad the digit bbox with a 2-px ``bg`` ring,
    then ``PIL.resize((28, 28))``) STRETCHES the box to a square. A thin
    digit like ``1`` (≈6×24 ink) becomes ≈3× too wide — the "smushed 1"
    the per-glyph viewer shows — which is wildly out of the training
    distribution (where ``1`` is a thin, centered stroke with padding on
    both sides). This helper instead scales the already-padded crop
    UNIFORMLY so its larger dimension is 28, then centers it on a 28×28
    canvas filled with the SAME ``bg`` the ring already uses.

    Polarity-safe by construction: identical input array, identical bg
    value, identical dtype path as the stretch — the ONLY difference is
    the smaller dimension gets ``bg`` padding instead of being stretched.
    So clean glyphs (already near-square) are essentially unchanged,
    while thin/odd-aspect glyphs land in-distribution. No ink detection,
    so no over-crop regression (the failure mode that got
    ``_tight_repad_glyph`` disabled).

    Args:
        padded: HxW (gray) or HxWx3 (RGB) uint8/float crop, already
            ``bg``-padded by the caller.
        bg: background fill value (255 = the white pad the OLD tail uses).

    Returns:
        28×28 (gray) or 28×28×3 (RGB) uint8 array.
    """
    if padded is None or padded.size == 0:
        is_rgb0 = padded is not None and padded.ndim == 3
        shape0 = (28, 28, 3) if is_rgb0 else (28, 28)
        return np.full(shape0, bg, dtype=np.uint8)
    h, w = padded.shape[:2]
    is_rgb = padded.ndim == 3
    m = max(int(h), int(w))
    if m <= 0:
        shape0 = (28, 28, 3) if is_rgb else (28, 28)
        return np.full(shape0, bg, dtype=np.uint8)
    scale = 28.0 / float(m)
    nh = max(1, min(28, int(round(h * scale))))
    nw = max(1, min(28, int(round(w * scale))))
    try:
        pil = Image.fromarray(
            padded.astype(np.uint8), mode="RGB" if is_rgb else "L",
        ).resize((nw, nh), Image.BILINEAR)
        small = np.asarray(pil, dtype=np.uint8)
    except Exception:
        shape0 = (28, 28, 3) if is_rgb else (28, 28)
        return np.full(shape0, bg, dtype=np.uint8)
    canvas = np.full(
        (28, 28, 3) if is_rgb else (28, 28), bg, dtype=np.uint8,
    )
    y0 = (28 - nh) // 2
    x0 = (28 - nw) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = small
    return canvas


def _tight_repad_glyph_rgb(
    crop_canonical: np.ndarray,
    *,
    skip_padding_ring: bool = True,
) -> np.ndarray:
    """Tighten a loose canonical RGB glyph crop and re-pad to 28×28×3.

    Same algorithm as :func:`_tight_repad_glyph` but RGB. Ink detection
    uses the per-pixel max-channel projection as a luminance proxy.
    Output structure matches training (three-tier with bright outer
    pad, mid pill bg, dark digit) AFTER the polarity routing in
    :func:`_feed_signal_cnn`.

    Empirical calibration (training_data_user_sig_rgb): target_inner = 24.
    """
    if crop_canonical is None or crop_canonical.size == 0:
        return np.full((28, 28, 3), 255, dtype=np.uint8)
    src = crop_canonical
    if src.dtype != np.uint8:
        src = np.clip(src.astype(np.float32), 0, 255).astype(np.uint8)
    if src.ndim == 2:
        src = np.stack([src, src, src], axis=-1)
    if src.shape[-1] != 3:
        return np.full((28, 28, 3), 255, dtype=np.uint8)

    h, w = src.shape[:2]
    gray = src.max(axis=2).astype(np.uint8)

    interior_geom = np.ones((h, w), dtype=bool)
    if skip_padding_ring and h == 28 and w == 28:
        interior_geom[:2, :] = False
        interior_geom[-2:, :] = False
        interior_geom[:, :2] = False
        interior_geom[:, -2:] = False

    interior_gray = gray[interior_geom]
    if interior_gray.size == 0:
        return np.full((28, 28, 3), 255, dtype=np.uint8)

    if skip_padding_ring and h == 28 and w == 28:
        ink_pct = 85.0
    else:
        ink_pct = 78.0
    thr = max(
        min(254, int(np.percentile(interior_gray, ink_pct))),
        int(np.median(interior_gray)) + 6,
    )
    ink_mask = (gray >= thr) & interior_geom

    # Sample the panel pill color from non-ink, non-chrome interior.
    non_ink_interior = (~ink_mask) & interior_geom
    if int(non_ink_interior.sum()) > 0:
        non_ink_gray = gray[non_ink_interior]
        if non_ink_gray.size > 0:
            chrome_thr = float(np.percentile(non_ink_gray, 20))
            mid_mask = non_ink_interior & (gray > chrome_thr)
            if int(mid_mask.sum()) > 0:
                pill_bg_color_rgb = np.median(
                    src[mid_mask], axis=0,
                ).astype(np.uint8)
            else:
                pill_bg_color_rgb = np.median(
                    src[non_ink_interior], axis=0,
                ).astype(np.uint8)
        else:
            pill_bg_color_rgb = np.array([55, 55, 55], dtype=np.uint8)
    else:
        pill_bg_color_rgb = np.array([55, 55, 55], dtype=np.uint8)

    # Clamp to a plausible bright mid-tier per channel.
    pill_max = int(pill_bg_color_rgb.max())
    if pill_max < 80 or pill_max > 240:
        pill_bg_color_rgb = np.array([55, 55, 55], dtype=np.uint8)

    # For a CANONICAL bright-on-dark RGB input, after _feed_signal_cnn
    # inverts (for "rgb" target which expects dark-on-light), the
    # output should have:
    #   Outer ring : 255 (pure white) — restored after invert
    #   Mid annulus: pill_bg_color_rgb (training pill bg) → invert to
    #                255 - pill_bg → so output here = 255 - pill
    #   Digit core : training-dark-digit (~55 RGB) → invert to ~200
    #                → so canonical output has ink at ~200 (mid-bright)
    #
    # We keep the canonical convention (bright outer, dark mid-annulus,
    # mid-bright digit) so the routing's invert lands correctly.
    canon_mid_tier = (255 - pill_bg_color_rgb.astype(np.int16)).clip(0, 255).astype(np.uint8)

    if int(ink_mask.sum()) < 5:
        # No ink detected — return a solid mid-tier with bright outer.
        canvas = np.broadcast_to(canon_mid_tier, (28, 28, 3)).copy()
        canvas[:2, :, :] = 255
        canvas[-2:, :, :] = 255
        canvas[:, :2, :] = 255
        canvas[:, -2:, :] = 255
        return canvas

    rows = np.any(ink_mask, axis=1)
    cols = np.any(ink_mask, axis=0)
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    tight = src[rmin:rmax + 1, cmin:cmax + 1, :]
    th, tw = tight.shape[:2]

    target_inner = _TIGHT_TARGET_INNER_RGB
    scale = target_inner / max(th, tw)
    new_h = max(1, min(target_inner, int(round(th * scale))))
    new_w = max(1, min(target_inner, int(round(tw * scale))))
    try:
        tight_resized = np.asarray(
            Image.fromarray(tight, mode="RGB").resize(
                (new_w, new_h), Image.BILINEAR,
            ),
            dtype=np.uint8,
        )
    except Exception:
        canvas = np.broadcast_to(canon_mid_tier, (28, 28, 3)).copy()
        canvas[:2, :, :] = 255
        canvas[-2:, :, :] = 255
        canvas[:, :2, :] = 255
        canvas[:, -2:, :] = 255
        return canvas

    # Map the tight RGB digit pixels:
    #   ink (bright) → mid-bright (200) per channel
    #   bg (dark)   → dark mid-tier (55)
    # Linear remap of luminance: 0 → 55, 255 → 200, per channel.
    digit_lum = tight_resized.astype(np.float32)
    digit_remapped = (55.0 + (digit_lum / 255.0) * 145.0).clip(0, 255).astype(np.uint8)

    canvas = np.broadcast_to(canon_mid_tier, (28, 28, 3)).copy()
    canvas[:2, :, :] = 255
    canvas[-2:, :, :] = 255
    canvas[:, :2, :] = 255
    canvas[:, -2:, :] = 255

    y0 = (28 - new_h) // 2
    x0 = (28 - new_w) // 2
    canvas[y0:y0 + new_h, x0:x0 + new_w, :] = digit_remapped
    return canvas


def _route_to_dark_on_light(crop: np.ndarray) -> np.ndarray:
    """Ensure ``crop`` is in DARK-ON-LIGHT polarity (training format).

    Auto-detects current polarity from the median value:
    - median ≥ 128 → already dark-on-light → no-op
    - median < 128 → bright-on-dark canonical → invert to dark-on-light

    Used by the four signal classifiers under the new training-
    distribution input convention. With ``_tight_repad_glyph`` outputs
    (dark-on-light by design), this is a no-op. Legacy callers that
    still pass canonical bright-on-dark crops trigger the inversion.
    Preserves dtype and shape.
    """
    if crop is None or crop.size == 0:
        return crop
    if crop.dtype == np.uint8:
        med = float(np.median(crop))
        if med >= 128:
            return crop
        return (255 - crop.astype(np.int16)).clip(0, 255).astype(np.uint8)
    # Float input — assume [0, 1]
    med = float(np.median(crop))
    if med >= 0.5:
        return crop
    return np.clip(1.0 - crop.astype(np.float32), 0.0, 1.0).astype(crop.dtype)


def _route_to_bright_on_dark(crop: np.ndarray) -> np.ndarray:
    """Ensure ``crop`` is in BRIGHT-ON-DARK polarity (training-flipped).

    Symmetric counterpart to :func:`_route_to_dark_on_light`. Used by
    secondary / rgb_inv classifiers (which were trained on the
    flipped distribution).
    """
    if crop is None or crop.size == 0:
        return crop
    if crop.dtype == np.uint8:
        med = float(np.median(crop))
        if med < 128:
            return crop
        return (255 - crop.astype(np.int16)).clip(0, 255).astype(np.uint8)
    med = float(np.median(crop))
    if med < 0.5:
        return crop
    return np.clip(1.0 - crop.astype(np.float32), 0.0, 1.0).astype(crop.dtype)


def _route_rgb_to_dol(crop: np.ndarray) -> np.ndarray:
    """RGB version of :func:`_route_to_dark_on_light`. Polarity decided
    via max-channel luminance median.
    """
    if crop is None or crop.size == 0:
        return crop
    if crop.ndim != 3 or crop.shape[-1] != 3:
        return crop
    if crop.dtype == np.uint8:
        gray = crop.max(axis=2)
        med = float(np.median(gray))
        if med >= 128:
            return crop
        return (255 - crop.astype(np.int16)).clip(0, 255).astype(np.uint8)
    return crop


def _route_rgb_to_bod(crop: np.ndarray) -> np.ndarray:
    """RGB version of :func:`_route_to_bright_on_dark`."""
    if crop is None or crop.size == 0:
        return crop
    if crop.ndim != 3 or crop.shape[-1] != 3:
        return crop
    if crop.dtype == np.uint8:
        gray = crop.max(axis=2)
        med = float(np.median(gray))
        if med < 128:
            return crop
        return (255 - crop.astype(np.int16)).clip(0, 255).astype(np.uint8)
    return crop


def _feed_signal_cnn(
    model_name: str, crop: np.ndarray,
) -> np.ndarray:
    """Return ``crop`` in the polarity the named model was trained on.

    Inputs are assumed CANONICAL bright-on-dark (the output of
    ``_canonicalize_polarity``). This helper routes that canonical crop
    through the inversion needed to match each model's training polarity.

    Padding-aware inversion (CRITICAL): the segmenters
    (``_segment_glyphs``, ``_crop_to_28x28``, the proportional
    segmenter's tail) all PAD their per-glyph crops with 255 (white) on
    a 2-pixel border before resizing to 28×28. The training-data
    extractor uses the same convention. So the bright border around a
    glyph is a SHARED CONVENTION between training and inference — both
    polarities have a bright outer ring.

    A naive ``1.0 - crop`` inversion flips the bright border to dark,
    creating a dark ring around the glyph that the model never saw at
    training time. Empirically this caused the gray PRIMARY model to
    classify every input as the icon (@) class because the dark
    border looked like the icon's silhouette boundary.

    The fix: after inverting, RESTORE the corner-padding pixels (the
    outer ring) to their pre-inversion bright value. We detect padding
    pixels as those whose pre-inversion value is at the BRIGHT extreme
    of the crop's range — the segmenters set them to 255 / 1.0
    explicitly so they're trivially separable.

    Float crops in [0, 1] are inverted via ``1 - crop``; uint8 crops in
    [0, 255] are inverted via ``255 - crop``. The data type and shape are
    preserved.

    ``model_name`` must be one of ``_CNN_POLARITY`` keys: ``primary``,
    ``secondary``, ``rgb``, ``rgb_inv``. An unknown name returns the
    crop unchanged with a debug log — defensive so a typo never breaks
    classification entirely.
    """
    expected = _CNN_POLARITY.get(model_name)
    if expected is None:
        log.debug(
            "_feed_signal_cnn: unknown model_name=%r — returning crop "
            "unchanged", model_name,
        )
        return crop
    if expected == "bright_on_dark":
        # Canonical is already bright-on-dark — feed as-is.
        return crop

    # expected == "dark_on_light" — INK-AREA-ONLY inversion.
    #
    # The segmenter pipeline pads each 28×28 crop with a 2-px white
    # (bright) ring before resizing. A naive ``1 - x`` would flip that
    # ring to dark, creating a feature the training-time data never
    # had (training samples were extracted with the same bright-pad
    # convention but on dark-on-light source). Empirically the model
    # then classifies every input as the icon ('@') class because the
    # dark ring resembles the icon's silhouette boundary.
    #
    # Solution: invert the ink area, then restore the padding ring's
    # bright pixels to bright. We detect the padding ring positionally
    # — it lives in the outermost 1-2 rows/cols of the 28×28 crop —
    # and gate the restoration on the INPUT being near-bright (the
    # bilinear-resized padding is ≥ 0.9 in the float [0, 1] domain).
    if crop.dtype == np.uint8:
        out = (255 - crop.astype(np.int16)).clip(0, 255).astype(np.uint8)
        h, w = out.shape[:2]
        if h >= 8 and w >= 8:
            ring = 2
            for region in (
                np.s_[:ring, :],
                np.s_[h - ring:, :],
                np.s_[:, :ring],
                np.s_[:, w - ring:],
            ):
                ring_in = crop[region]
                ring_out = out[region]
                ring_out[ring_in >= 230] = 255
        return out

    # Float (any width). Use 1.0 - x and clamp.
    out = np.clip(1.0 - crop.astype(np.float32), 0.0, 1.0)
    if out.ndim >= 2:
        h = out.shape[0]
        w = out.shape[1]
        if h >= 8 and w >= 8:
            ring = 2
            for region in (
                np.s_[:ring, :],
                np.s_[h - ring:, :],
                np.s_[:, :ring],
                np.s_[:, w - ring:],
            ):
                ring_in = crop[region]
                ring_out = out[region]
                ring_out[ring_in >= 0.9] = 1.0
    return out.astype(crop.dtype)


def _classify_crops_signal(
    crops: list[np.ndarray],
) -> list[tuple[str, float]]:
    """Batch-classify 28×28 crops via the signature-specific ONNX CNN.

    Uses ``model_signal_cnn.onnx`` from the ``signal`` region kind in
    the training registry — trained on user-collected signature glyphs
    (``training_data_user_sig``), so reads of the SC mining-signal
    panel benefit from the user's own labelled data instead of being
    classified by the HUD model (which was trained on mass / resistance
    / instability digits with different rendering scale + colors).

    Input convention: ``crops`` are CANONICAL bright-on-dark (text
    pixels ≈ 1.0, bg ≈ 0.0 in the float32 [0, 1] domain). The function
    routes them through :func:`_feed_signal_cnn` which inverts to the
    DARK-on-LIGHT polarity this model was trained on before invoking
    ``.run``. Callers should NOT pre-invert.

    Mirrors :func:`_classify_crops` (HUD primary CNN) so the signature
    pipeline can swap classifier without changing the surrounding
    pipeline code. Returns ``[]`` when the signal CNN is missing or
    its session can't be loaded — caller treats that as "no signal-
    specific opinion" and falls through to the next voter.
    """
    if not crops:
        return []
    try:
        import onnxruntime as _ort  # noqa: F401
    except Exception as exc:
        log.debug("api: _classify_crops_signal swallowed: %s", exc)
        return []
    try:
        from . import training_registry as _tr  # type: ignore
    except Exception:
        try:
            from .. import training_registry as _tr  # type: ignore
        except Exception as exc:
            log.debug("api: _classify_crops_signal swallowed: %s", exc)
            return []
    try:
        model_path = _tr.get_model_path("signal")
    except Exception as exc:
        log.debug("api: _classify_crops_signal swallowed: %s", exc)
        return []
    if not model_path.is_file():
        return []
    global _signal_session, _signal_session_path, _signal_classes
    try:
        if (
            _signal_session is None
            or _signal_session_path != str(model_path)
        ):
            # Cap thread count — same rationale as _signal_cnn_at_tess_boxes:
            # ONNX defaults to one thread per core and starves the Qt GUI
            # thread.
            import onnxruntime as _ort
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = 1
            _opts.inter_op_num_threads = 1
            _signal_session = _ort.InferenceSession(
                str(model_path),
                sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            _signal_session_path = str(model_path)
            try:
                import json as _json
                meta = _json.loads(
                    model_path.with_suffix(".json").read_text(
                        encoding="utf-8",
                    )
                )
                _signal_classes = meta.get("charClasses", "0123456789@")
            except Exception:
                # No JSON sidecar in the production tree → introspect
                # the model's output shape and infer the alphabet.
                # 11 = digits + icon class @ (current production model).
                # 10 = digits-only legacy.
                try:
                    out_shape = _signal_session.get_outputs()[0].shape
                    n_out = int(out_shape[-1]) if out_shape else 11
                except Exception:
                    n_out = 11
                _signal_classes = (
                    "0123456789@" if n_out == 11 else "0123456789"
                )
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal session load failed: %s",
            exc,
        )
        return []

    inp_name = _signal_session.get_inputs()[0].name
    # PRIMARY expects dark-on-light. Auto-detect input polarity per
    # crop and invert canonical bright-on-dark inputs to match training
    # distribution. This routing is REQUIRED for the production CNN
    # weights (verified empirically: feeding canonical bright-on-dark
    # produces ``@`` at 1.00 confidence on every input).
    routed = [_route_to_dark_on_light(c) for c in crops]
    batch = np.array(routed, dtype=np.float32).reshape(-1, 1, 28, 28)
    try:
        logits = _signal_session.run(None, {inp_name: batch})[0]
        # ── POLARITY-ROBUST SELF-CORRECTION (2026-05) ──────────────────
        # _route_to_dark_on_light's polarity DETECTION misfires on some
        # captures — notably higher-resolution HUDs whose value pills have
        # near-pure-white backgrounds — and hands the model the INVERTED
        # crop, producing confident-looking but wrong reads (live 21,350
        # -> "40919"). The CNN is only confident (>0.9) on the CORRECT
        # polarity, so we also classify the inverse and, per glyph, keep
        # whichever polarity the model is more confident about. This is a
        # no-op when detection was already right (routed crop wins), and
        # it self-heals any future canonicalization drift.
        logits_inv = _signal_session.run(None, {inp_name: (1.0 - batch)})[0]
    except Exception as exc:
        log.debug("sc_ocr.signal: _classify_crops_signal infer failed: %s", exc)
        return []
    results: list[tuple[str, float]] = []
    n_classes = len(_signal_classes)
    for i in range(len(crops)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        probs_inv = np.exp(logits_inv[i] - np.max(logits_inv[i]))
        probs_inv /= probs_inv.sum()
        if float(probs_inv.max()) > float(probs.max()):
            probs = probs_inv
        idx = int(np.argmax(probs))
        if not (0 <= idx < n_classes):
            results.append(("?", 0.0))
            continue
        results.append((_signal_classes[idx], float(probs[idx])))
    # Apply font-specific pixel rules to fix the CNN's known confusions.
    # Same convention as the HUD CNN's tail (_classify_crops calls
    # _apply_one_vs_seven_rule before returning). For the signal font:
    #   * 8 vs 0/6: ``8`` has a horizontal waist; ``0`` has a vertical
    #     strut; ``6`` has a populated mid-row tail. Override 0/6 -> 8
    #     when waist is unambiguous.
    #
    # NOTE: the 1-vs-7 rule (``_apply_one_vs_seven_rule_signal``) was
    # initially wired here too, but the rule's bottom_density and
    # top_right_density thresholds were tuned for the HUD font and
    # produce 40%+ false-positive '1' overrides on real signal '7's
    # (the signal-font 7's diagonal terminates at the bottom-left
    # quadrant, looking enough like a '1' bottom-serif at the rule's
    # bands). The actual fix for the 7-misread-as-1 case is the
    # comma-trimmer above (now firing at 1.10×+2px instead of 1.15×+
    # 3px), which strips the fused comma so the CNN sees a clean '7'.
    # The helper is kept for potential future use with re-tuned
    # thresholds.
    results = _apply_eight_vs_zero_rule(crops, results)
    return results


def _classify_crops_signal_topk(
    crops: list[np.ndarray],
    k: int = 2,
) -> list[list[tuple[str, float]]]:
    """Top-K variant of :func:`_classify_crops_signal`.

    Returns ``[[(top1_char, top1_conf), (top2_char, top2_conf), ...], ...]``
    — one inner list per input crop, each holding the top ``k`` softmax
    classes ordered by descending probability. Used by the proportional
    segmenter's lexicon-driven backtracking: when the top-1 composition
    isn't a known signature value, the segmenter probes single-position
    swaps to top-2 to see if any composition lands in the lexicon.

    The 8-vs-0 pixel rule is applied to the top-1 only — its purpose is
    to fix a known confusion where the CNN's primary class is wrong but
    the structural waist test corrects it. Top-2/3 are emitted raw from
    the softmax. Backtracking should only swap top-1 → top-2 when top-1
    isn't lexicon-valid, so the slight asymmetry doesn't change behavior.

    Mirrors :func:`_classify_crops_signal` byte-for-byte through model
    load and inference. The only delta is the post-softmax accumulation:
    we keep the top ``k`` indices via ``argpartition`` instead of just
    ``argmax``. Returns an empty list when the model isn't available;
    callers detect that and fall back to top-1-only paths.
    """
    if not crops:
        return []
    try:
        import onnxruntime as _ort  # noqa: F401
    except Exception as exc:
        log.debug("api: _classify_crops_signal_topk swallowed: %s", exc)
        return []
    try:
        from . import training_registry as _tr  # type: ignore
    except Exception:
        try:
            from .. import training_registry as _tr  # type: ignore
        except Exception as exc:
            log.debug("api: _classify_crops_signal_topk swallowed: %s", exc)
            return []
    try:
        model_path = _tr.get_model_path("signal")
    except Exception as exc:
        log.debug("api: _classify_crops_signal_topk swallowed: %s", exc)
        return []
    if not model_path.is_file():
        return []
    global _signal_session, _signal_session_path, _signal_classes
    try:
        if (
            _signal_session is None
            or _signal_session_path != str(model_path)
        ):
            import onnxruntime as _ort
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = 1
            _opts.inter_op_num_threads = 1
            _signal_session = _ort.InferenceSession(
                str(model_path),
                sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            _signal_session_path = str(model_path)
            try:
                import json as _json
                meta = _json.loads(
                    model_path.with_suffix(".json").read_text(
                        encoding="utf-8",
                    )
                )
                _signal_classes = meta.get("charClasses", "0123456789@")
            except Exception:
                try:
                    out_shape = _signal_session.get_outputs()[0].shape
                    n_out = int(out_shape[-1]) if out_shape else 11
                except Exception:
                    n_out = 11
                _signal_classes = (
                    "0123456789@" if n_out == 11 else "0123456789"
                )
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_topk session load "
            "failed: %s",
            exc,
        )
        return []

    inp_name = _signal_session.get_inputs()[0].name
    routed = [_route_to_dark_on_light(c) for c in crops]
    batch = np.array(routed, dtype=np.float32).reshape(-1, 1, 28, 28)
    try:
        logits = _signal_session.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_topk infer failed: %s",
            exc,
        )
        return []

    n_classes = len(_signal_classes)
    k_eff = max(1, min(k, n_classes))
    results: list[list[tuple[str, float]]] = []
    for i in range(len(crops)):
        row = logits[i]
        probs = np.exp(row - np.max(row))
        probs /= probs.sum()
        # argpartition returns the indices of the k largest unsorted;
        # sort that subset descending to get top-K in order.
        if k_eff < n_classes:
            top_idx = np.argpartition(probs, -k_eff)[-k_eff:]
        else:
            top_idx = np.arange(n_classes)
        top_idx = top_idx[np.argsort(-probs[top_idx])]
        per_crop: list[tuple[str, float]] = []
        for ci in top_idx:
            cidx = int(ci)
            if 0 <= cidx < n_classes:
                per_crop.append((_signal_classes[cidx], float(probs[cidx])))
        if not per_crop:
            per_crop = [("?", 0.0)]
        results.append(per_crop)

    # Apply the 8-vs-0 pixel rule to the top-1 entry only. The rule
    # operates on per-crop top-1 strings and may rewrite that single
    # entry; top-2/3 are unaffected (and not consulted by the rule).
    top1_pairs: list[tuple[str, float]] = [
        r[0] if r else ("?", 0.0) for r in results
    ]
    top1_pairs = _apply_eight_vs_zero_rule(crops, top1_pairs)
    for i, p in enumerate(top1_pairs):
        if results[i]:
            results[i][0] = p
    return results


# Lazy session for the polarity-inverted signature CNN (paired with
# the primary _signal_session). Same threading caps as the primary.
_signal_inv_session = None
_signal_inv_session_path = ""
_signal_inv_classes = "0123456789"

# Lazy session for the RGB-input experimental signature CNN. Runs as
# a SHADOW voter — its classifications appear in the live viewer
# (SIGNATURE (RGB) row) but are NEVER consumed by the strict / dual-
# agree gates that decide the actual OCR output. Lets us watch the
# RGB CNN's behaviour against real captures without any risk to the
# production read.
_signal_rgb_session = None
_signal_rgb_session_path = ""
_signal_rgb_classes = "0123456789"


def _classify_crops_signal_rgb(
    rgb_crops: list[np.ndarray],
) -> list[tuple[str, float]]:
    """Classify a list of (28, 28, 3) uint8 RGB crops with the RGB
    signal CNN.

    Shadow-mode classifier: parallel to the grayscale primary/secondary
    pair, but its output is NEVER fed into the strict-gate or
    dual-agree-gate decision. The runtime calls this just so the live
    viewer's diagnostic row shows what the RGB CNN would say on the
    same segmented glyphs.

    Returns ``[]`` when the model isn't on disk yet, when ONNXRuntime
    fails to load it, or when inference errors. The caller treats the
    empty list as "RGB CNN not available — skip the diag row".
    """
    if not rgb_crops:
        return []
    try:
        import onnxruntime as _ort  # noqa: F401
    except Exception as exc:
        log.debug("api: _classify_crops_signal_rgb swallowed: %s", exc)
        return []
    try:
        from . import training_registry as _tr  # type: ignore
    except Exception:
        try:
            from .. import training_registry as _tr  # type: ignore
        except Exception as exc:
            log.debug("api: _classify_crops_signal_rgb swallowed: %s", exc)
            return []
    try:
        model_path = _tr.get_model_path("signal_rgb")
    except Exception as exc:
        log.debug("api: _classify_crops_signal_rgb swallowed: %s", exc)
        return []
    # Prefer the v2 model when present — adds the @ icon class as an
    # 11th output, trained on the same RGB pool plus colorized icon
    # samples. The v2 file lives next to the v1 in ``ocr/models/`` and
    # the registry doesn't have a separate ``signal_rgb_v2`` kind, so
    # we route here at the call site.
    try:
        v2_path = model_path.with_name("model_signal_rgb_cnn_v2.onnx")
        if v2_path.is_file():
            model_path = v2_path
    except Exception:
        pass
    if not model_path.is_file():
        return []

    global _signal_rgb_session, _signal_rgb_session_path, _signal_rgb_classes
    try:
        if (
            _signal_rgb_session is None
            or _signal_rgb_session_path != str(model_path)
        ):
            import onnxruntime as _ort
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = 1
            _opts.inter_op_num_threads = 1
            _signal_rgb_session = _ort.InferenceSession(
                str(model_path),
                sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            _signal_rgb_session_path = str(model_path)
            try:
                import json as _json
                meta = _json.loads(
                    model_path.with_suffix(".json").read_text(
                        encoding="utf-8",
                    )
                )
                _signal_rgb_classes = meta.get("charClasses", "0123456789")
            except Exception:
                # Introspect output count: v1 = 10 classes (digits),
                # v2 = 11 classes (digits + @ icon).
                try:
                    out_shape = (
                        _signal_rgb_session.get_outputs()[0].shape
                    )
                    n_out = int(out_shape[-1]) if out_shape else 10
                except Exception:
                    n_out = 10
                _signal_rgb_classes = (
                    "0123456789@" if n_out == 11 else "0123456789"
                )
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_rgb session load "
            "failed: %s", exc,
        )
        return []

    inp_name = _signal_rgb_session.get_inputs()[0].name
    # RGB v2 expects dark-on-light. Auto-detect polarity per crop via
    # max-channel projection median and invert when needed. Keeps the
    # call site simple (callers pass canonical RGB; routing happens
    # internally) and is required for the production v2 model weights.
    routed = [_route_rgb_to_dol(c) for c in rgb_crops]
    # Build NCHW float32 batch in [0, 1].
    try:
        batch = np.stack(routed, axis=0).astype(np.float32) / 255.0
        # rgb_crops are HWC; transpose to CHW.
        if batch.ndim == 4 and batch.shape[3] == 3:
            batch = batch.transpose(0, 3, 1, 2)
        elif batch.ndim != 4 or batch.shape[1] != 3:
            log.debug(
                "sc_ocr.signal: RGB CNN got unexpected batch shape %s",
                batch.shape,
            )
            return []
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_rgb prep failed: %s",
            exc,
        )
        return []
    try:
        logits = _signal_rgb_session.run(None, {inp_name: batch})[0]
        # Polarity-robust self-correction (see _classify_crops_signal).
        logits_inv = _signal_rgb_session.run(None, {inp_name: (1.0 - batch)})[0]
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_rgb infer failed: %s",
            exc,
        )
        return []
    results: list[tuple[str, float]] = []
    n_classes = len(_signal_rgb_classes)
    for i in range(len(rgb_crops)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        probs_inv = np.exp(logits_inv[i] - np.max(logits_inv[i]))
        probs_inv /= probs_inv.sum()
        if float(probs_inv.max()) > float(probs.max()):
            probs = probs_inv
        idx = int(np.argmax(probs))
        if not (0 <= idx < n_classes):
            results.append(("?", 0.0))
            continue
        results.append((_signal_rgb_classes[idx], float(probs[idx])))
    return results


# Lazy session for the polarity-inverted RGB experimental signature
# CNN. Same shadow-only contract as ``_signal_rgb_session`` — its
# classifications appear in the live viewer (``SIGNATURE
# (signal_rgb_inv)`` row) but are NEVER consumed by any voting gate.
_signal_rgb_inv_session = None
_signal_rgb_inv_session_path = ""
_signal_rgb_inv_classes = "0123456789"


def _classify_crops_signal_rgb_inv(
    rgb_inv_crops: list[np.ndarray],
) -> list[tuple[str, float]]:
    """Classify a list of (28, 28, 3) uint8 RGB crops with the
    polarity-inverted RGB signal CNN.

    Input convention (POLARITY ROUTING REFACTOR 2026-05): callers now
    pass CANONICAL bright-on-dark crops, identical to what
    ``_classify_crops_signal_rgb`` expects. This function applies its
    own polarity routing via :func:`_feed_signal_cnn` — and since the
    rgb_inv model was trained on data the trainer flipped (1 - x), it
    expects bright-on-dark input → no inversion applied here.

    The historical contract was "caller pre-inverts" but that pre-
    inversion was based on a flawed polarity assumption — the runtime
    was already feeding the wrong polarity to the primary RGB CNN, so
    the rgb_inv pre-inversion was further compounding the mismatch.
    The new contract aligns inputs across all four signal classifiers
    so call sites don't have to track polarity per model.

    Shadow-mode in the legacy gate, but PROMOTED to a real voter under
    the new digit-position consensus (:func:`_vote_on_digit_position`).
    """
    if not rgb_inv_crops:
        return []
    try:
        import onnxruntime as _ort  # noqa: F401
    except Exception as exc:
        log.debug("api: _classify_crops_signal_rgb_inv swallowed: %s", exc)
        return []
    try:
        from . import training_registry as _tr  # type: ignore
    except Exception:
        try:
            from .. import training_registry as _tr  # type: ignore
        except Exception as exc:
            log.debug(
                "api: _classify_crops_signal_rgb_inv swallowed: %s", exc,
            )
            return []
    try:
        model_path = _tr.get_model_path("signal_rgb_inv")
    except Exception as exc:
        log.debug("api: _classify_crops_signal_rgb_inv swallowed: %s", exc)
        return []
    if not model_path.is_file():
        return []

    global _signal_rgb_inv_session, _signal_rgb_inv_session_path, _signal_rgb_inv_classes
    try:
        if (
            _signal_rgb_inv_session is None
            or _signal_rgb_inv_session_path != str(model_path)
        ):
            import onnxruntime as _ort
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = 1
            _opts.inter_op_num_threads = 1
            _signal_rgb_inv_session = _ort.InferenceSession(
                str(model_path),
                sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            _signal_rgb_inv_session_path = str(model_path)
            try:
                import json as _json
                meta = _json.loads(
                    model_path.with_suffix(".json").read_text(
                        encoding="utf-8",
                    )
                )
                _signal_rgb_inv_classes = meta.get(
                    "charClasses", "0123456789",
                )
            except Exception:
                _signal_rgb_inv_classes = "0123456789"
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_rgb_inv session "
            "load failed: %s", exc,
        )
        return []

    inp_name = _signal_rgb_inv_session.get_inputs()[0].name
    # RGB-INV expects bright-on-dark (training-flipped). Auto-detect
    # polarity per crop and invert when needed. Tolerates both call
    # conventions: the legacy WingmanAI pattern (caller pre-inverts via
    # ``255 - crop``) and the canonical pattern (caller passes the same
    # crop the RGB-PRIMARY consumes).
    routed = [_route_rgb_to_bod(c) for c in rgb_inv_crops]
    try:
        batch = np.stack(routed, axis=0).astype(np.float32) / 255.0
        if batch.ndim == 4 and batch.shape[3] == 3:
            batch = batch.transpose(0, 3, 1, 2)
        elif batch.ndim != 4 or batch.shape[1] != 3:
            log.debug(
                "sc_ocr.signal: RGB-inv CNN got unexpected batch "
                "shape %s", batch.shape,
            )
            return []
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_rgb_inv prep "
            "failed: %s", exc,
        )
        return []
    try:
        logits = _signal_rgb_inv_session.run(None, {inp_name: batch})[0]
        # Polarity-robust self-correction (see _classify_crops_signal).
        logits_inv = _signal_rgb_inv_session.run(None, {inp_name: (1.0 - batch)})[0]
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_rgb_inv infer "
            "failed: %s", exc,
        )
        return []
    results: list[tuple[str, float]] = []
    n_classes = len(_signal_rgb_inv_classes)
    for i in range(len(rgb_inv_crops)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        probs_inv = np.exp(logits_inv[i] - np.max(logits_inv[i]))
        probs_inv /= probs_inv.sum()
        if float(probs_inv.max()) > float(probs.max()):
            probs = probs_inv
        idx = int(np.argmax(probs))
        if not (0 <= idx < n_classes):
            results.append(("?", 0.0))
            continue
        results.append((_signal_rgb_inv_classes[idx], float(probs[idx])))
    return results


def _classify_crops_signal_inv(
    crops: list[np.ndarray],
) -> list[tuple[str, float]]:
    """Batch-classify CANONICAL bright-on-dark 28×28 crops via the
    signal-specific INVERTED ONNX CNN.

    Input convention (POLARITY ROUTING REFACTOR 2026-05): callers pass
    CANONICAL bright-on-dark crops — identical to what
    :func:`_classify_crops_signal` accepts. The internal polarity
    helper :func:`_feed_signal_cnn` routes for this model: the
    ``signal_inv`` trainer flipped ``1 - x`` on dark-on-light source
    samples, so the resulting model expects BRIGHT-on-DARK at
    inference → canonical input matches → feed AS-IS (no inversion).

    Pairs with :func:`_classify_crops_signal` to give the signature
    pipeline a fully-isolated dual-CNN setup:

        primary   = ``model_signal_cnn.onnx``       (expects dark-on-light)
        secondary = ``model_signal_inv_cnn.onnx``   (expects bright-on-dark)

    Both wrappers now accept the SAME canonical input — the polarity
    routing happens internally per model. This eliminates the prior
    pattern where the call site pre-inverted (``1.0 - c``) for the
    secondary, which compounded with the runtime canonicalization to
    feed the wrong polarity to the model.

    Returns ``[]`` if the model is missing or the session can't be
    loaded — caller falls through to the HUD-inverted CNN as a
    backstop.
    """
    if not crops:
        return []
    try:
        import onnxruntime as _ort  # noqa: F401
    except Exception as exc:
        log.debug("api: _classify_crops_signal_inv swallowed: %s", exc)
        return []
    try:
        from . import training_registry as _tr  # type: ignore
    except Exception:
        try:
            from .. import training_registry as _tr  # type: ignore
        except Exception as exc:
            log.debug("api: _classify_crops_signal_inv swallowed: %s", exc)
            return []
    try:
        model_path = _tr.get_model_path("signal_inv")
    except Exception as exc:
        log.debug("api: _classify_crops_signal_inv swallowed: %s", exc)
        return []
    if not model_path.is_file():
        return []

    global _signal_inv_session, _signal_inv_session_path, _signal_inv_classes
    try:
        if (
            _signal_inv_session is None
            or _signal_inv_session_path != str(model_path)
        ):
            import onnxruntime as _ort
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = 1
            _opts.inter_op_num_threads = 1
            _signal_inv_session = _ort.InferenceSession(
                str(model_path),
                sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            _signal_inv_session_path = str(model_path)
            try:
                import json as _json
                meta = _json.loads(
                    model_path.with_suffix(".json").read_text(
                        encoding="utf-8",
                    )
                )
                _signal_inv_classes = meta.get("charClasses", "0123456789")
            except Exception:
                _signal_inv_classes = "0123456789"
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_inv session load failed: %s",
            exc,
        )
        return []

    inp_name = _signal_inv_session.get_inputs()[0].name
    # SECONDARY expects bright-on-dark (training was flipped).
    # Auto-detect input polarity and invert if needed. The legacy
    # WingmanAI call-site convention pre-inverts; the production
    # call-sites under the per-position consensus may pass canonical.
    # The auto-detect routing handles both correctly.
    routed = [_route_to_bright_on_dark(c) for c in crops]
    batch = np.array(routed, dtype=np.float32).reshape(-1, 1, 28, 28)
    try:
        logits = _signal_inv_session.run(None, {inp_name: batch})[0]
        # Polarity-robust self-correction (see _classify_crops_signal).
        logits_inv = _signal_inv_session.run(None, {inp_name: (1.0 - batch)})[0]
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: _classify_crops_signal_inv infer failed: %s",
            exc,
        )
        return []
    results: list[tuple[str, float]] = []
    n_classes = len(_signal_inv_classes)
    for i in range(len(crops)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        probs_inv = np.exp(logits_inv[i] - np.max(logits_inv[i]))
        probs_inv /= probs_inv.sum()
        if float(probs_inv.max()) > float(probs.max()):
            probs = probs_inv
        idx = int(np.argmax(probs))
        if not (0 <= idx < n_classes):
            results.append(("?", 0.0))
            continue
        results.append((_signal_inv_classes[idx], float(probs[idx])))
    # Same pixel-rule pass as the primary — distinguishes 8/0 by the
    # waist-vs-strut structural feature, which is polarity-agnostic.
    results = _apply_eight_vs_zero_rule(crops, results)
    return results


def _classify_crops_inv(crops: list[np.ndarray]) -> list[tuple[str, float]]:
    """Batch-classify polarity-INVERTED crops via the sibling ONNX CNN.

    Mirrors :func:`_classify_crops` but uses the inverted-polarity
    model (``model_cnn_inv.onnx``) trained on dark-text-on-light-bg
    crops.  Returns ``[]`` if the inverted model is not present so the
    secondary voter can fall through gracefully without affecting the
    primary read.

    Used by the secondary path in :func:`_ocr_value_crop` to provide a
    truly decorrelated peer vote — different polarity AND different
    weights vs the primary classifier.
    """
    if not crops or not fallback._ensure_model_inv():
        return []
    session = fallback._session_inv
    char_classes = fallback._char_classes_inv
    inp_name = session.get_inputs()[0].name
    batch = np.array(crops, dtype=np.float32).reshape(-1, 1, 28, 28)
    try:
        logits = session.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug("sc_ocr: inverted ONNX inference failed: %s", exc)
        return []
    results = []
    for i in range(len(crops)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        idx = int(np.argmax(probs))
        results.append((char_classes[idx], float(probs[idx])))
    # Same 1-vs-7 rule as the primary classifier — both CNNs share
    # this failure mode (saw 0.95-0.96 conf "7" on a glyph that's
    # clearly a "1" in user-supplied screenshots).
    return _apply_one_vs_seven_rule(crops, results)


def _classify_crops_inv_topk(
    crops: list[np.ndarray],
    k: int = 2,
) -> list[list[tuple[str, float]]]:
    """Top-K variant of :func:`_classify_crops_inv`.

    See :func:`_classify_crops_topk` for shape/semantics. Uses the
    inverted-polarity HUD CNN (``fallback._session_inv``).
    """
    if not crops or not fallback._ensure_model_inv():
        return []
    session = fallback._session_inv
    char_classes = fallback._char_classes_inv
    inp_name = session.get_inputs()[0].name
    batch = np.array(crops, dtype=np.float32).reshape(-1, 1, 28, 28)
    try:
        logits = session.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug("sc_ocr: inverted ONNX top-K inference failed: %s", exc)
        return []
    n_classes = len(char_classes)
    k_eff = max(1, min(k, n_classes))
    results: list[list[tuple[str, float]]] = []
    for i in range(len(crops)):
        row = logits[i]
        probs = np.exp(row - np.max(row))
        probs /= probs.sum()
        if k_eff < n_classes:
            top_idx = np.argpartition(probs, -k_eff)[-k_eff:]
        else:
            top_idx = np.arange(n_classes)
        top_idx = top_idx[np.argsort(-probs[top_idx])]
        per_crop: list[tuple[str, float]] = []
        for ci in top_idx:
            cidx = int(ci)
            if 0 <= cidx < n_classes:
                per_crop.append((char_classes[cidx], float(probs[cidx])))
        if not per_crop:
            per_crop = [("?", 0.0)]
        results.append(per_crop)
    top1_pairs: list[tuple[str, float]] = [
        r[0] if r else ("?", 0.0) for r in results
    ]
    top1_pairs = _apply_one_vs_seven_rule(crops, top1_pairs)
    for i, p in enumerate(top1_pairs):
        if results[i]:
            results[i][0] = p
    return results


# ──────────────────────────────────────────────────────────────────────
# HUD-RGB per-glyph CNN (side voter, additive)
# ──────────────────────────────────────────────────────────────────────
# Loads ``ocr/models/model_hud_rgb_cnn.onnx`` (path from
# ``training_registry.REGIONS["hud_rgb"]``) which is the RGB twin of the
# HUD primary CNN. Trained on training_data_user_panel_rgb/ — RGB
# per-glyph crops extracted via scripts/extract_hud_glyph_crops_rgb.py
# from the same HUD value-strip crops the CRNN consumes.
#
# **HUD font ≠ Signature font** — this loader uses its OWN session
# distinct from the signature RGB CNN's session (_signal_rgb_session).
# Substituting one for the other would mix font priors and degrade
# both regions' accuracy.
#
# Side-voter contract: the function is "additive" — it can only PROVIDE
# a vote, never veto one. In the dual-agree gate (primary + secondary
# match → accept), if this RGB voter agrees with the primary (with both
# at reasonable mean confidence), accept the read even if secondary
# disagreed. Captures cases where the polarity-inverted secondary is
# misled by chromatic aberration but the colour-aware RGB voter sees
# the digit correctly.
_hud_rgb_session = None
_hud_rgb_classes: str = "0123456789.%"
_hud_rgb_session_tried: bool = False


def _ensure_hud_rgb_model() -> bool:
    """Lazy-load ``model_hud_rgb_cnn.onnx``. Short-circuits on absence.

    Imports the registry inside the function to avoid a module-load
    dependency cycle (training_registry imports nothing from sc_ocr,
    sc_ocr imports nothing from training_registry at module load).
    """
    global _hud_rgb_session, _hud_rgb_classes, _hud_rgb_session_tried
    if _hud_rgb_session is not None:
        return True
    if _hud_rgb_session_tried:
        return False
    _hud_rgb_session_tried = True

    try:
        from .. import training_registry as _reg
        spec = _reg.get("hud_rgb")
        model_path = str(spec.model_path)
    except Exception as exc:
        log.debug("sc_ocr.hud: registry lookup for hud_rgb failed: %s", exc)
        return False

    if not os.path.isfile(model_path):
        log.debug("sc_ocr.hud: HUD-RGB ONNX not found at %s", model_path)
        return False

    try:
        import onnxruntime as _ort
    except ImportError:
        log.debug("sc_ocr.hud: onnxruntime not installed (hud_rgb)")
        return False

    try:
        import json as _json
        meta_path = os.path.join(
            os.path.dirname(model_path),
            os.path.splitext(os.path.basename(model_path))[0] + ".json",
        )
        if os.path.isfile(meta_path):
            with open(meta_path) as fh:
                _hud_rgb_classes = _json.load(fh).get(
                    "charClasses", _hud_rgb_classes,
                )

        opts = _ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = _ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _hud_rgb_session = _ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        log.info(
            "sc_ocr.hud: HUD-RGB ONNX loaded (%s, classes=%r)",
            os.path.basename(model_path), _hud_rgb_classes,
        )
        return True
    except Exception as exc:
        log.warning("sc_ocr.hud: HUD-RGB ONNX load failed: %s", exc)
        return False


def _classify_crops_hud_rgb(
    rgb_crops: list[np.ndarray],
) -> list[tuple[str, float]]:
    """Batch-classify RGB 28×28 tiles via ``model_hud_rgb_cnn.onnx``.

    Accepts ``(28, 28, 3)`` ``uint8`` or ``float32`` arrays — the
    function normalizes to ``float32 / 255.0`` and transposes to CHW
    internally. Returns ``[]`` if the model isn't on disk or
    onnxruntime isn't installed; callers must handle that as "voter
    abstains" rather than "voter said wrong".

    Crops should follow the same DARK-on-LIGHT convention as the
    grayscale segmenter — bg pixels ≈ 255, ink pixels darker. The
    extractor's pad+resize already produces this distribution.
    """
    if not rgb_crops or not _ensure_hud_rgb_model():
        return []
    arrs: list[np.ndarray] = []
    for c in rgb_crops:
        a = np.asarray(c)
        if a.dtype != np.float32:
            a = a.astype(np.float32)
        if a.max() > 1.0:
            a = a / 255.0
        if a.shape == (28, 28, 3):
            a = a.transpose(2, 0, 1)
        elif a.shape != (3, 28, 28):
            # Skip mis-shaped crops rather than crash.
            log.debug(
                "sc_ocr.hud: HUD-RGB classifier skip crop with shape %s",
                a.shape,
            )
            continue
        arrs.append(a)
    if not arrs:
        return []
    batch = np.stack(arrs, axis=0).astype(np.float32)
    try:
        inp_name = _hud_rgb_session.get_inputs()[0].name
        logits = _hud_rgb_session.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug("sc_ocr.hud: HUD-RGB ONNX inference failed: %s", exc)
        return []
    out: list[tuple[str, float]] = []
    for i in range(logits.shape[0]):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        idx = int(np.argmax(probs))
        out.append((_hud_rgb_classes[idx], float(probs[idx])))
    return out


def _classify_crops_hud_rgb_topk(
    rgb_crops: list[np.ndarray],
    k: int = 2,
) -> list[list[tuple[str, float]]]:
    """Top-K variant of :func:`_classify_crops_hud_rgb`.

    See :func:`_classify_crops_topk` for shape/semantics. Uses the
    HUD-RGB per-glyph CNN session (``_hud_rgb_session``).
    """
    if not rgb_crops or not _ensure_hud_rgb_model():
        return []
    arrs: list[np.ndarray] = []
    for c in rgb_crops:
        a = np.asarray(c)
        if a.dtype != np.float32:
            a = a.astype(np.float32)
        if a.max() > 1.0:
            a = a / 255.0
        if a.shape == (28, 28, 3):
            a = a.transpose(2, 0, 1)
        elif a.shape != (3, 28, 28):
            log.debug(
                "sc_ocr.hud: HUD-RGB top-K skip crop with shape %s",
                a.shape,
            )
            continue
        arrs.append(a)
    if not arrs:
        return []
    batch = np.stack(arrs, axis=0).astype(np.float32)
    try:
        inp_name = _hud_rgb_session.get_inputs()[0].name
        logits = _hud_rgb_session.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug("sc_ocr.hud: HUD-RGB top-K inference failed: %s", exc)
        return []
    n_classes = len(_hud_rgb_classes)
    k_eff = max(1, min(k, n_classes))
    results: list[list[tuple[str, float]]] = []
    for i in range(logits.shape[0]):
        row = logits[i]
        probs = np.exp(row - np.max(row))
        probs /= probs.sum()
        if k_eff < n_classes:
            top_idx = np.argpartition(probs, -k_eff)[-k_eff:]
        else:
            top_idx = np.arange(n_classes)
        top_idx = top_idx[np.argsort(-probs[top_idx])]
        per_crop: list[tuple[str, float]] = []
        for ci in top_idx:
            cidx = int(ci)
            if 0 <= cidx < n_classes:
                per_crop.append((_hud_rgb_classes[cidx], float(probs[cidx])))
        if not per_crop:
            per_crop = [("?", 0.0)]
        results.append(per_crop)
    return results


# ──────────────────────────────────────────
# Per-glyph debug dump for the live glyph reader
# ──────────────────────────────────────────
# When a viewer polls the dump path, the pipeline atomically writes
# the most recent per-field glyph crops + classifier outputs to disk
# so the user can SEE exactly what the OCR is consuming and what it's
# returning. Same shape as the existing debug_panel_overlay.png
# pattern — file-based IPC, no in-process coupling.

_GLYPH_DUMP_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "debug_glyphs",
)
_GLYPH_DUMP_DIR = os.path.normpath(_GLYPH_DUMP_DIR)
_GLYPH_DUMP_INDEX = os.path.join(_GLYPH_DUMP_DIR, "latest.json")
_glyph_dump_lock = threading.Lock()
# Serializes mutations of the process-global TESSDATA_PREFIX env var.
# Signal + HUD pool workers run pytesseract in parallel and would
# otherwise race each other's save/restore in the finally blocks.
_TESSDATA_LOCK = threading.Lock()


def _dump_value_crop(field: str, value_crop) -> None:
    """Dump the per-field value crop (raw input to the segmenter) so we
    can see whether a misread is a cropping bug (digits cut off / label
    text leaking in) vs a downstream segmentation/classification bug.

    Writes ``debug_glyphs/<field>_value_crop.png``. Best-effort, must
    never affect OCR output. Companion to :func:`_dump_glyphs` which
    only shows the post-segmentation 28×28 glyphs and so can't tell us
    if the missing digit was already gone before segmentation ran.

    Gated on the diagnostic heartbeat: when no viewer is open this is
    a single os.path.getmtime + a cached compare = effectively free.
    """
    if value_crop is None or not field:
        return
    from . import debug_overlay as _dbg_gate
    if not _dbg_gate.diagnostics_active():
        return
    try:
        os.makedirs(_GLYPH_DUMP_DIR, exist_ok=True)
        from PIL import Image as _Image
        path = os.path.join(_GLYPH_DUMP_DIR, f"{field}_value_crop.png")
        # Use atomic write so any concurrent reader never sees a half
        # file. Pass format=PNG explicitly because PIL's save() infers
        # format from the extension and the temp suffix '.tmp' isn't
        # a known image type.
        tmp = path + ".tmp"
        if isinstance(value_crop, _Image.Image):
            value_crop.save(tmp, format="PNG")
        else:
            _Image.fromarray(np.asarray(value_crop)).save(tmp, format="PNG")
        os.replace(tmp, path)
    except Exception as exc:
        log.debug("api: _dump_value_crop swallowed: %s", exc)


def _dump_voter(
    field: str, source: str, text: str,
    mean_conf: "float | None" = None,
) -> None:
    """Dump a whole-value-crop engine's read into the live viewer index.

    CRNN, Tesseract, and the parallel-vote *winner* operate on the
    entire value crop (not per-character), so they don't have 28×28
    glyph PNGs to display. Persist their text + mean confidence in
    the same JSON the viewer polls so all voters appear side-by-side
    with the per-glyph CNN rows.

    Source values used:
      * ``crnn``      — CRNN whole-crop decoding
      * ``tesseract`` — Tesseract whole-crop OCR
      * ``vote``      — parallel-vote winner that the field actually
                        returned to the caller
    """
    if not field:
        return
    from . import debug_overlay as _dbg_gate
    if not _dbg_gate.diagnostics_active():
        return
    try:
        import json as _json
        import time as _time
        os.makedirs(_GLYPH_DUMP_DIR, exist_ok=True)
        with _glyph_dump_lock:
            try:
                with open(_GLYPH_DUMP_INDEX, "r", encoding="utf-8") as f:
                    index = _json.load(f)
                if not isinstance(index, dict):
                    index = {}
            except Exception:
                index = {}
            index.setdefault("fields", {})
            index["timestamp"] = _time.time()
            index["fields"][f"{field}_{source}"] = {
                "field": field,
                "source": source,
                "timestamp": _time.time(),
                "joined": text or "",
                "glyphs": [],
                "mean_conf": (
                    float(mean_conf) if mean_conf is not None else None
                ),
            }
            tmp = _GLYPH_DUMP_INDEX + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(index, f, indent=2)
            os.replace(tmp, _GLYPH_DUMP_INDEX)
    except Exception as exc:
        log.debug("api: _dump_voter swallowed: %s", exc)


# In-process ring buffer of recent signature reads for the live viewer
# history panel. Capped at 8 to match the viewer's HISTORY_LEN. Entries
# are dicts with ``timestamp``, ``raw_text``, ``digits``, ``mean_conf``,
# ``validated_value``, and ``rejection_reason``. Diagnostic only.
_SIGNATURE_HISTORY: "list[dict]" = []
_SIGNATURE_HISTORY_MAX = 8


def _dump_signature_winner(
    raw_text: str,
    digits: str,
    mean_conf: float,
    validated_value: "int | None",
    rejection_reason: "str | None",
    per_digit_classifications: "list[dict] | None" = None,
) -> None:
    """Emit the signature pipeline's CRNN read + gate verdict to the
    live viewer index.

    Writes a ``signature_winner`` entry (mirroring the HUD ``winner``
    voter convention) so the glyph reader can render exactly what the
    signal-side OCR is feeding to the bubble. Includes ``validated_value``
    (what the bubble would show, or ``None`` if rejected) and a
    ``rejection_reason`` string when the gate rejects (e.g. ``"not 4-5
    digits"``, ``"out of range"``, ``"low confidence"``). Also appends to
    a ring-buffered ``signature_history`` for the viewer's history panel.

    ``per_digit_classifications`` is an optional list of per-digit CNN
    cross-check entries (``{"char", "confidence", "crop_path"}``) so
    the live viewer can render tiles for each digit the dual-polarity
    signal CNN classified. Additive — legacy callers omit it and the
    field is left out of the JSON entirely.

    Best-effort: any failure logs at DEBUG and returns. MUST NEVER
    affect OCR output.
    """
    from . import debug_overlay as _dbg_gate
    if not _dbg_gate.diagnostics_active():
        return
    try:
        import json as _json
        import time as _time
        os.makedirs(_GLYPH_DUMP_DIR, exist_ok=True)
        now_ts = _time.time()
        # Append to ring buffer (cap matches viewer HISTORY_LEN).
        _SIGNATURE_HISTORY.append({
            "timestamp": now_ts,
            "raw_text": raw_text or "",
            "digits": digits or "",
            "mean_conf": float(mean_conf) if mean_conf is not None else None,
            "validated_value": (
                int(validated_value) if validated_value is not None else None
            ),
            "rejection_reason": rejection_reason,
        })
        if len(_SIGNATURE_HISTORY) > _SIGNATURE_HISTORY_MAX:
            del _SIGNATURE_HISTORY[: -_SIGNATURE_HISTORY_MAX]

        with _glyph_dump_lock:
            try:
                with open(_GLYPH_DUMP_INDEX, "r", encoding="utf-8") as f:
                    index = _json.load(f)
                if not isinstance(index, dict):
                    index = {}
            except Exception:
                index = {}
            index.setdefault("fields", {})
            index["timestamp"] = now_ts
            # Display string mirrors the HUD pattern (joined = the read
            # the pipeline is exposing). When validated, show the int;
            # when rejected, fall back to the raw CRNN text so the user
            # can see what the model produced even though it was vetoed.
            if validated_value is not None:
                joined = str(validated_value)
            else:
                joined = raw_text or ""
            entry: dict = {
                "field": "signature",
                "source": "winner",
                "timestamp": now_ts,
                "joined": joined,
                "glyphs": [],
                "mean_conf": (
                    float(mean_conf) if mean_conf is not None else None
                ),
                # Signature-specific extras the viewer reads directly.
                "value_crop": "signature_value_crop.png",
                "crnn_text": raw_text or "",
                "crnn_digits": digits or "",
                "crnn_confidence": (
                    float(mean_conf) if mean_conf is not None else None
                ),
                "validated_value": (
                    int(validated_value) if validated_value is not None
                    else None
                ),
                "rejection_reason": rejection_reason,
                "history": list(_SIGNATURE_HISTORY),
            }
            # Additive: only emit when the CNN cross-check produced
            # tiles. Legacy viewers without per-digit support ignore
            # the field; new viewers render a per-digit tile row.
            if per_digit_classifications:
                entry["per_digit_classifications"] = list(
                    per_digit_classifications
                )
            index["fields"]["signature_winner"] = entry
            tmp = _GLYPH_DUMP_INDEX + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(index, f, indent=2)
            os.replace(tmp, _GLYPH_DUMP_INDEX)
    except Exception as exc:
        log.debug("api: _dump_signature_winner swallowed: %s", exc)


# ── Filter-event publishing for the live glyph viewer ──────────────
# Every box the runtime filter chain removes (chroma ghost, pitch
# ghost, quarantine veto, envelope) is published as a per-field
# "dropped" tile row in the live viewer (reason code as the tile
# label: C=chroma  P=pitch  V=quarantine-veto  E=envelope), and EVERY
# event — drops, suspicious-keeps, recenter shifts — is appended to
# debug_glyphs/filter_events.log regardless of whether the viewer is
# open, so post-hoc debugging has a timeline even for headless runs.
_FILTER_EVENT_LOG = None  # resolved lazily next to the glyph dump dir
_filter_drops_acc: "dict[str, list]" = {}


def _filter_event_log(line: str) -> None:
    """Append one timestamped line to debug_glyphs/filter_events.log.
    Always on (cheap); self-rotates at ~1 MB keeping the newest half."""
    global _FILTER_EVENT_LOG
    try:
        import time as _t
        if _FILTER_EVENT_LOG is None:
            os.makedirs(_GLYPH_DUMP_DIR, exist_ok=True)
            _FILTER_EVENT_LOG = os.path.join(
                _GLYPH_DUMP_DIR, "filter_events.log",
            )
        try:
            if (os.path.isfile(_FILTER_EVENT_LOG)
                    and os.path.getsize(_FILTER_EVENT_LOG) > 1_000_000):
                with open(_FILTER_EVENT_LOG, encoding="utf-8",
                          errors="replace") as _f:
                    _keep = _f.readlines()[-2000:]
                with open(_FILTER_EVENT_LOG, "w", encoding="utf-8") as _f:
                    _f.writelines(_keep)
        except Exception:
            pass
        with open(_FILTER_EVENT_LOG, "a", encoding="utf-8") as _f:
            _f.write(
                _t.strftime("%H:%M:%S") + f".{int(_t.time()*1000)%1000:03d} "
                + line + "\n"
            )
    except Exception:
        pass


def _filter_drops_begin(field: str) -> None:
    """Reset the per-field drop accumulator at the start of a filter
    chain and clear any stale viewer row from the previous frame."""
    try:
        _filter_drops_acc[field] = []
        from . import debug_overlay as _dbg_fd
        if _dbg_fd.diagnostics_active():
            _clear_viewer_entry(field, "dropped")
    except Exception:
        pass


def _record_filter_drops(field: str, filter_name: str, items) -> None:
    """Publish dropped boxes: viewer tiles (when the viewer is live)
    + always-on event log.

    ``items``: list of (crop, reason_code, detail_str). reason_code is
    the single-letter tile label; detail goes to the log file."""
    if not items:
        return
    try:
        for _crop, _code, _detail in items:
            _filter_event_log(
                f"DROP field={field} filter={filter_name} "
                f"reason={_code} {_detail}"
            )
        from . import debug_overlay as _dbg_rd
        if not _dbg_rd.diagnostics_active():
            return
        _acc = _filter_drops_acc.setdefault(field, [])
        _acc.extend(items)
        _dump_glyphs(
            field, "dropped",
            [_c for _c, _, _ in _acc],
            [(_code, 0.0) for _, _code, _ in _acc],
        )
    except Exception as _exc:
        log.debug("sc_ocr.hud: _record_filter_drops swallowed: %s", _exc)


def _dump_glyphs(
    field: str,
    source: str,
    crops: "list[np.ndarray]",
    results: "list[tuple[str, float]]",
    overrides: "Optional[list[Optional[str]]]" = None,
) -> None:
    """Dump per-glyph crops + classifier output for the live viewer.

    ``field`` is one of ``mass / resistance / instability``.
    ``source`` is ``primary`` (high-confidence ONNX path) or
    ``secondary`` (parallel-vote ONNX path). Each (field, source)
    pair is tracked independently in the index JSON so the viewer
    can show both decision paths side-by-side when both fire.

    ``overrides`` is an optional list parallel to ``crops`` / ``results``
    whose entries are ``"."`` for positions where the box-size dot
    detector overrode the CNN prediction, ``None`` otherwise. When set,
    the glyph entry gets an extra ``"override": "box_size_dot"`` field
    so the live viewer can visually distinguish heuristic-driven labels
    from CNN-driven ones.

    Writes one PNG per glyph (28×28) plus an atomic-rewrite of the
    index JSON. Cheap (~5-10 ms per call). No-ops gracefully if
    anything fails; this is purely diagnostic and must NEVER affect
    the OCR result.
    """
    if not crops or not results:
        return
    from . import debug_overlay as _dbg_gate
    if not _dbg_gate.diagnostics_active():
        return
    try:
        from PIL import Image as _Image
        import json as _json
        import time as _time
        os.makedirs(_GLYPH_DUMP_DIR, exist_ok=True)

        with _glyph_dump_lock:
            # Save each glyph crop as PNG.
            glyphs_meta = []
            for i, (crop, (ch, conf)) in enumerate(zip(crops, results)):
                fname = f"{field}_{source}_{i}.png"
                fpath = os.path.join(_GLYPH_DUMP_DIR, fname)
                try:
                    arr = (crop.astype(np.float32) * 255).clip(0, 255).astype(np.uint8) \
                        if crop.dtype != np.uint8 and crop.max() <= 1.5 \
                        else crop.astype(np.uint8)
                    # Detect channel count: RGB (H, W, 3) vs grayscale
                    # (H, W). Used by the shadow ``signal_rgb`` voter
                    # to display its tile row in colour while every
                    # other voter remains grayscale.
                    if arr.ndim == 3 and arr.shape[-1] == 3:
                        _Image.fromarray(arr, mode="RGB").save(fpath)
                    else:
                        _Image.fromarray(arr, mode="L").save(fpath)
                except Exception:
                    continue
                entry = {
                    "idx": i,
                    "char": ch,
                    "conf": float(conf),
                    "img": fname,
                }
                if overrides is not None and i < len(overrides) and overrides[i] == ".":
                    entry["override"] = "box_size_dot"
                glyphs_meta.append(entry)

            # Merge into the index JSON. Preserves the most-recent
            # per-(field, source) entry across calls.
            try:
                with open(_GLYPH_DUMP_INDEX, "r", encoding="utf-8") as f:
                    index = _json.load(f)
                if not isinstance(index, dict):
                    index = {}
            except Exception:
                index = {}
            index.setdefault("fields", {})
            index["timestamp"] = _time.time()
            joined = "".join(g["char"] for g in glyphs_meta)
            index["fields"][f"{field}_{source}"] = {
                "field": field,
                "source": source,
                "timestamp": _time.time(),
                "joined": joined,
                "glyphs": glyphs_meta,
            }
            tmp = _GLYPH_DUMP_INDEX + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(index, f, indent=2)
            os.replace(tmp, _GLYPH_DUMP_INDEX)
    except Exception as exc:
        # Diagnostic dump failure must never break OCR. Swallow.
        log.debug("api: _dump_glyphs swallowed: %s", exc)


def _clear_viewer_entry(field: str, source: str) -> None:
    """Remove a stale (field, source) entry from the live-viewer index.

    Works for both glyph dumps (``_dump_glyphs``) and voter dumps
    (``_dump_voter``) — they share the same ``index["fields"]`` dict
    keyed by ``f"{field}_{source}"``.

    When the primary classifier returns early on high confidence, the
    secondary / CRNN / Tesseract blocks never run and never overwrite
    their previous dumps. Without explicit clearing, the live viewer
    keeps showing the stale crops from whatever earlier scan last
    triggered the fallback path — which is misleading when the
    previous scan had misaligned row crops or other transient bugs
    (e.g. classifying pixels from a commodity row as "INSTABILITY
    (SECONDARY) '571'" while the current scan correctly reads
    '32.17').

    Best-effort: any failure is swallowed so this stays purely
    diagnostic.
    """
    try:
        import json as _json
        if not os.path.exists(_GLYPH_DUMP_INDEX):
            return
        with _glyph_dump_lock:
            try:
                with open(_GLYPH_DUMP_INDEX, "r", encoding="utf-8") as f:
                    index = _json.load(f)
                if not isinstance(index, dict):
                    return
            except Exception as exc:
                log.debug("api: _clear_viewer_entry swallowed: %s", exc)
                return
            fields = index.get("fields") or {}
            key = f"{field}_{source}"
            if key not in fields:
                return
            fields.pop(key, None)
            index["fields"] = fields
            tmp = _GLYPH_DUMP_INDEX + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(index, f, indent=2)
            os.replace(tmp, _GLYPH_DUMP_INDEX)
    except Exception as exc:
        log.debug("api: _clear_viewer_entry swallowed: %s", exc)


def _filter_runtime_junk_boxes(crops, boxes, field: str = ""):
    """Segmenter runtime ladder, steps 1+2 (user-approved 2026-06-10).

    VETO (disable: SC_NO_BOX_VETO) — drop boxes whose crop is a
    near-duplicate of a Glyph-Review-quarantined tile (ocr.glyph_gate):
    the user's 2,629 hand-rejections become a runtime junk detector for
    icon fragments / glow blobs / merged pairs that geometry can't
    describe.

    ENVELOPE (disable: SC_NO_BOX_ENVELOPE) — drop digit-sized boxes
    violating the measured ink-geometry envelope from the 8,879-tile
    geometry report: no SC digit's ink is wider than tall (max w/h
    ~0.85; merged pairs run >= ~1.2), and a box under 0.55x the tallest
    box is a stroke fragment. Both ratios are scale-free. Dot-sized
    boxes (w < 0.55x median) are exempt.

    Safety: never empties the set; if more than half the boxes would
    drop, keeps the originals (that smells like a bad frame, and
    honest downstream Nones beat mass-vetoed guesses).
    """
    try:
        if field not in ("mass", "resistance", "instability"):
            return crops, boxes
        if not boxes or not crops or len(crops) != len(boxes):
            return crops, boxes
        # LADDER + LIVE VERDICT: ALL three runtime filters are opt-in.
        # The ENVELOPE regressed the 130-panel harness (mass -1,
        # instability -1 and +2 confident-wrong). The VETO was
        # harness-neutral but REGRESSED LIVE (2026-06-10, caught by the
        # filter_events log on its first night): it false-matched the
        # narrow leading '1' of instability 1.43 against the quarantine
        # every scan — the quarantine contains QUALITY-rejects of real
        # digits, so a live narrow '1' legitimately near-dups a blurry
        # rejected '1'; low-detail glyphs can't be safely near-dup
        # vetoed. [.][4][3] then read '743'/'243' -> structural reject
        # -> instability=None on a rock that read 1.43 without it.
        # A future veto needs junk-only references (icons/fragments),
        # not the full quarantine. Enable: SC_BOX_VETO=1 /
        # SC_BOX_ENVELOPE=1.
        _veto_on = bool(os.environ.get("SC_BOX_VETO"))
        _env_on = bool(os.environ.get("SC_BOX_ENVELOPE"))
        if not (_veto_on or _env_on):
            return crops, boxes
        _gate_fn = None
        if _veto_on:
            try:
                from ..glyph_gate import (
                    is_quarantine_lookalike as _gate_fn,
                )
            except Exception:
                _gate_fn = None
        _ws = sorted(int(_b[2]) for _b in boxes)
        _med_w = _ws[len(_ws) // 2]
        _max_h = max(int(_b[3]) for _b in boxes)
        _keep: list[int] = []
        _drops: list = []  # (idx, reason, box)
        for _i, (_bx, _c) in enumerate(zip(boxes, crops)):
            _w, _h = int(_bx[2]), int(_bx[3])
            _is_dot = _med_w > 0 and _w < 0.55 * _med_w
            if not _is_dot and _env_on:
                if _h > 0 and _w / float(_h) > 1.2:
                    _drops.append((_i, "env-wide", tuple(_bx)))
                    continue
                if _max_h > 0 and _h < 0.55 * _max_h:
                    _drops.append((_i, "env-frag", tuple(_bx)))
                    continue
            if not _is_dot and _veto_on and _gate_fn is not None:
                try:
                    _a = np.asarray(_c, dtype=np.float32)
                    if _a.ndim == 2 and _a.size:
                        if float(_a.max()) <= 1.5:
                            _a = _a * 255.0
                        if _gate_fn(_a.astype(np.uint8), near=True):
                            _drops.append((_i, "veto", tuple(_bx)))
                            continue
                except Exception:
                    pass
            _keep.append(_i)
        if not _drops:
            return crops, boxes
        if not _keep or len(_drops) > len(boxes) // 2:
            log.info(
                "sc_ocr.hud: runtime box filters would drop %d/%d "
                "(field=%s) — suspicious, keeping originals: %s",
                len(_drops), len(boxes), field, _drops,
            )
            _filter_event_log(
                f"SUSPICIOUS-KEEP field={field} filter=veto/envelope "
                f"would_drop={len(_drops)}/{len(boxes)} "
                f"{[(r, b) for _, r, b in _drops]}"
            )
            return crops, boxes
        log.info(
            "sc_ocr.hud: runtime box filters dropped %d/%d (field=%s): %s",
            len(_drops), len(boxes), field,
            [(r, b) for _, r, b in _drops],
        )
        _record_filter_drops(field, "veto/envelope", [
            (crops[_i], "V" if _r == "veto" else "E",
             f"box={_b} rule={_r}")
            for _i, _r, _b in _drops
        ])
        return [crops[_i] for _i in _keep], [boxes[_i] for _i in _keep]
    except Exception as _exc:
        log.debug("sc_ocr.hud: runtime box filters failed: %s", _exc)
        return crops, boxes


def _recenter_crops_to_centroid(crops, field: str = ""):
    """Segmenter runtime ladder, step 3 (disable: SC_NO_RECENTER).

    LADDER VERDICT (130-panel harness, 2026-06-10): REGRESSED HARD
    (mass 52->49, resistance 27->24) — the production CNNs are trained
    on tiles from the SAME extraction pipeline that serves them, so
    they already learned the pipeline's placement; recentering at
    serving time moves crops OUT of the learned distribution. The skew
    the geometry report measured was pipeline-tiles vs the synthetic
    sets, not train-vs-serve. Opt-IN only (SC_RECENTER=1); if ever
    revisited, recenter the TRAINING tiles and the serving crops
    symmetrically, then retrain.
    """
    if not os.environ.get("SC_RECENTER") or not crops:
        return crops
    _out = []
    for _c in crops:
        try:
            _a = np.asarray(_c, dtype=np.float32)
            if _a.ndim != 2 or not _a.size:
                _out.append(_c)
                continue
            _h, _w = _a.shape
            _r = 2
            _ring = np.concatenate([
                _a[:_r].ravel(), _a[-_r:].ravel(),
                _a[:, :_r].ravel(), _a[:, -_r:].ravel(),
            ])
            _bg = float(np.median(_ring))
            _diff = np.abs(_a - _bg)
            _scale = 1.0 if float(_a.max()) <= 1.5 else 255.0
            _thr = max(0.12 * _scale, 0.30 * float(_diff.max()))
            _mask = _diff > _thr
            if not _mask.any():
                _out.append(_c)
                continue
            _ys, _xs = np.nonzero(_mask)
            _dy = int(round((_h - 1) / 2.0 - float(_ys.mean())))
            _dx = int(round((_w - 1) / 2.0 - float(_xs.mean())))
            _dy = max(-4, min(4, _dy))
            _dx = max(-4, min(4, _dx))
            if _dx == 0 and _dy == 0:
                _out.append(_c)
                continue
            _filter_event_log(
                f"RECENTER field={field} tile={len(_out)} "
                f"dx={_dx:+d} dy={_dy:+d}"
            )
            _sh = np.full_like(_a, _bg)
            _sh[max(0, _dy):_h + min(0, _dy),
                max(0, _dx):_w + min(0, _dx)] = (
                _a[max(0, -_dy):_h + min(0, -_dy),
                   max(0, -_dx):_w + min(0, -_dx)]
            )
            _out.append(_sh)
        except Exception:
            _out.append(_c)
    return _out


def _filter_pitch_ghost_boxes(
    crops: list,
    boxes: list,
    field: str = "",
) -> "tuple[list, list]":
    """Drop pitch-breaking ghost boxes (instability only).

    Live 2026-06-09 (true 14.21, 7 boxes): chromatic aberration can emit
    ghost copies with WHITE cores that every CNN reads as a confident
    real digit (the duplicated '2' @ 1.00) — no classifier or chroma
    signal separates them. Geometry does: panel digits are fixed-pitch,
    and an inserted ghost splits one pitch interval into two sub-pitch
    gaps. Iteratively remove the interior box whose removal most reduces
    the spread of center-to-center deltas, while removal produces a
    DRAMATIC improvement (spread > 0.25x pitch before, < 0.60x of the
    prior spread after). A uniform row (all real digits, e.g. 103.10's
    six boxes) offers no such improvement, so nothing is removed.
    Never drops below 4 boxes (D.DD) and never touches the dot-sized
    box (the decimal breaks pitch legitimately — it is half-width and
    sits low; its slot still holds a center, so deltas use centers).
    """
    try:
        # GATE AT 7+ BOXES: the widest legitimate instability form is
        # DDD.DD = 6 boxes, so a 7-box segmentation PROVABLY contains
        # phantoms, and a legit row can never be touched. (A first cut
        # gated at 5 regressed the harness: a narrow leading '1' in
        # '126.02' sits off the center-grid — its ink-center shifts
        # within the monospace cell — and got culled as a ghost.)
        if field != "instability" or not boxes or len(boxes) < 7:
            return crops, boxes
        if not crops or len(crops) != len(boxes):
            return crops, boxes
        _ws = sorted(int(_b[2]) for _b in boxes)
        _med_w = _ws[len(_ws) // 2]
        _c = [float(_b[0]) + float(_b[2]) / 2.0 for _b in boxes]
        _n = len(_c)
        _d = [_c[_i + 1] - _c[_i] for _i in range(_n - 1)]
        # GRID FIT (not greedy removal): with two ghosts, removing either
        # one alone doesn't reduce delta-spread (the other still breaks
        # pitch), so a greedy loop never starts. Instead, try candidate
        # pitches (each delta and each adjacent-pair sum — a ghost splits
        # one pitch into two sub-deltas that SUM to the pitch), anchor
        # the grid at each box, and keep the (pitch, anchor) fitting the
        # most boxes. Real digits land on the grid; ghosts don't.
        _cands = sorted({round(_x, 1) for _x in _d}
                        | {round(_d[_i] + _d[_i + 1], 1)
                           for _i in range(len(_d) - 1)})
        # The pitch floor must come from the WIDEST box: a monospace
        # cell is never narrower than its widest glyph. A median-width
        # floor broke when narrow '1's dragged the median down — that
        # admitted half-pitch grids, and a dense-enough grid trivially
        # fits EVERY box, masking the ghosts ("all on grid" bail).
        _max_w = max(int(_b[2]) for _b in boxes)
        _min_p = max(4.0, 0.9 * _max_w)
        _best = (0, None, None)  # (fit_count, on_grid_mask, pitch)
        for _p in _cands:
            if _p < _min_p:
                continue  # sub-cell pitch is structural nonsense
            for _a in range(_n):
                _mask = []
                for _bi, _x in enumerate(_c):
                    _k = round((_x - _c[_a]) / _p)
                    # narrow glyphs ('1') hug their ink: the box center
                    # may shift within the monospace cell by up to half
                    # the width deficit — widen their tolerance.
                    _tol = 0.25 * _p + max(
                        0.0, (_med_w - float(boxes[_bi][2])) / 2.0
                    )
                    _mask.append(abs(_x - (_c[_a] + _k * _p)) <= _tol)
                _fit = sum(_mask)
                if _fit > _best[0]:
                    _best = (_fit, _mask, _p)
        _fit, _mask, _p = _best
        # Only act on a decisive verdict: nearly all boxes on one grid,
        # at most 2 stragglers, and never drop the dot-sized box.
        if _mask is None or _fit == _n or (_n - _fit) > 2 or _fit < 4:
            return crops, boxes
        _idx = [
            _i for _i in range(_n)
            if _mask[_i]
            or (_med_w > 0 and int(boxes[_i][2]) < 0.55 * _med_w)
        ]
        _removed = [_i for _i in range(_n) if _i not in set(_idx)]
        if not _removed or len(_idx) < 4:
            return crops, boxes
        log.info(
            "sc_ocr.hud: pitch-ghost filter dropped %d/%d instability "
            "boxes %s (pitch outliers)",
            len(_removed), len(boxes),
            [boxes[_i] for _i in sorted(_removed)],
        )
        _record_filter_drops(field, "pitch-ghost", [
            (crops[_i], "P",
             f"box={tuple(boxes[_i])} off-grid pitch={_p:.1f}")
            for _i in sorted(_removed)
        ])
        return (
            [crops[_i] for _i in _idx],
            [boxes[_i] for _i in _idx],
        )
    except Exception as _exc:
        log.debug("sc_ocr.hud: pitch-ghost filter failed: %s", _exc)
        return crops, boxes


def _filter_chromatic_ghost_boxes(
    value_crop,
    crops: list,
    boxes: list,
    field: str = "",
) -> "tuple[list, list]":
    """Drop chromatic-aberration ghost boxes (instability only).

    Heavy RGB fringing on the SCAN RESULTS panel splits each white glyph
    into offset single-channel ghost copies; the segmenter then emits
    PHANTOM boxes between/after real digits (live 2026-06-09: '14.21'
    segmented into 7 boxes — a junk tile after the '4' plus a duplicated
    '2' — voters read '1464221'/'141221'). Real glyphs have an ACHROMATIC
    bright core (all channels high together); ghosts are single-channel
    fringes with no white core. Score each box by its achromatic-core
    fraction and drop boxes far below the median box. Scoring is
    RELATIVE, so all-red renders (the red '0.99') score uniformly low
    and nothing is dropped.
    """
    try:
        if field != "instability" or not boxes or len(boxes) < 3:
            return crops, boxes
        if not crops or len(crops) != len(boxes):
            return crops, boxes
        _rgb = np.asarray(value_crop.convert("RGB"), dtype=np.float32)
        _H, _W = _rgb.shape[0], _rgb.shape[1]
        _scores: list[float] = []
        for (_x, _y, _w, _h) in boxes:
            if _w < 1 or _h < 1 or _x + _w > _W or _y + _h > _H:
                return crops, boxes  # geometry mismatch — don't touch
            _t = _rgb[_y:_y + _h, _x:_x + _w]
            _mn = _t.min(axis=2)
            _mx = _t.max(axis=2)
            _core = float(((_mn > 0.55 * _mx) & (_mx > 120.0)).mean())
            _scores.append(_core)
        # The decimal DOT is a few bright pixels in a mostly-background
        # box, so its core FRACTION is naturally near zero — it must
        # never be a ghost candidate, or the consensus string loses its
        # dot and degenerates to the CRNN's own dotless read (which
        # would silently disable the '0.99' vs '099' override). Use the
        # pipeline's canonical dot test (w < 0.55x median width) and
        # judge only digit-sized boxes, against a digit-only median.
        _ws_f = sorted(int(_b[2]) for _b in boxes)
        _med_w = _ws_f[len(_ws_f) // 2]
        _is_dot = [
            _med_w > 0 and int(_b[2]) < 0.55 * _med_w for _b in boxes
        ]
        _digit_scores = sorted(
            _s for _s, _d in zip(_scores, _is_dot) if not _d
        )
        if not _digit_scores:
            return crops, boxes
        _med = _digit_scores[len(_digit_scores) // 2]
        if _med <= 0.03:
            return crops, boxes  # uniformly chromatic render — no-op
        _keep = [
            _i for _i, _s in enumerate(_scores)
            if _is_dot[_i] or _s >= 0.25 * _med
        ]
        if len(_keep) == len(boxes) or len(_keep) < 3:
            return crops, boxes
        _kept_set = set(_keep)
        _dropped = [
            (boxes[_i], round(_scores[_i], 3))
            for _i in range(len(boxes)) if _i not in _kept_set
        ]
        log.info(
            "sc_ocr.hud: chroma-ghost filter dropped %d/%d instability "
            "boxes %s (median core=%.3f)",
            len(boxes) - len(_keep), len(boxes), _dropped, _med,
        )
        _record_filter_drops(field, "chroma-ghost", [
            (crops[_i], "C",
             f"box={tuple(boxes[_i])} core={_scores[_i]:.3f} med={_med:.3f}")
            for _i in range(len(boxes)) if _i not in _kept_set
        ])
        return (
            [crops[_i] for _i in _keep],
            [boxes[_i] for _i in _keep],
        )
    except Exception as _exc:
        log.debug("sc_ocr.hud: chroma-ghost filter failed: %s", _exc)
        return crops, boxes


def _perglyph_consensus_digits(field: str, value_crop) -> "Optional[str]":
    """Return the per-glyph CONSENSUS digit string, or None.

    For mass/resistance: segment + classify the crop with the gray
    PRIMARY CNN and the inverted-polarity SECONDARY CNN. If the two
    decorrelated voters INDEPENDENTLY agree on the same digit string at
    high confidence, return it. Callers use this to override a confident-
    but-wrong CRNN gate read — the CRNN confuses look-alike digits
    (6/8, 3/8) while both per-glyph voters read it right (user
    2026-06-01: CRNN "3738" vs per-glyph "3736", both voters @ 1.00).

    Now also covers instability (X.XX): the per-glyph tiles read the
    decimal point as its own '.' tile, so the consensus string preserves
    it. Callers override the CRNN on ANY disagreement — a length mismatch
    (the CRNN dropping a digit/decimal, e.g. 3384->994 or 1.43->143) is
    even stronger evidence the CRNN is wrong than a same-length swap, so
    the old same-length guard was dropped (it blocked exactly those).
    """
    if field not in ("mass", "resistance", "instability"):
        return None
    try:
        _g = _canonicalize_polarity(
            np.asarray(value_crop.convert("L"), dtype=np.uint8)
        )
        _b = _adaptive_binarize(_g)
        _crops, _boxes = _segment_glyphs(_g, _b, field=field)
        # NOTE: no _filter_drops_begin here — the consensus helper runs
        # INSIDE a scan whose cascade/shadow chain owns the viewer row;
        # resetting would wipe their published drops mid-frame. Drops
        # found here still merge into the same row + event log.
        _crops, _boxes = _filter_chromatic_ghost_boxes(
            value_crop, _crops, _boxes, field=field,
        )
        _crops, _boxes = _filter_pitch_ghost_boxes(
            _crops, _boxes, field=field,
        )
        _crops, _boxes = _filter_runtime_junk_boxes(
            _crops, _boxes, field=field,
        )
        _crops = _recenter_crops_to_centroid(_crops, field=field)
        if not _crops:
            return None
        _pri = _classify_crops(_crops)
        _sec = _classify_crops_inv(
            [np.clip(1.0 - c, 0.0, 1.0).astype(np.float32) for c in _crops]
        )
        # GEOMETRY DOT OVERRIDE (live 2026-06-10): the cascade replaces
        # the dot-sized box's CNN read via box_size_dot, but this helper
        # classified raw — the instability dot tile read '7' (primary)
        # vs '2' (secondary), breaking exact agreement on a tile whose
        # identity GEOMETRY already proves, so the CRNN's '13' survived
        # into a structural reject (instability=None on a clean 1.43).
        # Apply the cascade's own rule: w < 0.55x median width -> '.'.
        _dot_med = 0
        if _boxes and len(_boxes) == len(_crops):
            _bws = sorted(int(_b[2]) for _b in _boxes)
            _dot_med = _bws[len(_bws) // 2]
            for _di, _bx in enumerate(_boxes):
                if _dot_med > 0 and int(_bx[2]) < 0.55 * _dot_med:
                    if _di < len(_pri):
                        _pri[_di] = (".", 1.0)
                    if _di < len(_sec):
                        _sec[_di] = (".", 1.0)

        def _dc(_res):
            if not _res:
                return "", 0.0
            # keep digits AND the decimal point (instability X.XX); the
            # trailing '%' on resistance is neither, so it drops out
            return ("".join(c for c, _ in _res if c.isdigit() or c == "."),
                    sum(cf for _, cf in _res) / len(_res))

        _pd, _pm = _dc(_pri)
        _sd, _sm = _dc(_sec)
        # EXACT string agreement between the two polarity-decorrelated
        # voters is the load-bearing signal; the confidence bars only
        # screen out mutual garbage. The secondary (inverted) model runs
        # systematically less confident on blurry live frames even when
        # right (user 2026-06-09: resistance 1@0.86, instability mean
        # 0.945, both correct and agreeing while the CRNN winner was
        # wrong) — a symmetric 0.97 bar silently disabled the override
        # on exactly those frames, so the secondary bar is lower.
        if _pd and _pd == _sd and _pm >= 0.95 and _sm >= 0.85:
            return _pd
        # The inverted SECONDARY sometimes fumbles a single glyph at low
        # confidence on live frames (0->4@0.52, 4->6@0.46 — user
        # 2026-06-09) which kills the exact-agreement above even though
        # the RGB per-glyph CNN agrees with the PRIMARY at 1.00. The RGB
        # model is an equally decorrelated partner (different weights,
        # different input space), so accept PRIMARY+RGB agreement as the
        # consensus pair too. Crop recipe mirrors the viewer's hud_rgb
        # row (_shadow_perglyph_dump), which is the combination the
        # screenshots validated.
        if _pd and _pm >= 0.95 and _boxes:
            _rgb = np.asarray(value_crop.convert("RGB"), dtype=np.uint8)
            _Hc, _Wc = _rgb.shape[0], _rgb.shape[1]
            _rgb_crops: list = []
            for (_bx, _by, _bw, _bh) in _boxes:
                if _bw < 1 or _bh < 1 or _bx + _bw > _Wc or _by + _bh > _Hc:
                    _rgb_crops = []
                    break
                _gl = _rgb[_by:_by + _bh, _bx:_bx + _bw].astype(np.float32)
                _padc = np.full(
                    (_bh + 4, _bw + 4, 3), 255.0, dtype=np.float32
                )
                _padc[2:-2, 2:-2] = _gl
                _rgb_crops.append(
                    _aspect_pad_resize_28(_padc.astype(np.uint8), bg=255)
                )
            if _rgb_crops:
                _rres = _classify_crops_signal_rgb(_rgb_crops)
                # same geometry dot override as the gray voters
                for _di, _bx in enumerate(_boxes):
                    if (_dot_med > 0
                            and int(_bx[2]) < 0.55 * _dot_med
                            and _di < len(_rres)):
                        _rres[_di] = (".", 1.0)
                _rd, _rm = _dc(_rres)
                if _pd == _rd and _rm >= 0.85:
                    return _pd
    except Exception as _exc:
        log.debug("sc_ocr.hud: per-glyph consensus failed: %s", _exc)
    return None


def _shadow_perglyph_dump(field: str, value_crop) -> None:
    """Refresh the glyph viewer's per-glyph tiles when a CRNN gate
    short-circuited the real per-glyph path.

    The HUD-RGB / legacy CRNN gates skip the per-glyph CNN cascade for
    speed, so its dump entries (``primary`` / ``secondary`` / ``hud_rgb``)
    never refresh and the live ``glyph_reader_viewer`` freezes on stale
    crops — the "glyph reader not updating for the mining HUD" symptom.

    Rather than just clearing those tiles (which makes the rows vanish),
    we run a fresh per-glyph SEGMENTATION + CLASSIFICATION of the SAME
    value crop the CRNN just won on and publish it as the ``primary``
    voter. The user sees the per-glyph CNN's read of the current frame
    side-by-side with the CRNN winner (e.g. CRNN ``4343`` vs per-glyph
    ``3384``), which is exactly the cross-check they want.

    This is DISPLAY-ONLY — it never changes the OCR result, and no-ops
    entirely unless a glyph viewer is active (heartbeat fresh).
    """
    try:
        from . import debug_overlay as _dbg_sg
        if not _dbg_sg.diagnostics_active():
            return
        _sg_rgb = np.asarray(value_crop.convert("RGB"), dtype=np.uint8)
        _sg_gray = _canonicalize_polarity(
            np.array(value_crop.convert("L"), dtype=np.uint8)
        )
        _sg_bin = _adaptive_binarize(_sg_gray)
        _sg_crops, _sg_boxes = _segment_glyphs(_sg_gray, _sg_bin, field=field)
        # Keep the viewer honest: apply the same ghost filters the
        # functional paths use, so the tiles shown match what the
        # pipeline actually classifies.
        _filter_drops_begin(field)
        _sg_crops, _sg_boxes = _filter_chromatic_ghost_boxes(
            value_crop, _sg_crops, _sg_boxes, field=field,
        )
        _sg_crops, _sg_boxes = _filter_pitch_ghost_boxes(
            _sg_crops, _sg_boxes, field=field,
        )
        _sg_crops, _sg_boxes = _filter_runtime_junk_boxes(
            _sg_crops, _sg_boxes, field=field,
        )
        _sg_crops = _recenter_crops_to_centroid(_sg_crops, field=field)
        if not _sg_crops:
            for _s in ("primary", "secondary", "hud_rgb", "vote"):
                _clear_viewer_entry(field, _s)
            return
        # primary — grayscale per-glyph CNN
        _sg_pri = _classify_crops(_sg_crops)
        _dump_glyphs(field, "primary", _sg_crops, _sg_pri)
        # secondary — inverted-polarity grayscale CNN. Must feed the
        # INVERTED crops exactly as the real cascade does (api.py ~9927:
        # `_classify_crops_inv([1.0 - c for c in primary_crops])`).
        # Feeding the non-inverted crops makes the inv-model read digits
        # as '%' — the "secondary corrupt?" symptom (user 2026-06-01).
        try:
            _sg_sec = _classify_crops_inv(
                [np.clip(1.0 - c, 0.0, 1.0).astype(np.float32)
                 for c in _sg_crops]
            )
        except Exception:
            _sg_sec = []
        if _sg_sec and len(_sg_sec) == len(_sg_crops):
            _dump_glyphs(field, "secondary", _sg_crops, _sg_sec)
        else:
            _clear_viewer_entry(field, "secondary")
        # hud_rgb — RGB per-glyph CNN on colour tiles built from the boxes
        _sg_rgb_crops: list = []
        try:
            _Hr, _Wr = _sg_rgb.shape[0], _sg_rgb.shape[1]
            for (_bx, _by, _bw, _bh) in _sg_boxes:
                if _bw < 1 or _bh < 1 or _bx + _bw > _Wr or _by + _bh > _Hr:
                    _sg_rgb_crops = []
                    break
                _gl = _sg_rgb[_by:_by + _bh, _bx:_bx + _bw].astype(np.float32)
                _pd = np.full((_bh + 4, _bw + 4, 3), 255.0, dtype=np.float32)
                _pd[2:-2, 2:-2] = _gl
                _sg_rgb_crops.append(
                    _aspect_pad_resize_28(_pd.astype(np.uint8), bg=255)
                )
            _sg_rgbres = (
                _classify_crops_signal_rgb(_sg_rgb_crops)
                if _sg_rgb_crops else []
            )
        except Exception:
            _sg_rgb_crops, _sg_rgbres = [], []
        if _sg_rgbres and len(_sg_rgbres) == len(_sg_rgb_crops):
            _dump_glyphs(field, "hud_rgb", _sg_rgb_crops, _sg_rgbres)
        else:
            _clear_viewer_entry(field, "hud_rgb")
        _clear_viewer_entry(field, "vote")
    except Exception as _sg_exc:
        log.debug("sc_ocr.hud: shadow per-glyph dump failed: %s", _sg_exc)


# ── Signal RGB CRNN (gate 0 in the signal recognition hierarchy) ──
# Whole-strip OCR model trained on user-verified signature captures.
# Far more accurate than the per-glyph segmenter+classifier pipeline
# (97% on test set, 83% on held-out val vs production's 10% on the
# 106-capture failure profile). Replaces the segmenter as the primary
# digit-reading path; existing per-glyph gates remain as fallbacks
# for low-confidence CRNN reads.

_SIGNAL_CRNN_RGB_SESSION = None
_SIGNAL_CRNN_RGB_ALPHABET: Optional[str] = None
_SIGNAL_CRNN_RGB_BLANK: int = -1
_SIGNAL_CRNN_RGB_H_TARGET: int = 48

# ── HUD value RGB CRNN (gate 0 in the HUD value-read hierarchy) ──
# Sibling of the signature CRNN: same architecture (5 conv blocks +
# 2-layer BiLSTM + CTC), different alphabet (``0123456789.%``), different
# data source (mass / resistance / instability value crops extracted
# from labeled HUD panels via scripts/extract_hud_value_crops.py).
#
# Replaces the per-glyph CNN's failure mode (segmenter mis-splits +
# digit-by-digit CNN confidently agreeing on garbage) the same way
# the signature CRNN did: read the whole strip in one shot via CTC,
# skip segmentation entirely.
_HUD_CRNN_RGB_SESSION = None
_HUD_CRNN_RGB_ALPHABET: Optional[str] = None
_HUD_CRNN_RGB_BLANK: int = -1
_HUD_CRNN_RGB_H_TARGET: int = 48


def _classify_hud_value_via_crnn_rgb(
    value_crop: "Image.Image",
    field: str,
    beam_width: int = 0,
) -> Optional[tuple[str, float]]:
    """Run the HUD-specific RGB CRNN on a value crop.

    Loads ``ocr/models/model_hud_crnn_rgb.onnx`` on first call,
    caches the onnxruntime session in module state. On every
    subsequent scan the lazy-loaded session is reused.

    Parameters
    ----------
    value_crop : PIL.Image
        The HUD value sub-region — output of ``_find_value_crop``
        for a mass / resistance / instability row.
    field : str
        One of ``"mass"`` / ``"resistance"`` / ``"instability"``.
        Used for:
          * per-field alphabet mask before softmax-argmax (mass is
            digits only; resistance allows ``%``; instability allows
            ``.``)
          * plausibility-aware beam rerank when ``beam_width > 0``
    beam_width : int, optional
        When ``> 0``, use prefix-beam-search CTC with the given beam
        width (typical 8) and rerank candidates by per-field
        plausibility. When ``0`` (default), use greedy CTC — kept as
        the default for back-compat with callers that haven't opted
        in to beam decode.

    Returns
    -------
    ``(digit_string, mean_confidence)`` on success, ``None`` when:
      * the ONNX model isn't on disk yet (e.g. before training has
        finished),
      * onnxruntime / inference fails,
      * the crop is too small to be meaningful.

    The caller (``_ocr_value_crop``) gates the returned read by
    length + confidence + field-specific format before accepting.
    """
    if value_crop is None:
        return None
    try:
        rgb = np.asarray(value_crop.convert("RGB"), dtype=np.uint8)
    except Exception:
        return None
    if rgb.size == 0 or rgb.shape[0] < 4 or rgb.shape[1] < 8:
        return None

    global _HUD_CRNN_RGB_SESSION
    global _HUD_CRNN_RGB_ALPHABET
    global _HUD_CRNN_RGB_BLANK
    global _HUD_CRNN_RGB_H_TARGET

    if _HUD_CRNN_RGB_SESSION is None:
        try:
            import onnxruntime as _ort  # noqa: F401
        except Exception as exc:
            log.debug("api: HUD CRNN onnxruntime missing: %s", exc)
            return None
        try:
            from pathlib import Path as _Path
            _models = _Path(__file__).resolve().parent.parent / "models"
            _onnx = _models / "model_hud_crnn_rgb.onnx"
            _meta = _models / "model_hud_crnn_rgb.json"
            if not _onnx.is_file() or not _meta.is_file():
                log.debug(
                    "api: HUD CRNN files not found (%s / %s)",
                    _onnx.name, _meta.name,
                )
                return None
            import json as _json
            meta = _json.loads(_meta.read_text(encoding="utf-8"))
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = 1
            _opts.inter_op_num_threads = 1
            _HUD_CRNN_RGB_SESSION = _ort.InferenceSession(
                str(_onnx), sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            _HUD_CRNN_RGB_ALPHABET = str(meta.get("alphabet", "0123456789.%"))
            _HUD_CRNN_RGB_BLANK = int(
                meta.get("blank_idx", len(_HUD_CRNN_RGB_ALPHABET))
            )
            shape = meta.get("input_shape") or [None, 3, 48, None]
            try:
                _HUD_CRNN_RGB_H_TARGET = int(shape[2]) if shape[2] else 48
            except Exception:
                _HUD_CRNN_RGB_H_TARGET = 48
            log.info(
                "sc_ocr.hud: loaded HUD CRNN (alphabet=%r blank=%d H=%d "
                "checkpoint_val_acc=%.3f)",
                _HUD_CRNN_RGB_ALPHABET, _HUD_CRNN_RGB_BLANK,
                _HUD_CRNN_RGB_H_TARGET,
                float(meta.get("checkpoint_val_acc", -1.0)),
            )
        except Exception as exc:
            log.debug("api: HUD CRNN load failed: %s", exc)
            return None

    sess = _HUD_CRNN_RGB_SESSION
    alphabet = _HUD_CRNN_RGB_ALPHABET or "0123456789.%"
    blank = _HUD_CRNN_RGB_BLANK if _HUD_CRNN_RGB_BLANK >= 0 else len(alphabet)
    h_target = _HUD_CRNN_RGB_H_TARGET

    # Lanczos resize to h_target preserving aspect.
    #
    # The HUD CRNN was trained on 48-px-height crops (per
    # ``train_hud_crnn_rgb.py``'s ``H_TARGET = 48``). The ONNX model's
    # height axis is FIXED (only batch + width are dynamic), so the
    # crop MUST be resized to 48 before inference. The interesting
    # distinction is the DIRECTION of the resize:
    #
    #   * Upscale (h0 < h_target) — the typical production case for
    #     1080p captures where ``_find_value_crop`` returns ~14-30 px
    #     tall crops. Lanczos upscale to 48 is the explicit "match
    #     training-sample crispness" step lifted from the signature
    #     pipeline. The sinc-kernel interpolation preserves stroke
    #     geometry better than bilinear (which a naive downstream
    #     resize-to-28×28 would otherwise apply).
    #   * Identity (h0 == h_target) — rare exact-match case.
    #   * Downscale (h0 > h_target) — happens when the row-band
    #     finder produces a tall band (covered by the oversized-crop
    #     gate downstream); still required since H is fixed.
    #
    # Log the upscale case at INFO so production logs surface the
    # "stretched + Lanczos" event the user expects to see — mirrors
    # the signature pipeline's ``stretched + Lanczos %dx to ~32px``
    # log. Downscale and identity stay at DEBUG to avoid log spam.
    try:
        h0 = int(rgb.shape[0])
        w0 = int(rgb.shape[1])
        if h0 != h_target:
            scale = h_target / max(1, h0)
            new_w = max(8, int(round(w0 * scale)))
            pil = Image.fromarray(rgb, mode="RGB").resize(
                (new_w, h_target), Image.LANCZOS,
            )
            resized = np.asarray(pil, dtype=np.uint8)
            if h0 < h_target:
                log.info(
                    "sc_ocr.hud: stretched + Lanczos to %dpx "
                    "(h=%d→%d, w=%d→%d) so crops match training-"
                    "sample crispness",
                    h_target, h0, h_target, w0, new_w,
                )
            else:
                log.debug(
                    "sc_ocr.hud: Lanczos downscale to %dpx "
                    "(h=%d→%d, w=%d→%d) — row band was oversized",
                    h_target, h0, h_target, w0, new_w,
                )
        else:
            resized = rgb
            log.debug(
                "sc_ocr.hud: CRNN input already at h_target=%d "
                "(no resize)", h_target,
            )
    except Exception as exc:
        log.debug("sc_ocr.hud: CRNN resize failed: %s", exc)
        return None

    # Per-channel polarity canonicalization (matches training).
    try:
        canon = np.empty_like(resized)
        for c in range(3):
            canon[..., c] = _canonicalize_polarity(resized[..., c])
    except Exception as exc:
        log.debug("sc_ocr.hud: CRNN polarity-canon failed: %s", exc)
        return None

    # Forward pass.
    try:
        x = canon.astype(np.float32).transpose(2, 0, 1)[None, ...] / 255.0
        in_name = sess.get_inputs()[0].name
        logits = sess.run(None, {in_name: x})[0]
    except Exception as exc:
        log.debug("sc_ocr.hud: CRNN inference failed: %s", exc)
        return None

    # ── Field-specific alphabet mask ──
    # Mirror of the signature CRNN's ``digit_only=True`` mask but
    # per-field. The HUD CRNN was trained on alphabet "0123456789.%"
    # but each field has a stricter valid character set:
    #   mass         → digits only (no '.', no '%')
    #   resistance   → digits + '%' (no '.')
    #   instability  → digits + '.' (no '%')
    # Zeroing the disallowed classes' softmax probabilities BEFORE
    # argmax prevents the CRNN from emitting impossible characters
    # on noisy inputs (e.g. reading a comma-aliased fragment as '%'
    # in the mass field).
    try:
        if logits.ndim == 3:
            lt = logits[:, 0, :]
        elif logits.ndim == 2:
            lt = logits
        else:
            return None
        shifted = lt - lt.max(axis=-1, keepdims=True)
        probs = np.exp(shifted)
        probs /= probs.sum(axis=-1, keepdims=True)
        # Build the per-field allow-mask. We always allow blank and
        # all digits; the special chars '.' / '%' are conditionally
        # allowed based on field.
        allow = [True] * (len(alphabet) + 1)
        if field == "mass":
            # Mask out '.' and '%'.
            for i, ch in enumerate(alphabet):
                if ch in ".%":
                    allow[i] = False
        elif field == "resistance":
            # Mask out '.'.
            for i, ch in enumerate(alphabet):
                if ch == ".":
                    allow[i] = False
        elif field == "instability":
            # Mask out '%'.
            for i, ch in enumerate(alphabet):
                if ch == "%":
                    allow[i] = False
        # Blank class is at the end (index = len(alphabet)); always
        # allowed. The mask array length matches probs.shape[-1].
        if probs.shape[-1] == len(allow):
            mask = np.asarray(allow, dtype=np.float32)
            probs = probs * mask[None, :]
            # Renormalize so confidences stay in [0, 1].
            s = probs.sum(axis=-1, keepdims=True)
            probs = np.where(s > 0, probs / np.maximum(s, 1e-12), probs)
        preds = probs.argmax(axis=-1)
        confs = probs.max(axis=-1)
    except Exception as exc:
        log.debug("sc_ocr.hud: CRNN decode failed: %s", exc)
        return None

    # ── Beam search + lexicon rerank (optional) ──
    # When ``beam_width > 0`` the caller wants alternative-path
    # decoding. Run prefix-beam-search on the (already field-masked)
    # softmax distributions, then rerank by per-field LEARNED LEXICON:
    # promote (never reject) the highest-mean-confidence in-lexicon
    # candidate; fall back to the greedy top when no candidate hits.
    #
    # Why this rerank flavor works when the earlier plausibility flavor
    # didn't: plausibility was a CONTINUOUS predicate (resistance 0-100,
    # instability 0-200, mass open-ended) — almost every digit string
    # passed it, so promotion was effectively random among confident
    # reads and regressed the benchmark by -0.8pp. Lexicon is a DISCRETE
    # predicate — the per-field LRU set of values the user has actually
    # confirmed this session via ``frozen_panel`` auto-freeze — so the
    # rerank only fires when there's a *specific* in-lexicon alternative
    # to a not-in-lexicon top. Mirrors the signature CRNN's beam+lexicon
    # win (664-value discrete chart-derived set).
    #
    # Empty-lexicon safety: ``_lexicon_rerank_candidates`` early-returns
    # the greedy top when the lexicon is empty, so cold-start installs
    # behave exactly like greedy. Promote-not-reject semantics: an
    # out-of-lexicon top is kept; we never drop confident reads just
    # because we haven't seen the value yet.
    if beam_width and beam_width >= 2:
        try:
            candidates = _prefix_beam_search_ctc(
                probs, blank, alphabet,
                beam_width=int(beam_width),
                top_k_return=int(beam_width),
            )
        except Exception as _beam_exc:
            log.debug(
                "sc_ocr.hud: beam search failed (%s) — falling back "
                "to greedy", _beam_exc,
            )
            candidates = []
        if candidates:
            try:
                picked, _rerank_info = _lexicon_rerank_candidates(
                    candidates, field,
                )
            except Exception as _rerank_exc:
                log.debug(
                    "sc_ocr.hud: lexicon rerank failed (%s) — "
                    "falling back to greedy", _rerank_exc,
                )
                picked, _rerank_info = None, {"changed": False}
            if picked is not None:
                # INFO log on lexicon-driven winner change, so the user
                # can see the rerank fire in production logs. No-op
                # cases (empty lexicon, no in-lexicon alternative,
                # greedy already in lexicon) log nothing.
                if _rerank_info.get("changed"):
                    g_text, g_mean = _rerank_info.get(
                        "greedy", (None, 0.0),
                    )
                    w_text, w_mean = _rerank_info.get(
                        "winner", (None, 0.0),
                    )
                    log.info(
                        "sc_ocr.hud: beam-lexicon rerank field=%s — "
                        "greedy=%r mean=%.2f → "
                        "lexicon-confirmed=%r mean=%.2f "
                        "(%d candidates, %d in lexicon)",
                        field, g_text, float(g_mean),
                        w_text, float(w_mean),
                        int(_rerank_info.get("n_candidates", 0)),
                        int(_rerank_info.get("n_in_lexicon", 0)),
                    )
                ptxt, _plp, pconf = picked
                return ptxt, pconf

    # Greedy CTC decode (default).
    out_chars: list[str] = []
    out_confs: list[float] = []
    prev = -1
    for t in range(int(preds.shape[0])):
        p = int(preds[t])
        if p == prev:
            prev = p
            continue
        prev = p
        if p == blank:
            continue
        if 0 <= p < len(alphabet):
            out_chars.append(alphabet[p])
            out_confs.append(float(confs[t]))
    if not out_chars:
        return None
    text = "".join(out_chars)
    mean_conf = float(sum(out_confs) / max(1, len(out_confs)))
    return text, mean_conf


def _hud_beam_rerank_plausible(
    candidates: list[tuple[str, float, float]],
    field: str,
    lp_margin: float = 1.5,
) -> Optional[tuple[str, float, float]]:
    """Rerank HUD beam-search candidates by plausibility.

    ``candidates``: list of ``(text, log_prob, geom_mean_conf)``
    pre-sorted by ``log_prob`` descending.

    Picks the highest-scoring candidate whose parsed value passes
    ``priors.is_plausible(field, value)``. If none are plausible,
    returns the overall highest-scoring candidate.

    Margin guard (``lp_margin``): only consider candidates whose
    ``log_prob`` is within ``lp_margin`` log-units of the top
    (~5x probability ratio at the default 1.5). Past the margin
    the top is so much more probable that promoting a lower-scored
    plausible alternative would trade a high-confidence read for
    a wrong-but-plausible one. Mirror of the signature CRNN's
    ``_beam_rerank_with_lexicon`` margin guard.
    """
    if not candidates:
        return None
    top_text, top_lp, top_mc = candidates[0]

    def _parse(text: str) -> Optional[float]:
        digits = "".join(c for c in text if c.isdigit() or c == ".")
        if not digits:
            return None
        try:
            return float(digits)
        except ValueError:
            return None

    def _plausible(text: str) -> bool:
        v = _parse(text)
        if v is None:
            return False
        try:
            from . import priors as _priors_rk
            ok, _ = _priors_rk.is_plausible(field, v, {})
            return bool(ok)
        except Exception:
            return False

    # Fast path: top is plausible already → no rerank needed.
    if _plausible(top_text):
        return candidates[0]
    # Look for the best plausible candidate within the lp margin.
    for cand in candidates:
        text, lp, _ = cand
        if (top_lp - lp) > lp_margin:
            break
        if _plausible(text):
            return cand
    return candidates[0]


def _lexicon_rerank_candidates(
    candidates: list[tuple[str, float, float]],
    field: str,
) -> tuple[Optional[tuple[str, float, float]], dict]:
    """Rerank HUD beam-search candidates by lexicon membership.

    ``candidates``: list of ``(text, log_prob, geom_mean_conf)``
    pre-sorted by ``log_prob`` descending (output of
    ``_prefix_beam_search_ctc``).

    Semantics — PROMOTE in-lexicon candidates, never REJECT
    out-of-lexicon ones:

    1. The greedy/highest-mean baseline winner is always ``candidates[0]``
       (the beam's top by joint log-probability is what greedy would
       have picked too on a non-degenerate distribution; if not, the
       caller passes us a beam list with mean-confidence tracked).
    2. For each candidate we parse its digit-string to a float using
       the SAME per-field convention the gate uses (digits + optional
       ``.`` collapsed to a single number; ``%`` is stripped).
    3. Candidates whose parse FAILS or whose parsed value is NOT in
       ``hud_lexicon.get_values(field)`` are skipped silently.
    4. Among the lexicon-confirmed survivors we pick the one with the
       HIGHEST geometric-mean confidence (not log-prob — mean conf is
       the quantity the downstream gate compares against its accept
       thresholds, so picking by mean-conf keeps the accept/reject
       boundary stable across rerank vs. greedy).
    5. If NO candidate is lexicon-confirmed (typical at cold start
       when the lexicon is empty), we return ``candidates[0]``
       unchanged — the rerank is a strict no-op in that case, so an
       empty lexicon cannot regress the greedy baseline.

    Why this works when the earlier plausibility-rerank didn't:
    plausibility was a CONTINUOUS predicate (resistance 0-100,
    instability 0-200, mass open-ended) — almost every CRNN read of
    any digit string lands inside it, so promotion was driven by the
    arbitrary tie between two confident reads. Lexicon membership is
    a DISCRETE predicate — a 100-entry-per-field LRU set of values
    the user has actually seen this session — so the rerank only
    fires when there's a *specific* in-lexicon alternative to a
    not-in-lexicon top candidate. Mirrors the signature CRNN's
    beam+lexicon win (664-value discrete chart-derived set).

    Returns ``(picked_candidate, info)`` where ``info`` is a dict
    with diagnostics for the call-site log line::

        {"n_candidates": <int>,
         "n_in_lexicon": <int>,
         "lex_size":     <int>,        # snapshot size of lexicon
         "greedy":       <(text, mean)>,   # the cand[0] baseline
         "winner":       <(text, mean)>,   # what we picked
         "changed":      <bool>}            # True iff winner != greedy

    Returns ``(None, info)`` when ``candidates`` is empty (caller
    falls back to greedy decode).
    """
    info: dict = {
        "n_candidates": 0,
        "n_in_lexicon": 0,
        "lex_size": 0,
        "greedy": None,
        "winner": None,
        "changed": False,
    }
    if not candidates:
        return None, info
    info["n_candidates"] = len(candidates)
    greedy = candidates[0]
    info["greedy"] = (greedy[0], float(greedy[2]))
    info["winner"] = info["greedy"]

    def _parse(text: str) -> Optional[float]:
        # SAME parse convention as the downstream gate:
        # digits + optional '.' (resistance's '%' is stripped — only
        # digits + '.' survive). Mass is digits-only so the dot doesn't
        # appear in legit reads; we still allow it through so a
        # malformed mass candidate doesn't crash.
        digits = "".join(c for c in text if c.isdigit() or c == ".")
        if not digits:
            return None
        try:
            return float(digits)
        except ValueError:
            return None

    # Snapshot the lexicon once. ``get_values`` returns a fresh set
    # so we can iterate safely without holding the module lock.
    try:
        from . import hud_lexicon as _hud_lex
        lex_values = _hud_lex.get_values(field)
    except Exception:
        lex_values = set()
    info["lex_size"] = len(lex_values)
    # Empty-lexicon early return — the rerank is a strict no-op so a
    # cold-start install can't regress the greedy baseline.
    if not lex_values:
        return greedy, info

    # Canonicalize lexicon values the same way the lexicon module
    # canonicalizes its keys: mass / resistance round to int, so a
    # candidate parse of 50.0 matches a lexicon entry stored as 50.
    # We do this on the *candidate* side rather than rebuilding the
    # lexicon set, because :func:`_hud_lex.is_known` is the source
    # of truth for canonical-form matching.
    in_lex_candidates: list[tuple[str, float, float]] = []
    for cand in candidates:
        text, _lp, _mc = cand
        v = _parse(text)
        if v is None:
            # Non-numeric candidate (e.g. empty after digit-filter) —
            # silently skip per spec.
            continue
        try:
            if _hud_lex.is_known(field, v):
                in_lex_candidates.append(cand)
        except Exception:
            # Lexicon query failed for some reason — treat as miss.
            continue
    info["n_in_lexicon"] = len(in_lex_candidates)

    if not in_lex_candidates:
        # No lexicon-confirmed candidate → keep greedy. Promote-only
        # semantics: never reject out-of-lexicon reads.
        return greedy, info

    # Pick the HIGHEST-MEAN-CONFIDENCE in-lexicon candidate. Among the
    # confirmed-good set, mean conf is the quality signal that lines
    # up with the downstream accept gate thresholds.
    winner = max(in_lex_candidates, key=lambda c: float(c[2]))
    info["winner"] = (winner[0], float(winner[2]))
    info["changed"] = winner[0] != greedy[0]
    return winner, info


def _forced_align_mean_conf(
    probs: np.ndarray,
    prefix: tuple[int, ...],
) -> float:
    """For a fixed character sequence ``prefix``, find the time-step
    assignment ``0 <= t_1 < t_2 < ... < t_n < T`` that maximizes the
    sum of log per-class softmax values, and return the geometric
    mean of ``probs[t_i][prefix[i]]``.

    This is the natural counterpart to the greedy decoder's
    "peak softmax at emission step" mean: each character is
    placed at the time step where its own class probability is
    largest (subject to monotonic ordering), and we average those
    peaks.

    O(T*n) dynamic program: ``best[i][t]`` = max sum-log-prob to
    place the first ``i+1`` characters using time steps ending at
    ``t``. We don't need the actual time assignments — just the
    geometric mean — so we keep only the running max and recover
    it via a simple backtrack at the end.
    """
    import math as _math

    n = len(prefix)
    if n == 0:
        return 0.0
    T, C = probs.shape
    if T < n:
        # Can't place n characters in T steps. Fall back to the
        # simple per-character max (ignoring monotonicity).
        confs = []
        for c in prefix:
            if 0 <= c < C:
                confs.append(float(probs[:, c].max()))
        if not confs:
            return 0.0
        log_sum = sum(_math.log(max(v, 1e-12)) for v in confs)
        return float(_math.exp(log_sum / len(confs)))

    eps = 1e-12
    # ``dp[i]`` holds the best sum-log-prob (over t in [i, T-n+i])
    # for placing the first i+1 chars; ``bt[i]`` holds the chosen
    # time step for char i along the best path.
    log_probs = np.log(np.clip(probs, eps, 1.0))
    # dp[t] = best sum-log-prob using char i at time t (or
    # -inf if infeasible).
    dp = np.full(T, -np.inf)
    parent = np.full((n, T), -1, dtype=np.int32)
    # Initialize char 0 at every t (must leave n-1 slots after).
    c0 = prefix[0]
    if not (0 <= c0 < C):
        return 0.0
    for t in range(T - (n - 1)):
        dp[t] = log_probs[t, c0]
    # Fill subsequent rows.
    for i in range(1, n):
        ci = prefix[i]
        if not (0 <= ci < C):
            return 0.0
        new_dp = np.full(T, -np.inf)
        # Running max of dp[0..t-1] so we can pick the best parent
        # in O(1) per t.
        best_so_far = -np.inf
        best_t = -1
        for t in range(T):
            if t > 0 and dp[t - 1] > best_so_far:
                best_so_far = dp[t - 1]
                best_t = t - 1
            if t >= i and t <= T - (n - i) and best_t >= 0:
                new_dp[t] = best_so_far + log_probs[t, ci]
                parent[i, t] = best_t
        dp = new_dp

    # Find best terminal t for char n-1 and backtrack.
    final_t = int(np.argmax(dp))
    chosen_ts: list[int] = [final_t]
    cur = final_t
    for i in range(n - 1, 0, -1):
        cur = int(parent[i, cur])
        chosen_ts.append(cur)
    chosen_ts.reverse()

    # Mean of per-emission probs at chosen time steps.
    if not chosen_ts:
        return 0.0
    confs = [float(probs[chosen_ts[i], prefix[i]]) for i in range(n)]
    log_sum = sum(_math.log(max(v, eps)) for v in confs)
    return float(_math.exp(log_sum / len(confs)))


def _prefix_beam_search_ctc(
    probs: np.ndarray,
    blank: int,
    alphabet: str,
    beam_width: int = 8,
    top_k_return: Optional[int] = None,
) -> list[tuple[str, float, float]]:
    """Prefix-beam-search CTC decoder.

    ``probs``: shape ``(T, C)`` per-time-step softmax distribution.
    ``blank``: blank class index.
    ``alphabet``: string indexed by class.
    ``beam_width``: number of prefixes kept at each time step.
    ``top_k_return``: number of final candidates to return (defaults
    to ``beam_width``).

    Returns a list of ``(prefix_text, log_prob, geom_mean_conf)``
    sorted by ``log_prob`` descending. ``log_prob`` is the standard
    CTC joint log-probability (``logaddexp(blank_end, non_blank_end)``)
    over all alignment paths producing the prefix. ``geom_mean_conf``
    is computed by a forced-alignment pass over ``probs`` after the
    beam converges (see ``_forced_align_mean_conf``): for each char
    in the surviving prefix, find the time step where that class
    has its highest probability (subject to monotonic ordering),
    and take the geometric mean of those peaks. This stays directly
    comparable to greedy decode's ``mean_conf`` semantics — and to
    the downstream 0.55/0.80 gate thresholds — without needing to
    track per-step bookkeeping inside the beam.

    Standard prefix-beam-search (Graves & Jaitly 2014). Maintains
    ``{prefix: (log_prob_blank, log_prob_non_blank)}``.

    Numerical stability: uses ``np.logaddexp`` and clips ``probs`` to
    a small floor to avoid ``log(0)``.
    """
    if probs is None or probs.size == 0:
        return []
    T, C = probs.shape
    if T < 1 or C < 1:
        return []
    if blank < 0 or blank >= C:
        # Defensive: caller should never pass an out-of-range blank,
        # but if it happens, treat last class as blank.
        blank = C - 1

    # Clip + log once. log(0) → -inf would poison logaddexp chains.
    eps = 1e-12
    log_probs = np.log(np.clip(probs, eps, 1.0))
    neg_inf = -1e30

    # State: prefix → (log_prob_blank_end, log_prob_non_blank_end).
    # Start with empty prefix; only blank-ending state is valid.
    beams: dict[tuple[int, ...], tuple[float, float]] = {
        tuple(): (0.0, neg_inf),
    }

    for t in range(T):
        # ``log_probs[t]`` is the per-class log-prob at this step.
        lp_t = log_probs[t]
        new_beams: dict[tuple[int, ...], tuple[float, float]] = {}

        # Iterate over the current beams and distribute their mass.
        for prefix, (pb, pnb) in beams.items():
            # 1) Stay on blank → prefix unchanged, ends in blank.
            #    Both blank-ending and non-blank-ending mass can extend
            #    to a blank step (i.e. emit nothing this step).
            blank_lp = lp_t[blank]
            cur_pb, cur_pnb = new_beams.get(prefix, (neg_inf, neg_inf))
            cand_pb = np.logaddexp(cur_pb, np.logaddexp(pb, pnb) + blank_lp)
            new_beams[prefix] = (cand_pb, cur_pnb)

            # For each non-blank class, two cases.
            for c in range(C):
                if c == blank:
                    continue
                c_lp = lp_t[c]

                if prefix and prefix[-1] == c:
                    # 2a) Same char as last → new emission via the
                    #     prev-blank state (the duplicate after blank).
                    new_prefix = prefix + (c,)
                    cur_pb2, cur_pnb2 = new_beams.get(
                        new_prefix, (neg_inf, neg_inf)
                    )
                    new_pnb_a = np.logaddexp(cur_pnb2, pb + c_lp)
                    new_beams[new_prefix] = (cur_pb2, new_pnb_a)

                    # 2b) Same char as last → prefix unchanged, ends
                    #     in non-blank. Prev-non-blank mass collapses
                    #     under the CTC repeat rule.
                    cur_pb3, cur_pnb3 = new_beams.get(
                        prefix, (neg_inf, neg_inf)
                    )
                    cand_pnb = np.logaddexp(cur_pnb3, pnb + c_lp)
                    new_beams[prefix] = (cur_pb3, cand_pnb)
                else:
                    # 3) Different char → new emission. Both blank-
                    #    ending and non-blank-ending mass extend.
                    new_prefix = prefix + (c,)
                    cur_pb4, cur_pnb4 = new_beams.get(
                        new_prefix, (neg_inf, neg_inf)
                    )
                    new_pnb = np.logaddexp(
                        cur_pnb4, np.logaddexp(pb, pnb) + c_lp
                    )
                    new_beams[new_prefix] = (cur_pb4, new_pnb)

        # Prune to top-K by total log-prob (logaddexp of blank+non-blank).
        if len(new_beams) > beam_width:
            scored = [
                (np.logaddexp(pb, pnb), prefix, pb, pnb)
                for prefix, (pb, pnb) in new_beams.items()
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            beams = {
                prefix: (pb, pnb)
                for (_score, prefix, pb, pnb) in scored[:beam_width]
            }
        else:
            beams = new_beams

    # Final ranking. Total log-prob is logaddexp(blank, non-blank).
    # Compute the forced-alignment mean confidence per surviving
    # prefix so callers get a value comparable to greedy's mean_conf.
    finals: list[tuple[str, float, float]] = []
    for prefix, (pb, pnb) in beams.items():
        total_lp = float(np.logaddexp(pb, pnb))
        text = "".join(alphabet[c] for c in prefix if 0 <= c < len(alphabet))
        geom_mean_conf = _forced_align_mean_conf(probs, prefix)
        finals.append((text, total_lp, geom_mean_conf))
    finals.sort(key=lambda x: x[1], reverse=True)

    if top_k_return is not None and top_k_return > 0:
        finals = finals[:top_k_return]
    return finals


def _beam_rerank_with_lexicon(
    candidates: list[tuple[str, float, float]],
    lexicon: Optional[set[int]],
    lex_min: int = 1000,
    lex_max: int = 35000,
    lex_margin: float = 1.5,
) -> Optional[tuple[str, float, float]]:
    """Rerank beam-search candidates by lexicon membership.

    ``candidates``: list of ``(text, log_prob, geom_mean_conf)``
    pre-sorted by ``log_prob`` descending.

    Picks the highest-scoring candidate whose digit-only integer is
    in ``lexicon`` AND in ``[lex_min, lex_max]``. Falls back to the
    overall highest-scoring candidate if none match. Returns
    ``None`` only when ``candidates`` is empty.

    Confidence-margin guard (``lex_margin``): only promote a non-top
    in-lexicon candidate if its log-probability is within
    ``lex_margin`` log-units of the top candidate's log-probability
    (~5x probability ratio at the default 1.5). Past that margin the
    top candidate is so much more probable than the rerank candidate
    that the model is essentially certain — promoting the lex-valid
    alternative would trade a high-confidence read for a lower-
    confidence one just because of lexicon coverage.

    Concrete failure mode this prevents (cap_20260425_095113_320,
    gt=11585): greedy / beam-top reads "11585" at 0.98 conf,
    off-lex (chart-export gap, 11585 not in chart or verified
    cache). Beam rank-2 reads "11565" at 0.56 conf, in-lex (close
    match in the chart). Without the margin guard the rerank
    promotes 11565 → production accepts a wrong-but-lex-valid read.
    With ``lex_margin=1.5`` the gap from top is too wide to promote,
    so we keep 11585 → gate falls through (off-lex) but we don't
    actively *replace* a correct read with a wrong one.
    """
    if not candidates:
        return None
    if not lexicon:
        return candidates[0]
    top_text, top_lp, _ = candidates[0]
    # If the top itself is lexicon-valid, no rerank needed.
    top_digits = "".join(c for c in top_text if c.isdigit())
    if top_digits:
        try:
            top_v = int(top_digits)
            if lex_min <= top_v <= lex_max and top_v in lexicon:
                return candidates[0]
        except (TypeError, ValueError):
            pass
    # Look for the best in-lex candidate within the margin.
    for text, lp, mc in candidates:
        if (top_lp - lp) > lex_margin:
            # Past the margin — top is too clearly better than any
            # remaining lex-valid alternative. Stop and keep top.
            break
        digits = "".join(c for c in text if c.isdigit())
        if not digits:
            continue
        try:
            v = int(digits)
        except (TypeError, ValueError):
            continue
        if lex_min <= v <= lex_max and v in lexicon:
            return (text, lp, mc)
    return candidates[0]


def _classify_signal_via_crnn_rgb(
    work_rgb: np.ndarray,
    return_alignment: bool = False,
    beam_width: int = 0,
    lexicon: Optional[set[int]] = None,
):
    """Run the SC mining-signature RGB CRNN on a row-isolated RGB
    work crop and return ``(digit_string, mean_confidence)`` — or, when
    ``return_alignment=True``, ``(digit_string, mean_confidence,
    alignment_info)``.

    Preprocessing mirrors training exactly:
      1. Lanczos resize to height=48 preserving aspect.
      2. Polarity-canonicalize per channel via ``_canonicalize_polarity``.
      3. Normalize to float32 in [0, 1].
      4. Forward pass through ``model_signal_crnn_rgb.onnx``.
      5. CTC decode:
           * ``beam_width <= 0`` (default) → greedy decode — collapse
             consecutive same-class predictions, drop blanks.
           * ``beam_width >= 2`` → prefix-beam-search with width K.
             If ``lexicon`` is provided, the final top-K candidates
             are reranked: prefer the highest-scoring candidate whose
             digit-only integer is in ``lexicon`` AND in
             ``[1000, 35000]``; otherwise fall back to the highest-
             scoring candidate (matches greedy behavior on lexicon-
             miss).

    When ``return_alignment=True`` the decoder ALWAYS falls back to
    greedy. The alignment-box extraction uses argmax runs and isn't
    well-defined for beam-search paths — and no current caller needs
    beam-search alignment.

    Returns ``None`` when:
      * the ONNX model isn't on disk yet,
      * onnxruntime / inference fails,
      * the input strip is too narrow / tall to be meaningful.

    The caller (``_signal_recognize_pil``) gates the returned digit
    string by length + numeric range + confidence before accepting.
    Bad reads (low conf, wrong-length, out-of-range) fall through to
    the existing per-glyph gates.

    Alignment info (only populated when ``return_alignment=True``)::

        {
            "resized_w":   <int>,        # width of the H=48-resized strip
            "resized_h":   <int>,        # 48
            "input_w":     <int>,        # original ``work_rgb`` width
            "stride":      4,            # CRNN's width downsampling factor
            "boxes": [
                {"char": "1",
                 "t_start": <int>,        # first CTC time step in input space
                 "t_end":   <int>,        # last time step
                 "x_start": <int>,        # left pixel in RESIZED strip
                 "x_end":   <int>,        # right pixel in RESIZED strip
                 "x_start_input": <int>,  # left pixel in ORIGINAL work_rgb
                 "x_end_input":   <int>,  # right pixel in ORIGINAL work_rgb
                 "conf":   <float>},     # mean confidence over the span
                ...
            ],
        }

    Each box is the CTC-collapsed character's time-step span mapped
    back to input pixel columns via the CRNN's width stride of 4
    (two 2x2 max-pools + a 2x2 no-pad conv7 that trims 1 column).
    Boxes are returned in both the resized (H=48) coordinate space the
    model actually saw, and the original ``work_rgb`` space the caller
    can use to crop directly.
    """
    if work_rgb is None or work_rgb.size == 0:
        return None
    if work_rgb.ndim != 3 or work_rgb.shape[2] != 3:
        return None
    if work_rgb.shape[0] < 4 or work_rgb.shape[1] < 8:
        return None

    global _SIGNAL_CRNN_RGB_SESSION
    global _SIGNAL_CRNN_RGB_ALPHABET
    global _SIGNAL_CRNN_RGB_BLANK
    global _SIGNAL_CRNN_RGB_H_TARGET

    # Lazy-load the ONNX session + metadata once per process.
    if _SIGNAL_CRNN_RGB_SESSION is None:
        try:
            import onnxruntime as _ort  # noqa: F401
        except Exception as exc:
            log.debug("api: signal CRNN onnxruntime missing: %s", exc)
            return None
        try:
            from pathlib import Path as _Path
            _models = _Path(__file__).resolve().parent.parent / "models"
            _onnx = _models / "model_signal_crnn_rgb.onnx"
            _meta = _models / "model_signal_crnn_rgb.json"
            if not _onnx.is_file() or not _meta.is_file():
                log.debug(
                    "api: signal CRNN files not found (%s / %s)",
                    _onnx.name, _meta.name,
                )
                return None
            import json as _json
            meta = _json.loads(_meta.read_text(encoding="utf-8"))
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = 1
            _opts.inter_op_num_threads = 1
            _SIGNAL_CRNN_RGB_SESSION = _ort.InferenceSession(
                str(_onnx), sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            _SIGNAL_CRNN_RGB_ALPHABET = str(meta.get("alphabet", "0123456789,"))
            _SIGNAL_CRNN_RGB_BLANK = int(meta.get("blank_idx", len(_SIGNAL_CRNN_RGB_ALPHABET)))
            shape = meta.get("input_shape") or [None, 3, 48, None]
            try:
                _SIGNAL_CRNN_RGB_H_TARGET = int(shape[2]) if shape[2] else 48
            except Exception:
                _SIGNAL_CRNN_RGB_H_TARGET = 48
            log.info(
                "sc_ocr.signal: loaded RGB CRNN (alphabet=%r blank=%d H=%d)",
                _SIGNAL_CRNN_RGB_ALPHABET, _SIGNAL_CRNN_RGB_BLANK,
                _SIGNAL_CRNN_RGB_H_TARGET,
            )
        except Exception as exc:
            log.debug("api: signal CRNN load failed: %s", exc)
            return None

    sess = _SIGNAL_CRNN_RGB_SESSION
    alphabet = _SIGNAL_CRNN_RGB_ALPHABET or "0123456789,"
    blank = _SIGNAL_CRNN_RGB_BLANK if _SIGNAL_CRNN_RGB_BLANK >= 0 else len(alphabet)
    h_target = _SIGNAL_CRNN_RGB_H_TARGET

    # Preprocess: Lanczos resize to h_target preserving aspect.
    try:
        h0 = int(work_rgb.shape[0])
        if h0 != h_target:
            scale = h_target / max(1, h0)
            new_w = max(8, int(round(work_rgb.shape[1] * scale)))
            pil = Image.fromarray(work_rgb, mode="RGB").resize(
                (new_w, h_target), Image.LANCZOS,
            )
            resized = np.asarray(pil, dtype=np.uint8)
        else:
            resized = work_rgb
    except Exception as exc:
        log.debug("sc_ocr.signal: CRNN resize failed: %s", exc)
        return None

    # Polarity-canonicalize each channel separately. Training applied
    # ``_canonicalize_polarity`` per channel so the model sees bright-
    # glyphs-on-dark-bg regardless of original capture polarity.
    try:
        canon = np.empty_like(resized)
        for c in range(3):
            canon[..., c] = _canonicalize_polarity(resized[..., c])
    except Exception as exc:
        log.debug("sc_ocr.signal: CRNN polarity-canon failed: %s", exc)
        return None

    # Reshape to (1, 3, H, W) float32 in [0, 1].
    try:
        x = canon.astype(np.float32).transpose(2, 0, 1)[None, ...] / 255.0
        in_name = sess.get_inputs()[0].name
        logits = sess.run(None, {in_name: x})[0]
    except Exception as exc:
        log.debug("sc_ocr.signal: CRNN inference failed: %s", exc)
        return None

    # logits shape: (T, B=1, C). Softmax over C for per-time-step
    # probability distributions, argmax for the predicted class at
    # each step.
    try:
        if logits.ndim == 3:
            lt = logits[:, 0, :]
        elif logits.ndim == 2:
            lt = logits
        else:
            return None
        shifted = lt - lt.max(axis=-1, keepdims=True)
        probs = np.exp(shifted)
        probs /= probs.sum(axis=-1, keepdims=True)
        preds = probs.argmax(axis=-1)
        confs = probs.max(axis=-1)
    except Exception as exc:
        log.debug("sc_ocr.signal: CRNN decode failed: %s", exc)
        return None

    # ── Prefix-beam-search CTC + optional lexicon rerank ─────────────
    # Beam search is opt-in via ``beam_width >= 2``. We can only take
    # this path when the caller does NOT need alignment boxes; the
    # alignment logic below uses argmax-run spans which aren't
    # well-defined for beam paths. Existing callers (no opt-in) hit
    # the unchanged greedy path below.
    if beam_width and beam_width >= 2 and not return_alignment:
        try:
            beam_candidates = _prefix_beam_search_ctc(
                probs, blank, alphabet,
                beam_width=int(beam_width),
                top_k_return=int(beam_width),
            )
        except Exception as exc:
            log.debug(
                "sc_ocr.signal: CRNN beam-search failed (%s) — "
                "falling back to greedy decode", exc,
            )
            beam_candidates = []
        if beam_candidates:
            picked = _beam_rerank_with_lexicon(
                beam_candidates,
                lexicon=lexicon,
            )
            if picked is not None:
                pick_text, _pick_lp, pick_mean = picked
                if pick_text:
                    return pick_text, float(pick_mean)
                # Empty pick (rare): fall through to greedy.

    # Greedy CTC decode: collapse consecutive same-class predictions,
    # drop blank class. Track per-emitted-character confidence AND
    # the time-step span each emission covers, so callers that want
    # to know where each glyph lives in the input can map back via
    # the CRNN's width stride.
    #
    # The "span" of an emitted character is defined as the contiguous
    # run of time steps where the argmax stayed on that class — i.e.
    # from when the model first predicted it (after a blank or another
    # class) up through the last step it stayed on that prediction.
    # We open a span at the start of a non-blank run and close it when
    # the prediction changes (or at the end of the sequence).
    out_chars: list[str] = []
    out_confs: list[float] = []
    out_spans: list[tuple[int, int, float]] = []  # (t_start, t_end_inclusive, mean_conf)
    T = int(preds.shape[0])
    prev = -1
    cur_t_start: Optional[int] = None
    cur_class: Optional[int] = None
    cur_confs: list[float] = []
    for t in range(T):
        p = int(preds[t])
        if p != prev:
            # Boundary: close out any in-flight non-blank span.
            if cur_class is not None and cur_class != blank and cur_t_start is not None:
                mc = float(sum(cur_confs) / max(1, len(cur_confs)))
                out_spans.append((cur_t_start, t - 1, mc))
                out_chars.append(alphabet[cur_class])
                out_confs.append(mc)
            # Start a new span (regardless of class — blank-runs are
            # tracked so cur_t_start stays correct when we transition
            # back to a class).
            cur_t_start = t
            cur_class = p
            cur_confs = [float(confs[t])]
        else:
            if cur_confs is not None:
                cur_confs.append(float(confs[t]))
        prev = p
    # Close any trailing non-blank run.
    if cur_class is not None and cur_class != blank and cur_t_start is not None:
        mc = float(sum(cur_confs) / max(1, len(cur_confs)))
        out_spans.append((cur_t_start, T - 1, mc))
        out_chars.append(alphabet[cur_class])
        out_confs.append(mc)

    if not out_chars:
        return None
    text = "".join(out_chars)
    mean_conf = float(sum(out_confs) / max(1, len(out_confs)))

    if not return_alignment:
        return text, mean_conf

    # Width stride = 4 (two 2x2 max-pools on the width axis in the
    # CRNN backbone). Conv7's 2x2 no-pad kernel trims one column off
    # the trailing edge so the effective time-step index ``t`` maps
    # to resized-strip pixel range ``[t*4, (t+1)*4)`` — a window 4
    # input pixels wide centered at ``t*4 + 2``.
    #
    # See ``time_dim_for_width`` in ``ocr/train_signal_crnn_rgb.py``
    # for the formula ``T = w//4 - 1`` this inverts.
    #
    # Each emission's *argmax run* is typically 1-2 time steps (the
    # model spikes then blanks), so the raw spans are way narrower
    # than the actual glyph width. Voronoi-tile across time-step
    # centers to get natural glyph extents:
    #   * first emission extends from t=0 to the midpoint with the
    #     next emission's center.
    #   * each middle emission spans from the midpoint with the
    #     previous emission's center to the midpoint with the next.
    #   * last emission extends from the midpoint with the previous
    #     to t=T-1.
    # This matches the segmenter's notion of glyph bounding boxes
    # tiling the strip without gaps.
    stride = 4
    resized_w = int(canon.shape[1])
    resized_h = int(canon.shape[0])
    input_w = int(work_rgb.shape[1])
    # Original ``work_rgb`` width may differ from the H=48-resized
    # ``canon`` width when the upstream Lanczos changed scale (h0 !=
    # h_target). Compute the back-scale once so callers can crop on
    # either coordinate system.
    if resized_w > 0:
        back_scale = input_w / resized_w
    else:
        back_scale = 1.0

    # Compute the time-step center of each emission as the midpoint
    # of its argmax run.
    centers_t = [
        (t_s + t_e) / 2.0 for (t_s, t_e, _) in out_spans
    ]
    n = len(centers_t)
    # Voronoi-expanded box bounds in time-step space.
    t_lo: list[float] = []
    t_hi: list[float] = []
    for i in range(n):
        if i == 0:
            t_lo.append(0.0)
        else:
            t_lo.append((centers_t[i - 1] + centers_t[i]) / 2.0)
        if i == n - 1:
            t_hi.append(float(T - 1))
        else:
            t_hi.append((centers_t[i] + centers_t[i + 1]) / 2.0)

    boxes: list[dict] = []
    for i, (ch, (t_start, t_end, mc)) in enumerate(zip(out_chars, out_spans)):
        # ``x_start`` / ``x_end`` are the Voronoi-expanded bounds in
        # resized-strip pixel space. ``t_start`` / ``t_end`` are kept
        # as the original argmax-run bounds for diagnostic purposes.
        x_start = max(0, int(round(t_lo[i] * stride)))
        x_end = min(resized_w, int(round((t_hi[i] + 1.0) * stride)))
        # Also report the narrow argmax-run-only span so callers can
        # see where the model was MOST confident the digit lives —
        # useful when comparing CTC alignment to the segmenter's
        # boxes.
        x_peak_start = max(0, int(t_start * stride))
        x_peak_end = min(resized_w, int((t_end + 1) * stride))
        boxes.append({
            "char": ch,
            "t_start": int(t_start),
            "t_end": int(t_end),
            "x_start": x_start,
            "x_end": x_end,
            "x_start_input": int(round(x_start * back_scale)),
            "x_end_input": int(round(x_end * back_scale)),
            "x_peak_start": x_peak_start,
            "x_peak_end": x_peak_end,
            "conf": float(mc),
        })
    alignment = {
        "resized_w": resized_w,
        "resized_h": resized_h,
        "input_w": input_w,
        "stride": stride,
        "boxes": boxes,
    }
    return text, mean_conf, alignment


def _crnn_decode(
    session, classes: str, blank: int,
    value_crop: Image.Image, h_target: int,
    digit_only: bool = False,
) -> Optional[tuple[str, list[float]]]:
    """Run a single CRNN at a single scale and greedy-decode.

    Shared body for both the primary and optional v2 CRNN; caller
    passes the session + vocabulary + blank index to use.

    ``digit_only=True`` masks every non-digit, non-blank class index
    out of the logits before the argmax so the greedy decoder cannot
    produce letters. Use this for callers (e.g. the signal-value
    pipeline) that read pure-digit fields against this multi-purpose
    CRNN model — without the mask the model can hallucinate alphabet
    glyphs (``'HIP20'``, ``'Cu'``) on noisy inputs because its
    training set spanned both digits and letters. Mineral-name
    callers must keep the default ``False``.
    """
    gray = np.array(value_crop.convert("L"), dtype=np.uint8)
    if gray.size == 0:
        return None

    if float(np.median(gray)) > 140:
        gray = 255 - gray

    H, W = gray.shape
    w_new = max(16, int(round(W * h_target / max(1, H))))
    resized = np.array(
        Image.fromarray(gray).resize((w_new, h_target), Image.BILINEAR),
        dtype=np.float32,
    ) / 255.0

    inp = resized.reshape(1, 1, h_target, w_new).astype(np.float32)
    try:
        inp_name = session.get_inputs()[0].name
        logits = session.run(None, {inp_name: inp})[0]
    except Exception as exc:
        log.debug("sc_ocr: CRNN inference failed at h=%d: %s", h_target, exc)
        return None

    if logits.ndim == 3:
        logits_tc = logits[:, 0, :]
    elif logits.ndim == 2:
        logits_tc = logits
    else:
        return None

    # Optional digit-only mask: zero the probability of every class
    # position that is neither a digit nor the blank, so argmax can
    # only land on digits or blank. Mask is applied to the raw logits
    # by setting non-kept positions to -inf, which becomes 0 after
    # softmax (numerically stable).
    if digit_only:
        n_classes = logits_tc.shape[-1]
        keep_mask = np.zeros(n_classes, dtype=bool)
        if 0 <= blank < n_classes:
            keep_mask[blank] = True
        for _i, _c in enumerate(classes):
            if _i >= n_classes:
                break
            if _c.isdigit():
                keep_mask[_i] = True
        # Use np.float32 -inf so the dtype matches logits_tc.
        masked = np.where(keep_mask, logits_tc, np.float32(-np.inf))
        logits_tc = masked

    shifted = logits_tc - logits_tc.max(axis=-1, keepdims=True)
    probs = np.exp(shifted)
    probs /= probs.sum(axis=-1, keepdims=True)
    preds = probs.argmax(axis=-1)
    confs = probs.max(axis=-1)

    text_chars: list[str] = []
    per_char: list[float] = []
    prev = -1
    for t in range(len(preds)):
        p = int(preds[t])
        if p != prev and p != blank and 0 <= p < len(classes):
            text_chars.append(classes[p])
            per_char.append(float(confs[t]))
        prev = p
    return "".join(text_chars), per_char


def _crnn_recognize_single(
    value_crop: Image.Image, h_target: int,
    digit_only: bool = False,
) -> Optional[tuple[str, list[float]]]:
    """Primary-CRNN single-scale pass. Kept as a thin wrapper over
    ``_crnn_decode`` so callers that only want the primary model
    don't need to know about the ensemble partner.

    ``digit_only`` is forwarded to ``_crnn_decode`` so digit-only
    callers (signature value pipeline) can't hit the alphabet-leak
    path on borderline crops. Without this, ``_crnn_recognize`` was
    falling back to this single-pass when every multi-scale probe
    returned empty — and that fallback was unconstrained, so the
    multi-purpose CRNN happily produced strings like ``'Activatatte'``
    on noisy signature crops, which the live viewer then rendered as
    the SIGNATURE (CRNN) read.
    """
    if not fallback._ensure_crnn_model():
        return None
    return _crnn_decode(
        fallback._crnn_session,
        fallback._crnn_classes,
        fallback._crnn_blank_idx,
        value_crop, h_target,
        digit_only=digit_only,
    )


def _crnn_recognize(
    value_crop: Image.Image,
    digit_only: bool = False,
) -> Optional[tuple[str, list[float]]]:
    """End-to-end CRNN read with multi-scale + multi-model ensembling.

    Runs inference at 3 different input heights (base−8, base, base+16)
    on EACH available CRNN (primary + optional v2 partner) and picks
    the read with the highest mean confidence across all candidates.

    Two-model ensemble is the default when ``model_crnn_v2.onnx``
    exists on disk; otherwise falls back to single-model multi-scale.
    The v2 partner is expected to have been trained with different
    init / augmentation than the primary so their errors decorrelate.

    ``digit_only=True`` is forwarded to ``_crnn_decode`` and constrains
    every recognizer in the ensemble to digit-only class outputs. Use
    this from pure-digit callers (signal value, HUD numerics) where
    the model's letter vocabulary would otherwise produce
    ``'HIP20'``-style hallucinations on noisy inputs.
    """
    if not fallback._ensure_crnn_model():
        return None

    base_h = int(fallback._crnn_input_height)
    # The shipped ONNX models were exported with height FIXED (only
    # batch + width are dynamic axes), so off-scale probes at
    # base_h-8 / base_h+16 fail with INVALID_ARGUMENT and are silently
    # dropped. Restrict to base_h to stop the wasted inference calls
    # and log noise. A future retrain with dynamic height could re-
    # introduce the multi-scale ensemble; guard via model metadata.
    scales = [base_h]

    # Assemble the list of (session, classes, blank, tag) pairs. Each
    # is a complete recognizer; we probe each at each scale.
    recognizers: list[tuple[object, str, int, str]] = [
        (fallback._crnn_session, fallback._crnn_classes,
         fallback._crnn_blank_idx, "v1"),
    ]
    if fallback._ensure_crnn2_model():
        recognizers.append((
            fallback._crnn2_session, fallback._crnn2_classes,
            fallback._crnn2_blank_idx, "v2",
        ))

    candidates: list[tuple[str, list[float], str, int]] = []  # +tag, +scale
    for sess, classes, blank, tag in recognizers:
        for h in scales:
            r = _crnn_decode(
                sess, classes, blank, value_crop, h,
                digit_only=digit_only,
            )
            if r is not None and r[0]:
                candidates.append((r[0], r[1], tag, h))

    if not candidates:
        # Every probe returned empty — fall back to a single primary
        # pass at base height (may still return empty; caller handles).
        # Forward ``digit_only`` so the fallback honours the same
        # constraint as the ensemble above; otherwise the multi-purpose
        # CRNN produces alphabet strings (``'Activatatte'``,
        # ``'HIP20'``) that surface in the live viewer's SIGNATURE
        # (CRNN) row even though the caller's downstream digit filter
        # strips them before validation.
        r = _crnn_recognize_single(
            value_crop, base_h, digit_only=digit_only,
        )
        return r

    # Rank by mean confidence, ties broken by length (more preserved
    # chars ⇒ CTC collapsed correctly at that scale).
    def _score(item):
        _text, confs, _tag, _h = item
        mean = sum(confs) / len(confs) if confs else 0.0
        return (mean, len(_text))
    candidates.sort(key=_score, reverse=True)
    winner = candidates[0]
    # Audit logging only when >1 recognizer actually ran, to keep the
    # single-model case quiet.
    if len(recognizers) > 1:
        _wtxt, _wconfs, _wtag, _wh = winner
        _wmean = sum(_wconfs) / len(_wconfs) if _wconfs else 0.0
        log.debug(
            "sc_ocr: crnn-ensemble winner=%s@h%d text=%r mean=%.2f "
            "(ncand=%d)", _wtag, _wh, _wtxt, _wmean, len(candidates),
        )
    return winner[0], winner[1]




def _try_tesseract_eng_sc(value_crop: Image.Image) -> str:
    """Primary Tesseract read using eng_sc + 3x+ upscale.

    Extracted from the main body of ``_ocr_value_crop`` so it can be
    called ahead of CRNN for digit-only fields. Returns "" on any
    failure (eng_sc model missing, pytesseract not installed, crop
    dimensions invalid, etc.).
    """
    try:
        import pytesseract
        from ..screen_reader import _check_tesseract
        _check_tesseract()
    except Exception as exc:
        log.debug("api: _try_tesseract_eng_sc swallowed: %s", exc)
        return ""

    _tessdata_local = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "ocr", "tessdata",
    )
    _have_sc = os.path.isfile(os.path.join(_tessdata_local, "eng_sc.traineddata"))
    if not _have_sc:
        return ""

    W, H = value_crop.size
    shortest = min(W, H)
    if shortest < 80:
        scale = max(3, 100 // max(1, shortest))
        tess_input = value_crop.resize((W * scale, H * scale), Image.LANCZOS)
    else:
        tess_input = value_crop

    # Pad with a polarity-appropriate border. Tesseract's PSM 7/8 both
    # need "quiet space" around the text to lock onto a baseline; a
    # tightly-cropped number sometimes returns empty when the text
    # touches the image edge. Use the image's corner pixel as the fill
    # color to keep polarity consistent with the crop.
    _pad = max(16, tess_input.height // 4)
    _corner = tess_input.getpixel((0, 0))
    if isinstance(_corner, tuple):
        _bg = _corner
    else:
        _bg = _corner  # grayscale int
    try:
        from PIL import ImageOps as _ImageOps
        tess_input = _ImageOps.expand(tess_input, border=_pad, fill=_bg)
    except Exception as exc:
        log.debug("api: _try_tesseract_eng_sc swallowed: %s", exc)

    def _run(psm: int) -> str:
        try:
            return pytesseract.image_to_string(
                tess_input,
                config=(
                    f"-l eng_sc --psm {psm} "
                    "-c tessedit_char_whitelist=0123456789.%"
                ),
            ).strip()
        except Exception as exc:
            log.debug("api: _run swallowed: %s", exc)
            return ""

    with _TESSDATA_LOCK:
        prev_env = os.environ.get("TESSDATA_PREFIX")
        os.environ["TESSDATA_PREFIX"] = _tessdata_local
        try:
            # PSM 7 first (single text line, best when the crop is already
            # line-shaped). Fall back to PSM 8 (single word) which is more
            # forgiving when PSM 7 can't find a baseline.
            text = _run(7)
            if not text:
                text = _run(8)
        finally:
            if prev_env is None:
                os.environ.pop("TESSDATA_PREFIX", None)
            else:
                os.environ["TESSDATA_PREFIX"] = prev_env
    return text


def _parallel_vote(
    field: str,
    crnn_text: str,
    crnn_confs: list[float],
    tess_text: str,
) -> Optional[tuple[str, list[float]]]:
    """Field-aware voter between a CRNN read and a Tesseract read.

    Both engines run every scan (not a cascade) and their outputs are
    reconciled here. Returns the winning (text, confs) pair or None
    to indicate no confident agreement (caller should fall through to
    the ONNX segmenter).

    Decision rules (in order):
      1. Both empty → None (caller falls through).
      2. Only one produced text → use that one.
      3. Texts are identical after stripping non-digit/./% → return
         that text with CRNN's confidences (or fabricated 0.95 if
         only Tesseract spoke).
      4. Disagree: apply field-specific validity filters. For
         percentages anything > 100 is invalid; for instability,
         > 200 is suspicious. The read that passes its field's
         sanity check wins. If both pass (or neither passes), prefer
         CRNN when mean conf ≥ 0.80 else prefer Tesseract (eng_sc
         is generally more stable on digit-only HUD text at small
         sizes).
    """
    def _digits_only(s: str) -> str:
        return "".join(c for c in s if c in "0123456789.%")

    c_norm = _digits_only(crnn_text or "")
    t_norm = _digits_only(tess_text or "")

    if not c_norm and not t_norm:
        return None
    if c_norm and not t_norm:
        return c_norm, crnn_confs or [0.85] * len(c_norm)
    if t_norm and not c_norm:
        return t_norm, [0.9] * len(t_norm)
    if c_norm == t_norm:
        return c_norm, crnn_confs or [0.95] * len(c_norm)

    # Disagreement — apply field sanity checks.
    def _field_ok(s: str) -> bool:
        try:
            if field == "resistance":
                v = float(s.replace("%", "")) if s else -1.0
                return 0.0 <= v <= 100.0
            if field == "instability":
                v = float(s) if s and "%" not in s else -1.0
                return 0.0 <= v <= 10000.0
            if field == "mass":
                v = float(s) if s and "%" not in s else -1.0
                return 0.1 <= v <= 10_000_000.0
        except ValueError:
            return False
        return True

    c_ok = _field_ok(c_norm)
    t_ok = _field_ok(t_norm)
    if c_ok and not t_ok:
        return c_norm, crnn_confs or [0.85] * len(c_norm)
    if t_ok and not c_ok:
        return t_norm, [0.9] * len(t_norm)

    # Both pass (or both fail) — prefer Tesseract eng_sc. The shipped
    # CRNN (47% val snapshot) is poorly calibrated: it frequently hits
    # 0.85–0.90 mean confidence on wrong reads. eng_sc is SC-Datarunner
    # trained on the actual HUD font and reads live crops reliably.
    # Only override Tesseract when CRNN is VERY confident (≥0.95) AND
    # the CRNN read has more digits (usually indicates Tesseract chopped
    # a leading digit). This keeps the ensemble benefit without letting
    # overconfident CRNN hallucinations dominate. Retraining the CRNN
    # re-calibrates its confidences — this threshold can drop to 0.85
    # once the model hits >80% val and its confidence distribution
    # becomes reliable.
    mean_c = (sum(crnn_confs) / len(crnn_confs)) if crnn_confs else 0.0
    c_longer = len(c_norm) > len(t_norm)
    crnn_wins = mean_c >= 0.95 and c_longer
    log.debug(
        "sc_ocr: vote-disagree field=%s crnn=%r(%.2f,%d) tess=%r(%d) -> %s",
        field, c_norm, mean_c, len(c_norm),
        t_norm, len(t_norm),
        "crnn" if crnn_wins else "tess",
    )
    if crnn_wins:
        return c_norm, crnn_confs
    return t_norm, [0.9] * len(t_norm)


_FULL_ROW_DEBUG_SAVED: dict[str, bool] = {}


def _ocr_full_row(
    img: Image.Image, y1: int, y2: int, field: str,
) -> tuple[str, list[float]]:
    """OCR the full row (label + value) and extract the trailing number.

    Robust against label-right-edge mis-detection because we don't
    need to know WHERE the value starts — we let the label itself
    serve as a baseline anchor for Tesseract, then regex out the
    trailing numeric token after decode.

    Pipeline:
      1. Crop ``img[y1_pad:y2_pad, 0:W]`` — full panel width.
      2. Polarity-correct + upscale if small + border-pad.
      3. Run Tesseract eng_sc with NO char whitelist (letters must
         decode properly so "MASS:", "RESISTANCE:", "INSTABILITY:"
         anchor the line).
      4. Regex: find all ``\\d[\\d.,]*%?`` tokens, return rightmost.

    Returns (text, confidences) or ("", []) on any failure. Caller
    falls through to ``_ocr_value_crop`` for a second opinion.
    """
    try:
        import pytesseract
        from ..screen_reader import _check_tesseract
        _check_tesseract()
    except Exception as exc:
        log.debug("api: _ocr_full_row swallowed: %s", exc)
        return "", []

    _tessdata = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "ocr", "tessdata",
    )
    if not os.path.isfile(os.path.join(_tessdata, "eng_sc.traineddata")):
        return "", []

    # Full-width row crop with small vertical padding.
    y1_p = max(0, y1 - 2)
    y2_p = min(img.height, y2 + 2)
    row = img.crop((0, y1_p, img.width, y2_p))

    # Polarity correction: Tesseract prefers dark-text-on-light.
    gray = np.array(row.convert("L"), dtype=np.uint8)
    if float(np.median(gray)) < 130:
        row = Image.fromarray(255 - gray)

    # Contrast stretch. Pull min/max to full [0, 255] range so
    # thin-stroke digits on dim backgrounds get Tesseract's full
    # signal. No-op when the row already uses full range.
    try:
        _arr = np.array(row.convert("L"), dtype=np.float32)
        _mn, _mx = float(_arr.min()), float(_arr.max())
        if _mx - _mn > 10:
            _arr = (_arr - _mn) * (255.0 / (_mx - _mn))
            row = Image.fromarray(np.clip(_arr, 0, 255).astype(np.uint8))
    except Exception as exc:
        log.debug("api: _ocr_full_row swallowed: %s", exc)

    # Upscale small rows to ~120 px tall so Tesseract's LSTM has
    # plenty of room even on extraction-mode tiny panels (rows as
    # small as 20 px get a full 6× upscale → 120 px). Bumped from
    # the previous 80-px target because 80 was marginal on thin
    # digits rendered at small native size.
    W, H = row.size
    if H < 100:
        scale = max(3, 120 // max(1, H))
        row = row.resize((W * scale, H * scale), Image.LANCZOS)

    # Unsharp-mask after upscale to restore stroke sharpness lost
    # during interpolation. Modest strength — heavier values hurt
    # more than help because they exaggerate anti-aliasing artifacts.
    try:
        from PIL import ImageFilter as _ImageFilter
        row = row.filter(_ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=2))
    except Exception as exc:
        log.debug("api: _ocr_full_row swallowed: %s", exc)

    # Border pad so PSM 7 can lock onto a baseline.
    try:
        from PIL import ImageOps as _ImageOps
        _pad = max(20, row.height // 4)
        _corner = row.getpixel((0, 0))
        row = _ImageOps.expand(row, border=_pad, fill=_corner)
    except Exception as exc:
        log.debug("api: _ocr_full_row swallowed: %s", exc)

    # Save one debug sample per field per session so we can inspect
    # what Tesseract actually received.
    try:
        if not _FULL_ROW_DEBUG_SAVED.get(field):
            row.save(f"debug_fullrow_{field}.png")
            _FULL_ROW_DEBUG_SAVED[field] = True
    except Exception as exc:
        log.debug("api: _ocr_full_row swallowed: %s", exc)

    with _TESSDATA_LOCK:
        prev_env = os.environ.get("TESSDATA_PREFIX")
        os.environ["TESSDATA_PREFIX"] = _tessdata
        try:
            # image_to_data so we get per-word bounding boxes; lets us
            # crop the right-of-label pixel region for targeted reads
            # even when Tesseract fails on the value digits in its own
            # full-row pass. No char whitelist so labels decode cleanly.
            data = pytesseract.image_to_data(
                row, config="-l eng_sc --psm 7",
                output_type=pytesseract.Output.DICT,
            )
            # Reassemble the text in Tesseract's reading order for the
            # regex-extraction fast path below.
            words = [w for w in data.get("text", []) if (w or "").strip()]
            text = " ".join(words)
        except Exception:
            data = {}
            text = ""
        finally:
            if prev_env is None:
                os.environ.pop("TESSDATA_PREFIX", None)
            else:
                os.environ["TESSDATA_PREFIX"] = prev_env

    # Fast path: did Tesseract see a numeric token in the full-row read?
    if text:
        matches = _RE_NUMERIC_TOKEN.findall(text)
        if matches:
            value = matches[-1].replace(",", "")
            log.info(
                "sc_ocr: row-ocr field=%s decoded=%r -> %r (fast-path)",
                field, text, value,
            )
            return value, [0.95] * len(value)

    # Slow path: Tesseract saw the LABEL but couldn't decode the
    # value digits (small extraction-mode panels hit this). Find the
    # label word's bounding box, crop the pixel region to its right,
    # upscale aggressively, and run CRNN + Tesseract on the value-
    # only crop. This uses Tesseract as a row anchor but relies on
    # CRNN for actual digit recognition where it's often stronger.
    if not data:
        return "", []
    label_prefix = {"mass": "mass", "resistance": "resi", "instability": "inst"}.get(field, "")
    if not label_prefix:
        log.debug("sc_ocr: row-ocr field=%s decoded=%r no_numeric_token", field, text)
        return "", []

    # Walk words in reading order; find the last one whose alpha-only
    # form contains the 4-char prefix (e.g. 'MASS:' → 'mass' → OK).
    anchor = None
    n_words = len(data.get("text", []))
    for i in range(n_words):
        w = (data["text"][i] or "").strip()
        if not w:
            continue
        alpha = "".join(c for c in w if c.isalpha()).lower()
        if label_prefix in alpha:
            anchor = (
                int(data["left"][i]),
                int(data["top"][i]),
                int(data["width"][i]),
                int(data["height"][i]),
            )
    if anchor is None:
        log.debug(
            "sc_ocr: row-ocr field=%s decoded=%r no_label_anchor",
            field, text,
        )
        return "", []

    lx, ly, lw, lh = anchor
    row_W, row_H = row.size
    # Leave a tiny gap after the colon
    value_x0 = min(row_W - 4, lx + lw + max(4, lh // 2))
    value_x1 = row_W
    # Vertically pad so descenders/ascenders survive
    value_y0 = max(0, ly - max(4, lh // 4))
    value_y1 = min(row_H, ly + lh + max(4, lh // 4))
    value_only = row.crop((value_x0, value_y0, value_x1, value_y1))

    # Aggressive upscale to ~200 px tall — only the small value crop
    # gets this treatment, not the whole row, so per-digit pixel
    # count rises far beyond what Tesseract sees in its full-row pass.
    vw, vh = value_only.size
    if vh > 0 and vh < 200:
        vscale = max(2, 220 // vh)
        value_only = value_only.resize(
            (vw * vscale, vh * vscale), Image.LANCZOS,
        )

    # Re-contrast + unsharp — the slow path earns heavier treatment
    try:
        from PIL import ImageFilter as _IF
        _va = np.array(value_only.convert("L"), dtype=np.float32)
        _mn, _mx = float(_va.min()), float(_va.max())
        if _mx - _mn > 8:
            _va = (_va - _mn) * (255.0 / (_mx - _mn))
            value_only = Image.fromarray(np.clip(_va, 0, 255).astype(np.uint8))
        value_only = value_only.filter(
            _IF.UnsharpMask(radius=1.5, percent=180, threshold=2),
        )
    except Exception as exc:
        log.debug("api: _ocr_full_row swallowed: %s", exc)

    try:
        if not _FULL_ROW_DEBUG_SAVED.get(field + "_value"):
            value_only.save(f"debug_value_slowpath_{field}.png")
            _FULL_ROW_DEBUG_SAVED[field + "_value"] = True
    except Exception as exc:
        log.debug("api: _ocr_full_row swallowed: %s", exc)

    # Run the CRNN on the targeted value crop. Tesseract is no longer
    # part of this slow-path vote for digit fields — see
    # ``_ocr_value_crop`` for the rationale (CRNN+CNN-only architecture).
    crnn_result = _crnn_recognize(value_only)
    crnn_text = ""
    crnn_confs: list[float] = []
    if crnn_result is not None:
        _ct, _cc = crnn_result
        # Same digit-mapping as in the main _ocr_value_crop path.
        _mapped = (_ct.replace("I", "1").replace("l", "1")
                     .replace("O", "0").replace("o", "0")
                     .replace("S", "5").replace("s", "5")
                     .replace("B", "8").replace("Z", "2")
                     .replace("G", "6").replace("q", "9"))
        crnn_text = "".join(c for c in _mapped if c in "0123456789.%")
        crnn_confs = _cc

    _digit_only = field in ("mass", "resistance", "instability")
    if _digit_only:
        log.debug(
            "sc_ocr: row-ocr field=%s slow-path decoded=%r anchor=%r "
            "crnn=%r (Tesseract bypassed for digit field)",
            field, text, anchor, crnn_text,
        )
        if crnn_text:
            return crnn_text, crnn_confs
        return "", []

    tess_text = _try_tesseract_eng_sc(value_only)

    log.debug(
        "sc_ocr: row-ocr field=%s slow-path decoded=%r anchor=%r "
        "crnn=%r tess=%r",
        field, text, anchor, crnn_text, tess_text,
    )

    voted = _parallel_vote(field, crnn_text, crnn_confs, tess_text)
    if voted is None:
        return "", []
    return voted


def _ocr_value_crop(value_crop: Image.Image, field: str = "") -> tuple[str, list[float]]:
    """OCR a tight value crop → (text, per_char_confidences).

    Parallel CRNN + Tesseract voting for digit-only fields — both
    engines run every scan and their outputs are reconciled by
    ``_parallel_vote``. Falls through to the 28×28 ONNX segmenter only
    when both engines produce nothing agreeable.
    """
    # ── CRNN (primary) ──
    # Acceptance gate is deliberately strict. The initial CRNN was
    # trained on sc_templates-derived synthetic crops which don't
    # perfectly match the real SC HUD rendering — so its confidence
    # on real crops is lower than on synth. Requiring length >= 2 AND
    # mean confidence > 0.95 keeps the CRNN out of the way for
    # typical runs while still letting a future retrain (with
    # real-crop training data) take over once accuracy improves.
    # Gate tuning (two-tier):
    # * High-confidence CRNN read → use directly
    # * Low-confidence CRNN read → fall through to eng_sc Tesseract
    #   (which is rock-solid on digit-only HUD values via SC-Datarunner
    #   trained model). eng_sc can't do letters as well, so we still
    #   accept the CRNN for letter-containing text at a lower bar.
    # HUD values (mass/resistance/instability) must be numeric-only.
    # For those fields, reject any CRNN output containing letters —
    # letters are always hallucinations on numeric fields (the
    # infamous 'I' → '1' confusion, or trailing letter noise). For
    # other fields (explicitly labelled as text), allow the letter
    # gate.
    _digit_only_field = field in ("mass", "resistance", "instability")

    # Segmenter-side digit-count fallback. When the HUD-RGB CRNN's
    # read gets rejected via COUNT-MISMATCH, the segmenter's
    # honest digit count gets stashed here so the COUNT ORACLE
    # block can prefer it over the discredited CRNN count.
    # Stays ``None`` on the happy path where CRNN accepts or
    # COUNT-MISMATCH doesn't fire.
    _hud_count_from_segmenter: Optional[int] = None

    # ── (0-CRNN) WHOLE-STRIP CRNN gate for HUD numeric fields ──
    # Mirrors the signature CRNN gate (``_classify_signal_via_crnn_rgb``
    # at the start of ``_signal_recognize_pil``). The CRNN reads the
    # value strip as a single sequence via CTC, sidestepping the
    # per-glyph segmenter that is the dominant failure mode in the
    # legacy stack — when the segmenter mis-splits or fuses glyphs,
    # the per-glyph CNN voters below confidently classify the wrong
    # crops as wrong digits and the strict-gate / dual-agree gate
    # happily accepts the wrong-but-confident read.
    #
    # The CRNN runs FIRST and gets a chance to consume the read at a
    # very strict confidence bar (≥0.95 mean, ≥2 chars). If it accepts,
    # we return without ever invoking the per-glyph stack. If it
    # doesn't (low conf, empty, or letters that survive the digit
    # filter), we fall through to the existing primary→secondary→
    # CRNN-vote→Tesseract→segmenter cascade unchanged.
    #
    # Field-specific alphabets are enforced via ``digit_only=True`` on
    # ``_crnn_recognize`` so the multipurpose CRNN can't hallucinate
    # letter-mixed strings on noisy crops (the ``'HIP20'`` / ``'Cu'``
    # failure mode the existing fallback path already protects against
    # below at the digit-filter step).
    #
    # Best-effort: any failure (ONNX missing, inference error) falls
    # through silently. The CRNN gate NEVER blocks the rest of the
    # pipeline.
    if _digit_only_field:
        # ── Try the HUD-specific RGB CRNN first ──
        # ``_classify_hud_value_via_crnn_rgb`` returns None when the
        # model isn't on disk yet (training pending) OR onnxruntime
        # fails. The legacy CRNN promotion below remains the fallback
        # so the pipeline keeps working during a partial deployment.
        try:
            # Beam search width=8 with LEXICON rerank (re-enabled
            # after the learned-lexicon infrastructure landed in
            # ``hud_lexicon.py``).
            #
            # History: an earlier attempt at beam_width=8 with a
            # plausibility-rerank regressed -0.8pp on the benchmark
            # because plausibility is a CONTINUOUS predicate
            # (resistance 0-100, instability 0-200, mass open-ended)
            # — almost any digit string passes, so the rerank
            # effectively just shuffled confident reads. The new
            # lexicon predicate is DISCRETE (per-field LRU set of
            # values the user has actually confirmed this session
            # via ``frozen_panel`` auto-freeze), exactly mirroring
            # why the signature CRNN's beam+lexicon works on its
            # 664-value chart-derived set.
            #
            # Empty-lexicon safety: the rerank early-returns the
            # greedy top when ``hud_lexicon.get_values(field)`` is
            # empty, so cold-start installs behave exactly like the
            # previous greedy-only path. Promote-not-reject semantics:
            # an out-of-lexicon top is never dropped — we only ever
            # PROMOTE in-lexicon alternatives, never demote.
            _hud_rgb_crnn = _classify_hud_value_via_crnn_rgb(
                value_crop, field, beam_width=8,
            )
        except Exception as _hud_rgb_crnn_exc:
            log.debug(
                "sc_ocr.hud: HUD-RGB CRNN gate failed (%s)",
                _hud_rgb_crnn_exc,
            )
            _hud_rgb_crnn = None
        if _hud_rgb_crnn is not None:
            _hudr_text_raw, _hudr_mean = _hud_rgb_crnn
            _hudr_text = "".join(
                c for c in _hudr_text_raw if c in "0123456789.%"
            )
            # Field-specific format gates:
            #   mass:        integer, length >= 2 (digits only)
            #   resistance:  ends in % (digits + optional %)
            #   instability: decimal (digits + optional .)
            # All accept at mean_conf >= 0.85 — the HUD CRNN was
            # trained on user-confirmed labels so its high-conf reads
            # are trustworthy at a lower bar than the synth-trained
            # legacy CRNN.
            _hudr_ok = False
            if field == "mass":
                # Mass is integer kg. Require ≥ 2 digits in the general
                # case — single-digit reads like "1" or "5" are almost
                # always truncated CRNN output of a longer true value
                # (no real rock has mass between 1 and 9 kg). The ONE
                # legitimate single-character mass is "0", which the
                # HUD displays when a rock has just been scanned but
                # not yet mined. Without this carve-out, a confident
                # CRNN read of "0" on a just-spawned-rock crop falls
                # through to the per-glyph CNN, which segments the
                # whole "0.00" strip as one wide blob and emits
                # garbage (".1", "1", etc) — see commit history for
                # the labeled benchmark data that quantified this:
                # 19 of the 130 captures hit this exact failure mode.
                _hudr_ok = (
                    all(c.isdigit() for c in _hudr_text)
                    and (_hudr_text == "0" or len(_hudr_text) >= 2)
                )
            elif field in ("resistance", "instability"):
                # 1-3 digits + optional decimal/percent. Single-digit
                # '0' (resistance) or '0' / '0.0' (instability) is a
                # valid just-spawned-rock read.
                _hudr_digits = "".join(c for c in _hudr_text if c.isdigit())
                _hudr_ok = (
                    len(_hudr_text) >= 1
                    and len(_hudr_digits) >= 1
                )
            # ── Plausibility-based confidence relaxation ──
            # Mirror of the signature CRNN's lexicon-confirmed gate:
            # accept lower-confidence reads when the parsed value
            # passes the game-state plausibility check. The signature
            # pipeline uses a lexicon (discrete set of valid values);
            # HUD values are continuous so we use ``priors.is_plausible``
            # instead — same idea, different validation predicate.
            #
            # Threshold tiers:
            #   * mean_conf >= 0.85 → accept (always, current behavior)
            #   * mean_conf in [0.40, 0.85) AND plausible → accept
            #   * mean_conf < 0.40 → reject (fall through)
            #
            # The 0.40 floor mirrors the signature CRNN's
            # lexicon-confirmed gate threshold. The plausibility
            # filter prevents accepting confidence-borderline
            # hallucinations on broken crops (e.g. CRNN reads
            # "415" at conf 0.55 on a corrupted resistance crop —
            # 415 is implausible for resistance which is 0-100,
            # so the gate falls through).
            _hudr_plausible = False
            _hudr_lexicon_hit = False
            _hudr_val: Optional[float] = None
            if _hudr_ok:
                try:
                    _hudr_digits_only = "".join(
                        c for c in _hudr_text if c.isdigit() or c == "."
                    )
                    if _hudr_digits_only:
                        _hudr_val = float(_hudr_digits_only)
                        from . import priors as _priors_hud
                        _hudr_plausible, _ = _priors_hud.is_plausible(
                            field, _hudr_val, {},
                        )
                        # Learned-lexicon hit. Populated by the
                        # ``frozen_panel`` auto-freeze trigger above
                        # — values the system was confident enough
                        # to publish AND that survived divergence
                        # checks. Hitting the lexicon is a STRONGER
                        # signal than plausibility (which only checks
                        # the per-field continuous range), so it
                        # gates the same threshold relaxation.
                        try:
                            from . import hud_lexicon as _hud_lex
                            _hudr_lexicon_hit = _hud_lex.is_known(
                                field, _hudr_val,
                            )
                        except Exception as _lex_q_exc:
                            log.debug(
                                "sc_ocr.hud: lexicon query failed: %s",
                                _lex_q_exc,
                            )
                            _hudr_lexicon_hit = False
                except Exception as _hudr_plaus_exc:
                    log.debug(
                        "sc_ocr.hud: plausibility check failed: %s",
                        _hudr_plaus_exc,
                    )
                    _hudr_plausible = False

            # ── Acceptance thresholds (widened) ──
            # Original: strict 0.85, plausible 0.40. After moving to the
            # HUD-RGB CRNN as the authoritative primary (per the
            # signal pipeline pattern), we widen the gate substantially:
            #   * mean_conf >= 0.55 → accept (was 0.85)
            #   * mean_conf in [0.30, 0.55) AND (plausible OR
            #     lexicon-confirmed) → accept (was 0.40)
            # Rationale: the CRNN's val_acc on the noisy training set
            # under-represents production accuracy (the validation set
            # included a number of partially-rendered / cropped panels
            # that no model can read correctly). On real captures the
            # CRNN's confident reads are reliable, and the COUNT-MISMATCH
            # safety net above still catches the dropped-digit failure
            # mode. The structural validators downstream (mass >= 2 digits,
            # resistance <= 100, instability has '.') catch the rest.
            #
            # Lexicon-hit reads get the same relaxation as plausible
            # ones — mirrors the signal pipeline's lexicon-confirmed
            # gate (which accepts at conf ≥0.40 when the value is in
            # the chart-derived 664-set vs ≥0.80 otherwise). Empty
            # lexicon on first run = no-op (gate behaves exactly as
            # plausibility-only).
            _hudr_threshold = (
                0.30 if (_hudr_plausible or _hudr_lexicon_hit)
                else 0.55
            )
            _hudr_pass = _hudr_ok and _hudr_mean >= _hudr_threshold

            # ── Oversized-crop sanity gate ──
            # The HUD-RGB CRNN reads "0" confidently from ANY crop
            # that doesn't have distinct digit ink. The dangerous
            # failure mode is the DEGENERATE crop: when the upstream
            # row finder merges two rows or extends a row band past
            # its natural height, _find_value_crop returns a tall
            # value-column slice that includes adjacent UI text and
            # background. The CRNN sees a mostly-empty oversized area
            # and outputs "0" at modest confidence (~0.83). Because 0
            # passes plausibility for every HUD field, the relaxed-
            # threshold gate accepts it — producing the "values jump
            # to 0/0/0 between scans" failure mode the user observed
            # in production even after the anchor tracker was added.
            #
            # We use the CROP HEIGHT as the discriminator. Surveyed
            # against 280 labeled production captures (mass +
            # resistance + instability across multiple users and HUD
            # scales): every single legit crop has height ≤ 44 px,
            # and the modal height is exactly 44 px. The degenerate
            # crops in the user's 16:28:17 log were 56-57 px tall —
            # a 25-30% increase that doesn't occur in legit data.
            # Threshold 50 px sits cleanly between them.
            #
            # Width is NOT a clean discriminator: legitimate
            # _find_value_crop output can extend to 240+ px when the
            # value crop captures adjacent text like "SCU" — the
            # CRNN's read of "0" on those crops is lucky-correct
            # (the digit IS visible in the crop), not reliable.
            # Filtering by width would reject too many legit-but-
            # lucky reads. Height is the structural signal: a row
            # band that's too tall is always wrong.
            _OVERSIZED_CROP_HEIGHT_PX = 50
            if _hudr_pass and value_crop.height > _OVERSIZED_CROP_HEIGHT_PX:
                log.info(
                    "sc_ocr.hud: field=%s REJECTING CRNN read=%r "
                    "mean=%.2f — value_crop too tall (%dx%d, threshold "
                    "h>%d). The row band finder produced an "
                    "abnormally tall band — likely two rows merged "
                    "or label_match drift; falling through to CNN "
                    "path so a wrong-but-confident '0' read doesn't "
                    "poison the field.",
                    field, _hudr_text, _hudr_mean,
                    value_crop.width, value_crop.height,
                    _OVERSIZED_CROP_HEIGHT_PX,
                )
                _hudr_pass = False

            # ── Per-glyph CONSENSUS cross-check vs the CRNN ──
            # The CRNN gate is the authoritative primary, but it has two
            # failure modes the per-glyph CNNs catch: a relaxed-gate guess
            # (mass "4343" @ 0.39, lexicon-accepted, vs per-glyph "3384" @
            # 1.00) and a STRICT-but-wrong read (resistance "1%" decoded as
            # "0" @ 0.98 while every per-glyph voter says "1" — user
            # 2026-06-01). A single per-glyph voter can be noisy, so we
            # require the gray PRIMARY and the inverted-polarity SECONDARY
            # CNNs to INDEPENDENTLY agree on the same same-length value at
            # high confidence before overriding. Two decorrelated voters
            # agreeing is a stronger signal than the CRNN's lone read, and
            # requiring the agreement is what keeps this from regressing the
            # harness (a primary-only deferral on strict reads did regress
            # it; the consensus form holds the 290/377 baseline).
            #
            # Scoped to mass/resistance: instability is X.XX, and its
            # per-glyph reads currently fuse the leading "1." into a "%"
            # tile, so its consensus is unreliable — leave instability to
            # the CRNN + count-oracle cascade.
            if _hudr_pass and field in ("mass", "resistance", "instability"):
                _hudr_digits = "".join(
                    c for c in _hudr_text if c.isdigit() or c == "."
                )
                _cons = _perglyph_consensus_digits(field, value_crop)
                if (
                    _cons
                    and _cons != _hudr_digits
                ):
                    log.info(
                        "sc_ocr.hud: field=%s CRNN read=%r mean=%.2f "
                        "OVERRIDDEN — per-glyph primary+secondary agree on "
                        "%r — deferring to per-glyph CNN",
                        field, _hudr_text, _hudr_mean, _cons,
                    )
                    _hudr_pass = False

            # NOTE: a low-content gate (reject CRNN="0" reads when
            # the crop has contrast<30 AND edge_frac<0.04) was
            # attempted here to catch the "0/0/0 flicker on partially-
            # rendered panels" failure mode. It works against the
            # specific symptom but cannot distinguish:
            #
            #   (a) a legit just-spawned rock with mass=0 rendered as
            #       a barely-visible "0" on bright sky background, vs.
            #   (b) a non-zero rock whose panel partially failed to
            #       render, leaving a sky-only crop.
            #
            # Both produce visually-identical crops with contrast 5-30,
            # edge_frac 0-0.01. The signal that distinguishes them
            # lives in the broader panel/scene context, not in the
            # value crop pixels. The labelled benchmark has both
            # populations annotated as the user's known value, so a
            # gate that rejects both regresses the benchmark by ~2.5 pp
            # (10 false-rejects on legit-0 captures, no real-fix passes
            # because the empty-sky captures are mis-labeled anyway).
            # See git log around this comment for the experiment data.
            #
            # Mitigation for the production flicker now lives in the
            # field lock / consensus cache (multi-scan stability) and
            # the existing height-gate above (rejects degenerate-band
            # crops). A future per-panel "is the panel actually here"
            # gate would be the right fix.

            # ── Count-mismatch sanity gate ──
            # Failure mode this catches: the CRNN drops a middle digit
            # during CTC collapse. User-reported example: in-game panel
            # shows ``INSTABILITY: 523.33`` (6 chars: 3 digits + dot +
            # 2 digits) but the HUD-RGB CRNN reads ``52.33`` (5 chars).
            # The read comes in at HIGH confidence so the strict accept
            # gate above happily accepts a wrong-but-confident value.
            #
            # The per-glyph segmenter (``_segment_glyphs``) sees the
            # actual ink and consistently finds 3 digit spans + 1 small
            # dot span + 2 digit spans = 6 spans on the same crop. By
            # cross-checking the CRNN's DIGIT count against the
            # segmenter's DIGIT-shaped span count BEFORE the gate fires,
            # we can refuse the suspect read and fall through to the
            # per-glyph CNN cascade (which then has the proper count-
            # oracle plumbing to recover).
            #
            # The comparison is digit-only (not digit + dot) because:
            #   * The user-reported bug is a digit-drop, not a dot-drop.
            #   * The CRNN sometimes omits the dot in CTC output without
            #     being "wrong" — downstream ``proactive-decimal-recover``
            #     turns ``"1421"`` into ``14.21`` correctly. Comparing
            #     including the dot would false-reject those cases.
            #
            # Field-specific tolerance:
            #   * mass        → digits only. Reject when
            #                   seg_digit_spans >= crnn_digits + 1.
            #   * resistance  → digits + optional ``%``. Segmenter
            #                   picks ``%`` up as its own digit-shaped
            #                   span while the CRNN's digit-only output
            #                   omits it; ``seg == crnn + 1`` is normal
            #                   and MUST NOT trigger. Reject when
            #                   seg_digit_spans >= crnn_digits + 2.
            #   * instability → digits + one dot. Same +1 tolerance as
            #                   mass — instability has no ``%``.
            #
            # Guards against false positives:
            #   * Only fire when CRNN has ≥ 2 digits — single-digit
            #     reads like ``"0"`` are never the dropped-digit failure
            #     mode. The segmenter's overcount on a ``"0"`` crop is
            #     typically label intrusion (the ``"MASS:"`` colon getting
            #     captured) rather than real digits.
            #   * Segmenter must find at least 3 digit-shaped spans.
            #     Fewer means the segmenter is unreliable on this crop
            #     (e.g. mega-blob fusion). Setting this floor at 3 also
            #     reinforces the multi-digit assumption above.
            #   * Only fire when seg_count > crnn_count. When the CRNN
            #     reads MORE digits than the segmenter found (segmenter
            #     under-counted via mega-blob fusion), the CRNN is more
            #     likely correct and the count-oracle plumbing further
            #     downstream is the right tool to recover.
            #   * "Digit-shaped" filter requires h >= 8 AND w >= 2 — same
            #     thresholds as the live-overlay rendering uses to gate
            #     out segmenter noise (chromatic-aberration speckles,
            #     anti-aliasing fuzz). Dot spans are counted separately
            #     via the h <= 6 in a tall row criterion (diagnostic
            #     only — not part of the rejection comparison).
            if _hudr_pass and field in ("mass", "resistance", "instability"):
                try:
                    _cm_crnn_digits = sum(
                        1 for c in _hudr_text if c.isdigit()
                    )
                    # Run the gate for ALL reads with >=1 digit. We
                    # used to gate on >= 2 to skip single-digit reads,
                    # but that misses two real failure modes:
                    #   (a) WIDTH-FUSION on a single-digit CRNN read.
                    #       Example: CRNN reads instability='9' on a
                    #       crop where the segmenter finds widths
                    #       [16, 48, 15, 24] — the 48-px tile is a
                    #       fused pair. The single-digit CRNN read
                    #       LOOKS plausible but is wrong, and only the
                    #       segmenter's structural width-fusion check
                    #       can catch it.
                    #   (b) Single-digit dropped-digit reads. The
                    #       failure mode the original >= 2 gate was
                    #       targeting (a 3-digit value reads as 2)
                    #       generalizes: a 2-digit value can read as
                    #       1, and that 1-digit read should be
                    #       rejected the same way.
                    # The false-rejection concern that motivated the
                    # >= 2 gate (label intrusion overcounting the
                    # segmenter on legit "0" reads) is already
                    # absorbed by:
                    #   * the height-consistency filter (±40% of
                    #     median height drops label glyphs)
                    #   * the ``_cm_true_digit_count >= 3`` floor on
                    #     the COUNT-MISMATCH reject condition (so a
                    #     legit "0" with one label-intrusion span
                    #     finds 1 or 2 digit-shaped spans, fails the
                    #     >= 3 floor, and gets accepted)
                    if _cm_crnn_digits >= 1:
                        _cm_gray = np.array(
                            value_crop.convert("L"), dtype=np.uint8,
                        )
                        _cm_gray = _canonicalize_polarity(_cm_gray)
                        _cm_bin = _adaptive_binarize(_cm_gray)
                        _, _cm_boxes = _segment_glyphs(
                            _cm_gray, _cm_bin, field=field,
                        )
                        _cm_row_h = int(_cm_gray.shape[0])
                        # Tall-row gate for dot detection: a tall crop
                        # is one where digits would be ≥ 14 px tall.
                        # Below that the "small dot" heuristic is
                        # meaningless (every span is small).
                        _cm_tall_row = _cm_row_h >= 14
                        # First pass: collect all "candidate digit"
                        # spans (h >= 8, w >= 2) so we can compute the
                        # median height for the consistency filter.
                        _cm_candidates: list[tuple[int, int]] = []  # (h, w)
                        _cm_dot_spans = 0
                        for (_gx, _gy, _gw, _gh) in (_cm_boxes or []):
                            if _gh >= 8 and _gw >= 2:
                                _cm_candidates.append((_gh, _gw))
                            elif _cm_tall_row and _gh <= 6 and _gw >= 1:
                                # Small span in a tall row → candidate
                                # dot. Tracked for diagnostics; the
                                # rejection compares digit-shaped only.
                                _cm_dot_spans += 1
                        # Height-consistency filter: real digits in the
                        # value row have very uniform heights. If a
                        # span's height deviates >40% from the median
                        # digit height, it is most likely label-text
                        # intrusion (e.g. INSTABILITY's "I"/"T" tails
                        # leaking past gap-cut). Filtering these out
                        # prevents over-counting on crops where the
                        # value crop boundary fell inside the label.
                        # Conservatively wide tolerance (±40%) keeps
                        # legit aspect-ratio differences (e.g. "1" is
                        # narrower but still h-matches "0") in scope.
                        _cm_digit_spans = 0
                        _cm_height_outliers = 0
                        # Width-fusion detection. When two adjacent
                        # glyphs touch at the binarization stage they
                        # collapse into a single span whose width is
                        # ~2× the median digit width. The segmenter
                        # then hands ONE tile (containing two glyphs)
                        # to the CNN, which classifies it as a single
                        # character — typically "%" / "5" / "8" (the
                        # CNN's best guess at a fused-digit shape) at
                        # mediocre confidence. Visible in tile dumps
                        # as a 48-px tile containing "54" classified
                        # as "5" at 0.52.
                        #
                        # Each wide tile implies ``round(w/median_w) -
                        # 1`` extra digits hiding inside it. We sum
                        # those and propagate as the segmenter's
                        # honest digit count, so the COUNT ORACLE +
                        # PROPORTIONAL split downstream can target
                        # the correct number of digits and force the
                        # binarizer/segmenter to split the fused span.
                        _cm_width_extra = 0
                        if _cm_candidates:
                            _heights = sorted(h for h, _ in _cm_candidates)
                            _median_h = _heights[len(_heights) // 2]
                            _h_lo = int(_median_h * 0.60)
                            _h_hi = int(_median_h * 1.40)
                            _widths = sorted(
                                w for _, w in _cm_candidates
                            )
                            _median_w = _widths[len(_widths) // 2]
                            _cm_n_cand = len(_cm_candidates)
                            for _cm_ci, (_ch, _cw) in enumerate(
                                _cm_candidates
                            ):
                                if _h_lo <= _ch <= _h_hi:
                                    _cm_digit_spans += 1
                                else:
                                    _cm_height_outliers += 1
                                # The resistance value's trailing glyph
                                # is ALWAYS "%", which renders ~1.6-2.0×
                                # a digit's width. The width-fusion
                                # check below would flag it as a
                                # fused-digit tile and inflate the
                                # cascade target by 1, forcing a wrong
                                # split (observed: "65%" → 4-way split →
                                # "555"). Skip the rightmost span for
                                # resistance — it is the "%", not fused
                                # digits.
                                if (
                                    field == "resistance"
                                    and _cm_ci == _cm_n_cand - 1
                                ):
                                    continue
                                # Width fusion: any span whose width
                                # is ≥1.6× the median is likely a
                                # fused-digit tile. We use a wide
                                # tolerance (1.6×) because the "1"
                                # digit is genuinely narrow and the
                                # median can be pulled toward it on
                                # rows like "1XX"; setting the
                                # threshold at 1.6× the median avoids
                                # flagging legit wide digits ("0",
                                # "8") on those rows. Cap each tile's
                                # contribution at +3 extras so a
                                # spurious mega-blob doesn't blow up
                                # expected_count.
                                if _median_w > 0:
                                    _ratio = _cw / float(_median_w)
                                    if _ratio >= 1.6:
                                        _cm_width_extra += min(
                                            3, int(round(_ratio)) - 1,
                                        )
                        # Field-specific tolerance threshold. Reject
                        # when the segmenter found AT LEAST this many
                        # digit-shaped spans MORE than the CRNN's digit
                        # count. resistance gets +2 to absorb the ``%``
                        # span; mass and instability use +1.
                        _cm_tolerance = 2 if field == "resistance" else 1
                        # True structural digit count = visible spans
                        # + digits hiding inside fused wide tiles.
                        # (This is the COMPARISON count vs the CRNN's
                        # digit output — digits only, no dot.)
                        _cm_true_digit_count = (
                            _cm_digit_spans + _cm_width_extra
                        )
                        # Cascade target count: what the downstream
                        # per-glyph cascade actually needs to produce
                        # AFTER the COUNT ORACLE → PROPORTIONAL split
                        # path. For instability this includes the
                        # visible '.' span so PROPORTIONAL merge
                        # doesn't fuse the dot with a neighboring
                        # digit (the original failure mode that turned
                        # ``"12.09"`` into ``"1209"`` via 5→4 merge).
                        #
                        # The dot reads as a height-outlier (h~8-10
                        # vs digit-h~27, much shorter than ±40% of the
                        # digit median), so it doesn't fall into
                        # ``_cm_digit_spans`` or the ``_cm_dot_spans``
                        # h<=6 dot detector either — it lands in
                        # ``_cm_height_outliers``. We pull it back out
                        # by looking for a small-h narrow-w outlier
                        # within the candidate pool. Limited to 1 dot
                        # since instability has at most one decimal
                        # point.
                        _cm_instability_dot = 0
                        if (
                            field == "instability"
                            and _cm_candidates
                            and _median_h > 0
                            and _median_w > 0
                        ):
                            for (_ch, _cw) in _cm_candidates:
                                if (
                                    _ch < _h_lo
                                    and _ch >= 4
                                    and _cw <= max(4, int(_median_w * 0.7))
                                ):
                                    _cm_instability_dot = 1
                                    break
                        _cm_cascade_target = (
                            _cm_true_digit_count + _cm_instability_dot
                        )
                        if (
                            _cm_true_digit_count > _cm_crnn_digits
                            and (_cm_true_digit_count - _cm_crnn_digits)
                                >= _cm_tolerance
                            and _cm_true_digit_count >= 3
                        ):
                            log.info(
                                "sc_ocr.hud: COUNT-MISMATCH field=%s "
                                "CRNN=%r (%d digits) seg=%d digit-spans "
                                "(+%d dot, +%d height-outlier, "
                                "+%d width-fused, +%d instability-dot, "
                                "row_h=%d) tol=+%d → rejecting CRNN "
                                "read, true digit-count=%d cascade "
                                "target=%d, falling through to per-"
                                "glyph CNN cascade",
                                field, _hudr_text, _cm_crnn_digits,
                                _cm_digit_spans, _cm_dot_spans,
                                _cm_height_outliers, _cm_width_extra,
                                _cm_instability_dot, _cm_row_h,
                                _cm_tolerance, _cm_true_digit_count,
                                _cm_cascade_target,
                            )
                            _hudr_pass = False
                            # Stash the CASCADE TARGET count for the
                            # COUNT ORACLE block below. Includes:
                            #   * visible digit-shaped spans
                            #   * digits hiding inside fused-width
                            #     tiles (a 48-px tile next to 18-px
                            #     peers really contains ~2 digits)
                            #   * the decimal dot for instability
                            #     (otherwise PROPORTIONAL merge would
                            #     collapse the 5 real spans of
                            #     ``"12.09"`` down to 4 and fuse the
                            #     dot into a neighboring digit)
                            _hud_count_from_segmenter = _cm_cascade_target
                        elif _cm_width_extra > 0 or _cm_instability_dot > 0:
                            # Even if CRNN's digit count agrees with
                            # the visible digit-span count, two
                            # structural signals can still force
                            # rejection:
                            #   1. Wide-fused tiles (width_extra>0):
                            #      the CNN will mis-classify the
                            #      fused tile.
                            #   2. An unaccounted-for dot in
                            #      instability: PROPORTIONAL merge
                            #      will erase it.
                            log.info(
                                "sc_ocr.hud: WIDTH-FUSION/DOT-RESCUE "
                                "field=%s seg=%d visible + %d hidden "
                                "in wide tiles + %d instability-dot "
                                "→ rejecting CRNN=%r, cascade "
                                "target=%d, forcing re-segmentation",
                                field, _cm_digit_spans, _cm_width_extra,
                                _cm_instability_dot, _hudr_text,
                                _cm_cascade_target,
                            )
                            _hudr_pass = False
                            _hud_count_from_segmenter = _cm_cascade_target
                        else:
                            log.debug(
                                "sc_ocr.hud: count-match field=%s "
                                "CRNN=%r (%d digits) seg=%d (+%d dot "
                                "+%d outlier +%d width-fused) tol=+%d "
                                "→ accepting",
                                field, _hudr_text, _cm_crnn_digits,
                                _cm_digit_spans, _cm_dot_spans,
                                _cm_height_outliers, _cm_width_extra,
                                _cm_tolerance,
                            )
                except Exception as _cm_exc:
                    # Any segmenter / preprocessing failure: skip the
                    # check (best-effort gate, never blocks the rest of
                    # the pipeline).
                    log.debug(
                        "sc_ocr.hud: count-mismatch check failed (%s) "
                        "— leaving _hudr_pass=%s unchanged",
                        _cm_exc, _hudr_pass,
                    )

            if _hudr_pass and _bypass_revoked(field):
                log.info(
                    "sc_ocr.hud: HUD-RGB CRNN gate SKIPPED field=%s — "
                    "consistency reflex active (recent reads flapped "
                    "on stable pixels); falling through to full vote",
                    field,
                )
                _hudr_pass = False
            if _hudr_pass:
                if _hudr_mean >= 0.85:
                    _hudr_gate = "rgb-crnn-strict"
                elif _hudr_lexicon_hit:
                    # Lexicon hit takes precedence over plain
                    # plausibility in the log label even when both
                    # apply, since it's the stronger signal.
                    _hudr_gate = "rgb-crnn-lexicon"
                else:
                    _hudr_gate = "rgb-crnn-plausible"
                log.info(
                    "sc_ocr.hud: HUD-RGB CRNN gate accepted field=%s "
                    "text=%r mean=%.2f gate=%s val=%s lex_hit=%s "
                    "(skipping per-glyph CNN + secondary + vote)",
                    field, _hudr_text, _hudr_mean, _hudr_gate,
                    _hudr_val, _hudr_lexicon_hit,
                )
                try:
                    _dump_voter(field, "winner", _hudr_text, _hudr_mean)
                    _clear_viewer_entry(field, "cnn")
                    _clear_viewer_entry(field, "tesseract")
                    _clear_viewer_entry(field, "crnn")
                    # The per-glyph CNN cascade is skipped on this CRNN-
                    # accept fast path, so its viewer tiles would freeze
                    # stale ("glyph reader not updating for the mining
                    # HUD"). Refresh them with a fresh per-glyph shadow
                    # read of THIS frame's value crop (display-only).
                    _shadow_perglyph_dump(field, value_crop)
                except Exception as _hudr_dump_exc:
                    log.debug(
                        "sc_ocr.hud: HUD-RGB gate viewer dump failed: %s",
                        _hudr_dump_exc,
                    )

                # ── Diagnostic-only segmenter pass for the live overlay ──
                # The CRNN-accept fast path skips the per-glyph CNN, so
                # the live overlay would have no glyph-count / glyph-size
                # info to show without a separate diagnostic call. Gated
                # on the "overlay" heartbeat — production with no viewer
                # open pays zero cost. Boxes are pushed in crop-relative
                # coords; the overlay translates to panel coords at
                # render time via :func:`set_value_crop`'s rectangle.
                try:
                    from . import debug_overlay as _dbg_glyph
                    if _dbg_glyph.is_tag_active("overlay"):
                        _diag_gray = np.array(
                            value_crop.convert("L"), dtype=np.uint8,
                        )
                        _diag_gray = _canonicalize_polarity(_diag_gray)
                        _diag_bin = _adaptive_binarize(_diag_gray)
                        _, _diag_boxes = _segment_glyphs(
                            _diag_gray, _diag_bin, field=field,
                        )
                        _dbg_glyph.set_glyph_boxes(field, _diag_boxes)
                except Exception as _diag_glyph_exc:
                    log.debug(
                        "sc_ocr.hud: live-overlay glyph-box diag failed: %s",
                        _diag_glyph_exc,
                    )

                return _hudr_text, [_hudr_mean] * len(_hudr_text)

        # ── Legacy CRNN fallback (kept for partial deployments) ──
        # When the HUD CRNN isn't loaded (or its read didn't pass the
        # gate), the legacy multipurpose CRNN still gets a chance to
        # accept the read at a stricter bar. Predates the HUD CRNN
        # and was the original gate-0 promotion. Now serves as the
        # second-tier whole-strip reader before per-glyph CNN runs.
        try:
            _hud_crnn_pre = _crnn_recognize(value_crop, digit_only=True)
        except Exception as _hud_crnn_pre_exc:
            log.debug(
                "sc_ocr.hud: legacy CRNN gate attempt failed (%s) — "
                "falling through to per-glyph CNN", _hud_crnn_pre_exc,
            )
            _hud_crnn_pre = None
        if _hud_crnn_pre is not None and _hud_crnn_pre[0]:
            _hud_crnn_pre_text_raw, _hud_crnn_pre_confs = _hud_crnn_pre
            _hud_crnn_pre_text = "".join(
                c for c in _hud_crnn_pre_text_raw if c in "0123456789.%"
            )
            _hud_crnn_pre_mean = (
                sum(_hud_crnn_pre_confs) / len(_hud_crnn_pre_confs)
                if _hud_crnn_pre_confs else 0.0
            )
            # Per-glyph CONSENSUS cross-check (same as the HUD-RGB gate).
            # This legacy gate also short-circuits the cascade, so it
            # needs the same guard: if the gray PRIMARY and inverted
            # SECONDARY CNNs independently agree on a different same-length
            # digit string at high confidence, the CRNN lost a look-alike
            # digit (user 2026-06-01: CRNN "3738" @ 0.97 while every
            # per-glyph voter reads "3736" @ 1.00) — defer to per-glyph.
            _legacy_digits = "".join(
                c for c in _hud_crnn_pre_text if c.isdigit() or c == "."
            )
            _legacy_cons = _perglyph_consensus_digits(field, value_crop)
            _legacy_override = bool(
                _legacy_cons
                and _legacy_cons != _legacy_digits
            )
            if _legacy_override:
                log.info(
                    "sc_ocr.hud: LEGACY CRNN gate field=%s read=%r mean=%.2f "
                    "OVERRIDDEN — per-glyph primary+secondary agree on %r — "
                    "deferring to per-glyph CNN", field, _hud_crnn_pre_text,
                    _hud_crnn_pre_mean, _legacy_cons,
                )
            if (
                len(_hud_crnn_pre_text) >= 2
                and _hud_crnn_pre_mean >= 0.95
                and not _legacy_override
            ):
                log.info(
                    "sc_ocr.hud: LEGACY CRNN gate accepted field=%s "
                    "text=%r mean=%.2f (skipping per-glyph CNN + "
                    "secondary + vote)", field, _hud_crnn_pre_text,
                    _hud_crnn_pre_mean,
                )
                try:
                    _dump_voter(
                        field, "winner",
                        _hud_crnn_pre_text, _hud_crnn_pre_mean,
                    )
                    _clear_viewer_entry(field, "cnn")
                    _clear_viewer_entry(field, "tesseract")
                    # Per-glyph skipped on this fast path too — refresh
                    # its viewer tiles with a fresh shadow read (see the
                    # HUD-RGB gate above).
                    _shadow_perglyph_dump(field, value_crop)
                except Exception as _hud_crnn_pre_dump_exc:
                    log.debug(
                        "sc_ocr.hud: legacy gate viewer dump failed: %s",
                        _hud_crnn_pre_dump_exc,
                    )
                return _hud_crnn_pre_text, _hud_crnn_pre_confs

    # Holders for the primary CNN's segmentation output.  Set inside
    # the primary block below, reused by the secondary ONNX voter
    # further down the function.  The secondary inverts these crops
    # rather than running its own segmentation — so the two voters
    # see the EXACT same digit framing (no more catastrophic secondary
    # segmentation failures) and the vote is a true polarity-
    # decorrelated cross-check of the same source pixels.  Requires
    # the classifier to be trained with polarity augmentation (see
    # scripts/augment_from_source.py).
    _primary_crops: list[np.ndarray] = []
    _primary_results: list[tuple[str, float]] = []

    # ─── STANDALONE SEGMENTER COUNT (CRNN-independent) ───
    # The COUNT-MISMATCH/WIDTH-FUSION block above only runs when the
    # HUD-RGB CRNN returned a read AND that read survived the format /
    # threshold gates (``_hudr_pass = True``). Three real failure modes
    # are missed by that gating:
    #
    #   1. ``_hud_rgb_crnn is None`` — model file missing, onnxruntime
    #      error, or ``_classify_hud_value_via_crnn_rgb`` returned None
    #      for any other reason. The entire ``if _hud_rgb_crnn is not
    #      None:`` block is bypassed.
    #   2. CRNN ran but format gate rejected the read BEFORE
    #      COUNT-MISMATCH could observe it. Example: mass crop showing
    #      ``10810``, CRNN reads ``"9"`` (single-digit). ``_hudr_ok``
    #      becomes False because mass requires ``len >= 2 or text ==
    #      "0"``, so ``_hudr_pass`` is False, and the COUNT-MISMATCH
    #      block at line ``if _hudr_pass and field in (...)`` skips.
    #   3. CRNN ran, format passed, threshold passed, COUNT-MISMATCH /
    #      WIDTH-FUSION accepted (i.e. CRNN agrees with segmenter), but
    #      the CRNN's read is still wrong in some other way (e.g.
    #      mis-classified individual digit) AND the CRNN returns early
    #      at line 8583. In that case this block doesn't run — but
    #      that's fine because the CRNN's text is what gets returned
    #      anyway, no cascade involved.
    #
    # In failure modes 1 and 2 the cascade below runs WITHOUT a
    # segmenter-derived count, COUNT ORACLE has no override, the multi-
    # binarizer falls back to its field default (``_bin_expected = 4``
    # for mass), and a 5-digit crop gets binarized toward 4 spans —
    # producing the user's observed reads of ``"1610"`` / ``"110"``
    # instead of ``"10810"``.
    #
    # Originally (commit 8116c6d) this block only fired when the
    # segmenter found a wide-fused tile (≥ 1.6× median width). But the
    # production log at 19:22:17 showed the segmenter's FIRST pass on
    # the same crop returning a clean 5-span read with NO fusion at
    # all (widths=[13, 21, 20, 13, 22]). The fusion only appears in
    # the multi-binarizer's second pass when ``_bin_expected = 4`` is
    # used as the field default, because the scorer picks the recipe
    # whose output matches that wrong target.
    #
    # The fix: when the segmenter gives a confident multi-span count,
    # trust it directly. We use TWO independent paths:
    #   * ``_wf_visible_count``: visible digit-shaped spans (mirrors
    #     the existing height-consistency filter to drop label
    #     intrusion).
    #   * ``_wf_width_extra``: hidden digits inside wide-fused tiles
    #     (kept from the original block — covers the case where the
    #     single-recipe binarizer ALSO produces fusion).
    # The true count is the sum, capped at a sensible upper bound (7)
    # to defend against runaway over-counts from broken crops.
    #
    # The block is a no-op (early-exit) when COUNT-MISMATCH /
    # WIDTH-FUSION above already populated ``_hud_count_from_segmenter``
    # — no need to re-run the segmenter on the same crop.
    if (
        _digit_only_field
        and _hud_count_from_segmenter is None
        and field in ("mass", "resistance", "instability")
    ):
        try:
            _wf_gray = np.array(
                value_crop.convert("L"), dtype=np.uint8,
            )
            _wf_gray = _canonicalize_polarity(_wf_gray)
            _wf_bin = _adaptive_binarize(_wf_gray)
            _, _wf_boxes = _segment_glyphs(
                _wf_gray, _wf_bin, field=field,
            )
            # Collect digit-shaped candidates (mirror of the
            # COUNT-MISMATCH block's filter).
            _wf_candidates: list[tuple[int, int]] = []
            for (_gx, _gy, _gw, _gh) in (_wf_boxes or []):
                if _gh >= 8 and _gw >= 2:
                    _wf_candidates.append((_gh, _gw))
            # Require at least 2 candidates to compute a meaningful
            # median width and to clear the multi-digit plausibility
            # floor (a 1-span crop is either mass="0" or a broken
            # crop — neither benefits from a count override).
            if len(_wf_candidates) >= 2:
                _wf_heights = sorted(h for h, _ in _wf_candidates)
                _wf_median_h = _wf_heights[len(_wf_heights) // 2]
                _wf_h_lo = int(_wf_median_h * 0.60)
                _wf_h_hi = int(_wf_median_h * 1.40)
                _wf_widths = sorted(w for _, w in _wf_candidates)
                _wf_median_w = _wf_widths[len(_wf_widths) // 2]
                _wf_visible_count = 0
                _wf_width_extra = 0
                _wf_instability_dot = 0
                for (_ch, _cw) in _wf_candidates:
                    if _wf_h_lo <= _ch <= _wf_h_hi:
                        _wf_visible_count += 1
                    if _wf_median_w > 0:
                        _ratio = _cw / float(_wf_median_w)
                        if _ratio >= 1.6:
                            _wf_width_extra += min(
                                3, int(round(_ratio)) - 1,
                            )
                # Instability dot rescue — same logic as the
                # COUNT-MISMATCH block. The dot is shorter than the
                # digit-median by enough to fail the height filter
                # but tall enough (h~8-10) to pass the
                # ``_segment_glyphs`` h>=8 candidate gate. Pull it
                # back into the cascade target so PROPORTIONAL merge
                # doesn't collapse 5 real spans into 4 (the
                # ``"12.09" → "1209"`` failure mode).
                if (
                    field == "instability"
                    and _wf_median_w > 0
                    and _wf_median_h > 0
                ):
                    for (_ch, _cw) in _wf_candidates:
                        if (
                            _ch < _wf_h_lo
                            and _ch >= 4
                            and _cw <= max(4, int(_wf_median_w * 0.7))
                        ):
                            _wf_instability_dot = 1
                            break
                # Fire whenever we have a confident multi-span read.
                # The previous gate (``_wf_width_extra > 0``) was too
                # strict — it missed the common case where the first
                # binarizer pass already gives a clean split but the
                # multi-binarizer downstream needs the count to pick
                # the right recipe. Cap at 7 digits as a sanity bound;
                # legitimate HUD numerics fit well within that.
                if _wf_visible_count >= 2:
                    _wf_true = min(
                        7,
                        _wf_visible_count
                        + _wf_width_extra
                        + _wf_instability_dot,
                    )
                    log.info(
                        "sc_ocr.hud: STANDALONE SEGMENTER COUNT "
                        "field=%s (CRNN unavailable or _hudr_pass=False) "
                        "seg=%d visible + %d width-fused + %d "
                        "instability-dot → expected_count=%d, COUNT "
                        "ORACLE will steer binarizer recipe selection",
                        field, _wf_visible_count, _wf_width_extra,
                        _wf_instability_dot, _wf_true,
                    )
                    _hud_count_from_segmenter = _wf_true
        except Exception as _wf_exc:
            log.debug(
                "sc_ocr.hud: standalone width-fusion check failed "
                "(%s) — pipeline falls through unchanged",
                _wf_exc,
            )

    # ─── COUNT ORACLE ───
    # Use the HUD-RGB CRNN's read length as a count oracle for the
    # per-glyph segmenter, mirroring ``sc_ocr.signal``'s "CRNN COUNT
    # ORACLE". Only trust the oracle when the CRNN was reasonably
    # confident (mean >= 0.50) and produced a sensible character count
    # (1-7). When None, the segmenter behaves exactly as today.
    #
    # Failure mode this is meant to fix: on captures like ``mass=27265``
    # the adaptive binarizer occasionally fuses all five digits into a
    # single ~180-px mega-span. ``_segment_glyphs`` then under-counts
    # the row (leaving e.g. only an isolated leading "2") while the
    # CRNN reads "27265" correctly. When the CRNN gate doesn't trip
    # its accept threshold (mean < 0.85 plain, or mean < 0.40 with
    # plausibility), the per-glyph path takes over and emits "2". The
    # signature pipeline survives this exact failure mode because its
    # oracle says "expect 5 digits" and the proportional segmenter
    # then splits the mega-span into 5x36-px sub-spans for CNN
    # classification.
    _hud_expected_count: Optional[int] = None
    if _digit_only_field and field in ("mass", "resistance", "instability"):
        try:
            # Priority 1: when the segmenter caught the CRNN dropping
            # digits (COUNT-MISMATCH fired above), prefer the
            # segmenter's count. The CRNN that supplied the digit
            # string was just rejected as wrong — using its count to
            # drive binarization / forced merges produces the
            # 23694 → 2369 failure mode where the recipe selector is
            # steered to a wrong count and adjacent digits fuse.
            if _hud_count_from_segmenter is not None:
                _hud_expected_count = int(_hud_count_from_segmenter)
                log.info(
                    "sc_ocr.hud: COUNT ORACLE field=%s from segmenter "
                    "(via COUNT-MISMATCH, WIDTH-FUSION, or STANDALONE "
                    "WIDTH-FUSION above) -> expected_count=%d",
                    field, _hud_expected_count,
                )
            else:
                # Priority 2: use the HUD-RGB CRNN read length, same as
                # the signature pipeline. Only trust it when the CRNN
                # produced a sensible character count (1-7) at modest
                # confidence (mean >= 0.50). ``_hud_rgb_crnn`` was set
                # in the gate above — guard with ``locals().get`` so a
                # future refactor doesn't NameError this lookup.
                _oracle_crnn = locals().get("_hud_rgb_crnn", None)
                if _oracle_crnn is not None:
                    _oracle_text, _oracle_mean = _oracle_crnn
                    _oracle_digits = "".join(
                        c for c in _oracle_text if c.isdigit()
                    )
                    if (
                        1 <= len(_oracle_digits) <= 7
                        and _oracle_mean >= 0.50
                    ):
                        _hud_expected_count = len(_oracle_digits)
                        log.debug(
                            "sc_ocr.hud: COUNT ORACLE text=%r digits=%r "
                            "mean=%.2f -> expected_count=%d for field=%s",
                            _oracle_text, _oracle_digits, _oracle_mean,
                            _hud_expected_count, field,
                        )
        except Exception as _oracle_exc:
            log.debug(
                "sc_ocr.hud: count-oracle hook failed (%s) — "
                "segmenter will run without expected_count",
                _oracle_exc,
            )

    # ─── CUSTOM ONNX MODEL (priority for digit fields) ───
    # The user-trained 28x28 CNN classifier (in fallback._session) is
    # ~99% accurate on real SC HUD glyphs — beats both CRNN
    # (synth-trained) and Tesseract (general-purpose) on this domain.
    # Run it FIRST for digit-only fields. If it returns confident
    # output, use it directly and skip CRNN+Tesseract entirely.
    if _digit_only_field:
        try:
            # ── Lanczos upscale to match per-glyph CNN training scale ──
            # The HUD per-glyph CNN (model_hud_cnn.onnx) trains on
            # 28×28 grayscale tiles where digits occupy ~24 px of the
            # height. The segmenter (``_segment_glyphs``) downsamples
            # whatever crop it gets to 28×28 internally. When the
            # input value crop is tiny (typically 14-30 px tall at
            # 1080p capture resolution), that 28×28 final tile is the
            # result of a 14→28 bilinear UPSCALE = blurry. Lifting the
            # crop to ~48 px height first means the 28×28 result is a
            # slight DOWNSCALE = sharp, matching the training-crop
            # distribution. Direct port of the signature pipeline's
            # "stretched + Lanczos to ~32px" preprocessing step (with
            # ``H_TARGET = 48`` to match the HUD CRNN's training-size
            # convention rather than the signature's 32).
            #
            # Skip the upscale when the crop is already large enough
            # (h >= 36): the marginal gain is small, the cost
            # (LANCZOS + downstream binarization on the larger array)
            # is not. For oversized crops (h > 48) we likewise skip —
            # the downstream segmenter handles those fine and any
            # downscale here would just lose pixel info.
            _PERGLYPH_H_TARGET = 48
            _PERGLYPH_UPSCALE_THRESHOLD = 36
            _vc_for_cnn = value_crop
            _vc_h = int(value_crop.height)
            _vc_w = int(value_crop.width)
            # Track scale so we can map upscaled-crop coords (used by
            # the segmenter / gray / bin arrays) back to native coords
            # for the debug overlay push.
            _perglyph_scale: float = 1.0
            if _vc_h < _PERGLYPH_UPSCALE_THRESHOLD and _vc_h > 0:
                try:
                    _new_w_cnn = max(8, int(round(
                        _PERGLYPH_H_TARGET / max(1, _vc_h) * _vc_w
                    )))
                    _vc_for_cnn = value_crop.resize(
                        (_new_w_cnn, _PERGLYPH_H_TARGET),
                        Image.LANCZOS,
                    )
                    _perglyph_scale = _PERGLYPH_H_TARGET / float(_vc_h)
                    log.info(
                        "sc_ocr.hud: stretched + Lanczos to %dpx "
                        "(h=%d→%d, w=%d→%d) — per-glyph path",
                        _PERGLYPH_H_TARGET, _vc_h,
                        _PERGLYPH_H_TARGET, _vc_w, _new_w_cnn,
                    )
                except Exception as _up_exc:
                    log.debug(
                        "sc_ocr.hud: per-glyph Lanczos upscale "
                        "failed (%s) — using native crop",
                        _up_exc,
                    )
                    _vc_for_cnn = value_crop
                    _perglyph_scale = 1.0
            _rgb_pri = np.array(_vc_for_cnn.convert("RGB"), dtype=np.uint8)
            _gray_pri = np.array(_vc_for_cnn.convert("L"), dtype=np.uint8)
            # Background-agnostic polarity normalization (handles
            # bright-sky panels where median-based inversion fails).
            # After canonicalization text is BRIGHT (matches CNN's
            # training convention: bright glyphs on dark bg, padded
            # with white in _segment_glyphs).
            _gray_pri = _canonicalize_polarity(_gray_pri)
            # Multi-recipe binarization replaces the single-recipe
            # adaptive threshold. Tries 7 binarization variants
            # (Otsu / percentile 60/70/80 / adaptive w11 / adaptive
            # w21 / legacy) and picks whichever produces a column-
            # projection span count CLOSEST to the expected digit
            # count, with heavy penalties for noise-flood (lots of
            # tiny spans) and mega-blob (entire crop fused into one
            # span). Direct port of the signature pipeline's
            # ``_adaptive_binarize_multi`` adoption — fixes the
            # bright-sandy-background failure mode where the single
            # ``_adaptive_binarize`` recipe either floods the mask
            # with noise spans or fuses everything into one giant
            # blob.
            #
            # The ``expected_count`` parameter drives the scorer's
            # distance term. When the HUD-RGB CRNN count oracle ran
            # and produced a confident read, use its digit count as
            # ``expected_count`` (matches the signature pipeline's
            # CRNN-as-count-oracle pattern). Otherwise use a sane
            # field-specific default — the scorer's noise + mega
            # penalties dominate the distance term so an off-by-2
            # default is fine.
            if _hud_expected_count is not None:
                _bin_expected = _hud_expected_count
            elif field == "mass":
                _bin_expected = 4
            elif field == "resistance":
                _bin_expected = 2
            elif field == "instability":
                _bin_expected = 3
            else:
                _bin_expected = 4
            _bin_pri = _adaptive_binarize_multi(
                _gray_pri, expected_count=_bin_expected,
            )
            _primary_crops, _primary_boxes = _segment_glyphs(
                _gray_pri, _bin_pri, field=field,
            )
            # Chromatic-ghost phantom boxes (instability only) — drop
            # single-channel fringe boxes before the count logic sees
            # them, so the CRNN's ghost-inflated count can't validate a
            # phantom-inflated segmentation. NOTE: boxes are in the
            # (possibly Lanczos-upscaled) _vc_for_cnn coordinate space,
            # so the RGB must come from the SAME image — passing the
            # native value_crop makes the geometry check bail silently.
            _filter_drops_begin(field)
            _primary_crops, _primary_boxes = _filter_chromatic_ghost_boxes(
                _vc_for_cnn, _primary_crops, _primary_boxes, field=field,
            )
            # White-core ghosts (every CNN reads them as real digits) are
            # only separable by GEOMETRY — fixed-pitch violation.
            _primary_crops, _primary_boxes = _filter_pitch_ghost_boxes(
                _primary_crops, _primary_boxes, field=field,
            )
            # Runtime ladder: quarantine veto + geometry envelope on the
            # boxes, then centroid recentering on the surviving crops.
            _primary_crops, _primary_boxes = _filter_runtime_junk_boxes(
                _primary_crops, _primary_boxes, field=field,
            )
            _primary_crops = _recenter_crops_to_centroid(
                _primary_crops, field=field,
            )
            # Diagnostic: log how many glyphs the segmenter found.
            # Distinguishes "segmenter dropped digits" (pipeline issue)
            # from "classifier misread clean digits" (training issue).
            log.debug(
                "sc_ocr.diag: field=%s segment(primary)=%d glyphs",
                field, len(_primary_crops) if _primary_crops else 0,
            )
            # ── PROPORTIONAL split/merge to match CRNN count oracle ──
            # Mirror of the signature pipeline's
            # ``_split_wide_signature_spans`` / ``_merge_narrow_signature_spans``
            # post-processing, ported into reusable helpers in
            # :mod:`segment_helpers` so this file doesn't carry the
            # signature-specific assumptions (comma handling, lexicon).
            #
            # Triggers ONLY when:
            #   * The CRNN count oracle is set (mean conf >= 0.50 + 1-7
            #     digits read).
            #   * The segmenter's count differs from the oracle's count.
            #   * The gap is small (<=3) — a larger gap is a different
            #     failure mode (probably the segmenter ran on garbage)
            #     and forcing a count would just fabricate digit slots
            #     that don't correspond to real ink.
            #
            # The principal failure mode this fixes: binarization
            # occasionally fuses ALL digits of a mass=27265-class value
            # into a single ~180-px mega-span. _segment_glyphs then
            # under-counts the row, leaving only an isolated leading
            # digit. The CNN reads e.g. "2" while the CRNN reads
            # "27265" correctly. On captures where the CRNN gate's
            # confidence falls below the accept bar, the per-glyph path
            # silently emits the truncated read.
            # The CRNN count oracle counts DIGITS, but instability always
            # renders exactly one decimal point that the segmenter emits as
            # its OWN span. So for instability the expected SEGMENT count is
            # digits + 1 whenever a dot-sized span is actually present.
            # Without this, a correct 4-span "1.43" ([1][.][4][3]) is
            # force-MERGED down to the 3-digit oracle count, collapsing the
            # leading digit into the decimal ([1.][4][3]); that merged "1."
            # blob misreads as "%", the % is stripped as a non-digit, and
            # the value loses its leading digit (user 2026-06-02, on
            # current-resolution 448x670 panels: "1.43"->"4.3",
            # "0.99"->"9.9", "21.47"->"2.47"). Gated on a narrow span
            # existing so we only add the dot slot when the segmenter
            # genuinely separated it. Only the SEGMENT-count target moves;
            # _hud_expected_count (the digit cascade target downstream) is
            # untouched.
            _seg_expected = _hud_expected_count
            if (
                field == "instability"
                and _hud_expected_count is not None
                and _primary_boxes
            ):
                _seg_ws = sorted(int(_b[2]) for _b in _primary_boxes)
                _seg_med = _seg_ws[len(_seg_ws) // 2] if _seg_ws else 0
                _has_dot_span = any(
                    _seg_med > 0 and _w < 0.55 * _seg_med for _w in _seg_ws
                )
                if _has_dot_span:
                    _seg_expected = _hud_expected_count + 1
            if (
                _seg_expected is not None
                and _primary_boxes
                and abs(len(_primary_boxes) - _seg_expected) <= 3
                and len(_primary_boxes) != _seg_expected
            ):
                try:
                    from . import segment_helpers as _seg_helpers
                    _n_seg = len(_primary_boxes)
                    if _n_seg < _seg_expected:
                        # COUNT-ORACLE SANITY (live 2026-06-09): splitting
                        # needs EVIDENCE of a fusion — a span markedly wider
                        # than the median. The CRNN read a red-glow "0.99"
                        # crop as '4099', the dot logic then expected 5
                        # segments, and the splitter halved the perfectly
                        # segmented '0' into two garbage tiles that every
                        # voter misread ('44.99'/'46.99'/'40.99', winner
                        # '4099'). If no span is >= 1.45x the median there
                        # is nothing fused to split — the segmenter's count
                        # is right and the CRNN count is poisoned, so keep
                        # the segmenter's boxes.
                        _split_ws = sorted(
                            int(_b[2]) for _b in _primary_boxes
                        )
                        _sw_med = _split_ws[len(_split_ws) // 2]
                        if _sw_med > 0 and _split_ws[-1] >= 1.45 * _sw_med:
                            _new_boxes = (
                                _seg_helpers.split_wide_spans_to_count(
                                    _primary_boxes,
                                    target_count=_seg_expected,
                                    binary=_bin_pri,
                                )
                            )
                        else:
                            _new_boxes = None
                            log.info(
                                "sc_ocr.hud: PROPORTIONAL split SKIPPED "
                                "(field=%s, %d boxes, expected=%d): widest "
                                "span %.2fx median — no fusion evidence, "
                                "trusting segmenter count over CRNN",
                                field, _n_seg, _seg_expected,
                                (_split_ws[-1] / _sw_med) if _sw_med else 0.0,
                            )
                        if _new_boxes and len(_new_boxes) == _seg_expected:
                            log.info(
                                "sc_ocr.hud: PROPORTIONAL split %d -> %d "
                                "boxes (field=%s, expected=%d seg-count)",
                                _n_seg, len(_new_boxes), field,
                                _seg_expected,
                            )
                            _new_crops = _seg_helpers.extract_crops_from_boxes(
                                _gray_pri, _bin_pri, _new_boxes,
                            )
                            if len(_new_crops) == len(_new_boxes):
                                _primary_boxes = _new_boxes
                                _primary_crops = _new_crops
                            else:
                                log.debug(
                                    "sc_ocr.hud: PROPORTIONAL split crops "
                                    "mismatch (%d boxes vs %d crops) — "
                                    "keeping original segmenter output",
                                    len(_new_boxes), len(_new_crops),
                                )
                    else:  # _n_seg > _seg_expected
                        _new_boxes = _seg_helpers.merge_narrow_spans_to_count(
                            _primary_boxes,
                            target_count=_seg_expected,
                        )
                        if _new_boxes and len(_new_boxes) == _seg_expected:
                            log.info(
                                "sc_ocr.hud: PROPORTIONAL merge %d -> %d "
                                "boxes (field=%s, expected=%d seg-count)",
                                _n_seg, len(_new_boxes), field,
                                _seg_expected,
                            )
                            _new_crops = _seg_helpers.extract_crops_from_boxes(
                                _gray_pri, _bin_pri, _new_boxes,
                            )
                            if len(_new_crops) == len(_new_boxes):
                                _primary_boxes = _new_boxes
                                _primary_crops = _new_crops
                            else:
                                log.debug(
                                    "sc_ocr.hud: PROPORTIONAL merge crops "
                                    "mismatch (%d boxes vs %d crops) — "
                                    "keeping original segmenter output",
                                    len(_new_boxes), len(_new_crops),
                                )
                except Exception as _prop_exc:
                    log.debug(
                        "sc_ocr.hud: PROPORTIONAL split/merge failed "
                        "(%s) — keeping original segmenter output",
                        _prop_exc,
                    )
            # Push glyph bboxes to the live debug overlay so the user
            # can validate the segmenter independently of the OCR text
            # read (count match, per-glyph size, vertical position).
            # Best-effort; never blocks the OCR pipeline.
            #
            # Scale boxes back to native (pre-Lanczos-upscale) crop
            # coords so the overlay renders them at the right size
            # inside the panel-coords ``set_value_crop`` rectangle.
            # When the upscale didn't run, _perglyph_scale == 1.0 and
            # this is a no-op.
            try:
                from . import debug_overlay as _dbg_glyph
                if _perglyph_scale != 1.0 and _primary_boxes:
                    _inv = 1.0 / _perglyph_scale
                    _native_boxes = [
                        (
                            int(round(_bx * _inv)),
                            int(round(_by * _inv)),
                            max(1, int(round(_bw * _inv))),
                            max(1, int(round(_bh * _inv))),
                        )
                        for (_bx, _by, _bw, _bh) in _primary_boxes
                    ]
                else:
                    _native_boxes = _primary_boxes or []
                _dbg_glyph.set_glyph_boxes(field, _native_boxes)
            except Exception as _dbg_glyph_exc:
                log.debug(
                    "sc_ocr: live-overlay glyph-box push (primary) "
                    "failed: %s", _dbg_glyph_exc,
                )
            if _primary_crops:
                _primary_results = _classify_crops(_primary_crops)
                # ── Box-size dot detector (post-processing) ──
                # CNN normalizes every glyph to 28×28 — that strips the
                # pre-norm size signal that distinguishes a 4-px-wide
                # `.` from a 16-px-wide digit. Override CNN at positions
                # whose source bbox is much smaller than its peers.
                _dot_labels_pri = _dot_label_from_box(_primary_boxes)
                # Pre-compute medians once for the diagnostic log.
                if _primary_boxes:
                    _ws = sorted(
                        (w for (_x, _y, w, _h) in _primary_boxes),
                        reverse=True,
                    )
                    _hs = sorted(
                        (h for (_x, _y, _w, h) in _primary_boxes),
                        reverse=True,
                    )
                    _half = max(1, len(_ws) // 2)
                    _med_w_pri = float(np.median(_ws[:_half]))
                    _med_h_pri = float(np.median(_hs[:_half]))
                else:
                    _med_w_pri = 0.0
                    _med_h_pri = 0.0
                for _i, _dl in enumerate(_dot_labels_pri):
                    if _dl == ".":
                        _prev_ch, _prev_conf = _primary_results[_i]
                        _primary_results[_i] = (".", 1.0)
                        _bx = _primary_boxes[_i]
                        log.info(
                            "sc_ocr.%s: box-size dot detector overrode "
                            "CNN at idx=%d (was=%r conf=%.2f, "
                            "box w=%d h=%d, median_w=%.1f median_h=%.1f)",
                            field, _i, _prev_ch, _prev_conf,
                            _bx[2], _bx[3], _med_w_pri, _med_h_pri,
                        )

                # ── Backtrack adoption for `.` ↔ digit (Improvement 2) ──
                # Mirror of the signal pipeline's "adopted N segmenter-
                # backtracked classification(s) over CNN '@' reads" path:
                # when the per-glyph CNN says `.` at a position whose
                # source bbox is FULL-DIGIT-SIZED and the field grammar
                # forbids `.` at that position, drop the CNN's `.` and
                # adopt its top-2 softmax pick.
                #
                # Guards (every condition must hold to backtrack):
                #
                #   * Primary CNN's top-1 char is `.`
                #   * Box-size detector did NOT agree it's a dot
                #     (``_dot_labels_pri[i] is None``) — i.e. the source
                #     bbox is full-digit-sized
                #   * Direct bbox-size backstop: w >= 6 AND h >= 14
                #     (matches the task spec; the dot detector uses
                #     median-relative ratios which already cover this
                #     but the explicit floor catches edge cases where
                #     the digit median is small)
                #   * Field grammar disallows `.` at this position:
                #       mass: never allows `.` (any position)
                #       resistance: never allows `.` (any position)
                #       instability: allows exactly ONE `.` somewhere
                #         in the middle. We only veto a `.` when its
                #         peer positions already include another `.` —
                #         a legitimate instability decimal stays.
                #   * The top-2 candidate is a DIGIT (0-9) — if top-2
                #     is `%` or another non-digit we don't blindly swap
                #
                # Top-2 is lazily computed: only call ``_classify_crops_topk``
                # when we have at least one candidate position to swap.
                # Mirrors the cost-conscious lazy-call pattern used by
                # the signature pipeline's lexicon backtracker.
                _backtrack_indices: list[int] = []
                if _primary_results and _primary_boxes:
                    # Field-grammar predicate: returns True if a `.` at
                    # this position is impossible given the field.
                    def _dot_forbidden(_pos: int) -> bool:
                        if field == "mass":
                            return True  # mass is integer
                        if field == "resistance":
                            return True  # resistance is integer % or 0
                        if field == "instability":
                            # instability has at most one '.' across all
                            # positions. If ANOTHER position already
                            # carries a '.' (CNN or box-size override),
                            # an additional '.' is impossible.
                            for _j, (_ch, _cf) in enumerate(
                                _primary_results,
                            ):
                                if _j == _pos:
                                    continue
                                if _ch == "." or _dot_labels_pri[_j] == ".":
                                    return True
                            # Also: positions at the very start or very
                            # end of the strip can't be decimals (the
                            # leading or trailing position must be a
                            # digit). Span at position 0 or at last
                            # index being a '.' is implausible.
                            if _pos == 0 or _pos == len(_primary_results) - 1:
                                return True
                            return False
                        return False
                    for _i, (_ch, _cf) in enumerate(_primary_results):
                        if _ch != ".":
                            continue
                        if _dot_labels_pri[_i] == ".":
                            # Box-size detector ALREADY agreed it's a
                            # dot — trust the structural signal, don't
                            # backtrack a real dot.
                            continue
                        if _i >= len(_primary_boxes):
                            continue
                        _bx, _by, _bw, _bh = _primary_boxes[_i]
                        if _bw < 6 or _bh < 14:
                            # Span is too small to be a digit — let
                            # this case fall through to the box-size
                            # detector / consensus voters. Don't
                            # backtrack a small span.
                            continue
                        if not _dot_forbidden(_i):
                            continue
                        _backtrack_indices.append(_i)
                if _backtrack_indices:
                    try:
                        _topk = _classify_crops_topk(_primary_crops, k=3)
                    except Exception as _topk_exc:
                        log.debug(
                            "sc_ocr.hud: top-K backtrack inference "
                            "failed (%s) — keeping original CNN '.' reads",
                            _topk_exc,
                        )
                        _topk = []
                    if _topk and len(_topk) == len(_primary_crops):
                        for _i in _backtrack_indices:
                            _topk_row = _topk[_i] if _i < len(_topk) else []
                            if not _topk_row:
                                continue
                            # Find the highest-ranked DIGIT in the top-K.
                            _adopted: Optional[tuple[str, float]] = None
                            for _alt_ch, _alt_conf in _topk_row:
                                if _alt_ch.isdigit():
                                    _adopted = (_alt_ch, float(_alt_conf))
                                    break
                            if _adopted is None:
                                continue
                            _prev_ch, _prev_conf = _primary_results[_i]
                            _primary_results[_i] = _adopted
                            _bx, _by, _bw, _bh = _primary_boxes[_i]
                            log.info(
                                "sc_ocr.hud: backtrack-adopt field=%s "
                                "position=%d CNN said='.' (span %dx%d "
                                "too big) -> adopted %r (top-K softmax, "
                                "conf=%.2f, field grammar forbids '.' here)",
                                field, _i, _bw, _bh,
                                _adopted[0], _adopted[1],
                            )

                _txt_pri = "".join(ch for ch, _ in _primary_results)
                _confs_pri = [c for _, c in _primary_results]
                # Per-glyph debug dump for the live glyph-reader viewer.
                _dump_glyphs(
                    field, "primary", _primary_crops, _primary_results,
                    overrides=_dot_labels_pri,
                )
                # Diagnostic: log per-glyph classifier output with
                # confidence regardless of confidence-gate. Lets us
                # see whether ONNX is e.g. reading a `1`-shaped crop
                # as `7` with 0.80 confidence (classifier problem) or
                # whether segmentation only ever fed it 1 crop
                # (pipeline problem).
                if _primary_results:
                    _per_glyph = " ".join(
                        f"({ch},{conf:.2f})"
                        for ch, conf in _primary_results
                    )
                    log.debug(
                        "sc_ocr.diag: field=%s classify(primary)=%r "
                        "per-glyph=%s",
                        field, _txt_pri, _per_glyph,
                    )

                # ── Secondary inverted-polarity ONNX (always runs) ──
                # Reuse the primary path's crops (canonical polarity:
                # bright text on dark bg) and invert them, then classify
                # with the SIBLING inverted-polarity ONNX model. Different
                # polarity AND different weights → maximally decorrelated
                # peer voter; agreement between primary and secondary is
                # a strong high-confidence signal.
                #
                # This block was previously located AFTER the primary
                # high-confidence early-return AND after the parallel-
                # vote winner return — meaning it almost never executed
                # on real scans. Hoisted up here so secondary results
                # ALWAYS dump for the viewer regardless of which voter
                # wins downstream.
                #
                # Graceful degradation: if model_cnn_inv.onnx isn't on
                # disk yet, _classify_crops_inv returns []; we still
                # dump the inverted crops with placeholder labels so
                # the user can SEE what the secondary would classify.
                # Pre-initialize secondary outputs so the gate below can
                # reference them even when _classify_crops_inv raises
                # (model not on disk, polarity invert fails, etc.).
                _txt_sec: str = ""
                _confs_sec: list[float] = []
                try:
                    _secondary_crops_pri = [
                        np.clip(1.0 - c, 0.0, 1.0).astype(np.float32)
                        for c in _primary_crops
                    ]
                    _secondary_results_pri = _classify_crops_inv(
                        _secondary_crops_pri
                    )
                    if not _secondary_results_pri:
                        _secondary_results_pri = [
                            ("?", 0.0) for _ in _secondary_crops_pri
                        ]
                    # Apply the same box-size dot override to the
                    # secondary voter so primary/secondary agreement on
                    # a `.` position isn't broken by a stray CNN
                    # misread on the inverted-polarity model.
                    for _j, _dl in enumerate(_dot_labels_pri):
                        if _dl == ".":
                            _secondary_results_pri[_j] = (".", 1.0)
                    # Apply the same `.` ↔ digit backtrack to the
                    # secondary voter so it doesn't carry stale `.`
                    # reads at positions where primary already
                    # backtracked. Lazy top-K only when there's at
                    # least one secondary position to fix.
                    _sec_backtrack_indices: list[int] = []
                    for _j, _bt in enumerate(_backtrack_indices):
                        # Mirror the primary's backtrack indices when
                        # the secondary also currently classifies that
                        # position as '.'. Avoids needlessly running
                        # top-K when secondary already has a digit.
                        pass
                    for _j, (_ch, _cf) in enumerate(
                        _secondary_results_pri,
                    ):
                        if _ch != ".":
                            continue
                        # Reuse the same gating predicates as primary:
                        # box-size detector didn't agree, span is full-
                        # digit-sized, field grammar forbids.
                        if _j >= len(_dot_labels_pri):
                            continue
                        if _dot_labels_pri[_j] == ".":
                            continue
                        if _j >= len(_primary_boxes):
                            continue
                        _bsx, _bsy, _bsw, _bsh = _primary_boxes[_j]
                        if _bsw < 6 or _bsh < 14:
                            continue
                        # Field-grammar predicate, computed against the
                        # POST-PRIMARY-BACKTRACK results (so e.g. on
                        # instability we don't veto a legit single '.')
                        _veto = False
                        if field == "mass" or field == "resistance":
                            _veto = True
                        elif field == "instability":
                            for _k, (_pch, _) in enumerate(_primary_results):
                                if _k == _j:
                                    continue
                                if _pch == "." or _dot_labels_pri[_k] == ".":
                                    _veto = True
                                    break
                            if _j == 0 or _j == len(
                                _secondary_results_pri,
                            ) - 1:
                                _veto = True
                        if _veto:
                            _sec_backtrack_indices.append(_j)
                    if _sec_backtrack_indices:
                        try:
                            _topk_sec = _classify_crops_inv_topk(
                                _secondary_crops_pri, k=3,
                            )
                        except Exception as _topk_sec_exc:
                            log.debug(
                                "sc_ocr.hud: secondary top-K backtrack "
                                "inference failed (%s)",
                                _topk_sec_exc,
                            )
                            _topk_sec = []
                        if _topk_sec and len(_topk_sec) == len(
                            _secondary_crops_pri,
                        ):
                            for _j in _sec_backtrack_indices:
                                _row = (
                                    _topk_sec[_j]
                                    if _j < len(_topk_sec) else []
                                )
                                if not _row:
                                    continue
                                _adopted_sec: Optional[
                                    tuple[str, float]
                                ] = None
                                for _alt_ch, _alt_conf in _row:
                                    if _alt_ch.isdigit():
                                        _adopted_sec = (
                                            _alt_ch, float(_alt_conf),
                                        )
                                        break
                                if _adopted_sec is None:
                                    continue
                                _secondary_results_pri[_j] = _adopted_sec
                                log.debug(
                                    "sc_ocr.hud: secondary backtrack-"
                                    "adopt field=%s position=%d "
                                    "'.'->%r conf=%.2f",
                                    field, _j,
                                    _adopted_sec[0], _adopted_sec[1],
                                )
                    _dump_glyphs(
                        field, "secondary",
                        _secondary_crops_pri, _secondary_results_pri,
                        overrides=_dot_labels_pri,
                    )
                    _txt_sec = "".join(
                        ch for ch, _ in _secondary_results_pri
                    )
                    _confs_sec = [c for _, c in _secondary_results_pri]
                    if _txt_sec and _txt_sec != "?" * len(_txt_sec):
                        _per_glyph_sec = " ".join(
                            f"({ch},{conf:.2f})"
                            for ch, conf in _secondary_results_pri
                        )
                        log.debug(
                            "sc_ocr.diag: field=%s classify(secondary)=%r "
                            "per-glyph=%s",
                            field, _txt_sec, _per_glyph_sec,
                        )
                except Exception as _sec_exc:
                    log.debug(
                        "sc_ocr: secondary inverted classifier failed: %s",
                        _sec_exc,
                    )

                # ── HUD-RGB tertiary voter (side voter, additive) ──
                # The RGB-input HUD CNN is loaded from a SEPARATE
                # session (model_hud_rgb_cnn.onnx) — never substituted
                # for any signature model. It re-classifies the same
                # bboxes the gray segmenter produced, but with the
                # ORIGINAL RGB pixels preserved (cyan-vs-bg colour,
                # chromatic-aberration ringing). This gives a third
                # decorrelated vote on top of (primary=gray HUD CNN)
                # + (secondary=inverted gray HUD CNN).
                #
                # Used as an ADDITIVE acceptance lane below — never
                # rejects a read, only provides a new path to accept
                # when primary+RGB agree but secondary disagreed.
                # Graceful absence: if model_hud_rgb_cnn.onnx isn't on
                # disk yet, the function returns [] and the tri-agree
                # gate just doesn't fire — strict / dual-agree still
                # work unchanged.
                _hud_rgb_results: list[tuple[str, float]] = []
                _txt_rgb: str = ""
                _confs_rgb: list[float] = []
                try:
                    if _primary_boxes:
                        # Re-crop the primary bboxes from the ORIGINAL
                        # RGB strip with the same pad+resize convention
                        # the extractor used at training time:
                        #   - pad 2 px white around the inked bbox
                        #   - BILINEAR resize to 28×28
                        # This keeps inference distribution aligned
                        # with the training distribution.
                        _Hr, _Wr = _rgb_pri.shape[:2]
                        _hud_rgb_tiles: list[np.ndarray] = []
                        for _b in _primary_boxes:
                            _bx, _by, _bw, _bh = _b
                            if _bw < 1 or _bh < 1:
                                _hud_rgb_tiles = []
                                break
                            if _bx + _bw > _Wr or _by + _bh > _Hr:
                                _hud_rgb_tiles = []
                                break
                            _sub = _rgb_pri[
                                _by:_by + _bh, _bx:_bx + _bw
                            ]
                            _pad = 2
                            _padded = np.full(
                                (_bh + _pad * 2, _bw + _pad * 2, 3),
                                255, dtype=np.uint8,
                            )
                            _padded[_pad:_pad + _bh, _pad:_pad + _bw] = _sub
                            from PIL import Image as _PIL_RGB_HUD
                            _pil = _PIL_RGB_HUD.fromarray(
                                _padded, mode="RGB",
                            ).resize((28, 28), _PIL_RGB_HUD.BILINEAR)
                            _hud_rgb_tiles.append(
                                np.asarray(_pil, dtype=np.uint8),
                            )
                        if (
                            _hud_rgb_tiles
                            and len(_hud_rgb_tiles) == len(_primary_boxes)
                        ):
                            _hud_rgb_results = _classify_crops_hud_rgb(
                                _hud_rgb_tiles,
                            )
                            if _hud_rgb_results:
                                # Apply the same box-size dot override
                                # to the RGB voter so its read agrees
                                # with primary/secondary on dot
                                # positions (the CNN class for '.' is
                                # 11 in the alphabet but the box-size
                                # check is structurally more reliable).
                                for _j, _dl in enumerate(_dot_labels_pri):
                                    if _dl == "." and _j < len(_hud_rgb_results):
                                        _hud_rgb_results[_j] = (".", 1.0)
                                # ── HUD-RGB `.` ↔ digit backtrack ──
                                # Same logic as primary/secondary: when
                                # the RGB CNN says `.` at a full-digit-
                                # sized span and the field grammar
                                # forbids `.`, swap to its own top-2.
                                _rgb_backtrack_indices: list[int] = []
                                for _j, (_ch, _cf) in enumerate(
                                    _hud_rgb_results,
                                ):
                                    if _ch != ".":
                                        continue
                                    if _j >= len(_dot_labels_pri):
                                        continue
                                    if _dot_labels_pri[_j] == ".":
                                        continue
                                    if _j >= len(_primary_boxes):
                                        continue
                                    _brx, _bry, _brw, _brh = (
                                        _primary_boxes[_j]
                                    )
                                    if _brw < 6 or _brh < 14:
                                        continue
                                    _veto_rgb = False
                                    if (
                                        field == "mass"
                                        or field == "resistance"
                                    ):
                                        _veto_rgb = True
                                    elif field == "instability":
                                        for _k, (_pch, _) in enumerate(
                                            _primary_results,
                                        ):
                                            if _k == _j:
                                                continue
                                            if (
                                                _pch == "."
                                                or _dot_labels_pri[_k] == "."
                                            ):
                                                _veto_rgb = True
                                                break
                                        if (
                                            _j == 0
                                            or _j == len(
                                                _hud_rgb_results,
                                            ) - 1
                                        ):
                                            _veto_rgb = True
                                    if _veto_rgb:
                                        _rgb_backtrack_indices.append(_j)
                                if _rgb_backtrack_indices:
                                    try:
                                        _topk_rgb = (
                                            _classify_crops_hud_rgb_topk(
                                                _hud_rgb_tiles, k=3,
                                            )
                                        )
                                    except Exception as _topk_rgb_exc:
                                        log.debug(
                                            "sc_ocr.hud: HUD-RGB top-K "
                                            "backtrack inference failed: %s",
                                            _topk_rgb_exc,
                                        )
                                        _topk_rgb = []
                                    if _topk_rgb and len(_topk_rgb) == len(
                                        _hud_rgb_tiles,
                                    ):
                                        for _j in _rgb_backtrack_indices:
                                            _rrow = (
                                                _topk_rgb[_j]
                                                if _j < len(_topk_rgb)
                                                else []
                                            )
                                            if not _rrow:
                                                continue
                                            _adopted_rgb: Optional[
                                                tuple[str, float]
                                            ] = None
                                            for (
                                                _alt_ch, _alt_conf,
                                            ) in _rrow:
                                                if _alt_ch.isdigit():
                                                    _adopted_rgb = (
                                                        _alt_ch,
                                                        float(_alt_conf),
                                                    )
                                                    break
                                            if _adopted_rgb is None:
                                                continue
                                            _hud_rgb_results[_j] = (
                                                _adopted_rgb
                                            )
                                            log.debug(
                                                "sc_ocr.hud: HUD-RGB "
                                                "backtrack-adopt field=%s "
                                                "position=%d '.'->%r "
                                                "conf=%.2f",
                                                field, _j,
                                                _adopted_rgb[0],
                                                _adopted_rgb[1],
                                            )
                                _txt_rgb = "".join(
                                    ch for ch, _ in _hud_rgb_results
                                )
                                _confs_rgb = [
                                    c for _, c in _hud_rgb_results
                                ]
                                if (
                                    _txt_rgb
                                    and _txt_rgb != "?" * len(_txt_rgb)
                                ):
                                    log.debug(
                                        "sc_ocr.diag: field=%s "
                                        "classify(hud_rgb)=%r",
                                        field, _txt_rgb,
                                    )
                                # Surface in the viewer as the
                                # `hud_rgb` voter tile row.
                                _dump_glyphs(
                                    field, "hud_rgb",
                                    _hud_rgb_tiles, _hud_rgb_results,
                                    overrides=_dot_labels_pri,
                                )
                except Exception as _rgb_exc:
                    log.debug(
                        "sc_ocr.hud: HUD-RGB voter swallowed: %s",
                        _rgb_exc,
                    )

                # ── STRUCTURAL ANCHOR CHECKS (resistance %, instability .) ──
                # Mirror of the signature scanner's comma-anchored
                # sanity check (``,`` is always interior in a digit
                # string like ``8,375``). The HUD numeric fields have
                # their own anchors:
                #
                #   * resistance: ``%`` is ALWAYS the rightmost glyph.
                #     A mid-string ``%`` (e.g. ``"%4"``, ``"4%5"``)
                #     means the segmenter fused or mis-ordered boxes.
                #   * instability: ``.`` is ALWAYS interior, with at
                #     least one digit on each side. A leading dot
                #     (e.g. ``".09"``) means the integer digit on the
                #     left was clipped — direct analogue of the
                #     signature scanner's comma-anchored crop
                #     extension trigger.
                #
                # When either anchor fails on the per-glyph primary
                # output, all three downstream accept gates (N-way
                # consensus, 3-lane primary-accept, joint CRNN+CNN)
                # are suppressed for THIS frame. The function then
                # falls through to the CRNN+Tesseract parallel vote
                # below, which has independent reading and is more
                # likely to either get the value right or produce
                # something the sticky-consensus / frozen-panel
                # mechanisms can reject downstream.
                #
                # We DON'T attempt leftward-crop-extension recovery
                # here: re-running the full per-glyph cascade on an
                # extended crop would require plumbing the source
                # gray strip + binarization recipe through this scope,
                # and the existing parallel-vote / fallback paths
                # already get a second look at the read. Structural
                # rejection is the cheaper, lower-risk option that
                # composes with the existing self-healing mechanisms.
                #
                # TODO: investigate leftward-crop-extension recovery
                # for the ``dot_leading`` case once the source-gray /
                # binary references are plumbed through to this scope
                # — the digit-pitch estimate is already available via
                # :func:`validate.estimate_digit_pitch`.
                _anchor_reject_pri = False
                if (
                    field in ("resistance", "instability")
                    and _txt_pri
                ):
                    try:
                        _anchor_ok, _anchor_reason = (
                            validate.check_hud_anchors(
                                _txt_pri, field, boxes=_primary_boxes,
                            )
                        )
                    except Exception as _anchor_exc:
                        log.debug(
                            "sc_ocr.hud: anchor-check exception "
                            "field=%s text=%r exc=%s — passing through",
                            field, _txt_pri, _anchor_exc,
                        )
                        _anchor_ok, _anchor_reason = True, ""
                    if not _anchor_ok:
                        _pitch_est = None
                        try:
                            _pitch_est = validate.estimate_digit_pitch(
                                _primary_boxes,
                            )
                        except Exception:
                            _pitch_est = None
                        log.info(
                            "sc_ocr.hud: %s %s anchor failed "
                            "(text=%r, reason=%s, len=%d, "
                            "digit_pitch_estimate=%r) — rejecting "
                            "per-glyph read, falling through to "
                            "CRNN+Tesseract vote",
                            field,
                            "%" if field == "resistance" else ".",
                            _txt_pri, _anchor_reason, len(_txt_pri),
                            _pitch_est,
                        )
                        _anchor_reject_pri = True

                # ── N-WAY CONSENSUS GATE (Improvement 1) ──
                # Mirror of the signature pipeline's N-way digit-position
                # consensus (``_vote_on_digit_string``). Combines four
                # voters at the PER-POSITION level instead of comparing
                # full strings:
                #
                #   1. CRNN per-position (collapsed CTC chars zipped to
                #      segmenter positions when lengths match)
                #   2. Primary HUD CNN (grayscale per-glyph)
                #   3. Secondary HUD CNN (inverted-polarity per-glyph)
                #   4. HUD-RGB CNN (RGB per-glyph)
                #
                # The position-level voter handles 4-of-4 / 3-of-N /
                # 2-of-N-decorrelated / 2-of-N-same-tier cases; the
                # composed string accepts when at least 3 voters are
                # available and mean conf ≥ 0.70. The existing 3-lane
                # gate (strict/dual-agree/rgb-agree) is kept as a
                # fallback for cases the consensus doesn't accept (e.g.
                # only 2 voters available, very low conf, or position
                # count mismatch with the CRNN). No regression risk —
                # the consensus only ADDS an accept path.
                _crnn_aligned_results: list[tuple[str, float]] = []
                try:
                    # Build CRNN per-position votes from the gate-0a HUD
                    # CRNN read when the collapsed length matches the
                    # segmenter count. This is the SAME alignment the
                    # signature pipeline uses: CRNN's CTC output post-
                    # collapse is one character per position when the
                    # count oracle worked.
                    _crnn_text_for_align = str(
                        locals().get("_hudr_text", "")
                    )
                    _crnn_mean_for_align = float(
                        locals().get("_hudr_mean", 0.0)
                    )
                    if (
                        _crnn_text_for_align
                        and _confs_pri
                        and len(_crnn_text_for_align) == len(_confs_pri)
                        and all(
                            c in "0123456789.%" for c in _crnn_text_for_align
                        )
                    ):
                        _crnn_aligned_results = [
                            (c, _crnn_mean_for_align)
                            for c in _crnn_text_for_align
                        ]
                except Exception as _align_exc:
                    log.debug(
                        "sc_ocr.hud: CRNN per-position alignment failed: %s",
                        _align_exc,
                    )
                    _crnn_aligned_results = []

                _nway_consensus = None
                try:
                    # The per-position consensus helper expects 4 voter
                    # slots in fixed order:
                    #   primary, secondary, rgb, rgb_inv.
                    # We have no separate ``rgb_inv`` HUD model — instead,
                    # we route the CRNN as the 4th voter (it provides
                    # decorrelated whole-strip CTC evidence). The voter
                    # name doesn't change the algorithm — it only labels
                    # tier groups for the 2-of-N-same-tier path. Since
                    # CRNN is structurally distinct from any per-glyph
                    # CNN, placing it in the ``rgb_inv`` slot keeps the
                    # gray/RGB tier split intact:
                    #   gray tier: primary, secondary (per-glyph gray)
                    #   rgb tier:  rgb (per-glyph RGB), rgb_inv (CRNN)
                    if (
                        _primary_results
                        and len(_primary_results) >= 1
                        and (
                            _secondary_results_pri
                            or _hud_rgb_results
                            or _crnn_aligned_results
                        )
                    ):
                        # Filter abstains: only pass a voter when it has
                        # at least one non-? char.
                        def _is_real(r):
                            return (
                                r
                                and any(ch != "?" for ch, _ in r)
                            )
                        _nway_consensus = _vote_on_digit_string(
                            primary_results=(
                                _primary_results
                                if _is_real(_primary_results) else None
                            ),
                            secondary_results=(
                                _secondary_results_pri
                                if _is_real(_secondary_results_pri)
                                else None
                            ),
                            rgb_results=(
                                _hud_rgb_results
                                if _is_real(_hud_rgb_results) else None
                            ),
                            rgb_inv_results=(
                                _crnn_aligned_results
                                if _is_real(_crnn_aligned_results)
                                else None
                            ),
                            lexicon=None,  # HUD values are continuous
                        )
                except Exception as _nway_exc:
                    log.debug(
                        "sc_ocr.hud: N-way consensus failed (%s) — "
                        "falling through to legacy 3-lane gate",
                        _nway_exc,
                    )
                    _nway_consensus = None

                _nway_pass = False
                _nway_str = ""
                _nway_mean = 0.0
                _nway_n = 0
                _nway_path = ""
                _nway_per_position: list[dict] = []
                if _nway_consensus and _nway_consensus.get("string"):
                    _nway_str = str(_nway_consensus["string"])
                    _nway_mean = float(
                        _nway_consensus.get("mean_confidence", 0.0)
                    )
                    _nway_n = int(
                        _nway_consensus.get("available_voters", 0)
                    )
                    _nway_path = str(
                        _nway_consensus.get("consensus_path", "?")
                    )
                    _nway_per_position = list(
                        _nway_consensus.get("per_position", []),
                    )
                    # ── Acceptance criteria ──
                    #   * ≥3 voters available (so 3-of-N / 4-of-4
                    #     can actually fire)
                    #   * string is the right field shape (digits +
                    #     optional . or %)
                    #   * length ≥ 2
                    #   * mean conf ≥ 0.70 (matches the existing
                    #     dual-agree / rgb-agree threshold)
                    _nway_chars_ok = (
                        _nway_str
                        and all(
                            c in "0123456789.%" for c in _nway_str
                        )
                        and len(_nway_str) >= 2
                    )
                    if (
                        _nway_chars_ok
                        and _nway_n >= 3
                        and _nway_mean >= 0.70
                    ):
                        _nway_pass = True
                # Structural-anchor recheck on the consensus winner —
                # the consensus string can differ from ``_txt_pri``
                # (per-position voter can pick chars from other voters
                # at positions where the primary was outvoted). Re-run
                # the % / . anchor check against the composed string
                # so we don't accept a mid-string ``%`` that the
                # consensus assembled from a mis-segmented primary.
                if _nway_pass and field in ("resistance", "instability"):
                    try:
                        _na_ok, _na_reason = validate.check_hud_anchors(
                            _nway_str, field, boxes=_primary_boxes,
                        )
                    except Exception:
                        _na_ok, _na_reason = True, ""
                    if not _na_ok:
                        log.info(
                            "sc_ocr.hud: %s %s anchor failed on N-way "
                            "consensus (text=%r reason=%s) — "
                            "rejecting consensus read",
                            field,
                            "%" if field == "resistance" else ".",
                            _nway_str, _na_reason,
                        )
                        _nway_pass = False
                if _nway_pass:
                    log.info(
                        "sc_ocr.hud: N-way consensus field=%s voters=%d "
                        "str=%r path=%s mean_conf=%.2f",
                        field, _nway_n, _nway_str, _nway_path, _nway_mean,
                    )
                    # Map the per-position consensus back to per-char
                    # confidences for the caller. ``per_position`` carries
                    # one entry per ORIGINAL segmenter position, including
                    # ones whose char dropped out of the composed string
                    # (e.g. '@' icon class). Filter to the digits that
                    # actually made it into ``_nway_str`` so the returned
                    # confidences line up 1:1 with the string.
                    _nway_confs: list[float] = []
                    for _p in _nway_per_position:
                        _ch = _p.get("char", "?")
                        if _ch in "0123456789.%":
                            _nway_confs.append(
                                float(_p.get("confidence", 0.0)),
                            )
                    if len(_nway_confs) != len(_nway_str):
                        # Fallback: use the mean for all positions if
                        # filtering didn't produce a 1:1 mapping (e.g.
                        # when the composed string dropped some chars).
                        _nway_confs = [_nway_mean] * len(_nway_str)
                    _dump_voter(field, "winner", _nway_str, _nway_mean)
                    _clear_viewer_entry(field, "crnn")
                    _clear_viewer_entry(field, "tesseract")
                    return _nway_str, _nway_confs

                # ── PRIMARY-ACCEPT GATE (legacy 3-lane fallback) ──
                # Three ways to accept the CNN reading and skip the
                # CRNN+Tesseract vote:
                #
                #   (a) STRICT: every primary glyph hit ≥ 0.85 conf
                #       (the original gate, kept for solo-voter cases).
                #
                #   (b) DUAL-AGREEMENT (added v2.2.7): primary AND
                #       secondary CNNs produced the EXACT same text,
                #       length ≥ 2, both with reasonable mean confidence.
                #       Two independent classifiers (different polarity,
                #       different weights) producing identical output is
                #       stronger evidence than a single classifier
                #       hitting the strict per-glyph floor — and gets
                #       the system out of the trap where one glyph at
                #       0.84 conf forces fall-through to CRNN+Tesseract,
                #       which on instability/resistance tends to
                #       hallucinate single-character nonsense like "5"
                #       or empty strings, blowing up the displayed value.
                #
                #   (c) RGB-AGREEMENT (added 2026-05): primary AND
                #       HUD-RGB CNNs produced the EXACT same text,
                #       length ≥ 2, both with mean conf ≥ 0.70. The
                #       RGB voter is colour-aware and sees chromatic
                #       aberration that the polarity-inverted secondary
                #       can be misled by; agreement between primary
                #       (gray, canonical polarity) and HUD-RGB
                #       (RGB, canonical polarity) gives a path to
                #       accept when secondary disagrees but the gray
                #       and colour pipelines independently converged
                #       on the same digit string. Side-voter contract
                #       — strictly additive, never vetoes.
                _gate_chars_ok = (
                    _txt_pri
                    and all(c in "0123456789.%" for c in _txt_pri)
                    and _confs_pri
                )
                _strict_pass = (
                    _gate_chars_ok and min(_confs_pri) >= 0.85
                )
                _dual_agree_pass = (
                    _gate_chars_ok
                    and _txt_pri == _txt_sec
                    and len(_txt_pri) >= 2
                    and _confs_sec
                    and all(c in "0123456789.%" for c in _txt_sec)
                    and (sum(_confs_pri) / len(_confs_pri)) >= 0.70
                    and (sum(_confs_sec) / len(_confs_sec)) >= 0.70
                )
                _rgb_agree_pass = (
                    _gate_chars_ok
                    and _txt_pri == _txt_rgb
                    and len(_txt_pri) >= 2
                    and _confs_rgb
                    and all(c in "0123456789.%" for c in _txt_rgb)
                    and (sum(_confs_pri) / len(_confs_pri)) >= 0.70
                    and (sum(_confs_rgb) / len(_confs_rgb)) >= 0.70
                )
                # Structural-anchor gate: suppress the 3-lane accept
                # when the primary text fails the %/. anchor check.
                # This composes with the existing strict / dual /
                # rgb-agree paths — those paths are conditioned on
                # _txt_pri AND its peers being identical, which means
                # all peers agree on a structurally broken string. We
                # still reject because the structural break is more
                # reliable evidence than peer agreement on a mis-
                # segmented read.
                if _anchor_reject_pri:
                    _strict_pass = False
                    _dual_agree_pass = False
                    _rgb_agree_pass = False
                if _strict_pass or _dual_agree_pass or _rgb_agree_pass:
                    _mean = sum(_confs_pri) / len(_confs_pri)
                    if _strict_pass:
                        _gate = "strict"
                    elif _dual_agree_pass:
                        _gate = "dual-agree"
                    else:
                        _gate = "rgb-agree"
                    log.debug(
                        "sc_ocr: PRIMARY field=%s text=%r mean=%.2f "
                        "(custom CNN, gate=%s)",
                        field, _txt_pri, _mean, _gate,
                    )
                    if _dual_agree_pass and not _strict_pass:
                        log.warning(
                            "[DIAG] dual-agreement gate accepted "
                            "field=%s text=%r (pri_mean=%.2f sec_mean=%.2f) "
                            "— would have fallen through to CRNN+Tesseract "
                            "under strict gate",
                            field, _txt_pri,
                            sum(_confs_pri) / len(_confs_pri),
                            sum(_confs_sec) / len(_confs_sec),
                        )
                    if (
                        _rgb_agree_pass
                        and not (_strict_pass or _dual_agree_pass)
                    ):
                        log.warning(
                            "[DIAG] rgb-agreement gate accepted "
                            "field=%s text=%r (pri_mean=%.2f rgb_mean=%.2f) "
                            "sec_text=%r — would have fallen through "
                            "to CRNN+Tesseract without HUD-RGB voter",
                            field, _txt_pri,
                            sum(_confs_pri) / len(_confs_pri),
                            sum(_confs_rgb) / len(_confs_rgb) if _confs_rgb else 0.0,
                            _txt_sec,
                        )
                    # Primary locked → it's the winner. Show in viewer.
                    _dump_voter(field, "winner", _txt_pri, _mean)
                    # Drop any stale CRNN / Tesseract entries from a
                    # prior scan — those blocks below are about to be
                    # skipped by the early return. We DON'T clear
                    # secondary anymore because it's now always dumped
                    # above (with current scan's data, never stale).
                    _clear_viewer_entry(field, "crnn")
                    _clear_viewer_entry(field, "tesseract")
                    return _txt_pri, _confs_pri

                # ── (1-JOINT) HUD-RGB CRNN ↔ per-glyph CNN agreement gate ──
                # Mirror of the signature pipeline's (0-JOINT) gate.
                # The per-glyph CNN's strict + dual-agree gates above
                # both failed (so the per-glyph stack alone wasn't
                # confident enough to commit). But if the HUD-RGB CRNN
                # gate-0a ALSO ran earlier and its read happens to
                # MATCH the per-glyph primary's read, that's two
                # genuinely independent reading mechanisms producing
                # the same string — much stronger evidence than either
                # one alone. Accept at lower confidence (≥0.55 mean
                # per-glyph) when the strings agree.
                #
                # This catches captures where:
                #   * gate-0a rejected the CRNN read (too low conf
                #     for the strict/plausible threshold)
                #   * AND per-glyph strict failed (one glyph below 0.85)
                #   * AND per-glyph dual-agree failed (primary != sec)
                #   * BUT both pipelines independently agreed
                #
                # Without this gate those captures fall through to the
                # legacy parallel CRNN+Tesseract vote which tends to
                # over-vote on borderline reads.
                _joint_ok = False
                _joint_crnn_text = ""
                _joint_crnn_mean = 0.0
                try:
                    # ``_hudr_text`` and ``_hudr_mean`` are set only when
                    # gate-0a's _classify_hud_value_via_crnn_rgb produced
                    # a non-None result earlier in this function. Use
                    # ``locals()`` lookup to be safe against scope edge
                    # cases (e.g. helper threw an exception before
                    # setting them).
                    _joint_crnn_text = str(locals().get("_hudr_text", ""))
                    _joint_crnn_mean = float(locals().get("_hudr_mean", 0.0))
                    if (
                        _gate_chars_ok
                        and _joint_crnn_text
                        and _joint_crnn_text == _txt_pri
                        and len(_txt_pri) >= 2
                    ):
                        # Lowered threshold: CRNN+CNN string agreement
                        # is itself strong evidence (two independent
                        # readers landing on the same string is hard
                        # to do by coincidence on a 4+ char value).
                        # 0.40 mirrors the signature CRNN's lexicon-
                        # confirmed gate threshold.
                        _joint_pri_mean = sum(_confs_pri) / len(_confs_pri)
                        if _joint_pri_mean >= 0.40:
                            _joint_ok = True
                except Exception as _joint_exc:
                    log.debug(
                        "sc_ocr.hud: joint-agree check failed: %s",
                        _joint_exc,
                    )
                if _joint_ok and _anchor_reject_pri:
                    # Same structural-anchor gate as the 3-lane accept
                    # above: if % / . is structurally wrong on _txt_pri
                    # (which is identical to _joint_crnn_text here by
                    # construction), don't accept just because the
                    # CRNN happens to agree on the broken string.
                    _joint_ok = False
                if _joint_ok:
                    _joint_pri_mean = sum(_confs_pri) / len(_confs_pri)
                    log.info(
                        "sc_ocr.hud: JOINT GATE accepted field=%s "
                        "text=%r (CRNN+CNN agree, cnn_mean=%.2f "
                        "crnn_mean=%.2f) — skipping CRNN+Tesseract vote",
                        field, _txt_pri, _joint_pri_mean, _joint_crnn_mean,
                    )
                    try:
                        _dump_voter(
                            field, "winner", _txt_pri, _joint_pri_mean,
                        )
                        _clear_viewer_entry(field, "crnn")
                        _clear_viewer_entry(field, "tesseract")
                    except Exception:
                        pass
                    return _txt_pri, _confs_pri
        except Exception as _exc:
            log.debug("sc_ocr: primary ONNX path failed: %s", _exc)

    # ── CRNN whole-strip read for digit fields (Tesseract removed) ──
    # The legacy CRNN+Tesseract parallel-vote block ran the SC-tuned
    # ``eng_sc`` Tesseract LSTM in parallel with the CRNN and voted
    # between them. With the HUD-RGB CRNN promoted to the authoritative
    # primary (the path above the segmenter) and the per-glyph CNNs
    # available as the segmenter-side voters, Tesseract no longer adds
    # value here — its subprocess startup cost (~150 ms) was the
    # dominant per-scan latency, and its char-whitelisted reads
    # consistently lost to the CRNN/CNN ensemble on the labelled
    # benchmark. Removed. The legacy CRNN (``_crnn_recognize``) still
    # runs as a low-priority hint feeding the per-glyph cascade
    # downstream, but it no longer drives a winner-takes-all vote.
    if _digit_only_field:
        _crnn_raw = _crnn_recognize(value_crop)
        _crnn_text, _crnn_confs = ("", [])
        if _crnn_raw is not None:
            _ctxt, _cconfs = _crnn_raw
            # Digit-mapping on CRNN output so the downstream consumers
            # see canonical digit characters.
            if _ctxt:
                _mapped = (_ctxt.replace("I", "1").replace("l", "1")
                                .replace("O", "0").replace("o", "0")
                                .replace("S", "5").replace("s", "5")
                                .replace("B", "8").replace("Z", "2")
                                .replace("G", "6").replace("q", "9"))
                _crnn_text = "".join(c for c in _mapped if c in "0123456789.%")
                _crnn_confs = _cconfs
        # Dump CRNN read for the live viewer (whole-crop engine — no
        # per-glyph tiles, just the text + mean conf).
        _dump_voter(
            field, "crnn", _crnn_text,
            (sum(_crnn_confs) / len(_crnn_confs)) if _crnn_confs else None,
        )
        # Clear any stale Tesseract entry from a previous scan so the
        # live viewer doesn't show a phantom voter.
        try:
            _clear_viewer_entry(field, "tesseract")
        except Exception:
            pass

    # Non-digit field: keep the original CRNN-first flow (letter text
    # can't be voted against the digit-only eng_sc model anyway).
    if not _digit_only_field:
        crnn_result = _crnn_recognize(value_crop)
        if crnn_result is not None:
            text, confs = crnn_result
            mean_conf = (sum(confs) / len(confs)) if confs else 0.0
            if text and mean_conf > 0.75:
                log.info(
                    "sc_ocr: crnn(text-field) text=%r mean=%.2f field=%s",
                    text, mean_conf, field,
                )
                return text, confs

    # Tesseract is only retained for non-digit fields (mineral_name
    # etc.) below. For digit-only fields we skip the import + binary
    # check + subprocess overhead entirely.
    if not _digit_only_field:
        import pytesseract
        # Ensure Tesseract binary path is configured
        from ..screen_reader import _check_tesseract
        _check_tesseract()

    W, H = value_crop.size

    # Auto-upscale small crops — keep the original upscale here so the
    # ONNX segmenter sees reasonable text sizes. Tesseract gets its own
    # more aggressive upscale below (see _tess_input).
    if H < 25:
        scale_up = max(2, 28 // max(1, H))
        value_crop = value_crop.resize(
            (W * scale_up, H * scale_up), Image.LANCZOS,
        )

    rgb = np.array(value_crop.convert("RGB"), dtype=np.uint8)
    gray = np.array(value_crop.convert("L"), dtype=np.uint8)
    max_ch = rgb.max(axis=2).astype(np.uint8)
    median = float(np.median(gray))

    # Polarity correction
    if median > 140:
        gray = 255 - gray
        max_ch = 255 - max_ch

    # ── Tesseract (primary) ──
    # Feed the raw value crop DIRECTLY — Tesseract's internal
    # preprocessing (adaptive threshold, upscaling) handles the
    # SC HUD font better than our manual pipeline. Verified:
    # raw crop → "499" correct; 4x+Otsu+flip → empty or wrong.
    #
    # Uses the SC-specific Tesseract LSTM (``eng_sc.traineddata``)
    # from the SC-Datarunner-UEX project when available. That model
    # is fine-tuned on SC HUD renderings and is dramatically more
    # robust than default ``eng`` at scale — default eng hallucinates
    # characters (e.g. '499' → '43%' at 4× upscale) while eng_sc
    # reads '499' stably. Our local tessdata dir ships the SC model
    # alongside a copy of eng for fallback.
    import os as _os
    _tessdata_local = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
        "ocr", "tessdata",
    )
    _have_sc = _os.path.isfile(_os.path.join(_tessdata_local, "eng_sc.traineddata"))
    _tess_env = _os.environ.copy()
    if _have_sc:
        _tess_env["TESSDATA_PREFIX"] = _tessdata_local
    _tess_lang = "eng_sc" if _have_sc else "eng"
    _tess_cfg = f"-l {_tess_lang} --psm 7 -c tessedit_char_whitelist=0123456789.%"
    # Tesseract does best when text height is ~80-120 px. If the
    # current crop is smaller, upscale JUST for Tesseract (don't
    # touch the pipeline-shared ``value_crop`` which ONNX needs).
    # Empirically, x2 hits a Tesseract failure mode with the SC font
    # (clean '499' reads empty at x2 but correctly at x1/x3/x4). So
    # we jump straight to x3+ when upscaling is needed.
    _tW, _tH = value_crop.size
    _t_short = min(_tW, _tH)
    if _t_short < 80:
        _t_scale = max(3, 100 // max(1, _t_short))
        _tess_input = value_crop.resize(
            (_tW * _t_scale, _tH * _t_scale), Image.LANCZOS,
        )
    else:
        _tess_input = value_crop

    tess_text = ""
    # Tesseract is only run for non-digit fields. Digit fields rely on
    # the HUD-RGB CRNN (above) + per-glyph CNN cascade (below).
    if not _digit_only_field:
        if _have_sc:
            with _TESSDATA_LOCK:
                _prev_env_val = _os.environ.get("TESSDATA_PREFIX")
                _os.environ["TESSDATA_PREFIX"] = _tessdata_local
                try:
                    tess_text = pytesseract.image_to_string(
                        _tess_input,
                        config=_tess_cfg,
                    ).strip()
                except Exception as exc:
                    log.debug("api: _ocr_value_crop swallowed: %s", exc)
                finally:
                    if _prev_env_val is None:
                        _os.environ.pop("TESSDATA_PREFIX", None)
                    else:
                        _os.environ["TESSDATA_PREFIX"] = _prev_env_val
        else:
            try:
                tess_text = pytesseract.image_to_string(
                    _tess_input,
                    config=_tess_cfg,
                ).strip()
            except Exception as exc:
                log.debug("api: _ocr_value_crop swallowed: %s", exc)

    # ── ONNX (secondary voter) ──
    # Reuse the primary path's crops (canonical polarity: bright text
    # on dark bg) and invert them, then classify with the SIBLING
    # inverted-polarity ONNX model.  Different polarity AND different
    # weights → maximally decorrelated peer voter; agreement between
    # primary and secondary is a strong high-confidence signal.
    #
    # Graceful degradation: if model_cnn_inv.onnx isn't on disk yet
    # (user hasn't trained it), _classify_crops_inv returns []; the
    # secondary then contributes no opinion to the vote and the
    # primary stands alone — no worse than the old behaviour.
    #
    # Crops come from _segment_glyphs as float32 in [0,1], so polarity
    # inversion is simply 1.0 - crop.
    onnx_text = ""
    onnx_confs: list[float] = []
    if _primary_crops:
        secondary_crops = [
            np.clip(1.0 - c, 0.0, 1.0).astype(np.float32)
            for c in _primary_crops
        ]
        log.debug(
            "sc_ocr.diag: field=%s segment(secondary)=%d glyphs (from primary)",
            field, len(secondary_crops),
        )
        results = _classify_crops_inv(secondary_crops)
        if not results:
            # Inverted model not available (or inference failed).
            # Still dump the inverted crops to the viewer with
            # placeholder labels so the user can SEE what the
            # secondary would classify, just without a real prediction.
            results = [("?", 0.0) for _ in secondary_crops]
            log.debug(
                "sc_ocr.diag: field=%s inverted model unavailable, "
                "secondary contributes no vote",
                field,
            )
        else:
            onnx_text = "".join(ch for ch, _ in results)
            onnx_confs = [c for _, c in results]
        _dump_glyphs(field, "secondary", secondary_crops, results)
        if onnx_text:
            _per_glyph = " ".join(
                f"({ch},{conf:.2f})" for ch, conf in results
            )
            log.debug(
                "sc_ocr.diag: field=%s classify(secondary)=%r per-glyph=%s",
                field, onnx_text, _per_glyph,
            )
    else:
        # Non-digit field, or primary segmentation produced nothing —
        # fall back to the original independent-segmentation path so
        # we still emit *something* for the vote.
        bin_a = _adaptive_binarize(gray)
        crops, _fallback_boxes = _segment_glyphs(gray, bin_a, field=field)
        log.debug(
            "sc_ocr.diag: field=%s segment(secondary,fallback)=%d glyphs",
            field, len(crops) if crops else 0,
        )
        if crops:
            results = _classify_crops(crops)
            # Apply the box-size dot detector here too — same pipeline
            # constraint (28×28 normalization) breaks dots on this path
            # whenever it's reached.
            _dot_labels_fb = _dot_label_from_box(_fallback_boxes)
            for _i, _dl in enumerate(_dot_labels_fb):
                if _dl == ".":
                    _prev = results[_i]
                    results[_i] = (".", 1.0)
                    log.info(
                        "sc_ocr.%s: box-size dot detector overrode CNN "
                        "(secondary,fallback) at idx=%d (was=%r conf=%.2f)",
                        field, _i, _prev[0], _prev[1],
                    )
            onnx_text = "".join(ch for ch, _ in results)
            onnx_confs = [c for _, c in results]
            _dump_glyphs(
                field, "secondary", crops, results,
                overrides=_dot_labels_fb,
            )
            if results:
                _per_glyph = " ".join(
                    f"({ch},{conf:.2f})" for ch, conf in results
                )
                log.info(
                    "sc_ocr.diag: field=%s classify(secondary,fallback)"
                    "=%r per-glyph=%s",
                    field, onnx_text, _per_glyph,
                )

    # ── Consensus → collect for CRNN retraining ──
    # When Tesseract AND ONNX agree on the exact same text, that's
    # ground truth we can trust. Save the original value crop with
    # this label so a future CRNN retrain has domain-matched data.
    if tess_text and onnx_text and tess_text == onnx_text:
        try:
            from ..training_collector import collect_crnn_value_sample
            collect_crnn_value_sample(value_crop, tess_text, source="live_consensus")
        except Exception as exc:
            log.debug("sc_ocr: CRNN sample save failed: %s", exc)

    # ── Vote ──
    # Digit fields: Tesseract is bypassed entirely (tess_text=""), so
    # the ONNX CNN output is what we return. Non-digit fields keep the
    # legacy "prefer Tesseract" behaviour for now (mineral_name and
    # similar text fields aren't yet served by an RGB CRNN/CNN).
    if _digit_only_field:
        if onnx_text:
            return onnx_text, onnx_confs
        return "", []
    if tess_text:
        # Use Tesseract result with dummy confidences (non-digit fields)
        return tess_text, [0.9] * len(tess_text)
    elif onnx_text:
        return onnx_text, onnx_confs
    else:
        return "", []


# ── Public API ─────────────────────────────────────────────────────

def scan_region(region: dict) -> Optional[int]:
    """Read a signal-number region → int in [1000, 35000].

    Same architectural pattern as ``scan_hud_onnx``:

      1. Capture the region.
      2. **Anchor**: locate the location-pin icon via blacklist
         pHash matching. The icon's right edge is the rigid
         coordinate from which the digit cluster starts at a known
         offset — this is the signal scanner's equivalent of the
         HUD's mineral-row template anchor.
      3. **Crop**: snip from (icon_right + 4 px) to right edge,
         then row-isolate to the dominant text band.
      4. **Multi-engine OCR**: run the trained per-region CNN
         (``model_signal_cnn.onnx``) AND Tesseract on the same
         clean crop. Vote when they agree; fall back to None
         (caller falls back to legacy 3-engine vote) when they
         don't, mirroring the HUD's lock-gate consensus.
      5. **Validate**: result must be in [1000, 35000].

    Returns the recognized integer or None on any failure (anchor
    miss, OCR disagreement, out-of-range value).
    """
    img = capture.grab(region)
    if img is None:
        return None
    return _signal_recognize_pil(img, region=region)


# ── Glyph-duplicator CRNN voter panel (ocr/models_glyph/) ───────────────
# The 4 glyph-duplicator CRNNs trained from reconstructed real glyph pixels.
# They are NOT the live production reader — they serve as an independent
# voting panel that, together with the per-glyph CNN consensus, can override
# the production CRNN when it is the lone dissenter (see
# ``_signal_voter_panel_decision``). Weights are each variant's solo accuracy
# on the 106-panel bench (RGB best; gray-INV weakest).
_GLYPH_CRNN_SPECS = (
    ("glyph_rgb",      "model_signal_crnn_rgb.onnx",     0.87),
    ("glyph_rgb_inv",  "model_signal_crnn_rgb_inv.onnx", 0.82),
    ("glyph_gray",     "model_signal_crnn.onnx",         0.77),
    ("glyph_gray_inv", "model_signal_crnn_inv.onnx",     0.65),
)
# name -> (session, alphabet, blank_idx, H, channels, inverse) | None (no model)
_GLYPH_CRNN_CACHE: dict = {}


def _read_signal_glyph_crnns(strip_rgb):
    """Run the 4 glyph-duplicator CRNNs over the signature work strip.

    Preprocessing mirrors each model's own ``.json`` (and the production
    CRNN): Lanczos resize to the model's H, per-channel polarity-canon,
    255-invert for the ``inverse`` variants, max-channel collapse for the
    1-channel (gray) variants. Sessions + metadata are cached per process.

    Returns ``[(name, digit_string, mean_conf, weight), ...]`` for the
    models that loaded and produced a 4-5 digit read. Returns ``[]`` on any
    failure (missing dir, onnxruntime absent, bad strip) — the caller then
    simply keeps the production read.
    """
    out: list[tuple[str, str, float, float]] = []
    if strip_rgb is None or getattr(strip_rgb, "size", 0) == 0:
        return out
    if strip_rgb.ndim != 3 or strip_rgb.shape[2] != 3:
        return out
    try:
        import onnxruntime as _ort
        from pathlib import Path as _Path
        import json as _json
    except Exception:
        return out
    _dir = _Path(__file__).resolve().parent.parent / "models_glyph"
    if not _dir.is_dir():
        return out
    for name, fname, weight in _GLYPH_CRNN_SPECS:
        spec = _GLYPH_CRNN_CACHE.get(name, "unset")
        if spec == "unset":
            spec = None
            _onnx = _dir / fname
            _meta = _dir / fname.replace(".onnx", ".json")
            if _onnx.is_file() and _meta.is_file():
                try:
                    m = _json.loads(_meta.read_text(encoding="utf-8"))
                    _o = _ort.SessionOptions()
                    _o.intra_op_num_threads = 1
                    _o.inter_op_num_threads = 1
                    _sess = _ort.InferenceSession(
                        str(_onnx), sess_options=_o,
                        providers=["CPUExecutionProvider"],
                    )
                    spec = (
                        _sess,
                        str(m.get("alphabet", "0123456789,")),
                        int(m.get("blank_idx", 11)),
                        int(m.get("input_height", 48)),
                        int(m.get("input_channels", 3)),
                        bool(m.get("inverse", False)),
                    )
                except Exception as _gl_load_exc:
                    log.debug(
                        "sc_ocr.signal: glyph CRNN %s load failed: %s",
                        name, _gl_load_exc,
                    )
                    spec = None
            _GLYPH_CRNN_CACHE[name] = spec
        if not spec:
            continue
        sess, alphabet, blank, h_t, chan, inverse = spec
        try:
            h0 = int(strip_rgb.shape[0])
            if h0 != h_t:
                _sc = h_t / max(1, h0)
                _nw = max(8, int(round(strip_rgb.shape[1] * _sc)))
                rs = np.asarray(
                    Image.fromarray(strip_rgb, "RGB").resize(
                        (_nw, h_t), Image.LANCZOS),
                    dtype=np.uint8,
                )
            else:
                rs = strip_rgb
            canon = np.empty_like(rs)
            for c in range(3):
                canon[..., c] = _canonicalize_polarity(rs[..., c])
            base = (255 - canon) if inverse else canon
            if chan == 1:
                x = (base.max(axis=2).astype(np.float32) / 255.0)[None][None]
            else:
                x = (base.astype(np.float32).transpose(2, 0, 1) / 255.0)[None]
            logits = sess.run(None, {sess.get_inputs()[0].name: x})[0]
            lt = logits[:, 0, :] if logits.ndim == 3 else logits
            sh = lt - lt.max(axis=-1, keepdims=True)
            pr = np.exp(sh)
            pr /= pr.sum(axis=-1, keepdims=True)
            preds = pr.argmax(axis=-1)
            confs = pr.max(axis=-1)
            chars: list[str] = []
            cfs: list[float] = []
            prev = -1
            for t in range(int(preds.shape[0])):
                p = int(preds[t])
                if p != prev and p != blank and p < len(alphabet):
                    chars.append(alphabet[p])
                    cfs.append(float(confs[t]))
                prev = p
            digits = "".join(c for c in chars if c.isdigit())
            if 4 <= len(digits) <= 5:
                out.append(
                    (name, digits, float(sum(cfs) / max(1, len(cfs))), weight))
        except Exception as _gl_run_exc:
            log.debug(
                "sc_ocr.signal: glyph CRNN %s read failed: %s",
                name, _gl_run_exc,
            )
            continue
    return out


def _signal_voter_panel_decision(
    prod_val, strip_rgb, pri, sec, rgb, rgb_inv, lexicon,
):
    """Unified CNN+CRNN voter panel adjudicating a production CRNN read.

    Returns an override integer value ONLY when the production CRNN is the
    lone dissenter: no panel member (per-glyph CNN consensus + the 4 glyph
    CRNNs) backs production's value, AND ≥2 members agree on one different
    in-range value. Otherwise returns ``None`` (keep production unchanged).

    Cost-bounded: the per-glyph CNN consensus is already computed upstream,
    so when it AGREES with production (the common case) this returns early
    WITHOUT running the glyph CRNNs. The expensive 4-CRNN panel only runs on
    a genuine CNN-vs-production disagreement.
    """
    if prod_val is None:
        return None
    # 1) Per-glyph CNN consensus (cheap — reuses already-computed results).
    cnn_digits = ""
    try:
        _cnn = _vote_on_digit_string(
            primary_results=pri or None, secondary_results=sec or None,
            rgb_results=rgb or None, rgb_inv_results=rgb_inv or None,
            lexicon=lexicon or None,
        )
        cnn_digits = (_cnn or {}).get("string", "") or ""
    except Exception:
        cnn_digits = ""
    # Fast path: the CNN family agrees with production — no dispute, no
    # override, and we avoid loading/running the glyph CRNNs entirely.
    if cnn_digits and cnn_digits == str(prod_val):
        return None
    # 2) Dispute → consult the panel. CNN-only mode (SC_SIGNAL_VOTER_CNN_ONLY=1)
    #    skips the glyph CRNNs so the safe per-glyph-CNN override can be
    #    measured on its own (no glyph 1↔2 confusion in the vote).
    _cnn_only = os.environ.get("SC_SIGNAL_VOTER_CNN_ONLY") == "1"
    glyphs = [] if _cnn_only else _read_signal_glyph_crnns(strip_rgb)

    # LEADING-DIGIT GUARD (SC_SIGNAL_VOTER_LEADGUARD=1, default on): never let
    # the override overturn production's LEADING digit. The readers' one
    # systematic failure is the leading 1↔2 confusion (it both creates AND
    # mis-"fixes" 21565↔11565); refusing leading-digit changes blocks that
    # regression while still letting the panel correct TRAILING digits (e.g.
    # 17020→17200), where the readers are reliable.
    _leadguard = os.environ.get("SC_SIGNAL_VOTER_LEADGUARD", "1") == "1"

    def _ok(winner):
        if winner is None or winner == prod_val:
            return None
        if not (1000 <= winner <= 99995):
            return None
        if _leadguard and str(winner)[:1] != str(prod_val)[:1]:
            return None
        return winner

    # BLOC mode (SC_SIGNAL_VOTER_BLOC=1): the 4 glyph CRNNs are NOT
    # independent (same method/data/1↔2 bias), so collapse them to ONE bloc
    # vote and pit it against the CNN family's ONE bloc vote. Override only
    # when the two INDEPENDENT families AGREE on the same value ≠ production —
    # a lone CRNN-bloc vote against the CNNs does NOT override (production
    # stands). Cross-family corroboration beats 4 correlated glyph votes.
    if os.environ.get("SC_SIGNAL_VOTER_BLOC") == "1":
        _gvals = [int(d) for _n, d, _c, _w in glyphs
                  if d.isdigit() and 4 <= len(d) <= 5]
        _gbloc = max(set(_gvals), key=_gvals.count) if _gvals else None
        _cbloc = int(cnn_digits) if (cnn_digits and 4 <= len(cnn_digits) <= 5) else None
        return _ok(_gbloc) if (_gbloc is not None and _gbloc == _cbloc) else None

    # FLAT panel: CNN consensus + each glyph CRNN as weighted challengers.
    challengers: list[tuple[int, float]] = []
    if cnn_digits and 4 <= len(cnn_digits) <= 5:
        try:
            challengers.append((int(cnn_digits), 0.90))
        except ValueError:
            pass
    for _name, _digits, _conf, _w in glyphs:
        try:
            challengers.append((int(_digits), _w))
        except ValueError:
            pass
    if not challengers:
        return None
    # Production must be a LONE dissenter — no panel member backs its value.
    if any(v == prod_val for v, _ in challengers):
        return None
    # Reliability-weighted winner + how many members back it.
    from collections import defaultdict as _dd
    _acc: dict = _dd(float)
    _cnt: dict = _dd(int)
    for v, w in challengers:
        _acc[v] += w
        _cnt[v] += 1
    winner = max(_acc, key=_acc.get)
    # CNN-only has a single (multi-CNN) challenger, so require just 1 backer;
    # the full panel requires ≥2 members to agree before overruling production.
    _min_agree = 1 if _cnn_only else 2
    if _cnt[winner] < _min_agree:
        return None
    return _ok(winner)


def _signal_recognize_pil(img, region: Optional[dict] = None) -> Optional[int]:
    """Same pipeline as ``scan_region`` but takes an in-memory PIL
    image — used by ``screen_reader.scan_region`` to avoid a second
    capture pass after it's already grabbed the frame.

    Pipeline (mirrors HUD's ``scan_hud_onnx`` architecture):

      0. **Manual calibration** (when ``region`` is supplied): if the
         user has locked a "signature" row in the calibration dialog
         for this region, use that rectangle directly and skip the
         icon anchor. This is the steady-state fast path after the
         user has dialled in their HUD layout — same role calibration
         plays for HUD label rows.
      1. **Anchor** via NCC against the location-pin icon template
         (signal_anchor.find_digit_crop_box). The icon is a fixed-
         shape UI element that NEVER changes across rocks/sessions
         — same role as the HUD's mineral-row template.
      2. **Crop** to just the digit cluster (right of icon).
      3. **Row-isolate** to the dominant text band (drops UNKNOWN
         caption, distance text, etc. that are below the number).
      4. **Multi-PSM/scale Tesseract** OCR of the crop. The icon is
         already excluded so Tesseract only sees the digits — much
         higher accuracy than the legacy 3-engine vote.
      5. **Range validate** in [1000, 35000].
    """
    # Reset the per-tick latency-skip flag so a slow frame on the
    # previous scan doesn't permanently silence the CNN cross-check.
    global _signal_cnn_skip_for_tick
    _signal_cnn_skip_for_tick = False
    try:
        from PIL import Image as _PILImage
        if not isinstance(img, _PILImage.Image):
            img = _PILImage.fromarray(np.asarray(img))
        # Max-of-channels grayscale, NOT luma. SC's signal panel has
        # heavy chromatic aberration — coloured fringes that drag the
        # luma weighted average toward background (red text → Y=76,
        # blue text → Y=29 out of 255) and destroy contrast for
        # Tesseract. Per-pixel max preserves the brightest channel,
        # so a digit rendered as "red on dark" stays bright in the
        # output regardless of how the CA smears it. Mirrors the
        # same recipe the HUD label-row OCR uses.
        from . import frame_context as _fc
        # rgb is still needed by the downstream COLOR finders (pill /
        # localize_icon / comma) — only the gray percept moved to the
        # shared frame_context cache.
        rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
        gray = _fc.max_channel(img)   # shared per-frame percept (skin)
    except Exception as exc:
        log.debug("sc_ocr.signal: bad input: %s", exc)
        return None
    if gray.ndim != 2 or gray.shape[0] < 8 or gray.shape[1] < 12:
        return None

    try:
        import sys
        from pathlib import Path as _Path
        _scripts = _Path(__file__).resolve().parent.parent.parent / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))
        import extract_labeled_glyphs as _xlg  # type: ignore
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: extract_labeled_glyphs unavailable; "
            "trying local tight-crop fallback: %s",
            exc,
        )
        local_value = _signal_read_tight_local(gray)
        if local_value is not None:
            return _signal_accept_candidate(local_value, "local")
        return None

    # ── Manual calibration (region-keyed) takes precedence ──
    # If the user has locked a "signature" row in the calibration
    # dialog for this region, honor that rectangle directly and
    # skip the icon-anchor / heuristic-mask path entirely. This is
    # the equivalent of how ``_find_label_rows`` short-circuits to
    # the saved coordinates for HUD rows.
    #
    # Resolution order (first hit wins):
    #   1. Manual-override mode + ``signature`` override box → use it
    #      and SKIP every auto-detect path (icon anchor + heuristic
    #      mask). Mirrors the HUD's manual-override bypass.
    #   2. Legacy ``signature`` lock from the calibration dialog
    #      (``rows`` dict) → same skip behaviour.
    #   3. Icon-anchor NCC.
    #   4. Heuristic icon mask + row isolate (deepest fallback).
    crop_box: Optional[tuple[int, int, int, int]] = None
    used_manual_crop = False
    # Tracks whether ANY structural anchor branch in this scan tick
    # produced verifiable icon evidence (icon NCC matched, geometry
    # detector hit, or find_digit_crop_box returned a structurally-
    # validated mode). Drives the scanning timeout below: only icon-
    # confirmed scans update ``_signal_last_icon_seen_ts``.
    #
    # The manual-calibration path doesn't run icon detection — it
    # honours the user's locked rectangle directly. To keep the
    # timeout firing for users with calibration locks who look away
    # from rocks, the manual-cal branch ALSO runs a lightweight
    # ``localize_icon`` presence check (separate from the crop
    # derivation) and sets this flag accordingly.
    _icon_seen_this_tick: bool = False
    if region is not None:
        try:
            from . import calibration as _cal
            _manual: Optional[dict] = None
            _manual_source = ""
            # Step 1: manual-override mode → use override box for signature.
            try:
                if _cal.get_manual_override_mode(region):
                    _override_box = _cal.get_manual_override_box(
                        region, "signature",
                    )
                    if _override_box is not None:
                        _manual = _override_box
                        _manual_source = "manual_override"
                        log.info(
                            "sc_ocr.signal: manual override mode active — "
                            "using override box for signature"
                        )
            except Exception as _mo_exc:
                log.debug(
                    "sc_ocr.signal: manual_override lookup failed: %s",
                    _mo_exc,
                )
            # Step 2: legacy ``signature`` lock (only when override
            # didn't supply a box).
            if _manual is None:
                _manual = _cal.get_row(region, "signature")
                _manual_source = "calibration_row"
        except Exception as _cal_exc:
            log.debug("sc_ocr.signal: calibration lookup failed: %s", _cal_exc)
            _manual = None
            _manual_source = ""
        if _manual is not None:
            try:
                _mx = max(0, int(_manual["x"]))
                _my = max(0, int(_manual["y"]))
                _mw = max(1, int(_manual["w"]))
                _mh = max(1, int(_manual["h"]))
                _mx2 = min(int(gray.shape[1]), _mx + _mw)
                _my2 = min(int(gray.shape[0]), _my + _mh)
                if _mx2 - _mx >= 8 and _my2 - _my >= 6:
                    crop_box = (_mx, _my, _mx2, _my2)
                    used_manual_crop = True
                    log.debug(
                        "sc_ocr.signal: using manual calibration crop "
                        "x=%d y=%d w=%d h=%d source=%s "
                        "(skipping icon anchor)",
                        _mx, _my, _mx2 - _mx, _my2 - _my, _manual_source,
                    )
                    # Even though the manual crop bypasses the icon
                    # anchor for cropping, we still want the scanning
                    # timeout below to fire when the user looks away
                    # from the rock. Run a lightweight icon-presence
                    # probe so ``_icon_seen_this_tick`` is set
                    # truthfully for the manual-cal path too.
                    try:
                        from hud_tracker.anchors.icon_voter import (
                            localize_icon as _li_manual,
                        )
                        if _li_manual(rgb) is not None:
                            _icon_seen_this_tick = True
                    except Exception as _li_manual_exc:
                        log.debug(
                            "sc_ocr.signal: manual-cal icon probe "
                            "skipped: %s", _li_manual_exc,
                        )
            except Exception as _box_exc:
                log.debug(
                    "sc_ocr.signal: manual crop unusable (%s) — "
                    "falling back to anchor", _box_exc,
                )

    # ── PRIMARY anchor: localize_icon (RGB-aware structural detector) ──
    # Mirrors the auto-annotator's detect_icon. Tries the new HUD-tracker
    # primary path first: find_icon_by_geometry + find_icon_rgb_ncc must
    # produce a consensus (IoU > 0.4). If they agree, the icon position
    # is reliable enough that we can derive the digit crop directly,
    # without going through the legacy NCC + voter pipeline.
    #
    # The legacy path (find_digit_crop_box below) remains as the
    # fallback — it handles captures where the structural primaries
    # disagree (e.g. very dim/washed-out backgrounds where geometric
    # checks fail) AND captures of HUD variants the consensus doesn't
    # cover yet.

    # ── PRIMARY: world-model-region2 proportional derivation ──
    # When BOTH the calibration file and the pill bbox are available,
    # we know exactly where the digit cluster lives inside the pill
    # (mean fractional layout from 51 labeled captures). This sidesteps
    # the icon-relative heuristic's assumption that digits span exactly
    # ~6.5× icon width — that estimate is right on average but the
    # icon-relative path stops short of the comma-separated thousands
    # group on values like 12,000, leaving digit ink uncovered.
    if crop_box is None:
        _wmr = _load_region2_world_model_for_api()
        if _wmr is not None:
            _vfrac = (_wmr.get("features") or {}).get("value")
            _pill = _find_pill_for_signal(rgb) if _vfrac else None
            if _vfrac and _pill is not None:
                _px, _py, _pw, _ph = _pill
                _vx = int(round(_px + float(_vfrac["x_frac"]["mean"]) * _pw))
                _vy = int(round(_py + float(_vfrac["y_frac"]["mean"]) * _ph))
                _vw = int(round(float(_vfrac["w_frac"]["mean"]) * _pw))
                _vh = int(round(float(_vfrac["h_frac"]["mean"]) * _ph))

                # Refine LHS using the icon position. icon_voter gives
                # us the icon bbox in image-frame coordinates; the
                # right edge of the icon is a per-frame-precise anchor
                # for the digit cluster's left edge — far more reliable
                # than the world-model's ``x_frac.mean`` which is
                # averaged over captures of different digit counts.
                # Pill width varies with digit count (4-digit values
                # have a narrower pill than 5-digit values), so a fixed
                # ``x_frac`` mean over-shoots the digit start on small
                # values and clips the leading digit on large ones.
                #
                # Field-tested case: a "11,520" capture where the
                # world-model placed ``vx`` at 63 (10 px past the icon)
                # while the actual leading "1" started at col ~51.
                # The leading digit was clipped entirely and the CNN
                # read "5" as if it were the first character. After
                # this anchor change, ``vx`` lands at icon_right+small
                # gap (~51) and the leading "1" is preserved.
                #
                # We expand ``vw`` to cover the gap we just retreated
                # past, so RHS doesn't shift inward.
                try:
                    from hud_tracker.anchors.icon_voter import (
                        localize_icon as _li,
                    )
                    _icon_loc = _li(rgb)
                    if _icon_loc is not None:
                        _ix, _iy, _iw, _ih = _icon_loc["bbox"]
                        _icon_anchor = (
                            _ix + _iw + max(2, int(_pw * 0.03))
                        )
                        _delta = _vx - _icon_anchor
                        _vx = _icon_anchor
                        # Preserve original RHS extent.
                        _vw = _vw + _delta
                        log.debug(
                            "sc_ocr.signal: icon-anchored LHS vx=%d "
                            "(world-model wanted %d, delta=%+d, "
                            "icon=(%d,%d,%dx%d))",
                            _vx, _vx - _delta, -_delta,
                            _ix, _iy, _iw, _ih,
                        )
                        # World-model + pill produced a crop AND
                        # ``localize_icon`` confirmed icon presence —
                        # the digit cluster is structurally validated
                        # for this tick. Refresh the scanning-timeout
                        # timestamp.
                        _icon_seen_this_tick = True
                except Exception as _ic_exc:
                    log.debug(
                        "sc_ocr.signal: icon refinement skipped: %s",
                        _ic_exc,
                    )

                # Clamp RHS to pill - margin and to image bounds.
                _rhs_ceiling = _px + _pw - max(2, int(_pw * 0.05))
                _digits_x2 = min(_vx + _vw, _rhs_ceiling, gray.shape[1])
                _digits_x1 = max(0, _vx)
                _digits_y1 = max(0, _vy)
                _digits_y2 = min(_vy + _vh, gray.shape[0])
                if (_digits_x2 - _digits_x1 >= 20 and
                        _digits_y2 - _digits_y1 >= 8):
                    crop_box = (_digits_x1, _digits_y1,
                                _digits_x2, _digits_y2)
                    log.info(
                        "sc_ocr.signal: world_model_region2 picked "
                        "pill=(%d,%d,%dx%d); derived digit crop_box=%s",
                        _px, _py, _pw, _ph, crop_box,
                    )
                    # Finding the PILL itself is structural proof the
                    # signature panel is on screen — refresh the
                    # scanning-timeout from it. _find_pill_for_signal
                    # (hud_color_finder, strict cyan/green + aspect +
                    # area filters) only fires on the distinctive
                    # saturated pill shape and returns None when the
                    # player looks away, so the timeout still trips
                    # correctly when the panel is truly gone.
                    #
                    # Previously this path required the SEPARATE, much
                    # flakier localize_icon voter (icon NCC + geometry
                    # consensus) to ALSO confirm before crediting the
                    # tick. In the field that voter rejects every
                    # candidate on perfectly-present panels (user
                    # 2026-06-02: pill locked rock-steady at ~(45,32,
                    # 117x32) every frame and a value was read each
                    # tick, yet "voter accepted 0 of N candidates" so
                    # _icon_seen_this_tick never set → SCANNING TIMEOUT
                    # climbed forever → panel stuck "scanning", no
                    # result emitted). The pill is the reliable anchor;
                    # the icon voter is not, so the pill alone refreshes
                    # the timeout now. (The icon refinement above still
                    # runs when it CAN, purely to sharpen the LHS crop.)
                    _icon_seen_this_tick = True

    # ── PILL-POSE FALLBACK (JSON-free, region2 skeleton) ──
    # If the world-model didn't produce a crop (its JSON is absent, or its
    # pill find missed) try the pill-anchored signal_solve geometry BEFORE
    # dropping to the flakier icon path below. signal_solve's fixed pill
    # fractions are calibrated (90% IoU on the annotated region2 set) and
    # need no JSON — a strictly better fallback than icon-only when the
    # pill is present. Dormant whenever the world-model fired (crop_box
    # set ⇒ skipped), so normal operation and the gates stay byte-
    # identical; this only changes the DEGRADED (no-JSON) path.
    if crop_box is None:
        try:
            from . import signal_solve as _ssolve
            _pill_fb = _find_pill_for_signal(rgb)
            _pose_fb = _ssolve.solve(_pill_fb) if _pill_fb else None
            if _pose_fb is not None:
                _vx, _vy, _vw, _vh = _pose_fb["value_box"]
                _fx1 = max(0, _vx)
                _fy1 = max(0, _vy)
                _fx2 = min(_vx + _vw, gray.shape[1])
                _fy2 = min(_vy + _vh, gray.shape[0])
                if _fx2 - _fx1 >= 20 and _fy2 - _fy1 >= 8:
                    crop_box = (_fx1, _fy1, _fx2, _fy2)
                    _icon_seen_this_tick = True
                    log.info(
                        "sc_ocr.signal: signal_solve pill fallback "
                        "(no world-model crop) crop_box=%s", crop_box,
                    )
        except Exception as _fb_exc:
            log.debug("sc_ocr.signal: pill fallback skipped: %s", _fb_exc)

    # ── SECONDARY: localize_icon (RGB-aware structural detector) ──
    # Used when the world-model path didn't fire (pill detection
    # missed, or calibration file absent). Same heuristic the auto-
    # annotator's older detect_value used — derive digit area from
    # icon position alone.
    if crop_box is None:
        try:
            from hud_tracker.anchors.icon_voter import localize_icon
            _loc = localize_icon(rgb)
            if _loc is not None:
                _ix, _iy, _iw, _ih = _loc["bbox"]
                # Derive digit-cluster bbox from icon position. Icon sits
                # at the left of the pill; digits start ~icon_w + 4 px
                # to the right and span ~6× icon width for a 4-5 digit
                # signature. Vertical extent matches the icon row.
                _gap = max(2, int(_iw * 0.15))
                _digits_x1 = _ix + _iw + _gap
                _digits_x2 = _digits_x1 + max(80, int(_iw * 6.5))
                # Clamp to image bounds.
                _digits_x2 = min(_digits_x2, gray.shape[1])
                _digits_y1 = max(0, _iy - 2)
                _digits_y2 = min(gray.shape[0], _iy + _ih + 2)
                if (_digits_x2 - _digits_x1 >= 20 and
                        _digits_y2 - _digits_y1 >= 8):
                    crop_box = (_digits_x1, _digits_y1,
                                _digits_x2, _digits_y2)
                    log.info(
                        "sc_ocr.signal: localize_icon consensus picked "
                        "icon=(%d,%d,%dx%d) score=%.2f det=%s; derived "
                        "digit crop_box=%s",
                        _ix, _iy, _iw, _ih,
                        float(_loc.get("score", 0.0)),
                        _loc.get("details", {}).get("detector", "?"),
                        crop_box,
                    )
                    # localize_icon consensus matched — icon is
                    # structurally present this tick.
                    _icon_seen_this_tick = True
        except Exception as exc:
            log.debug("sc_ocr.signal: localize_icon failed: %s", exc)

    # ── FALLBACK: legacy NCC + voter via find_digit_crop_box ──
    # Used when localize_icon couldn't get consensus. Same approach the
    # HUD uses for label rows, with the per-candidate voter as the
    # discriminator. Pass rgb so the voter has access to all 4 tiers.
    if crop_box is None:
        try:
            from . import signal_anchor as _sa
            crop_box = _sa.find_digit_crop_box(gray, rgb_image=rgb)
            # Only treat structurally-validated crop modes as icon
            # evidence for the scanning timeout. ``icon_only`` is the
            # degenerate path (icon NCC matched but no digit cluster
            # was found) which the downstream gates already refuse —
            # accepting it here would defeat the timeout's purpose.
            # ``none`` means both anchors missed.
            if crop_box is not None:
                _crop_mode_now = _sa.last_crop_mode()
                if _crop_mode_now in ("combo", "digit_only"):
                    _icon_seen_this_tick = True
        except Exception as exc:
            log.debug("sc_ocr.signal: anchor failed: %s", exc)
            crop_box = None

    # ── Scanning-timeout gate ────────────────────────────────────────
    # Refresh the icon-seen timestamp when ANY structural anchor in
    # this tick confirmed icon evidence. Then, if no scan has seen
    # the icon within the timeout window, drop signature consensus
    # back to the cold-start state and short-circuit the rest of the
    # pipeline so the upstream UI receives None (its "scanning"
    # sentinel) instead of a stale persisted value.
    #
    # Ordering: refresh BEFORE checking, so a successful icon-
    # detection THIS tick always survives the gate (gap collapses
    # to ~0). On the first scan after a long pause, the gate fires
    # whenever the icon ISN'T seen this tick — exactly the requested
    # behaviour.
    #
    # Region gating: only enforce the timeout when called from the
    # production path (``region is not None``). Benchmark / debug
    # call sites (``compare_crnn_rgb.py`` etc.) invoke
    # ``_signal_recognize_pil(img)`` with ``region=None`` and process
    # captures back-to-back over many seconds — a 3 s wall-clock
    # timeout from a previous capture's miss must not bleed into the
    # next capture's verdict in that context. The benchmark's job
    # is to test the OCR pipeline's reads on each frame
    # independently; the scanning timeout is purely a UI-freshness
    # behaviour and has no place in that comparison.
    if _icon_seen_this_tick:
        _signal_mark_icon_seen()
    if region is not None and _signal_scanning_timeout_exceeded():
        _signal_age_now = (
            time.monotonic() - _signal_last_icon_seen_ts
        )
        log.info(
            "sc_ocr.signal: SCANNING TIMEOUT (%.1fs since last icon "
            "detection > %.1fs threshold) — returning None so UI "
            "shows 'scanning' state",
            _signal_age_now, _SIGNAL_SCANNING_TIMEOUT_SEC,
        )
        # Reset signature consensus so the next valid detection
        # starts fresh. Only signature state — HUD locks live in
        # ``_STABLE_VALUE`` / ``_field_lock_cache`` and are managed
        # by their own per-region lifecycle.
        _reset_signal_consensus()
        return None

    # ── CRNN crop_box snapshot (pre-extension) ──────────────────────
    # The whole-strip RGB CRNN was trained on bytes derived from the
    # world-model + icon-anchor crop_box WITHOUT the comma-anchored
    # extension below and WITHOUT the per-glyph CNN's 2x Lanczos
    # upscale (see hud_tracker/anchors/compare_crnn_rgb.py
    # ``_normalize_to_work_rgb`` — that's the function the training
    # data was generated from).
    #
    # Production currently feeds the CRNN bytes that have been
    # through both transforms. The comma extension in particular can
    # push x1 left by 60-70 px on captures where the comma detector
    # picks up a noisy left-edge artifact, dragging icon/pill pixels
    # into the CRNN input and causing it to read e.g. "8276" as
    # "2525" / "21,520" / "21,350". The 2x Lanczos compounded with
    # the CRNN's own H=48 Lanczos resize adds a smaller pixel-level
    # drift (~6 luma units max on per-channel bytes).
    #
    # Fix: snapshot the crop_box here, BEFORE the comma extension,
    # and stash it so the CRNN call site below can derive its own
    # ``_work_rgb_for_crnn`` that matches what training saw. The
    # per-glyph CNN path keeps using ``crop_box`` post-extension and
    # the 2x-upscaled ``_work_rgb`` — those are calibrated to its
    # training distribution and must not change.
    _crnn_crop_box = crop_box

    # ── Comma-anchored crop extension ───────────────────────────────
    # When the anchor-derived crop happens to start AFTER the leading
    # digit's left edge — typical on values like 11,520 / 3,400 where
    # the world-model layout averages over crops with different digit
    # counts and on individual frames lands a few pixels to the right
    # of where the leading digit actually starts — the leading digit
    # gets clipped and the segmenter sees one fewer digit than is
    # actually present.
    #
    # The structural fix: run the comma detector on the current
    # crop, then use the comma's x_center plus the right-of-comma
    # digit pitch (3 digits, well-bounded by the pill's right edge)
    # to estimate where the leading digit's left edge SHOULD be.
    # If that estimate is outside the current crop, extend the crop
    # leftward into the original RGB image's data to recover the
    # missing pixels.
    #
    # Skipped for manual crops — when the user has explicitly locked
    # a calibration row, we honor their box exactly and never
    # auto-extend.
    if crop_box is not None and not used_manual_crop:
        try:
            from hud_tracker.anchors.comma_finder import find_comma_voted
            _x1, _y1, _x2, _y2 = crop_box
            _trial = rgb[_y1:_y2, _x1:_x2]
            _LAST_SIGNAL_COMMA_X[:] = []   # fresh per scan (nervous system)
            _comma = find_comma_voted(_trial)
            if _comma is not None:
                _comma_x_in_crop = int(_comma["x_center"])
                _LAST_SIGNAL_COMMA_X[:] = [float(_x1 + _comma_x_in_crop)]
                _trial_w = int(_trial.shape[1])
                _right_region_w = _trial_w - _comma_x_in_crop
                # 3 digits to the right of the comma + small margin.
                # The +0.5 represents the digit-to-edge gutter the
                # SC HUD font reserves at the pill's right edge.
                if _right_region_w >= 12:
                    _estimated_pitch = _right_region_w / 3.5
                    # 5-digit hypothesis: 2 digits to the LEFT of comma
                    _leading_x_5 = _comma_x_in_crop - 2 * _estimated_pitch
                    # 4-digit hypothesis: 1 digit to the LEFT of comma
                    _leading_x_4 = _comma_x_in_crop - 1 * _estimated_pitch
                    # Use the lower (leftmost) leading-x — extending too
                    # far left is harmless (extra background pixels) but
                    # extending too little clips the leading digit.
                    _min_leading_x = min(_leading_x_4, _leading_x_5)
                    # If the leading position is at the very edge or
                    # beyond, extend leftward.
                    if _min_leading_x < 4:
                        _extension_needed = max(
                            0, int(round(4 - _min_leading_x))
                        )
                        _new_x1 = max(0, _x1 - _extension_needed)
                        if _new_x1 < _x1:
                            crop_box = (_new_x1, _y1, _x2, _y2)
                            log.info(
                                "sc_ocr.signal: comma-anchored crop "
                                "extension: x1 %d -> %d (extended %d "
                                "px left); comma x_in_crop=%d "
                                "trial_w=%d est_pitch=%.1f "
                                "leading_x_4=%.1f leading_x_5=%.1f",
                                _x1, _new_x1, _x1 - _new_x1,
                                _comma_x_in_crop, _trial_w,
                                _estimated_pitch,
                                _leading_x_4, _leading_x_5,
                            )
        except Exception as _ce_exc:
            log.debug(
                "sc_ocr.signal: comma-anchored crop extension "
                "skipped: %s", _ce_exc,
            )

    if crop_box is not None:
        # Use the anchor-derived (or manual-calibration) crop.
        x1, y1, x2, y2 = crop_box
        work = gray[y1:y2, x1:x2]
        # Parallel RGB work crop for the shadow RGB CNN (no voting).
        # Sliced from the SAME crop_box in the original RGB panel so
        # the per-glyph box coordinates returned by the segmenter on
        # ``work`` map 1-to-1 onto ``_work_rgb``.
        _work_rgb = rgb[y1:y2, x1:x2]
        log.info(
            "sc_ocr.signal: crop_box=%s manual=%s gray_shape=%s "
            "work_shape_pre_isolate=%s",
            crop_box, used_manual_crop, gray.shape, work.shape,
        )
    else:
        # Anchor missed (no icon in image, or NCC below threshold).
        # Fall back to the heuristic icon mask + row isolate, which
        # is less reliable but still better than nothing.
        bg = int(np.median(gray))
        work = gray.copy()
        # Parallel RGB work crop. Apply the same icon mask via per-
        # channel median (avoid introducing a high-saturation patch).
        _work_rgb = rgb.copy()
        icon_right = _xlg._locate_icon_via_blacklist_match(work)
        floor_mask = int(work.shape[1] * 0.30)
        mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
        if 0 < mask_w < work.shape[1]:
            work[:, :mask_w] = bg
            for _ch in range(3):
                _ch_med = int(np.median(_work_rgb[:, :, _ch]))
                _work_rgb[:, :mask_w, _ch] = _ch_med
        log.info(
            "sc_ocr.signal: ANCHOR MISS — fallback heuristic mask "
            "icon_right=%d mask_w=%d work_shape=%s",
            icon_right, mask_w, work.shape,
        )

    # ── Row isolation: keep only the dominant text band ──
    # Skip when the user has supplied a manual crop — they already
    # told us exactly where the digits are; the auto row-isolator
    # would only re-trim to a sub-band of that.
    if not used_manual_crop:
        _shape_before = work.shape
        # Use the bounds-returning variant so we can apply the SAME
        # trim to ``_work_rgb`` (keeps the parallel array spatially
        # aligned with ``work``).
        _band = (
            _xlg._find_main_row_bounds(work)
            if hasattr(_xlg, "_find_main_row_bounds") else None
        )
        if _band is not None:
            _by1, _by2 = _band
            work = work[_by1:_by2, :]
            _work_rgb = _work_rgb[_by1:_by2, :]
        else:
            # Fallback: legacy path that doesn't return bounds. RGB
            # parallel will be slightly mis-aligned in this branch but
            # still useful for the shadow viewer row.
            work = _xlg._isolate_main_row(work)
        if work.shape != _shape_before:
            log.info(
                "sc_ocr.signal: _isolate_main_row trim %s -> %s",
                _shape_before, work.shape,
            )
    if work.shape[0] < 6 or work.shape[1] < 12:
        return None

    # ── Two enhancements that match runtime to training: ──
    #
    # (1) Min-max contrast stretch
    #     Training samples have full 0–255 dynamic range (verified:
    #     min=32, max=255, std ≈ 58 on class-6 samples). Runtime
    #     ``max-of-channels → canonicalize`` produces compressed
    #     range (~85–225, std ≈ 30). Linear remap to full range.
    #
    # (2) Lanczos upscale of the work crop to ~32 px tall
    #     This matches the NATIVE PANEL RESOLUTION the training
    #     samples came from — training panels happened to be
    #     captured with digits ~28–32 px tall, which then went
    #     bilinear-resized 30→28 (slight downscale = sharp). Runtime
    #     panels at 1080p come in with digits ~14–18 px tall, which
    #     would otherwise get bilinear-resized 14→28 (2× upscale =
    #     blurry). A Lanczos upscale of the work crop to 32 px tall
    #     BEFORE segmentation puts the per-glyph crops at the same
    #     native resolution training had, so the final 28×28 crop is
    #     the result of a SLIGHT DOWNSCALE (sharp) instead of an
    #     aggressive upscale (blurry).
    #
    #     Just Lanczos — no unsharp mask, no gamma, no bright-push.
    #     Earlier attempts that stacked those on top of Lanczos
    #     created halo artifacts that hollowed out the digit ink.
    #     Lanczos alone preserves stroke geometry and gives crisp
    #     edges through its sinc-kernel weighting, which is exactly
    #     what training crops have natively.
    try:
        # (1) Contrast stretch
        _w_arr = work.astype(np.float32)
        _mn, _mx = float(_w_arr.min()), float(_w_arr.max())
        if _mx - _mn > 8:
            _w_arr = (_w_arr - _mn) * (255.0 / (_mx - _mn))
            work = np.clip(_w_arr, 0, 255).astype(np.uint8)

        # (2) Lanczos upscale to ~32 px tall (training-native size)
        _h_pre = work.shape[0]
        if _h_pre < 28:
            _scale_up = max(2, 32 // max(1, _h_pre))
            from PIL import Image as _PILImage_up
            _pil = _PILImage_up.fromarray(work, mode="L").resize(
                (work.shape[1] * _scale_up, _h_pre * _scale_up),
                _PILImage_up.LANCZOS,
            )
            work = np.asarray(_pil, dtype=np.uint8)
            # Apply the SAME Lanczos upscale to the parallel RGB array
            # so per-glyph box coordinates produced by the segmenter
            # on ``work`` map 1-to-1 onto ``_work_rgb``.
            try:
                _pil_rgb = _PILImage_up.fromarray(_work_rgb, mode="RGB").resize(
                    (_work_rgb.shape[1] * _scale_up, _work_rgb.shape[0] * _scale_up),
                    _PILImage_up.LANCZOS,
                )
                _work_rgb = np.asarray(_pil_rgb, dtype=np.uint8)
            except Exception as _rgb_up_exc:
                log.debug(
                    "sc_ocr.signal: parallel RGB Lanczos failed (%s) — "
                    "shadow RGB CNN will be skipped", _rgb_up_exc,
                )
            log.info(
                "sc_ocr.signal: stretched + Lanczos %dx to ~32px "
                "(h=%d→%d) so crops match training-sample crispness",
                _scale_up, _h_pre, work.shape[0],
            )
        else:
            log.info(
                "sc_ocr.signal: stretched (h=%d already large enough, "
                "no upscale needed)", _h_pre,
            )
    except Exception as _enh_exc:
        log.debug(
            "sc_ocr.signal: enhancement failed (%s) — proceeding "
            "with native crop", _enh_exc,
        )

    # ── Broadcast the digit-cluster crop to in-process listeners ──
    # The Calibration Dialog's "signature" row subscribes via
    # live_broadcast. Mirrors the HUD ``deliver_crop("mass", _vc)``
    # call sites — same shape, different field name.
    try:
        from PIL import Image as _PILImage_bcast
        from . import live_broadcast as _bcast
        _crop_pil = _PILImage_bcast.fromarray(work, mode="L")
        # Stash the crop box (in signal-region coords) so the
        # Calibration Dialog can show absolute coordinates next to
        # the live preview and seed Lock with a real box (not a
        # placeholder). Only valid when crop_box is known — the
        # heuristic-mask fallback path doesn't produce one.
        if crop_box is not None:
            _bx1, _by1, _bx2, _by2 = crop_box
            _LAST_SIGNAL_CROP_BOX[:] = [
                int(_bx1), int(_by1),
                int(_bx2 - _bx1), int(_by2 - _by1),
            ]
        else:
            _LAST_SIGNAL_CROP_BOX[:] = []
        _bcast.deliver_crop("signature", _crop_pil)
    except Exception as _bc_exc:
        log.debug("sc_ocr.signal: live_broadcast crop failed: %s", _bc_exc)

    from PIL import Image as _PILImage
    base = _PILImage.fromarray(work, mode="L")

    # ──────────────────────────────────────────────────────────────
    # HUD-STYLE PRIMARY CNN PATH (signal-specific classifier) ─────
    # Mirrors _ocr_value_crop's primary block architecturally, but
    # the primary classifier is the SIGNATURE-trained CNN
    # (``model_signal_cnn.onnx`` from the ``signal`` region kind),
    # not the HUD CNN. So user-collected signature glyphs in
    # ``training_data_user_sig`` actually drive the primary read —
    # the HUD CNN would just be guessing at digits its training set
    # rendered at a different scale + colour.
    #
    # Pipeline:
    #   1. Polarity-canonicalize the work crop (bright text on dark).
    #   2. Adaptive-binarize (locally-windowed threshold).
    #   3. Segment with the HUD's _segment_glyphs — same leading-1
    #      detector + width filters used for HUD reads.
    #   4. Split merged-digit spans (signature-specific post-process).
    #   5. Classify the SIGNAL CNN (model_signal_cnn.onnx) on the
    #      28×28 crops — this is the user's signature-specific model.
    #   6. Classify the HUD inverted CNN (model_cnn_inv.onnx) on
    #      polarity-flipped copies as the secondary voter — different
    #      weights AND different polarity → maximally decorrelated peer
    #      vote, exactly the same convention ``_signal_cnn_per_digit``
    #      uses for its dual-polarity check.
    #   7. Apply the HUD's two-tier accept gate (STRICT / DUAL-AGREE)
    #      with signature-specific validation (4-5 digit integer in
    #      [1000, 35000]).
    #   8. ALWAYS dump per-glyph tiles for the live viewer regardless
    #      of whether the gate passed — that's what the user sees in
    #      the SIGNATURE (PRIMARY) / SIGNATURE (SECONDARY) rows.
    #
    # On gate-pass: return the integer (with display-stability
    # filter). On gate-fail: drop into the legacy CRNN+Tesseract
    # block below.
    # ──────────────────────────────────────────────────────────────
    _hud_pri_crops: list[np.ndarray] = []
    _hud_pri_results: list[tuple[str, float]] = []
    _hud_sec_crops: list[np.ndarray] = []
    _hud_sec_results: list[tuple[str, float]] = []
    # RGB / RGB_INV shadow voters — promoted to PRIMARY voters in the
    # gate hierarchy below. Pre-initialise so the gates can safely
    # check truthiness even when the segmentation block exits early
    # (no crops, exception, etc.).
    _hud_rgb_crops: list[np.ndarray] = []
    _hud_rgb_results: list[tuple[str, float]] = []
    _hud_rgb_inv_crops: list[np.ndarray] = []
    _hud_rgb_inv_results: list[tuple[str, float]] = []

    # ── EARLY CRNN PASS (count oracle for segmentation) ──────────────
    # Run the whole-strip RGB CRNN once, BEFORE segmentation. The
    # model is reliable at *what* and *in what order*, even when its
    # gate-level confidence is too low to consume the read directly.
    # We use it here as a count oracle: "you should be finding N
    # digits + this many commas." Downstream the segmenter (both
    # binarize-recipe selection and the wide-span splitter) and the
    # merge-narrow-spans post-process use this count to coerce their
    # output to match.
    #
    # The CRNN gate further below also reads from this stash instead
    # of re-running the model — single forward pass per tick.
    _crnn_rgb_text: Optional[str] = None
    _crnn_rgb_mean_conf: float = 0.0
    _crnn_rgb_digits: str = ""
    _crnn_rgb_n_digits: Optional[int] = None
    # Trust threshold for using the CRNN's count as a segmenter hint.
    # Mirrors the lexicon-confirmed gate threshold so a borderline
    # CRNN read doesn't force a bad coercion on a segmenter that
    # would otherwise have done fine.
    _CRNN_COUNT_TRUST_CONF = 0.55

    # ── Build CRNN-specific RGB input ─────────────────────────────────
    # The CRNN was trained on the compare-script's preprocessing:
    # world-model + icon-anchor crop_box → row-isolate → H=48 Lanczos
    # done INSIDE _classify_signal_via_crnn_rgb. That path does NOT
    # include the comma-anchored extension or the 2x Lanczos upscale
    # the per-glyph CNN path uses. Feeding the CRNN the per-glyph
    # path's _work_rgb introduces (a) major divergence on captures
    # where the comma extension fires and pulls icon pixels into the
    # crop, and (b) small pixel drift from the compound Lanczos
    # resize (work_rgb already at 2x → CRNN's H=48 second resize).
    #
    # When the snapshot is available, derive a parallel _work_rgb
    # from it that mirrors compare-script's bytes; otherwise fall
    # back to the per-glyph path's _work_rgb (status quo for the
    # rare cases where world-model + icon-anchor didn't fire).
    _work_rgb_for_crnn = None
    if _crnn_crop_box is not None and not used_manual_crop:
        try:
            _cx1, _cy1, _cx2, _cy2 = _crnn_crop_box
            _cb_rgb = rgb[_cy1:_cy2, _cx1:_cx2]
            _cb_gray = gray[_cy1:_cy2, _cx1:_cx2]
            # Same row-isolate the compare-script does (xlg
            # ._find_main_row_bounds on the gray slice). Recomputed
            # against this crop's gray rather than reusing the
            # per-glyph path's _band, which was derived from the
            # post-extension crop and would land at different row
            # coordinates.
            _cb_band = (
                _xlg._find_main_row_bounds(_cb_gray)
                if hasattr(_xlg, "_find_main_row_bounds") else None
            )
            if _cb_band is not None:
                _cb_by1, _cb_by2 = _cb_band
                _cb_rgb = _cb_rgb[_cb_by1:_cb_by2, :, :]
            if _cb_rgb.shape[0] >= 4 and _cb_rgb.shape[1] >= 8:
                _work_rgb_for_crnn = _cb_rgb
        except Exception as _crnn_rgb_exc:
            log.debug(
                "sc_ocr.signal: building CRNN-specific _work_rgb "
                "failed (%s) — falling back to per-glyph path bytes",
                _crnn_rgb_exc,
            )
            _work_rgb_for_crnn = None
    _crnn_rgb_input = _work_rgb_for_crnn if _work_rgb_for_crnn is not None else _work_rgb

    try:
        # Beam-search CTC (width=8) + lexicon rerank. Beam search
        # recovers from local CTC mistakes the greedy argmax commits
        # to early — especially comma-boundary slips like the 5-digit
        # → 4-digit drops that plagued the prior greedy decoder. The
        # lexicon rerank prefers the highest-scoring candidate whose
        # digit-integer parse is a known signature value, falling back
        # to the best candidate overall when nothing matches. Existing
        # downstream gates (confidence threshold, length, range) keep
        # their semantics — the mean confidence returned is the
        # geometric mean of per-emission softmax peaks, matching the
        # greedy decoder's mean_conf scale.
        _crnn_pre = _classify_signal_via_crnn_rgb(
            _crnn_rgb_input,
            beam_width=8,
            lexicon=_KNOWN_SIGNAL_VALUES if _KNOWN_SIGNAL_VALUES else None,
        )
    except Exception as _crnn_pre_exc:
        log.debug(
            "sc_ocr.signal: early CRNN pass failed (%s) — "
            "segmentation will use the default expected_count=5",
            _crnn_pre_exc,
        )
        _crnn_pre = None
    if _crnn_pre is not None:
        _crnn_rgb_text, _crnn_rgb_mean_conf = _crnn_pre
        _crnn_rgb_digits = "".join(c for c in _crnn_rgb_text if c.isdigit())
        if (
            4 <= len(_crnn_rgb_digits) <= 5
            and _crnn_rgb_mean_conf >= _CRNN_COUNT_TRUST_CONF
        ):
            _crnn_rgb_n_digits = len(_crnn_rgb_digits)
            log.info(
                "sc_ocr.signal: CRNN COUNT ORACLE text=%r digits=%r "
                "mean=%.2f -> expected_count=%d",
                _crnn_rgb_text, _crnn_rgb_digits, _crnn_rgb_mean_conf,
                _crnn_rgb_n_digits,
            )
        else:
            log.info(
                "sc_ocr.signal: CRNN COUNT ORACLE text=%r digits=%r "
                "mean=%.2f -> NOT TRUSTED (need 4-5 digits and "
                "mean>=%.2f) — segmenter uses default expected_count=5",
                _crnn_rgb_text, _crnn_rgb_digits, _crnn_rgb_mean_conf,
                _CRNN_COUNT_TRUST_CONF,
            )
    _expected_digit_count = _crnn_rgb_n_digits if _crnn_rgb_n_digits else 5

    try:
        _work_canon = _canonicalize_polarity(work)
        # Multi-recipe binarization: tries Otsu / percentile / multiple
        # adaptive-window sizes / legacy and picks the recipe whose
        # column-projection span count is closest to the 4-5 digit
        # signal signature. Falls back gracefully to legacy on any
        # recipe failure. See ``_adaptive_binarize_multi`` for the
        # selection rationale.
        _hud_bin = _adaptive_binarize_multi(
            _work_canon, expected_count=_expected_digit_count,
        )
        # ── Pre-segmentation: strip pill-outline bridges + walls ──
        # The world-model crop_box derivation is proportional to the
        # pill bbox and on individual frames can include the pill
        # outline along the bottom (and sometimes the right edge).
        # After polarity-canonicalize, those originally-dark boundary
        # pixels become BRIGHT in the binary mask, manifesting as a
        # full-width horizontal bridge or a full-height vertical wall.
        # Either bridges all digit columns into one mega-span and
        # makes the column-projection segmenter return one giant span
        # covering the whole row. This trims those edge artifacts
        # before projection so the segmenter sees clean per-digit
        # valleys. See ``_strip_pill_outline_bridges`` for thresholds
        # + rationale.
        _hud_bin = _strip_pill_outline_bridges(_hud_bin)
        # ── Pre-segmentation: scrub commas from the binary mask ──
        # Column-projection segmentation otherwise fuses the comma
        # with the trailing edge of the preceding digit when the SC
        # HUD font kerns them into the same x-range. The fused crop
        # has the digit on top + comma below baseline, which the
        # secondary HUD-inverted CNN reads as ``%`` because ``,`` is
        # not in its alphabet. Masking the comma's pixels here means
        # the segmenter only ever sees clean digits.
        _hud_bin = _mask_commas_in_signature_band(_hud_bin)
        # ── PRIMARY: proportional segmenter (constrained-format-aware) ──
        # SC mining signatures are deterministically formatted as either
        # ``D,DDD`` (4-digit) or ``DD,DDD`` (5-digit). The proportional
        # segmenter exploits that structure: it detects the comma's
        # bottom-only-ink signature, then anchors digit slots to either
        # side using the structural prior. Sidesteps column-projection
        # entirely — projection-based ``_segment_glyphs`` regularly
        # finds spurious valleys WITHIN digits (chromatic-aberration
        # streaks) and misses real valleys BETWEEN digits (aberration
        # smears bright pixels across kerning gaps). On user-reported
        # captures like "3,400" → ``'47777'``, the projection segmenter
        # produces fragmented spans the CNN classifies as garbage; the
        # proportional segmenter avoids that failure mode by construction.
        #
        # We try proportional FIRST and adopt its bboxes when its self-
        # reported confidence is good. On any exception or low-conf
        # result, we fall through to the legacy column-projection path
        # below, preserving existing behaviour as the safety net.
        _hud_pri_crops = []
        _hud_pri_boxes = []
        _proportional_used = False
        try:
            from hud_tracker.anchors.signal_proportional_segmenter import (
                segment_signal_proportional as _seg_prop,
            )
            from PIL import Image as _PILImage_prop
            # The proportional segmenter wants the RGB value crop. We
            # have the L (gray) ``work``; rebuild RGB from ``_work_rgb``
            # if it's available, else feed the gray as RGB.
            try:
                _work_rgb_local = _work_rgb  # set earlier in the path
                _prop_pil = _PILImage_prop.fromarray(_work_rgb_local, mode="RGB")
            except (NameError, Exception):
                _prop_pil = _PILImage_prop.fromarray(work, mode="L").convert("RGB")
            # When the CRNN reads with enough confidence to be trusted
            # as a count oracle (4 or 5 digits, mean conf ≥ 0.55), pass
            # the count to the proportional segmenter as
            # ``expected_digits`` — that forces it to evaluate ONLY the
            # matching hypothesis (DD,DDD vs D,DDD) instead of scoring
            # both and picking the higher-confidence one. The segmenter
            # has its own confidence threshold that occasionally picks
            # the wrong hypothesis (e.g. 17,080 → "7,080" at 0.70 conf
            # over "17,080" at 0.65 conf because its scoring penalizes
            # empty slots more than missing digits). CRNN's count
            # signal is more reliable on captures where it's confident,
            # so we use it to break that tie. When CRNN didn't pass
            # the trust threshold we pass ``None`` so the segmenter
            # behaves exactly as before (both hypotheses scored).
            _prop_expected = (
                _crnn_rgb_n_digits
                if _crnn_rgb_n_digits in (4, 5)
                else None
            )
            _prop_result = _seg_prop(
                _prop_pil,
                expected_digits=_prop_expected,
                classifier=_classify_crops_signal,
                classifier_topk=_classify_crops_signal_topk,
                lexicon=_KNOWN_SIGNAL_VALUES if _KNOWN_SIGNAL_VALUES else None,
            )
            if _prop_expected is not None:
                log.info(
                    "sc_ocr.signal: proportional segmenter forced to "
                    "expected_digits=%d (from CRNN count oracle)",
                    _prop_expected,
                )
            # ── DETERMINISM FIX 2026-05-10 ──────────────────────────
            # Bind the canonical gray the segmenter ACTUALLY used to
            # classify, so the per-glyph crops re-extracted below use
            # IDENTICAL pixels to the ones the segmenter scored on.
            #
            # Prior bug: production used ``_work_canon``
            # (Otsu-minority-class polarity) while the segmenter used
            # its own ``gray_canon`` (border-vs-center polarity). The
            # two heuristics disagree on captures where digit ink
            # dominates the center sample, producing OPPOSITE-polarity
            # arrays at the SAME bboxes. The gray CNN then read the
            # icon class ``@`` on every crop in production while the
            # segmenter classified them as digits — the user-reported
            # "jumping" between primary and inverse reads.
            #
            # The segmenter exposes its canonical gray in the result
            # dict. We adopt it here so all downstream re-extractions
            # (gray PRIMARY, gray SECONDARY) see the segmenter's view.
            # The RGB pipeline already uses ``_work_rgb`` (untouched
            # by either canonicalization) so it stays in sync.
            _seg_gray_canon = (
                _prop_result.get("gray_canon_used")
                if _prop_result is not None else None
            )
            # Acceptance bar for adopting the proportional segmenter's
            # bboxes over the column-projection fallback. Default 0.5 on
            # the segmenter's per-glyph CLASSIFICATION confidence.
            #
            # COMMA-GATED LOWERING: when the comma is confidently
            # localized in this crop, the proportional segmenter's SLOT
            # STRUCTURE (evenly pitched from the comma, D,DDD / DD,DDD)
            # is trustworthy even when per-glyph classification confidence
            # is low — which it is on blurry / chromatically-aberrated
            # captures. In that regime the column-projection fallback
            # MERGES adjacent digits (no clean gap in the low-contrast
            # binary, e.g. "10,620" fused into a "10" box), producing
            # structurally WORSE boxes than the comma-pitched slots. So
            # when the comma anchor is solid we trust the structure and
            # adopt the proportional boxes at a much lower bar. The boxes
            # are re-classified downstream by the 4 voters, so this
            # improves their inputs (and the live viewer's tiles).
            # Corpus-validated: no regression on the 170-panel set.
            _prop_accept_thr = 0.5
            try:
                _pat_env = os.environ.get("SC_PROP_THR")
                if _pat_env:
                    _prop_accept_thr = float(_pat_env)
                else:
                    from hud_tracker.anchors.comma_finder import (
                        find_comma_voted as _fcv_gate,
                    )
                    _carr_gate = np.asarray(_prop_pil)
                    if _carr_gate.ndim == 3:
                        _cres_gate = _fcv_gate(
                            np.ascontiguousarray(
                                _carr_gate[..., :3]
                            ).astype(np.uint8)
                        )
                        if (
                            _cres_gate is not None
                            and float(_cres_gate.get("confidence", 0.0))
                            >= 0.9
                        ):
                            _prop_accept_thr = 0.25
            except Exception:
                _prop_accept_thr = 0.5
            if (
                _prop_result is not None
                and float(_prop_result.get("confidence", 0.0))
                >= _prop_accept_thr
            ):
                # Adopt proportional bboxes. Convert to the
                # ``_segment_glyphs`` output shape (28×28 float32 [0,1]
                # crops + parallel ``(x, y, w, h)`` boxes list) so the
                # downstream pipeline doesn't care which segmenter ran.
                _seg_digits = _prop_result.get("digits") or []
                _digit_only = [d for d in _seg_digits if not d.get("is_comma")]
                # The segmenter's classifications carry the post-
                # backtrack characters: when lexicon backtracking
                # rewrote a position from '@' or another non-digit
                # to its top-2 alternative, the entry in
                # ``classification`` reflects the corrected char.
                # We capture those here so the downstream icon-drop
                # logic sees the corrected reads instead of dropping
                # crops the segmenter has already healed.
                _seg_pri_results: list[tuple[str, float]] = []
                for d in _digit_only:
                    cls = str(d.get("classification", "?"))
                    conf = float(d.get("confidence", 0.0))
                    _seg_pri_results.append((cls, conf))
                if _digit_only:
                    _hud_pri_crops = []
                    _hud_pri_boxes = []
                    # OLD-STYLE PAD+RESIZE (restored 2026-05-09): the
                    # ``_tight_repad_glyph`` over-cropping introduced
                    # mid-2026-05 broke clean glyphs (the user-confirmed
                    # ``16,960`` regressed from 1.00-confidence reads to
                    # garbage). The OLD pipeline produces crops where
                    # the digit fills ~50% of the canvas with halo blur
                    # at edges — that distribution is what the trained
                    # signal CNNs actually learned on. So we feed the
                    # proportional segmenter's bbox crops through the
                    # same ``pad=2 + PIL.resize(28,28) BILINEAR`` tail
                    # the column-projection segmenter uses (mirrors
                    # ``_segment_glyphs`` lines 1492-1502).
                    #
                    # Vertical-tight-to-ink (kept): the proportional
                    # segmenter returns bboxes with FULL row height,
                    # which includes substantial dark background above
                    # and below the digit. Without trimming the y
                    # extent, the resized 28×28 ends up dominated by
                    # dark bg → bright digit ink shrinks to ~10% of
                    # the canvas, far enough out of the training
                    # distribution that the CNN classifies them as
                    # the icon ('@') class. ``_segment_glyphs`` does
                    # the same y-tighten trick on its own output
                    # (lines 1484-1491). Mirroring it here keeps the
                    # proportional path's CNN inputs in distribution
                    # without re-introducing the over-cropping that
                    # ``_tight_repad_glyph`` would do.
                    # CANONICAL-GRAY SOURCE (DETERMINISM 2026-05-10):
                    # ``_work_canon`` is retained as the gray-CNN crop
                    # source. The segmenter exposes its own canonical
                    # gray (``gray_canon_used``) so a future caller
                    # could swap to it for byte-identical alignment
                    # with the segmenter's pre-classification, but the
                    # production CNN weights have been calibrated
                    # against ``_work_canon``'s Otsu-minority polarity
                    # (16,960 / 11,520 captures verified). Determinism
                    # within a single ``_signal_recognize_pil`` call
                    # is preserved because:
                    #
                    #  * ``_work_canon`` is computed once and reused.
                    #  * ``_hud_pri_boxes`` is computed once and reused.
                    #  * Both polarity-canonicalization functions are
                    #    pure deterministic functions of their inputs.
                    #
                    # So all four CNN feeds (gray PRIMARY, gray
                    # SECONDARY, signal_rgb, signal_rgb_inv) receive
                    # crops sliced from the SAME source arrays at the
                    # SAME bboxes — no run-to-run drift, no cross-CNN
                    # bbox disagreement. Verified by
                    # ``debug_segmenter/determinism_check.py``: 5 runs
                    # of any capture produce 1 unique bbox set + 1
                    # unique per-CNN classification set across runs.
                    _crop_source_gray = _work_canon
                    for d in _digit_only:
                        bx, by, bw, bh = d["bbox"]
                        # Defensive bounds against malformed data.
                        if bw < 1 or bh < 1:
                            continue
                        if bx + bw > _crop_source_gray.shape[1]:
                            continue
                        if by + bh > _crop_source_gray.shape[0]:
                            continue
                        # OLD-STYLE PAD+RESIZE (restored 2026-05-09):
                        # Pad the canonical sub with ``pad=2`` of
                        # white, resize to 28×28 via BILINEAR, scale
                        # to [0, 1]. Mirrors the WingmanAI reference
                        # path where this exact preprocessing
                        # produces the user-confirmed 1.00-confidence
                        # reads on clean glyphs. Tight-repad
                        # (``_tight_repad_glyph``) is disabled in this
                        # branch — re-introducing it would put the
                        # crops into a distribution the WingmanAI-
                        # source models weren't trained on.
                        _g_crop = _crop_source_gray[
                            by:by + bh, bx:bx + bw,
                        ].astype(np.float32)
                        _pad = 2
                        _padded = np.full(
                            (_g_crop.shape[0] + _pad * 2,
                             _g_crop.shape[1] + _pad * 2),
                            255.0, dtype=np.float32,
                        )
                        _padded[
                            _pad:_pad + _g_crop.shape[0],
                            _pad:_pad + _g_crop.shape[1],
                        ] = _g_crop
                        if not os.environ.get("SC_NO_ASPECT_REPAD"):
                            # Aspect-preserving repad (fixes smushed thin
                            # glyphs); polarity/bg-identical to the stretch.
                            # Default ON; set SC_NO_ASPECT_REPAD=1 to use
                            # the legacy stretch-to-square tail.
                            _canvas = _aspect_pad_resize_28(
                                _padded.astype(np.uint8), bg=255,
                            )
                            _hud_pri_crops.append(
                                _canvas.astype(np.float32) / 255.0,
                            )
                        else:
                            _pil = Image.fromarray(
                                _padded.astype(np.uint8),
                            ).resize((28, 28), Image.BILINEAR)
                            _hud_pri_crops.append(
                                np.array(_pil, dtype=np.float32) / 255.0,
                            )
                        _hud_pri_boxes.append((int(bx), int(by), int(bw), int(bh)))
                    _proportional_used = True
                    log.info(
                        "sc_ocr.signal: PROPORTIONAL segmenter accepted: "
                        "%d digit bboxes, n_digits=%s, composed=%r, conf=%.3f",
                        len(_hud_pri_boxes),
                        _prop_result.get("n_digits"),
                        _prop_result.get("details", {}).get("string_composed"),
                        _prop_result.get("confidence", 0.0),
                    )
        except Exception as _prop_exc:
            log.debug(
                "sc_ocr.signal: proportional segmenter failed (%s) — "
                "falling back to column-projection",
                _prop_exc,
            )

        # ── FALLBACK: column-projection segmenter (legacy path) ──
        # ``disable_gap_cut=True``: region2 crops have no label
        # intrusion (icon-anchored LHS sits flush with the digit
        # cluster), so the gap-cut would mistake a right-edge pill-
        # cap artifact for a label-to-value boundary and erase the
        # leading digits. See ``_segment_glyphs`` for details.
        #
        # NameError fix (v2.2.10): the prior call passed ``field=field``,
        # copy-pasted from the HUD per-mineral-field OCR path where
        # ``field`` is a parameter ("mass" / "resistance" /
        # "instability"). ``_signal_recognize_pil`` has no ``field``
        # variable in scope, so on the column-projection fallback this
        # raised ``NameError: name 'field' is not defined`` — swallowed
        # by the outer ``except Exception`` at the bottom of this try
        # block as a DEBUG log ("HUD-style primary CNN path failed").
        # The result: any user whose anchor missed the proportional
        # segmenter's confidence floor silently lost the primary CNN
        # voter, and the signature scan fell through to the lower-
        # ranked CRNN / gray gates. The reported runtime logs (anchor
        # miss + "name 'field' is not defined" + CRNN reading "2,520"
        # but lexicon_hit=False) are this exact failure cascade.
        #
        # Drop the kwarg: ``_segment_glyphs`` only branches on
        # ``field`` for HUD-specific behaviour (mass/resistance/
        # instability leading-narrow-span tuning); signatures don't
        # use any of those branches, so the default empty string is
        # the intended behaviour for this call.
        if not _proportional_used:
            _hud_pri_crops, _hud_pri_boxes = _segment_glyphs(
                _work_canon, _hud_bin, disable_gap_cut=True,
            )
        # The proportional segmenter produces bboxes that already
        # respect the structural prior (D,DDD or DD,DDD with comma
        # excluded), so it doesn't need the post-processing helpers
        # below — those are designed to clean up column-projection
        # output where commas get fused into digit spans, where
        # icon-shaped spans leak in from misdetected anchor crops,
        # and where wide spans need splitting. Skipping them when
        # the proportional path was used keeps the proportional
        # bboxes pristine.
        if not _proportional_used:
            # 0a. Trim comma protrusions fused into digit boxes. Mirrors
            # the HUD ``_dot_label_from_box`` size heuristic but inverted:
            # uses box HEIGHT vs the row's median to detect digit+comma
            # fusions where column-projection couldn't separate them. The
            # comma's known vertical extent (~3-5 px) reliably stands out
            # against median digit height (~20-25 px) — anything more
            # than 15% taller than the median has a fused comma below.
            _hud_pri_crops, _hud_pri_boxes = _trim_comma_fused_into_signature_boxes(
                _hud_pri_crops, _hud_pri_boxes, _work_canon, _hud_bin,
            )
            # 1. Drop icon-shaped spans via the blacklist pHash check.
            # When the NCC anchor finds a weak match on noise instead of
            # the real location-pin icon, the icon ends up inside ``work``
            # and the segmenter's leftmost output is the icon itself. The
            # offline glyph extractor uses this same filter; reusing it
            # here guarantees the icon NEVER appears in the live viewer's
            # tile rows even when the anchor misfires.
            _hud_pri_crops, _hud_pri_boxes = _drop_blacklisted_signature_glyphs(
                _hud_pri_crops, _hud_pri_boxes,
            )
            # 2. Drop the comma + enforce signature structure. The signal
            # CNN's training set has no comma class, so a comma-shaped
            # sliver between digits would otherwise be misclassified as a
            # stray digit, corrupting the read. The same pass enforces the
            # ``D,DDD`` / ``DD,DDD`` structural prior: post-comma is
            # ALWAYS exactly 3 digits. Anything more is a right-edge
            # artifact and gets trimmed.
            _hud_pri_crops, _hud_pri_boxes = _enforce_comma_signature_structure(
                _hud_pri_crops, _hud_pri_boxes,
            )
        # 3 + 4 — CRNN COUNT COERCION
        #
        # Column-projection path: always run split/merge. The
        # column-projection segmenter has no structural awareness, so
        # any count discrepancy is fair game for width-based fixes.
        #
        # Proportional path: only run coercion when the proportional
        # segmenter's count is wrong AND CRNN was confident enough to
        # trust as a count oracle. Proportional's bboxes are already
        # structure-aware (it knows the comma position, the digit
        # slot count from the constrained format prior), so blindly
        # post-processing its output regresses captures the
        # proportional segmenter already had right. Empirically: with
        # blind coercion on proportional we lost 2 captures vs not
        # touching it; with conditional coercion (only when the
        # proportional count visibly disagrees with the CRNN count)
        # we preserve those wins and gain back the cases proportional
        # got wrong.
        _need_count_coercion = (
            (not _proportional_used)
            or (
                _crnn_rgb_n_digits is not None
                and len(_hud_pri_boxes) != _crnn_rgb_n_digits
            )
        )
        if _need_count_coercion:
            # 3. Split merged-digit spans. _segment_glyphs intentionally
            # lacks a wide-span splitter (would slice '%' on resistance/
            # instability), but signature values are pure 4-5 digit
            # integers — safe to split. Fires on under-count.
            _hud_pri_crops, _hud_pri_boxes = _split_wide_signature_spans(
                _work_canon, _hud_bin, _hud_pri_crops, _hud_pri_boxes,
                expected_count=_expected_digit_count,
            )
            # 4. Merge over-split spans — mirror of (3) for the OPPOSITE
            # failure mode. When the segmenter produces too MANY boxes
            # (e.g. a "0" sliced through its hole, a "1" treated as two
            # thin halves, a comma promoted to a span), the count is
            # over and per-glyph CNNs read garbage. This pass merges
            # the narrowest adjacent pairs until the count matches the
            # CRNN's read.
            _hud_pri_crops, _hud_pri_boxes = _merge_narrow_signature_spans(
                _work_canon, _hud_bin, _hud_pri_crops, _hud_pri_boxes,
                expected_count=_expected_digit_count,
            )
        # Snapshot the segmenter output BEFORE the icon-drop step
        # mutates it. The RGB shadow path (later in this branch) needs
        # to iterate over the FULL set of bbox positions even when
        # the gray CNN's icon-drop empties ``_hud_pri_boxes`` — when
        # the gray CNN misclassifies all crops as ``@`` (training-
        # distribution mismatch), we still want the RGB CNN's votes
        # to reach the N-way consensus gate.
        _hud_pri_boxes_pre_drop: list[tuple[int, int, int, int]] = list(
            _hud_pri_boxes or [],
        )
        if _hud_pri_crops:
            # ── Per-glyph dilation REVERTED ──
            # An earlier attempt applied 1-pixel grayscale dilation
            # (``_grow_signal_glyph_holes``) on each 28×28 crop here
            # before classification, intending to grow the loop
            # interiors of 0/6/8/9. The dilation was symmetric (it
            # also eroded the digit strokes by 1 pixel on every
            # side), and on the 28×28 scale where strokes are only
            # 3–5 pixels wide to begin with, that erosion thinned
            # them down to 1–2 pixels — visible in the live viewer
            # as nearly-empty tiles where strokes had been almost
            # completely erased. Both primary AND secondary
            # classifiers misread badly as a result. Helper kept
            # available (``_grow_signal_glyph_holes`` above) for
            # potential future use with a more selective kernel
            # (e.g. dilate only true ENCLOSED bright regions, not
            # bright pixels at stroke edges).
            #
            # Hole legibility now comes from the work-crop level
            # enhancements (gamma 0.75 + bright-side push) which
            # brighten mid-tones inside loops without eroding stroke
            # geometry. Those happen on the upscaled work crop where
            # there's enough resolution headroom for the operation
            # to be safe.
            # Primary: signal-specific CNN trained on the user's
            # signature glyphs. Falls back to the HUD CNN if the
            # signal model is missing on disk so the pipeline still
            # produces a read on installs without signal training.
            _hud_pri_results = _classify_crops_signal(_hud_pri_crops)
            if not _hud_pri_results:
                log.debug(
                    "sc_ocr.signal: signal-CNN unavailable — "
                    "falling back to HUD CNN for primary classification",
                )
                _hud_pri_results = _classify_crops(_hud_pri_crops)
            # ── Lexicon-backtracked classification carry-over ──
            # When the proportional segmenter accepted a hypothesis
            # that needed lexicon backtracking, its per-position
            # classifications already carry the corrected reads
            # (e.g. the leading '@' was rewritten to '3' because
            # ``3,XXX`` is the lexicon-valid composition). The HUD
            # primary CNN re-runs on the same crops here without the
            # lexicon, so it produces the SAME unhealed classes as
            # the segmenter saw before backtracking. To avoid
            # discarding the segmenter's healing work, we adopt any
            # segmenter classification that disagrees with the CNN
            # primary AND doesn't itself say '@' — i.e. backtracked
            # positions get their corrected char propagated, while
            # positions where both the segmenter and the CNN say '@'
            # remain '@' (and get dropped below) so the icon-drop
            # logic still kicks in for genuine icon overlap. Confidence
            # for adopted positions is taken from the segmenter (it
            # reflects the post-backtrack mean).
            if (
                _proportional_used
                and _hud_pri_results
                and _seg_pri_results
                and len(_seg_pri_results) == len(_hud_pri_results)
            ):
                _adopted = 0
                _new_results = list(_hud_pri_results)
                for _i, (_seg_pair, _cnn_pair) in enumerate(
                    zip(_seg_pri_results, _hud_pri_results),
                ):
                    _seg_ch, _seg_conf = _seg_pair
                    _cnn_ch, _cnn_conf = _cnn_pair
                    if (
                        _seg_ch.isdigit()
                        and _cnn_ch == "@"
                    ):
                        # Segmenter has a digit, CNN says '@' — adopt
                        # the segmenter's healed read.
                        _new_results[_i] = (_seg_ch, _seg_conf)
                        _adopted += 1
                if _adopted > 0:
                    _hud_pri_results = _new_results
                    log.info(
                        "sc_ocr.signal: adopted %d segmenter-backtracked "
                        "classification(s) over CNN '@' reads",
                        _adopted,
                    )
            # ── Drop CNN classifications == location-pin icon ──
            # The signal CNN is now trained with an 11th '@' class
            # (the location-pin icon). Any crop the CNN classified as
            # '@' is the icon — the segmenter included it because the
            # NCC anchor matched on a digit pair instead of the actual
            # icon, leaving the icon's body inside the work crop. Drop
            # those crops + their results so the joined digit string,
            # the secondary classifier, and the live viewer only see
            # real digit positions.
            if _hud_pri_results:
                _kept = [
                    i for i, (ch, _conf) in enumerate(_hud_pri_results)
                    if ch != "@"
                ]
                _n_dropped = len(_hud_pri_results) - len(_kept)
                if _n_dropped > 0:
                    log.info(
                        "sc_ocr.signal: dropped %d crop(s) classified "
                        "as the icon class '@' (signal-CNN identified "
                        "them as location-pin shapes, not digits)",
                        _n_dropped,
                    )
                    _hud_pri_crops = [_hud_pri_crops[i] for i in _kept]
                    _hud_pri_results = [
                        _hud_pri_results[i] for i in _kept
                    ]
                    if _hud_pri_boxes is not None and len(
                        _hud_pri_boxes,
                    ) == len(_kept) + _n_dropped:
                        _hud_pri_boxes = [
                            _hud_pri_boxes[i] for i in _kept
                        ]
            # POLARITY ROUTING REFACTOR 2026-05:
            #   * ``_classify_crops_signal_inv`` now accepts CANONICAL
            #     bright-on-dark crops (its internal _feed_signal_cnn
            #     handles the polarity match — the model expects
            #     bright-on-dark, so canonical feeds AS-IS).
            #   * ``_classify_crops_inv`` (HUD-inverted CNN fallback)
            #     still wants PRE-INVERTED crops because its model
            #     expects dark-on-light. We keep ``_hud_sec_crops``
            #     pre-inverted only for that fallback path and the
            #     8-vs-0 pixel rule (the rule is polarity-symmetric
            #     in its corner-sampling, but we keep the input it has
            #     been validated on).
            _hud_sec_crops = [
                np.clip(1.0 - c, 0.0, 1.0).astype(np.float32)
                for c in _hud_pri_crops
            ]
            # Prefer the signal-specific inverted CNN. Falls back to
            # the HUD-inverted CNN if the signal_inv model isn't on
            # disk yet (older installs, or before signal_inv has been
            # trained), so the pipeline still produces a secondary
            # opinion either way. Once model_signal_inv_cnn.onnx is
            # in place, the signature pipeline is fully isolated from
            # the HUD-trained models — primary and secondary both use
            # signal-trained models, just with opposite polarities.
            #
            # Pass CANONICAL crops (bright-on-dark) — the signal_inv
            # function applies its own polarity routing.
            _hud_sec_results = _classify_crops_signal_inv(_hud_pri_crops)
            if not _hud_sec_results:
                log.debug(
                    "sc_ocr.signal: signal_inv CNN unavailable — "
                    "falling back to HUD-inverted CNN for secondary"
                )
                # HUD-inverted CNN's caller convention is unchanged —
                # it wants pre-inverted crops.
                _hud_sec_results = _classify_crops_inv(_hud_sec_crops)
            if not _hud_sec_results:
                # Both models unavailable — emit "?" placeholders so
                # the viewer still renders the secondary tile row.
                _hud_sec_results = [("?", 0.0) for _ in _hud_sec_crops]
            # Apply the 8-vs-0 pixel rule to the SECONDARY (HUD-
            # inverted CNN) too. The secondary CNN was trained on HUD
            # digits where the 8/0 confusion isn't present in the same
            # rendering, but the SAME signature crops we feed it
            # exhibit the waist-vs-strut structural difference. The
            # rule's polarity-agnostic corner-sampling means it works
            # equally well on the inverted-polarity crops the
            # secondary classifier consumes. We do NOT modify
            # ``_classify_crops_inv`` itself — that function is also
            # used by the HUD pipeline (mass / resistance /
            # instability secondary), and we don't want to perturb
            # those reads. Applying the rule HERE keeps the override
            # signature-only.
            _hud_sec_results = _apply_eight_vs_zero_rule(
                _hud_sec_crops, _hud_sec_results,
            )
            # 1-vs-7 rule was also wired here but disabled due to
            # the same threshold mismatch documented in
            # _classify_crops_signal — the signal-font ``7``'s
            # bottom-left diagonal looks like a ``1``-bottom-serif at
            # the rule's bands. The comma-trimmer above is the real
            # fix for the ``7``-misread-as-``1`` case the rule was
            # meant to catch.
            # Strip '%' / '.' from secondary — the HUD-inverted CNN
            # has those in its alphabet but signature digits never do.
            # When a fused digit+comma crop slips through segmentation,
            # the secondary classifies the dangling-comma shape as '%'
            # and that surfaces as a spurious tile in the live viewer.
            # Replace with the primary's class at confidence 0 so the
            # tile shows the digit and the dual-agree mean conf gate
            # correctly accounts for the missing secondary opinion.
            _hud_sec_results = _strip_non_digit_signature_secondary(
                _hud_sec_results, _hud_pri_results,
            )
            # ── 0/6/8 secondary-tiebreak (signature-only) ──
            # When primary AND secondary disagree on a glyph but both
            # of their labels are members of {0, 6, 8}, defer to the
            # secondary — provided it's confident (≥0.85). The signal
            # CNN's primary classifier on chromatically-aberrated SC
            # captures sometimes misreads `0` → `8` (a colour-fringe
            # shadow across the mid-band looks like an `8`'s waist) or
            # `6` → `8` (the closed-loop top looks like an `8`'s top
            # loop). The secondary inverse-polarity classifier, fed
            # the SAME crops with pixels inverted, sees DIFFERENT
            # artifacts on the same crop — its read is structurally
            # complementary, not just a duplicate vote. Field-tested
            # case: a "17,020" capture where primary's glyph[2] read
            # `8` at conf=1.00 while secondary correctly read `0` at
            # conf=1.00; the strict gate (min conf ≥ 0.85) accepted
            # the primary's wrong "17820" and produced an incorrect
            # signature value. With this tiebreak, the disagreement
            # is resolved in secondary's favour and the gate sees
            # "17020" → correct.
            #
            # Why scoped to {0, 6, 8}: those three digits share gross
            # silhouette (rounded rectangle) and the primary/secondary
            # disagreements that are STRUCTURAL (not just noise) cluster
            # almost entirely within that set. Letting the rule fire
            # outside {0, 6, 8} would override low-confidence primary
            # reads on cleanly-distinct digit pairs (e.g. primary `1`
            # vs secondary `7`) where neither classifier has the
            # systematic polarity-shadow advantage. Conservative scope
            # keeps the rule precise.
            _hud_pri_results = _apply_zero_six_eight_secondary_tiebreak(
                _hud_pri_results, _hud_sec_results,
            )
            # ── Shadow RGB CNN (NO VOTING — diagnostic only) ──
            # Run the experimental ``model_signal_rgb_cnn.onnx`` over
            # the SAME segmented glyphs the primary/secondary CNNs
            # just classified. Produces a third tile row in the live
            # viewer (``signal_rgb``) so we can watch what an RGB-
            # input classifier would say without any of its votes
            # touching the strict / dual-agree gate that decides the
            # actual OCR output.
            #
            # Box coords in ``_hud_pri_boxes`` are in the same
            # coordinate system as ``_work_rgb`` (we threaded the
            # parallel RGB through the row-isolate + Lanczos upscale
            # earlier so they stay aligned), so we can slice
            # ``_work_rgb`` at those boxes and run the RGB CNN.
            try:
                from PIL import Image as _PILImage_rgb
                _hud_rgb_crops: list[np.ndarray] = []
                _h_w_rgb = _work_rgb.shape[0]
                _w_w_rgb = _work_rgb.shape[1]
                # Use the pre-drop bboxes so the RGB CNN gets to vote
                # on every segmented digit position, even when the
                # gray CNN's icon-drop pruned ``_hud_pri_boxes``. The
                # N-way consensus gate downstream needs RGB+RGB-INV
                # voters per position — without this fallback they'd
                # both abstain when gray is unavailable.
                for _box in (_hud_pri_boxes_pre_drop or _hud_pri_boxes or []):
                    _bx, _by, _bw, _bh = _box
                    if _bw < 1 or _bh < 1:
                        continue
                    if _bx + _bw > _w_w_rgb or _by + _bh > _h_w_rgb:
                        continue
                    # OLD-STYLE PAD+RESIZE (restored 2026-05-09):
                    # mirrors the WingmanAI reference flow — pad with
                    # white pad=2 and bilinear-resize to 28×28. The
                    # trained ``signal_rgb_cnn_v2`` was fitted on this
                    # distribution (verified by feeding the same
                    # crops directly: WingmanAI runtime reads 16,960
                    # at 1.00 mean confidence per digit on a typical
                    # clean capture).
                    _glyph_rgb = _work_rgb[
                        _by:_by + _bh, _bx:_bx + _bw,
                    ].astype(np.float32)
                    _pad = 2
                    _padded = np.full(
                        (_bh + _pad * 2, _bw + _pad * 2, 3),
                        255.0, dtype=np.float32,
                    )
                    _padded[_pad:_pad + _bh, _pad:_pad + _bw] = _glyph_rgb
                    if not os.environ.get("SC_NO_ASPECT_REPAD"):
                        _hud_rgb_crops.append(
                            _aspect_pad_resize_28(
                                _padded.astype(np.uint8), bg=255,
                            )
                        )
                    else:
                        _pil_rgb_glyph = _PILImage_rgb.fromarray(
                            _padded.astype(np.uint8), mode="RGB",
                        ).resize((28, 28), _PILImage_rgb.BILINEAR)
                        _hud_rgb_crops.append(
                            np.asarray(_pil_rgb_glyph, dtype=np.uint8),
                        )
                if _hud_rgb_crops and os.environ.get("SC_DUMP_GLYPHS"):
                    try:
                        _gn = len(_hud_rgb_crops)
                        _mont = np.full((28, _gn * 30 + 2, 3), 40, np.uint8)
                        for _gi, _gc in enumerate(_hud_rgb_crops):
                            _mont[0:28, _gi * 30:_gi * 30 + 28] = _gc.astype(
                                np.uint8,
                            )
                        from PIL import Image as _DG
                        _DG.fromarray(_mont, "RGB").resize(
                            (_mont.shape[1] * 5, 28 * 5), _DG.NEAREST,
                        ).save(os.environ["SC_DUMP_GLYPHS"])
                    except Exception:
                        pass
                if _hud_rgb_crops:
                    _hud_rgb_results = _classify_crops_signal_rgb(_hud_rgb_crops)
                    if _hud_rgb_results:
                        log.info(
                            "sc_ocr.signal: SHADOW RGB CNN read %r "
                            "(%d glyphs, NOT consumed by gate)",
                            "".join(c for c, _ in _hud_rgb_results),
                            len(_hud_rgb_results),
                        )
                        # Dump to viewer as the ``signal_rgb`` voter row.
                        _dump_glyphs(
                            "signature", "signal_rgb",
                            _hud_rgb_crops, _hud_rgb_results,
                        )

                    # ── PROMOTED voter: polarity-inverted RGB CNN ──
                    # Used to be a SHADOW (debug-only) voter; promoted
                    # to a real voter under the new digit-position
                    # consensus :func:`_vote_on_digit_position`.
                    #
                    # Decorrelated peer voter to ``signal_rgb`` —
                    # different decision boundary because trained on
                    # the channel-inverted distribution.
                    #
                    # POLARITY ROUTING REFACTOR 2026-05: this function
                    # accepts crops in the same convention as the
                    # RGB-PRIMARY (``_classify_crops_signal_rgb``);
                    # the per-model polarity routing (auto-detect via
                    # ``_route_rgb_to_bod``) happens internally, so
                    # we just pass the same ``_hud_rgb_crops`` here.
                    try:
                        _hud_rgb_inv_crops = list(_hud_rgb_crops)
                        _hud_rgb_inv_results = _classify_crops_signal_rgb_inv(
                            _hud_rgb_inv_crops,
                        )
                        if _hud_rgb_inv_results:
                            log.info(
                                "sc_ocr.signal: RGB-INV CNN read "
                                "%r (%d glyphs)",
                                "".join(c for c, _ in _hud_rgb_inv_results),
                                len(_hud_rgb_inv_results),
                            )
                            _dump_glyphs(
                                "signature", "signal_rgb_inv",
                                _hud_rgb_inv_crops, _hud_rgb_inv_results,
                            )
                    except Exception as _rgb_inv_exc:
                        log.debug(
                            "sc_ocr.signal: RGB-INV CNN failed "
                            "(%s) — falling through to other voters",
                            _rgb_inv_exc,
                        )
            except Exception as _rgb_shadow_exc:
                log.debug(
                    "sc_ocr.signal: shadow RGB CNN failed (%s) — "
                    "no impact on production read",
                    _rgb_shadow_exc,
                )
            # Always dump the value crop — useful for debugging even on
            # degraded scans (so the user can see the work crop fed
            # into segmentation).
            _dump_value_crop("signature", base)
            # ── Sticky tile-row gate ──
            # Only update the SIGNATURE (PRIMARY) / SIGNATURE
            # (SECONDARY) tile rows when this scan produced 4-5 valid
            # digit crops. On degraded scans (binary noise dropped a
            # span, classifier hallucinated, etc.), keeping the
            # previous good dump means the live viewer "sticks" on the
            # last frame where everything aligned, rather than
            # flickering between 5 boxes and 1 box as the user
            # observed. The runtime's stable_signal filter already
            # protects the bubble; this protects the diagnostic UI.
            #
            # We count classifications matching ``isdigit()`` because
            # the icon class '@' is dropped earlier in this branch and
            # any '@' still present means the icon-drop missed something
            # — we'd rather skip the dump than display a glyph the
            # downstream pipeline already rejected.
            _n_digit_crops = sum(
                1 for ch, _ in _hud_pri_results if ch.isdigit()
            )
            if 4 <= _n_digit_crops <= 5:
                _dump_glyphs(
                    "signature", "primary",
                    _hud_pri_crops, _hud_pri_results,
                )
                _dump_glyphs(
                    "signature", "secondary",
                    _hud_sec_crops, _hud_sec_results,
                )
            else:
                log.info(
                    "sc_ocr.signal: skipping primary/secondary tile "
                    "dump (only %d digit-classified crops, need 4-5) "
                    "— viewer will keep showing previous good scan",
                    _n_digit_crops,
                )
    except Exception as _hud_path_exc:
        # Severity split (v2.2.10): this catch-all spent an unknown
        # amount of time silently swallowing a ``NameError: name
        # 'field' is not defined`` at DEBUG level because the
        # column-projection fallback referenced an undefined ``field``
        # (now fixed above). Genuine programming bugs
        # (NameError / AttributeError / TypeError / SyntaxError) get
        # WARNING so the audit pipeline + log triage will surface them
        # on the next user report — every other exception type stays
        # at DEBUG so expected runtime conditions (the CNN model
        # being unavailable, etc.) don't spam the log.
        if isinstance(
            _hud_path_exc,
            (NameError, AttributeError, TypeError, SyntaxError),
        ):
            log.warning(
                "sc_ocr.signal: HUD-style primary CNN path raised "
                "%s (%s) — likely a code bug, not a runtime failure. "
                "Falling through to downstream voters.",
                type(_hud_path_exc).__name__, _hud_path_exc,
            )
        else:
            log.debug(
                "sc_ocr.signal: HUD-style primary CNN path failed: %s",
                _hud_path_exc,
            )

    # ──────────────────────────────────────────────────────────────
    # NEW VOTER HIERARCHY (user-configured 2026-05-06):
    #   1. RGB + RGB_INV agreement     ← PRIMARY
    #   2. CRNN sequence read          ← SECONDARY
    #   3. Gray CNN + Gray_INV gate    ← BACKUP (existing block below)
    #   4. Tesseract ensemble          ← LAST RESORT
    #
    # The RGB CNNs were trained on a user-cleaned, manually-reviewed
    # pool of cyan-on-dark color samples, so their reads of the SC
    # signal panel are typically more reliable than the gray CNNs
    # (which were trained from HUD-extracted samples). Field-tested:
    # RGB nailed `16,960` confidently after the cleanup run while
    # gray PRIMARY/SECONDARY were misclassifying digit 9 as 8 etc.
    #
    # Order within RGB primary (mirror of the gray gate's structure):
    #   strict     : every per-glyph confidence ≥ 0.85
    #   dual-agree : RGB == RGB_INV exactly AND mean confs ≥ 0.70 each
    # CRNN secondary: 4-5 digits, mean conf ≥ 0.85, value in range.
    # ──────────────────────────────────────────────────────────────

    def _accept_signal_value(val: int, source_label: str) -> int:
        """Update the stable-signal hysteresis buffer and return the
        currently-displayed stable value. Same logic the existing
        gray and CRNN gates use — DRY'd out so the new RGB primary
        and CRNN secondary gates don't duplicate it.

        Returns ``_STABLE_SIGNAL`` so callers can ``return`` directly.
        """
        global _STABLE_SIGNAL
        # ── Units 0/5 structural snap (canonical signature prior) ──
        # Snap a non-0/5 read to its 0/5 sibling BEFORE it enters the
        # stable buffer, so hysteresis consensus forms on the canonical
        # value (chart-matchable) rather than a transient/misread units
        # digit. No-op when the read already ends in 0/5. See
        # _snap_signal_units_05.
        _snapped = _snap_signal_units_05(
            val,
            _LAST_SIGNAL_UNITS_HINT[0] if _LAST_SIGNAL_UNITS_HINT else None,
        )
        if _snapped != val:
            log.info(
                "sc_ocr.signal: units 0/5 snap %d -> %d (%s)",
                val, _snapped, source_label,
            )
            val = _snapped
        _RECENT_SIGNAL_READS.append(val)
        if _STABLE_SIGNAL is None:
            _STABLE_SIGNAL = val
        elif val != _STABLE_SIGNAL:
            _recent = list(_RECENT_SIGNAL_READS)[-_SIGNAL_AGREEMENT_REQ:]
            if (
                len(_recent) >= _SIGNAL_AGREEMENT_REQ
                and all(r == val for r in _recent)
            ):
                log.info(
                    "sc_ocr.signal: stable swap %d → %d (%s consensus)",
                    _STABLE_SIGNAL, val, source_label,
                )
                _STABLE_SIGNAL = val
        # ── NERVOUS SYSTEM (region2 afferent + consistency reflex) ──
        # Record the RAW accepted read (not the hysteresis value) so the
        # flap reflex sees oscillation, with its pill-anchored crop and
        # comma provenance. Telemetry only — never affects the return.
        try:
            from . import signal_record as _sigrec
            _sigrec.write(
                val, source_label, get_last_signal_crop_box(),
                _LAST_SIGNAL_COMMA_X[0] if _LAST_SIGNAL_COMMA_X else None,
            )
        except Exception:
            pass
        return _STABLE_SIGNAL

    # ── (0-CRNN) RGB CRNN WHOLE-STRIP gate ──
    # Whole-strip OCR via :func:`_classify_signal_via_crnn_rgb` runs the
    # SC mining-signature RGB CRNN (``model_signal_crnn_rgb.onnx``) on
    # the row-isolated, polarity-canonicalized ``_work_rgb`` crop. The
    # CRNN reads the whole signature value as a single sequence (CTC
    # decode), sidestepping per-glyph segmentation entirely — which is
    # the segmenter that the per-position voter hierarchy below depends
    # on, and the dominant failure mode on the production failure
    # profile (segmenter mis-splits → wrong crops → confidently wrong
    # CNN reads).
    #
    # End-to-end measured at 195/201 (97%) on the held-out test set vs
    # production's 20/201 (10%) on the same captures, so we run this
    # FIRST and only fall through to the per-glyph gates when the CRNN
    # produces a low-confidence / out-of-range / off-lexicon read.
    #
    # Acceptance:
    #   * 4-5 digit string after stripping the model's commas
    #   * integer value in ``[1000, 35000]``
    #   * lexicon match (when the chart-values table is loaded) at
    #     mean confidence ≥ 0.55 — OR mean confidence ≥ 0.80 when
    #     lexicon is unloaded / value is missing from the chart
    #
    # Best-effort: any failure (ONNX missing, inference error, range
    # violation, low conf) falls through silently to the existing
    # gates. The CRNN never blocks the rest of the pipeline.
    #
    # The actual CRNN forward pass already ran earlier (the "EARLY
    # CRNN PASS" block before segmentation), so the variables
    # ``_crnn_rgb_text`` / ``_crnn_rgb_mean_conf`` / ``_crnn_rgb_digits``
    # are already populated when the model loaded successfully. This
    # gate just consumes the stashed values — single forward pass per
    # tick.
    if _crnn_rgb_text:
        _crnn_rgb_in_range = False
        _crnn_rgb_val: Optional[int] = None
        if 4 <= len(_crnn_rgb_digits) <= 5:
            try:
                _crnn_rgb_val = int(_crnn_rgb_digits)
                _crnn_rgb_in_range = (
                    1000 <= _crnn_rgb_val <= 35000
                )
            except ValueError:
                _crnn_rgb_val = None
        _crnn_rgb_lexicon_known = bool(_KNOWN_SIGNAL_VALUES)
        _crnn_rgb_lexicon_hit = (
            _crnn_rgb_val is not None
            and _crnn_rgb_lexicon_known
            and _crnn_rgb_val in _KNOWN_SIGNAL_VALUES
        )
        # Lower confidence floor when the lexicon confirms the read —
        # a chart-known value at moderate conf is a much safer accept
        # than a high-conf hallucination. Mirrors the gating shape of
        # the N-way consensus gate below.
        #
        # Threshold history:
        #   0.55  — initial; recovered most lexicon-confirmed reads
        #           but missed borderline 0.40-0.54 confident cases
        #           (cap_20260418_085716_872: CRNN reads 8276 at 0.50
        #           conf, in verified cache, would otherwise be the
        #           best available read).
        #   0.40  — sweep; trades small risk of accepting marginal
        #           reads for picking up the 0.40-0.54 lex-confirmed
        #           band. The lexicon membership check is doing the
        #           hallucination filtering (chart + verified union),
        #           so a marginal-conf read that's still in the
        #           lexicon is much safer than a strict confidence
        #           floor implies.
        # Crop-mode anti-hallucination check. Query the signature
        # anchor module for whichever resolution mode produced the
        # current crop:
        #   * "combo" / "digit_only" — digit-cluster anchor confirmed
        #     real digit-shaped spans exist in this crop. Apply normal
        #     lexicon-relaxed gating.
        #   * "icon_only" — only icon NCC matched; find_digit_cluster
        #     returned None. The crop is GUESSED from icon position
        #     and may be empty (e.g. the rock has left view but the
        #     panel chrome lingers). The CRNN can hallucinate a
        #     known-lexicon value from background noise at near-1.0
        #     confidence — DO NOT lexicon-relax in this mode.
        #   * "none" — both anchors missed; we shouldn't have a crop
        #     at all, but defend against it.
        # In production (user's 21,200 hallucination report), the
        # signal panel was still on screen but the rock had moved
        # out of view, icon NCC scored 0.51-0.66 and was cache-
        # smoothed to a stale position, the cropped pixels were
        # empty sky, and the CRNN reported "21,200" @ 0.99 because
        # that value was the lexicon's nearest match. The icon-only
        # gate below promotes the threshold from 0.40 → 0.92,
        # killing the lexicon-confirmed-but-empty-crop accept path.
        try:
            from . import signal_anchor as _sa_mode
            _crop_mode = _sa_mode.last_crop_mode()
        except Exception:
            _crop_mode = "combo"  # fail-open if module missing
        _icon_only_mode = (_crop_mode == "icon_only")
        # Crop content check for icon_only mode. The CRNN can report
        # a known-lexicon value at 0.99 confidence even on totally-
        # empty crops (lexicon-biased hallucination). Raising the
        # threshold to 0.92 is insufficient — the user's reported
        # bug had mean=0.99 reads on solid-sky crops. Add a HARD
        # ink-density check: in icon_only mode, refuse the CRNN
        # gate entirely if the crop has < 1.5% of pixels above the
        # median + 0.5σ threshold (i.e. essentially uniform
        # background with no digit strokes). This kills the
        # hallucination at its root.
        _icon_only_crop_empty = False
        if _icon_only_mode:
            try:
                _ic_src = _work_rgb_for_crnn if _work_rgb_for_crnn is not None else _work_rgb
                if hasattr(_ic_src, "convert"):
                    _ic_gray = np.array(_ic_src.convert("L"), dtype=np.uint8)
                elif hasattr(_ic_src, "ndim") and _ic_src.ndim == 3:
                    _ic_gray = _ic_src[..., :3].mean(axis=-1).astype(np.uint8)
                elif hasattr(_ic_src, "ndim") and _ic_src.ndim == 2:
                    _ic_gray = _ic_src.astype(np.uint8)
                else:
                    _ic_gray = None
                if _ic_gray is not None and _ic_gray.size > 0:
                    _ic_canon = _canonicalize_polarity(_ic_gray)
                    _ic_med = float(np.median(_ic_canon))
                    _ic_std = float(_ic_canon.std())
                    _ic_thr = _ic_med + 0.5 * _ic_std
                    _ic_frac = float(
                        (_ic_canon > _ic_thr).sum()
                    ) / max(1, _ic_canon.size)
                    if _ic_frac < 0.015:
                        _icon_only_crop_empty = True
            except Exception as _ic_exc:
                log.debug(
                    "sc_ocr.signal: icon-only ink check failed: %s",
                    _ic_exc,
                )

        if _icon_only_mode and _icon_only_crop_empty:
            # Bypass the gate entirely — there's nothing to read in
            # this crop. Setting threshold above 1.0 guarantees the
            # confidence check below fails. Caller falls through to
            # consensus / lock-cache logic which preserves the last
            # good displayed value.
            _crnn_rgb_thresh = 2.0
        elif _crnn_rgb_lexicon_hit and not _icon_only_mode:
            _crnn_rgb_thresh = 0.40
        elif _crnn_rgb_lexicon_hit and _icon_only_mode:
            # Lexicon hit BUT no digit-cluster anchor — likely an
            # empty-crop hallucination of a known value. Require near-
            # certain confidence to accept.
            _crnn_rgb_thresh = 0.92
        elif not _crnn_rgb_lexicon_known:
            _crnn_rgb_thresh = 0.80
        else:
            # Lexicon loaded but this read isn't in it — refuse to
            # consume. Setting threshold above 1.0 guarantees the
            # confidence check below fails, so we fall through.
            _crnn_rgb_thresh = 2.0
        log.info(
            "sc_ocr.signal: RGB CRNN text=%r digits=%r mean=%.2f "
            "in_range=%s lexicon_known=%s lexicon_hit=%s "
            "crop_mode=%s thresh=%.2f",
            _crnn_rgb_text, _crnn_rgb_digits, _crnn_rgb_mean_conf,
            _crnn_rgb_in_range, _crnn_rgb_lexicon_known,
            _crnn_rgb_lexicon_hit, _crop_mode, _crnn_rgb_thresh,
        )
        if (
            _crnn_rgb_val is not None
            and _crnn_rgb_in_range
            and _crnn_rgb_mean_conf >= _crnn_rgb_thresh
        ):
            # ── (0-VOTE) Unified CNN+CRNN voter override ──
            # Before accepting the production CRNN, consult the independent
            # panel: the per-glyph CNN consensus + the 4 glyph-duplicator
            # CRNNs. If NOTHING backs production's value and ≥2 members agree
            # on a different in-range value, production is the lone dissenter
            # — accept the panel consensus instead. When the CNN family backs
            # production (common case + the 21565 leading-1↔2 case) it's a
            # no-op, so passing reads stay byte-identical. Opt out via
            # SC_SIGNAL_VOTER_OVERRIDE=0 (keeps the offline harness stable).
            if os.environ.get("SC_SIGNAL_VOTER_OVERRIDE", "1") != "0":
                try:
                    _ovr = _signal_voter_panel_decision(
                        _crnn_rgb_val,
                        locals().get("_crnn_rgb_input"),
                        locals().get("_hud_pri_results"),
                        locals().get("_hud_sec_results"),
                        locals().get("_hud_rgb_results"),
                        locals().get("_hud_rgb_inv_results"),
                        _KNOWN_SIGNAL_VALUES if _KNOWN_SIGNAL_VALUES else None,
                    )
                except Exception as _ovr_exc:
                    log.debug(
                        "sc_ocr.signal: voter override error (%s)", _ovr_exc)
                    _ovr = None
                if _ovr is not None and _ovr != _crnn_rgb_val:
                    log.info(
                        "sc_ocr.signal: VOTER OVERRIDE — production CRNN %r "
                        "is the lone dissenter; CNN+glyph-CRNN panel → %d",
                        _crnn_rgb_digits, _ovr,
                    )
                    return _accept_signal_value(_ovr, "crnn-cnn-voter")
            _crnn_rgb_gate = (
                "rgb-crnn-lexicon" if _crnn_rgb_lexicon_hit
                else "rgb-crnn-strict"
            )
            log.info(
                "sc_ocr.signal: RGB CRNN gate accepted text=%r "
                "mean=%.2f gate=%s → %d (skipping per-glyph voters)",
                _crnn_rgb_text, _crnn_rgb_mean_conf, _crnn_rgb_gate,
                _crnn_rgb_val,
            )
            try:
                _dump_signature_winner(
                    raw_text=_crnn_rgb_text,
                    digits=_crnn_rgb_digits,
                    mean_conf=_crnn_rgb_mean_conf,
                    validated_value=_crnn_rgb_val,
                    rejection_reason=None,
                    per_digit_classifications=None,
                )
                _clear_viewer_entry("signature", "crnn")
                _clear_viewer_entry("signature", "cnn")
            except Exception as _crnn_rgb_winner_exc:
                log.debug(
                    "sc_ocr.signal: RGB CRNN winner dump failed: %s",
                    _crnn_rgb_winner_exc,
                )
            return _accept_signal_value(_crnn_rgb_val, _crnn_rgb_gate)

    # ── (0-JOINT) CRNN ↔ per-glyph CNN AGREEMENT gate ──
    # CRNN gate above rejected (low conf or lexicon-miss), but we
    # still have CRNN's text in the stash. The per-glyph CNN voters
    # have ALSO just run, against segmenter boxes whose count was
    # coerced to match CRNN's digit count. Two genuinely independent
    # reading mechanisms — whole-strip CRNN vs per-glyph CNN voting
    # — that now agree at the string level is strong joint evidence
    # for the read being correct.
    #
    # This gate catches:
    #   * CRNN at 0.40-0.54 mean conf (below the lexicon-confirmed
    #     threshold) — too unsure to accept alone, fine to accept when
    #     the per-glyph stack agrees.
    #   * CRNN read that's off-lexicon (number not in the chart) —
    #     the CRNN-only gate refuses those, but two-source agreement
    #     overrides the safety net.
    #
    # Acceptance: CRNN's digit string equals the per-glyph consensus
    # string AND mean per-position confidence ≥ 0.55.
    if (
        _crnn_rgb_text
        and _crnn_rgb_digits
        and 4 <= len(_crnn_rgb_digits) <= 5
        and (_hud_pri_results or _hud_rgb_results)
    ):
        try:
            _joint_consensus = _vote_on_digit_string(
                primary_results=_hud_pri_results or None,
                secondary_results=_hud_sec_results or None,
                rgb_results=_hud_rgb_results or None,
                rgb_inv_results=_hud_rgb_inv_results or None,
                lexicon=_KNOWN_SIGNAL_VALUES if _KNOWN_SIGNAL_VALUES else None,
            )
        except Exception as _joint_exc:
            log.debug(
                "sc_ocr.signal: joint-accept N-way vote failed (%s) — "
                "skipping joint gate", _joint_exc,
            )
            _joint_consensus = None
        if _joint_consensus and _joint_consensus.get("string"):
            _joint_cnn_str = str(_joint_consensus["string"])
            _joint_cnn_mean = float(
                _joint_consensus.get("mean_confidence", 0.0)
            )
            _joint_voters = int(_joint_consensus.get("available_voters", 0))
            # Both strings are digit-only — the CRNN's comma was
            # already stripped and ``_vote_on_digit_string`` votes
            # over digit positions only.
            _joint_match = _joint_cnn_str == _crnn_rgb_digits
            try:
                _joint_val = int(_crnn_rgb_digits) if _joint_match else None
            except ValueError:
                _joint_val = None
            _joint_in_range = (
                _joint_val is not None and 1000 <= _joint_val <= 35000
            )
            log.info(
                "sc_ocr.signal: JOINT gate crnn=%r per_glyph=%r match=%s "
                "cnn_mean=%.2f voters=%d in_range=%s",
                _crnn_rgb_digits, _joint_cnn_str, _joint_match,
                _joint_cnn_mean, _joint_voters, _joint_in_range,
            )
            if (
                _joint_match
                and _joint_in_range
                and _joint_cnn_mean >= 0.55
                and _joint_voters >= 2
            ):
                log.info(
                    "sc_ocr.signal: JOINT GATE accepted crnn=%r == "
                    "per_glyph=%r (cnn_mean=%.2f voters=%d crnn_mean=%.2f) "
                    "→ %d",
                    _crnn_rgb_digits, _joint_cnn_str, _joint_cnn_mean,
                    _joint_voters, _crnn_rgb_mean_conf, _joint_val,
                )
                try:
                    _dump_signature_winner(
                        raw_text=_crnn_rgb_text,
                        digits=_crnn_rgb_digits,
                        mean_conf=max(_joint_cnn_mean, _crnn_rgb_mean_conf),
                        validated_value=_joint_val,
                        rejection_reason=None,
                        per_digit_classifications=None,
                    )
                    _clear_viewer_entry("signature", "crnn")
                    _clear_viewer_entry("signature", "cnn")
                except Exception as _joint_winner_exc:
                    log.debug(
                        "sc_ocr.signal: joint winner dump failed: %s",
                        _joint_winner_exc,
                    )
                return _accept_signal_value(_joint_val, "rgb-crnn-joint")

    # ── (0) N-WAY DIGIT-POSITION CONSENSUS gate ──
    # Voters: PRIMARY (gray), SECONDARY (gray inv), SIGNAL_RGB,
    # SIGNAL_RGB_INV. The :func:`_vote_on_digit_string` helper runs
    # per-position N-way voting and falls back to the lexicon when
    # individual positions can't reach consensus on the same tier.
    # Promoted from shadow-only: this is now the FIRST gate in the
    # hierarchy. The existing gates below remain as fallbacks (older
    # deployments may not have all four CNNs on disk; same-string
    # lexicon hits can still come through gates 1-3).
    #
    # Acceptance: 4-5 digit string, in range [1000, 35000], lexicon
    # match (when chart is loaded), mean conf ≥ 0.65. The conf bar
    # is lower than gates 1 and 3 because per-position voting filters
    # single-classifier overconfidence on a wrong class.
    if _hud_pri_results or _hud_rgb_results:
        try:
            _consensus = _vote_on_digit_string(
                primary_results=_hud_pri_results or None,
                secondary_results=_hud_sec_results or None,
                rgb_results=_hud_rgb_results or None,
                rgb_inv_results=_hud_rgb_inv_results or None,
                lexicon=_KNOWN_SIGNAL_VALUES if _KNOWN_SIGNAL_VALUES else None,
            )
        except Exception as _cons_exc:
            log.debug(
                "sc_ocr.signal: N-way digit consensus failed (%s) — "
                "falling through to legacy gates", _cons_exc,
            )
            _consensus = None

        if _consensus and _consensus.get("string"):
            _cons_str = _consensus["string"]
            _cons_mean = float(_consensus.get("mean_confidence", 0.0))
            _cons_n = int(_consensus.get("available_voters", 0))
            _cons_path = _consensus.get("consensus_path", "?")
            log.info(
                "sc_ocr.signal: N-way consensus voters=%d str=%r "
                "mean_conf=%.2f path=%s",
                _cons_n, _cons_str, _cons_mean, _cons_path,
            )
            try:
                _cons_val = int(_cons_str) if _cons_str else None
            except ValueError:
                _cons_val = None
            _cons_in_range = (
                _cons_val is not None
                and 1000 <= _cons_val <= 35000
                and 4 <= len(_cons_str) <= 5
            )
            _cons_lexicon_ok = (
                not _KNOWN_SIGNAL_VALUES
                or (_cons_val in _KNOWN_SIGNAL_VALUES)
            )
            # Lower confidence threshold (0.65) when the lexicon
            # confirms; 0.85 otherwise (matches the strict gates).
            _cons_thresh = 0.65 if _cons_lexicon_ok else 0.85
            if (
                _cons_in_range
                and _cons_lexicon_ok
                and _cons_mean >= _cons_thresh
                and _cons_n >= 2
            ):
                log.info(
                    "sc_ocr.signal: N-WAY CONSENSUS gate accepted str=%r "
                    "→ %d (voters=%d mean=%.2f path=%s)",
                    _cons_str, _cons_val, _cons_n, _cons_mean, _cons_path,
                )
                try:
                    _dump_signature_winner(
                        raw_text=_cons_str,
                        digits=_cons_str,
                        mean_conf=_cons_mean,
                        validated_value=_cons_val,
                        rejection_reason=None,
                        per_digit_classifications=None,
                    )
                    _clear_viewer_entry("signature", "crnn")
                    _clear_viewer_entry("signature", "cnn")
                except Exception as _cons_winner_exc:
                    log.debug(
                        "sc_ocr.signal: N-way winner dump failed: %s",
                        _cons_winner_exc,
                    )
                return _accept_signal_value(_cons_val, "n-way-consensus")

    # ── (1) RGB-PRIMARY gate ──
    # ``_hud_rgb_results`` and ``_hud_rgb_inv_results`` are populated
    # in the segmentation block above; if both classifiers fired and
    # agreed (or RGB alone is high-confidence), we take their read
    # without consulting any other voter.
    if _hud_rgb_results:
        _rgb_text = "".join(c for c, _ in _hud_rgb_results)
        _rgb_confs = [c for _, c in _hud_rgb_results]
        _rgb_inv_text = "".join(c for c, _ in _hud_rgb_inv_results)
        _rgb_inv_confs = [c for _, c in _hud_rgb_inv_results]
        _rgb_digits = "".join(c for c in _rgb_text if c.isdigit())
        _rgb_chars_ok = (
            _rgb_text
            and all(c.isdigit() for c in _rgb_text)
            and _rgb_confs
            and 4 <= len(_rgb_digits) <= 5
        )
        # ``_rgb_strict`` was originally meant as a fallback for runs
        # where the inverse-polarity pipeline didn't produce results
        # (no CNN crops, classifier unavailable). When INV DID produce
        # a competing 4-5 digit string that disagrees with primary,
        # accepting primary alone at min-conf ≥0.85 is unsafe — the
        # CNN is confidently wrong on mis-segmented crops (e.g. the
        # segmenter splits "6,400" into 5 wrong slices and the CNN
        # confidently classifies each slice). Restrict strict mode to
        # cases where INV produced no competing output.
        _rgb_inv_unavailable = (
            not _rgb_inv_text or not _rgb_inv_confs
        )
        _rgb_strict = (
            _rgb_chars_ok
            and min(_rgb_confs) >= 0.85
            and _rgb_inv_unavailable
        )
        _rgb_dual_agree = (
            _rgb_chars_ok
            and _rgb_text == _rgb_inv_text
            and _rgb_inv_confs
            and (sum(_rgb_confs) / len(_rgb_confs)) >= 0.70
            and (sum(_rgb_inv_confs) / len(_rgb_inv_confs)) >= 0.70
        )
        if _rgb_strict or _rgb_dual_agree:
            try:
                _rgb_val = int(_rgb_digits)
            except ValueError:
                _rgb_val = None
            # When the value isn't a known mining signature, both
            # polarities of the CNN can still agree on a hallucination
            # (e.g. segmenter splits a low-quality crop into 4 chunks
            # the CNN classifies '7777' identically across polarities).
            # If the chart-values table is loaded, refuse to consume an
            # RGB-primary read that isn't in it — fall through to
            # Tesseract / consensus instead. Empty table = no preference
            # applied (cold start before chart load).
            if (
                _rgb_val is not None
                and _KNOWN_SIGNAL_VALUES
                and _rgb_val not in _KNOWN_SIGNAL_VALUES
            ):
                log.info(
                    "sc_ocr.signal: RGB primary %r (mean=%.2f) NOT in "
                    "known-values table — falling through to other voters",
                    _rgb_text, sum(_rgb_confs) / len(_rgb_confs),
                )
                _rgb_val = None
            if _rgb_val is not None and 1000 <= _rgb_val <= 35000:
                _rgb_gate = "rgb-strict" if _rgb_strict else "rgb-dual-agree"
                _rgb_mean = sum(_rgb_confs) / len(_rgb_confs)
                log.info(
                    "sc_ocr.signal: RGB PRIMARY text=%r mean=%.2f "
                    "gate=%s → %d (skipping gray CNN + CRNN + Tesseract)",
                    _rgb_text, _rgb_mean, _rgb_gate, _rgb_val,
                )
                try:
                    _dump_signature_winner(
                        raw_text=_rgb_text,
                        digits=_rgb_digits,
                        mean_conf=_rgb_mean,
                        validated_value=_rgb_val,
                        rejection_reason=None,
                        per_digit_classifications=None,
                    )
                    _clear_viewer_entry("signature", "crnn")
                    _clear_viewer_entry("signature", "cnn")
                except Exception as _rgb_winner_exc:
                    log.debug(
                        "sc_ocr.signal: RGB-primary winner dump failed: %s",
                        _rgb_winner_exc,
                    )
                return _accept_signal_value(_rgb_val, _rgb_gate)

    # ── (2) CRNN-SECONDARY gate ──
    # Falls to here when RGB primary failed (low conf, polarity
    # disagree, or RGB classifier unavailable). Run CRNN on the value
    # crop; if it produces a 4-5 digit string at mean conf ≥ 0.85
    # within range, take it. The full CRNN block further below still
    # runs as part of the BACKUP path with its more elaborate CNN
    # cross-check, but that's only consulted if both this gate AND
    # the gray gate fail.
    try:
        _crnn_sec_out = _crnn_recognize(base, digit_only=True)
    except Exception as _crnn_sec_exc:
        log.debug(
            "sc_ocr.signal: CRNN secondary attempt failed: %s",
            _crnn_sec_exc,
        )
        _crnn_sec_out = None
    if _crnn_sec_out is not None and _crnn_sec_out[0]:
        _crnn_sec_text = _crnn_sec_out[0]
        _crnn_sec_confs = _crnn_sec_out[1]
        _crnn_sec_digits = "".join(c for c in _crnn_sec_text if c.isdigit())
        _crnn_sec_mean = (
            sum(_crnn_sec_confs) / len(_crnn_sec_confs)
            if _crnn_sec_confs else 0.0
        )
        if (
            4 <= len(_crnn_sec_digits) <= 5
            and _crnn_sec_mean >= 0.85
        ):
            try:
                _crnn_sec_val = int(_crnn_sec_digits)
            except ValueError:
                _crnn_sec_val = None
            # Same table-membership safety net as the other primary
            # gates — a 0.85+ confident CRNN read is not enough on
            # its own when the segmenter is feeding bad crops; require
            # the value to be in the chart when the chart is loaded.
            if (
                _crnn_sec_val is not None
                and _KNOWN_SIGNAL_VALUES
                and _crnn_sec_val not in _KNOWN_SIGNAL_VALUES
            ):
                log.info(
                    "sc_ocr.signal: CRNN SECONDARY %r (mean=%.2f) NOT "
                    "in known-values table — falling through to other "
                    "voters",
                    _crnn_sec_text, _crnn_sec_mean,
                )
                _crnn_sec_val = None
            if _crnn_sec_val is not None and 1000 <= _crnn_sec_val <= 35000:
                log.info(
                    "sc_ocr.signal: CRNN SECONDARY text=%r mean=%.2f "
                    "→ %d (skipping gray CNN + Tesseract)",
                    _crnn_sec_text, _crnn_sec_mean, _crnn_sec_val,
                )
                try:
                    _dump_signature_winner(
                        raw_text=_crnn_sec_text,
                        digits=_crnn_sec_digits,
                        mean_conf=_crnn_sec_mean,
                        validated_value=_crnn_sec_val,
                        rejection_reason=None,
                        per_digit_classifications=None,
                    )
                    _clear_viewer_entry("signature", "cnn")
                except Exception as _crnn_sec_winner_exc:
                    log.debug(
                        "sc_ocr.signal: CRNN-secondary winner dump "
                        "failed: %s", _crnn_sec_winner_exc,
                    )
                return _accept_signal_value(_crnn_sec_val, "crnn-secondary")

    # ── (3) GRAY CNN BACKUP gate ──
    # Existing gray-CNN strict + dual-agree gate, demoted from
    # primary to backup. Runs only when both RGB primary and CRNN
    # secondary failed to produce a confident validated read.
    if _hud_pri_results:
        _hud_pri_text = "".join(c for c, _ in _hud_pri_results)
        _hud_pri_confs = [c for _, c in _hud_pri_results]
        _hud_sec_text = "".join(c for c, _ in _hud_sec_results)
        _hud_sec_confs = [c for _, c in _hud_sec_results]
        # Signature values are pure integers — strip any '.' / '%' /
        # other non-digits the HUD CNN may have classified (the model
        # was trained on '0-9.%' alphabet; on a digit-only crop it can
        # still misfire as '%' on a dense glyph).
        _hud_pri_digits = "".join(c for c in _hud_pri_text if c.isdigit())
        _hud_chars_ok = (
            _hud_pri_text
            and all(c.isdigit() for c in _hud_pri_text)
            and _hud_pri_confs
            and 4 <= len(_hud_pri_digits) <= 5
        )
        _hud_strict_pass = (
            _hud_chars_ok and min(_hud_pri_confs) >= 0.85
        )
        _hud_dual_agree_pass = (
            _hud_chars_ok
            and _hud_pri_text == _hud_sec_text
            and _hud_sec_confs
            and all(c.isdigit() for c in _hud_sec_text)
            and (sum(_hud_pri_confs) / len(_hud_pri_confs)) >= 0.70
            and (sum(_hud_sec_confs) / len(_hud_sec_confs)) >= 0.70
        )
        if _hud_strict_pass or _hud_dual_agree_pass:
            try:
                _hud_val = int(_hud_pri_digits)
            except ValueError:
                _hud_val = None
            # Same table-membership safety net as the RGB primary gate.
            # The HUD-style strict/dual-agree paths can both be high-
            # confidence but wrong when the segmenter feeds a clipped
            # or hallucinated crop (e.g. "11,700" → '1676' at 0.98).
            # If a chart-values table is loaded and the read isn't in
            # it, fall through to Tesseract / consensus instead of
            # locking it as stable.
            if (
                _hud_val is not None
                and _KNOWN_SIGNAL_VALUES
                and _hud_val not in _KNOWN_SIGNAL_VALUES
            ):
                log.info(
                    "sc_ocr.signal: HUD-style PRIMARY %r (mean=%.2f) "
                    "NOT in known-values table — falling through to "
                    "other voters",
                    _hud_pri_text,
                    sum(_hud_pri_confs) / len(_hud_pri_confs),
                )
                _hud_val = None
            if _hud_val is not None and 1000 <= _hud_val <= 35000:
                _hud_gate = "strict" if _hud_strict_pass else "dual-agree"
                _hud_mean = sum(_hud_pri_confs) / len(_hud_pri_confs)
                log.info(
                    "sc_ocr.signal: HUD-style PRIMARY text=%r mean=%.2f "
                    "gate=%s → %d (skipping CRNN+Tesseract)",
                    _hud_pri_text, _hud_mean, _hud_gate, _hud_val,
                )
                # Surface the winner in the live viewer; clear stale
                # CRNN/CNN entries from a prior scan so the panel
                # doesn't show contradictory reads.
                try:
                    _dump_signature_winner(
                        raw_text=_hud_pri_text,
                        digits=_hud_pri_digits,
                        mean_conf=_hud_mean,
                        validated_value=_hud_val,
                        rejection_reason=None,
                        per_digit_classifications=None,
                    )
                    _clear_viewer_entry("signature", "crnn")
                    _clear_viewer_entry("signature", "cnn")
                except Exception as _hud_viewer_exc:
                    log.debug(
                        "sc_ocr.signal: HUD-path viewer dump failed: %s",
                        _hud_viewer_exc,
                    )
                # Display-stability filter, same shape as the CRNN /
                # Tesseract paths below so successive scans don't flicker
                # on a one-frame disagreement.
                global _STABLE_SIGNAL
                _RECENT_SIGNAL_READS.append(_hud_val)
                if _STABLE_SIGNAL is None:
                    _STABLE_SIGNAL = _hud_val
                elif _hud_val != _STABLE_SIGNAL:
                    _recent = list(_RECENT_SIGNAL_READS)[-_SIGNAL_AGREEMENT_REQ:]
                    if (
                        len(_recent) >= _SIGNAL_AGREEMENT_REQ
                        and all(r == _hud_val for r in _recent)
                    ):
                        log.info(
                            "sc_ocr.signal: stable swap %d → %d "
                            "(HUD-style consensus)",
                            _STABLE_SIGNAL, _hud_val,
                        )
                        _STABLE_SIGNAL = _hud_val
                return _STABLE_SIGNAL

    # ── CRNN primary read (~30 ms) ──
    # Same trick that fixed mineral_name: try the CRNN end-to-end on
    # the digit cluster first. The CRNN was trained on SC Datarunner
    # digit + alphabet sequences and runs ~100x faster than the 9-call
    # Tesseract variant matrix below. Comma is NOT in its alphabet, so
    # "7,680" comes out as "7680" — exactly what the integer parser
    # wants. If CRNN's mean confidence is high enough AND the parsed
    # value is in range, we return immediately and skip Tesseract.
    #
    # ``digit_only=True`` masks every letter class out of the CRNN's
    # logits before argmax. Without it the model — which shares
    # weights with the mineral-name reader and therefore knows the
    # full alphabet — hallucinates letter-mixed strings (e.g.
    # ``'HIP20'`` for star-system overlays, ``'Cu'`` for noise) on
    # borderline crops, breaking the 4-5-digit gate downstream and
    # producing empty bubbles.
    try:
        _crnn_out = _crnn_recognize(base, digit_only=True)
    except Exception as _crnn_exc:
        log.debug("sc_ocr.signal: CRNN attempt failed: %s", _crnn_exc)
        _crnn_out = None
    if _crnn_out is not None and _crnn_out[0]:
        _crnn_text = _crnn_out[0]
        _crnn_confs = _crnn_out[1]
        # Strip any non-digit characters (CRNN sometimes hallucinates
        # the comma as '.' or a stray letter on noisy inputs).
        _crnn_digits = "".join(c for c in _crnn_text if c.isdigit())
        _crnn_mean = (
            sum(_crnn_confs) / len(_crnn_confs) if _crnn_confs else 0.0
        )
        # Diagnostic: log every CRNN attempt so we can see WHY it's
        # not locking when it isn't (vs. silent fall-through to
        # Tesseract). Demote to DEBUG once the path is proven stable.
        log.info(
            "sc_ocr.signal: CRNN raw text=%r digits=%r mean=%.2f",
            _crnn_text, _crnn_digits, _crnn_mean,
        )
        # ── Diagnostic emit for the live glyph reader (signature row) ──
        # Mirrors the HUD per-field voter dumps so the user can SEE
        # exactly what the signal CRNN is being fed and what it's
        # returning. Purely additive — must NEVER affect OCR control
        # flow. Failures are swallowed at log.debug.
        # ── CRNN/CNN agreement gate ──
        # Run the dual-polarity per-digit signal CNN on the SAME work
        # crop CRNN saw, then digit-by-digit compare. The CNN was
        # trained on thousands of user-collected glyphs; when it
        # disagrees with CRNN on any digit, fall through to the
        # Tesseract ensemble below instead of locking in CRNN's read.
        # When the CNN bails (no model, polarity disagree, segmenter
        # missed) we conservatively keep CRNN's behavior unchanged.
        _cnn_result: "Optional[tuple[str, list[float], list[np.ndarray]]]" = None
        try:
            _cnn_result = _signal_cnn_per_digit(work)
        except Exception as _cnn_gate_exc:
            log.debug(
                "sc_ocr.signal: per-digit CNN gate swallowed: %s",
                _cnn_gate_exc,
            )
            _cnn_result = None
        _cnn_digits = _cnn_result[0] if _cnn_result is not None else None
        _cnn_confs = _cnn_result[1] if _cnn_result is not None else []
        _cnn_crops = _cnn_result[2] if _cnn_result is not None else []

        # NOTE: signature_primary / signature_secondary glyph dumps now
        # live in the HUD-style block ABOVE this CRNN section — running
        # them here would clobber the entries that block already wrote
        # (both call sites key on the same `fields["signature_primary"]`
        # / `signature_secondary` slots). The HUD-style block uses
        # `_classify_crops` / `_classify_crops_inv` directly on
        # `_segment_glyphs` output, which is exactly what the live
        # viewer wants to render as per-glyph tiles. The CRNN flow's
        # `_signal_cnn_per_digit` cross-check still runs to gate the
        # CRNN read — it just doesn't dump tiles any more.

        # Pre-compute the gate's verdict so the viewer can show
        # what the bubble would display vs. why it was rejected.
        _sig_validated: "int | None" = None
        _sig_reject: "str | None" = None
        if not (4 <= len(_crnn_digits) <= 5):
            _sig_reject = f"not 4-5 digits ({len(_crnn_digits)})"
        else:
            try:
                _sig_try_val = int(_crnn_digits)
            except ValueError:
                _sig_try_val = None
            if _sig_try_val is None:
                _sig_reject = "non-integer digits"
            elif not (1000 <= _sig_try_val <= 35000):
                _sig_reject = f"out of range: {_sig_try_val}"
            elif _crnn_mean < 0.70:
                _sig_reject = f"low confidence: {_crnn_mean:.2f}"
            else:
                _sig_validated = _sig_try_val

        # CRNN passed the gate — now check CNN. The agreement rule is
        # strict digit-by-digit equality (length match required;
        # commas already stripped from both — CRNN had non-digits
        # filtered, CNN's segmenter never produces a comma component).
        _crnn_cnn_agree: "bool | None" = None
        if (
            _sig_validated is not None
            and _cnn_digits is not None
            and len(_cnn_digits) == len(_crnn_digits)
            and _cnn_digits == _crnn_digits
        ):
            _crnn_cnn_agree = True
        elif _sig_validated is not None and _cnn_digits is not None:
            _crnn_cnn_agree = False

        # Build per-digit classification metadata for the live viewer.
        # Crops are written under ``signature_primary_<i>.png`` so the
        # viewer can render tiles. Best-effort: any IO failure here is
        # purely diagnostic.
        _per_digit_meta: "list[dict] | None" = None
        try:
            _dump_value_crop("signature", base)
            _dump_voter("signature", "crnn", _crnn_text, _crnn_mean)
            if _cnn_result is not None:
                from . import debug_overlay as _dbg_gate_inline
                if _dbg_gate_inline.diagnostics_active():
                    from PIL import Image as _Image_inline
                    os.makedirs(_GLYPH_DUMP_DIR, exist_ok=True)
                    _per_digit_meta = []
                    with _glyph_dump_lock:
                        for _i, (_ch, _conf, _crop) in enumerate(zip(
                            _cnn_digits or "", _cnn_confs, _cnn_crops,
                        )):
                            _fname = f"signature_primary_{_i}.png"
                            _fpath = os.path.join(_GLYPH_DUMP_DIR, _fname)
                            try:
                                _arr = _crop.astype(np.uint8) \
                                    if _crop.dtype != np.uint8 \
                                    else _crop
                                _tmp = _fpath + ".tmp"
                                _Image_inline.fromarray(
                                    _arr, mode="L",
                                ).save(_tmp, format="PNG")
                                os.replace(_tmp, _fpath)
                            except Exception:
                                continue
                            _per_digit_meta.append({
                                "char": _ch,
                                "confidence": float(_conf),
                                "crop_path": _fname,
                            })
                # Voter dump for the CNN side mirrors the CRNN one so
                # the viewer can show ``signature_cnn`` alongside
                # ``signature_crnn`` in the same panel.
                _cnn_mean = (
                    sum(_cnn_confs) / len(_cnn_confs)
                    if _cnn_confs else 0.0
                )
                _dump_voter(
                    "signature", "cnn",
                    _cnn_digits or "", _cnn_mean,
                )
            _dump_signature_winner(
                raw_text=_crnn_text, digits=_crnn_digits,
                mean_conf=_crnn_mean, validated_value=_sig_validated,
                rejection_reason=_sig_reject,
                per_digit_classifications=_per_digit_meta,
            )
        except Exception as _sig_diag_exc:
            log.debug(
                "sc_ocr.signal: signature diagnostic emit swallowed: %s",
                _sig_diag_exc,
            )

        if 4 <= len(_crnn_digits) <= 5:
            try:
                _crnn_val = int(_crnn_digits)
            except ValueError:
                _crnn_val = None
            if (
                _crnn_val is not None
                and 1000 <= _crnn_val <= 35000
                and _crnn_mean >= 0.70
            ):
                # Cross-check verdict. Only block CRNN's early-return
                # when the CNN actually ran AND disagreed digit-by-
                # digit. CNN-bailed (None) → conservative: keep CRNN.
                if _crnn_cnn_agree is False:
                    log.info(
                        "sc_ocr.signal: CRNN/CNN disagree crnn=%r "
                        "cnn=%r — falling through to Tesseract",
                        _crnn_digits, _cnn_digits,
                    )
                    # Don't return — drop into the Tesseract ensemble
                    # below. Don't update _STABLE_SIGNAL on this frame.
                else:
                    log.info(
                        "sc_ocr.signal: CRNN primary text=%r digits=%r → %d "
                        "mean=%.2f (cnn=%s, skipping Tesseract)",
                        _crnn_text, _crnn_digits, _crnn_val, _crnn_mean,
                        "agree" if _crnn_cnn_agree else "no-check",
                    )
                    # Apply the same display-stability filter that the
                    # Tesseract path uses below so successive scans don't
                    # flicker on a one-frame disagreement. (``global
                    # _STABLE_SIGNAL`` is already declared up in the
                    # HUD-style primary block — Python requires the
                    # declaration to precede the first use, so we don't
                    # repeat it here.)
                    _RECENT_SIGNAL_READS.append(_crnn_val)
                    if _STABLE_SIGNAL is None:
                        _STABLE_SIGNAL = _crnn_val
                    elif _crnn_val != _STABLE_SIGNAL:
                        _recent = list(_RECENT_SIGNAL_READS)[-_SIGNAL_AGREEMENT_REQ:]
                        if (
                            len(_recent) >= _SIGNAL_AGREEMENT_REQ
                            and all(r == _crnn_val for r in _recent)
                        ):
                            log.info(
                                "sc_ocr.signal: stable swap %d → %d (CRNN consensus)",
                                _STABLE_SIGNAL, _crnn_val,
                            )
                            _STABLE_SIGNAL = _crnn_val
                    return _STABLE_SIGNAL

    # ── Tesseract ensemble (multi-PSM/scale) — VOTE across variants ──
    # Reached only when CRNN didn't lock above. Trimmed from 3×3=9
    # calls to 2×2=4 calls — the marginal accuracy from PSM 13 + 1x
    # scale was small and the wall-clock cost is real. Each remaining
    # call still costs ~300 ms on Windows, but 4 calls = ~1.2 s
    # instead of ~2.7 s.
    variants = [
        (base.resize((base.width * 2, base.height * 2), _PILImage.LANCZOS), "2x", 2),
        (base.resize((base.width * 3, base.height * 3), _PILImage.LANCZOS), "3x", 3),
    ]
    # Each entry: (value, text, boxes, tag, scale)
    candidates: list[tuple[int, str, list, str, int]] = []
    for psm in ("7", "8"):
        for img_v, tag, scale in variants:
            try:
                boxes = _xlg._tesseract_char_boxes(
                    img_v, whitelist="0123456789.", psm=psm,
                )
            except Exception:
                continue
            if not boxes:
                continue
            text = "".join(b[0] for b in boxes if b[0].isdigit())
            if not (4 <= len(text) <= 5):
                continue
            try:
                v = int(text)
            except ValueError:
                continue
            if not (1000 <= v <= 35000):
                continue
            digit_boxes = [b for b in boxes if b[0].isdigit()]
            candidates.append((v, text, digit_boxes, f"{tag}/psm{psm}", scale))

    if not candidates:
        log.debug("sc_ocr.signal: no Tesseract variant produced 4-5 digits in range")
        local_value = _signal_read_tight_local(gray)
        if local_value is not None:
            return _signal_accept_candidate(local_value, "local")
        return None

    # Vote. Two-tier scoring:
    #   1. Among variants whose value EXACT-matches a known signature
    #      table entry, take the most common. Known-table hits are
    #      strong evidence the read is correct (the table is finite
    #      and the truth IS one of those values).
    #   2. If no variant matches the table (table empty, or rock value
    #      outside the chart), fall back to plain majority vote across
    #      ALL in-range variants.
    from collections import Counter
    table_hits = [c for c in candidates if c[0] in _KNOWN_SIGNAL_VALUES]
    if table_hits:
        counts = Counter(c[0] for c in table_hits)
        winner_val, winner_count = counts.most_common(1)[0]
        winner = next(c for c in table_hits if c[0] == winner_val)
        vote_strength = f"{winner_count}/{len(candidates)} table-match"
    else:
        counts = Counter(c[0] for c in candidates)
        winner_val, winner_count = counts.most_common(1)[0]
        winner = next(c for c in candidates if c[0] == winner_val)
        vote_strength = f"{winner_count}/{len(candidates)} majority"

    tess_val = winner[0]
    tess_text = winner[1]
    tess_boxes_used = winner[2]
    tess_tag = winner[3]
    tess_scale = winner[4]

    # ── Dual-polarity CNN promoted to PRIMARY classifier ──
    # The original comment justifying "Tesseract primary, CNN
    # informational" was based on a single-CNN setup where the model
    # had no peer to disagree with on noisy inputs. The dual-polarity
    # voter in ``_signal_cnn_at_tess_boxes`` returns None when the
    # original-polarity signal CNN and the inverted HUD CNN disagree
    # on ANY digit — which is the SC font's classic 5-vs-6 ambiguity
    # case. So a non-None CNN result now carries strong evidence:
    # both polarities AND both architectures agreed on every digit.
    #
    # Use that as the value when it parses to a valid integer in
    # range. Tesseract's vote stays the fallback for the case where
    # CNN bailed (polarity disagreement, OOB index, inverted model
    # missing, etc.).
    chosen_text = tess_text
    chosen_via = f"tess/{tess_tag}"
    try:
        cnn_text = _signal_cnn_at_tess_boxes(
            work, tess_boxes_used, tess_scale,
        )
    except Exception as _cnn_exc:
        log.debug(
            "sc_ocr.signal: CNN classification failed: %s", _cnn_exc,
        )
        cnn_text = None
    if cnn_text is not None and 4 <= len(cnn_text) <= 5:
        try:
            cnn_val = int(cnn_text)
        except ValueError:
            cnn_val = None
        if cnn_val is not None and 1000 <= cnn_val <= 35000:
            if cnn_text != tess_text:
                # CNN and Tesseract disagree. The dual-polarity voter's
                # premise — "both polarities can't independently make
                # the same mistake" — only holds when the underlying
                # crop is well-formed. At unusual render scales the
                # segmenter can hand the CNN bogus crops (clipped
                # digits, fragments of panel chrome) and BOTH
                # polarities then classify them the same wrong way.
                # The errors are correlated, not independent.
                #
                # Tiebreak via the known-signature-values table: when
                # Tesseract reports a value the chart actually
                # contains and CNN reports a value it doesn't, prefer
                # Tesseract. If both or neither are in the table, the
                # original "trust CNN" rule still wins.
                tess_in_table = tess_val in _KNOWN_SIGNAL_VALUES
                cnn_in_table = cnn_val in _KNOWN_SIGNAL_VALUES
                if (
                    _KNOWN_SIGNAL_VALUES
                    and tess_in_table
                    and not cnn_in_table
                ):
                    log.info(
                        "sc_ocr.signal: keeping Tesseract %r (in known-"
                        "values table) over CNN %r (not in table)",
                        tess_text, cnn_text,
                    )
                    chosen_via = f"tess/{tess_tag}"
                else:
                    log.info(
                        "sc_ocr.signal: dual-polarity CNN overrides "
                        "Tesseract (cnn=%r tess=%r, tess_in_table=%s "
                        "cnn_in_table=%s)",
                        cnn_text, tess_text,
                        tess_in_table, cnn_in_table,
                    )
                    tess_val = cnn_val
                    chosen_text = cnn_text
                    chosen_via = "cnn-dual"
            else:
                chosen_via = "cnn-dual"
    log.debug(
        "sc_ocr.signal: chose=%r via=%s (tess=%r cnn=%r)",
        chosen_text, chosen_via, tess_text, cnn_text,
    )

    # ── Display-level stabilisation ─────────────────────────────
    # Even after voting, consecutive frames can swing between two
    # plausible-looking readings on heavy HUD jitter (e.g. when the
    # icon anchor briefly slides ±1 px and re-cuts the leading digit).
    # We hold the LAST DISPLAYED value steady until the buffer shows
    # _SIGNAL_AGREEMENT_REQ consecutive reads of a NEW value. This
    # turns a 17,020 → 17,011 → 17,020 single-frame blip into a
    # silent no-op at the display layer.
    # (``global _STABLE_SIGNAL`` already declared in the CRNN
    # primary-read block above.)
    _RECENT_SIGNAL_READS.append(tess_val)
    if _STABLE_SIGNAL is None:
        # First read of a fresh buffer — show it immediately.
        _STABLE_SIGNAL = tess_val
    elif tess_val != _STABLE_SIGNAL:
        # Candidate new value. Default rule: swap when the last
        # _SIGNAL_AGREEMENT_REQ reads all agree on the new value.
        # Stickiness rule: when the current stable IS a known
        # signature value and the new candidate is NOT, require a
        # longer agreement window before swapping. Tesseract at
        # awkward render scales intermittently drops a leading digit
        # (e.g. "11,700" → "1700") and stabilises on the truncated
        # value over multiple ticks; without this stickiness the
        # truncated read wins after just 3 consecutive frames and the
        # display flips to a wrong but plausible-looking number.
        # Empty known-values table ⇒ no stickiness applied (cold start
        # fail-open, original behavior preserved).
        stable_in_table = (
            _KNOWN_SIGNAL_VALUES
            and _STABLE_SIGNAL in _KNOWN_SIGNAL_VALUES
        )
        cand_in_table = (
            _KNOWN_SIGNAL_VALUES
            and tess_val in _KNOWN_SIGNAL_VALUES
        )
        if stable_in_table and not cand_in_table:
            required = _SIGNAL_STICKY_AGREEMENT_REQ
        else:
            required = _SIGNAL_AGREEMENT_REQ
        recent = list(_RECENT_SIGNAL_READS)[-required:]
        if (
            len(recent) >= required
            and all(r == tess_val for r in recent)
        ):
            log.info(
                "sc_ocr.signal: stable swap %d → %d (consensus %d-of-%d, "
                "stable_in_table=%s cand_in_table=%s)",
                _STABLE_SIGNAL, tess_val, required,
                len(_RECENT_SIGNAL_READS),
                stable_in_table, cand_in_table,
            )
            _STABLE_SIGNAL = tess_val
        else:
            log.debug(
                "sc_ocr.signal: outlier %d (stable=%d, vote=%s, "
                "need %d consecutive) — holding stable value",
                tess_val, _STABLE_SIGNAL, vote_strength, required,
            )

    log.info(
        "sc_ocr.signal: vote %d via %s (%s) → display %d",
        tess_val, tess_tag, vote_strength, _STABLE_SIGNAL,
    )
    return _STABLE_SIGNAL


# ── Lazy CNN session for the signal-region model ──
_signal_session = None
_signal_session_path = ""
_signal_classes = "0123456789"


def _signal_cnn_at_tess_boxes(
    gray_work: np.ndarray,
    tess_boxes: list,
    scale: int,
) -> Optional[str]:
    """Run the trained signal CNN on the per-character bounding boxes
    Tesseract reported. Tesseract finds the spatial positions; the
    CNN does the digit identification. If they round-trip to the
    same string, both engines agree.

    Returns the predicted string or None on failure."""
    try:
        import onnxruntime as _ort
        from . import training_registry as _tr  # type: ignore
    except Exception:
        try:
            from .. import training_registry as _tr  # type: ignore
        except Exception as exc:
            log.debug("api: _signal_cnn_at_tess_boxes swallowed: %s", exc)
            return None
    try:
        from ocr import training_registry as _tr  # type: ignore
    except Exception as exc:
        log.debug("api: _signal_cnn_at_tess_boxes swallowed: %s", exc)
    model_path = _tr.get_model_path("signal")
    if not model_path.is_file():
        return None
    global _signal_session, _signal_session_path, _signal_classes
    try:
        if _signal_session is None or _signal_session_path != str(model_path):
            # Cap thread count — see onnx_hud_reader._load_session
            # for the rationale. Without this, ONNX defaults to one
            # thread per CPU core and starves the Qt GUI thread.
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = 1
            _opts.inter_op_num_threads = 1
            _signal_session = _ort.InferenceSession(
                str(model_path),
                sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            _signal_session_path = str(model_path)
            try:
                import json as _json
                meta = _json.loads(
                    model_path.with_suffix(".json").read_text(encoding="utf-8")
                )
                _signal_classes = meta.get("charClasses", "0123456789")
            except Exception:
                _signal_classes = "0123456789"
    except Exception as exc:
        log.debug("sc_ocr.signal: CNN session load failed: %s", exc)
        return None

    # Reuse the offline extractor's glyph-rendering helper so the CNN
    # sees inputs shaped EXACTLY like its training data.
    try:
        import sys
        from pathlib import Path as _Path
        _scripts = _Path(__file__).resolve().parent.parent.parent / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))
        import extract_labeled_glyphs as _xlg  # type: ignore
    except Exception as exc:
        log.debug("api: _signal_cnn_at_tess_boxes swallowed: %s", exc)
        return None

    # Convert Tesseract's bounding boxes back to original `gray_work`
    # coordinates and resolve overlapping boxes via the midpoint trick
    # (same as the training pipeline).
    raw_spans = []
    for b in tess_boxes:
        if not b[0].isdigit():
            continue
        x1 = b[1] // scale
        x2 = b[3] // scale
        if x2 > x1:
            raw_spans.append([x1, x2])
    for i in range(len(raw_spans)):
        if i + 1 < len(raw_spans):
            cur_x1, cur_x2 = raw_spans[i]
            nxt_x1, nxt_x2 = raw_spans[i+1]
            if nxt_x1 < cur_x2:
                cur_c = (cur_x1 + cur_x2) / 2.0
                nxt_c = (nxt_x1 + nxt_x2) / 2.0
                if nxt_c > cur_c:
                    boundary = int((cur_c + nxt_c) / 2.0)
                    raw_spans[i][1] = boundary
                    raw_spans[i+1][0] = boundary
    # ── Dual-polarity voter ──
    # Mirrors ``_ocr_value_crop``'s primary+secondary CNN pattern from
    # the HUD digit pipeline. The signal CNN was trained on bright-
    # text-on-dark crops; we ALSO run the HUD's polarity-INVERTED
    # CNN (``model_cnn_inv.onnx``, trained on the same crops with
    # pixel inversion) on the inverted glyph. The two models share
    # zero weights, so their errors decorrelate strongly — when one
    # misclassifies a 5 as 6, the other almost always catches it.
    #
    # The HUD inverted CNN's char-classes include digits + ``.-%``;
    # we mask everything except 0-9 since signal values are pure
    # integers with no decimal.
    inv_available = False
    try:
        if fallback._ensure_model_inv() and fallback._session_inv is not None:
            inv_available = True
    except Exception as _inv_exc:
        log.debug("sc_ocr.signal: inv-CNN load failed: %s", _inv_exc)

    digits: list[str] = []
    for x1, x2 in raw_spans:
        if x2 - x1 < 3:
            return None
        g = _xlg._glyph_to_28x28(gray_work, x1, x2)
        if g is None:
            return None
        x = (g.astype(np.float32) / 255.0)[None, None, :, :]
        try:
            out = _signal_session.run(None, {"input": x})[0]
        except Exception as exc:
            log.debug("api: _signal_cnn_at_tess_boxes swallowed: %s", exc)
            return None
        idx_pri = int(np.argmax(out, axis=1)[0])
        if not (0 <= idx_pri < len(_signal_classes)):
            return None

        # Secondary voter: HUD inverted CNN on the inverted glyph.
        # When both agree we lock in. When they disagree, return None
        # to defer to Tesseract — this is the path that prevents
        # 5-vs-6 single-classifier ambiguity from flipping the
        # stable signal value.
        if inv_available:
            try:
                g_inv = (255 - g.astype(np.int16)).clip(0, 255).astype(np.uint8)
                x_inv = (g_inv.astype(np.float32) / 255.0)[None, None, :, :]
                inv_input_name = (
                    fallback._session_inv.get_inputs()[0].name
                )
                out_inv = fallback._session_inv.run(
                    None, {inv_input_name: x_inv},
                )[0]
                # Mask to digit classes only (HUD's char_classes are
                # ``0-9.-%``; first 10 are digits).
                inv_logits = out_inv[0]
                if len(inv_logits) >= 10:
                    digit_logits = inv_logits[:10]
                    idx_sec = int(np.argmax(digit_logits))
                    if idx_pri != idx_sec:
                        # Polarity vote disagreement — let the caller
                        # fall back to Tesseract's classification for
                        # this scan instead of trusting either CNN.
                        log.debug(
                            "sc_ocr.signal: dual-polarity disagree "
                            "pri=%d sec=%d at glyph (x=%d-%d) — "
                            "deferring",
                            idx_pri, idx_sec, x1, x2,
                        )
                        return None
            except Exception as _sec_exc:
                # Inverted CNN failed mid-glyph — gracefully degrade
                # to single-CNN behaviour for this scan.
                log.debug(
                    "sc_ocr.signal: inv-CNN inference failed: %s",
                    _sec_exc,
                )
        digits.append(_signal_classes[idx_pri])
    return "".join(digits) if digits else None


# ── Per-frame latency guard for the Tesseract-free CNN cross-check ──
# When ``_signal_cnn_per_digit`` exceeds the budget, this flag is
# flipped for the rest of the scan tick so the gate can no-op cheaply.
# Reset at the top of every ``_signal_recognize_pil`` invocation.
_SIGNAL_CNN_BUDGET_S = 0.200  # 200 ms — the user notices >150 ms steadily
_signal_cnn_skip_for_tick: bool = False


def _signal_cnn_per_digit(
    work_gray: np.ndarray,
) -> "Optional[tuple[str, list[float], list[np.ndarray]]]":
    """Tesseract-free per-digit CNN read of a signal-region work crop.

    Mirrors :func:`_signal_cnn_at_tess_boxes` for everything *after*
    span resolution, but does its own segmentation via
    ``extract_labeled_glyphs._segment_digits`` (column-projection /
    width-equalising splitter). Lets the CRNN primary path obtain a
    second opinion *without* paying the ~1.2 s cost of a full Tesseract
    ensemble.

    Parameters
    ----------
    work_gray : (H, W) uint8 ndarray
        The post-row-isolation grayscale digit cluster — the same array
        ``_signal_recognize_pil`` feeds to CRNN. The segmenter expects
        the comma to already be absent; ``_isolate_main_row`` keeps
        only the dominant text band so commas in the SC font do segment
        as a separate sub-MIN_SPLIT_WIDTH span and drop out below.

    Returns
    -------
    None when ANY of:
      * ``model_signal_cnn.onnx`` missing or session load failed
      * the inverted-polarity HUD CNN failed to load (we require both
        polarities to vote — single-CNN doesn't carry enough evidence
        to over-rule CRNN)
      * the segmenter found <4 or >5 digit-shaped spans
      * any per-digit dual-polarity vote disagreed (mirrors
        ``_signal_cnn_at_tess_boxes``'s polarity-disagree → bail)
      * any glyph crop failed to normalize to 28×28
      * the wall-clock budget overran (sets the tick-skip flag)

    Returns ``(digits, per_digit_confs, per_digit_crops)`` on success:
      * ``digits``: ``"17020"``-style string of 4–5 chars, no comma
      * ``per_digit_confs``: float list of softmax-max-of-primary, same
        length as ``digits``
      * ``per_digit_crops``: list of uint8 28×28 ndarrays (the same
        shape as the training data) — useful for the diagnostic dump.

    Best-effort: any unexpected exception logs at DEBUG and returns
    None. MUST NEVER raise to the caller.
    """
    global _signal_cnn_skip_for_tick
    if _signal_cnn_skip_for_tick:
        return None
    if work_gray is None or not isinstance(work_gray, np.ndarray):
        return None
    if work_gray.ndim != 2 or work_gray.shape[0] < 6 or work_gray.shape[1] < 12:
        return None

    _t0 = time.monotonic()
    try:
        # Reuse the same shared modules / sessions as the Tesseract path.
        try:
            import onnxruntime as _ort  # noqa: F401
        except Exception as exc:
            log.debug(
                "sc_ocr.signal: per-digit CNN onnxruntime unavailable: %s",
                exc,
            )
            return None
        try:
            from . import training_registry as _tr  # type: ignore
        except Exception:
            try:
                from .. import training_registry as _tr  # type: ignore
            except Exception as exc:
                log.debug(
                    "sc_ocr.signal: per-digit CNN registry import failed: %s",
                    exc,
                )
                return None

        model_path = _tr.get_model_path("signal")
        if not model_path.is_file():
            # No trained signal CNN on this install — the agreement
            # gate will see None and conservatively keep CRNN's read.
            return None

        # Lazy-load the same _signal_session the Tesseract path uses;
        # the load logic is intentionally identical so the two helpers
        # share exactly one InferenceSession in steady state.
        global _signal_session, _signal_session_path, _signal_classes
        if (
            _signal_session is None
            or _signal_session_path != str(model_path)
        ):
            try:
                _opts = _ort.SessionOptions()
                _opts.intra_op_num_threads = 1
                _opts.inter_op_num_threads = 1
                _signal_session = _ort.InferenceSession(
                    str(model_path),
                    sess_options=_opts,
                    providers=["CPUExecutionProvider"],
                )
                _signal_session_path = str(model_path)
                try:
                    import json as _json
                    meta = _json.loads(
                        model_path.with_suffix(".json").read_text(
                            encoding="utf-8",
                        )
                    )
                    _signal_classes = meta.get("charClasses", "0123456789")
                except Exception:
                    _signal_classes = "0123456789"
            except Exception as exc:
                log.debug(
                    "sc_ocr.signal: per-digit CNN session load failed: %s",
                    exc,
                )
                return None

        # Segmenter + 28×28 helper come from the offline pipeline so we
        # train and infer on identical pre-processing.
        try:
            import sys
            from pathlib import Path as _Path
            _scripts = (
                _Path(__file__).resolve().parent.parent.parent / "scripts"
            )
            if str(_scripts) not in sys.path:
                sys.path.insert(0, str(_scripts))
            import extract_labeled_glyphs as _xlg  # type: ignore
        except Exception as exc:
            log.debug(
                "sc_ocr.signal: per-digit CNN segmenter import failed: %s",
                exc,
            )
            return None

        # ── Tesseract-free segmentation ──
        # Column-projection-based split. Comma in the SC font is
        # narrower than ``MIN_SPLIT_WIDTH`` and falls below
        # ``len(spans) >= 2`` runs; in practice the segmenter on a
        # signal-row crop produces 4–5 digit spans plus optionally one
        # comma sliver that is pruned by the digit-min-width filter
        # below.
        try:
            spans = _xlg._segment_digits(work_gray, expected_count=5)
        except Exception as exc:
            log.debug(
                "sc_ocr.signal: per-digit CNN segmenter failed: %s", exc,
            )
            return None
        if not spans:
            return None
        # Filter out comma-width / speck spans (< 3 px wide → never a
        # digit in any HUD font). This is the same width floor the
        # Tesseract-path classifier uses to bail.
        digit_spans = [
            (int(s), int(e)) for (s, e) in spans
            if (int(e) - int(s)) >= 3
        ]
        if not (4 <= len(digit_spans) <= 5):
            return None

        # ── Inverted-polarity voter availability ──
        inv_available = False
        try:
            if (
                fallback._ensure_model_inv()
                and fallback._session_inv is not None
            ):
                inv_available = True
        except Exception as _inv_exc:
            log.debug(
                "sc_ocr.signal: per-digit inv-CNN load failed: %s",
                _inv_exc,
            )
        if not inv_available:
            # Single-CNN evidence is too weak to over-rule CRNN; defer.
            return None

        digits: list[str] = []
        confs: list[float] = []
        crops: list[np.ndarray] = []
        for x1, x2 in digit_spans:
            # Latency guard — bail mid-loop if we're already over budget.
            if (time.monotonic() - _t0) > _SIGNAL_CNN_BUDGET_S:
                log.warning(
                    "sc_ocr.signal: per-digit CNN exceeded %.0f ms budget "
                    "(skipping cross-check on remaining frames in this "
                    "tick)", _SIGNAL_CNN_BUDGET_S * 1000.0,
                )
                _signal_cnn_skip_for_tick = True
                return None
            g = _xlg._glyph_to_28x28(work_gray, x1, x2)
            if g is None:
                return None
            x = (g.astype(np.float32) / 255.0)[None, None, :, :]
            try:
                out = _signal_session.run(None, {"input": x})[0]
            except Exception as exc:
                log.debug(
                    "sc_ocr.signal: per-digit CNN inference failed: %s",
                    exc,
                )
                return None
            logits_pri = out[0]
            idx_pri = int(np.argmax(logits_pri))
            if not (0 <= idx_pri < len(_signal_classes)):
                return None
            # Softmax-style confidence (max prob) for the primary
            # voter. Mirrors the per-glyph confidence the HUD CNN
            # surfaces in `_classify_crops`.
            try:
                ex = np.exp(logits_pri - np.max(logits_pri))
                probs = ex / float(ex.sum() or 1.0)
                conf_pri = float(probs[idx_pri])
            except Exception:
                conf_pri = 0.0

            # Secondary: HUD inverted CNN on the inverted glyph.
            g_inv = (
                255 - g.astype(np.int16)
            ).clip(0, 255).astype(np.uint8)
            x_inv = (
                g_inv.astype(np.float32) / 255.0
            )[None, None, :, :]
            try:
                inv_input_name = (
                    fallback._session_inv.get_inputs()[0].name
                )
                out_inv = fallback._session_inv.run(
                    None, {inv_input_name: x_inv},
                )[0]
            except Exception as exc:
                log.debug(
                    "sc_ocr.signal: per-digit inv-CNN inference "
                    "failed: %s", exc,
                )
                return None
            inv_logits = out_inv[0]
            if len(inv_logits) < 10:
                return None
            digit_logits = inv_logits[:10]
            idx_sec = int(np.argmax(digit_logits))
            if idx_pri != idx_sec:
                # Polarity-disagree → defer. Same rule as
                # ``_signal_cnn_at_tess_boxes``.
                log.debug(
                    "sc_ocr.signal: per-digit dual-polarity disagree "
                    "pri=%d sec=%d at glyph (x=%d-%d) — deferring",
                    idx_pri, idx_sec, x1, x2,
                )
                return None

            digits.append(_signal_classes[idx_pri])
            confs.append(conf_pri)
            crops.append(g.astype(np.uint8))

        if not digits:
            return None
        elapsed = (time.monotonic() - _t0) * 1000.0
        log.debug(
            "sc_ocr.signal: per-digit CNN read=%r confs=%s in %.1f ms",
            "".join(digits),
            ["%.2f" % c for c in confs],
            elapsed,
        )
        return ("".join(digits), confs, crops)
    except Exception as exc:
        log.debug(
            "sc_ocr.signal: per-digit CNN swallowed unexpected: %s", exc,
        )
        return None


# ── Paren anchor for the mineral-name row ────────────────────────────────
#
# The mineral name is the ONLY line in the scan panel whose text carries a
# "(ORE)" / "(RAW)"-style parenthesized suffix (86 of the 129 annotated
# panels; RAW ICE and bare names have none). A high-confidence NCC match
# of BOTH parens, correctly ordered with a suffix-sized gap, is therefore
# a near-unambiguous "this line is the name" signal — stronger than the
# bottom-most-run heuristic when transient ink (particles, composition
# rows inside a mis-anchored cap) adds junk lines.
#
# Calibrated 2026-06-11 on the annotated corpus: true name parens score
# open 0.96-0.99 / close 0.87-0.93; the best impostor (the 'C' of RAW
# ICE) tops out at 0.80; the difficulty meter's real "( EASY )" parens
# score only ~0.59 (different glyph scale) AND sit ~25 template-widths
# apart vs 4-5 for "(ORE)" — rejected by score and by gap. Washed-out
# panels (0916xx cluster) render glyphs embossed (dark core/bright halo)
# and cap at ~0.66 — undetectable without admitting RAW ICE impostors,
# so those intentionally fall through to the legacy heuristic.
_PAREN_TPL_CACHE: "Optional[dict]" = None


def _paren_templates() -> "Optional[dict]":
    """Load + cache zero-mean/unit-var paren templates (open/close).
    Returns None when the npz is missing — every caller falls back to
    the legacy bottom-most-run behavior."""
    global _PAREN_TPL_CACHE
    if _PAREN_TPL_CACHE is not None:
        return _PAREN_TPL_CACHE or None
    try:
        _p = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sc_templates", "paren.npz",
        )
        _d = np.load(_p)
        _out = {}
        for _cls in _d.files:
            if _cls == "height":
                continue
            _t = _d[_cls].astype(np.float32)
            _out[_cls] = (_t - _t.mean()) / (_t.std() + 1e-6)
        _PAREN_TPL_CACHE = _out
    except Exception as _exc:
        log.debug("sc_ocr: paren templates unavailable: %s", _exc)
        _PAREN_TPL_CACHE = {}
    return _PAREN_TPL_CACHE or None


def _match_paren_pair(
    strip: "np.ndarray",
    refs: dict,
) -> "Optional[tuple[float, int, float, int]]":
    """Test ONE text-line strip (2D uint8, max-of-RGB, any polarity) for
    the name's parenthesized suffix. Two independent detectors, OR'd:

    1. PAREN PAIR — confident ordered ``(`` + ``)``, thresholds
       0.90/0.85 with gap in [1.2, 9] template-widths. Content-blind
       but suffix-agnostic (catches an unseen "(GEM)" etc.). The
       per-run strip + gap cap make a cross-row pairing (name ``(``
       with the EASY meter's ``)``) impossible.
    2. WHOLE SUFFIX — ``(ORE)`` / ``(RAW)`` matched as one unit,
       threshold 0.80. Five glyphs of joint signal: verifies the
       content BETWEEN the parens and survives scale drift the pair
       can miss (0.845 at 1.83x live upscale), while the pair covers
       polarity/letter-spacing cases the suffix fumbles (bright-sky
       COPPER: suffix 0.46, pair 0.96/0.93).

    Returns ``(score_open, x_open, score_close, x_close)`` in native
    strip coordinates (suffix hits synthesize the pair span), or None.

    Do not lower the thresholds: RAW ICE's 'C' reaches 0.80 against a
    bare paren, and RAW ICE's own R-A-W letters reach 0.699 against
    the "(RAW)" suffix — the margins above those ARE the design."""
    try:
        _h, _w = strip.shape
        if _h < 8 or _h > 80 or _w < 60:
            return None
        _s8 = _canonicalize_polarity(strip)
        _H = 28
        _neww = max(20, int(round(_w * _H / float(_h))))
        if _neww < 60:
            return None
        _s = np.asarray(
            Image.fromarray(_s8).resize((_neww, _H), Image.BILINEAR),
            dtype=np.float32,
        )
        _best: dict = {}
        for _cls, _t in refs.items():
            _th, _tw = _t.shape
            if _neww < _tw + 2:
                continue
            _win = np.lib.stride_tricks.sliding_window_view(
                _s, (_th, _tw)
            )[0]
            _wf = _win.reshape(_win.shape[0], -1)
            _nf = (_wf - _wf.mean(axis=1, keepdims=True)) / (
                _wf.std(axis=1, keepdims=True) + 1e-6
            )
            _sc = _nf @ (_t.reshape(-1) / float(_t.size))
            _bi = int(np.argmax(_sc))
            _best[_cls] = (float(_sc[_bi]), _bi, _tw)
        _fx = _w / float(_neww)
        # Detector 1: ordered pair with sane gap.
        if "open" in _best and "close" in _best:
            _so, _xo, _two = _best["open"]
            _scl, _xc, _ = _best["close"]
            if _so >= 0.90 and _scl >= 0.85:
                _gap = _xc - _xo
                if 1.2 * _two <= _gap <= 9.0 * _two:
                    return (
                        _so, int(round(_xo * _fx)),
                        _scl, int(round(_xc * _fx)),
                    )
        # Detector 2: whole "(XXX)" suffix as one unit.
        for _cls, (_ss, _xs, _tws) in _best.items():
            if _cls.startswith("suffix_") and _ss >= 0.80:
                return (
                    _ss, int(round(_xs * _fx)),
                    _ss, int(round((_xs + _tws) * _fx)),
                )
        return None
    except Exception:
        return None


def _refine_mineral_band_above_mass(
    img: Image.Image,
    mass_y1: Optional[int],
) -> "Optional[tuple[int, int, int]]":
    """Locate the mineral-name line as the BOTTOM-MOST text line above
    the MASS row top. Returns ``(y1, y2, x_left)`` or None.

    Why (live 2026-06-09, COPPER rock, mineral '?'/garbage for 37s): the
    color detector misses copper-toned names (palette gap) and sometimes
    locks the COMPOSITION list's colored entries far below; the legacy
    projection fallback drifts onto composition rows too; either way the
    OCR crop ends up spanning the SCAN RESULTS title + name (2 lines) or
    the wrong row entirely — single-line readers then emit 'CCIT'/'aee'.
    Structurally the name is the ONLY text line between the title (and
    its underline) and the MASS row, both of which are anchored every
    scan — so the bottom-most ink line above MASS *is* the name,
    regardless of color, panel scale, or which detector misfired.
    """
    try:
        if mass_y1 is None or int(mass_y1) <= 12:
            return None
        _w, _h = img.size
        _cap = min(int(mass_y1) - 4, _h)
        if _cap <= 12 or _w <= 0:
            return None
        _strip = np.asarray(
            img.crop((0, 0, _w, _cap)).convert("L"), dtype=np.uint8
        )
        _gs = _canonicalize_polarity(_strip)
        _bs = _adaptive_binarize(_gs)
        _ink = (_bs > 0).mean(axis=1)
        # contiguous ink-row runs >= 5 px tall
        _runs: list[tuple[int, int]] = []
        _start: Optional[int] = None
        for _yy in range(_cap):
            _on = bool(_ink[_yy] > 0.02)
            if _on and _start is None:
                _start = _yy
            elif not _on and _start is not None:
                if _yy - _start >= 5:
                    _runs.append((_start, _yy))
                _start = None
        if _start is not None and _cap - _start >= 5:
            _runs.append((_start, _cap))
        if not _runs:
            return None
        # ── PAREN ANCHOR ──
        # Stage A: among the candidate runs (bottom→top), prefer the one
        # carrying a confident "(...)" pair — the name's suffix. The
        # plain bottom-most rule mis-picks when transient ink or (with a
        # mis-anchored MASS cap) composition rows enter the strip.
        # Stage B: no pair anywhere above the cap AND the cap looks like
        # it may be wrong → extend the run search below the cap; the
        # strict pair gate (score+order+gap) is what makes looking below
        # MASS safe (values rows / "( EASY )" meter never pass it).
        _pick: "Optional[tuple[int, int, tuple]]" = None
        _refs = _paren_templates()
        _mx_full: "Optional[np.ndarray]" = None
        if _refs is not None:
            try:
                from . import frame_context as _fc
                _mx_full = _fc.max_channel(img)  # shared per-frame percept
                # ±3 row pad: the templates were cut from bands carrying
                # the refine's own ±3 padding — tighter strips shrink the
                # parens ~5% in canonical scale and shave ~0.08 off NCC.
                for _ra, _rb in reversed(_runs[-6:]):
                    _hit = _match_paren_pair(
                        _mx_full[max(0, _ra - 3):min(_cap, _rb + 3)],
                        _refs,
                    )
                    if _hit is not None:
                        _pick = (_ra, _rb, _hit)
                        break
                if _pick is None and _cap < _h - 12:
                    _gs2 = _canonicalize_polarity(
                        np.asarray(img.convert("L"), dtype=np.uint8)[_cap:]
                    )
                    _bs2 = _adaptive_binarize(_gs2)
                    _ink2 = (_bs2 > 0).mean(axis=1)
                    _runs2: "list[tuple[int, int]]" = []
                    _st2: Optional[int] = None
                    for _y2 in range(_ink2.shape[0]):
                        _on2 = bool(_ink2[_y2] > 0.02)
                        if _on2 and _st2 is None:
                            _st2 = _y2
                        elif not _on2 and _st2 is not None:
                            if _y2 - _st2 >= 5:
                                _runs2.append((_st2 + _cap, _y2 + _cap))
                            _st2 = None
                    for _ra, _rb in _runs2[:6]:
                        _hit = _match_paren_pair(
                            _mx_full[max(0, _ra - 3):min(_h, _rb + 3)],
                            _refs,
                        )
                        if _hit is not None:
                            _pick = (_ra, _rb, _hit)
                            break
            except Exception as _pexc:
                log.debug("sc_ocr: paren anchor swallowed: %s", _pexc)
                _pick = None
        if _pick is not None and _pick[:2] != _runs[-1]:
            try:
                _filter_event_log(
                    "PAREN-ANCHOR mineral band y%d-%d open=%.2f@%d "
                    "close=%.2f@%d (default run y%d-%d overridden)"
                    % (
                        _pick[0], _pick[1],
                        _pick[2][0], _pick[2][1],
                        _pick[2][2], _pick[2][3],
                        _runs[-1][0], _runs[-1][1],
                    )
                )
            except Exception:
                pass
        if _pick is not None:
            _ry1, _ry2 = _pick[0], _pick[1]
            _ylim = _h
        else:
            _ry1, _ry2 = _runs[-1]
            _ylim = _cap
        _ry1 = max(0, _ry1 - 3)
        _ry2 = min(_ylim, _ry2 + 3)
        if _pick is not None and _ry1 >= _cap and _mx_full is not None:
            # Stage-B band lies below the original strip — derive the
            # left edge from its own rows, not the (out-of-range) strip.
            _band = _adaptive_binarize(
                _canonicalize_polarity(_mx_full[_ry1:_ry2])
            ) > 0
        else:
            _band = _bs[_ry1:_ry2] > 0
        _colf = _band.mean(axis=0)
        _xs = np.flatnonzero(_colf > 0.05)
        _xl = int(_xs[0]) if _xs.size else 0
        return (_ry1, _ry2, max(0, _xl - 6))
    except Exception as _exc:
        log.debug("sc_ocr: mineral band refine failed: %s", _exc)
        return None


def _ocr_mineral_name(
    img: "Image.Image",
    y1: int,
    y2: int,
    x_min: int,
) -> Optional[str]:
    """Extract the mineral name (e.g. 'Beryl', 'Quantanium') from the
    mineral row crop.

    Multi-pass strategy — robust against dark backgrounds with
    chromatic aberration, light backgrounds with low text-vs-bg
    contrast, and small text where a single PSM choice misreads.

    Pass plan (fail fast on a confident match, otherwise vote):

      Fast path (single attempt, ~50 ms):
        max-of-RGB channels, scale=60px, PSM 7 → fuzzy match.
        If similarity ≥ 0.85, return immediately.

      Slow path (up to 7 more attempts, ~350 ms total):
        Try alternates across {luma, max-channel, max-channel inverted}
        × {scale 60, scale 90} × {PSM 7, PSM 8}, collect every fuzzy
        match with its similarity score, then vote: most-frequent
        canonical name wins; ties broken by max similarity.

    Returns the canonical mineral name on success or None when no
    pass produced a fuzzy-match-able read. Refusing to return raw
    OCR keeps single-letter Tesseract noise out of the break bubble's
    Resource: row.
    """
    try:
        import re
        import pytesseract
        from ..screen_reader import _check_tesseract
        if not _check_tesseract():
            return None
    except Exception as exc:
        log.debug("api: _ocr_mineral_name swallowed: %s", exc)
        return None
    if y2 <= y1 or (y2 - y1) < 6:
        return None
    crop_x_left = max(0, x_min - 20)
    if crop_x_left >= img.width - 4:
        return None
    crop = img.crop((crop_x_left, y1, img.width, y2))
    if crop.width < 20 or crop.height < 8:
        return None

    # ── Trim the empty space to the RIGHT of the mineral text ──
    # The crop spans crop_x_left -> img.width to cover long mineral names,
    # but short names ("Aluminum (Ore)") leave most of it empty. That wide
    # blank strip (a) reads as "grabbing the whole image" in the debug
    # overlay and (b) shrinks the text under Tesseract's fixed-height
    # resize so each pass runs slow enough to blow the slow-path TIME
    # BUDGET — the read then bails to None even though the text is sharp
    # (user 2026-06-02: "Aluminum (Ore)" -> mineral=? while the same crop
    # reads fine given unlimited time). Find where the bright text ends and
    # trim to there + a generous margin; fall back to full width on no
    # clear edge so long names are never clipped.
    try:
        _ct = np.array(crop.convert("RGB"), dtype=np.uint8).max(axis=2)
        _ctc = _canonicalize_polarity(_ct).astype(np.float32)
        _col = _ctc.max(axis=0)  # brightest pixel per column
        _clo, _chi = float(_col.min()), float(_col.max())
        if _chi - _clo > 25.0:
            _cthr = _clo + 0.45 * (_chi - _clo)
            _cink = np.where(_col > _cthr)[0]
            if _cink.size:
                _cmargin = max(50, int((y2 - y1) * 2.5))
                _cneww = min(crop.width, int(_cink[-1]) + _cmargin)
                if 20 <= _cneww < crop.width:
                    crop = crop.crop((0, 0, _cneww, crop.height))
    except Exception:
        pass

    # ── Preprocessing helpers ──
    rgb = np.array(crop.convert("RGB"), dtype=np.uint8)
    luma = np.array(crop.convert("L"), dtype=np.uint8)
    # Max-of-channels is CA-resilient: chromatic aberration smears red
    # and blue from green by 1-2 px, so luma (R*0.30 + G*0.59 + B*0.11)
    # spatially blurs the strokes. Each pixel taking its brightest
    # channel preserves the actual stroke shape.
    max_ch = rgb.max(axis=2).astype(np.uint8)

    def _prep(variant: str, target_h: int) -> Optional["Image.Image"]:
        """Build a Tesseract-ready PIL image from the named variant."""
        if variant == "luma":
            base = _canonicalize_polarity(luma)
        elif variant == "max":
            base = _canonicalize_polarity(max_ch)
        elif variant == "max_inv":
            # Force the opposite polarity from what canonicalize picks.
            # Useful when the row's background histogram is bimodal in
            # a way that fools the minority-class rule.
            canon = _canonicalize_polarity(max_ch)
            base = (255 - canon).astype(np.uint8)
        else:
            return None
        H, W = base.shape
        if H < target_h:
            scale = max(2, target_h // max(1, H))
            new_w = max(1, W * scale)
            new_h = max(1, H * scale)
            try:
                return Image.fromarray(base).resize(
                    (new_w, new_h), Image.LANCZOS,
                )
            except Exception as exc:
                log.debug("api: _prep swallowed: %s", exc)
                return None
        return Image.fromarray(base)

    _CFG_TEMPLATE = (
        "--psm {psm} -c tessedit_char_whitelist="
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz () "
    )

    def _run(tess_input: "Image.Image", psm: int) -> str:
        try:
            return pytesseract.image_to_string(
                tess_input,
                config=_CFG_TEMPLATE.format(psm=psm),
            ).strip()
        except Exception as exc:
            log.debug("mineral OCR pass failed psm=%d: %s", psm, exc)
            return ""

    try:
        from ..refinery_reader import _fuzzy_mineral_scored
    except Exception:
        # Fallback to the legacy unscored matcher if scored variant
        # isn't available (older builds).
        try:
            from ..refinery_reader import _fuzzy_mineral as _legacy_fm

            def _fuzzy_mineral_scored(t: str):
                r = _legacy_fm(t)
                return (r, 0.7) if r else None
        except Exception as exc:
            log.debug("api: _ocr_mineral_name swallowed: %s", exc)
            return None

    def _try_fuzzy(raw: str) -> Optional[tuple[str, float, str]]:
        """Return (canonical, similarity, raw_text) or None."""
        if not raw:
            return None
        base = _RE_PARENS_GROUP.sub("", raw).strip()
        if not base:
            base = raw
        try:
            scored = _fuzzy_mineral_scored(base)
        except Exception as exc:
            log.debug("mineral fuzzy match failed: %s", exc)
            return None
        if scored is None:
            return None
        return (scored[0], scored[1], raw)

    # ── CRNN attempt (before any Tesseract) ──
    # The CRNN was pretrained on SC Datarunner's full alphabet
    # (`0-9.-% ()A-Za-z`), so it can read mineral panels like
    # ``COPPER (ORE)`` end-to-end. One ONNX forward pass is ~30 ms vs
    # Tesseract's ~300-400 ms per attempt, AND the CRNN handles the
    # chromatic-aberration cases that trip Tesseract on coloured HUDs.
    # Run on the same max-of-channels preprocessed image the fast
    # Tesseract path uses so colour text registers as bright. Fuzzy-
    # match through the same canonical-mineral filter; if we lock at
    # ≥0.85 we skip Tesseract entirely.
    crnn_input = _prep("max", 60)
    if crnn_input is not None:
        try:
            _crnn_out = _crnn_recognize(crnn_input)
        except Exception as _crnn_exc:
            log.debug(
                "sc_ocr: mineral_name CRNN attempt failed: %s",
                _crnn_exc,
            )
            _crnn_out = None
        if _crnn_out is not None and _crnn_out[0]:
            _crnn_text = _crnn_out[0]
            _crnn_confs = _crnn_out[1] if len(_crnn_out) > 1 else []
            _crnn_mean = (
                sum(_crnn_confs) / len(_crnn_confs) if _crnn_confs else 0.0
            )
            # Glyph-reader telemetry: dump every CRNN attempt so
            # users can see WHY mineral_name is failing when it is.
            _dump_voter("mineral", "crnn", _crnn_text, _crnn_mean)
            _crnn_match = _try_fuzzy(_crnn_text)
            # Tighter threshold (0.92) for CRNN than Tesseract (0.85)
            # because CRNN is more likely to produce noisy near-miss
            # text on letters (it was fine-tuned on digits; the
            # alphabet vocab survives from the SC Datarunner pretrain
            # but the LETTER recall is less proven). Without this
            # tightening, a CRNN read of "ALMINUM" / "AMUNIUM" can
            # fuzzy-snap to "Ammonia" via SequenceMatcher because
            # both share leading 'A' + 'M's. Falling through to
            # Tesseract is cheaper than displaying the wrong mineral.
            if _crnn_match is not None and _crnn_match[1] >= 0.92:
                log.info(
                    "sc_ocr: mineral_name CRNN sim=%.2f raw=%r → %r",
                    _crnn_match[1], _crnn_match[2], _crnn_match[0],
                )
                _dump_voter(
                    "mineral", "winner",
                    _crnn_match[0], _crnn_match[1],
                )
                return _crnn_match[0]
            else:
                log.debug(
                    "sc_ocr: mineral_name CRNN raw=%r fuzzy=%s — "
                    "falling through to Tesseract",
                    _crnn_text,
                    f"{_crnn_match[1]:.2f}" if _crnn_match else "no-hit",
                )

    # ── Fast Tesseract path ──
    fast_input = _prep("max", 60)
    fast_text = ""
    if fast_input is not None:
        fast_text = _run(fast_input, 7)
        # Glyph-reader telemetry: same diagnostic role as the digit
        # field's tesseract dump — surfaces what Tesseract sees on
        # the mineral-row crop so the user can compare it to the
        # actual on-screen text.
        _dump_voter("mineral", "tesseract", fast_text, None)
        fast = _try_fuzzy(fast_text)
        if fast is not None and fast[1] >= 0.85:
            log.info(
                "sc_ocr: mineral_name fast=%.2f raw=%r → %r",
                fast[1], fast[2], fast[0],
            )
            _dump_voter("mineral", "winner", fast[0], fast[1])
            return fast[0]

    # ── Slow path: vote across preprocessing × PSM combinations ──
    # Per-scan budget: each Tesseract call is ~300-400 ms on Windows
    # (process spawn + load + recognize), and the full 3×2×2 = 12-call
    # matrix below was eating 4-5 seconds per scan when no candidate
    # ever crossed threshold. The break bubble update is gated on the
    # full scan completing, so this latency was directly visible to
    # the user. Wall-clock cap stops the search early.
    #
    # Also: if the fast path returned EMPTY text (Tesseract found no
    # readable characters at all), the slow path's variant tweaks
    # almost never recover anything — skip the matrix entirely.
    if not fast_text.strip():
        log.info(
            "sc_ocr: mineral_name fast Tesseract returned empty — "
            "skipping slow path (would burn ~4 s for likely no hit)",
        )
        return None

    import time as _time
    # Budget for the multi-pass Tesseract vote. Was 0.6s, which is enough
    # in isolation (it collects candidates within ~600ms) but NOT under
    # live load — there the per-pass Tesseract spawn is slow enough that
    # the budget expires with 0 candidates collected and the read drops to
    # None, even on a perfectly sharp crop (user 2026-06-02: "Aluminum
    # (Ore)" -> mineral=?). Mineral name is read ONCE per rock and then
    # latched into the frozen reference (it stops re-running once
    # resolved), so a longer one-shot budget buys correctness without a
    # recurring per-scan cost. Paired with the right-trim above (smaller
    # crop -> faster passes) so this rarely needs the full window.
    _SLOW_BUDGET_S = 1.5
    _slow_t0 = _time.monotonic()
    candidates: list[tuple[str, float, str, str]] = []  # (canonical, sim, raw, source_tag)
    _aborted = False
    for variant in ("max", "max_inv", "luma"):
        if _aborted:
            break
        for target_h in (60, 90):
            if _aborted:
                break
            inp = _prep(variant, target_h)
            if inp is None:
                continue
            for psm in (7, 8):
                if (_time.monotonic() - _slow_t0) > _SLOW_BUDGET_S:
                    log.info(
                        "sc_ocr: mineral_name slow-path budget "
                        "exceeded (%.0f ms) — bailing with %d cand",
                        _SLOW_BUDGET_S * 1000, len(candidates),
                    )
                    _aborted = True
                    break
                txt = _run(inp, psm)
                m = _try_fuzzy(txt)
                if m is not None:
                    candidates.append(
                        (m[0], m[1], m[2], f"{variant}@h{target_h}/psm{psm}"),
                    )

    if not candidates:
        log.info(
            "sc_ocr: mineral_name no fuzzy hit across any pass — dropping",
        )
        return None

    # Vote: most-frequent canonical name wins, ties broken by max similarity.
    counts: dict[str, int] = {}
    max_sim: dict[str, float] = {}
    sample: dict[str, str] = {}
    for name, sim, raw, _tag in candidates:
        counts[name] = counts.get(name, 0) + 1
        if sim > max_sim.get(name, 0.0):
            max_sim[name] = sim
            sample[name] = raw
    winner = max(counts.keys(), key=lambda n: (counts[n], max_sim[n]))
    log.info(
        "sc_ocr: mineral_name vote winner=%r (count=%d max_sim=%.2f, "
        "%d total candidates) raw=%r",
        winner, counts[winner], max_sim[winner],
        len(candidates), sample[winner],
    )
    _dump_voter("mineral", "vote", sample[winner], max_sim[winner])
    _dump_voter("mineral", "winner", winner, max_sim[winner])
    return winner


def _label_rows_for_region(
    region: dict, img,
) -> dict[str, tuple[int, int, int]]:
    """Run ``_find_label_rows`` for ``region`` and apply the user's
    global ``column_x_offset`` to every entry's ``x_value_start``.

    Centralises:
      * ``_set_current_region`` (so manual-override + persistent
        calibration paths inside ``_find_label_rows`` see the region).
      * ``column_x_offset`` shift uniformly across both the locked
        fast-path validation call and the full-OCR call.

    The shift is applied to the ``x_value_start`` (third tuple
    element) of EVERY field in the returned dict — including the
    ``_mineral_row`` entry — so the value-crop x-start moves
    together for all rows that share the HUD's value column.
    """
    from ..onnx_hud_reader import _find_label_rows, _set_current_region
    _set_current_region(region)
    label_rows = _find_label_rows(img)
    if not label_rows:
        return label_rows
    try:
        from . import calibration as _cal_off
        offset = int(_cal_off.get_column_x_offset(region) or 0)
    except Exception as _exc:
        log.debug("column_x_offset lookup failed: %s", _exc)
        offset = 0
    if offset == 0:
        return label_rows
    img_w = getattr(img, "width", None)
    if img_w is None:
        try:
            img_w = int(img.size[0])
        except Exception:
            img_w = None
    shifted: dict[str, tuple[int, int, int]] = {}
    for _f, _entry in label_rows.items():
        try:
            _y_s, _y_e, _x_v = _entry
        except (TypeError, ValueError):
            shifted[_f] = _entry
            continue
        _new_x = int(_x_v) + offset
        # Clamp into image so a misconfigured offset can't crash
        # downstream crop slicing.
        if _new_x < 0:
            _new_x = 0
        if img_w is not None and _new_x > int(img_w) - 1:
            _new_x = max(0, int(img_w) - 1)
        shifted[_f] = (_y_s, _y_e, _new_x)
    log.debug(
        "hud: applied column_x_offset=%d, x_v_starts now=%s",
        offset, {k: v[2] for k, v in shifted.items()},
    )
    return shifted


def scan_hud_onnx(
    region: dict,
    *,
    _img_override: Optional[Image.Image] = None,
    _suppress_freeze: bool = False,
) -> dict:
    """Read the mining HUD panel → {mass, resistance, instability, panel_visible}.

    Uses pure-NumPy mineral-row detection + fixed pixel offsets to
    locate value crops, then ONNX batch classification. No Tesseract,
    no PaddleOCR, no subprocesses. ~23 ms per scan.

    Internal-use parameters (leading underscore):

    ``_img_override``
        When supplied, skip the screen-capture step and run the
        pipeline against this PIL image instead. Used by the
        snapshot re-OCR path to re-run the OCR pipeline on the
        static frozen image — same code path, different input.
    ``_suppress_freeze``
        When ``True``, skip the freeze block entirely (no auto-
        freeze, no auto-clear, no frozen-wins override). Must be
        set on recursive calls from inside the freeze block to
        avoid infinite recursion.
    """
    empty = {
        "mass": None,
        "resistance": None,
        "instability": None,
        "mineral_name": None,
        "panel_visible": False,
    }
    t0 = time.time()
    # Live-frame heartbeat: lets panel_solve hold the rigid pose steady
    # across this continuous stream (kills the per-frame NCC jitter).
    # ONLY on true live capture (no _img_override) — the offline harness
    # and snapshot re-OCR drive this with _img_override over UNRELATED
    # stills, where holding pose across frames would alias one panel onto
    # the next and corrupt the gate. Those paths get a fresh stateless
    # solve every image.
    if _img_override is None:
        try:
            from . import panel_solve as _psolve_hb
            _psolve_hb.note_live_frame()
        except Exception:
            pass
    # ── Profile-aware dispatch (scaffolding) ──
    # Load the profile that scopes this scan (mining HUD = digit-only,
    # uses model_cnn.onnx). Subsequent steps will route classification
    # and validation through this profile so:
    #   1. The mining HUD's digit model is reserved for mining HUD only
    #      (other panels use the SC alphabet model when it's trained).
    #   2. Per-field char whitelists are enforced post-classification
    #      (a digit-only field can never produce a letter even if the
    #      classifier is confused).
    # For now the profile is loaded but not yet enforced — that's a
    # follow-up wiring change so we can verify nothing regresses first.
    try:
        from . import profile_loader as _pl
        _profile = _pl.get_profile("mining_hud")
    except Exception as _pexc:
        log.warning("profile_loader: get_profile('mining_hud') failed: %s", _pexc)
        _profile = None
    # Diagnostic overlay handle. NOTE: do NOT call ``_dbg.reset()`` here.
    # The ThreadPoolExecutor(64) runs scans concurrently against the
    # module-global ``_state`` dict. A per-scan ``reset()`` wipes state
    # set by sibling scans before they can fire their late-stage
    # ``write()`` — symptom: ``debug_overlay_wrote.txt`` always shows
    # ``state_keys=['image']`` because whichever scan's ``reset()`` ran
    # most recently nuked the panel_finder / label_rows / value_crops
    # entries that earlier scans had successfully populated. Each
    # ``set_*()`` setter already overwrites its own key on every call,
    # so stale data is naturally refreshed by the next scan that
    # produces a result. Letting state accumulate is what makes the
    # SCAN RESULTS box stick on-screen across momentary anchor misses.
    try:
        from . import debug_overlay as _dbg
    except Exception:
        _dbg = None
    # Single-frame capture. The previous 12-frame averaging blurred
    # text rendering enough to confuse OCR on tight glyphs, and the
    # anchor-based row reconciliation in _find_label_rows handles
    # jiggle-related row mis-identification structurally instead.
    #
    # ``_img_override`` lets the snapshot re-OCR path inject the
    # frozen snapshot in place of a live screen-grab — the rest of
    # the pipeline (find_label_rows, per-field OCR, etc.) runs
    # unchanged against the override image. Recursion is bounded by
    # ``_suppress_freeze=True`` on the inner call (no second-level
    # re-OCR).
    if _img_override is not None:
        img = _img_override
    else:
        img = capture.grab(region)
        if img is None:
            img = capture.grab(region)
        if img is None:
            return empty
    # Upscale to the reference size IMMEDIATELY (before any overlay write
    # or the clean dump), so the entire scan — capture dump, both debug
    # overlay writes, the OCR, and the solved pose — lives in ONE
    # resolution. Previously the upscale happened mid-scan, so the early
    # overlay write at the raw capture size and the end-of-scan write at
    # the upscaled size produced TWO different-sized overlays per scan
    # (e.g. 424x276 vs 831x541) — the recording caught both and it looked
    # like the HUD was shrinking. The model wants ~24px text rows; a small
    # user HUD region (e.g. 276px tall) upscales to REF_H so glyph size is
    # consistent regardless of the user's region dimensions.
    # REF_H = the title/glyph scale the anchor NCC templates were built for.
    # Was 541 (the old 397x541 training-panel height) and ONE-DIRECTIONAL: it
    # only upscaled regions SMALLER than ~514px, so a LARGE capture (high-DPI /
    # high-res / big in-game HUD scale) sailed through un-normalized. There the
    # title outgrows the multi-scale template coverage, the pose-solver loses its
    # lock and returns a garbage scale, and every row reads off a bad pose — the
    # "works on my machine, breaks for other users" collapse. Native panels are
    # ~670px tall, ABOVE the old 514 threshold, so the dev machine never triggered
    # it. Fix (Elah 2026-07-14): calibrate REF_H to the native height and normalize
    # in BOTH directions, so the anchor NCC always sees the title at the scale its
    # templates cover regardless of the user's render geometry. Validated on 22
    # real labelled panels: 2.0x rescaled 0% -> 77%, native 100%, off-native band
    # tightened from a ragged 0-86% to a steady 77-91%.
    REF_H = 670
    _W_img, _H_img = img.size
    if abs(_H_img - REF_H) > REF_H * 0.02:   # normalize BOTH directions to REF_H
        img = img.resize(
            (max(1, int(round(_W_img * (REF_H / _H_img)))), REF_H), Image.LANCZOS,
        )
    # One-shot CLEAN capture dump (no overlay annotations) so the exact
    # input the reader sees can be replayed offline to fix crop geometry
    # against real data instead of guessing. Gated on a viewer heartbeat
    # so production with no viewer open pays nothing.
    try:
        from . import debug_overlay as _dbg_clean
        if _dbg_clean.diagnostics_active() and img is not None:
            img.save(os.path.join(
                os.path.dirname(_GLYPH_DUMP_DIR), "debug_clean_region.png"
            ))
    except Exception:
        pass
    if _dbg is not None:
        _dbg.set_image(img)
        # Write IMMEDIATELY so the viewer reflects the latest capture
        # even if any downstream step crashes before reaching the
        # end-of-scan write. End-of-scan write overwrites with the
        # fully-annotated version.
        try:
            _dbg.write()
        except Exception as _wexc:
            log.warning("debug_overlay early write failed: %s", _wexc)

    # (img was already upscaled to REF_H right after capture, above.)
    gray = np.asarray(img.convert("L"), dtype=np.uint8)

    median_gray = float(np.median(gray))
    if median_gray > 130:
        gray = 255 - gray

    # ── STAGE 1 (region1): structural HUD panel localization ──
    # ``find_hud_panel`` (used by the auto-annotator's ``detect_pill``
    # for region2) is also useful for region1 — it gives a tight
    # x/y/w/h bbox of the SCAN RESULTS panel from RGB color mask +
    # connected components alone, without depending on any text or
    # chrome detection. We use it as a position prior for the mineral
    # row detector below. Returns None when no panel is found; the
    # downstream detectors fall back to image-fraction priors.
    _panel_bbox_for_region1: Optional[tuple[int, int, int, int]] = None
    try:
        from hud_tracker.anchors.hud_color_finder import (
            find_hud_panel as _find_hud_panel_r1,
        )
        _hud_arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
        _hud_res = _find_hud_panel_r1(_hud_arr)
        if _hud_res is not None and _hud_res.get("bbox"):
            _hb = _hud_res["bbox"]
            _panel_bbox_for_region1 = (
                int(_hb[0]), int(_hb[1]), int(_hb[2]), int(_hb[3]),
            )
            log.info(
                "sc_ocr: find_hud_panel (region1) picked bbox=%s conf=%.2f",
                _panel_bbox_for_region1, _hud_res.get("confidence", 0.0),
            )
    except ImportError:
        pass
    except Exception as _hud_exc:
        log.debug("sc_ocr: find_hud_panel (region1) failed: %s", _hud_exc)

    # ── PRIMARY: color-aware mineral row detector ──
    # The mineral name renders in distinctive warm/cyan/purple colors
    # that the cyan label text and chrome lines don't share, so a
    # multi-band HSV mask + connected-components survives the bright
    # icy-asteroid backgrounds that fool the legacy projection-band
    # detector (which has been observed to fire in the COMPOSITION
    # area on ~15% of unlabeled captures with bright backgrounds).
    # See hud_tracker/anchors/mineral_name_color.py for the algorithm.
    # When ``_panel_bbox_for_region1`` is available the detector's
    # position prior is tightened to the panel's vertical span.
    mineral_row: Optional[tuple[int, int]] = None
    # Left edge of the mineral-name text, from the color detector's bbox.
    # The shared "_mineral_row" entry carries the NUMERIC value-column x
    # (~180px in), which crops off the whole mineral name; this is the
    # real text start so the OCR crop begins in the right place.
    _mineral_text_x: Optional[int] = None
    try:
        from hud_tracker.anchors.mineral_name_color import (
            find_mineral_name_row as _find_mineral_color,
        )
        _color_res = _find_mineral_color(
            img, panel_bbox=_panel_bbox_for_region1,
        )
        if _color_res is not None:
            _bx, _by, _bw, _bh = _color_res["bbox"]
            mineral_row = (int(_by), int(_by) + int(_bh))
            _mineral_text_x = int(_bx)
            log.info(
                "sc_ocr: mineral_name_color picked y=(%d, %d) hue=%.1f conf=%.2f",
                mineral_row[0], mineral_row[1],
                _color_res["details"].get("dominant_hue", float("nan")),
                _color_res["confidence"],
            )
    except ImportError:
        pass
    except Exception as _color_exc:
        log.debug("sc_ocr: mineral_name_color failed: %s", _color_exc)

    # ── FALLBACK: legacy projection-band detector ──
    # Kept as the safety net when color detection returns None (e.g.
    # very dim mineral text where the saturated palette mask comes up
    # empty). The deepest-fallback ``_find_mineral_row_universal`` is
    # also available via this module for callers (e.g. the auto-
    # annotator) that want luma-only behaviour as a last resort —
    # currently unused at runtime but kept for API stability.
    if mineral_row is None:
        mineral_row = _find_mineral_row(img)

    # MASS-anchor drift correction.  Detection of the SCAN RESULTS
    # header + MASS label runs every scan — that's the panel anchor
    # the user explicitly asked us to keep.  The locked row Y values
    # ride along with this anchor via a small drift offset so the
    # locks stay aligned with the panel as it shifts a few pixels.
    #
    # We CAP the drift at ±25 px.  Anything bigger almost always
    # means the NCC matched against the wrong text (e.g. "MASS" inside
    # COMPOSITION), so we ignore it and keep using the saved Y values
    # verbatim.  The previous "Tier-2 override" path that silently
    # rewrote saved Y to NCC-detected Y on large drifts is GONE — it
    # caused locks to randomly jump across the screen whenever the
    # NCC anchor mis-fired.  If a real, large panel move happens, the
    # user re-opens the calibration dialog and re-locks; that's
    # explicit and predictable.
    _cal_drift_y = 0
    _ncc_label_positions: dict = {}
    try:
        from . import calibration as _cal_drift_mod
        _saved_cal = _cal_drift_mod.load(region)
        _saved_mass = (_saved_cal or {}).get("rows", {}).get("mass") if _saved_cal else None
        from . import label_match as _lm_drift
        _ncc_label_positions = _lm_drift.find_label_positions(img)
        _mass_match = _ncc_label_positions.get("mass")
        if (
            _saved_mass is not None
            and _mass_match is not None
            and _mass_match.get("score", 0) >= 0.50
        ):
            _cur_mass_y = int(_mass_match["y"])
            _cal_mass_y = int(_saved_mass["y"])
            _proposed_drift = _cur_mass_y - _cal_mass_y
            if abs(_proposed_drift) <= 25:
                _cal_drift_y = _proposed_drift
                if _cal_drift_y != 0:
                    log.info(
                        "sc_ocr: MASS-anchor drift %+d px "
                        "(cur=%d, cal=%d, ncc=%.2f) — drift-"
                        "correcting locked crops",
                        _cal_drift_y, _cur_mass_y, _cal_mass_y,
                        _mass_match["score"],
                    )
            else:
                log.debug(
                    "sc_ocr: MASS-anchor drift %+d px exceeds ±25 px "
                    "cap (cur=%d, cal=%d) — likely an NCC false "
                    "positive; keeping saved Y verbatim",
                    _proposed_drift, _cur_mass_y, _cal_mass_y,
                )
    except Exception as _drift_exc:
        log.debug("anchor-drift compute failed: %s", _drift_exc)
        _cal_drift_y = 0
        _ncc_label_positions = {}

    if mineral_row is None:
        # No panel visible — reset consensus buffers AND drop any
        # locked field values for this region. The user looked away
        # from the rock; next rock starts fresh.
        _reset_consensus_buffers()
        _field_lock_cache.pop(_region_key(region), None)
        _difficulty_cache.pop(_region_key(region), None)
        # Also drop the SCAN RESULTS anchor cache — next rock might
        # have a slightly different panel position if the user moved.
        try:
            from ..onnx_hud_reader import _scan_results_anchor_cache
            _scan_results_anchor_cache.clear()
        except Exception as exc:
            log.debug("api: scan_hud_onnx swallowed: %s", exc)
        # Feed the UI-freshness gate even though we're bailing early.
        # mineral_row is None means the panel finder couldn't lock the
        # mineral-name row, which is a strong signal the panel is
        # absent. We still record count=0 so the streak counter
        # advances and the gate can clear the frozen reference on the
        # next ZERO_LABEL-streak-reaching scan.
        try:
            from . import frozen_panel as _fp_early
            _frozen_early = _fp_early.get_frozen_ref(region)
            _freshness_early = _frozen_early.record_label_match_count(0)
            if _freshness_early.get("action") == "clear":
                log.info(
                    "sc_ocr.hud: UI-freshness — %d consecutive scans "
                    "with 0/3 labels matched -> clear (mineral_row absent)",
                    _freshness_early.get("zero_label_streak", 0),
                )
                _frozen_early.clear()
        except Exception as _fp_early_exc:
            log.debug(
                "api: scan_hud_onnx UI-freshness early gate swallowed: %s",
                _fp_early_exc,
            )
        # Write a (mostly-empty) overlay so the viewer reflects
        # "no panel detected" instead of stale data.
        if _dbg is not None:
            try:
                _dbg.write()
            except Exception as exc:
                log.debug("api: scan_hud_onnx swallowed: %s", exc)
        return empty

    result = dict(empty)
    result["panel_visible"] = True

    # ── Field-value lock fast-path ──
    # If all three fields are already locked from previous scans,
    # short-circuit the entire OCR pipeline. This is the steady-state
    # behavior once the user has stopped on a rock — we read each
    # field once, validate it, and then just return the locked values
    # until the panel disappears OR until any field's crop drifts
    # from its stored fingerprint (drop the lock, re-OCR).
    _rk = _region_key(region)
    _locks = _field_lock_cache.get(_rk, {})
    if _locks and _rk in _field_lock_cache:
        _field_lock_cache.move_to_end(_rk)
    if (
        "mass" in _locks
        and "resistance" in _locks
        and "instability" in _locks
    ):
        # All-locked steady state. We STILL run label-row detection
        # and per-field crop save + NCC self-invalidation, otherwise
        # locks set under wrong row geometry can never recover (and
        # the live debug viewer goes stale). What we skip is the
        # CNN classification + Tesseract fallback per field — that's
        # where the real cost is.
        try:
            _label_rows_for_validation = _label_rows_for_region(region, img)
        except Exception as _exc:
            log.debug("label_rows in lock-validation failed: %s", _exc)
            _label_rows_for_validation = {}

        # Telemetry: push the row geometry into the debug overlay
        # even on the locked fast path.
        if _dbg is not None:
            _dbg.set_label_rows(_label_rows_for_validation)

        # Per-field crop save + NCC drift check.
        for _field in ("mass", "resistance", "instability"):
            _entry = _label_rows_for_validation.get(_field)
            if _entry is None:
                # No row geometry — emit a placeholder so the live
                # viewer reflects the failure state (otherwise stale
                # crops mask the failure).
                try:
                    _placeholder = Image.new(
                        "RGB", (200, 30), (40, 20, 20),
                    )
                    from . import live_broadcast as _bcast
                    _bcast.deliver_crop(_field, _placeholder)
                except Exception as exc:
                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
                if _field == "mass":
                    result["mass"] = _locks["mass"][0]
                elif _field == "resistance":
                    result["resistance"] = _locks["resistance"][0]
                elif _field == "instability":
                    result["instability"] = _locks["instability"][0]
                if _dbg is not None:
                    _dbg.set_lock(_field, _locks[_field][0])
                continue
            _y1, _y2, _lr = _entry
            try:
                _vc = _find_value_crop(
                    img, gray, _y1, _y2,
                    x_min=max(0, _lr + 6),
                )
            except Exception:
                _vc = None
            _locked_val, _locked_fp = _locks[_field]
            if _vc is None:
                # Value-column extraction failed — emit a row-strip
                # placeholder so the live viewer reflects what's
                # currently on screen instead of going stale.
                # Otherwise the user can't tell if the panel finder
                # is misaligned.
                try:
                    _placeholder = img.crop((0, _y1, img.width, _y2))
                    from . import live_broadcast as _bcast
                    _bcast.deliver_crop(_field, _placeholder)
                except Exception as exc:
                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
                # Preserve the locked value (we can't re-OCR without a
                # value crop).
                if _field == "mass":
                    result["mass"] = _locked_val
                elif _field == "resistance":
                    result["resistance"] = _locked_val
                elif _field == "instability":
                    result["instability"] = _locked_val
                if _dbg is not None:
                    _dbg.set_lock(_field, _locked_val)
                continue
            # Push current crop to live viewers (in-process broadcast
            # for the calibration dialog, gated disk write for
            # cross-process viewers).
            try:
                from . import live_broadcast as _bcast
                _bcast.deliver_crop(_field, _vc)
            except Exception as exc:
                log.debug("api: scan_hud_onnx swallowed: %s", exc)
            # Push value-crop box into telemetry.
            if _dbg is not None:
                try:
                    _vc_w, _vc_h = _vc.size
                    _ax_left = max(0, _lr + 6)
                    _ax_right = min(img.width, _ax_left + _vc_w)
                    _dbg.set_value_crop(
                        _field,
                        (_ax_left, _y1, _ax_right, _y1 + _vc_h),
                    )
                except Exception as exc:
                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
            # NCC drift check vs stored fingerprint.
            _current_fp = _crop_fingerprint(_vc)
            _drift_ncc = 1.0
            if _current_fp is not None and _locked_fp is not None:
                _drift_ncc = float(np.dot(_current_fp, _locked_fp) / len(_current_fp))
            if _drift_ncc < _LOCK_INVALIDATE_NCC:
                log.info(
                    "sc_ocr: LOCK INVALIDATED field=%s in fast-path "
                    "(crop drifted, NCC=%.2f < %.2f)",
                    _field, _drift_ncc, _LOCK_INVALIDATE_NCC,
                )
                if _dbg is not None:
                    _dbg.set_lock(_field, None, invalidated=True)
                # Drop the lock and the field's value (becomes None).
                # The next scan will go through the full per-field
                # OCR loop below, since not-all-locked anymore.
                del _field_lock_cache[_rk][_field]
                _RECENT_READS[_field].clear()
                _RECENT_CROPS[_field].clear()
                # Reset the reverify counter — the lock is gone so
                # there's nothing left to verify against.
                _scans_since_lock_verify[_field] = 0
                # NCC drift = rock changed under us. Drop the
                # per-region difficulty cache so the next scan
                # re-detects EASY/MEDIUM/HARD/etc.
                _difficulty_cache.pop(_rk, None)
                # Force fall-through to the full OCR path
                _locks = _field_lock_cache.get(_rk, {})
                break

            # ── Periodic lock re-verification ──
            # Every _REVERIFY_THRESHOLD scans on this locked field,
            # force a fresh OCR pass to catch wrong-locked values that
            # got latched during a degenerate earlier read. Done on
            # ONE field per scan tick (the field whose counter tripped)
            # to keep latency overhead bounded — typical worst case is
            # one extra _ocr_value_crop call (~150 ms) per ~5 s.
            _scans_since_lock_verify[_field] = (
                _scans_since_lock_verify.get(_field, 0) + 1
            )
            if (
                _scans_since_lock_verify[_field] >= _REVERIFY_THRESHOLD
            ):
                _verdict, _fresh, _mean = _lock_reverify_field(
                    _vc, _field, float(_locked_val),
                )
                if _verdict == "INVALIDATED" and _fresh is not None:
                    log.info(
                        "sc_ocr: LOCK-REVERIFY field=%s v_locked=%s "
                        "fresh_read=%s mean=%.2f -> INVALIDATED "
                        "(lock cleared)",
                        _field, _locked_val, _fresh, _mean,
                    )
                    if _dbg is not None:
                        _dbg.set_lock(_field, None, invalidated=True)
                    # Drop the lock entry, flush per-field consensus
                    # so the fresh value doesn't have to fight the
                    # old buffered reads, reset the counter, and
                    # signal the persistence-bias to bypass the
                    # sticky-streak check for this one scan.
                    del _field_lock_cache[_rk][_field]
                    _RECENT_READS[_field].clear()
                    _RECENT_CROPS[_field].clear()
                    _scans_since_lock_verify[_field] = 0
                    _REVERIFY_BYPASS[_field] = float(_fresh)
                    # Also clear the persistence-bias sticky tracker
                    # for this field. The streak was tracking the
                    # WRONG value — leaving it intact would mean any
                    # subsequent confirmation read of _fresh has to
                    # fight a 4-deep stickiness from the bogus locked
                    # value. Reset cleanly so the corrected value
                    # starts a new streak from scratch.
                    _PERSIST_STREAK[_field] = 0
                    _PERSIST_LAST[_field] = None
                    # Surface the fresh value in THIS scan's result so
                    # downstream consumers see the corrected number
                    # immediately, not on the next scan. The spec
                    # requires accepting a >=_REVERIFY_CONF_MIN
                    # disagreeing read; pushing it into the result
                    # dict directly is the simplest faithful path.
                    if _field == "mass":
                        result["mass"] = float(_fresh)
                    elif _field == "resistance":
                        result["resistance"] = float(_fresh)
                    elif _field == "instability":
                        result["instability"] = float(_fresh)
                    # NB: we do NOT drop the per-region difficulty
                    # cache here. The rock didn't change — only a
                    # single field's locked digit was wrong. Difficulty
                    # is rock-wide and still valid.
                    # Break out of the for-loop. The existing
                    # post-loop branch returns result with the other
                    # locked fields intact + this field's fresh value.
                    _locks = _field_lock_cache.get(_rk, {})
                    break
                if _verdict == "CONFIRMED":
                    log.info(
                        "sc_ocr: LOCK-REVERIFY field=%s v_locked=%s "
                        "fresh_read=%s mean=%.2f -> CONFIRMED "
                        "(counter reset)",
                        _field, _locked_val, _fresh, _mean,
                    )
                    _scans_since_lock_verify[_field] = 0
                else:
                    # NO_READ — leave the counter ticked up but DO
                    # NOT reset it. On the next scan the threshold
                    # will trigger again and we'll keep trying until
                    # either CONFIRMED or INVALIDATED. This avoids
                    # masking a wrong lock when the OCR is briefly
                    # noisy (a reset would push the next attempt out
                    # another _REVERIFY_THRESHOLD scans).
                    log.info(
                        "sc_ocr: LOCK-REVERIFY field=%s v_locked=%s "
                        "fresh_read=%s mean=%.2f -> NO_READ "
                        "(keeping lock)",
                        _field, _locked_val,
                        _fresh if _fresh is not None else "''",
                        _mean,
                    )
            # Lock holds — return locked value.
            if _field == "mass":
                result["mass"] = _locked_val
            elif _field == "resistance":
                result["resistance"] = _locked_val
            elif _field == "instability":
                result["instability"] = _locked_val
            if _dbg is not None:
                _dbg.set_lock(_field, _locked_val)
        else:
            # No invalidation — all locks hold. Cache mineral name with
            # PERIODIC REVERIFY (live 2026-06-10: a one-shot fast-path
            # misread cached 'Stannite' forever while every full-path
            # vote said 'Aluminum' — the cache had no verification and
            # no expiry, and the full path never runs while ALL LOCKED).
            # Same cadence as the numeric lock reverify; a disagreeing
            # fresh read replaces the cache, a NO_READ keeps it.
            _cached_mineral = _locks.get("_mineral_name")
            _min_ctr = _scans_since_lock_verify.get("_mineral_name", 0) + 1
            _scans_since_lock_verify["_mineral_name"] = _min_ctr
            if (_cached_mineral is not None
                    and _min_ctr < _REVERIFY_THRESHOLD):
                result["mineral_name"] = _cached_mineral[0]
            else:
                _mineral_entry = _label_rows_for_validation.get("_mineral_row")
                if _mineral_entry is not None:
                    try:
                        _my1, _my2, _mlr = _mineral_entry
                        # Same structural band re-derivation as the full
                        # path. The raw _mineral_row entry comes from the
                        # color/projection detector and can sit on a
                        # label row — reverifying from THAT band CONFIRMS
                        # the very stale value this cadence exists to
                        # catch (live 2026-06-11: reverify read
                        # 'anne'->'Stannite' off a label row every cycle
                        # while the full path's paren-anchored band read
                        # 'ALUMINUMORE'->'Aluminum').
                        _mass_e2 = _label_rows_for_validation.get("mass")
                        _rb2 = _refine_mineral_band_above_mass(
                            img,
                            int(_mass_e2[0]) if _mass_e2 else None,
                        )
                        # X priority mirrors the full path: calibration
                        # box, else the refine's text-left edge, else
                        # full row width. The entry's own third element
                        # is the NUMERIC VALUE-COLUMN x (~mid-panel) —
                        # passing it cropped the name out of the OCR
                        # entirely, so the reverify read fragments of
                        # whatever sat right of mid-row.
                        try:
                            from . import calibration as _cal_mod2
                            _mbox2 = _cal_mod2.get_row(
                                region, "_mineral_row")
                        except Exception:
                            _mbox2 = None
                        if _mbox2 is not None:
                            try:
                                _mlr = max(0, int(_mbox2["x"]) + 20)
                            except Exception:
                                _mlr = 0
                        elif _rb2 is not None:
                            _mlr = _rb2[2]
                        else:
                            _mlr = 0
                        if _rb2 is not None:
                            _my1, _my2 = _rb2[0], _rb2[1]
                        _mname = _ocr_mineral_name(img, _my1, _my2, _mlr)
                        if _mname:
                            if (_cached_mineral is not None
                                    and _mname != _cached_mineral[0]):
                                log.info(
                                    "sc_ocr: mineral lock REVERIFY %r -> "
                                    "%r (stale cache replaced)",
                                    _cached_mineral[0], _mname,
                                )
                            _locks["_mineral_name"] = (_mname, None)
                            _scans_since_lock_verify["_mineral_name"] = 0
                            result["mineral_name"] = _mname
                            if _dbg is not None:
                                _dbg.set_ocr_text("mineral", _mname, [1.0])
                        elif _cached_mineral is not None:
                            # NO_READ — keep the cache, retry next cycle
                            result["mineral_name"] = _cached_mineral[0]
                    except Exception as _exc:
                        log.debug("mineral OCR fast-path failed: %s", _exc)
                        if _cached_mineral is not None:
                            result["mineral_name"] = _cached_mineral[0]
            elapsed_ms = (time.time() - t0) * 1000
            log.info(
                "sc_ocr: ALL LOCKED mineral=%s mass=%s resistance=%s instability=%s in %.0fms",
                result.get("mineral_name"),
                result["mass"], result["resistance"], result["instability"],
                elapsed_ms,
            )
            if _dbg is not None:
                try:
                    _dbg.write()
                except Exception as exc:
                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
                try:
                    _dbg.consume_capture_for_scan()
                except Exception as exc:
                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
            return result
        # If we reach here, a lock was invalidated — fall through to
        # the full per-field OCR loop below to re-establish reads.
        if _dbg is not None:
            try:
                _dbg.consume_capture_for_scan()
            except Exception as exc:
                log.debug("api: scan_hud_onnx swallowed: %s", exc)
        return result

    H, W = gray.shape

    # Use Tesseract label detection to find the EXACT positions of
    # MASS/RESISTANCE/INSTABILITY labels. This handles ANY rock type
    # (different mineral names shift the layout). 3 Tesseract calls
    # for label detection + 3 for values = ~300ms total, vs legacy's
    # 12-15 calls at 600ms+.
    # Run label-row detection through the helper that:
    #   * stashes ``region`` on the worker thread (so
    #     ``_find_label_rows``'s persistent-calibration AND manual-
    #     override branches can look it up), AND
    #   * applies ``column_x_offset`` uniformly to every row's
    #     value-column x.
    label_rows = _label_rows_for_region(region, img)
    if _dbg is not None:
        _dbg.set_image(img)
        _dbg.set_label_rows(label_rows)
        # Write the overlay immediately after label-row detection so
        # the viewer always reflects what the panel finder produced,
        # even if downstream OCR raises an exception. End-of-scan
        # write() below will overwrite with the fully-populated
        # version including OCR text + lock state.
        try:
            _dbg.write()
        except Exception as exc:
            log.debug("api: scan_hud_onnx swallowed: %s", exc)

    # ── Detect difficulty label once per scan ──
    # The EASY / MEDIUM / HARD / EXTREME / IMPOSSIBLE label is rendered
    # as a single large word below the instability row. Reading it
    # gives us a game-logic prior that bounds instability's valid
    # range — a correctly-placed 'EASY' tag means instability ≈ 0-25.
    # Reuses the full-panel Tesseract pass from _find_label_rows
    # (re-runs because that function doesn't expose the raw text).
    #
    # Per-rock cache: the 4 Tesseract subprocess calls below cost
    # ~200ms+ and difficulty cannot change without the rock changing
    # (which clears the cache via the same lifecycle that clears
    # _field_lock_cache — see _difficulty_cache definition above).
    # Cache hit returns the prior detection (including a None result,
    # so we don't retry 4 calls per tick on unreadable difficulty bars).
    if _rk in _difficulty_cache:
        _difficulty: Optional[str] = _difficulty_cache[_rk]
        _difficulty_cache.move_to_end(_rk)
    else:
        _difficulty = None
        try:
            from . import priors
            import pytesseract as _pt
            from ..screen_reader import _check_tesseract
            if _check_tesseract():
                # Try multiple Tesseract configurations — the difficulty
                # label is inside a colored progress bar (EASY = green,
                # HARD = red) that Tesseract doesn't always see at PSM 11.
                # PSM 6 (uniform block) + both polarities + two crop regions
                # catches more cases. First hit wins.
                #
                # ── Calibrated "needle" override ──
                # If the user locked a "needle" row in the calibration
                # dialog, honour that rectangle instead of the default
                # left-half crop. Same pattern as ``_signal_recognize_pil``
                # uses for the manual signature crop (line ~2293+):
                # check ``calibration.get_row(region, "needle")``, fall
                # through to the auto crop on miss. The crop is in
                # HUD-region-relative coordinates; we still account for
                # the panel-image upscale that happened at the top of
                # this function (img was resized to REF_H=541 when the
                # capture was smaller — the saved needle box references
                # the upscaled image_size, so no further scaling here).
                _left = None
                try:
                    from . import calibration as _ndl_cal
                    _ndl_box = _ndl_cal.get_row(region, "needle")
                except Exception:
                    _ndl_box = None
                if _ndl_box is not None:
                    try:
                        _nx = max(0, int(_ndl_box["x"]))
                        _ny = max(0, int(_ndl_box["y"]))
                        _nx2 = min(int(img.width), _nx + max(1, int(_ndl_box["w"])))
                        _ny2 = min(int(img.height), _ny + max(1, int(_ndl_box["h"])))
                        if _nx2 - _nx >= 8 and _ny2 - _ny >= 6:
                            _left = img.crop((_nx, _ny, _nx2, _ny2))
                            log.debug(
                                "sc_ocr: difficulty using calibrated needle "
                                "crop x=%d y=%d w=%d h=%d",
                                _nx, _ny, _nx2 - _nx, _ny2 - _ny,
                            )
                    except Exception as _ndl_exc:
                        log.debug(
                            "sc_ocr: needle crop failed (%s) — falling back "
                            "to auto difficulty crop", _ndl_exc,
                        )
                        _left = None
                if _left is None:
                    # Auto-fallback: full-WIDTH crop, skipping the top
                    # ~25% of the panel (the SCAN RESULTS title and
                    # mineral-name area). This focuses the difficulty
                    # detection on the M/R/I rows + the EASY/MEDIUM/
                    # HARD/EXTREME/IMPOSSIBLE bar, which is where the
                    # difficulty word lives. Wider than the legacy left-
                    # 55% crop (which cut IMPOSSIBLE in half on some
                    # renders) but narrower than full-panel (which
                    # included title/mineral noise the user didn't want
                    # in the calibration preview).
                    _y_top = int(img.height * 0.25)
                    _left = img.crop((0, _y_top, img.width, img.height))
                # Broadcast the difficulty crop to the calibration dialog
                # so the Needle row's preview shows what's being scanned —
                # whether the user has locked a needle box or is still
                # using the auto-fallback left-half. Mirrors the HUD
                # ``deliver_crop("mass", _vc)`` pattern. Best-effort:
                # broadcast failure must never affect OCR.
                try:
                    from . import live_broadcast as _bcast_ndl
                    _bcast_ndl.deliver_crop("needle", _left)
                except Exception as _bc_ndl_exc:
                    log.debug(
                        "sc_ocr: needle live_broadcast failed: %s",
                        _bc_ndl_exc,
                    )
                _left_gray = np.array(_left.convert("L"), dtype=np.uint8)
                _rgb = np.array(_left.convert("RGB"), dtype=np.uint8)
                _max_ch = _rgb.max(axis=2).astype(np.uint8)  # catches colored labels
                _thr = _otsu(_left_gray)
                _thr_c = _otsu(_max_ch)

                _variants = [
                    ("gray_bright_psm11", np.where(_left_gray > _thr, 0, 255).astype(np.uint8), "--psm 11"),
                    ("gray_bright_psm6",  np.where(_left_gray > _thr, 0, 255).astype(np.uint8), "--psm 6"),
                    ("max_bright_psm6",   np.where(_max_ch > _thr_c, 0, 255).astype(np.uint8), "--psm 6"),
                    ("gray_dark_psm11",   np.where(_left_gray < _thr, 0, 255).astype(np.uint8), "--psm 11"),
                ]
                for _name, _bw, _cfg in _variants:
                    try:
                        _t = _pt.image_to_string(Image.fromarray(_bw), config=_cfg)
                    except Exception:
                        continue
                    _d = priors.detect_difficulty(_t)
                    if _d:
                        _difficulty = _d
                        log.info(
                            "sc_ocr: difficulty detected=%r (via %s)",
                            _difficulty, _name,
                        )
                        break
                if _difficulty is None:
                    log.debug("sc_ocr: difficulty not detected (tried 4 variants)")
        except Exception as _exc:
            log.debug("sc_ocr: difficulty detection failed: %s", _exc)
        # Store the result (including None) so the next scan tick on
        # the same rock skips the 4 Tesseract calls. Invalidated when
        # the rock changes — see _difficulty_cache lifecycle above.
        _difficulty_cache[_rk] = _difficulty
        _difficulty_cache.move_to_end(_rk)
        if len(_difficulty_cache) > _CACHE_MAX:
            _difficulty_cache.popitem(last=False)

    # Fallback to fixed offsets from mineral row if label detection fails
    if not label_rows:
        mr_center = (mineral_row[0] + mineral_row[1]) // 2
        scale = H / 541
        _ROW_H = int(15 * scale)
        for field, off, lr in [("mass",43,110),("resistance",82,200),("instability",120,205)]:
            c = mr_center + int(off * scale)
            label_rows[field] = (max(0,c-_ROW_H), min(H,c+_ROW_H), int(lr*scale))

    fields = ["mass", "resistance", "instability"]

    # Per-field raw OCR value captured BEFORE the sticky-consensus
    # step. Used by the frozen-panel block at the end of this function
    # for two things:
    #   * Earlier freeze trigger — "first frame where all three fields
    #     have valid non-zero raw values" fires sooner than the old
    #     triple-lock-unanimity trigger, capturing the panel right
    #     when it finishes loading.
    #   * Divergence detection that bypasses sticky consensus — if the
    #     live OCR is repeatedly rejected by structural validators
    #     (raw_val=None) the prior sticky value would mask the
    #     disagreement; using raw_val directly means a persistent
    #     rejected state DOESN'T spuriously appear as "agreement with
    #     frozen", and a persistent fresh-but-different read correctly
    #     accumulates divergence streaks for auto-clear.
    # Values: None means "structural validator rejected this scan's
    # OCR" (no live signal); a float means a structurally valid read.
    _raw_per_field: dict[str, Optional[float]] = {
        "mass": None,
        "resistance": None,
        "instability": None,
    }

    for field in fields:
        # Locked-field fast path with self-invalidation.
        # We always compute the current value crop (cheap), save it
        # for the live viewer, and compare it against the stored
        # fingerprint that was in effect when the lock fired. If
        # the fingerprint similarity drops below
        # _LOCK_INVALIDATE_NCC, the panel content has changed under
        # us — drop the lock and fall through to full OCR.
        if field in _locks:
            _locked_val, _locked_fp = _locks[field]
            try:
                _entry = label_rows.get(field)
                _current_vc = None
                if _entry is not None:
                    _y1, _y2, _lr = _entry
                    if _y2 > _y1 and (_y2 - _y1) >= 6:
                        _current_vc = _find_value_crop(
                            img, gray, _y1, _y2,
                            x_min=max(0, _lr + 6),
                        )
                if _current_vc is not None:
                    try:
                        from . import live_broadcast as _bcast
                        _bcast.deliver_crop(field, _current_vc)
                    except Exception as exc:
                        log.debug("api: scan_hud_onnx swallowed: %s", exc)
                    _current_fp = _crop_fingerprint(_current_vc)
                    if _current_fp is not None and _locked_fp is not None:
                        _sim = float(np.dot(_current_fp, _locked_fp) / len(_current_fp))
                        if _sim < _LOCK_INVALIDATE_NCC:
                            log.info(
                                "sc_ocr: LOCK INVALIDATED field=%s "
                                "(crop drifted, NCC=%.2f < %.2f) — re-OCR",
                                field, _sim, _LOCK_INVALIDATE_NCC,
                            )
                            if _dbg is not None:
                                _dbg.set_lock(field, None, invalidated=True)
                            del _field_lock_cache[_rk][field]
                            # Also flush per-field consensus + crop
                            # buffers so a stale value doesn't lock
                            # back in immediately.
                            _RECENT_READS[field].clear()
                            _RECENT_CROPS[field].clear()
                            # Reset the reverify counter — the lock
                            # is gone so the counter has nothing left
                            # to verify against.
                            _scans_since_lock_verify[field] = 0
                            # NCC drift = rock changed under us. Drop
                            # the per-region difficulty cache so the
                            # difficulty block below re-detects.
                            _difficulty_cache.pop(_rk, None)
                            # Fall through to full OCR below
                            _locks = _field_lock_cache.get(_rk, {})
                        else:
                            # Lock is still valid — use it.
                            if field == "mass":
                                result["mass"] = _locked_val
                            elif field == "resistance":
                                result["resistance"] = _locked_val
                            elif field == "instability":
                                result["instability"] = _locked_val
                            if _dbg is not None:
                                _dbg.set_lock(field, _locked_val)
                                # Reconstruct crop box for overlay
                                try:
                                    _vc_w, _vc_h = _current_vc.size
                                    _ax_left = max(0, _lr + 6)
                                    _ax_right = min(img.width, _ax_left + _vc_w)
                                    _dbg.set_value_crop(
                                        field,
                                        (_ax_left, _y1, _ax_right, _y1 + _vc_h),
                                    )
                                except Exception as exc:
                                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
                            # Diagnostic-only OCR pass: when a viewer
                            # is watching the heartbeat, run the full
                            # per-field OCR so the Glyph Reader's
                            # per-glyph PNGs and voter rows update
                            # each scan. The result is DISCARDED — the
                            # pipeline still returns the locked value
                            # for stability — but the side-effect
                            # dumps inside _ocr_value_crop are what
                            # the user actually came to look at.
                            #
                            # Without this, locked fields freeze the
                            # Glyph Reader at whatever was on screen
                            # when the lock first established (often
                            # >5 minutes stale).
                            if _dbg is not None and _dbg.diagnostics_active():
                                try:
                                    _ocr_value_crop(_current_vc, field)
                                except Exception as _diag_exc:
                                    log.debug(
                                        "sc_ocr: diagnostic OCR pass "
                                        "failed for field=%s: %s",
                                        field, _diag_exc,
                                    )
                            continue
                else:
                    # Couldn't compute a current crop — emit a
                    # placeholder so the live viewer doesn't go stale
                    # while the lock is held. Without this, a locked
                    # field whose row geometry can't produce a clean
                    # crop freezes the dialog for the lock's lifetime.
                    try:
                        if _entry is not None:
                            _y1, _y2, _lr = _entry
                            if _y2 > _y1 and (_y2 - _y1) >= 4:
                                _placeholder = img.crop(
                                    (0, _y1, img.width, _y2),
                                )
                            else:
                                _placeholder = Image.new(
                                    "RGB", (200, 30), (40, 20, 20),
                                )
                        else:
                            _placeholder = Image.new(
                                "RGB", (200, 30), (40, 20, 20),
                            )
                        from . import live_broadcast as _bcast
                        _bcast.deliver_crop(field, _placeholder)
                    except Exception as exc:
                        log.debug("api: scan_hud_onnx swallowed: %s", exc)
                    if field == "mass":
                        result["mass"] = _locked_val
                    elif field == "resistance":
                        result["resistance"] = _locked_val
                    elif field == "instability":
                        result["instability"] = _locked_val
                    continue
            except Exception as _exc:
                log.debug(
                    "sc_ocr: lock-validation failed for %s: %s — "
                    "keeping lock", field, _exc,
                )
                # Even on exception, emit a placeholder so we know the
                # path is being reached.
                try:
                    _ph = Image.new("RGB", (200, 30), (60, 20, 20))
                    from . import live_broadcast as _bcast
                    _bcast.deliver_crop(field, _ph)
                except Exception as exc:
                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
                if field == "mass":
                    result["mass"] = _locked_val
                elif field == "resistance":
                    result["resistance"] = _locked_val
                elif field == "instability":
                    result["instability"] = _locked_val
                continue

        entry = label_rows.get(field)
        if entry is None:
            log.info("sc_ocr: field=%s MISSING from label_rows (panel layout?)", field)
            continue
        y1, y2, lr = entry

        # Sanity-check the row geometry. In fracture/extraction mode
        # the panel is positioned differently than ship-scan mode, and
        # fixed offsets from the mineral row can shoot past the image
        # bottom — returning (y1=547, y2=541) when image is only 541
        # tall. The full-row OCR path would then crop an empty strip
        # and Tesseract would hallucinate garbage like '¤- ¤8'.
        # Skip the field outright if the geometry is inverted or
        # degenerate (< 6 px tall).
        if y1 >= y2 or (y2 - y1) < 6 or y2 > img.height or y1 < 0:
            log.info(
                "sc_ocr: field=%s row geometry invalid y=%d-%d "
                "(img_h=%d) — skipping", field, y1, y2, img.height,
            )
            continue

        # Full-row OCR runs before _find_value_crop; the full-row path
        # has its own sanity checks and can still succeed when the
        # tight crop would fail. Compute value_crop for the slow-path
        # fallback but don't bail if it's None — row OCR may carry.
        #
        # CALIBRATION OVERRIDE: if the user locked a box for this
        # field, crop THAT box directly. Auto-detection via
        # _find_value_crop uses `lr + 6` as x_min, where `lr` is the
        # shared value_column_left across rows — this frequently
        # overshoots past the leading digit when the user's locked
        # box starts slightly to the left of the widest label's
        # colon position (e.g. instability "2.22" starting at x=193
        # when lr=196 → x_min=202 cuts off "2."). Respect the lock
        # verbatim when present; it's exactly what the calibration
        # dialog previewed.
        value_crop = None
        try:
            from . import calibration as _cal_mod
            # Trust the user's lock verbatim.  The only mutation we
            # apply is the explicit drift-correction (_cal_drift_y),
            # which is itself zero unless the user has saved the
            # _mineral_row anchor — i.e. opted in to drift tracking.
            #
            # NOTE: a previous "Tier-2 override" path used to silently
            # overwrite the saved Y with an NCC-detected Y whenever the
            # two disagreed by more than 25 px.  That defeated the
            # whole point of locking — the value/row positions kept
            # being re-detected scan-to-scan, so users saw their
            # carefully-placed boxes drift.  Removed.  If the panel
            # genuinely slid out of the locked region, the user
            # re-opens the calibration dialog and re-locks; that's
            # predictable, the override was not.
            _locked_box = _cal_mod.get_row(region, field, dy=_cal_drift_y)
        except Exception:
            _locked_box = None
        if _locked_box is not None:
            try:
                _bx = int(_locked_box["x"])
                _by = int(_locked_box["y"])
                _bw = int(_locked_box["w"])
                _bh = int(_locked_box["h"])
                _x0 = max(0, _bx)
                _y0 = max(0, _by)
                _x1 = min(img.width, _bx + _bw)
                _y1 = min(img.height, _by + _bh)
                if _x1 - _x0 >= 4 and _y1 - _y0 >= 6:
                    _candidate = img.crop((_x0, _y0, _x1, _y1))
                    # Sanity-check the lock against the actual pixels.
                    # Calibrated boxes can drift off-target when the
                    # panel slides inside the captured region. We
                    # require BOTH:
                    #
                    #   (a) the crop has digit-like ink density
                    #       (5-45 % of pixels above text threshold —
                    #       below 5 % means empty background, above
                    #       45 % means a solid block like the
                    #       difficulty bar);
                    #   (b) the bright pixels form ≥1 distinct
                    #       vertical column cluster (real digit
                    #       crops have at least 1 column-group of
                    #       ink; a row band with no digits has no
                    #       structured columns).
                    #
                    # If either fails, drop the lock for THIS scan
                    # only and let auto-detect run.
                    try:
                        _gc = np.asarray(_candidate.convert("L"), dtype=np.uint8)
                        if float(np.median(_gc)) > 130:
                            _gc = 255 - _gc
                        _bin = (_gc > 80).astype(np.uint8)
                        _area = max(1, _bin.size)
                        _density = float(_bin.sum()) / float(_area)
                        if not (0.05 <= _density <= 0.45):
                            log.info(
                                "sc_ocr: locked crop for %s has out-of-"
                                "range ink density %.3f — falling back "
                                "to auto-detect this scan",
                                field, _density,
                            )
                        else:
                            # Column-cluster check. Project bright
                            # pixels onto x-axis; count runs of
                            # ink-bearing columns. ≥1 run = at least
                            # one digit-shaped vertical band.
                            _col_proj = _bin.sum(axis=0) > 1
                            _runs = int(np.sum(
                                np.diff(_col_proj.astype(np.int8)) == 1
                            )) + (1 if _col_proj[0] else 0)
                            if _runs < 1:
                                log.info(
                                    "sc_ocr: locked crop for %s has no "
                                    "vertical column structure — "
                                    "falling back to auto-detect",
                                    field,
                                )
                            else:
                                # Right-edge overflow check. The
                                # calibrated box has FIXED width — if
                                # a longer value appears (e.g. mass
                                # locked for 4 digits then later reads
                                # 5-digit "30064"), the trailing
                                # digits overflow the lock and get
                                # silently truncated. The earlier
                                # density + column checks pass because
                                # the visible portion is still digit-
                                # like, just incomplete. Detect the
                                # overflow by inspecting the rightmost
                                # columns of the bin mask: if there's
                                # ink AT the right edge (vs. a clean
                                # blank gutter), the value extends
                                # past the lock — drop it for this
                                # scan and let _find_value_crop
                                # auto-detect the true bounds.
                                #
                                # User workaround: lock/unlock the row
                                # to force re-calibration. This check
                                # makes that workaround unnecessary.
                                _W = _col_proj.size
                                # Two ways the lock can clip the
                                # rightmost digit:
                                #
                                # (a) Bleed-through: ink reaches the
                                #     last few columns (a wide digit
                                #     overflows). Last 3 cols.
                                # (b) Amputation: a narrow trailing
                                #     digit (the SC font's `1` is
                                #     only ~6 px wide) sits ENTIRELY
                                #     past the right edge so the
                                #     gutter looks clean — but the
                                #     last detected ink-column is
                                #     suspiciously close to the edge,
                                #     hinting the next digit just
                                #     barely got cut. Flag if the
                                #     right gutter (clean cols after
                                #     the last ink) is <½ a typical
                                #     inter-digit gap.
                                #
                                # Either condition ⇒ drop the lock
                                # for this scan and let
                                # _find_value_crop auto-detect.
                                _edge_band = max(1, min(3, _W // 10))
                                _bleed_overflow = bool(
                                    _col_proj[-_edge_band:].any()
                                )
                                _amputation_overflow = False
                                _ink_idx = np.where(_col_proj)[0]
                                if _ink_idx.size > 0:
                                    _last_ink = int(_ink_idx[-1])
                                    _right_gutter = _W - 1 - _last_ink
                                    # Typical SC inter-digit gap is
                                    # ~4-6 px. If the gutter is
                                    # narrower than ~5 px, a thin
                                    # next-digit may have been
                                    # clipped just past the edge.
                                    if _right_gutter < 5:
                                        _amputation_overflow = True
                                if _bleed_overflow or _amputation_overflow:
                                    log.info(
                                        "sc_ocr: locked crop for %s "
                                        "looks truncated on the "
                                        "right (bleed=%s amputate=%s "
                                        "gutter=%dpx) — falling back "
                                        "to auto-detect",
                                        field, _bleed_overflow,
                                        _amputation_overflow,
                                        (_W - 1 - int(_ink_idx[-1])
                                         if _ink_idx.size else -1),
                                    )
                                else:
                                    value_crop = _candidate
                    except Exception:
                        # On any sanity-check failure, accept the
                        # lock anyway — better than dropping a real
                        # crop on a transient numpy hiccup.
                        value_crop = _candidate
            except Exception:
                value_crop = None
        if value_crop is None:
            value_crop = _find_value_crop(img, gray, y1, y2, x_min=max(0, lr + 6))
        # Per-field value-crop debug dump for the live glyph viewer.
        # Lets us see the EXACT input the segmenter sees per field, so
        # we can distinguish "wrong crop bounds" (digits already gone)
        # from "good crop, bad downstream" failures.
        _dump_value_crop(field, value_crop)
        # Telemetry: record the value crop box for the debug overlay.
        if _dbg is not None and value_crop is not None:
            try:
                _vc_w, _vc_h = value_crop.size
                # value_crop is cropped from img; we need its position
                # in img coords. Reconstruct via the bounds used
                # inside _find_value_crop (x_min + small offset, y1).
                # For overlay purposes, the right-anchored crop ends
                # near img.width and starts at img.width - _vc_w.
                # Use a heuristic: pick centered around shared lr.
                _approx_x_left = max(0, lr + 6)
                _approx_x_right = min(img.width, _approx_x_left + _vc_w)
                _dbg.set_value_crop(
                    field, (_approx_x_left, y1, _approx_x_right, y1 + _vc_h),
                )
            except Exception as exc:
                log.debug("api: scan_hud_onnx swallowed: %s", exc)
        if value_crop is None:
            # _find_value_crop failed (often happens when the value
            # is a single thin digit like "1" that doesn't meet the
            # _MIN_VALUE_WIDTH cluster filter). Save the full row
            # strip to BOTH the diagnostic file AND the live-viewer
            # crop file so the viewer reflects current geometry
            # instead of going stale.
            try:
                from . import debug_overlay as _dbg_gate
                if _dbg_gate.is_tag_active("crops"):
                    _debug_row = img.crop((0, max(0, y1 - 2), img.width, min(img.height, y2 + 2)))
                    _debug_row.save(f"debug_row_{field}_failed.png")
                # Crop the value column area (right of label) so the
                # live viewer shows what the OCR was looking at.
                _vc_left = max(0, lr + 6)
                _vc_right = min(img.width, _vc_left + int(img.width * 0.30))
                if _vc_right > _vc_left:
                    _row_crop = img.crop(
                        (_vc_left, max(0, y1 - 2), _vc_right, min(img.height, y2 + 2)),
                    )
                    from . import live_broadcast as _bcast
                    _bcast.deliver_crop(field, _row_crop)
            except Exception as exc:
                log.debug("api: scan_hud_onnx swallowed: %s", exc)
            log.info(
                "sc_ocr: field=%s value_crop is None "
                "(y=%d-%d x_lr=%d saved debug_row_%s_failed.png)",
                field, y1, y2, lr, field,
            )
            continue

        # Push successful crops to live viewers on EVERY scan: the
        # in-process broadcast feeds the calibration dialog instantly,
        # the gated disk write feeds cross-process viewers
        # (scripts/live_crop_viewer.py) when their heartbeat is fresh.
        try:
            from . import live_broadcast as _bcast
            _bcast.deliver_crop(field, value_crop)
        except Exception as exc:
            log.debug("api: scan_hud_onnx swallowed: %s", exc)

        # Capture every value crop to a pending/ buffer for later manual
        # labeling + retraining. Rate-limited internally to ~5 s per
        # field, so the hot path stays cheap.
        try:
            from ..training_collector import save_pending_crop
            save_pending_crop(value_crop, field)
        except Exception as exc:
            log.debug("api: scan_hud_onnx swallowed: %s", exc)

        # ── Value-crop OCR (PRIMARY — custom CNN inside) ──
        # Run the value-crop path FIRST. The user-trained 28×28 CNN
        # has 99% val_acc on real SC HUD glyphs and is wired as the
        # top voter inside _ocr_value_crop. Only fall back to the
        # full-row Tesseract path when the value-crop OCR returns
        # nothing or doesn't validate.
        text, confs = _ocr_value_crop(value_crop, field=field)
        _valid_primary = None
        if text:
            if field == "mass":
                _valid_primary = validate.validate_mass(text)
            elif field == "resistance":
                _valid_primary = validate.validate_pct(text)
            elif field == "instability":
                _valid_primary = validate.validate_instability(text, confidences=confs)
        if _valid_primary is None:
            # Primary failed — try full-row Tesseract as fallback.
            row_text, row_confs = _ocr_full_row(img, y1, y2, field)
            if row_text:
                _valid_row = None
                if field == "mass":
                    _valid_row = validate.validate_mass(row_text)
                elif field == "resistance":
                    _valid_row = validate.validate_pct(row_text)
                elif field == "instability":
                    _valid_row = validate.validate_instability(row_text, confidences=row_confs)
                if _valid_row is not None:
                    log.debug(
                        "sc_ocr: PRIMARY failed for %s, using row-fallback %r",
                        field, row_text,
                    )
                    text, confs = row_text, row_confs
        if not text:
            log.info("sc_ocr: field=%s ocr returned empty text", field)
            continue

        log.info("sc_ocr raw %s: text=%r confs=%s", field, text,
                 [f"{c:.2f}" for c in confs[:8]])
        if _dbg is not None:
            _dbg.set_ocr_text(field, text, confs)
        if field == "mass":
            raw_val = validate.validate_mass(text)
        elif field == "resistance":
            raw_val = validate.validate_pct(text)
        elif field == "instability":
            raw_val = validate.validate_instability(text, confidences=confs)
        else:
            raw_val = None

        # ── Game-logic priors + NCC template fallback ──
        # If the voted value contradicts game knowledge (e.g. EASY
        # difficulty but instability=278), try the NCC template voter
        # as a fourth opinion. Templates are deterministic — 100%
        # accurate when the font matches — so they're the right
        # tiebreaker when all three neural/heuristic engines disagree
        # with the rock's observed difficulty.
        try:
            from . import priors as _priors
            _ctx = {"difficulty": _difficulty} if _difficulty else {}

            # ── Proactive decimal recovery for instability ──
            # Most mineable rocks have instability in the 0-30 range,
            # with rare edge cases up to ~200. A raw read ≥ 30 that
            # contains NO decimal point almost certainly lost one —
            # e.g. `4.65` → `465`, `12.10` → `1210`. We try this BEFORE
            # the plausibility check so we don't depend on difficulty
            # detection (which misses when the EASY/MEDIUM bar has
            # non-standard polarity, e.g. white text on green).
            if (field == "instability"
                    and raw_val is not None
                    and float(raw_val) >= 30.0
                    and "." not in (text or "")):
                _recovered = _priors.try_decimal_recovery(field, text, _ctx)
                if _recovered is not None and 0.0 <= _recovered <= 200.0:
                    log.info(
                        "sc_ocr: proactive-decimal-recover field=%s "
                        "raw=%r orig_val=%s -> %s",
                        field, text, raw_val, _recovered,
                    )
                    raw_val = _recovered

            _ok = True
            if raw_val is not None:
                _ok, _reason = _priors.is_plausible(field, float(raw_val), _ctx)
                if not _ok:
                    # Second-chance decimal recovery when priors reject
                    # (e.g. difficulty IS detected and bounds say value
                    # is out-of-range).
                    _recovered = _priors.try_decimal_recovery(field, text, _ctx)
                    if _recovered is not None:
                        log.info(
                            "sc_ocr: prior-decimal-recover field=%s "
                            "raw=%r rejected_val=%s -> %s",
                            field, text, raw_val, _recovered,
                        )
                        raw_val = _recovered
                        _ok = True
                    else:
                        log.info(
                            "sc_ocr: prior-reject field=%s val=%s (%s) — "
                            "trying NCC templates",
                            field, raw_val, _reason,
                        )
            if (raw_val is None) or (not _ok):
                # Template-voter fallback
                try:
                    from .. import templates_furore as _tf
                    _ttext, _tconfs = _tf.match_value_crop(value_crop)
                    if _ttext:
                        _mean = sum(_tconfs) / len(_tconfs) if _tconfs else 0.0
                        if field == "mass":
                            _tv = validate.validate_mass(_ttext)
                        elif field == "resistance":
                            _tv = validate.validate_pct(_ttext)
                        elif field == "instability":
                            _tv = validate.validate_instability(_ttext, confidences=_tconfs)
                        else:
                            _tv = None
                        _t_ok = False
                        if _tv is not None:
                            _t_ok, _ = _priors.is_plausible(field, float(_tv), _ctx)
                        log.info(
                            "sc_ocr: templates field=%s text=%r val=%s "
                            "mean=%.2f plausible=%s",
                            field, _ttext, _tv, _mean, _t_ok,
                        )
                        if _tv is not None and _t_ok and _mean >= 0.55:
                            raw_val = _tv
                except Exception as _texc:
                    log.debug("sc_ocr: template voter failed: %s", _texc)
        except Exception as _pexc:
            log.debug("sc_ocr: priors check failed: %s", _pexc)

        # ── Per-rock monotonicity gate ──
        # The HUD's three numeric fields evolve in predictable ways
        # while the user mines a single rock:
        #   * mass strictly decreases (rock loses material);
        #   * resistance is a constant rock property;
        #   * instability fluctuates freely (exempt).
        # A new OCR candidate that violates these constraints is almost
        # certainly an OCR error (typical failure mode: a stray leading
        # digit splice that explodes magnitude, e.g. 15683 → 21449).
        # We reject such candidates BEFORE they enter the consensus
        # buffer so they cannot influence the stable value. The check
        # only fires once a lock has actually been established
        # (locked_count >= 2) — first reads on a fresh rock always pass.
        if raw_val is not None:
            try:
                from . import monotonicity as _mono
                _locked_entry = _locks.get(field)
                _v_locked: Optional[float] = (
                    float(_locked_entry[0]) if _locked_entry else None
                )
                # When a lock exists it was acquired via the
                # all-of-_LOCK_WINDOW unanimity rule, so the effective
                # locked-count is _LOCK_WINDOW. Without a lock the
                # gate is a no-op and the count value is irrelevant.
                _locked_count = _LOCK_WINDOW if _v_locked is not None else 0
                _mono_ok, _mono_why = _mono.is_monotonically_plausible(
                    field, float(raw_val), _v_locked, _locked_count,
                )
                if not _mono_ok:
                    log.info(
                        "sc_ocr: MONOTONICITY REJECT field=%s "
                        "v_new=%s v_locked=%s (%s)",
                        field, raw_val, _v_locked, _mono_why,
                    )
                    # Drop the candidate — do NOT push into consensus.
                    raw_val = None
            except Exception as _mexc:
                log.debug(
                    "sc_ocr: monotonicity check failed: %s", _mexc,
                )

        # ── Structural validators ──
        # Field-level format constraints that catch misreads the
        # per-glyph classifier slipped through. These are panel-
        # design invariants from the game UI:
        #
        #   * instability ALWAYS shows a decimal point (e.g. 340.17,
        #     15.46, 5.23) — when the OCR text has no "." the read
        #     is almost certainly a fragment of the real value (e.g.
        #     "340.17" mis-cropped to just "17"). Drop it.
        #   * resistance is ALWAYS 0-100% — values >100 are misreads.
        #     We do NOT require "%" in the text because the N-way
        #     consensus voter strips trailing non-digits, so a valid
        #     read of "82%" arrives here as text="82" without the
        #     percent sign. Earlier attempts to require "%" in text
        #     blocked legitimate consensus outputs from updating the
        #     sticky stable value, keeping stale bad values pinned
        #     in place forever.
        #   * mass values in SC mining HUD are at minimum 2 digits
        #     (typical range 100s-100,000s). Single-digit "1"/"2"
        #     reads come from oversized row crops where the
        #     binarizer fused all digits into one blob and the
        #     proportional segmenter forced N=1 or N=2 boxes —
        #     each "box" then classifies as a single best-guess
        #     character. Real masses never look like that.
        #
        # Rejecting here BEFORE the consensus push means the value
        # never enters the rolling buffer, so it can't satisfy the
        # all-of-N unanimity gate that drives the lock cache, which
        # means it also can't trigger an auto-freeze. The whole
        # downstream pipeline stays clean.
        if raw_val is not None:
            if field == "instability":
                # Instability values are always decimals (e.g. 29.21,
                # 15.46, 340.17). The OCR can produce text with or
                # without a "." depending on whether the dot glyph
                # was segmented:
                #   * "29.21" → 29.21              ← dot in text, OK
                #   * "2921"  → 29.21 (recovered)  ← dot dropped, OK
                #   * "17"    → 17.0               ← integer, NOT OK
                # We reject only the third case: raw text has no "."
                # AND the post-recovery value is an integer. Recovery
                # always produces a non-integer (it divides by 100),
                # so if val is still an integer, recovery didn't run
                # — the read is structurally bad (probably a 2-digit
                # mis-crop of a longer value).
                _text_has_dot = "." in (text or "")
                _val_is_integer = False
                try:
                    _val_is_integer = float(raw_val).is_integer()
                except (TypeError, ValueError):
                    _val_is_integer = False
                if not _text_has_dot and _val_is_integer:
                    log.warning(
                        "sc_ocr: STRUCTURAL REJECT field=instability "
                        "text=%r val=%s — instability is always a "
                        "decimal; integer reads with no '.' in source "
                        "text indicate a mis-segmented fragment of a "
                        "longer value (e.g. '340.17' cropped to '17')",
                        text, raw_val,
                    )
                    raw_val = None
            elif field == "resistance":
                if float(raw_val) > 100.0:
                    log.warning(
                        "sc_ocr: STRUCTURAL REJECT field=resistance "
                        "val=%s — resistance is always 0-100; "
                        "treating as misread",
                        raw_val,
                    )
                    raw_val = None
            elif field == "mass":
                _digit_count = sum(
                    1 for c in (text or "") if c.isdigit()
                )
                if _digit_count < 2:
                    log.warning(
                        "sc_ocr: STRUCTURAL REJECT field=mass "
                        "text=%r val=%s — mass values are at least "
                        "2 digits (real range typically 100+); "
                        "single-digit reads come from oversized row "
                        "crops + fused-blob binarization, treating "
                        "as misread",
                        text, raw_val,
                    )
                    raw_val = None

        # Capture the post-structural-validator raw value (may be
        # None if rejected). The frozen-panel block at the end of
        # this function uses this rather than the sticky consensus
        # value so a persistent fresh disagreement triggers
        # divergence auto-clear instead of being masked.
        try:
            _raw_per_field[field] = (
                float(raw_val) if raw_val is not None else None
            )
        except (TypeError, ValueError):
            _raw_per_field[field] = None

        if field == "mass":
            result["mass"] = _consensus_value("mass", raw_val)
        elif field == "resistance":
            result["resistance"] = _consensus_value("resistance", raw_val)
        elif field == "instability":
            result["instability"] = _consensus_value("instability", raw_val)

        # ── Persistence-bias override ──
        # After consensus picks a value to display, check whether the
        # field is in a "sticky" state (≥_PERSIST_STREAK_MIN equal
        # reads in a row). If so AND the consensus's pick disagrees
        # with the sticky value, demand strong evidence — high
        # confidence OR multi-source agreement — before allowing the
        # flip. Without that evidence the sticky value persists. The
        # underlying consensus buffer was still updated (so a real
        # change accumulates over time), only the DISPLAY is held.
        try:
            _consensus_pick = result.get(field)
            if _consensus_pick is not None and raw_val is not None:
                # Mean confidence from the primary OCR pass.
                try:
                    _mean_conf = (
                        float(sum(confs) / len(confs)) if confs else 0.0
                    )
                except Exception:
                    _mean_conf = 0.0
                # Voter agreement: how many entries in the current
                # rolling read buffer match v_new? The lock window is
                # 3, so a value of 2 means two separate frames have
                # produced the same number — the cross-frame analogue
                # of the spec's "≥2 of 3 voters" requirement.
                _buf = _RECENT_READS.get(field)
                _voter_count = 0
                if _buf is not None:
                    try:
                        _voter_count = sum(
                            1
                            for v in _buf
                            if v is not None
                            and float(v) == float(raw_val)
                        )
                    except Exception:
                        _voter_count = 0
                _display_new, _persist_reason = _persistence_check(
                    field, float(_consensus_pick),
                    _mean_conf, _voter_count,
                )
                if not _display_new:
                    _sticky_val = _PERSIST_LAST.get(field)
                    log.info(
                        "sc_ocr: PERSISTENCE field=%s holding sticky "
                        "v=%s (rejected v_new=%s conf=%.2f voters=%d)",
                        field, _sticky_val, _consensus_pick,
                        _mean_conf, _voter_count,
                    )
                    if _sticky_val is not None:
                        if field == "mass":
                            result["mass"] = _sticky_val
                        elif field == "resistance":
                            result["resistance"] = _sticky_val
                        elif field == "instability":
                            result["instability"] = _sticky_val
            # Update the streak counter based on what's ACTUALLY being
            # displayed (post-override). A held sticky bumps the
            # counter; a confirmed flip resets it to 1.
            _persistence_track_streak(field, result.get(field))
        except Exception as _persist_exc:
            log.debug(
                "sc_ocr: persistence check failed: %s", _persist_exc,
            )

        # ── Push crop fingerprint into the per-field buffer ──
        # Used by the pre-lock verifier to confirm the underlying
        # crop pixels are stable across the lock window (catches the
        # "row jumped to a progress bar" case where OCR text might
        # coincidentally match but the crop content is unrelated).
        _RECENT_CROPS[field].append(_crop_fingerprint(value_crop))

        # ── Strict lock gate ──
        # Two independent checks must both pass:
        #   (1) ALL N reads in the window agreed on the same value
        #       (much stricter than _consensus_value's 2-of-3 — a
        #       coincidental misread can satisfy 2-of-3 but rarely
        #       satisfies all-of-N).
        #   (2) Mean pairwise NCC of the last N CROP IMAGES is ≥
        #       _LOCK_CROP_NCC_MIN — the row crop has been visually
        #       stable, not jumping between targets.
        # If either fails, no lock this frame; we keep evaluating
        # next scan with the rolling window.
        if field not in _locks:
            _unanimous = _value_buffer_unanimous(field)
            _crop_ok, _crop_sim = _crop_buffer_consistent(field)
            _consistency_reflex(field, _unanimous, _crop_ok, _crop_sim)
            _displayed = result.get(field)
            if (
                _unanimous is not None
                and _displayed is not None
                and float(_unanimous) == float(_displayed)
                and _crop_ok
            ):
                # Store the most recent crop fingerprint alongside
                # the value; lock self-invalidation compares against
                # this on subsequent scans to detect drift.
                _fp = _crop_fingerprint(value_crop)
                _locks_for_region = _field_lock_cache.setdefault(_rk, {})
                _locks_for_region[field] = (float(_unanimous), _fp)
                _field_lock_cache.move_to_end(_rk)
                if len(_field_lock_cache) > _CACHE_MAX:
                    _field_lock_cache.popitem(last=False)
                # Reset the periodic reverify counter — we'll start
                # counting from zero scans-since-lock and trigger a
                # forced re-OCR every _REVERIFY_THRESHOLD scans (see
                # ALL LOCKED fast-path).
                _scans_since_lock_verify[field] = 0
                log.info(
                    "sc_ocr: LOCKED field=%s value=%s "
                    "(unanimous %d/%d frames, crop-NCC=%.2f)",
                    field, _unanimous, _LOCK_WINDOW, _LOCK_WINDOW, _crop_sim,
                )
                if _dbg is not None:
                    _dbg.set_lock(field, float(_unanimous))
            else:
                log.debug(
                    "sc_ocr: lock-gate field=%s unanimous=%s "
                    "crop_ok=%s crop_sim=%.2f (need %.2f)",
                    field, _unanimous, _crop_ok, _crop_sim,
                    _LOCK_CROP_NCC_MIN,
                )

    # ── Mineral name (placeholder Tesseract → snap to KNOWN_MINERALS) ──
    # The mineral row is surfaced under the "_mineral_row" key by
    # _find_label_rows_by_position. Tesseract is the placeholder OCR
    # here; when the SC alphabet CNN is trained, we swap it inside
    # _ocr_mineral_name. The fuzzy snap to KNOWN_MINERALS keeps the
    # final read clean regardless of OCR quality.
    _mineral_entry = label_rows.get("_mineral_row")
    if _mineral_entry is not None:
        try:
            _my1, _my2, _mlr = _mineral_entry
            # The shared value_column_left `_mlr` is derived from the
            # NUMERIC rows (mass/resistance/instability label ends), which
            # sit ~180 px into the panel — way to the right of where the
            # mineral name text starts (usually x=11). Passing that lr into
            # _ocr_mineral_name crops from x=_mlr-20, which chops off the
            # entire mineral name and leaves only the trailing ")" or
            # "(ORE)" — hence garbage reads like 'Elo' / 'PR' and a
            # dropped mineral_name (user 2026-06-02).
            #
            # Prefer, in order of authority:
            #   1. the user's locked calibration box (explicit), else
            #   2. the COLOR DETECTOR's text-left edge (automatic, found
            #      every scan the mineral row is detected) — this is the
            #      fix that makes the common, un-calibrated case work, else
            #   3. the numeric _mlr (last resort).
            if _mineral_text_x is not None:
                _mlr = max(0, int(_mineral_text_x))
            try:
                from . import calibration as _cal_mod
                _mineral_box = _cal_mod.get_row(region, "_mineral_row")
            except Exception:
                _mineral_box = None
            if _mineral_box is not None:
                try:
                    _mlr = max(0, int(_mineral_box["x"]) + 20)
                except Exception as exc:
                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
            # STRUCTURAL BAND REFINE: the name is the bottom-most text
            # line above the MASS row — re-derive the band from that
            # anchor so a mis-locked color/projection band (title
            # included, or composition rows below) can't reach the OCR.
            # The user's explicit calibration X keeps priority.
            _mass_entry_for_name = label_rows.get("mass")
            _refined_band = _refine_mineral_band_above_mass(
                img,
                int(_mass_entry_for_name[0])
                if _mass_entry_for_name else None,
            )
            if _refined_band is not None:
                _ry1, _ry2, _rxl = _refined_band
                if (_ry1, _ry2) != (_my1, _my2):
                    log.info(
                        "sc_ocr: mineral band refined (%d,%d)->(%d,%d) "
                        "x_left=%d (bottom-line-above-MASS)",
                        _my1, _my2, _ry1, _ry2, _rxl,
                    )
                _my1, _my2 = _ry1, _ry2
                if _mineral_box is None:
                    _mlr = _rxl
            _mineral_name = _ocr_mineral_name(img, _my1, _my2, _mlr)
            result["mineral_name"] = _mineral_name
            if _mineral_name:
                # Seed/replace the ALL-LOCKED mineral cache with this
                # full-path read. The cache used to be written ONLY by
                # the fast-path reverify, so a correct full-path read
                # never displaced a stale cache and the published name
                # flapped between the two paths (live 2026-06-11:
                # 'Aluminum' on full scans, locked 'Stannite' on
                # ALL-LOCKED scans of the same rock).
                try:
                    _mlk = _field_lock_cache.get(_region_key(region))
                    if _mlk is not None:
                        _prev_m = _mlk.get("_mineral_name")
                        if (_prev_m is not None
                                and _prev_m[0] != _mineral_name):
                            log.info(
                                "sc_ocr: mineral cache %r -> %r "
                                "(full-path read replaces stale)",
                                _prev_m[0], _mineral_name,
                            )
                        _mlk["_mineral_name"] = (_mineral_name, None)
                        _scans_since_lock_verify["_mineral_name"] = 0
                except Exception as exc:
                    log.debug("api: scan_hud_onnx swallowed: %s", exc)
            if _dbg is not None and _mineral_name:
                _dbg.set_ocr_text("mineral", _mineral_name, [1.0])
            # Save the mineral row crop so the calibration dialog can
            # display it as a live preview.
            #
            # The mineral name (e.g. "ALUMINUM (ORE)") sits on the
            # LEFT side of the row — NOT in the value column. So we
            # crop the FULL row width starting from the panel's left
            # margin (matches where MASS row content begins). This
            # ensures the entire mineral name is visible in the
            # preview, not just the trailing parenthesis.
            try:
                if _my2 > _my1 and img.width > 0:
                    _mineral_crop = img.crop((0, _my1, img.width, _my2))
                    from . import live_broadcast as _bcast
                    _bcast.deliver_crop("_mineral_row", _mineral_crop)
            except Exception as exc:
                log.debug("api: scan_hud_onnx swallowed: %s", exc)
        except Exception as _mexc:
            log.debug("sc_ocr: mineral name read failed: %s", _mexc)

    # ── Frozen panel reference (auto-freeze + frozen-wins + 3s clear) ──
    # The first time all three numeric fields lock simultaneously,
    # snapshot the captured ``img`` and the locked values. Subsequent
    # scans publish the frozen values rather than re-running consensus
    # against jiggling live frames. The freeze auto-clears if the SCAN
    # RESULTS title hasn't been detected for 3 seconds (rock left view).
    #
    # Skipped entirely on recursive calls from the snapshot re-OCR
    # path — that pass just wants raw OCR output, not freeze-state
    # bookkeeping (avoids infinite recursion + spurious divergence
    # events from the snapshot's own deterministic reads).
    if _suppress_freeze:
        elapsed_ms = (time.time() - t0) * 1000
        log.debug(
            "sc_ocr (suppress_freeze): mineral=%s mass=%s "
            "resistance=%s instability=%s in %.0fms",
            result.get("mineral_name"),
            result.get("mass"), result.get("resistance"),
            result.get("instability"), elapsed_ms,
        )
        return result
    try:
        from . import frozen_panel as _fp
        _frozen = _fp.get_frozen_ref(region)
        # ── Debug toggle: SC_HUD_NO_FREEZE ──
        # When set, disable the auto-freeze / frozen-publish entirely:
        # every scan publishes the LIVE read instead of a locked snapshot.
        # Useful for watching raw per-frame reads while debugging (the
        # freeze otherwise holds a stable value and ignores flaky live
        # frames). NOT for normal play — the freeze is what suppresses
        # value flicker. Mirrors the _suppress_freeze early-return.
        import os as _os_nofreeze
        if _os_nofreeze.environ.get("SC_HUD_NO_FREEZE"):
            try:
                if _frozen.is_frozen:
                    _frozen.clear()
            except Exception:
                pass
            return result
        _rk_frozen = _region_key(region)
        _title_seen_this_scan = bool(
            label_rows and (
                "mass" in label_rows
                or "resistance" in label_rows
                or "instability" in label_rows
                or "_mineral_row" in label_rows
            )
        )
        if _title_seen_this_scan:
            _frozen.refresh_title_seen()
        # ── UI-freshness gate (label-presence streak) ──
        # Count how many of the three numeric label rows
        # (mass / resistance / instability) matched this scan and feed
        # it to the frozen ref's streak tracker. Two consecutive
        # zero-label scans → "panel definitely gone": clear the freeze
        # AND the sticky field-value lock cache so the UI surfaces
        # "scanning..." instead of stale numbers. Three consecutive
        # ≤1-label scans → "panel mostly gone": shorten the freeze age
        # tolerance from 3s to ~1s so we don't publish 30s-old data
        # through a partial occlusion.
        #
        # This complements the existing 3s ``is_expired`` timeout —
        # both gates run every scan and either can fire, but the
        # label-streak gate has a stronger signal (zero labels for two
        # ticks ≈ the panel really left the screen) so it fires sooner.
        _label_match_count = 0
        if label_rows:
            for _label_key in ("mass", "resistance", "instability"):
                if label_rows.get(_label_key) is not None:
                    _label_match_count += 1
        _freshness = _frozen.record_label_match_count(_label_match_count)
        _freshness_timeout = 3.0
        if _freshness.get("action") == "clear":
            log.info(
                "sc_ocr.hud: UI-freshness — %d consecutive scans with "
                "%d/3 labels matched -> clear",
                _freshness.get("zero_label_streak", 0),
                _label_match_count,
            )
            _frozen.clear()
            # Also wipe the per-region sticky consensus caches so the
            # next visible scan starts fresh — otherwise locked field
            # values would survive the freeze clear and immediately
            # re-publish stale numbers via the all-locked fast path.
            try:
                _field_lock_cache.pop(_rk_frozen, None)
            except Exception as exc:
                log.debug("api: scan_hud_onnx swallowed: %s", exc)
            # Surface the absence to the caller. The downstream
            # "frozen wins" override is skipped because the ref is now
            # unfrozen, and we explicitly null out the numeric fields
            # so consumers see "scanning..." rather than whatever the
            # live OCR returned for an empty-label scan.
            result = dict(result)
            for _f in ("mass", "resistance", "instability"):
                result[_f] = None
            result["mineral_name"] = None
            result["panel_visible"] = False
            result["frozen"] = False
        elif _freshness.get("action") == "shorten_tolerance":
            _tol = _freshness.get("tolerance_sec")
            if isinstance(_tol, (int, float)) and _tol > 0:
                _freshness_timeout = float(_tol)
                log.info(
                    "sc_ocr.hud: UI-freshness — %d consecutive scans "
                    "with %d/3 labels matched -> shorten_tolerance(%.1fs)",
                    _freshness.get("low_label_streak", 0),
                    _label_match_count,
                    _freshness_timeout,
                )
        # Auto-clear on title-absent (3s by default, shortened by the
        # UI-freshness gate above when the panel is mostly gone). Check
        # this BEFORE the auto-freeze attempt so a stale freeze can't
        # suppress a fresh consensus on a new rock.
        if _frozen.is_expired(timeout_sec=_freshness_timeout):
            log.info(
                "frozen_panel: title absent %.1fs (tolerance=%.1fs) "
                "— clearing frozen reference",
                _frozen.time_since_title_seen(), _freshness_timeout,
            )
            _frozen.clear()
        # Divergence-based auto-clear: feed each live OCR reading
        # back into the frozen ref so it can count consecutive
        # disagreements. We use _raw_per_field (the post-structural-
        # validator raw value) rather than result[field] (the sticky
        # consensus output). The sticky consensus returns the LAST
        # STABLE VALUE when the current scan's read was rejected,
        # which means a persistent live failure looks identical to
        # an agreement — divergence never fires. Reading raw_val
        # makes "rejected by validator" (None, no signal) distinct
        # from "successfully read a different value" (float, counts
        # as divergence).
        if _frozen.is_frozen:
            for _f in ("mass", "resistance", "instability"):
                _raw_live = _raw_per_field.get(_f)
                if _raw_live is not None:
                    _frozen.record_live_reading(_f, _raw_live)
                    # If the divergence trigger fired, the ref is no
                    # longer frozen — break out, the freeze-trigger
                    # block below will look at the new unfrozen state.
                    if not _frozen.is_frozen:
                        break
            # All-None auto-clear: divergence can't fire when every
            # field returned None (no value to disagree with), but a
            # sustained inability to read ANY field means the freeze
            # has lost touch with reality (panel changed / row geometry
            # drift / new rock the current calibration can't handle).
            # ``record_scan_outcome`` counts consecutive all-None
            # scans and clears once the streak crosses the threshold.
            if _frozen.is_frozen:
                _any_field_read = any(
                    _raw_per_field.get(_f) is not None
                    for _f in ("mass", "resistance", "instability")
                )
                _frozen.record_scan_outcome(any_field_read=_any_field_read)
        # Auto-freeze: fire on the FIRST scan where all three numeric
        # fields produce valid non-zero raw OCR readings (earlier than
        # the previous triple-lock-unanimity trigger). The panel
        # briefly shows zeros / placeholders while the game computes
        # the values, then transitions to real numbers — the first
        # frame with non-zero values is the cleanest snapshot we'll
        # get, free of subsequent jiggle. Structural validators have
        # already filtered out implausible reads (instab without ".",
        # mass < 2 digits, resist > 100), so any non-zero raw value
        # here is structurally plausible.
        #
        # If the trigger fires on a moment that's actually wrong, the
        # divergence-based auto-clear above will catch it within 3
        # consecutive disagreement frames and re-freeze on a better
        # moment.
        if not _frozen.is_frozen:
            _mass_raw = _raw_per_field.get("mass")
            _resist_raw = _raw_per_field.get("resistance")
            _instab_raw = _raw_per_field.get("instability")
            # Require all three structurally valid AND mass/instab
            # non-zero. Resistance is allowed to be 0 (some low-grade
            # rocks have 0% resistance, although uncommon).
            _trigger_ok = (
                _mass_raw is not None
                and _resist_raw is not None
                and _instab_raw is not None
                and float(_mass_raw) > 0.0
                and float(_instab_raw) > 0.0
            )
            if _trigger_ok:
                # Annotate the snapshot with OCR overlay (cyan row
                # bands + per-field value labels + FROZEN watermark)
                # so the panel-finder UI shows the "second scanner"
                # visualization — what the OCR pipeline thinks the
                # static frozen image says, laid out on top of the
                # snapshot. The annotation uses the same label_rows
                # the live OCR used, so it's literally the second-
                # scanner-pass visualization the user requested.
                _annotated = _annotate_frozen_snapshot(
                    img, label_rows, _raw_per_field,
                )
                # Capture the current calibration version so the
                # snapshot re-OCR pass knows when band geometry has
                # changed since this freeze was captured.
                try:
                    from . import hud_panel_tracker as _hpt_freeze
                    _freeze_cal_ver = _hpt_freeze.get_calibration_version()
                except Exception:
                    _freeze_cal_ver = 0
                _frozen.freeze(
                    _annotated,
                    {
                        "mass": _mass_raw,
                        "resistance": _resist_raw,
                        "instability": _instab_raw,
                        "mineral_name": result.get("mineral_name"),
                    },
                    raw_img=img,
                    calibration_version=_freeze_cal_ver,
                )
                # Feed the just-confirmed values into the learned
                # per-field lexicon. The freeze trigger only fires
                # when all three numeric fields pass structural
                # validators AND mass/instab are non-zero, so this
                # is the highest-confidence moment to learn from.
                # The lexicon then relaxes the HUD-RGB CRNN gate on
                # subsequent scans of the same / similar rocks —
                # same mechanic the signal pipeline uses with its
                # chart-derived 664-value set.
                try:
                    from . import hud_lexicon as _hud_lex
                    if _mass_raw is not None:
                        _hud_lex.observe("mass", float(_mass_raw))
                    if _resist_raw is not None:
                        _hud_lex.observe(
                            "resistance", float(_resist_raw),
                        )
                    if _instab_raw is not None:
                        _hud_lex.observe(
                            "instability", float(_instab_raw),
                        )
                except Exception as _lex_obs_exc:  # pragma: no cover
                    log.debug(
                        "hud_lexicon observe failed: %s",
                        _lex_obs_exc,
                    )
        # Late-arriving mineral_name back-fill: the live mineral-name
        # OCR (CRNN + Tesseract + fuzzy lexicon) is slower and less
        # reliable than the numeric OCR. The freeze trigger fires as
        # soon as the three numbers are clean, so mineral_name is
        # often still None at freeze time. If a subsequent scan
        # resolves it, latch it into the freeze so the UI's "Resource"
        # row populates without us un-freezing the panel.
        if _frozen.is_frozen and _frozen.values.get("mineral_name") is None:
            _live_mineral = result.get("mineral_name")
            if _live_mineral:
                _frozen.update_field_if_missing("mineral_name", _live_mineral)
        # ── Snapshot re-OCR (DISABLED) ──────────────────────────────
        # The recursive ``scan_hud_onnx`` call on the frozen snapshot
        # was disabled after it was found to pollute module-level
        # state. Specifically:
        #   * ``scan_results_match`` keeps a per-region smoothed
        #     title-position tracker. The snapshot's title detector
        #     occasionally picks a small false-positive (e.g. the
        #     "SCAN RESULTS" sub-pattern at w=95 h=17 instead of the
        #     real h=45 title), and the panel-jump-accept logic
        #     re-baselines the live tracker to that bad position.
        #   * Field lock cache, sticky consensus, and find_hud_panel
        #     cache are all module-level and get written by the
        #     recursive call, contaminating the next live scan.
        # In aggregate the re-OCR pass was running once per
        # calibration version bump (which happens almost every scan
        # while the learner refines its medians) and degrading the
        # live OCR each time — the opposite of what was intended.
        #
        # The infrastructure (raw_image stored on the freeze, the
        # calibration_version_at_* fields, scan_hud_onnx's
        # ``_img_override`` + ``_suppress_freeze`` kwargs) is kept
        # in place so a future implementation that properly
        # isolates state (per-region tracker save/restore, lock
        # cache snapshotting, or a refactored OCR-only sub-function)
        # can re-enable this without re-introducing the plumbing.
        # For now we don't run the re-OCR pass.
        # Frozen wins: override result values with the frozen ones.
        # Live OCR keeps running (so training data flows + UI shows
        # live-vs-frozen comparison), but the published numbers are
        # the locked-in frozen set.
        if _frozen.is_frozen:
            _frozen_vals = _frozen.values
            result = dict(result)  # copy so we don't mutate the live-OCR record
            for _f in ("mass", "resistance", "instability"):
                if _frozen_vals.get(_f) is not None:
                    result[_f] = _frozen_vals[_f]
            if _frozen_vals.get("mineral_name") is not None:
                result["mineral_name"] = _frozen_vals["mineral_name"]
            result["panel_visible"] = True
            result["frozen"] = True
            log.debug(
                "frozen_panel: published frozen values (age=%.1fs) — %s",
                _frozen.age_seconds(),
                {k: result.get(k) for k in (
                    "mass", "resistance", "instability", "mineral_name",
                )},
            )
    except Exception as _fp_exc:
        log.warning(
            "frozen_panel integration failed: %s", _fp_exc, exc_info=True,
        )

    elapsed_ms = (time.time() - t0) * 1000
    log.info(
        "sc_ocr: mineral=%s mass=%s resistance=%s instability=%s in %.0fms",
        result.get("mineral_name"),
        result["mass"], result["resistance"], result["instability"],
        elapsed_ms,
    )
    if _dbg is not None:
        try:
            _dbg.write()
        except Exception as exc:
            log.debug("api: scan_hud_onnx swallowed: %s", exc)
        # Decrement the force-capture counter once per scan so a
        # "📼 Record Next Scan" click captures EXACTLY one scan rather
        # than leaking across calls. Safe to call when the counter is 0.
        try:
            _dbg.consume_capture_for_scan()
        except Exception as exc:
            log.debug("api: scan_hud_onnx swallowed: %s", exc)
    return result


def scan_refinery(region: dict, station: str = "") -> Optional[list[dict]]:
    """Read a refinery terminal region → list of order dicts.

    v1: delegates to the legacy refinery_reader since it needs
    full-alphabet recognition which the 13-class ONNX model can't do.
    """
    try:
        from ..refinery_reader import scan_refinery as legacy_scan
        return legacy_scan(region, station)
    except Exception as exc:
        log.debug("sc_ocr.scan_refinery: legacy fallback: %s", exc)
        return None
