"""
Battle Buddy — HUD subprocess entry point.

Usage (launched by skill_launcher.py):
    python hud_app.py <x> <y> <w> <h> <opacity> <cmd_file>
"""
from __future__ import annotations

import logging
import os
import sys

# Ensure local packages are importable
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.ipc import ipc_read_and_clear
from core.log_monitor import LogMonitor
from core.inventory_parser import InventoryParser
from core.inventory_tracker import InventoryTracker
from ui.hud_window import HudWindow
from ui.tutorial_popup import TutorialPopup
from ui.options_popup import OptionsPopup, load_settings, save_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("battle_buddy")


def _parse_args() -> dict:
    """Parse CLI args from either launch path.

    skill_launcher sends positional:  x y w h opacity cmd_file
    main.py (WingmanAI) sends named:  --x 60 --y 880 --opacity 0.92 ...

    We detect which form by checking if the first arg starts with '--'.
    """
    argv = sys.argv[1:]

    if argv and argv[0].startswith("--"):
        # Named flags from main.py
        import argparse
        p = argparse.ArgumentParser(description="Battle Buddy HUD subprocess")
        p.add_argument("--x", type=int, default=60)
        p.add_argument("--y", type=int, default=880)
        p.add_argument("--opacity", type=float, default=0.92)
        p.add_argument("--log-path", default="")
        p.add_argument("--orientation", default="horizontal",
                        choices=["horizontal", "vertical"])
        p.add_argument("--cmd-file", default="")
        ns = p.parse_args(argv)
        return {
            "x": ns.x, "y": ns.y, "opacity": ns.opacity,
            "log_path": ns.log_path, "orientation": ns.orientation,
            "cmd_file": ns.cmd_file,
        }

    # Positional args from skill_launcher
    _ROOT = os.path.dirname(os.path.dirname(_HERE))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from shared.data_utils import parse_cli_args

    cli = parse_cli_args(argv)
    return {
        "x": cli["x"], "y": cli["y"], "opacity": cli["opacity"],
        "log_path": "", "orientation": "horizontal",
        "cmd_file": cli["cmd_file"] or "",
        "ipc_read": _read_launcher_commands,
    }


def _read_launcher_commands(cmd_file: str) -> list:
    """Read the command file the way the launcher writes it (shared/ipc.py).

    The two writers lock differently.  main.py (WingmanAI) uses core/ipc.py: the
    lock is a file that exists only while it is held.  The launcher uses
    shared/ipc.py: the lock file stays, and a byte of it is locked.  Reading the
    launcher's file with core/ipc.py therefore found "the lock" held after every
    command, waited its 2 s on the GUI thread, and only got through once the file
    was 10 s old and it deleted it as stale: show, hide and quit from the launcher
    each took 10 s (measured 2026-10-06: 10.02, 10.03, 10.06, 10.01, 10.03 s), with
    the HUD frozen meanwhile."""
    if not os.path.exists(cmd_file):
        return []
    from shared.ipc import ipc_read_and_clear as read
    return read(cmd_file)


def _place_on_screen(win, has_saved_position: bool) -> None:
    """First launch: centre on the primary screen, so the HUD is never lost on a
    monitor smaller than the one the defaults assumed (y=880 is off a 768-high
    screen).  Later launches keep the saved spot only while at least half the
    bar sits on a connected monitor; an unplugged second screen or a lower
    resolution snaps it back to the centre."""
    from PySide6.QtGui import QGuiApplication

    win.adjustSize()
    rect = win.frameGeometry()
    area = max(1, rect.width() * rect.height())
    if has_saved_position:
        for screen in QGuiApplication.screens():
            seen = screen.availableGeometry().intersected(rect)
            if seen.width() * seen.height() * 2 >= area:
                return
        logger.info("Saved HUD position %s is off every screen; centring", rect.topLeft())
    _centre_on_primary(win)


