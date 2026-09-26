"""Self-healing screen capture for the SC mining HUD.

Why this exists
---------------
``scan_hud_onnx`` reads a user-defined ``region`` (x/y/w/h on screen)
and feeds it straight into the OCR pipeline. If that user-drawn
rectangle is smaller than the actual SCAN RESULTS panel — for example
188x160 around a panel that needs ~450x670 — the capture cuts off
the panel title and one or more value rows. The LSQ rigid-body
solver in ``hud_panel_tracker`` then tries to register the visible
label rows against an out-of-frame title, the residuals blow up
(332 px residuals on a 160 px image have been observed), the lock
fails, and downstream stages emit garbage values such as
``44 mass / 444,444``.

The fix is captured here. Rather than trusting the user rectangle as
the literal capture region, we:

  1. Expand the rectangle by ``expand_margin`` pixels on every side
     (clamped to monitor bounds) and capture the larger area.
  2. Run ``hud_color_finder.find_hud_panel`` on the larger frame to
     locate the SCAN RESULTS panel from RGB pixel evidence alone.
  3. If the locator finds the panel with adequate confidence, crop
     to ``panel_bbox + padding`` (also clamped) and return that crop
     plus the screen-space coordinates it maps to.
  4. If the locator returns nothing (or low confidence), fall back to
     the original user rectangle — the user gets exactly the image
     they would have got without self-heal enabled, so the worst
     case is "no improvement".

The locator is the same color-mask HUD finder that already powers
the position prior inside ``scan_hud_onnx``. Lifting it one level
earlier (before the OCR pipeline runs) means a too-small rectangle
no longer truncates the panel.

Public API
----------
``autoheal_capture(user_region, *, expand_margin=200, padding=20,
                   min_confidence=0.4)``
    Returns ``(cropped_image, actual_region, diagnostic_info)``.

Constraints
-----------
* Pure capture-layer concern: never touches the OCR pipeline itself
  and never reaches into UI code.
* Always returns a 3-tuple; callers can rely on the shape even on
  catastrophic failure (``cropped_image=None`` then).
* When ``find_hud_panel`` is unavailable (missing scipy/PIL/etc.)
  this module degrades gracefully to a plain capture at the user
  region — the OCR pipeline keeps its previous behaviour.
"""
from __future__ import annotations

import logging
from typing import Optional

from PIL import Image

from . import capture

log = logging.getLogger(__name__)

__all__ = ["autoheal_capture", "DEFAULT_EXPAND_MARGIN", "DEFAULT_PADDING"]


# ─────────────────────────────────────────────────────────────────────
# Tunables — overridable per call.
# ─────────────────────────────────────────────────────────────────────

# How many extra pixels to capture on every side of the user region.
# 200 px is enough to recover from rectangles that miss the title row
# (which is ~50 px tall and may be drawn 30-150 px above the user's
# top edge) while still keeping the capture well under a full monitor
# grab on typical displays.
DEFAULT_EXPAND_MARGIN = 200

# How many pixels of breathing room to keep around the detected panel
# bbox after find_hud_panel succeeds. The locator returns a tight
# bbox around the color-mask connected component; downstream stages
# (label match, colon anchor, value-crop search) want a few pixels
# of margin so glyphs near the panel edge don't get clipped.
DEFAULT_PADDING = 20

# Minimum confidence from find_hud_panel below which we fall back to
# the user region. 0.4 corresponds to a panel that scored
# above-half on at least two of the three (area / extent / aspect)
# sub-scores in the locator's heuristic — empirically that's the
# threshold below which the picked bbox is more likely to be HUD
# debris (mini-map, comm overlay) than the SCAN RESULTS panel.
DEFAULT_MIN_CONFIDENCE = 0.4


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _primary_monitor() -> Optional[dict]:
    """Return the full primary monitor as ``{x, y, w, h}`` or None.

    ``mss.mss().monitors[1]`` is the primary display (index 0 is the
    bounding box of all monitors). Returns None when mss is missing —
    the caller treats that as "no full-monitor fallback available".
    """
    try:
        import mss
    except ImportError:
        log.debug("region_autoheal: mss missing, no monitor fallback")
        return None
    try:
        with mss.mss() as sct:
            mons = sct.monitors
            if len(mons) < 2:
                return None
            m = mons[1]
            return {
                "x": int(m["left"]),
                "y": int(m["top"]),
                "w": int(m["width"]),
                "h": int(m["height"]),
            }
    except Exception as exc:
        log.debug("region_autoheal: primary monitor lookup failed: %s", exc)
        return None


