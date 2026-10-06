"""Pico rig runtime: bones, transform composition, poses, constraints, slots.

===============================================================================
TRANSFORM SPACE — read this before touching anything below
===============================================================================

PICO_CONTRACT.md section 2 demands that the space be decided once and written
down. It is decided here and stated in every signature that carries a delta.

COORDINATE SYSTEM
    Canvas is 2048x2048, origin TOP-LEFT, +x right, +y DOWN.
    Because +y is DOWN, a POSITIVE rotation in degrees appears CLOCKWISE
    on screen. There is no way to make that intuitive; it is written here so
    nobody has to rediscover it from a splayed penguin.

MATRIX
    `Mat2D` is a 2x3 affine, column-vector convention, stored row-major:

        | a  c  tx |
        | b  d  ty |
        | 0  0  1  |

    `m.apply((x, y))` -> (a*x + c*y + tx, b*x + d*y + ty)
    `m.mul(n)` is the mathematical `m @ n`: n is applied FIRST, then m.

COMPOSITION (the contract's rule, implemented literally)
    world[child] = world[parent] @ local[child]
    local[bone]  = translate @ rotate @ scale

    Because `translate` is leftmost, the translation is NOT affected by the
    bone's own rotation or scale. Consequence, and this is the load-bearing
    sentence:

        *** A POSE'S dx/dy ARE IN THE PARENT BONE'S SPACE. ***

    So `body_y -12px` is 12px up the PARENT's y axis. For `root` the parent is
    the identity, so root's dx/dy are canvas pixels. If `root` is ever rotated,
    every descendant's dx/dy rotates with it. That is the definition, not an
    accident; a delta is never in world space and never in the bone's own
    post-rotation space.

REST POSE, AND WHY THE JSON NEEDS CONVERTING
    `pico_skeleton.json` gives every bone an ABSOLUTE canvas x/y (the file says
    "Reference coordinates for initial rig construction"), not an offset from
    its parent. A `rotation` field is read as the bone's LOCAL rest rotation
    relative to its parent.

    Those two facts interact, and the interaction is the first place a rig of
    this shape goes wrong. `flipper_L` has rest rotation +12 and `hand_L` is
    authored at absolute (590, 1280). If we naively took hand_L's local offset
    as (authored_child - authored_parent), then composed it through flipper_L's
    12 degrees, hand_L would land somewhere other than (590, 1280) and the
    whole arm would be subtly wrong at REST, before a single keyframe.

    So the local rest translation is derived by inverting the parent's rest
    world matrix:

        local_rest_t = world_rest[parent].inverse().apply((auth_x, auth_y))

    which makes the invariant exact and testable:

        *** rest_world[bone].apply((0,0)) == (auth_x, auth_y) for all bones ***

    `Rig.check_rest_positions()` asserts it, and `selftest.py` uses it as the
    discriminating check that kills three of the five deliberately-wrong
    implementations.

POSE
    Sparse `{bone_name: Delta}` of DELTAS FROM REST, never absolutes. A clip
    touching one bone leaves the other 22 alone, which is what makes layering
    possible. Identity is `Delta()` == (0, 0, 0, 1.0): additive identity for
    dx/dy/drot, MULTIPLICATIVE identity for scale. A key `"scale": 1.2` means
    1.2x the bone's rest scale, not rest + 1.2.

CONSTRAINTS
    Clamped at pose time, on the LOCAL TOTAL rotation (rest_rot + drot), not on
    the delta alone. See `ConstraintPolicy` for why, and for the switch if that
    is decided otherwise. `flipper` is deliberately ASYMMETRIC, [-55, +70]. Do not
    tidy it.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, NamedTuple, Optional, Sequence

__all__ = [
    "Bone",
    "Canvas",
    "ClampEvent",
    "ConstraintPolicy",
    "Constraints",
    "DEFAULT_CONSTRAINT_GROUPS",
    "Delta",
    "Mat2D",
    "Pose",
    "PoseResult",
    "Rig",
    "RigError",
    "Skeleton",
    "Slot",
    "SlotDraw",
    "TransformOps",
    "DEFAULT_OPS",
    "default_skeleton_path",
    "verify_skeleton_provenance",
]


class RigError(Exception):
    """A load-time or pose-time rig fault. Never raised for a merely odd pose."""


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------


class Mat2D(NamedTuple):
    """2x3 affine, column-vector convention. See the module docstring.

        | a  c  tx |
        | b  d  ty |
    """

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    # -- constructors ------------------------------------------------------
    @staticmethod
    def identity() -> "Mat2D":
        return Mat2D()

    @staticmethod
    def translate(tx: float, ty: float) -> "Mat2D":
        return Mat2D(1.0, 0.0, 0.0, 1.0, float(tx), float(ty))

    @staticmethod
    def rotate_deg(deg: float) -> "Mat2D":
        r = math.radians(deg)
        cs, sn = math.cos(r), math.sin(r)
        # Column-vector CCW in math axes; with +y DOWN this reads CLOCKWISE
        # on screen. Stated in the module docstring.
        return Mat2D(cs, sn, -sn, cs, 0.0, 0.0)

    @staticmethod
    def scale(s: float) -> "Mat2D":
        return Mat2D(float(s), 0.0, 0.0, float(s), 0.0, 0.0)

    # -- algebra -----------------------------------------------------------
    def mul(self, other: "Mat2D") -> "Mat2D":
        """`self @ other`. `other` is applied FIRST, then `self`."""
        m, n = self, other
        return Mat2D(
            m.a * n.a + m.c * n.b,
            m.b * n.a + m.d * n.b,
            m.a * n.c + m.c * n.d,
            m.b * n.c + m.d * n.d,
            m.a * n.tx + m.c * n.ty + m.tx,
            m.b * n.tx + m.d * n.ty + m.ty,
        )

    __matmul__ = mul

    def apply(self, p: Sequence[float]) -> tuple[float, float]:
        x, y = float(p[0]), float(p[1])
        return (self.a * x + self.c * y + self.tx,
                self.b * x + self.d * y + self.ty)

    @property
    def origin(self) -> tuple[float, float]:
        """Where this bone's own (0,0) lands. Cheaper than apply((0,0))."""
        return (self.tx, self.ty)

    def inverse(self) -> "Mat2D":
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-12:
            raise RigError(
                "singular bone matrix (determinant %r) — a rest or pose scale "
                "of 0 collapses the whole subtree and cannot be inverted" % det
            )
        a, b, c, d, tx, ty = self
        return Mat2D(
            d / det,
            -b / det,
            -c / det,
            a / det,
            (c * ty - d * tx) / det,
            (b * tx - a * ty) / det,
        )

    @property
    def rotation_deg(self) -> float:
        """Accumulated world rotation, degrees, screen-clockwise-positive."""
        return math.degrees(math.atan2(self.b, self.a))

    def close_to(self, other: "Mat2D", tol: float = 1e-9) -> bool:
        return all(abs(x - y) <= tol for x, y in zip(self, other))


