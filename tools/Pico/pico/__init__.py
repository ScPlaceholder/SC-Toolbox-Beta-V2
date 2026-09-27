"""Pico Pal RIG RUNTIME — layer A of PICO_CONTRACT.md.

PURE LOGIC. No Qt, no I/O beyond reading JSON off disk, headless-testable.
`selftest.py::check_no_qt` proves the no-Qt claim in a subprocess rather than
asserting it in prose.

    py -3.13 -m pico.selftest -v        # baseline + control, exit 0/1/2
    py -3.13 -m pytest pico/tests -q    # 144 tests

===============================================================================
THE SURFACE LAYERS B, C AND D CONSUME
===============================================================================

    from pico.rig  import Skeleton, Rig, Pose, Delta, Mat2D, SlotDraw, ClampEvent
    from pico.anim import Clip, ClipLibrary, ChannelMap, Layered

LOADING
    Skeleton.load(path=None) -> Skeleton        # defaults to the shipped copy
      .bones: tuple[Bone, ...]                  # topological, parents first
      .bone(name) -> Bone                       # (name, parent, rest_x, rest_y,
      .bone_names: tuple[str, ...]              #  rest_rot, rest_scale)
      .slots: tuple[Slot, ...]                  # (name, bone, z), sorted by z
      .slot(name) -> Slot
      .canvas -> Canvas(width, height, origin)
      .constraints -> Constraints(head, body, flipper, foot, dashboard_boundary)
      .children / .ancestors(name)
      .has_bone(name) -> bool
      .require_bones(names, what="skin")        # FOR LAYER D: raises, naming
      .constraint_for(bone) / .unconstrained_bones()

    ChannelMap(skeleton, named=None) -> ChannelMap
      .channels / .bones(channel) / .channel_of(bone)
      Asserts the channel bone sets are DISJOINT at construction. BASE is the
      computed complement, so coverage is total.

    Clip.load(path, skeleton=..., channel_map=...) -> Clip
    Clip.from_dict(d, skeleton=..., channel_map=...) -> Clip
      .name .duration .loop .channel .tracks .warnings .source
      .sample(t) -> Pose                        # pure; same t, same Pose
      .local_time(t) -> float                   # wraps if loop, else clamps
      .bones() -> tuple[str, ...]

    ClipLibrary.load_dir(path=None, *, skeleton, channel_map) -> ClipLibrary
      Mapping[str, Clip] keyed by file stem. `.all_warnings()`.

PLAYING
    Layered(channel_map)
      .play(clip, at=0.0)      # clip goes on its OWN declared channel
      .stop(channel) / .clear() / .active()
      .sample(now) -> Pose     # disjoint union of the active channels

POSING  — the one call the renderer needs per frame
    Rig(skeleton, *, debug=False, policy=None, clamp_sink=None)
      .slot_transforms(pose, attachments=None) -> tuple[SlotDraw, ...]
          SlotDraw(slot, bone, z, matrix, attachments), sorted by z ascending.
          `attachments` is {slot_name: [art_id, ...]} — a SKIN. It selects what
          to draw and provably cannot alter any matrix.
      .pose_world(pose) -> PoseResult(world, local, clamps)
      .bone_points(pose) -> {bone: (x, y)}
      .rest_world() / .check_rest_positions() / .clamp_rotation(...)

    Mat2D(a, b, c, d, tx, ty) is a 2x3 affine, column-vector convention:
        | a  c  tx |      .apply((x,y))  .mul(n) == n applied first
        | b  d  ty |      .inverse()  .origin  .rotation_deg  .close_to()

===============================================================================
DECISIONS THIS LAYER OWNED AND MADE (for J to fold into the contract)
===============================================================================

TRANSFORM SPACE      world[child] = world[parent] @ local[child],
                     local = translate @ rotate @ scale. Pose dx/dy are in the
                     PARENT bone's space. Full reasoning in rig.py's docstring.

REST DERIVATION      `pico_skeleton.json` gives ABSOLUTE canvas coordinates, so
                     each bone's local rest translation is derived by inverting
                     its parent's rest world matrix. This makes
                     `rest_world[bone].origin == (authored x, y)` exact for all
                     23 bones, and that invariant is the single most
                     discriminating check in the suite.

ROTATION SIGN        +y is DOWN, so a POSITIVE angle is CLOCKWISE on screen.

scale                a MULTIPLIER on rest scale. Identity 1.0, not 0.0.

EASE                 a key's `ease` governs the segment LEAVING it. An ease on
                     a field's last key is inert and is warned about.

CLAMPING             on the LOCAL TOTAL (rest_rot + drot), inclusive bounds,
                     loud in debug via `clamp_sink`. `ConstraintPolicy` flips to
                     delta-clamping without a code edit.

FRAME RATE /         ⇒ CONTRACT SECTION 8 ASKS OWNER A TO DECIDE THIS. Decided:
TICK SOURCE          this layer is TICK-SOURCE AGNOSTIC and owns no timer.
                     `Layered.sample(now)` takes a wall-clock float in seconds
                     and is pure, so it can be called at any rate, out of order,
                     or twice for one instant without consequence.
                     RECOMMENDATION for layer B: drive it RENDER-DRIVEN — sample
                     inside the paint with `time.perf_counter()` — rather than
                     from a QTimer. A timer at one rate and a paint at another
                     means either sampling twice per frame or showing a pose one
                     tick stale, and neither is visible in this layer's tests.
                     Nothing here forbids a QTimer; it just cannot help.
"""

from .rig import (  # noqa: F401
    Bone,
    Canvas,
    ClampEvent,
    ConstraintPolicy,
    Constraints,
    Delta,
    Mat2D,
    Pose,
    PoseResult,
    Rig,
    RigError,
    Skeleton,
    Slot,
    SlotDraw,
    default_skeleton_path,
    verify_skeleton_provenance,
)
from .anim import (  # noqa: F401
    AnimError,
    ChannelMap,
    Clip,
    ClipLibrary,
    Layered,
    Track,
)

__all__ = [
    "AnimError",
    "Bone",
    "Canvas",
    "ChannelMap",
    "ClampEvent",
    "Clip",
    "ClipLibrary",
    "ConstraintPolicy",
    "Constraints",
    "Delta",
    "Layered",
    "Mat2D",
    "Pose",
    "PoseResult",
    "Rig",
    "RigError",
    "Skeleton",
    "Slot",
    "SlotDraw",
    "Track",
    "default_skeleton_path",
    "verify_skeleton_provenance",
]
