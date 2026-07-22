"""BlockerWindow — Mouse Blocker main window.

Sits ABOVE Star Citizen but BELOW every other SC Toolbox window.

The window is created topmost (so it covers SC), but a 250 ms Win32
z-order timer continuously pushes it just below the lowest *other*
topmost window so the launcher and the rest of the toolbox stay on
top and remain clickable.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QVBoxLayout, QApplication

from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.i18n import s_ as _

from ui.blocker_overlay import BlockerHoloSurface, BlockerBody

log = logging.getLogger(__name__)


ACCENT = "#ff3355"

# ── Win32 z-order helpers ────────────────────────────────────────────────
_IS_WINDOWS = sys.platform.startswith("win")

if _IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _GWL_EXSTYLE = -20
    _WS_EX_TOPMOST = 0x00000008
    _SWP_NOMOVE = 0x0002
    _SWP_NOSIZE = 0x0001
    _SWP_NOACTIVATE = 0x0010
    _SWP_NOSENDCHANGING = 0x0400
    # HWND_TOPMOST is the magic -1 handle that means "place this window
    # above all non-topmost windows".  Cast to c_void_p so the call site
    # matches the wintypes.HWND argtype (raw -1 would otherwise be
    # interpreted as a 32-bit signed int by ctypes on 64-bit Windows).
    _HWND_TOPMOST = ctypes.c_void_p(-1)

    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL

    # Diagnostic — used to identify *which* window is sitting on top of
    # us when z-order shifts.  Only called when logging transitions,
    # never on the steady-state hot path.
    _user32.GetWindowTextW.argtypes = [
        wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int,
    ]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [
        wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int,
    ]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _user32.GetWindowRect.restype = wintypes.BOOL

    _ENUM_PROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    _user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL


def _hwnd_title(hwnd: int) -> str:
    """Return the window title for *hwnd*, for diagnostic logging.

    Silently returns an empty string on any failure so logging can
    never break z-order enforcement.
    """
    if not hwnd or not _IS_WINDOWS:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(128)
        _user32.GetWindowTextW(hwnd, buf, 128)
        return buf.value or "<no-title>"
    except (OSError, ValueError):
        return ""


def _hwnd_class(hwnd: int) -> str:
    """Return the window class name for *hwnd*, for diagnostic logging."""
    if not hwnd or not _IS_WINDOWS:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(hwnd, buf, 128)
        return buf.value or ""
    except (OSError, ValueError):
        return ""


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _hwnd_size(hwnd: int) -> tuple[int, int]:
    """Return (width, height) of *hwnd*'s bounding rect, or (0, 0)."""
    if not hwnd or not _IS_WINDOWS:
        return (0, 0)
    try:
        r = _RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return (r.right - r.left, r.bottom - r.top)
    except (OSError, ValueError):
        pass
    return (0, 0)


