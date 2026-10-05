"""Fakes shared by the picture-pace, no-absence, disable-companions and hardware-guard tests. No screen is captured,
no model runs, no service is started."""
from __future__ import annotations

import time

from companion_core import CompanionCore


class Speech:
    muted = False

    def __init__(self):
        self.said = []

    def say(self, text, speaker, priority):
        self.said.append((speaker, text))
        return True

    def pending(self):
        return 0


class FakeEyes:
    """Stands where the eyes stand in the core: state(), look(reason) and set_pace(...), all recorded."""

    def __init__(self, saw="A wrecked hull drifting in the dark.", scene="cockpit"):
        self.saw, self.scene, self.looks, self.paces = saw, scene, [], []

    def state(self):
        return {"scene": self.scene, "transitions": 0}

    def look(self, reason="curiosity"):
        self.looks.append(reason)
        return self.saw

    def set_pace(self, interval_s=None, never=False, hold=""):
        self.paces.append((interval_s, never, hold))
        return True


_GRAPH = []


def make_core(eyes=None, clock=None, **kw):
    """A real CompanionCore with a fake voice and no model. clock: a one-item list read as the time."""
    import companion_core as cc
    if not _GRAPH:
        _GRAPH.append(cc.TopicGraph.load())
    real = cc.TopicGraph.load
    cc.TopicGraph.load = classmethod(lambda cls, *a, **k: _GRAPH[0])
    if clock is not None:
        kw["now"] = lambda: clock[0]
    try:
        core = CompanionCore(Speech(), realizer=None, eyes=eyes, ambient_every_s=3600,
                             features={"manufacturer_flavour": False, "place_flavour": False}, **kw)
    finally:
        cc.TopicGraph.load = real
    core.considered = []
    core._consider = lambda spec, gp, sp, why: core.considered.append(spec)
    return core


def tick(core, scene=None, st=None):
    """One turn of the core's eyes loop, then wait for the look it may have started."""
    eyes = core.eyes
    state = st if st is not None else (eyes.state() if eyes is not None else {})
    core._eyes_present(state, scene if scene is not None else state.get("scene"), state.get("scene"))
    deadline = time.time() + 5
    while core._look_busy and time.time() < deadline:
        time.sleep(0.005)
    assert not core._look_busy, "the look never finished"


def spoke(core, spec):
    """Mark a considered line as SAID, the way the realize loop does once it has been spoken."""
    from speak_gate import Candidate, Priority
    core._spoke(spec, "said", Candidate(priority=Priority.AMBIENT, speaker=spec["speaker"], text_len_words=8,
                                        created_at=core.now()))