def _centre_on_primary(win) -> None:
    """Centre *win* on the primary screen's available area.  The primary one, not
    the one the window is on: a window lost on an unplugged monitor is the case
    this is for."""
    from PySide6.QtGui import QGuiApplication

    primary = QGuiApplication.primaryScreen()
    if primary is None:
        return
    g = primary.availableGeometry()
    rect = win.frameGeometry()
    win.move(g.center().x() - rect.width() // 2, g.center().y() - rect.height() // 2)


class BattleBuddyApp:
    def __init__(self, cfg: dict) -> None:
        self._cfg      = cfg
        self._cmd_file = cfg["cmd_file"]
        # The reader that matches whoever writes the file: see _read_launcher_commands
        self._ipc_read = cfg.get("ipc_read") or ipc_read_and_clear

        # Merge saved settings on top of launch-arg defaults
        saved = load_settings()
        log_path    = saved.get("log_path",    cfg["log_path"])
        orientation = saved.get("orientation", cfg["orientation"])

        # Restore saved window position and opacity (overrides launcher args)
        has_saved_position = "window_x" in saved and "window_y" in saved
        x = int(saved.get("window_x", cfg["x"]))
        y = int(saved.get("window_y", cfg["y"]))
        opacity = float(saved.get("opacity", cfg["opacity"]))

        # Core pipeline
        self._monitor = LogMonitor(log_path)
        self._parser  = InventoryParser()
        self._tracker = InventoryTracker()

        self._monitor.subscribe(self._parser.on_line)
        self._parser.subscribe(self._tracker.on_event)
        self._parser.subscribe(self._on_parser_event)   # for auto-show
        self._tracker.on_changed(self._on_state_changed)

        # HUD window
        self._hud = HudWindow(
            orientation = orientation,
            opacity     = opacity,
            x           = x,
            y           = y,
            on_options  = self._show_options,
            on_tutorial = self._show_tutorial,
        )
        _place_on_screen(self._hud, has_saved_position)

        self._log_path   = log_path
        self._orientation = orientation

    def start(self) -> None:
        # Backfill: if a session is already active, replay its events
        # so the HUD shows the current loadout immediately.
        self._backfill_active_session()

        self._monitor.start()

        # IPC poll timer (50 ms)
        self._ipc_timer = QTimer()
        self._ipc_timer.setInterval(50)
        self._ipc_timer.timeout.connect(self._poll_ipc)
        if self._cmd_file:
            self._ipc_timer.start()

        # Show the HUD immediately so it's visible on launch
        self._hud.show()
        self._hud.raise_()

        logger.info("Battle Buddy HUD ready. Log: %s", self._log_path)

    # ── Session backfill ─────────────────────────────────────────────────────

    def _backfill_active_session(self) -> None:
        """Scan the existing Game.log for an active session and replay its
        loadout events so the HUD is populated on startup.

        Reads backwards from end-of-file to find the last ``{Join PU}``.
        If no ``SystemQuit`` / disconnect appears after it, the session is
        still active and all relevant lines from that point forward are fed
        through the parser → tracker pipeline.
        """
        if not os.path.isfile(self._log_path):
            logger.info("Backfill: log file not found, skipping")
            return

        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(0, 2)  # seek to end
                file_size = fh.tell()
                if file_size == 0:
                    return

                # Read backwards in chunks to find the last {Join PU}
                join_pos = self._find_last_join(fh, file_size)
                if join_pos is None:
                    logger.info("Backfill: no {Join PU} found in log")
                    return

                # Read forward from the join position to check for session end
                # and collect all lines for replay
                fh.seek(join_pos)
                session_lines: list[str] = []
                session_ended = False

                for line in fh:
                    stripped = line.rstrip("\n")
                    if not stripped:
                        continue
                    if "SystemQuit" in stripped or "Disconnecting from Stanton" in stripped or "Disconnecting from Pyro" in stripped:
                        # Check if another {Join PU} comes after this quit
                        # (player reconnected in the same log file)
                        session_ended = True
                        session_lines.clear()
                        continue
                    if "{Join PU}" in stripped:
                        # New session started — reset and collect from here
                        session_ended = False
                        session_lines.clear()
                    session_lines.append(stripped)

                if session_ended:
                    logger.info("Backfill: no active session (last session ended)")
                    return

                # Replay the collected lines through the parser
                count = 0
                for line in session_lines:
                    self._parser.on_line(line)
                    count += 1

                # Flush any pending batch so all events are processed
                self._tracker.flush()

                logger.info("Backfill: replayed %d lines from active session", count)

        except OSError as exc:
            logger.warning("Backfill: error reading log: %s", exc)

    def _find_last_join(self, fh, file_size: int) -> int | None:
        """Binary-ish search backwards through *fh* to find the byte offset
        of the last line containing ``{Join PU}``.  Returns None if not found.
        """
        CHUNK = 256 * 1024  # 256 KB chunks
        pos = file_size
        overlap = 0
        last_join_pos = None

        # Switch to byte mode for backwards scanning
        fh.seek(0)
        raw = None

        while pos > 0:
            read_start = max(0, pos - CHUNK)
            read_size = pos - read_start + overlap
            fh.seek(read_start)
            raw = fh.read(read_size)

            # Find the LAST occurrence of the marker in this chunk
            idx = raw.rfind("{Join PU}")
            if idx != -1:
                # Walk back to the start of the line
                line_start = raw.rfind("\n", 0, idx)
                line_start = line_start + 1 if line_start != -1 else 0
                last_join_pos = read_start + line_start
                break

            # Move window backwards, keeping overlap for lines split across chunks
            overlap = 512
            pos = read_start

        return last_join_pos

    def stop(self) -> None:
        self._monitor.stop()
        if hasattr(self, "_ipc_timer"):
            self._ipc_timer.stop()

    # ── IPC ──────────────────────────────────────────────────────────────────

    def _poll_ipc(self) -> None:
        if not self._cmd_file:
            return
        for cmd in self._ipc_read(self._cmd_file):
            t = cmd.get("type", "")
            if t == "show":
                self._hud.show()
                self._hud.raise_()
            elif t == "hide":
                self._hud.hide()
            elif t == "toggle":
                if self._hud.isVisible():
                    self._hud.hide()
                else:
                    self._hud.show()
                    self._hud.raise_()
            elif t == "report":
                self._speak_loadout()
            elif t == "reset_position":
                self._reset_position()
            elif t == "quit":
                QApplication.instance().quit()

    def _reset_position(self) -> None:
        """The launcher's Settings > "Reset position for Pico Pals and Battle Buddy":
        the HUD goes back to the centre of the primary screen and that spot is
        saved at once, so the next hide or exit cannot write the old one back.
        The click-through toggle follows the HUD by itself; an Options or
        Tutorial window that is open comes back to the centre too."""
        _centre_on_primary(self._hud)
        self._hud.save_position_now()
        for popup in (OptionsPopup._instance, TutorialPopup._instance):
            if popup is not None and popup.isVisible():
                _centre_on_primary(popup)
        logger.info("HUD position reset to the centre of the primary screen: %s", self._hud.pos())

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_parser_event(self, event) -> None:
        if event.event_type == "session_join":
            # Show HUD when player loads into PU — must be on main thread
            QTimer.singleShot(0, lambda: (self._hud.show(), self._hud.raise_()))

    def _on_state_changed(self, state) -> None:
        self._hud.push_state(state)

    # ── Options / Tutorial ────────────────────────────────────────────────────

    def _show_options(self) -> None:
        OptionsPopup.show_options(on_save=self._apply_options)

    def _show_tutorial(self) -> None:
        TutorialPopup.show_tutorial()

    def _apply_options(self, settings: dict) -> None:
        new_log   = settings.get("log_path",    self._log_path)
        new_orient = settings.get("orientation", self._orientation)

        # Restart log monitor if path changed
        if new_log != self._log_path:
            self._log_path = new_log
            self._monitor.stop()
            self._monitor = LogMonitor(new_log)
            self._monitor.subscribe(self._parser.on_line)
            self._monitor.start()
            logger.info("Log monitor restarted: %s", new_log)

        # Rebuild HUD if orientation changed
        if new_orient != self._orientation:
            self._orientation = new_orient
            self._hud.set_orientation(new_orient)

    # ── Voice report ──────────────────────────────────────────────────────────

    def _speak_loadout(self) -> None:
        """Log a text summary (WingmanAI reads the return value from the tool)."""
        state = self._tracker.get_state()
        lines = []
        for slot in ("primary_1", "primary_2", "sidearm", "utility", "utility_2"):
            w = state.weapons.get(slot)
            if w:
                lines.append(f"{w.display_name}: {w.spare_mags} spare mag(s)")
        lines.append(f"Medpens: {state.medpens}/2")
        lines.append(f"Oxypens: {state.oxypens}/2")
        lines.append(f"Grenades: {state.grenades}")
        logger.info("Loadout report: %s", " | ".join(lines))


def main() -> None:
    cfg = _parse_args()
    logger.info("Starting with config: %s", cfg)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    buddy = BattleBuddyApp(cfg)
    buddy.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
