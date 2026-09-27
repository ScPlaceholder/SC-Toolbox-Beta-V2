"""The open mic hears the speakers, so the assistant answered its own question.

Owner's screenshot, 2026-09-27, "Always on" + "Voice Replies" both selected:

    You: Want me to open Starmouth? Say yes or not. Yes. Yes.
    AI:  Okay, I won't open Starmap.

The first NINE words of that "You:" line are the ASSISTANT'S OWN PROMPT ("Want me to
open Starmap? Say yes or no"), spoken through the speakers and picked straight back up
by the open mic, with the user's real "Yes. Yes." appended to the same utterance. The
combined string contains "not", and `logic.classify_confirmation` is documented to let
any negative marker win -- so it read a refusal and refused an action the user had just
approved twice. The inversion is not a guess; the two assertions in the first test put
both strings through the real parser.

Two independent defences are asserted here, because the first one cannot be trusted
alone:

  1. the GATE -- while the mouth is talking, an always-open mic captures nothing.
  2. the ECHO FILTER -- a transcript that repeats what was just spoken is stripped
     (or dropped) before it reaches the parser.

The gate is timing, and timing is the unreliable part: neither Mouth.speak() nor
CharacterMouth.speak() blocks -- both are a Queue.put that returns in microseconds,
before a single sample has played -- so the gate's window is an ESTIMATE and the filter
is what catches an estimate that ran short.

Nothing here touches a real cache, a real model, a real microphone or ~/.sctoolbox:
the whisper model is faked per test, the audio blocks are plain lists, and the panel's
_speak is exercised unbound against a stub so no AssistantWindow (and so no panel state
file) is ever built.

Run explicitly (this directory is not in pyproject's testpaths), with the interpreter
that has PySide6:

    "C:/Users/prjgn/AppData/Local/Programs/Python/Python313/python.exe" -m pytest \
        tools/Assistant/tests/test_assistant_self_hearing.py -q
"""

import types

import pytest

from assistant import voice as voice_mod
from assistant.logic import classify_confirmation

# The assistant's own line, and the utterance the mic handed back -- the screenshot verbatim.
SPOKEN = "Want me to open Starmap? Say yes or no."
HEARD = "Want me to open Starmouth? Say yes or not. Yes. Yes."
USER_ONLY = "Yes. Yes."


# ── fakes ────────────────────────────────────────────────────────────────────
class _FakeSeg:
    def __init__(self, text):
        self.text = text


class _FakeModel:
    """Stands in for faster_whisper: no model, no download, no cache."""

    def __init__(self, text):
        self._text = text
        self.calls = 0

    def transcribe(self, *_a, **_kw):
        self.calls += 1
        return [_FakeSeg(self._text)], object()


class _Block(list):
    """One audio block, loud enough to count as speech, with just enough
    arithmetic for _audio_cb. A list, so numpy is not needed to test the gate."""

    def __pow__(self, n):
        return _Block(v ** n for v in self)

    def copy(self):
        return _Block(self)


class _FakeNp:
    @staticmethod
    def sqrt(x):
        return x ** 0.5

    @staticmethod
    def mean(x):
        return sum(x) / len(x)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _ears(monkeypatch, mode="always", heard=HEARD):
    e = voice_mod.EarsController()
    e.set_mode(mode)
    monkeypatch.setattr(e, "_get_model", lambda: _FakeModel(heard))
    got = []
    e.transcript.connect(got.append)
    return e, got


def _mouth_speaks(ears, text=SPOKEN) -> bool:
    """What panel._speak has to do when Voice Replies is on: tell the ears the
    mouth is about to talk, before queueing the line.

    Before the fix there is nothing to tell, so this degrades to a no-op and
    returns False -- which IS the defect, and lets the assertions below run and
    fail on the echo rather than on a missing attribute."""
    note = getattr(ears, "note_speaking", None)
    if note is None:
        return False
    note(text)
    return True


