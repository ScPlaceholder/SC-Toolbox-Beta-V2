"""THE FOUR DISCRIMINATING TESTS, plus the owner's freeze criterion.

These are not taste. Tests 1 and 2 were derived twice independently — once from
16 hours of transform-space pain on the July Loom rig, and once from the art
side by the rig pack's author, who had not read the engineering contract. Two
derivations converging is the strongest evidence either side has that these are
the right tests, so they are simultaneously the engineering gate and the
owner's "freeze the rig" trigger.

    1. idle_breathe keeps the flippers DOWN.
    2. A skin swap mid-clip moves ZERO bones.
    3. Two channels on disjoint bone sets COMMUTE.
    4. A loop played twice returns to its exact starting pose.

Every one of them is also run against deliberately WRONG implementations in
`test_control.py`, because a test that only ever sees the right answer cannot
tell you whether it would notice the wrong one.
"""

from __future__ import annotations

import inspect
import math

import pytest

from pico import selftest as st
from pico.anim import Clip, Layered
from pico.rig import Delta, Pose, Rig


# ===========================================================================
# 1. idle_breathe keeps the flippers DOWN
# ===========================================================================


def test_1_idle_breathe_keeps_the_flippers_down(rig, clips):
    """The boring clip is the discriminating one.

    A transform-space or rotation-order fault gives a splayed penguin, and a
    splayed penguin still reads as "some animation" — plausibly a wave. So this
    runs on the low-motion idle and never on `dance`.
    """
    clip = clips["idle_breathe"]
    rest = rig.bone_points()
    assert rest["hand_L"][1] - rest["shoulder_L"][1] == pytest.approx(340.0), (
        "the fixture's premise: at rest each hand hangs 340px below its "
        "shoulder. If that changed, the thresholds below are stale.")

    worst_sep, worst_drift, least_drift = math.inf, 0.0, math.inf
    for i in range(121):
        t = clip.duration * i / 120.0
        pts = rig.bone_points(clip.sample(t))
        for hand, shoulder in (("hand_L", "shoulder_L"), ("hand_R", "shoulder_R")):
            sep = pts[hand][1] - pts[shoulder][1]
            worst_sep = min(worst_sep, sep)
            assert sep >= st.ARMS_DOWN_MIN_DY, (
                "SPLAYED PENGUIN at t=%.3f: %s only %.1fpx below %s"
                % (t, hand, sep, shoulder))
            drift = math.hypot(pts[hand][0] - rest[hand][0],
                               pts[hand][1] - rest[hand][1])
            worst_drift = max(worst_drift, drift)
        assert pts["hand_L"][0] < pts["shoulder_L"][0]
        assert pts["hand_R"][0] > pts["shoulder_R"][0]

    assert worst_drift <= st.IDLE_MAX_DRIFT_PX, (
        "motion ARRIVING from somewhere unauthored: %.2fpx" % worst_drift)
    assert worst_drift >= st.IDLE_MIN_DRIFT_PX, (
        "motion MISSING: the breath is authored on body and spine_upper, so "
        "%.2fpx at the hands means the parent rotation is not reaching them"
        % worst_drift)


