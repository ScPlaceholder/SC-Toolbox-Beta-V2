"""Transform maths, skeleton loading, poses, constraints, slots."""

from __future__ import annotations

import math

import pytest

from pico.rig import (
    ConstraintPolicy,
    Delta,
    Mat2D,
    Pose,
    Rig,
    RigError,
    Skeleton,
    verify_skeleton_provenance,
)


# ---------------------------------------------------------------------------
# Mat2D
# ---------------------------------------------------------------------------


def test_identity_is_a_no_op():
    p = (137.0, -42.5)
    assert Mat2D.identity().apply(p) == p


def test_multiplication_applies_the_right_operand_first():
    t = Mat2D.translate(10, 0)
    s = Mat2D.scale(2)
    # t @ s: scale first, then translate -> (2*3 + 10, 2*0)
    assert t.mul(s).apply((3, 0)) == pytest.approx((16.0, 0.0))
    # s @ t: translate first, then scale -> (2*(3+10), 0)
    assert s.mul(t).apply((3, 0)) == pytest.approx((26.0, 0.0))


def test_matmul_operator_matches_mul():
    a = Mat2D.translate(3, 4).mul(Mat2D.rotate_deg(17))
    b = Mat2D.translate(3, 4) @ Mat2D.rotate_deg(17)
    assert a == b


def test_positive_rotation_is_clockwise_on_screen():
    """+y is DOWN, so a positive angle must move +x toward +y."""
    x, y = Mat2D.rotate_deg(90).apply((1.0, 0.0))
    assert x == pytest.approx(0.0, abs=1e-12)
    assert y == pytest.approx(1.0)  # downward on screen


def test_inverse_round_trips():
    m = Mat2D.translate(101, -7).mul(Mat2D.rotate_deg(31)).mul(Mat2D.scale(1.75))
    p = (12.0, 34.0)
    back = m.inverse().apply(m.apply(p))
    assert back == pytest.approx(p)


def test_inverse_of_a_collapsed_matrix_is_refused_by_name():
    with pytest.raises(RigError, match="singular"):
        Mat2D.scale(0.0).inverse()


def test_rotation_deg_reads_back_accumulated_rotation():
    m = Mat2D.rotate_deg(20).mul(Mat2D.rotate_deg(15))
    assert m.rotation_deg == pytest.approx(35.0)


# ---------------------------------------------------------------------------
# Skeleton loading
# ---------------------------------------------------------------------------


def test_the_shipped_skeleton_is_the_rig_packs_shape(skeleton: Skeleton):
    assert skeleton.name == "Pico_Master"
    assert skeleton.version == 1
    assert len(skeleton.bones) == 23
    assert len(skeleton.slots) == 26
    assert (skeleton.canvas.width, skeleton.canvas.height) == (2048, 2048)
    assert skeleton.canvas.origin == "top-left"
    assert skeleton.root == "root"


def test_shipped_copy_matches_the_rig_pack_or_says_cannot_tell():
    verdict, detail = verify_skeleton_provenance()
    assert verdict in ("PASS", "CANNOT-TELL"), detail


def test_bones_come_back_parents_before_children(skeleton: Skeleton):
    seen: set[str] = set()
    for b in skeleton.bones:
        if b.parent is not None:
            assert b.parent in seen, "%s precedes its parent %s" % (b.name, b.parent)
        seen.add(b.name)


def test_slots_are_sorted_by_z_and_are_not_bones(skeleton: Skeleton):
    zs = [s.z for s in skeleton.slots]
    assert zs == sorted(zs)
    assert skeleton.slots[0].name == "shadow"
    assert skeleton.slots[-1].name == "fx_front"
    # A slot name that collides with a bone name is legal and common here;
    # what matters is that they are different namespaces.
    assert "jacket_front" not in skeleton.bone_names
    assert skeleton.slot("jacket_front").bone == "body"


def test_constraints_keep_the_deliberate_flipper_asymmetry(skeleton: Skeleton):
    assert skeleton.constraints.head == (-18.0, 18.0)
    assert skeleton.constraints.body == (-12.0, 12.0)
    assert skeleton.constraints.foot == (-18.0, 18.0)
    assert skeleton.constraints.flipper == (-55.0, 70.0), (
        "the flipper range is asymmetric on purpose; do not tidy it")
    assert skeleton.constraints.dashboard_boundary


def test_a_missing_parent_is_named(skeleton_variant):
    with pytest.raises(RigError, match="nowhere"):
        skeleton_variant(lambda d: d["bones"].append(
            {"name": "ghost", "parent": "nowhere", "x": 0, "y": 0}))


def test_a_cycle_is_named(skeleton_variant):
    def mutate(d):
        for b in d["bones"]:
            if b["name"] == "body":
                b["parent"] = "head"
    with pytest.raises(RigError, match="cycle"):
        skeleton_variant(mutate)


def test_two_roots_are_refused(skeleton_variant):
    with pytest.raises(RigError, match="parentless"):
        skeleton_variant(lambda d: d["bones"].append(
            {"name": "second_root", "parent": None, "x": 0, "y": 0}))


