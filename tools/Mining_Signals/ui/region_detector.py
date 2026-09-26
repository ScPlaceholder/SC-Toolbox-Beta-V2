"""Live region-detector tie-in for :class:`ui.region_selector.RegionSelector`.

While the user drags a selection rectangle, the selector hands the
current rectangle (in native screen pixels) to :func:`detect`, which
captures that region and runs the real HUD finders against it. The
returned boxes are drawn back inside the selection as "ghost" outlines
— the same regions the live OCR pipeline's debug overlay annotates —
so the user can see what the detectors are locking onto *as they
select*, and watch every region snap green the moment the panel is
fully framed.

Two detector modes:

  * ``"hud"`` — the SCAN RESULTS panel. Runs the NCC title anchor
    (``scan_results_match.find_scan_results_anchor``) plus the per-row
    label matcher (``label_match.find_label_positions``). ``complete``
    is True once the title and all three numeric-row labels (MASS /
    RESISTANCE / INSTABILITY) are found together.

  * ``"signal"`` — the signature scanner. Runs the location-pin icon
    NCC (``signal_anchor.find_icon``) plus the digit-cluster pattern
    finder (``signal_anchor.find_digit_cluster``). ``complete`` is True
    once both the icon and the digit cluster are found.

All box coordinates returned are REGION-RELATIVE pixels (origin at the
captured region's top-left), so the caller can map them through the
selection rectangle's transform without knowing anything about the
detectors.

Every import of the (heavy) OCR machinery is deferred into the call so
this module is cheap to import on the GUI thread; :func:`detect` itself
is intended to run on a worker thread.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Region-relative box shape used throughout this module / the selector:
#   {"x": int, "y": int, "w": int, "h": int, "label": str, "kind": str}
# ``kind`` is a coarse tag ("title" / "label" / "icon" / "digit") the
# caller may use for styling; ``label`` is the short text drawn above
# the box.


def prewarm(mode: str) -> None:
    """Build the detector caches up-front so the first real detection is
    fast.

    The cold cost of these detectors is almost entirely one-time work:
    the HUD title/label matchers build their multi-scale NCC template
    variants on first call (~0.9 s total) and are ~0 ms after that, and
    the signal icon matcher builds its template cache (and loads its
    ONNX voter). Running each detector once against a throwaway image
    forces that work to happen on a background thread *before* the user
    finishes their first drag, so the live ghosts appear instantly
    instead of stalling for ~1 s on the first frame.

    Content of the dummy image doesn't matter — the caches are built
    regardless of what the detectors find in it. Best-effort: any
    failure here just means the first real detection pays the cold cost
    as before.
    """
    try:
        import numpy as np
        from PIL import Image
        # Deterministic low-contrast pattern (no RNG): enough structure
        # that the matchers run their full path, small enough to be cheap.
        ramp = (np.indices((130, 240)).sum(axis=0) % 64).astype("uint8")
        dummy = Image.fromarray(np.stack([ramp] * 3, axis=-1), "RGB")
        if mode == "hud":
            _detect_hud(dummy)
        elif mode == "signal":
            _detect_signal(dummy, reset=True)
    except Exception as exc:  # pragma: no cover — warming is best-effort
        log.debug("region_detector.prewarm(%s) failed: %s", mode, exc)


def detect(
    region: dict,
    mode: str,
    *,
    reset: bool = False,
    game_resolution: Optional[dict] = None,
) -> Optional[dict]:
    """Capture *region* and run the *mode* detectors against it.

    Parameters
    ----------
    region:
        ``{"x", "y", "w", "h"}`` in NATIVE screen pixels (matching what
        ``mss`` captures — exactly what the selector tracks via the
        Win32 cursor position).
    mode:
        ``"hud"`` or ``"signal"``.
    reset:
        When True, clears any per-drag detector state before running
        (currently the signal anchor's temporal-smoothing cache). The
        selector passes this on the first detection of a fresh drag so
        a previous selection's cache doesn't leak in.
    game_resolution:
        Effective game resolution ``{"w","h","source"}`` (see
        ``ui.game_resolution``). When the player's vertical resolution
        differs from the detectors' reference, the captured region is
        normalized to reference scale before detection so the HUD
        elements land at the template-calibrated size — improving
        detection on non-reference displays. Boxes are mapped back to
        region-relative pixels before returning, so callers are
        unaffected. ``None`` skips normalization (reference behavior).

    Returns
    -------
    ``{"boxes": [...], "complete": bool, "w": int, "h": int,
       "mode": str}`` with region-relative boxes, or ``None`` if the
    region is too small / capture failed / the mode is unknown.
    """
    if not region:
        return None
    if int(region.get("w", 0)) < 8 or int(region.get("h", 0)) < 8:
        return None
    try:
        from ocr.screen_reader import capture_region
    except Exception as exc:  # pragma: no cover — import guard
        log.debug("region_detector: capture import failed: %s", exc)
        return None
    pil = capture_region(region)
    if pil is None:
        return None

    # Resolution normalization: rescale the capture so the HUD renders at
    # the detectors' reference pixel size, then map the detected boxes
    # back. A no-op at the reference resolution (factor ≈ 1).
    norm = _normalization_factor(game_resolution)
    if norm is not None and abs(norm - 1.0) > 0.04:
        pil_n = _resize(pil, norm)
        if mode == "hud":
            res = _detect_hud(pil_n)
        elif mode == "signal":
            res = _detect_signal(pil_n, reset=reset)
        else:
            return None
        if res is not None:
            _rescale_boxes(res, 1.0 / norm)
            res["w"], res["h"] = pil.size
        return res

    if mode == "hud":
        return _detect_hud(pil)
    if mode == "signal":
        return _detect_signal(pil, reset=reset)
    return None


def _normalization_factor(game_resolution: Optional[dict]) -> Optional[float]:
    """Factor to resize a capture so HUD elements match the detectors'
    reference scale: ``REFERENCE_HEIGHT / game_height``.

    A 1080p player (half the reference height) → factor 2.0 (upscale so
    the HUD title lands at the calibrated ~45 px). Returns ``None`` when
    the resolution is unknown (skip normalization).
    """
    if not isinstance(game_resolution, dict):
        return None
    try:
        h = int(game_resolution.get("h") or 0)
    except (TypeError, ValueError):
        return None
    if h <= 0:
        return None
    try:
        from ui.game_resolution import REFERENCE_HEIGHT
    except Exception:
        REFERENCE_HEIGHT = 2160
    factor = REFERENCE_HEIGHT / float(h)
    # Clamp to a sane range so a bogus resolution can't blow up the
    # capture into a multi-thousand-pixel resize.
    if factor < 0.25 or factor > 4.0:
        return None
    return factor


def _resize(pil, factor: float):
    """Resize a PIL image by *factor* (bilinear), guarding tiny sizes."""
    from PIL import Image
    w = max(8, int(round(pil.width * factor)))
    h = max(8, int(round(pil.height * factor)))
    return pil.resize((w, h), Image.BILINEAR)


def _rescale_boxes(result: dict, factor: float) -> None:
    """Scale every box in *result* by *factor* in place (detection ran on
    a normalized image; map boxes back to region pixels)."""
    for b in result.get("boxes", []):
        for k in ("x", "y", "w", "h"):
            if k in b:
                b[k] = int(round(b[k] * factor))


def _detect_hud(pil) -> dict:
    """Run the SCAN RESULTS title + label detectors on *pil*."""
    W, H = pil.size
    boxes: list[dict] = []

    title: Optional[dict] = None
    try:
        from ocr.sc_ocr.scan_results_match import find_scan_results_anchor
        anchor = find_scan_results_anchor(pil)
        if anchor is not None:
            title = {
                "x": int(anchor["title_x"]),
                "y": int(anchor["title_y"]),
                "w": int(anchor["title_w"]),
                "h": int(anchor["title_h"]),
            }
    except Exception as exc:
        log.debug("region_detector: HUD title detect failed: %s", exc)

    labels: dict = {}
    try:
        from ocr.sc_ocr.label_match import find_label_positions
        labels = find_label_positions(pil) or {}
    except Exception as exc:
        log.debug("region_detector: HUD label detect failed: %s", exc)

    if title is not None:
        boxes.append({**title, "label": "SCAN RESULTS", "kind": "title"})

    # Numeric rows in top-to-bottom order. Each entry from
    # find_label_positions is {"x","y","w","h","score"}.
    for field in ("mass", "resistance", "instability"):
        info = labels.get(field)
        if not info:
            continue
        try:
            boxes.append({
                "x": int(info["x"]), "y": int(info["y"]),
                "w": int(info["w"]), "h": int(info["h"]),
                "label": field.upper(), "kind": "label",
            })
        except (KeyError, TypeError, ValueError):
            continue

    n_rows = sum(
        1 for f in ("mass", "resistance", "instability") if labels.get(f)
    )
    complete = (title is not None) and (n_rows >= 3)
    return {"boxes": boxes, "complete": complete, "w": W, "h": H, "mode": "hud"}


def _detect_signal(pil, *, reset: bool = False) -> dict:
    """Run the signature icon + digit-cluster detectors on *pil*."""
    import numpy as np

    W, H = pil.size
    boxes: list[dict] = []
    icon: Optional[dict] = None
    digit: Optional[dict] = None

    try:
        from ocr.sc_ocr import signal_anchor
        if reset:
            try:
                signal_anchor.reset_anchor_cache()
            except Exception:
                pass
        gray = np.asarray(pil.convert("L"), dtype=np.uint8)
        rgb = np.asarray(pil.convert("RGB"), dtype=np.uint8)

        ic = signal_anchor.find_icon(gray, rgb_image=rgb)
        if ic is not None:
            x1, y1, x2, y2, _score = ic
            icon = {
                "x": int(x1), "y": int(y1),
                "w": int(x2 - x1), "h": int(y2 - y1),
            }
        dc = signal_anchor.find_digit_cluster(gray)
        if dc is not None:
            x1, y1, x2, y2 = dc
            digit = {
                "x": int(x1), "y": int(y1),
                "w": int(x2 - x1), "h": int(y2 - y1),
            }
    except Exception as exc:
        log.debug("region_detector: signal detect failed: %s", exc)

    if icon is not None:
        boxes.append({**icon, "label": "ICON", "kind": "icon"})
    if digit is not None:
        boxes.append({**digit, "label": "VALUE", "kind": "digit"})

    complete = (icon is not None) and (digit is not None)
    return {
        "boxes": boxes, "complete": complete, "w": W, "h": H, "mode": "signal",
    }
