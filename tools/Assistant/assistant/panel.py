"""AssistantPanel — the always-on-top HUD window the user talks to.

Layout:

  [ title bar                                             ✕ ]
  [ Ears ] [ Set Mic Key ] [ Voice Replies ] [ Settings… ]
  <no-mic-key hint, only while no key is set>
  <one-time notice: what came over from the Star Map's voice settings>
  You:  <last thing the ears heard>
  AI:   <the assistant's reply, word-wrapped>
  <status line>

The agent runs blocking LLM calls on a worker thread; everything reaches
the GUI through Qt signals. The ears/mouth/speak pipeline is optional-
dependency gated.

This window is the toolbox's ONE microphone. The Star Map
used to have its own ears and voice bar; they are gone, and what is said for
the map is relayed to it from here (starmap_bridge.py, the starmap_command
tool). The Star Map's saved mic settings are folded in once at start-up.

Its own push-to-talk key. The mic key is the ASSISTANT's key:
held, it opens the mic and what is said goes to this agent, whichever tab of
the shared window is showing and with the window closed. SuitMk2 has a key of
its own; shared/mic_floor.py keeps the two from opening the microphone at
once. See "the microphone" below for who may listen when.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from shared import ptt_keys
from shared.mic_floor import FLOOR, OneMicMixin
from shared.qt.theme import P
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar

from .agent import AssistantAgent
from .config import LLMConfig, model_note
from .set_route import gate as route_gate
from .set_route import phrases as route_phrases
from .tools import ToolContext
from .voice import EarsController, Mouth
from .voice_input import InputBinding, KeyCaptureDialog

log = logging.getLogger(__name__)

_STATE_PATH = os.path.join(os.path.expanduser("~"), ".sctoolbox",
                           "assistant_panel.json")

#: Said to close an always-open mic. It was the Star Map's "ears off" command;
#: the mic is this window's now, so the command is handled here and never
#: reaches the agent. There is no "off": it drops to push-to-talk.
_STOP_LISTENING = re.compile(r"^\W*(?:please\s+)?(?:ears?\s+off|stop\s+listening|go\s+to\s+sleep)\W*$",
                             re.IGNORECASE)


def _btn_ss() -> str:
    return (
        f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; "
        f"border: 1px solid {P.border}; padding: 4px 12px; "
        f"font-family: Consolas; font-size: 9pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {P.energy_cyan}; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; }} "
        f"QPushButton:checked {{ color: {P.energy_cyan}; "
        f"border-color: {P.energy_cyan}; }}"
    )


class _Ears(OneMicMixin, EarsController):
    """The Assistant's ears behind the process's one microphone (shared/mic_floor.py):
    while SuitMk2's key is held, this one's is refused, and the other way round."""

    captureFailed = Signal(str)
    captureBusy = Signal(str)


class _AskWorker(QThread):
    """Run one agent turn off the GUI thread."""

    replyReady = Signal(str)
    failed = Signal(str)

    def __init__(self, agent: AssistantAgent, text: str, parent=None) -> None:
        super().__init__(parent)
        self._agent = agent
        self._text = text

    def run(self) -> None:
        try:
            reply = self._agent.handle_user_text(self._text)
            self.replyReady.emit(reply)
        except Exception as exc:                          # noqa: BLE001
            log.exception("assistant turn failed")
            self.failed.emit(str(exc))


#: The first row of the Windows voice list: no voice is named, so Windows uses its own default.
WINDOWS_VOICE_DEFAULT = "Windows default"
#: After the name of a saved voice that Windows no longer lists. It is kept, and the default voice speaks.
WINDOWS_VOICE_MISSING = " (not on this PC)"
WINDOWS_VOICE_TIP = ("Which of the Windows voices installed on this PC speaks.\n"
                     "It is heard when Tool voice, in the launcher's Settings, is set to Windows voice,\n"
                     "and whenever Elah's or Montaigne's own voice cannot be used.")


class _SettingsDialog(QDialog):
    """LLM endpoint editor — the plug-in point, in GUI form. It also has the choice of Windows voice.

    windows_voice: the saved choice ("" = Windows default). list_voices() -> the names of the installed voices;
    it is asked on a thread of its own, because asking Windows takes a second or two, and the list fills in when
    the answer is there. Until then the list has the default and the saved choice."""

    def __init__(self, cfg: LLMConfig, parent=None, windows_voice: str = "", list_voices=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("LLM Settings")
        self.setMinimumWidth(460)
        self.cfg = cfg
        lay = QFormLayout(self)

        self.provider = QLineEdit(cfg.provider)
        self.base_url = QLineEdit(cfg.base_url)
        self.model = QLineEdit(cfg.model)
        self.api_key = QLineEdit(cfg.api_key)
        self.api_key.setEchoMode(QLineEdit.Password)
        self.max_tokens = QLineEdit(str(cfg.max_tokens))
        self.mode = QLineEdit(cfg.mode)

        lay.addRow("Provider (openai|anthropic)", self.provider)
        lay.addRow("Base URL", self.base_url)
        lay.addRow("Model", self.model)
        lay.addRow("API key (empty for local)", self.api_key)
        lay.addRow("Max tokens", self.max_tokens)
        lay.addRow("Mode (router | router+llm | llm)", self.mode)

        self.windows_voice = QComboBox()
        self.windows_voice.setToolTip(WINDOWS_VOICE_TIP)
        self._saved_voice = str(windows_voice or "").strip()
        self._fill_windows_voices(None)
        lay.addRow("Windows voice", self.windows_voice)
        self._voices_found: Optional[list] = None       # set by the thread; the list is touched only by the poll
        self._voice_poll = QTimer(self)
        self._voice_poll.setInterval(150)
        self._voice_poll.timeout.connect(self._poll_windows_voices)
        self._voice_thread = threading.Thread(target=self._find_windows_voices, args=(list_voices,),
                                              name="assistant_windows_voices", daemon=True)
        self._voice_thread.start()
        self._voice_poll.start()

        btns = QDialogButtonBox(QDialogButtonBox.Save |
                                QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addRow(btns)

    # ── the Windows voice ─────────────────────────────────────────────────
    def _find_windows_voices(self, list_voices) -> None:
        """On its own thread: ask which Windows voices are installed. Never raises; no list is an empty list."""
        try:
            if list_voices is None:
                from shared.character_voice import installed_windows_voices as list_voices
            self._voices_found = [str(n) for n in list_voices()]
        except Exception:
            log.exception("assistant settings: the Windows voices could not be listed")
            self._voices_found = []

    def _poll_windows_voices(self) -> None:
        if self._voices_found is None:
            return
        self._voice_poll.stop()
        self._fill_windows_voices(self._voices_found)

    def _fill_windows_voices(self, names: Optional[list]) -> None:
        """Windows default, then the installed voices. names=None: they are not known yet. What is chosen stays
        chosen: the row picked in this dialog, or else the saved one, which is listed even when Windows no
        longer has it, so that opening the dialog and saving never changes the choice by itself. It is marked
        as not on this PC only when Windows did list its voices; an empty list says nothing either way."""
        box = self.windows_voice
        chosen = str(box.currentData() or "") if box.count() else self._saved_voice
        box.blockSignals(True)
        box.clear()
        box.addItem(WINDOWS_VOICE_DEFAULT, "")
        for name in names or []:
            if name and box.findData(name) < 0:
                box.addItem(name, name)
        if chosen and box.findData(chosen) < 0:
            box.addItem(chosen + (WINDOWS_VOICE_MISSING if names else ""), chosen)
        box.setCurrentIndex(max(0, box.findData(chosen)))
        box.blockSignals(False)

    def result_windows_voice(self) -> str:
        """The chosen Windows voice by name, or "" for Windows default."""
        return str(self.windows_voice.currentData() or "")

    def result_config(self) -> LLMConfig:
        self.cfg.provider = self.provider.text().strip() or "openai"
        self.cfg.base_url = self.base_url.text().strip()
        self.cfg.model = self.model.text().strip()
        self.cfg.api_key = self.api_key.text().strip()
        from .agent import normalize_mode
        self.cfg.mode = normalize_mode(self.mode.text())
        try:
            self.cfg.max_tokens = max(64, int(self.max_tokens.text()))
        except ValueError:
            pass
        return self.cfg


class _AssistantBody:
    """Everything the assistant HUD is and does, without the window around it.

    Two classes at the bottom of this file put it on screen: AssistantWindow
    (its own window, as it has always been) and AssistantPanel (one tab of the
    Toolbox Assistant window, which also has SuitMk2 as a tab).
    Both get every method here. They differ in the frame, in whether a window
    position is saved, and in who owns the microphone.

    The two classes each declare speakRequested / statusRequested / pttState /
    pttChanged themselves: a Qt signal has to be declared on a QObject class,
    and this one is not.
    """

    # False while another tab of a shared window is the one in front; see
    # mic_take / mic_release. Always True for AssistantWindow.
    _mic_mine = True
    # True when the window has said this tab may go on watching its
    # push-to-talk key while it is not in front; see ptt_background.
    _ptt_bg = False
    # What to say on this tab when the other tab's key is the same key.
    _ptt_clash = ""
    # True while the turn in hand was started with the push-to-talk key.
    _ptt_turn = False
    # True when this body is a whole window, so its position is worth saving.
    _owns_window = True

    def _build(self, base_dir: str, chrome: bool = True) -> None:
        self._base_dir = base_dir
        self._state = self._load_state()
        self._migration_notes = self._migrate_starmap_voice()
        self._worker: Optional[_AskWorker] = None
        self._capture: Optional[KeyCaptureDialog] = None

        # ── brain ────────────────────────────────────────────────────────
        cfg = LLMConfig.load()
        self.speakRequested.connect(self._speak)
        self.statusRequested.connect(self._set_status)
        ctx = ToolContext(base_dir=base_dir, speak=self.speakRequested.emit,
                          status=self.statusRequested.emit)
        from .builtin_tools import build_default_registry
        self._agent = AssistantAgent(build_default_registry(), ctx)
        self._agent.configure(cfg)
        self._agent.on_speak = self.speakRequested.emit

        # ── voice ────────────────────────────────────────────────────────
        # Speaks as the player's chosen character (Elah by default, set in the launcher's Settings).
        try:
            from shared.character_voice import CharacterMouth
            self._mouth = CharacterMouth()
        except Exception:
            self._mouth = Mouth()
        self._apply_windows_voice()
        self._make_ears()

        # ── listener penguin ─────────────────────────────────────────────
        # Driven by speakingChanged, NOT listeningChanged: the latter says the MIC IS
        # OPEN, which in "always" mode fires once at startup and never again, so he
        # would fade in at launch and stand there for the rest of the session.
        # Optional by construction — the Assistant already treats voice as optional and
        # must not fail to open because an ornament is missing.
        self._penguin = None
        try:
            from .listener_penguin import ListenerPenguin
            self._penguin = ListenerPenguin(self)
            if self._penguin.asset_ok():
                self._ears.speakingChanged.connect(self._penguin.set_speaking)
            else:
                log.info("listener penguin idle: %s", self._penguin.degrade_reason())
                self._penguin = None
        except Exception as exc:                      # noqa: BLE001 - ornament, never fatal
            log.info("listener penguin unavailable (%s: %s)", type(exc).__name__, exc)
            self._penguin = None

        # ── chrome ───────────────────────────────────────────────────────
        if chrome:                      # as a tab, the window it sits in has the title bar
            tb = SCTitleBar(self, title="TOOLBOX ASSISTANT", icon_text="🤖",
                            accent_color=P.energy_cyan, show_minimize=True)
            tb.minimize_clicked.connect(self.showMinimized)
            tb.close_clicked.connect(self.close)
            self.content_layout.addWidget(tb)

        row = QHBoxLayout()
        row.setContentsMargins(10, 6, 10, 2)
        row.setSpacing(8)

        # The ears' armed state; never shown (the ears are always on).
        self._btn_ears = QPushButton("Ears", self)
        self._btn_ears.setCheckable(True)
        self._btn_ears.setVisible(False)
        self._btn_ears.toggled.connect(self._on_ears_toggled)

        self._mode_btns = {}
        for label, value in (("Push-to-talk", "push"), ("Always on", "always")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(self._ears.mode() == value)
            b.setStyleSheet(_btn_ss())
            b.setToolTip("Hold your mic key to talk" if value == "push"
                         else "The mic stays open; just talk")
            b.clicked.connect(lambda _c=False, v=value: self._set_mic_mode(v))
            row.addWidget(b)
            self._mode_btns[value] = b

        self._btn_key = QPushButton("Set Mic Key")
        self._btn_key.setStyleSheet(_btn_ss())
        self._btn_key.setToolTip(
            "The Assistant's own push-to-talk key. Hold it and talk: what you say goes to "
            "the Assistant, whichever tab is showing and with this window closed.\n"
            "Suit Mk2 has a key of its own on its tab. Click to change this one.")
        self._btn_key.clicked.connect(self._pick_binding)
        row.addWidget(self._btn_key)

        self._btn_replies = QPushButton("Voice Replies")
        self._btn_replies.setCheckable(True)
        self._btn_replies.setChecked(bool(self._state.get("voice_replies", True)))
        self._btn_replies.setStyleSheet(_btn_ss())
        row.addWidget(self._btn_replies)

        self._btn_cfg = QPushButton("Settings…")
        self._btn_cfg.setStyleSheet(_btn_ss())
        self._btn_cfg.clicked.connect(self._edit_settings)
        row.addWidget(self._btn_cfg)
        row.addStretch(1)
        self.content_layout.addLayout(row)

        # ── set route: the In-Game switch and its calibration ────────────
        # Setting a route in the game is this tool's own code now
        # (assistant/set_route/), so the switch that allows it and the
        # calibration it needs are here too, and work with the Star Map closed.
        # The Star Map's In-Game button is the same switch (one saved file).
        route_row = QHBoxLayout()
        route_row.setContentsMargins(10, 0, 10, 2)
        route_row.setSpacing(8)
        self._btn_game = QPushButton("In-Game")
        self._btn_game.setCheckable(True)
        self._btn_game.setStyleSheet(_btn_ss())
        self._btn_game.setToolTip(
            "On: 'navigate to Area 18' may set the route inside Star Citizen (it asks "
            "first, then uses the mouse and keyboard).\n"
            "Off: nothing is ever sent to the game.")
        self._btn_game.setChecked(route_gate.in_game_enabled())
        self._btn_game.toggled.connect(self._on_in_game_toggled)
        route_row.addWidget(self._btn_game)
        self._btn_calibrate = QPushButton("Calibrate Route")
        self._btn_calibrate.setStyleSheet(_btn_ss())
        self._btn_calibrate.setToolTip(
            "3-click calibration of the in-game route setter: where the game's star map "
            "has its search bar, its first result and its centre.\n"
            "Or say \"calibrate star map\": it then talks you through the clicks.\n"
            "Open Star Citizen first. This was Calibrate Star Map in the Star Map tool.")
        self._btn_calibrate.clicked.connect(lambda _c=False: self._calibrate_route())
        route_row.addWidget(self._btn_calibrate)
        route_row.addStretch(1)
        self.content_layout.addLayout(route_row)

        # shown while no mic key is set (and after a refused one), so the
        # user sees why holding a key does nothing
        self._lbl_mic = QLabel("")
        self._lbl_mic.setWordWrap(True)
        self._lbl_mic.setStyleSheet(
            f"color: {P.yellow}; "
            f"font-family: Consolas; font-size: 9pt; "
            f"background: transparent; padding: 0 12px;")
        self.content_layout.addWidget(self._lbl_mic)

        # Shown once, the first time this window opens after the Star Map's voice
        # moved here: what was carried over and what was deliberately left alone.
        self._lbl_notice = QLabel(" ".join(self._migration_notes))
        self._lbl_notice.setWordWrap(True)
        self._lbl_notice.setStyleSheet(
            f"color: {P.energy_cyan}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent; padding: 0 12px;")
        self._lbl_notice.setVisible(bool(self._migration_notes))
        self.content_layout.addWidget(self._lbl_notice)

        self._lbl_heard = QLabel("You: —")
        self._lbl_heard.setWordWrap(True)
        self._lbl_heard.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent; padding: 0 12px;")
        self.content_layout.addWidget(self._lbl_heard)

        self._lbl_reply = QLabel("AI: hold your mic key (or pick Always on) and "
                                 "talk to me. Ask for the best cargo route for "
                                 "your ship.")
        self._lbl_reply.setWordWrap(True)
        self._lbl_reply.setStyleSheet(
            f"color: {P.fg_bright}; font-family: Consolas; font-size: 10pt; "
            f"background: transparent; padding: 0 12px;")
        self.content_layout.addWidget(self._lbl_reply, 1)

        if self._penguin is not None:
            self.content_layout.addWidget(self._penguin, 0)

        # ── text input (for users without a mic) ───────────────────────
        input_row = QHBoxLayout()
        input_row.setContentsMargins(10, 2, 10, 6)
        input_row.setSpacing(6)
        self._txt_input = QLineEdit()
        self._txt_input.setPlaceholderText(
            "Type a message… (Enter to send)")
        self._txt_input.setStyleSheet(
            f"QLineEdit {{ background: {P.bg_card}; color: {P.fg_bright}; "
            f"border: 1px solid {P.border}; padding: 4px 8px; "
            f"font-family: Consolas; font-size: 9pt; }} "
            f"QLineEdit:focus {{ border-color: {P.energy_cyan}; }}")
        self._txt_input.returnPressed.connect(self._on_send)
        input_row.addWidget(self._txt_input, 1)
        btn_send = QPushButton("Send")
        btn_send.setStyleSheet(_btn_ss())
        btn_send.setDefault(True)
        btn_send.clicked.connect(self._on_send)
        input_row.addWidget(btn_send)
        self.content_layout.addLayout(input_row)

        self._lbl_status = QLabel("ready — router only (no model)" if cfg.mode == "router"
                                  else f"ready — {cfg.mode} / {cfg.model}")
        self._lbl_status.setStyleSheet(
            f"color: {P.energy_cyan}; font-family: Consolas; font-size: 8pt; "
            f"background: transparent; padding: 4px 12px;")
        self.content_layout.addWidget(self._lbl_status)
        self._ready_text = "ready"              # what the status line goes back to after a turn
        self._check_model()

        geom = self._state.get("geom")
        if self._owns_window and geom and len(geom) == 4:
            self.setGeometry(*geom)
        self._restore_binding()
        self._ensure_ears()

    def _make_ears(self) -> None:
        """The ears, set up from the saved state and wired to this HUD. What they
        hear comes back to THIS tool (_on_transcript) and to no other: that
        wiring, plus the one-microphone floor, is the whole of "the key decides
        which AI hears it"."""
        self._ears = _Ears(self)
        self._ears.use_floor(FLOOR, ptt_keys.ASSISTANT)
        # Ears are always on; the player picks push-to-talk
        # (default) or always on. An old saved "toggle" becomes push-to-talk.
        mode = self._state.get("mic_mode", "push")
        self._ears.set_mode(mode if mode in ("push", "always") else "push")
        if self._state.get("whisper_model"):       # carried over from the Star Map, if it had one
            self._ears.set_model(str(self._state["whisper_model"]))
        self._ears.statusChanged.connect(self._set_status)
        self._ears.statusChanged.connect(self._on_ears_status)
        self._ears.transcript.connect(self._on_transcript)
        self._ears.needsInstall.connect(self._on_needs_install)
        self._ears.listeningChanged.connect(self._on_listening)
        self._ears.captureFailed.connect(self._on_capture_failed)
        self._ears.captureBusy.connect(self._on_capture_busy)

    # ── the microphone, when this HUD is one tab of a bigger window ──────
    # In its own window the assistant always owns its microphone. As a tab it
    # shares the window with SuitMk2, which has ears of its own, and two tabs
    # must never listen at once: the same sentence would be answered twice.
    #
    # An ALWAYS-OPEN mic belongs to the tab that is showing: the window hands
    # it over with mic_take and takes it away with mic_release.
    #
    # A PUSH-TO-TALK key is different: each tool has its own,
    # and holding one talks to that tool whichever tab is showing. So after
    # mic_release the window says ptt_background(True), and this tab goes on
    # watching its key. Two keys still cannot open the microphone twice:
    # the ears ask shared/mic_floor.py before every capture, and the second
    # key held is refused. The window does not say ptt_background(True) when
    # both tools are set to the SAME key; then only the tab in front has it.
    #
    # Every arm goes through the hidden Ears button (_ensure_ears checks it,
    # _on_ears_toggled arms), and mic_release leaves it unchecked, so
    # _on_ears_toggled is the one door: it refuses unless _may_listen(), and a
    # mode change or a new mic key cannot open it.
    def _may_listen(self) -> bool:
        """In front: yes, as the saved mode says. Not in front: only a
        push-to-talk key, and only once the window has allowed it."""
        return self._mic_mine or (self._ptt_bg and self._ears.mode() == "push")

    def mic_release(self) -> None:
        """Another tab is in front now: close the mic, stop watching the key
        (ptt_background says afterwards whether the key may be watched)."""
        self._mic_mine = False
        self._ptt_bg = False
        if self._btn_ears.isChecked():
            self._btn_ears.setChecked(False)       # -> _on_ears_toggled -> disarm
        if self._ears.armed() or self._ears.recording():
            self._ears.disarm()

    def mic_take(self) -> None:
        """This tab is the one showing: listen again, as the saved settings say."""
        self._mic_mine = True
        self._ensure_ears()

    def ptt_background(self, on: bool) -> None:
        """Whether this tab may watch its push-to-talk key while another tab is
        in front. Says nothing to a tab that is in front."""
        self._ptt_bg = bool(on)
        if self._mic_mine:
            return
        if self._may_listen():
            if not self._ears.armed():
                self._ensure_ears()
        else:
            if self._btn_ears.isChecked():
                self._btn_ears.setChecked(False)
            if self._ears.armed() or self._ears.recording():
                self._ears.disarm()

    def ptt_binding(self) -> Optional[dict]:
        """This tool's push-to-talk key as a dict (shared/ptt_keys.py reads it),
        or None when holding a key does nothing here (mic always open, no key)."""
        b = self._ears.binding()
        if b is None or self._ears.mode() != "push":
            return None
        return {"kind": b.kind, "code": b.code}

    def ptt_clash(self, other: str) -> None:
        """The window found this key is also *other*'s key ("" = it is not)."""
        self._ptt_clash = ("This is also %s's key, so it only works for the tab that is showing. "
                           "Give one of them a different key." % other) if other else ""
        self._show_mic_key()

    def _on_listening(self, on: bool) -> None:
        """The mic opened or closed. Told to the window only for a held key: an
        always-open mic opens once and stays, and is nobody holding anything."""
        if self._ears.mode() != "push":
            return
        if on:
            self._ptt_turn = True
        self.pttState.emit("listening" if on else "released", "")

    def _on_capture_failed(self, why: str) -> None:
        """The key was held and the microphone did not open. Said, and nothing
        else happens: what was meant for this tool is never sent to the other."""
        self._ptt_turn = False
        self.pttState.emit("error", why or "the microphone did not open")

    def _on_capture_busy(self, owner: str) -> None:
        self._set_status("the other tab's key is being held - let go of it first")

    def _on_ears_status(self, msg: str) -> None:
        if self._ears.mode() != "push":
            return
        if msg.startswith("ears error"):          # speech-to-text failed after the key was released
            self._ptt_turn = False
            self.pttState.emit("error", msg)
        elif msg in ("heard nothing", "that was me — ignored"):
            self._ptt_turn = False
            self.pttState.emit("note", msg)

    # ── ears: always on, push-to-talk or always-open mic ─────────────────
    def _ensure_ears(self) -> None:
        """(Re)arm the ears with the current mode and key."""
        if self._btn_ears.isChecked():
            self._ears.disarm()
            if not self._ears.arm():
                self._btn_ears.setChecked(False)
        else:
            self._btn_ears.setChecked(True)

    def _migrate_starmap_voice(self) -> list:
        """Fold the Star Map's saved mic settings into this window's state, once.

        See starmap_bridge.migrate_starmap_voice for the rule (what this window
        already has is kept; the Star Map's values only fill gaps). Runs before
        the ears are built, so a carried-over mode is the one they start in.
        Never fatal: a failure leaves the settings as they were and tries again
        next launch."""
        try:
            import datetime
            from . import starmap_bridge
            before = json.dumps(self._state, sort_keys=True, default=str)
            notes = starmap_bridge.migrate_starmap_voice(
                self._state, starmap_bridge.load_starmap_state(),
                today=datetime.date.today().isoformat())
            if json.dumps(self._state, sort_keys=True, default=str) != before:
                os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
                with open(_STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(self._state, f, indent=2)
            for line in notes:
                log.info("assistant: star map voice migration: %s", line)
            return notes
        except Exception as exc:                          # noqa: BLE001 - never block the window
            log.warning("assistant: could not carry over the Star Map's voice settings "
                        "(%s: %s); they are untouched", type(exc).__name__, exc)
            return []

    def _set_mic_mode(self, value: str) -> None:
        self._lbl_notice.setVisible(False)     # the pilot has now chosen for themselves
        for v, b in self._mode_btns.items():
            b.setChecked(v == value)
        self._ears.set_mode(value)
        self._state["mic_mode"] = value
        self._save_state()
        self._show_mic_key()
        self._ensure_ears()
        self.pttChanged.emit()

    # ── voice plumbing ───────────────────────────────────────────────────
    def _speak(self, text: str) -> None:
        if self._btn_replies.isChecked() and Mouth.available():
            # Warn the ears BEFORE the line is queued: with the mic always open they
            # hear the speakers, and "Say yes or no" can come back as the user's own
            # next utterance, mistranscribed to "or not": the yes/no parse then reads a
            # refusal and refuses what was just approved.
            self._ears.note_speaking(text)
            try:
                self._mouth.speak(text)
            except Exception:
                self._ears.cancel_speaking()   # never spoken: do not sit there deaf
                raise
        self._lbl_reply.setText("AI: " + text)
        self._lbl_reply.setToolTip(text)
        if getattr(self, "_ptt_turn", False):                         # he asked with the key: he may not be looking at this tab
            self.pttState.emit("reply", text)

    def _set_status(self, msg: str) -> None:
        self._lbl_status.setText(msg)

    def _on_send(self) -> None:
        """Text-box path into the same turn pipeline the ears use."""
        text = self._txt_input.text().strip()
        if not text:
            return
        self._txt_input.clear()
        self._lbl_heard.setText("You: " + text)
        self._set_status("thinking…")
        self._run_turn(text)

    def _on_transcript(self, text: str) -> None:
        self._lbl_heard.setText("You: " + text)
        if getattr(self, "_ptt_turn", False):
            self.pttState.emit("heard", text)
        if _STOP_LISTENING.match(text or ""):
            self._stop_listening()
            return
        if route_phrases.is_calibrate(text):
            self._calibrate_by_voice()
            return
        self._set_status("thinking…")
        self._run_turn(text)

    def _stop_listening(self) -> None:
        """'Stop listening' / 'ears off': close an always-open mic (push-to-talk)."""
        if self._ears.mode() != "push":
            self._set_mic_mode("push")
        msg = ("Push-to-talk. Hold " + self._ears.binding().describe() + " to talk."
               if self._ears.binding() is not None
               else "Push-to-talk. Set a mic key to talk to me again.")
        self._lbl_reply.setText("AI: " + msg)
        self._set_status("mic closed — push-to-talk")


    def _run_turn(self, text: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._set_status("still busy — wait a beat")
            return
        self._worker = _AskWorker(self._agent, text, self)
        self._worker.replyReady.connect(self._on_reply)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_reply(self, reply: str) -> None:
        self._lbl_reply.setText("AI: " + reply)
        self._lbl_reply.setToolTip(reply)
        ready = getattr(self, "_ready_text", "ready")
        self._set_status(ready)
        if ready != "ready":                    # still without its model a moment ago: it may be here by now
            self._check_model()
        if getattr(self, "_ptt_turn", False):
            self._ptt_turn = False
            self.pttState.emit("reply", reply)
        self._sync_in_game()

    # ── set route: the In-Game switch and calibration ────────────────────
    def _on_in_game_toggled(self, on: bool) -> None:
        """Save the switch where the route code reads it (set_route/gate.py)."""
        if not route_gate.set_in_game(bool(on)):
            self._set_status("could not save the In-Game switch")
            self._sync_in_game()
            return
        self._set_status("in-game route plotting on" if on
                         else "in-game route plotting off: nothing is sent to the game")

    def _sync_in_game(self) -> None:
        """Show the saved switch; the Star Map's In-Game button writes it too."""
        on = route_gate.in_game_enabled()
        if self._btn_game.isChecked() != on:
            self._btn_game.blockSignals(True)
            self._btn_game.setChecked(on)
            self._btn_game.blockSignals(False)

    def _calibrate_route(self, begin: bool = False) -> None:
        """The in-game macro's 3-click calibration. This is the only button that
        opens it: the Star Map's "Calibrate Star Map" moved here.

        It needs the game's own star map on screen (the pilot clicks its search
        bar, a result and its centre) and pynput to see those clicks. It needs
        nothing from the toolbox's Star Map tool. The positions are saved in the
        pilot's own folder (~/.sctoolbox/set_route/mouse_calibration.json), which
        an update leaves alone. Until then a calibration made with the WingmanAI
        set-route skill is read, where that skill's folder exists."""
        open_dlg = getattr(self, "_cal_dlg", None)
        if open_dlg is not None:            # already open (a second request queued behind the first)
            if begin:
                open_dlg.begin()
            return
        try:
            from .set_route.route_setter import RouteCalibrationDialog
        except ImportError as exc:
            self._set_status("calibration unavailable: %s" % exc)
            return
        # The dialog opens on step 0, "Click on the game and say 'calibrate star map' to begin.",
        # and watches nothing until _calibrate_by_voice hears that, or its Begin button is pressed. Its prompts
        # are spoken through _speak: the same path as every reply, so Voice Replies decides whether they are
        # heard and the ears are warned before each one (an always-open mic would otherwise hear the prompt).
        dlg = RouteCalibrationDialog(self, speak=self._speak, mic_hint=self._mic_hint(), begin=bool(begin))
        self._cal_dlg = dlg
        try:
            ok = dlg.exec() and dlg.result_ready
        finally:
            self._cal_dlg = None
        if ok:
            self._set_status("route setter calibrated")
        else:
            self._set_status("calibration cancelled")

    def _calibrate_by_voice(self) -> None:
        """"Calibrate star map" was heard.

        With the dialog showing its first step, that is the cue it is waiting for. With no dialog, he has already
        said the words: open it and start straight away. The dialog is modal and its exec() does not return until
        it closes, so it is opened from the event loop, not from inside the slot the transcript arrived on."""
        dlg = getattr(self, "_cal_dlg", None)
        if dlg is not None:
            dlg.begin()
            return
        QTimer.singleShot(0, lambda: self._calibrate_route(begin=True))

    def _mic_hint(self) -> str:
        """One line for the dialog's first step: can he be heard right now, and how. "" when he simply can."""
        use_begin = " Press Begin instead."
        try:
            from . import missing_voice_deps
            missing = list(missing_voice_deps() or [])
        except Exception:                   # noqa: BLE001 - cannot tell: say nothing rather than guess
            missing = []
        if missing:
            return "Voice input is not installed (missing: " + ", ".join(missing) + ")." + use_begin
        if not self._may_listen():
            return "The microphone is with the other tab right now." + use_begin
        ears = self._ears
        if ears.mode() == "always":
            return "" if ears.armed() else "The microphone is off." + use_begin
        b = ears.binding()
        if b is None:
            return "No mic key is set, so I cannot hear you." + use_begin
        if not ears.armed():
            return "The microphone is off." + use_begin
        return "Hold " + b.describe() + " while you say it."

    def _on_failed(self, err: str) -> None:
        self._lbl_reply.setText("AI: error — " + err)
        self._set_status("error")
        if getattr(self, "_ptt_turn", False):
            self._ptt_turn = False
            self.pttState.emit("error", err)

    def _on_needs_install(self, packages: list) -> None:
        self._btn_ears.setChecked(False)
        self._set_status("pip install " + " ".join(packages))

    def _on_ears_toggled(self, on: bool) -> None:
        if on:
            if not self._may_listen() or not self._ears.arm():
                self._btn_ears.setChecked(False)
        else:
            self._ears.disarm()

    def _pick_binding(self) -> None:
        self._set_status("press the key to hold while you talk (left/right click not allowed)…")
        self._btn_key.setText("Press a key…")
        self._capture = KeyCaptureDialog(self)
        self._capture.captured.connect(self._binding_captured)
        self._capture.refused.connect(self._binding_refused)
        if not self._capture.start():
            self._set_status("pynput missing — pip install pynput")
            self._show_mic_key()

    def _binding_refused(self, why: str) -> None:
        # the capture keeps listening; say why in the binding UI itself
        self._btn_key.setText("Not that one - press a key…")
        self._lbl_mic.setText(why)
        self._lbl_mic.setVisible(True)
        self._set_status(why)

    def _binding_captured(self, binding: InputBinding) -> None:
        self._capture = None
        if not self._ears.set_binding(binding):
            self._binding_refused(binding.refused())
            return
        self._state["binding"] = {"kind": binding.kind, "code": binding.code}
        self._save_state()
        self._set_status("mic key set: " + binding.describe()
                         + (" (hold to talk)" if self._ears.mode() == "push" else ""))
        self._show_mic_key()
        self._ensure_ears()
        self.pttChanged.emit()

    def _restore_binding(self) -> None:
        # What was saved, or the Assistant's default key when nothing ever was
        # (shared/ptt_keys.py). The default is not written to the state file:
        # only a key the pilot picked is.
        raw = ptt_keys.assistant_binding(self._state)
        if raw and raw.get("code"):
            b = InputBinding(raw.get("kind", "key"), raw["code"])
            if b.refused():
                # an old saved left/right click: do not arm it, ask for a key
                self._lbl_mic.setText("Your saved mic key (" + b.describe() + ") is no longer "
                                      "allowed. " + b.refused())
                self._show_mic_key(keep_hint=True)
                return
            self._ears.set_binding(b)
        self._show_mic_key()

    def _show_mic_key(self, keep_hint: bool = False) -> None:
        """Button text + hint line from the current binding."""
        b = self._ears.binding()
        if b is None and self._ears.mode() == "always":
            self._btn_key.setText("Set Mic Key")
            self._lbl_mic.setText("")
            self._lbl_mic.setVisible(False)       # no key needed with the mic always open
        elif b is None:
            self._btn_key.setText("Set Mic Key")
            if not keep_hint:
                self._lbl_mic.setText("No mic key set. Click Set Mic Key and press the "
                                      "keyboard key you want to hold while you talk.")
            self._lbl_mic.setVisible(True)
        else:
            self._btn_key.setText("Mic key: " + b.describe())
            self._lbl_mic.setText(self._ptt_clash)
            self._lbl_mic.setVisible(bool(self._ptt_clash))

    def _check_model(self) -> None:
        """Say so, in the status line, when the Assistant is answering without its small local model
        (config.model_note: not on this PC yet, or the service is not running). One short request off
        the GUI thread; the line is set through statusRequested, which is safe from a thread."""
        cfg = LLMConfig.load()

        def work():
            try:
                note = model_note(cfg)
            except Exception:                    # a check that cannot run says nothing
                return
            self._ready_text = note or "ready"
            if note:
                try:
                    self.statusRequested.emit(note)
                except RuntimeError:             # the window closed meanwhile
                    pass
        threading.Thread(target=work, name="assistant_model_check", daemon=True).start()

    # ── settings ─────────────────────────────────────────────────────────
    def _edit_settings(self) -> None:
        dlg = _SettingsDialog(LLMConfig.load(), self, windows_voice=self._state.get("windows_voice", ""))
        if dlg.exec() == QDialog.Accepted:
            self._state["windows_voice"] = dlg.result_windows_voice()
            self._apply_windows_voice()
            self._save_state()
            cfg = dlg.result_config()
            cfg.save()
            self._agent.configure(cfg)
            self._set_status(f"LLM set — {cfg.mode} / {cfg.provider} / {cfg.model}")
            self._check_model()

    def _apply_windows_voice(self) -> None:
        """Tell the mouth which Windows voice to use: the saved name, or "" for Windows default. A state file
        from before this choice existed has no name, which is Windows default, as it always was."""
        name = self._state.get("windows_voice")
        self._mouth.windows_voice = name.strip() if isinstance(name, str) else ""

    # ── state ────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            with open(_STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
            if self._owns_window:       # a tab's own rectangle is not a window position
                g = self.geometry()
                self._state["geom"] = [g.x(), g.y(), g.width(), g.height()]
            self._state["voice_replies"] = self._btn_replies.isChecked()
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
        except OSError as exc:
            log.warning("assistant panel: state save failed: %s", exc)

    def _stop_voice(self) -> None:
        """Save the settings, close the mic and stop talking. Both frames end with this."""
        self._save_state()
        try:
            self._ears.shutdown()
            self._mouth.stop()
        except Exception:
            pass


class AssistantWindow(_AssistantBody, SCWindow):
    """The assistant HUD in its own window (assistant_app.py)."""

    # Cross-thread bridges: the agent (and tools) run on a worker thread,
    # so all GUI mutations go through queued signals.
    speakRequested = Signal(str)
    statusRequested = Signal(str)
    # (what, text) for whoever shows the held key: "listening", "released",
    # "heard", "reply", "note", "error". Nobody listens in this window.
    pttState = Signal(str, str)
    pttChanged = Signal()

    def __init__(self, base_dir: str, opacity: float = 0.95,
                 parent: Optional[QWidget] = None) -> None:
        SCWindow.__init__(self, title="Toolbox Assistant", width=520, height=340,
                          min_w=420, min_h=240, opacity=opacity,
                          accent=P.energy_cyan, parent=parent)
        self._build(base_dir)

    def closeEvent(self, event) -> None:
        self._stop_voice()
        super().closeEvent(event)


class AssistantPanel(_AssistantBody, QWidget):
    """The assistant HUD as one tab of the Toolbox Assistant window
    (toolbox_assistant_app.py), beside SuitMk2.

    The same HUD and the same agent, with no title bar of its own. ``mic``
    says whether this tab is the one listening when it is built; the window
    moves the microphone afterwards with mic_take / mic_release.
    """

    speakRequested = Signal(str)
    statusRequested = Signal(str)
    # (what, text): "listening", "released", "heard", "reply", "note", "error".
    # The window shows it on screen (assistant/ptt_overlay.py), because the
    # pilot holding the key is in the game, not looking at this tab.
    pttState = Signal(str, str)
    # the key or the mic mode changed: the window checks the two tabs' keys again
    pttChanged = Signal()

    _owns_window = False

    def __init__(self, base_dir: str, parent: Optional[QWidget] = None,
                 mic: bool = True) -> None:
        QWidget.__init__(self, parent)
        self._mic_mine = bool(mic)
        self.content_layout = QVBoxLayout(self)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        self._build(base_dir, chrome=False)

    def shutdown(self) -> None:
        """Launcher quit, or the window closing for good: what AssistantWindow
        does in closeEvent, plus the worker subprocesses the agent started
        (assistant_app.py stops those itself after its event loop ends)."""
        self._stop_voice()
        from .worker_pool import shutdown_all
        shutdown_all()