class BlockerWindow(SCWindow):
    """Frameless overlay that blocks clicks to Star Citizen but lets
    every other SC Toolbox tool (and the launcher) sit above it.
    """

    Z_ORDER_INTERVAL_MS = 250

    def __init__(self, opacity: float = 0.95, hotkey_text: str = "Shift+0"):
        super().__init__(
            title=_("Mouse Blocker"),
            width=1440, height=860,
            min_w=320, min_h=140,
            opacity=opacity,
            accent=ACCENT,
        )

        # Pre-warm mode: when the launcher pre-spawns this skill, the
        # entry point does an off-screen show()+hide() to force DWM
        # to allocate the layered-window backing store now (vs. paying
        # ~900ms on the first user show).  During that pre-warm pass
        # we MUST skip the deferred fullscreen / animation / z-order
        # setup — otherwise we'd cover the screen and start animating
        # while invisible.  hideEvent flips the flag off so the next
        # (real) show runs the full setup.
        import os as _os
        self._prewarming = bool(_os.environ.get("SC_TOOLBOX_PRELOAD"))

        # Don't steal focus on show, and don't auto-raise on click.
        # WS_EX_NOACTIVATE (set by WindowDoesNotAcceptFocus) prevents
        # Windows from promoting us within the topmost tier when the
        # user clicks the title bar / body.
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowFlags(self.windowFlags() | Qt.WindowDoesNotAcceptFocus)

        # Replace SCWindow's default _HoloSurface with the transparent
        # blocker variant.  setCentralWidget reparents the old one.
        self._central = BlockerHoloSurface(self, accent=ACCENT)
        self.setCentralWidget(self._central)
        self._layout = QVBoxLayout(self._central)
        self._layout.setContentsMargins(1, 1, 1, 1)
        self._layout.setSpacing(0)

        # Title bar
        self._title_bar = SCTitleBar(
            window=self,
            title=_("Mouse Blocker"),
            icon_text="\U0001f6ab",  # 🚫
            accent_color=ACCENT,
            hotkey_text=hotkey_text,
        )
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        self._title_bar.close_clicked.connect(self.user_close)
        self._layout.addWidget(self._title_bar, 0)

        # Body — pulsing label + click absorber, fills the remaining space.
        # Animation start is deferred to showEvent so it doesn't compete
        # with first-paint rendering during launch.
        self._body = BlockerBody(accent_color=ACCENT, parent=self._central)
        self._layout.addWidget(self._body, 1)

        # Geometry: open at the launcher's CLI-provided size first
        # (smaller surface buffer = much faster first frame on Windows
        # DWM), then expand to cover the whole screen on the next event-
        # loop tick after show.  See showEvent.

        # Z-order enforcer — periodically pushes the blocker just below
        # the lowest *other* topmost window so SC Toolbox tools stay on
        # top.  Started/stopped by show/hide events.
        self._zorder_timer = QTimer(self)
        self._zorder_timer.setInterval(self.Z_ORDER_INTERVAL_MS)
        self._zorder_timer.timeout.connect(self._push_to_bottom_of_topmost)

        # Cached ctypes callback — kept on the instance so it isn't
        # garbage-collected mid-call.
        if _IS_WINDOWS:
            self._enum_state = {
                "my_hwnd": 0, "lowest_other": 0,
                "screen_w": 0, "screen_h": 0,
            }
            self._enum_callback = _ENUM_PROC(self._enum_proc_cb)

    # ── Geometry ──
    def _cover_primary_screen(self) -> None:
        """Resize / move so the window covers the entire primary screen."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            log.warning("cover_primary_screen: no primary screen!")
            return
        geom = screen.geometry()
        self.setGeometry(geom)
        # Force a synchronous paint of the layered backing store.  On
        # some Windows builds, after a hide()/show() cycle (e.g. our
        # pre-warm), Qt doesn't re-trigger a paint event for the new
        # geometry, leaving the layered window's compositor surface
        # stale — Qt + Win32 both report the window as visible at the
        # right geometry, but the user sees nothing.  Forcing a
        # repaint here makes the surface match the widget tree.
        self.repaint()
        if hasattr(self, "_central") and self._central is not None:
            self._central.repaint()
        if hasattr(self, "_body") and self._body is not None:
            self._body.repaint()
        # Log the actual resulting geometry after Qt processes the
        # request — DWM / window-manager constraints can override what
        # we asked for, and on multi-monitor setups the blocker may
        # have ended up on the wrong screen entirely (which is exactly
        # what "I don't see the HUD" looks like).
        actual = self.geometry()
        all_screens = [
            (s.name(), s.geometry().x(), s.geometry().y(),
             s.geometry().width(), s.geometry().height())
            for s in QGuiApplication.screens()
        ]
        log.info(
            "cover_primary_screen: requested=(%d,%d %dx%d) actual=(%d,%d %dx%d) "
            "opacity=%.2f primary=%r screens=%r",
            geom.x(), geom.y(), geom.width(), geom.height(),
            actual.x(), actual.y(), actual.width(), actual.height(),
            self.windowOpacity(),
            screen.name(), all_screens,
        )
        t0 = getattr(self, "_show_t0", None)
        if t0 is not None:
            log.info("show: covered primary screen after %.0f ms", (time.monotonic() - t0) * 1000)
            self._show_t0 = None  # one-shot — only log the first show

    def reset_layout(self) -> None:
        """Title-bar 'reset layout' button → re-cover the screen."""
        self._cover_primary_screen()

    # ── Show / hide hooks ──
    def showEvent(self, event):
        super().showEvent(event)
        if self._prewarming:
            # Pre-warm: skip every deferred setup so we don't cover the
            # screen / start the pulse / activate the z-order timer
            # while the user can't see us.  hideEvent will clear the
            # flag once the pre-warm pass completes.
            return
        # Log how long it took from IPC arrival to first showEvent.
        # Helps diagnose when the launcher sends "show" but Windows
        # holds up the actual paint (working-set page-faults under
        # memory pressure from other tools).
        t0 = getattr(self, "_show_t0", None)
        if t0 is not None:
            log.info("show: showEvent fired after %.0f ms", (time.monotonic() - t0) * 1000)
        # Defer expensive setup off the show path so the first frame
        # paints fast — the user sees the window immediately, then the
        # remaining setup happens on the next event-loop tick.
        QTimer.singleShot(0, self._cover_primary_screen)
        QTimer.singleShot(0, self._body.start)
        if _IS_WINDOWS:
            QTimer.singleShot(0, self._push_to_bottom_of_topmost)
            self._zorder_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._prewarming:
            # Pre-warm cycle (off-screen show + hide) is now complete.
            # Subsequent shows are real user activations.
            self._prewarming = False
            log.info("hideEvent: pre-warm complete")
            return
        # Log the actual hide path so we can distinguish:
        # - explicit IPC hide  (paired with "hide: IPC received" above)
        # - close button       (paired with "user_close" path)
        # - implicit hide      (Qt or OS-driven — the suspicious one)
        log.info("hideEvent: window hidden (visible=%s)", self.isVisible())
        if hasattr(self, "_zorder_timer"):
            self._zorder_timer.stop()

    # ── Z-order enforcement ──
    def _enum_proc_cb(self, hwnd, _lparam):
        state = self._enum_state
        if hwnd == state["my_hwnd"]:
            return True
        if not _user32.IsWindowVisible(hwnd):
            return True
        ex_style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if not (ex_style & _WS_EX_TOPMOST):
            return True
        # Filter out windows that are bad z-order anchors:
        #   - Zero-sized windows (Windows internals like
        #     XamlExplorerHostIslandWindow_WASDK, which back the Start
        #     menu / Action Center / search box).  Slipping beneath
        #     these can leave the blocker in a DWM composition state
        #     that doesn't render at all.
        #   - Windows the size of the primary screen or larger.  Star
        #     Citizen in borderless-fullscreen sets WS_EX_TOPMOST on a
        #     screen-sized window; treating it as a target would put
        #     the blocker *beneath* SC, hiding the HUD entirely.
        # The launcher and other toolbox tools fall comfortably between
        # these two extremes, so they remain valid anchors.
        w, h = _hwnd_size(hwnd)
        if w == 0 or h == 0:
            return True
        screen_w = state.get("screen_w", 0)
        screen_h = state.get("screen_h", 0)
        if screen_w and screen_h and w >= screen_w and h >= screen_h:
            return True
        # EnumWindows enumerates top-to-bottom in z-order, so the
        # *last* eligible topmost we see is the lowest.
        state["lowest_other"] = hwnd
        return True  # keep enumerating

    def _push_to_bottom_of_topmost(self) -> None:
        """Place this window just below the lowest other visible
        topmost window so SC Toolbox tools / launcher stay above us.
        Silently no-ops on non-Windows platforms or on any error.
        """
        if not _IS_WINDOWS or not self.isVisible():
            return
        try:
            my_hwnd = int(self.winId())
            if not my_hwnd:
                return

            # Probe current topmost status — re-asserting HWND_TOPMOST
            # unconditionally on some Windows builds repositions the
            # window to the *top* of the topmost group, which fights
            # with the push-below logic on the next line and can cause
            # a visible flicker as the blocker bounces top→bottom every
            # 250 ms.  Only re-assert when we've actually been demoted.
            my_ex_style = _user32.GetWindowLongW(my_hwnd, _GWL_EXSTYLE)
            was_topmost = bool(my_ex_style & _WS_EX_TOPMOST)
            reasserted = False
            if not was_topmost:
                # Star Citizen's in-game scanner can briefly promote its
                # own window to topmost; if that happened while we were
                # at the bottom of the topmost group, our SetWindowPos
                # below SC may have demoted us out of topmost entirely.
                # Re-promote so SC can't sit visually above us.
                _user32.SetWindowPos(
                    my_hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE
                    | _SWP_NOACTIVATE | _SWP_NOSENDCHANGING,
                )
                reasserted = True

            # Enumerate every other visible topmost window; the *last*
            # eligible one we see is the lowest in z-order, and that's
            # the window we want to slip just beneath.  We pass the
            # primary screen dimensions through the enum state so the
            # callback can skip game-sized topmost windows (SC in
            # borderless fullscreen sets WS_EX_TOPMOST on a window the
            # size of the whole screen).
            self._enum_state["my_hwnd"] = my_hwnd
            self._enum_state["lowest_other"] = 0
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                sg = screen.geometry()
                self._enum_state["screen_w"] = sg.width()
                self._enum_state["screen_h"] = sg.height()
            else:
                self._enum_state["screen_w"] = 0
                self._enum_state["screen_h"] = 0
            _user32.EnumWindows(self._enum_callback, 0)
            target = self._enum_state["lowest_other"]
            if target:
                _user32.SetWindowPos(
                    my_hwnd, target, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE
                    | _SWP_NOACTIVATE | _SWP_NOSENDCHANGING,
                )

            # Log only on state transitions so we can diagnose flicker
            # without spamming 4 lines per second.  We track:
            # - whether we just had to re-promote ourselves to topmost
            # - the identity of the window we slipped under
            # Both are stable in the steady state; transitions point at
            # who's fighting us for z-order.
            prev = getattr(self, "_zorder_state", None)
            new_state = (was_topmost, target)
            if new_state != prev:
                tw, th = _hwnd_size(target) if target else (0, 0)
                log.info(
                    "z-order: was_topmost=%s reasserted=%s target=0x%x "
                    "title=%r class=%r size=%dx%d visible=%s",
                    was_topmost, reasserted, target or 0,
                    _hwnd_title(target) if target else "",
                    _hwnd_class(target) if target else "",
                    tw, th, self.isVisible(),
                )
                self._zorder_state = new_state
        except OSError as exc:
            log.warning("z-order: SetWindowPos failed: %s", exc)

    # ── IPC ──
    def handle_ipc_command(self, cmd: dict) -> None:
        """Dispatch commands from the launcher.

        Note: never call raise_() / activateWindow() — that would push
        the blocker above other SC Toolbox tools, defeating the
        bottom-of-topmost layering.
        """
        cmd_type = cmd.get("type", "")
        if cmd_type == "show":
            # Mark when the show command arrived so we can measure how
            # long the actual paint takes (logged from showEvent).
            self._show_t0 = time.monotonic()
            log.info("show: IPC received")
            # If we're still in pre-warm (off-screen show + 500 ms
            # delayed hide), the user's IPC 'show' would otherwise
            # land in the showEvent guard and silently return early —
            # window stays off-screen, user sees nothing, and they
            # press the hotkey two more times before it works.
            #
            # Abort the pre-warm here: clear the flag, hide the
            # off-screen window to reset its show-state, then call
            # show() so showEvent runs the full deferred setup
            # (cover_primary_screen, animation, z-order timer).
            if self._prewarming:
                self._prewarming = False
                if self.isVisible():
                    self.hide()
            self.show()
        elif cmd_type == "hide":
            log.info("hide: IPC received (was visible=%s)", self.isVisible())
            self.hide()
        elif cmd_type == "toggle":
            log.info("toggle: IPC received (was visible=%s)", self.isVisible())
            if self.isVisible():
                self.hide()
            else:
                self.show()
        elif cmd_type == "set_opacity":
            log.info("set_opacity: IPC received value=%r", cmd.get("value"))
            try:
                self.set_opacity(float(cmd.get("value", 0.95)))
            except (TypeError, ValueError):
                pass
        elif cmd_type == "quit":
            log.info("quit: IPC received")
            QApplication.quit()
        else:
            # Surface unexpected command types so we can spot a launcher
            # vs. mouse-blocker schema mismatch (or some other tool
            # accidentally writing into our IPC file).
            log.warning("unknown IPC command: %r", cmd)
