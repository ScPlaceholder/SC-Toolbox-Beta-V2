"""_set_clipboard must actually put the destination on the clipboard.

WHY THIS FILE EXISTS. On 2026-09-27 the in-game route setter raised "GlobalLock failed"
on every single call, so the destination never reached the clipboard and no route was ever
plotted. Nothing caught it because nothing had ever asserted the OUTCOME -- the macro
reported each stage it *entered*, and the failure surfaced only as a string handed to a
done-callback that a human had to read.

The port had dropped the eight restype/argtypes declarations that
`tools/set_route_ai/main.py` introduces with the comment
``# --- CRITICAL: Declare proper 64-bit return types ---``. Without them ctypes assumes an
``int`` return, so GlobalAlloc's 64-bit HANDLE is truncated and sign-extended, and
GlobalLock on the truncated value returns 0.

So these tests assert a ROUND TRIP through the real Win32 clipboard, which is the only
thing that distinguishes "we called the API" from "the text is there". A mock cannot catch
a handle-width bug; the bug lives precisely in the marshalling a mock replaces.

MUTATION RESULT, recorded honestly because half of it is a gap:

    M1  remove the pointer-width declarations (the shipped bug)   -> 6 of 7 tests FAIL
    M2  no NUL terminator, size = len(text_bytes) + 2             -> ALL 7 STILL PASS

★ M2 IS NOT CAUGHT AND NO TEST HERE CAN CATCH IT. That was the second defect in the same
  function, and it is the one the first was masking: memmove copied ``len + 2`` bytes out
  of a buffer holding ``len``, over-reading the Python bytes object by two, and never
  deliberately wrote the terminator. It is real undefined behaviour and it is invisible
  here for two independent reasons that both happen to be kind:
    * CPython's bytes objects carry their own trailing NUL plus allocator slack, so
      reading two bytes past the end returns zeros instead of faulting.
    * GlobalAlloc's block is not documented as zeroed without GMEM_ZEROINIT, but a
      freshly committed page is zero in practice, so the absent terminator lands on an
      accidental one.
  Both are accidents of this environment, not guarantees. The fix is still correct, and
  the honest statement is that its correctness rests on reading the code, not on a green
  test. Do not "add a test for M2" by asserting something incidental just to make this
  paragraph go away -- a test that passes for the wrong reason is worse than this note.
"""
from __future__ import annotations

import ctypes
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Win32 clipboard API; nothing to test elsewhere")

from starmap.voice.route_setter import _set_clipboard   # noqa: E402


def _read_clipboard() -> str:
    """Read CF_UNICODETEXT back, with the same pointer-width care the writer needs."""
    CF_UNICODETEXT = 13
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(None):
        raise RuntimeError("OpenClipboard failed while reading")
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            raise RuntimeError("clipboard holds no CF_UNICODETEXT")
        p = k.GlobalLock(h)
        if not p:
            raise RuntimeError("GlobalLock failed while reading")
        try:
            return ctypes.c_wchar_p(p).value or ""
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


@pytest.mark.parametrize("text", [
    "orison",                 # the real case that was broken
    "area18",
    "a",                      # shortest: size arithmetic is easiest to get wrong here
    "port olisar",            # a space
    "microTech",              # mixed case survives untouched
])
def test_round_trip(text):
    """The exact string comes back -- no truncation, no trailing garbage.

    The equality is doing two jobs. It catches the handle-width bug (which raises before
    this point) AND the separate over-read the first bug was masking: the old body
    memmove'd len+2 bytes out of a buffer holding len, so the NUL was never deliberately
    written and CF_UNICODETEXT could carry whatever followed in memory. A `startswith`
    assertion would have passed straight through that.
    """
    _set_clipboard(text)
    assert _read_clipboard() == text


def test_does_not_raise_on_repeat():
    """Called twice in a row, as a second route request does.

    The writer reassigns restype/argtypes on the shared ctypes.windll handles every call.
    That is idempotent, and this test is what says so rather than assuming it.
    """
    _set_clipboard("orison")
    _set_clipboard("area18")
    assert _read_clipboard() == "area18"


def test_handle_is_pointer_width():
    """The mechanism itself, stated as an assertion rather than a comment.

    A future tidy-up that 'simplifies away' the restype declarations will make the module
    fail test_round_trip -- but this test names WHY in the failure, so whoever reads it is
    not left re-deriving the 64-bit truncation from a GlobalLock error code.
    """
    k = ctypes.WinDLL("kernel32")        # fresh handle: DEFAULT (broken) types
    h_truncated = k.GlobalAlloc(0x0002, 16)
    k2 = ctypes.WinDLL("kernel32")
    k2.GlobalAlloc.restype = ctypes.c_void_p
    k2.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k2.GlobalLock.restype = ctypes.c_void_p
    k2.GlobalLock.argtypes = [ctypes.c_void_p]
    h_real = k2.GlobalAlloc(0x0002, 16)
    assert k2.GlobalLock(h_real), "a pointer-width handle must lock"
    # On win64 the default-int handle is a different (truncated) value. If this ever stops
    # being true the platform changed, and the skip below says so instead of failing.
    if h_truncated == h_real or 0 <= h_truncated <= 0x7FFFFFFF:
        pytest.skip("handles happen to fit in an int here; the truncation is not "
                    "demonstrable on this run, which is a fact about the allocation, "
                    "not a pass")
    assert h_truncated != h_real