def _expanded_region(user_region: dict, expand_margin: int) -> dict:
    """Expand ``user_region`` by ``expand_margin`` px on every side.

    Always clamped against the primary monitor when one is reachable;
    when mss is missing we still expand but only constrain to
    non-negative origin (the underlying ``capture.grab`` will refuse
    a region that runs off the right/bottom edge of the screen, which
    is the right failure mode for that fallback).
    """
    x = int(user_region["x"])
    y = int(user_region["y"])
    w = int(user_region["w"])
    h = int(user_region["h"])

    nx = x - expand_margin
    ny = y - expand_margin
    nw = w + 2 * expand_margin
    nh = h + 2 * expand_margin

    mon = _primary_monitor()
    if mon is not None:
        m_x, m_y = int(mon["x"]), int(mon["y"])
        m_w, m_h = int(mon["w"]), int(mon["h"])
        # Clamp top-left to monitor bounds.
        if nx < m_x:
            nw -= (m_x - nx)
            nx = m_x
        if ny < m_y:
            nh -= (m_y - ny)
            ny = m_y
        # Clamp right/bottom to monitor bounds.
        if nx + nw > m_x + m_w:
            nw = (m_x + m_w) - nx
        if ny + nh > m_y + m_h:
            nh = (m_y + m_h) - ny
    else:
        if nx < 0:
            nw += nx       # shrink width by the off-screen amount
            nx = 0
        if ny < 0:
            nh += ny
            ny = 0

    nw = max(1, nw)
    nh = max(1, nh)
    return {"x": int(nx), "y": int(ny), "w": int(nw), "h": int(nh)}


