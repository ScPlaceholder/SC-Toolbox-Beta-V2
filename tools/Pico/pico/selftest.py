"""`--selftest` for the Pico rig runtime, WITH A CONTROL.

    py -3.13 -m pico.selftest            # run everything
    py -3.13 -m pico.selftest --verbose  # plus every check's detail line

WHY A CONTROL AND NOT JUST ASSERTIONS
-------------------------------------
A suite that only asserts the right answer cannot distinguish a discriminating
fixture from a fixture that would accept anything. So every check here is run
twice: once against the contract's maths, and once against each of several
DELIBERATELY WRONG implementations that an engineer could plausibly write.

The verdict has two halves and BOTH must hold:

    BASELINE   the contract implementation passes every check
    CONTROL    every wrong implementation is caught by at least one check

If the baseline fails, the control is meaningless — a differential test with a
broken control scores a perfect 100%, because every mutant dies regardless of
what it does. If a wrong implementation passes everything, the checks are not
discriminating and this module says so in capitals instead of printing OK.

Exit codes: 0 all good, 1 a real failure, 2 the checks are not discriminating.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import sys
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Optional

if __package__ in (None, ""):  # allow `python selftest.py` from this directory
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pico.anim import AnimError, ChannelMap, Clip, ClipLibrary, Layered
from pico.rig import (
    ConstraintPolicy,
    Mat2D,
    Rig,
    RigError,
    Skeleton,
    TransformOps,
    default_skeleton_path,
    verify_skeleton_provenance,
)

# ---------------------------------------------------------------------------
# Deliberately wrong implementations
# ---------------------------------------------------------------------------

#: Each of these is a mistake someone could make while reading the contract.
#: The comment is the mistake in one sentence.
WRONG_OPS: Mapping[str, TransformOps] = {
    # local = scale @ rotate @ translate: the translation ends up inside the
    # bone's own rotation, so a rotated bone's PIVOT moves.
    "order_SRT": TransformOps(
        local_matrix=lambda tx, ty, rot, s: (
            Mat2D.scale(s).mul(Mat2D.rotate_deg(rot)).mul(Mat2D.translate(tx, ty))
        ),
        name="order_SRT",
    ),
    # child = local @ parent instead of parent @ local. The classic.
    "reversed_compose": TransformOps(
        compose=lambda p, l: l.mul(p),
        name="reversed_compose",
    ),
    # Treat the JSON's absolute coordinates as a plain offset from the parent,
    # ignoring the parent's rest rotation. Only wrong where a parent is rotated,
    # which in this rig is exactly the two flippers.
    "parent_relative_coords": TransformOps(
        derive_local=lambda pw, x, y: (x - pw.tx, y - pw.ty),
        name="parent_relative_coords",
    ),
    # Inherit the parent's POSITION but not its rotation or scale: the "my
    # bones follow but never swing" bug.
    "drop_parent_rotation": TransformOps(
        compose=lambda p, l: Mat2D.translate(p.tx, p.ty).mul(l),
        name="drop_parent_rotation",
    ),
    # Accumulate deltas across calls instead of recomputing from rest.
    "accumulating": TransformOps(accumulate=True, name="accumulating"),
}


def _skeleton_with_flipper(lo: float, hi: float) -> Skeleton:
    d = json.loads(default_skeleton_path().read_text(encoding="utf-8"))
    d["constraints"]["flipper_rotation_deg"] = [lo, hi]
    return Skeleton.from_dict(d, source=default_skeleton_path())


#: Constraint-side mutations. Not expressible as TransformOps.
WRONG_CONSTRAINTS: Mapping[str, Callable[[], tuple[Skeleton, ConstraintPolicy]]] = {
    # "Tidy" the asymmetric flipper range to +/-70. The contract warns about
    # this by name; this proves the warning has teeth.
    "tidied_flipper_symmetric": lambda: (
        _skeleton_with_flipper(-70.0, 70.0), ConstraintPolicy()
    ),
    # Constraints declared but never wired to any bone: clamping silently
    # becomes a no-op and every limit is decorative.
    "clamping_not_wired": lambda: (Skeleton.load(), ConstraintPolicy(groups={})),
}


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class Ctx(NamedTuple):
    skeleton: Skeleton
    rig: Rig
    channels: ChannelMap
    clips: ClipLibrary


def build(
    ops: TransformOps = TransformOps(),
    *,
    skeleton: Optional[Skeleton] = None,
    policy: Optional[ConstraintPolicy] = None,
    clip_dir: Optional[Path] = None,
) -> Ctx:
    sk = skeleton or Skeleton.load()
    cm = ChannelMap(sk)
    lib = ClipLibrary.load_dir(clip_dir, skeleton=sk, channel_map=cm)
    rig = Rig(sk, ops=ops, policy=policy)
    return Ctx(skeleton=sk, rig=rig, channels=cm, clips=lib)


# ---------------------------------------------------------------------------
# The checks. Each raises AssertionError with a message a human can act on.
# ---------------------------------------------------------------------------

#: The six clips J's START_HERE names as the freeze gate, plus the two files
#: look_left/look_right that his item 3 is shorthand for.
GATE_CLIPS = ("idle_breathe", "blink", "look_left", "look_right",
              "wave", "dance", "death_flop")

#: Two skins. Deliberately different art in different slots, including slots
#: the other one leaves empty.
SKIN_BASE: Mapping[str, tuple[str, ...]] = {
    "head_base": ("pico_head",), "belly": ("pico_belly",),
    "flipper_L": ("pico_flipper_l",), "flipper_R": ("pico_flipper_r",),
    "beak": ("pico_beak",),
}
SKIN_DRAKE: Mapping[str, tuple[str, ...]] = {
    "head_base": ("drake_head",), "belly": ("drake_belly",),
    "flipper_L": ("drake_flipper_l",), "flipper_R": ("drake_flipper_r",),
    "beak": ("drake_beak",),
    "jacket_front": ("drake_jacket_front",), "jacket_back": ("drake_jacket_back",),
    "sleeve_L": ("drake_sleeve_l",), "sleeve_R": ("drake_sleeve_r",),
    "headwear": ("drake_cap",), "belt_gear": ("drake_belt",),
}

#: Arms-down thresholds for idle_breathe. Rest separation is 340px.
ARMS_DOWN_MIN_DY = 300.0

#: A CEILING and a FLOOR, and the floor is the one that was missing.
#:
#: MEASURED 2026-09-27, peak hand drift from rest over idle_breathe:
#:     contract maths            22.995 px
#:     order_SRT                 26.911
#:     reversed_compose          82.769
#:     parent_relative_coords    24.300
#:     drop_parent_rotation       6.000   <-- LESS motion, not more
#:     accumulating             619.953
#:
#: The ceiling alone let `drop_parent_rotation` through: a mutation that stops
#: the spine's rotation reaching the hands makes the idle MORE still, and a
#: check whose only bound is "must stay near rest" reads that as a pass. So the
#: floor is not decoration — it is the half of this check that notices the
#: animation chain has gone quiet. 12.0 sits between 6.0 and 22.995.
#:
#: ★ THE CEILING IS DERIVED, NOT OBSERVED. `tests/test_gate.py::test_1b`
#: computes the clip's own upper bound from the skeleton and the keys — peak
#: translation plus each keyed ancestor's rotation as an arc at the hand's rest
#: radius — which comes to 25.15px for idle_breathe, and asserts this constant
#: is at least that. A ceiling below the derived bound would be encoding today's
#: output. 26.0 is the derived bound plus ~3%.
#: ⚠ order_SRT sits at 26.91, only 0.9px past it, so the ceiling catches that
#: mutation NARROWLY. `rest_positions` catches it decisively; do not rely on
#: this margin.
IDLE_MAX_DRIFT_PX = 26.0
IDLE_MIN_DRIFT_PX = 12.0


def check_rest_positions(c: Ctx) -> str:
    """Every bone's rest world origin equals its authored absolute x/y."""
    bad = c.rig.check_rest_positions()
    assert not bad, (
        "%d bone(s) do not sit at their authored coordinates AT REST: %s. "
        "The rig is wrong before a single keyframe." % (len(bad), ", ".join(bad))
    )
    return "23/23 bones at their authored absolute coordinates"


