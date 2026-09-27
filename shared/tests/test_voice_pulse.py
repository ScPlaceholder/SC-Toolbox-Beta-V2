"""The capture pulse must say five different things about five different faults.

Each test names the fault it stands for. The important one is
`test_zero_blocks_is_its_own_line`: a "frames arriving" detector that has never been
observed with zero frames is not evidence about anything.

Every test drives the pulse the way the controllers really do - one `tick()` per 100 ms of
the fake clock, `note()` only when a block actually arrived - so the block-vs-check counts
in the rendered line mean here what they will mean in the log.
"""
from shared.voice_pulse import CapturePulse


class _Clock:
    """A hand-cranked monotonic clock, so a 3 s window costs no wall time."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _pulse(threshold=0.012, window=3.0):
    c = _Clock()
    return CapturePulse(threshold=threshold, window_s=window, clock=c), c


def _run(p, c, blocks, ticks=30, voice_ms=None):
    """Tick `ticks` times at 100 ms; `blocks(i)` returns this tick's note() args or None."""
    line = None
    for i in range(ticks):
        arg = blocks(i)
        if arg is not None:
            p.note(*arg)
        c.advance(0.1)
        line = p.tick(voice_ms=voice_ms) or line
    return line


def test_nothing_is_said_before_the_window_elapses():
    p, c = _pulse()
    for _ in range(29):
        p.note(0.2)
        c.advance(0.1)
        assert p.tick() is None
    p.note(0.2)
    c.advance(0.1)
    assert p.tick() is not None


def test_zero_blocks_is_its_own_line():
    """Fault 2: the stream opened and the device delivers nothing."""
    p, c = _pulse()
    line = _run(p, c, lambda i: None)
    assert "NO audio has arrived" in line
    assert "0 blocks" in line
    assert "30 checks" in line          # the checks prove the reporter ran
    assert "peak" not in line           # never a fabricated 0.0000


def test_quiet_room_says_audio_is_arriving():
    """Fault 3/4: blocks arrive, nothing crosses the line."""
    p, c = _pulse()
    line = _run(p, c, lambda i: (0.003,))
    assert "30 blocks in over 30 checks" in line
    assert "peak 0.0030" in line
    assert "NOTHING has crossed the line yet" in line
    assert "speech line 0.012" in line
    assert "STARVED" not in line


def test_loud_room_reports_blocks_over_the_line():
    """Fault 5: it was loud, the gate crossed, and no utterance came out."""
    p, c = _pulse()
    line = _run(p, c, lambda i: (0.4 if i >= 10 else 0.001,), voice_ms=2000)
    assert "20 blocks above it" in line
    assert "peak 0.4000" in line
    assert "gate counts 2000 ms of speech" in line
    assert "NOTHING has crossed" not in line


def test_unmeasured_blocks_are_excluded_not_counted_as_silence():
    p, c = _pulse()
    line = _run(p, c, lambda i: (0.5,) if i < 10 else ((None,) if i < 20 else None))
    assert "10 of 20 blocks UNMEASURED" in line
    assert "peak 0.5000" in line
    assert "mean 0.5000" in line        # the 10 failures must not drag the mean down


def test_every_block_unmeasured_refuses_to_report_a_level():
    p, c = _pulse()
    line = _run(p, c, lambda i: (None,))
    assert "NONE could be measured" in line
    assert "no evidence either way" in line
    assert "peak" not in line


def test_portaudio_status_flags_are_reported():
    p, c = _pulse()
    line = _run(p, c, lambda i: (0.2, "input overflow") if i == 3 else (0.2, None))
    assert "1 blocks flagged by PortAudio (input overflow)" in line


def test_a_starved_capture_is_named():
    """Ticks with no blocks behind them mean the gate is re-reading one stale block."""
    p, c = _pulse()
    line = _run(p, c, lambda i: (0.2,) if i % 5 == 0 else None)
    assert "capture is STARVED: 24 fewer blocks than checks" in line


def test_closing_always_speaks_even_inside_the_first_window():
    p, c = _pulse()
    p.note(0.2)
    c.advance(0.4)
    for _ in range(4):
        assert p.tick() is None
    line = p.closing("key released")
    assert "[closed: key released]" in line
    assert "1 blocks in" in line


def test_closing_a_mic_that_never_delivered_says_so():
    p, c = _pulse()
    for _ in range(12):
        c.advance(0.1)
        p.tick()
    line = p.closing("disarmed")
    assert "NO audio has arrived" in line
    assert "over 12 checks" in line
    assert "[closed: disarmed]" in line


def test_stats_are_countable_not_inferred():
    p, _c = _pulse()
    p.note(0.5)
    p.note(None)
    p.note(0.001)
    s = p.stats()
    assert s == {"blocks": 3, "unmeasured": 1, "over": 1, "peak": 0.5, "ticks": 0,
                 "status_flags": 0, "threshold": 0.012}
