"""voice_pulse.py - positive evidence that the ears are receiving audio.

WHAT WAS WRONG (2026-09-26). Every ears stack in this toolbox logged only the END of an
utterance ("ears: attempt ended ... loudest block %.4f rms"). That line is written by
`_finish()`, and `_finish()` is only reached when the silence gate has already decided an
utterance happened. So a session in which the gate never fired wrote NOTHING, and one empty
log was the shared symptom of five unrelated faults:

    1. the capture never opened          (no stream: a "mic error" line does exist for this)
    2. the stream opened but the OS delivered no callbacks at all   (dead/unplugged device)
    3. blocks arrive, the room really is quiet                      (nobody spoke)
    4. blocks arrive loud, but the speech line is set above them     (gate mis-tuned)
    5. blocks arrive loud, the gate crosses, and the finaliser fails (a bug downstream)

Those need five different fixes and the log could not tell them apart. The owner's Star Map
session on 2026-09-26 20:56 held the mic open for 16.4 s and produced exactly zero lines
between "listening..." and "ears off".

WHAT THIS CHANGES. A `CapturePulse` is fed one `note()` per audio block from the capture
callback, and `tick()`ed from the controller's existing 100 ms QTimer. It returns a line at
most once per `window_s` while the mic is open, and nothing at all when it is closed. The
five cases above now read as five DIFFERENT sentences, because the line always carries the
three facts that separate them: how many blocks arrived, how loud the loudest one was, and
what number it is being compared against.

★ THE PULSE IS DRIVEN BY THE TIMER, NOT BY THE AUDIO CALLBACK, and that is the whole design.
  A pulse emitted from `note()` can only ever say "audio is arriving" - it is structurally
  incapable of reporting case 2, which is the case that costs the most to diagnose. Driving
  it from the tick means the zero-block line is the one branch guaranteed to be reachable.

★★ AN ABSENCE IS NOT A MEASUREMENT. `note(rms=None)` records a block whose loudness could
  not be computed, and those blocks are reported as UNMEASURED rather than folded in as
  0.0. Averaging a failed measurement in as zero is how a broken loudness calculation comes
  to look exactly like a silent room - the same trap the callers' `_rms_fail_logged` guard
  was added for. Peak and mean are printed only over blocks that really were measured, and
  when none were the line says so instead of printing 0.0000.

REJECTED: counting speech in this module as well. The callers already own a `_voice_ms`
accumulator sampled by their gate at 100 ms, and a second, differently-sampled count of the
same thing would produce two numbers that disagree by design - and then the interesting
question stops being "did it hear me" and becomes "which counter do I trust". So the pulse
counts BLOCKS OVER THE LINE (every block, from the callback) and prints the caller's
`voice_ms` verbatim when it is handed one. Where they disagree, that disagreement is itself
the finding: the gate samples the newest block every 100 ms and the pulse sees all of them.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

DEFAULT_WINDOW_S = 3.0


class CapturePulse:
    """Rolling evidence about one open microphone. Not a Qt object; no dependencies.

    Usage (both ears stacks do exactly this):

        self._pulse = CapturePulse(threshold=_VOICE_RMS)       # when the stream opens
        self._pulse.note(rms)                                  # from the audio callback
        line = self._pulse.tick(voice_ms=self._voice_ms)       # from the 100 ms QTimer
        if line: _log.info("%s", line)
        _log.info("%s", self._pulse.closing("disarmed"))        # when the stream closes
    """

    def __init__(self, threshold: float, window_s: float = DEFAULT_WINDOW_S,
                 clock=time.monotonic) -> None:
        self._threshold = float(threshold)
        self._window = float(window_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._opened = clock()
        self._next_due = self._opened + self._window

        # Totals since the stream opened (never reset: a per-window count would make a
        # capture that died after two seconds unreadable in the last window's line).
        self._blocks = 0
        self._unmeasured = 0
        self._over = 0
        self._peak: Optional[float] = None
        self._sum = 0.0
        self._ticks = 0
        self._status_flags = 0
        self._status_text = ""

        # Marks so the run-long totals can still be reported per window.
        self._last_blocks = 0
        self._last_ticks = 0

    # -- fed from the audio callback thread ----------------------------------------------
    def note(self, rms: Optional[float], status=None) -> None:
        """One captured block. `rms` is None when its loudness could not be measured.

        `status` is sounddevice's CallbackFlags (input overflow and friends). It is
        recorded rather than ignored because an overflowing capture is a real cause of
        "it only heard half of what I said", and it is invisible in the audio itself.
        """
        with self._lock:
            self._blocks += 1
            if rms is None:
                self._unmeasured += 1
            else:
                r = float(rms)
                self._sum += r
                if self._peak is None or r > self._peak:
                    self._peak = r
                if r >= self._threshold:
                    self._over += 1
            if status:
                self._status_flags += 1
                if not self._status_text:
                    self._status_text = str(status)

    # -- called from the controller's 100 ms timer ---------------------------------------
    def tick(self, voice_ms: Optional[int] = None) -> Optional[str]:
        """Count a timer tick; return a line when `window_s` has elapsed, else None."""
        with self._lock:
            self._ticks += 1
            now = self._clock()
            if now < self._next_due:
                return None
            self._next_due = now + self._window
            line = self._line(now, voice_ms)
            self._last_blocks = self._blocks
            self._last_ticks = self._ticks
            return line

    def closing(self, reason: str, voice_ms: Optional[int] = None) -> str:
        """One final line, unconditionally, whatever closed the mic.

        Unconditional on purpose: an attempt shorter than one window would otherwise
        produce no evidence at all, and a mic held open for two seconds and closed is
        exactly the shape of "I pressed the key and nothing happened".
        """
        with self._lock:
            return "%s [closed: %s]" % (self._line(self._clock(), voice_ms), reason)

    # -- rendering (callers hold the lock) ----------------------------------------------
    def _line(self, now: float, voice_ms: Optional[int]) -> str:
        held = now - self._opened
        if self._blocks == 0:
            # Case 2, and the reason this class is timer-driven. Naming the tick count is
            # what makes it a measurement instead of an absence: it says the code that
            # would have reported audio really did run, this many times, and saw none.
            return ("ears: mic open %.1fs and NO audio has arrived - 0 blocks from the "
                    "capture callback over %d checks. The stream is open; the device is "
                    "delivering nothing." % (held, self._ticks))

        measured = self._blocks - self._unmeasured
        if measured == 0:
            return ("ears: mic open %.1fs - %d blocks arrived but NONE could be measured "
                    "for loudness, so there is no evidence either way about the room."
                    % (held, self._blocks))

        peak = self._peak if self._peak is not None else 0.0
        parts = ["ears: mic open %.1fs - %d blocks in over %d checks, peak %.4f mean %.4f "
                 "rms, speech line %.3f"
                 % (held, self._blocks, self._ticks, peak, self._sum / measured,
                    self._threshold)]
        if self._over == 0:
            parts.append("NOTHING has crossed the line yet (audio IS arriving: this is a "
                         "quiet room, a quiet mic, or a line set too high)")
        else:
            parts.append("%d blocks above it" % self._over)
        if voice_ms is not None:
            parts.append("gate counts %d ms of speech" % int(voice_ms))
        if self._unmeasured:
            parts.append("%d of %d blocks UNMEASURED (excluded, not counted as silence)"
                         % (self._unmeasured, self._blocks))
        if self._status_flags:
            parts.append("%d blocks flagged by PortAudio (%s)"
                         % (self._status_flags, self._status_text))
        starved = self._ticks - self._blocks
        if self._ticks >= 10 and starved >= max(3, self._ticks // 5):
            # Blocks are 100 ms and the tick is 100 ms, so these two counts track each
            # other on a healthy capture. A persistent shortfall means the gate is
            # re-reading one stale block, which inflates whichever verdict it reached.
            parts.append("capture is STARVED: %d fewer blocks than checks" % starved)
        return " - ".join(parts)

    # -- for tests and callers that want the raw numbers --------------------------------
    def stats(self) -> dict:
        with self._lock:
            return {"blocks": self._blocks, "unmeasured": self._unmeasured,
                    "over": self._over, "peak": self._peak, "ticks": self._ticks,
                    "status_flags": self._status_flags,
                    "threshold": self._threshold}
