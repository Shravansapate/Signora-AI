from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.blender.blender_utils import (  # noqa: E402
    export_avatar_glb,
    import_avatar,
    isolate_target_animation,
    purge_orphans,
    reset_scene,
    validate_animation_state,
    suspend_mesh_deformation,
    restore_mesh_deformation,
    normalize_export_skin_weights,
)


FINGERS = ("Thumb", "Index", "Middle", "Ring", "Little")


def main() -> int:
    args = parse_args()
    reset_scene()
    purge_orphans()
    armature = import_avatar(args.avatar)
    skin_weight_report = normalize_export_skin_weights(armature)
    # Rig solving only reads bones. Avoid reevaluating every skinned vertex for
    # each finger/IK update; restore all modifier states before mesh export.
    mesh_states = suspend_mesh_deformation(armature)
    motion = np.load(args.motion)
    fps = float(motion["fps"])
    frame_count = int(motion["frame_count"])
    avatar_bone_names = [str(name) for name in motion["avatar_bone_names"]]
    rotations = motion["rotations"].astype(float)
    root_translation = motion["root_translation"].astype(float)
    action_name = str(motion["action_name"])
    ik_targets = motion["ik_targets"].astype(float) if args.use_ik and "ik_targets" in motion.files else None
    finger_directions = motion["finger_directions"].astype(float) if args.use_finger_tracking and "finger_directions" in motion.files else None
    finger_direction_valid = motion["finger_direction_constraint_valid"].astype(bool) if finger_directions is not None and "finger_direction_constraint_valid" in motion.files else None
    if finger_direction_valid is None and finger_directions is not None and "finger_direction_valid" in motion.files:
        finger_direction_valid = motion["finger_direction_valid"].astype(bool)
    finger_direction_influence = motion["finger_direction_influence"].astype(float) if finger_directions is not None and "finger_direction_influence" in motion.files else None
    palm_basis = motion["palm_basis"].astype(float) if args.use_palm_tracking and "palm_basis" in motion.files else None
    palm_basis_valid = motion["palm_basis_valid"].astype(bool) if palm_basis is not None and "palm_basis_valid" in motion.files else None
    palm_basis_influence = motion["palm_basis_influence"].astype(float) if palm_basis is not None and "palm_basis_influence" in motion.files else None
    neutral_wrist_weights = motion["neutral_wrist_weights"].astype(float) if "neutral_wrist_weights" in motion.files else np.zeros((frame_count, 2))
    neutral_finger_weights = motion["neutral_finger_weights"].astype(float) if "neutral_finger_weights" in motion.files else np.zeros((frame_count, 2))

    scene = bpy.context.scene
    scene.render.fps = max(1, int(round(fps)))
    scene.render.fps_base = scene.render.fps / max(fps, 1e-8)
    scene.frame_start = 1
    scene.frame_end = frame_count
    action = bpy.data.actions.new(action_name)
    armature.animation_data_create()
    armature.animation_data.action = action
    bone_map = {
        str(canonical): str(avatar)
        for canonical, avatar in zip(motion["bone_names"], motion["avatar_bone_names"])
    }
    missing = [name for name in avatar_bone_names if name not in armature.pose.bones]
    if missing:
        raise RuntimeError(f"Required animated avatar bones are missing: {missing}")
    ik_bones = {bone_map.get(f"{side}{part}") for side in ("Left", "Right") for part in ("UpperArm", "ForeArm")}
    ik_objects, ik_constraints = setup_arm_ik(armature, ik_targets, bone_map) if ik_targets is not None else ({}, {})
    ik_calibration = calibrate_arm_poles(armature, ik_objects, ik_constraints, ik_targets) if ik_targets is not None else {}
    ik_calibration["skin_weight_preparation"] = skin_weight_report
    finger_objects, finger_constraints = setup_finger_tracking(armature, finger_directions, bone_map) if finger_directions is not None else ({}, {})
    for details in finger_constraints.values():
        details["maximum_rotation_step"] = float(np.radians(24.0) * 25.0 / fps)
        details["continuity_corrections"] = []
    if "neutral_finger_palm_rotations" in motion.files:
        neutral_frames = np.asarray(motion["neutral_finger_palm_rotations"], dtype=float)
        if neutral_frames.shape != (2, 5, 3, 4) or not np.isfinite(neutral_frames).all():
            raise RuntimeError("Invalid neutral palm-space finger rotations.")
        for side_index, side in enumerate(("Left", "Right")):
            if side in finger_constraints:
                for finger_index, segments in finger_constraints[side]["fingers"].items():
                    for joint_index, segment in enumerate(segments):
                        segment["neutral_palm_rotation"] = Quaternion(neutral_frames[side_index, finger_index, joint_index])
    palm_tracking = setup_palm_tracking(armature, bone_map) if palm_basis is not None else {}

    for frame_index in range(frame_count):
        blender_frame = frame_index + 1
        bpy.context.scene.frame_set(blender_frame)
        if ik_targets is not None:
            update_ik_targets(armature, ik_objects, ik_targets[frame_index], blender_frame)
        for bone_index, bone_name in enumerate(avatar_bone_names):
            if ik_targets is not None and bone_name in ik_bones:
                continue
            pose_bone = armature.pose.bones[bone_name]
            pose_bone.rotation_mode = "QUATERNION"
            w, x, y, z = rotations[frame_index, bone_index]
            if not np.isfinite([w, x, y, z]).all():
                raise RuntimeError(f"Non-finite quaternion for {bone_name} frame {frame_index}.")
            pose_bone.rotation_quaternion = Quaternion((float(w), float(x), float(y), float(z)))
            pose_bone.keyframe_insert(data_path="rotation_quaternion", frame=blender_frame)

        if args.apply_root_translation:
            armature.location = Vector(root_translation[frame_index])
            armature.keyframe_insert(data_path="location", frame=blender_frame)
        bpy.context.view_layer.update()
        update_neutral_wrist_orientation(armature, bone_map, neutral_wrist_weights[frame_index], blender_frame)
        if palm_basis is not None:
            update_palm_orientation(
                armature,
                palm_tracking,
                palm_basis[frame_index],
                palm_basis_valid[frame_index] if palm_basis_valid is not None else None,
                palm_basis_influence[frame_index] if palm_basis_influence is not None else None,
                blender_frame,
            )
            bpy.context.view_layer.update()
        if finger_directions is not None:
            update_finger_targets(
                armature,
                finger_objects,
                finger_constraints,
                finger_directions[frame_index],
                finger_direction_valid[frame_index] if finger_direction_valid is not None else None,
                finger_direction_influence[frame_index] if finger_direction_influence is not None else None,
                blender_frame,
                neutral_finger_weights[frame_index],
            )

    bpy.context.view_layer.update()
    if ik_targets is not None or finger_directions is not None:
        bpy.ops.object.select_all(action="DESELECT")
        armature.select_set(True)
        bpy.context.view_layer.objects.active = armature
        bpy.ops.nla.bake(
            frame_start=1,
            frame_end=frame_count,
            only_selected=False,
            visual_keying=True,
            clear_constraints=True,
            clear_parents=False,
            use_current_action=True,
            bake_types={"POSE"},
        )
        for obj in [*ik_objects.values(), *finger_objects.values()]:
            bpy.data.objects.remove(obj, do_unlink=True)
    if ik_targets is not None:
        ik_calibration["arm_temporal_conditioning"] = condition_baked_arm_rotations(armature, bone_map, frame_count)
    enforce_baked_hand_quaternion_continuity(armature, bone_map, frame_count)
    isolate_target_animation(armature, action)
    validate_animation_state(armature, avatar_bone_names)
    restore_mesh_deformation(mesh_states)
    corrections = [record for details in finger_constraints.values() for record in details.get("continuity_corrections", [])]
    ik_calibration["finger_continuity_correction"] = {
        "status": "REVIEW" if corrections else "PASS",
        "reasons": ["Finger recovery rotations were rate-limited; compare affected transitions with the source."] if corrections else [],
        "corrected_sample_count": len(corrections),
        "maximum_target_rotation_error_degrees": max((record["target_error_degrees"] for record in corrections), default=0.0),
        "sample_corrections": corrections[:32],
        "reference_fps": 25.0, "maximum_step_degrees_at_reference_fps": 24.0,
    }
    planned_returns = [record for details in finger_constraints.values()
                       for record in details.get("planned_neutral_transitions", [])]
    ik_calibration["planned_neutral_transition"] = {
        "status": "REVIEW" if planned_returns else "NOT_APPLIED",
        "method": "verified_anchored_monotonic_palm_local_slerp",
        "sample_count": len(planned_returns),
        "maximum_step_degrees": max((record["step_degrees"] for record in planned_returns), default=0.0),
        "maximum_previous_plan_error_degrees": max((record["previous_plan_error_degrees"] for record in planned_returns), default=0.0),
        "sample_transitions": planned_returns[:32],
        "live_tracking_step_limit_unchanged": True,
        "post_export_validation_required": True,
    }
    if ik_targets is not None and finger_directions is not None:
        from src.blender.mesh_contact import correct_baked_torso_contacts
        ik_calibration["mesh_contact_correction"] = correct_baked_torso_contacts(armature, bone_map, frame_count, fps)
        mesh_states = suspend_mesh_deformation(armature)
        enforce_baked_hand_quaternion_continuity(armature, bone_map, frame_count)
        restore_mesh_deformation(mesh_states)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    export_avatar_glb(args.output, armature)
    if args.ik_report:
        Path(args.ik_report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.ik_report).write_text(json.dumps(ik_calibration, indent=2), encoding="utf-8")
    return 0


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--avatar", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--bone-map", required=True)
    parser.add_argument("--motion", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply-root-translation", action="store_true")
    parser.add_argument("--use-ik", action="store_true", help="Use body-relative wrist targets with avatar-length arm IK.")
    parser.add_argument("--use-finger-tracking", action="store_true", help="Use local hand-world finger directions with constrained Blender tracking.")
    parser.add_argument("--use-palm-tracking", action="store_true", help="Apply the full tracked palm basis after arm IK.")
    parser.add_argument("--ik-report", help="Optional JSON report containing per-clip arm IK pole calibration.")
    return parser.parse_args(argv)


def condition_baked_arm_rotations(armature, mapping, count):
    from src.motion.smoothing import smooth_arm_rotation_spikes
    names = [side + part for side in ("Left", "Right") for part in ("UpperArm", "ForeArm")]
    bones = [armature.pose.bones[mapping[name]] for name in names]
    hands = [armature.pose.bones[mapping[side + "Hand"]] for side in ("Left", "Right")]
    values = np.empty((count, 4, 4))
    palm_rotations, wrists = [], []
    for index in range(count):
        bpy.context.scene.frame_set(index + 1)
        values[index] = [tuple(bone.rotation_quaternion) for bone in bones]
        palm_rotations.append([hand.matrix.to_quaternion().copy() for hand in hands])
        wrists.append([hand.head.copy() for hand in hands])
    conditioned, report = smooth_arm_rotation_spikes(values)
    maximum_shift = 0.0
    for index in range(1, count - 1):
        if np.allclose(conditioned[index], values[index], atol=1e-8, rtol=0):
            continue
        bpy.context.scene.frame_set(index + 1)
        for bone, quaternion in zip(bones, conditioned[index]):
            bone.rotation_quaternion = Quaternion(quaternion)
            bone.keyframe_insert(data_path="rotation_quaternion", frame=index + 1)
        bpy.context.view_layer.update()
        for side, hand in enumerate(hands):
            maximum_shift = max(maximum_shift, float((hand.head - wrists[index][side]).length))
            matrix = palm_rotations[index][side].to_matrix().to_4x4()
            matrix.translation = hand.head.copy()
            hand.matrix = matrix
            hand.keyframe_insert(data_path="rotation_quaternion", frame=index + 1)
        bpy.context.view_layer.update()
    report.update(channels=names, maximum_wrist_shift_avatar_units=maximum_shift,
                  finger_and_palm_orientations_preserved=True, arm_lengths_preserved=True)
    return report


def setup_arm_ik(armature, ik_targets, bone_map: dict[str, str]):
    objects = {}
    constraints = {}
    if ik_targets is None:
        return objects, constraints
    for name in ["LeftWrist", "LeftElbow", "RightWrist", "RightElbow"]:
        bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0, 0, 0))
        empty = bpy.context.object
        empty.name = f"IK_{name}"
        empty.empty_display_size = 3.0
        objects[name] = empty

    left_forearm = bone_map.get("LeftForeArm", "")
    left_constraint = add_ik_constraint(
        armature,
        left_forearm,
        objects["LeftWrist"],
        objects["LeftElbow"],
        pole_angle=0.0,
    )
    constraints["Left"] = {"constraint": left_constraint, "forearm_name": left_forearm, "elbow_target_index": 1}
    right_forearm = bone_map.get("RightForeArm", "")
    right_constraint = add_ik_constraint(
        armature,
        right_forearm,
        objects["RightWrist"],
        objects["RightElbow"],
        pole_angle=0.0,
    )
    constraints["Right"] = {"constraint": right_constraint, "forearm_name": right_forearm, "elbow_target_index": 3}
    return objects, constraints