def test_1b_why_idle_breathe_and_not_dance_measured_not_assumed(rig, clips):
    """⚠ THE CONTRACT'S REASON FOR THIS FIXTURE DOES NOT SURVIVE MEASUREMENT.

    PICO_CONTRACT.md says "Do not validate the rig on idle_dance; an expressive
    clip hides everything." I wrote this test to confirm it and it FAILED.
    Measured 2026-09-27, minimum hand-shoulder separation and peak hand drift
    for the contract maths and each of five wrong implementations:

                            idle_breathe        dance          wave
        contract            332 / 23.0      289 / 91.1     275 / 110.5
        order_SRT           327 / 26.9      212 / 198.6     150 / 257.8
        reversed_compose    265 / 82.8     -395 / 773.9    -821 / 1151.8
        parent_relative     314 / 24.3      259 /  96.6     242 / 111.4
        drop_parent_rot     349 /  6.0      349 /  14.0     349 /   0.0
        accumulating        139 / 620.0      63 / 923.9     139 / 232.4

    An arms-down FLOOR 32px under each clip's own correct worst case catches
    2 of 5 on idle_breathe, 3 of 5 on dance and 4 of 5 on wave. An expressive
    clip AMPLIFIES the fault, and for an automated numeric bound it is
    therefore MORE sensitive, not less.

    The contract's claim is about a HUMAN check — "arms-up looked plausibly
    taunt-ish and hid the bug" — and there it is correct, because a person
    cannot hold dance's expected pose in their head. It does not transfer to a
    bound with a rest reference.

    SO WHY IS idle_breathe STILL THE FIXTURE? Because its expected envelope is
    PREDICTABLE FROM THE CLIP: the breath is 1.5 degrees on spine_upper over a
    340px arm, so ~23px at the hands is derivable before running anything, and
    a band of [12, 25] is defensible. dance's 91px is a number nobody could
    have predicted and can only be recorded after the fact, so any band around
    it encodes today's output rather than an expectation. A tight band on a
    predictable clip beats a loose band on an unpredictable one — which is a
    different argument from the contract's, and it is the one that holds.
    """
    rest = rig.bone_points()

    def envelope(name):
        clip = clips[name]
        min_sep, max_drift = math.inf, 0.0
        for i in range(121):
            pts = rig.bone_points(clip.sample(clip.duration * i / 120.0))
            for hand, shoulder in (("hand_L", "shoulder_L"),
                                   ("hand_R", "shoulder_R")):
                min_sep = min(min_sep, pts[hand][1] - pts[shoulder][1])
                max_drift = max(max_drift, math.hypot(
                    pts[hand][0] - rest[hand][0], pts[hand][1] - rest[hand][1]))
        return min_sep, max_drift

    idle_sep, idle_drift = envelope("idle_breathe")
    dance_sep, dance_drift = envelope("dance")

    # THE DERIVED UPPER BOUND, computed from the clip and the skeleton with no
    # reference to what the rig produced. Each keyed ancestor contributes at
    # most its peak translation, plus its peak rotation as an arc at the hand's
    # rest radius from that bone's pivot. The triangle inequality makes the sum
    # an upper bound; the true answer is smaller because the contributions are
    # not collinear.
    #
    # ⚠ MY FIRST ATTEMPT AT THIS PREDICTION WAS WRONG AND THE TEST CAUGHT IT.
    # I wrote "1.5 degrees over a 340px arm" = 8.9px against a measured 23.0,
    # having counted one of three contributions and measured the radius from
    # the wrong pivot. A single-term estimate dressed up as a derivation is
    # worse than an honest recorded number, because it looks principled.
    def derived_bound(clip_name: str, hand: str) -> float:
        clip = clips[clip_name]
        total = 0.0
        chain = (hand,) + rig.skeleton.ancestors(hand)
        for bone, track in clip.tracks.items():
            if bone not in chain:
                continue
            peak_t = math.hypot(
                max((abs(k.value) for k in track.fields.get("x", ())), default=0.0),
                max((abs(k.value) for k in track.fields.get("y", ())), default=0.0))
            peak_rot = max((abs(k.value) for k in track.fields.get("rot", ())),
                           default=0.0)
            radius = math.hypot(rest[hand][0] - rest[bone][0],
                                rest[hand][1] - rest[bone][1])
            total += peak_t + math.radians(peak_rot) * radius
        return total

    bound = max(derived_bound("idle_breathe", h) for h in ("hand_L", "hand_R"))
    assert idle_drift <= bound + 1e-6, (
        "measured %.2fpx exceeds the derived upper bound %.2fpx; the bound is "
        "a sum of independent contributions and cannot be beaten"
        % (idle_drift, bound))
    assert idle_drift >= bound * 0.4, (
        "measured %.2fpx is under 40%% of the %.2fpx the clip's own keys "
        "predict — the motion is not arriving" % (idle_drift, bound))
    assert st.IDLE_MAX_DRIFT_PX >= bound, (
        "the ceiling in test 1 is %.1fpx but the clip's keys can legitimately "
        "produce %.2fpx. A ceiling below the derived bound is encoding today's "
        "output, not an expectation." % (st.IDLE_MAX_DRIFT_PX, bound))

    # And the uncomfortable half, pinned so it cannot quietly rot back into
    # the contract's version.
    assert dance_drift > idle_drift * 3.0, (
        "dance drift %.1f vs idle %.1f — the amplification finding above "
        "depends on this and it has changed" % (dance_drift, idle_drift))
    assert dance_sep < idle_sep, (
        "dance is supposed to bring the hands CLOSER to the shoulders than the "
        "idle does; that is why its floor is more sensitive")


