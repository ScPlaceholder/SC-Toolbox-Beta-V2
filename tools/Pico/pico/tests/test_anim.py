""".anim parsing, interpolation, easing, channels, and layered blending."""

from __future__ import annotations

import json

import pytest

from pico.anim import (
    AnimError,
    ChannelMap,
    Clip,
    ClipLibrary,
    Layered,
    PRECEDENCE,
)
from pico.rig import Delta, Pose, Skeleton


def base_clip(**over) -> dict:
    d = {
        "name": "t", "duration": 1.0, "loop": False, "channel": "BASE",
        "tracks": {"body": [{"t": 0.0, "rot": 0}, {"t": 1.0, "rot": 10}]},
    }
    d.update(over)
    return d


def make(d: dict, skeleton=None, channel_map=None) -> Clip:
    return Clip.from_dict(d, skeleton=skeleton, channel_map=channel_map)


# ---------------------------------------------------------------------------
# The shipped clips
# ---------------------------------------------------------------------------


def test_the_shipped_library_holds_the_owners_gate_clips(clips: ClipLibrary):
    assert set(clips) == {"idle_breathe", "blink", "look_left", "look_right",
                          "wave", "dance", "death_flop"}


def test_the_shipped_clips_load_without_warnings(clips: ClipLibrary):
    assert clips.all_warnings() == ()


def test_the_shipped_clips_only_key_bones(clips: ClipLibrary, skeleton: Skeleton):
    slot_names = {s.name for s in skeleton.slots}
    for name in clips:
        for bone in clips[name].bones():
            assert skeleton.has_bone(bone)
            if bone in slot_names:
                # legal: some slots share a bone's name. What matters is that
                # the track resolved to the BONE namespace.
                assert skeleton.bone(bone)


def test_idle_breathe_is_base_and_therefore_cannot_key_the_head(clips: ClipLibrary):
    clip = clips["idle_breathe"]
    assert clip.channel == "BASE" and clip.loop
    assert "head" not in clip.bones() and "neck" not in clip.bones()


def test_asking_for_an_unknown_clip_lists_what_there_is(clips: ClipLibrary):
    with pytest.raises(AnimError, match="idle_breathe"):
        clips["idle_wiggle"]


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------


def test_linear_interpolation_between_two_keys():
    c = make(base_clip())
    assert c.sample(0.0).get("body").drot == pytest.approx(0.0)
    assert c.sample(0.25).get("body").drot == pytest.approx(2.5)
    assert c.sample(1.0).get("body").drot == pytest.approx(10.0)


def test_a_value_holds_before_the_first_key_and_after_the_last():
    c = make(base_clip(duration=2.0, tracks={
        "body": [{"t": 0.5, "rot": 4}, {"t": 1.0, "rot": 8}]}))
    assert c.sample(0.0).get("body").drot == pytest.approx(4.0)
    assert c.sample(2.0).get("body").drot == pytest.approx(8.0)


def test_absent_fields_are_independent_timelines():
    """A key carries only what it changes, so the four fields are four tracks."""
    c = make(base_clip(tracks={"body": [
        {"t": 0.0, "x": 0, "rot": 0},
        {"t": 0.5, "rot": 20},
        {"t": 1.0, "x": 100},
    ]}))
    d = c.sample(0.5)
    body = d.get("body")
    assert body.drot == pytest.approx(20.0)
    assert body.dx == pytest.approx(50.0), "x interpolates 0->100 across 0->1"
    assert body.dy == 0.0
    assert body.scale == 1.0, "scale defaults to the multiplicative identity"


def test_scale_is_a_multiplier_not_an_additive_delta():
    c = make(base_clip(tracks={"body": [{"t": 0.0, "scale": 1.0},
                                        {"t": 1.0, "scale": 2.0}]}))
    assert c.sample(0.0).get("body").scale == pytest.approx(1.0)
    assert c.sample(0.5).get("body").scale == pytest.approx(1.5)


@pytest.mark.parametrize("ease,at_half", [
    ("linear", 5.0), ("in", 2.5), ("out", 7.5), ("inout", 5.0)])
def test_each_ease_shape(ease, at_half):
    c = make(base_clip(tracks={"body": [{"t": 0.0, "rot": 0, "ease": ease},
                                       {"t": 1.0, "rot": 10}]}))
    assert c.sample(0.5).get("body").drot == pytest.approx(at_half)


def test_an_ease_governs_the_segment_leaving_its_key():
    """The contract does not say which segment; this module says the outgoing
    one, and an ease on the final key is therefore inert and warned about."""
    c = make(base_clip(tracks={"body": [{"t": 0.0, "rot": 0},
                                        {"t": 1.0, "rot": 10, "ease": "in"}]}))
    assert c.sample(0.5).get("body").drot == pytest.approx(5.0), (
        "the ease on the LAST key must not affect the preceding segment")
    assert any("inert" in w for w in c.warnings)


