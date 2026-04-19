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


def set_panel_finder(
    top_y: Optional[int] = None,
    mineral_y_top: Optional[int] = None,
    mineral_y_bot: Optional[int] = None,
    mineral_center: Optional[int] = None,
    pitch: Optional[int] = None,
    bot_line_y: Optional[int] = None,
    source: str = "",
) -> None:
    _state["panel_finder"] = {
        "top_line_y": top_y,
        "mineral_y_top": mineral_y_top,
        "mineral_y_bot": mineral_y_bot,
        "mineral_center": mineral_center,
        "pitch": pitch,
        "bot_line_y": bot_line_y,
        "source": source,  # "by_position" or "tesseract_fallback"
    }


def set_label_rows(rows: dict[str, tuple[int, int, int]]) -> None:
    _state["label_rows"] = {
        k: {"y1": int(y1), "y2": int(y2), "label_right": int(lr)}
        for k, (y1, y2, lr) in (rows or {}).items()
    }


def set_value_crop(field: str, box: tuple[int, int, int, int]) -> None:
    _state.setdefault("value_crops", {})[field] = tuple(int(v) for v in box)


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
    """Render and atomically save the annotated overlay PNG."""
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
        # ── Top line marker (orange) ──
        if pf.get("top_line_y") is not None:
            ty = pf["top_line_y"]
            draw.line([(0, ty), (W - 1, ty)], fill=(255, 140, 0), width=1)
            draw.text((4, ty + 1), "TOP_LINE", fill=(255, 140, 0))

        # ── Mineral name band (green) ──
        if pf.get("mineral_y_top") is not None and pf.get("mineral_y_bot") is not None:
            mt, mb = pf["mineral_y_top"], pf["mineral_y_bot"]
            draw.rectangle([(0, mt), (W - 1, mb)], outline=(0, 230, 100), width=1)
            draw.text((4, mt - 11), "MINERAL", fill=(0, 230, 100))

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
        for field in ("mass", "resistance", "instability"):
            row = rows.get(field)
            if row is not None:
                y1, y2, lr = row["y1"], row["y2"], row["label_right"]
                draw.rectangle(
                    [(0, y1), (W - 1, y2)],
                    outline=(0, 200, 255), width=1,
                )
                draw.text((4, y1 + 1), field.upper(), fill=(0, 200, 255))
                # Mark the shared label_right (value-column-left anchor)
                draw.line([(lr, y1), (lr, y2)], fill=(255, 100, 100), width=1)

            crop = crops.get(field)
            if crop is not None:
                x1, vy1, x2, vy2 = crop
                draw.rectangle(
                    [(x1, vy1), (x2, vy2)],
                    outline=(255, 0, 255), width=2,
                )

            # Status string for this field
            status_parts = []
            ocr = ocrs.get(field)
            if ocr is not None:
                status_parts.append(
                    f"text={ocr['text']!r} mc={ocr['min_conf']:.2f}"
                )
            lock = locks.get(field)
            if lock is not None:
                if lock.get("invalidated"):
                    status_parts.append("LOCK_INVALIDATED")
                elif lock.get("value") is not None:
                    status_parts.append(f"LOCKED={lock['value']}")
            if status_parts and row is not None:
                ty = max(0, row["y1"] - 11)
                draw.text((W // 2 - 80, ty), " ".join(status_parts), fill=(255, 200, 0))

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
