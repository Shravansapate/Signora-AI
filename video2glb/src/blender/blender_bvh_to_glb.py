"""Blender script: BVH → GLB direct export.

This script is executed by Blender in background mode:

    blender --background --python src/blender/blender_bvh_to_glb.py -- \\
        --bvh path/to/input.bvh \\
        --avatar path/to/character.fbx \\
        --bone-map path/to/avatar_bone_map.json \\
        --output path/to/output.glb

It:
1. Imports the avatar FBX.
2. Parses the BVH file (using the bundled bvh_reader module via sys.path).
3. Builds a BVH-to-avatar bone mapping using the canonical map.
4. Creates a Blender action by converting BVH Euler rotations to quaternions.
5. Sets root translation from the BVH root position channels.
6. Bakes the animation and exports a GLB.

All existing retargeting helpers (blender_utils, normalize_export_skin_weights, etc.)
are reused without modification.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# Ensure the project root is on the Python path so src.* imports work.
_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector
import numpy as np

from src.blender.blender_utils import (
    export_avatar_glb,
    import_avatar,
    isolate_target_animation,
    purge_orphans,
    reset_scene,
    suspend_mesh_deformation,
    restore_mesh_deformation,
    normalize_export_skin_weights,
)
from src.bvh.bvh_reader import (
    BVHData,
    parse_bvh,
    build_bvh_bone_mapping,
    validate_bvh_skeleton,
)
from src.motion.arm_ik import solve_two_bone_endpoint


# ---------------------------------------------------------------------------
# Euler → Quaternion helper (respects BVH channel order)
# ---------------------------------------------------------------------------

_BLENDER_ORDER_MAP: dict[str, str] = {
    "XYZ": "XYZ", "XZY": "XZY",
    "YXZ": "YXZ", "YZX": "YZX",
    "ZXY": "ZXY", "ZYX": "ZYX",
}


def _euler_to_quat(angles_deg: tuple[float, float, float], order: str) -> Quaternion:
    """Convert BVH channel-ordered Euler values to a Blender quaternion."""
    blender_order = _BLENDER_ORDER_MAP.get(order.upper(), "ZXY")
    combined = Matrix.Identity(4)
    for axis, angle_deg in zip(blender_order, angles_deg):
        combined = combined @ Matrix.Rotation(math.radians(angle_deg), 4, axis)
    return combined.to_quaternion()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = _parse_args()

    bvh_path = Path(args.bvh)
    avatar_path = Path(args.avatar)
    bone_map_path = Path(args.bone_map)
    output_path = Path(args.output)

    if not bvh_path.is_file():
        print(f"[ERROR] BVH file not found: {bvh_path}", flush=True)
        return 1
    if not avatar_path.is_file():
        print(f"[ERROR] Avatar FBX not found: {avatar_path}", flush=True)
        return 1
    if not bone_map_path.is_file():
        print(f"[ERROR] Bone map not found: {bone_map_path}", flush=True)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Parse BVH
    # ------------------------------------------------------------------
    print(f"[BVH] Parsing: {bvh_path.name}", flush=True)
    bvh = parse_bvh(bvh_path)
    print(
        f"[BVH] {len(bvh.joints)} joints, {bvh.frame_count} frames @ {bvh.fps:.4f} fps",
        flush=True,
    )

    # ------------------------------------------------------------------
    # 2. Load bone map and validate skeleton compatibility
    # ------------------------------------------------------------------
    raw_map_payload = json.loads(bone_map_path.read_text(encoding="utf-8"))
    canonical_to_avatar: dict[str, str] = raw_map_payload.get("map", {})
    direct_bvh_map: dict[str, str] = raw_map_payload.get("bvh_map", {})
    if not canonical_to_avatar:
        print(f"[ERROR] Bone map has no 'map' entries: {bone_map_path}", flush=True)
        return 1

    issues = validate_bvh_skeleton(bvh, canonical_to_avatar)
    if issues:
        for issue in issues:
            print(f"[WARN] {issue}", flush=True)
        if any("missing canonical joints" in i for i in issues):
            print("[ERROR] BVH skeleton incompatible with avatar — aborting.", flush=True)
            return 1

    bvh_to_avatar = build_bvh_bone_mapping(
        bvh,
        canonical_to_avatar,
        direct_map=direct_bvh_map,
    )
    if not bvh_to_avatar:
        print("[ERROR] No BVH joints could be mapped to avatar bones.", flush=True)
        return 1
    print(f"[BVH] Mapped {len(bvh_to_avatar)} BVH joints to avatar bones.", flush=True)

    # ------------------------------------------------------------------
    # 3. Import avatar into Blender
    # ------------------------------------------------------------------
    reset_scene()
    purge_orphans()
    armature = import_avatar(str(avatar_path))
    print(f"[BVH] Armature: {armature.name}", flush=True)
    skin_weight_report = normalize_export_skin_weights(armature)
    mesh_states = suspend_mesh_deformation(armature)

    # ------------------------------------------------------------------
    # 4. Validate avatar bones exist in the armature
    # ------------------------------------------------------------------
    avatar_pose_bones = set(armature.pose.bones.keys())
    missing_in_armature = [
        (bvh_name, av_name)
        for bvh_name, av_name in bvh_to_avatar.items()
        if av_name not in avatar_pose_bones
    ]
    if missing_in_armature:
        print(
            f"[WARN] These mapped avatar bones are absent from the armature: "
            f"{[av for _, av in missing_in_armature]}",
            flush=True,
        )
        # Remove invalid mappings rather than aborting
        for bvh_name, _ in missing_in_armature:
            bvh_to_avatar.pop(bvh_name, None)

    # ------------------------------------------------------------------
    # 5. Set scene FPS
    # ------------------------------------------------------------------
    fps = bvh.fps
    scene = bpy.context.scene
    scene.render.fps = max(1, int(round(fps)))
    scene.render.fps_base = scene.render.fps / max(fps, 1e-8)
    scene.frame_start = 0
    scene.frame_end = max(0, bvh.frame_count - 1)
    print(f"[BVH] Scene FPS set to {scene.render.fps} (exact: {fps:.6f})", flush=True)

    # ------------------------------------------------------------------
    # 6. Build Blender action from BVH channels
    # ------------------------------------------------------------------
    action_name = bvh_path.stem
    action = bpy.data.actions.new(action_name)
    armature.animation_data_create()
    armature.animation_data.action = action

    # Index BVH joints by name for fast lookup. Target bone matrices carry
    # arbitrary roll/rest axes, so raw BVH Euler values cannot be assigned to
    # rotation_quaternion directly. We solve source global rotations and apply
    # them before each target bone's own rest orientation.
    bvh_joint_index: dict[str, int] = {j.name: i for i, j in enumerate(bvh.joints)}
    target_rest_rotations = {
        target_name: armature.data.bones[target_name].matrix_local.to_3x3().normalized()
        for target_name in bvh_to_avatar.values()
    }
    previous_quaternions: dict[str, Quaternion] = {}

    # Process root translation separately
    root_joint = bvh.joints[0] if bvh.joints else None
    has_root_translation = root_joint is not None and any(
        "position" in ch.lower() for ch in root_joint.channels
    )

    for frame_idx in range(bvh.frame_count):
        blender_frame = frame_idx
        bpy.context.scene.frame_set(blender_frame)

        source_global_rotations: list[Matrix] = []
        source_positions: list[Vector] = []
        for joint in bvh.joints:
            rot_indices = [
                joint.channel_offset + i
                for i, channel in enumerate(joint.channels)
                if "rotation" in channel.lower()
            ]
            rot_channels = [
                channel for channel in joint.channels
                if "rotation" in channel.lower()
            ]
            if len(rot_indices) == 3:
                values = tuple(float(bvh.frames[frame_idx, i]) for i in rot_indices)
                order = "".join(channel[0].upper() for channel in rot_channels)
                local_rotation = _euler_to_quat(values, order).to_matrix()
            else:
                local_rotation = Matrix.Identity(3)
            if joint.parent_index < 0:
                global_rotation = local_rotation
                translation = bvh.get_root_translation()
                source_position = (
                    Vector(tuple(float(value) for value in translation[frame_idx]))
                    if translation is not None
                    else Vector((0.0, 0.0, 0.0))
                )
            else:
                global_rotation = (
                    source_global_rotations[joint.parent_index] @ local_rotation
                )
                source_position = (
                    source_positions[joint.parent_index]
                    + source_global_rotations[joint.parent_index]
                    @ Vector(joint.offset)
                )
            source_global_rotations.append(global_rotation)
            source_positions.append(source_position)

        # Root translation (location keyframe on armature object)
        if has_root_translation and args.apply_root_translation:
            root_trans = bvh.get_root_translation()
            if root_trans is not None:
                tx, ty, tz = root_trans[frame_idx]
                # BVH Y-up → Blender Z-up: remap (X, Y, Z) → (X, Z, -Y)
                # Scale from cm to meters if configured (BVH is typically in cm)
                scale = args.bvh_scale
                armature.location = Vector((tx * scale, tz * scale, -ty * scale))
                armature.keyframe_insert(data_path="location", frame=blender_frame)

        # Assign parent-first. Blender must evaluate each parent before a child
        # matrix is solved, otherwise identical held BVH frames incorrectly
        # converge toward the pose over several output frames.
        for bvh_name, avatar_bone_name in bvh_to_avatar.items():
            source_index = bvh_joint_index[bvh_name]
            pose_bone = armature.pose.bones.get(avatar_bone_name)
            if pose_bone is None:
                continue
            desired_rotation = (
                source_global_rotations[source_index]
                @ target_rest_rotations[avatar_bone_name]
            )
            desired_matrix = desired_rotation.to_4x4()
            desired_matrix.translation = pose_bone.head.copy()
            pose_bone.rotation_mode = "QUATERNION"
            pose_bone.matrix = desired_matrix
            bpy.context.view_layer.update()
            quat = pose_bone.rotation_quaternion.copy()
            previous = previous_quaternions.get(avatar_bone_name)
            if previous is not None and previous.dot(quat) < 0.0:
                quat.negate()
            previous_quaternions[avatar_bone_name] = quat.copy()
            pose_bone.rotation_quaternion = quat
            pose_bone.keyframe_insert(data_path="rotation_quaternion", frame=blender_frame)

        if not args.disable_hand_contact:
            _preserve_bilateral_thumb_contact(
                armature,
                bvh,
                bvh_to_avatar,
                bvh_joint_index,
                source_positions,
                source_global_rotations,
                blender_frame,
            )

        bpy.context.view_layer.update()

    # ------------------------------------------------------------------
    # 7. Isolate and validate
    # ------------------------------------------------------------------
    bpy.context.view_layer.update()
    isolate_target_animation(armature, action)
    restore_mesh_deformation(mesh_states)

    if not action.fcurves:
        print("[ERROR] No animation curves were written — check bone mapping.", flush=True)
        return 1

    for curve in action.fcurves:
        for keyframe in curve.keyframe_points:
            keyframe.interpolation = "LINEAR"

    print(
        f"[BVH] Action '{action_name}': {len(action.fcurves)} fcurves, "
        f"{bvh.frame_count} frames.",
        flush=True,
    )

    # ------------------------------------------------------------------
    # 8. Export GLB
    # ------------------------------------------------------------------
    print(f"[BVH] Exporting GLB → {output_path}", flush=True)
    export_avatar_glb(str(output_path), armature)

    if not output_path.is_file() or output_path.stat().st_size < 1000:
        print("[ERROR] GLB export produced an empty or missing file.", flush=True)
        return 1

    size_kb = output_path.stat().st_size / 1024
    print(f"[BVH] SUCCESS — {output_path.name} ({size_kb:.1f} KB)", flush=True)
    return 0


def _preserve_bilateral_thumb_contact(
    armature,
    bvh: BVHData,
    bvh_to_avatar: dict[str, str],
    joint_index: dict[str, int],
    source_positions: list[Vector],
    source_global_rotations: list[Matrix],
    frame: int,
) -> None:
    """Close source-evidenced thumb contact with length-preserving arm IK.

    BVH limb rotations can put the source thumb tips together while avatar
    proportion differences leave a visible gap. This correction activates only
    when the source's two thumb tips are already within 10% of shoulder width.
    It translates each rigid hand by half the residual and re-solves both arms,
    preserving the hand and finger rotations from the BVH.
    """
    required_source = (
        "LeftUpperArm", "RightUpperArm",
        "LeftThumbDistal", "RightThumbDistal",
    )
    required_targets = (
        "LeftUpperArm", "LeftLowerArm", "LeftHand", "LeftThumbDistal",
        "RightUpperArm", "RightLowerArm", "RightHand", "RightThumbDistal",
    )
    if any(name not in joint_index for name in required_source):
        return
    if any(name not in bvh_to_avatar for name in required_targets):
        return

    def source_tip(name: str) -> Vector:
        index = joint_index[name]
        joint = bvh.joints[index]
        # These captures use an End Site offset equal to the distal bone's
        # offset. Using it here retains the contact evidence discarded by a
        # channel-only BVH parser without inventing a new finger pose.
        return source_positions[index] + source_global_rotations[index] @ Vector(joint.offset)

    left_source_tip = source_tip("LeftThumbDistal")
    right_source_tip = source_tip("RightThumbDistal")
    shoulder_width = (
        source_positions[joint_index["LeftUpperArm"]]
        - source_positions[joint_index["RightUpperArm"]]
    ).length
    if shoulder_width <= 1e-8:
        return
    source_ratio = (left_source_tip - right_source_tip).length / shoulder_width
    activation_ratio = 0.10
    full_contact_ratio = 0.08
    if source_ratio >= activation_ratio:
        return
    weight = min(
        1.0,
        max(0.0, (activation_ratio - source_ratio) / (activation_ratio - full_contact_ratio)),
    )

    left_thumb = armature.pose.bones[bvh_to_avatar["LeftThumbDistal"]]
    right_thumb = armature.pose.bones[bvh_to_avatar["RightThumbDistal"]]
    left_tip = _terminal_bone_tip(left_thumb)
    right_tip = _terminal_bone_tip(right_thumb)
    tip_delta = right_tip - left_tip
    target_shoulder_width = (
        armature.pose.bones[bvh_to_avatar["LeftUpperArm"]].head
        - armature.pose.bones[bvh_to_avatar["RightUpperArm"]].head
    ).length
    # Skeleton endpoints do not quite reach the skinned thumb surface on this
    # avatar. A bounded 0.75%-of-shoulder-width shift per hand closes that last
    # visible seam without changing the recorded finger articulation.
    surface_closure = (
        tip_delta.normalized() * (target_shoulder_width * 0.0075 * weight)
        if tip_delta.length > 1e-8
        else Vector((0.0, 0.0, 0.0))
    )
    correction = tip_delta * (0.5 * weight) + surface_closure
    if correction.length <= 1e-7:
        return

    _move_wrist_with_arm_ik(armature, bvh_to_avatar, "Left", correction, frame)
    _move_wrist_with_arm_ik(armature, bvh_to_avatar, "Right", -correction, frame)


def _terminal_bone_tip(pose_bone) -> Vector:
    terminal = pose_bone
    while len(terminal.children) == 1 and "thumb" in terminal.children[0].name.casefold():
        terminal = terminal.children[0]
    return terminal.tail.copy()


def _move_wrist_with_arm_ik(
    armature,
    bvh_to_avatar: dict[str, str],
    side: str,
    shift: Vector,
    frame: int,
) -> None:
    source_names = {
        "Left": ("LeftUpperArm", "LeftLowerArm", "LeftHand"),
        "Right": ("RightUpperArm", "RightLowerArm", "RightHand"),
    }[side]
    upper, forearm, hand = (
        armature.pose.bones[bvh_to_avatar[name]] for name in source_names
    )
    shoulder, elbow, wrist = upper.head.copy(), forearm.head.copy(), hand.head.copy()
    upper_matrix, forearm_matrix, hand_matrix = (
        bone.matrix.copy() for bone in (upper, forearm, hand)
    )
    result = solve_two_bone_endpoint(
        np.asarray(shoulder),
        np.asarray(elbow),
        np.asarray(wrist),
        np.asarray(wrist + shift),
        (elbow - shoulder).length,
        (wrist - elbow).length,
    )
    desired_elbow = Vector(result["elbow"])
    desired_wrist = Vector(result["wrist"])

    upper_swing = (elbow - shoulder).rotation_difference(desired_elbow - shoulder)
    target = (upper_swing.to_matrix() @ upper_matrix.to_3x3()).to_4x4()
    target.translation = shoulder
    upper.matrix = target
    bpy.context.view_layer.update()

    forearm_swing = (wrist - elbow).rotation_difference(desired_wrist - desired_elbow)
    target = (forearm_swing.to_matrix() @ forearm_matrix.to_3x3()).to_4x4()
    target.translation = forearm.head.copy()
    forearm.matrix = target
    bpy.context.view_layer.update()

    hand_matrix.translation = hand.head.copy()
    hand.matrix = hand_matrix
    bpy.context.view_layer.update()
    for bone in (upper, forearm, hand):
        bone.rotation_mode = "QUATERNION"
        bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    # Blender passes its own args before '--'; everything after belongs to our script.
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Convert a BVH motion file to a GLB via Blender retargeting."
    )
    parser.add_argument("--bvh", required=True, help="Input .bvh file path.")
    parser.add_argument("--avatar", required=True, help="Character FBX path.")
    parser.add_argument("--bone-map", required=True, help="avatar_bone_map.json path.")
    parser.add_argument("--output", required=True, help="Output .glb file path.")
    parser.add_argument(
        "--apply-root-translation",
        action="store_true",
        default=False,
        help="Apply BVH root position channels to the armature location.",
    )
    parser.add_argument(
        "--bvh-scale",
        type=float,
        default=0.01,
        help="Scale factor applied to BVH translation values (default: 0.01 = cm→m).",
    )
    parser.add_argument(
        "--disable-hand-contact",
        action="store_true",
        help="Do not close bilateral thumb contact that is present in the source BVH.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