def test_an_unknown_ease_is_refused_by_name():
    with pytest.raises(AnimError, match="bounce"):
        make(base_clip(tracks={"body": [{"t": 0.0, "rot": 0, "ease": "bounce"},
                                        {"t": 1.0, "rot": 1}]}))


def test_binary_search_agrees_with_a_linear_scan():
    keys = [{"t": i * 0.1, "rot": float(i * i)} for i in range(11)]
    c = make(base_clip(duration=1.0, tracks={"body": keys}))
    for i in range(101):
        t = i / 100.0
        seg = min(9, int(t * 10))
        t0, t1 = seg * 0.1, (seg + 1) * 0.1
        v0, v1 = float(seg * seg), float((seg + 1) ** 2)
        u = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        expect = v0 + (v1 - v0) * min(1.0, max(0.0, u))
        assert c.sample(t).get("body").drot == pytest.approx(expect)


# ---------------------------------------------------------------------------
# Load-time refusals
# ---------------------------------------------------------------------------


def test_a_looping_clip_that_pops_is_refused_and_names_the_track_and_field():
    with pytest.raises(AnimError) as exc:
        make(base_clip(loop=True))
    msg = str(exc.value)
    assert "LOOP POP" in msg and "body" in msg and "rot" in msg


def test_a_loop_pop_is_caught_per_field_not_only_per_track():
    """x loops cleanly while rot does not; the clip must still be refused."""
    with pytest.raises(AnimError, match="field rot"):
        make(base_clip(loop=True, tracks={"body": [
            {"t": 0.0, "x": 0, "rot": 0}, {"t": 1.0, "x": 0, "rot": 9}]}))


def test_a_key_past_the_duration_can_never_be_reached():
    with pytest.raises(AnimError, match="never be reached"):
        make(base_clip(tracks={"body": [{"t": 0.0, "rot": 0}, {"t": 1.5, "rot": 1}]}))


def test_a_negative_time_is_refused():
    with pytest.raises(AnimError, match="< 0"):
        make(base_clip(tracks={"body": [{"t": -0.1, "rot": 0}, {"t": 1.0, "rot": 0}]}))


def test_two_keys_at_the_same_time_on_one_field_are_refused():
    with pytest.raises(AnimError, match="two keys at"):
        make(base_clip(tracks={"body": [{"t": 0.5, "rot": 0}, {"t": 0.5, "rot": 5}]}))


def test_a_key_that_changes_nothing_is_refused():
    with pytest.raises(AnimError, match="changes nothing"):
        make(base_clip(tracks={"body": [{"t": 0.0}, {"t": 1.0, "rot": 1}]}))


def test_an_unknown_key_field_is_refused_rather_than_ignored():
    with pytest.raises(AnimError, match="unknown field"):
        make(base_clip(tracks={"body": [{"t": 0.0, "rotation": 0},
                                        {"t": 1.0, "rot": 1}]}))


def test_a_zero_duration_is_refused():
    with pytest.raises(AnimError, match="duration"):
        make(base_clip(duration=0.0))


def test_a_track_naming_a_slot_is_refused(skeleton: Skeleton):
    with pytest.raises(AnimError, match="Slots are not"):
        make(base_clip(tracks={"jacket_front": [{"t": 0.0, "rot": 0}]}),
             skeleton=skeleton)


def test_a_track_naming_nothing_at_all_is_refused(skeleton: Skeleton):
    with pytest.raises(AnimError, match="not a bone"):
        make(base_clip(tracks={"torso": [{"t": 0.0, "rot": 0}]}), skeleton=skeleton)


def test_an_empty_tracks_object_is_refused():
    with pytest.raises(AnimError, match="non-empty"):
        make(base_clip(tracks={}))


def test_a_file_whose_clip_name_disagrees_with_its_stem_is_refused(
        write_clip, skeleton: Skeleton, channels: ChannelMap):
    p = write_clip("walk", base_clip(name="run"))
    with pytest.raises(AnimError, match="fork waiting to drift"):
        Clip.load(p, skeleton=skeleton, channel_map=channels)


def test_loading_a_directory_with_no_clips_is_refused(tmp_path, skeleton, channels):
    (tmp_path / "empty").mkdir()
    with pytest.raises(AnimError, match="no .*anim"):
        ClipLibrary.load_dir(tmp_path / "empty", skeleton=skeleton,
                             channel_map=channels)


def test_a_clip_round_trips_through_a_file(write_clip, skeleton, channels):
    p = write_clip("t", base_clip())
    clip = Clip.load(p, skeleton=skeleton, channel_map=channels)
    assert clip.source == p
    assert json.loads(p.read_text(encoding="utf-8"))["name"] == "t"
    assert clip.sample(0.5).get("body").drot == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def test_a_looping_clip_wraps_and_a_held_clip_clamps():
    loop = make(base_clip(loop=True, duration=2.0, tracks={
        "body": [{"t": 0.0, "rot": 0}, {"t": 1.0, "rot": 10}, {"t": 2.0, "rot": 0}]}))
    held = make(base_clip(duration=2.0, tracks={
        "body": [{"t": 0.0, "rot": 0}, {"t": 2.0, "rot": 10}]}))
    assert loop.local_time(5.0) == pytest.approx(1.0)
    assert loop.sample(5.0).get("body").drot == pytest.approx(10.0)
    assert held.local_time(5.0) == pytest.approx(2.0)
    assert held.sample(5.0).get("body").drot == pytest.approx(10.0)


