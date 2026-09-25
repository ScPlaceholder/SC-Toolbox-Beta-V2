"""AssistantPanel — the always-on-top HUD window the user talks to.

Layout:

  [ title bar                                             ✕ ]
  [ Ears ] [ Set Mic Key ] [ Voice Replies ] [ Settings… ]
  <no-mic-key hint, only while no key is set>
  You:  <last thing the ears heard>
  AI:   <the assistant's reply, word-wrapped>
  <status line>

The agent runs blocking LLM calls on a worker thread; everything reaches
the GUI through Qt signals. The ears/mouth/speak pipeline is optional-
dependency gated exactly like the Starmap voice bar.
"""
from __future__ import annotations

import json
import logging
import os
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
        super().__init__(title="AI Assistant", width=520, height=340,
                         min_w=420, min_h=240, opacity=opacity,
                         accent=P.energy_cyan, parent=parent)
        self._base_dir = base_dir
        self._state = self._load_state()
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
        self._mouth = Mouth()
        self._ears = EarsController(self)
        # hold-to-talk by default; "mic_mode": "toggle" in the state file opts out
        self._ears.set_mode(self._state.get("mic_mode", "push"))
        self._ears.statusChanged.connect(self._set_status)
        self._ears.transcript.connect(self._on_transcript)
        self._ears.needsInstall.connect(self._on_needs_install)

        # ── chrome ───────────────────────────────────────────────────────
        tb = SCTitleBar(self, title="AI ASSISTANT", icon_text="🤖",
                        accent_color=P.energy_cyan, show_minimize=True)
        tb.minimize_clicked.connect(self.showMinimized)
        tb.close_clicked.connect(self.close)
        self.content_layout.addWidget(tb)

        row = QHBoxLayout()
        row.setContentsMargins(10, 6, 10, 2)
        row.setSpacing(8)

        self._btn_ears = QPushButton("Ears")
        self._btn_ears.setCheckable(True)
        self._btn_ears.setStyleSheet(_btn_ss())
        self._btn_ears.toggled.connect(self._on_ears_toggled)
        row.addWidget(self._btn_ears)

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

        self._lbl_heard = QLabel("You: —")
        self._lbl_heard.setWordWrap(True)
        self._lbl_heard.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent; padding: 0 12px;")
        self.content_layout.addWidget(self._lbl_heard)

        self._lbl_reply = QLabel("AI: set a mic key, arm the ears, then hold "
                                 "the key and talk to me. Ask for the best "
                                 "cargo route for your ship.")
        self._lbl_reply.setWordWrap(True)
        self._lbl_reply.setStyleSheet(
            f"color: {P.fg_bright}; font-family: Consolas; font-size: 10pt; "
            f"background: transparent; padding: 0 12px;")
        self.content_layout.addWidget(self._lbl_reply, 1)

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

    # ── voice plumbing ───────────────────────────────────────────────────
    def _speak(self, text: str) -> None:
        if self._btn_replies.isChecked() and Mouth.available():
            self._mouth.speak(text)
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
        self._set_status("thinking…")
        self._run_turn(text)


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
        if self._btn_ears.isChecked():
            self._ears.disarm()
            self._ears.arm()

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
        if b is None:
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
