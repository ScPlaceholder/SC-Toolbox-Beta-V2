"""Ears + Mouth for the toolbox assistant.

Adapted from the Starmap tool's proven voice subsystem
(skills/Starmap/starmap/voice/ears.py + mouth.py) — same capture
pipeline (sounddevice callback + faster-whisper on a worker thread,
silence-gap finalising), same SAPI TTS speak queue. Every dependency
is optional; without them the panel degrades to a disabled ears button.
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
import logging
import threading
import time
from queue import Queue
from typing import Callable, List, Optional, Tuple

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

# ── the assistant hearing itself ─────────────────────────────────────────────
# Owner's screenshot, 2026-09-27, "Always on" + "Voice Replies":
#
#     You: Want me to open Starmouth? Say yes or not. Yes. Yes.
#     AI:  Okay, I won't open Starmap.
#
# The first NINE words of that "You:" line are the assistant's OWN prompt, spoken
# through the speakers and picked back up by the open mic, with the user's real
# "Yes. Yes." appended to the same utterance. logic.classify_confirmation lets any
# negative marker win, so the mistranscribed "no" -> "not" read as a refusal and the
# assistant refused an action the user had just approved twice.
#
# Two defences below, and the second is not decoration. See SpeechGate's docstring
# for why the first one cannot be trusted on its own.
_SPEAK_LEAD_MS = 400        # queue put -> first sample: synthesis, buffering, PortAudio
_SPEAK_TAIL_MS = 800        # the speakers are still sounding after the last sample
_SPEAK_MIN_MS = 800         # even "Done." must not reopen the mic instantly
_SPEAK_MAX_MS = 30000       # one line can never deafen the ears longer than the TTS timeout
_TTS_WORDS_PER_S = 2.4      # ~145 wpm; deliberately SLOW, so the estimate over-covers

_ECHO_TTL_S = 30.0          # after this, a spoken line no longer explains an utterance
_ECHO_REMEMBER = 4          # how many recent lines to hold
_ECHO_MIN_TOKENS = 4        # a shorter "match" than this is coincidence, not an echo
_ECHO_FUZZ = 0.6            # difflib ratio at which a misheard word counts as the same word

_WORD_RE = re.compile(r"[^\w']+")


def _norm_token(raw: str) -> str:
    return _WORD_RE.sub("", raw).lower()


def _words(text: str) -> Tuple[List[str], List[str]]:
    """(raw words, normalised tokens) in step, so a remainder can be rebuilt with
    its original casing and punctuation intact."""
    raw, toks = [], []
    for w in (text or "").split():
        t = _norm_token(w)
        if t:
            raw.append(w)
            toks.append(t)
    return raw, toks


def _close(a: str, b: str) -> bool:
    """Whisper mishears the words it is hearing through speakers: "Starmap" came
    back "Starmouth" and "no" came back "not" in the same sentence. Exact token
    equality would have matched neither, and the "not" is what inverted the answer."""
    if a == b:
        return True
    if len(a) < 2 or len(b) < 2:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= _ECHO_FUZZ


class SpeechGate:
    """The mouth is talking, so the ears must not listen.

    Time-based, and that compromise is worth naming rather than hiding: NEITHER mouth
    in this toolbox can be asked when it stopped. `Mouth.speak()` and
    `shared.character_voice.CharacterMouth.speak()` are both a `Queue.put` that returns
    in microseconds -- before a single sample has played -- and neither exposes a
    "finished" signal or a busy flag. So there is nothing at the call site to time
    from, and this gate estimates instead: a lead-in, the line's length from its word
    count, and a tail for the speakers still sounding after the last sample.

    All three numbers can be wrong. A cold Piper model load delays the first sample by
    seconds; a list of ship names reads slower than 2.4 words a second. That is exactly
    why EchoFilter exists and is not optional -- this gate is the cheap defence that
    catches the common case, and the filter catches what leaks past it.

    Lines ADD instead of replacing: the mouth's queue plays them one after another, so
    two replies in a row are two windows end to end.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def estimate_ms(text: str) -> int:
        words = len((text or "").split())
        ms = _SPEAK_LEAD_MS + _SPEAK_TAIL_MS + (words / _TTS_WORDS_PER_S) * 1000.0
        return int(max(_SPEAK_MIN_MS, min(_SPEAK_MAX_MS, ms)))

    def note_speaking(self, text: str) -> int:
        """Shut the gate for this line. Returns the whole remaining window in ms."""
        ms = self.estimate_ms(text)
        with self._lock:
            base = max(self._clock(), self._until)
            self._until = base + ms / 1000.0
            return int((self._until - self._clock()) * 1000)

    def cancel(self) -> None:
        """Reopen now -- for a line that will never be spoken (speak() threw) or a
        mouth that was stopped. Never let a failure leave the ears deaf."""
        with self._lock:
            self._until = 0.0

    def active(self) -> bool:
        with self._lock:
            return self._clock() < self._until

    def remaining_ms(self) -> int:
        with self._lock:
            return max(0, int((self._until - self._clock()) * 1000))