# ---------------------------------------------------------------------------
# Skeleton data
# ---------------------------------------------------------------------------


class Bone(NamedTuple):
    """`(name, parent, rest_x, rest_y, rest_rot, rest_scale)`.

    `rest_x`/`rest_y` are ABSOLUTE canvas coordinates as authored in
    `pico_skeleton.json`. `rest_rot` is degrees, LOCAL to the parent.
    `rest_scale` is not present in the rig pack at all and defaults to 1.0 —
    a contract/data gap, recorded as one rather than invented.
    """

    name: str
    parent: Optional[str]
    rest_x: float
    rest_y: float
    rest_rot: float = 0.0
    rest_scale: float = 1.0


class Slot(NamedTuple):
    """`(name, bone, z)`. A slot carries ARTWORK and z-order, never a transform.

    The animation format keyframes BONES ONLY. Nothing here is animatable; a
    skin swaps what sits in a slot and that is the whole of skin/anim
    separation on this side of the seam.
    """

    name: str
    bone: str
    z: int


class Canvas(NamedTuple):
    width: int
    height: int
    origin: str  # "top-left" for Pico_Master v1


class Constraints(NamedTuple):
    """Rotation limits in degrees, plus the rig pack's non-numeric viewport note.

    `flipper` is ASYMMETRIC by design: [-55, +70]. A previous draft of the
    contract had no constraints at all; a later reader's instinct will be to
    "tidy" the flipper to +/-70. `selftest.py` has a control that fails if
    anyone does.
    """

    head: tuple[float, float]
    body: tuple[float, float]
    flipper: tuple[float, float]
    foot: tuple[float, float]
    dashboard_boundary: str = ""


