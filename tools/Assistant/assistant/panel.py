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

This window is the toolbox's ONE microphone (J, 2026-10-04). The Star Map
used to have its own ears and voice bar; they are gone, and what is said for
the map is relayed to it from here (starmap_bridge.py, the starmap_command
tool). The Star Map's saved mic settings are folded in once at start-up.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar

from .agent import AssistantAgent
from .config import LLMConfig
from .tools import ToolContext
from .voice import EarsController, Mouth
from .voice_input import InputBinding, KeyCaptureDialog

log = logging.getLogger(__name__)

_STATE_PATH = os.path.join(os.path.expanduser("~"), ".sctoolbox",
                           "assistant_panel.json")

#: Said to close an always-open mic. It was the Star Map's "ears off" command;
#: the mic is this window's now, so the command is handled here and never
#: reaches the agent. There is no "off" (J 2026-09-26): it drops to push-to-talk.
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


class _SettingsDialog(QDialog):
    """LLM endpoint editor — the plug-in point, in GUI form."""

    def __init__(self, cfg: LLMConfig, parent=None) -> None:
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

        btns = QDialogButtonBox(QDialogButtonBox.Save |
                                QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addRow(btns)

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


class AssistantWindow(SCWindow):
    """The assistant HUD."""

    # Cross-thread bridges: the agent (and tools) run on a worker thread,
    # so all GUI mutations go through queued signals.
    speakRequested = Signal(str)
    statusRequested = Signal(str)

    def __init__(self, base_dir: str, opacity: float = 0.95,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(title="Toolbox Assistant", width=520, height=340,
                         min_w=420, min_h=240, opacity=opacity,
                         accent=P.energy_cyan, parent=parent)
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
        self._ears = EarsController(self)
        # Ears are always on (J 2026-09-26); the player picks push-to-talk
        # (default) or always on. An old saved "toggle" becomes push-to-talk.
        mode = self._state.get("mic_mode", "push")
        self._ears.set_mode(mode if mode in ("push", "always") else "push")
        if self._state.get("whisper_model"):       # carried over from the Star Map, if it had one
            self._ears.set_model(str(self._state["whisper_model"]))
        self._ears.statusChanged.connect(self._set_status)
        self._ears.transcript.connect(self._on_transcript)
        self._ears.needsInstall.connect(self._on_needs_install)

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

        geom = self._state.get("geom")
        if geom and len(geom) == 4:
            self.setGeometry(*geom)
        self._restore_binding()
        self._ensure_ears()

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

    # ── voice plumbing ───────────────────────────────────────────────────
    def _speak(self, text: str) -> None:
        if self._btn_replies.isChecked() and Mouth.available():
            # Warn the ears BEFORE the line is queued: with the mic always open they
            # hear the speakers, and on 2026-09-27 "Say yes or no" came back as the
            # user's own next utterance, mistranscribed to "or not" — so the yes/no
            # parse read a refusal and refused what had just been approved twice.
            self._ears.note_speaking(text)
            try:
                self._mouth.speak(text)
            except Exception:
                self._ears.cancel_speaking()   # never spoken: do not sit there deaf
                raise
        self._lbl_reply.setText("AI: " + text)
        self._lbl_reply.setToolTip(text)

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
        if _STOP_LISTENING.match(text or ""):
            self._stop_listening()
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
        self._set_status("ready")

    def _on_failed(self, err: str) -> None:
        self._lbl_reply.setText("AI: error — " + err)
        self._set_status("error")

    def _on_needs_install(self, packages: list) -> None:
        self._btn_ears.setChecked(False)
        self._set_status("pip install " + " ".join(packages))

    def _on_ears_toggled(self, on: bool) -> None:
        if on:
            if not self._ears.arm():
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

    def _restore_binding(self) -> None:
        raw = self._state.get("binding")
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
            self._lbl_mic.setText("")
            self._lbl_mic.setVisible(False)

    # ── settings ─────────────────────────────────────────────────────────
    def _edit_settings(self) -> None:
        dlg = _SettingsDialog(LLMConfig.load(), self)
        if dlg.exec() == QDialog.Accepted:
            cfg = dlg.result_config()
            cfg.save()
            self._agent.configure(cfg)
            self._set_status(f"LLM set — {cfg.mode} / {cfg.provider} / {cfg.model}")

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
            g = self.geometry()
            self._state["geom"] = [g.x(), g.y(), g.width(), g.height()]
            self._state["voice_replies"] = self._btn_replies.isChecked()
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
        except OSError as exc:
            log.warning("assistant panel: state save failed: %s", exc)

    def closeEvent(self, event) -> None:
        self._save_state()
        try:
            self._ears.shutdown()
            self._mouth.stop()
        except Exception:
            pass
        super().closeEvent(event)