def test_negative_time_is_handled_on_both_kinds():
    loop = make(base_clip(loop=True, duration=2.0, tracks={
        "body": [{"t": 0.0, "rot": 0}, {"t": 1.0, "rot": 10}, {"t": 2.0, "rot": 0}]}))
    held = make(base_clip(duration=2.0))
    assert 0.0 <= loop.local_time(-0.5) <= 2.0
    assert held.local_time(-3.0) == 0.0


def test_sampling_is_pure(clips: ClipLibrary):
    clip = clips["dance"]
    a = clip.sample(0.37)
    for _ in range(5):
        assert clip.sample(0.37).close_to(a, tol=0.0)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_the_default_channels_partition_the_skeleton(channels: ChannelMap,
                                                     skeleton: Skeleton):
    union: set[str] = set()
    total = 0
    for chan in channels.channels:
        s = channels.bones(chan)
        union |= set(s)
        total += len(s)
    assert union == set(skeleton.bone_names)
    assert total == len(skeleton.bones), "the sets must be disjoint, not merely cover"
    assert channels.bones("HEAD") == frozenset({"head", "neck"})
    assert channels.bones("FACE") == frozenset({"eye_L", "eye_R", "beak", "visor"})
    assert channels.bones("PROP") == frozenset({"prop_anchor"})
    assert len(channels.bones("BASE")) == 16


def test_overlapping_channels_are_refused_with_the_overlap_named(skeleton: Skeleton):
    with pytest.raises(AnimError, match="head"):
        ChannelMap(skeleton, {"HEAD": ("neck", "head"), "FACE": ("head", "beak")})


def test_a_channel_naming_an_unknown_bone_is_refused(skeleton: Skeleton):
    with pytest.raises(AnimError, match="torso"):
        ChannelMap(skeleton, {"HEAD": ("torso",)})


def test_base_may_not_be_declared_explicitly(skeleton: Skeleton):
    with pytest.raises(AnimError, match="complement"):
        ChannelMap(skeleton, {"BASE": ("root",), "HEAD": ("head",)})


def test_a_clip_may_not_key_a_bone_outside_its_channel(skeleton, channels):
    with pytest.raises(AnimError, match="channel HEAD"):
        make(base_clip(channel="HEAD", tracks={"body": [{"t": 0.0, "rot": 0}]}),
             skeleton=skeleton, channel_map=channels)


def test_an_unknown_channel_is_refused(skeleton, channels):
    with pytest.raises(AnimError, match="unknown channel"):
        make(base_clip(channel="SKIN"), skeleton=skeleton, channel_map=channels)


def test_skin_is_not_a_channel(channels: ChannelMap):
    assert "SKIN" not in channels.sets
    assert "SKIN" not in PRECEDENCE


def test_channel_of_answers_for_every_bone(channels: ChannelMap, skeleton: Skeleton):
    for name in skeleton.bone_names:
        assert channels.channel_of(name) in channels.sets


# ---------------------------------------------------------------------------
# Layered
# ---------------------------------------------------------------------------


def test_layering_three_channels_unions_their_bones(channels, clips):
    lay = Layered(channels)
    lay.play(clips["idle_breathe"])
    lay.play(clips["look_left"])
    lay.play(clips["blink"])
    pose = lay.sample(0.09)
    assert set(pose.bones()) == (
        set(clips["idle_breathe"].bones())
        | set(clips["look_left"].bones())
        | set(clips["blink"].bones()))


def test_a_second_clip_on_one_channel_replaces_the_first(channels, clips):
    lay = Layered(channels)
    lay.play(clips["look_left"])
    lay.play(clips["look_right"])
    assert lay.active() == {"HEAD": clips["look_right"]}
    assert lay.sample(0.5).get("head").drot == pytest.approx(10.0)


def test_stopping_a_channel_removes_its_deltas(channels, clips):
    lay = Layered(channels)
    lay.play(clips["idle_breathe"])
    lay.play(clips["look_left"])
    lay.stop("HEAD")
    assert "head" not in lay.sample(0.5).bones()


def test_the_start_time_offsets_the_clip(channels, clips):
    lay = Layered(channels)
    lay.play(clips["look_left"], at=10.0)
    assert lay.sample(10.0).get("head").drot == pytest.approx(0.0)
    assert lay.sample(10.5).get("head").drot == pytest.approx(-10.0)


def test_an_empty_layered_samples_to_an_empty_pose(channels):
    assert Layered(channels).sample(1.0) == Pose.empty()


def test_precedence_order_is_written_down_once():
    assert PRECEDENCE == ("BASE", "HEAD", "FACE", "PROP")