#: Which bones each named rotation limit governs.
#:
#: A DECISION MADE HERE, not rig-pack data. `pico_skeleton.json` names four limits and does
#: NOT say which of the 23 bones each one covers. Exact-name-only would leave
#: `neck` and the two spine bones unlimited, so a `look_left` that rotated
#: `neck` by 40 degrees would sail past a limit called `head_rotation_deg`.
#: Extending each limit to its obvious anatomical group is the least-surprising
#: reading. It is data, in one place, so it can be overruled without a code edit.
#: Reported as OPEN in the handback.
DEFAULT_CONSTRAINT_GROUPS: Mapping[str, tuple[str, ...]] = {
    "head": ("head", "neck"),
    "body": ("body", "spine_lower", "spine_upper"),
    "flipper": ("flipper_L", "flipper_R"),
    "foot": ("foot_L", "foot_R"),
}


@dataclass(frozen=True)
class ConstraintPolicy:
    """How a rotation limit is applied.

    `clamp_local_total=True` (the default) clamps `rest_rot + drot` against the
    limit. The alternative is clamping `drot` alone.

    WHY THE TOTAL: the limits sit in the same file as the rest rotations and
    read as a property of the bone's pose. Clamping the delta instead would let
    `flipper_L` (rest +12) reach a local total of +82 against a stated ceiling
    of +70, and a range that can be exceeded is not a range.

    THE COST, stated because it is real: rest rotations are mirrored (+12 / -12)
    and the limit is not, so a mirrored wave does not clamp symmetrically.
    flipper_L can take drot +58 before clamping; flipper_R can take -43.
    CANNOT TELL which was intended. Flip this flag, do not edit the code.
    """

    clamp_local_total: bool = True
    groups: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_CONSTRAINT_GROUPS)
    )


class ClampEvent(NamedTuple):
    """One clamped rotation. Emitted LOUDLY in debug, per the contract.

    "a silently clamped keyframe makes an animation look subtly wrong with
    nothing in the data to explain it."
    """

    bone: str
    limit: str
    requested_deg: float
    allowed_deg: float
    low: float
    high: float

    def describe(self) -> str:
        return (
            "CLAMP %-11s %s wanted %+.3f deg, allowed %+.3f "
            "(limit %s = [%+g, %+g])"
            % (self.bone, self.limit, self.requested_deg, self.allowed_deg,
               self.limit, self.low, self.high)
        )