def add_ik_constraint(armature, bone_name: str, target, pole, pole_angle: float):
    if bone_name not in armature.pose.bones:
        return None
    pose_bone = armature.pose.bones[bone_name]
    constraint = pose_bone.constraints.new(type="IK")
    constraint.name = "VideoIK"
    constraint.target = target
    constraint.pole_target = pole
    constraint.pole_angle = pole_angle
    constraint.chain_count = 2
    # The target represents the wrist position.  The tail of a forearm is the
    # wrist; its head is the elbow.  Solving the head collapses the hand into
    # the torso, so the IK end effector must be the tail.
    constraint.use_tail = True
    constraint.use_rotation = False
    return constraint


def calibrate_arm_poles(armature, objects: dict, constraints: dict, targets: np.ndarray) -> dict:
    """Find the rig pole angles that keep elbows closest to tracked elbows."""
    if not constraints:
        return {}

    sample_count = min(72, len(targets))
    sample_indices = np.unique(np.linspace(0, len(targets) - 1, sample_count, dtype=int))
    candidate_angles = np.linspace(-np.pi, np.pi, 33)
    result: dict[str, dict] = {}

    for side, details in constraints.items():
        constraint = details["constraint"]
        forearm_name = details["forearm_name"]
        elbow_target_index = details["elbow_target_index"]
        if constraint is None:
            continue
        if forearm_name not in armature.pose.bones:
            continue
        candidate_scores: list[tuple[float, float]] = []
        for angle in candidate_angles:
            constraint.pole_angle = float(angle)
            errors: list[float] = []
            for frame_index in sample_indices:
                bpy.context.scene.frame_set(int(frame_index) + 1)
                _set_ik_target_locations(armature, objects, targets[frame_index])
                bpy.context.view_layer.update()
                actual = armature.matrix_world @ armature.pose.bones[forearm_name].head
                expected = armature.matrix_world @ Vector(targets[frame_index, elbow_target_index])
                errors.append(float((actual - expected).length))
            candidate_scores.append((float(np.mean(errors)), float(angle)))

        mean_error, best_angle = min(candidate_scores, key=lambda item: item[0])
        constraint.pole_angle = best_angle
        result[side] = {
            "pole_angle_radians": best_angle,
            "sample_count": int(len(sample_indices)),
            "mean_elbow_target_error": mean_error,
        }

    bpy.context.scene.frame_set(1)
    _set_ik_target_locations(armature, objects, targets[0])
    bpy.context.view_layer.update()
    return result


