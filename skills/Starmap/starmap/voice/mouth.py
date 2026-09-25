"""Mouth — spoken feedback for the Starmap voice loop.

The voice pipeline talks back through Windows SAPI text-to-speech
("Navigate to Area 18", "route plotted") without adding any pip
dependency: each utterance is spoken by PowerShell's System.Speech
synthesizer, serialized on a daemon thread so consecutive messages
never overlap.

If PowerShell / SAPI is unavailable the mouth stays silent — speech is
a nicety on top of the status line, never a hard dependency.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from queue import Queue
from typing import Optional

_RATE = 2          # SAPI speech rate, -10 .. 10
_VOLUME = 90       # 0 .. 100
_TIMEOUT = 30      # seconds; utterances are short


def _ps_quote(text: str) -> str:
    """Single-quote a string for embedding in a PowerShell command."""
    return "'" + text.replace("'", "''") + "'"


class Mouth:
    """Small speak-queue over the SAPI synthesizer."""

    def __init__(self) -> None:
        self._q: "Queue[Optional[str]]" = Queue()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="MouthTTS").start()

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._q.put(text)

    def stop(self) -> None:
        """Drain the queue and kill any in-flight utterance."""
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

    # ── worker ─────────────────────────────────────────────────────────────
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
                    f"$s.Rate = {_RATE}; $s.Volume = {_VOLUME}; "
                    f"$s.Speak({_ps_quote(text)}); "
                    "$s.Dispose()"
                )
                with self._lock:
                    self._proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-NonInteractive",
                         "-Command", cmd],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._proc.wait(timeout=_TIMEOUT)
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