def test_a_slot_on_an_unknown_bone_is_a_load_error_not_a_silent_skip(skeleton_variant):
    with pytest.raises(RigError, match="silent skip"):
        skeleton_variant(lambda d: d["slots"].append(
            {"name": "phantom", "bone": "no_such_bone", "z": 99}))


def test_a_non_top_left_origin_is_refused_rather_than_guessed(skeleton_variant):
    with pytest.raises(RigError, match="Refusing to guess"):
        skeleton_variant(lambda d: d["canvas"].update({"origin": "centre"}))


def test_require_bones_names_every_missing_one(skeleton: Skeleton):
    skeleton.require_bones(["head", "flipper_L"])  # fine
    with pytest.raises(RigError) as exc:
        skeleton.require_bones(["head", "jacket_L", "collar"], what="DRAKE skin")
    msg = str(exc.value)
    assert "DRAKE skin" in msg and "jacket_L" in msg and "collar" in msg


def test_rest_scale_defaults_to_one_because_the_rig_pack_has_no_scale_field(
        skeleton: Skeleton):
    assert all(b.rest_scale == 1.0 for b in skeleton.bones)


# ---------------------------------------------------------------------------
# Rest pose: the single most discriminating invariant in the module
# ---------------------------------------------------------------------------


def test_every_bone_rests_at_its_authored_absolute_coordinate(rig: Rig):
    assert rig.check_rest_positions() == ()


def test_the_rest_check_can_actually_fail(skeleton: Skeleton):
    """A check that reports an absence must be shown to see the thing present.

    Derive the local offset the naive way — authored child minus authored
    parent, ignoring the parent's rest rotation — and the invariant must break
    for exactly the two bones whose parent is rotated.
    """
    from pico.rig import TransformOps
    naive = TransformOps(derive_local=lambda pw, x, y: (x - pw.tx, y - pw.ty))
    bad = Rig(skeleton, ops=naive).check_rest_positions()
    assert set(bad) == {"hand_L", "hand_R"}, (
        "ignoring the flippers' 12-degree rest rotation should displace exactly "
        "the two hands, got %s" % (bad,))


def test_the_parents_rest_rotation_is_folded_into_the_local_offset(rig: Rig):
    """hand_L's local offset is NOT simply (child - parent) in canvas space."""
    tx, ty = rig.rest_local_translation("hand_L")
    naive = (590.0 - 650.0, 1280.0 - 1110.0)
    assert (tx, ty) != pytest.approx(naive)
    # It is that offset rotated back through the flipper's +12 degrees.
    expect = Mat2D.rotate_deg(-12.0).apply(naive)
    assert (tx, ty) == pytest.approx(expect, abs=1e-9)


def test_root_rests_at_the_canvas_anchor(rig: Rig):
    assert rig.rest_world()["root"].origin == pytest.approx((1024.0, 1650.0))


# ---------------------------------------------------------------------------
# Poses
# ---------------------------------------------------------------------------


def test_delta_identity_is_zero_zero_zero_one():
    assert Delta().is_identity
    assert Delta(scale=1.0).is_identity
    assert not Delta(scale=0.0).is_identity, (
        "scale composes multiplicatively; 0.0 is not the identity")


def test_an_empty_pose_reproduces_rest(rig: Rig):
    a = rig.pose_world(None).world
    b = rig.pose_world(Pose.empty()).world
    c = rig.rest_world()
    for bone in a:
        assert a[bone] == b[bone] == c[bone]


def test_a_pose_is_sparse_and_leaves_untouched_bones_at_rest(rig: Rig):
    rest = rig.rest_world()
    posed = rig.pose_world(Pose.of({"flipper_L": Delta(drot=20)})).world
    assert posed["flipper_L"] != rest["flipper_L"]
    assert posed["hand_L"] != rest["hand_L"], "a child must follow its parent"
    for bone in ("flipper_R", "hand_R", "head", "foot_L", "body"):
        assert posed[bone] == rest[bone], "%s moved and should not have" % bone


def test_pose_dx_dy_are_in_the_parents_space(rig: Rig):
    """Stated in the module docstring; asserted here so it cannot drift.

    `root` has no parent, so its dx/dy are canvas pixels. Rotate root, and a
    child's identical dy then travels along root's rotated axis instead of
    straight down the canvas.
    """
    only_dy = rig.pose_world(Pose.of({"body": Delta(dy=100)})).world["body"].origin
    rest = rig.rest_world()["body"].origin
    assert only_dy == pytest.approx((rest[0], rest[1] + 100.0))

    turned = rig.pose_world(
        Pose.of({"root": Delta(drot=90), "body": Delta(dy=100)})).world["body"]
    root = rig.rest_world()["root"].origin
    # body's local offset (0, -440) plus dy 100 -> (0, -340), rotated 90 deg
    # clockwise on screen about root: (+340, 0).
    assert turned.origin == pytest.approx((root[0] + 340.0, root[1]), abs=1e-9)


