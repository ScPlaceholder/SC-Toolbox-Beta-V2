"""Per-scan diagnostic overlay for the panel finder.

The OCR pipeline pushes telemetry (HUD lines detected, mineral band,
row positions, value crops, lock state) into a module-level dict as
the scan progresses, then ``write()`` renders a single annotated PNG
showing every decision the panel finder made.

A live viewer (``scripts/live_panel_finder_viewer.py``) polls the
PNG every 400 ms so the user can watch the panel finder in real
time and see exactly where each crop came from.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

# Compute an absolute output path so the overlay always lands in
# tools/Mining_Signals/ regardless of the toolbox's CWD. THIS file
# is at tools/Mining_Signals/ocr/sc_ocr/debug_overlay.py — two
# parent dirs gets us to tools/Mining_Signals/.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOL_DIR = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))
OUT_PATH = os.path.join(_TOOL_DIR, "debug_panel_overlay.png")

# ── Diagnostic-write gate ────────────────────────────────────────────────
# The OCR pipeline writes ~50 files per scan to populate live diagnostic
# viewers (Glyph Reader, Calibration Dialog, Panel Finder, Signature
# Finder). Most of the time NO viewer is open, but the writes still
# happen — pure waste. This heartbeat scheme lets each viewer announce
# "I'm watching" by touching a file every poll tick; the OCR pipeline
# then no-ops every diagnostic dump when the heartbeat is stale.
#
# Cross-process safe: viewers in scripts/ run as separate Python procs
# (launched via .bat files) and the heartbeat file is the only shared
# state. Crash-safe: if a viewer dies without removing the file, the
# mtime check naturally goes stale within HEARTBEAT_TTL_SEC.
_HEARTBEAT_FILE = os.path.join(_TOOL_DIR, "debug_glyphs", ".viewer_heartbeat")
HEARTBEAT_TTL_SEC = 5.0          # heartbeat older than this = no viewer
# Cache TTL had to come down: at 1.0 s, opening a viewer mid-scan could
# leave the gate's cached "False" stuck for a whole second after the
# viewer started touching the heartbeat — long enough to skip an entire
# overlay frame and surface as "no selection boxes". 0.2 s is short
# enough that a fresh heartbeat reaches the next dump call but long
# enough that the ~30 dump calls within a single scan still share one
# stat, so total per-scan stat overhead stays around 100 µs.
_DIAG_CACHE_TTL_SEC = 0.2
_diag_last_check: float = 0.0
_diag_last_result: bool = False


def viewer_heartbeat() -> None:
    """Called by every viewer's poll tick. Marks a heartbeat file with
    the current mtime so the OCR pipeline knows at least one viewer is
    watching and should write its diagnostic dumps.

    Also invalidates the in-process diagnostics_active() cache so a
    subsequent dump call this same scan picks up the fresh heartbeat
    instead of returning a stale False. Critical for the same-process
    viewers (calibration dialog, panel finder popout): without the
    invalidation they'd race against the cache for the first
    _DIAG_CACHE_TTL_SEC after open and miss an overlay frame.
    Cross-process viewers (scripts/) need the short TTL above instead.
    """
    global _diag_last_check, _diag_last_result
    try:
        os.makedirs(os.path.dirname(_HEARTBEAT_FILE), exist_ok=True)
        # Touching is enough — we only care about mtime. Use os.utime
        # against an empty file to avoid Windows-specific quirks of
        # Path.touch() on network drives.
        with open(_HEARTBEAT_FILE, "ab"):
            pass
        os.utime(_HEARTBEAT_FILE, None)
        # Force the next diagnostics_active() to re-stat. Same-process
        # viewers share this module's globals with the OCR pipeline, so
        # the next dump call sees True instantly.
        _diag_last_check = 0.0
        _diag_last_result = True
    except Exception:
        # Heartbeat is best-effort; failure just means dumps stay off.
        pass


def diagnostics_active() -> bool:
    """True if any viewer touched the heartbeat file within
    HEARTBEAT_TTL_SEC. Cached for _DIAG_CACHE_TTL_SEC so the ~30 dump
    calls per scan share a single os.stat."""
    global _diag_last_check, _diag_last_result
    import time as _time
    now = _time.monotonic()
    if (now - _diag_last_check) < _DIAG_CACHE_TTL_SEC:
        return _diag_last_result
    try:
        mtime = os.path.getmtime(_HEARTBEAT_FILE)
        result = (_time.time() - mtime) < HEARTBEAT_TTL_SEC
    except OSError:
        result = False
    _diag_last_check = now
    # Surface flips at INFO so the user can see in the log when the
    # gate opens / closes; helps diagnose "no boxes" symptoms.
    if result != _diag_last_result:
        log.info(
            "diagnostics_active flipped %s → %s (heartbeat=%s)",
            _diag_last_result, result, _HEARTBEAT_FILE,
        )
    _diag_last_result = result
    return result


# ── Per-tag heartbeat + capture-counter ──────────────────────────────────
# The legacy `viewer_heartbeat()` / `diagnostics_active()` pair is binary:
# any viewer alive = full ~50-files-per-scan dump. The tag-aware API
# below lets viewers declare WHICH dumps they actually need:
#
#   "crops"   — per-scan value crops (calibration dialog, live_crop_viewer)
#   "glyphs"  — per-glyph PNGs + voter JSON (glyph_reader_viewer)
#   "overlay" — annotated panel overlay PNG (panel finder viewers)
#
# Activation is OR over: legacy heartbeat (back-compat: legacy = all
# tags), per-tag heartbeat, and the force-capture counter. Cross-process
# scripts that haven't migrated keep working unchanged.
_capture_counter: int = 0
_capture_lock = threading.Lock()
_tag_cache: dict[str, tuple[float, bool]] = {}


def _tag_heartbeat_path(tag: str) -> str:
    return os.path.join(
        os.path.dirname(_HEARTBEAT_FILE), f".viewer_heartbeat_{tag}",
    )


def viewer_heartbeat_tag(tag: str) -> None:
    """Like viewer_heartbeat() but activates ONLY the given tag.

    Use this when only one diagnostic stream is needed instead of the
    full ~50-file-per-scan dump. Cheap to call every poll tick.
    """
    if not tag:
        return
    try:
        path = _tag_heartbeat_path(tag)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab"):
            pass
        os.utime(path, None)
        _tag_cache.pop(tag, None)
    except Exception:
        pass


def is_tag_active(tag: str) -> bool:
    """True if anyone is watching the given tag.

    OR over: force-capture counter, legacy heartbeat (= all tags), the
    per-tag heartbeat for ``tag``. The per-tag check is cached for
    _DIAG_CACHE_TTL_SEC so the dozens of save sites in a single scan
    share one os.stat per tag.
    """
    if _capture_active():
        return True
    if diagnostics_active():
        return True
    import time as _time
    now = _time.monotonic()
    cached = _tag_cache.get(tag)
    if cached is not None and (now - cached[0]) < _DIAG_CACHE_TTL_SEC:
        return cached[1]
    try:
        mtime = os.path.getmtime(_tag_heartbeat_path(tag))
        result = (_time.time() - mtime) < HEARTBEAT_TTL_SEC
    except OSError:
        result = False
    _tag_cache[tag] = (now, result)
    return result


def force_capture_next(n: int = 1) -> None:
    """Force the next N scans to write the FULL diagnostic dump,
    regardless of whether any viewer is registered.

    Powers the dialog's "📼 Record Next Scan" button. Each scan
    consumes one count via consume_capture_for_scan() at end of scan.
    Safe to call repeatedly — counter saturates at the requested value.
    """
    global _capture_counter
    with _capture_lock:
        _capture_counter = max(_capture_counter, int(n))


def consume_capture_for_scan() -> None:
    """Decrement the capture counter at the end of a scan.

    Called once per scan by the OCR pipeline after every save site has
    had a chance to fire. No-op when the counter is already 0.
    """
    global _capture_counter
    with _capture_lock:
        if _capture_counter > 0:
            _capture_counter -= 1


def _capture_active() -> bool:
    """True if the force-capture counter is currently positive."""
    with _capture_lock:
        return _capture_counter > 0


_state: dict[str, Any] = {}


def reset() -> None:
    _state.clear()


def set_image(img: Image.Image) -> None:
    _state["image"] = img.copy() if img is not None else None
    # Ping file: lets us tell from disk whether set_image() is being
    # called even when write() never produces an overlay PNG.
    try:
        with open(os.path.join(_TOOL_DIR, "debug_overlay_ping.txt"), "w") as f:
            import time as _time
            f.write(f"set_image called at {_time.time()}\n")
            f.write(f"image_size={img.size if img is not None else None}\n")
    except Exception:
        pass


def set_hud_lines(lines: list[tuple[int, int, int]]) -> None:
    _state["hud_lines"] = list(lines or [])


_PANEL_FINDER_TTL_SEC = 8.0  # how long a stale title_box stays visible
_LABEL_ROWS_TTL_SEC = 8.0    # same, for the row band overlays
_GLYPH_BOXES_TTL_SEC = 8.0   # same, for the per-glyph bbox overlays


def set_panel_finder(
    top_y: Optional[int] = None,
    mineral_y_top: Optional[int] = None,
    mineral_y_bot: Optional[int] = None,
    mineral_center: Optional[int] = None,
    pitch: Optional[int] = None,
    bot_line_y: Optional[int] = None,
    source: str = "",
    title_box: Optional[tuple[int, int, int, int]] = None,
) -> None:
    """Push panel-finder telemetry for the debug overlay.

    ``title_box`` is the SCAN RESULTS title bounding rectangle
    ``(x, y, w, h)`` produced by the NCC anchor — drawing it makes
    row-mapping bugs immediately diagnosable: if the box is missing
    or in the wrong place, the off-by-one rows below it are an
    anchor-detection failure rather than an offset-math bug.

    Concurrency-safety: with 64 scan workers writing to this single
    module-global state, a transient anchor failure (one frame's NCC
    score dipping below threshold) would otherwise immediately blank
    a title_box that's been visible for many seconds. We treat the
    incoming ``title_box=None`` as "no update on this frame", not "the
    title is gone". A genuinely-gone title_box ages out via the TTL in
    ``write()`` (default 8 s).
    """
    import time as _time
    now = _time.time()
    new_title_box = (
        tuple(int(v) for v in title_box) if title_box else None
    )
    prev = _state.get("panel_finder") or {}
    prev_title_box = prev.get("title_box") if isinstance(prev, dict) else None
    prev_title_ts = prev.get("title_box_ts") if isinstance(prev, dict) else 0
    # If the new call has no title_box but a recent one is on file, keep
    # the cached value + its timestamp. Stale-only-ness is checked at
    # render time against ``_PANEL_FINDER_TTL_SEC``.
    if new_title_box is None and prev_title_box is not None and (
        now - float(prev_title_ts or 0) < _PANEL_FINDER_TTL_SEC
    ):
        kept_title_box = prev_title_box
        kept_ts = prev_title_ts
    else:
        kept_title_box = new_title_box
        kept_ts = now if new_title_box is not None else 0
    _state["panel_finder"] = {
        "top_line_y": top_y,
        "mineral_y_top": mineral_y_top,
        "mineral_y_bot": mineral_y_bot,
        "mineral_center": mineral_center,
        "pitch": pitch,
        "bot_line_y": bot_line_y,
        "source": source,  # "by_position" or "tesseract_fallback"
        "title_box": kept_title_box,
        "title_box_ts": kept_ts,
    }


def set_label_rows(rows: dict[str, tuple[int, int, int]]) -> None:
    """Update the per-row band overlays.

    Concurrency-safety: an empty / None ``rows`` arg is treated as
    "no update on this frame", not "wipe everything". Sibling scan
    threads racing against each other would otherwise blank the
    bands every time one of them failed to find rows. Per-key TTL is
    enforced at render time against ``_LABEL_ROWS_TTL_SEC`` so old
    rows naturally age out when the panel actually disappears.
    """
    import time as _time
    now = _time.time()
    incoming = {
        k: {"y1": int(y1), "y2": int(y2), "label_right": int(lr)}
        for k, (y1, y2, lr) in (rows or {}).items()
    }
    if not incoming:
        # No new rows on this frame — keep prior with their existing
        # timestamps; they'll age out via TTL in write().
        return
    prev = _state.get("label_rows")
    if not isinstance(prev, dict):
        prev = {}
    merged: dict = dict(prev)  # copy
    for k, v in incoming.items():
        v["ts"] = now
        merged[k] = v
    _state["label_rows"] = merged


def set_expected_rows(
    expected_centers: Optional[dict[str, int]],
    half_h: int = 12,
) -> None:
    """Stash the panel finder's PREDICTED row centers (mass / resistance /
    instability) so the overlay can draw a faint "search window" rectangle
    for any row that was looked for but not confidently matched.

    When ``label_rows`` is missing a field, the renderer falls back to
    these expected positions and draws a dim rectangle labelled
    ``MASS? / RESI? / INST?`` so the user can see "we looked here and
    found nothing" instead of a blank slot.

    ``half_h`` is the half-height (px) of the search rectangle; the
    overlay draws ``[cy - half_h, cy + half_h]``.
    """
    if not expected_centers:
        _state.pop("expected_rows", None)
        return
    _state["expected_rows"] = {
        "centers": {
            str(k): int(v) for k, v in expected_centers.items()
            if v is not None
        },
        "half_h": max(4, int(half_h)),
    }


def set_panel_pose(
    origin: Optional[tuple[int, int]],
    scale: Optional[float],
    nodes: Optional[list],
    rejected: Optional[list] = None,
    stab: Optional[str] = None,
) -> None:
    """Stash the single rigid-body pose so the overlay can draw the
    SPINE — the proof that every anchor hangs off ONE solved body.

    ``origin`` = panel origin (title top-left) in image px.
    ``scale``  = solved title height (px).
    ``nodes``  = ``[(name, pred_x, pred_y, actual_y_or_None), ...]`` for
                 title / mineral / mass / resistance / instability:
                 ``pred_*`` is where the POSE places that part,
                 ``actual_y`` is where the part's own detector landed
                 (None if it wasn't independently detected). A node ON
                 the spine = seated; a node with an offset actual_y =
                 a part that tried to wander.
    ``rejected`` = anchor ids the solve threw out as outliers.
    """
    if origin is None or scale is None or not nodes:
        _state.pop("panel_pose", None)
        return
    import time as _time
    _state["panel_pose"] = {
        "origin": (int(origin[0]), int(origin[1])),
        "scale": float(scale),
        "nodes": [
            (str(n[0]), int(n[1]), int(n[2]),
             None if n[3] is None else int(n[3]))
            for n in nodes
        ],
        "rejected": list(rejected or []),
        "stab": stab,
        "ts": _time.time(),
    }


def set_value_crop(field: str, box: tuple[int, int, int, int]) -> None:
    _state.setdefault("value_crops", {})[field] = tuple(int(v) for v in box)


def set_glyph_boxes(
    field: str,
    boxes: "list[tuple[int, int, int, int]]",
) -> None:
    """Per-glyph segmenter bboxes for the value-crop area.

    Boxes are stored in CROP-relative coordinates ``(x, y, w, h)``;
    the renderer translates to panel coords at draw time using the
    matching :func:`set_value_crop` rectangle as the origin. Pushed
    by ``_ocr_value_crop`` in api.py so the live overlay can show
    three independent accuracy axes per row:

      1. Glyph count — does the segmented count match the OCR text
         length? Mismatch hints at fused pairs, missed dots, or a
         hallucinated split.
      2. Per-glyph (w, h) — anomalous sizes (e.g. 18×26 mega-blob in
         a row of 11×16 digits) are visible at a glance.
      3. Vertical position — drifted bboxes (segmenter grabbed the
         wrong y-band) are visible as glyphs floating above or below
         where the digits actually sit.

    Mirrors what the HUD Row Reviewer GUI shows for offline QA, so a
    user spotting an issue in the live overlay can find the same
    failure mode in the reviewer's labeled-data view (or vice versa).
    """
    import time as _time
    _state.setdefault("glyph_boxes", {})[field] = {
        "boxes": [tuple(int(v) for v in b) for b in boxes],
        "ts": _time.time(),
    }


def set_lock(field: str, value: Optional[float], invalidated: bool = False) -> None:
    locks = _state.setdefault("locks", {})
    locks[field] = {"value": value, "invalidated": invalidated}


def set_ocr_text(field: str, text: str, confs: list[float]) -> None:
    _state.setdefault("ocr_text", {})[field] = {
        "text": text or "",
        "min_conf": min(confs) if confs else 0.0,
        "mean_conf": (sum(confs) / len(confs)) if confs else 0.0,
    }


def write() -> None:
    """Render and atomically save the annotated overlay PNG.

    Gated on the ``"overlay"`` tag specifically — no-ops when no
    viewer has pinged that tag (or the legacy heartbeat) recently.
    ``is_tag_active("overlay")`` ORs in ``diagnostics_active()`` so
    legacy callers of ``viewer_heartbeat()`` (cross-process scripts
    in ``scripts/``) continue to trigger writes.

    The annotated overlay PNG is the most expensive diagnostic write
    in the pipeline (large RGB image + multiple draw calls), so
    skipping it when nothing's watching is a major lag reduction.
    """
    if not is_tag_active("overlay"):
        return
    # Ping the "wrote" file unconditionally so we can tell from disk
    # whether write() is being reached even if image is None.
    try:
        with open(os.path.join(_TOOL_DIR, "debug_overlay_wrote.txt"), "w") as f:
            import time as _time
            f.write(f"write() called at {_time.time()}\n")
            f.write(f"state_keys={list(_state.keys())}\n")
            f.write(f"image_set={_state.get('image') is not None}\n")
    except Exception:
        pass
    img = _state.get("image")
    if img is None:
        return
    try:
        overlay = img.convert("RGB").copy()
        draw = ImageDraw.Draw(overlay)
        W, H = overlay.size

        # ── HUD separator lines (yellow) ──
        for line in _state.get("hud_lines", []):
            try:
                y, xl, xr = line
            except (TypeError, ValueError):
                continue
            draw.line([(xl, y), (xr, y)], fill=(255, 220, 0), width=2)
            draw.text((xr + 4, y - 6), "HUD", fill=(255, 220, 0))

        pf = _state.get("panel_finder", {})
        # ── SCAN RESULTS title box (gold, thick) ──
        # Drawn first so the row markers below render on top of its
        # outline, not behind it. Gold (255, 200, 0) was picked to
        # stand apart from the orange top/bot lines, the green mineral
        # band, and the cyan row bands — at a glance the user can
        # tell anchor-found-here from row-bands-here.
        # TTL gate: if the cached title_box is older than
        # ``_PANEL_FINDER_TTL_SEC``, skip drawing — the panel has
        # genuinely disappeared from view rather than just missing one
        # frame's NCC threshold.
        import time as _time_render
        _now_render = _time_render.time()
        title_box = pf.get("title_box")
        title_box_ts = float(pf.get("title_box_ts") or 0)
        if (
            title_box is not None
            and (_now_render - title_box_ts) > _PANEL_FINDER_TTL_SEC
        ):
            title_box = None
        if title_box is not None:
            tx, ty, tw, th = title_box
            tx2 = min(W - 1, tx + tw)
            ty2 = min(H - 1, ty + th)
            draw.rectangle(
                [(tx, ty), (tx2, ty2)],
                outline=(255, 200, 0), width=3,
            )
            draw.text(
                (tx, max(0, ty - 12)),
                "SCAN RESULTS", fill=(255, 200, 0),
            )

        # ── Top line marker (orange) ──
        if pf.get("top_line_y") is not None:
            ty = pf["top_line_y"]
            draw.line([(0, ty), (W - 1, ty)], fill=(255, 140, 0), width=1)
            draw.text((4, ty + 1), "TOP_LINE", fill=(255, 140, 0))

        # ── Mineral name band (green) + OCR'd name ──
        if pf.get("mineral_y_top") is not None and pf.get("mineral_y_bot") is not None:
            mt, mb = pf["mineral_y_top"], pf["mineral_y_bot"]
            draw.rectangle([(0, mt), (W - 1, mb)], outline=(0, 230, 100), width=1)
            draw.text((4, mt - 11), "MINERAL", fill=(0, 230, 100))
            # If OCR has produced a mineral-name read, surface it on
            # the overlay so we can verify the read against the panel.
            _mineral_ocr = _state.get("ocr_text", {}).get("mineral")
            if _mineral_ocr:
                _label = f"→ {_mineral_ocr['text']}"
                draw.text((W // 2 - 60, mt - 11), _label, fill=(0, 230, 100))

        # ── Bottom line marker (orange) ──
        if pf.get("bot_line_y") is not None:
            by = pf["bot_line_y"]
            draw.line([(0, by), (W - 1, by)], fill=(255, 140, 0), width=1)
            draw.text((4, by - 11), "BOT_LINE", fill=(255, 140, 0))

        # ── Pitch annotation ──
        pitch = pf.get("pitch")
        source = pf.get("source", "")
        info = []
        if source:
            info.append(f"finder={source}")
        if pitch is not None:
            info.append(f"pitch={pitch}")
        if info:
            draw.text((4, 4), " | ".join(info), fill=(255, 255, 255))

        # ── Row bands (cyan) + value crops (magenta) + lock state ──
        rows = _state.get("label_rows", {})
        crops = _state.get("value_crops", {})
        locks = _state.get("locks", {})
        ocrs = _state.get("ocr_text", {})
        expected = _state.get("expected_rows") or {}
        expected_centers = expected.get("centers") or {}
        expected_half_h = expected.get("half_h", 12)
        # INFO diagnostic so the user can grep the log and confirm WHICH
        # rows are present in state at render time, and which ages they
        # have (TTL gate at 8s drops stale ones). If this prints
        # ``label_rows_keys=['mass']`` but the user expected all three,
        # the helper isn't pushing them; if it prints all three but only
        # mass draws, the TTL or per-row dict shape is the next thing to
        # check. Demote to DEBUG once stable.
        try:
            _row_ages = {
                k: round(_now_render - float((rows.get(k) or {}).get("ts") or 0), 1)
                for k in ("mass", "resistance", "instability")
                if rows.get(k)
            }
            log.info(
                "debug_overlay.write: label_rows_keys=%s ages=%s expected_keys=%s",
                sorted(rows.keys()) if isinstance(rows, dict) else "n/a",
                _row_ages,
                sorted(expected_centers.keys()),
            )
        except Exception:
            pass
        # Short-form labels for the dim "we looked here" boxes drawn for
        # missing fields (rows whose expected_cy is known but whose
        # detection didn't confidently match).
        _MISSING_LABEL = {
            "mass": "MASS?",
            "resistance": "RESI?",
            "instability": "INST?",
        }
        for field in ("mass", "resistance", "instability"):
            row = rows.get(field)
            # TTL gate: drop rows older than _LABEL_ROWS_TTL_SEC so the
            # band fades when the panel actually disappears rather than
            # sticking forever after the rock is no longer in view.
            if row is not None and isinstance(row, dict):
                _row_ts = float(row.get("ts") or 0)
                if _row_ts and (_now_render - _row_ts) > _LABEL_ROWS_TTL_SEC:
                    row = None
            if row is not None:
                y1, y2, lr = row["y1"], row["y2"], row["label_right"]
                draw.rectangle(
                    [(0, y1), (W - 1, y2)],
                    outline=(0, 200, 255), width=1,
                )
                draw.text((4, y1 + 1), field.upper(), fill=(0, 200, 255))
                # Mark the shared label_right (value-column-left anchor)
                draw.line([(lr, y1), (lr, y2)], fill=(255, 100, 100), width=1)
            else:
                # Missing field — draw a dim "search window" box at the
                # expected position so the user sees "we looked here
                # and found nothing" instead of a blank slot. Dim grey
                # (80,80,80) makes it visually distinct from the bright
                # cyan boxes used for confident matches.
                cy = expected_centers.get(field)
                if cy is not None:
                    ey1 = max(0, int(cy) - expected_half_h)
                    ey2 = min(H - 1, int(cy) + expected_half_h)
                    if ey2 > ey1:
                        draw.rectangle(
                            [(0, ey1), (W - 1, ey2)],
                            outline=(80, 80, 80), width=1,
                        )
                        draw.text(
                            (4, ey1 + 1),
                            _MISSING_LABEL.get(field, field.upper() + "?"),
                            fill=(120, 120, 120),
                        )

            crop = crops.get(field)
            if crop is not None:
                x1, vy1, x2, vy2 = crop
                draw.rectangle(
                    [(x1, vy1), (x2, vy2)],
                    outline=(255, 0, 255), width=2,
                )

            # ── Per-glyph bboxes (from _ocr_value_crop's segmenter) ──
            # Drawn ON TOP of the value-crop magenta rectangle in real
            # (x, y, w, h) extents — surfaces fused pairs, missed dots,
            # vertical drift, and count anomalies independently of the
            # OCR text read. Green when segmented count falls in the
            # field-aware expected range; red on mismatch so the user
            # can spot disagreement at a glance.
            #
            # Field-aware tolerance avoids false alarms for normal
            # render artefacts that aren't actual segmentation bugs:
            #   * mass:        commas (segmenter naturally skips them)
            #   * resistance:  trailing "%" (segmenter picks it up but
            #                  the digit-only CRNN drops it — allow
            #                  +1 over the CRNN digit count).
            #   * instability: decimal point (segmenter should pick it
            #                  up as a small span — strict match against
            #                  CRNN text length INCLUDING the dot).
            # A real segmenter regression on instability (missed leading
            # "1" or fused-dot mega-blob) still shows red here.
            gb_entry = _state.get("glyph_boxes", {}).get(field) if crop is not None else None
            n_seg: int = 0
            n_text: int = 0
            n_max: int = 0
            count_match: bool = True
            if gb_entry is not None:
                gb_ts = float(gb_entry.get("ts") or 0)
                if (_now_render - gb_ts) <= _GLYPH_BOXES_TTL_SEC:
                    boxes_crop_rel = gb_entry.get("boxes") or []
                    n_seg = len(boxes_crop_rel)
                    ocr_for_count = (ocrs.get(field) or {}).get("text", "")
                    _ocr_visible = (ocr_for_count or "").strip().replace(",", "")
                    if field == "resistance":
                        # CRNN may or may not emit the % — count digits
                        # only and let segmenter range be [n, n+1].
                        _digits_only = "".join(
                            c for c in _ocr_visible if c.isdigit()
                        )
                        n_text = len(_digits_only)
                        n_max = n_text + 1  # tolerate seg picking up "%"
                    elif field == "instability":
                        # CRNN includes the dot in its output; segmenter
                        # should too. Strict match — under/over-count is
                        # a real bug (e.g. missed leading "1" or
                        # fused-dot mega-blob).
                        n_text = len(_ocr_visible)
                        n_max = n_text
                    else:
                        # mass and other digit-only fields
                        n_text = len(_ocr_visible)
                        n_max = n_text
                    count_match = (
                        n_text == 0
                        or (n_text <= n_seg <= n_max)
                    )
                    _box_color = (51, 221, 136) if count_match else (255, 100, 100)
                    x1c, vy1c, _x2c, _vy2c = crop
                    for (gx, gy, gw, gh) in boxes_crop_rel:
                        px1 = x1c + int(gx)
                        py1 = vy1c + int(gy)
                        px2 = px1 + int(gw)
                        py2 = py1 + int(gh)
                        draw.rectangle(
                            [(px1, py1), (px2, py2)],
                            outline=_box_color, width=1,
                        )

            # Status string for this field
            status_parts = []
            ocr = ocrs.get(field)
            if ocr is not None:
                status_parts.append(
                    f"text={ocr['text']!r} mc={ocr['min_conf']:.2f}"
                )
            # Glyph-count badge after the text/conf. Always show seg
            # count when we have it. ✓ when seg falls in the field-
            # aware expected range; ✗ on a real mismatch. For
            # resistance the badge shows the range "seg=2/1..2" so the
            # user can see the tolerated extra count (the "%" glyph).
            if n_seg or gb_entry is not None:
                if n_text > 0:
                    _marker = "✓" if count_match else "✗"
                    if n_max > n_text:
                        status_parts.append(
                            f"seg={n_seg}/{n_text}..{n_max}{_marker}"
                        )
                    else:
                        status_parts.append(f"seg={n_seg}/{n_text}{_marker}")
                else:
                    status_parts.append(f"seg={n_seg}")
            lock = locks.get(field)
            if lock is not None:
                if lock.get("invalidated"):
                    status_parts.append("LOCK_INVALIDATED")
                elif lock.get("value") is not None:
                    status_parts.append(f"LOCKED={lock['value']}")
            if status_parts and row is not None:
                ty = max(0, row["y1"] - 11)
                draw.text((W // 2 - 80, ty), " ".join(status_parts), fill=(255, 200, 0))

        # ── THE SPINE ──────────────────────────────────────────────
        # Draw the single rigid-body pose as a backbone: one vertical
        # line with a node at every anchor's POSE-PREDICTED center
        # (title / mineral / mass / resistance / instability). Beside
        # each node, the part's OWN detection: a node sitting ON the
        # spine = seated; a red offset bar = a part that tried to
        # wander (and was snapped back). If everything is truly one
        # body, every node is strung on one straight line. This is the
        # user's "can I SEE it tied together" — drawn last, on top.
        try:
            pp = _state.get("panel_pose")
            if pp and (_now_render - float(pp.get("ts") or 0)) < 8.0:
                nodes = pp.get("nodes") or []
                if nodes:
                    sx = max(6, int(pp["origin"][0]) - 10)
                    ys = [n[2] for n in nodes]
                    y_top, y_bot = min(ys) - 6, max(ys) + 6
                    # backbone
                    draw.line([(sx, y_top), (sx, y_bot)],
                              fill=(0, 255, 255), width=2)
                    _abbr = {"title": "T", "_mineral_row": "Mn",
                             "mineral": "Mn", "mass": "Ma",
                             "resistance": "R", "instability": "I"}
                    seated = 0
                    for name, _px, py, ay in nodes:
                        # pose node on the spine
                        draw.ellipse([sx - 4, py - 4, sx + 4, py + 4],
                                     outline=(0, 255, 255), fill=(0, 60, 70))
                        draw.line([(sx, py), (sx + 16, py)],
                                  fill=(0, 255, 255), width=1)
                        draw.text((sx - 22, py - 6),
                                  _abbr.get(name, name[:2]),
                                  fill=(0, 255, 255))
                        if ay is not None:
                            off = ay - py
                            if abs(off) <= 8:
                                seated += 1
                                draw.ellipse(
                                    [sx + 18, py - 3, sx + 24, py + 3],
                                    fill=(0, 230, 100))  # seated = green
                            else:
                                # strayed: red bar at actual, connector
                                draw.line([(sx, py), (sx + 30, ay)],
                                          fill=(255, 60, 60), width=2)
                                draw.line(
                                    [(sx + 22, ay), (sx + 40, ay)],
                                    fill=(255, 60, 60), width=2)
                                draw.text((sx + 42, ay - 6),
                                          "%+d" % off, fill=(255, 90, 90))
                    rej = pp.get("rejected") or []
                    _stab = pp.get("stab")
                    draw.text(
                        (sx - 4, y_top - 13),
                        "SPINE scale=%.0f seated=%d/%d%s%s" % (
                            pp.get("scale", 0), seated, len(nodes),
                            (" %s" % _stab.upper()) if _stab else "",
                            (" rej=%s" % rej) if rej else ""),
                        fill=(0, 255, 255))
        except Exception:
            pass

        # Atomic write: tmp + rename so the viewer never reads a half-written file.
        # ``format="PNG"`` is required because PIL infers the format from the
        # file extension — a ``.tmp`` suffix raises ``unknown file extension``
        # and silently leaves the overlay stuck on the import-time placeholder.
        tmp = OUT_PATH + ".tmp"
        overlay.save(tmp, format="PNG")
        try:
            os.replace(tmp, OUT_PATH)
        except OSError:
            # Windows can race; best-effort fallback
            overlay.save(OUT_PATH, format="PNG")
    except Exception as exc:
        # Warning (not debug) so a broken overlay pipeline surfaces in the
        # normal app log instead of needing DEBUG-level logging to diagnose.
        log.warning("debug_overlay.write failed: %s", exc)


def _self_test_write_placeholder() -> None:
    """Write a placeholder PNG on first import so the viewer immediately
    confirms the path + write permissions are working. Subsequent
    real scans overwrite this with actual data."""
    try:
        ph = Image.new("RGB", (400, 80), (30, 35, 45))
        d = ImageDraw.Draw(ph)
        d.text((10, 10), "debug_overlay placeholder", fill=(180, 180, 180))
        d.text((10, 30), f"path: {OUT_PATH}", fill=(120, 200, 120))
        d.text((10, 50), "waiting for first OCR scan...", fill=(120, 120, 120))
        ph.save(OUT_PATH)
    except Exception as exc:
        log.warning("debug_overlay placeholder write failed: %s", exc)


# Fire once at import time
_self_test_write_placeholder()