def update_ik_targets(armature, objects: dict, targets, frame: int) -> None:
    _set_ik_target_locations(armature, objects, targets)
    for obj in objects.values():
        obj.keyframe_insert(data_path="location", frame=frame)


def _set_ik_target_locations(armature, objects: dict, targets) -> None:
    mapping = {
        "LeftWrist": 0,
        "LeftElbow": 1,
        "RightWrist": 2,
        "RightElbow": 3,
    }
    for name, index in mapping.items():
        obj = objects[name]
        obj.location = armature.matrix_world @ Vector(targets[index])


def setup_finger_tracking(armature, finger_directions: np.ndarray, bone_map: dict[str, str]):
    """Prepare direct palm-local swing transport for all finger joints.

    A direction alone cannot specify axial twist. Transport the last solved
    frame instead of independently Damped-Tracking from a curled base each
    frame, which can choose opposite twists around nearly reversed segments.
    """
    objects: dict[str, bpy.types.Object] = {}
    constraints: dict[str, dict] = {}
    if finger_directions is None:
        return objects, constraints

    for side in ("Left", "Right"):
        hand_name = bone_map.get(f"{side}Hand", "")
        canonical_finger_names = [f"{side}{finger}{segment}" for finger in FINGERS for segment in range(1, 4)]
        if hand_name not in armature.pose.bones or any(bone_map.get(name, "") not in armature.pose.bones for name in canonical_finger_names):
            raise RuntimeError(f"Cannot retarget {side} fingers: incomplete avatar mapping.")
        axes_local = _hand_rest_palm_axes_local(armature, hand_name, bone_map, side)
        if axes_local is None:
            raise RuntimeError(f"Cannot retarget {side} fingers: degenerate rest palm basis.")
        side_constraints: dict[str, dict] = {"hand_name": hand_name, "axes_local": axes_local, "fingers": {}}
        for finger_index, finger in enumerate(FINGERS):
            segments: list[dict] = []
            for segment_index in range(3):
                canonical = f"{side}{finger}{segment_index + 1}"
                bone_name = bone_map[canonical]
                segments.append({"bone_name": bone_name, "previous_palm_rotation": None, "previous_final_rotation": None})
            side_constraints["fingers"][finger_index] = segments
        constraints[side] = side_constraints
    return objects, constraints