class EchoFilter:
    """Second line of defence: an utterance that repeats what was just spoken.

    Independent of the gate on purpose. The gate depends on timing and timing is the
    unreliable part; this looks only at CONTENT, so it still catches an echo that
    arrived after the window closed.

    It STRIPS rather than discards wherever it can, because the screenshot's utterance
    was both things at once -- the assistant's question AND the user's answer in one
    string. Discarding the lot would lose the "Yes. Yes." and leave the user asked a
    question that never got an answer; matching the whole string would not have fired
    at all. So the echo is peeled off the front and the remainder goes on to the parser.

    Deliberately conservative. Eating a genuine "yes" would invert the decision in the
    other direction, which is the same bug wearing a hat -- so a match shorter than
    _ECHO_MIN_TOKENS words is treated as coincidence and nothing is stripped.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._spoken: List[Tuple[float, List[str]]] = []
        self._lock = threading.Lock()

    def note_spoken(self, text: str) -> None:
        _raw, toks = _words(text)
        if not toks:
            return
        with self._lock:
            self._spoken.append((self._clock(), toks))
            del self._spoken[:-_ECHO_REMEMBER]

    def clean(self, heard: str) -> Tuple[str, str]:
        """(what should reach the parser, why it was changed).

        The reason is "" when the utterance was left alone, so a caller can tell
        "nothing to do" from "I dropped your words" and say so."""
        raw, toks = _words(heard)
        if not toks:
            return heard, ""
        now = self._clock()
        with self._lock:
            recent = [t for (ts, t) in self._spoken if now - ts <= _ECHO_TTL_S]
        for spoken in reversed(recent):
            n = _echo_prefix_len(toks, spoken)
            if n < _ECHO_MIN_TOKENS:
                continue
            kept = " ".join(raw[n:]).strip()
            why = ("all %d word(s) were my own voice" % n if not kept
                   else "dropped %d word(s) of my own voice" % n)
            return kept, why
        return heard, ""


def _echo_prefix_len(heard: List[str], spoken: List[str]) -> int:
    """How many words at the START of *heard* the spoken line accounts for.

    difflib rather than a hand-rolled walk, because whisper both SUBSTITUTES
    ("Starmap" -> "Starmouth", "no" -> "not") and DROPS words when it is listening to
    a speaker, and a substitution in the middle must not end the match -- in the
    screenshot the very last echoed word was a substitution, and stopping there would
    have left "not" in front of the user's "Yes. Yes." and inverted the answer anyway.

    The opcodes are read with heard as `a` and spoken as `b`, so:

      equal    -> accounted for.
      insert   -> spoken words the mic never got; they consume no heard word.
      replace  -> walked word by word, absorbing only the ones that are the same word
                  misheard, then stopping. NOT all-or-nothing: in the screenshot the
                  final chunk was ["not", "yes", "yes"] against ["no"] -- my last
                  echoed word and the user's whole answer in ONE opcode. Refusing the
                  chunk left "not" in front of "Yes. Yes." and the answer still
                  inverted; taking the chunk would have eaten the answer.
      delete   -> heard words the line does not contain. THIS IS THE USER. Stop.
    """
    if not heard or not spoken:
        return 0
    sm = difflib.SequenceMatcher(None, heard, spoken, autojunk=False)
    n = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            n = i2
        elif tag == "insert":
            continue                      # spoken words missing from the transcript
        elif tag == "replace":
            h, s = heard[i1:i2], spoken[j1:j2]
            k = 0
            while k < len(h) and k < len(s) and _close(h[k], s[k]):
                k += 1
            n = i1 + k
            if k < len(h):
                break                     # the rest of this chunk is not mine
        else:                             # delete: the user's own words
            break
    return n


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
    #: True while the USER is audibly speaking, False when they stop. Distinct from
    #: listeningChanged, which says the MIC IS OPEN — in "always" mode that fires once
    #: at startup and never again, so anything driven off it would appear at launch and
    #: stay there forever. This one tracks voice activity.
    speakingChanged = Signal(bool)
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
        # Self-hearing (2026-09-27). The gate closes the always-open mic while the
        # mouth talks; the filter catches an echo that leaked past it. Both are armed
        # by note_speaking(), which the panel calls before it queues a line.
        self._gate = SpeechGate()
        self._echo = EchoFilter()
        self._deaf_blocks = 0
        self._speaking = False
        #: ⛔ A SECOND THRESHOLD, DELIBERATELY NOT THE TRANSCRIPTION ONE. J, 2026-09-27:
        #:   "have the option for users to reduce the mic sensitivity to spawn him but it
        #:    won't reduce the sensitivity to spawn the whisper".
        #:   A player who finds the companion twitchy raises THIS, and the ears go on
        #:   hearing exactly as well as before. `_VOICE_RMS` drives _voice_ms and therefore
        #:   WHEN AN UTTERANCE IS CAPTURED; this drives only speakingChanged.
        #: ⚠ Defaults EQUAL to _VOICE_RMS, so behaviour is unchanged until someone asks.
        #:   Raising it can only make the indicator quieter — it can never deafen the mic,
        #:   because nothing on the transcription path reads it. The test that matters is
        #:   the one that raises it to a level no speech passes and asserts _finish() still
        #:   fires on the same input.
        self._show_rms = _VOICE_RMS

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

    def show_sensitivity(self) -> float:
        """RMS above which the user counts as speaking FOR DISPLAY purposes."""
        return self._show_rms

    def set_show_sensitivity(self, rms: float) -> None:
        """Raise to make a speech indicator less twitchy. Cannot affect hearing.

        Clamped at _VOICE_RMS from below on purpose: lowering it beneath the
        transcription threshold would light the indicator for sound the ears will never
        act on, which is a worse lie than a twitchy penguin — it would show the player
        "I heard that" for audio that produced no utterance.
        """
        self._show_rms = max(_VOICE_RMS, float(rms))

    def _set_speaking(self, on: bool) -> None:
        """Emit only on a CHANGE. A per-tick emit would fire 10x a second and make any
        consumer's fade restart continuously."""
        if bool(on) == self._speaking:
            return
        self._speaking = bool(on)
        self.speakingChanged.emit(self._speaking)

    def set_model(self, name: str) -> None:
        self._model_name = name or "small.en"
        self._model = None
        # Drop the preload handle too, or _preload_model() sees a finished thread,
        # believes a preload is already in flight, and never loads the NEW model
        # ahead of time. Clearing _model alone would silently disable the preload
        # for the rest of the session the first time the player switches models.
        self._preload = None

    # ── the mouth ────────────────────────────────────────────────────────
    def note_speaking(self, text: str) -> None:
        """"I am about to say this." Call it BEFORE handing the line to the mouth.

        Arms both defences: the capture gate (always-on mic only) and the echo filter
        (every mode). Costs nothing but two timestamps, so it is safe to call even when
        Voice Replies is off -- though the panel does not, since nothing is spoken then.
        """
        if not (text or "").strip():
            return
        self._echo.note_spoken(text)
        ms = self._gate.note_speaking(text)
        if self._mode == "always":
            _log.info("ears: deaf for ~%d ms while I speak %d word(s)", ms, len(text.split()))

    def cancel_speaking(self) -> None:
        """The line will not be spoken after all (speak() threw, the mouth was
        stopped). Reopen the ears at once rather than serving out an estimate for
        audio that never played. The gate would also expire on its own -- it is a
        deadline, not a held lock, so nothing here can wedge the ears shut."""
        self._gate.cancel()

    def deaf(self) -> bool:
        """True while the always-open mic is being held shut for the mouth.

        Push-to-talk is deliberately NOT gated: holding the key is an explicit
        intent to be heard, and going deaf mid-hold would make hold-to-talk
        mysteriously dead whenever the assistant was mid-sentence. The echo filter
        still runs in push mode, so the content defence covers it either way."""
        return self._mode == "always" and self._gate.active()

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
            if self.deaf():
                # My own voice, arriving through the speakers. Dropped here rather
                # than filtered later so it can never be concatenated onto the
                # front of the user's next sentence -- which is exactly what the
                # 2026-09-27 screenshot was: one utterance, my question and their
                # answer, and the parser saw a single string.
                # _last_rms is zeroed so the silence watcher cannot keep counting
                # speech from the last block it did see.
                self._deaf_blocks += 1
                self._last_rms = 0.0
                return
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
        if self.deaf():
            # Hold the utterance at zero for as long as the mouth is talking, so the
            # silence gap cannot fire on a half-buffer and the mic resumes clean. The
            # stream stays open and the timer keeps running: the ears resume on the
            # tick after the gate expires, with nothing carried over.
            with self._lock:
                self._frames = []
            self._voice_ms = 0
            self._quiet_ms = 0
            self._last_rms = 0.0
            # The assistant is talking. Whatever the mic hears is ours, so the user is
            # by definition not speaking — a companion that danced here would be dancing
            # at my own voice, which is the bug the gate exists to stop, wearing a costume.
            self._set_speaking(False)
            return
        if self._deaf_blocks:
            _log.info("ears: listening again (dropped %d block(s) of my own voice)",
                      self._deaf_blocks)
            self._deaf_blocks = 0
        if self._pulse is not None:
            line = self._pulse.tick(voice_ms=self._voice_ms)
            if line:
                _log.info("%s", line)
        if self._last_rms >= _VOICE_RMS:
            self._voice_ms += 100
            self._quiet_ms = 0
        elif self._voice_ms >= _MIN_VOICE_MS:
            self._quiet_ms += 100
        # Display only, and on its OWN threshold — see _show_rms. Nothing above this
        # line reads it, which is what makes "less twitchy companion" unable to become
        # "deafer ears".
        if self._last_rms >= self._show_rms:
            self._set_speaking(True)
        elif self._quiet_ms >= _GAP_MS:
            self._set_speaking(False)
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
        self._set_speaking(False)
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
                # Second defence, and independent of the gate's timing: whatever the
                # mic caught, do not hand the parser words I just said. Runs in every
                # mode, because a leak is a leak.
                kept, why = self._echo.clean(text)
                if why:
                    _log.info("ears: %s -- heard %r, passing on %r", why, text, kept)
                if not kept:
                    self.statusChanged.emit("that was me — ignored")
                    return
                self.transcript.emit(kept)
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