# ── 1. the repro ─────────────────────────────────────────────────────────────
def test_the_assistants_own_question_does_not_reach_the_parser(monkeypatch):
    # The inversion, from the real parser, before anything about the ears is claimed.
    assert classify_confirmation(HEARD) == "no", "the screenshot's string no longer inverts"
    assert classify_confirmation(USER_ONLY) == "yes"

    e, got = _ears(monkeypatch)
    wired = _mouth_speaks(e)
    e._transcribe(object())          # the utterance captured while the mouth was talking

    reached = got[0] if got else ""
    assert "starmouth" not in reached.lower() and "open" not in reached.lower(), (
        "the mouth's own question reached the parser (gate wired: %s): %r" % (wired, reached))
    assert got, "the user's answer was thrown away along with the echo"
    assert classify_confirmation(got[0]) == "yes", (
        "the user said yes twice and the parser got %r" % got[0])


def test_the_panel_tells_the_ears_before_it_queues_the_line():
    """panel._speak's ordering, unbound: no window, no state file."""
    from assistant import panel as panel_mod

    order = []
    stub = types.SimpleNamespace(
        _btn_replies=types.SimpleNamespace(isChecked=lambda: True),
        _mouth=types.SimpleNamespace(speak=lambda _t: order.append("mouth")),
        _ears=types.SimpleNamespace(note_speaking=lambda _t: order.append("ears"),
                                    cancel_speaking=lambda: order.append("cancel")),
        _lbl_reply=types.SimpleNamespace(setText=lambda _t: None, setToolTip=lambda _t: None),
    )
    monkeypatched = getattr(panel_mod.Mouth, "available")
    assert monkeypatched(), "this test needs a platform where Voice Replies is possible"
    panel_mod.AssistantWindow._speak(stub, SPOKEN)
    assert order == ["ears", "mouth"], (
        "the ears must be warned BEFORE the line is queued; got %r" % (order,))


def test_the_open_mic_captures_nothing_while_the_mouth_is_talking(monkeypatch):
    e, _ = _ears(monkeypatch)
    e._np = _FakeNp()
    e._recording = True
    e._frames = []
    loud = _Block([0.4] * 8)

    e._audio_cb(loud, 8, None, None)
    assert len(e._frames) == 1, "a quiet house must still be captured normally"

    wired = _mouth_speaks(e)
    for _ in range(5):
        e._audio_cb(loud, 8, None, None)
    assert len(e._frames) == 1, (
        "captured %d block(s) of my own voice (gate wired: %s)" % (len(e._frames) - 1, wired))


# ── 2. push-to-talk must not regress ─────────────────────────────────────────
def test_push_to_talk_is_not_deafened_by_the_gate(monkeypatch):
    """Holding the key IS an explicit intent to be heard. The capture gate is for
    the always-open mic only; going deaf mid-hold would make hold-to-talk
    mysteriously dead whenever the assistant was mid-sentence."""
    said = "Best trade route for my Caterpillar"
    e, got = _ears(monkeypatch, mode="push", heard=said)
    e._np = _FakeNp()
    e._recording = True
    e._frames = []

    _mouth_speaks(e)
    for _ in range(4):
        e._audio_cb(_Block([0.4] * 8), 8, None, None)
    assert len(e._frames) == 4, "push-to-talk stopped capturing while the mouth talked"

    e._transcribe(object())
    assert got == [said], "push-to-talk lost a genuine utterance: %r" % (got,)


# ── 3. the gate itself ───────────────────────────────────────────────────────
def test_the_gate_shuts_for_the_line_then_reopens_on_its_own():
    clock = _Clock()
    g = voice_mod.SpeechGate(clock=clock.now)
    assert not g.active()
    g.note_speaking(SPOKEN)
    assert g.active()
    clock.advance(1.0)
    assert g.active(), "reopened while a nine-word line was still playing"
    clock.advance(60.0)
    assert not g.active(), "never reopened -- the ears would be deaf for good"


def test_queued_lines_add_up_instead_of_replacing_each_other():
    """The mouth's queue plays lines one after another, so two replies in a row
    must extend the window, not reset it to one line's length."""
    clock = _Clock()
    g = voice_mod.SpeechGate(clock=clock.now)
    g.note_speaking(SPOKEN)
    one = g.remaining_ms()
    g.note_speaking(SPOKEN)
    assert g.remaining_ms() > one * 1.5, "the second line overwrote the first"