def check_arms_down(c: Ctx) -> str:
    """idle_breathe keeps the flippers DOWN, sampled across the whole clip.

    THE discriminating test. A splayed penguin still reads as "some animation"
    — plausibly a wave — so an expressive clip cannot catch this and only the
    boring one can.
    """
    clip = c.clips["idle_breathe"]
    rest = c.rig.bone_points()
    worst_dy, worst_drift = math.inf, 0.0
    for i in range(121):
        t = clip.duration * i / 120.0
        pts = c.rig.bone_points(clip.sample(t))
        for hand, shoulder in (("hand_L", "shoulder_L"), ("hand_R", "shoulder_R")):
            dy = pts[hand][1] - pts[shoulder][1]
            worst_dy = min(worst_dy, dy)
            assert dy >= ARMS_DOWN_MIN_DY, (
                "SPLAYED PENGUIN at t=%.3f: %s is only %.1fpx below %s "
                "(rest is 340px, floor is %.0f). Arms up means a transform-"
                "space or rotation-order fault is leaking."
                % (t, hand, dy, shoulder, ARMS_DOWN_MIN_DY)
            )
            drift = math.hypot(pts[hand][0] - rest[hand][0],
                               pts[hand][1] - rest[hand][1])
            worst_drift = max(worst_drift, drift)
            assert drift <= IDLE_MAX_DRIFT_PX, (
                "at t=%.3f %s has moved %.1fpx from rest; a breathing idle is "
                "low-motion and anything past %.0fpx is motion arriving from "
                "somewhere it was not authored"
                % (t, hand, drift, IDLE_MAX_DRIFT_PX)
            )
        assert pts["hand_L"][0] < pts["shoulder_L"][0], (
            "t=%.3f: hand_L crossed inboard of shoulder_L" % t)
        assert pts["hand_R"][0] > pts["shoulder_R"][0], (
            "t=%.3f: hand_R crossed inboard of shoulder_R" % t)
    assert worst_drift >= IDLE_MIN_DRIFT_PX, (
        "the hands moved at most %.2fpx over the whole clip, below the %.0fpx "
        "floor. The breath is authored on body and spine_upper, so the hands "
        "can only move if the parent's rotation is reaching them. This is the "
        "chain going QUIET, which the upper bound above cannot see."
        % (worst_drift, IDLE_MIN_DRIFT_PX))
    return ("flippers down for 121 frames: worst separation %.1fpx, drift "
            "%.2fpx inside [%.0f, %.0f]"
            % (worst_dy, worst_drift, IDLE_MIN_DRIFT_PX, IDLE_MAX_DRIFT_PX))