def test_1c_the_flippers_stay_below_the_shoulders_in_every_gate_clip(rig, clips):
    """Weaker than test 1 and true of every clip, including death_flop.

    death_flop deliberately splays, so its bound is the loose one: the hands
    may rise but must not end up ABOVE the shoulders, which would mean the
    45-degree flipper rotation is being composed in the wrong direction.
    """
    for name in st.GATE_CLIPS:
        clip = clips[name]
        for i in range(41):
            pts = rig.bone_points(clip.sample(clip.duration * i / 40.0))
            for hand, shoulder in (("hand_L", "shoulder_L"), ("hand_R", "shoulder_R")):
                assert pts[hand][1] > pts[shoulder][1], (
                    "%s t=%.3f: %s rose above %s"
                    % (name, clip.duration * i / 40.0, hand, shoulder))


# ===========================================================================
# 2. A skin swap moves ZERO bones
# ===========================================================================


def test_2_a_skin_swap_moves_zero_bones(rig, clips, skeleton):
    """Same clip, same frame, two skins: every one of 26 slot matrices equal."""
    compared = 0
    for name in st.GATE_CLIPS:
        clip = clips[name]
        for frac in (0.0, 0.17, 0.5, 0.83, 1.0):
            pose = clip.sample(clip.duration * frac)
            base = rig.slot_transforms(pose, st.SKIN_BASE)
            drake = rig.slot_transforms(pose, st.SKIN_DRAKE)
            bare = rig.slot_transforms(pose, None)
            assert len(base) == len(skeleton.slots) == 26
            for a, b, n in zip(base, drake, bare):
                assert a.slot == b.slot == n.slot
                assert a.bone == b.bone == n.bone
                assert a.matrix == b.matrix == n.matrix, (
                    "%s t=%.3f slot %s moved with the skin" % (name, frac, a.slot))
                compared += 1
    assert compared == 26 * 5 * len(st.GATE_CLIPS)


def test_2b_the_skin_fixtures_actually_differ(rig):
    """A differential test whose two arms are identical scores a perfect pass.

    So before believing the assertion above: prove the two skins are genuinely
    different art, in different slots, with one carrying slots the other leaves
    empty. Without this the test passes for a fixture that swaps nothing.
    """
    base = {d.slot: d.attachments for d in rig.slot_transforms(None, st.SKIN_BASE)}
    drake = {d.slot: d.attachments for d in rig.slot_transforms(None, st.SKIN_DRAKE)}
    assert base != drake
    differing = [s for s in base if base[s] != drake[s]]
    assert len(differing) >= 5, differing
    only_drake = [s for s in drake if drake[s] and not base[s]]
    assert {"jacket_front", "headwear", "sleeve_L"} <= set(only_drake)


