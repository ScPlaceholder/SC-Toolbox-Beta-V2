"""The client's "Failed to get starmap route data" line must not move Pico's mood.

J 2026-10-04: "Why is the pico mad when I'm in a ship?" Sitting in a ship with no route loaded, the game
wrote this line six times in two and a half minutes. Upstream reads each one as `qt_error`, worth 0.35
irritation, so Pico went from happy to irritation 1.00 with nothing having happened to the player.
A real failed jump (the HUD's "Quantum Travel: ... obstructed") is a different line and is not filtered.
"""
from pico import events

# verbatim from J's Game.log, 2026-10-04
NOISE = ("<2026-10-04T17:13:32.126Z> [Notice] <Failed to get starmap route data!> [ItemNavigation][CL][26140] "
         "| NOT AUTH | KRIG_L21_Wolf_856701633691[856701633691]|CSCItemNavigation::GetStarmapRouteSegmentData|"
         "No Route loaded! [Team_CGP4][QuantumTravel]")
# verbatim from J's logbackups, 2025-12-19: a jump the game refused
REAL = ('<2025-12-19T01:53:32.692Z> [Notice] <SHUDEvent_OnNotification> Added notification "Quantum Travel: Your '
        'destination is obstructed and Quantum Travel cannot be initiated. You may need to travel somewhere else '
        'first to find a clear path." [79] to queue. New queue size: 3, MissionId: '
        '[00000000-0000-0000-0000-000000000000], ObjectiveId: [] [Team_CoreGameplayFeatures][Missions][Comms]')


def _source():
    return events.MoodSource(events.load_suit())


def test_upstream_still_calls_the_noise_line_qt_error():
    # the premise of the fix: if upstream stops classifying it, this file is guarding nothing
    src = _source()
    src.feed_line(NOISE)
    assert src.events_seen == 1
    assert src.last_event.event_type == "qt_error"


def test_route_noise_does_not_move_the_mood():
    src = _source()
    for _ in range(6):
        src.feed_line(NOISE)
    assert src.events_seen == 6            # still observed: the feed is alive
    assert src.events_moved == 0           # but none of it reached the affect model
    assert src.noise_ignored == 6
    reading = src.reading()
    assert reading.emotion != "irritation", reading


def test_a_refused_jump_is_not_caught_by_the_noise_filter():
    """The HUD's "destination is obstructed" line is a real failure and must never be treated as route noise.
    Checked 2026-10-04 with this real line: upstream's parser emits NO event for it at all today (its
    `qt_error` rule for "obstructed" is not reached), so nothing about quantum travel moves his mood now.
    That is an upstream gap, recorded here, not something this filter causes: the filter must stay out of
    its way for the day upstream starts emitting it."""
    assert events.ROUTE_NOISE not in REAL
    src = _source()
    src.feed_line(NOISE)
    src.feed_line(REAL)
    assert src.noise_ignored == 1          # only the noise line was filtered