def check_skin_swap_moves_nothing(c: Ctx) -> str:
    """Same clip, same frame, two skins: every bone matrix identical.

    Also asserts STRUCTURALLY that no skin can reach the maths: `pose_world`
    and `Clip.sample` take no art parameter at all.
    """
    for fn, label in ((Rig.pose_world, "Rig.pose_world"),
                      (Clip.sample, "Clip.sample")):
        params = set(inspect.signature(fn).parameters) - {"self"}
        leak = sorted(p for p in params
                      if any(w in p.lower()
                             for w in ("skin", "attach", "art", "sprite", "texture")))
        assert not leak, (
            "%s takes %s — art must not be able to reach the transform maths"
            % (label, leak))

    checked = 0
    for name in GATE_CLIPS:
        clip = c.clips[name]
        for frac in (0.0, 0.17, 0.5, 0.83, 1.0):
            t = clip.duration * frac
            pose = clip.sample(t)
            a = c.rig.slot_transforms(pose, SKIN_BASE)
            b = c.rig.slot_transforms(pose, SKIN_DRAKE)
            n = c.rig.slot_transforms(pose, None)
            assert len(a) == len(b) == len(n) == len(c.skeleton.slots)
            for sa, sb, sn in zip(a, b, n):
                assert sa.slot == sb.slot == sn.slot
                assert sa.matrix == sb.matrix == sn.matrix, (
                    "%s t=%.3f slot %s: the skin moved the bone. base=%s "
                    "drake=%s" % (name, t, sa.slot, sa.matrix, sb.matrix))
                checked += 1
            assert a != b, (
                "the two skins produced identical SlotDraws including their "
                "attachments — the fixture is not actually swapping any art, "
                "so this check proves nothing")
    return "%d slot matrices identical across base/Drake/no-skin" % checked