class Skeleton:
    """Loaded `pico_skeleton.json`. Immutable once constructed.

    The file is "the stable naming contract" and OUTRANKS PICO_CONTRACT.md on
    anything structural. Nothing here restates a bone list; it is all read from
    the JSON, because a copied list is a fork waiting to drift.
    """

    def __init__(
        self,
        name: str,
        version: int,
        canvas: Canvas,
        bones: Sequence[Bone],
        slots: Sequence[Slot],
        constraints: Constraints,
        source: Optional[Path] = None,
    ) -> None:
        self.name = name
        self.version = version
        self.canvas = canvas
        self.constraints = constraints
        self.source = source

        by_name: dict[str, Bone] = {}
        for b in bones:
            if b.name in by_name:
                raise RigError("duplicate bone name %r" % b.name)
            by_name[b.name] = b
        for b in bones:
            if b.parent is not None and b.parent not in by_name:
                raise RigError(
                    "bone %r names parent %r which is not in the skeleton"
                    % (b.name, b.parent)
                )
        roots = [b.name for b in bones if b.parent is None]
        if len(roots) != 1:
            raise RigError(
                "expected exactly one parentless bone, found %d: %s"
                % (len(roots), roots)
            )

        self._by_name = by_name
        self.root = roots[0]
        self.bones: tuple[Bone, ...] = tuple(self._topological(bones, by_name))
        self.bone_names: tuple[str, ...] = tuple(b.name for b in self.bones)

        for s in slots:
            if s.bone not in by_name:
                raise RigError(
                    "slot %r hangs off bone %r which is not in the skeleton "
                    "— a load error, not a silent skip" % (s.name, s.bone)
                )
        seen_slots: set[str] = set()
        for s in slots:
            if s.name in seen_slots:
                raise RigError("duplicate slot name %r" % s.name)
            seen_slots.add(s.name)
        self.slots: tuple[Slot, ...] = tuple(sorted(slots, key=lambda s: (s.z, s.name)))
        self._slot_by_name = {s.name: s for s in self.slots}

        self.children: Mapping[str, tuple[str, ...]] = {
            b.name: tuple(c.name for c in self.bones if c.parent == b.name)
            for b in self.bones
        }

    # -- loading -----------------------------------------------------------
    @staticmethod
    def _topological(bones: Sequence[Bone], by_name: Mapping[str, Bone]) -> list[Bone]:
        """Parents strictly before children. Raises on a cycle, naming it."""
        order: list[Bone] = []
        placed: set[str] = set()
        remaining = list(bones)
        while remaining:
            progressed = False
            still: list[Bone] = []
            for b in remaining:
                if b.parent is None or b.parent in placed:
                    order.append(b)
                    placed.add(b.name)
                    progressed = True
                else:
                    still.append(b)
            remaining = still
            if not progressed:
                raise RigError(
                    "bone hierarchy has a cycle or an unreachable island: %s"
                    % sorted(b.name for b in remaining)
                )
        return order

    @classmethod
    def from_dict(cls, d: Mapping, source: Optional[Path] = None) -> "Skeleton":
        try:
            canvas_d = d["canvas"]
            canvas = Canvas(
                int(canvas_d["width"]),
                int(canvas_d["height"]),
                str(canvas_d.get("origin", "top-left")),
            )
            bones = [
                Bone(
                    name=str(b["name"]),
                    parent=(None if b.get("parent") in (None, "") else str(b["parent"])),
                    rest_x=float(b["x"]),
                    rest_y=float(b["y"]),
                    rest_rot=float(b.get("rotation", 0.0)),
                    # The rig pack has no scale field on bones at all.
                    rest_scale=float(b.get("scale", 1.0)),
                )
                for b in d["bones"]
            ]
            slots = [
                Slot(str(s["name"]), str(s["bone"]), int(s["z"])) for s in d["slots"]
            ]
            c = d["constraints"]
            constraints = Constraints(
                head=(float(c["head_rotation_deg"][0]), float(c["head_rotation_deg"][1])),
                body=(float(c["body_rotation_deg"][0]), float(c["body_rotation_deg"][1])),
                flipper=(float(c["flipper_rotation_deg"][0]),
                         float(c["flipper_rotation_deg"][1])),
                foot=(float(c["foot_rotation_deg"][0]), float(c["foot_rotation_deg"][1])),
                dashboard_boundary=str(c.get("dashboard_boundary", "")),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RigError("malformed skeleton JSON: %s" % exc) from exc

        if canvas.origin != "top-left":
            raise RigError(
                "canvas origin %r — every transform in this module assumes "
                "top-left with +y DOWN. Refusing to guess." % canvas.origin
            )
        return cls(
            name=str(d.get("name", "unnamed")),
            version=int(d.get("version", 0)),
            canvas=canvas,
            bones=bones,
            slots=slots,
            constraints=constraints,
            source=source,
        )

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> "Skeleton":
        p = Path(path) if path is not None else default_skeleton_path()
        with open(p, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh), source=p)

    # -- queries -----------------------------------------------------------
    def bone(self, name: str) -> Bone:
        try:
            return self._by_name[name]
        except KeyError:
            raise RigError("no such bone %r" % name) from None

    def slot(self, name: str) -> Slot:
        try:
            return self._slot_by_name[name]
        except KeyError:
            raise RigError("no such slot %r" % name) from None

    def has_bone(self, name: str) -> bool:
        return name in self._by_name

    def require_bones(self, names: Iterable[str], what: str = "attachment") -> None:
        """Raise naming every unknown bone. For layer D (skins) to call.

        The contract: "A skin referencing a bone the skeleton does not have is
        a load error, not a silent skip" — silently dropping it produces a
        penguin missing one sleeve and no explanation.
        """
        missing = sorted({n for n in names if n not in self._by_name})
        if missing:
            raise RigError(
                "%s references %d bone(s) the skeleton does not have: %s"
                % (what, len(missing), ", ".join(missing))
            )

    def ancestors(self, name: str) -> tuple[str, ...]:
        out: list[str] = []
        cur = self.bone(name).parent
        while cur is not None:
            out.append(cur)
            cur = self.bone(cur).parent
        return tuple(out)

    def constraint_for(self, bone: str,
                       policy: Optional[ConstraintPolicy] = None) -> Optional[
                           tuple[str, float, float]]:
        """`(limit_name, low, high)` for a bone, or None if unlimited."""
        pol = policy or ConstraintPolicy()
        for limit_name, members in pol.groups.items():
            if bone in members:
                lo, hi = getattr(self.constraints, limit_name)
                return (limit_name, lo, hi)
        return None

    def unconstrained_bones(self, policy: Optional[ConstraintPolicy] = None
                            ) -> tuple[str, ...]:
        """Bones no rotation limit covers. 14 of 23 in Pico_Master v1.

        Not a fault — `hat_anchor` and `back_anchor` are mount points and
        nothing should ever keyframe them. It IS worth printing, because
        `hand_L`, `hand_R`, the shoulders and the hips are in this list and a
        clip may rotate them arbitrarily.
        """
        pol = policy or ConstraintPolicy()
        covered = {b for members in pol.groups.values() for b in members}
        return tuple(n for n in self.bone_names if n not in covered)

    def __repr__(self) -> str:
        return "<Skeleton %s v%d: %d bones, %d slots, %dx%d>" % (
            self.name, self.version, len(self.bones), len(self.slots),
            self.canvas.width, self.canvas.height,
        )