def setup_palm_tracking(armature, bone_map: dict[str, str]) -> dict:
    result: dict[str, dict] = {}
    for side in ("Left", "Right"):
        hand_name = bone_map.get(f"{side}Hand", "")
        required = [hand_name, bone_map.get(f"{side}Index1", ""), bone_map.get(f"{side}Middle1", ""), bone_map.get(f"{side}Little1", "")]
        if any(name not in armature.data.bones for name in required):
            continue
        hand = armature.data.bones[hand_name]
        index = armature.data.bones[bone_map[f"{side}Index1"]]
        middle = armature.data.bones[bone_map[f"{side}Middle1"]]
        little = armature.data.bones[bone_map[f"{side}Little1"]]
        across = _vector_or_none(index.head_local - little.head_local)
        forward = _vector_or_none(middle.head_local - hand.head_local)
        if across is None or forward is None:
            continue
        normal = _vector_or_none(across.cross(forward))
        if normal is None:
            continue
        forward = _vector_or_none(normal.cross(across))
        if forward is None:
            continue
        rest_basis = Matrix((across, forward, normal)).transposed()
        rest_rotation = hand.matrix_local.to_3x3().normalized()
        result[side] = {
            "bone_name": hand_name,
            "palm_basis_in_bone": rest_rotation.inverted() @ rest_basis,
            "previous_local_quaternion": None,
        }
    return result