def test_merge_disjoint_refuses_to_pick_a_winner():
    a = Pose.of({"head": Delta(drot=3)})
    b = Pose.of({"head": Delta(drot=-3), "beak": Delta(dy=1)})
    with pytest.raises(RigError, match="order-dependent"):
        a.merge_disjoint(b)


def test_merge_disjoint_is_a_union():
    a = Pose.of({"head": Delta(drot=3)})
    b = Pose.of({"beak": Delta(dy=1)})
    m = a.merge_disjoint(b)
    assert m.bones() == ("beak", "head")
    assert m.get("head") == Delta(drot=3)
    assert m.get("visor").is_identity, "an absent bone reads as the identity"


def test_pose_close_to_with_zero_tolerance_demands_exactness():
    a = Pose.of({"head": Delta(drot=1.0)})
    b = Pose.of({"head": Delta(drot=1.0 + 1e-15)})
    assert a.close_to(b, tol=1e-9)
    assert not a.close_to(b, tol=0.0)


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_clamping_the_local_total_uses_rest_rotation(rig: Rig):
    # flipper_L rests at +12, so +58 of delta reaches the +70 ceiling exactly.
    got, ev = rig.clamp_rotation("flipper_L", 12.0, 58.0)
    assert ev is None and got == pytest.approx(58.0)
    got, ev = rig.clamp_rotation("flipper_L", 12.0, 59.0)
    assert ev is not None
    assert 12.0 + got == pytest.approx(70.0)
    assert ev.requested_deg == pytest.approx(71.0)


def test_the_asymmetric_lower_bound_is_minus_fiftyfive_not_minus_seventy(rig: Rig):
    got, ev = rig.clamp_rotation("flipper_R", -12.0, -43.0)
    assert ev is None, "a local total of -55 is legal"
    got, ev = rig.clamp_rotation("flipper_R", -12.0, -58.0)
    assert ev is not None and -12.0 + got == pytest.approx(-55.0)


def test_an_unconstrained_bone_is_not_clamped(rig: Rig):
    got, ev = rig.clamp_rotation("hand_L", 0.0, 900.0)
    assert (got, ev) == (900.0, None)


def test_fourteen_of_twentythree_bones_carry_no_rotation_limit(skeleton: Skeleton):
    """Recorded as a fact, not asserted as good. Reported to J as OPEN."""
    free = skeleton.unconstrained_bones()
    assert len(free) == 14
    assert {"hand_L", "hand_R", "shoulder_L", "shoulder_R", "hip_L", "hip_R"} <= set(free)


def test_the_delta_clamping_policy_is_available_without_a_code_edit(skeleton: Skeleton):
    r = Rig(skeleton, policy=ConstraintPolicy(clamp_local_total=False))
    got, ev = r.clamp_rotation("flipper_L", 12.0, 70.0)
    assert ev is None, "clamping the delta alone permits a local total of +82"
    got, ev = r.clamp_rotation("flipper_L", 12.0, 71.0)
    assert ev is not None and got == pytest.approx(70.0)


def test_clamping_is_loud_in_debug(skeleton: Skeleton):
    heard = []
    r = Rig(skeleton, debug=True, clamp_sink=heard.append)
    res = r.pose_world(Pose.of({"head": Delta(drot=90)}))
    assert len(res.clamps) == 1 and len(heard) == 1
    ev = res.clamps[0]
    assert ev.bone == "head" and ev.limit == "head"
    assert "head" in ev.describe() and "18" in ev.describe()


def test_a_clamped_pose_still_produces_finite_transforms(rig: Rig):
    res = rig.pose_world(Pose.of({"flipper_L": Delta(drot=4000)}))
    assert res.clamps
    for m in res.world.values():
        assert all(math.isfinite(v) for v in m)


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


def test_slot_transforms_are_z_ordered_and_carry_their_bones_matrix(rig: Rig):
    pose = Pose.of({"head": Delta(drot=5)})
    world = rig.pose_world(pose).world
    draws = rig.slot_transforms(pose)
    assert [d.z for d in draws] == sorted(d.z for d in draws)
    for d in draws:
        assert d.matrix == world[d.bone]


def test_an_attachment_map_cannot_name_an_unknown_slot(rig: Rig):
    with pytest.raises(RigError, match="silent skip"):
        rig.slot_transforms(None, {"not_a_slot": ["art"]})


def test_attachments_reach_the_draw_list_and_nothing_else(rig: Rig):
    a = rig.slot_transforms(None, {"headwear": ["drake_cap"]})
    b = rig.slot_transforms(None, {"headwear": ["origin_visor_hat", "extra"]})
    by_slot_a = {d.slot: d for d in a}
    by_slot_b = {d.slot: d for d in b}
    assert by_slot_a["headwear"].attachments == ("drake_cap",)
    assert by_slot_b["headwear"].attachments == ("origin_visor_hat", "extra")
    for slot in by_slot_a:
        assert by_slot_a[slot].matrix == by_slot_b[slot].matrix


def test_multiple_slots_can_share_one_bone(skeleton: Skeleton):
    on_body = [s.name for s in skeleton.slots if s.bone == "body"]
    assert len(on_body) >= 5, on_body