# ---------------------------------------------------------------------------
# Pose
# ---------------------------------------------------------------------------


class Delta(NamedTuple):
    """A bone's DELTA FROM REST. Never an absolute.

    dx, dy   pixels, in the PARENT bone's space (see the module docstring).
    drot     degrees, added to the bone's LOCAL rest rotation.
    scale    MULTIPLIER on rest scale. Identity is 1.0, not 0.0.

    The mixed identity is deliberate and is the one asymmetry in this file:
    translation and rotation compose additively, scale composes
    multiplicatively, so `Delta()` is the true no-op in both.
    """

    dx: float = 0.0
    dy: float = 0.0
    drot: float = 0.0
    scale: float = 1.0

    @property
    def is_identity(self) -> bool:
        return (self.dx, self.dy, self.drot, self.scale) == (0.0, 0.0, 0.0, 1.0)


IDENTITY_DELTA = Delta()


@dataclass(frozen=True)
class Pose:
    """Sparse `{bone: Delta}` deltas from rest. Immutable.

    Sparse is the point: a clip touching `flipper_L` leaves the other 22 bones
    absent from the map, so another channel's clip can be merged in without
    either one having an opinion about the other's bones.
    """

    deltas: Mapping[str, Delta] = field(default_factory=dict)

    @staticmethod
    def empty() -> "Pose":
        return Pose({})

    @staticmethod
    def of(mapping: Mapping[str, Delta | Sequence[float]]) -> "Pose":
        out: dict[str, Delta] = {}
        for k, v in mapping.items():
            out[k] = v if isinstance(v, Delta) else Delta(*v)
        return Pose(out)

    def get(self, bone: str) -> Delta:
        return self.deltas.get(bone, IDENTITY_DELTA)

    def bones(self) -> tuple[str, ...]:
        return tuple(sorted(self.deltas))

    def merge_disjoint(self, other: "Pose", *, who: str = "pose") -> "Pose":
        """Union of two poses whose bone sets MUST NOT overlap.

        Raises on overlap rather than picking a winner. The contract's
        precedence rule (FACE > HEAD > BASE) only has work to do when sets
        overlap, and `ChannelMap` guarantees they never do — so an overlap here
        means a clip wrote outside its channel, which is a load error that got
        past `Clip` somehow. Refusing beats silently becoming order-dependent.
        """
        clash = set(self.deltas) & set(other.deltas)
        if clash:
            raise RigError(
                "%s merge is not disjoint — %d bone(s) written twice: %s. "
                "Blending would become order-dependent and the bug invisible "
                "until two clips coincide." % (who, len(clash), sorted(clash))
            )
        merged = dict(self.deltas)
        merged.update(other.deltas)
        return Pose(merged)

    def close_to(self, other: "Pose", tol: float = 1e-9) -> bool:
        """Tolerant compare. Pass tol=0.0 to demand bit-identical deltas."""
        keys = set(self.deltas) | set(other.deltas)
        for k in keys:
            a, b = self.get(k), other.get(k)
            if tol == 0.0:
                if a != b:
                    return False
            elif any(abs(x - y) > tol for x, y in zip(a, b)):
                return False
        return True

    def diff(self, other: "Pose", tol: float = 1e-9) -> tuple[str, ...]:
        """Bones where two poses disagree. For test failure messages."""
        keys = sorted(set(self.deltas) | set(other.deltas))
        return tuple(
            k for k in keys
            if any(abs(x - y) > tol for x, y in zip(self.get(k), other.get(k)))
        )

    def __len__(self) -> int:
        return len(self.deltas)

    def __repr__(self) -> str:
        return "Pose(%s)" % ", ".join(
            "%s=%s" % (k, tuple(round(v, 4) for v in self.deltas[k]))
            for k in sorted(self.deltas)
        )