def test_2c_no_art_parameter_can_reach_the_transform_maths():
    """The structural half of test 2, which the functional half cannot give.

    Two skins agreeing proves these two skins agree. A signature with no art
    parameter proves no skin ever can.
    """
    for fn, label in ((Rig.pose_world, "Rig.pose_world"),
                      (Rig.bone_points, "Rig.bone_points"),
                      (Clip.sample, "Clip.sample"),
                      (Layered.sample, "Layered.sample")):
        params = set(inspect.signature(fn).parameters) - {"self"}
        leak = sorted(p for p in params if any(
            w in p.lower() for w in ("skin", "attach", "art", "sprite", "texture")))
        assert not leak, "%s takes %s" % (label, leak)


def test_2d_an_animation_never_names_a_skin(clips):
    """`dance.anim` does not know that Pico is wearing Drake clothes."""
    for name in clips:
        clip = clips[name]
        blob = repr(clip.tracks) + clip.name + clip.channel
        for word in ("drake", "origin", "aegis", "rsi", "anvil", "argo", "skin"):
            assert word not in blob.lower(), "%s mentions %r" % (name, word)


# ===========================================================================
# 3. Two channels on disjoint bone sets COMMUTE
# ===========================================================================


def test_3_head_then_face_equals_face_then_head(channels, clips):
    """Demanded EXACTLY. A disjoint union has no excuse for a 1e-9."""
    head, face = clips["look_left"], clips["blink"]
    a, b = Layered(channels), Layered(channels)
    a.play(head); a.play(face)
    b.play(face); b.play(head)
    pa, pb = a.sample(0.09), b.sample(0.09)
    assert pa.close_to(pb, tol=0.0), pa.diff(pb)
    assert set(pa.bones()) == {"head", "neck", "eye_L", "eye_R"}


def test_3b_all_orderings_of_three_channels_agree_exactly(channels, clips):
    import itertools
    trio = [clips["idle_breathe"], clips["look_left"], clips["blink"]]
    poses = []
    for order in itertools.permutations(trio):
        lay = Layered(channels)
        for clip in order:
            lay.play(clip)
        poses.append(lay.sample(0.09))
    assert len(poses) == 6
    for p in poses[1:]:
        assert p.close_to(poses[0], tol=0.0), p.diff(poses[0])


def test_3c_commuting_is_a_consequence_of_disjointness_not_of_luck(channels):
    """If the sets ever overlap, the union raises instead of picking a winner."""
    a = Pose.of({"head": Delta(drot=5)})
    b = Pose.of({"head": Delta(drot=-5)})
    with pytest.raises(Exception, match="order-dependent"):
        a.merge_disjoint(b)


def test_3d_world_matrices_commute_too_not_only_the_pose(rig, channels, clips):
    head, face = clips["look_left"], clips["blink"]
    a, b = Layered(channels), Layered(channels)
    a.play(head); a.play(face)
    b.play(face); b.play(head)
    wa = rig.pose_world(a.sample(0.09)).world
    wb = rig.pose_world(b.sample(0.09)).world
    for bone in wa:
        assert wa[bone] == wb[bone], bone


# ===========================================================================
# 4. A loop played twice returns to its exact starting pose
# ===========================================================================


