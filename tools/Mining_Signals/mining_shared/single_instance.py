"""Cross-process single-instance enforcement for SC Toolbox tools.

The user has three calibration-adjacent tools that must NEVER run in
parallel:

  * Mining HUD OCR Calibration dialog  (slot ``calibration_dialog``)
  * SC-OCR Panel Finder                (slot ``panel_finder``)
  * Signature Finder                   (slot ``signature_finder``)

Each slot is implemented as a localhost TCP port reservation. The
first process to bind the port owns the slot and runs a tiny
``QTcpServer`` that accepts incoming connections; any byte received
counts as a "raise yourself" request. Subsequent processes that try
to bind the same port get an OS-level address-in-use error, take that
as proof someone else owns the slot, send a single byte to the
holder so it pops to the front, and exit cleanly.

Why TCP-port-bind and not a lockfile or QSharedMemory:
  * The port is auto-released when the holding process dies (even on
    crash) — no stale-lock cleanup needed.
  * The same primitive serves BOTH "is anyone home?" detection AND
    "raise yourself" signalling in one round-trip.
  * Works identically across Python interpreters launched from
    different .bat files (the popout in calibration_dialog.py and
    the standalone script in scripts/ both share the slot).

Loopback-only binding means no Windows firewall popup. Ports live in
the IANA dynamic range (49152-65535) so they don't clash with
well-known services.

Usage from a standalone script:

    app = QApplication(sys.argv)
    win = MyWindow()
    guard = SingleInstance("signature_finder", win)
    if not guard.acquire():
        # Someone else owns the slot. acquire() already poked them
        # to raise their window. Just exit.
        sys.exit(0)
    win.show()
    sys.exit(app.exec())

Usage from a popout opener (the calibration dialog opening the panel
finder window inside the same Qt process):

    guard = SingleInstance("panel_finder", popout_window)
    if not guard.acquire():
        # A standalone instance is already running. Surface a status
        # message and don't open a duplicate.
        self._status_bar.showMessage("Panel Finder is already open", 4000)
        return
    popout_window._single_instance = guard   # keep ref alive
    popout_window.show()
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

# Per-tool port allocations. Hard-coded so subprocesses can resolve
# the same number without sharing config. Picked from the IANA
# dynamic/private port range (49152-65535) above the ephemeral
# allocations Windows tends to hand out, to minimise collisions.
SLOT_PORTS: dict[str, int] = {
    "calibration_dialog": 49813,
    "panel_finder":       49814,
    "signature_finder":   49815,
}


class SingleInstance(QObject):
    """Wraps a Qt window with cross-process single-instance semantics.

    Lifecycle:
      1. Construct with ``slot`` (one of ``SLOT_PORTS``) + the window
         instance to raise on incoming requests.
      2. Call :meth:`acquire`. Returns True iff this process now
         holds the slot.
      3. While held, any external poke (TCP connect to the slot port)
         triggers ``window.show / raise_ / activateWindow`` on the Qt
         main thread.
      4. The slot is released automatically when the QObject is
         destroyed (server gets ``deleteLater``-ed via parent chain),
         OR explicitly via :meth:`release`. The OS also reclaims the
         port at process exit, so crashes don't leak the lock.
    """

    def __init__(self, slot: str, window: QWidget):
        # Parent the QObject to the window so destroying the window
        # cleans up the QTcpServer too.
        super().__init__(window)
        if slot not in SLOT_PORTS:
            raise KeyError(
                f"Unknown single-instance slot: {slot!r}. "
                f"Known slots: {sorted(SLOT_PORTS)}"
            )
        self._slot = slot
        self._port = SLOT_PORTS[slot]
        self._window = window
        self._server: Optional[QTcpServer] = None

    # ───────────────────────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────────────────────

    def acquire(self) -> bool:
        """Try to claim the slot.

        Returns:
            True  → we now hold the slot. Window may proceed.
            False → another process holds it. A "raise yourself"
                    poke has already been sent to that process; the
                    caller should NOT show its own window and should
                    typically exit (standalone) or surface a status
                    message (popout).
        """
        srv = QTcpServer(self)
        if srv.listen(QHostAddress.LocalHost, self._port):
            self._server = srv
            srv.newConnection.connect(self._on_incoming_raise)
            log.info(
                "single_instance: acquired slot=%r on port %d",
                self._slot, self._port,
            )
            return True
        # Bind failed → someone else holds the slot. Drop the unused
        # server and ping the holder.
        srv.deleteLater()
        log.info(
            "single_instance: slot=%r port=%d already held — "
            "sending raise request to holder",
            self._slot, self._port,
        )
        self._send_raise_request()
        return False

    def release(self) -> None:
        """Drop the slot explicitly. Idempotent."""
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            try:
                self._server.deleteLater()
            except Exception:
                pass
            self._server = None
            log.info(
                "single_instance: released slot=%r on port %d",
                self._slot, self._port,
            )

    @property
    def held(self) -> bool:
        """Whether this object currently owns its slot."""
        return self._server is not None

    # ───────────────────────────────────────────────────────────
    # Internal: handle incoming raise pokes
    # ───────────────────────────────────────────────────────────

    def _on_incoming_raise(self) -> None:
        if self._server is None:
            return
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        # Don't bother reading the body — any connection counts as a
        # raise request. Schedule disconnect on the next event loop
        # tick so the client's readyRead has a chance to flush.
        try:
            sock.disconnected.connect(sock.deleteLater)
            sock.disconnectFromHost()
        except Exception:
            try:
                sock.deleteLater()
            except Exception:
                pass
        # Defer the raise to the next event loop tick so we're
        # definitely back on the main thread.
        QTimer.singleShot(0, self._raise_window)

    def _raise_window(self) -> None:
        if self._window is None:
            return
        try:
            # Restore from minimised, then raise + activate.
            state = self._window.windowState()
            self._window.setWindowState(
                (state & ~Qt.WindowMinimized) | Qt.WindowActive
            )
            self._window.show()
            self._window.raise_()
            self._window.activateWindow()
        except Exception as exc:
            log.warning(
                "single_instance: raise failed for slot=%r: %s",
                self._slot, exc,
            )

    # ───────────────────────────────────────────────────────────
    # Internal: send a raise poke to the existing holder
    # ───────────────────────────────────────────────────────────

    def _send_raise_request(self) -> None:
        """Best-effort: connect to the holder and write one byte. The
        holder's QTcpServer fires newConnection, which raises its
        window. We don't wait for or expect a reply."""
        sock = QTcpSocket()
        try:
            sock.connectToHost(QHostAddress.LocalHost, self._port)
            if not sock.waitForConnected(500):
                return
            try:
                sock.write(b"raise\n")
                sock.flush()
                sock.waitForBytesWritten(200)
            except Exception:
                pass
            try:
                sock.disconnectFromHost()
            except Exception:
                pass
        finally:
            sock.deleteLater()


