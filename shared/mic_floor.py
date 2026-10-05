"""mic_floor.py - one microphone, however many ears share a process.

J, 2026-10-05: "For the assistant and suit Mk 2 can you have individual push to talk buttons which also
auto-route to the right ai?" The Assistant and SuitMk2 are two tabs of one window and one process, each with ears
of its own (assistant/voice.py and tools/SuitMk2/core/voice_in/ears.py) and now each with a push-to-talk key that
works whichever tab is showing. Two keys must never open the microphone twice: the same sentence would be
transcribed twice and answered by both.

This is the one place that decides who has the microphone. Nothing here opens it; the ears do, after asking.

    FLOOR.claim("assistant", "push")   -> True: it is yours, open the mic.  False: somebody has it, do nothing.
    FLOOR.release("assistant")         -> you closed it.

Two kinds of holder:

    push   a held push-to-talk key. It keeps the microphone until it lets go; a second key pressed meanwhile is
           refused, so which AI hears a sentence is decided by the key that was held first and by nothing else.
    open   an always-open mic (the "Always on" mode of the tab in front). Nobody is holding anything, so a push
           key takes the microphone from it: the open holder is told to close (on_yield) and, when the key is
           released, to open again (on_resume). Holding a key is the explicit request; an open mic is a default.

OneMicMixin puts an EarsController behind the floor without editing it: both tools' controllers begin a capture
in ``_begin`` and end every capture in ``_abort_recording``, and those two are all it wraps.

A process with one tool in it (each tool's own window, the tests) has one owner, and one owner is never refused.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Tuple

log = logging.getLogger(__name__)

PUSH, OPEN = "push", "open"

_Callback = Optional[Callable[[], None]]


class MicFloor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: Optional[Tuple[str, str]] = None          # (owner, kind)
        self._yield_cb: _Callback = None                        # the holder's, when it is an open mic
        self._resume_cb: _Callback = None
        self._parked: Optional[Tuple[str, _Callback]] = None    # an open mic waiting to come back

    def holder(self) -> str:
        """Who has the microphone, or "" when nobody does."""
        with self._lock:
            return self._holder[0] if self._holder else ""

    def claim(self, owner: str, kind: str = PUSH, on_yield: _Callback = None, on_resume: _Callback = None) -> bool:
        """Ask for the microphone. True means open it; False means leave it shut.

        on_yield / on_resume matter only to an "open" claim: on_yield is called (and must close the stream before
        it returns) when a push key takes the microphone away, on_resume when that key has been released."""
        yield_cb = None
        with self._lock:
            held = self._holder
            if held is None or held[0] == owner:
                self._holder = (owner, kind)
                self._yield_cb, self._resume_cb = on_yield, on_resume
                if self._parked is not None and self._parked[0] == owner:
                    self._parked = None
                return True
            if kind == PUSH and held[1] == OPEN:
                # the key wins over the open mic; the open mic comes back afterwards
                self._parked = (held[0], self._resume_cb)
                yield_cb = self._yield_cb
                self._holder = (owner, kind)
                self._yield_cb = self._resume_cb = None
            elif kind == OPEN:
                # an open mic that cannot open now waits for the key to be released
                self._parked = (owner, on_resume)
                return False
            else:
                return False
        if yield_cb is not None:
            try:
                yield_cb()
            except Exception:                           # noqa: BLE001 - the key still gets the microphone
                log.exception("mic floor: the open microphone did not close cleanly")
        return True

    def release(self, owner: str) -> None:
        """*owner* has closed the microphone. Not the holder: nothing happens (a capture that was taken over
        still runs its own close, and that must not free the microphone under the key that took it)."""
        resume = None
        with self._lock:
            if self._holder is None or self._holder[0] != owner:
                return
            self._holder = None
            self._yield_cb = self._resume_cb = None
            if self._parked is not None:
                resume, self._parked = self._parked[1], None
        if resume is not None:
            try:
                resume()
            except Exception:                           # noqa: BLE001 - the microphone is free either way
                log.exception("mic floor: the open microphone did not come back")

    def forget(self, owner: str) -> None:
        """*owner* is going away (its tab was shut down): drop whatever it holds or is waiting for."""
        with self._lock:
            if self._parked is not None and self._parked[0] == owner:
                self._parked = None
        self.release(owner)


# The process's floor. Both tools' ears use this one.
FLOOR = MicFloor()


class OneMicMixin:
    """Put an EarsController behind a MicFloor: ``class Ears(OneMicMixin, EarsController)``.

    The class that mixes this in declares two Qt signals itself (a signal has to be declared on a QObject class):

        captureFailed = Signal(str)    a capture was asked for and the microphone did not open; the reason
        captureBusy   = Signal(str)    refused: another owner's key is being held; that owner's name

    With no floor set (use_floor never called) the controller behaves exactly as it did.
    """

    floor: Optional[MicFloor] = None
    floor_owner: str = ""

    def use_floor(self, floor: Optional[MicFloor], owner: str) -> None:
        self.floor, self.floor_owner = floor, owner

    def _begin(self) -> None:
        if self._recording:
            return
        floor = self.floor
        if floor is None:
            super()._begin()
            return
        kind = OPEN if self._mode == "always" else PUSH
        if not floor.claim(self.floor_owner, kind, on_yield=self._floor_yield, on_resume=self._floor_resume):
            if kind == PUSH:
                self.captureBusy.emit(floor.holder())
            return
        said = []

        def note(msg: str) -> None:
            said.append(msg)
        self.statusChanged.connect(note)
        try:
            super()._begin()
        finally:
            self.statusChanged.disconnect(note)
        if not self._recording:
            # the controller said why on statusChanged ("mic error: ...", a missing library) and opened nothing
            floor.release(self.floor_owner)
            self.captureFailed.emit(said[-1] if said else "the microphone did not open")

    def _abort_recording(self, *args, **kwargs) -> None:
        super()._abort_recording(*args, **kwargs)
        if self.floor is not None:
            self.floor.release(self.floor_owner)

    def _floor_yield(self) -> None:
        """A push key took the microphone from this open mic: close now, keeping nothing of the half sentence."""
        if self._recording:
            super()._abort_recording()

    def _floor_resume(self) -> None:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._restart_if_armed)
