"""Mining HUD OCR calibration dialog.

User flow:
  1. Open dialog (from "Calibrate Mining Crops" button in scan bar)
  2. Tool starts streaming the live OCR-detected crops in real time
  3. For each row (Resource / Mass / Resistance / Instability):
       - User adjusts the bounding box if needed (drag to resize)
       - When the crop "looks right", user clicks "🔒 Lock <ROW>"
       - Lock button turns GREEN, that row is saved to disk immediately
  4. When all 3 required rows are locked, "CALIBRATION COMPLETE"
     banner appears in big text
  5. User closes dialog; runtime now uses the saved coords directly,
     skipping all detection

Tabs:
  • Calibrate — the live streaming + lock UI
  • Tutorial — how-to text + screenshot examples
  • (future) Voice — narrated walkthrough via Wingman TTS

The dialog is intentionally large + non-modal so the user can
continue to see the actual game HUD beside it for visual comparison.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import (
    QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFrame, QGraphicsPixmapItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QStatusBar, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ocr.sc_ocr import calibration
from ocr.sc_ocr.calibration import DISPLAY_NAMES, FIELD_NAMES

from .theme import ACCENT

log = logging.getLogger(__name__)

# What a PySide6 widget/timer/signal call can actually raise. RuntimeError is
# the important one: PySide6 raises "Internal C++ object already deleted" for
# any call on a wrapper whose C++ side Qt has destroyed, which is the real
# reason these calls were wrapped at all. AttributeError covers an attribute
# that construction never got as far as creating; TypeError/ValueError cover
# a bad argument or an out-of-range value. Anything outside this set is a
# real defect and now propagates instead of vanishing.
_QT_CALL_ERRORS = (RuntimeError, AttributeError, TypeError, ValueError)

# Reading the OCR pipeline's shared debug_overlay state. The module may be
# absent in a trimmed build (ImportError) or predate a key (AttributeError);
# the dicts and tuples are written by the scan thread, so a missing key, a
# short tuple, a None where a number belongs, or an unparseable value are
# all reachable (KeyError, IndexError, TypeError, ValueError).
_OVERLAY_READ_ERRORS = (
    ImportError, AttributeError, KeyError, IndexError, TypeError, ValueError,
)

LOCK_GREEN = "#2a8"
LOCK_GRAY = "#555"
PANEL_BG = "#1d2530"
TEXT_PRIMARY = "#cbd"
TEXT_DIM = "#888"

# Polling interval for the live preview
POLL_MS = 400

# Cadence for the continuous "preview shows the live screen at the
# calibrated crop position" refresh timer. Independent of the OCR scan
# loop — when the user has scanning paused, this timer still re-crops
# the latest screen capture so the boxes are visibly persistent. ~1 s
# is the sweet spot: visible drift between updates is still below the
# threshold where the boxes feel "frozen", but we steal half as much
# GUI-thread time as 500 ms (interactions stay responsive).
LIVE_REFRESH_MS = 1500

# Window during which a freshly-arrived broadcast crop suppresses the
# timer's fallback render. The broadcast crop is the EXACT image OCR
# saw, so when one arrives we want to show it (not the timer's slightly
# different re-crop) until it's clearly stale.
BROADCAST_FRESHNESS_S = 1.0

# How often the live-refresh tick re-runs the heavy label-detection
# pass (`_find_label_rows`). Multi-scale NCC against a fresh capture is
# ~20-100 ms — fine once in a while, lethal at 2 fps. The HUD doesn't
# move within a single calibration session, so we cache the detected
# rows for several seconds and reuse them between detect runs. Locked
# rows skip detection entirely; this only matters in auto-detect mode.
LABEL_DETECT_INTERVAL_S = 3.0

# After any user input event (click / key / wheel) on the dialog or any
# of its children, suppress the next live-refresh tick for this many
# milliseconds. Keeps the GUI thread free for the input handler so
# slider drags / spinner clicks / scrolling feel snappy instead of
# fighting the screen-capture pipeline for CPU.
INPUT_PAUSE_MS = 400

# Field colors (matches debug_overlay)
FIELD_COLORS: dict[str, tuple[int, int, int]] = {
    "_mineral_row":   (0, 230, 100),
    "mass":           (0, 200, 255),
    "resistance":     (200, 100, 255),
    "instability":    (255, 100, 200),
    # Signature uses a warm amber so it visually separates from the
    # HUD rows (cool blues / pinks) — the user reads them as different
    # data sources.
    "signature":      (255, 180, 80),
    # Needle (difficulty bar) — yellow so it visually distinguishes
    # from every other HUD row. The difficulty bar is the bottom-most
    # element of the SCAN RESULTS panel and is its own thing.
    "needle":         (255, 240, 100),
}

# Mouse-drag throttle: minimum interval (ms) between successive
# ``box_changed`` emits during an in-progress drag. Mouse moves can fire
# at hundreds of Hz; without throttling we'd flood the OCR pipeline with
# re-crop requests. ~33 ms ≈ 30 Hz, smooth visually and well below
# anything the OCR pipeline cares about.
DRAG_EMIT_THROTTLE_MS = 33


class _CropPreview(QLabel):
    """Renders a single row's current value crop with a colored border.

    Sized to scale with the parent dialog: minimum is small enough that
    a compact 480×420 dialog still shows usable previews, and there's
    no max height — the preview grows when the dialog is enlarged. The
    last PIL crop is cached so ``resizeEvent`` can re-render it
    instantly on drag-resize (without waiting for the 400 ms poll tick).

    Mouse-drag: left-click + drag inside the preview translates the
    underlying box (x, y) so the user can re-position the crop without
    using the arrow nudges. The preview shows a SCALED copy of the
    PIL crop, so widget-pixel deltas must be inverted through the
    scale ratio to land in image-pixel units. See ``_drag_scale_factor``.
    """

    # Drag-delta in IMAGE-PIXEL units: (field, dx, dy). The owning
    # ``_RowControl`` connects this to apply the delta to its own box
    # and emit ``box_changed`` (matching the arrow nudges' contract).
    drag_delta = Signal(str, int, int)

    def __init__(self, field: str, parent=None):
        super().__init__(parent)
        self._field = field
        # Per-field minimum height. The needle row's crop covers the
        # full MASS / RESISTANCE / INSTABILITY band (placeholder is
        # 280x130 px in image coords) — at the default 36-px minimum
        # the aspect-fit scaler shrinks it to a thumbnail too small to
        # read. 160 px gives the needle preview enough room to render
        # the M/R/I rows at near-native scale so the user can actually
        # verify what's being captured. Other rows keep the small floor
        # because their crops are single-line value strips that read
        # fine at 36 px.
        _min_h = 160 if field == "needle" else 36
        self.setMinimumSize(180, _min_h)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        color = FIELD_COLORS.get(field, (200, 200, 200))
        self.setStyleSheet(
            f"background: #111; border: 2px solid rgb{color}; "
            "color: #666; font-family: Consolas; font-size: 9pt;"
        )
        self.setText("(no crop yet)")
        # Cache last image so resizeEvent can re-render at the new size
        # without waiting for the next poll tick.
        self._last_pil: Optional[Image.Image] = None

        # ── Mouse-drag state ──
        # ``_dragging`` flips True on left-press inside the preview and
        # back to False on release; mouseMoveEvent uses it as a guard.
        # ``_drag_anchor_widget`` is the press-time mouse position in
        # widget coords, snapshotted so we can compute total delta from
        # the press point each move (rather than incremental deltas
        # which would compound rounding errors).
        # ``_drag_emitted_dx/dy`` track how much we've already emitted
        # in image-px units so each move only emits the *new* delta.
        # ``_drag_disabled`` is set by the row when locked — drag is a
        # no-op then, mirroring how arrow nudges no-op when locked.
        # ``_last_emit_ms`` throttles emit cadence to ~30 Hz.
        self._dragging: bool = False
        self._drag_anchor_widget: Optional[QPoint] = None
        self._drag_emitted_dx: int = 0
        self._drag_emitted_dy: int = 0
        self._drag_disabled: bool = False
        self._last_emit_ms: int = 0
        # Cursor reflects whether drag is allowed.
        self.setCursor(Qt.SizeAllCursor)

    def update_crop(self, pil: Optional[Image.Image]) -> None:
        if pil is None:
            self._last_pil = None
            self.setText("(no crop yet)")
            return
        # Needle row only: bake "NOT SCANNED" / "SCAN" zone labels onto
        # the PIL image as visual reference markers. The labels are
        # drawn once (here) rather than as Qt overlays so they survive
        # any future re-render path and stay aligned with the image.
        if self._field == "needle":
            try:
                pil = self._add_needle_zone_labels(pil)
            except (AttributeError, OSError, IndexError,
                    TypeError, ValueError) as exc:
                # Overlay drawing must never break the preview itself, so
                # the un-annotated image is used -- but the draw failure is
                # recorded rather than looking like "no needle zones".
                log.debug("needle-zone label overlay failed: %r", exc)
        self._last_pil = pil
        self._render_cached()

    @staticmethod
    def _add_needle_zone_labels(pil: Image.Image) -> Image.Image:
        """Overlay the needle preview with zone-calibration markers:

          * Three full-height vertical lines at 25 / 50 / 75 % width,
            so the user can see the bar's quarter divisions when the
            in-game needle isn't currently rendered (impossible rocks,
            no laser applied) and still calibrate by quartile.
          * Big, obvious 'NOT SCANNED' / 'SCAN' badges centered over
            the two halves so left vs right is unmistakable.
          * Subtle red / green half-strip tints at the very top edge
            so even at small preview sizes the side designation reads
            at a glance.

        Drawn on a copy — caller's PIL image untouched."""
        from PIL import ImageDraw
        out = pil.copy().convert("RGB")
        draw = ImageDraw.Draw(out)
        W, H = out.size
        if W < 12 or H < 12:
            return out

        # ── Font sizing: scale to crop height, but cap so labels stay
        # readable on tall previews without dominating the image. ──
        font_size = max(11, min(22, H // 8))
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            try:
                from PIL import ImageFont
                font = ImageFont.load_default()
            except Exception:
                font = None

        def _tw(t: str) -> int:
            if font is None:
                return len(t) * 6
            try:
                bbox = font.getbbox(t)
                return int(bbox[2] - bbox[0])
            except Exception:
                return len(t) * 6

        # ── Top edge tint strips (red left / green right) so the side
        # is obvious at a glance even when labels are hard to read. ──
        tint_h = max(4, font_size // 3)
        draw.rectangle([(0, 0), (W // 2, tint_h)], fill=(140, 40, 40))
        draw.rectangle([(W // 2, 0), (W, tint_h)], fill=(40, 120, 40))

        # ── Vertical guide lines, full crop height, at 25% / 50% / 75%.
        # Color-coded: 25% → bright red (deep "NOT SCANNED" zone),
        # 50% → white (zone divider, where the needle would sit at
        # mid-power), 75% → bright green (deep "SCAN" zone). ──
        line_color_25 = (220, 60, 60)     # red
        line_color_50 = (240, 240, 240)   # white center divider
        line_color_75 = (60, 200, 60)     # green
        x_25 = W // 4
        x_50 = W // 2
        x_75 = (3 * W) // 4
        # Width=2 so they're visible on top of detailed game content
        # without obscuring the underlying pixels too much.
        draw.line([(x_25, 0), (x_25, H - 1)], fill=line_color_25, width=2)
        draw.line([(x_50, 0), (x_50, H - 1)], fill=line_color_50, width=2)
        draw.line([(x_75, 0), (x_75, H - 1)], fill=line_color_75, width=2)

        # ── Zone labels: BIG, centered over each half, with a solid
        # background plate so they stay legible on busy game content.
        left_text = "NOT SCANNED"
        right_text = "SCAN"
        left_w = _tw(left_text)
        right_w = _tw(right_text)
        pad_x = 8
        pad_y = 3
        badge_h = font_size + pad_y * 2

        # LEFT label — center horizontally in left half
        lx0 = max(0, (W // 4) - (left_w // 2) - pad_x)
        lx1 = min(W // 2 - 2, lx0 + left_w + pad_x * 2)
        ly0 = tint_h + 2
        ly1 = ly0 + badge_h
        draw.rectangle([(lx0, ly0), (lx1, ly1)], fill=(160, 30, 30))
        # White outline so the badge stands out from the dark game UI
        draw.rectangle(
            [(lx0, ly0), (lx1, ly1)], outline=(255, 255, 255), width=1,
        )
        if font is not None:
            draw.text(
                (lx0 + pad_x, ly0 + pad_y),
                left_text, fill=(255, 255, 255), font=font,
            )
        else:
            draw.text(
                (lx0 + pad_x, ly0 + pad_y),
                left_text, fill=(255, 255, 255),
            )

        # RIGHT label — center horizontally in right half
        rx0 = max(W // 2 + 2, (3 * W) // 4 - (right_w // 2) - pad_x)
        rx1 = min(W - 1, rx0 + right_w + pad_x * 2)
        ry0 = tint_h + 2
        ry1 = ry0 + badge_h
        draw.rectangle([(rx0, ry0), (rx1, ry1)], fill=(30, 130, 30))
        draw.rectangle(
            [(rx0, ry0), (rx1, ry1)], outline=(255, 255, 255), width=1,
        )
        if font is not None:
            draw.text(
                (rx0 + pad_x, ry0 + pad_y),
                right_text, fill=(255, 255, 255), font=font,
            )
        else:
            draw.text(
                (rx0 + pad_x, ry0 + pad_y),
                right_text, fill=(255, 255, 255),
            )

        return out

    def _render_cached(self) -> None:
        pil = self._last_pil
        if pil is None:
            return
        # Scale to fit BOTH dimensions, keep aspect ratio. Earlier
        # version only used height — that left the preview empty on
        # the sides when the dialog was wide-but-short.
        avail_w = max(40, self.width() - 6)
        avail_h = max(24, self.height() - 6)
        src_w = max(1, pil.width)
        src_h = max(1, pil.height)
        ratio = min(avail_w / src_w, avail_h / src_h)
        new_w = max(20, int(src_w * ratio))
        new_h = max(12, int(src_h * ratio))
        try:
            scaled = pil.resize((new_w, new_h), Image.NEAREST)
            self.setPixmap(QPixmap.fromImage(ImageQt(scaled.convert("RGB"))))
        except (OSError, MemoryError) + _QT_CALL_ERRORS as exc:
            # Leaves the previous pixmap on screen, as before.
            log.debug("preview pixmap render failed: %r", exc)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-scale the cached image to the new label size immediately
        # so dragging the dialog edge looks smooth (no 400 ms pop).
        if self._last_pil is not None:
            self._render_cached()

    # ── Mouse-drag plumbing ─────────────────────────────────────────

    def set_drag_disabled(self, disabled: bool) -> None:
        """Owner toggles drag on/off (e.g. when row is locked).

        When disabled, mouse-press silently no-ops and the cursor
        reverts to the default arrow so the move-affordance disappears.
        """
        self._drag_disabled = bool(disabled)
        if self._drag_disabled:
            self.setCursor(Qt.ArrowCursor)
            # Cancel any in-flight drag so a row that locks mid-drag
            # doesn't keep emitting deltas.
            self._dragging = False
            self._drag_anchor_widget = None
        else:
            self.setCursor(Qt.SizeAllCursor)

    def _drag_scale_factor(self) -> float:
        """Image-px per widget-px during a drag.

        The preview displays the row's PIL crop scaled by ``ratio`` (see
        ``_render_cached`` — same min(avail_w/src_w, avail_h/src_h)
        formula). So 1 widget-px == 1/ratio image-px. We invert that
        ratio here. Falls back to 1.0 if the cached PIL is missing
        (shouldn't happen during drag because the press path won't
        engage without a crop), so the math doesn't divide-by-zero.
        """
        pil = self._last_pil
        if pil is None:
            return 1.0
        src_w = max(1, pil.width)
        src_h = max(1, pil.height)
        avail_w = max(40, self.width() - 6)
        avail_h = max(24, self.height() - 6)
        ratio = min(avail_w / src_w, avail_h / src_h)
        if ratio <= 0:
            return 1.0
        return 1.0 / ratio

    def mousePressEvent(self, event):
        # Right / middle clicks: explicitly ignore — no context menu,
        # no drag, no nothing. Per task spec: "simplest interpretation".
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        if self._drag_disabled:
            super().mousePressEvent(event)
            return
        # No-op when there's no crop to drag. Without a cached PIL we
        # don't have a sensible scale factor and there's also nothing
        # visible to anchor the user's drag against.
        if self._last_pil is None:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self._drag_anchor_widget = event.pos()
        self._drag_emitted_dx = 0
        self._drag_emitted_dy = 0
        self._last_emit_ms = 0
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging or self._drag_disabled:
            super().mouseMoveEvent(event)
            return
        if not (event.buttons() & Qt.LeftButton):
            # Lost the button somewhere — release acts as a defensive end.
            self._dragging = False
            self._drag_anchor_widget = None
            super().mouseMoveEvent(event)
            return
        if self._drag_anchor_widget is None:
            super().mouseMoveEvent(event)
            return
        # Throttle: monotonic-ms compare. This approach is simpler than
        # juggling a QTimer.singleShot pending state, and good enough at
        # ~30 Hz (the user's cursor doesn't notice a 33 ms pause between
        # emits because each emit triggers a re-crop that takes longer).
        try:
            import time as _time
            now_ms = int(_time.monotonic() * 1000)
        except Exception:
            now_ms = 0
        if now_ms and (now_ms - self._last_emit_ms) < DRAG_EMIT_THROTTLE_MS:
            event.accept()
            return
        # Total widget-px delta from the press point.
        delta_w = event.pos() - self._drag_anchor_widget
        scale = self._drag_scale_factor()
        total_dx_img = int(round(delta_w.x() * scale))
        total_dy_img = int(round(delta_w.y() * scale))
        # Emit only the INCREMENT since the last emit. The owning row's
        # nudge logic clamps and re-emits a full box, so each delta is
        # applied once and the accumulated total tracks press-to-now.
        inc_dx = total_dx_img - self._drag_emitted_dx
        inc_dy = total_dy_img - self._drag_emitted_dy
        if inc_dx == 0 and inc_dy == 0:
            event.accept()
            return
        self._drag_emitted_dx = total_dx_img
        self._drag_emitted_dy = total_dy_img
        self._last_emit_ms = now_ms
        try:
            self.drag_delta.emit(self._field, inc_dx, inc_dy)
        except _QT_CALL_ERRORS as exc:
            log.debug("drag_delta emit failed (field=%s): %r",
                      self._field, exc)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._dragging:
            self._dragging = False
            self._drag_anchor_widget = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _RowControl(QGroupBox):
    """One row: live preview + nudge controls + lock button + status."""

    locked = Signal(str, dict)   # field_name, {"x":..,"y":..,"w":..,"h":..}
    unlocked = Signal(str)
    box_changed = Signal(str, dict)  # emitted when user nudges box (re-crop request)

    def __init__(self, field: str, parent=None):
        super().__init__(DISPLAY_NAMES.get(field, field), parent)
        self._field = field
        self._is_locked = False
        self._is_manual = False    # user has nudged → freeze live updates
        self._latest_box: Optional[dict] = None

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 18, 8, 8)
        v.setSpacing(4)

        self._preview = _CropPreview(field)
        # Mouse-drag inside the preview translates the row's box. The
        # preview emits delta in IMAGE-PIXEL units (it owns the scale
        # factor since it owns the rendered pixmap); we apply the delta
        # via the same path as the arrow nudges so the contract is
        # identical (clamp to non-negative, mark manual, fire
        # ``box_changed`` for the parent dialog to re-crop).
        self._preview.drag_delta.connect(self._on_preview_drag)
        v.addWidget(self._preview)

        # ── Nudge controls row ──
        nudge = QHBoxLayout()
        nudge.setSpacing(2)

        nudge.addWidget(self._make_nudge_label("MOVE:"))
        self._btn_left  = self._make_nudge_btn("←", -1,  0,  0,  0,
            "Move crop LEFT 1 px (Shift+click = 5 px)")
        self._btn_up    = self._make_nudge_btn("↑",  0, -1,  0,  0,
            "Move crop UP 1 px (Shift+click = 5 px)")
        self._btn_down  = self._make_nudge_btn("↓",  0, +1,  0,  0,
            "Move crop DOWN 1 px (Shift+click = 5 px)")
        self._btn_right = self._make_nudge_btn("→", +1,  0,  0,  0,
            "Move crop RIGHT 1 px (Shift+click = 5 px)")
        for b in (self._btn_left, self._btn_up, self._btn_down, self._btn_right):
            nudge.addWidget(b)

        nudge.addSpacing(10)
        nudge.addWidget(self._make_nudge_label("RESIZE:"))
        self._btn_wider   = self._make_nudge_btn("W+",  0,  0, +1,  0,
            "Make crop WIDER (extend right edge by 1 px)")
        self._btn_narrow  = self._make_nudge_btn("W−",  0,  0, -1,  0,
            "Make crop NARROWER")
        self._btn_taller  = self._make_nudge_btn("H+",  0,  0,  0, +1,
            "Make crop TALLER")
        self._btn_shorter = self._make_nudge_btn("H−",  0,  0,  0, -1,
            "Make crop SHORTER")
        for b in (self._btn_wider, self._btn_narrow, self._btn_taller, self._btn_shorter):
            nudge.addWidget(b)

        nudge.addStretch(1)

        # ✏ Set — manually seed a placeholder crop when auto-detection
        # has rejected this row. Without this, the user has nothing to
        # nudge ("Cannot nudge: no crop detected yet"). Clicking ✏ Set
        # drops a sensible default rectangle into the row's box and
        # flips into manual mode so the user can use the existing nudge
        # arrows + Lock to position it precisely.
        self._btn_seed_manual = QPushButton("✏ Set")
        self._btn_seed_manual.setToolTip(
            "Set a starting crop rectangle when auto-detection failed. "
            "Then use ← ↑ ↓ → and W± H± to position, click Lock to save."
        )
        self._btn_seed_manual.setStyleSheet(
            "QPushButton { background: #335577; color: #cce0ff; padding: 2px 6px; "
            "border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #4477aa; }"
        )
        self._btn_seed_manual.clicked.connect(self._on_seed_manual)
        nudge.addWidget(self._btn_seed_manual)

        self._btn_reset_live = QPushButton("↻ Auto")
        self._btn_reset_live.setToolTip(
            "Discard manual adjustments, return to live auto-detection"
        )
        self._btn_reset_live.setStyleSheet(
            "QPushButton { background: #444; color: #ccc; padding: 2px 6px; "
            "border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #666; }"
        )
        self._btn_reset_live.clicked.connect(self._on_reset_to_live)
        nudge.addWidget(self._btn_reset_live)

        v.addLayout(nudge)

        # ── Status + lock row ──
        row = QHBoxLayout()
        self._status = QLabel("Waiting for crop…")
        self._status.setStyleSheet(
            f"color: {TEXT_DIM}; font-family: Consolas; font-size: 9pt;"
        )
        row.addWidget(self._status, 1)

        self._lock_btn = QPushButton("🔒 Lock")
        self._lock_btn.setCursor(Qt.PointingHandCursor)
        self._lock_btn.setMinimumWidth(120)
        self._apply_lock_style(False)
        self._lock_btn.clicked.connect(self._on_lock_toggle)
        row.addWidget(self._lock_btn)

        v.addLayout(row)

    def _make_nudge_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {TEXT_DIM}; font-family: Consolas; font-size: 8pt;"
        )
        return lbl

    def _make_nudge_btn(
        self, text: str, dx: int, dy: int, dw: int, dh: int, tooltip: str,
    ) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedSize(32, 26)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(
            f"QPushButton {{ background: {LOCK_GRAY}; color: white; "
            "border: none; font-family: Consolas; font-size: 10pt; "
            "font-weight: bold; }}"
            "QPushButton:hover { background: #777; }"
            "QPushButton:pressed { background: #444; }"
        )
        btn.setAutoRepeat(True)
        btn.setAutoRepeatInterval(60)
        btn.setAutoRepeatDelay(350)
        btn.clicked.connect(
            lambda _checked, dx=dx, dy=dy, dw=dw, dh=dh:
                self._nudge(dx, dy, dw, dh)
        )
        return btn

    def update_live(self, pil: Optional[Image.Image], box: Optional[dict]) -> None:
        """Called every poll tick by the parent dialog."""
        if self._is_locked or self._is_manual:
            return  # don't overwrite locked / manually-nudged preview
        self._preview.update_crop(pil)
        self._latest_box = box
        if box is None:
            if self._field == "needle":
                # The needle row has no auto-detect: the difficulty crop
                # is computed inside api.py (currently as a left-half
                # fallback) rather than derived from a label-row anchor.
                # When the broadcast delivers a non-None pil, the OCR IS
                # producing a crop — saying "crop not detected" would be
                # wrong. Tell the user what's actually happening: they
                # see the live OCR crop until they click Set to customize.
                if pil is not None:
                    self._status.setText(
                        "(showing live OCR crop — click ✏ Set to seed "
                        "your own rectangle)"
                    )
                else:
                    self._status.setText(
                        "(no scan yet — start scanning so the OCR "
                        "produces a crop)"
                    )
            elif self._field == "signature":
                self._status.setText(
                    "(crop not detected — check signal scanner region)"
                )
            else:
                self._status.setText(
                    "(crop not detected — check HUD region)"
                )
        else:
            self._status.setText(
                f"x={box['x']} y={box['y']} w={box['w']} h={box['h']}"
            )

    def update_manual(self, pil: Optional[Image.Image], box: dict) -> None:
        """Called when the parent re-crops after a nudge (manual mode)."""
        self._latest_box = box
        self._preview.update_crop(pil)
        self._status.setText(
            f"MANUAL: x={box['x']} y={box['y']} w={box['w']} h={box['h']}"
        )

    def refresh_preview_image(self, pil: Optional[Image.Image]) -> None:
        """Lightweight preview-only refresh used by the dialog's
        live-refresh timer (between scans).

        Unlike :meth:`update_live` / :meth:`update_manual`, this method
        deliberately leaves ``_latest_box`` and the status text
        untouched: the timer just re-crops the SAME box (locked,
        seeded-manual, or auto-detected) from the most recent screen
        capture so the user sees the live screen content at that crop
        position. Status text continues to reflect the authoritative
        source (LOCKED / MANUAL / detected coords).
        """
        if pil is None:
            return
        self._preview.update_crop(pil)

    def get_latest_box(self) -> Optional[dict]:
        """Public accessor for the row's currently active box, or None
        if no box is known yet. Used by the dialog's live-refresh timer
        to decide whether the row already has a box to crop with."""
        return self._latest_box

    def is_manual(self) -> bool:
        return self._is_manual

    def _nudge(self, dx: int, dy: int, dw: int, dh: int) -> None:
        """User clicked an arrow / resize button. Adjust the box by
        the requested deltas and ask the parent to re-crop."""
        if self._is_locked:
            return
        if self._latest_box is None:
            self._status.setText("Cannot nudge: no crop detected yet")
            return
        # Honor Shift modifier for 5× steps
        try:
            mods = QApplication.keyboardModifiers()
            if mods & Qt.ShiftModifier:
                dx, dy, dw, dh = dx * 5, dy * 5, dw * 5, dh * 5
        except _QT_CALL_ERRORS as exc:
            # Falls through to the un-multiplied step, as before.
            log.debug("nudge shift-modifier probe failed: %r", exc)
        box = dict(self._latest_box)
        box["x"] = max(0, box["x"] + dx)
        box["y"] = max(0, box["y"] + dy)
        box["w"] = max(4, box["w"] + dw)
        box["h"] = max(4, box["h"] + dh)
        self._latest_box = box
        self._is_manual = True
        # Ask parent to re-crop the panel image with the new box
        self.box_changed.emit(self._field, dict(box))

    def _on_preview_drag(self, field: str, dx: int, dy: int) -> None:
        """Apply an in-progress drag delta from the preview widget.

        Arrives in IMAGE-PIXEL units already (the preview converted
        widget-px → image-px via its current scale ratio). This is a
        TRANSLATION only; width/height stay fixed (drag is mouse-move,
        not edge-resize). Mirrors ``_nudge`` for the lock + no-crop
        guards so the contract is identical.
        """
        if self._is_locked:
            return
        if self._latest_box is None:
            return
        if dx == 0 and dy == 0:
            return
        box = dict(self._latest_box)
        box["x"] = max(0, box["x"] + int(dx))
        box["y"] = max(0, box["y"] + int(dy))
        # w/h are not touched during drag — translation only.
        self._latest_box = box
        self._is_manual = True
        self.box_changed.emit(self._field, dict(box))

    def _on_reset_to_live(self) -> None:
        """Discard manual adjustments, return to live auto-detection."""
        self._is_manual = False
        if self._is_locked:
            self._status.setText(
                "(unlock first to return to live detection)"
            )
            return
        self._status.setText("Returned to live detection — waiting for next scan")

    # Reasonable starting rectangles when the user clicks ✏ Set on a row
    # whose auto-detect failed. Coords are HUD-region-relative; the user
    # nudges from here. Heights are deliberately tall (28-32 px) and
    # x-starts deliberately ~halfway across so labels-on-left, values-on-
    # right HUDs work as a starting point. If the user's HUD is laid out
    # differently they nudge — point of this feature is "give me SOMETHING
    # to drag around."
    _PLACEHOLDER_BOXES: dict = {
        "mineral":     {"x":  20, "y":  10, "w": 200, "h": 30},
        "mass":        {"x": 180, "y":  70, "w": 160, "h": 28},
        "resistance":  {"x": 180, "y": 110, "w": 160, "h": 28},
        "instability": {"x": 180, "y": 150, "w": 160, "h": 28},
        # Signature lives in the SIGNAL-scanner region (not the HUD
        # region), and that region is typically narrow + short — a
        # small inset box that the user nudges right after the
        # location-pin icon and down onto the digit row.
        "signature":   {"x": 200, "y":  10, "w": 120, "h": 30},
        # Needle (difficulty bar) — covers BOTH the M / R / I row band
        # AND the EASY / MEDIUM / HARD / EXTREME / IMPOSSIBLE difficulty
        # bar at the bottom of the panel, in one tall full-width crop.
        # User asked for the whole context (full rows including values
        # AND the red difficulty bar all the way down).
        #
        # Geometry (placeholder — user nudges/locks):
        #   x=0   → left edge (full width)
        #   y=60  → ~10 px above the MASS row's top (mass.y=70)
        #   w=400 → full width — wide enough to cover labels AND values
        #   h=200 → from y=60 down through the difficulty bar
        "needle":      {"x":   0, "y":  60, "w": 400, "h": 200},
    }

    def _on_seed_manual(self) -> None:
        """Drop a default-position rectangle in this row so the user can
        nudge from there. Used when label_match rejects the row entirely
        (no crop appears) — without this the existing nudge arrows are
        no-ops because there's no box to adjust.

        After seeding, the row is in manual mode: live auto-detection no
        longer overwrites the box. User uses ← ↑ ↓ → / W± / H± to position
        and Lock to commit. Lock saves through the same path as a normal
        auto-detected lock — calibration.json gets the manual rectangle.
        """
        if self._is_locked:
            self._status.setText("(unlock first to seed a manual crop)")
            return
        box = dict(self._PLACEHOLDER_BOXES.get(
            self._field, {"x": 80, "y": 60, "w": 160, "h": 28}
        ))
        self._latest_box = box
        self._is_manual = True
        self._status.setText(
            f"MANUAL (seed): x={box['x']} y={box['y']} w={box['w']} h={box['h']} "
            "— nudge to position, then Lock"
        )
        # Trigger re-crop so the preview pane shows the placeholder rectangle
        # cropped from the latest HUD frame.
        self.box_changed.emit(self._field, dict(box))

    def add_footer_widget(self, widget: QWidget) -> None:
        """Insert an extra widget into this row's group-box, just above
        the status/lock line.

        Used by the dialog to embed a globally-scoped sub-control inside
        a specific row's panel (e.g. the column x-offset bar gets nested
        into the needle row so the difficulty-tuning controls all live in
        one visual block). Semantics of the embedded widget are NOT
        scoped to this row — the caller is responsible for whatever
        behavior the widget owns.
        """
        # The internal layout ``v`` has, in order:
        #   [0] preview, [1] nudge layout, [2] status+lock layout
        # plus any previously added footer widgets between [1] and the
        # status row. We always insert RIGHT BEFORE the last item (the
        # status+lock layout) so footers stack just above the status
        # line in the order they're added.
        layout = self.layout()
        if layout is None:
            return
        last = layout.count() - 1
        if last < 0:
            layout.addWidget(widget)
        else:
            layout.insertWidget(last, widget)

    def display_locked(self, pil: Optional[Image.Image], box: dict) -> None:
        """Show the locked box visualization (called when load from disk)."""
        self._is_locked = True
        self._latest_box = box
        self._preview.update_crop(pil)
        self._status.setText(
            f"LOCKED: x={box['x']} y={box['y']} w={box['w']} h={box['h']}"
        )
        self._apply_lock_style(True)

    def is_locked(self) -> bool:
        return self._is_locked

    def reset(self) -> None:
        self._is_locked = False
        self._latest_box = None
        self._preview.setText("(no crop yet)")
        self._status.setText("Waiting for crop…")
        self._apply_lock_style(False)

    def _on_lock_toggle(self) -> None:
        if self._is_locked:
            self._is_locked = False
            self._status.setText("Unlocked — adjust HUD or wait for new crop")
            self._apply_lock_style(False)
            self.unlocked.emit(self._field)
            return
        # Recovery path: if _latest_box is stale-None (debug_overlay
        # state was cleared between our last update_live and this
        # click), try one more time to find a usable box from any
        # available source.
        if self._latest_box is None:
            recovered = self._recover_box()
            if recovered is not None:
                self._latest_box = recovered
                log.info(
                    "lock toggle: recovered box for %s from %s",
                    self._field, recovered.get("_source", "unknown"),
                )
        if self._latest_box is None:
            # LOUD failure — popup so the user actually notices.
            self._status.setText(
                "❌ Cannot lock: no crop detected yet. Make sure the "
                "toolbox's main scan is running ('Start Scan' button)."
            )
            try:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self, "Cannot lock",
                    f"Cannot lock {self._field} — no crop detected.\n\n"
                    "Possible causes:\n"
                    "  • The toolbox's main scan loop isn't running. "
                    "Click 'Start Scan' in the scanner bar first.\n"
                    "  • The HUD region isn't pointed at a visible "
                    "SCAN RESULTS panel.\n"
                    "  • The OCR pipeline crashed. Check "
                    "logs/mining_signals.log for ERROR entries.",
                )
            except (ImportError, RuntimeError,
                    AttributeError, TypeError) as exc:
                log.warning("could not show 'cannot lock' dialog: %r", exc)
            return
        self._is_locked = True
        self._status.setText(
            f"✓ LOCKED: x={self._latest_box['x']} y={self._latest_box['y']} "
            f"w={self._latest_box['w']} h={self._latest_box['h']}"
        )
        self._apply_lock_style(True)
        # Strip any internal _source key before emitting
        emit_box = {k: v for k, v in self._latest_box.items()
                    if not k.startswith("_")}
        self.locked.emit(self._field, dict(emit_box))

    def _recover_box(self) -> Optional[dict]:
        """Last-resort attempt to derive a box at lock-click time
        when _latest_box is stale-None. Tries sources in order:
          1. needle field → placeholder fallback (no auto-detect path,
             so locking without prior Set should still work; placeholder
             gives the user a sane starting rectangle to lock)
          2. signature → consult sc_ocr.api.get_last_signal_crop_box
          3. debug_overlay's current in-memory state (HUD rows)
          4. The saved debug_value_<field>_crop.png file's pixel size
             paired with the most recent label_rows from disk
          5. None — caller will show a popup
        """
        # ``needle`` has no auto-detect source — the difficulty crop is
        # computed entirely inside api.py. If the user clicks Lock
        # without first clicking Set, _latest_box is None and the
        # standard recovery (debug_overlay.label_rows) wouldn't find it
        # either. Fall back to the placeholder so Lock just works.
        if self._field == "needle":
            try:
                box = dict(self._PLACEHOLDER_BOXES.get("needle", {}))
                if box:
                    box["_source"] = "placeholder_fallback"
                    return box
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                log.debug("needle placeholder box unavailable: %r", exc)
        # ``signature`` lives outside the HUD debug_overlay; consult
        # the signal API's own last-crop telemetry first.
        if self._field == "signature":
            try:
                from ocr.sc_ocr import api as _api
                _sig_box = _api.get_last_signal_crop_box()
                if _sig_box is not None:
                    box = dict(_sig_box)
                    box["_source"] = "sc_ocr.api.get_last_signal_crop_box"
                    return box
            except (ImportError, AttributeError,
                    TypeError, ValueError) as exc:
                log.debug("signal crop box unavailable from api: %r", exc)
        # Source 1: in-memory debug_overlay state
        try:
            from ocr.sc_ocr import debug_overlay
            state = debug_overlay._state
            label_rows = state.get("label_rows", {})
            row = label_rows.get(self._field)
            crops = state.get("value_crops", {})
            crop = crops.get(self._field)
            if crop is not None:
                x1, y1, x2, y2 = crop
                box = {"x": int(x1), "y": int(y1),
                       "w": int(x2 - x1), "h": int(y2 - y1)}
                box["_source"] = "debug_overlay.value_crops"
                return box
            if row is not None:
                box = {
                    "x": int(row.get("label_right", 0)),
                    "y": int(row["y1"]),
                    "w": 200,
                    "h": int(row["y2"] - row["y1"]),
                }
                box["_source"] = "debug_overlay.label_rows"
                return box
        except _OVERLAY_READ_ERRORS as exc:
            log.debug(
                "box recovery from debug_overlay failed (field=%s): %r",
                self._field, exc,
            )
        # Source 2: derive from saved crop file size + last-known
        # state. We don't have its absolute coordinates so fall back
        # to a region-relative estimate.
        try:
            from pathlib import Path as _P
            tool_dir = _P(__file__).resolve().parent.parent
            crop_path = tool_dir / f"debug_value_{self._field}_crop.png"
            if crop_path.is_file():
                from PIL import Image as _Img
                pil = _Img.open(crop_path)
                w, h = pil.size
                # We don't know the absolute x/y from the cropped PNG
                # alone — give a placeholder positioned where the
                # value column typically is. User can nudge after.
                box = {"x": 200, "y": 100, "w": int(w), "h": int(h)}
                box["_source"] = "fallback_from_crop_file"
                return box
        except (ImportError, OSError, TypeError, ValueError) as exc:
            # PIL raises UnidentifiedImageError (an OSError) on a
            # half-written debug crop, which is the common case here.
            log.debug(
                "box recovery from crop file failed (field=%s): %r",
                self._field, exc,
            )
        return None

    def _apply_lock_style(self, locked: bool) -> None:
        # Keep the preview's drag-affordance in sync with the lock
        # state. Locked rows refuse arrow nudges (see ``_nudge``) — the
        # mouse-drag in the preview matches that contract.
        try:
            self._preview.set_drag_disabled(bool(locked))
        except _QT_CALL_ERRORS as exc:
            log.debug("set_drag_disabled failed (field=%s): %r",
                      self._field, exc)
        if locked:
            self._lock_btn.setText("🔓 Unlock")
            self._lock_btn.setStyleSheet(
                f"QPushButton {{ background: {LOCK_GREEN}; color: white; "
                "font-weight: bold; padding: 6px; border: none; }}"
                f"QPushButton:hover {{ background: #3b9; }}"
            )
            self.setStyleSheet(
                f"QGroupBox {{ border: 2px solid {LOCK_GREEN}; "
                "border-radius: 4px; margin-top: 6px; padding-top: 4px; }}"
                f"QGroupBox::title {{ color: {LOCK_GREEN}; "
                "font-weight: bold; }}"
            )
        else:
            self._lock_btn.setText("🔒 Lock")
            self._lock_btn.setStyleSheet(
                f"QPushButton {{ background: {LOCK_GRAY}; color: white; "
                "padding: 6px; border: none; }}"
                f"QPushButton:hover {{ background: #777; }}"
            )
            self.setStyleSheet(
                "QGroupBox { border: 1px solid #444; border-radius: 4px; "
                "margin-top: 6px; padding-top: 4px; }"
                f"QGroupBox::title {{ color: {TEXT_PRIMARY}; }}"
            )


class _LiveCropSignaler(QObject):
    """Cross-thread bridge for live OCR crop delivery.

    The OCR pipeline broadcasts crops from a worker thread (via
    ``ocr.sc_ocr.live_broadcast``). Qt widgets must be touched only on
    the UI thread. This QObject hosts a signal whose default
    cross-thread connection (QueuedConnection) hands the payload to the
    UI thread for safe widget updates.

    Payload type is ``object`` so a PIL.Image can pass through verbatim
    — no encode/decode, no copy beyond the queue itself.
    """
    crop_ready = Signal(str, object)


class _LiveRefreshSignaler(QObject):
    """Cross-thread bridge for the live-refresh tick result.

    Mirrors ``_LiveCropSignaler``: the heavy parts of the live-refresh
    tick (screen capture + label-row detection) run on a daemon worker
    thread to keep the UI responsive. The worker emits this signal with
    the captured PILs and detected label rows; the default cross-thread
    QueuedConnection delivers the payload to the UI thread, where Phase
    3 (per-row preview update) can safely touch widgets.

    Payload types are ``object`` so ``Optional[PIL.Image.Image]`` and
    plain dicts pass through verbatim without Qt metatype gymnastics.
    """
    result_ready = Signal(object, object, object)


class CalibrationDialog(QDialog):
    """Main calibration dialog — non-modal so user can see the game HUD
    beside it."""

    def __init__(
        self,
        region: dict,
        scan_callback,
        parent=None,
        signature_region: Optional[dict] = None,
    ):
        """
        Parameters:
            region : the user's HUD region dict {"x", "y", "w", "h"}
            scan_callback : callable(region: dict) -> dict
                Should return a single OCR scan result. We use it to
                trigger the pipeline and read back what crops were
                used. The OCR pipeline already saves debug crops to
                disk; we read them from there for the live preview.
            signature_region : the user's SIGNATURE-scanner ocr_region
                dict, or None if the user hasn't set it yet. Used as
                the calibration key for the "signature" row so it
                lives under the signal region rather than the HUD
                region (the two are independent on-screen rectangles).
        """
        super().__init__(parent)
        self.setWindowTitle("Mining HUD OCR Calibration")
        # Lowered from 720×720 so the dialog can occupy a corner of
        # the screen instead of half of it. The Calibrate tab wraps
        # its rows in a QScrollArea, so anything that doesn't fit at
        # this size just becomes scrollable rather than truncated.
        self.setMinimumSize(480, 380)
        self.resize(720, 640)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        self._region = region
        self._signature_region = signature_region
        self._scan_callback = scan_callback

        # ── Tabs ──
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # Top header — region info + completion banner
        self._header = QLabel("")
        self._header.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 12pt; "
            f"color: {ACCENT}; padding: 4px 8px;"
        )
        v.addWidget(self._header)

        self._completion_banner = QLabel("")
        self._completion_banner.setAlignment(Qt.AlignCenter)
        self._completion_banner.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 22pt; "
            f"font-weight: bold; color: {LOCK_GREEN}; padding: 10px; "
            "background: rgba(42, 136, 0, 0.12); border-radius: 6px;"
        )
        self._completion_banner.setVisible(False)
        v.addWidget(self._completion_banner)

        self._tabs = QTabWidget()
        v.addWidget(self._tabs, 1)

        self._tabs.addTab(self._build_calibrate_tab(), "Calibrate")
        self._tabs.addTab(self._build_tutorial_tab(), "Tutorial")
        # Pause OCR polling when the user is on the Tutorial tab
        # (otherwise every 400 ms we'd run a full OCR scan in the
        # background, locking up the UI as you read).
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # Status bar
        self._status_bar = QStatusBar()
        v.addWidget(self._status_bar)

        # ── Live crop delivery: in-process broadcast (real-time) ──
        # The OCR pipeline broadcasts crops from a worker thread via
        # ocr.sc_ocr.live_broadcast. We bridge into Qt's signal/slot
        # system so the listener (worker thread) hands the payload to
        # the UI thread for safe widget updates. This eliminates the
        # PNG-encode → disk-write → mtime-poll → PNG-decode roundtrip
        # that was the dominant lag source on the calibrate path.
        self._live_signaler = _LiveCropSignaler()
        self._live_signaler.crop_ready.connect(self._on_live_crop)
        try:
            from ocr.sc_ocr import live_broadcast as _bcast
            _bcast.register_listener(self._on_pil_crop)
            self._broadcast_registered = True
        except Exception as exc:
            log.debug("live_broadcast registration failed: %s", exc)
            self._broadcast_registered = False

        # mtime cache for the disk-fallback path: lets _tick skip the
        # PIL decode when nothing has changed on disk since last tick.
        # Only used as a fallback / cold-open populator now that
        # broadcast covers the live path.
        self._last_mtimes: dict[str, float] = {}

        # Tracks when the most recent in-process broadcast arrived. The
        # status bar prefers this over the disk mtime when present so a
        # user with no cross-process viewers sees an accurate freshness.
        self._last_broadcast_ts: float = 0.0
        # Per-field timestamp so the live-refresh timer can suppress
        # its fallback render for rows whose authoritative broadcast
        # crop arrived very recently (avoids flicker between the OCR-
        # exact crop and the timer's re-crop, which can differ by a
        # frame's worth of HUD content).
        self._last_broadcast_ts_per_field: dict[str, float] = {}

        # ── Polling timer for status / cold-start fallback ──
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

        # ── Continuous live-preview refresh timer ──
        # Independent of the OCR scan loop. Every ~500 ms we capture
        # the current HUD region + signature region and re-crop each
        # row's preview using its currently-active box (locked >
        # seeded-manual > auto-detected). This makes the calibration
        # boxes "persistent" rather than "freezing on the last broadcast
        # crop": the user sees what each box selects on the live screen
        # even when scanning is paused or hasn't started.
        #
        # When a real scan completes and broadcasts a crop via
        # ``live_broadcast``, that broadcast crop is preferred for a
        # short freshness window (BROADCAST_FRESHNESS_S) — the broadcast
        # crop went through the actual OCR pipeline, so it's the most
        # authoritative image to show. The live-refresh timer fills the
        # gaps between scans.
        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.timeout.connect(self._live_refresh_tick)
        self._live_refresh_timer.start(LIVE_REFRESH_MS)
        # Cross-thread bridge for the live-refresh tick. Phase 1
        # (capture) + Phase 2 (label-row detection) run on a daemon
        # worker thread; the worker emits this signal and the default
        # QueuedConnection delivers the payload back to the UI thread
        # for Phase 3 (per-row preview update). See
        # ``_live_refresh_tick`` and ``_on_live_refresh_result``.
        self._live_refresh_signaler = _LiveRefreshSignaler()
        self._live_refresh_signaler.result_ready.connect(
            self._on_live_refresh_result
        )
        # Re-entrancy guard for the live-refresh worker. The QTimer can
        # fire faster than the worker completes (cold mss capture +
        # NCC detect can take >1.5 s on first tick), so we drop ticks
        # that arrive while a worker is already in flight rather than
        # piling them up.
        self._live_refresh_in_flight: bool = False
        # Per-tick label_match cache: avoids re-running the heavy NCC /
        # anchor detection every tick when no row needs it (locked +
        # manual rows never need it; only auto-detect rows do). Reused
        # across ticks within `LABEL_DETECT_INTERVAL_S` even when an
        # auto-detect row IS present — see ``_live_refresh_tick``.
        self._cached_hud_label_rows: Optional[dict] = None
        self._cached_hud_label_rows_id: Optional[int] = None
        self._last_label_detect_ts: float = 0.0

        # User-input tracking for tick suppression. Updated by
        # ``eventFilter`` on every meaningful input event (mouse press,
        # key press, wheel) bubbling out of any child widget. Read by
        # ``_live_refresh_tick`` to skip if the user is actively
        # interacting — prevents the screen-capture pipeline from
        # stealing GUI cycles mid-drag.
        self._last_user_input_ts: float = 0.0
        # Install ourselves as an event filter on the QApplication so
        # we see input events regardless of which child widget receives
        # them. Cleaned up in ``closeEvent``.
        try:
            _app = QApplication.instance()
            if _app is not None:
                _app.installEventFilter(self)
                self._installed_app_filter = True
            else:
                self._installed_app_filter = False
        except Exception:
            self._installed_app_filter = False

        self._refresh_header()
        self._reload_locked_state_from_disk()

        # ── Deferred bootstrap scan ──
        # The bootstrap scans (HUD + signature) are HEAVY: each runs a
        # full label_match + Tesseract anchor + OCR pipeline pass and
        # can take 200ms-5s on cold start (multi-voter ensemble + ONNX
        # model load). On the GUI thread that's enough for Windows to
        # mark the window "Not Responding" and freeze interactions on
        # every other Qt window in the same process.
        #
        # v2.2.7+: run the bootstrap on a daemon Python thread instead.
        # The OCR pipeline is thread-safe enough for this — it writes to
        # module-level caches but those caches are also read by the GUI
        # thread via copy-on-read, so no shared mutable state. Live
        # refresh tick on the GUI thread will pick up the freshly-cached
        # state on the next 1s tick.
        QTimer.singleShot(0, self._run_bootstrap_scans)
        self._tick()

    def _run_bootstrap_scans(self) -> None:
        """Kick off the bootstrap scan on a worker thread so the GUI
        thread stays responsive. The scan results land in shared
        module-level caches that the live-refresh tick reads on its
        next firing — no signal/slot plumbing required because we
        don't touch widgets directly from the worker."""
        import threading as _threading

        def _bg() -> None:
            try:
                if self._scan_callback is not None:
                    try:
                        self._scan_callback(self._region)
                    except Exception as exc:
                        log.debug("bootstrap scan failed: %s", exc)
                if self._signature_region is not None:
                    try:
                        from ocr import screen_reader as _sr
                        _sr.scan_region(dict(self._signature_region))
                    except Exception as exc:
                        log.debug("signal bootstrap scan failed: %s", exc)
            except Exception as exc:
                # Last-ditch — never let a worker crash leak into the GUI.
                log.debug("bootstrap worker exception: %s", exc)

        try:
            _threading.Thread(
                target=_bg, daemon=True, name="cal_bootstrap",
            ).start()
        except Exception as exc:
            # Threading failure is exceptional (probably interpreter
            # shutting down). Fall back to synchronous so we at least
            # do *something* — but log loudly.
            log.warning(
                "calibration: bootstrap thread spawn failed (%s); "
                "running synchronously, may freeze UI briefly", exc,
            )
            try:
                if self._scan_callback is not None:
                    self._scan_callback(self._region)
            # DELIBERATELY BROAD (see the noqa on the except below):
            # _scan_callback is injected by the caller and
            # runs the WHOLE OCR pipeline (screen capture, onnxruntime,
            # Tesseract subprocesses, PIL). Its raise set is not knowable
            # from here, and this is the synchronous last-resort fallback
            # after the bootstrap thread already failed to spawn: if this
            # raises, the calibration dialog must still open. Kept broad,
            # but no longer silent -- a bootstrap scan that never happens
            # otherwise looks exactly like a HUD with nothing on it.
            # Bound as `exc2`, NOT `exc`: Python deletes the handler's name
            # on exit, so reusing `exc` here would unbind the enclosing
            # handler's own exception variable.
            except Exception as exc2:  # noqa: BLE001 - see comment above
                log.warning(
                    "calibration: synchronous bootstrap scan failed: %r",
                    exc2,
                )

    # ──────────────────────────────────────────
    # Calibrate tab
    # ──────────────────────────────────────────

    def _build_calibrate_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)

        # ── Voice tutorial bar (front-and-center on the Calibrate tab) ──
        voice_bar = QWidget()
        voice_bar.setStyleSheet(f"background: {PANEL_BG}; padding: 6px;")
        vh = QHBoxLayout(voice_bar)
        vh.setContentsMargins(8, 6, 8, 6)
        vh.setSpacing(8)
        self._voice_btn = QPushButton("🔊 Play Voice Tutorial")
        self._voice_btn.setCursor(Qt.PointingHandCursor)
        self._voice_btn.setToolTip(
            "Audio walkthrough of how to calibrate the mining HUD crops"
        )
        self._voice_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: black; "
            "padding: 8px 18px; font-weight: bold; font-size: 10pt; "
            "border: none; }}"
            "QPushButton:hover { background: #5e8; }"
            "QPushButton:disabled { background: #444; color: #888; }"
        )
        self._voice_btn.clicked.connect(self._on_voice_play)
        vh.addWidget(self._voice_btn)

        # Pause / Resume — single button that toggles its label and
        # behaviour based on the player's current state. While playing
        # it shows "⏸ Pause"; while paused it shows "▶ Resume". Disabled
        # whenever there's nothing to act on (idle / stopped).
        self._voice_pause_btn = QPushButton("⏸ Pause")
        self._voice_pause_btn.setCursor(Qt.PointingHandCursor)
        self._voice_pause_btn.setStyleSheet(
            "QPushButton { background: #444; color: white; padding: 8px 14px; "
            "border: none; font-size: 10pt; }"
            "QPushButton:hover { background: #666; }"
            "QPushButton:disabled { background: #2a2a2a; color: #555; }"
        )
        self._voice_pause_btn.setEnabled(False)
        self._voice_pause_btn.clicked.connect(self._on_voice_pause)
        vh.addWidget(self._voice_pause_btn)

        self._voice_stop_btn = QPushButton("⏹ Stop")
        self._voice_stop_btn.setCursor(Qt.PointingHandCursor)
        self._voice_stop_btn.setStyleSheet(
            "QPushButton { background: #444; color: white; padding: 8px 14px; "
            "border: none; font-size: 10pt; }"
            "QPushButton:hover { background: #666; }"
            "QPushButton:disabled { background: #2a2a2a; color: #555; }"
        )
        self._voice_stop_btn.setEnabled(False)
        self._voice_stop_btn.clicked.connect(self._on_voice_stop)
        vh.addWidget(self._voice_stop_btn)

        self._voice_status = QLabel("")
        self._voice_status.setStyleSheet(
            f"color: {TEXT_DIM}; font-family: Consolas; font-size: 9pt;"
        )
        vh.addWidget(self._voice_status, 1)

        # Panel Finder popout — opens a separate, resizable window
        # showing the live annotated panel image.
        self._panel_finder_btn = QPushButton("🖼 SC-OCR Panel Finder")
        self._panel_finder_btn.setCursor(Qt.PointingHandCursor)
        self._panel_finder_btn.setToolTip(
            "Open the SC-OCR Panel Finder in a separate window. "
            "Resizable from small to large; shows the live annotated "
            "panel as a visual reference while you calibrate."
        )
        self._panel_finder_btn.setStyleSheet(
            "QPushButton { background: #2a4a6a; color: white; "
            "padding: 8px 14px; font-weight: bold; font-size: 10pt; "
            "border: none; }"
            "QPushButton:hover { background: #3b5d7a; }"
        )
        self._panel_finder_btn.clicked.connect(self._on_open_panel_finder)
        vh.addWidget(self._panel_finder_btn)

        # Signature Finder popout — same pattern as Panel Finder, but
        # for the signal/signature scanner pipeline (icon-anchored
        # NCC + Tesseract diagnostic). Useful while calibrating to
        # confirm the signature scan region picks up the icon AND
        # all digits.
        self._signature_finder_btn = QPushButton("📈 Signature Finder")
        self._signature_finder_btn.setCursor(Qt.PointingHandCursor)
        self._signature_finder_btn.setToolTip(
            "Open the Signature Finder in a separate window. Live "
            "diagnostic for the signal scanner — shows the captured "
            "scan region, the NCC icon anchor (red box), the digit "
            "crop (green box), and the OCR result for every poll."
        )
        self._signature_finder_btn.setStyleSheet(
            "QPushButton { background: #2a6a4a; color: white; "
            "padding: 8px 14px; font-weight: bold; font-size: 10pt; "
            "border: none; }"
            "QPushButton:hover { background: #3b7a5d; }"
        )
        self._signature_finder_btn.clicked.connect(
            self._on_open_signature_finder
        )
        vh.addWidget(self._signature_finder_btn)

        # Glyph Reader popout — visualises per-glyph OCR vision: each
        # individual digit crop the classifier sees, alongside the
        # classified character + confidence. Color-coded by conf so
        # you can immediately see which digits are misread.
        self._glyph_reader_btn = QPushButton("🔍 Glyph Reader")
        self._glyph_reader_btn.setCursor(Qt.PointingHandCursor)
        self._glyph_reader_btn.setToolTip(
            "Open the Glyph Reader in a separate window. Shows what "
            "the OCR pipeline sees per individual digit crop alongside "
            "the classifier's output and confidence — the visual "
            "companion to the sc_ocr.diag log lines."
        )
        self._glyph_reader_btn.setStyleSheet(
            "QPushButton { background: #6a4a2a; color: white; "
            "padding: 8px 14px; font-weight: bold; font-size: 10pt; "
            "border: none; }"
            "QPushButton:hover { background: #7a5d3b; }"
        )
        self._glyph_reader_btn.clicked.connect(self._on_open_glyph_reader)
        vh.addWidget(self._glyph_reader_btn)

        # Record Next Scan — forces the OCR pipeline to write the full
        # ~50-PNG forensic dump for the next scan. Useful when you see
        # a weird read and want a complete record of what the pipeline
        # was looking at, without holding the diagnostic gate open
        # continuously (which is what causes the lag).
        self._record_next_btn = QPushButton("📼 Record Next Scan")
        self._record_next_btn.setCursor(Qt.PointingHandCursor)
        self._record_next_btn.setToolTip(
            "Capture a full forensic disk dump of the NEXT OCR scan "
            "(every value crop, glyph, and panel overlay). One click "
            "= one scan recorded. Lets you investigate odd reads "
            "without leaving the high-cost diagnostic dump on all the "
            "time."
        )
        self._record_next_btn.setStyleSheet(
            "QPushButton { background: #6a2a4a; color: white; "
            "padding: 8px 14px; font-weight: bold; font-size: 10pt; "
            "border: none; }"
            "QPushButton:hover { background: #7a3b5d; }"
        )
        self._record_next_btn.clicked.connect(self._on_record_next_scan)
        vh.addWidget(self._record_next_btn)

        v.addWidget(voice_bar)

        # Voice player held by the dialog so it survives playback.
        self._voice_player = None

        info = QLabel(
            "<b>How it works:</b> Each row shows the live crop being "
            "fed to the OCR pipeline. When a row's crop looks correct "
            "(value clearly visible, no label leakage), click "
            "<b style='color:#2a8'>🔒 Lock</b>. Locked rows are saved "
            "immediately and used at runtime instead of detection."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            f"background: {PANEL_BG}; color: {TEXT_PRIMARY}; "
            "padding: 8px; border-radius: 4px; font-size: 9pt;"
        )
        v.addWidget(info)

        # ── Global column x-offset (one shift applied to ALL HUD rows) ──
        # The mass / resistance / instability values share a column on
        # the HUD, so a single x-offset lets the user nudge all three
        # crops left or right without touching each row individually.
        # Per-region: the value re-loads when the user changes their
        # HUD region (see _on_region_changed-style hooks in the future
        # — for now we read in _build_calibrate_tab and reload on tick).
        #
        # NOTE: this widget is GLOBAL (affects mass/resi/inst together);
        # we just nest it visually inside the needle row's group box
        # below so all the value-column / difficulty-tuning controls
        # live in one place. The shift it applies is unchanged — still
        # global, still affects all HUD rows.
        col_bar = QWidget()
        col_bar.setStyleSheet(
            f"background: {PANEL_BG}; padding: 4px; border-radius: 4px;"
        )
        ch = QHBoxLayout(col_bar)
        ch.setContentsMargins(8, 6, 8, 6)
        ch.setSpacing(6)
        col_title = QLabel("Column x-offset (all rows):")
        col_title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-family: Consolas; font-size: 10pt;"
        )
        ch.addWidget(col_title)

        self._col_left_btn = QPushButton("←")
        self._col_left_btn.setFixedSize(28, 26)
        self._col_left_btn.setToolTip(
            "Shift the value column 1 px LEFT (Shift+click = 5 px). "
            "Affects mass / resistance / instability simultaneously."
        )
        self._col_left_btn.setStyleSheet(
            f"QPushButton {{ background: {LOCK_GRAY}; color: white; "
            "border: none; font-weight: bold; }}"
            "QPushButton:hover { background: #777; }"
        )
        self._col_left_btn.clicked.connect(
            lambda: self._on_col_nudge(-1)
        )
        ch.addWidget(self._col_left_btn)

        self._col_spin = QSpinBox()
        self._col_spin.setRange(-200, 200)
        self._col_spin.setValue(self._safe_get_col_offset())
        self._col_spin.setSuffix(" px")
        self._col_spin.setMinimumWidth(90)
        self._col_spin.setToolTip(
            "Direct entry for the column x-offset. Negative = shift "
            "value crops LEFT; positive = shift RIGHT. Applies to all "
            "HUD rows in this region."
        )
        self._col_spin.valueChanged.connect(self._on_col_offset_changed)
        ch.addWidget(self._col_spin)

        self._col_right_btn = QPushButton("→")
        self._col_right_btn.setFixedSize(28, 26)
        self._col_right_btn.setToolTip(
            "Shift the value column 1 px RIGHT (Shift+click = 5 px). "
            "Affects mass / resistance / instability simultaneously."
        )
        self._col_right_btn.setStyleSheet(
            f"QPushButton {{ background: {LOCK_GRAY}; color: white; "
            "border: none; font-weight: bold; }}"
            "QPushButton:hover { background: #777; }"
        )
        self._col_right_btn.clicked.connect(
            lambda: self._on_col_nudge(+1)
        )
        ch.addWidget(self._col_right_btn)

        self._col_status = QLabel("")
        self._col_status.setStyleSheet(
            f"color: {TEXT_DIM}; font-family: Consolas; font-size: 9pt;"
        )
        ch.addWidget(self._col_status, 1)
        self._refresh_col_status()
        # NOTE: col_bar is NOT added to the top-level layout `v` — it is
        # instead embedded into the needle row's group box below
        # (`_row_controls["needle"].add_footer_widget(col_bar)`) so the
        # value-column / difficulty-tuning controls live together
        # visually. The shift it applies is still GLOBAL — see the
        # comment at col_bar construction.

        # Row controls live inside a scroll area so the dialog can be
        # shrunk down without truncating rows or fighting their minimum
        # heights. The action bar (Reset / Close) stays pinned below
        # the scroll area, so it's always reachable no matter how short
        # the user drags the window.
        rows_host = QWidget()
        rows_layout = QVBoxLayout(rows_host)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(6)

        self._row_controls: dict[str, _RowControl] = {}
        for field in FIELD_NAMES:
            ctrl = _RowControl(field)
            ctrl.locked.connect(self._on_row_locked)
            ctrl.unlocked.connect(self._on_row_unlocked)
            ctrl.box_changed.connect(self._on_row_box_changed)
            self._row_controls[field] = ctrl
            # stretch=1 → all four rows split spare vertical space
            # equally when the dialog is enlarged. Combined with the
            # _CropPreview's Expanding size policy this means dragging
            # the dialog taller actually makes the crop images larger
            # rather than just adding empty padding at the bottom.
            rows_layout.addWidget(ctrl, 1)

        # Inject the global column x-offset bar into the needle row's
        # group box. This keeps difficulty-detection-area tuning + the
        # value-column shift in the same visual block, since the column
        # x-offset directly controls where the M/R/I values get cropped
        # — which is what difficulty detection inspects. The widget's
        # behavior is unchanged (still globally affects mass/resi/inst).
        # If the needle row isn't in FIELD_NAMES (defensive guard for
        # future field-list changes), fall back to adding col_bar at
        # the top level so it doesn't disappear.
        needle_ctrl = self._row_controls.get("needle")
        if needle_ctrl is not None:
            needle_ctrl.add_footer_widget(col_bar)
        else:
            v.addWidget(col_bar)

        rows_scroll = QScrollArea()
        rows_scroll.setWidgetResizable(True)
        rows_scroll.setFrameShape(QFrame.NoFrame)
        rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        rows_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        rows_scroll.setWidget(rows_host)
        # Stretch=1 so the scroll area absorbs all the spare vertical
        # space — the row crops grow with the dialog instead of leaving
        # a dead band at the bottom.
        v.addWidget(rows_scroll, 1)

        # Action row
        actions = QHBoxLayout()
        actions.addStretch(1)

        # User-initiated one-shot scan. Lets the user verify a freshly
        # adjusted region or locked crop without closing the dialog and
        # toggling the main UI's Start Scan. Green (accent) because
        # it's a positive, safe action — it doesn't change toggle
        # state, doesn't mutate calibration, just fires a single OCR
        # pass against the latest config.
        self._scan_now_btn = QPushButton("▶ Scan now")
        self._scan_now_btn.setToolTip(
            "Fire one OCR scan against the current calibration so you "
            "can verify the result without closing the dialog. Does "
            "not change the Start/Stop Scan state in the main window."
        )
        self._scan_now_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: black; "
            "padding: 6px 14px; font-weight: bold; border: none; }}"
            "QPushButton:hover { background: #5e8; }"
            "QPushButton:disabled { background: #2a4a35; color: #888; }"
        )
        self._scan_now_btn.clicked.connect(self._on_scan_now)
        actions.addWidget(self._scan_now_btn)

        # Manual escape hatch — wipes every rolling window + lock
        # cache so the next scan starts fresh. Lives next to the other
        # destructive "reset" controls. Orange (between "Reset all
        # calibration"'s red and the green close button) so users see
        # it as recoverable: it doesn't touch saved calibration on
        # disk, only runtime in-memory consensus state.
        reset_consensus_btn = QPushButton("Reset consensus")
        reset_consensus_btn.setToolTip(
            "Clear all rolling-window consensus and locked-in OCR "
            "values for the signal + HUD scanners. Use this if a "
            "stuck reading is showing the wrong value (e.g. a "
            "leading 1 that won't go away). The next scan starts "
            "fresh. Does not touch saved calibration."
        )
        reset_consensus_btn.setStyleSheet(
            "QPushButton { background: #b56000; color: white; padding: 6px 14px; "
            "border: none; }"
            "QPushButton:hover { background: #d97a1a; }"
        )
        reset_consensus_btn.clicked.connect(self._on_reset_consensus)
        actions.addWidget(reset_consensus_btn)

        reset_btn = QPushButton("Reset all calibration")
        reset_btn.setStyleSheet(
            "QPushButton { background: #722; color: white; padding: 6px 14px; "
            "border: none; }"
            "QPushButton:hover { background: #944; }"
        )
        reset_btn.clicked.connect(self._on_reset_all)
        actions.addWidget(reset_btn)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: black; "
            "padding: 6px 14px; font-weight: bold; border: none; }}"
            "QPushButton:hover { background: #5e8; }"
        )
        close_btn.clicked.connect(self.accept)
        actions.addWidget(close_btn)

        v.addLayout(actions)

        # ── EMERGENCY OVERRIDE row (separate row so it visually stands
        # apart from the normal Close / Reset actions). Big red button
        # with a tooltip explaining what it does. The "Disable manual
        # override" sibling shows up only when override mode is active.
        emergency_row = QHBoxLayout()
        emergency_row.addStretch(1)

        self._override_status_lbl = QLabel("")
        self._override_status_lbl.setStyleSheet(
            "color: #ff8888; font-weight: bold; font-family: Electrolize, Consolas;"
        )
        emergency_row.addWidget(self._override_status_lbl)

        self._disable_override_btn = QPushButton("Disable manual override")
        self._disable_override_btn.setToolTip(
            "Turn off manual override mode for this region. The OCR "
            "pipeline returns to using auto-detection / row locks."
        )
        self._disable_override_btn.setStyleSheet(
            "QPushButton { background: #555; color: #ffd0d0; "
            "padding: 6px 14px; border: none; }"
            "QPushButton:hover { background: #777; }"
        )
        self._disable_override_btn.clicked.connect(self._on_disable_override)
        self._disable_override_btn.setVisible(False)
        emergency_row.addWidget(self._disable_override_btn)

        self._emergency_btn = QPushButton("⚠ EMERGENCY OVERRIDE")
        self._emergency_btn.setToolTip(
            "Manual selection only. Disables auto-detection. Opens a "
            "dialog where you draw each field's box on a live HUD shot."
        )
        self._emergency_btn.setStyleSheet(
            "QPushButton { background: #aa1a1a; color: white; "
            "padding: 8px 18px; font-weight: bold; font-size: 10pt; "
            "border: 2px solid #ff4040; }"
            "QPushButton:hover { background: #cc2222; }"
            "QPushButton:pressed { background: #881010; }"
        )
        self._emergency_btn.clicked.connect(self._on_open_emergency_override)
        emergency_row.addWidget(self._emergency_btn)

        v.addLayout(emergency_row)

        # Now that all visible widgets exist, sync the override-mode UI
        # to whatever's currently saved on disk. Doing this AFTER the
        # widgets are created avoids attribute-order pitfalls.
        self._refresh_override_indicator()
        return page

    # ──────────────────────────────────────────
    # Tutorial tab
    # ──────────────────────────────────────────

    def _build_tutorial_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Tutorial text only — voice button lives on the Calibrate
        # tab now (front-and-center where users actually start).
        browser = QTextBrowser()
        browser.setStyleSheet(
            f"background: {PANEL_BG}; color: {TEXT_PRIMARY}; "
            "padding: 12px; font-family: Consolas; font-size: 10pt;"
        )
        browser.setOpenExternalLinks(False)
        browser.setHtml(self._tutorial_html())
        v.addWidget(browser, 1)

        return page

    def _tutorial_html(self) -> str:
        return """
<h2 style="color:#33dd88;">Why calibrate?</h2>
<p>The Mining Signals OCR engine reads the values in your in-game
SCAN RESULTS panel (MASS / RESISTANCE / INSTABILITY) so it can match
rocks to your fleet's breakability and surface them in the floating
HUD bubble.</p>

<p>Detecting <i>where</i> those values sit on screen is harder than
reading them. Backgrounds change (asteroid surface, deep space,
planet sky), HUD labels can be abbreviated, and panels render at
different positions depending on your resolution and HUD scale.</p>

<p>Calibration solves all of that with a one-time setup: you confirm
where each value sits, and the OCR uses your confirmed coordinates
forever after — no detection, no drift.</p>

<h2 style="color:#33dd88;">How to calibrate</h2>

<ol>
  <li><b>Open the SCAN RESULTS panel in-game.</b> Aim your mining
      laser at any rock until the panel appears.</li>
  <li><b>Watch the live crops.</b> Each row in the dialog shows the
      live OCR crop — the rectangle of pixels being fed to the
      digit recognizer. The crops update every ~0.4 seconds.</li>
  <li><b>For each row, when the crop looks right:</b>
      <ul>
          <li>The value digits should be FULLY VISIBLE
              (no label text on the left, no missing digits)</li>
          <li>The crop should NOT include parts of the row above
              or below it</li>
      </ul>
      Click <b style="color:#2a8;">🔒 Lock</b>. The row's border
      and lock button turn <b style="color:#2a8;">green</b>, and the
      crop coordinates are saved to disk immediately.</li>
  <li><b>Repeat for all three rows</b> (Mass, Resistance,
      Instability). The other rows in the dialog, Resource (Mineral),
      Signature / Signal Value and Needle (difficulty), are optional.</li>
  <li>When all three are locked, the dialog displays
      <b style="color:#2a8;">"CALIBRATION COMPLETE"</b> in
      large text at the top. You can now close the dialog.</li>
</ol>

<h2 style="color:#33dd88;">When to recalibrate</h2>

<ul>
  <li>You change the screen resolution or HUD scale</li>
  <li>You switch between full-label and short-label HUD modes
      (e.g. helmet HUD vs. ship scanner panel)</li>
  <li>You move the user-defined HUD scan region</li>
</ul>

<p>To recalibrate, just open this dialog again. Click
<b style="color:#a44;">Reset all calibration</b> to start fresh, or
<b style="color:#888;">🔓 Unlock</b> a single row to redo just that
one.</p>

<h2 style="color:#33dd88;">Where is the calibration saved?</h2>

<p>Per-user file at:</p>
<p><code style="color:#fb0;">%LOCALAPPDATA%\\SC_Toolbox\\sc_ocr\\calibration.json</code></p>

<p>Calibration persists across toolbox restarts and updates. Multiple
HUD regions get separate calibrations (keyed by region geometry),
so you can switch between setups without losing your work.</p>
"""

    # ──────────────────────────────────────────
    # Live polling
    # ──────────────────────────────────────────

    def _tick(self) -> None:
        # The fast path is the in-process broadcast (see _on_live_crop).
        # This timer handles two residual jobs:
        #   1. Cold-start population: read whatever PNG was left on
        #      disk by a previous session so the dialog has something
        #      to show before the next scan arrives.
        #   2. Status-bar freshness: report when crops were last
        #      delivered (broadcast time wins; disk mtime is fallback).
        #
        # We deliberately DO NOT touch the diagnostic heartbeat any
        # more. The broadcast covers in-process delivery, so the
        # ~50-files-per-scan disk dump only fires when an external
        # viewer (cross-process script) is watching or the user
        # pressed "Record Next Scan".

        from pathlib import Path
        tool_dir = Path(__file__).resolve().parent.parent
        newest_mtime = 0.0
        for field, ctrl in self._row_controls.items():
            crop_path = tool_dir / f"debug_value_{field}_crop.png"
            if not crop_path.is_file():
                continue
            try:
                mtime = crop_path.stat().st_mtime
                newest_mtime = max(newest_mtime, mtime)
                # mtime gate: skip the PIL decode when nothing has
                # advanced. Halves the per-tick work in the common
                # case of the dialog open while no scan is running.
                if mtime <= self._last_mtimes.get(field, 0.0):
                    continue
                self._last_mtimes[field] = mtime
                pil = Image.open(crop_path).convert("RGB")
                if ctrl.is_locked():
                    ctrl._preview.update_crop(pil)
                else:
                    box = self._read_live_box(field)
                    ctrl.update_live(pil, box)
            except Exception as exc:
                log.debug("preview load failed for %s: %s", field, exc)

        # Status bar: prefer in-process broadcast time when it's recent
        # so the freshness reading remains accurate even when the disk
        # files aren't being updated (cross-process viewers absent).
        import time as _time
        from datetime import datetime as _dt
        bcast_age = (
            (_time.time() - self._last_broadcast_ts)
            if self._last_broadcast_ts > 0 else None
        )
        disk_age = (
            int(_dt.now().timestamp() - newest_mtime)
            if newest_mtime > 0 else None
        )

        if bcast_age is not None and bcast_age <= 3:
            self._status_bar.showMessage(
                f"✓ Live (last crop {int(bcast_age)}s ago, in-process)",
                2000,
            )
        elif disk_age is not None and disk_age <= 3:
            self._status_bar.showMessage(
                f"✓ Live (last crop {disk_age}s ago, on disk)", 2000,
            )
        elif disk_age is not None:
            self._status_bar.showMessage(
                f"⚠ Crops are {disk_age}s old — start the toolbox's "
                "main scan ('Start Scan' button) to refresh", 0,
            )
        else:
            self._status_bar.showMessage(
                "⚠ No crop files yet — start the toolbox's main scan "
                "('Start Scan' button) to populate", 0,
            )

    # ──────────────────────────────────────────
    # Continuous live-preview refresh
    # ──────────────────────────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Track meaningful user input events to suppress live-refresh
        ticks while the user is interacting.

        Installed on QApplication in ``__init__`` so we see events from
        every child widget (sliders, spinners, buttons, scroll areas)
        without per-widget plumbing. Read by ``_live_refresh_tick``.

        Returns False unconditionally — never consume the event, just
        observe it.
        """
        try:
            t = event.type()
            # Cherry-pick events that mean "the user is doing
            # something". MouseMove is intentionally excluded — it
            # fires constantly on hover and would keep the live-refresh
            # permanently paused.
            if t in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
                QEvent.Type.KeyPress,
                QEvent.Type.Wheel,
            ):
                # Only count if the event target is OUR dialog or a
                # descendant — events from unrelated windows in the
                # same Qt app shouldn't pause our timer. Uses
                # ``QWidget.isAncestorOf`` (a single C++ call) instead
                # of a Python-side parent-chain walk; the global filter
                # sees events from EVERY widget in the toolbox process
                # so the per-event cost matters here.
                try:
                    if isinstance(obj, QWidget) and (
                        obj is self or self.isAncestorOf(obj)
                    ):
                        import time as _t
                        self._last_user_input_ts = _t.time()
                except _QT_CALL_ERRORS as exc:
                    log.debug("user-input timestamp update failed: %r", exc)
        # DELIBERATELY BROAD (see the noqa on the except below):
        # this is an event filter installed on the
        # QApplication, so it is re-entered from C++ for EVERY event of
        # EVERY widget in the toolbox process. An exception that escapes
        # here unwinds into the Qt event loop, where PySide6's unhandled
        # -exception path can abort the process -- a crash of the whole
        # toolbox caused by a diagnostic timestamp. Kept broad for that
        # reason, and now logged so the rate is observable.
        except Exception as exc:  # noqa: BLE001 - see comment above
            log.debug("calibration dialog eventFilter failed: %r", exc)
        return False

    def _live_refresh_tick(self) -> None:
        """Re-crop each row's preview from the LIVE screen, every tick.

        Independent of the OCR scan loop. The point is: when the user
        opens this dialog, every row's preview pane should show what
        the calibrated crop position currently selects on screen RIGHT
        NOW — not whatever frame the last OCR scan happened to capture
        (which could be many seconds old, or never if scanning is
        paused).

        Strategy per row:
          * locked → re-crop the LIVE HUD/signature capture using the
            locked rectangle. The user sees the locked region track
            the live screen, so they can verify the box still
            captures the right content as the game state changes.
          * seeded-manual → same, but with the placeholder rectangle
            the user nudged from. So the ✏ Set rectangle is also
            visibly persistent.
          * auto-detect → ask the existing ``_find_label_rows`` (HUD)
            or ``get_last_signal_crop_box`` (signature) for the
            current detected box, then re-crop from the live capture.

        Cost mitigation:
          * Skip the entire tick if the user just interacted (within
            ``INPUT_PAUSE_MS``). Keeps slider drags / spinner clicks
            from competing with screen capture for GUI thread time.
          * One mss capture per region (HUD + signature) per tick,
            shared across all rows in that region.
          * ``_find_label_rows`` only runs when at least one row is
            in auto-detect mode AND the cached result is stale (older
            than ``LABEL_DETECT_INTERVAL_S``). Locked + manual rows
            skip detection entirely.
          * Per-row freshness: rows that received a broadcast crop
            within the last ``BROADCAST_FRESHNESS_S`` seconds skip
            this tick (the broadcast is more authoritative).

        Defensive: every step is wrapped so a screen-capture failure
        or label-match exception doesn't kill the timer.
        """
        # Skip when the dialog is on the Tutorial tab — the existing
        # ``_on_tab_changed`` already gates the OCR-poll timer; mirror
        # that behaviour for this timer so we don't burn CPU on screen
        # captures the user isn't looking at.
        try:
            if self._tabs.currentIndex() != 0:
                return
        except _QT_CALL_ERRORS as exc:
            # Cannot tell which tab is up -- fall through and refresh,
            # which is the pre-existing behaviour.
            log.debug("live-refresh tab gate failed: %r", exc)

        import time as _time
        _now = _time.time()

        # Skip if the user just interacted — let the GUI thread
        # finish handling their input cleanly before stealing it back.
        try:
            if (_now - self._last_user_input_ts) < (INPUT_PAUSE_MS / 1000.0):
                return
        except (AttributeError, TypeError) as exc:
            log.debug("live-refresh input-pause gate failed: %r", exc)

        # Re-entrancy guard: drop this tick if the previous worker is
        # still running. The QTimer fires every LIVE_REFRESH_MS (1.5 s)
        # but a cold-start mss capture + NCC label-row detection can
        # exceed that on the first tick. Without this guard we'd pile
        # up workers whose results race each other to update the UI.
        if getattr(self, "_live_refresh_in_flight", False):
            return

        # Snapshot the per-field auto/locked/manual state on the UI
        # thread (cheap; touches our own widgets only). The worker
        # thread reads only this snapshot, never `_row_controls`, so
        # it doesn't race with user toggles.
        any_auto_hud_row = False
        try:
            for field, ctrl in self._row_controls.items():
                # Signature and needle aren't HUD-label-row fields:
                # signature lives under the signal region; needle
                # (difficulty bar) has no auto-detect path of its own
                # (the difficulty detector in api.scan_hud_onnx crops
                # the panel left-half, not a label row). Skip both so
                # they don't trigger the heavy ``_find_label_rows`` pass.
                if field == "signature" or field == "needle":
                    continue
                if not ctrl.is_locked() and not ctrl.is_manual():
                    any_auto_hud_row = True
                    break
        except Exception:
            any_auto_hud_row = False

        # Snapshot what the worker needs about the cache so it can
        # decide whether to re-run label-row detection. Reading these
        # is cheap; the worker mutates its own locals only.
        cached_label_rows = self._cached_hud_label_rows
        last_detect_ts = self._last_label_detect_ts
        region_snap = dict(self._region) if self._region else None
        sig_region_snap = (
            dict(self._signature_region)
            if self._signature_region is not None
            else None
        )

        self._live_refresh_in_flight = True

        import threading as _threading

        def _worker() -> None:
            """Phase 1 + Phase 2 on a daemon worker thread.

            Captures the HUD/signature regions and (when needed) runs
            the heavy label-row detection. Emits the result to the UI
            thread via the queued ``result_ready`` signal; Phase 3
            (per-row preview update) runs in
            ``_on_live_refresh_result`` on the UI thread.
            """
            hud_pil_w: Optional[Image.Image] = None
            sig_pil_w: Optional[Image.Image] = None
            hud_label_rows_w: dict = {}
            try:
                # ── Phase 1: capture the screens we need ──
                try:
                    from ocr import screen_reader as _sr
                    try:
                        if region_snap is not None:
                            hud_pil_w = _sr.capture_region(region_snap)
                    except Exception as exc:
                        log.debug(
                            "live_refresh worker: HUD capture failed: %s",
                            exc,
                        )
                    if sig_region_snap is not None:
                        try:
                            sig_pil_w = _sr.capture_region(sig_region_snap)
                        except Exception as exc:
                            log.debug(
                                "live_refresh worker: signature capture "
                                "failed: %s", exc,
                            )
                except Exception as exc:
                    log.debug(
                        "live_refresh worker: screen_reader import/capture "
                        "failed: %s", exc,
                    )

                # ── Phase 2: optional heavy label-row detection ──
                # Only when at least one HUD row is in auto-detect AND
                # the cached result is stale. Locked + manual rows
                # never need this.
                if any_auto_hud_row and hud_pil_w is not None:
                    stale = (
                        cached_label_rows is None
                        or (_now - last_detect_ts)
                        >= LABEL_DETECT_INTERVAL_S
                    )
                    if stale:
                        try:
                            from ocr import onnx_hud_reader as _ohr
                            try:
                                _ohr._set_current_region(region_snap)
                            except (AttributeError, KeyError,
                                    TypeError, ValueError) as exc:
                                log.debug(
                                    "live_refresh worker: region handoff "
                                    "failed: %r", exc,
                                )
                            hud_label_rows_w = (
                                _ohr._find_label_rows(hud_pil_w) or {}
                            )
                        except Exception as exc:
                            log.debug(
                                "live_refresh worker: _find_label_rows "
                                "failed: %s", exc,
                            )
                            hud_label_rows_w = cached_label_rows or {}
                    else:
                        hud_label_rows_w = cached_label_rows or {}

                # Hand the payload back to the UI thread. The default
                # cross-thread connection (QueuedConnection) marshals
                # this to ``_on_live_refresh_result``.
                try:
                    self._live_refresh_signaler.result_ready.emit(
                        hud_pil_w, sig_pil_w, hud_label_rows_w,
                    )
                except Exception as exc:
                    # If the dialog is being torn down, the signaler
                    # may be partially destroyed. Swallow — the next
                    # tick (if any) will try again.
                    log.debug(
                        "live_refresh worker: signal emit failed: %s",
                        exc,
                    )
            finally:
                # Always clear the in-flight flag so the next tick can
                # spawn a fresh worker.
                try:
                    self._live_refresh_in_flight = False
                except (AttributeError, RuntimeError) as exc:
                    log.debug(
                        "live_refresh worker: in-flight clear failed: %r",
                        exc,
                    )

        try:
            _threading.Thread(
                target=_worker, daemon=True, name="cal_live_refresh",
            ).start()
        except Exception as exc:
            # Thread spawn failure is exceptional — clear the flag so
            # we don't deadlock the timer.
            self._live_refresh_in_flight = False
            log.debug("live_refresh: worker spawn failed: %s", exc)

    def _on_live_refresh_result(
        self,
        hud_pil: Optional[Image.Image],
        sig_pil: Optional[Image.Image],
        hud_label_rows: dict,
    ) -> None:
        """UI-thread Phase 3: apply worker's capture + detect results.

        Receives the captured HUD/signature PILs and detected label
        rows from ``_live_refresh_tick``'s worker thread (delivered via
        the queued ``result_ready`` signal). Updates the dialog's
        cached state, then runs the per-row preview refresh — same
        widgets and cache TTL semantics as before, just split.
        """
        # Drop the result if the dialog is closing — a queued signal
        # may arrive after closeEvent has stopped the timer but before
        # the dialog is destroyed.
        try:
            if not self.isVisible():
                return
        except (RuntimeError, AttributeError) as exc:
            # A closing dialog's C++ side may already be gone; dropping
            # the payload is the correct and pre-existing behaviour.
            log.debug("live-refresh result dropped, visibility "
                      "unknown: %r", exc)
            return

        import time as _time
        _now = _time.time()

        # Mirror the old function's writes to the dialog's cached
        # capture state. Other code paths read these (status bar,
        # disk-fallback _tick, etc.).
        self.latest_hud_pil = hud_pil
        self.latest_signature_pil = sig_pil

        # Refresh the label-row cache only when the worker actually
        # ran detection (non-empty dict). An empty dict means either
        # the worker reused the cached value or there were no auto
        # rows — in both cases we leave the cache alone.
        if hud_label_rows:
            self._cached_hud_label_rows = hud_label_rows
            self._last_label_detect_ts = _now

        # Resolve the rows dict the per-row update should use. If the
        # worker reused cached rows, fall back to the current cache.
        rows_for_render = hud_label_rows or (self._cached_hud_label_rows or {})

        # ── Phase 3: per-row crop + preview refresh (UI thread) ──
        for field, ctrl in self._row_controls.items():
            try:
                # Suppress this tick's render if a broadcast crop
                # arrived very recently — that crop is the OCR-exact
                # image and we don't want to overwrite it with a re-
                # cropped frame that may differ by a frame or two.
                last_bcast = self._last_broadcast_ts_per_field.get(field, 0.0)
                if last_bcast > 0 and (_now - last_bcast) < BROADCAST_FRESHNESS_S:
                    continue

                if field == "signature":
                    self._refresh_signature_row(ctrl, sig_pil)
                else:
                    self._refresh_hud_row(ctrl, field, hud_pil, rows_for_render)
            except Exception as exc:
                log.debug(
                    "live_refresh: row %s refresh failed: %s", field, exc,
                )

    def _refresh_hud_row(
        self,
        ctrl: "_RowControl",
        field: str,
        hud_pil: Optional[Image.Image],
        hud_label_rows: dict,
    ) -> None:
        """Re-crop one HUD row from the live HUD capture and push to
        its preview. See :meth:`_live_refresh_tick` for the strategy."""
        if hud_pil is None:
            return
        # Pick the box: locked → seeded-manual → auto-detected.
        box: Optional[dict] = None
        if ctrl.is_locked() or ctrl.is_manual():
            box = ctrl.get_latest_box()
            # Locked rows also have the calibrated box on disk; if the
            # in-memory latest_box is somehow None, fall back to disk.
            if box is None and ctrl.is_locked():
                try:
                    from ocr.sc_ocr import calibration as _cal
                    box = _cal.get_row(self._region, field)
                except Exception:
                    box = None
        else:
            # Auto-detect: derive from the freshly-computed label_rows.
            row = hud_label_rows.get(field)
            if row is not None:
                # row tuple is (y1, y2, label_right) per
                # _find_label_rows return shape.
                try:
                    y1, y2, label_right = row[0], row[1], row[2]
                    box = {
                        "x": int(label_right),
                        "y": int(y1),
                        "w": max(4, int(hud_pil.width - label_right)),
                        "h": max(4, int(y2 - y1)),
                    }
                except Exception:
                    box = None

        if box is None:
            return
        cropped = self._crop_safely(hud_pil, box)
        if cropped is None:
            return
        ctrl.refresh_preview_image(cropped)

    def _refresh_signature_row(
        self,
        ctrl: "_RowControl",
        sig_pil: Optional[Image.Image],
    ) -> None:
        """Re-crop the signature row from the live signal-region
        capture. Same locked > manual > auto fallback as HUD rows."""
        if sig_pil is None:
            return
        box: Optional[dict] = None
        if ctrl.is_locked() or ctrl.is_manual():
            box = ctrl.get_latest_box()
            if box is None and ctrl.is_locked() and self._signature_region is not None:
                try:
                    from ocr.sc_ocr import calibration as _cal
                    box = _cal.get_row(self._signature_region, "signature")
                except Exception:
                    box = None
        else:
            # Auto-detect: ask the signal pipeline for the last crop
            # box. This is updated by the most recent
            # ``_signal_recognize_pil`` call (i.e. by the toolbox's
            # main scan loop). Cheap accessor — no work per tick.
            try:
                from ocr.sc_ocr import api as _api
                box = _api.get_last_signal_crop_box()
            except Exception:
                box = None

        if box is None:
            return
        cropped = self._crop_safely(sig_pil, box)
        if cropped is None:
            return
        ctrl.refresh_preview_image(cropped)

    @staticmethod
    def _crop_safely(
        img: Image.Image, box: dict,
    ) -> Optional[Image.Image]:
        """Crop ``img`` by ``box`` while clamping to image bounds.
        Returns None on degenerate input."""
        try:
            x = max(0, int(box.get("x", 0)))
            y = max(0, int(box.get("y", 0)))
            w = max(1, int(box.get("w", 0)))
            h = max(1, int(box.get("h", 0)))
            x2 = min(img.width, x + w)
            y2 = min(img.height, y + h)
            if x2 <= x or y2 <= y:
                return None
            return img.crop((x, y, x2, y2))
        except (AttributeError, KeyError, OSError,
                TypeError, ValueError) as exc:
            log.debug("crop failed for box=%r: %r", box, exc)
            return None

    # ──────────────────────────────────────────
    # Live broadcast handlers
    # ──────────────────────────────────────────

    def _on_pil_crop(self, field: str, pil) -> None:
        """Bridge from OCR worker thread → UI thread.

        Called by ``ocr.sc_ocr.live_broadcast`` on whichever thread
        produced the crop. We just emit the Qt signal — the default
        QueuedConnection will marshal the call to the UI thread.
        """
        try:
            self._live_signaler.crop_ready.emit(field, pil)
        except _QT_CALL_ERRORS as exc:
            log.debug("crop_ready emit failed (field=%s): %r", field, exc)

    def _on_live_crop(self, field: str, pil) -> None:
        """UI-thread slot for crops broadcast from the OCR pipeline.

        Updates the relevant row control with the in-memory image —
        no disk read, no PNG decode roundtrip.
        """
        import time as _time
        _now = _time.time()
        self._last_broadcast_ts = _now
        # Per-field marker: the live-refresh timer skips this row for a
        # short freshness window so the OCR-exact broadcast crop isn't
        # immediately overwritten by the timer's re-crop. ALL rows get
        # this — locked rows benefit too (they may have been getting a
        # stale frozen crop before this method fires).
        self._last_broadcast_ts_per_field[field] = _now
        ctrl = self._row_controls.get(field)
        if ctrl is None or pil is None:
            return
        try:
            if ctrl.is_locked():
                ctrl._preview.update_crop(pil)
            else:
                box = self._read_live_box(field)
                ctrl.update_live(pil, box)
        except Exception as exc:
            log.debug("live crop slot failed for %s: %s", field, exc)

    def _on_record_next_scan(self) -> None:
        """Force the next scan to write the FULL forensic disk dump.

        One click = the next OCR scan that reaches a save site will
        write every diagnostic PNG to disk regardless of which viewers
        are watching. Counter is consumed at end-of-scan in
        ``ocr.sc_ocr.api`` so it captures EXACTLY one scan.
        """
        try:
            from ocr.sc_ocr import debug_overlay as _dbg
            _dbg.force_capture_next(1)
            self._status_bar.showMessage(
                "📼 Recording next scan to disk…", 4000,
            )
        except Exception as exc:
            log.warning("Record Next Scan failed: %s", exc)
            self._status_bar.showMessage(
                f"⚠ Record Next Scan failed: {exc}", 4000,
            )


    def _crop_row_from_panel(self, field: str) -> Optional[Image.Image]:
        """Crop a row's strip from the latest panel image (for the
        mineral row, which doesn't get a separate value-crop file)."""
        try:
            from ocr.sc_ocr import debug_overlay
            img = debug_overlay._state.get("image")
            if img is None:
                return None
            label_rows = debug_overlay._state.get("label_rows", {})
            row = label_rows.get(field)
            if row is None:
                return None
            y1 = max(0, int(row["y1"]))
            y2 = min(img.height, int(row["y2"]))
            x1 = max(0, int(row.get("label_right", 0)))
            x2 = img.width
            if y2 <= y1 or x2 <= x1:
                return None
            return img.crop((x1, y1, x2, y2))
        except _OVERLAY_READ_ERRORS + (OSError,) as exc:
            log.debug("row crop from panel failed (field=%s): %r",
                      field, exc)
            return None

    def _read_live_box(self, field: str) -> Optional[dict]:
        """Read the current detected bounding box from the debug overlay
        telemetry file (if it exists)."""
        # ``signature`` lives outside debug_overlay's HUD-only state,
        # so consult the signal scanner's own last-crop-box telemetry.
        if field == "signature":
            try:
                from ocr.sc_ocr import api as _api
                box = _api.get_last_signal_crop_box()
            except Exception:
                box = None
            return box
        # The runtime debug_overlay module has a label_rows dict that
        # we'd ideally read, but it's in-memory. Easiest is to read
        # the saved label_rows from the most recent scan via a small
        # JSON sidecar. For now, derive from crop size + region.
        try:
            from ocr.sc_ocr import debug_overlay
            state = debug_overlay._state
            label_rows = state.get("label_rows", {})
            row = label_rows.get(field)
            if row is None:
                return None
            # value-crop telemetry has the precise crop box
            crops = state.get("value_crops", {})
            crop = crops.get(field)
            if crop is not None:
                x1, y1, x2, y2 = crop
                return {"x": int(x1), "y": int(y1),
                        "w": int(x2 - x1), "h": int(y2 - y1)}
            # Fallback: row band + default x range
            return {
                "x": int(row.get("label_right", 0)),
                "y": int(row["y1"]),
                "w": 200,  # rough estimate
                "h": int(row["y2"] - row["y1"]),
            }
        except _OVERLAY_READ_ERRORS as exc:
            log.debug("live box read failed (field=%s): %r", field, exc)
            return None

    # ──────────────────────────────────────────
    # Lock / unlock handlers
    # ──────────────────────────────────────────

    def _on_row_box_changed(self, field: str, box: dict) -> None:
        """User nudged a row's box. Re-crop the panel image with the
        new coords and update the preview.

        Source image differs by field:
          * HUD rows → ``debug_overlay._state["image"]`` (the HUD-region
            capture the OCR pipeline most recently scanned).
          * ``signature`` → ``screen_reader.get_last_capture()`` (the
            signal-region capture). The signal pipeline doesn't push
            into ``debug_overlay``, so we read from the signal scanner's
            own last-frame cache instead.
        """
        try:
            img = None
            if field == "signature":
                try:
                    from ocr import screen_reader as _sr
                    img = _sr.get_last_capture()
                except Exception:
                    img = None
                if img is None:
                    self._status_bar.showMessage(
                        "Cannot re-crop signature: no signal-region "
                        "capture yet (start scanning so the signal "
                        "scanner grabs a frame)", 5000,
                    )
                    return
            else:
                # Pull the latest panel image from the debug overlay state
                from ocr.sc_ocr import debug_overlay
                img = debug_overlay._state.get("image")
                if img is None:
                    # Fall back: read the saved overlay PNG
                    from pathlib import Path
                    overlay_path = Path(debug_overlay.OUT_PATH)
                    if overlay_path.is_file():
                        img = Image.open(overlay_path).convert("RGB")
                if img is None:
                    self._status_bar.showMessage(
                        "Cannot re-crop: no panel image available "
                        "(start scanning so a panel is captured)", 5000,
                    )
                    return
            x, y, w, h = box["x"], box["y"], box["w"], box["h"]
            x2 = min(img.width, x + w)
            y2 = min(img.height, y + h)
            if x2 <= x or y2 <= y:
                return
            crop = img.crop((x, y, x2, y2))
            self._row_controls[field].update_manual(crop, box)
        except Exception as exc:
            log.debug("re-crop on nudge failed: %s", exc)

    def _region_for_field(self, field: str) -> Optional[dict]:
        """Return the region dict the given field's calibration is
        keyed under.

        ``signature`` lives in the SIGNAL-scanner ``ocr_region`` (a
        separate on-screen rectangle from the HUD); every other field
        belongs to the HUD region. Returns ``None`` for ``signature``
        when the user hasn't set the signal region yet — callers should
        treat that as "can't save / load this field" and surface a
        helpful status message.
        """
        if field == "signature":
            return self._signature_region
        return self._region

    def _on_row_locked(self, field: str, box: dict) -> None:
        target_region = self._region_for_field(field)
        if target_region is None:
            self._status_bar.showMessage(
                f"⚠ Cannot lock {DISPLAY_NAMES.get(field, field)}: "
                "signal scanner region not set. Use 'Set Scanning "
                "Region' in the toolbox first, then re-open this "
                "dialog.",
                10000,
            )
            log.warning(
                "calibration_dialog: refusing to save field=%s — no "
                "signature_region available",
                field,
            )
            return

        # Determine value_column_left. If multiple HUD rows are locked,
        # use the rightmost x (= longest label's colon position).
        # Skip this for the signature field since it's keyed under a
        # different region and value_column_left is meaningless for
        # the signal pipeline. Likewise needle (the difficulty-bar
        # crop) sits below the value column entirely — its x
        # coordinate has nothing to do with where MASS / RESISTANCE /
        # INSTABILITY values start.
        if field == "signature" or field == "needle":
            value_column_left = int(box["x"])
        else:
            all_xs = [box["x"]]
            for f, c in self._row_controls.items():
                # Same exclusion logic when sourcing peer x's: skip
                # signature (different region) and needle (irrelevant
                # to the value column).
                if (
                    f == field
                    or f == "signature"
                    or f == "needle"
                    or not c.is_locked()
                ):
                    continue
                existing = calibration.get_row(self._region, f)
                if existing:
                    all_xs.append(existing["x"])
            # Wait — value_column_left should be the X where values START
            # (the user-locked crop's LEFT edge IS that x). Pick max so
            # short-label rows (smaller x) don't override the longest
            # label's anchor.
            value_column_left = max(all_xs)

        # Try to capture image size from the debug overlay state
        image_size = None
        try:
            from ocr.sc_ocr import debug_overlay
            img = debug_overlay._state.get("image")
            if img is not None:
                image_size = img.size
        except _OVERLAY_READ_ERRORS as exc:
            log.debug("panel image size unavailable on row lock: %r", exc)

        calibration.save_row(
            target_region, field, box,
            image_size=image_size,
            value_column_left=value_column_left,
        )
        # Verify the row actually landed on disk. If the file was
        # written but the row didn't persist (e.g. AV interference,
        # permission issue, race), the user needs to know NOW rather
        # than discover it later when boxes jump during scanning.
        verify_box = calibration.get_row(target_region, field)
        if verify_box is None:
            self._status_bar.showMessage(
                f"⚠ FAILED to persist {DISPLAY_NAMES.get(field, field)} "
                f"to calibration.json — check log for details",
                10000,
            )
            log.error(
                "calibration_dialog: save_row(%s) reported success but "
                "get_row read-back returned None for region=%s",
                field, target_region,
            )
        else:
            self._status_bar.showMessage(
                f"Saved {DISPLAY_NAMES.get(field, field)} → "
                f"x={box['x']} y={box['y']} w={box['w']} h={box['h']}",
                5000,
            )
        self._refresh_completion_banner()

    def _on_row_unlocked(self, field: str) -> None:
        target_region = self._region_for_field(field)
        if target_region is None:
            return
        calibration.remove_row(target_region, field)
        self._status_bar.showMessage(
            f"Unlocked {DISPLAY_NAMES.get(field, field)}", 3000,
        )
        self._refresh_completion_banner()

    def _on_reset_all(self) -> None:
        calibration.clear_region(self._region)
        for ctrl in self._row_controls.values():
            ctrl.reset()
        self._refresh_completion_banner()
        self._status_bar.showMessage("All calibration cleared", 3000)

    def _on_scan_now(self) -> None:
        """Fire one OCR scan via the parent ``MiningSignalsApp``.

        Routes through ``MiningSignalsApp.force_one_scan`` so the dialog
        doesn't have to know anything about the scan worker plumbing.
        Defensive: if the parent isn't a ``MiningSignalsApp`` (e.g. the
        dialog was opened in a test harness), just log and no-op.

        UI affordances:
        * Disable the button for ~1.5 s so the user can't spam-click it.
          The Qt event loop debounce is sufficient — we don't need a
          re-entrancy lock because the button is the only caller.
        * Show a transient status-bar message; when the next scan crop
          arrives the existing ``_on_live_crop`` flow refreshes the
          previews automatically.
        """
        cb = getattr(self, "_force_one_scan_cb", None)
        if cb is None:
            try:
                parent = self.parent()
            except Exception:
                parent = None
            if parent is not None and hasattr(parent, "force_one_scan"):
                cb = parent.force_one_scan

        if cb is None:
            log.info(
                "scan-now: parent has no force_one_scan() — no-op "
                "(dialog likely opened standalone)",
            )
            self._status_bar.showMessage(
                "Scan now unavailable in this context", 3000,
            )
            return

        try:
            cb()
        except Exception as exc:
            log.warning("scan-now: force_one_scan() raised: %s", exc)
            self._status_bar.showMessage(
                "Scan now failed — see logs", 4000,
            )
            return

        self._status_bar.showMessage(
            "Scanning now — preview will refresh momentarily", 3000,
        )
        log.info("calibration dialog: scan-now button clicked")

        # Brief debounce so the user can't queue a flood of one-shot
        # scans by hammering the button. 1.5 s comfortably covers the
        # typical OCR pipeline pass and matches the status-bar visual.
        # Wrap the re-enable in a try/except — if the user closes the
        # dialog before the timer fires the underlying QWidget has
        # been deleted, and accessing it raises RuntimeError("Internal
        # C++ object already deleted").
        self._scan_now_btn.setEnabled(False)

        def _re_enable() -> None:
            try:
                self._scan_now_btn.setEnabled(True)
            except RuntimeError:
                # Dialog was closed before the debounce expired — the
                # button no longer exists. Nothing to do.
                pass

        QTimer.singleShot(1500, _re_enable)

    def _on_reset_consensus(self) -> None:
        """Manual unstick — clear all rolling-window consensus + locked
        OCR values so the next scan starts fresh.

        Routes through the parent ``MiningSignalsApp.reset_consensus_state``
        if available (it owns the per-app HUD windows AND knows how to
        reach into ``ocr.sc_ocr.api``'s module-level state). Falls back
        to a direct ``api.reset_all_consensus()`` call if the parent
        hookup is missing — defensive only, so the button still does
        something sensible if the dialog is ever opened standalone.
        """
        cleared_app = False
        try:
            parent = self.parent()
            if parent is not None and hasattr(parent, "reset_consensus_state"):
                parent.reset_consensus_state()
                cleared_app = True
        except Exception as exc:
            log.warning(
                "reset consensus: parent.reset_consensus_state() failed: %s",
                exc,
            )

        if not cleared_app:
            # Fallback: at least flush the SC-OCR module-level buffers.
            try:
                from ocr.sc_ocr import api as _sc_api
                _sc_api.reset_all_consensus()
            except Exception as exc:
                log.warning(
                    "reset consensus: sc_ocr.api.reset_all_consensus() "
                    "fallback failed: %s",
                    exc,
                )
                self._status_bar.showMessage(
                    "Reset consensus failed — see logs", 4000,
                )
                return

        self._status_bar.showMessage(
            "Consensus state cleared — next scan starts fresh", 4000,
        )
        log.info("calibration dialog: reset consensus button clicked")

    def _refresh_completion_banner(self) -> None:
        complete = calibration.is_complete(self._region)
        if complete:
            self._completion_banner.setText("✅ CALIBRATION COMPLETE")
            self._completion_banner.setVisible(True)
        else:
            self._completion_banner.setVisible(False)

    def _refresh_header(self) -> None:
        r = self._region
        self._header.setText(
            f"HUD region: x={r.get('x')}, y={r.get('y')}, "
            f"w={r.get('w')}, h={r.get('h')}"
        )

    def _reload_locked_state_from_disk(self) -> None:
        """On open, read existing calibration and mark locked rows.

        HUD rows (mass / resistance / instability / _mineral_row) are
        keyed under ``self._region`` (the HUD region). The signature
        row is keyed under ``self._signature_region`` (the signal
        scanner's ocr_region) when one is set — load it from there.
        """
        from pathlib import Path as _P
        tool_dir = _P(__file__).resolve().parent.parent

        def _restore_rows(cal_dict: Optional[dict], allow_signature: bool) -> None:
            if not cal_dict:
                return
            for field, box in cal_dict.get("rows", {}).items():
                if field not in self._row_controls:
                    continue
                # Filter so HUD-keyed cal data only restores HUD rows
                # and signature-keyed cal data only restores signature.
                if field == "signature" and not allow_signature:
                    continue
                if field != "signature" and allow_signature:
                    continue
                crop_path = tool_dir / f"debug_value_{field}_crop.png"
                pil = None
                if crop_path.is_file():
                    try:
                        pil = Image.open(crop_path).convert("RGB")
                    except Exception:
                        pil = None
                self._row_controls[field].display_locked(pil, box)

        _restore_rows(calibration.load(self._region), allow_signature=False)
        if self._signature_region is not None:
            _restore_rows(
                calibration.load(self._signature_region),
                allow_signature=True,
            )
        self._refresh_completion_banner()

    # ──────────────────────────────────────────
    # Panel Finder popout
    # ──────────────────────────────────────────

    def _on_open_panel_finder(self) -> None:
        """Open (or raise) the standalone Panel Finder window.

        Single-instance guard:
          1. If we already created one in THIS process, raise it.
          2. Otherwise try to claim the cross-process slot. If
             another process holds it (e.g. the user double-clicked
             ``LAUNCH_PanelFinderViewer.bat``), the holder is poked
             to come to the front and we abort our own open.
        """
        try:
            from ui.panel_finder_popout import PanelFinderPopout
            # See mining_signals_app comment: ``mining_shared`` avoids
            # the name collision with SC_Toolbox's parent ``shared/``
            # package that the launcher pre-imports.
            from mining_shared.single_instance import SingleInstance

            existing = getattr(self, "_panel_finder_window", None)
            if existing is not None and existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return

            popout = PanelFinderPopout(parent=self)
            guard = SingleInstance("panel_finder", popout)
            if not guard.acquire():
                popout.deleteLater()
                self._status_bar.showMessage(
                    "Panel Finder is already open in another window — "
                    "brought to the front.", 5000,
                )
                return
            popout._single_instance = guard  # keep guard alive
            self._panel_finder_window = popout
            popout.show()
        except Exception as exc:
            log.error("panel finder popout failed: %s", exc, exc_info=True)
            self._status_bar.showMessage(
                f"Could not open Panel Finder: {exc}", 5000,
            )

    # ──────────────────────────────────────────
    # Signature Finder popout
    # ──────────────────────────────────────────

    def _on_open_signature_finder(self) -> None:
        """Open (or raise) the Signature Finder window.

        Mirrors :meth:`_on_open_panel_finder` exactly:
          1. Raise an existing in-process window if present.
          2. Otherwise try to claim the cross-process slot. If
             another process holds it (standalone .bat launch), poke
             the holder to come to the front and abort.
        """
        try:
            from scripts.signature_finder_viewer import SignatureFinderViewer
            # See mining_signals_app comment: ``mining_shared`` avoids
            # the name collision with SC_Toolbox's parent ``shared/``
            # package that the launcher pre-imports.
            from mining_shared.single_instance import SingleInstance

            existing = getattr(self, "_signature_finder_window", None)
            if existing is not None and existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return

            popout = SignatureFinderViewer()
            # Re-parent to the dialog so closing the calibration
            # window also tears down the viewer cleanly. Qt.Window
            # flag keeps it as its own top-level window.
            popout.setParent(self, Qt.Window)
            guard = SingleInstance("signature_finder", popout)
            if not guard.acquire():
                popout.deleteLater()
                self._status_bar.showMessage(
                    "Signature Finder is already open in another "
                    "window — brought to the front.", 5000,
                )
                return
            popout._single_instance = guard  # keep guard alive
            self._signature_finder_window = popout
            popout.show()
        except Exception as exc:
            log.error(
                "signature finder popout failed: %s", exc, exc_info=True,
            )
            self._status_bar.showMessage(
                f"Could not open Signature Finder: {exc}", 5000,
            )

    # ──────────────────────────────────────────
    # Glyph Reader popout
    # ──────────────────────────────────────────

    def _on_open_glyph_reader(self) -> None:
        """Open (or raise) the Glyph Reader window. Same pattern as
        the other two popouts: in-process raise → cross-process slot →
        poke the existing holder if one exists."""
        try:
            import sys as _sys, importlib as _il
            from pathlib import Path as _P
            _tool = _P(__file__).resolve().parent.parent
            if str(_tool) not in _sys.path:
                _sys.path.insert(0, str(_tool))
            _il.invalidate_caches()
            from scripts.glyph_reader_viewer import GlyphReaderViewer
            from mining_shared.single_instance import SingleInstance

            existing = getattr(self, "_glyph_reader_window", None)
            if existing is not None and existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return

            popout = GlyphReaderViewer()
            popout.setParent(self, Qt.Window)
            guard = SingleInstance("glyph_reader", popout)
            if not guard.acquire():
                popout.deleteLater()
                self._status_bar.showMessage(
                    "Glyph Reader is already open in another window — "
                    "brought to the front.", 5000,
                )
                return
            popout._single_instance = guard
            self._glyph_reader_window = popout
            popout.show()
        except Exception as exc:
            log.error(
                "glyph reader popout failed: %s", exc, exc_info=True,
            )
            self._status_bar.showMessage(
                f"Could not open Glyph Reader: {exc}", 5000,
            )

    # ──────────────────────────────────────────
    # Voice tutorial
    # ──────────────────────────────────────────

    def _on_voice_play(self) -> None:
        """Play the calibration tutorial WAV.

        Fast path: cached WAV exists in the project → instant playback.
        First-time path: synthesize via Pocket TTS once, cache, then play.
        """
        from ui import voice_tutorial as _vt
        self._voice_btn.setEnabled(False)
        # Show synthesizing message ONLY if we actually need to generate
        # (avoid flashing the message for the cached path).
        if _vt._find_cached_tutorial() is None:
            self._voice_status.setText(
                "Generating tutorial audio (one-time, ~30 sec)…"
            )
            QApplication.processEvents()
        wav_path, source = _vt.get_tutorial_audio()
        if wav_path is None:
            self._voice_status.setText(
                "❌ No cached audio AND Pocket TTS unreachable on "
                "localhost:49112. Start Pocket TTS once to generate."
            )
            self._voice_btn.setEnabled(True)
            return
        if source == "generated":
            self._voice_status.setText(
                f"✓ Cached to {wav_path.name} — playing…"
            )
        # Lazy-init the player
        if self._voice_player is None:
            self._voice_player = _vt.VoicePlayer(
                on_state_change=self._on_voice_state,
            )
        ok = self._voice_player.play(wav_path)
        if not ok:
            self._voice_status.setText(
                "❌ Audio playback failed (Qt Multimedia issue)"
            )
            self._voice_btn.setEnabled(True)
            return
        if source == "cached":
            self._voice_status.setText("🔊 Playing…")
        self._voice_stop_btn.setEnabled(True)
        # Fresh playback starts in the playing state; reset the pause
        # button to the "⏸ Pause" face (it might be left as "▶ Resume"
        # if the user paused a previous run and clicked Play again).
        self._voice_pause_btn.setText("⏸ Pause")
        self._voice_pause_btn.setEnabled(True)

    def _on_voice_pause(self) -> None:
        """Toggle between pause and resume on the same button.

        Decision is based on the player's CURRENT state, not the
        button's label, so we stay in sync even if Qt's playback state
        change beats our click handler to the punch (the
        ``_on_voice_state`` callback also rewrites the label).
        """
        if self._voice_player is None:
            return
        if self._voice_player.is_playing():
            self._voice_player.pause()
            # Optimistic UI: relabel immediately. The state-change
            # callback will reaffirm a moment later.
            self._voice_pause_btn.setText("▶ Resume")
            self._voice_status.setText("⏸ Paused")
        elif self._voice_player.is_paused():
            self._voice_player.resume()
            self._voice_pause_btn.setText("⏸ Pause")
            self._voice_status.setText("🔊 Playing…")

    def _on_voice_stop(self) -> None:
        if self._voice_player is not None:
            self._voice_player.stop()
        self._voice_status.setText("Stopped")
        self._voice_btn.setEnabled(True)
        self._voice_stop_btn.setEnabled(False)
        self._voice_pause_btn.setEnabled(False)
        self._voice_pause_btn.setText("⏸ Pause")

    def _on_voice_state(self, state: str) -> None:
        if state == "stopped":
            self._voice_status.setText("Done")
            self._voice_btn.setEnabled(True)
            self._voice_stop_btn.setEnabled(False)
            self._voice_pause_btn.setEnabled(False)
            self._voice_pause_btn.setText("⏸ Pause")
        elif state == "playing":
            self._voice_status.setText("🔊 Playing…")
            self._voice_pause_btn.setEnabled(True)
            self._voice_pause_btn.setText("⏸ Pause")
        elif state == "paused":
            self._voice_status.setText("⏸ Paused")
            self._voice_pause_btn.setEnabled(True)
            self._voice_pause_btn.setText("▶ Resume")
        elif state.startswith("error"):
            self._voice_status.setText(f"❌ {state}")
            self._voice_btn.setEnabled(True)
            self._voice_stop_btn.setEnabled(False)
            self._voice_pause_btn.setEnabled(False)
            self._voice_pause_btn.setText("⏸ Pause")

    # ──────────────────────────────────────────
    # Global column x-offset (Feature 1)
    # ──────────────────────────────────────────

    def _safe_get_col_offset(self) -> int:
        """Read the saved column x-offset, defaulting to 0 on any error.

        Defensive because the dialog should always come up cleanly even
        if the calibration accessor is missing / throws (e.g. an old
        calibration.json schema during an upgrade)."""
        try:
            return int(calibration.get_column_x_offset(self._region))
        except Exception as exc:
            log.debug(
                "get_column_x_offset failed (defaulting to 0): %s", exc,
            )
            return 0

    def _refresh_col_status(self) -> None:
        """Update the human-readable status next to the spin box."""
        try:
            v = int(self._col_spin.value())
        except Exception:
            v = 0
        if v == 0:
            txt = "Column x: 0 px (no shift)"
        elif v < 0:
            txt = f"Column x: {v} px (left shift)"
        else:
            txt = f"Column x: +{v} px (right shift)"
        try:
            self._col_status.setText(txt)
        except _QT_CALL_ERRORS as exc:
            log.debug("column status label update failed: %r", exc)

    def _on_col_offset_changed(self, value: int) -> None:
        """Spin box value changed → persist + update label.

        Called on EVERY value change (typing, arrows, our own programmatic
        sets), so persistence has to be cheap. Failures are logged but
        not surfaced — the saved value lives in calibration.json which
        the pipeline picks up on the next scan automatically."""
        try:
            calibration.set_column_x_offset(self._region, int(value))
        except Exception as exc:
            log.warning(
                "set_column_x_offset(%s) failed: %s", value, exc,
            )
            try:
                self._status_bar.showMessage(
                    f"⚠ Could not save column offset: {exc}", 4000,
                )
            except _QT_CALL_ERRORS as exc2:
                # The save failure itself is already logged by the caller;
                # this only reports that the user never saw the toast.
                log.debug("column-offset toast not shown: %r", exc2)
        self._refresh_col_status()

    def _on_col_nudge(self, direction: int) -> None:
        """← / → button — bump the spin box by ±1 (or ±5 with Shift).

        We change the spin box value (rather than calling set_column_
        x_offset directly) so the existing valueChanged → save path
        runs and there's only one source of truth."""
        step = 1
        try:
            mods = QApplication.keyboardModifiers()
            if mods & Qt.ShiftModifier:
                step = 5
        except _QT_CALL_ERRORS as exc:
            log.debug("column-nudge shift-modifier probe failed: %r", exc)
        try:
            self._col_spin.setValue(int(self._col_spin.value()) + direction * step)
        except Exception as exc:
            log.debug("col nudge failed: %s", exc)

    def _reload_col_offset_for_region(self) -> None:
        """Re-read the saved column offset and push it into the spin box.

        Called after the dialog's region changes — the spin box reflects
        the NEW region's saved offset, not the previous region's. Block
        valueChanged briefly so the read-back doesn't immediately fire
        a save back into calibration with the same value."""
        try:
            self._col_spin.blockSignals(True)
            self._col_spin.setValue(self._safe_get_col_offset())
        except _QT_CALL_ERRORS as exc:
            log.debug("column spin reload failed: %r", exc)
        finally:
            try:
                self._col_spin.blockSignals(False)
            except _QT_CALL_ERRORS as exc:
                log.debug("column spin unblock failed: %r", exc)
        self._refresh_col_status()

    # ──────────────────────────────────────────
    # Emergency override / Manual Override dialog (Feature 2)
    # ──────────────────────────────────────────

    def _is_override_active(self) -> bool:
        try:
            return bool(calibration.get_manual_override_mode(self._region))
        except Exception as exc:
            log.debug("get_manual_override_mode failed: %s", exc)
            return False

    def _refresh_override_indicator(self) -> None:
        """Sync the title prefix + small status label + visibility of
        the 'Disable manual override' button to the saved override flag."""
        active = self._is_override_active()
        # Window title prefix.
        base = "Mining HUD OCR Calibration"
        try:
            if active:
                self.setWindowTitle(f"[OVERRIDE ACTIVE] {base}")
            else:
                self.setWindowTitle(base)
        except _QT_CALL_ERRORS as exc:
            log.debug("override window-title update failed: %r", exc)
        # Small inline label next to the EMERGENCY button.
        try:
            if active:
                self._override_status_lbl.setText("● Manual override ACTIVE")
            else:
                self._override_status_lbl.setText("")
        except _QT_CALL_ERRORS as exc:
            log.debug("override status label update failed: %r", exc)
        try:
            self._disable_override_btn.setVisible(active)
        except _QT_CALL_ERRORS as exc:
            log.debug("override button visibility update failed: %r", exc)

    def _on_open_emergency_override(self) -> None:
        """Open the Manual Override dialog modally."""
        # Lazy import so the calibration dialog still loads even if the
        # manual override module has an import-time issue (it shouldn't
        # — the smoke test catches that — but we'd rather show a status
        # message than crash the calibration UI).
        try:
            from ui.manual_override_dialog import ManualOverrideDialog
        except Exception as exc:
            log.error(
                "import ManualOverrideDialog failed: %s",
                exc, exc_info=True,
            )
            self._status_bar.showMessage(
                f"⚠ Could not open Manual Override: {exc}", 6000,
            )
            return

        # Capture a fresh HUD frame for the user to draw on. Prefer the
        # most-recent capture cached by the live-refresh timer (saves an
        # mss grab); fall back to a fresh capture if that's stale or
        # missing.
        hud_pil: Optional[Image.Image] = getattr(
            self, "latest_hud_pil", None,
        )
        if hud_pil is None and self._region is not None:
            try:
                from ocr import screen_reader as _sr
                hud_pil = _sr.capture_region(self._region)
            except Exception as exc:
                log.debug(
                    "manual override: live capture failed: %s", exc,
                )
                hud_pil = None

        dlg = ManualOverrideDialog(
            region=self._region,
            hud_pil=hud_pil,
            parent=self,
        )
        dlg.overrides_saved.connect(self._on_overrides_saved)
        dlg.exec()
        # exec() blocks; on return the override flag may have changed.
        self._refresh_override_indicator()

    def _on_overrides_saved(self, saved: dict) -> None:
        """Manual override dialog reported a successful save."""
        try:
            count = len(saved)
        except Exception:
            count = 0
        self._status_bar.showMessage(
            f"✅ Manual override saved ({count} field(s)). OCR pipeline "
            "will use these boxes on the next scan.", 8000,
        )
        log.info(
            "calibration: manual override saved for region=%s, "
            "fields=%s",
            self._region, list(saved.keys()),
        )
        self._refresh_override_indicator()

    def _on_disable_override(self) -> None:
        """Turn off manual override without re-drawing.

        Just flips the flag — leaves the saved boxes in place so the
        user can re-enable them later by editing calibration.json or by
        re-opening the manual override dialog (which loads from saved
        state if implemented). For the v2.2.6.1 release we don't expose
        a 're-enable from saved' button, but that's a future-friendly
        knob."""
        try:
            calibration.set_manual_override_mode(self._region, False)
        except Exception as exc:
            log.error(
                "set_manual_override_mode(False) failed: %s",
                exc, exc_info=True,
            )
            self._status_bar.showMessage(
                f"⚠ Could not disable override: {exc}", 6000,
            )
            return
        self._status_bar.showMessage(
            "Manual override disabled. Auto-detection / locks resumed.",
            5000,
        )
        log.info(
            "calibration: manual override DISABLED for region=%s",
            self._region,
        )
        self._refresh_override_indicator()

    def _on_tab_changed(self, index: int) -> None:
        """Pause the OCR polling timer when leaving the Calibrate tab."""
        # Tab 0 = Calibrate, Tab 1 = Tutorial
        if index == 0:
            if not self._timer.isActive():
                self._timer.start(POLL_MS)
            # Also resume the continuous live-preview refresh so
            # returning to the Calibrate tab immediately reflects the
            # current screen at every row's calibrated crop position.
            try:
                if not self._live_refresh_timer.isActive():
                    self._live_refresh_timer.start(LIVE_REFRESH_MS)
            except _QT_CALL_ERRORS as exc:
                log.debug("live-refresh timer start failed: %r", exc)
            self._status_bar.showMessage("Live polling resumed", 2000)
        else:
            if self._timer.isActive():
                self._timer.stop()
            try:
                if self._live_refresh_timer.isActive():
                    self._live_refresh_timer.stop()
            except _QT_CALL_ERRORS as exc:
                log.debug("live-refresh timer stop failed: %r", exc)
            self._status_bar.showMessage(
                "Live polling paused (not on Calibrate tab)", 0,
            )

    # ──────────────────────────────────────────
    # Drag-pause for live-refresh
    # ──────────────────────────────────────────
    # The 500ms live-refresh timer fires screen captures + label_match
    # — heavy work that competes with the window-move event handling
    # while the user drags the dialog around. The window stutters /
    # rubber-bands as a result. We pause both timers on first moveEvent
    # and restart them ~250ms after movement stops via a debounced
    # singleShot.

    def moveEvent(self, event):
        """Pause heavy timers during window drag; resume shortly after."""
        try:
            super().moveEvent(event)
        except _QT_CALL_ERRORS as exc:
            log.debug("moveEvent super() failed: %r", exc)
        try:
            # Lazily create the resume-debounce timer.
            if not hasattr(self, "_resume_after_move_timer"):
                self._resume_after_move_timer = QTimer(self)
                self._resume_after_move_timer.setSingleShot(True)
                self._resume_after_move_timer.timeout.connect(self._resume_timers_after_move)
            # Pause the heavy timers if they're currently running.
            if self._live_refresh_timer.isActive():
                self._live_refresh_timer.stop()
                self._was_live_refresh_active = True
            if self._timer.isActive():
                self._timer.stop()
                self._was_poll_timer_active = True
            # Debounce: each move resets the resume timer to fire
            # 250 ms after the LAST move event.
            self._resume_after_move_timer.start(250)
        except _QT_CALL_ERRORS as exc:
            log.debug("moveEvent timer pause failed: %r", exc)

    def _resume_timers_after_move(self) -> None:
        """Restart the timers paused during window drag."""
        try:
            if getattr(self, "_was_poll_timer_active", False):
                self._timer.start(POLL_MS)
                self._was_poll_timer_active = False
            # Only restart live-refresh if we're still on the Calibrate tab.
            if getattr(self, "_was_live_refresh_active", False):
                # Mirror the tab-aware logic in _on_tab_changed: only resume
                # if Calibrate tab is active.
                tab_idx = self._tabs.currentIndex() if hasattr(self, "_tabs") else 0
                if tab_idx == 0:
                    self._live_refresh_timer.start(LIVE_REFRESH_MS)
                self._was_live_refresh_active = False
        except _QT_CALL_ERRORS as exc:
            log.debug("timer resume after move failed: %r", exc)

    def closeEvent(self, event):
        # Each teardown step is independently guarded so one failure cannot
        # skip the rest. Narrowed to _QT_CALL_ERRORS: every body below is a
        # no-arg method call or an attribute set on a Qt wrapper, so a
        # deleted C++ object (RuntimeError) or a never-created attribute
        # (AttributeError) is the whole reachable set.
        try:
            self._timer.stop()
        except _QT_CALL_ERRORS as exc:
            log.debug("closeEvent: poll timer stop failed: %r", exc)
        try:
            self._live_refresh_timer.stop()
        except _QT_CALL_ERRORS as exc:
            log.debug("closeEvent: live-refresh timer stop failed: %r", exc)
        # Defensively clear the live-refresh in-flight flag. The worker
        # thread itself is fire-and-forget (daemon); if it's still
        # running it'll finish, attempt to emit the queued signal, and
        # ``_on_live_refresh_result`` will drop the payload because
        # ``isVisible()`` returns False on a closing dialog.
        try:
            self._live_refresh_in_flight = False
        except (AttributeError, RuntimeError) as exc:
            log.debug("closeEvent: in-flight clear failed: %r", exc)
        try:
            if hasattr(self, "_resume_after_move_timer"):
                self._resume_after_move_timer.stop()
        except _QT_CALL_ERRORS as exc:
            log.debug("closeEvent: resume timer stop failed: %r", exc)
        # Uninstall the global event filter we attached in __init__ —
        # leaving it on QApplication after the dialog is gone causes
        # event dispatch into a half-dead Python object (Qt has a C++
        # ref but Python may have dropped it).
        try:
            if getattr(self, "_installed_app_filter", False):
                _app = QApplication.instance()
                if _app is not None:
                    _app.removeEventFilter(self)
                self._installed_app_filter = False
        except _QT_CALL_ERRORS as exc:
            # Worth WARNING: a filter left installed on QApplication keeps
            # dispatching events into a half-dead Python object, which is
            # the crash this teardown exists to prevent.
            log.warning(
                "closeEvent: app event filter NOT uninstalled: %r", exc,
            )
        # Stop any in-flight voice playback so audio doesn't keep
        # narrating after the dialog is gone.
        try:
            if getattr(self, "_voice_player", None) is not None:
                self._voice_player.stop()
        except (AttributeError, RuntimeError, OSError) as exc:
            log.debug("closeEvent: voice playback stop failed: %r", exc)
        # Unregister the live-broadcast listener so the dialog can be
        # garbage-collected and reopened cleanly without leaking a
        # reference to the old instance.
        try:
            if getattr(self, "_broadcast_registered", False):
                from ocr.sc_ocr import live_broadcast as _bcast
                _bcast.unregister_listener(self._on_pil_crop)
                self._broadcast_registered = False
        except Exception as exc:
            log.debug("live_broadcast unregister failed: %s", exc)
        super().closeEvent(event)