def update_neutral_wrist_orientation(armature, bone_map: dict[str, str], weights: np.ndarray, frame: int) -> None:
    """Align fallback palms in the evaluated IK frame, not an assumed arm roll."""
    from src.motion.neutral_hand import anatomical_palm_normal

    center = armature.pose.bones[bone_map["Hips"]].head
    for side_index, side in enumerate(("Left", "Right")):
        weight = float(weights[side_index])
        if not np.isfinite(weight) or not 0.0 <= weight <= 1.0:
            raise RuntimeError("Invalid neutral wrist activation.")
        if weight <= 0.0:
            continue
        hand = armature.pose.bones[bone_map[f"{side}Hand"]]
        index = armature.pose.bones[bone_map[f"{side}Index1"]].head
        middle = armature.pose.bones[bone_map[f"{side}Middle1"]].head
        little = armature.pose.bones[bone_map[f"{side}Little1"]].head
        forward = (middle - hand.head).normalized()
        normal = Vector(anatomical_palm_normal(np.asarray(index - little), np.asarray(forward), side))
        inward = center - hand.head
        inward -= forward * inward.dot(forward)
        if inward.length < 1e-8:
            raise RuntimeError(f"Cannot align {side} neutral palm in degenerate posed frame.")
        inward.normalize()
        angle = float(np.arctan2(forward.dot(normal.cross(inward)), normal.dot(inward)))
        rotation = Quaternion(forward, angle * weight) @ hand.matrix.to_quaternion()
        matrix = rotation.to_matrix().to_4x4()
        matrix.translation = hand.head.copy()
        hand.matrix = matrix
        hand.rotation_mode = "QUATERNION"
        hand.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        bpy.context.view_layer.update()