# ─────────────────────────────────────────────────────────────
# Standalone helpers — useful from non-Qt callers (e.g. quick
# pre-flight checks or external CLIs).
# ─────────────────────────────────────────────────────────────

def request_raise(slot: str) -> bool:
    """Poke whoever currently holds the slot to raise their window.

    Returns True if the poke was delivered (no guarantee the holder
    actually raised — it's best-effort). Returns False if the slot
    is unknown OR no holder is reachable.
    """
    if slot not in SLOT_PORTS:
        return False
    port = SLOT_PORTS[slot]
    sock = QTcpSocket()
    try:
        sock.connectToHost(QHostAddress.LocalHost, port)
        if not sock.waitForConnected(500):
            return False
        try:
            sock.write(b"raise\n")
            sock.flush()
            sock.waitForBytesWritten(200)
        finally:
            try:
                sock.disconnectFromHost()
            except Exception:
                pass
        return True
    finally:
        sock.deleteLater()


def is_slot_held(slot: str) -> bool:
    """Non-destructive probe: True if the slot is currently held.

    Implementation: try to bind+immediately-release. If the bind
    succeeds, the slot is free (so we briefly held it ourselves and
    let go). If the bind fails, someone else holds it.

    Note: there's a microscopic race window between releasing here
    and a subsequent acquire — fine for grey-out logic, NOT a
    substitute for SingleInstance.acquire() before showing a window.
    """
    if slot not in SLOT_PORTS:
        return False
    port = SLOT_PORTS[slot]
    probe = QTcpServer()
    try:
        if probe.listen(QHostAddress.LocalHost, port):
            probe.close()
            return False
        return True
    finally:
        probe.deleteLater()