def check_channels_commute(c: Ctx) -> str:
    """HEAD-then-FACE == FACE-then-HEAD, bit-for-bit.

    Demanded with tol=0.0, not a tolerance: a disjoint union must be exactly
    equal, and accepting 1e-9 here would hide a real blend.
    """
    for chan, bones in c.channels.sets.items():
        for other, obones in c.channels.sets.items():
            if chan < other:
                assert not (bones & obones), (
                    "channels %s and %s overlap on %s" % (chan, other,
                                                          sorted(bones & obones)))
    head, face, base = c.clips["look_left"], c.clips["blink"], c.clips["idle_breathe"]
    t = 0.09
    orders = [
        (head, face, base), (head, base, face), (face, head, base),
        (face, base, head), (base, head, face), (base, face, head),
    ]
    poses = []
    for order in orders:
        lay = Layered(c.channels)
        for clip in order:
            lay.play(clip)
        poses.append(lay.sample(t))
    for p in poses[1:]:
        assert p.close_to(poses[0], tol=0.0), (
            "channel order changed the pose; disagreeing bones: %s"
            % (p.diff(poses[0]),))
    # And a negative: an overlapping channel map must be refused at load.
    try:
        ChannelMap(c.skeleton, {"HEAD": ("neck", "head"), "FACE": ("head", "beak")})
    except AnimError:
        pass
    else:
        raise AssertionError(
            "an overlapping channel map loaded without complaint — precedence "
            "is now silently order-dependent")
    # And a clip may not key a bone outside its own channel.
    try:
        Clip.from_dict(
            {"name": "x", "duration": 1.0, "loop": False, "channel": "FACE",
             "tracks": {"flipper_L": [{"t": 0.0, "rot": 0}]}},
            skeleton=c.skeleton, channel_map=c.channels)
    except AnimError:
        pass
    else:
        raise AssertionError("a FACE clip keyed flipper_L and was accepted")
    return "%d channel orderings agree exactly; overlap and out-of-channel refused" % len(orders)