def test_4_a_loop_played_twice_returns_to_its_starting_pose(rig, clips):
    """STEPPED PLAYBACK, not a comparison of the two seam instants.

    Comparing only `sample(0)` with `sample(duration)` catches nothing: at the
    seam a looping clip's pose is the identity delta, and accumulating the
    identity is idempotent, so a rig that adds each pose onto the last looks
    perfect there. Measured 2026-09-27 — the seam-only form passed for all five
    wrong implementations, including the accumulating one it exists to catch.
    Walking the clip in 60 steps separates them by 787px.
    """
    steps, cycles = 60, 3
    for name in ("idle_breathe", "wave", "dance"):
        clip = clips[name]
        assert clip.loop
        rig.reset_accumulator()
        marks = {}
        for i in range(cycles * steps + 1):
            world = rig.pose_world(clip.sample(clip.duration * i / steps)).world
            if i % steps == 0:
                marks[i // steps] = dict(world)
        first = marks[0]
        for cycle in range(1, cycles + 1):
            for bone, m in marks[cycle].items():
                assert m.close_to(first[bone], 1e-6), (
                    "%s: %s drifted after %d loop(s)" % (name, bone, cycle))


def test_4b_the_seam_pose_is_the_identity_which_is_why_the_naive_form_is_blind(clips):
    """Records the reason test 4 is written the way it is."""
    for name in ("idle_breathe", "wave", "dance"):
        pose = clips[name].sample(0.0)
        assert all(d.is_identity for d in pose.deltas.values()), (
            "%s does not start at rest, so the note in test_4 needs revisiting"
            % name)


def test_4c_mid_phase_is_identical_across_cycles(clips):
    for name in ("idle_breathe", "wave", "dance"):
        clip = clips[name]
        for phase in (0.13, 0.37, 0.5, 0.91):
            a = clip.sample(clip.duration * phase)
            for cycles in (1, 2, 5, 97):
                b = clip.sample(clip.duration * (phase + cycles))
                assert b.close_to(a, tol=1e-9), (name, phase, cycles, b.diff(a))


def test_4d_a_held_clip_does_not_wrap(clips):
    for name in ("blink", "look_left", "look_right", "death_flop"):
        clip = clips[name]
        assert not clip.loop
        end = clip.sample(clip.duration)
        assert clip.sample(clip.duration * 5).close_to(end, tol=0.0), (
            "%s wrapped; a non-looping clip holds its last key" % name)


# ===========================================================================
# The owner's freeze criterion, executed
# ===========================================================================


def test_gate_both_skins_run_all_six_clips_with_identical_animation_data(
        rig, clips, skeleton):
    """"Bind Base Pico, then Drake. Test idle_breathe, blink, look_left/right,
    wave, dance, death_flop. If both skins run the six clips with identical
    animation data, freeze the rig."
    """
    frames = 0
    for name in st.GATE_CLIPS:
        clip = clips[name]
        for i in range(41):
            t = clip.duration * i / 40.0
            pose = clip.sample(t)
            res = rig.pose_world(pose)
            assert not res.clamps, (
                "%s clamps at t=%.3f: %s — the gate clips are authored inside "
                "their limits" % (name, t, [e.describe() for e in res.clamps]))
            for bone, m in res.world.items():
                assert all(math.isfinite(v) for v in m), (name, t, bone)
            base = [s.matrix for s in rig.slot_transforms(pose, st.SKIN_BASE)]
            drake = [s.matrix for s in rig.slot_transforms(pose, st.SKIN_DRAKE)]
            assert base == drake
            frames += 1
    assert frames == 41 * len(st.GATE_CLIPS)


def test_gate_covers_every_channel(clips, channels):
    used = {clips[n].channel for n in st.GATE_CLIPS}
    assert "BASE" in used and "HEAD" in used and "FACE" in used
    assert "PROP" not in used, (
        "no gate clip exercises PROP. Recorded, not asserted as fine: the "
        "channel exists, is disjoint, and has no clip. CANNOT TELL whether a "
        "prop clip will behave until one exists.")


def test_gate_clips_stay_inside_the_canvas_horizontally(rig, clips, skeleton):
    """A weak sanity bound, and labelled as such.

    Clipping the character to the host viewport is `constraints.
    dashboard_boundary` and belongs to the RENDERER; this layer cannot see a
    viewport. All this asserts is that nothing flies off the 2048 master canvas
    in x, which would mean a transform blew up.
    """
    for name in st.GATE_CLIPS:
        clip = clips[name]
        for i in range(41):
            for bone, (x, y) in rig.bone_points(
                    clip.sample(clip.duration * i / 40.0)).items():
                assert -200.0 <= x <= skeleton.canvas.width + 200.0, (name, bone, x)
                assert -200.0 <= y <= skeleton.canvas.height + 200.0, (name, bone, y)
