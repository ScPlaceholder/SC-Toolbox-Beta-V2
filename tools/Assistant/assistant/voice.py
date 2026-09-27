"""Ears + Mouth for the toolbox assistant.

Adapted from the Starmap tool's proven voice subsystem
(skills/Starmap/starmap/voice/ears.py + mouth.py) — same capture
pipeline (sounddevice callback + faster-whisper on a worker thread,
silence-gap finalising), same SAPI TTS speak queue. Every dependency
is optional; without them the panel degrades to a disabled ears button.
"""
from __future__ import annotations

import os
import subprocess
import sys
import logging
import threading
import time
from queue import Queue
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

_SAMPLE_RATE = 16000
_log = logging.getLogger(__name__)
_VOICE_RMS = 0.012          # above this counts as speech
_MIN_VOICE_MS = 300         # shorter utterances are discarded
_GAP_MS = 900               # silence gap that ends an utterance
_MAX_UTTERANCE_MS = 12000   # hard cap

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


def _whisper_cpu_threads() -> int:
    """How many CPU threads to give ctranslate2. It is NOT the default, and that
    is the whole point.

    faster-whisper's cpu_threads defaults to 0, which ctranslate2 documents as
    "4 by default" -- four threads, whatever the machine. Measured 2026-09-27 on
    a 20-core / 28-thread i7-14700F, small.en int8, 28 SC command utterances,
    3 repeats, two independent passes:

        4 threads (today)            1.938 / 1.834 s per utterance
        16 threads + no timestamps   1.507 / 1.395   ->  -22.3% and -23.9%

    Two passes because this machine drifts: one repeated config moved 26% over an
    evening, so a single ordering cannot separate "faster config" from "ran at a
    quieter moment". The RATIO replicated across passes; the absolute number did
    not, so only the ratio is claimed here.

    Capped at 16 rather than the core count because more was not better --
    20 threads measured WORSE than 16 and 28 was no better than 4. Past the
    P-core threads the work spreads onto E-cores and the slowest thread sets the
    pace. min(16, cpu_count) also leaves a 4-core laptop at 4.

    Accuracy cost: none that reaches a command. Raising 4 -> 16 changed the
    transcript on 2 of 28 clips, both already-wrong ship names ("Orison" ->
    "Orazon"), and all 14 command phrases stayed exact at both settings.
    """
    return max(4, min(16, os.cpu_count() or 4))

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
      * always - no key; the mic stays open and every silence-gapped
        utterance is transcribed as it happens (ported from the Starmap).
    """

    listeningChanged = Signal(bool)
    statusChanged = Signal(str)
    transcript = Signal(str)
    needsInstall = Signal(list)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        # Diagnostics (2026-09-26): statuses were UI-only, so an in-game "it never heard me" left no trace.
        self.statusChanged.connect(lambda s: _log.info("ears status: %s", s))
        self.transcript.connect(lambda s: _log.info("ears heard: %r", s))
        self._last_trigger = None
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
        # Whisper loads once per process (~2.1s measured 2026-09-26: 0.25s import +
        # 1.81s small.en int8, warm cache — a cold first run also downloads ~250MB).
        # It used to load on the FIRST utterance, so J paid it mid-sentence. The lock
        # matters because arm() now preloads on a worker while _transcribe may call
        # _get_model on its own thread: without it both see None and build two models.
        self._model_lock = threading.Lock()
        self._preload = None
        self._lock = threading.Lock()
        self._np = None
        self._pulse = None          # shared.voice_pulse.CapturePulse while the mic is open

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
        self._mode = mode if mode in ("push", "toggle", "always") else "push"

    def set_model(self, name: str) -> None:
        self._model_name = name or "small.en"
        self._model = None
        # Drop the preload handle too, or _preload_model() sees a finished thread,
        # believes a preload is already in flight, and never loads the NEW model
        # ahead of time. Clearing _model alone would silently disable the preload
        # for the rest of the session the first time the player switches models.
        self._preload = None

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
        # After the deps check (faster_whisper must be importable) and before any
        # mode branch, so every path gets the head start — not just always-on.
        self._preload_model()
        if self._mode == "always":
            self._armed = True
            self.statusChanged.emit("ears on (always on)")
            QTimer.singleShot(0, self._begin)
            return True
        if self._binding is None:
            self.statusChanged.emit("no mic key set — click Set Mic Key and press the key to hold while you talk")
            return False
        from .voice_input import HotkeyMonitor
        if self._monitor is None:
            self._monitor = HotkeyMonitor(self)
            self._monitor.triggered.connect(self._on_trigger)
        ok = self._monitor.start(self._binding)
        if not ok:
            # ⛔ 2026-09-26: this used to emit "ears armed (...)" and `return ok`, so the
            # status line — and the log line fed from it — said ARMED whatever start()
            # returned. logs/assistant.crash.log holds "ears status: ears armed (z, hold to
            # talk)" for a session in which no key edge was ever seen, and that sentence was
            # worth nothing: it was printed without consulting the result. The claim now
            # follows the evidence, and self._armed is only set when the trigger is watched.
            self._armed = False
            self.statusChanged.emit(
                "mic key %s could not be watched - see logs/assistant.crash.log"
                % self._binding.describe())
            return False
        self._armed = True
        self.statusChanged.emit("ears armed (%s, %s)" % (
            self._binding.describe(), "hold to talk" if self._mode == "push" else "toggle"))
        return True

    def disarm(self) -> None:
        was_armed = self._armed
        self._armed = False
        if self._monitor is not None:
            self._monitor.stop()
        if self._recording:
            self._abort_recording("disarmed")
        if was_armed:                            # keep a failed arm()'s reason on screen
            self.statusChanged.emit("ears off")

    # ── trigger / capture ────────────────────────────────────────────────
    def _on_trigger(self, pressed: bool) -> None:
        if pressed != getattr(self, "_last_trigger", None):      # edges only, not key auto-repeat
            _log.info("ears: key %s", "down" if pressed else "up")
            self._last_trigger = pressed
        if self._mode == "always":
            return                               # mic never closes; the key is moot
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
            # The player's chosen mic (launcher Settings > Microphone), else the Windows default.
            try:
                from shared.mic import stream_device
                _dev = stream_device(refresh=True)
            except Exception as exc:
                # Broad on purpose (the ears must still open on the Windows default), but NOT silent: this is
                # the player's chosen microphone being dropped. Swallowed, it is indistinguishable from the
                # right mic simply hearing nothing.
                _log.warning("ears: could not resolve the chosen microphone (%s); "
                             "opening the Windows default instead", exc)
                _dev = None
            # Armed BEFORE the stream exists, so the very first block has somewhere to be
            # counted. Its own import is guarded: a missing shared/ must cost diagnostics,
            # never the microphone.
            try:
                from shared.voice_pulse import CapturePulse
                self._pulse = CapturePulse(threshold=_VOICE_RMS)
            except Exception as exc:
                self._pulse = None
                _log.warning("ears: capture pulse unavailable (%s: %s); the mic still "
                             "opens, but a session that hears nothing will leave no "
                             "evidence of why", type(exc).__name__, exc)
            self._stream = sd.InputStream(
                samplerate=_SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=1600, callback=self._audio_cb, device=_dev)
            self._stream.start()
            try:
                _log.info("ears: listening on input %r (mode %s)",
                          sd.query_devices(_dev if _dev is not None else sd.default.device[0])["name"],
                          self._mode)
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
            rms = None
            try:
                self._last_rms = rms = float(self._np.sqrt(self._np.mean(indata ** 2)))
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
            if self._pulse is not None:
                # rms stays None when the measurement above failed, so the pulse can
                # report UNMEASURED rather than fold a failure in as a 0.0 that is
                # indistinguishable from a silent room.
                self._pulse.note(rms, status)

    def _watch_silence(self) -> None:
        if not self._recording:
            self._tick.stop()
            return
        if self._pulse is not None:
            line = self._pulse.tick(voice_ms=self._voice_ms)
            if line:
                _log.info("%s", line)
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

    def _restart_if_armed(self) -> None:
        if self._armed and self._mode == "always" and not self._recording:
            self._begin()

    def _abort_recording(self, reason: str = "closed") -> None:
        self._tick.stop()
        if self._pulse is not None:
            # Unconditional, and this is the line the owner's 2026-09-26 session needed:
            # the mic was held open 16.4 s and closed without the gate ever firing, so
            # every existing log line was skipped and the session produced nothing at all
            # between "listening..." and "ears off". A close is the last moment anything
            # can be said, so something is always said.
            _log.info("%s", self._pulse.closing(reason, voice_ms=self._voice_ms))
            self._pulse = None
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception as exc:
            # Teardown of a stream that is being dropped either way — but a close that
            # throws can leak the device and make the NEXT arm fail, so it gets a line.
            _log.warning("ears: could not close the input stream cleanly: %s: %s",
                         type(exc).__name__, exc)
        self._stream = None
        self._recording = False
        self.listeningChanged.emit(False)

    def _finish(self) -> None:
        voice_ms = self._voice_ms
        pcm = self._pcm()
        try:
            import numpy as _np
            n = 0 if pcm is None else len(pcm) // 1600 * 1600
            peak = float(_np.sqrt((pcm[:n].reshape(-1, 1600) ** 2).mean(axis=1)).max()) if n else 0.0
            _log.info("ears: attempt ended: %d ms of speech, loudest block %.4f rms (speech line %.3f)",
                      voice_ms, peak, _VOICE_RMS)
        except Exception as exc:
            # Broad on purpose, and no longer silent. This is the "was the mic actually hearing anything"
            # diagnostic; _abort_recording() on the next line is what closes the stream, and _finish() runs
            # from a QTimer slot, so an exception escaping here would skip the teardown and leave the ears
            # stuck open with self._recording True. The arithmetic can raise ImportError / ValueError /
            # TypeError; the handler covers the rest too, but it reports instead of swallowing.
            _log.warning("ears: could not measure the captured audio (%s); "
                         "loudness unknown for this attempt", exc)
        self._abort_recording("attempt ended")
        if self._mode == "always" and self._armed:
            # keep the mic open; a disarm before the shot fires wins
            QTimer.singleShot(0, self._restart_if_armed)
        if pcm is None or voice_ms < _MIN_VOICE_MS:
            self.statusChanged.emit("heard nothing")
            return
        self.statusChanged.emit("transcribing...")
        threading.Thread(target=self._transcribe, args=(pcm,),
                         daemon=True, name="WhisperTranscribe").start()

    def _preload_model(self) -> None:
        """Load whisper off the hot path, so the first utterance does not pay for it.

        Deliberately fire-and-forget: a preload that fails must NOT stop the ears
        arming, because _get_model() will simply load it later the old way. The only
        thing lost is the head start, and the warning says so rather than failing.
        """
        if self._model is not None or self._preload is not None:
            return

        def _work():
            try:
                self._get_model()
            except Exception as exc:
                _log.warning("ears: whisper preload failed (%s: %s); the first "
                             "utterance will load it instead", type(exc).__name__, exc)

        self._preload = threading.Thread(target=_work, daemon=True,
                                         name="WhisperPreload")
        self._preload.start()

    def _get_model(self):
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                self.statusChanged.emit(
                    "loading whisper model '%s' (first run downloads it)..."
                    % self._model_name)
                self._model = WhisperModel(self._model_name, device="cpu",
                                           compute_type="int8",
                                           cpu_threads=_whisper_cpu_threads())
            return self._model

    def _transcribe(self, pcm) -> None:
        try:
            model = self._get_model()
            # without_timestamps=True: the segment timestamps are decoded tokens
            # and nothing here reads them -- the caller joins s.text and throws
            # the rest away. Measured 2026-09-27: -17% per utterance, and the
            # transcript moved on 1 of 28 clips, a capitalisation.
            segments, _info = model.transcribe(pcm, language="en", beam_size=5,
                                              initial_prompt=_WHISPER_PROMPT,
                                              without_timestamps=True)
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