def check_loop_returns_to_start(c: Ctx) -> str:
    """A looping clip played twice round returns to its exact starting pose.

    ⚠ THIS CHECK WAS INERT WHEN FIRST WRITTEN, AND THE CONTROL IS THE ONLY
    REASON I KNOW. Measured 2026-09-27: the original version compared
    `clip.sample(0)` against `clip.sample(duration * n)`, plus the world
    matrices at those instants. It passed for ALL FIVE wrong implementations
    including the accumulating one — the single mutation it exists to catch.

    WHY: at t=0 every looping clip's pose is the IDENTITY delta, because the
    contract requires the last key to equal the first and these clips start at
    rest. Accumulating the identity is idempotent, so a rig that adds each
    pose onto the last is indistinguishable from a correct one AT THE SEAM.
    A test that samples only the instants where the bug cancels cannot see it.

    So part B plays the clip through in STEPS, the way a player does, and
    compares the world matrices at each cycle boundary to the first. Measured
    worst-case drift across three cycles of idle_breathe:

        contract and every non-stateful mutation   1.08e-32 px
        accumulating                                787.3   px

    Part A is kept because it is a real and separate property — of the FORMAT,
    that interpolation is seamless across the wrap — and it is now labelled as
    something that cannot see a stateful rig.
    """
    looping = [n for n in GATE_CLIPS if c.clips[n].loop]
    assert looping, "no looping clip in the library — nothing to test"

    # -- part A: the clip's own wrap is seamless (says nothing about the rig)
    for name in looping:
        clip = c.clips[name]
        d = clip.duration
        for phase in (0.0, 0.13, 0.5, 0.77):
            a = clip.sample(d * phase)
            for cycles in (1, 2, 5):
                b = clip.sample(d * phase + d * cycles)
                assert b.close_to(a, tol=1e-9), (
                    "%s: phase %.2f differs after %d loop(s) on %s"
                    % (name, phase, cycles, b.diff(a)))

    # -- part B: STEPPED PLAYBACK through the rig. This is the discriminating
    #    half, and it only works because it visits the non-identity frames.
    steps, cycles = 60, 3
    worst = 0.0
    for name in looping:
        clip = c.clips[name]
        c.rig.reset_accumulator()
        first: Optional[Mapping[str, Mat2D]] = None
        for i in range(cycles * steps + 1):
            world = c.rig.pose_world(clip.sample(clip.duration * i / steps)).world
            if i % steps:
                continue
            if first is None:
                first = dict(world)
                continue
            for bone, m in world.items():
                drift = max(abs(x - y) for x, y in zip(m, first[bone]))
                worst = max(worst, drift)
                assert drift <= 1e-6, (
                    "%s: after %d complete loop(s) of stepped playback, %s has "
                    "drifted by %.4g. Deltas are accumulating instead of being "
                    "recomputed from rest."
                    % (name, i // steps, bone, drift))
    return ("%s: %d cycles of %d-step playback, worst matrix drift %.3g"
            % (", ".join(looping), cycles, steps, worst))


def check_flipper_asymmetry(c: Ctx) -> str:
    """-70 clamps to -55 while +70 does not clamp. Kills a "tidied" range."""
    lo, hi = c.skeleton.constraints.flipper
    assert (lo, hi) == (-55.0, 70.0), (
        "flipper limit is [%g, %g]; the rig pack says [-55, 70] and the "
        "asymmetry is deliberate" % (lo, hi))
    rest_l = c.skeleton.bone("flipper_L").rest_rot   # +12
    # local total +70 must be reachable, i.e. NOT clamped.
    drot_up = hi - rest_l
    got, ev = c.rig.clamp_rotation("flipper_L", rest_l, drot_up)
    assert ev is None and abs(got - drot_up) < 1e-9, (
        "a local total of exactly +70 was clamped; the upper bound is "
        "inclusive and +70 is legal for a flipper: %s" % (ev,))
    # local total -70 must clamp to -55.
    drot_down = -70.0 - rest_l
    got, ev = c.rig.clamp_rotation("flipper_L", rest_l, drot_down)
    assert ev is not None, (
        "a local total of -70 was NOT clamped. Either clamping is unwired or "
        "the range was tidied to +/-70. The flipper's lower bound is -55.")
    assert abs(rest_l + got - lo) < 1e-9, (
        "clamped to a local total of %g, expected %g" % (rest_l + got, lo))
    return "+70 legal, -70 clamped to -55, limit read from the rig pack"


def check_clamp_is_loud(c: Ctx) -> str:
    """An over-range keyframe emits a named ClampEvent, not silence."""
    heard: list = []
    rig = Rig(c.skeleton, debug=True, policy=c.rig.policy,
              clamp_sink=heard.append)
    overshoot = Clip.from_dict(
        {"name": "overshoot", "duration": 1.0, "loop": False, "channel": "BASE",
         "tracks": {"flipper_L": [{"t": 0.0, "rot": 0}, {"t": 1.0, "rot": -300}],
                    "body": [{"t": 0.0, "rot": 0}, {"t": 1.0, "rot": 90}]}},
        skeleton=c.skeleton, channel_map=c.channels)
    res = rig.pose_world(overshoot.sample(1.0))
    bones = {ev.bone for ev in res.clamps}
    assert bones == {"flipper_L", "body"}, (
        "expected clamp events for flipper_L and body, got %s. A silently "
        "clamped keyframe looks subtly wrong with nothing in the data to "
        "explain it." % sorted(bones))
    assert heard and len(heard) == len(res.clamps), (
        "clamps were returned but the debug sink heard %d of %d — in debug the "
        "clamp must be LOUD" % (len(heard), len(res.clamps)))
    for ev in res.clamps:
        d = ev.describe()
        assert ev.bone in d and str(int(ev.low)) in d, (
            "clamp message %r does not name the bone and the limit" % d)
    clean = rig.pose_world(c.clips["wave"].sample(0.3))
    assert not clean.clamps, (
        "the wave clip clamped: %s. The gate clips are authored inside their "
        "limits; a clamp here means the limits or the clip drifted."
        % [e.describe() for e in clean.clamps])
    return "over-range keyframes named %s; the gate clips clamp nothing" % sorted(bones)


def check_gate_six_clips_two_skins(c: Ctx) -> str:
    """The owner's freeze criterion, executed.

    "Bind Base Pico, then Drake. Test idle_breathe, blink, look_left/right,
    wave, dance, death_flop. If both skins run the six clips with identical
    animation data, freeze the rig."
    """
    missing = [n for n in GATE_CLIPS if n not in c.clips]
    assert not missing, "gate clips missing from the library: %s" % missing
    frames = 0
    for name in GATE_CLIPS:
        clip = c.clips[name]
        for i in range(41):
            t = clip.duration * i / 40.0
            pose = clip.sample(t)
            res = c.rig.pose_world(pose)
            assert not res.clamps, (
                "%s clamps at t=%.3f: %s" % (name, t,
                                             [e.describe() for e in res.clamps]))
            for bone, m in res.world.items():
                x, y = m.origin
                assert math.isfinite(x) and math.isfinite(y), (
                    "%s t=%.3f: %s went non-finite" % (name, t, bone))
            base = c.rig.slot_transforms(pose, SKIN_BASE)
            drake = c.rig.slot_transforms(pose, SKIN_DRAKE)
            assert [s.matrix for s in base] == [s.matrix for s in drake]
            frames += 1
    return "%d clips x 41 frames x 2 skins = %d poses, no clamps, no NaN" % (
        len(GATE_CLIPS), frames)


def check_every_clip_moves_what_it_keys(c: Ctx) -> str:
    """Every keyed bone must actually MOVE, and so must its children.

    Found while measuring, not designed in: under `drop_parent_rotation`,
    `wave` moves `hand_R` by 0.00px, because the flipper's rotation never
    reaches the hand it is attached to. A wave whose hand does not move is not
    a subtle error, and yet every bound in this file that asks "did it move too
    much" reads it as a clean pass. "Unchanged" is not continuity; it is
    nothing watching.

    ⚠ THIS CHECK WAS WRONG TWICE, AND BOTH WRONG VERSIONS ARE THE POINT.

    v1 compared bone ORIGINS and failed the baseline on `blink`: that clip
    scales `eye_L`, a leaf bone, and a scale about a bone's own pivot moves its
    origin by exactly nothing. It was measuring translation while calling itself
    motion.

    v2 compared whole MATRICES over "the bone or any descendant", which fixed
    blink and then caught NOTHING — including `drop_parent_rotation`, the
    mutation it was written for. Under that mutation `hand_R` still rotates on
    its own `rot` track, so its matrix differs from rest and the disjunction
    was satisfied by the bone's own key. The looseness that made v2 pass the
    baseline is the same looseness that made it inert.

    v3 asks a separate question per FIELD, because the fields fail differently:

        keyed x or y   -> this bone's ORIGIN must translate
        keyed scale    -> this bone's matrix SCALE must change
        keyed rot      -> this bone's matrix ROTATION must change, and if it
                          has descendants, at least one DESCENDANT must
                          TRANSLATE. That last clause is the whole check: a
                          rotation that does not move the things hanging off it
                          has not propagated, which is exactly the fault.
    """
    rest = c.rig.rest_world()
    checked = 0
    for name in GATE_CLIPS:
        clip = c.clips[name]
        translated: set[str] = set()
        rotated: set[str] = set()
        rescaled: set[str] = set()
        for i in range(61):
            world = c.rig.pose_world(clip.sample(clip.duration * i / 60.0)).world
            for bone, m in world.items():
                r = rest[bone]
                if math.hypot(m.tx - r.tx, m.ty - r.ty) > 0.5:
                    translated.add(bone)
                if abs(m.rotation_deg - r.rotation_deg) > 0.01:
                    rotated.add(bone)
                if abs(math.hypot(m.a, m.b) - math.hypot(r.a, r.b)) > 1e-4:
                    rescaled.add(bone)

        for bone, track in clip.tracks.items():
            kids = tuple(b for b in c.skeleton.bone_names
                         if bone in c.skeleton.ancestors(b))
            if {"x", "y"} & set(track.fields):
                assert bone in translated, (
                    "%s keys %s in x/y but its origin never moves 0.5px" % (name, bone))
                checked += 1
            if "scale" in track.fields:
                assert bone in rescaled, (
                    "%s keys %s in scale but its matrix scale never changes"
                    % (name, bone))
                checked += 1
            if "rot" in track.fields:
                assert bone in rotated, (
                    "%s keys %s in rot but its world rotation never changes"
                    % (name, bone))
                checked += 1
                if kids:
                    assert set(kids) & translated, (
                        "%s rotates %s and NONE of its %d descendants (%s) ever "
                        "translates. The rotation is not propagating down the "
                        "hierarchy — a wave whose hand does not move."
                        % (name, bone, len(kids), ", ".join(kids)))
                    checked += 1
    return "%d per-field motion assertions across %d clips" % (
        checked, len(GATE_CLIPS))


QT_ROOTS = ("PySide6", "PySide2", "PyQt5", "PyQt6")


def check_no_qt(c: Ctx) -> str:
    """This layer is pure logic. Nothing here may pull in Qt.

    ⚠ THE FIRST VERSION ASKED THE WRONG PROCESS. It scanned this interpreter's
    `sys.modules`, which passes standalone and FAILS the moment the check runs
    inside the repo's full pytest session, because `shared/tests/test_qt_widgets`
    imports PySide6 before my tests are reached. The check's answer depended on
    which other tests had already run — it was measuring the session, not the
    package. A verdict that changes with the company it keeps is not a verdict.

    So: a SUBPROCESS that imports nothing but `pico`, plus a static scan of the
    source. The subprocess answers "does importing this package pull in Qt"; the
    scan answers "does any line in it mention Qt", which catches a deferred
    import inside a function that the probe would never execute.
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent
    probe = (
        "import sys, pico, pico.rig, pico.anim, pico.selftest\n"
        "roots = %r\n"
        "print(','.join(sorted(m for m in sys.modules "
        "if m.split('.')[0] in roots)))\n" % (QT_ROOTS,)
    )
    r = subprocess.run([sys.executable, "-c", probe], cwd=str(root),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (
        "the import probe itself failed (rc=%d): %s" % (r.returncode, r.stderr.strip()))
    leaked = [m for m in r.stdout.strip().split(",") if m]
    assert not leaked, "importing pico pulled in Qt: %s" % leaked

    pkg = Path(__file__).resolve().parent
    hits = []
    for py in sorted(pkg.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for token in QT_ROOTS + ("QtCore", "QtWidgets", "QtGui"):
            # QT_ROOTS appears in this very function as a constant, so only
            # count lines that actually import.
            for line in text.splitlines():
                stripped = line.strip()
                if token in stripped and (stripped.startswith("import ")
                                          or stripped.startswith("from ")):
                    hits.append("%s: %s" % (py.name, stripped))
    assert not hits, "Qt named in an import statement: %s" % hits
    return "a clean subprocess import of pico pulls in no Qt; no Qt imports in %d files" % (
        len(list(pkg.rglob("*.py"))))


def check_loop_pop_refused(c: Ctx) -> str:
    """A looping clip whose last key differs from its first is a load error."""
    try:
        Clip.from_dict(
            {"name": "popper", "duration": 1.0, "loop": True, "channel": "BASE",
             "tracks": {"body": [{"t": 0.0, "rot": 0}, {"t": 1.0, "rot": 7}]}},
            skeleton=c.skeleton, channel_map=c.channels)
    except AnimError as exc:
        assert "body" in str(exc) and "rot" in str(exc), (
            "the loop-pop error does not name the offending track and field: %s"
            % exc)
    else:
        raise AssertionError(
            "a looping clip with a 7-degree pop loaded silently")
    # A slot is not a bone and cannot be keyed.
    try:
        Clip.from_dict(
            {"name": "slotclip", "duration": 1.0, "loop": False, "channel": "BASE",
             "tracks": {"jacket_front": [{"t": 0.0, "rot": 0}]}},
            skeleton=c.skeleton, channel_map=c.channels)
    except AnimError:
        pass
    else:
        raise AssertionError("a clip keyed the slot jacket_front and was accepted")
    return "loop pop, and keyframing a slot, both refused by name"


def check_skeleton_shape(c: Ctx) -> str:
    """The rig pack's own numbers: 23 bones, 26 slots, 2048x2048 top-left."""
    sk = c.skeleton
    assert sk.version == 1, "this check is written against Pico_Master v1"
    assert len(sk.bones) == 23, "expected 23 bones, got %d" % len(sk.bones)
    assert len(sk.slots) == 26, "expected 26 slots, got %d" % len(sk.slots)
    assert (sk.canvas.width, sk.canvas.height) == (2048, 2048)
    assert sk.canvas.origin == "top-left"
    assert [s.z for s in sk.slots] == sorted(s.z for s in sk.slots)
    return "23 bones, 26 slots ordered by z, canvas 2048x2048 top-left"


#: Ordered so the cheapest structural checks run first.
CHECKS: tuple[tuple[str, Callable[[Ctx], str]], ...] = (
    ("skeleton_shape", check_skeleton_shape),
    ("rest_positions", check_rest_positions),
    ("arms_down", check_arms_down),
    ("skin_swap_moves_nothing", check_skin_swap_moves_nothing),
    ("channels_commute", check_channels_commute),
    ("loop_returns_to_start", check_loop_returns_to_start),
    ("flipper_asymmetry", check_flipper_asymmetry),
    ("clamp_is_loud", check_clamp_is_loud),
    ("loop_pop_refused", check_loop_pop_refused),
    ("every_clip_moves", check_every_clip_moves_what_it_keys),
    ("gate_six_clips_two_skins", check_gate_six_clips_two_skins),
    ("no_qt", check_no_qt),
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_checks(ctx: Ctx) -> dict[str, Optional[str]]:
    """`{check_name: None if passed else the failure message}`."""
    out: dict[str, Optional[str]] = {}
    for name, fn in CHECKS:
        try:
            _DETAIL[name] = fn(ctx)
            out[name] = None
        except AssertionError as exc:
            out[name] = str(exc).strip() or "AssertionError with no message"
        except (RigError, AnimError, KeyError, ValueError, ZeroDivisionError,
                OverflowError) as exc:
            out[name] = "%s: %s" % (type(exc).__name__, exc)
    return out


_DETAIL: dict[str, str] = {}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print each passing check's detail line")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="only print the verdict and any failures")
    args = ap.parse_args(argv)

    def say(*a, **k):
        if not args.quiet:
            print(*a, **k)

    say("pico rig runtime selftest")
    verdict, detail = verify_skeleton_provenance()
    say("  rig data provenance: %-12s %s" % (verdict, detail))
    prov_bad = verdict == "FAIL"

    # ---------------- BASELINE ----------------
    say("\nBASELINE  contract maths, must pass everything")
    base_ctx = build()
    base = _run_checks(base_ctx)
    for name, err in base.items():
        if err is None:
            if args.verbose:
                say("  PASS  %-24s %s" % (name, _DETAIL.get(name, "")))
            else:
                say("  PASS  %s" % name)
        else:
            print("  FAIL  %-24s %s" % (name, err))
    baseline_ok = all(v is None for v in base.values())
    say("  baseline: %d/%d" % (sum(v is None for v in base.values()), len(base)))

    if not baseline_ok:
        print("\n*** BASELINE FAILED. The control below is MEANINGLESS: a "
              "differential test with a broken control scores 100%, because "
              "every mutant dies whatever it does. Fix the baseline first.")
        return 1

    # ---------------- CONTROL ----------------
    say("\nCONTROL  each wrong implementation must be caught by >=1 check")
    undetected: list[str] = []
    for label, ops in WRONG_OPS.items():
        try:
            ctx = build(ops)
            res = _run_checks(ctx)
            caught = [n for n, e in res.items() if e is not None]
        except (RigError, AnimError, ZeroDivisionError, OverflowError) as exc:
            caught = ["load: %s" % type(exc).__name__]
        if caught:
            say("  caught  %-24s by %s" % (label, ", ".join(caught)))
        else:
            print("  NOT CAUGHT  %s" % label)
            undetected.append(label)

    for label, make in WRONG_CONSTRAINTS.items():
        sk, pol = make()
        ctx = build(skeleton=sk, policy=pol)
        res = _run_checks(ctx)
        caught = [n for n, e in res.items() if e is not None]
        if caught:
            say("  caught  %-24s by %s" % (label, ", ".join(caught)))
        else:
            print("  NOT CAUGHT  %s" % label)
            undetected.append(label)

    total = len(WRONG_OPS) + len(WRONG_CONSTRAINTS)
    say("  control: %d/%d wrong implementations caught" % (total - len(undetected), total))

    if undetected:
        print("\n*** THE CHECKS ARE NOT DISCRIMINATING. These wrong "
              "implementations passed every check: %s. A fixture that accepts "
              "a wrong answer is not a test." % ", ".join(undetected))
        return 2
    if prov_bad:
        print("\nFAIL: rig data provenance — %s" % detail)
        return 1

    say("\nOK  baseline %d/%d, control %d/%d" % (len(base), len(base), total, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
