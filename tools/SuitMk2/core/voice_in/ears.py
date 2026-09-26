"""EarsController — mic + FastWhisper behind a trigger binding.

Modes:
  * toggle (default) — press the trigger once to open the ears, speak, and
    a silence gap auto-finalises the utterance and transcribes it; press
    again any time to close the ears.
  * push-to-talk — ears open only while the trigger is held; release
    transcribes. A pause while the key is held does NOT end it (the
    silence gap is toggle-only); only the release does, or the 12 s cap.

Capture runs on the sounddevice callback thread; transcription on a
daemon worker; everything reaches the GUI through Qt signals. The
WhisperModel is created lazily on first use (the first run downloads the
model, which can take a while — the status signal says so).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .input_devices import HotkeyMonitor, InputBinding

_log = logging.getLogger(__name__)
_SAMPLE_RATE = 16000
_VOICE_RMS = 0.012          # above this counts as speech
_MIN_VOICE_MS = 300         # utterances shorter than this are discarded
_GAP_MS = 900               # silence gap that ends an utterance (toggle mode only)
_MAX_UTTERANCE_MS = 12000   # hard cap, keeps stray noise from running forever

# Whisper hint (initial_prompt): the names and command words it should expect.
# Without it small.en heard "Montaigne" as "documentini", "Elah" as "Filla" and
# dropped it, "route to Pyro" as "Routed by room", "fun facts on" as "fun facts on it".
# The same text is used by the SuitMk2, Assistant and Starmap ears.
_WHISPER_PROMPT = (
    "Elah, Montaigne. Fun facts on. Fun facts off. What missions do I have? "
    "Navigate to Area 18. Set route to Port Tressler. Route to Pyro. Clear route. "
    "Zoom in. Zoom out. Back to galaxy. Take me home. Commodities. Market finder. "
    "Toggle the grocery list. Stop listening. Help. "
    "Best trade route for my Caterpillar. Open the Trade Hub.")


class EarsController(QObject):
    listeningChanged = Signal(bool)
    statusChanged = Signal(str)
    transcript = Signal(str)
    needsInstall = Signal(list)      # missing pip packages

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._binding: Optional[InputBinding] = None
        self._mode = "toggle"                    # toggle | push | always
        self._armed = False
        self._model_name = "small.en"
        self._monitor = HotkeyMonitor(self)
        self._monitor.triggered.connect(self._on_trigger)
        self._stream = None
        self._frames = []
        self._last_rms = 0.0
        self._voice_ms = 0
        self._quiet_ms = 0
        self._started = 0.0
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
        self._mode = mode if mode in ("push", "toggle", "always") else "toggle"

    def set_model(self, name: str) -> None:
        self._model_name = name or "small.en"
        self._model = None                       # force reload at next use

    def armed(self) -> bool:
        return self._armed

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
        if self._mode == "always":
            # no key: the mic stays open and each silence-gapped utterance is transcribed
            self._armed = True
            self.statusChanged.emit("ears on (always on)")
            QTimer.singleShot(0, self._begin)
            return True
        if self._binding is None:
            self.statusChanged.emit("no talk key set")
            return False
        ok = self._monitor.start(self._binding)
        self._armed = bool(ok)
        self.statusChanged.emit(
            "ears armed (%s, %s)" % (self._binding.describe(), self._mode))
        return ok

    def disarm(self) -> None:
        self._armed = False
        self._monitor.stop()
        if self._recording:
            self._abort_recording()
        self.statusChanged.emit("ears off")

    # ── trigger / capture ─────────────────────────────────────────────────
    def _on_trigger(self, pressed: bool) -> None:
        if self._mode == "always":
            return                               # mic never closes; the key is moot
        if self._mode == "push":
            if pressed:
                self._begin()                    # key auto-repeat: _begin ignores repeats
            elif self._recording:                # not after the 12 s cap already ended it
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
        self._started = time.monotonic()
        try:
            # The player's chosen mic (launcher Settings > Microphone), else the Windows default.
            try:
                from shared.mic import stream_device
                _dev = stream_device(refresh=False)
            except Exception as exc:
                # Broad on purpose (the ears must still open on the Windows default), but NOT silent: this is
                # the player's chosen microphone being dropped. Swallowed, it is indistinguishable from the
                # right mic simply hearing nothing.
                _log.warning("ears: could not resolve the chosen microphone (%s); "
                             "opening the Windows default instead", exc)
                _dev = None
            self._stream = sd.InputStream(
                samplerate=_SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=1600, callback=self._audio_cb, device=_dev)
            self._stream.start()
            try:
                _log.info("ears: listening on input %r", sd.query_devices(
                    _dev if _dev is not None else sd.default.device[0])["name"])
            except Exception as exc:
                # Broad on purpose, and no longer silent. The stream is ALREADY STARTED here; this block only
                # names the device for the log. Anything escaping it lands in the enclosing handler, which
                # reports "mic error" and sets self._stream = None - tearing down a working microphone because
                # a diagnostic failed. So it stays broad, and it says what went wrong.
                _log.warning("ears: listening, but could not name the input device: %s", exc)
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
            except Exception as exc:
                # Broad on purpose: this runs on the PortAudio callback thread, where a raised exception aborts
                # the stream outright. But it is NOT silent any more, and that matters more here than anywhere
                # else in the file: falling back to 0.0 makes a BROKEN loudness measurement look exactly like a
                # silent room, so the ears would report "heard nothing" forever and nothing would say why.
                # Logged once per controller (not per block) so the audio callback is not turned into a
                # log flood; the flag is never cleared, so one line per app run is the whole budget.
                self._last_rms = 0.0
                if not getattr(self, "_rms_fail_logged", False):
                    self._rms_fail_logged = True
                    _log.warning("ears: cannot measure input loudness (%s); every block now reads as "
                                 "silence, so the ears will report hearing nothing", exc)

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
            # held key: the release ends it, never a pause; 12 s cap on the
            # wall clock (timer ticks drift late under load)
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

    def _restart_if_armed(self) -> None:
        if self._armed and self._mode == "always" and not self._recording:
            self._begin()

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
        if self._mode == "always" and self._armed:
            QTimer.singleShot(0, self._restart_if_armed)    # a disarm before it fires wins
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
            segments, _info = model.transcribe(pcm, language="en", beam_size=5,
                                              initial_prompt=_WHISPER_PROMPT)
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