class SlotDraw(NamedTuple):
    """What layer B needs to draw one slot, and nothing more.

    `matrix` is the world matrix of the slot's BONE. A slot has no transform of
    its own; it inherits its bone's, which is exactly why a skin swap cannot
    move anything.
    """

    slot: str
    bone: str
    z: int
    matrix: Mat2D
    attachments: tuple[str, ...] = ()


class PoseResult(NamedTuple):
    """The whole output of posing. `clamps` is empty on a clean pose."""

    world: Mapping[str, Mat2D]
    local: Mapping[str, Mat2D]
    clamps: tuple[ClampEvent, ...]

    def point(self, bone: str) -> tuple[float, float]:
        return self.world[bone].origin


# ---------------------------------------------------------------------------
# Injectable maths — exists for the CONTROL in selftest.py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransformOps:
    """The three decisions the module docstring makes, as swappable functions.

    THIS EXISTS FOR ONE REASON: `selftest.py` substitutes deliberately WRONG
    maths here and asserts the fixtures reject it. Production code never passes
    this argument; `DEFAULT_OPS` is the contract's composition.

    A test that merely asserts the right answer cannot tell a discriminating
    fixture from a fixture that would accept anything. Being able to run the
    wrong implementation is what turns the four tests into a control.
    """

    #: `(parent_world, local) -> child_world`. Contract: parent @ local.
    compose: Callable[[Mat2D, Mat2D], Mat2D] = lambda p, l: p.mul(l)
    #: `(tx, ty, rot_deg, scale) -> local`. Contract: translate @ rotate @ scale.
    local_matrix: Callable[[float, float, float, float], Mat2D] = (
        lambda tx, ty, rot, s: Mat2D.translate(tx, ty)
        .mul(Mat2D.rotate_deg(rot))
        .mul(Mat2D.scale(s))
    )
    #: `(parent_rest_world, abs_x, abs_y) -> local rest translation`.
    #: Contract: invert the parent, so rest world == authored absolute.
    derive_local: Callable[[Mat2D, float, float], tuple[float, float]] = (
        lambda pw, x, y: pw.inverse().apply((x, y))
    )
    #: True == pose deltas accumulate across calls instead of recomputing from
    #: rest. Always False in production; the loop test exists to catch True.
    accumulate: bool = False
    name: str = "contract"


DEFAULT_OPS = TransformOps()


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