def _clamp_bbox_in_image(
    bbox: tuple[int, int, int, int],
    padding: int,
    img_w: int,
    img_h: int,
) -> tuple[int, int, int, int]:
    """Inflate ``bbox`` by ``padding`` on each side, clamped to image."""
    bx, by, bw, bh = (int(v) for v in bbox)
    px0 = max(0, bx - padding)
    py0 = max(0, by - padding)
    px1 = min(img_w, bx + bw + padding)
    py1 = min(img_h, by + bh + padding)
    return px0, py0, max(1, px1 - px0), max(1, py1 - py0)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def autoheal_capture(
    user_region: Optional[dict],
    *,
    expand_margin: int = DEFAULT_EXPAND_MARGIN,
    padding: int = DEFAULT_PADDING,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[Optional[Image.Image], dict, dict]:
    """Capture a self-healing crop of the SC mining HUD panel.

    Parameters
    ----------
    user_region
        ``{"x", "y", "w", "h"}`` (screen-space, integer pixels) or
        ``None`` to use the full primary monitor.
    expand_margin
        Pixels added to each side of ``user_region`` before capture.
        Increase if users tend to draw very small rectangles or aim
        the rectangle well off the actual panel; decrease to reduce
        per-scan capture cost on very large displays.
    padding
        Pixels added around the detected panel bbox when cropping the
        captured image back down. Gives downstream OCR stages a few
        pixels of margin around the chrome.
    min_confidence
        Minimum ``find_hud_panel`` confidence to accept the detection.
        Below this we treat the locator as having failed and fall
        back to the user-region crop.

    Returns
    -------
    ``(cropped_image, actual_region, diagnostic_info)`` where:

      * ``cropped_image`` is a PIL ``Image.Image`` in RGB mode, or
        ``None`` if capture failed entirely (caller should treat as
        the previous "img is None" case and return empty).
      * ``actual_region`` is ``{"x", "y", "w", "h"}`` in screen-space
        describing what was actually returned — handy for diagnostic
        overlays and for keeping snapshot metadata honest.
      * ``diagnostic_info`` always contains at minimum a ``"heal"``
        key with value ``"applied"`` (panel relocated), ``"fallback"``
        (locator failed / low confidence — user-region crop returned)
        or ``"error"`` (caught exception, ``cropped_image`` is None).

    Algorithm
    ---------
    1. Compute the capture rect = ``user_region`` expanded by
       ``expand_margin`` px on every side, clamped to monitor bounds.
       If ``user_region`` is None, use the full primary monitor.
    2. Capture that expanded area via ``capture.grab``.
    3. Run ``find_hud_panel`` on the captured image.
    4. If the locator finds a panel with confidence ≥ ``min_confidence``,
       crop to ``bbox + padding`` (clamped) and return it with
       screen-space coordinates and ``heal="applied"``.
    5. Otherwise crop the captured image back to ``user_region``
       (still a valid frame) and return ``heal="fallback"``.
    6. On any uncaught exception, return ``(None, user_region or {},
       {"heal": "error", "exc": str(exc)})``.
    """
    diag: dict = {
        "heal": "fallback",
        "user_region": dict(user_region) if user_region else None,
        "expand_margin": int(expand_margin),
        "padding": int(padding),
        "min_confidence": float(min_confidence),
    }

    # Resolve effective user region: either the supplied rect or the
    # full primary monitor.
    effective_user: Optional[dict]
    if user_region is None:
        effective_user = _primary_monitor()
        if effective_user is None:
            diag["heal"] = "error"
            diag["exc"] = "no user_region and primary monitor unavailable"
            return None, {}, diag
        diag["user_region"] = dict(effective_user)
    else:
        effective_user = dict(user_region)

    try:
        capture_region = _expanded_region(effective_user, int(expand_margin))
        diag["capture_region"] = dict(capture_region)
    except Exception as exc:
        log.warning("region_autoheal: expand failed: %s", exc)
        capture_region = dict(effective_user)
        diag["capture_region"] = dict(capture_region)
        diag["expand_error"] = str(exc)

    # Capture the (probably) expanded area. One retry for parity with
    # the previous double-grab pattern in api.py.
    captured = capture.grab(capture_region)
    if captured is None:
        captured = capture.grab(capture_region)
    if captured is None:
        # Last-ditch attempt at the unmodified user region — if even
        # that fails, we're done.
        captured = capture.grab(effective_user)
        if captured is None:
            diag["heal"] = "error"
            diag["exc"] = "capture.grab returned None twice for expanded region"
            return None, dict(effective_user), diag
        # Got the user region but not the expanded one — return as
        # the fallback (no self-heal possible without the larger
        # context).
        diag["heal"] = "fallback"
        diag["fallback_reason"] = "expanded grab failed; returned user-region grab"
        return captured, dict(effective_user), diag

    cap_w, cap_h = captured.size
    diag["captured_size"] = {"w": int(cap_w), "h": int(cap_h)}

    # Try to locate the panel inside the captured frame.
    panel_res: Optional[dict] = None
    try:
        from hud_tracker.anchors.hud_color_finder import find_hud_panel
        panel_res = find_hud_panel(captured, return_details=False)
    except ImportError as exc:
        diag["fallback_reason"] = f"hud_color_finder unavailable: {exc}"
        log.debug("region_autoheal: hud_color_finder import failed: %s", exc)
    except Exception as exc:
        diag["fallback_reason"] = f"find_hud_panel raised: {exc}"
        log.warning("region_autoheal: find_hud_panel raised: %s", exc)

    if panel_res is None or "bbox" not in panel_res:
        diag.setdefault("fallback_reason", "find_hud_panel returned None")
        return _fallback_to_user_crop(captured, capture_region, effective_user, diag)

    confidence = float(panel_res.get("confidence", 0.0))
    diag["panel_bbox_local"] = tuple(int(v) for v in panel_res["bbox"])
    diag["panel_confidence"] = confidence

    if confidence < float(min_confidence):
        diag["fallback_reason"] = (
            f"confidence {confidence:.2f} < min_confidence {min_confidence:.2f}"
        )
        return _fallback_to_user_crop(captured, capture_region, effective_user, diag)

    # Locator succeeded with adequate confidence — crop to bbox+padding.
    cx, cy, cw, ch = _clamp_bbox_in_image(
        panel_res["bbox"], int(padding), cap_w, cap_h,
    )
    healed = captured.crop((cx, cy, cx + cw, cy + ch))

    actual_region = {
        "x": int(capture_region["x"]) + int(cx),
        "y": int(capture_region["y"]) + int(cy),
        "w": int(cw),
        "h": int(ch),
    }
    diag["heal"] = "applied"
    diag["actual_region"] = dict(actual_region)
    diag["cropped_size"] = {"w": int(cw), "h": int(ch)}
    log.info(
        "region_autoheal: applied (conf=%.2f) user_region=%s actual_region=%s",
        confidence, dict(effective_user), actual_region,
    )
    return healed, actual_region, diag


def _fallback_to_user_crop(
    captured: Image.Image,
    capture_region: dict,
    user_region: dict,
    diag: dict,
) -> tuple[Optional[Image.Image], dict, dict]:
    """Return a crop of ``captured`` that matches ``user_region``.

    Used when ``find_hud_panel`` fails or returns low confidence —
    the user gets exactly the image they would have got without
    self-heal, so the worst case is "no improvement, no regression".
    """
    cap_w, cap_h = captured.size
    cap_x = int(capture_region["x"])
    cap_y = int(capture_region["y"])
    ux = int(user_region["x"]) - cap_x
    uy = int(user_region["y"]) - cap_y
    uw = int(user_region["w"])
    uh = int(user_region["h"])

    # If the expanded grab is identical in geometry to the user region
    # (e.g. the user region already touched both monitor edges so
    # expansion was clamped away), the trivial 0/0/w/h crop is the
    # same image — Pillow handles that fine.
    cx0 = max(0, ux)
    cy0 = max(0, uy)
    cx1 = min(cap_w, ux + uw)
    cy1 = min(cap_h, uy + uh)
    if cx1 <= cx0 or cy1 <= cy0:
        # The user region landed entirely outside the captured frame
        # (shouldn't normally happen — expansion is monotone — but
        # protect against pathological inputs). Return the captured
        # frame as-is.
        diag["fallback_reason"] = (
            diag.get("fallback_reason", "")
            + " | user crop outside captured frame; returning capture as-is"
        ).strip(" |")
        return captured, dict(capture_region), diag

    crop = captured.crop((cx0, cy0, cx1, cy1))
    diag["heal"] = "fallback"
    return crop, dict(user_region), diag