def update_palm_orientation(
    armature,
    tracking: dict,
    bases: np.ndarray,
    valid: np.ndarray | None,
    influences: np.ndarray | None,
    frame: int,
) -> None:
    for side_index, side in enumerate(("Left", "Right")):
        details = tracking.get(side)
        if details is None:
            continue
        basis_is_finite = bool(np.isfinite(bases[side_index]).all())
        is_valid = bool(valid[side_index]) if valid is not None else basis_is_finite
        # Conditioned motion files hold the last finite palm basis while the
        # influence fades after tracking disappears.  Do not reject those
        # deliberately unobserved fade frames solely because valid is false.
        influence = float(influences[side_index]) if influences is not None else float(is_valid)
        if not basis_is_finite or influence <= 0.0:
            continue
        source_basis = Matrix(np.asarray(bases[side_index], dtype=float).tolist())
        if abs(source_basis.determinant()) < 1e-6:
            continue
        target_rotation = (source_basis @ details["palm_basis_in_bone"].inverted()).to_quaternion()
        pose_bone = armature.pose.bones[details["bone_name"]]
        current_rotation = pose_bone.matrix.to_quaternion()
        blended = current_rotation.slerp(target_rotation, min(1.0, max(0.0, influence)))
        target_matrix = blended.to_matrix().to_4x4()
        target_matrix.translation = pose_bone.head.copy()
        pose_bone.matrix = target_matrix
        pose_bone.rotation_mode = "QUATERNION"
        local_rotation = pose_bone.rotation_quaternion.copy()
        previous_local = details.get("previous_local_quaternion")
        if previous_local is not None and previous_local.dot(local_rotation) < 0.0:
            local_rotation.negate()
            pose_bone.rotation_quaternion = local_rotation
        details["previous_local_quaternion"] = local_rotation.copy()
        pose_bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)


def enforce_baked_hand_quaternion_continuity(armature, bone_map: dict[str, str], frame_count: int) -> None:
    """Keep wrists and every finger joint in one hemisphere after constraint baking."""
    previous: dict[str, Quaternion] = {}
    for frame in range(1, frame_count + 1):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        for canonical, bone_name in bone_map.items():
            if not (canonical.endswith("Hand") or any(finger in canonical for finger in FINGERS)):
                continue
            if bone_name not in armature.pose.bones:
                continue
            pose_bone = armature.pose.bones[bone_name]
            pose_bone.rotation_mode = "QUATERNION"
            rotation = pose_bone.rotation_quaternion.copy()
            prior = previous.get(canonical)
            if prior is not None and prior.dot(rotation) < 0.0:
                rotation.negate()
                pose_bone.rotation_quaternion = rotation
            pose_bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)
            previous[canonical] = rotation.copy()


