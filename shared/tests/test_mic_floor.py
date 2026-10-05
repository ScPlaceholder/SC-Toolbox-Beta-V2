"""One microphone, however many ears share a process (shared/mic_floor.py; J, 2026-10-05).

The Assistant and SuitMk2 each have a push-to-talk key now. These pin the rule that keeps two keys from opening
the microphone twice, on the floor itself; tools/Assistant/tests/test_push_to_talk.py drives it through both
tools' real ears.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.mic_floor import OPEN, PUSH, MicFloor  # noqa: E402


def test_the_first_key_has_the_microphone_until_it_lets_go():
    f = MicFloor()
    assert f.claim("assistant", PUSH) is True
    assert f.claim("suitmk2", PUSH) is False, "a second key was given the microphone"
    assert f.holder() == "assistant"
    f.release("suitmk2")                            # the refused key going up frees nothing
    assert f.holder() == "assistant"
    f.release("assistant")
    assert f.holder() == "" and f.claim("suitmk2", PUSH) is True


def test_one_owner_is_never_refused():
    f = MicFloor()
    assert f.claim("assistant", PUSH) and f.claim("assistant", PUSH) and f.claim("assistant", OPEN)


def test_a_key_takes_the_microphone_from_an_open_mic_which_comes_back_when_the_key_lets_go():
    f, log = MicFloor(), []
    assert f.claim("suitmk2", OPEN, on_yield=lambda: (log.append("closed"), f.release("suitmk2")),
                   on_resume=lambda: log.append("reopen")) is True
    assert f.claim("assistant", PUSH) is True
    assert log == ["closed"] and f.holder() == "assistant", "the open mic's own close freed the key's microphone"
    f.release("assistant")
    assert log == ["closed", "reopen"] and f.holder() == ""


def test_an_open_mic_does_not_take_the_microphone_from_a_key_and_waits_for_it():
    f, log = MicFloor(), []
    assert f.claim("assistant", PUSH) is True
    assert f.claim("suitmk2", OPEN, on_resume=lambda: log.append("reopen")) is False
    assert log == []
    f.release("assistant")
    assert log == ["reopen"]


def test_an_owner_that_goes_away_leaves_nothing_behind():
    f, log = MicFloor(), []
    f.claim("assistant", PUSH)
    f.claim("suitmk2", OPEN, on_resume=lambda: log.append("reopen"))
    f.forget("suitmk2")
    f.release("assistant")
    assert log == [] and f.holder() == ""
    f.claim("assistant", PUSH)
    f.forget("assistant")
    assert f.holder() == ""


def test_a_callback_that_raises_does_not_cost_the_key_the_microphone():
    f = MicFloor()

    def boom():
        raise RuntimeError("stream already closed")
    f.claim("suitmk2", OPEN, on_yield=boom, on_resume=boom)
    assert f.claim("assistant", PUSH) is True
    f.release("assistant")
    assert f.holder() == ""
