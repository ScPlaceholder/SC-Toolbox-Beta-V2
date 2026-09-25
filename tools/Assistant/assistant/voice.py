"""Ears + Mouth for the toolbox assistant.

Adapted from the Starmap tool's proven voice subsystem
(skills/Starmap/starmap/voice/ears.py + mouth.py) — same capture
pipeline (sounddevice callback + faster-whisper on a worker thread,
silence-gap finalising), same SAPI TTS speak queue. Every dependency
is optional; without them the panel degrades to a disabled ears button.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from queue import Queue
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

_SAMPLE_RATE = 16000
_VOICE_RMS = 0.012          # above this counts as speech
_MIN_VOICE_MS = 300         # shorter utterances are discarded
_GAP_MS = 900               # silence gap that ends an utterance
_MAX_UTTERANCE_MS = 12000   # hard cap

_TTS_RATE = 2               # SAPI rate -10..10
_TTS_VOLUME = 90
_TTS_TIMEOUT = 30


class EarsController(QObject):
    """Mic + FastWhisper behind a trigger binding.

    Modes:
      * push (default) - hold-to-talk: the mic is open while the key is
        held and releasing it transcribes. A pause mid-sentence does not
        end it; only the release does, or the 12 s cap.
      * toggle - press once to open, a 0.9 s silence gap (or a second
        press) ends the utterance.
    """

    listeningChanged = Signal(bool)
    statusChanged = Signal(str)
    transcript = Signal(str)
    needsInstall = Signal(list)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._binding = None
        self._monitor = None
        self._armed = False
        self._mode = "push"                      # push (hold-to-talk) | toggle
        self._model_name = "small.en"
        self._stream = None
        self._frames = []
        self._last_rms = 0.0
        self._voice_ms = 0
        self._quiet_ms = 0
        self._started = 0.0
        self._recording = False
        self._model = None
        self._lock = threading.Lock()
        self._np = None

        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._watch_silence)

    # ── config ───────────────────────────────────────────────────────────
    def set_binding(self, binding) -> bool:
        """Returns False (binding not set) for a refused binding such as
        left or right click."""
        why = binding.refused() if binding is not None and hasattr(binding, "refused") else ""
        if why:
            self.statusChanged.emit(why)
            return False
        self._binding = binding
        if self._armed:
            self.disarm()
            self.arm()
        return True

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = "toggle" if mode == "toggle" else "push"

    def set_model(self, name: str) -> None:
        self._model_name = name or "small.en"
        self._model = None

    def armed(self) -> bool:
        return self._armed

    def recording(self) -> bool:
        return self._recording

    def binding(self):
        return self._binding

    # ── arming ───────────────────────────────────────────────────────────
    def arm(self) -> bool:
        from . import missing_voice_deps
        missing = missing_voice_deps()
        if missing:
            self.needsInstall.emit(missing)
            return False
        if self._binding is None:
            self.statusChanged.emit("no mic key set — click Set Mic Key and press the key to hold while you talk")
            return False
        from .voice_input import HotkeyMonitor
        if self._monitor is None:
            self._monitor = HotkeyMonitor(self)
            self._monitor.triggered.connect(self._on_trigger)
        self._armed = True
        ok = self._monitor.start(self._binding)
        self.statusChanged.emit("ears armed (%s, %s)" % (
            self._binding.describe(), "hold to talk" if self._mode == "push" else "toggle"))
        return ok

    def disarm(self) -> None:
        was_armed = self._armed
        self._armed = False
        if self._monitor is not None:
            self._monitor.stop()
        if self._recording:
            self._abort_recording()
        if was_armed:                            # keep a failed arm()'s reason on screen
            self.statusChanged.emit("ears off")

    # ── trigger / capture ────────────────────────────────────────────────
    def _on_trigger(self, pressed: bool) -> None:
        if self._mode == "push":
            if pressed:
                self._begin()                    # key auto-repeat: _begin ignores repeats
            elif self._recording:
                self._finish()
            return
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
        self._started = time.monotonic()
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
        if self._mode == "push":
            # hold-to-talk: the release ends it, never a pause; 12 s cap
            # (wall clock: timer ticks drift late under load)
            if (time.monotonic() - self._started) * 1000 >= _MAX_UTTERANCE_MS:
                self._finish()
            return
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
            self.statusChanged.emit(
                "loading whisper model '%s' (first run downloads it)..."
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
        if self._monitor is not None:
            self._monitor.stop()


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class Mouth:
    """SAPI text-to-speech over a serialised queue (PowerShell)."""

    def __init__(self) -> None:
        self._q: "Queue[Optional[str]]" = Queue()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True,
                         name="MouthTTS").start()

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._q.put(text)

    def stop(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except Exception:
            pass
        try:
            self._q.put(None)
        except Exception:
            pass
        self._kill()

    def _run(self) -> None:
        while True:
            try:
                text = self._q.get()
            except Exception:
                continue
            if text is None:
                return
            try:
                cmd = (
                    "Add-Type -AssemblyName System.Speech; "
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    "$s.Rate = %d; $s.Volume = %d; "
                    "$s.Speak(%s); "
                    "$s.Dispose()"
                    % (_TTS_RATE, _TTS_VOLUME, _ps_quote(text))
                )
                with self._lock:
                    self._proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-NonInteractive",
                         "-Command", cmd],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._proc.wait(timeout=_TTS_TIMEOUT)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._proc = None

    def _kill(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
