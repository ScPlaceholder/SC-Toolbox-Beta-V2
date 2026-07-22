"""Fullscreen overlay for selecting the OCR capture region.

The user clicks and drags to define a rectangle on screen.  The
selected region is returned via a Qt signal.  Coordinates are
in native screen pixels (matching what ``mss`` captures).

Live detector tie-in
---------------------
When constructed with a ``detector`` mode (``"hud"`` or ``"signal"``),
the selector captures the screen under the in-progress selection and
runs the real HUD finders against it on a worker thread (see
``ui/region_detector.py``).  The detected sub-regions are painted back
inside the selection as "ghost" outlines — the same regions the live
OCR debug overlay annotates — and snap GREEN once every expected region
is found, so the user gets immediate confirmation that the box is
framed correctly before releasing.

So the detectors see the clean game underneath (and never the ghost
boxes we draw on top), the overlay window is marked
``WDA_EXCLUDEFROMCAPTURE``: it stays fully visible to the user but is
invisible to screen capture.  Without that, each capture would include
our own painted boxes and the next detection would degrade.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

from PySide6.QtCore import Qt, QRect, QPoint, QObject, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QCursor
from PySide6.QtWidgets import QWidget, QApplication, QMessageBox

from shared.qt.theme import P

log = logging.getLogger(__name__)

# Minimum interval between successive detection spawns while dragging.
# Detection (multi-scale NCC) is tens of ms; a fresh spawn every ~180 ms
# keeps the ghosts feeling live without flooding the worker. The
# in-flight guard drops anything that arrives while a worker is busy.
DETECT_THROTTLE_MS = 180

# A low-frequency tick re-runs detection even when the cursor is holding
# still, so the signal anchor's temporal-smoothing cache can accumulate
# the few frames it needs to confirm the icon (and snap green) while the
# user keeps the box steady over the panel.
DETECT_TICK_MS = 120


def _get_cursor_pos() -> tuple[int, int]:
    """Get the cursor position in native screen pixels via Win32 API.

    This bypasses any Qt coordinate scaling and gives raw pixel
    coordinates that match what ``mss`` captures.
    """
    try:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y)
    except Exception:
        pos = QCursor.pos()
        return (pos.x(), pos.y())


class _DetectSignaler(QObject):
    """Cross-thread bridge for detection results.

    The detector runs on a daemon worker thread; Qt widgets must only be
    touched on the GUI thread. This QObject's signal carries the result
    payload back to the GUI thread via the default queued connection.
    Payload type is ``object`` so a plain dict passes through verbatim.
    """

    result_ready = Signal(object)


class RegionSelector(QWidget):
    """Translucent fullscreen overlay — drag to select a rectangle.

    Parameters
    ----------
    detector:
        Optional detector mode to tie in live region finders:
        ``"hud"`` (SCAN RESULTS panel) or ``"signal"`` (signature
        scanner). ``None`` (default) keeps the classic plain selector.
    """

    region_selected = Signal(dict)  # {"x": int, "y": int, "w": int, "h": int}
    cancelled = Signal()

    def __init__(
        self,
        parent=None,
        detector: str | None = None,
        game_resolution: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)

        # Cover the entire virtual desktop (all monitors)
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.virtualGeometry()
            self.setGeometry(geom)
        else:
            self.showFullScreen()

        # Store raw native pixel coordinates from Win32 API
        self._origin_native: tuple[int, int] | None = None
        self._current_native: tuple[int, int] | None = None

        # Visual rect in widget coordinates (for painting)
        self._rect: QRect = QRect()
        self._dragging = False

        # ── Live detection state ──
        self._detector_mode = detector if detector in ("hud", "signal") else None
        # Effective game resolution ({"w","h","source"}) used to scale
        # the detectors' template sweep and shown to the user.
        self._game_resolution = game_resolution if isinstance(
            game_resolution, dict
        ) and game_resolution.get("w") else None
        # Region-relative boxes from the most recent detection, the
        # native region they were measured against, and whether every
        # expected region was found (→ paint green).
        self._detect_boxes: list[dict] = []
        self._detect_region: dict | None = None
        self._detect_complete: bool = False
        # Worker plumbing + throttle bookkeeping.
        self._detect_in_flight: bool = False
        self._detect_first: bool = True   # reset detector caches on 1st run
        self._last_detect_spawn_ms: int = 0
        self._capture_excluded: bool = False
        # True while the one-time detector cache warm-up is running. Real
        # detections hold off until it clears, so the (expensive) first
        # template build happens exactly once instead of racing.
        self._prewarming: bool = False

        if self._detector_mode is not None:
            self._detect_signaler = _DetectSignaler()
            self._detect_signaler.result_ready.connect(self._on_detect_result)
            # Tick re-detects while dragging (even when the cursor holds
            # still) so the ghosts stay live and can settle to green.
            self._detect_timer = QTimer(self)
            self._detect_timer.timeout.connect(self._maybe_detect)
            self._detect_timer.start(DETECT_TICK_MS)
            # Kick off cache warm-up immediately so it overlaps with the
            # user lining up their first drag — by the time they release,
            # HUD detection is warm (~0 ms) instead of stalling ~1 s.
            self._start_prewarm()
        else:
            self._detect_signaler = None
            self._detect_timer = None

    # ──────────────────────────────────────────────────────────────
    # Capture exclusion
    # ──────────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Apply once the native window handle exists. Marking the overlay
        # WDA_EXCLUDEFROMCAPTURE keeps it visible to the user but hides it
        # from mss/BitBlt screen grabs, so the detectors only ever see the
        # clean game underneath — never the dim overlay or our ghost boxes.
        if self._detector_mode is not None and not self._capture_excluded:
            self._capture_excluded = self._apply_capture_exclusion()

    def _apply_capture_exclusion(self) -> bool:
        """Mark this window invisible to screen capture (Win10 2004+).

        Returns True on success. On older Windows the call fails
        harmlessly; detection still works because the ghost boxes are
        thin outlines, but a capture could include them — so we keep the
        success flag for diagnostics.
        """
        try:
            hwnd = int(self.winId())
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            ok = bool(
                ctypes.windll.user32.SetWindowDisplayAffinity(
                    hwnd, WDA_EXCLUDEFROMCAPTURE,
                )
            )
            if not ok:
                log.debug(
                    "SetWindowDisplayAffinity(EXCLUDEFROMCAPTURE) failed "
                    "(err=%s) — region ghosts may interfere with detection "
                    "on this OS",
                    ctypes.windll.kernel32.GetLastError(),
                )
            return ok
        except Exception as exc:
            log.debug("capture exclusion unavailable: %s", exc)
            return False

    # ──────────────────────────────────────────────────────────────
    # Painting
    # ──────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        painter = QPainter(self)

        # Semi-transparent dark overlay
        overlay = QColor(0, 0, 0, 140)
        painter.fillRect(self.rect(), overlay)

        if not self._rect.isNull():
            # Clear the selected region (punch a hole)
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(self._rect, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

            # Draw selection border
            accent = QColor(P.green)
            accent.setAlpha(220)
            painter.setPen(QPen(accent, 2))
            painter.setBrush(QBrush(QColor(51, 221, 136, 30)))
            painter.drawRect(self._rect)

            # Draw size label with native pixel dimensions
            if self._origin_native and self._current_native:
                nx = min(self._origin_native[0], self._current_native[0])
                ny = min(self._origin_native[1], self._current_native[1])
                nw = abs(self._current_native[0] - self._origin_native[0])
                nh = abs(self._current_native[1] - self._origin_native[1])
                label = f"{nw} x {nh}  @ ({nx}, {ny})"
            else:
                label = f"{self._rect.width()} x {self._rect.height()}"
            painter.setPen(QColor(P.fg_bright))
            painter.drawText(
                self._rect.x(), self._rect.y() - 6, label,
            )

            # Ghost finder boxes + detection status (detector modes only)
            if self._detector_mode is not None:
                self._paint_ghost_boxes(painter)

        # Instruction text
        painter.setPen(QColor(P.fg_bright))
        painter.drawText(
            self.rect().center().x() - 180,
            40,
            "Click and drag to select the scanner region. Press ESC to cancel.",
        )

        painter.end()

    def _paint_ghost_boxes(self, painter: QPainter) -> None:
        """Overlay the live detector boxes inside the selection rect.

        Boxes are region-relative pixels measured against
        ``self._detect_region`` (an absolute native region). We lift each
        box to absolute native coords, then map through the SAME
        native→widget transform the current selection uses, so the
        ghosts stay pinned to the on-screen panel even if the rectangle
        has drifted a little since the detection ran.
        """
        # Always surface a status line so the feature is discoverable
        # even before the first box appears.
        self._paint_status_line(painter)

        boxes = self._detect_boxes
        region = self._detect_region
        if not boxes or not region:
            return
        if not (self._origin_native and self._current_native):
            return

        nx0 = min(self._origin_native[0], self._current_native[0])
        ny0 = min(self._origin_native[1], self._current_native[1])
        nw = abs(self._current_native[0] - self._origin_native[0])
        nh = abs(self._current_native[1] - self._origin_native[1])
        if nw <= 0 or nh <= 0 or self._rect.isNull():
            return
        sx = self._rect.width() / float(nw)
        sy = self._rect.height() / float(nh)
        rx = self._rect.x()
        ry = self._rect.y()

        # Green once every expected region is found; amber "ghost" while
        # the finders are still searching / partially locked.
        if self._detect_complete:
            line = QColor(P.green)
            fill = QColor(P.green)
            fill.setAlpha(45)
        else:
            line = QColor(P.yellow)
            fill = QColor(P.yellow)
            fill.setAlpha(28)
        line.setAlpha(235)

        for box in boxes:
            try:
                abs_x = region["x"] + int(box["x"])
                abs_y = region["y"] + int(box["y"])
                bw = max(1, int(box["w"]))
                bh = max(1, int(box["h"]))
            except (KeyError, TypeError, ValueError):
                continue
            wx = rx + (abs_x - nx0) * sx
            wy = ry + (abs_y - ny0) * sy
            ww = bw * sx
            wh = bh * sy
            wrect = QRect(int(wx), int(wy), max(1, int(ww)), max(1, int(wh)))

            painter.setPen(QPen(line, 2))
            painter.setBrush(QBrush(fill))
            painter.drawRect(wrect)

            # Small caption above the box (kept inside the selection).
            cap = str(box.get("label", ""))
            if cap:
                painter.setPen(QColor(P.fg_bright))
                ty = wrect.y() - 4
                if ty < self._rect.y() + 10:
                    ty = wrect.y() + 12  # flip below the top edge
                painter.drawText(wrect.x() + 2, ty, cap)

    def _paint_status_line(self, painter: QPainter) -> None:
        """One-line detection status under the size label."""
        if self._detect_complete:
            text = "✓ All regions detected — release to confirm"
            color = QColor(P.green)
        elif self._detect_boxes:
            text = f"Detecting… {len(self._detect_boxes)} region(s) found"
            color = QColor(P.yellow)
        else:
            text = "Searching for HUD regions — frame the panel…"
            color = QColor(P.fg_dim)
        # Append the resolution the scale prior is using, so the user
        # can see (and trust) what the finders are tuned to.
        if self._game_resolution:
            gr = self._game_resolution
            text += (
                f"   ·   Game: {gr['w']}×{gr['h']} "
                f"({gr.get('source', '?')})"
            )
        painter.setPen(color)
        # Just below the size label (which sits at rect top − 6).
        painter.drawText(self._rect.x(), self._rect.y() - 20, text)

    # ──────────────────────────────────────────────────────────────
    # Detection worker
    # ──────────────────────────────────────────────────────────────

    def _start_prewarm(self) -> None:
        """Warm the detector caches on a daemon thread (see
        ``region_detector.prewarm``). Real detections are gated on
        ``self._prewarming`` so the costly first template build happens
        once rather than racing the first live tick."""
        self._prewarming = True

        def _bg() -> None:
            try:
                from .region_detector import prewarm
                prewarm(self._detector_mode)
            except Exception as exc:
                log.debug("region prewarm failed: %s", exc)
            finally:
                # Plain bool write from a daemon thread — atomic in
                # CPython; the GUI thread only ever reads it.
                self._prewarming = False

        try:
            threading.Thread(
                target=_bg, daemon=True, name="region_prewarm",
            ).start()
        except Exception as exc:
            log.debug("region prewarm spawn failed: %s", exc)
            self._prewarming = False

    def _maybe_detect(self) -> None:
        """Spawn a detection pass for the current selection, throttled."""
        if self._detector_mode is None or self._detect_signaler is None:
            return
        if self._prewarming:
            return  # hold off until the one-time cache build finishes
        if not self._dragging or self._detect_in_flight:
            return
        if not (self._origin_native and self._current_native):
            return
        ox, oy = self._origin_native
        ex, ey = self._current_native
        x = min(ox, ex)
        y = min(oy, ey)
        w = abs(ex - ox)
        h = abs(ey - oy)
        if w < 8 or h < 8:
            return
        now_ms = int(time.monotonic() * 1000)
        if (
            self._last_detect_spawn_ms
            and now_ms - self._last_detect_spawn_ms < DETECT_THROTTLE_MS
        ):
            return
        self._last_detect_spawn_ms = now_ms

        region = {"x": x, "y": y, "w": w, "h": h}
        reset = self._detect_first
        self._detect_first = False
        self._detect_in_flight = True
        try:
            threading.Thread(
                target=self._detect_worker,
                args=(region, reset),
                daemon=True,
                name="region_detect",
            ).start()
        except Exception as exc:
            log.debug("region detect thread spawn failed: %s", exc)
            self._detect_in_flight = False

    def _detect_worker(self, region: dict, reset: bool) -> None:
        """Worker-thread body: capture + detect, then signal the GUI."""
        result = None
        try:
            from .region_detector import detect
            result = detect(
                region, self._detector_mode, reset=reset,
                game_resolution=self._game_resolution,
            )
        except Exception as exc:
            log.debug("region detection failed: %s", exc)
            result = None
        try:
            self._detect_signaler.result_ready.emit(
                {"region": region, "result": result}
            )
        except Exception:
            pass

    def _on_detect_result(self, payload: object) -> None:
        """GUI-thread slot: store the latest boxes and repaint."""
        self._detect_in_flight = False
        if not isinstance(payload, dict):
            return
        result = payload.get("result")
        region = payload.get("region")
        # Keep the previous ghosts on a transient miss (capture failed /
        # import error) rather than flickering them away.
        if not isinstance(result, dict):
            return
        if result.get("mode") != self._detector_mode:
            return
        self._detect_boxes = result.get("boxes") or []
        self._detect_complete = bool(result.get("complete"))
        self._detect_region = region
        self.update()

    # ──────────────────────────────────────────────────────────────
    # Mouse / keyboard
    # ──────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._origin_native = _get_cursor_pos()
            self._rect = QRect(
                event.position().toPoint(),
                event.position().toPoint(),
            )
            self._dragging = True
            # Fresh drag → clear stale ghosts and reset detector caches.
            self._detect_boxes = []
            self._detect_region = None
            self._detect_complete = False
            self._detect_first = True
            self._last_detect_spawn_ms = 0
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._current_native = _get_cursor_pos()
            origin = self._rect.topLeft()
            # Update visual rect for painting
            self._rect = QRect(
                origin, event.position().toPoint(),
            ).normalized()
            self._maybe_detect()
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            end_native = _get_cursor_pos()

            # Track sub-floor drops so we can surface a warning AFTER
            # closing the overlay below.  Previously these were silently
            # dropped with no user feedback — the selector just closed,
            # the user assumed their region was saved, and nothing ever
            # reached the handler.  See the comment at the size gate
            # below for why this matters specifically for the signature
            # scanner UI.
            too_small: tuple[int, int] | None = None

            if self._origin_native:
                ox, oy = self._origin_native
                ex, ey = end_native
                x = min(ox, ex)
                y = min(oy, ey)
                w = abs(ex - ox)
                h = abs(ey - oy)

                # Minimum 4x4 native pixels — lowered from 10x10.  The
                # old floor used to silently drop tight drags around
                # small UI elements (notably the signature scanner
                # value digits at 1080p / 100% scaling, where 10
                # native pixels = 10 logical pixels and users were
                # easily drawing a "tight" box that fell below the
                # threshold).  Anything smaller than 4x4 is almost
                # certainly an accidental click, not a real drag.
                if w > 4 and h > 4:
                    self.region_selected.emit({
                        "x": x, "y": y, "w": w, "h": h,
                    })
                else:
                    too_small = (w, h)
            self.close()
            event.accept()
            # Surface sub-floor drops AFTER closing the overlay so the
            # user sees the warning instead of the selector silently
            # disappearing.  Defensive try/except: if the message box
            # itself fails for any reason (rare), at least don't crash
            # the selector — the bumped save-failure logging in
            # ``_save_config`` will still surface the rejection.
            if too_small is not None:
                try:
                    QMessageBox.warning(
                        None,
                        "Region too small",
                        f"The region you drew was "
                        f"{too_small[0]}×{too_small[1]} pixels — too "
                        "small to capture reliably.\n\nPlease draw a "
                        "larger box around the area you want to scan.",
                    )
                except Exception:
                    pass

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()
            event.accept()

    def closeEvent(self, event) -> None:
        # Stop the detection tick so a closed selector doesn't keep
        # spawning capture workers in the background.
        try:
            if self._detect_timer is not None:
                self._detect_timer.stop()
        except Exception:
            pass
        super().closeEvent(event)