def update_finger_targets(
    armature,
    objects: dict,
    constraints: dict,
    directions: np.ndarray,
    valid: np.ndarray | None,
    influences: np.ndarray | None,
    frame: int,
    neutral_weights: np.ndarray | None = None,
) -> None:
    for side_index, side in enumerate(("Left", "Right")):
        details = constraints.get(side)
        if details is None:
            continue
        hand_pose = armature.pose.bones[details["hand_name"]]
        hand_rotation = hand_pose.matrix.to_quaternion()
        axes_local = details["axes_local"]
        # Snapshot the whole fallback chain before solving any parent joint.
        # Otherwise a distal fade is repeatedly rotated by its already blended
        # ancestors and can jump even while the tracked direction is constant.
        bases = {segment["bone_name"]: armature.pose.bones[segment["bone_name"]].matrix.to_quaternion()
                 for segments in details["fingers"].values() for segment in segments}

        for finger_index, segments in details["fingers"].items():
            for segment_index, segment in enumerate(segments):
                neutral_weight = float(neutral_weights[side_index]) if neutral_weights is not None else 0.0
                neutral_rotation = segment.get("neutral_palm_rotation")
                previous_weight = segment.get("previous_neutral_weight", 0.0)

                # Seed previous_palm_rotation from neutral_rotation when available,
                # either on initial frame or immediately when neutral release occurs.
                if previous_weight > 0.0 and neutral_weight <= 0.0 and neutral_rotation is not None:
                    segment["previous_palm_rotation"] = neutral_rotation.copy()

                is_valid = bool(valid[side_index, finger_index, segment_index]) if valid is not None else np.isfinite(directions[side_index, finger_index, segment_index]).all()
                influence = float(influences[side_index, finger_index, segment_index]) if influences is not None else float(is_valid)
                if not np.isfinite(influence) or not 0.0 <= influence <= 1.0:
                    raise RuntimeError(f"Invalid {side} finger influence at frame {frame}.")
                direction_is_finite = bool(np.isfinite(directions[side_index, finger_index, segment_index]).all())
                if not direction_is_finite:
                    influence = 0.0
                bpy.context.view_layer.update()
                bone = armature.pose.bones[segment["bone_name"]]
                base = bases[segment["bone_name"]]
                prior_final = segment["previous_final_rotation"]
                if prior_final is not None and (neutral_weights is None or neutral_weights[side_index] <= 0.0):
                    # Missing interior observations hold the last solved hand
                    # shape, rather than reverting to an unrelated curl pose.
                    base = hand_rotation @ prior_final
                if not direction_is_finite or influence <= 0.0:
                    if segment["previous_palm_rotation"] is None:
                        segment["previous_palm_rotation"] = (
                            neutral_rotation.copy() if neutral_rotation is not None
                            else (hand_rotation.inverted() @ base).normalized()
                        )
                    final_rotation = base
                else:
                    source_direction = Vector(directions[side_index, finger_index, segment_index])
                    local_direction = sum((axes_local[axis] * source_direction[axis] for axis in range(3)), Vector((0.0, 0.0, 0.0)))
                    if local_direction.length < 1e-8:
                        raise RuntimeError(f"Degenerate {side} finger direction at frame {frame}.")
                    previous = segment["previous_palm_rotation"]
                    if previous is None:
                        previous = (
                            neutral_rotation.copy() if neutral_rotation is not None
                            else (hand_rotation.inverted() @ base).normalized()
                        )
                    current_direction = previous @ Vector((0.0, 1.0, 0.0))
                    swing = current_direction.rotation_difference(local_direction.normalized())
                    transported = (swing @ previous).normalized()
                    segment["previous_palm_rotation"] = transported
                    target_rotation = hand_rotation @ transported
                    final_rotation = base.slerp(target_rotation, influence)

                if neutral_weight > 0.0 and neutral_rotation is not None:
                    # Blend entire evaluated orientations in one palm frame.
                    # Blending each local ancestor independently compounds into
                    # a much larger distal turn during the neutral transition.
                    tracked_rotation = segment["previous_palm_rotation"]
                    if neutral_weight > previous_weight and segment.get("neutral_exit_anchor") is None:
                        segment["neutral_exit_anchor"] = (prior_final or tracked_rotation).copy()
                    if neutral_weight < previous_weight:
                        segment["neutral_exit_anchor"] = None
                    if segment.get("neutral_exit_anchor") is not None:
                        tracked_rotation = segment["neutral_exit_anchor"]
                    final_rotation = hand_rotation @ tracked_rotation.slerp(neutral_rotation, neutral_weight)
                else:
                    segment["neutral_exit_anchor"] = None

                segment["previous_neutral_weight"] = neutral_weight
                final_local = (hand_rotation.inverted() @ final_rotation).normalized()
                if prior_final is not None:
                    angle = 2.0 * float(np.arccos(np.clip(abs(prior_final.dot(final_local)), 0.0, 1.0)))
                    maximum = details.get("maximum_rotation_step", np.radians(24.0))

                    anchor = segment.get("neutral_exit_anchor")
                    planned_return = False
                    weight_step = neutral_weight - previous_weight
                    # An anchored ending return already follows one continuous
                    # shortest arc. Re-limiting each sample makes that scheduled
                    # return miss its declared fully-neutral endpoint. Exempt
                    # only an intact monotonic plan, never a sudden jump, an
                    # initial fade, or a source-tracking/recovery rotation.
                    if anchor is not None and neutral_rotation is not None and 0.0 < weight_step <= 0.25 + 1e-7:
                        prior_planned = anchor.slerp(neutral_rotation, previous_weight)
                        prior_error = 2.0 * float(np.arccos(np.clip(abs(prior_final.dot(prior_planned)), 0.0, 1.0)))
                        full_angle = 2.0 * float(np.arccos(np.clip(abs(anchor.dot(neutral_rotation)), 0.0, 1.0)))
                        planned_step = full_angle * weight_step
                        planned_return = prior_error <= np.radians(0.1) and angle <= planned_step + np.radians(0.1)
                        if planned_return:
                            details.setdefault("planned_neutral_transitions", []).append({
                                "frame": frame, "side": side, "bone": segment["bone_name"],
                                "weight": neutral_weight, "step_degrees": float(np.degrees(angle)),
                                "previous_plan_error_degrees": float(np.degrees(prior_error)),
                            })
                    if angle > maximum and not planned_return:
                        final_local = prior_final.slerp(final_local, maximum / angle).normalized()
                        final_rotation = hand_rotation @ final_local
                        details.setdefault("continuity_corrections", []).append({
                            "frame": frame, "side": side, "bone": segment["bone_name"],
                            "target_error_degrees": float(np.degrees(angle - maximum)),
                        })
                segment["previous_final_rotation"] = final_local
                matrix = final_rotation.to_matrix().to_4x4()
                matrix.translation = bone.head.copy()
                bone.matrix = matrix
                bone.rotation_mode = "QUATERNION"
                bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                bpy.context.view_layer.update()


def _hand_rest_palm_axes_local(armature, hand_name: str, bone_map: dict[str, str], side: str):
    hand = armature.data.bones[hand_name]
    index = armature.data.bones[bone_map[f"{side}Index1"]]
    middle = armature.data.bones[bone_map[f"{side}Middle1"]]
    little = armature.data.bones[bone_map[f"{side}Little1"]]
    inverse = hand.matrix_local.inverted()
    wrist_local = inverse @ hand.head_local
    index_local = inverse @ index.head_local
    middle_local = inverse @ middle.head_local
    little_local = inverse @ little.head_local
    across = _vector_or_none(index_local - little_local)
    forward = _vector_or_none(middle_local - wrist_local)
    if across is None or forward is None:
        return None
    normal = _vector_or_none(across.cross(forward))
    if normal is None:
        return None
    forward = _vector_or_none(normal.cross(across))
    if forward is None:
        return None
    return (across, forward, normal)


def _vector_or_none(vector):
    result = Vector(vector)
    if not np.isfinite(result[:]).all() or result.length < 1e-8:
        return None
    return result.normalized()


if __name__ == "__main__":
    raise SystemExit(main())