def test_a_cancelled_line_reopens_the_ears_at_once():
    clock = _Clock()
    g = voice_mod.SpeechGate(clock=clock.now)
    g.note_speaking(SPOKEN)
    g.cancel()
    assert not g.active()


def test_a_mouth_that_throws_does_not_leave_the_ears_deaf(monkeypatch):
    from assistant import panel as panel_mod

    e, _ = _ears(monkeypatch)

    class _Boom:
        def speak(self, _t):
            raise RuntimeError("no audio device")

    stub = types.SimpleNamespace(
        _btn_replies=types.SimpleNamespace(isChecked=lambda: True),
        _mouth=_Boom(), _ears=e,
        _lbl_reply=types.SimpleNamespace(setText=lambda _t: None, setToolTip=lambda _t: None),
    )
    with pytest.raises(RuntimeError):
        panel_mod.AssistantWindow._speak(stub, SPOKEN)
    assert not e._gate.active(), "a line that was never spoken left the ears deaf"


# ── 4. the echo filter, on its own ───────────────────────────────────────────
def _filter(clock=None):
    return voice_mod.EchoFilter(clock=clock) if clock else voice_mod.EchoFilter()


def test_the_whole_utterance_is_the_echo():
    f = _filter()
    f.note_spoken(SPOKEN)
    kept, why = f.clean("Want me to open Starmap? Say yes or no.")
    assert kept == "" and why


def test_a_leaked_tail_is_still_recognised_as_the_echo():
    """The gate's estimate running short leaks the END of the line, not its start."""
    f = _filter()
    f.note_spoken(SPOKEN)
    assert _filter_kept(f, "Say yes or no.") == ""


def test_the_users_answer_survives_the_strip():
    f = _filter()
    f.note_spoken(SPOKEN)
    kept, why = f.clean(HEARD)
    assert why
    assert classify_confirmation(kept) == "yes", "stripped to %r" % kept


def test_a_bare_yes_is_never_eaten():
    """"yes" appears inside the spoken prompt. Dropping it would invert the
    decision in the other direction, which is the same bug wearing a hat."""
    f = _filter()
    f.note_spoken(SPOKEN)
    assert _filter_kept(f, "Yes") == "Yes"
    assert _filter_kept(f, "Yes please") == "Yes please"


def test_a_real_command_that_shares_a_word_is_left_alone():
    f = _filter()
    f.note_spoken(SPOKEN)
    for said in ("Open the Trade Hub", "Set route to Pyro", "What missions do I have?",
                 "No, cancel that"):
        assert _filter_kept(f, said) == said, "mangled %r" % said


def test_nothing_spoken_means_nothing_stripped():
    f = _filter()
    assert _filter_kept(f, HEARD) == HEARD


def test_a_stale_line_stops_being_an_excuse():
    clock = _Clock()
    f = voice_mod.EchoFilter(clock=clock.now)
    f.note_spoken(SPOKEN)
    clock.advance(voice_mod._ECHO_TTL_S + 1)
    assert _filter_kept(f, "Say yes or no.") == "Say yes or no.", (
        "a line spoken a minute ago still explains a fresh utterance")


def _filter_kept(f, heard):
    return f.clean(heard)[0]


# ── 5. the two together, through the controller ──────────────────────────────
def test_an_echo_that_leaks_past_the_gate_is_still_not_parsed(monkeypatch):
    """The belt, with the braces cut: the gate has already expired (a cold Piper
    load delayed the audio), so only the filter stands between the speakers and
    the parser."""
    clock = _Clock()
    e, got = _ears(monkeypatch, heard="Say yes or no.")
    e._gate = voice_mod.SpeechGate(clock=clock.now)
    e.note_speaking(SPOKEN)
    clock.advance(600.0)                      # the window is long gone
    assert not e._gate.active()
    e._transcribe(object())
    assert got == [], "the echo reached the parser: %r" % (got,)
