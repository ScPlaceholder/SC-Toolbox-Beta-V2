"""EarsController — mic + FastWhisper behind a trigger binding.

Modes:
  * toggle (default) — press the trigger once to open the ears, speak, and
    a silence gap auto-finalises the utterance and transcribes it; press
    again any time to close the ears.
  * push-to-talk — ears open only while the trigger is held; release
    transcribes.

Capture runs on the sounddevice callback thread; transcription on a
daemon worker; everything reaches the GUI through Qt signals. The
WhisperModel is created lazily on first use (the first run downloads the
model, which can take a while — the status signal says so).
"""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .input_devices import HotkeyMonitor, InputBinding

_SAMPLE_RATE = 16000
_VOICE_RMS = 0.012          # above this counts as speech
_MIN_VOICE_MS = 300         # utterances shorter than this are discarded
_GAP_MS = 900               # silence gap that ends an utterance
_MAX_UTTERANCE_MS = 12000   # hard cap, keeps stray noise from running forever


class EarsController(QObject):
    listeningChanged = Signal(bool)
    statusChanged = Signal(str)
    transcript = Signal(str)
    needsInstall = Signal(list)      # missing pip packages

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._binding: Optional[InputBinding] = None
        self._mode = "toggle"                    # toggle | push
        self._model_name = "small.en"
        self._monitor = HotkeyMonitor(self)
        self._monitor.triggered.connect(self._on_trigger)
        self._stream = None
        self._frames = []
        self._last_rms = 0.0
        self._voice_ms = 0
        self._quiet_ms = 0
        self._recording = False
        self._model = None
        self._lock = threading.Lock()

        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._watch_silence)

    # ── config ─────────────────────────────────────────────────────────────
    def binding(self) -> Optional[InputBinding]:
        return self._binding

    def set_binding(self, binding: Optional[InputBinding]) -> None:
        self._binding = binding
        if self.armed():
            self.disarm()
            self.arm()

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = "push" if mode == "push" else "toggle"

    def set_model(self, name: str) -> None:
        self._model_name = name or "small.en"
        self._model = None                       # force reload at next use

    def armed(self) -> bool:
        return self._monitor._binding is not None

    def recording(self) -> bool:
        return self._recording

    # ── arming ─────────────────────────────────────────────────────────────
    def arm(self) -> bool:
        """Attach the trigger. Returns False (with needsInstall) if the
        optional dependencies are missing."""
        from . import missing_deps
        missing = missing_deps()
        if missing:
            self.needsInstall.emit(missing)
            return False
        if self._binding is None:
            self.statusChanged.emit("no trigger set — right-click the ears button")
            return False
        ok = self._monitor.start(self._binding)
        self.statusChanged.emit(
            "ears armed (%s, %s)" % (self._binding.describe(), self._mode))
        return ok

    def disarm(self) -> None:
        self._monitor.stop()
        if self._recording:
            self._abort_recording()
        self.statusChanged.emit("ears off")

    # ── trigger / capture ─────────────────────────────────────────────────
    def _on_trigger(self, pressed: bool) -> None:
        if self._mode == "push":
            if pressed:
                self._begin()
            else:
                self._finish()
        else:
            if not pressed:
                return
            if self._recording:
                self._finish()
            else:
                self._begin()

    def _begin(self) -> None:
        if self._recording:
            return
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            self.statusChanged.emit("ears missing dependency: %s" % exc)
            return
        self._np = np
        self._frames = []
        self._last_rms = 0.0
        self._voice_ms = 0
        self._quiet_ms = 0
        try:
            self._stream = sd.InputStream(
                samplerate=_SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=1600, callback=self._audio_cb)
            self._stream.start()
        except Exception as exc:
            self.statusChanged.emit("mic error: %s" % exc)
            self._stream = None
            return
        self._recording = True
        self._tick.start()
        self.listeningChanged.emit(True)
        self.statusChanged.emit("listening...")

    def _audio_cb(self, indata, frames, time_info, status) -> None:
        with self._lock:
            self._frames.append(indata.copy())
            try:
                self._last_rms = float(self._np.sqrt(self._np.mean(indata ** 2)))
            except Exception:
                self._last_rms = 0.0

    def _watch_silence(self) -> None:
        if not self._recording:
            self._tick.stop()
            return
        if self._last_rms >= _VOICE_RMS:
            self._voice_ms += 100
            self._quiet_ms = 0
        elif self._voice_ms >= _MIN_VOICE_MS:
            self._quiet_ms += 100
        if self._voice_ms >= _MAX_UTTERANCE_MS or \
                (self._voice_ms >= _MIN_VOICE_MS and self._quiet_ms >= _GAP_MS):
            self._finish()

    def _pcm(self):
        with self._lock:
            if not self._frames:
                return None
            pcm = self._np.concatenate(self._frames, axis=0)
            self._frames = []
        return pcm.reshape(-1)

    def _abort_recording(self) -> None:
        self._tick.stop()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        self._recording = False
        self.listeningChanged.emit(False)

    def _finish(self) -> None:
        voice_ms = self._voice_ms
        pcm = self._pcm()
        self._abort_recording()
        if pcm is None or voice_ms < _MIN_VOICE_MS:
            self.statusChanged.emit("heard nothing")
            return
        self.statusChanged.emit("transcribing...")
        threading.Thread(target=self._transcribe, args=(pcm,),
                         daemon=True, name="WhisperTranscribe").start()

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self.statusChanged.emit("loading whisper model '%s' (first run downloads it)..."
                                    % self._model_name)
            self._model = WhisperModel(self._model_name, device="cpu",
                                       compute_type="int8")
        return self._model

    def _transcribe(self, pcm) -> None:
        try:
            model = self._get_model()
            segments, _info = model.transcribe(pcm, language="en", beam_size=5)
            text = " ".join(s.text for s in segments).strip()
            if text:
                self.transcript.emit(text)
            else:
                self.statusChanged.emit("heard nothing")
        except Exception as exc:
            self.statusChanged.emit("ears error: %s" % exc)

    def shutdown(self) -> None:
        self.disarm()
        self._monitor.stop()