class Rig:
    """Poses a `Skeleton`. Stateless with respect to time; pure per call.

    Every `pose_world` call recomputes from REST. Nothing accumulates. That is
    what makes "a loop played twice returns to its exact starting pose" true by
    construction, and the test for it exists to prove the construction rather
    than to discover it.
    """

    def __init__(
        self,
        skeleton: Skeleton,
        *,
        debug: bool = False,
        policy: Optional[ConstraintPolicy] = None,
        ops: TransformOps = DEFAULT_OPS,
        clamp_sink: Optional[Callable[[ClampEvent], None]] = None,
    ) -> None:
        self.skeleton = skeleton
        self.debug = bool(debug)
        self.policy = policy or ConstraintPolicy()
        self.ops = ops
        self._clamp_sink = clamp_sink or self._default_clamp_sink
        self._accum: dict[str, Delta] = {}

        # Rest local translations, derived by inverting each parent's rest
        # world matrix. Computed once; bones are already topologically sorted.
        self._rest_local: dict[str, Mat2D] = {}
        self._rest_world: dict[str, Mat2D] = {}
        self._rest_local_t: dict[str, tuple[float, float]] = {}
        for b in skeleton.bones:
            if b.parent is None:
                pw = Mat2D.identity()
            else:
                pw = self._rest_world[b.parent]
            tx, ty = ops.derive_local(pw, b.rest_x, b.rest_y)
            local = ops.local_matrix(tx, ty, b.rest_rot, b.rest_scale)
            self._rest_local_t[b.name] = (tx, ty)
            self._rest_local[b.name] = local
            self._rest_world[b.name] = ops.compose(pw, local)

    # -- rest --------------------------------------------------------------
    def rest_world(self) -> Mapping[str, Mat2D]:
        return dict(self._rest_world)

    def rest_local_translation(self, bone: str) -> tuple[float, float]:
        return self._rest_local_t[bone]

    def check_rest_positions(self, tol: float = 1e-6) -> tuple[str, ...]:
        """Bones whose rest world origin != their authored absolute x/y.

        Empty tuple is the pass. This is the single most discriminating check
        in the module: it fails for a wrong composition order, for treating the
        authored coordinates as parent-relative, and for ignoring the parent's
        rest rotation when deriving the local offset.
        """
        bad: list[str] = []
        for b in self.skeleton.bones:
            wx, wy = self._rest_world[b.name].origin
            if abs(wx - b.rest_x) > tol or abs(wy - b.rest_y) > tol:
                bad.append(b.name)
        return tuple(bad)

    # -- clamping ----------------------------------------------------------
    def _default_clamp_sink(self, ev: ClampEvent) -> None:
        if self.debug:
            print("[pico.rig] " + ev.describe(), file=sys.stderr)

    def clamp_rotation(self, bone: str, rest_rot: float, drot: float
                       ) -> tuple[float, Optional[ClampEvent]]:
        """Clamp a bone's rotation. Returns `(allowed_drot, event_or_None)`.

        Clamps the LOCAL TOTAL by default; see `ConstraintPolicy` for the other
        reading and why this one was chosen.
        """
        lim = self.skeleton.constraint_for(bone, self.policy)
        if lim is None:
            return drot, None
        limit_name, lo, hi = lim
        if self.policy.clamp_local_total:
            want = rest_rot + drot
            got = min(hi, max(lo, want))
            if got == want:
                return drot, None
            return got - rest_rot, ClampEvent(bone, limit_name, want, got, lo, hi)
        want = drot
        got = min(hi, max(lo, want))
        if got == want:
            return drot, None
        return got, ClampEvent(bone, limit_name, want, got, lo, hi)

    # -- posing ------------------------------------------------------------
    def pose_world(self, pose: Optional[Pose] = None) -> PoseResult:
        """Compose world matrices for every bone from REST plus `pose`.

        `pose` deltas are FROM REST: dx/dy in the parent's space, drot added to
        the bone's local rest rotation, scale multiplied into rest scale.

        NOTE THE SIGNATURE: there is no skin, no attachment and no art
        parameter anywhere in it. That absence is the animation/skin separation
        the contract asks for, and `tests/test_gate.py` asserts it by
        inspecting this signature rather than trusting the prose.
        """
        p = pose or Pose.empty()
        ops = self.ops
        sk = self.skeleton

        if ops.accumulate:  # never in production; the loop test hunts for this
            for name, d in p.deltas.items():
                prev = self._accum.get(name, IDENTITY_DELTA)
                self._accum[name] = Delta(
                    prev.dx + d.dx, prev.dy + d.dy,
                    prev.drot + d.drot, prev.scale * d.scale,
                )
            effective: Mapping[str, Delta] = dict(self._accum)
        else:
            effective = p.deltas

        world: dict[str, Mat2D] = {}
        local: dict[str, Mat2D] = {}
        clamps: list[ClampEvent] = []

        for b in sk.bones:
            d = effective.get(b.name, IDENTITY_DELTA)
            drot, ev = self.clamp_rotation(b.name, b.rest_rot, d.drot)
            if ev is not None:
                clamps.append(ev)
                self._clamp_sink(ev)

            rtx, rty = self._rest_local_t[b.name]
            lm = ops.local_matrix(
                rtx + d.dx,
                rty + d.dy,
                b.rest_rot + drot,
                b.rest_scale * d.scale,
            )
            pw = Mat2D.identity() if b.parent is None else world[b.parent]
            local[b.name] = lm
            world[b.name] = ops.compose(pw, lm)

        return PoseResult(world=world, local=local, clamps=tuple(clamps))

    def reset_accumulator(self) -> None:
        """Only meaningful for the deliberately-broken accumulating ops."""
        self._accum.clear()

    def bone_points(self, pose: Optional[Pose] = None) -> Mapping[str, tuple[float, float]]:
        """`{bone: (world_x, world_y)}`. The cheap read for tests and debug."""
        return {k: m.origin for k, m in self.pose_world(pose).world.items()}

    def slot_transforms(
        self,
        pose: Optional[Pose] = None,
        attachments: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> tuple[SlotDraw, ...]:
        """Every slot with its bone's world matrix, sorted by z ascending.

        `attachments` is `{slot_name: [art_id, ...]}` — a SKIN, opaque to this
        layer. It selects what layer B draws and CANNOT influence any matrix:
        the matrices come from `pose_world`, which never sees it. That is the
        structural guarantee behind "a skin swap moves zero bones", and
        `test_gate.py` checks it by passing two different skins and demanding
        identical matrices.

        An `attachments` key that is not a slot in this skeleton raises, rather
        than being silently ignored.
        """
        result = self.pose_world(pose)
        att = dict(attachments or {})
        unknown = sorted(k for k in att if k not in {s.name for s in self.skeleton.slots})
        if unknown:
            raise RigError(
                "attachment map names %d slot(s) this skeleton does not have: "
                "%s — a load error, not a silent skip"
                % (len(unknown), ", ".join(unknown))
            )
        return tuple(
            SlotDraw(
                slot=s.name,
                bone=s.bone,
                z=s.z,
                matrix=result.world[s.bone],
                attachments=tuple(att.get(s.name, ())),
            )
            for s in self.skeleton.slots
        )

    def __repr__(self) -> str:
        return "<Rig %r ops=%s debug=%s>" % (
            self.skeleton.name, self.ops.name, self.debug)


# ---------------------------------------------------------------------------
# Rig data provenance
# ---------------------------------------------------------------------------

RIGDATA = Path(__file__).resolve().parent / "rigdata"


def default_skeleton_path() -> Path:
    return RIGDATA / "pico_skeleton.json"


def verify_skeleton_provenance() -> tuple[str, str]:
    """`(verdict, detail)` where verdict is PASS / FAIL / CANNOT-TELL.

    `pico_skeleton.json` is the stable naming contract and lives in the rig
    pack. This tool ships a byte copy so it can run without the pack, and a
    copy is a fork waiting to drift — which is the exact failure mode
    PICO_CONTRACT.md section 2 was written about.

    So: the copy carries a sha256 in `rigdata/PROVENANCE.json`, and this
    re-hashes both. If the original pack is not on disk it returns
    CANNOT-TELL, never PASS. An unreachable source is not agreement.
    """
    prov_path = RIGDATA / "PROVENANCE.json"
    if not prov_path.exists():
        return ("FAIL", "no PROVENANCE.json beside the skeleton copy")
    prov = json.loads(prov_path.read_text(encoding="utf-8"))
    local = default_skeleton_path()
    if not local.exists():
        return ("FAIL", "PROVENANCE.json present but %s is missing" % local.name)
    local_hash = hashlib.sha256(local.read_bytes()).hexdigest()
    if local_hash != prov.get("sha256"):
        return ("FAIL",
                "local copy %s does not match its own recorded sha256 %s — the "
                "shipped skeleton was edited in place"
                % (local_hash[:12], str(prov.get("sha256"))[:12]))
    src = Path(prov.get("source", ""))
    if not src.exists():
        return ("CANNOT-TELL",
                "copy is self-consistent (sha256 %s) but the upstream source "
                "%s is not on this disk, so drift from the rig pack cannot be "
                "ruled out" % (local_hash[:12], src))
    src_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    if src_hash != local_hash:
        return ("FAIL",
                "UPSTREAM HAS DRIFTED: pack sha256 %s != shipped copy %s. The pack's "
                "file is the authority; re-copy it."
                % (src_hash[:12], local_hash[:12]))
    return ("PASS", "shipped copy is byte-identical to %s" % src)
